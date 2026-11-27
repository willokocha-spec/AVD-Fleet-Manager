"""Installer execution context and subprocess runner.

``CommandRunner`` is the single place where the installer executes a process.
It always uses an argv vector (no shell unless a provider wraps an unavoidable
``sh -c``/``powershell -Command``), captures and redacts output, enforces a
timeout and records every invocation so ``--dry-run``, tests and audit trails
see exactly what the installer tried to do.

``InstallContext`` is the shared state passed to every step: resolved paths,
config, provider, runner, environment overlay, manifest and cross-step facts.
Dry-run is inherited from the options *and* the runner, so a dry-run can never
accidentally execute a command.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from avdfleet.core.config import Config
from avdfleet.core.errors import CommandError, InstallerError
from avdfleet.core.events import EventBus, EventType
from avdfleet.core.logging import get_logger, redact, redact_text
from avdfleet.core.models import HostInfo, InstallManifest, InstallOptions
from avdfleet.core.paths import Paths

from .providers.base import Command, EnvScope, InstallProvider, ToolTemplate

if TYPE_CHECKING:
    from avdfleet.core.interfaces import SdkBackend

#: Default command timeout (seconds); individual commands may override it.
DEFAULT_COMMAND_TIMEOUT_S = 1800


def _decode(value: object) -> str:
    """Decode subprocess output that may be ``bytes`` or ``None``."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


@dataclass
class CommandResult:
    """Outcome of one runner invocation."""

    argv: list[str] = field(default_factory=list)
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False
    dry_run: bool = False
    description: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def binary(self) -> str:
        return self.argv[0] if self.argv else ""

    @property
    def output(self) -> str:
        parts = [part for part in (self.stdout.strip(), self.stderr.strip()) if part]
        return "\n".join(parts)

    def tail(self, limit: int = 2000) -> str:
        text = self.output
        return text if len(text) <= limit else text[-limit:]

    def raise_for_status(self) -> CommandResult:
        if not self.ok:
            raise CommandError(
                f"command failed with exit code {self.exit_code}: "
                f"{self.argv[0] if self.argv else '<empty>'}",
                argv=list(self.argv),
                exit_code=self.exit_code,
                stdout=redact_text(self.stdout),
                stderr=redact_text(self.stderr),
                hint="check the command output above; re-run with --verbose for details",
            )
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "stdout": redact_text(self.stdout),
            "stderr": redact_text(self.stderr),
            "duration_s": self.duration_s,
            "timed_out": self.timed_out,
            "dry_run": self.dry_run,
            "description": self.description,
        }


class CommandRunner:
    """Execute provider commands with capture, timeout and dry-run support.

    Subclasses (the tests' ``RecordingRunner``) may override :meth:`_execute`
    and set ``simulate_effects = True`` to indicate that no process is really
    run, so steps skip filesystem/version verification that a real install
    would perform.
    """

    simulate_effects: bool = False

    def __init__(
        self,
        *,
        dry_run: bool = False,
        timeout_s: int = DEFAULT_COMMAND_TIMEOUT_S,
        env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> None:
        self.dry_run = dry_run
        self.default_timeout_s = timeout_s
        self._base_env = dict(env) if env is not None else None
        self.cwd = str(cwd) if cwd is not None else None
        self.records: list[CommandResult] = []
        self._lock = threading.Lock()

    # -- introspection ---------------------------------------------------
    @property
    def commands(self) -> list[list[str]]:
        return [list(record.argv) for record in self.records]

    @property
    def executed(self) -> list[CommandResult]:
        return [record for record in self.records if not record.dry_run]

    def reset(self) -> None:
        with self._lock:
            self.records.clear()

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ) if self._base_env is None else dict(self._base_env)
        if extra:
            env.update({str(key): str(value) for key, value in extra.items()})
        return env

    # -- execution -------------------------------------------------------
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: int | float | None = None,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
        check: bool = False,
        description: str = "",
    ) -> CommandResult:
        command_argv = [str(part) for part in argv]
        if not command_argv:
            raise ValueError("cannot run an empty command")
        result = self._dispatch(
            command_argv,
            timeout_s=timeout_s,
            stdin=stdin,
            env=env,
            cwd=cwd,
            description=description,
        )
        with self._lock:
            self.records.append(result)
        if check:
            result.raise_for_status()
        return result

    def run_command(self, command: Command) -> CommandResult:
        return self.run(
            command.argv,
            timeout_s=command.timeout_s,
            stdin=command.stdin,
            env=command.env,
            description=command.description,
        )

    def run_all(
        self,
        commands: Iterable[Command],
        *,
        stop_on_error: bool = False,
    ) -> list[CommandResult]:
        results: list[CommandResult] = []
        for command in commands:
            result = self.run_command(command)
            results.append(result)
            if stop_on_error and not result.ok:
                break
        return results

    # -- internals -------------------------------------------------------
    def _dispatch(
        self,
        argv: list[str],
        *,
        timeout_s: int | float | None,
        stdin: str | None,
        env: Mapping[str, str] | None,
        cwd: str | Path | None,
        description: str,
    ) -> CommandResult:
        timeout = float(timeout_s if timeout_s is not None else self.default_timeout_s)
        if self.dry_run:
            return CommandResult(
                argv=argv,
                exit_code=0,
                stdout="",
                stderr="",
                duration_s=0.0,
                dry_run=True,
                description=description,
            )
        started = time.perf_counter()
        result = self._execute(
            argv,
            timeout_s=timeout,
            stdin=stdin,
            env=self.environment(env),
            cwd=str(cwd) if cwd is not None else self.cwd,
            description=description,
        )
        result.duration_s = round(time.perf_counter() - started, 3)
        result.argv = argv
        result.description = description
        result.stdout = redact_text(result.stdout)
        result.stderr = redact_text(result.stderr)
        return result

    def _execute(
        self,
        argv: list[str],
        *,
        timeout_s: float,
        stdin: str | None,
        env: dict[str, str],
        cwd: str | None,
        description: str,
    ) -> CommandResult:
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                input=stdin,
                stdin=subprocess.DEVNULL if stdin is None else None,
                env=env,
                cwd=cwd,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                argv=argv,
                exit_code=124,
                stdout=_decode(exc.stdout),
                stderr=_decode(exc.stderr) or f"command timed out after {timeout_s:.0f}s",
                timed_out=True,
                description=description,
            )
        except FileNotFoundError as exc:
            return CommandResult(
                argv=argv,
                exit_code=127,
                stderr=f"executable not found: {argv[0]} ({exc})",
                description=description,
            )
        except OSError as exc:
            return CommandResult(
                argv=argv,
                exit_code=126,
                stderr=f"could not execute {argv[0]}: {exc}",
                description=description,
            )
        return CommandResult(
            argv=argv,
            exit_code=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            description=description,
        )


class InstallContext:
    """Shared state for a single installer run (all steps see the same object)."""

    def __init__(
        self,
        *,
        config: Config,
        paths: Paths,
        provider: InstallProvider,
        runner: CommandRunner,
        options: InstallOptions | None = None,
        logger: logging.Logger | None = None,
        bus: EventBus | None = None,
        manifest: InstallManifest | None = None,
        sdk_backend: SdkBackend | None = None,
        host: HostInfo | None = None,
        emit: Callable[[str, dict[str, Any]], None] | None = None,
        dry_run: bool | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.provider = provider
        self.runner = runner
        self.options = options or InstallOptions()
        self.logger = logger or get_logger("installer")
        self.bus = bus
        self.manifest = manifest
        self.sdk_backend = sdk_backend
        self.host = host or provider.host_info()
        self._emit_callback = emit
        self._dry_run_override = dry_run
        self.env: dict[str, str] = {}
        self.path_entries: list[str] = []
        self.facts: dict[str, Any] = {}
        sdk_root = self.options.sdk_root or config.sdk_root or paths.sdk_root
        if sdk_root is None:  # pragma: no cover - Paths always resolves an SDK root
            raise InstallerError(
                "no Android SDK root is configured",
                hint="pass --sdk-root or set AVDFLEET_SDK_ROOT",
            )
        self.sdk_root: Path = Path(sdk_root)
        avd_home = self.options.avd_home or config.avd_home or paths.avd_home
        if avd_home is None:  # pragma: no cover - Paths always resolves an AVD home
            raise InstallerError("no Android AVD home is configured", hint="set AVDFLEET_AVD_HOME")
        self.avd_home: Path = Path(avd_home)
        self.studio_config_dir: Path = provider.studio_config_dir

    # -- environment -----------------------------------------------------
    @property
    def dry_run(self) -> bool:
        if self._dry_run_override is not None:
            return bool(self._dry_run_override)
        return bool(self.options.dry_run or self.runner.dry_run)

    @property
    def simulated(self) -> bool:
        """True when commands are recorded but not executed (tests/dry-run)."""
        return bool(self.dry_run or getattr(self.runner, "simulate_effects", False))

    def get_env(self, name: str) -> str | None:
        if name in self.env:
            return self.env[name]
        value = self.provider.get_env_var(name)
        if value is not None:
            return value
        return os.environ.get(name)

    def set_env(
        self,
        name: str,
        value: str,
        *,
        scope: EnvScope = EnvScope.USER,
    ) -> list[Command]:
        """Set the variable for this run and return persistence commands."""
        self.env[name] = value
        return self.provider.set_env_var(name, value, scope)

    def add_path(self, entries: Sequence[str]) -> list[Command]:
        cleaned = [str(entry) for entry in entries if str(entry)]
        newly_added = [entry for entry in cleaned if entry not in self.path_entries]
        self.path_entries.extend(newly_added)
        if not newly_added:
            return []
        return self.provider.add_to_path(newly_added)

    def remove_env(self, name: str, *, scope: EnvScope = EnvScope.USER) -> list[Command]:
        self.env.pop(name, None)
        return self.provider.remove_env_var(name, scope)

    def process_env(self) -> dict[str, str]:
        """Environment overlay applied to every command of this install."""
        overlay = dict(self.env)
        if self.path_entries:
            current = overlay.get("PATH") or os.environ.get("PATH", "")
            parts = [*self.path_entries]
            if current:
                parts.append(current)
            overlay["PATH"] = os.pathsep.join(parts)
        return overlay

    # -- command execution ----------------------------------------------
    def _sync_dry_run(self) -> None:
        if self.dry_run and not self.runner.dry_run:
            self.runner.dry_run = True

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: int | float | None = None,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
        check: bool = False,
        description: str = "",
    ) -> CommandResult:
        self._sync_dry_run()
        merged_env = self.process_env()
        if env:
            merged_env.update({str(key): str(value) for key, value in env.items()})
        return self.runner.run(
            argv,
            timeout_s=timeout_s,
            stdin=stdin,
            env=merged_env,
            cwd=cwd,
            check=check,
            description=description,
        )

    def run_command(self, command: Command) -> CommandResult:
        self._sync_dry_run()
        env = self.process_env()
        env.update({str(key): str(value) for key, value in command.env.items()})
        return self.runner.run(
            command.argv,
            timeout_s=command.timeout_s,
            stdin=command.stdin,
            env=env,
            check=False,
            description=command.description,
        )

    def run_commands(self, commands: Iterable[Command], *, stop_on_error: bool = False) -> list[CommandResult]:
        results: list[CommandResult] = []
        for command in commands:
            result = self.run_command(command)
            results.append(result)
            if stop_on_error and not result.ok:
                break
        return results

    def command_output(self, argv: Sequence[str], *, timeout_s: int | float | None = None) -> str:
        result = self.run(argv, timeout_s=timeout_s)
        return result.stdout.strip() if result.ok else ""

    # -- command templates ----------------------------------------------
    def tool_template(self, tool: str) -> ToolTemplate | None:
        return self.provider.tool_template(tool)

    def tool_commands(self, tool: str, *, fallback: Sequence[Command] = ()) -> list[Command]:
        commands = self.provider.tool_install_commands(tool)
        return commands if commands else list(fallback)

    # -- admin / package manager ----------------------------------------
    def require_admin(self, what: str) -> None:
        if self.dry_run or self.simulated:
            return
        if self.provider.is_admin:
            return
        raise InstallerError(
            f"{what} requires administrator privileges",
            hint=self.provider.admin_error_hint,
            details={"provider": self.provider.id},
        )

    def ensure_package_manager(self) -> list[CommandResult]:
        """Run the provider's prerequisite commands when the package manager is missing."""
        if self.provider.package_manager_ready():
            return []
        prerequisites = self.provider.prerequisites()
        if not prerequisites:
            raise InstallerError(
                f"{self.provider.package_manager} is not installed and no bootstrap plan exists",
                hint=f"install {self.provider.package_manager} manually, then re-run 'avdfleet install --repair'",
                details={"provider": self.provider.id},
            )
        if any(command.admin for command in prerequisites):
            self.require_admin(f"bootstrapping {self.provider.package_manager}")
        results = self.run_commands(prerequisites, stop_on_error=False)
        failed = [result for result in results if not result.ok]
        if failed and not self.simulated:
            raise InstallerError(
                f"could not bootstrap {self.provider.package_manager}: {failed[0].tail(600)}",
                hint=self.provider.admin_error_hint,
            )
        if not self.simulated and not self.provider.package_manager_ready():
            raise InstallerError(
                f"{self.provider.package_manager} is still not available after bootstrapping",
                hint=f"install {self.provider.package_manager} manually and re-run 'avdfleet install --repair'",
            )
        return results

    # -- tool discovery --------------------------------------------------
    def which(self, binary: str) -> str | None:
        return self.provider.which(binary)

    def has(self, binary: str) -> bool:
        return self.provider.command_exists(binary)

    def _sdk_tool(self, *relative: str) -> str | None:
        for name in relative:
            candidate = self.sdk_root.joinpath(*name.split("/"))
            if candidate.is_file():
                return str(candidate)
        return None

    def sdkmanager_path(self) -> str | None:
        if self.provider.platform.value == "windows":
            relative: tuple[str, ...] = (
                "cmdline-tools/latest/bin/sdkmanager.bat",
                "cmdline-tools/latest/bin/sdkmanager",
                "tools/bin/sdkmanager.bat",
            )
        else:
            relative = (
                "cmdline-tools/latest/bin/sdkmanager",
                "tools/bin/sdkmanager",
            )
        return self._sdk_tool(*relative) or self.which("sdkmanager")

    def adb_path(self) -> str | None:
        suffix = "adb.exe" if self.provider.platform.value == "windows" else "adb"
        return self._sdk_tool(f"platform-tools/{suffix}") or self.which("adb")

    def emulator_path(self) -> str | None:
        suffix = "emulator.exe" if self.provider.platform.value == "windows" else "emulator"
        return self._sdk_tool(f"emulator/{suffix}") or self.which("emulator")

    def java_path(self) -> str | None:
        java_home = self.env.get("JAVA_HOME") or self.facts.get("java_home")
        if java_home:
            suffix = "java.exe" if self.provider.platform.value == "windows" else "java"
            candidate = Path(str(java_home)) / "bin" / suffix
            if candidate.is_file():
                return str(candidate)
        suffix = "java.exe" if self.provider.platform.value == "windows" else "java"
        return self.which(suffix)

    # -- filesystem helpers (no writes in dry-run) ----------------------
    def mkdir(self, path: str | Path) -> bool:
        if self.dry_run:
            return False
        Path(path).mkdir(parents=True, exist_ok=True)
        return True

    def write_text(self, path: str | Path, content: str) -> bool:
        if self.dry_run:
            return False
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.avdfleet.tmp")
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, target)
        return True

    def remove_file(self, path: str | Path) -> bool:
        if self.dry_run:
            return False
        try:
            Path(path).unlink()
        except FileNotFoundError:
            return False
        return True

    def is_managed_path(self, path: str | Path) -> bool:
        """True when *path* is inside avdfleet's data dir (safe to delete)."""
        try:
            data_dir = Path(self.paths.data_dir or self.paths.root)
            Path(path).expanduser().resolve().relative_to(data_dir.resolve())
        except (ValueError, OSError):
            return False
        return True

    # -- logging / events ------------------------------------------------
    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        data = redact(payload or {})
        if self._emit_callback is not None:
            self._emit_callback(event_type, data)
        if self.bus is not None:
            self.bus.emit(event_type, data, source="installer")

    def log(self, level: int, message: str, **extra: Any) -> None:
        self.logger.log(level, redact_text(message), extra=extra or None)

    def info(self, message: str, **extra: Any) -> None:
        self.log(logging.INFO, message, **extra)

    def warning(self, message: str, **extra: Any) -> None:
        self.log(logging.WARNING, message, **extra)
        self.emit(EventType.LOG, {"level": "warning", "message": redact_text(message)})


__all__ = [
    "DEFAULT_COMMAND_TIMEOUT_S",
    "CommandResult",
    "CommandRunner",
    "InstallContext",
]
