"""Port discovery and allocation for emulator instances.

The Android emulator uses paired ports: console = even port, adb = console + 1.
AVDFleet additionally allocates gRPC, Appium server, UiAutomator2 system port and
MJPEG ports per instance.  Allocation is guarded by a process-wide lock and
probes the host before handing out a port.
"""

from __future__ import annotations

import os
import socket
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field

from .errors import CapacityError
from .models import InstancePorts


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    if not 1 <= port <= 65535:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # A recently closed listener can leave accepted connections in
        # TIME_WAIT on the same local port.  POSIX servers (uvicorn, Appium,
        # the emulator) set SO_REUSEADDR themselves, so treat such a port as
        # reusable instead of reporting it busy for a minute after stop().
        # Do not set it on Windows: there SO_REUSEADDR can let a probe bind
        # over a live listener and would hide a genuinely busy port.
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def find_free_port(
    start: int,
    end: int,
    *,
    host: str = "127.0.0.1",
    avoid: Iterable[int] = (),
    used: Iterable[int] = (),
) -> int | None:
    blocked = set(avoid) | set(used)
    for port in range(start, end + 1):
        if port in blocked:
            continue
        if is_port_free(port, host):
            return port
    return None


def find_free_even_port(
    start: int,
    end: int,
    *,
    host: str = "127.0.0.1",
    avoid: Iterable[int] = (),
    used: Iterable[int] = (),
) -> int | None:
    blocked = set(avoid) | set(used)
    for port in range(start, end + 1):
        if port % 2:
            continue
        if port in blocked or port + 1 in blocked:
            continue
        if is_port_free(port, host) and is_port_free(port + 1, host):
            return port
    return None


@dataclass
class PortAllocator:
    start: int = 5554
    end: int = 5680
    appium_start: int = 4723
    bridge_start: int = 7800
    host: str = "127.0.0.1"
    _leases: dict[str, InstancePorts] = field(default_factory=dict)
    _reserved: set[int] = field(default_factory=set)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        if self.start % 2:
            self.start += 1
        if self.end <= self.start:
            raise ValueError("invalid emulator port range")

    # ------------------------------------------------------------------
    def lease(self, instance: str, *, preferred_console: int | None = None) -> InstancePorts:
        with self._lock:
            if instance in self._leases:
                return self._leases[instance]
            used = self._reserved | self._all_leased_ports()
            console = None
            if (
                preferred_console is not None
                and preferred_console % 2 == 0
                and self._pair_free(preferred_console, used)
            ):
                console = preferred_console
            if console is None:
                console = find_free_even_port(self.start, self.end, host=self.host, used=used)
            if console is None:
                raise CapacityError(
                    "No free emulator port pair available",
                    hint=f"Stop an instance or widen the console port range ({self.start}-{self.end}).",
                )
            ports = InstancePorts(console=console, adb=console + 1)
            ports.grpc = self._allocate_one(self._grpc_range()[0], self._grpc_range()[1], used | {ports.console, ports.adb})
            ports.appium_server = self._allocate_one(
                self.appium_start, self.appium_start + 300, used | self._ports_set(ports)
            )
            ports.system_port = self._allocate_one(
                self.appium_start + 301, self.appium_start + 600, used | self._ports_set(ports)
            )
            ports.mjpeg = self._allocate_one(
                self.appium_start + 601, self.appium_start + 900, used | self._ports_set(ports)
            )
            self._leases[instance] = ports
            self._reserved.update(self._ports_set(ports))
            return ports

    def release(self, instance: str) -> None:
        with self._lock:
            ports = self._leases.pop(instance, None)
            if ports:
                for port in self._ports_set(ports):
                    self._reserved.discard(port)

    def get(self, instance: str) -> InstancePorts | None:
        with self._lock:
            return self._leases.get(instance)

    def leased(self) -> dict[str, InstancePorts]:
        with self._lock:
            return dict(self._leases)

    def reserve(self, *ports: int) -> None:
        with self._lock:
            self._reserved.update(ports)

    def release_reserved(self, *ports: int) -> None:
        with self._lock:
            for port in ports:
                self._reserved.discard(port)

    def clear(self) -> None:
        with self._lock:
            self._leases.clear()
            self._reserved.clear()

    def capacity(self) -> dict[str, int]:
        with self._lock:
            limit = len(range(self.start, self.end + 1, 2))
            return {"limit": limit, "in_use": len(self._leases), "free": limit - len(self._leases)}

    # ------------------------------------------------------------------
    def _grpc_range(self) -> tuple[int, int]:
        # gRPC ports live outside the appium block, after the console range.
        return self.end + 2, self.end + 500

    def _allocate_one(self, start: int, end: int, used: set[int]) -> int:
        while True:
            candidate = find_free_port(start, end, host=self.host, used=used | self._reserved)
            if candidate is None:
                raise CapacityError(
                    "No free port available for an emulator side-channel",
                    hint=f"Port range exhausted in {start}-{end}.",
                )
            if candidate not in self._reserved and candidate not in used:
                return candidate
            start = candidate + 1

    def _all_leased_ports(self) -> set[int]:
        result: set[int] = set()
        for ports in self._leases.values():
            result.update(self._ports_set(ports))
        return result

    def _pair_free(self, console: int, used: set[int]) -> bool:
        return console not in used and console + 1 not in used and is_port_free(console, self.host) and is_port_free(console + 1, self.host)

    @staticmethod
    def _ports_set(ports: InstancePorts) -> set[int]:
        return {port for port in ports.model_dump().values() if isinstance(port, int)}


__all__ = [
    "PortAllocator",
    "find_free_port",
    "find_free_even_port",
    "is_port_free",
    "InstancePorts",
]
