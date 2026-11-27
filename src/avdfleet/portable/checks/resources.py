"""Free-space and write-speed checks for portable bundles.

Android system images, AVD disk images and snapshots need a lot of space and a
reasonably fast volume: a cheap USB 2.0 stick at ~5 MB/s makes first boot take
hours.  The write benchmark creates one small temporary file inside the bundle
and deletes it again; nothing else is written.
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from avdfleet.core.models import CheckStatus

from . import CheckResult

__all__ = [
    "DEFAULT_SPEED_BYTES",
    "FAIL_FREE_GIB",
    "GIB",
    "MIN_FREE_GIB",
    "WRITE_SPEED_OK_MBPS",
    "WRITE_SPEED_WARN_MBPS",
    "free_space_check",
    "measure_write_speed",
    "write_speed_check",
]

GIB = 1024**3
#: Below this no space check warns (system image + AVD disks are multi-GB).
MIN_FREE_GIB = 10.0
#: Below this the bundle cannot realistically hold a system image: refuse to start.
FAIL_FREE_GIB = 2.0
#: Write benchmark size (kept small: preflight runs on every launch).
DEFAULT_SPEED_BYTES = 2 * 1024 * 1024
#: Sustained write speed thresholds in MB/s (decimal megabytes).
WRITE_SPEED_OK_MBPS = 20.0
WRITE_SPEED_WARN_MBPS = 5.0


@dataclass(frozen=True)
class DiskUsage:
    total: int
    used: int
    free: int


def _as_usage(value: Any) -> DiskUsage:
    """Accept ``shutil.disk_usage`` tuples or anything with total/used/free."""
    if hasattr(value, "free") and hasattr(value, "total"):
        return DiskUsage(total=int(value.total), used=int(getattr(value, "used", 0)), free=int(value.free))
    total, used, free = (int(part) for part in value)
    return DiskUsage(total=total, used=used, free=free)


def free_space_check(
    root: str | Path,
    *,
    min_gib: float = MIN_FREE_GIB,
    fail_gib: float = FAIL_FREE_GIB,
    disk_usage: Callable[[Path], Any] | None = None,
    writable_probe: Callable[[Path], bool] | None = None,
) -> CheckResult:
    """Check free space and that the bundle root is writable."""
    target = Path(root)
    usage_source = disk_usage or shutil.disk_usage
    try:
        usage = _as_usage(usage_source(target))
    except OSError as exc:
        return CheckResult(
            id="resources.free_space",
            title="Free space",
            status=CheckStatus.FAIL,
            detail=f"Cannot read free space for {target}: {exc}",
            critical=False,
        )
    checker = writable_probe or (lambda path: os.access(path, os.W_OK))
    try:
        writable = bool(checker(target))
    except OSError:
        writable = False
    free_gib = usage.free / GIB
    value = f"{free_gib:.1f} GiB free"
    if not writable:
        return CheckResult(
            id="resources.free_space",
            title="Free space",
            status=CheckStatus.FAIL,
            detail=(
                "The bundle root is not writable; the portable edition must write data, config, "
                "logs, AVDs and snapshots inside its own root."
            ),
            value="read-only",
            critical=True,
        )
    if free_gib < fail_gib:
        return CheckResult(
            id="resources.free_space",
            title="Free space",
            status=CheckStatus.FAIL,
            detail=(
                f"Only {free_gib:.1f} GiB free on the bundle volume; at least {fail_gib:.0f} GiB "
                "is required to download/unpack a system image and create AVD disks."
            ),
            value=value,
            critical=True,
        )
    if free_gib < min_gib:
        return CheckResult(
            id="resources.free_space",
            title="Free space",
            status=CheckStatus.WARN,
            detail=(
                f"{free_gib:.1f} GiB free; {min_gib:.0f} GiB or more is recommended for system "
                "images, AVD disks and snapshots."
            ),
            value=value,
        )
    return CheckResult(
        id="resources.free_space",
        title="Free space",
        status=CheckStatus.OK,
        detail=f"{free_gib:.1f} GiB free of {usage.total / GIB:.1f} GiB on the bundle volume.",
        value=value,
    )


def measure_write_speed(root: str | Path, size_bytes: int = DEFAULT_SPEED_BYTES) -> float:
    """Write, fsync and remove a temporary file; return sustained MB/s."""
    target = Path(root)
    directory = target / "tmp"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        directory = target
    probe_file = directory / f".avdfleet-write-speed-{uuid.uuid4().hex}.tmp"
    payload = bytes(size_bytes)
    started = time.perf_counter()
    try:
        with open(probe_file, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        elapsed = time.perf_counter() - started
    finally:
        try:
            probe_file.unlink()
        except OSError:
            pass
    if elapsed <= 0:  # pragma: no cover - timer resolution guard
        return 0.0
    return (size_bytes / 1_000_000) / elapsed


def write_speed_check(
    root: str | Path,
    *,
    size_bytes: int = DEFAULT_SPEED_BYTES,
    probe: Callable[[Path, int], float] | None = None,
    ok_mbps: float = WRITE_SPEED_OK_MBPS,
    warn_mbps: float = WRITE_SPEED_WARN_MBPS,
) -> CheckResult:
    """Benchmark (or accept an injected measurement of) small-file write speed."""
    target = Path(root)
    if not target.is_dir():
        return CheckResult(
            id="resources.write_speed",
            title="Write speed",
            status=CheckStatus.SKIP,
            detail=f"Bundle root does not exist yet: {target}",
        )
    try:
        mbps = float(probe(target, size_bytes) if probe is not None else measure_write_speed(target, size_bytes))
    except OSError as exc:
        return CheckResult(
            id="resources.write_speed",
            title="Write speed",
            status=CheckStatus.FAIL,
            detail=f"Write benchmark failed: {exc}",
            critical=False,
        )
    label = f"{size_bytes / (1024 * 1024):.1f} MiB"
    value = f"{mbps:.1f} MB/s"
    if mbps < warn_mbps:
        return CheckResult(
            id="resources.write_speed",
            title="Write speed",
            status=CheckStatus.FAIL,
            detail=(
                f"Sustained write speed is {mbps:.1f} MB/s on a {label} file (below "
                f"{warn_mbps:.0f} MB/s); first boot and snapshots will be extremely slow."
            ),
            value=value,
            critical=False,
        )
    if mbps < ok_mbps:
        return CheckResult(
            id="resources.write_speed",
            title="Write speed",
            status=CheckStatus.WARN,
            detail=(
                f"Sustained write speed is {mbps:.1f} MB/s on a {label} file; "
                f"{ok_mbps:.0f} MB/s or more is recommended for emulator I/O."
            ),
            value=value,
        )
    return CheckResult(
        id="resources.write_speed",
        title="Write speed",
        status=CheckStatus.OK,
        detail=f"Sustained write speed is {mbps:.1f} MB/s on a {label} file.",
        value=value,
    )
