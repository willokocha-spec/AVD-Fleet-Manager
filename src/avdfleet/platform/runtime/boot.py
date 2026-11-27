"""Boot detection for emulator instances via ``adb``.

``start`` only proves that a process was spawned; "booted" means the guest
reports ``sys.boot_completed=1`` *and* the package manager answers
(``pm path android``).  Requiring both avoids the classic trap of treating the
first ``adb`` handshake as readiness while the framework is still restarting.

Everything external is behind :class:`AdbRunner`, so unit tests drive the state
machine with a scripted runner and never need a real ``adb``.  The default
implementation runs ``adb`` as an argv list through
:func:`subprocess.run`, honouring the SDK environment supplied by the backend.
The poll loop takes injectable ``clock``/``sleep`` callables so timeout tests are
fast and deterministic.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from avdfleet.core.errors import ToolError
from avdfleet.core.logging import get_logger, redact
from avdfleet.core.models import InstancePorts

logger = get_logger("platform.runtime.boot")

DEFAULT_SHELL_TIMEOUT_S = 30.0
DEFAULT_BOOT_TIMEOUT_S = 300.0
DEFAULT_BOOT_POLL_INTERVAL_S = 2.0
BOOT_PROPERTY = "sys.boot_completed"
PACKAGE_MANAGER_PROBE = "pm path android"
PACKAGE_MANAGER_LIST_PROBE = "pm list packages"


# ---------------------------------------------------------------------------
# adb results / devices
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdbResult:
    """Outcome of one ``adb`` invocation."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def text(self) -> str:
        return self.stdout.strip()

    @property
    def error_text(self) -> str:
        return (self.stderr or self.stdout).strip()

    def as_dict(self) -> dict[str, Any]:
        return {
            "returncode": self.returncode,
            "stdout": str(redact(self.stdout)),
            "stderr": str(redact(self.stderr)),
            "timed_out": self.timed_out,
        }


@dataclass(frozen=True)
class AdbDevice:
    """One ``adb devices -l`` row."""

    serial: str
    state: str = "device"
    properties: dict[str, str] = field(default_factory=dict)

    @property
    def online(self) -> bool:
        return self.state == "device"

    @property
    def console_port(self) -> int | None:
        match = re.search(r"emulator-(\d+)", self.serial)
        return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Runner protocol / implementations
# ---------------------------------------------------------------------------


@runtime_checkable
class AdbRunner(Protocol):
    """The small ``adb`` surface the runtime service depends on."""

    def devices(self, *, timeout_s: float = DEFAULT_SHELL_TIMEOUT_S) -> list[AdbDevice]: ...

    def shell(
        self, serial: str, command: str, *, timeout_s: float = DEFAULT_SHELL_TIMEOUT_S
    ) -> AdbResult: ...

    def emu(
        self, serial: str, command: str, *, timeout_s: float = DEFAULT_SHELL_TIMEOUT_S
    ) -> AdbResult: ...


class AdbError(ToolError):
    """``adb`` could not be executed at all."""

    code = "adb_error"


def parse_devices(output: str) -> list[AdbDevice]:
    """Parse ``adb devices -l`` output deterministically."""
    devices: list[AdbDevice] = []
    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("*") or line.lower().startswith("list of devices"):
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        serial, state = fields[0], fields[1]
        if ":" in state:
            # ``adb devices -l`` prints extra props after the transport state.
            continue
        properties: dict[str, str] = {}
        for token in fields[2:]:
            key, _, value = token.partition(":")
            if key and value:
                properties[key] = value
        devices.append(AdbDevice(serial=serial, state=state, properties=properties))
    return devices


class SubprocessAdbRunner:
    """Production :class:`AdbRunner` backed by a real ``adb`` executable."""

    def __init__(
        self,
        executable: str | Path,
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
        default_timeout_s: float = DEFAULT_SHELL_TIMEOUT_S,
    ) -> None:
        self.executable = Path(executable)
        self._env = {str(key): str(value) for key, value in (env or {}).items()}
        self._cwd = str(cwd) if cwd is not None else None
        self.default_timeout_s = float(default_timeout_s)

    def _run(self, args: Sequence[str], *, timeout_s: float) -> AdbResult:
        argv = [str(self.executable), *(str(item) for item in args)]
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": max(0.001, float(timeout_s)),
            "stdin": subprocess.DEVNULL,
            "check": False,
        }
        if self._env:
            kwargs["env"] = {**os.environ, **self._env}
        if self._cwd:
            kwargs["cwd"] = self._cwd
        try:
            completed = subprocess.run(argv, **kwargs)  # noqa: S603 - argv list, no shell
        except FileNotFoundError as exc:
            raise AdbError(
                f"adb executable was not found: {self.executable}",
                hint="Install platform-tools or configure the SDK path.",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            return AdbResult(
                returncode=-9,
                stdout=self._coerce(exc.stdout),
                stderr=self._coerce(exc.stderr),
                timed_out=True,
            )
        except OSError as exc:
            raise AdbError(
                f"could not execute adb: {redact(str(exc))}",
                hint="Repair or reinstall Android platform-tools.",
            ) from exc
        return AdbResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )

    @staticmethod
    def _coerce(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    def devices(self, *, timeout_s: float | None = None) -> list[AdbDevice]:
        result = self._run(["devices", "-l"], timeout_s=timeout_s or self.default_timeout_s)
        return parse_devices(result.stdout)

    def shell(self, serial: str, command: str, *, timeout_s: float | None = None) -> AdbResult:
        return self._run(
            ["-s", str(serial), "shell", str(command)],
            timeout_s=timeout_s or self.default_timeout_s,
        )

    def emu(self, serial: str, command: str, *, timeout_s: float | None = None) -> AdbResult:
        return self._run(
            ["-s", str(serial), "emu", str(command)],
            timeout_s=timeout_s or self.default_timeout_s,
        )


class BackendAdbRunner(SubprocessAdbRunner):
    """Resolve ``adb`` and its environment from an injected ``SdkBackend``."""

    def __init__(self, backend: Any, *, environ: Mapping[str, str] | None = None) -> None:
        executable = backend.executable("adb")
        sdk_env = backend.environment() if hasattr(backend, "environment") else {}
        env = {**os.environ, **(dict(environ or {}))}
        env.update({str(key): str(value) for key, value in dict(sdk_env or {}).items()})
        super().__init__(executable, env=env)


# ---------------------------------------------------------------------------
# Boot detection
# ---------------------------------------------------------------------------


def boot_serial(ports: InstancePorts | None) -> str:
    """The adb serial for an emulator console port."""
    if ports is None:
        raise ValueError("instance ports are required")
    return f"emulator-{int(ports.console)}"


@dataclass
class BootStatus:
    """Outcome of waiting for one instance to finish booting."""

    booted: bool = False
    boot_completed: bool = False
    package_manager_ready: bool = False
    polls: int = 0
    seconds: float = 0.0
    device_state: str = ""
    timed_out: bool = False
    process_exited: bool = False
    last_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "booted": self.booted,
            "boot_completed": self.boot_completed,
            "package_manager_ready": self.package_manager_ready,
            "polls": self.polls,
            "seconds": round(self.seconds, 3),
            "device_state": self.device_state,
            "timed_out": self.timed_out,
            "process_exited": self.process_exited,
            "last_error": str(redact(self.last_error)),
        }


class BootDetector:
    """Poll ``adb`` until both boot markers are ready, the process exits or timeout."""

    def __init__(
        self,
        adb: AdbRunner,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval_s: float = DEFAULT_BOOT_POLL_INTERVAL_S,
        shell_timeout_s: float = DEFAULT_SHELL_TIMEOUT_S,
    ) -> None:
        self.adb = adb
        self._clock = clock
        self._sleep = sleep
        self.poll_interval_s = max(0.0, float(poll_interval_s))
        self.shell_timeout_s = float(shell_timeout_s)

    def check(self, serial: str) -> tuple[bool, bool, str]:
        """Return ``(boot_completed, package_manager_ready, device_state)``."""
        device_state = ""
        try:
            devices = self.adb.devices(timeout_s=self.shell_timeout_s)
        except Exception as exc:  # noqa: BLE001 - probes are advisory
            logger.debug("adb devices failed during boot check: %s", exc)
            devices = []
        for device in devices:
            if device.serial == serial:
                device_state = device.state
                break
        result = self.adb.shell(serial, f"getprop {BOOT_PROPERTY}", timeout_s=self.shell_timeout_s)
        completed = result.ok and result.text.splitlines()[-1:] == ["1"] if result.text else False
        if not completed and result.ok:
            completed = result.text.strip().endswith("1")
        ready = False
        if completed:
            package_manager = self.adb.shell(
                serial, PACKAGE_MANAGER_PROBE, timeout_s=self.shell_timeout_s
            )
            ready = package_manager.ok and bool(package_manager.text)
            if not ready:
                # ``pm path android`` can be empty on some images; a non-empty
                # package list still proves the package manager is answering.
                listing = self.adb.shell(
                    serial, PACKAGE_MANAGER_LIST_PROBE, timeout_s=self.shell_timeout_s
                )
                ready = listing.ok and bool(listing.text)
        return completed, ready, device_state

    def wait(
        self,
        serial: str,
        *,
        timeout_s: float = DEFAULT_BOOT_TIMEOUT_S,
        poll_interval_s: float | None = None,
        is_process_alive: Callable[[], bool] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> BootStatus:
        """Block until booted/timeout/process exit, returning the final status."""
        interval = self.poll_interval_s if poll_interval_s is None else max(0.0, float(poll_interval_s))
        started = self._clock()
        polls = 0
        last_error = ""
        while True:
            if should_stop is not None and should_stop():
                return BootStatus(
                    polls=polls,
                    seconds=max(0.0, self._clock() - started),
                    last_error="boot wait was cancelled",
                )
            polls += 1
            try:
                completed, ready, device_state = self.check(serial)
            except Exception as exc:  # noqa: BLE001 - adb may be restarting
                completed, ready, device_state = False, False, ""
                last_error = str(exc)
                logger.debug("boot probe failed for %s: %s", serial, exc)
            elapsed = max(0.0, self._clock() - started)
            if completed and ready:
                return BootStatus(
                    booted=True,
                    boot_completed=True,
                    package_manager_ready=True,
                    polls=polls,
                    seconds=elapsed,
                    device_state=device_state,
                )
            if is_process_alive is not None and not is_process_alive():
                return BootStatus(
                    polls=polls,
                    seconds=elapsed,
                    device_state=device_state,
                    process_exited=True,
                    last_error="emulator process exited before boot completed",
                )
            if elapsed >= timeout_s:
                return BootStatus(
                    boot_completed=completed,
                    package_manager_ready=ready,
                    polls=polls,
                    seconds=elapsed,
                    device_state=device_state,
                    timed_out=True,
                    last_error=last_error
                    or (
                        "boot property was set but the package manager never answered"
                        if completed
                        else "sys.boot_completed was not reported"
                    ),
                )
            remaining = timeout_s - elapsed
            self._sleep(min(interval, max(0.0, remaining)) if remaining > 0 else 0.0)


__all__ = [
    "AdbDevice",
    "AdbError",
    "AdbResult",
    "AdbRunner",
    "BOOT_PROPERTY",
    "BackendAdbRunner",
    "BootDetector",
    "BootStatus",
    "DEFAULT_BOOT_POLL_INTERVAL_S",
    "DEFAULT_BOOT_TIMEOUT_S",
    "DEFAULT_SHELL_TIMEOUT_S",
    "PACKAGE_MANAGER_LIST_PROBE",
    "PACKAGE_MANAGER_PROBE",
    "SubprocessAdbRunner",
    "boot_serial",
    "parse_devices",
]
