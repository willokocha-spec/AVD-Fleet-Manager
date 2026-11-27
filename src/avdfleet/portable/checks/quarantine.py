"""Quarantine / Mark-of-the-Web checks for portable bundles.

Windows attaches a ``Zone.Identifier`` NTFS alternate data stream to files that
came from the internet; the launcher removes it on first run with
``Unblock-File``.  macOS stores a ``com.apple.quarantine`` extended attribute and
requires ``xattr -dr com.apple.quarantine``.  The checks here are read-only;
removal is the launcher's/documented command's job.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from avdfleet.core.models import CheckStatus
from avdfleet.portable.env.model import host_os_name

from . import CheckResult

__all__ = [
    "QuarantineFinding",
    "QuarantineScan",
    "has_mark_of_the_web",
    "quarantine_check",
    "scan_macos_quarantine",
    "scan_mark_of_the_web",
    "unblock_instruction",
    "xattr_instruction",
]

_MOTW_STREAM = "Zone.Identifier"
_QUARANTINE_ATTR = "com.apple.quarantine"
_DEFAULT_SCAN_LIMIT = 400


@dataclass(frozen=True)
class QuarantineFinding:
    path: str
    reason: str


@dataclass(frozen=True)
class QuarantineScan:
    host_os: str
    scanned: int
    findings: list[QuarantineFinding] = field(default_factory=list)
    instructions: list[str] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "host_os": self.host_os,
            "scanned": self.scanned,
            "findings": [{"path": finding.path, "reason": finding.reason} for finding in self.findings],
            "instructions": list(self.instructions),
            "truncated": self.truncated,
        }


# ---------------------------------------------------------------------------
# Windows: alternate data streams
# ---------------------------------------------------------------------------


def _windows_streams(path: Path) -> list[str]:
    """List NTFS alternate data stream names for *path* (empty off Windows)."""
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, AttributeError):  # pragma: no cover - non-Windows
        return []

    class WIN32_FIND_STREAM_DATA(ctypes.Structure):
        _fields_ = [
            ("StreamSize", ctypes.c_longlong),
            ("cStreamName", ctypes.c_wchar * 296),
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.FindFirstStreamW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(WIN32_FIND_STREAM_DATA),
            wintypes.DWORD,
        ]
        kernel32.FindFirstStreamW.restype = wintypes.HANDLE
        kernel32.FindNextStreamW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(WIN32_FIND_STREAM_DATA),
        ]
        kernel32.FindNextStreamW.restype = wintypes.BOOL
        kernel32.FindClose.argtypes = [wintypes.HANDLE]
        kernel32.FindClose.restype = wintypes.BOOL

        data = WIN32_FIND_STREAM_DATA()
        handle = kernel32.FindFirstStreamW(str(path), 0, ctypes.byref(data), 0)
        invalid = ctypes.c_void_p(-1).value
        if not handle or handle == invalid:
            return []
        names: list[str] = []
        try:
            while True:
                raw_name = data.cStreamName or ""
                name = raw_name.strip(":")
                if name and name.upper() != "$DATA":
                    names.append(name.split(":")[0])
                if not kernel32.FindNextStreamW(handle, ctypes.byref(data)):
                    break
        finally:
            kernel32.FindClose(handle)
        return names
    except (OSError, ValueError):  # pragma: no cover - filesystem without ADS support
        return []


def has_mark_of_the_web(
    path: str | Path,
    *,
    stream_lister: Callable[[Path], list[str]] | None = None,
) -> bool:
    """True when *path* carries a ``Zone.Identifier`` alternate data stream."""
    lister = stream_lister or _windows_streams
    try:
        streams = lister(Path(path))
    except OSError:
        return False
    return any(stream.lower() == _MOTW_STREAM.lower() for stream in streams)


def _priority_candidates(root: Path) -> list[Path]:
    """Files Windows/macOS quarantine in practice, checked before the bulk walk."""
    candidates = [root / name for name in ("AVD Fleet.cmd", "AVD Fleet.command", "avdfleet.sh")]
    candidates.extend(
        [
            root / "python" / "python.exe",
            root / "python" / "python3.exe",
            root / "python" / "bin" / "python3",
            root / "python" / "bin" / "python",
            root / "venv" / "Scripts" / "python.exe",
            root / "venv" / "bin" / "python3",
            root / "venv" / "bin" / "python",
            root / "sdk" / "platform-tools" / "adb.exe",
            root / "sdk" / "platform-tools" / "adb",
            root / "sdk" / "emulator" / "emulator.exe",
            root / "sdk" / "emulator" / "emulator",
            root / "sdk" / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat",
            root / "sdk" / "cmdline-tools" / "latest" / "bin" / "sdkmanager",
        ]
    )
    candidates.extend(sorted((root / "venv" / "Scripts").glob("*.exe")) if (root / "venv" / "Scripts").is_dir() else [])
    candidates.extend(sorted((root / "venv" / "bin").glob("*"))[:50] if (root / "venv" / "bin").is_dir() else [])
    return [candidate for candidate in candidates if candidate.is_file()]


def _walk_limited(
    root: Path,
    *,
    limit: int,
) -> tuple[list[Path], bool]:
    """Return candidate files up to *limit*, hot-path binaries first.

    The full SDK tree can hold hundreds of thousands of files; scanning all of
    them on every launch would be slow.  The executables Windows marks as blocked
    are checked first, then the remaining tree is walked only until the cap is
    reached (``truncated`` reports an incomplete scan).
    """
    files: list[Path] = []
    seen: set[str] = set()
    for candidate in _priority_candidates(root):
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        files.append(candidate)
        if len(files) >= limit:
            return files, True
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in {"__pycache__", ".git"})
        for name in sorted(filenames):
            path = Path(directory) / name
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            files.append(path)
            if len(files) >= limit:
                return files, True
    return files, False


def scan_mark_of_the_web(
    root: str | Path,
    *,
    stream_lister: Callable[[Path], list[str]] | None = None,
    limit: int = _DEFAULT_SCAN_LIMIT,
) -> QuarantineScan:
    """Scan a bundle for Windows Mark-of-the-Web streams (read-only)."""
    base = Path(root)
    lister = stream_lister or _windows_streams
    files, truncated = _walk_limited(base, limit=limit)
    findings: list[QuarantineFinding] = []
    for file in files:
        if has_mark_of_the_web(file, stream_lister=lister):
            findings.append(
                QuarantineFinding(
                    path=_relative_posix(file, base),
                    reason=f"NTFS alternate data stream {_MOTW_STREAM} (Mark-of-the-Web)",
                )
            )
    return QuarantineScan(
        host_os="windows",
        scanned=len(files),
        findings=findings,
        instructions=[unblock_instruction(base)] if findings else [],
        truncated=truncated,
    )


def unblock_instruction(root: str | Path) -> str:
    """PowerShell 5.1 compatible one-liner that removes Zone.Identifier streams."""
    root_path = str(Path(root))
    return (
        'powershell -NoProfile -ExecutionPolicy Bypass -Command "'
        f"Get-ChildItem -LiteralPath '{root_path}' -Recurse -File -ErrorAction SilentlyContinue "
        '| Unblock-File"'
    )


# ---------------------------------------------------------------------------
# macOS: quarantine xattr
# ---------------------------------------------------------------------------


def _default_xattr_reader(path: Path) -> bytes | None:
    if sys.platform != "darwin":  # pragma: no cover - exercised via injection on other hosts
        return None
    getxattr = getattr(os, "getxattr", None)
    if getxattr is None:  # pragma: no cover - older Python on macOS
        return None
    try:
        return getxattr(str(path), _QUARANTINE_ATTR)
    except OSError:
        return None


def scan_macos_quarantine(
    root: str | Path,
    *,
    xattr_reader: Callable[[Path], bytes | None] | None = None,
    limit: int = _DEFAULT_SCAN_LIMIT,
) -> QuarantineScan:
    """Scan a bundle for ``com.apple.quarantine`` xattrs (read-only)."""
    base = Path(root)
    reader = xattr_reader or _default_xattr_reader
    files, truncated = _walk_limited(base, limit=limit)
    findings: list[QuarantineFinding] = []
    for file in files:
        try:
            value = reader(file)
        except OSError:
            value = None
        if value:
            findings.append(
                QuarantineFinding(
                    path=_relative_posix(file, base),
                    reason=f"extended attribute {_QUARANTINE_ATTR}={value.decode(errors='replace')!r}",
                )
            )
    return QuarantineScan(
        host_os="macos",
        scanned=len(files),
        findings=findings,
        instructions=[xattr_instruction(base)] if findings else [],
        truncated=truncated,
    )


def xattr_instruction(root: str | Path) -> str:
    """The command that clears quarantine attributes on a bundle folder."""
    return f'xattr -dr com.apple.quarantine "{Path(root)}"'


def _relative_posix(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - walk only yields children
        return path.as_posix()


# ---------------------------------------------------------------------------
# Check
# ---------------------------------------------------------------------------


def quarantine_check(
    root: str | Path,
    *,
    host_os: str | None = None,
    motw_probe: Callable[[Path], list[str]] | None = None,
    xattr_probe: Callable[[Path], bytes | None] | None = None,
    limit: int = _DEFAULT_SCAN_LIMIT,
) -> CheckResult:
    """Report quarantined files with the exact remediation command."""
    host = host_os or host_os_name()
    if host == "windows":
        scan = scan_mark_of_the_web(root, stream_lister=motw_probe, limit=limit)
        if scan.findings:
            detail = (
                f"{len(scan.findings)} bundled file(s) carry the Mark-of-the-Web. "
                "Windows SmartScreen may block the bundled Python/emulator. "
                f"Remove it with: {scan.instructions[0]}"
            )
            if scan.truncated:
                detail += " (scan was truncated; more files may be affected)"
            return CheckResult(
                id="quarantine",
                title="Mark-of-the-Web",
                status=CheckStatus.WARN,
                detail=detail,
                value=str(len(scan.findings)),
            )
        suffix = " (scan truncated)" if scan.truncated else ""
        return CheckResult(
            id="quarantine",
            title="Mark-of-the-Web",
            status=CheckStatus.OK,
            detail=f"No Zone.Identifier streams in {scan.scanned} checked files{suffix}.",
            value="0",
        )
    if host == "macos":
        scan = scan_macos_quarantine(root, xattr_reader=xattr_probe, limit=limit)
        if scan.findings:
            detail = (
                f"{len(scan.findings)} bundled file(s) carry com.apple.quarantine. "
                'Gatekeeper may block them; run: ' + scan.instructions[0]
            )
            if scan.truncated:
                detail += " (scan was truncated; more files may be affected)"
            return CheckResult(
                id="quarantine",
                title="macOS quarantine",
                status=CheckStatus.WARN,
                detail=detail,
                value=str(len(scan.findings)),
            )
        suffix = " (scan truncated)" if scan.truncated else ""
        return CheckResult(
            id="quarantine",
            title="macOS quarantine",
            status=CheckStatus.OK,
            detail=f"No com.apple.quarantine xattrs in {scan.scanned} checked files{suffix}.",
            value="0",
        )
    return CheckResult(
        id="quarantine",
        title="Quarantine",
        status=CheckStatus.SKIP,
        detail="Quarantine/Mark-of-the-Web only applies to Windows and macOS hosts.",
    )
