"""``RuntimeManagerService`` - emulator lifecycle, boot, supervision, snapshots.

Implements :class:`avdfleet.core.interfaces.RuntimeManager` on top of an injected
:class:`~avdfleet.platform.avd.manager.AvdManagerService` (definitions, config.ini
and templates), an optional SDK backend (executables/environment), a
``core.PortAllocator``, an :class:`AdbRunner`, an injectable process factory and
an optional proxy bridge/pool.

Lifecycle and guarantees:

* ``start`` validates capacity **before** launching (RAM from psutil, CPU, disk,
  ports and ``max_instances``), leases a unique even console/adb pair, builds a
  credential-free emulator argv, spawns a detached process, records
  pid/ports/start time, persists the state and emits
  ``instance.state`` events.  ``dry_run`` returns the plan without spawning.
* ``wait_boot`` polls ``sys.boot_completed`` *and* ``pm path android`` through
  ``adb`` with the configured timeout; on timeout it force-stops the instance,
  marks it ``error`` and raises :class:`BootTimeoutError`.
* ``stop`` prefers the emulator console's graceful ``kill``, falls back to
  ``terminate`` then ``kill``, always releases ports and stops the proxy bridge.
* ``supervisor`` notices external exits in the background and marks the instance
  ``error`` (or ``stopped`` when the stop was requested) and optionally restarts.
* snapshots go through :class:`SnapshotService` (console first, ``adb emu``
  fallback) with persistent golden-snapshot metadata.
* no proxy password, token or vault secret ever enters argv, logs or persisted
  state; only the credential-free bridge upstream and the proxy id are stored.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from avdfleet.core.config import Config
from avdfleet.core.errors import (
    AvdFleetError,
    BootTimeoutError,
    CapacityError,
    ConfigError,
    ConflictError,
    InstanceError,
    NotFoundError,
    ValidationError,
)
from avdfleet.core.events import EventBus, EventSink, EventType
from avdfleet.core.logging import get_logger, redact
from avdfleet.core.models import (
    AvdInfo,
    BridgeConfig,
    InstancePorts,
    InstanceState,
    OperationResult,
    ProxyConfig,
    Snapshot,
    Template,
    utcnow,
)
from avdfleet.core.paths import Paths
from avdfleet.core.ports import PortAllocator

from .boot import (
    AdbRunner,
    BackendAdbRunner,
    BootDetector,
    SubprocessAdbRunner,
    boot_serial,
)
from .capacity import CapacityService
from .launch import (
    LaunchSpec,
    ProcessFactory,
    ProcessHandle,
    SubprocessProcessFactory,
    build_launch_spec,
    launch_log_path,
    screen_endpoint,
)
from .snapshots import (
    ConsoleFactory,
    EmulatorConsoleLike,
    SnapshotService,
    SnapshotTarget,
    default_console_factory,
)
from .supervisor import ExitRecord, ProcessSupervisor, RestartPolicy, SupervisedProcess

logger = get_logger("platform.runtime.manager")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RUNTIME_STATE_FILENAME = "runtime-state.json"
RUNTIME_STATE_VERSION = "1"
_ACTIVE_STATES = (InstanceState.STARTING, InstanceState.BOOTING, InstanceState.RUNNING)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pid_alive(pid: int | None) -> bool:
    """Cross-platform liveness probe (never signals the process)."""
    if not pid or int(pid) <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - Windows hosts
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # type: ignore[attr-defined]
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):  # type: ignore[attr-defined]
                    return False
                return exit_code.value == 259
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _coerce_event_sink(events: Any) -> EventSink:
    if events is None:
        return EventSink()
    if isinstance(events, EventSink):
        return events
    if hasattr(events, "on_instance_state") and hasattr(events, "emit"):
        return events
    if isinstance(events, EventBus) or (
        hasattr(events, "emit") and hasattr(events, "subscribe")
    ):
        return EventSink(events)
    raise ValidationError("events must be an EventBus or EventSink")


def _looks_like_instance_repo(candidate: Any) -> bool:
    return candidate is not None and all(
        callable(getattr(candidate, method, None))
        for method in ("list", "get", "upsert", "delete")
    )


# ---------------------------------------------------------------------------
# Persisted runtime state
# ---------------------------------------------------------------------------


class RuntimeStateStore:
    """Persist the last known state/ports/pid of every instance.

    Uses the injected store's ``instances`` repository when present, otherwise an
    atomic JSON file under ``Paths.data_dir``.  Read paths are defensive: a
    corrupt entry is skipped so a failed status query cannot break the daemon.
    """

    def __init__(self, store: Any | None = None, paths: Paths | None = None) -> None:
        self._repo = self._resolve_repo(store)
        self._paths = paths
        self._lock = threading.RLock()

    @staticmethod
    def _resolve_repo(store: Any | None) -> Any | None:
        if store is None:
            return None
        candidate = getattr(store, "instances", None)
        if _looks_like_instance_repo(candidate):
            return candidate
        if _looks_like_instance_repo(store):
            return store
        return None

    @property
    def backing(self) -> str:
        return "store" if self._repo is not None else "json"

    @property
    def path(self) -> Path | None:
        if self._paths is None or self._paths.data_dir is None:
            return None
        return Path(self._paths.data_dir) / RUNTIME_STATE_FILENAME

    # -- io -------------------------------------------------------------
    def _read_json(self) -> dict[str, dict[str, Any]]:
        path = self.path
        if path is None:
            return {}
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            logger.warning("could not read runtime state %s: %s", path, exc)
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("runtime state %s is corrupt; ignoring it", path)
            return {}
        entries = data.get("instances") if isinstance(data, dict) else None
        return entries if isinstance(entries, dict) else {}

    def _write_json(self, entries: Mapping[str, Mapping[str, Any]]) -> None:
        path = self.path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": RUNTIME_STATE_VERSION,
            "updated_at": utcnow().isoformat(),
            "instances": {name: dict(entry) for name, entry in entries.items()},
        }
        temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
        try:
            temporary.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError as exc:
            logger.warning("could not persist runtime state %s: %s", path, exc)

    # -- crud -----------------------------------------------------------
    def save(self, info: AvdInfo) -> None:
        if self._repo is not None:
            try:
                self._repo.upsert(info)
                return
            except Exception as exc:  # noqa: BLE001 - persistence is best effort
                logger.warning("could not persist runtime metadata for %s: %s", info.name, exc)
                return
        with self._lock:
            entries = self._read_json()
            entries[info.name] = info.model_dump(mode="json")
            self._write_json(entries)

    def load(self, name: str) -> AvdInfo | None:
        if self._repo is not None:
            try:
                return self._repo.get(name)
            except NotFoundError:
                return None
            except Exception as exc:  # noqa: BLE001
                logger.debug("runtime metadata lookup failed for %s: %s", name, exc)
                return None
        with self._lock:
            entry = self._read_json().get(name)
        if not entry:
            return None
        try:
            return AvdInfo.model_validate(entry)
        except Exception:  # noqa: BLE001
            logger.warning("ignoring corrupt runtime metadata for %s", name)
            return None

    def load_all(self) -> dict[str, AvdInfo]:
        if self._repo is not None:
            try:
                infos = self._repo.list() or []
            except Exception as exc:  # noqa: BLE001
                logger.warning("runtime metadata listing failed: %s", exc)
                return {}
            return {info.name: info for info in infos if isinstance(info, AvdInfo)}
        entries = self._read_json()
        result: dict[str, AvdInfo] = {}
        for name, entry in entries.items():
            try:
                result[str(name)] = AvdInfo.model_validate(entry)
            except Exception:  # noqa: BLE001
                logger.warning("ignoring corrupt runtime metadata for %s", name)
        return result

    def delete(self, name: str) -> None:
        if self._repo is not None:
            try:
                self._repo.delete(name)
            except NotFoundError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not delete runtime metadata for %s: %s", name, exc)
            return
        with self._lock:
            entries = self._read_json()
            if entries.pop(name, None) is not None:
                self._write_json(entries)


# ---------------------------------------------------------------------------
# Running instance record
# ---------------------------------------------------------------------------


@dataclass
class RunningInstance:
    """Everything avdfleet knows about a live emulator process it launched."""

    name: str
    process: ProcessHandle
    spec: LaunchSpec
    ports: InstancePorts
    state: InstanceState
    started_at: datetime
    started_monotonic: float
    template_id: str | None = None
    image_id: str | None = None
    device_id: str | None = None
    proxy_id: str | None = None
    bridge: BridgeConfig | None = None
    headless: bool = True
    boot_completed: bool = False
    adb_serial: str | None = None
    error: str = ""
    expected_stop: bool = False
    last_exit_code: int | None = None
    restart_attempts: int = 0

    @property
    def pid(self) -> int | None:
        return getattr(self.process, "pid", None)

    @property
    def active(self) -> bool:
        return self.state in _ACTIVE_STATES


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class RuntimeManagerService:
    """Concrete ``RuntimeManager`` implementation (see module docstring)."""

    def __init__(
        self,
        avds: Any,
        backend: Any | None = None,
        *,
        sdk: Any | None = None,
        config: Config | None = None,
        paths: Paths | None = None,
        store: Any | None = None,
        events: Any | None = None,
        port_allocator: PortAllocator | None = None,
        adb: Any | None = None,
        adb_runner: Any | None = None,
        process_factory: ProcessFactory | None = None,
        console_factory: ConsoleFactory | None = None,
        capacity: CapacityService | None = None,
        supervisor: ProcessSupervisor | None = None,
        restart_policy: RestartPolicy | None = None,
        proxy_pool: Any | None = None,
        proxy_resolver: Callable[[str], Any] | None = None,
        bridge: Any | None = None,
        direct_proxy_enabled: bool = False,
        snapshot_service: SnapshotService | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = utcnow,
        auto_supervision: bool = True,
        supervisor_interval_s: float = 2.0,
        startup_grace_s: float = 0.0,
        allow_external_processes: bool = True,
    ) -> None:
        if avds is None:
            raise ConfigError(
                "runtime requires the AVD manager service",
                hint="Construct AvdManagerService first and pass it as avds=...",
            )
        self.avds = avds
        self.backend = backend if backend is not None else (sdk or getattr(avds, "backend", None))
        self.config = config or getattr(avds, "config", None) or Config.defaults()
        self.paths = paths or getattr(avds, "paths", None) or self.config.resolved_paths()
        self._now = now
        self._clock = clock
        self._sleep = sleep
        self._sink = _coerce_event_sink(events)
        self._process_factory: ProcessFactory = process_factory or SubprocessProcessFactory()
        self._console_factory: ConsoleFactory = console_factory or default_console_factory
        self._proxy_pool = proxy_pool
        self._proxy_resolver = proxy_resolver
        self._bridge = bridge
        self.direct_proxy_enabled = bool(direct_proxy_enabled)
        self._auto_supervision = bool(auto_supervision)
        self._startup_grace_s = max(0.0, float(startup_grace_s))
        self._allow_external = bool(allow_external_processes)
        self._lock = threading.RLock()
        self._records: dict[str, RunningInstance] = {}
        self._state_store = RuntimeStateStore(store, self.paths)
        self._port_allocator = port_allocator or PortAllocator(
            start=int(self.config.port_range_start),
            end=int(self.config.port_range_end),
            appium_start=int(self.config.appium_port_start),
        )
        self._adb: AdbRunner = (
            adb
            or adb_runner
            or (BackendAdbRunner(self.backend) if self.backend is not None else SubprocessAdbRunner("adb"))
        )
        self._boot = BootDetector(
            self._adb,
            clock=self._clock,
            sleep=self._sleep,
            poll_interval_s=float(self.config.boot_poll_interval_s),
        )
        self._capacity = capacity or CapacityService(
            config=self.config,
            paths=self.paths,
            port_allocator=self._port_allocator,
            instances=self._capacity_instances,
        )
        self._snapshots = snapshot_service or SnapshotService(
            self.paths,
            console_factory=self._console_factory,
            adb=self._adb,
            clock=self._now,
        )
        self._supervisor = supervisor or ProcessSupervisor(
            self._supervised_processes,
            interval_s=supervisor_interval_s,
            restart_policy=restart_policy,
            restart=self._restart_from_supervisor,
            clock=self._clock,
            sleep=self._sleep,
        )

    # ------------------------------------------------------------------
    # Basic plumbing
    # ------------------------------------------------------------------
    def _validate_name(self, name: str) -> str:
        candidate = (name or "").strip()
        if not candidate:
            raise ValidationError("instance name must not be empty")
        if not _NAME_RE.match(candidate):
            raise ValidationError(
                f"invalid instance name {candidate!r}: use letters, digits, '.', '_' and '-'",
            )
        return candidate

    def _require_instance(self, name: str) -> AvdInfo:
        try:
            return self.avds.get_instance(name)
        except NotFoundError:
            persisted = self._state_store.load(name)
            if persisted is not None:
                return persisted
            raise

    def _base_info(self, name: str) -> AvdInfo:
        try:
            return self.avds.get_instance(name)
        except AvdFleetError as exc:
            logger.debug("AVD manager lookup failed for %s: %s", name, exc)
            persisted = self._state_store.load(name)
            return persisted or AvdInfo(name=name)

    def _avd_home(self) -> Path | None:
        home = getattr(self.avds, "avd_home", None)
        if home:
            return Path(home)
        if self.paths is not None and self.paths.avd_home:
            return Path(self.paths.avd_home)
        return None

    def _emulator_executable(self) -> Path:
        if self.backend is None:
            return Path("emulator")
        try:
            return Path(self.backend.executable("emulator"))
        except AvdFleetError as exc:
            raise InstanceError(
                f"the Android emulator executable is not available: {exc}",
                hint="Install the emulator SDK package or repair the SDK.",
            ) from exc

    def _launch_env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.backend is not None:
            try:
                env.update({str(k): str(v) for k, v in (self.backend.environment() or {}).items()})
            except AvdFleetError as exc:  # pragma: no cover - backend failure
                logger.warning("could not resolve SDK environment: %s", exc)
        home = self._avd_home()
        if home is not None:
            env.setdefault("ANDROID_AVD_HOME", str(home))
        return env

    # ------------------------------------------------------------------
    # Records / state
    # ------------------------------------------------------------------
    def _poll(self, record: RunningInstance) -> int | None:
        try:
            code = record.process.poll()
        except Exception as exc:  # noqa: BLE001 - treat a broken handle as exited
            logger.warning("could not poll emulator process for %s: %s", record.name, exc)
            code = -1
        if code is not None:
            record.last_exit_code = code
        return code

    def _record(self, name: str) -> RunningInstance | None:
        return self._records.get(name)

    def _active_record(self, name: str) -> RunningInstance | None:
        record = self._records.get(name)
        if record is None:
            return None
        if self._poll(record) is not None:
            return None
        return record

    def _emit_state(self, name: str, state: InstanceState, **extra: Any) -> None:
        # EventSink.on_instance_state publishes EventType.INSTANCE_STATE itself.
        self._sink.on_instance_state(name, state.value, **extra)

    def _persist_state(
        self,
        name: str,
        *,
        state: InstanceState,
        ports: InstancePorts | None = None,
        error: str = "",
        boot_completed: bool = False,
        started_at: datetime | None = None,
        template_id: str | None = None,
        image_id: str | None = None,
        device_id: str | None = None,
        proxy_id: str | None = None,
        adb_serial: str | None = None,
    ) -> AvdInfo:
        base = self._base_info(name)
        effective_ports = ports or base.ports
        updates: dict[str, Any] = {
            "state": state,
            "pid": None,
            "ports": effective_ports,
            "boot_completed": bool(boot_completed),
            "error": error,
            "started_at": started_at,
            "uptime_seconds": None,
            "adb_serial": adb_serial,
            "grpc_endpoint": screen_endpoint(effective_ports),
            "screen_available": effective_ports is not None
            and effective_ports.grpc is not None,
        }
        if template_id is not None or base.template_id is not None:
            updates["template_id"] = template_id or base.template_id
        if image_id is not None or base.image_id is not None:
            updates["image_id"] = image_id or base.image_id
        if device_id is not None or base.device_id:
            updates["device_id"] = device_id or base.device_id
        if proxy_id is not None:
            updates["proxy_id"] = proxy_id
        if ports is not None:
            updates["config"] = base.config
        info = base.model_copy(update=updates)
        self._state_store.save(info)
        return info

    def _persist_record(self, record: RunningInstance) -> AvdInfo:
        info = self._info_for(record.name, record)
        self._state_store.save(info)
        return info

    def _sync_record(self, record: RunningInstance) -> AvdInfo:
        """Build live info and persist only when the durable state actually changed.

        Status/list polls must not rewrite SQLite/JSON for every uptime tick; the
        stored record changes on state, pid or boot transitions only.
        """
        info = self._info_for(record.name, record)
        persisted = self._state_store.load(record.name)
        if (
            persisted is None
            or persisted.state is not info.state
            or persisted.pid != info.pid
            or persisted.boot_completed != info.boot_completed
        ):
            self._state_store.save(info)
        return info

    def _info_for(self, name: str, record: RunningInstance | None = None) -> AvdInfo:
        if record is None:
            base = self._require_instance(name)
            if base.state.active and not _pid_alive(base.pid):
                base = base.model_copy(
                    update={
                        "state": InstanceState.STOPPED,
                        "pid": None,
                        "boot_completed": False,
                        "error": base.error,
                    }
                )
            return base
        base = self._base_info(name)
        uptime = max(0.0, self._clock() - record.started_monotonic)
        return base.model_copy(
            update={
                "template_id": record.template_id or base.template_id,
                "image_id": record.image_id or base.image_id,
                "device_id": record.device_id or base.device_id,
                "state": record.state,
                "pid": record.pid,
                "ports": record.ports,
                "proxy_id": record.proxy_id if record.proxy_id is not None else base.proxy_id,
                "adb_serial": record.adb_serial or boot_serial(record.ports),
                "boot_completed": record.boot_completed,
                "created_at": base.created_at,
                "started_at": record.started_at,
                "uptime_seconds": round(uptime, 3),
                "error": record.error,
                "grpc_endpoint": screen_endpoint(record.ports),
                "screen_available": record.ports.grpc is not None,
                "config": base.config,
            }
        )

    def _release_ports(self, name: str) -> None:
        try:
            self._port_allocator.release(name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("port release failed for %s: %s", name, exc)

    def _stop_bridge(self, name: str) -> None:
        if self._bridge is None:
            return
        try:
            self._bridge.stop(name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("proxy bridge stop failed for %s: %s", name, exc)

    # ------------------------------------------------------------------
    # External process detection helpers
    # ------------------------------------------------------------------
    def _emu_devices(self) -> dict[str, Any] | None:
        try:
            devices = self._adb.devices()
        except Exception as exc:  # noqa: BLE001 - adb may be restarting
            logger.debug("adb devices probe failed: %s", exc)
            return None
        return {device.serial: device for device in devices}

    def _reconcile_visibility(
        self, record: RunningInstance, devices: Mapping[str, Any] | None = None
    ) -> None:
        """Adjust STARTING/BOOTING/RUNNING from what adb currently reports."""
        if devices is None:
            devices = self._emu_devices()
        if devices is None:
            return
        serial = record.adb_serial or boot_serial(record.ports)
        record.adb_serial = serial
        device = devices.get(serial)
        if device is None:
            if not record.boot_completed and record.state is InstanceState.RUNNING or record.state not in _ACTIVE_STATES:
                record.state = InstanceState.STARTING
            return
        state = str(getattr(device, "state", ""))
        if state == "device":
            if record.boot_completed:
                record.state = InstanceState.RUNNING
            elif record.state is InstanceState.STARTING:
                record.state = InstanceState.BOOTING
        elif state in {"offline", "booting", "connecting", "authorizing"}:
            record.state = InstanceState.BOOTING

    def _handle_process_exit(
        self,
        name: str,
        exit_code: int | None,
        *,
        expected: bool | None = None,
    ) -> AvdInfo | None:
        """Reconcile one process exit (called by stop() and the supervisor)."""
        with self._lock:
            record = self._records.pop(name, None)
            if record is None:
                return None
            anticipated = record.expected_stop if expected is None else bool(expected)
            record.last_exit_code = exit_code
            state = InstanceState.STOPPED if anticipated else InstanceState.ERROR
            error = (
                ""
                if anticipated
                else f"emulator exited unexpectedly (exit code {exit_code})"
            )
            self._release_ports(name)
            self._stop_bridge(name)
            info = self._persist_state(
                name,
                state=state,
                ports=record.ports,
                error=error,
                boot_completed=False,
                started_at=None,
                template_id=record.template_id,
                image_id=record.image_id,
                device_id=record.device_id,
                proxy_id=record.proxy_id,
            )
        self._emit_state(
            name,
            state,
            pid=None,
            exit_code=exit_code,
            error=error,
        )
        if not anticipated:
            self._sink.emit(
                EventType.ERROR,
                {
                    "instance": name,
                    "error": error,
                    "exit_code": exit_code,
                },
                source="platform.runtime",
            )
            logger.warning("instance %s %s", name, error)
        return info

    # ------------------------------------------------------------------
    # Capacity helpers
    # ------------------------------------------------------------------
    def _capacity_instances(self) -> list[AvdInfo]:
        infos: dict[str, AvdInfo] = {}
        try:
            for info in self.avds.list_instances() or []:
                infos[info.name] = info
        except AvdFleetError as exc:
            logger.debug("could not list AVDs for capacity: %s", exc)
        for name, record in self._records.items():
            try:
                infos[name] = self._info_for(name, record)
            except AvdFleetError:
                continue
        for name, info in self._state_store.load_all().items():
            if name not in infos and info.state.active and self._allow_external:
                infos[name] = info
        return list(infos.values())

    def capacity(self) -> dict[str, Any]:
        return self._capacity.status()

    # ------------------------------------------------------------------
    # Proxy integration
    # ------------------------------------------------------------------
    def _resolve_proxy(self, proxy_id: str | None) -> ProxyConfig | None:
        if not proxy_id:
            return None
        candidate: Any = None
        if self._proxy_pool is not None:
            getter = getattr(self._proxy_pool, "get", None)
            if callable(getter):
                try:
                    candidate = getter(proxy_id)
                except NotFoundError:
                    raise NotFoundError(f"proxy {proxy_id!r} was not found") from None
                except (KeyError, IndexError):
                    raise NotFoundError(f"proxy {proxy_id!r} was not found") from None
        if candidate is None and self._proxy_resolver is not None:
            candidate = self._proxy_resolver(proxy_id)
        if candidate is None:
            raise NotFoundError(
                f"proxy {proxy_id!r} was not found",
                hint="Import the proxy and retry, or start the instance without a proxy.",
            )
        if isinstance(candidate, ProxyConfig):
            proxy = candidate
        elif isinstance(candidate, Mapping):
            proxy = ProxyConfig.model_validate(dict(candidate))
        else:
            raise ValidationError(f"proxy {proxy_id!r} has an unsupported type")
        if not proxy.enabled:
            raise ValidationError(
                f"proxy {proxy_id!r} is disabled",
                hint="Enable the proxy before starting an instance with it.",
            )
        return proxy

    def _proxy_upstream(
        self, name: str, proxy: ProxyConfig | None
    ) -> tuple[str | None, BridgeConfig | None]:
        """Return a credential-free ``-http-proxy`` upstream and bridge config."""
        if proxy is None:
            return None, None
        if self._bridge is not None:
            try:
                ensured = self._bridge.ensure(name, proxy)
            except AvdFleetError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise InstanceError(
                    f"could not start the local proxy bridge for {name!r}: {redact(str(exc))}",
                    hint="Check the proxy bridge service and its port range.",
                ) from exc
            if isinstance(ensured, BridgeConfig):
                bridge_config = ensured
            elif isinstance(ensured, Mapping):
                bridge_config = BridgeConfig.model_validate(dict(ensured))
            else:
                raise ValidationError(f"proxy bridge returned an unsupported value: {ensured!r}")
            upstream = f"http://{bridge_config.listen_host}:{int(bridge_config.listen_port)}"
            return upstream, bridge_config
        if self.direct_proxy_enabled:
            if proxy.has_credentials:
                raise InstanceError(
                    "direct emulator proxying cannot carry proxy credentials",
                    hint="Enable the local proxy bridge for authenticated proxies.",
                )
            return f"{proxy.scheme.value}://{proxy.host}:{proxy.port}", None
        raise InstanceError(
            "a proxy was requested but no proxy bridge is configured",
            hint="Wire the local proxy bridge, or explicitly enable direct proxying "
            "for proxy servers that need no authentication.",
        )

    # ------------------------------------------------------------------
    # Launch helpers
    # ------------------------------------------------------------------
    def _template_for(self, info: AvdInfo) -> Template | None:
        template_id = info.template_id
        if not template_id:
            return None
        try:
            return self.avds.get_template(template_id)
        except NotFoundError:
            logger.warning("template %s for instance %s no longer exists", template_id, info.name)
            return None

    def _resolve_ram_mb(self, info: AvdInfo, template: Template | None) -> int:
        try:
            return max(512, int(str(info.config.get("hw.ramSize"))))
        except (TypeError, ValueError):
            pass
        if template is not None:
            return max(512, int(template.ram_mb))
        return max(512, int(self.config.default_ram_mb))

    def _lease_ports(self, name: str, previous: InstancePorts | None) -> InstancePorts:
        preferred = None
        if previous is not None:
            preferred = int(previous.console)
        try:
            return self._port_allocator.lease(name, preferred_console=preferred)
        except CapacityError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise InstanceError(f"could not allocate emulator ports: {exc}") from exc

    # ------------------------------------------------------------------
    # Core lifecycle
    # ------------------------------------------------------------------
    def start(
        self,
        name: str,
        *,
        proxy_id: str | None = None,
        headless: bool | None = None,
        dry_run: bool = False,
        wipe_data: bool = False,
    ) -> AvdInfo:
        candidate = self._validate_name(name)
        base_info = self._require_instance(candidate)
        with self._lock:
            record = self._records.get(candidate)
            if record is not None:
                code = self._poll(record)
                if code is None:
                    raise ConflictError(
                        f"instance {candidate!r} is already running (pid {record.pid})",
                        hint="Stop or restart the instance instead of starting it twice.",
                    )
                self._handle_process_exit(candidate, code)
            persisted = self._state_store.load(candidate)
            if (
                persisted is not None
                and persisted.state.active
                and _pid_alive(persisted.pid)
            ):
                raise ConflictError(
                    f"instance {candidate!r} is already running (pid {persisted.pid})",
                    hint="The process was started outside this daemon; stop it first.",
                )
            template = self._template_for(base_info)
            image_id = base_info.image_id or (template.image_id if template else None)
            if not image_id:
                raise ValidationError(
                    f"instance {candidate!r} has no system image",
                    hint="Recreate the instance from a template with an image.",
                )
            resolved_headless = (
                bool(template.headless)
                if headless is None and template is not None
                else (bool(self.config.headless) if headless is None else bool(headless))
            )
            gpu_mode = template.gpu_mode if template is not None else self.config.gpu_mode
            device_id = base_info.device_id or (template.device_id if template else "")
            ram_mb = self._resolve_ram_mb(base_info, template)
            requested_proxy = proxy_id or (template.proxy_id if template else None) or base_info.proxy_id
            proxy = self._resolve_proxy(requested_proxy)
            upstream, bridge_config = self._proxy_upstream(candidate, proxy)
            decision = self._capacity.ensure_can_start(ram_mb)
            previous_ports = (
                record.ports
                if record is not None
                else (persisted.ports if persisted is not None else base_info.ports)
            )
            ports = self._lease_ports(candidate, previous_ports)
            try:
                spec = build_launch_spec(
                    self._emulator_executable(),
                    candidate,
                    ports,
                    env=self._launch_env(),
                    cwd=self._avd_home(),
                    log_path=launch_log_path(self.paths, candidate),
                    headless=resolved_headless,
                    gpu_mode=gpu_mode,
                    proxy_upstream=upstream,
                    wipe_data=wipe_data,
                )
            except Exception:
                self._release_ports(candidate)
                raise
            if dry_run:
                self._release_ports(candidate)
                self._stop_bridge(candidate)
                planned = dict(base_info.config or {})
                planned.update(
                    {
                        "dry_run": "true",
                        "launch.argv": spec.redacted_command,
                        "launch.headless": "true" if resolved_headless else "false",
                        "launch.ram_mb": str(ram_mb),
                    }
                )
                return base_info.model_copy(
                    update={
                        "state": InstanceState.STOPPED,
                        "ports": ports,
                        "config": planned,
                        "grpc_endpoint": screen_endpoint(ports),
                        "screen_available": ports.grpc is not None,
                        "adb_serial": boot_serial(ports),
                        "error": "",
                    }
                )
            logger.info(
                "launching %s on console port %s (pid pending, headless=%s)",
                candidate,
                ports.console,
                resolved_headless,
            )
            try:
                process = self._process_factory(
                    spec.argv,
                    env=spec.env,
                    cwd=spec.cwd,
                    log_path=spec.log_path,
                )
            except AvdFleetError:
                self._release_ports(candidate)
                self._stop_bridge(candidate)
                raise
            except Exception as exc:  # noqa: BLE001
                self._release_ports(candidate)
                self._stop_bridge(candidate)
                raise InstanceError(
                    f"could not start emulator for {candidate!r}: {exc}",
                    hint="Check the emulator log and the SDK installation.",
                ) from exc
            record = RunningInstance(
                name=candidate,
                process=process,
                spec=spec,
                ports=ports,
                state=InstanceState.STARTING,
                started_at=self._now(),
                started_monotonic=self._clock(),
                template_id=template.id if template is not None else base_info.template_id,
                image_id=str(image_id),
                device_id=device_id,
                proxy_id=requested_proxy,
                bridge=bridge_config,
                headless=resolved_headless,
                adb_serial=boot_serial(ports),
            )
            self._records[candidate] = record
            if self._startup_grace_s > 0:
                self._sleep(self._startup_grace_s)
            code = self._poll(record)
            if code is not None:
                self._records.pop(candidate, None)
                self._release_ports(candidate)
                self._stop_bridge(candidate)
                error = (
                    f"emulator for {candidate!r} exited immediately (exit code {code})"
                )
                info = self._persist_state(
                    candidate,
                    state=InstanceState.ERROR,
                    ports=ports,
                    error=error,
                    template_id=record.template_id,
                    image_id=record.image_id,
                    device_id=record.device_id,
                    proxy_id=record.proxy_id,
                )
                self._emit_state(candidate, InstanceState.ERROR, error=error)
                self._sink.emit(
                    EventType.ERROR,
                    {"instance": candidate, "error": error, "exit_code": code},
                    source="platform.runtime",
                )
                raise InstanceError(
                    error,
                    hint="Inspect the emulator log; the AVD image or acceleration may be broken.",
                    details={
                        "exit_code": code,
                        "log_path": str(spec.log_path) if spec.log_path else None,
                        "capacity": decision.max_more,
                    },
                )
            self._supervisor.reset_attempts(candidate)
            self._ensure_supervisor()
            info = self._persist_record(record)
        self._emit_state(
            candidate,
            InstanceState.STARTING,
            pid=info.pid,
            ports=info.ports.model_dump(mode="json") if info.ports else None,
        )
        self._sink.emit(
            EventType.INSTANCE_LOG,
            {
                "instance": candidate,
                "message": f"emulator launched: {spec.redacted_command}",
            },
            source="platform.runtime",
        )
        return info

    def wait_boot(self, name: str, *, timeout_s: float | None = None) -> AvdInfo:
        candidate = self._validate_name(name)
        timeout = (
            float(self.config.boot_timeout_s) if timeout_s is None else max(0.1, float(timeout_s))
        )
        with self._lock:
            record = self._records.get(candidate)
            if record is None:
                info = self._require_instance(candidate)
                persisted = self._state_store.load(candidate)
                external = persisted or info
                if not (
                    external.state.active
                    and external.pid is not None
                    and _pid_alive(external.pid)
                ):
                    raise InstanceError(
                        f"instance {candidate!r} is not running",
                        hint="Start the instance before waiting for boot.",
                    )
                serial = external.adb_serial or boot_serial(external.ports)
                status = self._boot.wait(
                    serial,
                    timeout_s=timeout,
                    is_process_alive=lambda: _pid_alive(external.pid),
                )
                if not status.booted:
                    raise BootTimeoutError(
                        f"instance {candidate!r} did not boot within {timeout:.0f}s",
                        details=status.as_dict(),
                    )
                info = external.model_copy(
                    update={
                        "state": InstanceState.RUNNING,
                        "boot_completed": True,
                        "error": "",
                    }
                )
                self._state_store.save(info)
                self._emit_state(candidate, InstanceState.RUNNING, boot_completed=True)
                return info
            if self._poll(record) is not None:
                self._handle_process_exit(candidate, record.last_exit_code)
                raise InstanceError(
                    f"instance {candidate!r} exited while waiting for boot",
                    hint="Check the emulator log.",
                )
            record.state = InstanceState.BOOTING
            self._persist_record(record)
        self._emit_state(candidate, InstanceState.BOOTING, pid=record.pid)
        status = self._boot.wait(
            record.adb_serial or boot_serial(record.ports),
            timeout_s=timeout,
            is_process_alive=lambda: self._poll(record) is None,
            should_stop=lambda: record.expected_stop,
        )
        if status.booted:
            with self._lock:
                record.boot_completed = True
                record.state = InstanceState.RUNNING
                record.error = ""
                info = self._persist_record(record)
            self._emit_state(
                candidate,
                InstanceState.RUNNING,
                boot_completed=True,
                boot_seconds=round(status.seconds, 3),
            )
            self._sink.emit(
                EventType.INSTANCE_BOOT,
                {
                    "instance": candidate,
                    "boot_seconds": round(status.seconds, 3),
                    "polls": status.polls,
                    "package_manager_ready": status.package_manager_ready,
                },
                source="platform.runtime",
            )
            logger.info("instance %s booted in %.1fs", candidate, status.seconds)
            return info
        # Boot failed: always clean up the process before surfacing the error.
        error = status.last_error or "instance did not finish booting"
        with self._lock:
            record.expected_stop = True
        try:
            self.stop(candidate, timeout_s=min(30.0, timeout), force=True)
        except AvdFleetError as exc:
            logger.warning("could not stop %s after boot timeout: %s", candidate, exc)
        with self._lock:
            info = self._persist_state(
                candidate,
                state=InstanceState.ERROR,
                ports=record.ports,
                error=f"boot failed: {error}",
                template_id=record.template_id,
                image_id=record.image_id,
                device_id=record.device_id,
                proxy_id=record.proxy_id,
            )
        self._emit_state(candidate, InstanceState.ERROR, error=f"boot failed: {error}")
        self._sink.emit(
            EventType.ERROR,
            {"instance": candidate, "error": f"boot failed: {error}"},
            source="platform.runtime",
        )
        raise BootTimeoutError(
            f"instance {candidate!r} did not finish booting within {timeout:.0f}s",
            hint="Check the emulator log, the image, and hypervisor acceleration.",
            details=status.as_dict(),
        )

    # -- stop helpers ---------------------------------------------------
    def _console_kill(self, record: RunningInstance) -> None:
        console: EmulatorConsoleLike | None = None
        try:
            console = self._console_factory()
            console.connect(int(record.ports.console), timeout_s=5.0)
            console.command("kill")
        except Exception as exc:  # noqa: BLE001 - graceful path is best effort
            logger.debug("console kill failed for %s: %s", record.name, redact(str(exc)))
        finally:
            if console is not None:
                try:
                    console.close()
                except Exception:  # noqa: BLE001
                    pass

    def _wait_exit(self, record: RunningInstance, timeout_s: float) -> int | None:
        deadline = self._clock() + max(0.0, float(timeout_s))
        while True:
            code = self._poll(record)
            if code is not None:
                return code
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            self._sleep(min(0.1, max(0.01, remaining)))

    @staticmethod
    def _terminate(record: RunningInstance) -> None:
        try:
            record.process.terminate()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _kill(record: RunningInstance) -> None:
        try:
            record.process.kill()
        except Exception:  # noqa: BLE001
            pass

    def _stop_external(
        self,
        name: str,
        info: AvdInfo,
        *,
        timeout_s: float,
        force: bool,
    ) -> AvdInfo:
        """Stop an emulator discovered from persisted state but not owned by us."""
        serial = info.adb_serial or (boot_serial(info.ports) if info.ports else None)
        if info.ports is not None and not force:
            record = RunningInstance(
                name=name,
                process=_NullProcess(info.pid or 0),
                spec=None,  # type: ignore[arg-type]
                ports=info.ports,
                state=InstanceState.RUNNING,
                started_at=info.started_at or self._now(),
                started_monotonic=self._clock(),
            )
            self._console_kill(record)
        if info.pid is not None and _pid_alive(info.pid):
            if serial:
                try:
                    self._adb.emu(serial, "kill", timeout_s=10.0)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("adb emu kill failed for external %s: %s", name, exc)
            deadline = self._clock() + max(1.0, timeout_s)
            while self._clock() < deadline and _pid_alive(info.pid):
                self._sleep(0.1)
            if _pid_alive(info.pid):
                raise InstanceError(
                    f"could not stop externally-started instance {name!r} (pid {info.pid})",
                    hint="Stop the emulator manually; it was not launched by this daemon.",
                )
        self._release_ports(name)
        self._stop_bridge(name)
        stopped = self._persist_state(
            name,
            state=InstanceState.STOPPED,
            ports=info.ports,
            error="",
            boot_completed=False,
            template_id=info.template_id,
            image_id=info.image_id,
            device_id=info.device_id,
            proxy_id=info.proxy_id,
        )
        self._emit_state(name, InstanceState.STOPPED)
        return stopped

    def stop(
        self,
        name: str,
        *,
        timeout_s: float | None = None,
        force: bool = False,
    ) -> AvdInfo:
        candidate = self._validate_name(name)
        base_info = self._require_instance(candidate)
        timeout = (
            float(self.config.shutdown_timeout_s) if timeout_s is None else max(0.0, float(timeout_s))
        )
        with self._lock:
            record = self._records.get(candidate)
            if record is None:
                persisted = self._state_store.load(candidate) or base_info
                if (
                    persisted.state.active
                    and persisted.pid is not None
                    and _pid_alive(persisted.pid)
                ):
                    return self._stop_external(
                        candidate, persisted, timeout_s=timeout, force=force
                    )
                return self._persist_state(
                    candidate,
                    state=InstanceState.STOPPED,
                    ports=persisted.ports or base_info.ports,
                    error="",
                    template_id=persisted.template_id,
                    image_id=persisted.image_id,
                    device_id=persisted.device_id,
                    proxy_id=persisted.proxy_id,
                )
            if self._poll(record) is not None:
                info = self._handle_process_exit(
                    candidate, record.last_exit_code, expected=True
                )
                if info is not None:
                    return info
                return self._require_instance(candidate)
            record.expected_stop = True
            record.state = InstanceState.STOPPING
            self._persist_record(record)
        self._emit_state(candidate, InstanceState.STOPPING, pid=record.pid)
        if force:
            self._terminate(record)
            self._wait_exit(record, min(5.0, max(1.0, timeout)))
        else:
            self._console_kill(record)
            self._wait_exit(record, timeout)
        if self._poll(record) is None:
            logger.info("emulator %s did not stop gracefully; terminating", candidate)
            self._terminate(record)
            self._wait_exit(record, min(5.0, max(1.0, timeout)))
        if self._poll(record) is None:
            logger.warning("emulator %s ignored SIGTERM; killing", candidate)
            self._kill(record)
            self._wait_exit(record, 5.0)
        exit_code = record.last_exit_code
        if exit_code is None:
            exit_code = self._poll(record)
        info = self._handle_process_exit(candidate, exit_code, expected=True)
        if info is None:
            info = self._persist_state(
                candidate,
                state=InstanceState.STOPPED,
                ports=record.ports,
                error="",
                template_id=record.template_id,
                image_id=record.image_id,
                device_id=record.device_id,
                proxy_id=record.proxy_id,
            )
        self._emit_state(candidate, InstanceState.STOPPED)
        return info

    def restart(self, name: str, *, proxy_id: str | None = None) -> AvdInfo:
        candidate = self._validate_name(name)
        current = self.status(candidate)
        effective_proxy = proxy_id if proxy_id is not None else current.proxy_id
        if current.state.active:
            self.stop(candidate, force=True)
        return self.start(candidate, proxy_id=effective_proxy)

    # ------------------------------------------------------------------
    # Status / listing
    # ------------------------------------------------------------------
    def status(self, name: str) -> AvdInfo:
        candidate = self._validate_name(name)
        self._require_instance(candidate)
        with self._lock:
            record = self._records.get(candidate)
            if record is not None:
                code = self._poll(record)
                if code is not None:
                    self._handle_process_exit(candidate, code)
                    record = None
            if record is not None:
                self._reconcile_visibility(record)
                return self._sync_record(record)
            persisted = self._state_store.load(candidate)
            if persisted is not None and persisted.state.active:
                if persisted.pid is not None and _pid_alive(persisted.pid):
                    return persisted
                stopped = self._persist_state(
                    candidate,
                    state=InstanceState.STOPPED,
                    ports=persisted.ports,
                    error="",
                    boot_completed=False,
                    template_id=persisted.template_id,
                    image_id=persisted.image_id,
                    device_id=persisted.device_id,
                    proxy_id=persisted.proxy_id,
                )
                return stopped
            return persisted or self._require_instance(candidate)

    def list_running(self) -> list[AvdInfo]:
        active: dict[str, AvdInfo] = {}
        with self._lock:
            devices = self._emu_devices()
            for name, record in list(self._records.items()):
                code = self._poll(record)
                if code is not None:
                    self._handle_process_exit(name, code)
                    continue
                self._reconcile_visibility(record, devices)
                try:
                    active[name] = self._sync_record(record)
                except AvdFleetError:
                    continue
            if self._allow_external:
                for name, persisted in self._state_store.load_all().items():
                    if name in active or name in self._records:
                        continue
                    if not persisted.state.active:
                        continue
                    if persisted.pid is not None and _pid_alive(persisted.pid):
                        active[name] = persisted
                    else:
                        self._persist_state(
                            name,
                            state=InstanceState.STOPPED,
                            ports=persisted.ports,
                            error="",
                            template_id=persisted.template_id,
                            image_id=persisted.image_id,
                            device_id=persisted.device_id,
                            proxy_id=persisted.proxy_id,
                        )
        return sorted(active.values(), key=lambda item: item.name)

    def reconcile(self) -> list[AvdInfo]:
        """Reconcile every known instance once and return the active ones."""
        names: set[str] = set(self._records)
        names.update(self._state_store.load_all())
        try:
            names.update(info.name for info in self.avds.list_instances() or [])
        except AvdFleetError:
            pass
        for name in sorted(names):
            try:
                self.status(name)
            except NotFoundError:
                continue
        return self.list_running()

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------
    def _snapshot_target(self, name: str) -> SnapshotTarget:
        info = self.status(name)
        record = self._records.get(name)
        managed_alive = record is not None and self._poll(record) is None
        running = bool(
            info.state.active
            and (managed_alive or (info.pid is not None and _pid_alive(info.pid)))
        )
        avd_dir: Path | None = None
        getter = getattr(self.avds, "avd_dir", None)
        if callable(getter):
            try:
                avd_dir = Path(getter(name))
            except AvdFleetError:
                avd_dir = None
        return SnapshotTarget(
            name=name,
            console_port=info.ports.console if info.ports else None,
            adb_serial=info.adb_serial or (boot_serial(info.ports) if info.ports else None),
            avd_dir=avd_dir,
            running=running,
        )

    def save_snapshot(self, name: str, snapshot: str, *, golden: bool = False) -> Snapshot:
        candidate = self._validate_name(name)
        return self._snapshots.save(
            self._snapshot_target(candidate), snapshot, golden=golden
        )

    def load_snapshot(self, name: str, snapshot: str) -> Snapshot:
        candidate = self._validate_name(name)
        return self._snapshots.load(self._snapshot_target(candidate), snapshot)

    def delete_snapshot(self, name: str, snapshot: str) -> None:
        candidate = self._validate_name(name)
        self._snapshots.delete(self._snapshot_target(candidate), snapshot)

    def list_snapshots(self, name: str) -> list[Snapshot]:
        candidate = self._validate_name(name)
        return self._snapshots.list(self._snapshot_target(candidate))

    # ------------------------------------------------------------------
    # Supervision / cleanup
    # ------------------------------------------------------------------
    def _supervised_processes(self) -> list[SupervisedProcess]:
        entries: list[SupervisedProcess] = []
        for name, record in list(self._records.items()):
            def _poll(current: RunningInstance = record) -> int | None:
                return current.process.poll()

            def _on_exit(
                code: int | None,
                instance_name: str = name,
                current: RunningInstance = record,
            ) -> None:
                self._handle_process_exit(
                    instance_name, code, expected=current.expected_stop
                )

            entries.append(
                SupervisedProcess(
                    name=name,
                    poll=_poll,
                    on_exit=_on_exit,
                    label="emulator",
                    expected=record.expected_stop,
                )
            )
        return entries

    def _restart_from_supervisor(self, name: str) -> None:
        persisted = self._state_store.load(name)
        proxy_id = persisted.proxy_id if persisted is not None else None
        logger.info("supervisor restarting instance %s", name)
        try:
            self.start(name, proxy_id=proxy_id)
        except AvdFleetError as exc:
            error = f"supervisor restart failed: {exc}"
            logger.warning("instance %s: %s", name, error)
            self._persist_state(name, state=InstanceState.ERROR, error=error)

    def _ensure_supervisor(self) -> None:
        if self._auto_supervision and not self._supervisor.running:
            self._supervisor.start()

    def start_supervisor(self) -> None:
        self._supervisor.start()

    def stop_supervisor(self, *, timeout_s: float = 10.0) -> None:
        self._supervisor.stop(timeout_s=timeout_s)

    @property
    def supervisor_running(self) -> bool:
        return self._supervisor.running

    def supervisor_stats(self) -> dict[str, Any]:
        return self._supervisor.stats

    def supervisor_history(self) -> list[ExitRecord]:
        return self._supervisor.history()

    def cleanup(self, *, all_instances: bool = False) -> OperationResult:
        stopped: list[str] = []
        failed: list[dict[str, str]] = []
        with self._lock:
            names = set(self._records)
        if self._allow_external:
            names.update(
                name
                for name, info in self._state_store.load_all().items()
                if info.state.active
            )
        for name in sorted(names):
            try:
                self.stop(name, force=False)
                stopped.append(name)
            except AvdFleetError as exc:
                failed.append({"instance": name, "error": redact(str(exc))})
        external_killed: list[str] = []
        if all_instances:
            devices = self._emu_devices() or {}
            for serial in sorted(devices):
                if not serial.startswith("emulator-"):
                    continue
                try:
                    result = self._adb.emu(serial, "kill", timeout_s=10.0)
                    if getattr(result, "ok", True):
                        external_killed.append(serial)
                except Exception as exc:  # noqa: BLE001
                    failed.append({"instance": serial, "error": redact(str(exc))})
        ok = not failed
        self._sink.emit(
            EventType.SYSTEM,
            {"action": "runtime.cleanup", "stopped": stopped, "failed": failed},
            source="platform.runtime",
        )
        return OperationResult(
            ok=ok,
            message=(
                f"stopped {len(stopped)} instance(s)"
                + (f", {len(failed)} failure(s)" if failed else "")
            ),
            data={
                "stopped": stopped,
                "failed": failed,
                "killed_external": external_killed,
            },
        )

    # ------------------------------------------------------------------
    def close(self) -> None:
        """Stop background supervision (emulator processes are left running)."""
        self._supervisor.stop(timeout_s=5.0)

    def __enter__(self) -> RuntimeManagerService:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


class _NullProcess:
    """Minimal process handle used to reuse console helpers for external emulators."""

    def __init__(self, pid: int = 0) -> None:
        self.pid = int(pid)

    def poll(self) -> int | None:
        return None if _pid_alive(self.pid) else -1

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None

    def send_signal(self, sig: int) -> None:
        return None


__all__ = [
    "RUNTIME_STATE_FILENAME",
    "RunningInstance",
    "RuntimeManagerService",
    "RuntimeStateStore",
]
