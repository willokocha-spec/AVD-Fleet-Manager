"""Portable preflight checks.

Every check is a pure function with dependency injection (probes, providers,
mount tables, clocks) so tests can exercise all branches without a real device,
FAT32 volume or hypervisor.  Checks never mutate the filesystem except for the
small write-speed benchmark, which creates and removes one temporary file inside
``<root>/tmp``.

The report payload mirrors the ``CheckResult`` schema in ``contracts/openapi.yaml``
(``id/title/status/detail/value``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from avdfleet.core.models import CheckStatus
from avdfleet.portable.env.model import host_os_name


@dataclass(frozen=True)
class CheckResult:
    """One preflight result (API/CLI serialisable)."""

    id: str
    title: str
    status: CheckStatus
    detail: str = ""
    value: str | None = None
    critical: bool = field(default=False, compare=False)

    @property
    def ok(self) -> bool:
        return self.status is CheckStatus.OK

    @property
    def failed(self) -> bool:
        return self.status is CheckStatus.FAIL

    @property
    def warning(self) -> bool:
        return self.status is CheckStatus.WARN

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status.value,
            "detail": self.detail,
            "value": self.value,
        }


@dataclass(frozen=True)
class CheckReport:
    """Ordered bundle of preflight results with a refusal decision."""

    checks: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return not any(check.failed for check in self.checks)

    @property
    def refuse(self) -> bool:
        """True when a critical check failed (FAT32, noexec, unwritable, no space)."""
        return any(check.failed and check.critical for check in self.checks)

    @property
    def failures(self) -> list[CheckResult]:
        return [check for check in self.checks if check.failed]

    @property
    def warnings(self) -> list[CheckResult]:
        return [check for check in self.checks if check.warning]

    def get(self, check_id: str) -> CheckResult | None:
        for check in self.checks:
            if check.id == check_id:
                return check
        return None

    def status_counts(self) -> dict[str, int]:
        counts = {status.value: 0 for status in CheckStatus}
        for check in self.checks:
            counts[check.status.value] += 1
        return counts

    def summary(self) -> str:
        counts = self.status_counts()
        return ", ".join(f"{count} {status}" for status, count in counts.items() if count)

    def to_api(self) -> list[dict[str, Any]]:
        return [check.to_dict() for check in self.checks]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "refuse": self.refuse,
            "checks": self.to_api(),
            "summary": self.summary(),
        }


# Submodules import ``CheckResult`` from this package, so they are imported
# after the dataclasses above are defined.
from .filesystem import (  # noqa: E402
    FileSystemInfo,
    FileSystemKind,
    detect_filesystem,
    filesystem_check,
    noexec_check,
)
from .hypervisor import (  # noqa: E402
    HypervisorInfo,
    detect_hypervisor,
    hypervisor_check,
)
from .quarantine import (  # noqa: E402
    QuarantineFinding,
    QuarantineScan,
    quarantine_check,
)
from .resources import (  # noqa: E402
    DEFAULT_SPEED_BYTES,
    FAIL_FREE_GIB,
    MIN_FREE_GIB,
    WRITE_SPEED_OK_MBPS,
    WRITE_SPEED_WARN_MBPS,
    free_space_check,
    write_speed_check,
)


def run_preflight(
    root: str | Path,
    *,
    host_os: str | None = None,
    filesystem_probe: Any = None,
    mounts_text: str | None = None,
    exec_probe: Any = None,
    motw_probe: Any = None,
    xattr_probe: Any = None,
    disk_usage: Any = None,
    writable_probe: Any = None,
    write_probe: Any = None,
    speed_size_bytes: int = DEFAULT_SPEED_BYTES,
    include_write_speed: bool = True,
    include_quarantine: bool = True,
    include_hypervisor: bool = True,
    hypervisor_provider: Any = None,
    hypervisor_runner: Any = None,
) -> CheckReport:
    """Run every portable preflight check in a deterministic order.

    All probes are injectable callables (see the individual check modules); the
    defaults read the real host.  ``include_write_speed`` is False for fast
    launcher preflights, and the slower hypervisor probe is only run when asked.
    """
    paths = Path(root)
    host = host_os or host_os_name()
    checks: list[CheckResult] = [
        filesystem_check(paths, host_os=host, probe=filesystem_probe),
        noexec_check(paths, host_os=host, mounts_text=mounts_text, exec_probe=exec_probe),
    ]
    if include_quarantine:
        checks.append(
            quarantine_check(paths, host_os=host, motw_probe=motw_probe, xattr_probe=xattr_probe)
        )
    checks.append(free_space_check(paths, disk_usage=disk_usage, writable_probe=writable_probe))
    if include_write_speed:
        checks.append(write_speed_check(paths, size_bytes=speed_size_bytes, probe=write_probe))
    if include_hypervisor:
        checks.append(
            hypervisor_check(host_os=host, provider=hypervisor_provider, runner=hypervisor_runner)
        )
    return CheckReport(checks=tuple(checks))


__all__ = [
    "DEFAULT_SPEED_BYTES",
    "FAIL_FREE_GIB",
    "MIN_FREE_GIB",
    "WRITE_SPEED_OK_MBPS",
    "WRITE_SPEED_WARN_MBPS",
    "CheckReport",
    "CheckResult",
    "FileSystemInfo",
    "FileSystemKind",
    "HypervisorInfo",
    "QuarantineFinding",
    "QuarantineScan",
    "detect_filesystem",
    "detect_hypervisor",
    "filesystem_check",
    "free_space_check",
    "hypervisor_check",
    "noexec_check",
    "quarantine_check",
    "run_preflight",
    "write_speed_check",
]
