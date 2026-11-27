"""Bundle verification and inspection.

``verify`` detects missing files, modified content (checksum/size mismatch),
unexpected extra files, path traversal in manifest entries and symlinks (which
are not portable and can escape the bundle).  Files that relocation legitimately
rewrites are downgraded to warnings when the bundle has moved but was not yet
relocated, so verification still tells the truth without blocking a moved bundle.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from avdfleet.core.errors import PortableError
from avdfleet.core.models import BundleManifest, sha256_file
from avdfleet.portable.env.model import MANIFEST_FORMAT_VERSION

from .manifest import MARKER_NAME, is_ignored, iter_bundle_files, read_manifest

__all__ = [
    "RELOCATABLE_PREFIXES",
    "BundleInspection",
    "BundleVerificationError",
    "VerifyIssue",
    "VerifyReport",
    "format_verify_report",
    "inspect_bundle",
    "validate_entry_path",
    "verify_bundle",
]

#: Paths relocation may legitimately rewrite; mismatches are warnings after a move.
RELOCATABLE_PREFIXES: tuple[str, ...] = ("config/", "avd/", "venv/", "launchers/", "studio/")

_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_CONTROL = re.compile(r"[\x00-\x1f]")


@dataclass(frozen=True)
class VerifyIssue:
    kind: str
    path: str
    detail: str
    severity: str = "error"

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "path": self.path, "detail": self.detail, "severity": self.severity}


@dataclass(frozen=True)
class VerifyReport:
    root: Path
    manifest: BundleManifest | None = None
    issues: tuple[VerifyIssue, ...] = ()
    checked: int = 0
    relocated: bool = False

    @property
    def errors(self) -> list[VerifyIssue]:
        return [issue for issue in self.issues if issue.is_error]

    @property
    def warnings(self) -> list[VerifyIssue]:
        return [issue for issue in self.issues if not issue.is_error]

    @property
    def ok(self) -> bool:
        return not self.errors

    def by_kind(self, kind: str) -> list[VerifyIssue]:
        return [issue for issue in self.issues if issue.kind == kind]

    @property
    def missing(self) -> list[str]:
        return [issue.path for issue in self.issues if issue.kind == "missing"]

    @property
    def modified(self) -> list[str]:
        return [
            issue.path
            for issue in self.issues
            if issue.kind in {"checksum_mismatch", "size_mismatch", "modified"}
        ]

    @property
    def extra(self) -> list[str]:
        return [issue.path for issue in self.issues if issue.kind == "extra"]

    def summary(self) -> str:
        if self.manifest is None:
            return f"verification failed: {len(self.errors)} error(s), {len(self.warnings)} warning(s)"
        return (
            f"{self.checked} file(s) checked, {len(self.errors)} error(s), "
            f"{len(self.warnings)} warning(s), bundle root {self.root}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "ok": self.ok,
            "relocated": self.relocated,
            "checked": self.checked,
            "summary": self.summary(),
            "missing": self.missing,
            "modified": self.modified,
            "extra": self.extra,
            "issues": [issue.to_dict() for issue in self.issues],
        }


class BundleVerificationError(PortableError):
    """Raised by ``BundleBuilder.verify`` when the manifest does not match the tree."""

    code = "bundle_verification_failed"

    def __init__(self, report: VerifyReport) -> None:
        self.report = report
        message = f"Portable bundle verification failed at {report.root}: {report.summary()}"
        hint = (
            "Re-run 'avdfleet portable relocate' after moving the bundle, repair the listed "
            "files, or rebuild the bundle."
        )
        super().__init__(
            message,
            hint=hint,
            details={
                "errors": [issue.to_dict() for issue in report.errors[:50]],
                "warnings": [issue.to_dict() for issue in report.warnings[:50]],
                "checked": report.checked,
                "relocated": report.relocated,
            },
        )


def validate_entry_path(relative_path: str) -> str | None:
    """Return a problem description for an unsafe manifest path, else ``None``."""
    if not isinstance(relative_path, str) or not relative_path:
        return "entry path is empty or not a string"
    if "\\" in relative_path:
        return "entry path must use POSIX separators ('/')"
    if relative_path.startswith("/"):
        return "entry path must be relative to the bundle root"
    if _DRIVE_PREFIX.match(relative_path):
        return "entry path must not contain a drive letter"
    if _CONTROL.search(relative_path):
        return "entry path contains control characters"
    pure = PurePosixPath(relative_path)
    if pure.as_posix() != relative_path:
        return "entry path is not normalized"
    parts = pure.parts
    if any(part in {"..", "."} for part in parts):
        return "entry path contains a traversal segment"
    return None


def _norm(value: str) -> str:
    return os.path.normcase(os.path.normpath(value))


def _relative_posix(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover
        return path.as_posix()


def _inside(root: Path, target: Path) -> bool:
    root_norm = os.path.normcase(os.path.normpath(str(root)))
    target_norm = os.path.normcase(os.path.normpath(str(target)))
    return target_norm == root_norm or target_norm.startswith(root_norm + os.sep)


def _symlink_issues(root: Path) -> list[VerifyIssue]:
    issues: list[VerifyIssue] = []
    seen: set[str] = set()
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in list(dirnames) + list(filenames):
            path = current / name
            try:
                if not path.is_symlink():
                    continue
            except OSError:  # pragma: no cover
                continue
            relative = _relative_posix(path, root)
            if is_ignored(relative) or relative in seen:
                continue
            seen.add(relative)
            try:
                resolved = path.resolve(strict=False)
            except OSError:  # pragma: no cover
                resolved = path
            if not _inside(root, resolved):
                issues.append(
                    VerifyIssue(
                        "path_traversal",
                        relative,
                        f"symlink resolves outside the bundle root: {resolved}",
                    )
                )
            else:
                issues.append(
                    VerifyIssue(
                        "symlink",
                        relative,
                        "portable bundles must not contain symlinks (FAT/exFAT and archive "
                        "extraction do not preserve them)",
                    )
                )
    return issues


def verify_bundle(
    root: str | Path,
    *,
    manifest: BundleManifest | None = None,
    check_executable: bool = True,
) -> VerifyReport:
    """Verify a bundle tree against its manifest (read-only)."""
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        issue = VerifyIssue("root", str(root_path), "bundle root does not exist")
        return VerifyReport(root=root_path, issues=(issue,))
    if manifest is None:
        try:
            manifest = read_manifest(root_path)
        except PortableError as exc:
            issue = VerifyIssue("manifest", MARKER_NAME, str(exc))
            return VerifyReport(root=root_path, issues=(issue,))
    issues: list[VerifyIssue] = []
    if manifest.format_version != MANIFEST_FORMAT_VERSION:
        issues.append(
            VerifyIssue(
                "manifest",
                MARKER_NAME,
                f"unsupported format_version {manifest.format_version!r} "
                f"(expected {MANIFEST_FORMAT_VERSION!r})",
            )
        )
    relocated = bool(manifest.root and _norm(manifest.root) != _norm(str(root_path)))
    if relocated:
        issues.append(
            VerifyIssue(
                "relocated",
                MARKER_NAME,
                f"manifest records root {manifest.root!r} but the bundle is at {str(root_path)!r}; "
                "run relocation to rewrite AVD/config/venv paths",
                severity="warning",
            )
        )
    entries: dict[str, Any] = {}
    checked = 0
    for entry in manifest.entries:
        problem = validate_entry_path(entry.path)
        if problem is not None:
            issues.append(VerifyIssue("path_traversal", str(entry.path), problem))
            continue
        if entry.path in entries:
            issues.append(
                VerifyIssue("manifest", entry.path, "duplicate manifest entry", severity="warning")
            )
            continue
        entries[entry.path] = entry
        file_path = root_path / entry.path
        if file_path.is_symlink():
            issues.append(VerifyIssue("symlink", entry.path, "manifest entry is a symlink"))
            continue
        if not file_path.exists():
            issues.append(VerifyIssue("missing", entry.path, "file listed in the manifest is missing"))
            continue
        if not file_path.is_file():
            issues.append(VerifyIssue("missing", entry.path, "manifest entry is not a regular file"))
            continue
        checked += 1
        try:
            size = file_path.stat().st_size
        except OSError as exc:
            issues.append(VerifyIssue("missing", entry.path, f"cannot stat file: {exc}"))
            continue
        if size != entry.size:
            issues.append(
                VerifyIssue(
                    "size_mismatch",
                    entry.path,
                    f"size changed: manifest {entry.size} bytes, file {size} bytes",
                )
            )
        try:
            digest = sha256_file(str(file_path))
        except OSError as exc:
            issues.append(VerifyIssue("modified", entry.path, f"cannot hash file: {exc}"))
            continue
        if digest != entry.sha256:
            relocatable = relocated and entry.path.startswith(RELOCATABLE_PREFIXES)
            issues.append(
                VerifyIssue(
                    "checksum_mismatch",
                    entry.path,
                    "sha256 mismatch (manifest content differs)",
                    severity="warning" if relocatable else "error",
                )
            )
        if (
            check_executable
            and entry.executable
            and os.name != "nt"
            and not os.access(file_path, os.X_OK)
        ):
            issues.append(
                VerifyIssue(
                    "executable",
                    entry.path,
                    "manifest marks this file executable but the execute bit is not set",
                    severity="warning",
                )
            )

    issues.extend(_symlink_issues(root_path))

    for relative, _path in iter_bundle_files(root_path):
        if relative in entries or is_ignored(relative):
            continue
        issues.append(
            VerifyIssue(
                "extra",
                relative,
                "file is not listed in the manifest (undocumented write or stale build)",
            )
        )

    if manifest.total_bytes and manifest.total_bytes != sum(entry.size for entry in entries.values()):
        issues.append(
            VerifyIssue(
                "manifest",
                MARKER_NAME,
                f"total_bytes {manifest.total_bytes} does not match manifest entry sizes",
                severity="warning",
            )
        )

    return VerifyReport(
        root=root_path,
        manifest=manifest,
        issues=tuple(issues),
        checked=checked,
        relocated=relocated,
    )


@dataclass(frozen=True)
class BundleInspection:
    """Checksum-free bundle summary."""

    root: Path
    manifest: BundleManifest
    files_on_disk: int = 0
    total_bytes: int = 0
    components: dict[str, int] = field(default_factory=dict)
    largest_files: list[dict[str, Any]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "name": self.manifest.name,
            "version": self.manifest.version,
            "format_version": self.manifest.format_version,
            "host_os": self.manifest.host_os.value,
            "host_arch": self.manifest.host_arch.value,
            "offline": self.manifest.offline,
            "python_version": self.manifest.python_version,
            "created_at": self.manifest.created_at.isoformat(),
            "manifest_root": self.manifest.root,
            "entry_count": len(self.manifest.entries),
            "files_on_disk": self.files_on_disk,
            "total_bytes": self.total_bytes,
            "components": dict(sorted(self.components.items())),
            "largest_files": list(self.largest_files),
            "missing": list(self.missing),
            "warnings": list(self.manifest.warnings),
        }


def inspect_bundle(root: str | Path) -> BundleInspection:
    """Summarise a bundle without hashing every file (fast inspection)."""
    root_path = Path(root).expanduser().resolve()
    manifest = read_manifest(root_path)
    files = 0
    total = 0
    components: dict[str, int] = {}
    for relative, path in iter_bundle_files(root_path):
        files += 1
        try:
            total += path.stat().st_size
        except OSError:
            continue
        top = relative.split("/", 1)[0]
        components[top] = components.get(top, 0) + 1
    missing = [
        entry.path for entry in manifest.entries if not (root_path / entry.path).is_file()
    ]
    largest = sorted(manifest.entries, key=lambda entry: entry.size, reverse=True)[:5]
    return BundleInspection(
        root=root_path,
        manifest=manifest,
        files_on_disk=files,
        total_bytes=total,
        components=components,
        largest_files=[{"path": entry.path, "size": entry.size} for entry in largest],
        missing=missing,
    )


def format_verify_report(report: VerifyReport) -> str:
    """Human-readable multiline report for the CLI."""
    lines = [report.summary()]
    for issue in report.errors:
        lines.append(f"  ERROR {issue.kind}: {issue.path}: {issue.detail}")
    for issue in report.warnings:
        lines.append(f"  WARN  {issue.kind}: {issue.path}: {issue.detail}")
    if report.ok and not report.warnings:
        lines.append("  bundle is intact")
    return "\n".join(lines)
