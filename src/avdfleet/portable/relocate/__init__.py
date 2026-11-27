"""Portable bundle relocation.

Rewrite every recorded absolute path after a move, repair the virtualenv,
invalidate stale snapshots and verify that no old root remains.
"""

from __future__ import annotations

from .rewriter import (
    AVD_CONFIG_PATH_KEYS,
    IniRewrite,
    RelocateReport,
    RewriteChange,
    RootReplacer,
    TextRewrite,
    find_stale_roots,
    relocate_bundle,
    rewrite_avd_config_ini,
    rewrite_avd_ini,
    rewrite_config_toml,
    rewrite_environment_config,
    rewrite_launcher_working_dir,
    rewrite_text_roots,
    utc_stamp,
)
from .snapshots import (
    SnapshotInvalidation,
    SnapshotRef,
    find_snapshots,
    invalidate_stale_snapshots,
    snapshot_root_reference,
)
from .venv import (
    VenvInspection,
    VenvRepair,
    discover_venv_old_roots,
    inspect_venv,
    repair_venv,
)

__all__ = [
    "AVD_CONFIG_PATH_KEYS",
    "IniRewrite",
    "RelocateReport",
    "RewriteChange",
    "RootReplacer",
    "SnapshotInvalidation",
    "SnapshotRef",
    "TextRewrite",
    "VenvInspection",
    "VenvRepair",
    "discover_venv_old_roots",
    "find_snapshots",
    "find_stale_roots",
    "inspect_venv",
    "invalidate_stale_snapshots",
    "relocate_bundle",
    "repair_venv",
    "rewrite_avd_config_ini",
    "rewrite_avd_ini",
    "rewrite_config_toml",
    "rewrite_environment_config",
    "rewrite_launcher_working_dir",
    "rewrite_text_roots",
    "snapshot_root_reference",
    "utc_stamp",
]
