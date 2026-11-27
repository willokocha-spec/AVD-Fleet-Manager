"""Virtualenv relocation: inspect shebangs/``pyvenv.cfg`` and repair or rebuild.

A bundle ships a pre-built virtualenv whose console scripts have absolute
shebangs pointing at the build-time root.  After a move those shebangs and the
``pyvenv.cfg`` ``home`` are stale; rewriting them is much cheaper than rebuilding
(which would also need an offline reinstall), so repair is attempted first and a
rebuild is only offered as an explicit fallback.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from avdfleet.portable.env.model import host_os_name

_Runner = Callable[[list[str]], tuple[int, str]]


@dataclass(frozen=True)
class VenvInspection:
    venv: Path
    exists: bool = False
    reasons: list[str] = field(default_factory=list)
    stale_shebangs: list[str] = field(default_factory=list)
    stale_activate: list[str] = field(default_factory=list)
    stale_home: str | None = None

    @property
    def ok(self) -> bool:
        return not self.reasons

    @property
    def needs_repair(self) -> bool:
        return bool(self.reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "venv": str(self.venv),
            "exists": self.exists,
            "ok": self.ok,
            "reasons": list(self.reasons),
            "stale_shebangs": list(self.stale_shebangs),
            "stale_activate": list(self.stale_activate),
            "stale_home": self.stale_home,
        }


@dataclass(frozen=True)
class VenvRepair:
    venv: Path
    status: str = "ok"  # ok | repaired | rebuilt | unresolved | dry-run | missing
    reasons: list[str] = field(default_factory=list)
    rewritten: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    rebuilt: bool = False
    dry_run: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "repaired", "rebuilt", "dry-run", "missing"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "venv": str(self.venv),
            "status": self.status,
            "ok": self.ok,
            "reasons": list(self.reasons),
            "rewritten": list(self.rewritten),
            "backups": list(self.backups),
            "rebuilt": self.rebuilt,
            "dry_run": self.dry_run,
            "warnings": list(self.warnings),
        }


def _mentions_root(text: str, old_roots: Iterable[str], *, ignore_case: bool | None = None) -> str | None:
    fold = os.name == "nt" if ignore_case is None else ignore_case
    haystack = text.replace("\\", "/")
    if fold:
        haystack = haystack.lower()
    for root in old_roots:
        if not root:
            continue
        needle = str(root).replace("\\", "/").rstrip("/")
        if fold:
            needle = needle.lower()
        if needle and needle in haystack:
            return root
    return None


def _root_pattern(root: str, *, ignore_case: bool) -> re.Pattern[str] | None:
    """Separator-flexible regex matching *root* in text (plain or escaped)."""
    stripped = str(root).strip().rstrip("/\\")
    segments = [segment for segment in re.split(r"[\\/]+", stripped) if segment]
    if not segments:
        return None
    pattern = r"[\\/]+".join(re.escape(segment) for segment in segments)
    if stripped.startswith(("/", "\\")):
        pattern = r"[\\/]+" + pattern
    return re.compile(pattern, re.IGNORECASE if ignore_case else 0)


def _replace_root(text: str, roots: list[str], current_root: str) -> str:
    rewritten = text
    fold = os.name == "nt"
    for root in roots:
        if not root:
            continue
        pattern = _root_pattern(root, ignore_case=fold)
        if pattern is None:
            continue

        def replacement(match: re.Match[str], value: str = current_root) -> str:
            matched = match.group(0)
            if "\\" in matched:
                return value.replace("/", "\\")
            return value.replace("\\", "/")

        rewritten = pattern.sub(replacement, rewritten)
    return rewritten


def _read_head(path: Path, size: int = 4096) -> str:
    try:
        with open(path, "rb") as handle:
            return handle.read(size).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _venv_bin_dir(venv: Path, host_os: str) -> Path:
    return venv / ("Scripts" if host_os == "windows" else "bin")


def inspect_venv(
    venv: str | Path,
    *,
    old_roots: Iterable[str],
    current_root: str | Path,
    host_os: str | None = None,
) -> VenvInspection:
    """Report stale absolute paths inside the bundled virtualenv."""
    venv_dir = Path(venv)
    roots = [str(root) for root in old_roots]
    host = host_os or host_os_name()
    _ = current_root  # accepted for symmetry with repair_venv; not needed to detect staleness
    if not venv_dir.is_dir():
        return VenvInspection(venv=venv_dir, exists=False)
    reasons: list[str] = []
    stale_shebangs: list[str] = []
    stale_activate: list[str] = []
    stale_home: str | None = None

    pyvenv_cfg = venv_dir / "pyvenv.cfg"
    if pyvenv_cfg.is_file():
        for line in _read_head(pyvenv_cfg).splitlines():
            if not line.lower().startswith("home"):
                continue
            _, _, value = line.partition("=")
            matched = _mentions_root(value.strip(), roots)
            if matched is not None:
                stale_home = value.strip()
                reasons.append(f"pyvenv.cfg home points at old root {matched!r}")
            break

    bin_dir = _venv_bin_dir(venv_dir, host)
    scan_dirs = [bin_dir]
    other = venv_dir / ("bin" if host == "windows" else "Scripts")
    if other.is_dir() and other != bin_dir:
        # Be tolerant of a venv created on the other OS (or a bind-mounted layout).
        scan_dirs.append(other)
    for directory in scan_dirs:
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if not entry.is_file():
                continue
            head = _read_head(entry, 512)
            if head.startswith("#!"):
                matched = _mentions_root(head.splitlines()[0], roots)
                if matched is not None:
                    stale_shebangs.append(entry.relative_to(venv_dir).as_posix())
            elif entry.name.lower().startswith("activate"):
                matched = _mentions_root(_read_head(entry), roots)
                if matched is not None:
                    stale_activate.append(entry.relative_to(venv_dir).as_posix())
    if stale_shebangs:
        reasons.append(f"{len(stale_shebangs)} virtualenv script shebang(s) point at an old root")
    if stale_activate:
        reasons.append(f"{len(stale_activate)} activation script(s) point at an old root")

    return VenvInspection(
        venv=venv_dir,
        exists=True,
        reasons=reasons,
        stale_shebangs=stale_shebangs,
        stale_activate=stale_activate,
        stale_home=stale_home,
    )


def _root_from_interpreter_home(home: Path) -> str | None:
    """Infer the bundle root from a ``pyvenv.cfg`` ``home =`` line.

    Accepted layouts (relative to the bundle root)::

        python              (Windows base interpreter directory)
        python/bin          (POSIX base interpreter directory)
        runtime/python/bin  (research/R7 prepared layout, normalised on build)

    A plain system interpreter (``/usr/bin``, ``C:\\Python312``) yields ``None``
    so relocation never rewrites paths pointing outside the bundle.
    """
    parts = list(home.parts)
    popped_interpreter = False
    if parts and parts[-1].lower() in {"bin", "scripts"}:
        parts.pop()
    if parts and parts[-1].lower() in {"python", "python3", "jre", "node"}:
        parts.pop()
        popped_interpreter = True
        if parts and parts[-1].lower() == "runtime":
            parts.pop()
    if not popped_interpreter or not parts:
        return None
    candidate = Path(*parts)
    if candidate.parent == candidate or len(candidate.parts) < 2:
        return None
    return str(candidate)


def _drop_ancestor_duplicates(candidates: list[str]) -> list[str]:
    """Keep only the top-most root when one candidate is a descendant of another."""
    unique: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in unique:
            unique.append(candidate)
    result: list[str] = []
    for index, candidate in enumerate(unique):
        path = Path(candidate)
        ancestor_of_other = any(
            index != other_index
            and (path == other or other in path.parents)
            for other_index, other in enumerate(unique)
            if other_index != index
        )
        descendant_of_other = any(
            index != other_index
            and (other == path or other in path.parents)
            for other_index, other in enumerate(unique)
            if other_index != index
        )
        if descendant_of_other and not ancestor_of_other:
            continue
        if descendant_of_other and ancestor_of_other:  # pragma: no cover - impossible
            continue
        result.append(candidate)
    return result


def discover_venv_old_roots(
    venv: str | Path,
    *,
    host_os: str | None = None,
) -> list[str]:
    """Infer bundle roots a prepared virtualenv was built against.

    Bundle builds copy a pre-built venv from a *prepared source* tree, so its
    shebangs/``pyvenv.cfg`` may reference the preparation root instead of the
    destination.  This helper recovers those roots so the builder/relocator can
    rewrite them without the caller knowing the preparation root.
    """
    venv_dir = Path(venv)
    host = host_os or host_os_name()
    if not venv_dir.is_dir():
        return []
    candidates: list[str] = []
    pyvenv_cfg = venv_dir / "pyvenv.cfg"
    if pyvenv_cfg.is_file():
        for line in _read_head(pyvenv_cfg).splitlines():
            if not line.lower().startswith("home"):
                continue
            _, _, value = line.partition("=")
            home = Path(value.strip())
            derived = _root_from_interpreter_home(home)
            if derived is not None:
                candidates.append(derived)
            break
    bin_dir = _venv_bin_dir(venv_dir, host)
    virtual_env = re.compile(r"""VIRTUAL_ENV\s*=\s*["']?([^"'\r\n]+)""")
    if bin_dir.is_dir():
        for entry in sorted(bin_dir.iterdir()):
            if not entry.is_file():
                continue
            head = _read_head(entry)
            if head.startswith("#!"):
                first = head.splitlines()[0][2:].strip()
                if not first:
                    continue
                target = Path(first.split()[0])
                relative_parts = entry.relative_to(venv_dir).parts
                parents = target.parents
                index = len(relative_parts) - 1
                if index < len(parents):
                    old_venv = parents[index]
                    candidates.append(str(old_venv.parent))
            else:
                match = virtual_env.search(head)
                if match:
                    old_venv = Path(match.group(1).strip())
                    candidates.append(str(old_venv.parent))
    result: list[str] = []
    for candidate in candidates:
        candidate = candidate.rstrip("/\\")
        if not candidate or candidate in result:
            continue
        if not (candidate.startswith("/") or re.match(r"^[A-Za-z]:", candidate)):
            continue
        path = Path(candidate)
        if path.parent == path or len(path.parts) < 2:
            # Never treat a filesystem root (or a bare system prefix) as a bundle root.
            continue
        if candidate not in result:
            result.append(candidate)
    return _drop_ancestor_duplicates(result)


def _default_runner(argv: list[str]) -> tuple[int, str]:
    import subprocess

    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return completed.returncode, ((completed.stdout or "") + (completed.stderr or "")).strip()


def _backup_file(path: Path, root: Path, backup_dir: Path) -> str:
    relative = path.relative_to(root).as_posix()
    target = backup_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return relative


def _atomic_write(path: Path, content: str) -> None:
    try:
        mode = path.stat().st_mode
    except OSError:
        mode = None
    temp = path.with_name(f".{path.name}.venv-relocate.tmp")
    try:
        temp.write_text(content, encoding="utf-8", errors="surrogateescape", newline="")
        if mode is not None:
            try:
                temp.chmod(mode & 0o7777)
            except OSError:  # pragma: no cover
                pass
        os.replace(temp, path)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:  # pragma: no cover
                pass


def _rewrite_file(path: Path, roots: list[str], current_root: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return False
    rewritten = _replace_root(text, roots, current_root)
    if rewritten == text:
        return False
    _atomic_write(path, rewritten)
    return True


def repair_venv(
    venv: str | Path,
    *,
    old_roots: Iterable[str],
    current_root: str | Path,
    dry_run: bool = False,
    rebuild: bool = False,
    python_executable: str | Path | None = None,
    runner: _Runner | None = None,
    backup_dir: str | Path | None = None,
    host_os: str | None = None,
) -> VenvRepair:
    """Rewrite stale venv paths; optionally rebuild when repair cannot succeed."""
    venv_dir = Path(venv)
    host = host_os or host_os_name()
    roots = [str(root).rstrip("/\\") for root in old_roots]
    current = str(current_root).rstrip("/\\")
    inspection = inspect_venv(venv_dir, old_roots=roots, current_root=current, host_os=host)
    if not inspection.exists:
        return VenvRepair(venv=venv_dir, status="missing")
    if not inspection.reasons:
        return VenvRepair(venv=venv_dir, status="ok")

    root_dir = venv_dir.parent
    planned: list[Path] = []
    if inspection.stale_home and (venv_dir / "pyvenv.cfg").is_file():
        planned.append(venv_dir / "pyvenv.cfg")
    for relative in inspection.stale_shebangs + inspection.stale_activate:
        planned.append(venv_dir / relative)

    if dry_run:
        return VenvRepair(
            venv=venv_dir,
            status="dry-run",
            reasons=inspection.reasons,
            rewritten=[path.relative_to(root_dir).as_posix() for path in planned],
            dry_run=True,
        )

    rewritten: list[str] = []
    backups: list[str] = []
    backup_root = Path(backup_dir) if backup_dir is not None else None
    for path in planned:
        if not path.is_file():
            continue
        if backup_root is not None:
            try:
                backups.append(_backup_file(path, root_dir, backup_root))
            except OSError:  # pragma: no cover - best effort
                pass
        if _rewrite_file(path, roots, current):
            rewritten.append(path.relative_to(root_dir).as_posix())

    after = inspect_venv(venv_dir, old_roots=roots, current_root=current, host_os=host)
    warnings: list[str] = []
    if not after.reasons:
        return VenvRepair(
            venv=venv_dir,
            status="repaired" if rewritten else "ok",
            reasons=inspection.reasons,
            rewritten=rewritten,
            backups=backups,
            warnings=warnings,
        )
    if not rebuild:
        warnings.append(
            "Virtualenv still references an old root after rewriting: "
            + "; ".join(after.reasons)
            + " Rebuild it with 'avdfleet portable relocate --rebuild-venv'."
        )
        return VenvRepair(
            venv=venv_dir,
            status="unresolved",
            reasons=after.reasons,
            rewritten=rewritten,
            backups=backups,
            warnings=warnings,
        )

    executable = Path(python_executable) if python_executable else (
        venv_dir / ("Scripts/python.exe" if host == "windows" else "bin/python")
    )
    runner = runner or _default_runner
    code, output = runner([str(executable), "-m", "venv", "--clear", str(venv_dir)])
    if code != 0:
        warnings.append(f"Virtualenv rebuild failed ({code}): {output}")
        return VenvRepair(
            venv=venv_dir,
            status="unresolved",
            reasons=after.reasons,
            rewritten=rewritten,
            backups=backups,
            warnings=warnings,
        )
    warnings.append(
        "Virtualenv was rebuilt; reinstall avdfleet/Appium into it from the offline cache "
        "(the wheel cache is kept under cache/downloads)."
    )
    return VenvRepair(
        venv=venv_dir,
        status="rebuilt",
        reasons=after.reasons,
        rewritten=rewritten,
        backups=backups,
        rebuilt=True,
        warnings=warnings,
    )


__all__ = [
    "VenvInspection",
    "VenvRepair",
    "discover_venv_old_roots",
    "inspect_venv",
    "repair_venv",
]
