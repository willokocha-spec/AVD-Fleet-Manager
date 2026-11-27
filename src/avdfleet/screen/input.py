"""Screen input injection through ``adb shell input``.

All guest commands are built as argv lists (never host-shell strings) and the
single free-form argument (``input text``) is quoted for the *device* shell so
user data can never split into additional arguments or inject shell syntax.
Input is refused when the instance is not running; failures are reported as
:class:`~avdfleet.core.models.OperationResult` values, matching the REST input
endpoint contract.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..core.errors import ScreenError
from ..core.logging import get_logger
from ..core.models import OperationResult

logger = get_logger("avdfleet.screen.input")

_KEYCODE_RE = re.compile(r"^KEYCODE_[A-Z0-9_]+$")
_NUMERIC_KEY_RE = re.compile(r"^\d{1,3}$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


class AdbRunner(Protocol):
    def run(self, argv: Sequence[str], timeout_s: float | None = None) -> Any: ...


@dataclass
class RunResult:
    returncode: int = 0
    stdout: str | bytes = ""
    stderr: str | bytes = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def text(self) -> str:
        value = self.stdout
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value or "")

    def error_text(self) -> str:
        value = self.stderr
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value or "")


def normalize_result(result: Any) -> RunResult:
    if isinstance(result, RunResult):
        return result
    if result is None:
        return RunResult(returncode=0)
    returncode = getattr(result, "returncode", None)
    if returncode is None:
        returncode = getattr(result, "exit_code", None)
    if returncode is None:
        returncode = getattr(result, "code", 0)
    return RunResult(
        returncode=_coerce_int(returncode, 1),
        stdout=getattr(result, "stdout", ""),
        stderr=getattr(result, "stderr", ""),
    )


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Argument validation / quoting
# ---------------------------------------------------------------------------


def quote_device_argument(value: str) -> str:
    """Quote one argument for the guest ``sh`` invoked by ``adb shell``.

    The payload is wrapped in single quotes; embedded single quotes are escaped
    with the standard ``'\''`` idiom.  Control characters (including newlines)
    are rejected so a single request cannot express multiple guest commands.
    """
    if value is None:
        raise ScreenError("input text must not be empty")
    text = str(value)
    if not text:
        raise ScreenError("input text must not be empty")
    if _CONTROL_CHARS_RE.search(text):
        raise ScreenError(
            "input text must be a single line without control characters",
            hint="send each line separately",
        )
    escaped = text.replace("'", "'\\''")
    return f"'{escaped}'"


def validate_text(value: str) -> str:
    """Validate text input and return it unchanged (raises ScreenError)."""
    if value is None or not str(value):
        raise ScreenError("input text must not be empty")
    text = str(value)
    if _CONTROL_CHARS_RE.search(text):
        raise ScreenError(
            "input text must be a single line without control characters",
            hint="send each line separately",
        )
    return text


def validate_coordinate(value: Any, *, name: str = "coordinate") -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ScreenError(f"{name} must be an integer, got {value!r}") from exc
    if number < 0:
        raise ScreenError(f"{name} must be >= 0, got {number}")
    return number


def normalize_keycode(value: str) -> str:
    """Accept ``KEYCODE_HOME``, ``HOME`` or a numeric key code (``3``)."""
    candidate = str(value or "").strip()
    if not candidate:
        raise ScreenError("a key code is required")
    if _NUMERIC_KEY_RE.match(candidate):
        return candidate
    upper = candidate.upper()
    if not upper.startswith("KEYCODE_"):
        upper = f"KEYCODE_{upper}"
    if not _KEYCODE_RE.match(upper):
        raise ScreenError(
            f"invalid key code {value!r}",
            hint="use an Android key name such as KEYCODE_HOME or a numeric key code",
        )
    return upper


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------


def _adb_argv(serial: str, *, executable: str = "adb") -> list[str]:
    return [executable, "-s", str(serial), "shell"]


def build_tap_argv(serial: str, x: int, y: int, *, executable: str = "adb") -> list[str]:
    return [*_adb_argv(serial, executable=executable), "input", "tap", str(int(x)), str(int(y))]


def build_swipe_argv(
    serial: str,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    *,
    duration_ms: int = 300,
    executable: str = "adb",
) -> list[str]:
    duration = max(0, int(duration_ms))
    return [
        *_adb_argv(serial, executable=executable),
        "input",
        "swipe",
        str(int(x1)),
        str(int(y1)),
        str(int(x2)),
        str(int(y2)),
        str(duration),
    ]


def build_text_argv(serial: str, value: str, *, executable: str = "adb") -> list[str]:
    text = validate_text(value)
    return [*_adb_argv(serial, executable=executable), "input", "text", quote_device_argument(text)]


def build_key_argv(serial: str, keycode: str, *, executable: str = "adb") -> list[str]:
    return [*_adb_argv(serial, executable=executable), "input", "keyevent", normalize_keycode(keycode)]


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class InputController:
    """Validated input injection with an injectable runner and state probe.

    Parameters
    ----------
    runner:
        Process runner (``run(argv)``).
    serial_for:
        Callable resolving the adb serial for an AVD name; raises
        :class:`~avdfleet.core.errors.ScreenError` when unavailable.
    is_running:
        Callable telling whether the instance is running.  Input is refused
        (``OperationResult(ok=False)``) when it returns false.
    adb:
        adb executable name/path.
    timeout_s:
        Per-command timeout forwarded to the runner when supported.
    """

    def __init__(
        self,
        runner: AdbRunner | Callable[..., Any] | None,
        *,
        serial_for: Callable[[str], str],
        is_running: Callable[[str], bool],
        adb: str | None = None,
        timeout_s: float = 20.0,
    ) -> None:
        self._runner = runner
        self._serial_for = serial_for
        self._is_running = is_running
        self._adb = adb or "adb"
        self._timeout_s = float(timeout_s)

    # -- public API ------------------------------------------------------
    def tap(self, avd_name: str, x: int, y: int) -> OperationResult:
        return self._dispatch(
            avd_name,
            "tap",
            lambda serial: build_tap_argv(
                serial,
                validate_coordinate(x, name="x"),
                validate_coordinate(y, name="y"),
                executable=self._adb,
            ),
        )

    def swipe(
        self,
        avd_name: str,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
    ) -> OperationResult:
        return self._dispatch(
            avd_name,
            "swipe",
            lambda serial: build_swipe_argv(
                serial,
                validate_coordinate(x1, name="x1"),
                validate_coordinate(y1, name="y1"),
                validate_coordinate(x2, name="x2"),
                validate_coordinate(y2, name="y2"),
                duration_ms=validate_coordinate(duration_ms, name="duration_ms"),
                executable=self._adb,
            ),
        )

    def text(self, avd_name: str, value: str) -> OperationResult:
        return self._dispatch(
            avd_name,
            "text",
            lambda serial: build_text_argv(serial, value, executable=self._adb),
        )

    def key(self, avd_name: str, keycode: str) -> OperationResult:
        return self._dispatch(
            avd_name,
            "key",
            lambda serial: build_key_argv(serial, keycode, executable=self._adb),
        )

    # -- internals -------------------------------------------------------
    def _dispatch(
        self,
        avd_name: str,
        action: str,
        build: Callable[[str], list[str]],
    ) -> OperationResult:
        try:
            running = bool(self._is_running(avd_name))
        except Exception:  # noqa: BLE001 - unknown instance is simply not running
            running = False
        if not running:
            return OperationResult(
                ok=False,
                message=f"instance {avd_name!r} is not running; screen input was not sent",
                data={"action": action, "avd_name": avd_name},
            )
        try:
            serial = self._serial_for(avd_name)
            argv = build(serial)
        except ScreenError as exc:
            return OperationResult(ok=False, message=str(exc), data={"action": action, "avd_name": avd_name})
        result = self._run(argv)
        message = result.error_text().strip() or result.text().strip()
        if not message:
            message = "input delivered" if result.ok else f"adb exited {result.returncode}"
        return OperationResult(
            ok=result.ok,
            message=message,
            data={"action": action, "avd_name": avd_name, "serial": serial, "argv": list(argv)},
        )

    def _run(self, argv: list[str]) -> RunResult:
        if self._runner is None:
            return RunResult(returncode=-1, stderr="no adb runner is configured for screen input")
        try:
            from .screencap import invoke_runner

            return normalize_result(invoke_runner(self._runner, argv, timeout_s=self._timeout_s))
        except ScreenError as exc:
            return RunResult(returncode=-1, stderr=str(exc))


__all__ = [
    "InputController",
    "RunResult",
    "build_key_argv",
    "build_swipe_argv",
    "build_tap_argv",
    "build_text_argv",
    "normalize_keycode",
    "normalize_result",
    "quote_device_argument",
    "validate_coordinate",
    "validate_text",
]
