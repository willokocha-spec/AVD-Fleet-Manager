"""Filesystem-type and noexec-mount checks for the portable bundle.

Detection order per host:

* Windows: ``GetVolumeInformationW`` + ``GetDriveTypeW`` via ctypes.
* macOS: ``os.statvfs().f_fstypename`` then ``stat -f %T``.
* Linux: ``/proc/mounts`` (longest mount-point match) then ``stat -f -c %T``.
* Any host: psutil's mount table as a last fallback.

Everything is injectable for tests: pass ``volume_info``/``fstypename``/
``mounts_text``/``partitions``/``probe`` instead of touching the real host.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from avdfleet.core.models import CheckStatus
from avdfleet.portable.env.model import PortablePaths, host_os_name

from . import CheckResult


class FileSystemKind(StrEnum):
    NTFS = "ntfs"
    EXFAT = "exfat"
    FAT32 = "fat32"
    APFS = "apfs"
    HFS = "hfs"
    EXT = "ext"
    XFS = "xfs"
    BTRFS = "btrfs"
    ZFS = "zfs"
    TMPFS = "tmpfs"
    OVERLAY = "overlay"
    NETWORK = "network"
    UNKNOWN = "unknown"


_LABELS = {
    FileSystemKind.NTFS: "NTFS",
    FileSystemKind.EXFAT: "exFAT",
    FileSystemKind.FAT32: "FAT32",
    FileSystemKind.APFS: "APFS",
    FileSystemKind.HFS: "HFS+",
    FileSystemKind.EXT: "ext",
    FileSystemKind.XFS: "XFS",
    FileSystemKind.BTRFS: "Btrfs",
    FileSystemKind.ZFS: "ZFS",
    FileSystemKind.TMPFS: "tmpfs",
    FileSystemKind.OVERLAY: "overlay",
    FileSystemKind.NETWORK: "network",
    FileSystemKind.UNKNOWN: "unknown",
}

_DOS_KINDS = {FileSystemKind.NTFS, FileSystemKind.EXFAT, FileSystemKind.FAT32}
_UNIX_KINDS = {
    FileSystemKind.APFS,
    FileSystemKind.HFS,
    FileSystemKind.EXT,
    FileSystemKind.XFS,
    FileSystemKind.BTRFS,
    FileSystemKind.ZFS,
    FileSystemKind.TMPFS,
    FileSystemKind.OVERLAY,
}
_NETWORK_NAMES = {
    "cifs",
    "smbfs",
    "smb3",
    "smb2",
    "nfs",
    "nfs4",
    "afpfs",
    "webdav",
    "davfs",
    "sshfs",
    "9p",
    "fuse.sshfs",
    "fuse.rclone",
    "fuse.smbnetfs",
    "fuse.davfs2",
}


def _label(kind: FileSystemKind) -> str:
    return _LABELS.get(kind, kind.value)


def normalize_filesystem(raw: str | None, *, drive_type: str | None = None) -> FileSystemKind:
    """Map a native filesystem name to :class:`FileSystemKind`."""
    if drive_type == "4":  # DRIVE_REMOTE
        return FileSystemKind.NETWORK
    name = (raw or "").strip().lower()
    if not name:
        return FileSystemKind.UNKNOWN
    if name in _NETWORK_NAMES or name.startswith("fuse.sshfs") or name.startswith("fuse.rclone"):
        return FileSystemKind.NETWORK
    if name.startswith("fuse.") and any(token in name for token in ("smb", "dav", "ssh", "nfs")):
        return FileSystemKind.NETWORK
    if name in {"ntfs", "ntfs3"}:
        return FileSystemKind.NTFS
    if name in {"exfat", "exfat-fuse"}:
        return FileSystemKind.EXFAT
    if name in {"vfat", "fat", "fat32", "fat16", "fat12", "msdos", "msdosfs", "umsdos"}:
        return FileSystemKind.FAT32
    if name in {"apfs"}:
        return FileSystemKind.APFS
    if name in {"hfs", "hfsplus", "hfs+", "hfsx"}:
        return FileSystemKind.HFS
    if re.fullmatch(r"ext[234]?", name):
        return FileSystemKind.EXT
    if name == "xfs":
        return FileSystemKind.XFS
    if name in {"btrfs", "btrfs2"}:
        return FileSystemKind.BTRFS
    if name in {"zfs", "zfs-fuse"}:
        return FileSystemKind.ZFS
    if name == "tmpfs":
        return FileSystemKind.TMPFS
    if name == "overlay":
        return FileSystemKind.OVERLAY
    return FileSystemKind.UNKNOWN


@dataclass(frozen=True)
class FileSystemInfo:
    kind: FileSystemKind
    raw: str = ""
    mount_point: str = ""
    source: str = ""
    network: bool = False

    @property
    def label(self) -> str:
        return _label(self.kind)

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "kind": self.kind.value,
            "label": self.label,
            "raw": self.raw,
            "mount_point": self.mount_point,
            "source": self.source,
            "network": self.network,
        }


@dataclass(frozen=True)
class MountTableEntry:
    """One mount table row (parsed from /proc/mounts or provided by psutil)."""

    mount_point: str
    fstype: str = ""
    source: str = ""
    options: tuple[str, ...] = ()


_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


def _decode_mount_field(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 8))

    return _ESCAPE_RE.sub(replace, value)


def parse_proc_mounts(text: str) -> list[MountTableEntry]:
    """Parse ``/proc/mounts`` content into mount-table entries."""
    entries: list[MountTableEntry] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 4:
            continue
        source, mount_point, fstype, opts = fields[:4]
        entries.append(
            MountTableEntry(
                mount_point=_decode_mount_field(mount_point),
                fstype=_decode_mount_field(fstype),
                source=_decode_mount_field(source),
                options=tuple(option for option in opts.split(",") if option),
            )
        )
    return entries


def find_mount(entries: Iterable[MountTableEntry], path: str | Path) -> MountTableEntry | None:
    """Return the longest mount entry containing *path* (case-aware)."""
    target = os.path.normpath(os.path.realpath(str(path)))
    best: MountTableEntry | None = None
    best_len = -1
    for entry in entries:
        mount = os.path.normpath(os.path.realpath(entry.mount_point))
        candidate = os.path.normpath(target)
        if os.name == "nt":
            mount, candidate = mount.lower(), candidate.lower()
        if (
            candidate == mount or candidate.startswith(mount.rstrip(os.sep) + os.sep)
        ) and len(mount) > best_len:
            best = entry
            best_len = len(mount)
    return best


def _read_proc_mounts() -> str | None:
    try:
        return Path("/proc/mounts").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _default_runner(argv: list[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode, output.strip()


def _psutil_entries() -> list[MountTableEntry]:
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a declared dependency
        return []
    entries: list[MountTableEntry] = []
    try:
        partitions = psutil.disk_partitions(all=True)
    except (OSError, RuntimeError):  # pragma: no cover - platform dependent
        return []
    for partition in partitions:
        options = tuple(
            option for option in (partition.opts or "").replace(" ", ",").split(",") if option
        )
        entries.append(
            MountTableEntry(
                mount_point=partition.mountpoint,
                fstype=partition.fstype,
                source=partition.device,
                options=options,
            )
        )
    return entries


def _windows_volume_info(path: Path) -> tuple[str, str]:
    """Return ``(filesystem_name, drive_type)`` from the Windows API."""
    if sys.platform != "win32":  # pragma: no cover - guarded by caller
        raise OSError("GetVolumeInformation is only available on Windows")
    import ctypes
    from ctypes import wintypes

    anchor = Path(path.expanduser().resolve()).anchor or "C:\\"
    if anchor.startswith("\\\\"):  # UNC path: a network share
        return "", "4"
    root = anchor if anchor.endswith("\\") else anchor + "\\"
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    volume_name = ctypes.create_unicode_buffer(261)
    fs_name = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()
    max_component = wintypes.DWORD()
    flags = wintypes.DWORD()
    ok = kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root),
        volume_name,
        261,
        ctypes.byref(serial),
        ctypes.byref(max_component),
        ctypes.byref(flags),
        fs_name,
        261,
    )
    drive_type = str(kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)))
    if not ok:
        raise OSError(f"GetVolumeInformationW failed for {root}")
    return fs_name.value, drive_type


def _macos_fstype(path: Path) -> str:
    statvfs = getattr(os, "statvfs", None)  # not available on Windows builds
    if statvfs is None:  # pragma: no cover - macOS only
        return ""
    try:
        stats = statvfs(path)
    except OSError:
        return ""
    return str(getattr(stats, "f_fstypename", "") or "")


def _info_from_parts(
    raw: str,
    *,
    source: str,
    mount_point: str = "",
    drive_type: str | None = None,
) -> FileSystemInfo:
    kind = normalize_filesystem(raw, drive_type=drive_type)
    return FileSystemInfo(
        kind=kind,
        raw=raw,
        mount_point=mount_point,
        source=source,
        network=kind is FileSystemKind.NETWORK,
    )


def detect_filesystem(
    path: str | Path,
    *,
    host_os: str | None = None,
    probe: Callable[[Path], FileSystemInfo] | None = None,
    mounts_text: str | None = None,
    partitions: Iterable[MountTableEntry] | None = None,
    volume_info: Callable[[Path], tuple[str, str]] | None = None,
    fstypename: Callable[[Path], str] | None = None,
    runner: Callable[[list[str]], tuple[int, str]] | None = None,
) -> FileSystemInfo:
    """Detect the filesystem kind for *path* with per-OS fallbacks."""
    target = Path(path).expanduser()
    if probe is not None:
        return probe(target)
    host = host_os or host_os_name()
    if host == "windows":
        volume_getter = volume_info or _windows_volume_info
        try:
            raw, drive_type = volume_getter(target)
        except OSError:
            return _info_from_parts("", source="windows-volume")
        return _info_from_parts(raw, source="windows-volume", drive_type=drive_type)

    run = runner or _default_runner
    if host == "macos":
        fstype_getter = fstypename or _macos_fstype
        raw = fstype_getter(target)
        if raw:
            return _info_from_parts(raw, source="statvfs")
        code, output = run(["stat", "-f", "%T", str(target)])
        if code == 0 and output:
            return _info_from_parts(output.splitlines()[0].strip(), source="stat-f")
    else:
        text = mounts_text if mounts_text is not None else _read_proc_mounts()
        if text is not None:
            entry = find_mount(parse_proc_mounts(text), target)
            if entry is not None:
                return _info_from_parts(
                    entry.fstype, source="proc-mounts", mount_point=entry.mount_point
                )
        if mounts_text is None:
            code, output = run(["stat", "-f", "-c", "%T", str(target)])
            if code == 0 and output:
                return _info_from_parts(output.splitlines()[0].strip(), source="stat-f")

    entries = list(partitions) if partitions is not None else _psutil_entries()
    entry = find_mount(entries, target)
    if entry is not None:
        return _info_from_parts(entry.fstype, source="psutil", mount_point=entry.mount_point)
    return _info_from_parts("", source="psutil")


def filesystem_check(
    root: str | Path,
    *,
    host_os: str | None = None,
    probe: Callable[[Path], FileSystemInfo] | None = None,
    info: FileSystemInfo | None = None,
    mounts_text: str | None = None,
    partitions: Iterable[MountTableEntry] | None = None,
    volume_info: Callable[[Path], tuple[str, str]] | None = None,
    fstypename: Callable[[Path], str] | None = None,
    runner: Callable[[list[str]], tuple[int, str]] | None = None,
) -> CheckResult:
    """Refuse FAT32, warn on exFAT/network/unknown filesystems.

    A removable exFAT volume still mounts, but it carries no POSIX permission
    bits and its write caching can corrupt snapshots, so it is a warning rather
    than a hard refusal.
    """
    target = Path(root)
    try:
        detected = info if info is not None else detect_filesystem(
            target,
            host_os=host_os,
            probe=probe,
            mounts_text=mounts_text,
            partitions=partitions,
            volume_info=volume_info,
            fstypename=fstypename,
            runner=runner,
        )
    except Exception as exc:  # pragma: no cover - defensive: detection must not crash a launch
        return CheckResult(
            id="filesystem",
            title="Bundle filesystem",
            status=CheckStatus.FAIL,
            detail=f"Cannot determine the bundle filesystem type: {exc}",
            critical=False,
        )
    label = detected.label
    if detected.kind is FileSystemKind.FAT32:
        return CheckResult(
            id="filesystem",
            title="Bundle filesystem",
            status=CheckStatus.FAIL,
            detail=(
                "FAT32 is refused: its 4 GiB per-file limit cannot hold Android system images, "
                "snapshots or disk images, and it has no POSIX permission/executable bits."
            ),
            value=label,
            critical=True,
        )
    if detected.kind is FileSystemKind.EXFAT:
        return CheckResult(
            id="filesystem",
            title="Bundle filesystem",
            status=CheckStatus.WARN,
            detail=(
                "exFAT is supported but not recommended: it has no POSIX permissions/executable "
                "bits, so the launcher must chmod binaries on POSIX hosts and snapshots may be "
                "fragile. Prefer NTFS, APFS, ext4, XFS or Btrfs."
            ),
            value=label,
        )
    if detected.kind is FileSystemKind.NETWORK:
        return CheckResult(
            id="filesystem",
            title="Bundle filesystem",
            status=CheckStatus.WARN,
            detail=(
                "The bundle is on a network share; emulator disk I/O and locking are unreliable "
                "there. Copy the bundle to a local disk before booting AVDs."
            ),
            value=label,
        )
    if detected.kind is FileSystemKind.UNKNOWN:
        return CheckResult(
            id="filesystem",
            title="Bundle filesystem",
            status=CheckStatus.WARN,
            detail=(
                f"Could not determine the filesystem type (raw={detected.raw or '<none>'!r}); "
                "assume no executable bits are preserved."
            ),
            value=label,
        )
    if detected.kind is FileSystemKind.HFS:
        return CheckResult(
            id="filesystem",
            title="Bundle filesystem",
            status=CheckStatus.WARN,
            detail="HFS+ is deprecated; APFS is recommended for portable bundles on macOS.",
            value=label,
        )
    return CheckResult(
        id="filesystem",
        title="Bundle filesystem",
        status=CheckStatus.OK,
        detail=f"{label} at {detected.mount_point or target} supports large files and permissions.",
        value=label,
    )


def mount_options_for(
    root: str | Path,
    *,
    host_os: str | None = None,
    mounts_text: str | None = None,
    partitions: Iterable[MountTableEntry] | None = None,
) -> set[str] | None:
    """Return mount options for *root* (``None`` when the mount table is unreadable)."""
    host = host_os or host_os_name()
    target = Path(root)
    if host == "linux":
        text = mounts_text if mounts_text is not None else _read_proc_mounts()
        if text is not None:
            entry = find_mount(parse_proc_mounts(text), target)
            if entry is not None:
                return set(entry.options)
    entries = list(partitions) if partitions is not None else _psutil_entries()
    entry = find_mount(entries, target)
    if entry is not None:
        return set(entry.options)
    return None


def _exec_candidates(root: Path) -> list[Path]:
    paths = PortablePaths.for_root(root)
    candidates = [
        paths.python / "bin" / "python3",
        paths.python / "bin" / "python",
        paths.python / "python.exe",
        paths.venv / "bin" / "python3",
        paths.venv / "bin" / "python",
        paths.venv / "Scripts" / "python.exe",
        paths.sdk_platform_tools / ("adb.exe" if os.name == "nt" else "adb"),
        paths.sdk_emulator / ("emulator.exe" if os.name == "nt" else "emulator"),
        paths.bin / "avdfleet",
        paths.bin / "avdfleet.exe",
    ]
    for directory in (paths.bin, paths.sdk_cmdline_tools):
        try:
            candidates.extend(sorted(path for path in directory.iterdir() if path.is_file()))
        except OSError:
            continue
    seen: set[str] = set()
    result: list[Path] = []
    for candidate in candidates:
        key = os.path.normcase(str(candidate))
        if key not in seen and candidate.exists():
            seen.add(key)
            result.append(candidate)
    return result


def noexec_check(
    root: str | Path,
    *,
    host_os: str | None = None,
    mounts_text: str | None = None,
    partitions: Iterable[MountTableEntry] | None = None,
    exec_probe: Callable[[Path], bool | None] | None = None,
    access: Callable[[Path, int], bool] | None = None,
) -> CheckResult:
    """Detect ``noexec`` mounts and verify bundled binaries are executable.

    Uses the POSIX mount table (``/proc/mounts`` on Linux, psutil elsewhere) and
    probes ``os.access(X_OK)`` on the bundled interpreter/tools.  Windows has no
    execute mount option, so the check is skipped there.
    """
    target = Path(root)
    host = host_os or host_os_name()
    if host == "windows":
        return CheckResult(
            id="filesystem.noexec",
            title="Execute permission",
            status=CheckStatus.SKIP,
            detail="noexec is a POSIX mount flag; Windows tracks execute permission via ACLs.",
        )
    if exec_probe is not None:
        probed = exec_probe(target)
        if probed is False:
            return CheckResult(
                id="filesystem.noexec",
                title="Execute permission",
                status=CheckStatus.FAIL,
                detail="The injected execute probe failed; bundled binaries cannot run.",
                critical=True,
            )
        if probed is True:
            return CheckResult(
                id="filesystem.noexec",
                title="Execute permission",
                status=CheckStatus.OK,
                detail="Bundled executables run from the bundle (execute probe succeeded).",
            )
    options = mount_options_for(
        target, host_os=host, mounts_text=mounts_text, partitions=partitions
    )
    if options is not None and "noexec" in options:
        return CheckResult(
            id="filesystem.noexec",
            title="Execute permission",
            status=CheckStatus.FAIL,
            detail=(
                "The bundle is mounted noexec; the emulator, adb and Python cannot be executed. "
                "Remount with exec or copy the bundle to a local disk."
            ),
            value="noexec",
            critical=True,
        )
    candidates = _exec_candidates(target)
    checker = access or os.access
    if not candidates:
        return CheckResult(
            id="filesystem.noexec",
            title="Execute permission",
            status=CheckStatus.WARN,
            detail="No bundled executables found to probe (incomplete bundle or not yet built).",
        )
    executable = [candidate for candidate in candidates if checker(candidate, os.X_OK)]
    if not executable:
        return CheckResult(
            id="filesystem.noexec",
            title="Execute permission",
            status=CheckStatus.FAIL,
            detail=(
                f"None of the {len(candidates)} bundled binaries are executable; extraction "
                "dropped the execute bit (use the launcher on POSIX to chmod them)."
            ),
            value="not-executable",
            critical=True,
        )
    mounts = ",".join(sorted(options)) if options else "unknown"
    return CheckResult(
        id="filesystem.noexec",
        title="Execute permission",
        status=CheckStatus.OK,
        detail=f"{len(executable)}/{len(candidates)} bundled binaries executable (mount: {mounts}).",
        value=f"{len(executable)}/{len(candidates)}",
    )


__all__ = [
    "FileSystemInfo",
    "FileSystemKind",
    "MountTableEntry",
    "detect_filesystem",
    "filesystem_check",
    "find_mount",
    "mount_options_for",
    "noexec_check",
    "normalize_filesystem",
    "parse_proc_mounts",
]
