"""Bundle manifest: the ``avdfleet-portable.json`` contract.

The manifest doubles as the root marker read by the launchers and as the
checksum manifest verified on launch.  It implements
``contracts/bundle.schema.json`` via the frozen ``core.models.BundleManifest``
model; this module owns the file format, the per-file entry hashing and the
list of runtime paths that are *not* covered by checksums.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from avdfleet.core.config import APP_VERSION
from avdfleet.core.errors import PortableError
from avdfleet.core.models import BundleEntry, BundleManifest, CpuArch, HostOS, sha256_file
from avdfleet.portable.env.model import MANIFEST_FORMAT_VERSION, MARKER_NAME, host_arch_name, host_os_name

__all__ = [
    "BUNDLE_COMPONENT_DIRS",
    "IGNORED_FILE_NAMES",
    "IGNORED_SUFFIXES",
    "MARKER_NAME",
    "REQUIRED_COMPONENTS",
    "RUNTIME_DIRS",
    "build_manifest",
    "entry_executable",
    "iter_bundle_files",
    "manifest_path",
    "manifest_payload",
    "manifest_summary",
    "read_manifest",
    "write_manifest",
]

#: Top-level directories copied from the prepared source/cache into the bundle.
BUNDLE_COMPONENT_DIRS: tuple[str, ...] = (
    "python",
    "lib",
    "venv",
    "sdk",
    "node",
    "jdk",
    "ui",
    "studio",
)
#: Components without which a bundle cannot boot / serve the UI.
REQUIRED_COMPONENTS: tuple[str, ...] = ("python", "sdk", "ui")
#: Directories created at build time but holding runtime state (not checksummed).
RUNTIME_DIRS: tuple[str, ...] = ("data", "logs", "tmp", "avd")
#: Exact file names excluded from the manifest/verify (runtime or OS metadata).
IGNORED_FILE_NAMES: frozenset[str] = frozenset(
    {
        MARKER_NAME,
        "portable-state.json",
        ".unblocked",
        ".DS_Store",
        "Thumbs.db",
        "desktop.ini",
    }
)
IGNORED_DIR_NAMES: frozenset[str] = frozenset(
    {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)
IGNORED_PREFIXES: tuple[str, ...] = (
    "data/",
    "logs/",
    "tmp/",
    "avd/",
    "config/relocation-backups/",
)
IGNORED_SUFFIXES: frozenset[str] = frozenset({".pyc", ".pyo", ".log", ".swp", ".swo", ".tmp"})


def manifest_path(root: str | Path) -> Path:
    return Path(root) / MARKER_NAME


def is_ignored(relative_path: str) -> bool:
    """True for runtime/OS-metadata paths that are not part of the checksum set."""
    rel = relative_path.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel.lstrip("/")
    if not rel:
        return True
    parts = rel.split("/")
    if any(part in IGNORED_DIR_NAMES for part in parts):
        return True
    if rel.startswith(IGNORED_PREFIXES):
        return True
    if parts[-1] in IGNORED_FILE_NAMES:
        return True
    if parts[-1].startswith(".avdfleet-"):
        return True
    return Path(rel).suffix.lower() in IGNORED_SUFFIXES


def entry_executable(path: str | Path) -> bool:
    """The manifest ``executable`` flag: the POSIX execute bit is set."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return False
    return bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def _walk(root: Path) -> Iterator[tuple[str, Path]]:
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in IGNORED_DIR_NAMES
            and not (current == root and name in RUNTIME_DIRS)
            and not _relative_posix(current / name, root).startswith(IGNORED_PREFIXES)
        )
        for name in sorted(filenames):
            path = current / name
            relative = _relative_posix(path, root)
            if is_ignored(relative):
                continue
            yield relative, path


def _relative_posix(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - walk only yields children
        return path.as_posix()


def iter_bundle_files(root: str | Path) -> Iterator[tuple[str, Path]]:
    """Yield ``(posix_relative_path, absolute_path)`` for every checksummed file."""
    yield from _walk(Path(root))


def file_entry(root: str | Path, relative: str) -> BundleEntry:
    path = Path(root) / relative
    return BundleEntry(
        path=relative,
        sha256=sha256_file(str(path)),
        size=path.stat().st_size,
        executable=entry_executable(path),
    )


def build_manifest(
    entries: list[BundleEntry],
    *,
    root: str | Path,
    name: str = "avdfleet-portable",
    version: str = APP_VERSION,
    host_os: str | None = None,
    host_arch: str | None = None,
    offline: bool = False,
    python_version: str = "",
    tool_versions: dict[str, str] | None = None,
    warnings: list[str] | None = None,
    created_at: datetime | None = None,
) -> BundleManifest:
    """Assemble and validate a manifest for *root* (root is the *destination*)."""
    ordered = sorted(entries, key=lambda entry: entry.path)
    manifest = BundleManifest(
        format_version=MANIFEST_FORMAT_VERSION,
        name=name,
        version=version,
        created_at=created_at or datetime.now(UTC),
        host_os=_host_os_enum(host_os or host_os_name()),
        host_arch=_host_arch_enum(host_arch or host_arch_name()),
        root=str(Path(root)),
        offline=offline,
        entries=ordered,
        tool_versions=dict(tool_versions or {}),
        python_version=python_version,
        total_bytes=sum(entry.size for entry in ordered),
        warnings=list(warnings or []),
    )
    return manifest


def _host_os_enum(value: str) -> HostOS:
    try:
        return HostOS(value)
    except ValueError:
        return HostOS.UNKNOWN


def _host_arch_enum(value: str) -> CpuArch:
    try:
        return CpuArch(value)
    except ValueError:
        return CpuArch.UNKNOWN


def manifest_payload(manifest: BundleManifest) -> dict[str, Any]:
    """JSON-ready manifest preserving the contract's key names."""
    payload = manifest.model_dump(mode="json")
    created = payload.get("created_at")
    if isinstance(created, str):
        # Normalise ``+00:00`` to ``Z`` for byte-stable manifests across pydantic versions.
        payload["created_at"] = created.replace("+00:00", "Z")
    return payload


def write_manifest(root: str | Path, manifest: BundleManifest) -> Path:
    """Atomically write ``avdfleet-portable.json`` into *root*."""
    path = manifest_path(root)
    payload = manifest_payload(manifest)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:  # pragma: no cover
                pass
    return path


def read_manifest(root: str | Path) -> BundleManifest:
    """Read and validate the manifest, raising :class:`PortableError` with a hint."""
    path = manifest_path(root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PortableError(
            f"Not a portable bundle: {MARKER_NAME} is missing from {Path(root)}",
            hint="Run the bundle launcher from the folder that contains the manifest.",
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableError(f"Cannot read bundle manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PortableError(f"Bundle manifest {path} must contain a JSON object")
    try:
        return BundleManifest.model_validate(payload)
    except Exception as exc:
        raise PortableError(
            f"Bundle manifest {path} does not match bundle.schema.json: {exc}",
            hint="Rebuild the bundle with 'avdfleet bundle'.",
        ) from exc


def manifest_summary(manifest: BundleManifest) -> dict[str, Any]:
    """Quick, checksum-free overview used by ``portable inspect``."""
    components: dict[str, int] = {}
    for entry in manifest.entries:
        top = entry.path.split("/", 1)[0]
        components[top] = components.get(top, 0) + 1
    largest = sorted(manifest.entries, key=lambda entry: entry.size, reverse=True)[:5]
    return {
        "name": manifest.name,
        "version": manifest.version,
        "format_version": manifest.format_version,
        "host_os": manifest.host_os.value,
        "host_arch": manifest.host_arch.value,
        "root": manifest.root,
        "offline": manifest.offline,
        "python_version": manifest.python_version,
        "created_at": manifest.created_at.isoformat(),
        "entry_count": len(manifest.entries),
        "total_bytes": manifest.total_bytes,
        "components": dict(sorted(components.items())),
        "largest_files": [{"path": entry.path, "size": entry.size} for entry in largest],
        "warnings": list(manifest.warnings),
    }
