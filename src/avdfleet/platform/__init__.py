"""AVD Fleet platform package: host detection, doctor and platform selection.

Public surface
--------------
``PlatformDetector`` / ``detect_host``
    Host facts (:class:`avdfleet.core.models.HostInfo`) including OS/arch,
    resources and hardware-accelerator state.  Honours the
    ``AVDFLEET_FAKE_HOST_OS`` / ``AVDFLEET_FAKE_ARCH`` / ``AVDFLEET_FAKE_ACCEL`` /
    ``AVDFLEET_FAKE_SDK_ROOT`` overrides from ``contracts/fakes-spec.md`` section 8.
``sdk_paths`` / ``tool_executable``
    Android SDK layout resolution from config/paths/environment and tool lookup.
``Doctor``
    :class:`avdfleet.core.interfaces.DoctorService` implementation with safe
    ``fix(check_id)`` actions and an ordered check list (``doctor.CHECK_IDS``).
``PlatformService``
    Convenience bundle of detector + doctor that also implements the
    :class:`avdfleet.core.interfaces.HostProvider` protocol for the installer.
``get_provider_name``
    Maps a host OS (and Linux distribution) to an installer provider:
    ``windows``, ``macos``, ``debian``, ``fedora`` or ``arch``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from avdfleet.core.config import Config
from avdfleet.core.models import (
    DoctorCheck,
    DoctorReport,
    HostInfo,
    HostOS,
    OperationResult,
    SdkPaths,
)
from avdfleet.core.paths import Paths
from avdfleet.platform.detect import (
    PlatformDetector,
    clear_host_cache,
    detect_distro_id,
    detect_host,
    kvm_group_member,
    kvm_group_name,
    normalise_arch,
    normalise_host_os,
    real_host_os,
    sdk_executables,
    sdk_paths,
    tool_executable,
    which_tool,
)
from avdfleet.platform.doctor import (
    CHECK_IDS,
    FAST_CHECK_IDS,
    SLOW_CHECK_IDS,
    Doctor,
    can_write_udev_rule,
    defender_present,
    filesystem_type,
    windows_long_paths_enabled,
)

_LINUX_DEBIAN_IDS = frozenset(
    {"debian", "ubuntu", "linuxmint", "pop", "kali", "raspbian", "elementary", "zorin", "neon"}
)
_LINUX_FEDORA_IDS = frozenset(
    {"fedora", "rhel", "centos", "rocky", "almalinux", "ol", "amzn", "oracle"}
)
_LINUX_ARCH_IDS = frozenset(
    {"arch", "manjaro", "endeavouros", "garuda", "arcolinux", "cachyos", "artix", "steamos"}
)

_PACKAGE_MANAGERS: dict[str, str] = {
    "windows": "winget",
    "macos": "brew",
    "debian": "apt-get",
    "fedora": "dnf",
    "arch": "pacman",
    "unknown": "",
}


def get_provider_name(
    host_os: HostInfo | HostOS | str | None = None,
    *,
    distro: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return the installer provider for a host OS (and Linux distribution).

    Accepts a :class:`HostInfo`, a :class:`HostOS` or a plain OS name; ``None``
    means "detect the current host cheaply".  Linux distributions map to
    ``debian`` (Debian/Ubuntu family, also the fallback for unknown distros),
    ``fedora`` (Fedora/RHEL/Rocky/AlmaLinux) or ``arch`` (Arch/Manjaro/...).
    """
    if isinstance(host_os, HostInfo):
        os_value = host_os.os
    elif host_os is None:
        os_value = real_host_os()
    else:
        os_value = normalise_host_os(host_os)
    if os_value is HostOS.WINDOWS:
        return "windows"
    if os_value is HostOS.MACOS:
        return "macos"
    if os_value is not HostOS.LINUX:
        return "unknown"
    distro_id = (distro or detect_distro_id(environ) or "").strip().lower()
    if distro_id in _LINUX_FEDORA_IDS:
        return "fedora"
    if distro_id in _LINUX_ARCH_IDS:
        return "arch"
    if distro_id in _LINUX_DEBIAN_IDS:
        return "debian"
    return "debian"


def get_package_manager(provider: str | None) -> str:
    """Return the native package manager for a provider name (empty if unknown)."""
    if provider is None:
        return ""
    return _PACKAGE_MANAGERS.get(str(provider).strip().lower(), "")


class PlatformService:
    """Detector + doctor bundle used by the CLI, API and installer providers.

    The service implements the structural :class:`avdfleet.core.interfaces.HostProvider`
    protocol (``platform``, ``detect``, ``sdk_paths``, ``package_manager``,
    ``doctor_checks``, ``fix``) while adding ``run_doctor`` for the full report.
    """

    def __init__(
        self,
        config: Config | None = None,
        paths: Paths | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        command_runner: Any = None,
    ) -> None:
        self._explicit_environ = environ is not None
        self._environ: dict[str, str] = dict(os.environ if environ is None else environ)
        self._config = config
        self._paths = paths
        self._command_runner = command_runner
        self._detector: PlatformDetector | None = None
        self._doctor: Doctor | None = None

    # -- lazily resolved dependencies ------------------------------------
    @property
    def environ(self) -> dict[str, str]:
        return dict(self._environ)

    @property
    def config(self) -> Config:
        if self._config is None:
            try:
                self._config = Config.load(environ=dict(self._environ))
            except (OSError, ValueError):
                self._config = Config.defaults()
        return self._config

    @property
    def paths(self) -> Paths:
        if self._paths is None:
            self._paths = self.config.resolved_paths()
        return self._paths

    @property
    def detector(self) -> PlatformDetector:
        if self._detector is None:
            self._detector = PlatformDetector(
                self.config,
                self.paths,
                environ=self._environ,
                command_runner=self._command_runner,
            )
        return self._detector

    @property
    def doctor(self) -> Doctor:
        if self._doctor is None:
            self._doctor = Doctor(
                self.config,
                self.paths,
                detector=self.detector,
                environ=self._environ,
                command_runner=self._command_runner,
            )
        return self._doctor

    # -- HostProvider surface -------------------------------------------
    @property
    def platform(self) -> str:
        """Installer provider name for this host (``windows``/``macos``/debian family)."""
        return get_provider_name(self.detect().os, environ=self._environ)

    def provider_name(self) -> str:
        """Method form of :attr:`platform` (installer providers prefer callables)."""
        return self.platform

    def detect(self, *, refresh: bool = False) -> HostInfo:
        return self.detector.detect(refresh=refresh)

    def sdk_paths(self) -> SdkPaths:
        return sdk_paths(self.config, self.paths, environ=self._environ)

    def package_manager(self) -> str:
        return get_package_manager(self.platform)

    def doctor_checks(self) -> list[DoctorCheck]:
        return self.run_doctor(include_slow=False).checks

    def run_doctor(self, *, include_slow: bool = False) -> DoctorReport:
        return self.doctor.run(include_slow=include_slow)

    def fix(
        self,
        check_id: str,
        *,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> OperationResult:
        return self.doctor.fix(check_id, confirm=confirm, dry_run=dry_run)


__all__ = [
    "CHECK_IDS",
    "FAST_CHECK_IDS",
    "SLOW_CHECK_IDS",
    "Doctor",
    "PlatformDetector",
    "PlatformService",
    "can_write_udev_rule",
    "clear_host_cache",
    "defender_present",
    "detect_distro_id",
    "detect_host",
    "filesystem_type",
    "get_package_manager",
    "get_provider_name",
    "kvm_group_member",
    "kvm_group_name",
    "normalise_arch",
    "normalise_host_os",
    "real_host_os",
    "sdk_executables",
    "sdk_paths",
    "tool_executable",
    "which_tool",
    "windows_long_paths_enabled",
]
