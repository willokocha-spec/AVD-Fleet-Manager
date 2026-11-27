"""Per-session port planning for Appium 4.

UiAutomator2 needs four distinct kinds of ports for parallel sessions: the
Appium server itself, ``appium:systemPort`` (host port forwarded to the
on-device UiAutomator2 server), ``appium:mjpegServerPort`` (host port forwarded
to the device MJPEG server) and, for hybrid/webview sessions, a chromedriver
port.  The first three are planned here; they must be unique across the fleet
and ``systemPort`` must differ from ``mjpegServerPort``.

Allocation is delegated to an injected allocator when possible.  The expected
allocator is :class:`avdfleet.core.ports.PortAllocator` (shared with the runtime
manager so emulator console and Appium side-channel ports never collide); any
object with ``get``/``lease``/``release``/``leased``/``reserve`` methods of the
same shape works.  Without an allocator the planner probes the host and keeps
its own thread-safe ledger.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from ..core.config import Config
from ..core.errors import AppiumError
from ..core.models import InstancePorts
from ..core.ports import find_free_port, is_port_free

__all__ = ["AppiumPortPlanner", "AppiumPorts"]

DEFAULT_APPIUM_PORT_START = 4723
DEFAULT_HOST = "127.0.0.1"
_BLOCK_SPAN = 300
_MAX_PORT = 65535


@dataclass(frozen=True)
class AppiumPorts:
    """Ports reserved for one instance's Appium session."""

    server: int
    system_port: int
    mjpeg_port: int

    def as_dict(self) -> dict[str, int]:
        return {"server": self.server, "system_port": self.system_port, "mjpeg_port": self.mjpeg_port}

    @property
    def all_ports(self) -> tuple[int, int, int]:
        return (self.server, self.system_port, self.mjpeg_port)

    def validate(self) -> None:
        for port in self.all_ports:
            if not 1 <= int(port) <= _MAX_PORT:
                raise AppiumError(f"invalid Appium port: {port!r}")
        if len(set(self.all_ports)) != 3:
            raise AppiumError(
                "Appium server, system and MJPEG ports must be distinct",
                details={"ports": self.as_dict()},
            )


class AppiumPortPlanner:
    """Allocate unique Appium server/system/MJPEG ports per fleet instance."""

    def __init__(
        self,
        allocator: Any | None = None,
        *,
        appium_start: int = DEFAULT_APPIUM_PORT_START,
        host: str = DEFAULT_HOST,
        span: int = _BLOCK_SPAN,
    ) -> None:
        if not 1024 <= int(appium_start) <= _MAX_PORT - 3 * span:
            raise ValueError("appium_start is outside the usable port range")
        self._allocator = allocator
        self._appium_start = int(appium_start)
        self._host = host
        self._span = int(span)
        self._lock = threading.RLock()
        self._plans: dict[str, AppiumPorts] = {}
        self._reserved: set[int] = set()

    @classmethod
    def from_config(cls, config: Config, allocator: Any | None = None) -> AppiumPortPlanner:
        host = config.api_host if config.api_host in {"127.0.0.1", "localhost"} else DEFAULT_HOST
        return cls(allocator, appium_start=config.appium_port_start, host=host)

    # ------------------------------------------------------------------
    # Per-instance planning
    # ------------------------------------------------------------------
    def plan(self, instance: str, *, ports: InstancePorts | None = None) -> AppiumPorts:
        """Return (and remember) the Appium ports for *instance*.

        ``ports`` may be the runtime's already-leased
        :class:`~avdfleet.core.models.InstancePorts`; missing values are
        allocated.  Repeated calls for the same instance are idempotent.
        """
        name = str(instance or "").strip()
        if not name:
            raise AppiumError("instance name is required to plan Appium ports")
        with self._lock:
            cached = self._plans.get(name)
            if cached is not None:
                return cached
            base: Any = ports
            missing = not all(
                _field(base, field) for field in ("appium_server", "system_port", "mjpeg")
            )
            if self._allocator is not None and missing:
                leased = self._lease_from_allocator(name)
                leased_ports = _coerce_ports(leased)
                if leased_ports is not None:
                    if base is None:
                        leased_ports.validate()
                        self._plans[name] = leased_ports
                        return leased_ports
                    base = _merge_ports(base, leased_ports)
            used = self._used_ports()
            server = _field(base, "appium_server") or self._allocate_block(0, used)
            used.add(server)
            system = _field(base, "system_port") or self._allocate_block(1, used)
            used.add(system)
            mjpeg = _field(base, "mjpeg") or self._allocate_block(2, used)
            planned = AppiumPorts(server=int(server), system_port=int(system), mjpeg_port=int(mjpeg))
            planned.validate()
            self._plans[name] = planned
            self._reserved.update(planned.all_ports)
            return planned

    def get(self, instance: str) -> AppiumPorts | None:
        with self._lock:
            return self._plans.get(instance)

    def predicted(self) -> dict[str, AppiumPorts]:
        with self._lock:
            return dict(self._plans)

    def release(self, instance: str) -> None:
        with self._lock:
            self._plans.pop(instance, None)
        release = getattr(self._allocator, "release", None)
        if callable(release):
            try:
                release(instance)
            except Exception:  # pragma: no cover - allocator specific
                pass

    def sessions(self) -> int:
        with self._lock:
            return len(self._plans)

    # ------------------------------------------------------------------
    # Shared server port
    # ------------------------------------------------------------------
    def allocate_server(self, preferred: int | None = None) -> int:
        """Reserve a port for the locally managed Appium server.

        Raises :class:`AppiumError` when a preferred port is already in use, per
        the lifecycle contract ("error on port busy").
        """
        with self._lock:
            if preferred is not None:
                port = int(preferred)
                if not 1 <= port <= _MAX_PORT:
                    raise AppiumError(f"invalid Appium server port: {port}")
                if port in self._used_ports() or not is_port_free(port, self._host):
                    raise AppiumError(
                        f"port {port} is already in use",
                        hint="choose another port or stop the process that owns it",
                    )
                self._reserve_port(port)
                return port
            fallback = find_free_port(
                self._appium_start,
                min(self._appium_start + self._span - 1, _MAX_PORT),
                host=self._host,
                used=self._used_ports(),
            )
            if fallback is None:  # pragma: no cover - exhausted range
                raise AppiumError(
                    "no free port available for the Appium server",
                    hint=f"widen the Appium port range starting at {self._appium_start}",
                )
            self._reserve_port(fallback)
            return fallback

    def release_server(self, port: int | None) -> None:
        if port is None:
            return
        with self._lock:
            self._reserved.discard(int(port))
        release_reserved = getattr(self._allocator, "release_reserved", None)
        if callable(release_reserved):
            try:
                release_reserved(int(port))
            except Exception:  # pragma: no cover - allocator specific
                pass

    def reserve(self, *ports: int) -> None:
        with self._lock:
            for port in ports:
                self._reserved.add(int(port))

    def release_reserved(self, *ports: int) -> None:
        with self._lock:
            for port in ports:
                self._reserved.discard(int(port))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _reserve_port(self, port: int) -> None:
        self._reserved.add(port)
        reserve = getattr(self._allocator, "reserve", None)
        if callable(reserve):
            try:
                reserve(port)
            except Exception:  # pragma: no cover - allocator specific
                pass

    def _lease_from_allocator(self, instance: str) -> Any | None:
        getter = getattr(self._allocator, "get", None)
        if callable(getter):
            try:
                existing = getter(instance)
            except Exception:  # pragma: no cover - allocator specific
                existing = None
            if existing is not None and _coerce_ports(existing) is not None:
                return existing
        lease = getattr(self._allocator, "lease", None)
        if callable(lease):
            try:
                return lease(instance)
            except AppiumError:
                raise
            except Exception as exc:  # pragma: no cover - allocator specific
                raise AppiumError(f"port allocator failed for {instance!r}: {exc}") from exc
        return None

    def _used_ports(self) -> set[int]:
        used = set(self._reserved)
        for planned in self._plans.values():
            used.update(planned.all_ports)
        used.update(self._allocator_leased_ports())
        return used

    def _allocator_leased_ports(self) -> set[int]:
        leased = getattr(self._allocator, "leased", None)
        if not callable(leased):
            return set()
        try:
            mapping = leased()
        except Exception:  # pragma: no cover - allocator specific
            return set()
        ports: set[int] = set()
        for value in mapping.values() if isinstance(mapping, dict) else []:
            planned = _coerce_ports(value)
            if planned is not None:
                ports.update(planned.all_ports)
            else:
                ports.update(_port_values(value))
        return ports

    def _allocate_block(self, block: int, used: set[int]) -> int:
        start = self._appium_start + block * self._span
        end = min(start + self._span - 1, _MAX_PORT)
        port = find_free_port(start, end, host=self._host, used=used)
        if port is None:  # pragma: no cover - exhausted range
            raise AppiumError(
                "no free port available for an Appium side-channel",
                hint=f"widen the Appium port range starting at {self._appium_start}",
            )
        return port


def _coerce_ports(value: Any) -> AppiumPorts | None:
    if isinstance(value, AppiumPorts):
        return value
    if value is None:
        return None
    if isinstance(value, InstancePorts):
        server = value.appium_server
        system = value.system_port
        mjpeg = value.mjpeg
    else:
        server = getattr(value, "appium_server", getattr(value, "server", None))
        system = getattr(value, "system_port", None)
        mjpeg = getattr(value, "mjpeg", getattr(value, "mjpeg_port", getattr(value, "mjpeg_server_port", None)))
    if not (server and system and mjpeg):
        return None
    return AppiumPorts(server=int(server), system_port=int(system), mjpeg_port=int(mjpeg))


def _merge_ports(base: Any, leased: AppiumPorts) -> dict[str, int]:
    merged: dict[str, int] = {}
    for name in ("appium_server", "system_port", "mjpeg"):
        value = _field(base, name) or _field(leased, name)
        if value:
            merged[name] = value
    return merged


def _field(ports: Any, name: str) -> int | None:
    if ports is None:
        return None
    value = getattr(ports, name, None)
    if value is None and isinstance(ports, dict):
        value = ports.get(name)
    try:
        port = int(value) if value is not None else None
    except (TypeError, ValueError):
        port = None
    if port is None or not 1 <= port <= _MAX_PORT:
        return None
    return port


def _port_values(value: Any) -> set[int]:
    ports: set[int] = set()
    if value is None:
        return ports
    candidates: Any
    if isinstance(value, dict):
        candidates = value.values()
    elif isinstance(value, (list, tuple, set)):
        candidates = value
    else:
        candidates = (
            getattr(value, "appium_server", None),
            getattr(value, "system_port", None),
            getattr(value, "mjpeg", None),
            getattr(value, "console", None),
            getattr(value, "adb", None),
            getattr(value, "grpc", None),
        )
    for candidate in candidates:
        try:
            if candidate is not None and 1 <= int(candidate) <= _MAX_PORT:
                ports.add(int(candidate))
        except (TypeError, ValueError):
            continue
    return ports
