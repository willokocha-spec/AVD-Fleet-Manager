"""AVD Fleet installer: engine, ordered steps and per-platform providers.

Public entry points used by the CLI/container bootstrap::

    from avdfleet.platform.installer import InstallerEngine, get_provider

    engine = InstallerEngine(config, paths)          # detects the provider
    plan = engine.plan(InstallOptions(dry_run=True))
    try:
        manifest = engine.install(InstallOptions(accept_android_sdk_licenses=True, yes=True))
    except RebootRequiredError:
        sys.exit(3010)                               # reboot, then engine.resume()
    except InstallerError:
        sys.exit(1)

``--only`` accepts stable step ids (see :data:`STEP_IDS`), phases or aliases and
automatically pulls in the selected steps' dependency closure.
"""

from __future__ import annotations

from avdfleet.core.models import InstallOptions

from .context import CommandResult, CommandRunner, InstallContext
from .engine import REBOOT_REQUIRED_EXIT_CODE, InstallerEngine, ProgressCallback, create_engine
from .manifest import (
    MANIFEST_FILENAME,
    MANIFEST_VERSION,
    ManifestStore,
    atomic_write_json,
    first_incomplete_step,
    is_complete,
    new_manifest,
    pending_step,
    summarize,
)
from .providers import (
    PROVIDER_ALIASES,
    PROVIDER_IDS,
    PROVIDERS,
    create_provider,
    detect_provider_id,
    get_provider,
    provider_class,
    provider_ids,
    resolve_provider_name,
)
from .providers.base import (
    KVM_UDEV_RULE,
    KVM_UDEV_RULE_PATH,
    REQUIRED_TOOL_TEMPLATES,
    Command,
    EnvScope,
    InstallProvider,
    ToolTemplate,
    VirtualizationPlan,
)
from .steps import (
    PHASES,
    STEP_ALIASES,
    STEP_CLASSES,
    STEP_IDS,
    BaseStep,
    all_step_specs,
    build_steps,
    get_step_class,
    resolve_selection,
    step_ids,
)

__all__ = [
    "KVM_UDEV_RULE",
    "KVM_UDEV_RULE_PATH",
    "MANIFEST_FILENAME",
    "MANIFEST_VERSION",
    "PHASES",
    "PROVIDERS",
    "PROVIDER_ALIASES",
    "PROVIDER_IDS",
    "REBOOT_REQUIRED_EXIT_CODE",
    "REQUIRED_TOOL_TEMPLATES",
    "STEP_ALIASES",
    "STEP_CLASSES",
    "STEP_IDS",
    "BaseStep",
    "Command",
    "CommandResult",
    "CommandRunner",
    "EnvScope",
    "InstallContext",
    "InstallOptions",
    "InstallProvider",
    "InstallerEngine",
    "ManifestStore",
    "ProgressCallback",
    "ToolTemplate",
    "VirtualizationPlan",
    "all_step_specs",
    "atomic_write_json",
    "build_steps",
    "create_engine",
    "create_provider",
    "detect_provider_id",
    "first_incomplete_step",
    "get_provider",
    "get_step_class",
    "is_complete",
    "new_manifest",
    "pending_step",
    "provider_class",
    "provider_ids",
    "resolve_provider_name",
    "resolve_selection",
    "step_ids",
    "summarize",
]
