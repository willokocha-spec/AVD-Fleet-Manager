"""Path rewriting for relocated portable bundles.

A bundle records absolute paths in several places: the AVD ``.ini`` files
(``path=``/``path.rel=``), each ``config.ini`` (``image.sysdir.1``, ``skin.path``,
``snapshot.path``, ``disk.dataPartition.path``...), the bundled daemon config,
launcher scripts that may contain a baked root and the virtualenv (shebangs and
``pyvenv.cfg``).  :func:`relocate_bundle` rewrites all of them after the bundle
is moved, backs up every file first, is idempotent, supports ``dry_run`` and
invalidates snapshots that still reference the old root.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from avdfleet.core.models import sha256_file
from avdfleet.portable.env.builder import (
    build_environment,
    recorded_old_roots,
    save_root_state,
)
from avdfleet.portable.env.model import MANIFEST_FORMAT_VERSION, PortableEnvironment

from .snapshots import SnapshotInvalidation, invalidate_stale_snapshots
from .venv import VenvRepair, discover_venv_old_roots
from .venv import repair_venv as repair_virtualenv

#: Keys in ``<avd>.avd/config.ini`` that commonly hold absolute paths.
AVD_CONFIG_PATH_KEYS: tuple[str, ...] = (
    "image.sysdir.1",
    "image.sysdir.2",
    "skin.path",
    "snapshot.path",
    "disk.dataPartition.path",
    "disk.dataPartition.size",
    "disk.systemPartition.path",
    "disk.cachePartition.path",
    "disk.sdcard.path",
    "disk.sdcard.size",
    "hw.sdCard.path",
    "hw.keyboard.path",
    "test.sysimg.path",
    "path",
)

#: Relative config files that may embed the bundle root.
_CONFIG_SUFFIXES = (".toml", ".json", ".properties", ".vmoptions", ".ini", ".cfg")
_LAUNCHER_NAMES = ("AVD Fleet.cmd", "AVD Fleet.command", "avdfleet.sh")

# Regex matching one path separator in its plain, escaped or POSIX spelling.
_SEP_PATTERN = r"(?:\\\\|\\|/)"
_BEFORE_ROOT = r"(?<![A-Za-z0-9_])"
_AFTER_ROOT = r"(?=[\\/\"'\s,;:)\]]|$)"

_INI_LINE = re.compile(
    r"^(?P<indent>\s*)(?P<key>[A-Za-z0-9_.-]+)(?P<sep>\s*=\s*)(?P<value>.*?)(?P<eol>\r?)$"
)
_DRIVE_ROOT = re.compile(r"^[A-Za-z]:")


def utc_stamp(when: datetime | None = None) -> str:
    return (when or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")


def _windowsify(value: str) -> str:
    return value.replace("/", "\\")


def _posixify(value: str) -> str:
    return value.replace("\\", "/")


def _escapedify(value: str) -> str:
    return _windowsify(value).replace("\\", "\\\\")


def _flex_root_source(value: str) -> str:
    """Build a regex source matching *value* with any separator spelling."""
    tokens: list[str | None] = []
    buffer: list[str] = []
    for char in value:
        if char in "/\\":
            if buffer:
                tokens.append("".join(buffer))
                buffer = []
            if not tokens or tokens[-1] is not None:
                tokens.append(None)
        else:
            buffer.append(char)
    if buffer:
        tokens.append("".join(buffer))
    pieces: list[str] = []
    for token in tokens:
        if token is None:
            pieces.append(_SEP_PATTERN)
        else:
            pieces.append(re.escape(token))
    return "".join(pieces)


def _detect_case_insensitive(old_roots: Iterable[str], explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    return any(_DRIVE_ROOT.match(root) or "\\" in root for root in old_roots)


@dataclass(frozen=True)
class TextRewrite:
    """Result of rewriting one string/configuration body."""

    content: str
    changed: bool = False
    replaced: tuple[str, ...] = ()

    @property
    def replacements(self) -> int:
        return len(self.replaced)


@dataclass(frozen=True)
class RewriteChange:
    path: str
    key: str
    before: str
    after: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "key": self.key, "before": self.before, "after": self.after}


@dataclass(frozen=True)
class IniRewrite:
    """Result of rewriting an ini-style file (AVD ini or ``config.ini``)."""

    content: str
    changes: tuple[RewriteChange, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.changes)


class RootReplacer:
    """Replaces every recorded bundle root with the current root in text."""

    def __init__(
        self,
        old_roots: Iterable[str],
        new_root: str | Path,
        *,
        case_insensitive: bool | None = None,
    ) -> None:
        roots: list[str] = []
        for root in old_roots:
            value = str(root).strip().rstrip("/\\")
            if value and value not in roots:
                roots.append(value)
        self.old_roots: tuple[str, ...] = tuple(roots)
        self.new_root = str(new_root).strip().rstrip("/\\")
        self.case_insensitive = _detect_case_insensitive(self.old_roots, case_insensitive)
        flags = re.IGNORECASE if self.case_insensitive else 0
        self._patterns: list[tuple[str, re.Pattern[str]]] = []
        for root in self.old_roots:
            source = f"{_BEFORE_ROOT}(?:{_flex_root_source(root)}){_AFTER_ROOT}"
            self._patterns.append((root, re.compile(source, flags)))

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _matched_style(matched: str) -> str:
        if "\\\\" in matched:
            return "escaped"
        if "\\" in matched:
            return "windows"
        return "posix"

    def _render_root(self, style: str) -> str:
        if style == "escaped":
            return _escapedify(self.new_root)
        if os.name == "nt" and style == "windows":
            return _windowsify(self.new_root)
        # A POSIX bundle must contain POSIX absolute paths even if the recorded
        # old root used Windows separators (for example a bundle prepared on a
        # different host).  Windows keeps its native/escaped spelling.
        return _posixify(self.new_root)

    def matches(self, text: str) -> bool:
        return any(pattern.search(text) for _, pattern in self._patterns)

    # -- main ---------------------------------------------------------------
    def rewrite(self, text: str) -> TextRewrite:
        replaced: list[str] = []
        result = text
        for old_root, pattern in self._patterns:
            if not pattern.search(result):
                continue

            def replace(
                match: re.Match[str],
                _old: str = old_root,
            ) -> str:
                replacement = self._render_root(self._matched_style(match.group(0)))
                return replacement

            count = len(pattern.findall(result))
            result = pattern.sub(replace, result)
            if count:
                replaced.append(old_root)
        return TextRewrite(content=result, changed=result != text, replaced=tuple(replaced))


def rewrite_text_roots(
    content: str,
    old_roots: Iterable[str],
    new_root: str | Path,
    *,
    case_insensitive: bool | None = None,
) -> TextRewrite:
    return RootReplacer(old_roots, new_root, case_insensitive=case_insensitive).rewrite(content)


def rewrite_config_toml(
    content: str,
    *,
    old_roots: Iterable[str],
    new_root: str | Path,
    case_insensitive: bool | None = None,
) -> TextRewrite:
    return rewrite_text_roots(content, old_roots, new_root, case_insensitive=case_insensitive)


def rewrite_launcher_working_dir(
    content: str,
    *,
    old_roots: Iterable[str],
    new_root: str | Path,
) -> TextRewrite:
    """Rewrite baked absolute launcher working directories.

    Generated launchers derive the root from their own location, so they are
    relocation-safe by construction; this handles hand-edited or older launchers
    that contain an absolute ``cd``/``pushd`` target.
    """
    return rewrite_text_roots(content, old_roots, new_root, case_insensitive=None)


def _join_style(base: str, parts: Sequence[str], style: str) -> str:
    text = str(base).rstrip("/\\")
    if style in {"windows", "escaped"}:
        text = _windowsify(text)
        separator = "\\"
    else:
        text = _posixify(text)
        separator = "/"
    for part in parts:
        text = text + separator + part
    if style == "escaped":
        text = text.replace("\\", "\\\\")
    return text


def _path_style(value: str) -> str:
    if "\\\\" in value:
        return "escaped"
    if "\\" in value:
        return "windows"
    return "posix"


def _bare_style(value: str) -> str:
    """Separator style for an unescaped ini/toml value (posix or windows)."""
    return "windows" if "\\" in value else "posix"


def _iter_ini_lines(content: str) -> list[str]:
    return content.splitlines(keepends=True)


def _map_ini_lines(
    content: str,
    mapper: Callable[[str, str], str | None],
) -> tuple[str, list[tuple[str, str, str]]]:
    """Apply *mapper(key, value)* per ini line, returning new content and changes."""
    changes: list[tuple[str, str, str]] = []
    output: list[str] = []
    for raw_line in _iter_ini_lines(content):
        newline = ""
        line = raw_line
        if line.endswith("\r\n"):
            line, newline = line[:-2], "\r\n"
        elif line.endswith("\n"):
            line, newline = line[:-1], "\n"
        match = _INI_LINE.match(line)
        if not match:
            output.append(raw_line)
            continue
        key = match.group("key")
        value = match.group("value")
        transformed = mapper(key, value)
        if transformed is None or transformed == value:
            output.append(raw_line)
            continue
        changes.append((key, value, transformed))
        output.append(
            f"{match.group('indent')}{key}{match.group('sep')}{transformed}{match.group('eol')}{newline}"
        )
    return "".join(output), changes


def rewrite_avd_ini(
    content: str,
    *,
    old_roots: Iterable[str],
    new_root: str | Path,
    avd_name: str | None = None,
    avd_home: str | Path | None = None,
    rewrite_absolute: bool = True,
) -> IniRewrite:
    """Rewrite an AVD ``<name>.ini`` file (``path=`` and ``path.rel=``)."""
    roots = tuple(str(root) for root in old_roots)
    new_root_text = str(new_root).rstrip("/\\")
    replacer = RootReplacer(roots, new_root_text)
    new_avd_home = str(avd_home).rstrip("/\\") if avd_home is not None else None
    changes: list[RewriteChange] = []

    def value_for(key: str, value: str) -> str | None:
        stripped = value.strip()
        if not stripped:
            return None
        absolute = bool(_DRIVE_ROOT.match(stripped) or stripped.startswith("/") or stripped.startswith("\\\\"))
        if key == "path.rel":
            dir_name = Path(_posixify(stripped)).name or (f"{avd_name}.avd" if avd_name else "")
            if not dir_name:
                return None
            target = f"avd/{dir_name}"
            if stripped != target:
                changes.append(RewriteChange("", "path.rel", stripped, target))
                return target
            return None
        if key == "path":
            if not absolute:
                return None
            dir_name = Path(_posixify(stripped).rstrip("/")).name or (f"{avd_name}.avd" if avd_name else "")
            if not dir_name:
                return None
            style = _path_style(stripped)
            base = new_avd_home if new_avd_home is not None else new_root_text
            parts = [dir_name] if new_avd_home is not None else ["avd", dir_name]
            target = _join_style(base, parts, style)
            if target != stripped:
                changes.append(RewriteChange("", "path", stripped, target))
                return target
            return None
        if rewrite_absolute and replacer.matches(stripped):
            result = replacer.rewrite(stripped)
            if result.changed:
                changes.append(RewriteChange("", key, stripped, result.content))
                return result.content
        return None

    rewritten, raw_changes = _map_ini_lines(content, value_for)
    if not changes:
        # ``_map_ini_lines`` already records the path/path.rel changes.
        changes = [RewriteChange("", key, before, after) for key, before, after in raw_changes]
    return IniRewrite(content=rewritten, changes=tuple(changes))


def rewrite_avd_config_ini(
    content: str,
    *,
    old_roots: Iterable[str],
    new_root: str | Path,
) -> IniRewrite:
    """Rewrite absolute paths in ``<avd>.avd/config.ini``."""
    replacer = RootReplacer(old_roots, new_root)
    changes: list[RewriteChange] = []

    def value_for(key: str, value: str) -> str | None:
        stripped = value.strip()
        if not stripped:
            return None
        if not replacer.matches(stripped):
            return None
        result = replacer.rewrite(stripped)
        if not result.changed:
            return None
        changes.append(RewriteChange("", key, stripped, result.content))
        return result.content

    rewritten, _ = _map_ini_lines(content, value_for)
    return IniRewrite(content=rewritten, changes=tuple(changes))


# ---------------------------------------------------------------------------
# Bundle-wide relocation
# ---------------------------------------------------------------------------


@dataclass
class RelocateReport:
    """Everything relocation changed (or would change in dry-run mode)."""

    root: Path
    old_roots: list[str] = field(default_factory=list)
    dry_run: bool = False
    changes: list[RewriteChange] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    cleaned: list[str] = field(default_factory=list)
    venv: VenvRepair | None = None
    snapshots: list[SnapshotInvalidation] = field(default_factory=list)
    manifest_updated: bool = False

    @property
    def changed_files(self) -> int:
        return len({change.path for change in self.changes})

    @property
    def changed_paths(self) -> list[str]:
        return sorted({change.path for change in self.changes})

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "old_roots": list(self.old_roots),
            "dry_run": self.dry_run,
            "changed_files": self.changed_files,
            "changed_paths": self.changed_paths,
            "changes": [change.to_dict() for change in self.changes],
            "backups": list(self.backups),
            "warnings": list(self.warnings),
            "stale": list(self.stale),
            "cleaned": list(self.cleaned),
            "venv": self.venv.to_dict() if self.venv is not None else None,
            "snapshots": [snapshot.to_dict() for snapshot in self.snapshots],
            "manifest_updated": self.manifest_updated,
        }


def _relative_posix(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _targets(environment: PortableEnvironment) -> list[tuple[str, Path]]:
    """Return ``(kind, path)`` pairs that relocation may rewrite, deterministically."""
    paths = environment.paths
    root = paths.root
    targets: list[tuple[str, Path]] = [(kind, path) for kind, path in _config_targets(paths.config)]
    avd_dir = paths.avd
    if avd_dir.is_dir():
        for entry in sorted(avd_dir.glob("*.ini")):
            targets.append(("avd_ini", entry))
        for entry in sorted(avd_dir.glob("*.avd/config.ini")):
            targets.append(("avd_config", entry))
        for entry in sorted(avd_dir.glob("*.avd/*.ini")):
            # hardware-qemu.ini is regenerated by the emulator and is removed by
            # the relocation cleanup below; rewriting it would be wasted work.
            if entry.name not in {"config.ini", "hardware-qemu.ini"}:
                targets.append(("avd_config", entry))
    for name in _LAUNCHER_NAMES:
        candidate = root / name
        if candidate.is_file():
            targets.append(("launcher", candidate))
    studio_dir = paths.studio
    if studio_dir.is_dir():
        for entry in sorted(studio_dir.iterdir()):
            if entry.is_file() and entry.suffix.lower() in _CONFIG_SUFFIXES:
                targets.append(('config', entry))
    launchers_dir = paths.launchers
    if launchers_dir.is_dir():
        for entry in sorted(launchers_dir.iterdir()):
            if entry.is_file() and entry not in {path for _, path in targets}:
                targets.append(("launcher", entry))
    return targets


def _config_targets(config_dir: Path) -> list[tuple[str, Path]]:
    if not config_dir.is_dir():
        return []
    targets: list[tuple[str, Path]] = []
    for entry in sorted(config_dir.iterdir()):
        if not entry.is_file():
            continue
        if entry.name in {"portable-state.json"}:
            continue
        if entry.suffix.lower() not in _CONFIG_SUFFIXES:
            continue
        kind = "toml" if entry.suffix.lower() == ".toml" else "config"
        targets.append((kind, entry))
    return targets


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:4096]


def _read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if _looks_binary(data):
        return None
    return data.decode("utf-8", errors="surrogateescape")


def _atomic_write(path: Path, content: str) -> None:
    original_mode: int | None = None
    try:
        original_mode = path.stat().st_mode
    except OSError:
        pass
    temp = path.with_name(f".{path.name}.{os.getpid()}.relocate.tmp")
    try:
        temp.write_text(content, encoding="utf-8", errors="surrogateescape", newline="")
        if original_mode is not None:
            try:
                temp.chmod(original_mode & 0o7777)
            except OSError:  # pragma: no cover - filesystems without permissions
                pass
        os.replace(temp, path)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:  # pragma: no cover - best effort cleanup
                pass


def _backup(path: Path, root: Path, backup_dir: Path) -> str:
    relative = _relative_posix(path, root)
    target = backup_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return _relative_posix(target, root)


def _rewrite_target(
    kind: str,
    path: Path,
    replacer: RootReplacer,
    root: Path,
    *,
    avd_name: str | None = None,
) -> list[RewriteChange]:
    text = _read_text(path)
    if text is None:
        return []
    relative = _relative_posix(path, root)
    if kind == "avd_ini":
        ini = rewrite_avd_ini(text, old_roots=replacer.old_roots, new_root=root, avd_name=avd_name)
        return [RewriteChange(relative, change.key, change.before, change.after) for change in ini.changes]
    if kind == "avd_config":
        ini = rewrite_avd_config_ini(text, old_roots=replacer.old_roots, new_root=root)
        return [RewriteChange(relative, change.key, change.before, change.after) for change in ini.changes]
    result = replacer.rewrite(text)
    if not result.changed:
        return []
    return [RewriteChange(relative, "path", old, str(root)) for old in result.replaced]


def relocate_bundle(
    root: str | Path,
    *,
    environment: PortableEnvironment | None = None,
    old_roots: Sequence[str] | None = None,
    dry_run: bool = False,
    backup: bool = True,
    repair_venv: bool = True,
    rebuild_venv: bool = False,
    invalidate_snapshots: bool = True,
    update_manifest: bool = True,
    clean_emulator_state: bool = True,
) -> RelocateReport:
    """Rewrite all recorded absolute paths to the current bundle root.

    Backs up every rewritten file under ``config/relocation-backups/<utc>/``,
    invalidates snapshots containing old roots, repairs the virtualenv and
    re-baselines the manifest checksums.  Idempotent: running it twice changes
    nothing the second time.
    """
    root_path = Path(root).expanduser().resolve()
    environment = environment if environment is not None else build_environment(root_path)
    roots = [str(item) for item in (old_roots if old_roots is not None else recorded_old_roots(environment))]
    if repair_venv and environment.paths.venv.is_dir():
        for discovered in discover_venv_old_roots(environment.paths.venv, host_os=environment.host_os):
            if discovered not in roots:
                roots.append(discovered)
    current = str(root_path)
    roots = [item for item in roots if item and _norm_path(item) != _norm_path(current)]
    report = RelocateReport(root=root_path, old_roots=roots, dry_run=dry_run)
    if not roots:
        if not dry_run:
            save_root_state(environment)
        return report

    replacer = RootReplacer(roots, root_path)
    backup_dir = root_path / "config" / "relocation-backups" / utc_stamp()
    for kind, target in _targets(environment):
        if not target.is_file():
            continue
        avd_name = target.parent.name.removesuffix(".avd") if kind == "avd_config" else None
        if kind == "avd_ini":
            avd_name = target.stem
        changes = _rewrite_target(kind, target, replacer, root_path, avd_name=avd_name)
        if not changes:
            continue
        new_text = None
        if kind in {"avd_ini", "avd_config"}:
            raw = _read_text(target)
            if raw is None:
                continue
            ini = (
                rewrite_avd_ini(raw, old_roots=roots, new_root=root_path, avd_name=avd_name)
                if kind == "avd_ini"
                else rewrite_avd_config_ini(raw, old_roots=roots, new_root=root_path)
            )
            new_text = ini.content
        elif kind == "launcher":
            text = _read_text(target)
            if text is None:
                continue
            new_text = rewrite_launcher_working_dir(
                text, old_roots=roots, new_root=root_path
            ).content
        else:
            text = _read_text(target)
            if text is None:
                continue
            new_text = rewrite_config_toml(text, old_roots=roots, new_root=root_path).content
        report.changes.extend(changes)
        if dry_run:
            continue
        if backup:
            report.backups.append(_backup(target, root_path, backup_dir))
        _atomic_write(target, new_text)

    if repair_venv:
        venv_dir = environment.paths.venv
        if venv_dir.is_dir():
            repair = repair_virtualenv(
                venv_dir,
                old_roots=roots,
                current_root=root_path,
                dry_run=dry_run,
                rebuild=rebuild_venv,
                backup_dir=None if dry_run or not backup else backup_dir,
            )
            report.venv = repair
            for rewritten in repair.rewritten:
                report.changes.append(
                    RewriteChange(rewritten, "venv", "", str(root_path))
                )
            report.warnings.extend(repair.warnings)

    if invalidate_snapshots:
        report.snapshots = invalidate_stale_snapshots(
            environment.paths.avd, roots, dry_run=dry_run
        )

    if clean_emulator_state:
        _clean_generated_emulator_state(
            environment,
            roots,
            report,
            dry_run=dry_run,
            backup_dir=None if dry_run or not backup else backup_dir,
        )

    if not dry_run:
        if update_manifest:
            report.manifest_updated = _update_manifest(root_path, environment, report)
        save_root_state(environment)
        report.stale = find_stale_roots(root_path, roots, environment=environment)
        if report.stale:
            report.warnings.append(
                "Some files still reference the old bundle root after relocation: "
                + ", ".join(report.stale)
            )
    return report


def _clean_generated_emulator_state(
    environment: PortableEnvironment,
    roots: Sequence[str],
    report: RelocateReport,
    *,
    dry_run: bool,
    backup_dir: Path | None,
) -> None:
    """Remove regenerated emulator state that embeds host paths.

    `hardware-qemu.ini` is rewritten by the emulator on every launch and lock
    files are meaningless after a move; both can still contain the old root.  The
    files are backed up when backups are enabled and removed otherwise, never the
    AVD directory or user data.
    """
    root = environment.paths.root
    candidates: list[Path] = []
    for avd_dir in sorted(environment.paths.avd.glob("*.avd")):
        hardware = avd_dir / "hardware-qemu.ini"
        if hardware.is_file():
            candidates.append(hardware)
        candidates.extend(sorted(avd_dir.glob("*.lock")))
    for path in candidates:
        relative = _relative_posix(path, root)
        report.cleaned.append(relative)
        if dry_run:
            continue
        if backup_dir is not None:
            report.backups.append(_backup(path, root, backup_dir))
        try:
            path.unlink()
        except OSError as exc:  # pragma: no cover - transient lock
            report.warnings.append(f"Could not remove stale emulator state {relative}: {exc}")
    if report.cleaned:
        report.warnings.append(
            "Removed regenerated emulator state (hardware-qemu.ini/*.lock): "
            + ", ".join(report.cleaned)
        )


def _norm_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(value))


def _update_manifest(root: Path, environment: PortableEnvironment, report: RelocateReport) -> bool:
    marker = environment.paths.marker
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        report.warnings.append("Could not read the bundle manifest; checksums were not re-baselined.")
        return False
    if not isinstance(payload, dict):
        return False
    previous_root = payload.get("root") or ""
    payload["root"] = str(root)
    payload["format_version"] = payload.get("format_version") or MANIFEST_FORMAT_VERSION
    warnings = payload.get("warnings")
    if not isinstance(warnings, list):
        warnings = []
    warnings.append(
        f"relocated from {previous_root!r} to {str(root)!r} at {datetime.now(UTC).isoformat()}"
    )
    payload["warnings"] = warnings
    entries = payload.get("entries")
    if isinstance(entries, list):
        entry_map = {
            entry.get("path"): entry
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        }
        for changed_path in sorted({change.path for change in report.changes}):
            entry = entry_map.get(changed_path)
            if entry is None:
                continue
            file_path = root / changed_path
            if not file_path.is_file():
                continue
            try:
                entry["sha256"] = sha256_file(str(file_path))
                entry["size"] = file_path.stat().st_size
                if os.name != "nt":
                    entry["executable"] = bool(file_path.stat().st_mode & 0o111)
            except OSError:  # pragma: no cover - file disappeared mid-relocation
                continue
    temp = marker.with_name(f".{marker.name}.relocate.tmp")
    try:
        temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, marker)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:  # pragma: no cover
                pass
    return True


def find_stale_roots(
    root: str | Path,
    old_roots: Sequence[str],
    *,
    environment: PortableEnvironment | None = None,
) -> list[str]:
    """Return the relative paths still containing one of *old_roots*."""
    root_path = Path(root).expanduser().resolve()
    environment = environment if environment is not None else build_environment(root_path)
    replacer = RootReplacer(old_roots, root_path)
    stale: list[str] = []
    for _kind, target in _targets(environment):
        if not target.is_file():
            continue
        text = _read_text(target)
        if text is None:
            continue
        if replacer.matches(text):
            stale.append(_relative_posix(target, root_path))
    return stale


def rewrite_environment_config(
    content: str,
    *,
    old_roots: Iterable[str],
    new_root: str | Path,
) -> TextRewrite:
    """Alias used for bundled daemon config files."""
    return rewrite_config_toml(content, old_roots=old_roots, new_root=new_root)


__all__ = [
    "AVD_CONFIG_PATH_KEYS",
    "IniRewrite",
    "RelocateReport",
    "RewriteChange",
    "RootReplacer",
    "TextRewrite",
    "find_stale_roots",
    "relocate_bundle",
    "rewrite_avd_config_ini",
    "rewrite_avd_ini",
    "rewrite_config_toml",
    "rewrite_environment_config",
    "rewrite_launcher_working_dir",
    "rewrite_text_roots",
    "utc_stamp",
]
