"""Portable edition: self-contained bundles that survive relocation.

Sub-packages:

``env``        bundle root marker, layout, child-process environment, root-change detection
``launchers``  generated Windows/macOS/Linux launchers + the bundled Python launch helper
``checks``     preflight checks (filesystem, noexec, quarantine, space, write speed, hypervisor)
``relocate``   AVD/config/venv path rewriting and snapshot invalidation
``bundle``     bundle builder, checksum manifest, offline cache resolution, verify/inspect
``studio``     portable Android Studio config (``STUDIO_PROPERTIES``) and documented leftovers

Typical launcher flow::

    env = build_environment(root)
    relocate_bundle(root, environment=env)      # only when the bundle moved
    report = run_preflight(root)
    # avdfleet serve --portable runs with env["env"] exported

Attributes are resolved lazily from the sub-package ``__all__`` lists: importing
``avdfleet.portable`` stays cheap, and ``python -m
avdfleet.portable.launchers.generator`` does not double-import the generator
(runpy warns when a module is imported before ``-m`` executes it).
"""

from __future__ import annotations

import importlib
from typing import Any

_SUBMODULES: tuple[str, ...] = (
    ".env",
    ".checks",
    ".launchers",
    ".relocate",
    ".bundle",
    ".studio",
)


def __getattr__(name: str) -> Any:
    for relative in _SUBMODULES:
        module = importlib.import_module(relative, __name__)
        if name in getattr(module, "__all__", ()):
            value = getattr(module, name)
            globals()[name] = value
            return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    names = set(globals())
    for relative in _SUBMODULES:
        names.update(getattr(importlib.import_module(relative, __name__), "__all__", ()))
    return sorted(names)


__all__ = [
    "DEFAULT_PORT",
    "EXIT_MISSING_RUNTIME",
    "EXIT_OK",
    "EXIT_PREFLIGHT",
    "EXIT_SERVER",
    "EXIT_USAGE",
    "HEALTH_PATH",
    "MANIFEST_FORMAT_VERSION",
    "MARKER_NAME",
    "NON_ISOLATABLE",
    "STATE_FILE_NAME",
    "STUDIO_LEFTOVERS",
    "STUDIO_PROPERTIES_FILENAME",
    "STUDIO_VMOPTIONS_FILENAME",
    "BundleInspection",
    "BundleVerificationError",
    "CheckReport",
    "CheckResult",
    "FileSystemInfo",
    "FileSystemKind",
    "HypervisorInfo",
    "IniRewrite",
    "IsolationNote",
    "LaunchOptions",
    "LaunchPlan",
    "LauncherSpec",
    "PortableBundleBuilder",
    "PortableEnvironment",
    "PortablePaths",
    "PortableState",
    "QuarantineScan",
    "RelocateReport",
    "RewriteChange",
    "RootChange",
    "RootMarker",
    "RootReplacer",
    "RootValidation",
    "SnapshotInvalidation",
    "SnapshotRef",
    "SourcePlan",
    "StudioConfigResult",
    "StudioLeftover",
    "StudioPaths",
    "TextRewrite",
    "VenvInspection",
    "VenvRepair",
    "VerifyIssue",
    "VerifyReport",
    "all_launchers",
    "build_environment",
    "build_launch_plan",
    "build_studio_properties",
    "build_studio_vmoptions",
    "detect_filesystem",
    "detect_hypervisor",
    "detect_root_change",
    "discover_root",
    "ensure_environment_dirs",
    "filesystem_check",
    "find_snapshots",
    "find_stale_roots",
    "format_verify_report",
    "free_space_check",
    "hypervisor_check",
    "inspect_bundle",
    "inspect_venv",
    "invalidate_stale_snapshots",
    "isolation_notes",
    "isolation_summary",
    "launcher_for_host",
    "leftover_documentation",
    "load_root_state",
    "noexec_check",
    "parse_launcher_args",
    "quarantine_check",
    "read_manifest",
    "read_root_marker",
    "recorded_old_roots",
    "relocate_bundle",
    "repair_venv",
    "resolve_sources",
    "rewrite_avd_config_ini",
    "rewrite_avd_ini",
    "rewrite_config_toml",
    "rewrite_launcher_working_dir",
    "rewrite_text_roots",
    "run_launcher",
    "run_preflight",
    "save_root_state",
    "snapshot_root_reference",
    "studio_env",
    "studio_leftovers",
    "validate_launcher",
    "validate_root",
    "verify_bundle",
    "write_launchers",
    "write_manifest",
    "write_speed_check",
    "write_studio_config",
]
