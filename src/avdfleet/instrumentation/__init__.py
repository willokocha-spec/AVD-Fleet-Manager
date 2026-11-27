"""Root & Instrumentation subsystem.

Public surface:

:class:`~avdfleet.instrumentation.service.InstrumentationService`
    capability scanning, the module library, profiles and deployments.
:class:`~avdfleet.instrumentation.library.ModuleLibrary`
    inert inspection and storage of APK/ZIP artifacts.
:class:`~avdfleet.instrumentation.capabilities.DeviceProber`
    the adb measurement layer.

Nothing here bundles Magisk, LSPosed or rootAVD: those are GPL-licensed and are
supplied by the operator (see ``docs/instrumentation.md``).
"""

from __future__ import annotations

from .acquire import (
    PINNED_ARTIFACTS,
    AcquisitionStore,
    ArtifactStatus,
    PinnedArtifact,
    find_artifact,
)
from .capabilities import DeviceProber, classify_magisk_module, parse_module_prop
from .library import MAX_MODULE_BYTES, ModuleLibrary, find_aapt2, parse_badging
from .models import (
    CapabilityFlags,
    ComponentState,
    DeploymentRun,
    DeviceInstrumentation,
    InstalledModule,
    InstrumentationProfile,
    LogEntry,
    ModuleKind,
    ModuleRecord,
    ProfileStep,
    RootImplementation,
    RunState,
    StepAction,
    StepResult,
    TargetResult,
)
from .profiles import BUILTIN_PROFILES, ensure_builtin_profiles
from .repository import InstrumentationRepository
from .service import DEFAULT_CONCURRENCY, InstrumentationService, TargetDevice

__all__ = [
    "AcquisitionStore",
    "ArtifactStatus",
    "BUILTIN_PROFILES",
    "PINNED_ARTIFACTS",
    "PinnedArtifact",
    "find_artifact",
    "CapabilityFlags",
    "ComponentState",
    "DEFAULT_CONCURRENCY",
    "DeploymentRun",
    "DeviceInstrumentation",
    "DeviceProber",
    "InstalledModule",
    "InstrumentationProfile",
    "InstrumentationRepository",
    "InstrumentationService",
    "LogEntry",
    "MAX_MODULE_BYTES",
    "ModuleKind",
    "ModuleLibrary",
    "ModuleRecord",
    "ProfileStep",
    "RootImplementation",
    "RunState",
    "StepAction",
    "StepResult",
    "TargetDevice",
    "TargetResult",
    "classify_magisk_module",
    "ensure_builtin_profiles",
    "find_aapt2",
    "parse_badging",
    "parse_module_prop",
]
