"""Prepared-source and offline-cache resolution for bundle builds.

The bundle builder never downloads anything: it copies a *prepared* tree
(``source_root``) or a populated *offline cache* (``cache_dir``) laid out like
the bundle itself.  ``--offline`` requires the cache to contain every required
component; this module decides what exists, what is missing and whether the
build may proceed, and records that no network access was performed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from avdfleet.core.errors import PortableError
from avdfleet.portable.env.model import host_os_name

__all__ = [
    "COMPONENTS",
    "OPTIONAL_COMPONENTS",
    "REQUIRED_COMPONENTS",
    "SourcePlan",
    "component_present",
    "resolve_sources",
]

#: Component directories understood by the builder (mirrors the bundle layout).
COMPONENTS: tuple[str, ...] = ("python", "lib", "venv", "sdk", "node", "jdk", "ui", "studio")
REQUIRED_COMPONENTS: tuple[str, ...] = ("python", "sdk", "ui")
OPTIONAL_COMPONENTS: tuple[str, ...] = ("lib", "venv", "node", "jdk", "studio")

#: Alternative prepared-tree names (see research/R7: runtime/python, runtime/jre,
#: runtime/app-site-packages).  The builder normalises them into the bundle layout.
_COMPONENT_ALIASES: dict[str, tuple[str, ...]] = {
    "python": ("python", "runtime/python"),
    "jdk": ("jdk", "runtime/jre"),
    "lib": ("lib", "runtime/app-site-packages"),
    "venv": ("venv", "runtime/venv"),
}


def component_present(path: Path) -> bool:
    """A component is usable when it is a non-empty directory."""
    try:
        if not path.is_dir():
            return False
        return any(path.iterdir())
    except OSError:
        return False


@dataclass(frozen=True)
class SourcePlan:
    """Resolved component sources plus everything missing for a build."""

    source_root: Path | None
    cache_dir: Path | None
    host_os: str
    offline: bool
    components: dict[str, Path] = field(default_factory=dict)
    missing_required: list[str] = field(default_factory=list)
    missing_optional: list[str] = field(default_factory=list)
    downloads: Path | None = None
    warnings: list[str] = field(default_factory=list)
    network_used: bool = False

    @property
    def ok(self) -> bool:
        return not self.missing_required

    def require(self) -> SourcePlan:
        """Raise :class:`PortableError` with an actionable message when incomplete."""
        if self.ok:
            return self
        locations: list[str] = []
        if self.source_root is not None:
            locations.append(f"source_root={self.source_root}")
        if self.cache_dir is not None:
            locations.append(f"cache_dir={self.cache_dir}")
        where = ", ".join(locations) if locations else "no source_root or cache_dir was provided"
        hint = (
            "Bundle builds never download runtimes. Prepare a source tree or populate the "
            "offline cache (python/, sdk/, ui/) with 'avdfleet bundle --prepare' first, then "
            "rebuild."
        )
        if self.offline:
            hint = (
                "--offline requires a populated cache with python/, sdk/ and ui/; run the "
                "cache preparation step while online, then rebuild with --offline."
            )
        raise PortableError(
            f"Cannot build a portable bundle: missing required component(s) "
            f"{', '.join(self.missing_required)} ({where})",
            hint=hint,
            details={
                "missing_required": self.missing_required,
                "missing_optional": self.missing_optional,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_root": str(self.source_root) if self.source_root else None,
            "cache_dir": str(self.cache_dir) if self.cache_dir else None,
            "host_os": self.host_os,
            "offline": self.offline,
            "ok": self.ok,
            "components": {name: str(path) for name, path in sorted(self.components.items())},
            "missing_required": list(self.missing_required),
            "missing_optional": list(self.missing_optional),
            "downloads": str(self.downloads) if self.downloads else None,
            "warnings": list(self.warnings),
            "network_used": self.network_used,
        }


def _first_present(*candidates: Path | None) -> Path | None:
    for candidate in candidates:
        if candidate is not None and component_present(candidate):
            return candidate
    return None


def resolve_sources(
    *,
    source_root: str | Path | None = None,
    cache_dir: str | Path | None = None,
    offline: bool = False,
    host_os: str | None = None,
) -> SourcePlan:
    """Resolve each bundle component from the prepared source tree or the cache.

    ``source_root`` wins over ``cache_dir`` for components present in both; the
    cache's ``downloads/`` directory (offline image/package cache) is always taken
    from ``cache_dir`` when present.
    """
    host = host_os or host_os_name()
    source = Path(source_root).expanduser() if source_root is not None else None
    cache = Path(cache_dir).expanduser() if cache_dir is not None else None
    warnings: list[str] = []

    if source is None and cache is None:
        warnings.append("No prepared source_root/cache_dir provided; nothing can be copied.")
    if offline and cache is None:
        warnings.append("--offline requires a cache_dir containing the prepared components.")

    components: dict[str, Path] = {}
    for name in COMPONENTS:
        candidates: list[Path | None] = []
        for alias in _COMPONENT_ALIASES.get(name, (name,)):
            if source is not None:
                candidates.append(source / alias)
            if cache is not None:
                candidates.append(cache / alias)
        found = _first_present(*candidates)
        if found is not None:
            components[name] = found
    missing_required = [name for name in REQUIRED_COMPONENTS if name not in components]
    missing_optional = [name for name in OPTIONAL_COMPONENTS if name not in components]
    downloads = cache / "downloads" if cache is not None and (cache / "downloads").is_dir() else None

    if not offline and missing_required:
        warnings.append(
            "Build is online-capable but no downloader exists in this version: the prepared "
            "sources must already contain the missing components."
        )
    if offline:
        warnings.append("Offline build: no network access is performed by the bundle builder.")
    return SourcePlan(
        source_root=source,
        cache_dir=cache,
        host_os=host,
        offline=offline,
        components=components,
        missing_required=missing_required,
        missing_optional=missing_optional,
        downloads=downloads,
        warnings=warnings,
        network_used=False,
    )
