"""macOS install provider (Homebrew, Xcode Command Line Tools, HVF).

Order matters: Homebrew needs the Command Line Tools.  ``prerequisites()``
therefore emits the CLT check/install *before* the Homebrew bootstrap, and the
tests assert that ordering.  Acceleration is Hypervisor.framework (HVF), which
Apple provides in the OS - no host change and no reboot.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from avdfleet.core.models import AcceleratorKind, CpuArch, HostOS

from .base import (
    Command,
    EnvScope,
    InstallProvider,
    ToolTemplate,
    VirtualizationPlan,
    macos_hv_support,
)

CLT_INSTALL_SCRIPT = (
    "# Command Line Tools must exist before Homebrew can compile/install anything.\n"
    "if ! xcode-select -p >/dev/null 2>&1; then\n"
    "  touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress || exit 1\n"
    "  label=$(softwareupdate --list 2>/dev/null | awk -F'Label: ' '/Command Line Tools/ {print $2; exit}' | xargs)\n"
    "  [ -n \"$label\" ] || { echo 'Command Line Tools label not found' >&2; exit 1; }\n"
    "  softwareupdate --install \"$label\" --agree-to-license\n"
    "fi\n"
    "xcode-select -p\n"
)

HOMEBREW_INSTALL_SCRIPT = (
    "if ! command -v brew >/dev/null 2>&1; then\n"
    "  /bin/bash -c \"$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"\n"
    "fi\n"
    "if [ -x /opt/homebrew/bin/brew ]; then eval \"$(/opt/homebrew/bin/brew shellenv)\"; fi\n"
    "if [ -x /usr/local/bin/brew ]; then eval \"$(/usr/local/bin/brew shellenv)\"; fi\n"
    "command -v brew\n"
)


class MacOSInstallProvider(InstallProvider):
    id = "macos"
    platform = HostOS.MACOS
    display_name = "macOS"
    package_manager_name = "brew"
    path_separator = ":"
    admin_error_hint = "re-run with sudo (or grant your account admin rights)"

    def __init__(
        self,
        *,
        command_line_tools: bool | None = None,
        homebrew: bool | None = None,
        hv_support: bool | None = None,
        reboot_pending: bool | None = None,
        nested: bool | None = None,
        env: Mapping[str, str] | None = None,
        is_admin: bool | None = None,
        which=None,
        arch: CpuArch | None = None,
    ) -> None:
        super().__init__(env=env, is_admin=is_admin, which=which, arch=arch)
        self._command_line_tools = command_line_tools
        self._homebrew_override = homebrew
        self._hv_support_override = hv_support
        self._reboot_override = reboot_pending
        self._nested = nested

    # ------------------------------------------------------------------
    # Host state
    # ------------------------------------------------------------------
    def command_line_tools_ready(self) -> bool:
        if self._command_line_tools is not None:
            return self._command_line_tools
        return self.command_exists("xcode-select") and self.command_exists("clang")

    def homebrew_ready(self) -> bool:
        if self._homebrew_override is not None:
            return self._homebrew_override
        return self.command_exists("brew")

    def package_manager_ready(self) -> bool:
        return self.homebrew_ready()

    def reboot_required(self) -> bool:
        return bool(self._reboot_override)

    def nested_virtualization(self) -> bool | None:
        return self._nested

    @property
    def package_manager(self) -> str:
        return "brew"

    # ------------------------------------------------------------------
    # Package management
    # ------------------------------------------------------------------
    def prerequisites(self) -> list[Command]:
        """Command Line Tools first, then Homebrew (strict order)."""
        return [
            Command(
                ["sh", "-c", CLT_INSTALL_SCRIPT],
                description="ensure Xcode Command Line Tools are installed (before Homebrew)",
                admin=True,
                timeout_s=1800,
            ),
            Command(
                ["sh", "-c", HOMEBREW_INSTALL_SCRIPT],
                description="ensure Homebrew is installed",
                timeout_s=1800,
            ),
        ]

    def install_system_packages(self, names: Sequence[str]) -> list[Command]:
        formulas: list[str] = []
        cask_commands: list[Command] = []
        for name in names:
            if name.startswith("cask:") or name.startswith("cask/"):
                cask = name.split(":", 1)[-1] if name.startswith("cask:") else name.split("/", 1)[-1]
                cask_commands.append(
                    Command(
                        ["brew", "install", "--cask", cask],
                        description=f"install macOS cask {cask}",
                        timeout_s=1800,
                    )
                )
            else:
                formulas.append(name)
        commands: list[Command] = []
        if formulas:
            commands.append(
                Command(
                    ["brew", "install", *formulas],
                    description=f"install Homebrew formulae: {', '.join(formulas)}",
                    timeout_s=1800,
                )
            )
        commands.extend(cask_commands)
        return commands

    # ------------------------------------------------------------------
    # Environment
    # ------------------------------------------------------------------
    def set_env_var(self, name: str, value: str, scope: EnvScope = EnvScope.USER) -> list[Command]:
        if scope is EnvScope.PROCESS:
            return []
        return self._posix_env_commands(name, value)

    def remove_env_var(self, name: str, scope: EnvScope = EnvScope.USER) -> list[Command]:
        if scope is EnvScope.PROCESS:
            return []
        profile = self.profile_files()[0]
        script = self._posix_remove_block(name)
        return [Command(["sh", "-c", script], description=f"remove {name} from {profile}")]

    def profile_files(self) -> list[Path]:
        home = self.home()
        zprofile = home / ".zprofile"
        bash_profile = home / ".bash_profile"
        if bash_profile.is_file() and not zprofile.exists():
            return [bash_profile]
        return [zprofile]

    # ------------------------------------------------------------------
    # Virtualisation: HVF
    # ------------------------------------------------------------------
    def virtualization_plan(self) -> VirtualizationPlan:
        support = self._hv_support_override
        if support is None:
            support = macos_hv_support()
        notes = [
            "Hypervisor.framework (HVF) is part of macOS; no feature install or reboot is needed.",
        ]
        if not support:
            notes.append(
                "HVF is not available on this machine (virtualised/nested macOS?). "
                "The Android emulator cannot boot without hardware acceleration."
            )
        return VirtualizationPlan(
            kind=AcceleratorKind.HVF,
            available=bool(support),
            enabled=bool(support),
            requires_reboot=False,
            nested=self._nested,
            notes=tuple(notes),
        )

    # ------------------------------------------------------------------
    # Tool templates
    # ------------------------------------------------------------------
    @property
    def install_command_templates(self) -> dict[str, ToolTemplate]:
        return {
            "jdk": ToolTemplate(
                "jdk",
                commands=(
                    Command(
                        ["brew", "install", "--cask", "temurin@21"],
                        description="install Eclipse Temurin JDK 21",
                        timeout_s=1800,
                    ),
                ),
                binaries=("java", "javac"),
                version_argv=("java", "-version"),
            ),
            "node": ToolTemplate(
                "node",
                commands=(
                    Command(
                        ["brew", "install", "node@22"],
                        description="install Node.js 22",
                        timeout_s=1800,
                    ),
                ),
                binaries=("node", "npm"),
                version_argv=("node", "--version"),
                notes="Homebrew keg-only formula: avdfleet adds its bin directory to PATH.",
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
            ),
            "studio": ToolTemplate(
                "studio",
                commands=(
                    Command(
                        ["brew", "install", "--cask", "android-studio"],
                        description="install Android Studio",
                        timeout_s=1800,
                    ),
                ),
                optional=True,
            ),
            "scrcpy": ToolTemplate(
                "scrcpy",
                commands=(
                    Command(
                        ["brew", "install", "scrcpy"],
                        description="install scrcpy",
                        timeout_s=1800,
                    ),
                ),
                binaries=("scrcpy",),
                version_argv=("scrcpy", "--version"),
                optional=True,
            ),
        }

    @property
    def studio_config_dir(self) -> Path:
        base = self.home() / "Library" / "Application Support" / "Google"
        return _newest_studio_dir(base)

    def studio_install_dirs(self) -> list[Path]:
        return [Path("/Applications/Android Studio.app/Contents/jbr/Contents/Home")]

    @property
    def default_sdk_root(self) -> Path:
        return self.home() / "Library" / "Android" / "sdk"

    @property
    def default_avd_home(self) -> Path:
        return self.home() / ".android" / "avd"


def _newest_studio_dir(base: Path) -> Path:
    try:
        candidates = sorted(
            (path for path in base.glob("AndroidStudio*") if path.is_dir()),
            key=lambda path: path.name,
        )
    except OSError:
        candidates = []
    return candidates[-1] if candidates else base / "AndroidStudio"


__all__ = ["CLT_INSTALL_SCRIPT", "HOMEBREW_INSTALL_SCRIPT", "MacOSInstallProvider"]
