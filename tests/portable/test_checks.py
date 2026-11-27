"""Preflight check tests: filesystem, noexec, quarantine, resources, hypervisor.

All probes are injected, so no real FAT32 volume, noexec mount, hypervisor or
quarantine attribute is required.  One Windows-only test exercises the real
alternate-data-stream probe when the host supports it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from avdfleet.core.models import CheckStatus
from avdfleet.portable.checks import (
    CheckReport,
    CheckResult,
    FileSystemInfo,
    FileSystemKind,
    HypervisorInfo,
    detect_filesystem,
    filesystem_check,
    free_space_check,
    hypervisor_check,
    noexec_check,
    quarantine_check,
    run_preflight,
    write_speed_check,
)
from avdfleet.portable.checks.filesystem import (
    MountTableEntry,
    find_mount,
    normalize_filesystem,
    parse_proc_mounts,
)
from avdfleet.portable.checks.hypervisor import detect_hypervisor
from avdfleet.portable.checks.quarantine import (
    has_mark_of_the_web,
    scan_macos_quarantine,
    scan_mark_of_the_web,
)
from avdfleet.portable.checks.resources import GIB, measure_write_speed

try:  # tests are a package in the repo layout
    from .conftest import make_file, populate_runtime_dirs
except ImportError:  # pragma: no cover - collected without package __init__
    from conftest import make_file, populate_runtime_dirs  # type: ignore[no-redef]

pytestmark = pytest.mark.portable


# ---------------------------------------------------------------------------
# Filesystem detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ntfs", FileSystemKind.NTFS),
        ("NTFS3", FileSystemKind.NTFS),
        ("exfat", FileSystemKind.EXFAT),
        ("exFAT-fuse", FileSystemKind.EXFAT),
        ("vfat", FileSystemKind.FAT32),
        ("msdos", FileSystemKind.FAT32),
        ("ext4", FileSystemKind.EXT),
        ("ext2", FileSystemKind.EXT),
        ("apfs", FileSystemKind.APFS),
        ("hfsplus", FileSystemKind.HFS),
        ("btrfs", FileSystemKind.BTRFS),
        ("xfs", FileSystemKind.XFS),
        ("zfs", FileSystemKind.ZFS),
        ("tmpfs", FileSystemKind.TMPFS),
        ("overlay", FileSystemKind.OVERLAY),
        ("cifs", FileSystemKind.NETWORK),
        ("smb3", FileSystemKind.NETWORK),
        ("nfs4", FileSystemKind.NETWORK),
        ("fuse.sshfs", FileSystemKind.NETWORK),
        ("somefs", FileSystemKind.UNKNOWN),
        (None, FileSystemKind.UNKNOWN),
    ],
)
def test_normalize_filesystem(raw: str | None, expected: FileSystemKind) -> None:
    assert normalize_filesystem(raw) is expected


def test_normalize_filesystem_remote_drive() -> None:
    assert normalize_filesystem("NTFS", drive_type="4") is FileSystemKind.NETWORK


def test_parse_proc_mounts_decodes_escapes() -> None:
    text = r"/dev/sda1 /mnt/with\040space ext4 rw,relatime 0 0" + "\n"
    entries = parse_proc_mounts(text)
    assert len(entries) == 1
    assert entries[0].mount_point == "/mnt/with space"
    assert entries[0].fstype == "ext4"
    assert "rw" in entries[0].options


def test_find_mount_uses_longest_prefix() -> None:
    entries = [
        MountTableEntry(mount_point="/", fstype="ext4"),
        MountTableEntry(mount_point="/data", fstype="exfat"),
    ]
    assert find_mount(entries, "/data/bundle").fstype == "exfat"  # type: ignore[union-attr]
    assert find_mount(entries, "/home/user").fstype == "ext4"  # type: ignore[union-attr]


def test_detect_linux_from_proc_mounts(tmp_path: Path) -> None:
    mount = tmp_path.as_posix()
    mounts = f"/dev/sda1 {mount} ext4 rw,relatime 0 0\n/dev/sda2 / otherfs rw 0 0\n"
    info = detect_filesystem(tmp_path / "sdk", host_os="linux", mounts_text=mounts)
    assert info.kind is FileSystemKind.EXT
    assert info.source == "proc-mounts"


def test_detect_windows_volume_injection(tmp_path: Path) -> None:
    info = detect_filesystem(tmp_path, host_os="windows", volume_info=lambda path: ("NTFS", "3"))
    assert info.kind is FileSystemKind.NTFS
    assert info.source == "windows-volume"
    remote = detect_filesystem(tmp_path, host_os="windows", volume_info=lambda path: ("", "4"))
    assert remote.kind is FileSystemKind.NETWORK
    assert remote.network is True


def test_detect_macos_fstype_injection(tmp_path: Path) -> None:
    info = detect_filesystem(tmp_path, host_os="macos", fstypename=lambda path: "apfs")
    assert info.kind is FileSystemKind.APFS
    assert info.source == "statvfs"


def test_detect_psutil_fallback(tmp_path: Path) -> None:
    partitions = [MountTableEntry(mount_point=str(tmp_path), fstype="btrfs")]
    info = detect_filesystem(
        tmp_path, host_os="linux", mounts_text="", partitions=partitions
    )
    assert info.kind is FileSystemKind.BTRFS
    assert info.source == "psutil"


@pytest.mark.parametrize("kind", [FileSystemKind.FAT32, FileSystemKind.EXFAT, FileSystemKind.NTFS])
def test_filesystem_check_statuses(kind: FileSystemKind, tmp_path: Path) -> None:
    result = filesystem_check(tmp_path, info=FileSystemInfo(kind=kind, raw=kind.value))
    if kind is FileSystemKind.FAT32:
        assert result.status is CheckStatus.FAIL
        assert result.critical is True
        assert "FAT32" in result.value or "FAT32" in result.detail
    elif kind is FileSystemKind.EXFAT:
        assert result.status is CheckStatus.WARN
        assert "exFAT" in result.detail or "exFAT" in (result.value or "")
    else:
        assert result.status is CheckStatus.OK


def test_filesystem_check_network_and_unknown_warn(tmp_path: Path) -> None:
    network = filesystem_check(tmp_path, info=FileSystemInfo(kind=FileSystemKind.NETWORK, network=True))
    assert network.status is CheckStatus.WARN
    unknown = filesystem_check(tmp_path, info=FileSystemInfo(kind=FileSystemKind.UNKNOWN, raw="weird"))
    assert unknown.status is CheckStatus.WARN


# ---------------------------------------------------------------------------
# noexec
# ---------------------------------------------------------------------------


def test_noexec_skipped_on_windows(tmp_path: Path) -> None:
    result = noexec_check(tmp_path, host_os="windows")
    assert result.status is CheckStatus.SKIP


def test_noexec_mount_flag_fails(tmp_path: Path) -> None:
    populate_runtime_dirs(tmp_path)
    mounts = f"/dev/sda1 {tmp_path.as_posix()} ext4 rw,noexec 0 0\n"
    result = noexec_check(
        tmp_path,
        host_os="linux",
        mounts_text=mounts,
        exec_probe=None,
        access=lambda path, mode: True,
    )
    assert result.status is CheckStatus.FAIL
    assert result.critical is True
    assert result.value == "noexec"


def test_noexec_missing_execute_bits_fails(tmp_path: Path) -> None:
    populate_runtime_dirs(tmp_path)
    result = noexec_check(
        tmp_path,
        host_os="linux",
        mounts_text="",
        partitions=[],
        access=lambda path, mode: False,
    )
    assert result.status is CheckStatus.FAIL
    assert result.critical is True
    assert "executable" in result.detail


def test_noexec_injected_exec_probe_ok(tmp_path: Path) -> None:
    result = noexec_check(tmp_path, host_os="linux", exec_probe=lambda root: True)
    assert result.status is CheckStatus.OK


# ---------------------------------------------------------------------------
# Quarantine / Mark-of-the-Web
# ---------------------------------------------------------------------------


def test_quarantine_windows_motw_detected(tmp_path: Path) -> None:
    populate_runtime_dirs(tmp_path)
    make_file(tmp_path / "bin" / "tool.exe", "MZ")
    lister = lambda path: ["Zone.Identifier"] if path.name == "tool.exe" else []  # noqa: E731
    result = quarantine_check(tmp_path, host_os="windows", motw_probe=lister)
    assert result.status is CheckStatus.WARN
    assert "Unblock-File" in result.detail
    scan = scan_mark_of_the_web(tmp_path, stream_lister=lister)
    assert scan.findings and scan.findings[0].path == "bin/tool.exe"


def test_quarantine_windows_clean(tmp_path: Path) -> None:
    populate_runtime_dirs(tmp_path)
    result = quarantine_check(tmp_path, host_os="windows", motw_probe=lambda path: [])
    assert result.status is CheckStatus.OK
    assert result.value == "0"


def test_quarantine_macos_detected(tmp_path: Path) -> None:
    populate_runtime_dirs(tmp_path)
    make_file(tmp_path / "python" / "bin" / "python3", "#!/bin/sh\n")
    reader = lambda path: b"0081;deadbeef" if path.name == "python3" else None  # noqa: E731
    result = quarantine_check(tmp_path, host_os="macos", xattr_probe=reader)
    assert result.status is CheckStatus.WARN
    assert "xattr -dr com.apple.quarantine" in result.detail
    scan = scan_macos_quarantine(tmp_path, xattr_reader=reader)
    assert scan.instructions and "xattr -dr" in scan.instructions[0]


def test_quarantine_skipped_on_linux(tmp_path: Path) -> None:
    result = quarantine_check(tmp_path, host_os="linux")
    assert result.status is CheckStatus.SKIP


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS alternate data streams are Windows-only")
def test_real_mark_of_the_web_ads(tmp_path: Path) -> None:
    target = make_file(tmp_path / "python.exe", "MZ")
    assert has_mark_of_the_web(target) is False
    with open(str(target) + ":Zone.Identifier", "w", encoding="utf-8") as stream:
        stream.write("[ZoneTransfer]\nZoneId=3\n")
    assert has_mark_of_the_web(target) is True


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


def test_free_space_thresholds(tmp_path: Path) -> None:
    def usage(free_gib: float):
        total = 500 * GIB
        return (total, total - int(free_gib * GIB), int(free_gib * GIB))

    ok = free_space_check(tmp_path, disk_usage=lambda path: usage(50), writable_probe=lambda path: True)
    assert ok.status is CheckStatus.OK
    warn = free_space_check(tmp_path, disk_usage=lambda path: usage(5), writable_probe=lambda path: True)
    assert warn.status is CheckStatus.WARN
    fail = free_space_check(tmp_path, disk_usage=lambda path: usage(1), writable_probe=lambda path: True)
    assert fail.status is CheckStatus.FAIL and fail.critical is True


def test_free_space_unwritable(tmp_path: Path) -> None:
    result = free_space_check(
        tmp_path, disk_usage=lambda path: (100 * GIB, 0, 100 * GIB), writable_probe=lambda path: False
    )
    assert result.status is CheckStatus.FAIL
    assert result.value == "read-only"
    assert result.critical is True


def test_free_space_stat_error(tmp_path: Path) -> None:
    def broken(path: Path) -> tuple[int, int, int]:
        raise OSError("no such volume")

    result = free_space_check(tmp_path, disk_usage=broken)
    assert result.status is CheckStatus.FAIL
    assert result.critical is False


@pytest.mark.parametrize(("speed", "expected"), [(60.0, CheckStatus.OK), (10.0, CheckStatus.WARN), (2.0, CheckStatus.FAIL)])
def test_write_speed_thresholds(speed: float, expected: CheckStatus, tmp_path: Path) -> None:
    result = write_speed_check(tmp_path, probe=lambda root, size: speed)
    assert result.status is expected
    assert result.value == f"{speed:.1f} MB/s"


def test_write_speed_probe_error(tmp_path: Path) -> None:
    def broken(root: Path, size: int) -> float:
        raise OSError("read-only volume")

    result = write_speed_check(tmp_path, probe=broken)
    assert result.status is CheckStatus.FAIL


def test_write_speed_missing_root(tmp_path: Path) -> None:
    result = write_speed_check(tmp_path / "missing", probe=lambda root, size: 50.0)
    assert result.status is CheckStatus.SKIP


def test_measure_write_speed_real(tmp_path: Path) -> None:
    speed = measure_write_speed(tmp_path, size_bytes=32 * 1024)
    assert speed > 0
    leftovers = list((tmp_path / "tmp").glob(".avdfleet-write-speed-*"))
    assert leftovers == []


# ---------------------------------------------------------------------------
# Hypervisor
# ---------------------------------------------------------------------------


def test_hypervisor_windows_present() -> None:
    runner = lambda argv: (0, "firmware=True\nhypervisor=True")  # noqa: E731
    info = detect_hypervisor(host_os="windows", runner=runner)
    assert info.kind == "whpx"
    assert info.available is True


def test_hypervisor_windows_firmware_only() -> None:
    runner = lambda argv: (0, "firmware=True\nhypervisor=False")  # noqa: E731
    info = detect_hypervisor(host_os="windows", runner=runner)
    assert info.available is False
    assert info.requires_reboot is True


def test_hypervisor_windows_disabled() -> None:
    runner = lambda argv: (0, "firmware=False\nhypervisor=False")  # noqa: E731
    info = detect_hypervisor(host_os="windows", runner=runner)
    assert info.kind == "none"
    assert info.available is False


def test_hypervisor_windows_query_failure() -> None:
    info = detect_hypervisor(host_os="windows", runner=lambda argv: (1, ""))
    assert info.available is False
    assert info.kind == "whpx"


def test_hypervisor_linux_missing_kvm(tmp_path: Path) -> None:
    info = detect_hypervisor(
        host_os="linux", kvm_device=tmp_path / "kvm", cpuinfo="processor : 0\nflags : sse vmx\n"
    )
    assert info.kind == "kvm"
    assert info.available is False
    assert "/dev/kvm" in info.detail


def test_hypervisor_linux_kvm_device(tmp_path: Path) -> None:
    device = make_file(tmp_path / "kvm", "")
    info = detect_hypervisor(host_os="linux", kvm_device=device)
    assert info.available is True
    assert info.kind == "kvm"


def test_hypervisor_linux_no_virtualization(tmp_path: Path) -> None:
    info = detect_hypervisor(
        host_os="linux", kvm_device=tmp_path / "kvm", cpuinfo="flags : sse sse2\n"
    )
    assert info.kind == "none"
    assert info.nested is False


def test_hypervisor_macos_sysctl() -> None:
    info = detect_hypervisor(host_os="macos", runner=lambda argv: (0, "1"))
    assert info.kind == "hvf" and info.available is True
    disabled = detect_hypervisor(host_os="macos", runner=lambda argv: (0, "0"))
    assert disabled.available is False


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        (HypervisorInfo(kind="kvm", available=True, detail="kvm ok"), CheckStatus.OK),
        (HypervisorInfo(kind="kvm", available=False, detail="no kvm"), CheckStatus.WARN),
        (HypervisorInfo(kind="unknown", available=False, detail="?"), CheckStatus.SKIP),
    ],
)
def test_hypervisor_check_statuses(info: HypervisorInfo, expected: CheckStatus) -> None:
    result = hypervisor_check(host_os="linux", provider=lambda: info)
    assert result.status is expected
    if expected is CheckStatus.WARN:
        assert "kvm" in result.detail.lower()


def test_hypervisor_check_provider_error() -> None:
    def broken() -> HypervisorInfo:
        raise RuntimeError("probe exploded")

    result = hypervisor_check(host_os="linux", provider=broken)
    assert result.status is CheckStatus.SKIP


# ---------------------------------------------------------------------------
# Report composition
# ---------------------------------------------------------------------------


def _injected_preflight(root: Path) -> CheckReport:
    return run_preflight(
        root,
        host_os="linux",
        filesystem_probe=lambda path: FileSystemInfo(
            kind=FileSystemKind.EXT, raw="ext4", mount_point="/", source="test"
        ),
        mounts_text="/dev/sda1 / ext4 rw 0 0\n",
        exec_probe=lambda path: True,
        motw_probe=lambda path: [],
        xattr_probe=lambda path: None,
        disk_usage=lambda path: (500 * GIB, 100 * GIB, 400 * GIB),
        writable_probe=lambda path: True,
        write_probe=lambda path, size: 80.0,
        hypervisor_provider=lambda: HypervisorInfo(kind="kvm", available=True, detail="ok"),
    )


def test_run_preflight_composition(tmp_path: Path) -> None:
    report = _injected_preflight(tmp_path)
    assert [check.id for check in report.checks] == [
        "filesystem",
        "filesystem.noexec",
        "quarantine",
        "resources.free_space",
        "resources.write_speed",
        "hypervisor",
    ]
    assert report.ok is True
    assert report.refuse is False
    assert report.get("filesystem").status is CheckStatus.OK  # type: ignore[union-attr]
    payload = report.to_dict()
    assert set(payload) == {"ok", "refuse", "checks", "summary"}
    for check in payload["checks"]:
        assert set(check) == {"id", "title", "status", "detail", "value"}
        assert check["status"] in {status.value for status in CheckStatus}


def test_run_preflight_deterministic(tmp_path: Path) -> None:
    first = _injected_preflight(tmp_path).to_dict()
    second = _injected_preflight(tmp_path).to_dict()
    assert first == second


def test_run_preflight_fat32_refuses(tmp_path: Path) -> None:
    report = run_preflight(
        tmp_path,
        host_os="linux",
        filesystem_probe=lambda path: FileSystemInfo(kind=FileSystemKind.FAT32, raw="vfat"),
        mounts_text="/dev/sda1 / ext4 rw 0 0\n",
        exec_probe=lambda path: True,
        disk_usage=lambda path: (500 * GIB, 100 * GIB, 400 * GIB),
        writable_probe=lambda path: True,
        write_probe=lambda path, size: 80.0,
        include_quarantine=False,
        include_hypervisor=False,
    )
    assert report.refuse is True
    assert report.ok is False
    assert report.failures[0].id == "filesystem"


def test_report_helpers() -> None:
    report = CheckReport(
        checks=(
            CheckResult("a", "A", CheckStatus.OK),
            CheckResult("b", "B", CheckStatus.WARN, detail="careful"),
            CheckResult("c", "C", CheckStatus.FAIL, detail="bad", critical=True),
            CheckResult("d", "D", CheckStatus.SKIP),
        )
    )
    assert report.ok is False
    assert report.refuse is True
    assert [warning.id for warning in report.warnings] == ["b"]
    assert [failure.id for failure in report.failures] == ["c"]
    assert report.status_counts()["ok"] == 1
    assert "1 ok" in report.summary()
    assert report.get("missing") is None
