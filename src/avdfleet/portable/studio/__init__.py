"""Portable Android Studio configuration and documented leftovers."""

from __future__ import annotations

from .config import (
    DEFAULT_STUDIO_HEAP_MB,
    STUDIO_LEFTOVERS,
    STUDIO_PROPERTIES_FILENAME,
    STUDIO_VMOPTIONS_FILENAME,
    StudioConfigResult,
    StudioLeftover,
    StudioPaths,
    build_studio_properties,
    build_studio_vmoptions,
    leftover_documentation,
    studio_env,
    studio_leftovers,
    write_studio_config,
)

__all__ = [
    "DEFAULT_STUDIO_HEAP_MB",
    "STUDIO_LEFTOVERS",
    "STUDIO_PROPERTIES_FILENAME",
    "STUDIO_VMOPTIONS_FILENAME",
    "StudioConfigResult",
    "StudioLeftover",
    "StudioPaths",
    "build_studio_properties",
    "build_studio_vmoptions",
    "leftover_documentation",
    "studio_env",
    "studio_leftovers",
    "write_studio_config",
]
