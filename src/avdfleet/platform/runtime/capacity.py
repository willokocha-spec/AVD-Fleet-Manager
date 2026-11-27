"""Host capacity accounting for starting emulator instances.

Android emulators reserve their entire configured RAM up front and each instance
needs a paired console/adb port, a slice of CPU and tens of gigabytes of disk
over its lifetime.  This module answers three questions without ever
overcommitting memory:

* ``status()`` -- the dict the REST ``/capacity`` route expects;
* ``can_start(ram_mb)`` -- would one more instance fit (RAM, CPU, disk, ports,
  ``max_instances``)?
* ``ensure_can_start(ram_mb)`` -- the same, raising a redacted
  :class:`~avdfleet.core.errors.CapacityError` with a remediation hint.

``psutil`` is used when available (it is a declared dependency), with a small
stdlib fallback so unit tests and portable bundles still produce numbers.
Memory/disk/CPU providers are injectable, which is how the unit tests exercise
rejection paths deterministically.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from avdfleet.core.config import Config
from avdfleet.core.errors import CapacityError
from avdfleet.core.logging import get_logger
from avdfleet.core.models import AvdInfo
from avdfleet.core.paths import Paths
from avdfleet.core.ports import PortAllocator

logger = get_logger("platform.runtime.capacity")

BYTES_PER_MB = 1024 * 1024
MIN_DISK_FREE_MB = 2048
MIN_RAM_PER_INSTANCE_MB = 512


# ---------------------------------------------------------------------------
# Injectable host probes
# ---------------------------------------------------------------------------


def _default_virtual_memory() -> Any:
    try:
        import psutil  # type: ignore[import-not-found]

        return psutil.virtual_memory()
    except Exception:  # noqa: BLE001 - portable fallback
        total = _fallback_total_bytes()
        return type(
            "Memory",
            (),
            {"total": total, "available": int(total * 0.5), "percent": 50.0},
        )()


def _fallback_total_bytes() -> int:
    sysconf = getattr(os, "sysconf", None)
    if sysconf is None:  # pragma: no cover - Windows without psutil
        return 8 * 1024 * 1024 * 1024
    try:
        pages = sysconf("SC_PHYS_PAGES")
        page_size = sysconf("SC_PAGE_SIZE")
        return int(pages) * int(page_size)
    except (ValueError, OSError):
        return 8 * 1024 * 1024 * 1024


def _default_disk_usage(path: Path) -> Any:
    target = path
    while not target.exists() and target.parent != target:
        target = target.parent
    return shutil.disk_usage(target)


def _default_cpu_count() -> int:
    value = os.cpu_count()
    return int(value) if value else 1


# ---------------------------------------------------------------------------
# Decision object
# ---------------------------------------------------------------------------


@dataclass
class CapacityDecision:
    """The result of one capacity check for starting instance(s)."""

    ok: bool = True
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ram_available_mb: int = 0
    ram_required_mb: int = 0
    cpu_available: int = 0
    ports_available: int = 0
    disk_available_mb: int = 0
    max_more: int = 0

    def ensure(self) -> None:
        """Raise :class:`CapacityError` when the decision is negative."""
        if not self.ok:
            raise CapacityError(
                "insufficient host capacity to start another emulator: "
                + "; ".join(self.reasons),
                hint="Stop an instance, lower the instance RAM, or free host resources.",
                details={
                    "reasons": list(self.reasons),
                    "ram_available_mb": self.ram_available_mb,
                    "ram_required_mb": self.ram_required_mb,
                    "ports_available": self.ports_available,
                    "disk_available_mb": self.disk_available_mb,
                },
            )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@dataclass
class CapacityService:
    """Compute fleet capacity from configuration + live host state."""

    config: Config
    paths: Paths | None = None
    port_allocator: PortAllocator | None = None
    instances: Callable[[], Sequence[AvdInfo]] | None = None
    virtual_memory: Callable[[], Any] | None = None
    disk_usage: Callable[[Path], Any] | None = None
    cpu_count: Callable[[], int] | None = None

    def __post_init__(self) -> None:
        if self.virtual_memory is None:
            self.virtual_memory = _default_virtual_memory
        if self.cpu_count is None:
            self.cpu_count = _default_cpu_count

    # -- probes ---------------------------------------------------------
    def _memory_mb(self) -> tuple[int, int]:
        probe = self.virtual_memory or _default_virtual_memory
        try:
            memory = probe()
            total = int(getattr(memory, "total", 0) or 0) // BYTES_PER_MB
            available = int(getattr(memory, "available", 0) or 0) // BYTES_PER_MB
        except Exception as exc:  # noqa: BLE001 - never fail the API on a probe
            logger.warning("virtual memory probe failed: %s", exc)
            total, available = 0, 0
        if total <= 0:
            total = _fallback_total_bytes() // BYTES_PER_MB
        if available <= 0:
            available = int(total * 0.5)
        return total, available

    def _disk_mb(self) -> int:
        target = Path(self.paths.data_dir) if self.paths and self.paths.data_dir else Path.cwd()
        probe = self.disk_usage or _default_disk_usage
        try:
            usage = probe(target)
            return int(getattr(usage, "free", 0) or 0) // BYTES_PER_MB
        except Exception as exc:  # noqa: BLE001
            logger.warning("disk usage probe failed: %s", exc)
            return 0

    def _cpus(self) -> int:
        try:
            return max(1, int((self.cpu_count or _default_cpu_count)()))
        except Exception:  # noqa: BLE001
            return 1

    def _all_instances(self) -> list[AvdInfo]:
        if self.instances is None:
            return []
        try:
            return list(self.instances() or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("instance capacity provider failed: %s", exc)
            return []

    def _running_instances(self, infos: Sequence[AvdInfo] | None = None) -> list[AvdInfo]:
        candidates = self._all_instances() if infos is None else list(infos)
        return [info for info in candidates if getattr(info.state, "active", False)]

    def _instance_ram_mb(self, info: AvdInfo) -> int:
        raw = (info.config or {}).get("hw.ramSize")
        try:
            value = int(str(raw))
        except (TypeError, ValueError):
            value = int(self.config.default_ram_mb)
        return max(MIN_RAM_PER_INSTANCE_MB, value)

    def _instance_cores(self, info: AvdInfo) -> int:
        raw = (info.config or {}).get("hw.cpu.ncore")
        try:
            value = int(str(raw))
        except (TypeError, ValueError):
            value = int(self.config.default_cpu_cores)
        return max(1, value)

    def _ports_capacity(self) -> dict[str, Any]:
        if self.port_allocator is None:
            return {}
        try:
            return dict(self.port_allocator.capacity())
        except Exception:  # noqa: BLE001
            return {}

    def _disk_per_instance_mb(self, ram_mb: int) -> int:
        return max(2048, ram_mb * 2 + 3072)

    # -- public ---------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """Capacity snapshot matching the REST ``CapacityInfo`` schema."""
        total_mb, available_mb = self._memory_mb()
        reserve_mb = max(0, int(self.config.capacity_reserve_mb))
        usable_mb = max(0, available_mb - reserve_mb)
        all_instances = self._all_instances()
        running = self._running_instances(all_instances)
        running_ram = sum(self._instance_ram_mb(info) for info in running)
        running_cores = sum(self._instance_cores(info) for info in running)
        cpus = self._cpus()
        port_info = self._ports_capacity()
        port_free = int(port_info.get("free", 0) or 0)
        default_ram = max(MIN_RAM_PER_INSTANCE_MB, int(self.config.default_ram_mb))
        default_cores = max(1, int(self.config.default_cpu_cores))
        disk_free = self._disk_mb()
        deployed = len(all_instances)

        max_by_instances = max(0, int(self.config.max_instances) - len(running))
        max_by_ram = max(0, usable_mb // default_ram) if usable_mb else 0
        cpu_slots = max(0, cpus // default_cores)
        max_by_cpu = max(0, cpu_slots - len(running))
        max_by_ports = port_free if port_info else int(self.port_allocator_capacity_limit())
        disk_estimate = self._disk_per_instance_mb(default_ram)
        max_by_disk = max(0, (disk_free - MIN_DISK_FREE_MB) // max(1, disk_estimate))

        warnings: list[str] = []
        if reserve_mb and available_mb - reserve_mb < default_ram:
            warnings.append(
                f"available RAM ({available_mb} MB) barely covers the {reserve_mb} MB host reserve"
            )
        if running_cores > cpus:
            warnings.append(
                f"instances request {running_cores} CPU cores but the host has {cpus}; "
                "emulators will contend for CPU"
            )
        elif running_cores >= max(1, cpus - default_cores):
            warnings.append("CPU is close to fully committed; more instances will reduce frame rates")
        if port_free and port_free <= 4:
            warnings.append(
                f"only {port_free} emulator port pair(s) left; widen port_range_start/port_range_end"
            )
        if disk_free and disk_free < MIN_DISK_FREE_MB + disk_estimate:
            warnings.append(
                f"low disk space: {disk_free} MB free, an instance needs about {disk_estimate} MB"
            )
        if max_by_ram < max_by_instances:
            warnings.append(
                f"RAM limits the fleet to about {len(running) + max_by_ram} running instance(s)"
            )

        return {
            "max_instances": int(self.config.max_instances),
            "running": len(running),
            "deployed": deployed,
            "ram_total_mb": total_mb,
            "ram_available_mb": usable_mb,
            "ram_required_mb": running_ram,
            "ram_reserve_mb": reserve_mb,
            "cpu_cores": cpus,
            "cpu_cores_required": running_cores,
            "dimensions": {
                "instances": max_by_instances,
                "ram": max_by_ram,
                "cpu": max_by_cpu,
                "ports": max_by_ports,
                "disk": max_by_disk,
            },
            "can_start_more": max(0, min(max_by_instances, max_by_ram, max_by_cpu, max_by_ports)),
            "ports": {
                **port_info,
                "start": int(self.config.port_range_start),
                "end": int(self.config.port_range_end),
                "appium_start": int(self.config.appium_port_start),
            },
            "warnings": warnings,
        }

    def port_allocator_capacity_limit(self) -> int:
        """Static upper bound of concurrent emulator port pairs."""
        try:
            start = int(self.config.port_range_start)
            end = int(self.config.port_range_end)
            return max(0, len(range(start, end + 1, 2)))
        except (TypeError, ValueError):
            return 0

    def can_start(self, ram_mb: int, *, count: int = 1) -> CapacityDecision:
        """Decide whether *count* more instances needing ``ram_mb`` each can start."""
        try:
            per_instance = max(MIN_RAM_PER_INSTANCE_MB, int(ram_mb))
        except (TypeError, ValueError):
            per_instance = max(MIN_RAM_PER_INSTANCE_MB, int(self.config.default_ram_mb))
        requested = per_instance * max(1, int(count))
        total_mb, available_mb = self._memory_mb()
        reserve_mb = max(0, int(self.config.capacity_reserve_mb))
        usable_mb = max(0, available_mb - reserve_mb)
        running = self._running_instances()
        # Never reserve more than the host can actually back with free pages.
        ram_required = requested
        reasons: list[str] = []
        warnings: list[str] = []
        if ram_required > usable_mb:
            reasons.append(
                f"needs {ram_required} MB RAM but only {usable_mb} MB is available "
                f"after the {reserve_mb} MB host reserve"
            )
        elif usable_mb - ram_required < per_instance:
            warnings.append(
                f"only {max(0, usable_mb - ram_required)} MB RAM would remain after starting"
            )
        if len(running) + max(1, int(count)) > int(self.config.max_instances):
            reasons.append(
                f"max_instances={int(self.config.max_instances)} would be exceeded "
                f"({len(running)} already running)"
            )
        cpus = self._cpus()
        cores = max(1, int(self.config.default_cpu_cores))
        cpu_slots = max(0, cpus // cores)
        if len(running) + max(1, int(count)) > cpu_slots:
            reasons.append(
                f"cpu slots exhausted: {len(running)} running, {cpu_slots} supported by {cpus} CPUs"
            )
        port_info = self._ports_capacity()
        port_free = int(port_info.get("free", 0) or 0)
        if port_info and port_free < max(1, int(count)):
            reasons.append(f"no free emulator port pair (only {port_free} left)")
        disk_free = self._disk_mb()
        disk_needed = self._disk_per_instance_mb(per_instance) * max(1, int(count))
        if disk_free and disk_free < disk_needed + MIN_DISK_FREE_MB:
            reasons.append(
                f"needs about {disk_needed} MB disk, only {disk_free} MB free"
            )
        elif disk_free and disk_free < disk_needed * 3:
            warnings.append("disk space is getting low for further deploys")
        max_by_instances = int(self.config.max_instances) - len(running)
        max_by_ram = usable_mb // per_instance if per_instance else 0
        max_by_cpu = cpu_slots - len(running)
        max_by_ports = port_free if port_info else self.port_allocator_capacity_limit()
        return CapacityDecision(
            ok=not reasons,
            reasons=reasons,
            warnings=warnings,
            ram_available_mb=usable_mb,
            ram_required_mb=ram_required,
            cpu_available=cpu_slots,
            ports_available=port_free,
            disk_available_mb=disk_free,
            max_more=max(0, min(max_by_instances, max_by_ram, max_by_cpu, max_by_ports)),
        )

    def ensure_can_start(self, ram_mb: int, *, count: int = 1) -> CapacityDecision:
        """Like :meth:`can_start` but raises :class:`CapacityError` when blocked."""
        decision = self.can_start(ram_mb, count=count)
        for warning in decision.warnings:
            logger.warning("capacity: %s", warning)
        decision.ensure()
        return decision


__all__ = [
    "BYTES_PER_MB",
    "CapacityDecision",
    "CapacityService",
    "MIN_DISK_FREE_MB",
    "MIN_RAM_PER_INSTANCE_MB",
]
