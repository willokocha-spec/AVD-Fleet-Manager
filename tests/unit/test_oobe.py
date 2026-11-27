"""Unit tests for ``avdfleet.oobe`` — detection, verify, reset, matrix.

Everything here runs against in-process fakes: an injected adb runner (returns
deterministic ``dumpsys``/``getprop``/``screencap`` output), a fake runtime
(state machine + snapshots), a fake AVD manager and an in-memory image
catalogue.  No Android SDK, emulator or network access is required.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from avdfleet.core.errors import NotFoundError, OobeError, ValidationError
from avdfleet.core.models import (
    AvdInfo,
    ImageKind,
    InstancePorts,
    InstanceState,
    OobeMatrix,
    OobeRecord,
    OobeResetStrategy,
    OobeStatus,
    Snapshot,
    SystemImage,
    utcnow,
)
from avdfleet.core.paths import Paths
from avdfleet.oobe import (
    OobeManager,
    extract_top_activity,
    is_png,
    repair_shell_png,
)
from avdfleet.oobe.detection import ActivityKind, TopActivity, classify_top_activity
from avdfleet.oobe.reset import PACKAGE_CLEANUP_COMMANDS, ResetEngine

SETUP_WIZARD_ACTIVITY = "com.google.android.setupwizard/.SetupWizardActivity"
LAUNCHER_ACTIVITY = "com.google.android.apps.nexuslauncher/.NexusLauncherActivity"
IMAGE_ID = "system-images;android-35;google_apis_playstore;x86_64"

# A minimal, structurally valid PNG (signature + IHDR chunk); the OOBE service
# only requires the signature, and the screen service additionally checks IHDR.
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    + b"\x00\x00\x00\x0d"
    + b"IHDR"
    + b"\x00\x00\x04\x38\x00\x00\x07\x80\x08\x02\x00\x00\x00"
    + b"\x00\x00\x00\x00"
)

DUMPSYS_SETUP_WIZARD = f"""ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)
Display #0 (activities from top to bottom):
  * Task{{7f2a #9 type=standard A=10123:com.google.android.setupwizard}}
    mResumedActivity: ActivityRecord{{b2f1 u0 {SETUP_WIZARD_ACTIVITY} t9}}
    topResumedActivity=ActivityRecord{{b2f1 u0 {SETUP_WIZARD_ACTIVITY} t9}}
    * Hist #0: ActivityRecord{{b2f1 u0 {SETUP_WIZARD_ACTIVITY} t9}}
  ResumedActivity: ActivityRecord{{b2f1 u0 {SETUP_WIZARD_ACTIVITY} t9}}
"""

DUMPSYS_WINDOW_SETUP = f"""WINDOW MANAGER WINDOWS (dumpsys window windows)
  Window #0 Window{{a3c7 u0 {SETUP_WIZARD_ACTIVITY}}}:
    mCurrentFocus=Window{{a3c7 u0 com.google.android.setupwizard/com.google.android.setupwizard.SetupWizardActivity}}
    mFocusedApp=ActivityRecord{{b2f1 u0 {SETUP_WIZARD_ACTIVITY} t9}}
"""

DUMPSYS_ACTIVITY_LAUNCHER = f"""ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)
Display #0 (activities from top to bottom):
  * Task{{91aa #4 type=home}}
    * Hist #1: ActivityRecord{{c4d2 u0 {LAUNCHER_ACTIVITY} t4}}
    mResumedActivity: ActivityRecord{{c4d2 u0 {LAUNCHER_ACTIVITY} t4}}
"""

DUMPSYS_WINDOW_LAUNCHER = """WINDOW MANAGER WINDOWS (dumpsys window windows)
  mCurrentFocus=Window{e5a1 u0 com.google.android.apps.nexuslauncher/com.google.android.apps.nexuslauncher.NexusLauncherActivity}
"""

DUMPSYS_ACTIVITY_UNKNOWN = """ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)
Display #0 (activities from top to bottom):
  * No activities
"""

DUMPSYS_WINDOW_UNKNOWN = """WINDOW MANAGER WINDOWS (dumpsys window windows)
  mCurrentFocus=null
  mFocusedApp=null
"""

DUMPSYS_ACTIVITY_EMPTY = """ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)
Display #0 (activities from top to bottom):
  * Task{dead #1 type=standard}
    * Hist #0: ActivityRecord{dead u0 com.example.unrelated/.MainActivity t1}
"""

DUMPSYS_WINDOW_FOCUS_ONLY = f"""WINDOW MANAGER WINDOWS (dumpsys window windows)
  mCurrentFocus=Window{{f00d u0 {SETUP_WIZARD_ACTIVITY}}}
"""


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class RunResult:
    def __init__(self, returncode: int = 0, stdout: str | bytes = "", stderr: str | bytes = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeClock:
    def __init__(self, *values: float, default: float = 100.0) -> None:
        self._values = list(values)
        self._default = float(default)
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        if self._values:
            return float(self._values.pop(0))
        return self._default


class FakeAdb:
    """Process-boundary fake for adb (shell, getprop and screencap)."""

    def __init__(
        self,
        shell: dict[str, RunResult] | None = None,
        *,
        screenshot: bytes | None = PNG_BYTES,
        screenshot_error: str = "",
    ) -> None:
        self.shell = dict(shell or {})
        self.screenshot = screenshot
        self.screenshot_error = screenshot_error
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], timeout_s: float | None = None) -> RunResult:
        command = [str(item) for item in argv]
        self.calls.append(command)
        if "exec-out" in command:
            if self.screenshot is None:
                return RunResult(1, b"", self.screenshot_error.encode() or b"exec-out failed")
            return RunResult(0, self.screenshot, b"")
        if "shell" in command:
            shell_args = command[command.index("shell") + 1 :]
            key = " ".join(shell_args)
            if key in self.shell:
                return self.shell[key]
            if shell_args[:1] == ["getprop"]:
                return RunResult(0, "", b"")
            return RunResult(0, "", b"")
        return RunResult(0, b"", b"")

    # -- helpers ---------------------------------------------------------
    def shell_calls(self) -> list[str]:
        calls: list[str] = []
        for command in self.calls:
            if "shell" in command:
                calls.append(" ".join(command[command.index("shell") + 1 :]))
        return calls

    def dump_calls(self) -> list[str]:
        return [call for call in self.shell_calls() if call.startswith("dumpsys")]

    def set(self, key: str, stdout: str | bytes = "", *, returncode: int = 0, stderr: str = "") -> None:
        self.shell[key] = RunResult(returncode, stdout, stderr)


def detection_shell(
    activity: str = DUMPSYS_SETUP_WIZARD,
    window: str = DUMPSYS_WINDOW_SETUP,
) -> dict[str, RunResult]:
    return {
        "dumpsys activity activities": RunResult(0, activity),
        "dumpsys window": RunResult(0, window),
        "getprop sys.boot_completed": RunResult(0, "1\n"),
        "getprop ro.build.version.sdk": RunResult(0, "35\n"),
        "getprop ro.product.cpu.abi": RunResult(0, "x86_64\n"),
        "getprop ro.build.version.release": RunResult(0, "15\n"),
    }


class FakeRuntime:
    """In-memory RuntimeManager double."""

    def __init__(self, *, console: int = 5554) -> None:
        self._infos: dict[str, AvdInfo] = {}
        self.running: set[str] = set()
        self.calls: list[tuple[Any, ...]] = []
        self.snapshots: dict[str, list[Snapshot]] = {}
        self.wait_boot_error: Exception | None = None
        self.start_error: Exception | None = None
        self.console = console
        self.emulator_version = "35.1.4.0"

    # -- setup -----------------------------------------------------------
    def define(self, name: str, *, image_id: str | None = None) -> AvdInfo:
        info = AvdInfo(name=name, image_id=image_id)
        self._infos[name] = info
        return info

    def _base(self, name: str) -> AvdInfo:
        if name not in self._infos:
            raise NotFoundError(f"instance {name!r} was not found")
        return self._infos[name]

    def _live(self, name: str) -> AvdInfo:
        base = self._base(name)
        running = name in self.running
        return base.model_copy(
            update={
                "state": InstanceState.RUNNING if running else InstanceState.STOPPED,
                "adb_serial": f"emulator-{self.console}" if running else None,
                "ports": InstancePorts(console=self.console, adb=self.console + 1) if running else None,
                "pid": 4321 if running else None,
                "boot_completed": running,
                "uptime_seconds": 12.5 if running else None,
            }
        )

    # -- protocol --------------------------------------------------------
    def status(self, name: str) -> AvdInfo:
        self.calls.append(("status", name))
        return self._live(name)

    def list_running(self) -> list[AvdInfo]:
        self.calls.append(("list_running",))
        return [self._live(name) for name in sorted(self.running)]

    def start(self, name: str, *, wipe_data: bool = False, **kwargs: Any) -> AvdInfo:
        self.calls.append(("start", name, wipe_data))
        if self.start_error is not None:
            raise self.start_error
        self._infos.setdefault(name, AvdInfo(name=name))
        self.running.add(name)
        return self._live(name)

    def stop(self, name: str, *, force: bool = False, timeout_s: float | None = None) -> AvdInfo:
        self.calls.append(("stop", name, force))
        self.running.discard(name)
        return self._live(name)

    def restart(self, name: str) -> AvdInfo:
        self.calls.append(("restart", name))
        self._infos.setdefault(name, AvdInfo(name=name))
        self.running.add(name)
        return self._live(name)

    def wait_boot(self, name: str, *, timeout_s: float | None = None) -> AvdInfo:
        self.calls.append(("wait_boot", name, timeout_s))
        if self.wait_boot_error is not None:
            raise self.wait_boot_error
        return self._live(name)

    def save_snapshot(self, name: str, snapshot: str, *, golden: bool = False) -> Snapshot:
        self.calls.append(("save_snapshot", name, snapshot, golden))
        created = Snapshot(name=snapshot, avd_name=name, created_at=utcnow(), is_golden=golden)
        self.snapshots.setdefault(name, []).append(created)
        return created

    def load_snapshot(self, name: str, snapshot: str) -> Snapshot:
        self.calls.append(("load_snapshot", name, snapshot))
        if name not in self.running:
            raise OobeError(f"instance {name!r} must be running to load a snapshot")
        return Snapshot(name=snapshot, avd_name=name, created_at=utcnow(), is_golden=True)

    def list_snapshots(self, name: str) -> list[Snapshot]:
        self.calls.append(("list_snapshots", name))
        return list(self.snapshots.get(name, []))

    def capacity(self) -> dict[str, Any]:
        return {"running": len(self.running)}

    # -- helpers ---------------------------------------------------------
    def calls_of(self, kind: str) -> list[tuple[Any, ...]]:
        return [call for call in self.calls if call and call[0] == kind]


class FakeAvdManager:
    """In-memory AvdManager double: create-from-image + delete only."""

    def __init__(self) -> None:
        self.instances: dict[str, AvdInfo] = {}
        self.created: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, bool]] = []

    def create_instance_from_image(
        self,
        image_id: str,
        name: str,
        *,
        device_id: str | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> AvdInfo:
        self.created.append(
            {"image_id": image_id, "name": name, "device_id": device_id, "overrides": overrides}
        )
        info = AvdInfo(name=name, image_id=image_id, image_name=image_id, state=InstanceState.STOPPED)
        self.instances[name] = info
        return info

    def get_instance(self, name: str) -> AvdInfo:
        if name not in self.instances:
            raise NotFoundError(f"instance {name!r} was not found")
        return self.instances[name]

    def delete_instance(self, name: str, *, delete_files: bool = True) -> None:
        self.deleted.append((name, delete_files))
        self.instances.pop(name, None)


class FakeCatalog:
    """In-memory ImageCatalog double with the OOBE matrix store."""

    def __init__(
        self,
        images: list[SystemImage] | None = None,
        records: list[OobeRecord] | None = None,
    ) -> None:
        self.images = {image.id: image for image in (images or [])}
        self.records = {record.image_id: record for record in (records or [])}
        self.updates: list[OobeRecord] = []

    def get_image(self, image_id: str) -> SystemImage:
        if image_id not in self.images:
            raise NotFoundError(f"system image not found: {image_id}")
        return self.images[image_id]

    def oobe_matrix(self) -> OobeMatrix:
        return OobeMatrix(version="1", host_abi="x86_64", records=list(self.records.values()))

    def update_oobe_record(self, record: OobeRecord) -> None:
        self.records[record.image_id] = record
        self.updates.append(record)


class FakeScreen:
    def __init__(self, data: bytes | None = PNG_BYTES, *, error: str = "") -> None:
        self.data = data
        self.error = error
        self.calls: list[str] = []

    def capture(self, avd_name: str) -> bytes:
        self.calls.append(avd_name)
        if self.error:
            raise OobeError(self.error)
        assert self.data is not None
        return self.data


def default_image(**overrides: Any) -> SystemImage:
    values: dict[str, Any] = {
        "id": IMAGE_ID,
        "api_level": 35,
        "tag": "google_apis_playstore",
        "abi": "x86_64",
        "kind": ImageKind.GOOGLE_APIS_PLAYSTORE,
        "display_name": "Google Play API 35 (x86_64)",
        "package": IMAGE_ID,
        "sha256": "a" * 64,
        "installed": True,
        "local_path": "/sdk/system-images/android-35/google_apis_playstore/x86_64",
    }
    values.update(overrides)
    return SystemImage(**values)


def make_paths(tmp_path: Path) -> Paths:
    return Paths(
        root=tmp_path,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
        sdk_root=tmp_path / "sdk",
        avd_home=tmp_path / "avd",
    )


class OobeHarness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        shell: dict[str, RunResult] | None = None,
        screenshot: bytes | None = PNG_BYTES,
        screenshot_error: str = "",
        clock: FakeClock | None = None,
        screen: Any | None = None,
        images: list[SystemImage] | None = None,
        records: list[OobeRecord] | None = None,
    ) -> None:
        self.paths = make_paths(tmp_path)
        self.adb = FakeAdb(shell if shell is not None else detection_shell(), screenshot=screenshot, screenshot_error=screenshot_error)
        self.runtime = FakeRuntime()
        self.avds = FakeAvdManager()
        self.catalog = FakeCatalog(images if images is not None else [default_image()], records)
        self.screen = screen
        self.clock = clock or FakeClock(default=100.0)
        self.manager = OobeManager(
            self.runtime,
            self.avds,
            self.catalog,
            self.paths,
            adb=self.adb,
            screen=screen,
            emulator_version="35.1.4.0",
            clock=self.clock,
        )


# ---------------------------------------------------------------------------
# dumpsys parsing
# ---------------------------------------------------------------------------


def test_extract_top_activity_prefers_top_resumed_line() -> None:
    top = extract_top_activity(DUMPSYS_SETUP_WIZARD, DUMPSYS_WINDOW_SETUP)
    assert top.activity == SETUP_WIZARD_ACTIVITY
    assert top.package == "com.google.android.setupwizard"
    assert top.component == "com.google.android.setupwizard.SetupWizardActivity"
    assert top.source == "topResumedActivity"
    assert classify_top_activity(top) is ActivityKind.SETUP_WIZARD


def test_extract_top_activity_launcher_from_m_resumed() -> None:
    top = extract_top_activity(DUMPSYS_ACTIVITY_LAUNCHER, DUMPSYS_WINDOW_LAUNCHER)
    assert top.activity == LAUNCHER_ACTIVITY
    assert top.source == "mResumedActivity"
    assert classify_top_activity(top) is ActivityKind.LAUNCHER


def test_extract_top_activity_unknown() -> None:
    top = extract_top_activity(DUMPSYS_ACTIVITY_UNKNOWN, DUMPSYS_WINDOW_UNKNOWN)
    assert top.present is False
    assert classify_top_activity(top) is ActivityKind.UNKNOWN


@pytest.mark.parametrize(
    "activity",
    [
        # Observed on real boots of stock emulator images, which ship no Setup
        # Wizard: the framework parks on a blank HOME until self-provisioning.
        "com.android.settings/.FallbackHome",
        "com.google.android.googlesdksetup/.DefaultActivity",
        "com.google.android.sdksetup/.DefaultActivity",
        "com.android.provision/.DefaultActivity",
    ],
)
def test_classify_unprovisioned_first_boot_states(activity: str) -> None:
    """First-boot stubs are ``provisioning``, never ``unknown``."""
    package, _, cls = activity.partition("/")
    top = TopActivity(activity=activity, package=package, component=f"{package}.{cls.lstrip('.')}")
    assert classify_top_activity(top) is ActivityKind.PROVISIONING


def test_extract_top_activity_falls_back_to_window_focus() -> None:
    top = extract_top_activity(DUMPSYS_ACTIVITY_EMPTY, DUMPSYS_WINDOW_FOCUS_ONLY)
    assert top.activity == SETUP_WIZARD_ACTIVITY
    assert top.source == "mCurrentFocus"


def test_repair_shell_png_undoes_crlf_mangling() -> None:
    mangled = PNG_BYTES.replace(b"\n", b"\r\n")
    assert not is_png(mangled)
    repaired = repair_shell_png(mangled)
    assert repaired is not None
    assert is_png(repaired)


# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------


def test_detect_setup_wizard_saves_screenshot_evidence(tmp_path: Path) -> None:
    harness = OobeHarness(tmp_path)
    harness.runtime.define("avd-01")
    harness.runtime.running.add("avd-01")

    record = harness.manager.detect("avd-01")

    assert record.status is OobeStatus.VERIFIED
    assert record.activity == SETUP_WIZARD_ACTIVITY
    assert record.first_run_component == SETUP_WIZARD_ACTIVITY
    assert record.api_level == 35
    assert record.abi == "x86_64"
    assert record.boot_seconds == pytest.approx(12.5)
    assert record.checked_by == "avdfleet"
    assert record.emulator_version == "35.1.4.0"
    screenshot = Path(record.screenshot_path)
    assert screenshot.is_file()
    assert screenshot.parent == harness.paths.screenshots_dir
    assert screenshot.read_bytes() == PNG_BYTES
    assert record.screenshot_path in record.evidence
    assert any("setup wizard visible" in note for note in [record.notes])
    # The injected adb runner is the only boundary used.
    assert "dumpsys activity activities" in harness.adb.shell_calls()

    evidence_files = [Path(item) for item in record.evidence if item.endswith(".txt")]
    assert evidence_files
    text = evidence_files[0].read_text(encoding="utf-8")
    assert "top_activity: " + SETUP_WIZARD_ACTIVITY in text
    assert "classification: setup_wizard" in text


def test_detect_launcher_is_failed(tmp_path: Path) -> None:
    harness = OobeHarness(
        tmp_path,
        shell=detection_shell(DUMPSYS_ACTIVITY_LAUNCHER, DUMPSYS_WINDOW_LAUNCHER),
    )
    harness.runtime.define("avd-01")
    harness.runtime.running.add("avd-01")

    record = harness.manager.detect("avd-01")

    assert record.status is OobeStatus.FAILED
    assert record.activity == LAUNCHER_ACTIVITY
    assert "launcher visible" in record.notes
    assert record.screenshot_path  # failure evidence is still captured
    assert Path(record.screenshot_path).is_file()


def test_detect_unknown_activity_is_failed(tmp_path: Path) -> None:
    harness = OobeHarness(
        tmp_path,
        shell=detection_shell(DUMPSYS_ACTIVITY_UNKNOWN, DUMPSYS_WINDOW_UNKNOWN),
    )
    harness.runtime.define("avd-01")
    harness.runtime.running.add("avd-01")

    record = harness.manager.detect("avd-01")

    assert record.status is OobeStatus.FAILED
    assert record.activity == ""
    assert "could not determine the top activity" in record.notes


def test_detect_uses_injected_screen_capture(tmp_path: Path) -> None:
    screen = FakeScreen(PNG_BYTES)
    harness = OobeHarness(tmp_path, screen=screen, screenshot=None)
    harness.runtime.define("avd-01")
    harness.runtime.running.add("avd-01")

    record = harness.manager.detect("avd-01")

    assert record.status is OobeStatus.VERIFIED
    assert screen.calls == ["avd-01"]
    assert Path(record.screenshot_path).read_bytes() == PNG_BYTES
    assert "exec-out" not in " ".join(" ".join(call) for call in harness.adb.calls)


def test_detect_requires_running_instance(tmp_path: Path) -> None:
    harness = OobeHarness(tmp_path)
    harness.runtime.define("avd-01")

    with pytest.raises(OobeError, match="not running"):
        harness.manager.detect("avd-01")


def test_detect_unknown_instance_raises_not_found(tmp_path: Path) -> None:
    harness = OobeHarness(tmp_path)
    with pytest.raises(NotFoundError):
        harness.manager.detect("ghost")


def test_manager_without_injected_runner_uses_subprocess_default(tmp_path: Path, monkeypatch) -> None:
    """Container-style wiring: only adb_executable is provided, no runner."""

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        joined = " ".join(str(item) for item in argv)
        if "dumpsys activity activities" in joined:
            return SimpleNamespace(returncode=0, stdout=DUMPSYS_SETUP_WIZARD.encode(), stderr=b"")
        if "dumpsys window" in joined:
            return SimpleNamespace(returncode=0, stdout=DUMPSYS_WINDOW_SETUP.encode(), stderr=b"")
        if "getprop sys.boot_completed" in joined:
            return SimpleNamespace(returncode=0, stdout=b"1\n", stderr=b"")
        if "getprop ro.build.version.sdk" in joined:
            return SimpleNamespace(returncode=0, stdout=b"35\n", stderr=b"")
        if "getprop ro.product.cpu.abi" in joined:
            return SimpleNamespace(returncode=0, stdout=b"x86_64\n", stderr=b"")
        if "exec-out" in argv:
            return SimpleNamespace(returncode=0, stdout=PNG_BYTES, stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    from avdfleet.oobe import detection

    monkeypatch.setattr(detection.subprocess, "run", fake_run)
    runtime = FakeRuntime()
    runtime.define("avd-01")
    runtime.running.add("avd-01")
    manager = OobeManager(
        runtime,
        FakeAvdManager(),
        FakeCatalog([default_image()]),
        make_paths(tmp_path),
        adb=None,
        adb_executable="adb",
        emulator_version="35.1.4.0",
        clock=FakeClock(),
    )

    record = manager.detect("avd-01")

    assert record.status is OobeStatus.VERIFIED
    assert record.activity == SETUP_WIZARD_ACTIVITY
    assert Path(record.screenshot_path).is_file()


# ---------------------------------------------------------------------------
# verify_image()
# ---------------------------------------------------------------------------


def test_verify_image_success_records_and_cleans_up(tmp_path: Path) -> None:
    clock = FakeClock(100.0, 101.25)
    harness = OobeHarness(tmp_path, clock=clock)
    harness.runtime.define("existing")

    record = harness.manager.verify_image(IMAGE_ID, timeout_s=90)

    assert record.status is OobeStatus.VERIFIED
    assert record.image_id == IMAGE_ID
    assert record.activity == SETUP_WIZARD_ACTIVITY
    assert record.boot_seconds == pytest.approx(1.25)
    assert record.checked_at is not None
    assert record.notes and "sha256" in record.notes and "temporary AVD removed" in record.notes
    assert Path(record.screenshot_path).is_file()

    assert len(harness.avds.created) == 1
    created = harness.avds.created[0]
    assert created["image_id"] == IMAGE_ID
    assert created["name"].startswith("avdfleet-oobe-")
    assert created["device_id"] == "pixel_7"

    assert harness.avds.deleted and harness.avds.deleted[0][0] == created["name"]
    assert harness.avds.deleted[0][1] is True
    assert not harness.runtime.running
    starts = harness.runtime.calls_of("start")
    # OOBE verification must boot a pristine data partition: the temporary AVD
    # name is deterministic per image, so a repeat verification would otherwise
    # observe the previous run's provisioned userdata (launcher, not wizard).
    assert starts and starts[0] == ("start", created["name"], True)
    wait = harness.runtime.calls_of("wait_boot")
    assert wait and wait[0][2] == 90.0

    # Persisted through the injected ImageCatalog, and visible in matrix().
    assert harness.catalog.updates and harness.catalog.updates[0].status is OobeStatus.VERIFIED
    matrix_record = harness.manager.matrix().get(IMAGE_ID)
    assert matrix_record is not None and matrix_record.status is OobeStatus.VERIFIED


def test_verify_image_launcher_records_failed_and_still_cleans_up(tmp_path: Path) -> None:
    harness = OobeHarness(
        tmp_path,
        shell=detection_shell(DUMPSYS_ACTIVITY_LAUNCHER, DUMPSYS_WINDOW_LAUNCHER),
    )

    record = harness.manager.verify_image(IMAGE_ID)

    assert record.status is OobeStatus.FAILED
    assert record.activity == LAUNCHER_ACTIVITY
    assert harness.avds.deleted
    assert harness.catalog.updates and harness.catalog.updates[0].status is OobeStatus.FAILED


def test_launcher_on_an_image_without_a_wizard_is_unsupported(tmp_path: Path) -> None:
    """An image that ships no Setup Wizard boots to the launcher: unsupported.

    Stock modern emulator images have no wizard at all, so seeing the launcher on
    a pristine AVD says nothing about setup state.  Probing the guest's package
    list distinguishes that from "this AVD was already provisioned".
    """
    shell = detection_shell(DUMPSYS_ACTIVITY_LAUNCHER, DUMPSYS_WINDOW_LAUNCHER)
    shell["pm list packages -u"] = RunResult(
        0, "package:com.google.android.sdksetup\npackage:com.google.android.partnersetup\n"
    )
    harness = OobeHarness(tmp_path, shell=shell)

    record = harness.manager.verify_image(IMAGE_ID)

    assert record.status is OobeStatus.UNSUPPORTED
    assert record.activity == LAUNCHER_ACTIVITY
    assert "no Setup Wizard" in record.notes
    assert harness.catalog.updates and harness.catalog.updates[0].status is OobeStatus.UNSUPPORTED


def test_verify_image_screenshot_failure_is_not_verified(tmp_path: Path) -> None:
    harness = OobeHarness(tmp_path, screenshot=b"<html>not a png</html>")

    record = harness.manager.verify_image(IMAGE_ID)

    assert record.status is OobeStatus.FAILED
    assert record.screenshot_path == ""
    assert "screenshot evidence" in record.notes
    assert harness.avds.deleted


def test_verify_image_keep_avd_skips_stop_and_delete(tmp_path: Path) -> None:
    harness = OobeHarness(tmp_path)
    record = harness.manager.verify_image(IMAGE_ID, keep_avd=True)

    assert record.status is OobeStatus.VERIFIED
    assert harness.avds.deleted == []
    assert harness.runtime.calls_of("stop") == []
    assert "temporary AVD kept" in record.notes
    assert len(harness.avds.instances) == 1


def test_verify_image_unknown_image_raises(tmp_path: Path) -> None:
    harness = OobeHarness(tmp_path)
    with pytest.raises(NotFoundError):
        harness.manager.verify_image("system-images;android-99;nope;x86_64")


def test_verify_image_not_installed_records_failure(tmp_path: Path) -> None:
    harness = OobeHarness(tmp_path, images=[default_image(installed=False, local_path=None)])

    record = harness.manager.verify_image(IMAGE_ID)

    assert record.status is OobeStatus.FAILED
    assert "not installed" in record.notes
    assert harness.avds.created == []
    assert harness.avds.deleted == []
    assert harness.catalog.updates


def test_verify_image_boot_timeout_records_safe_failure(tmp_path: Path) -> None:
    from avdfleet.core.errors import BootTimeoutError

    harness = OobeHarness(tmp_path)
    harness.runtime.wait_boot_error = BootTimeoutError("boot timed out after 5s")

    record = harness.manager.verify_image(IMAGE_ID)

    assert record.status is OobeStatus.FAILED
    assert "boot timed out" in record.notes
    assert harness.avds.deleted
    assert harness.catalog.updates


# ---------------------------------------------------------------------------
# reset()
# ---------------------------------------------------------------------------


def test_reset_defaults_to_snapshot_when_golden_exists(tmp_path: Path) -> None:
    clock = FakeClock(100.0, 104.5)
    harness = OobeHarness(tmp_path, clock=clock)
    harness.runtime.define("avd-01")
    harness.runtime.running.add("avd-01")
    harness.runtime.snapshots["avd-01"] = [
        Snapshot(name="avdfleet-golden", avd_name="avd-01", is_golden=True, created_at=utcnow())
    ]

    result = harness.manager.reset("avd-01")

    assert result.strategy is OobeResetStrategy.SNAPSHOT
    assert result.ok is True
    assert result.seconds == pytest.approx(4.5)
    assert result.activity == SETUP_WIZARD_ACTIVITY
    assert Path(result.screenshot_path).is_file()
    loads = harness.runtime.calls_of("load_snapshot")
    assert loads == [("load_snapshot", "avd-01", "avdfleet-golden")]
    assert harness.runtime.calls_of("wait_boot")


def test_reset_requires_golden_snapshot_for_snapshot_strategy(tmp_path: Path) -> None:
    harness = OobeHarness(tmp_path)
    harness.runtime.define("avd-01")

    result = harness.manager.reset("avd-01", strategy="snapshot")

    assert result.ok is False
    assert result.strategy is OobeResetStrategy.SNAPSHOT
    assert "ensure_golden_snapshot" in result.message
    assert harness.runtime.calls_of("start") == []
    assert harness.runtime.calls_of("load_snapshot") == []


def test_reset_wipe_stops_running_instance_then_relaunches_with_wipe_data(tmp_path: Path) -> None:
    harness = OobeHarness(tmp_path)
    harness.runtime.define("avd-01")
    harness.runtime.running.add("avd-01")

    result = harness.manager.reset("avd-01", strategy="wipe")

    assert result.ok is True
    assert result.strategy is OobeResetStrategy.WIPE
    assert result.activity == SETUP_WIZARD_ACTIVITY
    calls = harness.runtime.calls
    stop_index = next(index for index, call in enumerate(calls) if call[0] == "stop")
    start_index = next(index for index, call in enumerate(calls) if call[0] == "start")
    assert stop_index < start_index
    assert any(call == ("start", "avd-01", True) for call in calls)


def test_reset_wipe_default_when_no_golden(tmp_path: Path) -> None:
    harness = OobeHarness(tmp_path)
    harness.runtime.define("avd-01")

    result = harness.manager.reset("avd-01")

    assert result.strategy is OobeResetStrategy.WIPE
    assert result.ok is True
    assert any(call == ("start", "avd-01", True) for call in harness.runtime.calls)


def test_reset_package_uses_safe_guest_cleanup(tmp_path: Path) -> None:
    harness = OobeHarness(tmp_path)
    harness.runtime.define("avd-01")
    harness.runtime.running.add("avd-01")

    result = harness.manager.reset("avd-01", strategy="package")

    assert result.ok is True
    assert result.strategy is OobeResetStrategy.PACKAGE
    shell_calls = harness.adb.shell_calls()
    for command in PACKAGE_CLEANUP_COMMANDS:
        assert " ".join(command) in shell_calls
    # The AVD was already running: no start/stop needed.
    assert harness.runtime.calls_of("start") == []


def test_reset_package_tolerates_guest_failures_when_verified(tmp_path: Path) -> None:
    harness = OobeHarness(tmp_path)
    harness.runtime.define("avd-01")
    harness.runtime.running.add("avd-01")
    harness.adb.set("pm clear com.google.android.setupwizard", returncode=1, stderr="Unknown package")

    result = harness.manager.reset("avd-01", strategy="package")

    assert result.ok is True
    assert "guest-side warnings" in result.message


def test_reset_reboot_restarts_instance(tmp_path: Path) -> None:
    harness = OobeHarness(tmp_path)
    harness.runtime.define("avd-01")
    harness.runtime.running.add("avd-01")

    result = harness.manager.reset("avd-01", strategy="reboot")

    assert result.ok is True
    assert result.strategy is OobeResetStrategy.REBOOT
    assert harness.runtime.calls_of("restart") == [("restart", "avd-01")]
    assert harness.runtime.calls_of("wait_boot")


def test_reset_verify_false_skips_detection(tmp_path: Path) -> None:
    clock = FakeClock(100.0, 106.0)
    harness = OobeHarness(tmp_path, clock=clock)
    harness.runtime.define("avd-01")

    result = harness.manager.reset("avd-01", strategy="wipe", verify=False)

    assert result.ok is True
    assert result.activity == ""
    assert result.screenshot_path == ""
    assert result.seconds == pytest.approx(6.0)
    assert harness.adb.dump_calls() == []


def test_reset_invalid_strategy_raises_validation_error(tmp_path: Path) -> None:
    harness = OobeHarness(tmp_path)
    harness.runtime.define("avd-01")
    with pytest.raises(ValidationError, match="unknown OOBE reset strategy"):
        harness.manager.reset("avd-01", strategy="magic")


def test_reset_unknown_instance_raises_not_found(tmp_path: Path) -> None:
    harness = OobeHarness(tmp_path)
    with pytest.raises(NotFoundError):
        harness.manager.reset("ghost", strategy="wipe")


def test_reset_runtime_failure_is_reported_not_raised(tmp_path: Path) -> None:
    harness = OobeHarness(tmp_path)
    harness.runtime.define("avd-01")
    harness.runtime.start_error = OobeError("emulator failed to launch")

    result = harness.manager.reset("avd-01", strategy="wipe")

    assert result.ok is False
    assert "emulator failed to launch" in result.message
    assert result.seconds is not None


def test_reset_verification_retries_for_guest_settle_time(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    runtime.define("avd-01")
    runtime.running.add("avd-01")
    attempts: list[str] = []
    launcher_record = OobeRecord(
        image_id="",
        status=OobeStatus.FAILED,
        activity=LAUNCHER_ACTIVITY,
        notes="launcher visible",
    )
    verified_record = OobeRecord(
        image_id="",
        status=OobeStatus.VERIFIED,
        activity=SETUP_WIZARD_ACTIVITY,
        screenshot_path=str(tmp_path / "shot.png"),
    )

    def detect(name: str) -> OobeRecord:
        attempts.append(name)
        return launcher_record if len(attempts) == 1 else verified_record

    sleeps: list[float] = []
    engine = ResetEngine(
        runtime,
        detector=FakeAdb(detection_shell()),
        serial_for=lambda name: "emulator-5554",
        golden_available=lambda name: False,
        clock=FakeClock(200.0, 201.5),
        sleep=sleeps.append,
    )

    result = engine.run("avd-01", OobeResetStrategy.REBOOT, verify=True, detect=detect)

    assert result.ok is True
    assert len(attempts) == 2
    assert sleeps == [0.5]
    assert result.activity == SETUP_WIZARD_ACTIVITY


def test_reset_verification_exception_is_reported(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    runtime.define("avd-01")
    runtime.running.add("avd-01")

    def detect(name: str) -> OobeRecord:
        raise OobeError("adb went away")

    engine = ResetEngine(
        runtime,
        serial_for=lambda name: "emulator-5554",
        golden_available=lambda name: False,
        clock=FakeClock(300.0, 302.0),
        sleep=lambda seconds: None,
    )

    result = engine.run("avd-01", OobeResetStrategy.REBOOT, verify=True, detect=detect)

    assert result.ok is False
    assert "adb went away" in result.message
    assert result.seconds == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# ensure_golden_snapshot()
# ---------------------------------------------------------------------------


def test_ensure_golden_snapshot_boots_and_records_marker(tmp_path: Path) -> None:
    harness = OobeHarness(tmp_path)
    harness.runtime.define("avd-01", image_id=IMAGE_ID)

    snapshot = harness.manager.ensure_golden_snapshot("avd-01")

    assert snapshot.name == "avdfleet-golden"
    assert snapshot.is_golden is True
    assert snapshot.avd_name == "avd-01"
    saves = harness.runtime.calls_of("save_snapshot")
    assert saves == [("save_snapshot", "avd-01", "avdfleet-golden", True)]
    assert harness.runtime.calls_of("start")
    marker_path = harness.manager.golden_markers_path
    assert marker_path.is_file()
    marker = json.loads(marker_path.read_text(encoding="utf-8"))["snapshots"]["avd-01"]
    assert marker["snapshot"] == "avdfleet-golden"
    assert marker["activity"] == SETUP_WIZARD_ACTIVITY
    assert marker["emulator_version"] == "35.1.4.0"
    # The marker is enough for the snapshot strategy to be considered available.
    assert harness.manager._golden_available("avd-01") is True  # noqa: SLF001 - bookkeeping check


def test_ensure_golden_snapshot_uses_running_first_boot_instance(tmp_path: Path) -> None:
    """A fully booted instance already showing OOBE is captured without a restart."""
    harness = OobeHarness(tmp_path)
    harness.runtime.define("avd-01")
    harness.runtime.running.add("avd-01")

    snapshot = harness.manager.ensure_golden_snapshot("avd-01")

    assert snapshot.is_golden is True
    assert harness.runtime.calls_of("start") == []
    assert harness.runtime.calls_of("stop") == []
    assert harness.runtime.calls_of("save_snapshot") == [
        ("save_snapshot", "avd-01", "avdfleet-golden", True)
    ]


def test_ensure_golden_snapshot_requires_visible_setup_wizard(tmp_path: Path) -> None:
    harness = OobeHarness(
        tmp_path,
        shell=detection_shell(DUMPSYS_ACTIVITY_LAUNCHER, DUMPSYS_WINDOW_LAUNCHER),
    )
    harness.runtime.define("avd-01")

    with pytest.raises(OobeError, match="setup wizard is not visible"):
        harness.manager.ensure_golden_snapshot("avd-01")

    assert harness.runtime.calls_of("save_snapshot") == []


def test_ensure_golden_snapshot_is_idempotent_for_existing_snapshot(tmp_path: Path) -> None:
    harness = OobeHarness(tmp_path)
    harness.runtime.define("avd-01")
    harness.runtime.snapshots["avd-01"] = [
        Snapshot(name="avdfleet-golden", avd_name="avd-01", is_golden=True, created_at=utcnow())
    ]

    snapshot = harness.manager.ensure_golden_snapshot("avd-01")

    assert snapshot.is_golden is True
    assert harness.runtime.calls_of("start") == []
    assert harness.runtime.calls_of("save_snapshot") == []


# ---------------------------------------------------------------------------
# matrix()
# ---------------------------------------------------------------------------


def test_matrix_downgrades_verified_record_without_evidence(tmp_path: Path) -> None:
    unbacked = OobeRecord(
        image_id="unbacked",
        status=OobeStatus.VERIFIED,
        activity="",
        screenshot_path="",
        checked_at=utcnow(),
    )
    backed = OobeRecord(
        image_id="backed",
        status=OobeStatus.VERIFIED,
        activity=SETUP_WIZARD_ACTIVITY,
        screenshot_path=str(tmp_path / "evidence.png"),
        checked_at=utcnow(),
        evidence=[],
    )
    harness = OobeHarness(tmp_path, records=[unbacked, backed])

    matrix = harness.manager.matrix()

    downgraded = matrix.get("unbacked")
    assert downgraded is not None
    assert downgraded.status is OobeStatus.UNVERIFIED
    assert "downgraded" in downgraded.notes
    assert harness.manager.warnings

    verified = harness.manager.record_for("backed")
    assert verified is not None
    assert verified.status is OobeStatus.VERIFIED
    assert str(tmp_path / "evidence.png") in verified.evidence
    assert harness.manager.record_for("missing") is None


def test_oobe_manager_satisfies_core_protocol(tmp_path: Path) -> None:
    from avdfleet.core.interfaces import OobeManager as OobeManagerProtocol

    harness = OobeHarness(tmp_path)
    assert isinstance(harness.manager, OobeManagerProtocol)


def test_matrix_reflects_persisted_verify_record(tmp_path: Path) -> None:
    harness = OobeHarness(tmp_path)
    harness.manager.verify_image(IMAGE_ID, keep_avd=True)

    record = harness.manager.record_for(IMAGE_ID)
    assert record is not None
    assert record.status is OobeStatus.VERIFIED
    assert record.screenshot_path
    assert record.activity == SETUP_WIZARD_ACTIVITY
