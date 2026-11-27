"""Windows install provider (winget + WHPX + AEHD).

Windows is the most involved provider because the emulator accelerator is an
optional Windows feature that needs one reboot, and because the legacy Android
Emulator Hypervisor Driver (AEHD) conflicts with WHPX and must be migrated.
Everything here returns command plans; the engine owns execution and the
reboot pause.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from avdfleet.core.models import AcceleratorKind, CpuArch, HostOS

from .base import (
    Command,
    EnvScope,
    InstallProvider,
    ToolTemplate,
    VirtualizationPlan,
    windows_reboot_pending,
)

#: winget package ids (stable, vendor-published ids).
_WINGET_IDS = {
    "jdk": "Microsoft.OpenJDK.21",
    "node": "OpenJS.NodeJS.LTS",
    "studio": "Google.AndroidStudio",
    "scrcpy": "Genymobile.scrcpy",
}

#: AEHD (a.k.a. the Android Emulator hypervisor driver) service/driver names.
AEHD_SERVICE_NAMES = ("aehd", "gvm", "AndroidEmulatorHypervisorDriver")


def _winget_install(package_id: str, *, description: str = "") -> Command:
    return Command(
        [
            "winget",
            "install",
            "--id",
            package_id,
            "--exact",
            "--silent",
            "--accept-package-agreements",
            "--accept-source-agreements",
        ],
        description=description or f"install {package_id} with winget",
        timeout_s=1800,
    )


class WindowsInstallProvider(InstallProvider):
    id = "windows"
    platform = HostOS.WINDOWS
    display_name = "Windows"
    package_manager_name = "winget"
    path_separator = ";"
    admin_error_hint = "run PowerShell as Administrator (or use the elevated bootstrap)"

    def __init__(
        self,
        *,
        features: Mapping[str, bool] | None = None,
        firmware_virtualization: bool | None = None,
        aehd_installed: bool | None = None,
        reboot_pending: bool | None = None,
        nested: bool | None = None,
        env: Mapping[str, str] | None = None,
        is_admin: bool | None = None,
        which=None,
        arch: CpuArch | None = None,
    ) -> None:
        super().__init__(env=env, is_admin=is_admin, which=which, arch=arch)
        self._features = dict(features) if features is not None else None
        self._firmware_virtualization = firmware_virtualization
        self._aehd_override = aehd_installed
        self._reboot_override = reboot_pending
        self._nested = nested

    # ------------------------------------------------------------------
    # Host state
    # ------------------------------------------------------------------
    @property
    def features(self) -> dict[str, bool]:
        """Optional Windows features as detected (or injected for tests)."""
        if self._features is not None:
            return dict(self._features)
        if sys.platform != "win32":
            return {}
        self._features = _query_windows_features()
        return dict(self._features)

    def _detect_admin(self) -> bool:
        return super()._detect_admin()

    def reboot_required(self) -> bool:
        if self._reboot_override is not None:
            return self._reboot_override
        return windows_reboot_pending()

    def nested_virtualization(self) -> bool | None:
        return self._nested

    @property
    def package_manager(self) -> str:
        return "winget"

    # ------------------------------------------------------------------
    # Package management
    # ------------------------------------------------------------------
    def prerequisites(self) -> list[Command]:
        # Windows 11 ships winget (App Installer). No bootstrap command is
        # generated here: if winget is missing the operator must install
        # "App Installer" from the Store, which cannot be automated safely.
        return []

    def install_system_packages(self, names: Sequence[str]) -> list[Command]:
        return [_winget_install(name) for name in names]

    # ------------------------------------------------------------------
    # Environment
    # ------------------------------------------------------------------
    def set_env_var(self, name: str, value: str, scope: EnvScope = EnvScope.USER) -> list[Command]:
        if scope is EnvScope.PROCESS:
            return []
        return self._windows_env_commands(name, value, scope)

    def remove_env_var(self, name: str, scope: EnvScope = EnvScope.USER) -> list[Command]:
        if scope is EnvScope.PROCESS:
            return []
        target = "User" if scope is not EnvScope.SYSTEM else "Machine"
        script = f"[Environment]::SetEnvironmentVariable({_ps(name)}, $null, {_ps(target)})"
        return [
            Command(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                description=f"remove {name} from the {target.lower()} environment",
                admin=scope is EnvScope.SYSTEM,
            )
        ]

    # ------------------------------------------------------------------
    # Virtualisation: WHPX, AEHD migration
    # ------------------------------------------------------------------
    def virtualization_plan(self) -> VirtualizationPlan:
        features = self.features
        whpx_enabled = bool(features.get("HypervisorPlatform", False))
        firmware_ok = self._firmware_virtualization is not False
        notes: list[str] = [
            "WHPX is the supported accelerator on Windows 11; it needs one reboot.",
            "Enable VT-x/AMD-V in firmware; nested virtualisation is frequently unavailable.",
        ]
        if self.aehd_installed() and not whpx_enabled:
            notes.append("AEHD is installed and conflicts with WHPX; accel.aehd migrates it first.")
        if not firmware_ok:
            notes.append("Virtualisation is disabled in firmware (VT-x/SVM not exposed).")
            return VirtualizationPlan(
                kind=AcceleratorKind.WHPX,
                available=False,
                enabled=False,
                requires_reboot=False,
                nested=self._nested,
                notes=tuple(notes),
            )
        commands: tuple[Command, ...] = ()
        if not whpx_enabled:
            enable_script = (
                "Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform "
                "-All -NoRestart | Out-Null"
            )
            commands = (
                Command(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", enable_script],
                    description="enable the Windows Hypervisor Platform feature",
                    admin=True,
                    requires_reboot=True,
                    timeout_s=900,
                ),
                Command(
                    ["bcdedit", "/set", "hypervisorlaunchtype", "auto"],
                    description="make the Windows hypervisor launch at boot",
                    admin=True,
                    requires_reboot=True,
                ),
            )
        return VirtualizationPlan(
            kind=AcceleratorKind.WHPX,
            available=True,
            enabled=whpx_enabled,
            requires_reboot=not whpx_enabled,
            commands=commands,
            nested=self._nested,
            notes=tuple(notes),
        )

    def aehd_installed(self) -> bool:
        if self._aehd_override is not None:
            return self._aehd_override
        if sys.platform != "win32":
            return False
        return _query_aehd_installed()

    def aehd_migration_plan(self) -> VirtualizationPlan:
        """Stop and remove AEHD before WHPX is enabled."""
        commands = (
            Command(
                ["sc.exe", "stop", "aehd"],
                description="stop the AEHD service",
                admin=True,
            ),
            Command(
                ["sc.exe", "delete", "aehd"],
                description="delete the AEHD service",
                admin=True,
            ),
            Command(
                ["pnputil", "/delete-driver", "aehd.inf", "/uninstall", "/force"],
                description="remove the AEHD driver package",
                admin=True,
            ),
        )
        return VirtualizationPlan(
            kind=AcceleratorKind.AEHD,
            available=True,
            enabled=False,
            requires_reboot=False,
            commands=commands,
            nested=self._nested,
            notes=(
                "AEHD (Android Emulator Hypervisor Driver) must be removed before WHPX can drive the emulator.",
                "accel.whpx enables WHPX afterwards and requests the single pending reboot.",
            ),
        )

    # ------------------------------------------------------------------
    # Tool templates
    # ------------------------------------------------------------------
    @property
    def install_command_templates(self) -> dict[str, ToolTemplate]:
        return {
            "jdk": ToolTemplate(
                "jdk",
                commands=(_winget_install(_WINGET_IDS["jdk"], description="install the Microsoft OpenJDK 21 JDK"),),
                binaries=("java", "javac"),
                version_argv=("java", "-version"),
                notes="Android Studio's JBR can also be used; JAVA_HOME is configured by sdk.env.",
            ),
            "node": ToolTemplate(
                "node",
                commands=(_winget_install(_WINGET_IDS["node"], description="install Node.js LTS"),),
                binaries=("node", "npm"),
                version_argv=("node", "--version"),
            ),
            "appium": ToolTemplate(
                "appium",
                commands=(
                    Command(
                        ["npm", "install", "--global", "appium@4"],
                        description="install Appium 4 globally",
                        timeout_s=1800,
                    ),
                ),
                binaries=("appium",),
                version_argv=("appium", "--version"),
            ),
            "appium_driver": ToolTemplate(
                "appium_driver",
                commands=(
                    Command(
                        ["appium", "driver", "install", "uiautomator2"],
                        description="install the Appium UiAutomator2 driver",
                        timeout_s=900,
                    ),
                ),
            ),
            "inspector": ToolTemplate(
                "inspector",
                commands=(
                    Command(
                        ["npm", "install", "--global", "appium-inspector"],
                        description="install the Appium Inspector",
                        timeout_s=1200,
                    ),
                ),
                optional=True,
                notes="The Inspector desktop build is optional; a failing install never fails the installer.",
            ),
            "studio": ToolTemplate(
                "studio",
                commands=(_winget_install(_WINGET_IDS["studio"], description="install Android Studio"),),
                notes="Optional (config.studio_required gates GUI usage).",
                optional=True,
            ),
            "scrcpy": ToolTemplate(
                "scrcpy",
                commands=(_winget_install(_WINGET_IDS["scrcpy"], description="install scrcpy"),),
                binaries=("scrcpy",),
                version_argv=("scrcpy", "--version"),
                optional=True,
            ),
        }

    @property
    def studio_config_dir(self) -> Path:
        base = Path(self.env_value("APPDATA") or (self.home() / "AppData" / "Roaming")) / "Google"
        return _newest_studio_dir(base)

    def studio_install_dirs(self) -> list[Path]:
        program_files = Path(self.env_value("PROGRAMFILES") or "C:/Program Files")
        return [
            program_files / "Android" / "Android Studio" / "jbr",
            Path(self.env_value("LOCALAPPDATA") or (self.home() / "AppData" / "Local"))
            / "Programs"
            / "Android Studio"
            / "jbr",
        ]

    @property
    def default_sdk_root(self) -> Path:
        local_app_data = self.env_value("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "Android" / "Sdk"
        return self.home() / "AppData" / "Local" / "Android" / "Sdk"

    @property
    def default_avd_home(self) -> Path:
        return self.home() / ".android" / "avd"


def _ps(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _newest_studio_dir(base: Path) -> Path:
    try:
        candidates = sorted(
            (path for path in base.glob("AndroidStudio*") if path.is_dir()),
            key=lambda path: path.name,
        )
    except OSError:
        candidates = []
    return candidates[-1] if candidates else base / "AndroidStudio"


def _query_windows_features() -> dict[str, bool]:
    """Query optional features via DISM/PowerShell (real Windows host only)."""
    names = ("HypervisorPlatform", "VirtualMachinePlatform", "Microsoft-Hyper-V-All")
    enabled: dict[str, bool] = {}
    probe = (
        "Get-WindowsOptionalFeature -Online | "
        "Where-Object { $_.FeatureName -in @('HypervisorPlatform','VirtualMachinePlatform','Microsoft-Hyper-V-All') } | "
        "ForEach-Object { \"$($_.FeatureName)=$($_.State)\" }"
    )
    try:
        import subprocess

        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", probe],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - Windows only
        return {name: False for name in names}
    for line in completed.stdout.splitlines():
        key, _, value = line.partition("=")
        key = key.strip()
        if key in names:
            enabled[key] = value.strip().lower() in {"enabled", "enable"}
    return {name: enabled.get(name, False) for name in names}


def _query_aehd_installed() -> bool:
    """Detect the AEHD service via ``sc.exe query`` (real Windows host only)."""
    try:
        import subprocess

        for service in AEHD_SERVICE_NAMES:
            completed = subprocess.run(
                ["sc.exe", "query", service],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if completed.returncode == 0 and "SERVICE_NAME" in completed.stdout.upper():
                return True
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - Windows only
        return False
    return False


__all__ = ["AEHD_SERVICE_NAMES", "WindowsInstallProvider"]
