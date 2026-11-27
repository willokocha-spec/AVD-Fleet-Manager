"""Snapshot invalidation after relocation.

An emulator snapshot embeds absolute paths (AVD disk images, system image
locations); a snapshot taken before the bundle moved cannot be restored safely.
Relocation therefore moves any snapshot directory that still references an old
root into ``<avd>.avd/snapshots-invalidated/`` and records why.  Nothing is
deleted, so a human can still inspect or manually recover the data.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: Snapshot files larger than this per file are only partially scanned.
MAX_SCAN_BYTES = 64 * 1024 * 1024
_CHUNK = 4 * 1024 * 1024
_INVALIDATED_DIR = "snapshots-invalidated"
_RECORD_SUFFIX = ".invalidated.json"


@dataclass(frozen=True)
class SnapshotRef:
    avd: str
    name: str
    path: Path

    @property
    def invalidated_path(self) -> Path:
        return self.path.parent.parent / _INVALIDATED_DIR / self.name

    def to_dict(self) -> dict[str, str]:
        return {"avd": self.avd, "name": self.name, "path": str(self.path)}


@dataclass(frozen=True)
class SnapshotInvalidation:
    avd: str
    snapshot: str
    original_path: Path
    quarantined_path: Path | None
    old_root: str
    dry_run: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "avd": self.avd,
            "snapshot": self.snapshot,
            "original_path": str(self.original_path),
            "quarantined_path": str(self.quarantined_path) if self.quarantined_path else None,
            "old_root": self.old_root,
            "dry_run": self.dry_run,
        }


def find_snapshots(avd_home: str | Path) -> list[SnapshotRef]:
    """List snapshot directories under an AVD home, deterministically sorted."""
    base = Path(avd_home)
    if not base.is_dir():
        return []
    snapshots: list[SnapshotRef] = []
    for avd_dir in sorted(base.glob("*.avd")):
        snapshots_dir = avd_dir / "snapshots"
        if not snapshots_dir.is_dir():
            continue
        for entry in sorted(snapshots_dir.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                snapshots.append(SnapshotRef(avd=avd_dir.name.removesuffix(".avd"), name=entry.name, path=entry))
    return snapshots


def _needles(old_roots: Iterable[str]) -> list[tuple[str, bytes]]:
    needles: list[tuple[str, bytes]] = []
    for root in old_roots:
        text = str(root)
        if not text:
            continue
        for encoding in ("utf-8", "utf-16-le"):
            needles.append((text, text.encode(encoding, errors="ignore")))
    return needles


def snapshot_root_reference(
    snapshot_dir: str | Path,
    old_roots: Iterable[str],
    *,
    max_bytes: int = MAX_SCAN_BYTES,
) -> str | None:
    """Return the first old root referenced by any file of *snapshot_dir*."""
    needles = _needles(old_roots)
    if not needles:
        return None
    directory = Path(snapshot_dir)
    scanned = 0
    try:
        files = sorted(entry for entry in directory.rglob("*") if entry.is_file())
    except OSError:
        return None
    longest = max(len(needle) for _, needle in needles)
    for file in files:
        try:
            with open(file, "rb") as handle:
                carry = b""
                while scanned < max_bytes:
                    chunk = handle.read(_CHUNK)
                    if not chunk:
                        break
                    scanned += len(chunk)
                    buffer = carry + chunk
                    for root, needle in needles:
                        if needle and needle in buffer:
                            return root
                    carry = buffer[-max(longest, 1) :]
                    if len(chunk) < _CHUNK:
                        break
        except OSError:
            continue
        if scanned >= max_bytes:
            break
    return None


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _unique_target(target: Path) -> Path:
    if not target.exists():
        return target
    base = f"{target.name}-{_utc_stamp()}"
    candidate = target.with_name(base)
    counter = 2
    while candidate.exists():  # pragma: no cover - multiple relocations in one second
        candidate = target.with_name(f"{base}-{counter}")
        counter += 1
    return candidate


def invalidate_stale_snapshots(
    avd_home: str | Path,
    old_roots: Iterable[str],
    *,
    dry_run: bool = False,
) -> list[SnapshotInvalidation]:
    """Move snapshots referencing an old root into ``snapshots-invalidated/``.

    The move is reversible and recorded next to the quarantined snapshot; nothing
    is deleted and AVDs/disk images are untouched.
    """
    roots = [str(root) for root in old_roots]
    results: list[SnapshotInvalidation] = []
    for snapshot in find_snapshots(avd_home):
        matched = snapshot_root_reference(snapshot.path, roots)
        if matched is None:
            continue
        target = _unique_target(snapshot.invalidated_path)
        record = SnapshotInvalidation(
            avd=snapshot.avd,
            snapshot=snapshot.name,
            original_path=snapshot.path,
            quarantined_path=None if dry_run else target,
            old_root=matched,
            dry_run=dry_run,
        )
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(snapshot.path, target)
            payload = {
                "reason": "snapshot referenced a stale bundle root after relocation",
                "old_root": matched,
                "relocated_at": datetime.now(UTC).isoformat(),
            }
            record_file = target.parent / f"{target.name}{_RECORD_SUFFIX}"
            try:
                record_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            except OSError:  # pragma: no cover - best effort
                pass
        results.append(record)
    return results


__all__ = [
    "MAX_SCAN_BYTES",
    "SnapshotInvalidation",
    "SnapshotRef",
    "find_snapshots",
    "invalidate_stale_snapshots",
    "snapshot_root_reference",
]
