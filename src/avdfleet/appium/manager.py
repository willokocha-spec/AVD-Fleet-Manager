"""Local Appium 4 server lifecycle management.

:class:`AppiumManager` owns one loopback-bound Appium server process for the
fleet daemon:

* locate the ``appium`` executable through an injectable resolver (never a hard
  dependency in tests or on hosts without Appium),
* allocate a free port from the configured Appium block, spawn the server with
  stdout/stderr captured to a log file, persist pid/port state and wait for the
  HTTP ``/status`` endpoint to report ``ready``,
* expose status/session counts, stop gracefully (terminate → kill), clean up
  per-instance adb forwards/reverses and build UiAutomator2 capabilities,
* degrade gracefully instead of crashing when Appium is not installed.

Port planning is delegated to :class:`~avdfleet.appium.ports.AppiumPortPlanner`,
which shares its ledger with the runtime's :class:`~avdfleet.core.ports.PortAllocator`
when one is injected.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from ..core.config import Config
from ..core.errors import AppiumError, NotFoundError
from ..core.events import EventBus, EventSink, EventType
from ..core.logging import get_logger, redact_text
from ..core.models import AppiumStatus, AvdInfo, InstancePorts, OperationResult, Template
from ..core.paths import Paths
from .capabilities import (
    DEFAULT_ADB_EXEC_TIMEOUT_MS,
    DEFAULT_NEW_COMMAND_TIMEOUT_S,
    build_uiautomator2_capabilities,
    serial_for,
)
from .ports import AppiumPortPlanner, AppiumPorts

try:  # psutil is a declared dependency; fall back to os.kill without it.
    import psutil as _psutil
except ImportError:  # pragma: no cover - only on a broken install
    _psutil = None  # type: ignore[assignment]

logger = get_logger("appium.manager")

__all__ = ["AppiumManager"]

_DEFAULT_APPIUM_PORT = 4723
_MAX_LOG_TAIL = 4000


class AppiumManager:
    """Lifecycle manager for the fleet's local Appium 4 server."""

    def __init__(
        self,
        config: Config | None = None,
        paths: Paths | None = None,
        *,
        event_bus: EventBus | None = None,
        event_sink: EventSink | None = None,
        executable_resolver: Callable[[], Any] | None = None,
        adb_runner: Callable[[Sequence[str]], Any] | None = None,
        adb_command: Sequence[str] | str | None = None,
        instance_resolver: Callable[[str], Any] | None = None,
        port_planner: AppiumPortPlanner | None = None,
        port_allocator: Any | None = None,
        which: Callable[[str], str | None] | None = None,
        http_client_factory: Callable[[], Any] | None = None,
        popen_factory: Callable[..., subprocess.Popen] | None = None,
        readiness_timeout_s: float = 60.0,
        poll_interval_s: float = 0.25,
        stop_timeout_s: float = 10.0,
        command_timeout_s: float = 20.0,
        base_path: str = "/wd/hub",
        address: str = "127.0.0.1",
        environment: Mapping[str, str] | None = None,
        chromedriver_executable: str | None = None,
        new_command_timeout: int = DEFAULT_NEW_COMMAND_TIMEOUT_S,
        adb_exec_timeout: int = DEFAULT_ADB_EXEC_TIMEOUT_MS,
        no_reset: bool = False,
        detect_drivers: bool = True,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config or Config()
        self._paths = paths
        self._which = which or shutil.which
        self._sink = event_sink if event_sink is not None else EventSink(event_bus)
        self._executable_resolver = executable_resolver
        self._adb_runner = adb_runner
        self._adb_command = _normalise_command(adb_command)
        self._instance_resolver = instance_resolver
        self._planner = port_planner or AppiumPortPlanner.from_config(self._config, port_allocator)
        self._http_client_factory = http_client_factory or self._default_http_client
        self._popen_factory = popen_factory or subprocess.Popen
        self._readiness_timeout_s = float(readiness_timeout_s)
        self._poll_interval_s = float(poll_interval_s)
        self._stop_timeout_s = float(stop_timeout_s)
        self._command_timeout_s = float(command_timeout_s)
        self._base_path = _normalise_base_path(base_path)
        self._address = address
        self._environment = dict(environment or {})
        self._chromedriver_executable = chromedriver_executable
        self._new_command_timeout = int(new_command_timeout)
        self._adb_exec_timeout = int(adb_exec_timeout)
        self._no_reset = bool(no_reset)
        self._detect_drivers = detect_drivers
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._log_handle: Any = None
        self._port: int | None = None
        self._version = ""
        self._drivers: list[str] = []

    # ------------------------------------------------------------------
    # Public: status / lifecycle
    # ------------------------------------------------------------------
    @property
    def port(self) -> int | None:
        return self._port

    @property
    def url(self) -> str:
        """Base URL of the running Appium server (empty when not running)."""
        port = self._port or self._state_port()
        if not port:
            return ""
        return self._server_url(int(port))

    def is_installed(self) -> bool:
        return self._executable() is not None

    def status(self) -> AppiumStatus:
        executable = self._executable()
        state = self._read_state()
        port = self._port or (state or {}).get("port")
        running = False
        pid: int | None = None
        version = ""
        sessions = 0
        message = ""
        if port:
            value = self._probe_status(int(port))
            if value is not None:
                running = True
                version = self._version or _version_from_status(value)
                pid = self._process.pid if self._process is not None and self._process.poll() is None else (state or {}).get("pid")
                sessions = self._count_sessions(int(port))
        if not running:
            if executable is None:
                message = (
                    "Appium is not installed; install Appium 4 with the avdfleet installer "
                    "(npm install -g appium, then appium driver install uiautomator2)"
                )
            elif self._process is not None and self._process.poll() is not None:
                message = f"Appium server exited with code {self._process.returncode}"
            else:
                message = "Appium server is not running"
        return AppiumStatus(
            running=running,
            pid=int(pid) if running and pid is not None else None,
            port=int(port) if running and port is not None else None,
            version=version if running else "",
            drivers=list(self._drivers) if running else [],
            sessions=sessions,
            url=self._server_url(int(port)) if running and port else "",
            message=message,
        )

    def start(self, *, port: int | None = None) -> AppiumStatus:
        """Start the managed Appium server; idempotent while it is running."""
        current = self.status()
        if current.running:
            if not current.message:
                current.message = "Appium is already running"
            return current
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                current.message = "Appium server is starting"
                return current
        executable = self._executable()
        if executable is None:
            return self.status()
        with self._lock:
            self._reap_stale_process()
            state = self._read_state() or {}
            preferred = port if port is not None else state.get("port")
            resolved_port = self._planner.allocate_server(int(preferred) if preferred else None)
            log_path = self._log_path(resolved_port)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                log_handle = open(  # noqa: SIM115 - kept open for the server lifetime
                    log_path, "w", encoding="utf-8", errors="replace"
                )
            except OSError as exc:
                self._planner.release_server(resolved_port)
                raise AppiumError(
                    redact_text(f"cannot open Appium log file {log_path}: {exc}"),
                    hint="check permissions on the avdfleet log directory",
                ) from exc
            argv = (
                *executable,
                "--address",
                self._address,
                "--port",
                str(resolved_port),
                "--base-path",
                self._base_path or "/",
                "--log-level",
                "info",
            )
            try:
                process = self._popen_factory(
                    list(argv),
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    cwd=str(self._data_dir()),
                    env=self._process_env(),
                    text=True,
                    bufsize=1,
                )
            except (OSError, ValueError) as exc:
                with contextlib.suppress(Exception):
                    log_handle.close()
                self._planner.release_server(resolved_port)
                raise AppiumError(
                    redact_text(f"failed to start the Appium server: {exc}"),
                    hint=f"check the Appium executable and the log at {log_path}",
                ) from exc
            self._process = process
            self._log_handle = log_handle
            self._port = resolved_port
            try:
                version = self._wait_ready(resolved_port, log_path)
            except AppiumError:
                self._terminate_process(process)
                self._close_log_handle()
                self._process = None
                self._port = None
                self._planner.release_server(resolved_port)
                raise
            drivers = self._detect_installed_drivers(executable)
            self._version = version
            self._drivers = drivers
            self._write_state(
                {
                    "pid": process.pid,
                    "port": resolved_port,
                    "base_path": self._base_path,
                    "url": self._server_url(resolved_port),
                    "log_path": str(log_path),
                    "version": version,
                    "drivers": drivers,
                    "started_at": time.time(),
                }
            )
            self._emit("started", port=resolved_port, pid=process.pid, version=version)
        return self.status()

    def stop(self) -> AppiumStatus:
        """Stop the managed server; safe to call when it is not running."""
        with self._lock:
            process = self._process
            port = self._port
            if process is not None:
                self._terminate_process(process)
                self._close_log_handle()
                self._process = None
                self._port = None
                self._version = ""
                self._drivers = []
                if port is not None:
                    self._planner.release_server(port)
                self._remove_state()
                self._emit("stopped", port=port)
                return AppiumStatus(running=False, port=None, message="Appium stopped")
            state = self._read_state()
            if state and state.get("pid"):
                pid = int(state["pid"])
                state_port = int(str(state["port"])) if state.get("port") else None
                if _pid_alive(pid):
                    _terminate_pid(pid, timeout=self._stop_timeout_s)
                if state_port is not None:
                    self._planner.release_server(state_port)
                self._remove_state()
                self._emit("stopped", port=state_port)
                return AppiumStatus(running=False, port=None, message="Appium stopped")
            self._remove_state()
            return AppiumStatus(
                running=False,
                port=None,
                message="Appium is not running",
            )

    def close(self) -> None:
        """Best-effort shutdown used by the service container."""
        try:
            self.stop()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.warning("failed to stop the Appium server cleanly", exc_info=True)

    # ------------------------------------------------------------------
    # Public: ports and capabilities
    # ------------------------------------------------------------------
    def plan_ports(self, avd: AvdInfo | str, *, ports: InstancePorts | None = None) -> AppiumPorts:
        """Plan (or return) the unique Appium ports for one instance."""
        record = self._coerce_avd(avd)
        return self._planner.plan(record.name, ports=ports if ports is not None else record.ports)

    def release_instance(self, avd_name: str) -> OperationResult:
        """Clean adb forwards/reverses for *avd_name* and release its ports."""
        result = self.cleanup_forwards(avd_name)
        self._planner.release(avd_name)
        return result

    def build_capabilities(self, avd: AvdInfo, *, template: Template | None = None) -> dict[str, Any]:
        """Build UiAutomator2 capabilities; rejects instances that are not running."""
        record = self._coerce_avd(avd)
        planned = self.plan_ports(record)
        return build_uiautomator2_capabilities(
            record,
            template=template,
            ports=planned,
            no_reset=self._no_reset,
            new_command_timeout=self._new_command_timeout,
            adb_exec_timeout=self._adb_exec_timeout,
            chromedriver_executable=self._chromedriver_executable,
        )

    # ------------------------------------------------------------------
    # Public: adb forward cleanup
    # ------------------------------------------------------------------
    def cleanup_forwards(
        self,
        avd_name: str,
        *,
        remove_all: bool = True,
        serial: str | None = None,
    ) -> OperationResult:
        """Remove adb forwards (and reverses) for an instance.

        ``remove_all=True`` (default) issues ``adb -s <serial> forward
        --remove-all`` plus the reverse equivalent, which is appropriate when the
        instance is going away or being reset.  ``remove_all=False`` removes only
        the ports this manager planned for the instance (the research-backed,
        shared-host-safe path) and verifies with ``forward --list``.
        """
        try:
            target_serial = serial or self._serial_for(avd_name)
        except NotFoundError as exc:
            return OperationResult(
                ok=False,
                message=redact_text(str(exc)),
                data={"serial": None, "commands": [], "remove_all": remove_all},
            )
        commands: list[list[str]] = []
        if remove_all:
            commands.append([*self._adb_prefix(), "-s", target_serial, "forward", "--remove-all"])
            commands.append([*self._adb_prefix(), "-s", target_serial, "reverse", "--remove-all"])
        else:
            planned = self._planner.get(avd_name)
            if planned is None:
                commands.append([*self._adb_prefix(), "-s", target_serial, "forward", "--list"])
            else:
                for side in ("forward", "reverse"):
                    for port in (planned.system_port, planned.mjpeg_port):
                        commands.append(
                            [*self._adb_prefix(), "-s", target_serial, side, "--remove", f"tcp:{port}"]
                        )
                commands.append([*self._adb_prefix(), "-s", target_serial, "forward", "--list"])
        runs: list[dict[str, Any]] = []
        ok = True
        failures: list[str] = []
        for argv in commands:
            outcome = self._run_adb(argv)
            runs.append(outcome)
            if outcome["returncode"] != 0:
                ok = False
                failures.append(
                    f"{' '.join(argv)} -> exit {outcome['returncode']}: {outcome.get('stderr') or outcome.get('error') or ''}".strip()
                )
        message = (
            f"cleaned adb forwards for {avd_name} ({target_serial})"
            if ok
            else f"adb forward cleanup reported failures for {avd_name}: " + "; ".join(failures[:3])
        )
        return OperationResult(
            ok=ok,
            message=redact_text(message),
            data={"serial": target_serial, "commands": runs, "remove_all": remove_all},
        )

    # ------------------------------------------------------------------
    # Internals: process management
    # ------------------------------------------------------------------
    def _executable(self) -> tuple[str, ...] | None:
        if self._executable_resolver is not None:
            return _normalise_command(self._executable_resolver())
        found = self._which("appium")
        return (found,) if found else None

    def _process_env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self._config.is_portable:
            appium_home = self._paths_obj().tools_dir / "appium-home"
            with contextlib.suppress(OSError):
                appium_home.mkdir(parents=True, exist_ok=True)
            env.setdefault("APPIUM_HOME", str(appium_home))
        env.update(self._environment)
        return env

    def _wait_ready(self, port: int, log_path: Path) -> str:
        deadline = self._clock() + self._readiness_timeout_s
        while self._clock() < deadline:
            process = self._process
            if process is not None and process.poll() is not None:
                raise AppiumError(
                    redact_text(
                        f"Appium server exited before becoming ready (code {process.returncode})"
                    ),
                    hint=f"see the Appium log at {log_path}: {self._log_tail(log_path)}",
                )
            value = self._probe_status(port)
            if value is not None:
                return _version_from_status(value)
            self._sleep(self._poll_interval_s)
        raise AppiumError(
            f"Appium did not become ready on {self._address}:{port} within {self._readiness_timeout_s:g}s",
            hint=f"see the Appium log at {log_path}: {self._log_tail(log_path)}",
        )

    def _probe_status(self, port: int) -> dict[str, Any] | None:
        url = self._api_url(port, "status")
        try:
            client = self._http_client_factory()
        except Exception:  # pragma: no cover - client factory specific
            return None
        try:
            response = client.get(url, timeout=2.0)
            if int(getattr(response, "status_code", 0)) != 200:
                return None
            payload = response.json()
        except (httpx.HTTPError, OSError, ValueError):
            return None
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()
        if not isinstance(payload, dict):
            return None
        value = payload.get("value")
        if not isinstance(value, dict):
            value = payload if payload.get("ready") is not None else {}
        ready = bool(value.get("ready")) or str(value.get("state", "")).lower() == "ready"
        return value if ready else None

    def _count_sessions(self, port: int) -> int:
        url = self._api_url(port, "sessions")
        try:
            client = self._http_client_factory()
            try:
                response = client.get(url, timeout=2.0)
                if int(getattr(response, "status_code", 0)) != 200:
                    return 0
                payload = response.json()
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    with contextlib.suppress(Exception):
                        close()
        except (httpx.HTTPError, OSError, ValueError):
            return 0
        value = payload.get("value") if isinstance(payload, dict) else payload
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            sessions = value.get("sessions")
            if isinstance(sessions, list):
                return len(sessions)
            return len(value)
        return 0

    def _detect_installed_drivers(self, executable: tuple[str, ...]) -> list[str]:
        if not self._detect_drivers:
            return []
        try:
            completed = subprocess.run(
                [*executable, "driver", "list", "--installed"],
                capture_output=True,
                text=True,
                timeout=self._command_timeout_s,
                check=False,
                env=self._process_env(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        text = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        drivers: list[str] = []
        for match in re.finditer(r"([A-Za-z][A-Za-z0-9_-]*)@([0-9][\w.\-]*)", text):
            entry = f"{match.group(1)}@{match.group(2)}"
            if entry not in drivers:
                drivers.append(entry)
        return drivers

    def _terminate_process(self, process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if _psutil is not None:
            try:
                parent = _psutil.Process(process.pid)
                children = parent.children(recursive=True)
                for child in children:
                    with contextlib.suppress(_psutil.Error):
                        child.terminate()
                with contextlib.suppress(_psutil.Error):
                    parent.terminate()
                _, alive = _psutil.wait_procs([parent, *children], timeout=self._stop_timeout_s)
                for remaining in alive:
                    with contextlib.suppress(_psutil.Error):
                        remaining.kill()
                return
            except _psutil.Error:  # pragma: no cover - vanished concurrently
                pass
        process.terminate()
        try:
            process.wait(timeout=self._stop_timeout_s)
        except subprocess.TimeoutExpired:
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=self._stop_timeout_s)

    def _reap_stale_process(self) -> None:
        if self._process is not None and self._process.poll() is not None:
            self._close_log_handle()
            self._process = None
            self._port = None

    def _close_log_handle(self) -> None:
        handle = self._log_handle
        self._log_handle = None
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.close()

    def _log_tail(self, log_path: Path) -> str:
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return redact_text(text[-_MAX_LOG_TAIL:]).strip()

    # ------------------------------------------------------------------
    # Internals: adb helpers
    # ------------------------------------------------------------------
    def _adb_prefix(self) -> list[str]:
        if self._adb_command:
            return list(self._adb_command)
        found = self._which("adb")
        return [found or "adb"]

    def _run_adb(self, argv: Sequence[str]) -> dict[str, Any]:
        argv_list = [str(part) for part in argv]
        if self._adb_runner is not None:
            try:
                completed = self._adb_runner(argv_list)
            except OSError as exc:
                return {
                    "argv": argv_list,
                    "returncode": 127,
                    "stdout": "",
                    "stderr": "",
                    "error": redact_text(f"cannot run adb: {exc}"),
                }
        else:
            try:
                completed = subprocess.run(
                    argv_list,
                    capture_output=True,
                    text=True,
                    timeout=self._command_timeout_s,
                    check=False,
                    env=self._process_env(),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {
                    "argv": argv_list,
                    "returncode": 127,
                    "stdout": "",
                    "stderr": "",
                    "error": redact_text(f"cannot run adb: {exc}"),
                }
        return {
            "argv": argv_list,
            "returncode": int(getattr(completed, "returncode", -1)),
            "stdout": redact_text(getattr(completed, "stdout", "") or ""),
            "stderr": redact_text(getattr(completed, "stderr", "") or ""),
        }

    def _serial_for(self, avd_name: str) -> str:
        resolver = self._instance_resolver
        if resolver is None:
            return avd_name
        value = resolver(avd_name)
        if value is None:
            raise NotFoundError(f"instance {avd_name!r} not found")
        if isinstance(value, AvdInfo):
            return serial_for(value)
        if isinstance(value, str):
            return value
        serial = getattr(value, "adb_serial", None)
        if serial:
            return str(serial)
        raise AppiumError(f"cannot determine the adb serial for instance {avd_name!r}")

    # ------------------------------------------------------------------
    # Internals: paths / URLs / state
    # ------------------------------------------------------------------
    def _paths_obj(self) -> Paths:
        if self._paths is None:
            self._paths = self._config.resolved_paths()
        return self._paths

    def _data_dir(self) -> Path:
        paths = self._paths_obj()
        directory = (Path(paths.data_dir) if paths.data_dir is not None else Path(paths.root) / "data") / "appium"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _log_path(self, port: int) -> Path:
        paths = self._paths_obj()
        directory = (Path(paths.logs_dir) if paths.logs_dir is not None else Path(paths.root) / "logs") / "appium"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"appium-{port}.log"

    def _state_file(self) -> Path:
        return self._data_dir() / "server.json"

    def _read_state(self) -> dict[str, Any] | None:
        path = self._state_file()
        try:
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_state(self, payload: dict[str, Any]) -> None:
        path = self._state_file()
        temp = path.with_suffix(".json.tmp")
        try:
            temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(temp, path)
        except OSError:  # pragma: no cover - best-effort state file
            logger.warning("could not persist Appium server state", exc_info=True)
            with contextlib.suppress(OSError):
                temp.unlink()

    def _remove_state(self) -> None:
        with contextlib.suppress(OSError):
            self._state_file().unlink()

    def _state_port(self) -> int | None:
        state = self._read_state()
        port = state.get("port") if state else None
        try:
            return int(port) if port is not None else None
        except (TypeError, ValueError):
            return None

    def _server_url(self, port: int) -> str:
        suffix = self._base_path.rstrip("/")
        return f"http://{self._address}:{port}{suffix}"

    def _api_url(self, port: int, resource: str) -> str:
        base = self._base_path if self._base_path else ""
        return f"http://{self._address}:{port}{base}/{resource.lstrip('/')}"

    @staticmethod
    def _default_http_client() -> Any:
        return httpx.Client(timeout=httpx.Timeout(2.0))

    # ------------------------------------------------------------------
    # Internals: misc
    # ------------------------------------------------------------------
    def _coerce_avd(self, avd: AvdInfo | str) -> AvdInfo:
        if isinstance(avd, AvdInfo):
            return avd
        resolver = self._instance_resolver
        if resolver is None:
            raise NotFoundError(f"instance {avd!r} not found")
        value = resolver(str(avd))
        if isinstance(value, AvdInfo):
            return value
        if value is None:
            raise NotFoundError(f"instance {avd!r} not found")
        raise AppiumError(f"instance resolver returned an unsupported value for {avd!r}")

    def _emit(self, action: str, **extra: Any) -> None:
        payload = {"component": "appium", "action": action, **extra}
        try:
            self._sink.emit(EventType.SYSTEM, payload, source="appium")
        except Exception:  # noqa: BLE001 - telemetry is best effort
            logger.debug("appium event emit failed", exc_info=True)


def _normalise_command(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, Path)):
        text = str(value)
        return (text,) if text else None
    try:
        argv = tuple(str(part) for part in value)
    except TypeError:
        return None
    return argv or None


def _normalise_base_path(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return "/"
    if not text.startswith("/"):
        text = "/" + text
    return text.rstrip("/") or "/"


def _version_from_status(value: Mapping[str, Any]) -> str:
    build = value.get("build")
    if isinstance(build, dict) and build.get("version"):
        return str(build["version"])
    if value.get("version"):
        return str(value["version"])
    return ""


def _pid_alive(pid: int) -> bool:
    if _psutil is not None:
        try:
            return bool(_psutil.pid_exists(pid))
        except Exception:  # pragma: no cover - psutil specific
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _terminate_pid(pid: int, *, timeout: float = 10.0) -> None:
    if _psutil is not None:
        try:
            process = _psutil.Process(pid)
            children = process.children(recursive=True)
            for child in children:
                with contextlib.suppress(_psutil.Error):
                    child.terminate()
            with contextlib.suppress(_psutil.Error):
                process.terminate()
            _, alive = _psutil.wait_procs([process, *children], timeout=timeout)
            for remaining in alive:
                with contextlib.suppress(_psutil.Error):
                    remaining.kill()
            return
        except _psutil.Error:  # pragma: no cover - vanished concurrently
            pass
    with contextlib.suppress(OSError):
        os.kill(pid, 15)
