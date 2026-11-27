"""Typed models for the Root & Instrumentation subsystem.

The subsystem manages three different package formats and two different root
implementations, so the types deliberately keep those separate:

* :class:`ModuleKind` distinguishes an Xposed **APK** from a Magisk/Zygisk
  **ZIP** and from the LSPosed framework ZIP. They are not interchangeable.
* :class:`RootImplementation` distinguishes plain ``adb root`` (host-side root
  for a userdebug image) from application-facing **Magisk** root. Measured
  behaviour on this project says ``adb root`` does not imply Magisk root, so they
  are reported separately everywhere.
* :class:`ComponentState` separates *installed* from *enabled* from *active*:
  a Magisk module ZIP copied onto the device is installed but not active until
  the framework runs it after a reboot.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from ..core.models import utcnow


class ModuleKind(StrEnum):
    """What kind of package an imported artifact is."""

    XPOSED_APK = "xposed_apk"
    MAGISK_ZIP = "magisk_zip"
    ZYGISK_ZIP = "zygisk_zip"
    LSPOSED_ZIP = "lsposed_zip"
    UNKNOWN = "unknown"


class RootImplementation(StrEnum):
    """How root is obtained on a device."""

    NONE = "none"
    ADB_ROOT = "adb_root"
    MAGISK = "magisk"


class ComponentState(StrEnum):
    """Lifecycle state of one component (Magisk, Zygisk, LSPosed, a module)."""

    ABSENT = "absent"
    INSTALLED = "installed"
    ENABLED = "enabled"
    ACTIVE = "active"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class StepAction(StrEnum):
    """A single deployable instrumentation step."""

    VERIFY_ROOT = "verify_root"
    INSTALL_MAGISK = "install_magisk"
    COMPLETE_MAGISK_SETUP = "complete_magisk_setup"
    ENABLE_ZYGISK = "enable_zygisk"
    DISABLE_ZYGISK = "disable_zygisk"
    INSTALL_LSPOSED = "install_lsposed"
    INSTALL_MODULE = "install_module"
    ENABLE_MODULE = "enable_module"
    DISABLE_MODULE = "disable_module"
    REMOVE_MODULE = "remove_module"
    ENABLE_XPOSED_MODULE = "enable_xposed_module"
    SET_XPOSED_SCOPE = "set_xposed_scope"
    ENABLE_ADB_ROOT = "enable_adb_root"
    ENABLE_WRITABLE_SYSTEM = "enable_writable_system"
    INSTALL_LSPOSED_MANAGER = "install_lsposed_manager"
    REBOOT = "reboot"
    REMOVE_LSPOSED = "remove_lsposed"
    REMOVE_MAGISK = "remove_magisk"
    VERIFY = "verify"


class RunState(StrEnum):
    """State of one deployment run across several targets."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InstalledModule(BaseModel):
    """One module as reported by a probed device."""

    identifier: str = ""
    kind: ModuleKind = ModuleKind.UNKNOWN
    version: str = ""
    enabled: bool = True
    active: bool = False
    source: str = ""
    #: Application packages this module targets (Xposed/LSPosed scope).
    scope: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CapabilityFlags(BaseModel):
    """What a single device can actually do, measured rather than assumed."""

    adb_root: bool = False
    writable_system: bool = False
    magisk: bool = False
    magisk_version: str = ""
    magisk_environment_ready: bool = False
    zygisk_available: bool = False
    zygisk_enabled: bool = False
    zygisk_active: bool = False
    lsposed_installed: bool = False
    lsposed_version: str = ""
    lsposed_active: bool = False
    reboot_required: bool = False
    notes: list[str] = Field(default_factory=list)


class DeviceInstrumentation(BaseModel):
    """A capability + module inventory snapshot for one AVD."""

    instance: str
    api_level: int = 0
    abi: str = ""
    image_id: str = ""
    image_tag: str = ""
    serial: str = ""
    running: bool = False
    boot_completed: bool = False
    root_implementation: RootImplementation = RootImplementation.NONE
    capabilities: CapabilityFlags = Field(default_factory=CapabilityFlags)
    xposed_modules: list[InstalledModule] = Field(default_factory=list)
    magisk_modules: list[InstalledModule] = Field(default_factory=list)
    compatible: bool = False
    blockers: list[str] = Field(default_factory=list)
    probed_at: datetime = Field(default_factory=utcnow)

    @property
    def root_state(self) -> ComponentState:
        """Compact root state used by the GUI badge."""
        if self.root_implementation is RootImplementation.MAGISK:
            return ComponentState.ACTIVE
        if self.root_implementation is RootImplementation.ADB_ROOT:
            return ComponentState.ENABLED
        return ComponentState.ABSENT if self.running else ComponentState.UNKNOWN


class ModuleRecord(BaseModel):
    """One artifact in the central module library."""

    id: str
    kind: ModuleKind = ModuleKind.UNKNOWN
    name: str = ""
    package_name: str = ""
    version: str = ""
    version_code: int = 0
    min_sdk: int = 0
    abi: str = ""
    description: str = ""
    sha256: str
    size_bytes: int = 0
    source: str = "upload"
    source_url: str = ""
    file_path: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ProfileStep(BaseModel):
    """One step of an instrumentation profile."""

    id: str
    action: StepAction
    module_id: str = ""
    module_identifier: str = ""
    params: dict[str, Any] = Field(default_factory=dict)


class InstrumentationProfile(BaseModel):
    """A reusable sequence of instrumentation steps."""

    id: str
    name: str
    description: str = ""
    revision: int = 1
    steps: list[ProfileStep] = Field(default_factory=list)
    builtin: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class StepResult(BaseModel):
    """Outcome of one step on one target."""

    step_id: str
    action: StepAction
    ok: bool
    state: ComponentState = ComponentState.UNKNOWN
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class TargetResult(BaseModel):
    """Every step result for one AVD."""

    instance: str
    ok: bool = False
    skipped: bool = False
    error: str = ""
    steps: list[StepResult] = Field(default_factory=list)


class DeploymentRun(BaseModel):
    """One deployment of a profile across selected targets."""

    id: str
    profile_id: str = ""
    label: str = ""
    state: RunState = RunState.QUEUED
    targets: list[str] = Field(default_factory=list)
    results: list[TargetResult] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str = ""


class LogEntry(BaseModel):
    """One line of deployment/install log."""

    id: int = 0
    run_id: str = ""
    instance: str = ""
    level: str = "info"
    message: str = ""
    at: datetime = Field(default_factory=utcnow)


__all__ = [
    "CapabilityFlags",
    "ComponentState",
    "DeploymentRun",
    "DeviceInstrumentation",
    "InstalledModule",
    "InstrumentationProfile",
    "LogEntry",
    "ModuleKind",
    "ModuleRecord",
    "ProfileStep",
    "RootImplementation",
    "RunState",
    "StepAction",
    "StepResult",
    "TargetResult",
]
