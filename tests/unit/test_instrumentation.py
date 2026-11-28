"""Unit tests for the Root & Instrumentation subsystem.

Everything here runs without an emulator, without ``adb`` and without network
access:

* persistence is exercised against a real ``Store(":memory:")`` (schema v3);
* :class:`ModuleLibrary` inspects crafted in-memory ZIP archives and an injected
  ``http_get``;
* :class:`AcquisitionStore` downloads from an injected ``http_get`` and verifies
  against test pins, so no release asset is ever fetched;
* :class:`DeviceProber` is driven through its injected ``runner`` boundary, so
  each "device" is a script of ``adb`` answers.

The distinctions the subsystem promises are asserted explicitly: ``adb root`` is
not Magisk root, Magisk root does not imply adbd runs as uid 0, and a module is
reported as installed/enabled separately from active.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import avdfleet.instrumentation.acquire as acquire_module
from avdfleet.core.errors import ConfigError, ConflictError, NotFoundError, ValidationError
from avdfleet.core.events import Event, EventType
from avdfleet.core.models import utcnow
from avdfleet.instrumentation import (
    BUILTIN_PROFILES,
    MAX_MODULE_BYTES,
    PINNED_ARTIFACTS,
    AcquisitionStore,
    ComponentState,
    DeploymentRun,
    DeviceProber,
    InstrumentationProfile,
    InstrumentationRepository,
    InstrumentationService,
    LogEntry,
    ModuleKind,
    ModuleLibrary,
    PinnedArtifact,
    ProfileStep,
    RootImplementation,
    RunState,
    StepAction,
    StepResult,
    TargetDevice,
    TargetResult,
    classify_magisk_module,
    ensure_builtin_profiles,
    find_artifact,
    parse_badging,
    parse_module_prop,
)
from avdfleet.instrumentation.capabilities import LSPOSED_DB, CommandResult
from avdfleet.store import Store
from avdfleet.store.migrations import SCHEMA_VERSION

MAGISK_PROPS = (
    "[ro.build.type]: [user]\n"
    "[ro.build.version.sdk]: [34]\n"
    "[ro.product.cpu.abi]: [x86_64]\n"
    "[ro.debuggable]: [0]\n"
)
USERDEBUG_PROPS = (
    "[ro.build.type]: [userdebug]\n"
    "[ro.build.version.sdk]: [30]\n"
    "[ro.debuggable]: [1]\n"
)
ALPHA_PROP = "id=alpha\nname=Alpha\nversion=1.0\n"
BETA_PROP = "id=beta\nname=Beta\nversion=2.0\n"
LSPOSED_PROP = "id=zygisk_lsposed\nname=LSPosed\nversion=v1.9.2\n"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture()
def store() -> Iterator[Store]:
    store = Store(":memory:")
    store.initialize()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture()
def repository(store: Store) -> InstrumentationRepository:
    return InstrumentationRepository(store.database)


@pytest.fixture()
def library(tmp_path: Path) -> ModuleLibrary:
    return ModuleLibrary(tmp_path / "modules")


def _zip_bytes(entries: dict[str, bytes], directories: tuple[str, ...] = ()) -> bytes:
    """Build an in-memory ZIP/APK archive."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in directories:
            archive.writestr(zipfile.ZipInfo(name), b"")
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _magisk_zip(**prop: str) -> bytes:
    fields = {"id": "alpha", "name": "Alpha", "version": "1.0", "versionCode": "1"}
    fields.update(prop)
    body = "".join(f"{key}={value}\n" for key, value in fields.items())
    return _zip_bytes({"module.prop": body.encode("utf-8")})


class ScriptedAdb:
    """Deterministic stand-in for the ``adb`` process boundary.

    ``DeviceProber`` accepts this callable as its ``runner``, so probing is
    exercised end to end without a device, a binary or ``subprocess``.  Commands
    are matched after unwrapping ``root_shell``'s ``su -c "..."`` quoting so a
    script reads exactly like the shell command the prober meant to run.
    """

    def __init__(
        self,
        responses: dict[str, tuple[int, str]],
        *,
        default: tuple[int, str] = (1, ""),
    ) -> None:
        self.responses = dict(responses)
        self.default = default
        self.commands: list[str] = []
        self.argvs: list[list[str]] = []

    @staticmethod
    def inner(argv: list[str]) -> str:
        command = argv[-1]
        prefix = 'su -c "'
        if command.startswith(prefix) and command.endswith('"'):
            return command[len(prefix) : -1].replace('\\"', '"')
        return command

    def __call__(self, argv: list[str]) -> CommandResult:
        command = self.inner(argv)
        self.commands.append(command)
        self.argvs.append(list(argv))
        returncode, stdout = self.responses.get(command, self.default)
        return CommandResult(argv=argv, returncode=returncode, stdout=stdout)


def _magisk_script(
    *,
    modules: dict[str, str] | None = None,
    disabled: tuple[str, ...] = (),
    zygisk_setting: str = "value=1",
    zygisk_active_process: bool = False,
    magisk_installed: bool = True,
    magisk_environment_ready: bool = True,
    lsposed_running: bool = True,
) -> ScriptedAdb:
    """A Magisk-rooted device whose every ``adb`` answer is scripted.

    The answers model the live-measured signals rather than guesses: Magisk root
    is the SELinux context ``u:r:magisk:s0``, Zygisk is *active* only when a
    ``zygiskd`` process (or the staged loader) exists, and LSPosed is *active*
    only when ``pidof lspd`` reports a pid (``/data/adb/lspd`` is the fallback).
    ``/data/adb/magisk`` only proves that Magisk's "Additional Setup" finished.
    """
    module_props = {"alpha": ALPHA_PROP} if modules is None else dict(modules)
    responses: dict[str, tuple[int, str]] = {
        "getprop": (0, MAGISK_PROPS),
        # A plain userdebug su says context=u:r:su:s0; only Magisk's su says magisk.
        "su -c id": (0, "uid=0(root) gid=0(root) context=u:r:magisk:s0\n"),
        "magisk --sqlite \"select value from settings where key='zygisk'\"": (0, f"{zygisk_setting}\n"),
        # Neither API 25 nor API 34 sets this property; when it *is* set the probe
        # trusts it, which test_probe_zygisk_active_from_explicit_marker covers.
        "getprop ro.zygisk.active": (0, "\n"),
        "ls /data/adb/modules": (0, "".join(f"{name}\n" for name in module_props)),
        "mount": (0, "/dev/block/by-name/system /system ext4 ro,seclabel 0 0\n"),
    }
    if magisk_installed:
        responses["magisk -V"] = (0, "27000\n")
        responses["ls /data/adb/magisk"] = (
            (0, "magisk32\nmagisk64\n") if magisk_environment_ready else (0, "")
        )
    if zygisk_active_process:
        responses["ps -A"] = (0, "USER PID PPID NAME\nroot 1234 1 zygiskd64\n")
    if lsposed_running:
        responses["pidof lspd"] = (0, "4242\n")
        responses["ls /data/adb/lspd"] = (0, "config\n")
    for name, prop in module_props.items():
        base = f"/data/adb/modules/{name}"
        responses[f"cat {base}/module.prop"] = (0, prop)
        responses[f"[ -e {base}/disable ] && echo yes"] = (0, "yes\n") if name in disabled else (1, "")
        responses[f"[ -e {base}/remove ] && echo yes"] = (1, "")
        responses[f"ls {base}/zygisk 2>/dev/null"] = (1, "")
    return ScriptedAdb(responses)


def _userdebug_script(*, adb_root: bool = True, extra: dict[str, tuple[int, str]] | None = None) -> ScriptedAdb:
    """A userdebug image with no usable ``su``: only ``adb root`` can help."""
    responses: dict[str, tuple[int, str]] = {
        "getprop": (0, USERDEBUG_PROPS),
        "su -c id": (1, "su: not found\n"),
        "root": (0, "restarting adbd as root\n") if adb_root else (1, "adbd cannot run as root\n"),
        "id": (0, "uid=0(root) gid=0(root)\n") if adb_root else (1, "uid=2000(shell)\n"),
        "mount": (0, "/dev/block/by-name/system /system ext4 ro,seclabel 0 0\n"),
    }
    responses.update(extra or {})
    return ScriptedAdb(responses)


def _lsposed_zip() -> bytes:
    """A valid Magisk/LSPosed module ZIP (a real archive, not a blob)."""
    return _zip_bytes({"module.prop": LSPOSED_PROP.encode("utf-8")})


def _test_pin(
    data: bytes,
    *,
    key: str = "testtool",
    version: str = "9.9.9 (1)",
    filename: str = "testtool.bin",
    kind: str = "apk",
) -> PinnedArtifact:
    """A pinned artifact whose exact bytes a test can actually produce."""
    return PinnedArtifact(
        key=key,
        name=key.title(),
        version=version,
        url=f"https://downloads.example.invalid/{key}/{filename}",
        filename=filename,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        kind=kind,
        min_api=21,
        notes="test pin",
    )


def _install_pin(monkeypatch: pytest.MonkeyPatch, pin: PinnedArtifact) -> None:
    """Make ``find_artifact`` resolve *pin* for the duration of one test."""
    monkeypatch.setitem(acquire_module._BY_KEY, pin.key, pin)


class ScriptedDownload:
    """Injected ``http_get``: returns fixed bytes and records every request."""

    def __init__(self, payload: bytes = b"") -> None:
        self.payload = payload
        self.calls: list[tuple[str, int]] = []

    def __call__(self, url: str, limit: int) -> bytes:
        self.calls.append((url, limit))
        return self.payload


@pytest.fixture()
def downloads() -> ScriptedDownload:
    return ScriptedDownload()


@pytest.fixture()
def adb() -> ScriptedAdb:
    """The scripted device the service fixture probes."""
    return _magisk_script()


def _service_with_runner(
    repository: InstrumentationRepository,
    library: ModuleLibrary,
    tmp_path: Path,
    downloads: ScriptedDownload,
    runner: ScriptedAdb,
    *,
    events: Any | None = None,
    running: dict[str, bool] | None = None,
) -> InstrumentationService:
    states = dict(running or {})

    def resolve(name: str) -> TargetDevice:
        is_running = states.get(name, True)
        return TargetDevice(
            instance=name,
            serial="emulator-5554",
            running=is_running,
            boot_completed=is_running,
            image_tag="google_apis",
        )

    return InstrumentationService(
        repository,
        library,
        DeviceProber(runner=runner),
        resolve,
        acquisition=AcquisitionStore(tmp_path / "toolchain", http_get=downloads),
        events=events,
    )


@pytest.fixture()
def service(
    repository: InstrumentationRepository,
    library: ModuleLibrary,
    tmp_path: Path,
    downloads: ScriptedDownload,
    adb: ScriptedAdb,
) -> InstrumentationService:
    """A service wired to the in-memory store and a scripted toolchain."""
    return _service_with_runner(repository, library, tmp_path, downloads, adb)


def _target(api_level: int = 34) -> TargetDevice:
    return TargetDevice(
        instance="avd-01",
        serial="emulator-5554",
        running=True,
        boot_completed=True,
        api_level=api_level,
    )


# ---------------------------------------------------------------------------
# Schema: migration v3 is applied by Store.initialize()
# ---------------------------------------------------------------------------
def test_store_initialize_applies_instrumentation_migration(store: Store) -> None:
    applied = store.applied_migrations()
    assert [version for version, _name, _at in applied] == [1, 2, 3]
    assert max(version for version, _name, _at in applied) == 3
    assert store.schema_version == 3
    assert SCHEMA_VERSION == 3

    with store.database.connect() as conn:
        tables = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {
        "instrumentation_modules",
        "instrumentation_profiles",
        "instrumentation_runs",
        "instrumentation_logs",
    } <= tables


# ---------------------------------------------------------------------------
# Repository: module library
# ---------------------------------------------------------------------------
def test_module_add_get_list_and_delete(repository: InstrumentationRepository, library: ModuleLibrary) -> None:
    record = library.build_record(_magisk_zip(), "alpha.zip")

    assert repository.add_module(record) is record
    stored = repository.get_module(record.id)
    assert stored is not None
    assert stored.kind is ModuleKind.MAGISK_ZIP
    assert stored.sha256 == record.sha256
    assert stored.name == "Alpha"
    assert stored.size_bytes == record.size_bytes
    assert repository.get_module_by_sha(record.sha256) is not None
    assert repository.get_module_by_sha(record.sha256).id == record.id  # type: ignore[union-attr]
    assert [module.id for module in repository.list_modules()] == [record.id]

    repository.delete_module(record.id)
    assert repository.get_module(record.id) is None
    assert repository.list_modules() == []
    with pytest.raises(NotFoundError):
        repository.delete_module(record.id)


def test_duplicate_sha256_raises_conflict(repository: InstrumentationRepository, library: ModuleLibrary) -> None:
    record = library.build_record(_magisk_zip(), "alpha.zip")
    repository.add_module(record)

    duplicate = record.model_copy(update={"id": "mod_same_bytes"})
    with pytest.raises(ConflictError) as raised:
        repository.add_module(duplicate)
    assert raised.value.code == "conflict"
    assert "Alpha" in raised.value.message
    assert len(repository.list_modules()) == 1


def test_module_list_filters_by_kind(repository: InstrumentationRepository, library: ModuleLibrary) -> None:
    magisk = library.build_record(_magisk_zip(), "alpha.zip")
    xposed = library.build_record(
        _zip_bytes(
            {"AndroidManifest.xml": b"<manifest/>", "assets/xposed_init": b"com.example.Hook\n"}
        ),
        "hook.apk",
    )
    repository.add_module(magisk)
    repository.add_module(xposed)

    assert {module.id for module in repository.list_modules()} == {magisk.id, xposed.id}
    assert [module.id for module in repository.list_modules(ModuleKind.MAGISK_ZIP)] == [magisk.id]
    assert [module.id for module in repository.list_modules(ModuleKind.XPOSED_APK)] == [xposed.id]
    assert repository.list_modules(ModuleKind.LSPOSED_ZIP) == []


# ---------------------------------------------------------------------------
# Repository: profiles
# ---------------------------------------------------------------------------
def test_profile_create_read_update_delete(repository: InstrumentationRepository) -> None:
    profile = InstrumentationProfile(
        id="prof_custom",
        name="My profile",
        steps=[ProfileStep(id="root-verify", action=StepAction.VERIFY_ROOT)],
    )
    repository.add_profile(profile)

    stored = repository.get_profile("prof_custom")
    assert stored is not None
    assert stored.revision == 1
    assert stored.builtin is False
    assert [step.action for step in stored.steps] == [StepAction.VERIFY_ROOT]

    updated = stored.model_copy(
        update={
            "name": "My profile v2",
            "revision": 2,
            "steps": [ProfileStep(id="root-verify", action=StepAction.VERIFY_ROOT)],
            "updated_at": utcnow(),
        }
    )
    repository.save_profile(updated)

    reloaded = repository.get_profile("prof_custom")
    assert reloaded is not None
    assert reloaded.name == "My profile v2"
    assert reloaded.revision == 2
    assert reloaded.created_at == stored.created_at

    repository.delete_profile("prof_custom")
    assert repository.get_profile("prof_custom") is None
    with pytest.raises(NotFoundError):
        repository.delete_profile("prof_custom")


def test_duplicate_profile_name_raises_conflict(repository: InstrumentationRepository) -> None:
    steps = [ProfileStep(id="root-verify", action=StepAction.VERIFY_ROOT)]
    repository.add_profile(InstrumentationProfile(id="prof_a", name="Same name", steps=steps))

    with pytest.raises(ConflictError):
        repository.add_profile(InstrumentationProfile(id="prof_b", name="Same name", steps=steps))
    assert [profile.id for profile in repository.list_profiles()] == ["prof_a"]


def test_builtin_profile_cannot_be_deleted(repository: InstrumentationRepository) -> None:
    assert ensure_builtin_profiles(repository) == len(BUILTIN_PROFILES)

    with pytest.raises(ConflictError) as raised:
        repository.delete_profile("prof_builtin_root")
    assert raised.value.code == "conflict"
    assert "built in" in raised.value.message
    assert repository.get_profile("prof_builtin_root") is not None


def test_ensure_builtin_profiles_is_idempotent(repository: InstrumentationRepository) -> None:
    assert len(BUILTIN_PROFILES) == 5
    assert ensure_builtin_profiles(repository) == 5
    assert ensure_builtin_profiles(repository) == 0

    profiles = repository.list_profiles()
    assert len(profiles) == 5
    assert {profile.id for profile in profiles} == {template.id for template in BUILTIN_PROFILES}
    assert {profile.name for profile in profiles} == {template.name for template in BUILTIN_PROFILES}
    assert all(profile.builtin for profile in profiles)


# ---------------------------------------------------------------------------
# Repository: deployment runs and logs
# ---------------------------------------------------------------------------
def test_run_create_save_get_and_list(repository: InstrumentationRepository) -> None:
    run = DeploymentRun(
        id="irun_1",
        profile_id="prof_builtin_root",
        label="Root -> 2 AVD(s)",
        state=RunState.QUEUED,
        targets=["avd-01", "avd-02"],
    )
    repository.create_run(run)
    created = repository.get_run("irun_1")
    assert created is not None
    assert created.state is RunState.QUEUED
    assert created.targets == ["avd-01", "avd-02"]

    run.state = RunState.PARTIAL
    run.error = "avd-02 failed"
    run.started_at = utcnow()
    run.finished_at = utcnow()
    run.results = [
        TargetResult(
            instance="avd-01",
            ok=True,
            steps=[
                StepResult(
                    step_id="root-verify",
                    action=StepAction.VERIFY_ROOT,
                    ok=True,
                    state=ComponentState.ACTIVE,
                    message="root available via magisk",
                    data={"root_implementation": "magisk"},
                )
            ],
        ),
        TargetResult(instance="avd-02", ok=False, skipped=True, error="instance is not running"),
    ]
    repository.save_run(run)

    loaded = repository.get_run("irun_1")
    assert loaded is not None
    assert loaded.state is RunState.PARTIAL
    assert loaded.error == "avd-02 failed"
    assert loaded.started_at is not None and loaded.finished_at is not None
    assert [result.instance for result in loaded.results] == ["avd-01", "avd-02"]
    step = loaded.results[0].steps[0]
    assert step.ok is True
    assert step.action is StepAction.VERIFY_ROOT
    assert step.state is ComponentState.ACTIVE
    assert step.message == "root available via magisk"
    assert step.data == {"root_implementation": "magisk"}
    assert loaded.results[1].skipped is True
    assert loaded.results[1].error == "instance is not running"
    assert [item.id for item in repository.list_runs()] == ["irun_1"]
    assert repository.get_run("irun_missing") is None


def test_list_runs_is_newest_first_and_bounded(repository: InstrumentationRepository) -> None:
    base = utcnow()
    for index in range(3):
        repository.create_run(
            DeploymentRun(
                id=f"irun_{index}",
                label=f"run {index}",
                created_at=base + timedelta(seconds=index),
            )
        )

    assert [run.id for run in repository.list_runs()] == ["irun_2", "irun_1", "irun_0"]
    assert [run.id for run in repository.list_runs(limit=2)] == ["irun_2", "irun_1"]
    assert len(repository.list_runs(limit=0)) == 1


def test_logs_append_and_list_in_insertion_order(repository: InstrumentationRepository) -> None:
    for message in ("first", "second", "third"):
        repository.append_log(LogEntry(run_id="irun_1", instance="avd-01", message=message))
    repository.append_log(LogEntry(run_id="irun_2", instance="avd-02", message="other run"))

    entries = repository.list_logs("irun_1")
    assert [entry.message for entry in entries] == ["first", "second", "third"]
    assert [entry.id for entry in entries] == sorted(entry.id for entry in entries)
    assert all(entry.run_id == "irun_1" and entry.instance == "avd-01" for entry in entries)
    assert [entry.message for entry in repository.list_logs("irun_1", limit=2)] == ["first", "second"]
    assert repository.list_logs("irun_missing") == []

    repository.append_log(
        LogEntry(run_id="irun_3", instance="avd-03", level="error", message="boom /data/adb")
    )
    failed = repository.list_logs("irun_3")
    assert len(failed) == 1
    assert (failed[0].instance, failed[0].level, failed[0].message) == ("avd-03", "error", "boom /data/adb")
    assert failed[0].at.tzinfo is not None


# ---------------------------------------------------------------------------
# Library: aapt2 badging parsing
# ---------------------------------------------------------------------------
BADGING = """\
package: name='com.example.instrumented' versionCode='4200' versionName='4.2' platformBuildVersionName='14' platformBuildVersionCode='34' compileSdkVersion='34' compileSdkVersionCodename='14'
sdkVersion:'24'
targetSdkVersion:'34'
uses-permission: name='android.permission.INTERNET'
application-label:'Instrumented Example'
application-label-af:'Voorbeeld'
application: label='Instrumented Example' icon='res/mipmap-hdpi/ic.png'
launchable-activity: name='com.example.instrumented.MainActivity'  label='' icon=''
"""


def test_parse_badging_reads_aapt2_output() -> None:
    info = parse_badging(BADGING)

    assert info.package_name == "com.example.instrumented"
    assert info.version == "4.2"
    assert info.version_code == 4200
    assert info.min_sdk == 24
    assert info.label == "Instrumented Example"
    assert info.raw == BADGING


def test_parse_badging_defaults_for_empty_text() -> None:
    info = parse_badging("")
    assert (info.package_name, info.version, info.version_code, info.min_sdk) == ("", "", 0, 0)
    assert info.label == ""


# ---------------------------------------------------------------------------
# Library: archive classification
# ---------------------------------------------------------------------------
def test_magisk_zip_is_classified(library: ModuleLibrary) -> None:
    data = _magisk_zip(id="quickstep", name="Quickstep", version="1.2.3", versionCode="7")

    kind, metadata = library.inspect(data, "quickstep.zip")
    assert kind is ModuleKind.MAGISK_ZIP
    assert metadata["module_prop"]["id"] == "quickstep"
    assert metadata["zygisk_payload"] is False

    record = library.build_record(data, "quickstep.zip")
    assert record.kind is ModuleKind.MAGISK_ZIP
    assert record.name == "Quickstep"
    assert record.package_name == "quickstep"
    assert record.version == "1.2.3"
    assert record.version_code == 7
    assert record.source == "upload"
    assert record.sha256 == hashlib.sha256(data).hexdigest()
    assert record.id == f"mod_{record.sha256[:16]}"
    assert record.size_bytes == len(data)
    assert Path(record.file_path).is_file()


def test_lsposed_zip_is_classified_from_its_module_id(library: ModuleLibrary) -> None:
    data = _magisk_zip(id="zygisk_lsposed", name="LSPosed", version="v1.9.2")

    kind, metadata = library.inspect(data, "lsposed.zip")
    assert kind is ModuleKind.LSPOSED_ZIP
    assert metadata["module_prop"]["name"] == "LSPosed"

    record = library.build_record(data, "lsposed.zip")
    assert record.kind is ModuleKind.LSPOSED_ZIP
    assert record.version == "v1.9.2"


def test_zygisk_zip_is_classified_from_its_directory(library: ModuleLibrary) -> None:
    data = _zip_bytes(
        {
            "module.prop": b"id=shamiko\nname=Shamiko\nversion=v1.1\n",
            "zygisk/arm64-v8a.so": b"\x7fELF zygisk payload",
        },
        directories=("zygisk/",),
    )

    kind, metadata = library.inspect(data, "shamiko.zip")
    assert kind is ModuleKind.ZYGISK_ZIP
    assert metadata["zygisk_payload"] is True
    assert library.build_record(data, "shamiko.zip").kind is ModuleKind.ZYGISK_ZIP


def test_xposed_apk_is_classified_from_its_markers(library: ModuleLibrary) -> None:
    legacy = _zip_bytes(
        {
            "AndroidManifest.xml": b"<manifest package='com.example.hook'/>",
            "assets/xposed_init": b"com.example.hook.Hook\n",
        }
    )
    kind, metadata = library.inspect(legacy, "hook.apk")
    assert kind is ModuleKind.XPOSED_APK
    assert "assets/xposed_init" in metadata["xposed_markers"]

    modern = _zip_bytes(
        {
            "AndroidManifest.xml": b"<manifest package='com.example.modern'/>",
            "META-INF/xposed/module.prop": b"id=com.example.modern\n",
            "META-INF/xposed/scope.list": b"com.example.target\n",
        }
    )
    modern_kind, modern_metadata = library.inspect(modern, "modern.apk")
    assert modern_kind is ModuleKind.XPOSED_APK
    assert "meta-inf/xposed/module.prop" in modern_metadata["xposed_markers"]
    assert "META-INF/xposed/scope.list" in modern_metadata["xposed_markers"]


def test_plain_apk_is_unknown_and_explains_why(library: ModuleLibrary) -> None:
    data = _zip_bytes({"AndroidManifest.xml": b"<manifest/>", "classes.dex": b"dex\n039\x00"})

    kind, metadata = library.inspect(data, "plain.apk")
    assert kind is ModuleKind.UNKNOWN
    assert metadata["xposed_markers"] == []
    assert "note" in metadata
    assert "xposed" in metadata["note"].lower()
    assert library.build_record(data, "plain.apk").kind is ModuleKind.UNKNOWN


def test_non_archive_bytes_are_rejected(library: ModuleLibrary) -> None:
    with pytest.raises(ValidationError) as raised:
        library.build_record(b"\x00\x01not a zip at all", "random.bin")
    assert raised.value.code == "invalid_request"
    assert "not a ZIP/APK archive" in raised.value.message

    with pytest.raises(ValidationError) as empty:
        library.build_record(b"", "empty.zip")
    assert "empty" in empty.value.message

    with pytest.raises(ValidationError) as neither:
        library.build_record(_zip_bytes({"readme.txt": b"hello"}), "readme.zip")
    assert "neither an APK nor a Magisk module" in neither.value.message

    # Invalid input is rejected before the artifact is stored.
    assert not library.root.exists() or list(library.root.iterdir()) == []


def test_oversized_artifact_is_rejected(
    library: ModuleLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("avdfleet.instrumentation.library.MAX_MODULE_BYTES", 64)
    assert MAX_MODULE_BYTES > 64  # only the injected module-level ceiling was lowered

    with pytest.raises(ValidationError) as raised:
        library.build_record(b"x" * 65, "big.zip")
    assert "MiB limit" in raised.value.message
    assert not library.root.exists() or list(library.root.iterdir()) == []


def test_artifact_identity_is_content_addressed(library: ModuleLibrary) -> None:
    first = library.build_record(_magisk_zip(), "alpha.zip")
    second = library.build_record(_magisk_zip(), "renamed.zip")

    assert first.id == second.id
    assert first.sha256 == second.sha256
    assert Path(first.file_path).parent == Path(second.file_path).parent
    assert Path(first.file_path).is_file()
    assert Path(second.file_path).is_file()

    other = library.build_record(_magisk_zip(id="beta", name="Beta"), "alpha.zip")
    assert other.id != first.id
    assert other.sha256 != first.sha256


def test_import_file_reads_and_stores_the_artifact(tmp_path: Path, library: ModuleLibrary) -> None:
    path = tmp_path / "alpha.zip"
    path.write_bytes(_magisk_zip())

    record = library.import_file(path)
    assert record.source == "upload"
    stored = Path(record.file_path)
    assert stored.is_file()
    assert stored.parent.name == record.sha256

    library.delete_file(record)
    assert not stored.exists()
    assert not stored.parent.exists()

    with pytest.raises(ValidationError):
        library.import_file(tmp_path / "missing.zip")


def test_import_url_refuses_plain_http_unless_allowed(library: ModuleLibrary) -> None:
    calls: list[tuple[str, int]] = []

    def fake_http_get(url: str, limit: int) -> bytes:
        calls.append((url, limit))
        return _magisk_zip()

    secure = ModuleLibrary(library.root, http_get=fake_http_get)
    url = "https://modules.example.invalid/alpha.zip?token=abc"
    record = secure.import_url(url)

    assert record.source == "url"
    # Provenance never stores query secrets (QA-16).
    assert record.source_url == "https://modules.example.invalid/alpha.zip?token=<redacted>"
    assert record.name == "Alpha"
    assert calls == [(url, MAX_MODULE_BYTES)]

    with pytest.raises(ValidationError) as refused:
        secure.import_url("http://modules.example.invalid/alpha.zip")
    assert "plain HTTP" in refused.value.message
    assert len(calls) == 1  # nothing was downloaded

    insecure = ModuleLibrary(library.root, http_get=fake_http_get, allow_insecure_urls=True)
    plain = insecure.import_url("http://modules.example.invalid/alpha.zip")
    assert plain.source == "url"
    assert plain.source_url == "http://modules.example.invalid/alpha.zip"
    assert len(calls) == 2

    with pytest.raises(ValidationError):
        secure.import_url("ftp://modules.example.invalid/alpha.zip")


# ---------------------------------------------------------------------------
# Capability classification
# ---------------------------------------------------------------------------
def test_parse_module_prop_skips_comments_blanks_and_malformed_lines() -> None:
    text = "\n".join(
        [
            "# a comment with = in it",
            "",
            "id=alpha",
            "name= Alpha Module ",
            "this line has no equals sign",
            "=no key",
            "version=v1.2.3",
            "   ",
        ]
    )

    assert parse_module_prop(text) == {
        "id": "alpha",
        "name": "Alpha Module",
        "version": "v1.2.3",
    }
    assert parse_module_prop("") == {}
    assert parse_module_prop("name=first\nname=second\n") == {"name": "second"}


@pytest.mark.parametrize(
    ("module_id", "fields", "has_zygisk", "expected"),
    [
        ("plain", {"name": "Plain module"}, False, ModuleKind.MAGISK_ZIP),
        ("plain", {}, True, ModuleKind.ZYGISK_ZIP),
        ("shamiko", {"name": "Shamiko"}, True, ModuleKind.ZYGISK_ZIP),
        ("whatever", {"name": "My Zygisk module"}, False, ModuleKind.ZYGISK_ZIP),
        ("zygisk_lsposed", {"name": "LSPosed"}, False, ModuleKind.LSPOSED_ZIP),
        ("whatever", {"name": "LSPosed manager"}, False, ModuleKind.LSPOSED_ZIP),
        ("zygisk_lsposed", {}, True, ModuleKind.LSPOSED_ZIP),
    ],
)
def test_classify_magisk_module(
    module_id: str,
    fields: dict[str, str],
    has_zygisk: bool,
    expected: ModuleKind,
) -> None:
    assert classify_magisk_module(module_id, fields, has_zygisk=has_zygisk) is expected


def test_magisk_modules_report_the_disable_marker() -> None:
    prober = DeviceProber(
        runner=_magisk_script(
            modules={"alpha": ALPHA_PROP, "beta": BETA_PROP}, disabled=("beta",)
        )
    )

    modules = {module.identifier: module for module in prober.magisk_modules("emulator-5554")}

    assert set(modules) == {"alpha", "beta"}
    assert modules["alpha"].enabled is True
    assert modules["alpha"].notes == []
    assert modules["alpha"].version == "1.0"
    assert modules["alpha"].source == "/data/adb/modules/alpha/module.prop"
    assert modules["beta"].enabled is False
    assert modules["beta"].active is False
    assert modules["beta"].notes
    assert "disable" in modules["beta"].notes[0]


def test_probe_magisk_device_reports_root_and_zygisk_state() -> None:
    prober = DeviceProber(runner=_magisk_script(zygisk_setting="value=1"))

    device = prober.probe(instance="avd-01", serial="emulator-5554", image_tag="google_apis")

    assert device.root_implementation is RootImplementation.MAGISK
    assert device.root_state is ComponentState.ACTIVE
    assert device.compatible is True
    assert device.blockers == []
    assert device.api_level == 34
    assert device.abi == "x86_64"
    assert device.image_tag == "google_apis"

    flags = device.capabilities
    assert flags.magisk is True
    assert flags.magisk_version == "27000"
    assert flags.magisk_environment_ready is True
    assert flags.adb_root is False  # adbd is not uid 0 just because Magisk root exists
    assert flags.zygisk_available is True
    assert flags.zygisk_enabled is True
    assert flags.zygisk_active is False  # enabled, but not mounted until the reboot
    assert flags.reboot_required is True
    assert flags.lsposed_installed is False


def test_probe_magisk_device_does_not_claim_adb_root() -> None:
    """Magisk root is not ``adb root``: adbd still runs as the shell user.

    Regression for the ``adb_root`` flag being derived from ``root is not NONE``;
    a Magisk device reported ``adb_root=True`` even though a bare
    ``adb shell id`` returns uid 2000.
    """
    script = _magisk_script()
    script.responses["id"] = (0, "uid=2000(shell) gid=2000(shell) context=u:r:shell:s0\n")

    device = DeviceProber(runner=script).probe(instance="avd-01", serial="emulator-5554")

    assert device.root_implementation is RootImplementation.MAGISK
    assert device.capabilities.magisk is True
    assert device.capabilities.adb_root is False
    assert "id" in script.commands  # the bare shell identity really was measured
    assert "su -c id" in script.commands


def test_probe_zygisk_becomes_active_once_zygiskd_runs() -> None:
    """``zygisk`` being enabled is not proof that Zygisk is running.

    Active state is measured from the ``zygiskd`` process (or the staged loader
    below), never from the persisted setting alone — otherwise the GUI would claim
    success one reboot too early. Live verification found the daemon is named
    ``zygiskd64`` on API 34.
    """
    script = _magisk_script(zygisk_active_process=True)

    flags = DeviceProber(runner=script).probe(instance="avd-01", serial="emulator-5554").capabilities

    assert flags.zygisk_enabled is True
    assert flags.zygisk_active is True
    assert flags.reboot_required is False
    assert "ps -A" in script.commands


def test_probe_zygisk_active_from_explicit_marker() -> None:
    """A build that sets ``ro.zygisk.active=1`` is trusted without process probing."""
    script = _magisk_script()
    script.responses["getprop ro.zygisk.active"] = (0, "1\n")

    flags = DeviceProber(runner=script).probe(instance="avd-01", serial="emulator-5554").capabilities

    assert flags.zygisk_enabled is True
    assert flags.zygisk_active is True
    assert flags.reboot_required is False


def test_probe_zygisk_active_from_the_staged_loader() -> None:
    script = _magisk_script()
    script.responses["magisk --path"] = (0, "/dev/magisk123\n")
    script.responses["ls /dev/magisk123/.magisk/zygisk"] = (0, "loader64.so\n")

    flags = DeviceProber(runner=script).probe(instance="avd-01", serial="emulator-5554").capabilities

    assert flags.zygisk_enabled is True
    assert flags.zygisk_active is True
    assert flags.reboot_required is False
    assert "magisk --path" in script.commands


def test_probe_zygisk_disabled_setting_is_never_active() -> None:
    flags = DeviceProber(
        runner=_magisk_script(zygisk_setting="value=0", zygisk_active_process=True)
    ).probe(instance="avd-01", serial="emulator-5554").capabilities

    assert flags.zygisk_available is True
    assert flags.zygisk_enabled is False
    assert flags.zygisk_active is False


def test_probe_incomplete_magisk_environment_is_not_ready() -> None:
    """``/data/adb/magisk`` proves "Additional Setup" ran, not that Magisk exists."""
    device = DeviceProber(
        runner=_magisk_script(magisk_environment_ready=False)
    ).probe(instance="avd-01", serial="emulator-5554")

    assert device.capabilities.magisk is True
    assert device.capabilities.magisk_environment_ready is False
    assert any("Additional Setup" in note for note in device.capabilities.notes)


def test_probe_magisk_device_lists_its_modules() -> None:
    """A probe must report every Magisk module, disabled ones included.

    Regression: the inventory used to be taken from ``lsposed_state``'s
    side-product, which is empty when LSPosed is not installed, so a plain
    Magisk device reported no modules at all.
    """
    prober = DeviceProber(
        runner=_magisk_script(modules={"alpha": ALPHA_PROP, "beta": BETA_PROP}, disabled=("beta",))
    )

    device = prober.probe(instance="avd-01", serial="emulator-5554")

    assert device.capabilities.lsposed_installed is False  # the trigger for the dropped inventory
    assert [module.identifier for module in device.magisk_modules] == ["alpha", "beta"]
    by_identifier = {module.identifier: module for module in device.magisk_modules}
    assert by_identifier["alpha"].enabled is True
    assert by_identifier["beta"].enabled is False


def test_probe_magisk_device_with_lsposed_reports_the_framework() -> None:
    script = _magisk_script(modules={"alpha": ALPHA_PROP, "zygisk_lsposed": LSPOSED_PROP})
    prober = DeviceProber(runner=script)

    device = prober.probe(instance="avd-01", serial="emulator-5554")

    assert device.capabilities.lsposed_installed is True
    assert device.capabilities.lsposed_version == "v1.9.2"
    assert device.capabilities.lsposed_active is True
    modules = {module.identifier: module for module in device.magisk_modules}
    assert set(modules) == {"alpha", "zygisk_lsposed"}
    assert modules["zygisk_lsposed"].kind is ModuleKind.LSPOSED_ZIP
    # lspd is the framework daemon; `pidof lspd` is the live-verified signal.
    assert "pidof lspd" in script.commands


def test_probe_lsposed_active_falls_back_to_the_data_dir() -> None:
    script = _magisk_script(modules={"zygisk_lsposed": LSPOSED_PROP})
    script.responses.pop("pidof lspd")  # no daemon pid, but the data dir exists

    device = DeviceProber(runner=script).probe(instance="avd-01", serial="emulator-5554")

    assert device.capabilities.lsposed_installed is True
    assert device.capabilities.lsposed_active is True


def test_probe_reports_the_xposed_inventory_when_lsposed_runs() -> None:
    script = _magisk_script(modules={"zygisk_lsposed": LSPOSED_PROP})
    script.responses["command -v sqlite3"] = (0, "/system/bin/sqlite3\n")
    script.responses[
        f"sqlite3 {LSPOSED_DB} 'select module_pkg_name, enabled from modules'"
    ] = (0, "com.example.hook|1\ncom.example.other|0\n")
    script.responses[
        f"sqlite3 {LSPOSED_DB} 'select m.module_pkg_name, s.app_pkg_name from modules m"
        " join scope s on s.mid = m.mid'"
    ] = (0, "com.example.hook|com.example.target\ncom.example.other|com.example.target\n")

    device = DeviceProber(runner=script).probe(instance="avd-01", serial="emulator-5554")

    assert device.capabilities.lsposed_installed is True
    assert [module.identifier for module in device.xposed_modules] == [
        "com.example.hook",
        "com.example.other",
    ]
    scopes = {module.identifier: module.scope for module in device.xposed_modules}
    assert scopes == {
        "com.example.hook": ["com.example.target"],
        "com.example.other": ["com.example.target"],
    }
    assert not any("sqlite3" in note for note in device.capabilities.notes)


def test_probe_userdebug_su_is_not_magisk_root() -> None:
    """uid 0 from a plain userdebug ``su`` is not Magisk root.

    Measured discriminator: Magisk's su reports ``context=u:r:magisk:s0``; the
    userdebug image's su reports ``context=u:r:su:s0`` even though it is uid 0.
    """
    script = _userdebug_script()
    script.responses["su -c id"] = (0, "uid=0(root) gid=0(root) context=u:r:su:s0\n")

    device = DeviceProber(runner=script).probe(instance="avd-02", serial="emulator-5556")

    assert device.root_implementation is RootImplementation.ADB_ROOT
    assert device.capabilities.magisk is False
    assert device.capabilities.adb_root is True


def test_probe_userdebug_device_without_su_reports_adb_root_only() -> None:
    script = _userdebug_script()

    device = DeviceProber(runner=script).probe(instance="avd-02", serial="emulator-5556")

    assert device.root_implementation is RootImplementation.ADB_ROOT
    assert device.root_state is ComponentState.ENABLED
    assert device.capabilities.adb_root is True
    # adb root is a host capability: it must never be reported as Magisk root.
    assert device.capabilities.magisk is False
    assert device.capabilities.magisk_version == ""
    assert device.capabilities.magisk_environment_ready is False
    assert device.capabilities.zygisk_available is False
    assert device.capabilities.zygisk_enabled is False
    assert device.capabilities.zygisk_active is False
    assert device.capabilities.lsposed_installed is False
    assert device.magisk_modules == []
    assert not any("magisk" in command for command in script.commands)

    assert any("adb root" in note for note in device.capabilities.notes)
    assert any("not adb root" in blocker for blocker in device.blockers)
    assert device.compatible is True

    # The probe really used `adb root` and then verified the rooted adbd.
    assert "root" in script.commands
    assert script.commands[script.commands.index("root") + 1] == "id"


def test_probe_userdebug_device_does_not_adb_root_without_opt_in() -> None:
    script = _userdebug_script()

    device = DeviceProber(runner=script, allow_adb_root=False).probe(
        instance="avd-02", serial="emulator-5556"
    )

    assert device.root_implementation is RootImplementation.NONE
    assert device.compatible is False
    assert "root" not in script.commands
    assert any("opt-in" in note for note in device.capabilities.notes)
    assert any("no root" in blocker for blocker in device.blockers)


def test_probe_non_running_device_is_incompatible_with_blockers() -> None:
    script = ScriptedAdb({})

    device = DeviceProber(runner=script).probe(
        instance="avd-03", serial="", running=False, boot_completed=False
    )

    assert device.running is False
    assert device.compatible is False
    assert device.root_implementation is RootImplementation.NONE
    assert device.root_state is ComponentState.UNKNOWN
    assert device.capabilities.magisk is False
    assert device.magisk_modules == []
    assert device.blockers
    assert "not running" in device.blockers[0]
    assert script.commands == []  # nothing is executed against a stopped AVD


# ---------------------------------------------------------------------------
# Acquisition of pinned toolchain artifacts
# ---------------------------------------------------------------------------
def test_find_artifact_and_the_pinned_release_baseline() -> None:
    magisk = find_artifact("magisk")
    assert magisk.sha256 == "ae1a02b1ab608a51d5bc9b323e0588d06d30d9987ac8da01f4710d76f705dccb"
    assert magisk.size_bytes == 11_411_692
    assert magisk.kind == "apk"
    assert magisk.version.startswith("26.1")
    assert magisk.url.startswith("https://github.com/topjohnwu/Magisk/releases/download/")

    lsposed = find_artifact("LSPosed")  # the lookup is case-insensitive
    assert lsposed.key == "lsposed"
    assert lsposed.sha256 == "0ebc6bcb465d1c4b44b7220ab5f0252e6b4eb7fe43da74650476d2798bb29622"
    assert lsposed.size_bytes == 2_462_055
    assert lsposed.kind == "module_zip"
    assert lsposed.min_api == 27

    assert [artifact.key for artifact in PINNED_ARTIFACTS] == ["magisk", "lsposed"]

    with pytest.raises(NotFoundError) as raised:
        find_artifact("nope")
    assert "no pinned artifact" in raised.value.message
    assert "magisk" in (raised.value.hint or "")
    assert "lsposed" in (raised.value.hint or "")


def test_list_statuses_reports_every_pin_as_absent_on_a_clean_root(tmp_path: Path) -> None:
    store = AcquisitionStore(tmp_path / "toolchain", http_get=ScriptedDownload())

    statuses = store.list_statuses()

    assert len(statuses) == len(PINNED_ARTIFACTS) == 2
    assert {status.key for status in statuses} == {"magisk", "lsposed"}
    assert all(status.present is False for status in statuses)
    assert all(status.verified is False for status in statuses)
    assert all(status.path == "" and status.size_bytes == 0 and status.sha256 == "" for status in statuses)
    assert {status.pinned_sha256 for status in statuses} == {
        artifact.sha256 for artifact in PINNED_ARTIFACTS
    }
    assert {status.source_url for status in statuses} == {artifact.url for artifact in PINNED_ARTIFACTS}
    assert not store.root.exists()  # status inspection never creates anything


def test_artifact_paths_are_key_version_and_filename_for_the_real_pins(tmp_path: Path) -> None:
    store = AcquisitionStore(tmp_path / "toolchain", http_get=ScriptedDownload())

    assert store.artifact_path(find_artifact("magisk")) == (
        store.root / "magisk" / "26.1" / "Magisk-v26.1.apk"
    )
    assert store.artifact_path(find_artifact("lsposed")) == (
        store.root / "lsposed" / "1.9.2" / "LSPosed-v1.9.2-7024-zygisk-release.zip"
    )


def test_acquire_requires_explicit_confirmation(tmp_path: Path) -> None:
    downloads = ScriptedDownload(b"irrelevant")
    store = AcquisitionStore(tmp_path / "toolchain", http_get=downloads)

    with pytest.raises(ConflictError) as raised:
        store.acquire("lsposed", confirm=False)

    assert raised.value.code == "conflict"
    assert "confirmation" in raised.value.message
    assert "GPL-3.0" in (raised.value.hint or "")
    assert downloads.calls == []  # nothing was fetched
    assert not store.root.exists() or list(store.root.rglob("*")) == []


def test_acquire_stores_verifies_and_records_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = b"verified release bytes" * 5
    pin = _test_pin(data)
    _install_pin(monkeypatch, pin)
    downloads = ScriptedDownload(data)
    store = AcquisitionStore(tmp_path / "toolchain", http_get=downloads)

    status = store.acquire(pin.key, confirm=True)

    expected = store.root / pin.key / "9.9.9" / pin.filename
    assert status.present is True
    assert status.verified is True
    assert status.path == str(expected)
    assert status.size_bytes == len(data)
    assert status.sha256 == hashlib.sha256(data).hexdigest()
    assert status.pinned_sha256 == pin.sha256
    assert status.pinned_size_bytes == len(data)
    assert status.source_url == pin.url
    assert status.kind == pin.kind
    assert status.min_api == pin.min_api
    assert status.acquired_at
    assert expected.read_bytes() == data

    # The download is size-capped: the pinned size plus a small slack, announced
    # to the downloader up front.
    assert downloads.calls == [(pin.url, pin.size_bytes + 1024 * 1024)]

    manifest = store.manifest()
    assert len(manifest) == 1
    entry = manifest[0]
    assert entry["key"] == pin.key
    assert entry["name"] == pin.name
    assert entry["version"] == pin.version
    assert entry["kind"] == pin.kind
    assert entry["sha256"] == pin.sha256
    assert entry["size_bytes"] == len(data)
    assert entry["source_url"] == pin.url
    assert entry["licence"] == "GPL-3.0"
    assert entry["acquired_at"]


def test_acquire_rejects_a_hash_mismatch_and_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = b"release-bytes-0123456789"
    tampered = b"release-bytes-9876543210"
    assert len(good) == len(tampered)
    pin = _test_pin(good)
    _install_pin(monkeypatch, pin)
    downloads = ScriptedDownload(tampered)
    store = AcquisitionStore(tmp_path / "toolchain", http_get=downloads)

    with pytest.raises(ValidationError) as raised:
        store.acquire(pin.key, confirm=True)

    assert "integrity" in raised.value.message
    assert pin.sha256 in (raised.value.hint or "")
    assert not (store.root / pin.key / "9.9.9" / pin.filename).exists()
    assert store.manifest() == []
    assert list(store.root.rglob("*")) == []  # no artifact, no .part file, no manifest
    assert len(downloads.calls) == 1


@pytest.mark.parametrize("payload", [b"too-short", b"x" * 32])
def test_acquire_rejects_a_wrong_length_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    data = b"exact bytes here"
    pin = _test_pin(data)
    _install_pin(monkeypatch, pin)
    store = AcquisitionStore(tmp_path / "toolchain", http_get=ScriptedDownload(payload))

    with pytest.raises(ValidationError) as raised:
        store.acquire(pin.key, confirm=True)

    assert "bytes, expected" in raised.value.message
    assert not (store.root / pin.key / "9.9.9" / pin.filename).exists()
    assert store.manifest() == []


def test_acquire_is_idempotent_and_downloads_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = b"idempotent bytes" * 2
    pin = _test_pin(data)
    _install_pin(monkeypatch, pin)
    downloads = ScriptedDownload(data)
    store = AcquisitionStore(tmp_path / "toolchain", http_get=downloads)

    first = store.acquire(pin.key, confirm=True)
    second = store.acquire(pin.key, confirm=True)

    assert second.verified is True
    assert second.sha256 == first.sha256
    assert second.path == first.path
    assert len(downloads.calls) == 1
    assert len(store.manifest()) == 1  # provenance is not duplicated either

    # A fresh store over the same directory trusts the verified file, too.
    reopened_download = ScriptedDownload(b"must-not-be-used")
    reopened = AcquisitionStore(store.root, http_get=reopened_download)
    third = reopened.acquire(pin.key, confirm=True)
    assert third.verified is True
    assert reopened_download.calls == []


def test_acquire_replaces_a_present_but_corrupted_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = b"the real payload"
    pin = _test_pin(data)
    _install_pin(monkeypatch, pin)
    downloads = ScriptedDownload(data)
    store = AcquisitionStore(tmp_path / "toolchain", http_get=downloads)

    target = store.root / pin.key / "9.9.9" / pin.filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"corrupted earlier download")
    assert store.status(pin).present is True
    assert store.status(pin).verified is False

    status = store.acquire(pin.key, confirm=True)

    assert status.verified is True
    assert target.read_bytes() == data
    assert len(downloads.calls) == 1
    assert len(store.manifest()) == 1


# ---------------------------------------------------------------------------
# Service: toolchain acquisition and module resolution
# ---------------------------------------------------------------------------
def _lsposed_pin(data: bytes) -> PinnedArtifact:
    return _test_pin(
        data, key="lsposed", version="1.9.2 (test)", filename="LSPosed-test.zip", kind="module_zip"
    )


def test_service_acquire_toolchain_imports_the_module_zip(
    service: InstrumentationService,
    downloads: ScriptedDownload,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = _lsposed_zip()
    pin = _lsposed_pin(data)
    _install_pin(monkeypatch, pin)
    downloads.payload = data

    result = service.acquire_toolchain("lsposed", confirm=True)

    module_id = result["module_id"]
    assert module_id
    assert result["artifact"]["verified"] is True
    assert Path(result["artifact"]["path"]).is_file()
    assert downloads.calls == [(pin.url, pin.size_bytes + 1024 * 1024)]

    record = service.get_module(module_id)
    assert record.kind is ModuleKind.LSPOSED_ZIP
    assert record.source == "official-release"
    assert record.source_url == pin.url
    assert [module.id for module in service.list_modules(ModuleKind.LSPOSED_ZIP)] == [module_id]

    status = service.toolchain_status()
    assert status["available"] is True
    assert len(status["manifest"]) == 1
    assert status["manifest"][0]["licence"] == "GPL-3.0"
    assert status["manifest"][0]["sha256"] == pin.sha256
    assert status["manifest"][0]["source_url"] == pin.url


def test_service_does_not_duplicate_the_module_when_acquired_twice(
    service: InstrumentationService,
    downloads: ScriptedDownload,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = _lsposed_zip()
    pin = _lsposed_pin(data)
    _install_pin(monkeypatch, pin)
    downloads.payload = data

    first = service.acquire_toolchain("lsposed", confirm=True)
    second = service.acquire_toolchain("lsposed", confirm=True)

    assert first["module_id"] == second["module_id"]
    assert len(service.list_modules(ModuleKind.LSPOSED_ZIP)) == 1
    assert len(downloads.calls) == 1
    assert len(service.toolchain_status()["manifest"]) == 1


def test_service_dedupes_by_sha_when_the_bytes_are_already_in_the_library(
    service: InstrumentationService,
    downloads: ScriptedDownload,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = _lsposed_zip()
    imported = service.import_upload("LSPosed-test.zip", data)
    pin = _lsposed_pin(data)
    _install_pin(monkeypatch, pin)
    downloads.payload = data

    result = service.acquire_toolchain("lsposed", confirm=True)

    assert result["module_id"] == imported.id
    assert len(service.list_modules(ModuleKind.LSPOSED_ZIP)) == 1


def test_install_lsposed_step_resolves_the_only_library_module(
    service: InstrumentationService,
) -> None:
    imported = service.import_upload("LSPosed-test.zip", _lsposed_zip())
    step = ProfileStep(id="lsposed-install", action=StepAction.INSTALL_LSPOSED)

    # The fallback is deliberately internal: it is what a built-in profile
    # (which cannot know the operator's module ids) relies on.
    record = service._module_for(step, ModuleKind.LSPOSED_ZIP, "LSPosed")

    assert record.id == imported.id
    assert record.kind is ModuleKind.LSPOSED_ZIP


def test_install_lsposed_step_without_any_module_points_at_the_toolchain(
    service: InstrumentationService,
) -> None:
    step = ProfileStep(id="lsposed-install", action=StepAction.INSTALL_LSPOSED)

    with pytest.raises(NotFoundError) as raised:
        service._module_for(step, ModuleKind.LSPOSED_ZIP, "LSPosed")

    assert "no LSPosed" in raised.value.message
    hint = raised.value.hint or ""
    assert "toolchain acquire" in hint
    assert "lsposed" in hint


def test_module_binding_still_wins_and_wrong_kinds_are_rejected(
    service: InstrumentationService,
) -> None:
    lsposed = service.import_upload("LSPosed-test.zip", _lsposed_zip())
    plain = service.import_upload("plain.zip", _magisk_zip())

    bound = ProfileStep(id="bound", action=StepAction.INSTALL_LSPOSED, module_id=lsposed.id)
    assert service._module_for(bound, ModuleKind.LSPOSED_ZIP, "LSPosed").id == lsposed.id

    wrong = ProfileStep(id="wrong", action=StepAction.INSTALL_LSPOSED, module_id=plain.id)
    with pytest.raises(ValidationError):
        service._module_for(wrong, ModuleKind.LSPOSED_ZIP, "LSPosed")


def test_install_module_without_a_binding_still_needs_an_explicit_module(
    service: InstrumentationService,
) -> None:
    """Only the kind-specific step may fall back; a generic install must be bound.

    Guessing an artifact for ``install_module`` could install an unrelated module,
    so the library-wide fallback is deliberately limited to ``install_lsposed``.
    """
    service.import_upload("LSPosed-test.zip", _lsposed_zip())
    step = ProfileStep(id="apk-install", action=StepAction.INSTALL_MODULE)

    with pytest.raises(ValidationError):
        service._module_for(step, None, "module")


def test_service_without_acquisition_reports_unavailable(
    repository: InstrumentationRepository, library: ModuleLibrary
) -> None:
    def resolve(name: str) -> TargetDevice:
        return TargetDevice(instance=name, serial="emulator-5554", running=True, boot_completed=True)

    service = InstrumentationService(
        repository, library, DeviceProber(runner=_magisk_script()), resolve
    )

    assert service.toolchain_status() == {"artifacts": [], "manifest": [], "available": False}
    with pytest.raises(ConfigError):
        service.acquire_toolchain("lsposed", confirm=True)


# ---------------------------------------------------------------------------
# Xposed inventory (LSPosed database) and its step actions
# ---------------------------------------------------------------------------
def test_xposed_modules_parses_the_lsposed_tables() -> None:
    script = _magisk_script(modules={"zygisk_lsposed": LSPOSED_PROP})
    script.responses["command -v sqlite3"] = (0, "/system/bin/sqlite3\n")
    script.responses[
        f"sqlite3 {LSPOSED_DB} 'select module_pkg_name, enabled from modules'"
    ] = (0, "com.example.hook|1\ncom.example.other|0\n")
    script.responses[
        f"sqlite3 {LSPOSED_DB} 'select m.module_pkg_name, s.app_pkg_name from modules m"
        " join scope s on s.mid = m.mid'"
    ] = (
        0,
        "com.example.hook|com.example.target\n"
        "com.example.hook|com.example.other\n"
        "com.example.other|com.example.target\n",
    )

    modules, notes = DeviceProber(runner=script).xposed_modules("emulator-5554")

    assert [module.identifier for module in modules] == ["com.example.hook", "com.example.other"]
    assert all(module.kind is ModuleKind.XPOSED_APK for module in modules)
    assert all(module.source == "lsposed" for module in modules)
    assert modules[0].enabled is True and modules[0].active is True
    assert modules[1].enabled is False and modules[1].active is False
    # Scopes are de-duplicated and sorted; each module keeps its own apps.
    assert modules[0].scope == ["com.example.other", "com.example.target"]
    assert modules[1].scope == ["com.example.target"]
    assert notes == []


def test_xposed_modules_reports_unavailable_without_sqlite3() -> None:
    modules, notes = DeviceProber(runner=_magisk_script()).xposed_modules("emulator-5554")

    assert modules == []
    assert len(notes) == 1
    assert "no sqlite3" in notes[0]
    assert "unavailable" in notes[0]


def test_xposed_modules_notes_when_the_database_cannot_be_read() -> None:
    script = _magisk_script(modules={"zygisk_lsposed": LSPOSED_PROP})
    script.responses["command -v sqlite3"] = (0, "/system/bin/sqlite3\n")
    # The modules query is left unscripted, so sqlite3 exits non-zero.

    modules, notes = DeviceProber(runner=script).xposed_modules("emulator-5554")

    assert modules == []
    assert any("could not read the LSPosed database" in note for note in notes)


def test_enable_xposed_module_rejects_an_injected_package_name(
    service: InstrumentationService,
) -> None:
    step = ProfileStep(
        id="enable-xposed",
        action=StepAction.ENABLE_XPOSED_MODULE,
        params={"package": "com.example.hook'; DROP TABLE modules;--"},
    )

    with pytest.raises(ValidationError) as raised:
        service._step_enable_xposed_module(step, _target())

    assert "invalid Android package" in raised.value.message
    assert "com.example.module" in (raised.value.hint or "")


def test_set_xposed_scope_rejects_an_injected_application_name(
    service: InstrumentationService,
) -> None:
    step = ProfileStep(
        id="scope",
        action=StepAction.SET_XPOSED_SCOPE,
        params={
            "package": "com.example.hook",
            "apps": ["com.example.target", "com.example.x'); DELETE FROM scope;--"],
        },
    )

    with pytest.raises(ValidationError) as raised:
        service._step_set_xposed_scope(step, _target())

    assert "invalid Android application package" in raised.value.message


def test_enable_xposed_module_writes_the_lsposed_database(
    service: InstrumentationService, adb: ScriptedAdb
) -> None:
    update = (
        f"sqlite3 {LSPOSED_DB} \"UPDATE modules SET enabled=1"
        " WHERE module_pkg_name='com.example.hook'\""
    )
    adb.responses[update] = (0, "")
    # The step fails closed on an unregistered package, so the registration lookup
    # must answer with a row id first.
    adb.responses[
        f"sqlite3 {LSPOSED_DB} \"select mid from modules where"
        " module_pkg_name='com.example.hook'\""
    ] = (0, "7\n")
    step = ProfileStep(
        id="enable-xposed",
        action=StepAction.ENABLE_XPOSED_MODULE,
        params={"package": "com.example.hook"},
    )

    result = service._step_enable_xposed_module(step, _target())

    assert result.ok is True
    assert result.state is ComponentState.ENABLED
    assert result.data["package"] == "com.example.hook"
    assert result.data["reboot_required"] is True
    assert "reboot" in result.message
    assert update in adb.commands


def test_set_xposed_scope_reports_a_package_lsposed_does_not_know(
    service: InstrumentationService,
) -> None:
    step = ProfileStep(
        id="scope",
        action=StepAction.SET_XPOSED_SCOPE,
        params={"package": "com.example.unknown", "apps": ["com.example.target"]},
    )

    result = service._step_set_xposed_scope(step, _target())

    assert result.ok is False
    assert result.state is ComponentState.UNSUPPORTED
    assert "does not recognise" in result.message
    assert "com.example.unknown" in result.message


# ---------------------------------------------------------------------------
# Magisk "Additional Setup" and the LSPosed support matrix
# ---------------------------------------------------------------------------
def _acquire_fake_magisk(
    service: InstrumentationService,
    downloads: ScriptedDownload,
    monkeypatch: pytest.MonkeyPatch,
) -> bytes:
    data = b"fake Magisk APK payload"
    pin = _test_pin(
        data, key="magisk", version="26.1 (test)", filename="Magisk-test.apk", kind="apk"
    )
    _install_pin(monkeypatch, pin)
    downloads.payload = data
    service.acquire_toolchain("magisk", confirm=True)
    return data


def test_complete_magisk_setup_requires_the_acquired_apk(
    service: InstrumentationService,
) -> None:
    step = ProfileStep(id="magisk-setup", action=StepAction.COMPLETE_MAGISK_SETUP)

    with pytest.raises(ValidationError) as raised:
        service._step_complete_magisk_setup(step, _target())

    assert "pinned Magisk APK has not been acquired" in raised.value.message
    assert "toolchain acquire magisk" in (raised.value.hint or "")


def test_complete_magisk_setup_is_a_noop_when_the_environment_is_ready(
    service: InstrumentationService,
    downloads: ScriptedDownload,
    monkeypatch: pytest.MonkeyPatch,
    adb: ScriptedAdb,
) -> None:
    _acquire_fake_magisk(service, downloads, monkeypatch)
    step = ProfileStep(id="magisk-setup", action=StepAction.COMPLETE_MAGISK_SETUP)

    result = service._step_complete_magisk_setup(step, _target())

    assert result.ok is True
    assert result.state is ComponentState.ACTIVE
    assert "already complete" in result.message
    assert not any("install" in argv for argv in adb.argvs)


def test_complete_magisk_setup_unsupported_when_magisk_is_not_running(
    repository: InstrumentationRepository,
    library: ModuleLibrary,
    tmp_path: Path,
    downloads: ScriptedDownload,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _magisk_script(magisk_installed=False)
    service = _service_with_runner(repository, library, tmp_path, downloads, script)
    _acquire_fake_magisk(service, downloads, monkeypatch)
    step = ProfileStep(id="magisk-setup", action=StepAction.COMPLETE_MAGISK_SETUP)

    result = service._step_complete_magisk_setup(step, _target())

    assert result.ok is False
    assert result.state is ComponentState.UNSUPPORTED
    assert "install_magisk" in result.message
    assert not any("install" in argv for argv in script.argvs)


def test_install_lsposed_refuses_an_api_25_target(service: InstrumentationService) -> None:
    """LSPosed 1.9.2 aborts below API 27 (verified live), so the step fails early."""
    step = ProfileStep(id="lsposed-install", action=StepAction.INSTALL_LSPOSED)

    result = service._step_install_lsposed(step, _target(api_level=25))

    assert result.ok is False
    assert result.state is ComponentState.UNSUPPORTED
    assert "API 27" in result.message
    assert "API 25" in result.message


# ---------------------------------------------------------------------------
# Built-in profile ordering
# ---------------------------------------------------------------------------
def test_builtin_profiles_have_the_documented_step_order() -> None:
    by_name = {profile.name: profile for profile in BUILTIN_PROFILES}

    root = by_name["Root"].steps
    assert [step.action for step in root] == [
        StepAction.ENABLE_ADB_ROOT,
        StepAction.VERIFY_ROOT,
    ]
    assert root[0].id == "adb-root"

    magisk = by_name["Magisk + Zygisk"].steps
    assert [step.action for step in magisk] == [
        StepAction.INSTALL_MAGISK,
        StepAction.COMPLETE_MAGISK_SETUP,
        StepAction.ENABLE_ZYGISK,
        StepAction.REBOOT,
        StepAction.VERIFY,
    ]
    assert magisk[3].id == "zygisk-reboot"

    lsposed = by_name["LSPosed"].steps
    assert [step.action for step in lsposed] == [
        StepAction.INSTALL_MAGISK,
        StepAction.COMPLETE_MAGISK_SETUP,
        StepAction.ENABLE_ZYGISK,
        StepAction.INSTALL_LSPOSED,
        StepAction.REBOOT,
        StepAction.INSTALL_LSPOSED_MANAGER,
        StepAction.VERIFY,
    ]
    assert [step.id for step in lsposed[4:6]] == ["lsposed-reboot", "lsposed-manager"]

    xposed = by_name["Xposed Instrumentation"].steps
    assert [step.action for step in xposed] == [
        StepAction.INSTALL_MAGISK,
        StepAction.COMPLETE_MAGISK_SETUP,
        StepAction.ENABLE_ZYGISK,
        StepAction.INSTALL_LSPOSED,
        StepAction.REBOOT,
        StepAction.INSTALL_LSPOSED_MANAGER,
        StepAction.INSTALL_MODULE,
        StepAction.VERIFY,
    ]
    assert xposed[6].id == "xposed-apk-install"


# ---------------------------------------------------------------------------
# Host-side root and writable-system steps
# ---------------------------------------------------------------------------
def test_enable_adb_root_restarts_adbd(service: InstrumentationService, adb: ScriptedAdb) -> None:
    adb.responses["root"] = (0, "restarting adbd as root\n")
    adb.responses["id"] = (0, "uid=0(root) gid=0(root)\n")
    step = ProfileStep(id="adb-root", action=StepAction.ENABLE_ADB_ROOT)

    result = service._step_enable_adb_root(step, _target())

    assert result.ok is True
    assert result.state is ComponentState.ENABLED
    assert result.data["adb_root"] is True
    assert "root" in adb.commands


def test_enable_adb_root_unsupported_on_a_production_build(
    service: InstrumentationService, adb: ScriptedAdb
) -> None:
    adb.responses["root"] = (1, "adbd cannot run as root in production builds\n")
    step = ProfileStep(id="adb-root", action=StepAction.ENABLE_ADB_ROOT)

    result = service._step_enable_adb_root(step, _target())

    assert result.ok is False
    assert result.state is ComponentState.UNSUPPORTED
    assert "production/user build" in result.message
    assert "root" in adb.commands


def test_enable_writable_system_reports_a_writable_system(
    service: InstrumentationService, adb: ScriptedAdb
) -> None:
    adb.responses["root"] = (0, "restarting adbd as root\n")
    adb.responses["remount"] = (0, "remount succeeded\n")
    adb.responses["mount"] = (0, "/dev/block/by-name/system /system ext4 rw,seclabel 0 0\n")
    step = ProfileStep(id="writable", action=StepAction.ENABLE_WRITABLE_SYSTEM)

    result = service._step_enable_writable_system(step, _target())

    assert result.ok is True
    assert result.state is ComponentState.ENABLED
    assert result.data == {"writable_system": True, "reboot_required": False}
    assert {"root", "remount", "mount"} <= set(adb.commands)


def test_enable_writable_system_overlayfs_needs_a_reboot(
    service: InstrumentationService, adb: ScriptedAdb
) -> None:
    adb.responses["root"] = (0, "restarting adbd as root\n")
    adb.responses["remount"] = (0, "remount succeeded\n")
    # The base script's mount output keeps /system read-only (overlayfs case).
    step = ProfileStep(id="writable", action=StepAction.ENABLE_WRITABLE_SYSTEM)

    result = service._step_enable_writable_system(step, _target())

    assert result.ok is False
    assert result.state is ComponentState.INSTALLED
    assert result.data["writable_system"] is False
    assert result.data["reboot_required"] is True
    assert "overlayfs" in result.message


def test_enable_writable_system_reports_a_remount_failure(
    service: InstrumentationService, adb: ScriptedAdb
) -> None:
    adb.responses["root"] = (0, "restarting adbd as root\n")
    adb.responses["remount"] = (1, "remount failed: Operation not permitted\n")
    step = ProfileStep(id="writable", action=StepAction.ENABLE_WRITABLE_SYSTEM)

    result = service._step_enable_writable_system(step, _target())

    assert result.ok is False
    assert "remount failed: Operation not permitted" in result.message
    assert result.data["reboot_required"] is False


# ---------------------------------------------------------------------------
# LSPosed manager and removal steps
# ---------------------------------------------------------------------------
def test_install_lsposed_manager_requires_the_module(service: InstrumentationService) -> None:
    step = ProfileStep(id="manager", action=StepAction.INSTALL_LSPOSED_MANAGER)

    result = service._step_install_lsposed_manager(step, _target())

    assert result.ok is False
    assert result.state is ComponentState.UNSUPPORTED
    assert "not installed" in result.message


def test_install_lsposed_manager_installs_and_verifies(
    service: InstrumentationService, adb: ScriptedAdb
) -> None:
    adb.responses["[ -f /data/adb/modules/zygisk_lsposed/manager.apk ] && echo yes"] = (0, "yes\n")
    adb.responses["pm install -r /data/adb/modules/zygisk_lsposed/manager.apk"] = (0, "Success\n")
    adb.responses["pm list packages org.lsposed.manager"] = (0, "package:org.lsposed.manager\n")
    step = ProfileStep(id="manager", action=StepAction.INSTALL_LSPOSED_MANAGER)

    result = service._step_install_lsposed_manager(step, _target())

    assert result.ok is True
    assert result.state is ComponentState.ACTIVE
    assert result.data["package"] == "org.lsposed.manager"
    assert "pm install -r /data/adb/modules/zygisk_lsposed/manager.apk" in adb.commands


def test_install_lsposed_manager_reports_an_install_failure(
    service: InstrumentationService, adb: ScriptedAdb
) -> None:
    adb.responses["[ -f /data/adb/modules/zygisk_lsposed/manager.apk ] && echo yes"] = (0, "yes\n")
    adb.responses["pm install -r /data/adb/modules/zygisk_lsposed/manager.apk"] = (
        1,
        "Failure [INSTALL_FAILED_INVALID_APK]\n",
    )
    adb.responses["pm list packages org.lsposed.manager"] = (0, "")
    step = ProfileStep(id="manager", action=StepAction.INSTALL_LSPOSED_MANAGER)

    result = service._step_install_lsposed_manager(step, _target())

    assert result.ok is False
    assert "INSTALL_FAILED_INVALID_APK" in result.message


def test_remove_lsposed_absent_is_a_noop(
    service: InstrumentationService, adb: ScriptedAdb
) -> None:
    adb.responses["[ -d /data/adb/modules/zygisk_lsposed ] && echo yes || echo no"] = (
        1,
        "no\n",
    )
    step = ProfileStep(id="remove-lsposed", action=StepAction.REMOVE_LSPOSED)
    assert service._prober is not None  # the fake answers "no" for an absent module

    result = service._step_remove_lsposed(step, _target())

    assert result.ok is True
    assert result.state is ComponentState.ABSENT
    assert "not installed" in result.message


def test_remove_lsposed_writes_the_remove_marker(
    service: InstrumentationService, adb: ScriptedAdb
) -> None:
    adb.responses[
        "[ -d /data/adb/modules/zygisk_lsposed ] && echo yes || echo no"
    ] = (0, "yes\n")
    adb.responses["touch /data/adb/modules/zygisk_lsposed/remove"] = (0, "")
    step = ProfileStep(id="remove-lsposed", action=StepAction.REMOVE_LSPOSED)

    result = service._step_remove_lsposed(step, _target())

    assert result.ok is True
    assert result.state is ComponentState.INSTALLED
    assert result.data["reboot_required"] is True
    assert "touch /data/adb/modules/zygisk_lsposed/remove" in adb.commands


def test_remove_magisk_is_always_unsupported(
    service: InstrumentationService, adb: ScriptedAdb
) -> None:
    step = ProfileStep(id="remove-magisk", action=StepAction.REMOVE_MAGISK)

    result = service._step_remove_magisk(step, _target())

    assert result.ok is False
    assert result.state is ComponentState.UNSUPPORTED
    assert "restoring the image ramdisk" in result.message
    assert "ramdisk.img" in result.message
    assert adb.commands == []  # it refuses without touching the device


# ---------------------------------------------------------------------------
# Reboot step
# ---------------------------------------------------------------------------
class FakeClock:
    """Virtual clock so a boot poll costs no real time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_reboot_step_waits_for_boot_completed(
    service: InstrumentationService, adb: ScriptedAdb, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    monkeypatch.setattr("avdfleet.instrumentation.service.time", clock)
    adb.responses["reboot"] = (0, "")
    adb.responses["getprop sys.boot_completed"] = (0, "1\n")
    step = ProfileStep(id="reboot", action=StepAction.REBOOT, params={"timeout_s": 1})

    result = service._step_reboot(step, _target())

    assert result.ok is True
    assert result.state is ComponentState.ACTIVE
    assert result.data["timeout_s"] == 1.0
    assert "reboot" in adb.commands
    assert "getprop sys.boot_completed" in adb.commands


def test_reboot_step_reports_a_boot_timeout(
    service: InstrumentationService, adb: ScriptedAdb, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    monkeypatch.setattr("avdfleet.instrumentation.service.time", clock)
    adb.responses["reboot"] = (0, "")
    adb.responses["getprop sys.boot_completed"] = (0, "0\n")
    step = ProfileStep(id="reboot", action=StepAction.REBOOT, params={"timeout_s": 1})

    result = service._step_reboot(step, _target())

    assert result.ok is False
    assert "did not finish booting within 1s" in result.message


def test_reboot_step_does_not_wait_past_its_timeout(
    service: InstrumentationService, adb: ScriptedAdb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``params.timeout_s`` is documented as the wait budget.

    Defect: ``_step_reboot`` sleeps a fixed five seconds *before* the first poll
    (``service.py:911-912``), so a one-second budget waits five seconds — and it
    can still report success after the budget expired because the deadline is
    never re-checked. The sleep must be clamped to the remaining budget.
    """
    clock = FakeClock()
    monkeypatch.setattr("avdfleet.instrumentation.service.time", clock)
    adb.responses["reboot"] = (0, "")
    adb.responses["getprop sys.boot_completed"] = (0, "1\n")
    step = ProfileStep(id="reboot", action=StepAction.REBOOT, params={"timeout_s": 1})

    service._step_reboot(step, _target())

    assert sum(clock.sleeps) <= 1.0, f"slept {sum(clock.sleeps)}s for a 1s budget"


# ---------------------------------------------------------------------------
# Events on the daemon bus
# ---------------------------------------------------------------------------
class RecordingBus:
    """Fake event bus that records everything published."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def publish(self, event: Event) -> int:
        self.events.append(event)
        return 1


class ExplodingBus:
    """A bus whose publish always raises; telemetry must never break a run."""

    def publish(self, event: Event) -> None:
        raise RuntimeError("event bus is down")


def _execute_probe_run(
    service: InstrumentationService, name: str, targets: list[str]
) -> tuple[InstrumentationProfile, DeploymentRun, DeploymentRun]:
    profile = service.save_profile(
        profile_id=None, name=name, steps=[{"action": "verify_root"}]
    )
    run = service.start_run(profile_id=profile.id, targets=targets)
    return profile, run, service.execute_run(run.id)


def test_execute_run_publishes_started_and_finished_events(
    repository: InstrumentationRepository,
    library: ModuleLibrary,
    tmp_path: Path,
    downloads: ScriptedDownload,
) -> None:
    bus = RecordingBus()
    service = _service_with_runner(
        repository,
        library,
        tmp_path,
        downloads,
        _magisk_script(),
        events=bus,
        running={"avd-01": True, "avd-stopped": False},
    )

    profile, run, finished = _execute_probe_run(service, "Event probe", ["avd-01", "avd-stopped"])

    assert [event.payload["action"] for event in bus.events] == ["started", "finished"]
    assert all(event.type == EventType.INSTRUMENTATION_RUN for event in bus.events)
    assert all(event.source == "instrumentation" for event in bus.events)

    started = bus.events[0].payload
    assert started["run_id"] == run.id
    assert started["profile_id"] == profile.id
    assert started["targets"] == ["avd-01", "avd-stopped"]

    done = bus.events[1].payload
    assert done["run_id"] == run.id
    assert done["state"] == "partial"
    assert done["targets"] == ["avd-01", "avd-stopped"]
    assert done["failed"] == ["avd-stopped"]  # exactly the target that failed
    assert finished.state is RunState.PARTIAL


def test_execute_run_survives_a_failing_event_bus(
    repository: InstrumentationRepository,
    library: ModuleLibrary,
    tmp_path: Path,
    downloads: ScriptedDownload,
) -> None:
    service = _service_with_runner(
        repository,
        library,
        tmp_path,
        downloads,
        _magisk_script(),
        events=ExplodingBus(),
        running={"avd-01": True, "avd-stopped": False},
    )

    _profile, _run, finished = _execute_probe_run(service, "Event probe 2", ["avd-01", "avd-stopped"])

    assert finished.state is RunState.PARTIAL
    assert [result.instance for result in finished.results] == ["avd-01", "avd-stopped"]
    assert finished.results[0].ok is True
    assert finished.results[1].skipped is True

def test_dialog_target_prefers_the_confirm_button_over_the_message() -> None:
    """Regression: Magisk's body text contains "proceed", which used to be tapped.

    The real API-34 dump has the message node before the buttons, so a substring
    match on the whole tree hit the text view and the dialog never dismissed.
    """
    import xml.etree.ElementTree as ElementTree

    from avdfleet.instrumentation.service import _pick_dialog_target

    document = """<hierarchy>
      <node text="Requires Additional Setup" clickable="false" bounds="[84,1084][996,1146]" />
      <node text="Your device needs additional setup. Do you want to proceed and reboot?"
            clickable="false" bounds="[84,1167][996,1253]" />
      <node text="CANCEL" resource-id="com.topjohnwu.magisk:id/dialog_base_button_3"
            clickable="true" bounds="[544,1285][775,1411]" />
      <node text="OK" resource-id="com.topjohnwu.magisk:id/dialog_base_button_1"
            clickable="true" bounds="[775,1285][1006,1411]" />
    </hierarchy>"""
    root = ElementTree.fromstring(document)

    target = _pick_dialog_target(root, ("ok", "allow", "proceed", "yes", "reboot"))

    assert target == (890, 1348)
