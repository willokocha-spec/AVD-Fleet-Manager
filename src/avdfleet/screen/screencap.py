"""``adb exec-out screencap`` fallback and PNG validation.

The emulator's gRPC screen service is optional; this module is the always
available fallback.  It implements the two adb capture paths, repairs the
legacy ``adb shell screencap -p`` CRLF mangling, validates the PNG signature and
caches the latest frame on disk under :attr:`Paths.screenshots_dir`.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..core.errors import ScreenError
from ..core.logging import get_logger
from ..core.models import utcnow

logger = get_logger("avdfleet.screen.screencap")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_IHDR = b"IHDR"


class AdbRunner(Protocol):
    """Injected process runner (subprocess helper or test fake)."""

    def run(self, argv: Sequence[str], timeout_s: float | None = None) -> Any: ...


class SubprocessRunner:
    """Default adb runner used when the container injects no process backend."""

    def __init__(
        self,
        executable: str = "adb",
        *,
        env: Mapping[str, str] | None = None,
        timeout_s: float = 20.0,
    ) -> None:
        self.executable = str(executable or "adb")
        self._env: dict[str, str] | None = (
            {str(key): str(value) for key, value in env.items()} if env is not None else None
        )
        self._timeout_s = float(timeout_s)

    def run(self, argv: Sequence[str], timeout_s: float | None = None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(  # noqa: S603 - argv list, never shell=True
            [str(item) for item in argv],
            capture_output=True,
            timeout=self._timeout_s if timeout_s is None else timeout_s,
            env=self._env,
        )


@dataclass
class RunResult:
    returncode: int = 0
    stdout: str | bytes = ""
    stderr: str | bytes = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def error_text(self) -> str:
        value = self.stderr
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value or "")

    def binary(self) -> bytes:
        value = self.stdout
        if value is None:
            return b""
        if isinstance(value, bytes):
            return value
        if isinstance(value, (bytearray, memoryview)):
            return bytes(value)
        return str(value).encode("latin-1", "replace")


def _accepts_keyword(function: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return False
    if name in parameters:
        return True
    return any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())


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


def invoke_runner(
    runner: AdbRunner | Callable[..., Any] | None,
    argv: Sequence[str],
    *,
    timeout_s: float | None = None,
) -> RunResult:
    """Run *argv* through an injected runner, normalising failures to results."""
    if runner is None:
        raise ScreenError(
            "no adb runner is configured for screen capture",
            hint="inject a runner with run(argv, timeout_s=...) into ScreenService",
        )
    argv_list = [str(item) for item in argv]
    try:
        if hasattr(runner, "run"):
            run = runner.run  # type: ignore[union-attr]
            if _accepts_keyword(run, "timeout_s"):
                return normalize_result(run(argv_list, timeout_s=timeout_s))
            if _accepts_keyword(run, "timeout"):
                return normalize_result(run(argv_list, timeout=timeout_s))  # type: ignore[call-arg]
            return normalize_result(run(argv_list))
        if callable(runner):
            return normalize_result(runner(argv_list))
    except ScreenError:
        raise
    except Exception as exc:  # noqa: BLE001 - process failures are data here
        return RunResult(returncode=-1, stderr=f"{type(exc).__name__}: {exc}")
    raise ScreenError(
        "the injected screen runner is neither callable nor exposes run()",
        hint="pass an object with run(argv) or a plain callable",
    )


# ---------------------------------------------------------------------------
# PNG helpers
# ---------------------------------------------------------------------------


def is_png(data: Any) -> bool:
    """True when *data* begins with the 8-byte PNG signature."""
    if data is None:
        return False
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return False
    return bytes(data[: len(PNG_SIGNATURE)]) == PNG_SIGNATURE


def has_png_ihdr(data: Any) -> bool:
    """True when the PNG signature and an IHDR chunk are present."""
    if not is_png(data):
        return False
    raw = bytes(data)
    if len(raw) < len(PNG_SIGNATURE) + 8:
        return False
    return raw[len(PNG_SIGNATURE) + 4 : len(PNG_SIGNATURE) + 8] == _PNG_IHDR


def repair_shell_png(data: bytes) -> bytes | None:
    """Repair ``adb shell screencap -p`` newline mangling, if present.

    The legacy shell path writes PNG bytes through a text-mode pty which turns
    every ``\\n`` into ``\\r\\n``; the inverse is a single ``\\r\\n`` -> ``\\n``
    replacement (which also restores the PNG signature).
    """
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


def validate_png(data: Any, *, context: str = "screenshot") -> bytes:
    """Return *data* as bytes, raising :class:`ScreenError` when not a PNG."""
    if not is_png(data):
        raise ScreenError(
            f"{context} is not a PNG image",
            hint="the emulator/adb returned an error page or mangled output",
        )
    raw = bytes(data)
    if not has_png_ihdr(raw):
        raise ScreenError(
            f"{context} is missing the PNG IHDR header",
            hint="use 'adb exec-out screencap -p' instead of a text pipeline",
        )
    return raw


# ---------------------------------------------------------------------------
# adb capture
# ---------------------------------------------------------------------------


def adb_capture_argv(serial: str, *, executable: str = "adb", mode: str = "exec-out") -> list[str]:
    """Build the adb capture argv for *serial*."""
    if mode not in {"exec-out", "shell"}:
        raise ValueError(f"unsupported screencap mode: {mode!r}")
    return [executable, "-s", str(serial), mode, "screencap", "-p"]


class AdbScreencap:
    """Capture PNG frames with ``adb`` as the mandatory fallback path."""

    def __init__(
        self,
        runner: AdbRunner | Callable[..., Any] | None,
        *,
        adb: str | None = None,
        timeout_s: float = 20.0,
    ) -> None:
        self._runner = runner
        self._adb = adb or os.environ.get("ADB") or "adb"
        self._timeout_s = float(timeout_s)

    @property
    def adb(self) -> str:
        return self._adb

    @property
    def runner(self) -> AdbRunner | Callable[..., Any] | None:
        return self._runner

    def capture(self, serial: str, *, timeout_s: float | None = None) -> bytes:
        """Capture one PNG via ``exec-out`` then the ``shell`` fallback."""
        if not serial:
            raise ScreenError(
                "cannot capture a screenshot without an adb serial",
                hint="the instance must be running with a known adb serial",
            )
        attempts: list[str] = []
        timeout = self._timeout_s if timeout_s is None else timeout_s
        for mode in ("exec-out", "shell"):
            argv = adb_capture_argv(serial, executable=self._adb, mode=mode)
            result = invoke_runner(self._runner, argv, timeout_s=timeout)
            data = result.binary()
            candidate = data if mode == "exec-out" else repair_shell_png(data)
            if result.ok and candidate is not None and is_png(candidate):
                try:
                    return validate_png(candidate)
                except ScreenError as exc:
                    attempts.append(f"{mode}: {exc}")
            else:
                detail = result.error_text().strip() or f"exit {result.returncode}"
                attempts.append(f"{mode}: {detail}")
        raise ScreenError(
            "adb screenshot capture failed",
            hint="check that the instance is running and `adb screencap` works",
            details={"serial": serial, "attempts": attempts},
        )


# ---------------------------------------------------------------------------
# Frame cache
# ---------------------------------------------------------------------------


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_frame_name(avd_name: str) -> str:
    cleaned = _SAFE_NAME.sub("_", str(avd_name)).strip("._")
    return (cleaned or "instance")[:96]


class FrameCache:
    """Last-frame cache persisted under ``Paths.screenshots_dir/live``."""

    def __init__(self, directory: str | Path, *, enabled: bool = True) -> None:
        self.directory = Path(directory)
        self.enabled = enabled
        self._latest: dict[str, tuple[bytes, Path, float]] = {}

    def path_for(self, avd_name: str) -> Path:
        return self.directory / f"{safe_frame_name(avd_name)}.png"

    def store(self, avd_name: str, data: bytes) -> Path | None:
        if not self.enabled:
            return None
        target = self.path_for(avd_name)
        try:
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
                tmp.unlink(missing_ok=True)
                raise
        except OSError as exc:
            logger.warning("could not cache the screen frame for %s: %s", avd_name, exc)
            return None
        self._latest[avd_name] = (data, target, time.monotonic())
        return target

    def get(self, avd_name: str) -> bytes | None:
        entry = self._latest.get(avd_name)
        return entry[0] if entry is not None else None

    def get_path(self, avd_name: str) -> Path | None:
        entry = self._latest.get(avd_name)
        return entry[1] if entry is not None else None

    def age(self, avd_name: str) -> float | None:
        entry = self._latest.get(avd_name)
        if entry is None:
            return None
        return max(0.0, time.monotonic() - entry[2])

    def clear(self, avd_name: str | None = None) -> None:
        if avd_name is None:
            self._latest.clear()
        else:
            self._latest.pop(avd_name, None)


async def capture_async(blocking: Callable[[], bytes]) -> bytes:
    """Run a blocking capture callable in the default thread executor."""
    return await asyncio.to_thread(blocking)


def frame_timestamp() -> str:
    return utcnow().strftime("%Y%m%dT%H%M%S%f")


__all__ = [
    "PNG_SIGNATURE",
    "AdbRunner",
    "AdbScreencap",
    "FrameCache",
    "RunResult",
    "SubprocessRunner",
    "adb_capture_argv",
    "capture_async",
    "has_png_ihdr",
    "invoke_runner",
    "is_png",
    "normalize_result",
    "repair_shell_png",
    "safe_frame_name",
    "validate_png",
]
