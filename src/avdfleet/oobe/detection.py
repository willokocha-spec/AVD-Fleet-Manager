"""OOBE detection primitives: adb access, ``dumpsys`` parsing and screenshots.

This module is deliberately dependency-free apart from the standard library and
``avdfleet.core``.  Nothing here talks to a real emulator directly: every host
process goes through an injected *adb runner*, so unit tests can supply simple
fakes and the runtime service can reuse its own subprocess helper.

Detection model
---------------
The first-boot Setup Wizard is identified from the current top activity, which
is read from ``adb -s <serial> shell dumpsys activity activities`` and, as a
fallback, from ``dumpsys window`` (``mCurrentFocus`` / ``mFocusedApp``).  The
top activity is classified as:

``setup_wizard``
    The Setup Wizard (``com.google.android.setupwizard`` /
    ``SetupWizardActivity`` and equivalents) is in the foreground: the guest is
    still in the out-of-box experience.
``provisioning``
    The guest is in the unprovisioned first-boot state
    (``com.android.settings/.FallbackHome`` or a provisioning activity).  Stock
    emulator images have no Setup Wizard, so this is the furthest "not set up"
    state they can reach.
``launcher``
    A launcher/home activity is in the foreground: first-run setup has already
    completed on this AVD.
``unknown``
    No activity could be parsed or the foreground app is neither of the above.
"""

from __future__ import annotations

import inspect
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from ..core.childenv import child_environment
from ..core.errors import OobeError
from ..core.logging import get_logger
from ..core.models import utcnow

logger = get_logger("avdfleet.oobe.detection")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

SETUP_WIZARD_PACKAGES: tuple[str, ...] = (
    "com.google.android.setupwizard",
    "com.android.setupwizard",
)

LAUNCHER_PACKAGES: tuple[str, ...] = (
    "com.google.android.apps.nexuslauncher",
    "com.android.launcher3",
    "com.android.launcher",
)

PROVISIONING_TOKENS: tuple[str, ...] = (
    "fallbackhome",
    "com.android.provision",
    "provisionactivity",
    # Emulator SDK-setup stubs.  Two distinct packages have been observed on
    # real boots: com.google.android.googlesdksetup (API 34) and
    # com.google.android.sdksetup (API 25).  Match the common suffix so both are
    # covered and any future variant is classified instead of reported unknown.
    "sdksetup",
)
"""Tokens identifying the unprovisioned first-boot state.

Stock emulator system images (AOSP ``default``, ``google_apis`` and
``google_apis_playstore``) ship **no** Google Setup Wizard and bake
``ro.setupwizard.mode=DISABLED`` into the image, so a freshly wiped AVD never
shows a visual wizard.  What it does show while first-run setup is still
pending is one of two placeholder activities, both observed on real boots:

* ``com.google.android.googlesdksetup/.DefaultActivity`` - the emulator's own
  SDK-setup stub, shown immediately after boot; and
* ``com.android.settings/.FallbackHome`` - the blank HOME placeholder the
  framework keeps until ``device_provisioned`` flips to 1.

That state is the closest thing to an OOBE those images can reach, so it is
classified explicitly instead of being reported as ``unknown``.
"""


# ---------------------------------------------------------------------------
# Process boundary
# ---------------------------------------------------------------------------


@dataclass
class RunOutput:
    """Normalised result of one host command."""

    returncode: int = 0
    stdout: str | bytes = ""
    stderr: str | bytes = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def text(self) -> str:
        return _as_text(self.stdout)

    def error_text(self) -> str:
        return _as_text(self.stderr)

    def binary(self) -> bytes:
        return _as_bytes(self.stdout)


class CommandRunner(Protocol):
    """Minimal process runner protocol (implemented by the adb backends)."""

    def run(self, argv: Sequence[str], timeout_s: float | None = None) -> Any: ...


class SubprocessRunner:
    """Default adb runner used when no backend runner is injected.

    The daemon's container wires the services with an ``adb`` executable path
    only, so this keeps ``OobeManager`` fully functional while still allowing
    tests and N1 to inject their own process boundary.
    """

    def __init__(
        self,
        argv0: str = "adb",
        *,
        executable: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.executable = str(executable or argv0)
        self._env: dict[str, str] = child_environment(env)
        self._timeout_s = float(timeout_s)

    def run(self, argv: Sequence[str], timeout_s: float | None = None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(  # noqa: S603 - argv list, never shell=True
            [str(item) for item in argv],
            capture_output=True,
            timeout=self._timeout_s if timeout_s is None else timeout_s,
            env=self._env,
        )


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _as_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    # A text-decoded runner cannot round-trip binary PNG data; latin-1 is the
    # closest reversible mapping and is validated with the PNG signature later.
    return str(value).encode("latin-1", "replace")


def normalize_run_result(result: Any) -> RunOutput:
    """Accept ``CompletedProcess``/fake results and return a :class:`RunOutput`."""
    if isinstance(result, RunOutput):
        return result
    if result is None:
        return RunOutput(returncode=0)
    returncode = getattr(result, "returncode", None)
    if returncode is None:
        returncode = getattr(result, "exit_code", None)
    if returncode is None:
        returncode = getattr(result, "code", 0)
    return RunOutput(
        returncode=_coerce_int(returncode, 1),
        stdout=getattr(result, "stdout", ""),
        stderr=getattr(result, "stderr", ""),
    )


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _accepts_keyword(function: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):  # pragma: no cover - C callables
        return False
    if name in parameters:
        return True
    return any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())


def call_runner(
    runner: CommandRunner | Callable[..., Any] | None,
    argv: Sequence[str],
    *,
    timeout_s: float | None = None,
) -> RunOutput:
    """Run *argv* through an injected runner, normalising the result."""
    if runner is None:
        raise OobeError(
            "no adb runner is configured for OOBE detection",
            hint="inject a runner with run(argv, timeout_s=...) into OobeManager",
        )
    argv_list = [str(item) for item in argv]
    try:
        if hasattr(runner, "run"):
            run = runner.run  # type: ignore[union-attr]
            if _accepts_keyword(run, "timeout_s"):
                return normalize_run_result(run(argv_list, timeout_s=timeout_s))
            if _accepts_keyword(run, "timeout"):
                return normalize_run_result(run(argv_list, timeout=timeout_s))  # type: ignore[call-arg]
            return normalize_run_result(run(argv_list))
        if callable(runner):
            return normalize_run_result(runner(argv_list))
    except OobeError:
        raise
    except Exception as exc:  # noqa: BLE001 - process failures are data here
        return RunOutput(returncode=-1, stderr=f"{type(exc).__name__}: {exc}")
    raise OobeError(
        "the injected OOBE runner is neither callable nor exposes run()",
        hint="pass an object with run(argv) or a plain callable",
    )


# ---------------------------------------------------------------------------
# dumpsys parsing
# ---------------------------------------------------------------------------


class ActivityKind(StrEnum):
    SETUP_WIZARD = "setup_wizard"
    PROVISIONING = "provisioning"
    LAUNCHER = "launcher"
    UNKNOWN = "unknown"


_ACTIVITY_TOKEN = re.compile(
    r"(?P<pkg>[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)/(?P<cls>[A-Za-z0-9_.$]+)"
)

# Ordered by trust: modern dumpsys emits topResumedActivity; mResumedActivity is
# the older equivalent; the window manager fallbacks are only used when the
# activity service output has no usable line.
_TOP_ACTIVITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("topResumedActivity", re.compile(r"topResumedActivity\s*[=:]\s*")),
    ("mResumedActivity", re.compile(r"mResumedActivity\s*[=:]\s*")),
    ("ResumedActivity", re.compile(r"(?<![A-Za-z])ResumedActivity\s*[=:]\s*")),
    ("mFocusedApp", re.compile(r"mFocusedApp\s*[=:]\s*")),
    ("mCurrentFocus", re.compile(r"mCurrentFocus\s*[=:]\s*")),
    ("mFocusedWindow", re.compile(r"mFocusedWindow\s*[=:]\s*")),
)

_WINDOW_ONLY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("mCurrentFocus", re.compile(r"mCurrentFocus\s*[=:]\s*")),
    ("mFocusedApp", re.compile(r"mFocusedApp\s*[=:]\s*")),
    ("mFocusedWindow", re.compile(r"mFocusedWindow\s*[=:]\s*")),
)


@dataclass(frozen=True)
class TopActivity:
    """A parsed foreground activity."""

    activity: str = ""
    package: str = ""
    component: str = ""
    source: str = ""

    @property
    def present(self) -> bool:
        return bool(self.activity)


def normalize_component(package: str, short_class: str) -> tuple[str, str]:
    """Return ``(display, fully_qualified_class)`` for a ``pkg/class`` pair."""
    package = package.strip()
    short_class = short_class.strip()
    if short_class.startswith("."):
        full = f"{package}{short_class}"
    elif "." not in short_class:
        full = f"{package}.{short_class}"
    else:
        full = short_class
    display_class = short_class
    if not display_class.startswith(".") and display_class.startswith(package + "."):
        display_class = "." + display_class[len(package) + 1 :]
    return f"{package}/{display_class}", full


def _extract_activity(line: str) -> TopActivity | None:
    match = _ACTIVITY_TOKEN.search(line)
    if match is None:
        return None
    display, full = normalize_component(match.group("pkg"), match.group("cls"))
    return TopActivity(
        activity=display,
        package=match.group("pkg"),
        component=full,
    )


def _find_in_dump(dump: str, patterns: Sequence[tuple[str, re.Pattern[str]]]) -> TopActivity | None:
    if not dump:
        return None
    for source, pattern in patterns:
        for line in dump.splitlines():
            if pattern.search(line) is None:
                continue
            candidate = _extract_activity(line)
            if candidate is not None:
                return TopActivity(
                    activity=candidate.activity,
                    package=candidate.package,
                    component=candidate.component,
                    source=source,
                )
    return None


def extract_top_activity(activity_dump: str = "", window_dump: str = "") -> TopActivity:
    """Return the top activity from ``dumpsys activity activities`` / ``dumpsys window``."""
    found = _find_in_dump(activity_dump, _TOP_ACTIVITY_PATTERNS)
    if found is not None:
        return found
    found = _find_in_dump(window_dump, _WINDOW_ONLY_PATTERNS)
    if found is not None:
        return found
    return TopActivity()


def classify_top_activity(top: TopActivity) -> ActivityKind:
    """Classify a parsed top activity into the OOBE taxonomy."""
    if not top.present:
        return ActivityKind.UNKNOWN
    haystack = f"{top.package} {top.component} {top.activity}".lower()
    if any(package.lower() in haystack for package in SETUP_WIZARD_PACKAGES):
        return ActivityKind.SETUP_WIZARD
    if "setupwizard" in haystack or "setup_wizard" in haystack:
        return ActivityKind.SETUP_WIZARD
    if "oobe" in haystack and "setup" in haystack:
        return ActivityKind.SETUP_WIZARD
    if any(token in haystack for token in PROVISIONING_TOKENS):
        return ActivityKind.PROVISIONING
    if any(package.lower() in haystack for package in LAUNCHER_PACKAGES):
        return ActivityKind.LAUNCHER
    if "launcher" in haystack or "homeactivity" in haystack or "launcherhome" in haystack:
        return ActivityKind.LAUNCHER
    return ActivityKind.UNKNOWN


@dataclass
class Detection:
    """Everything one detection pass learned about a booted instance."""

    serial: str = ""
    top: TopActivity = field(default_factory=TopActivity)
    kind: ActivityKind = ActivityKind.UNKNOWN
    api_level: int = 0
    abi: str = ""
    release: str = ""
    boot_completed: bool = False
    activity_dump: str = ""
    window_dump: str = ""
    errors: list[str] = field(default_factory=list)
    detected_at: Any = field(default_factory=utcnow)

    @property
    def activity(self) -> str:
        return self.top.activity

    def evidence_text(self, *, image_id: str = "", avd_name: str = "") -> str:
        """Human-readable evidence blob persisted next to the OOBE record."""
        lines = [
            "# OOBE detection evidence",
            f"avd: {avd_name}",
            f"image_id: {image_id}",
            f"serial: {self.serial}",
            f"detected_at: {self.detected_at.isoformat() if self.detected_at else ''}",
            f"top_activity: {self.activity or '<none>'}",
            f"activity_source: {self.top.source or '<none>'}",
            f"classification: {self.kind.value}",
            f"api_level: {self.api_level}",
            f"abi: {self.abi}",
            f"release: {self.release}",
            f"boot_completed: {self.boot_completed}",
        ]
        if self.errors:
            lines.append("errors:")
            lines.extend(f"  - {error}" for error in self.errors)
        lines.extend(["", "## dumpsys activity activities", self.activity_dump or "<empty>"])
        lines.extend(["", "## dumpsys window", self.window_dump or "<empty>"])
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


class OobeDetector:
    """Read-only OOBE facts obtained through ``adb``.

    Parameters
    ----------
    runner:
        Injected process runner accepting ``run(argv)``.  The adapter is
        tolerant of ``exit_code``/``returncode`` names so the N1/S1 helpers and
        the test fakes both work.
    adb:
        Name or path of the adb executable (defaults to ``$ADB``/``adb``).
    timeout_s:
        Per-command timeout forwarded to the runner when supported.
    """

    def __init__(
        self,
        runner: CommandRunner | Callable[..., Any] | None,
        *,
        adb: str | None = None,
        timeout_s: float = 30.0,
        logger_: Any | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._adb = adb or os.environ.get("ADB") or "adb"
        self._timeout_s = float(timeout_s)
        self._logger = logger_ or logger
        self._runner: CommandRunner | Callable[..., Any] | None = runner or SubprocessRunner(
            self._adb,
            env=env,
            timeout_s=self._timeout_s,
        )

    # -- low level -------------------------------------------------------
    @property
    def runner(self) -> CommandRunner | Callable[..., Any] | None:
        return self._runner

    def argv(self, serial: str, *args: str) -> list[str]:
        return [self._adb, "-s", str(serial), *[str(arg) for arg in args]]

    def run(self, argv: Sequence[str], *, timeout_s: float | None = None) -> RunOutput:
        return call_runner(
            self._runner,
            argv,
            timeout_s=self._timeout_s if timeout_s is None else timeout_s,
        )

    def shell(self, serial: str, *args: str, timeout_s: float | None = None) -> RunOutput:
        return self.run(self.argv(serial, "shell", *args), timeout_s=timeout_s)

    def getprop(self, serial: str, name: str, *, timeout_s: float | None = None) -> str:
        result = self.shell(serial, "getprop", name, timeout_s=timeout_s)
        if not result.ok:
            return ""
        return result.text().strip()

    # -- detection -------------------------------------------------------
    def detect(self, serial: str, *, include_dumps: bool = True) -> Detection:
        """Read the current top activity and device facts for *serial*."""
        serial = str(serial or "").strip()
        if not serial:
            raise OobeError(
                "cannot detect OOBE without an adb serial",
                hint="start the instance so runtime.status(name).adb_serial is populated",
            )
        detection = Detection(serial=serial)

        activity_result = self.shell(serial, "dumpsys", "activity", "activities")
        if include_dumps:
            if activity_result.ok:
                detection.activity_dump = activity_result.text()
            else:
                detection.errors.append(
                    f"dumpsys activity activities failed (exit {activity_result.returncode})"
                    f": {activity_result.error_text().strip()}"
                )

        window_result = self.shell(serial, "dumpsys", "window")
        if include_dumps:
            if window_result.ok:
                detection.window_dump = window_result.text()
            else:
                detection.errors.append(
                    f"dumpsys window failed (exit {window_result.returncode})"
                    f": {window_result.error_text().strip()}"
                )

        top = extract_top_activity(detection.activity_dump, detection.window_dump)
        detection.top = top
        detection.kind = classify_top_activity(top)

        detection.api_level = _to_int(self.getprop(serial, "ro.build.version.sdk"))
        detection.abi = self.getprop(serial, "ro.product.cpu.abi")
        detection.release = self.getprop(serial, "ro.build.version.release")
        boot = self.getprop(serial, "sys.boot_completed")
        detection.boot_completed = boot.strip() in {"1", "true", "True"}
        detection.detected_at = utcnow()
        return detection

    # -- screenshots -----------------------------------------------------
    def capture_screenshot(self, serial: str, *, timeout_s: float | None = None) -> bytes:
        """Capture a PNG through adb, preferring binary-safe ``exec-out``."""
        attempts: list[str] = []
        for tail in (
            ("exec-out", "screencap", "-p"),
            ("shell", "screencap", "-p"),
        ):
            result = self.run(self.argv(serial, *tail), timeout_s=timeout_s)
            data = result.binary()
            if result.ok and is_png(data):
                return data
            if tail[0] == "shell":
                repaired = repair_shell_png(data)
                if repaired is not None:
                    return repaired
            snippet = result.error_text().strip() or f"exit {result.returncode}"
            attempts.append(f"{' '.join(tail)}: {snippet}")
        raise OobeError(
            "could not capture an OOBE screenshot over adb",
            hint="check that the instance is running and screencap is available in the image",
            details={"serial": serial, "attempts": attempts},
        )


# ---------------------------------------------------------------------------
# PNG + filesystem helpers
# ---------------------------------------------------------------------------


def is_png(data: Any) -> bool:
    """True when *data* starts with the PNG signature."""
    if not data:
        return False
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return False
    return bytes(data[: len(PNG_SIGNATURE)]) == PNG_SIGNATURE


def repair_shell_png(data: bytes) -> bytes | None:
    """Undo the CRLF mangling of legacy ``adb shell screencap -p`` output."""
    if not data:
        return None
    if is_png(data):
        return data
    fixed = data.replace(b"\r\n", b"\n")
    if is_png(fixed):
        return fixed
    index = data.find(PNG_SIGNATURE)
    if index > 0:
        candidate = data[index:].replace(b"\r\n", b"\n")
        if is_png(candidate):
            return candidate
    return None


def _to_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def screenshot_filename(avd_name: str, image_id: str = "") -> str:
    """Build a collision-free, filesystem-safe screenshot name."""
    stamp = utcnow().strftime("%Y%m%dT%H%M%S%f")
    parts = ["oobe", _safe_slug(avd_name) or "avd"]
    if image_id:
        parts.append(_safe_slug(image_id, limit=60))
    return "-".join(parts) + f"-{stamp}.png"


def _safe_slug(value: str, *, limit: int = 48) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip("-._")
    return cleaned[:limit]


def write_bytes_atomic(path: str | Path, data: bytes) -> Path:
    """Write *data* to *path* atomically (tmp file + replace)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort cleanup
            pass
        raise
    return target


def write_text_atomic(path: str | Path, text: str) -> Path:
    return write_bytes_atomic(path, text.encode("utf-8"))


def sleep_seconds(seconds: float) -> None:
    """Indirection used by tests that must not really sleep."""
    time.sleep(seconds)


__all__ = [
    "PNG_SIGNATURE",
    "SETUP_WIZARD_PACKAGES",
    "ActivityKind",
    "CommandRunner",
    "Detection",
    "OobeDetector",
    "RunOutput",
    "SubprocessRunner",
    "TopActivity",
    "call_runner",
    "classify_top_activity",
    "extract_top_activity",
    "is_png",
    "normalize_component",
    "normalize_run_result",
    "repair_shell_png",
    "screenshot_filename",
    "write_bytes_atomic",
    "write_text_atomic",
]
