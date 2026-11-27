"""Fedora / RHEL-family install provider (dnf).

Covers Fedora, Rocky Linux and RHEL 8+ (``dnf``).  Group installs are
supported through the ``@group`` convention shared with dnf itself.
KVM uses the same udev rule/group approach as Debian, but the kernel modules
ship with the kernel package and SELinux generally needs no changes when the
SDK lives under the user's home directory.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from avdfleet.core.models import CpuArch, HostOS

from .base import (
    Command,
    EnvScope,
    InstallProvider,
    ToolTemplate,
    VirtualizationPlan,
    kvm_available,
    kvm_group_member,
    kvm_udev_rule_present,
    kvm_virtualization_plan,
    linux_reboot_required,
)
from .debian import DEFAULT_STUDIO_TARBALL_URL, STUDIO_ARCHIVE, STUDIO_INSTALL_DIR


class FedoraInstallProvider(InstallProvider):
    id = "fedora"
    platform = HostOS.LINUX
    display_name = "Fedora/RHEL"
    package_manager_name = "dnf"
    path_separator = ":"
    admin_error_hint = "re-run with sudo (the bootstrap uses sudo)"

    def __init__(
        self,
        *,
        kvm_available_now: bool | None = None,
        in_kvm_group: bool | None = None,
        udev_rule_present: bool | None = None,
        reboot_pending: bool | None = None,
        nested: bool | None = None,
        env: Mapping[str, str] | None = None,
        is_admin: bool | None = None,
        which=None,
        arch: CpuArch | None = None,
    ) -> None:
        super().__init__(env=env, is_admin=is_admin, which=which, arch=arch)
        self._kvm_available = kvm_available_now
        self._in_kvm_group = in_kvm_group
        self._udev_rule_present = udev_rule_present
        self._reboot_override = reboot_pending
        self._nested = nested

    # ------------------------------------------------------------------
    # Host state
    # ------------------------------------------------------------------
    @property
    def package_manager(self) -> str:
        return "dnf"

    def reboot_required(self) -> bool:
        if self._reboot_override is not None:
            return self._reboot_override
        return linux_reboot_required()

    def nested_virtualization(self) -> bool | None:
        return self._nested

    def username(self) -> str:
        return self.env_value("SUDO_USER") or self.env_value("USER") or self.env_value("LOGNAME") or "avdfleet"

    def _kvm_available_now(self) -> bool:
        return self._kvm_available if self._kvm_available is not None else kvm_available()

    def _in_group(self) -> bool:
        return self._in_kvm_group if self._in_kvm_group is not None else kvm_group_member("kvm")

    def _rule_present(self) -> bool:
        if self._udev_rule_present is not None:
            return self._udev_rule_present
        return kvm_udev_rule_present()

    # ------------------------------------------------------------------
    # Package management
    # ------------------------------------------------------------------
    def prerequisites(self) -> list[Command]:
        return [
            Command(
                [*self._sudo(), "dnf", "makecache", "-y"],
                description="refresh the dnf metadata cache",
                admin=True,
                timeout_s=900,
            )
        ]

    def install_system_packages(self, names: Sequence[str]) -> list[Command]:
        packages: list[str] = []
        groups: list[str] = []
        for name in names:
            if name.startswith("@") or name.startswith("group:"):
                groups.append(name.split(":", 1)[-1].lstrip("@"))
            else:
                packages.append(name)
        commands: list[Command] = []
        if packages:
            commands.append(
                Command(
                    [*self._sudo(), "dnf", "install", "-y", *packages],
                    description=f"install Fedora packages: {', '.join(packages)}",
                    admin=True,
                    timeout_s=1800,
                )
            )
        for group in groups:
            commands.append(
                Command(
                    [
                        *self._sudo(),
                        "dnf",
                        "group",
                        "install",
                        "-y",
                        "--setopt=group_package_types=mandatory,default",
                        group,
                    ],
                    description=f"install the dnf package group: {group}",
                    admin=True,
                    timeout_s=1800,
                )
            )
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
        return [Command(["sh", "-c", self._posix_remove_block(name)], description=f"remove {name}")]

    def profile_files(self) -> list[Path]:
        home = self.home()
        bash_profile = home / ".bash_profile"
        if bash_profile.is_file():
            return [bash_profile]
        return [home / ".profile"]

    # ------------------------------------------------------------------
    # Virtualisation: KVM
    # ------------------------------------------------------------------
    def virtualization_plan(self) -> VirtualizationPlan:
        return kvm_virtualization_plan(
            sudo=self._sudo(),
            kvm_available_now=self._kvm_available_now(),
            in_group=self._in_group(),
            rule_present=self._rule_present(),
            nested=self._nested,
            username=self.username(),
            family="fedora",
            extra_notes=(
                "Keep the SDK under your home directory: Fedora SELinux may block emulator "
                "executables in non-standard locations unless relabelled.",
                "RHEL/Rocky hosts may need 'dnf install qemu-kvm-core' only for non-Android QEMU use.",
            ),
        )

    # ------------------------------------------------------------------
    # Tool templates
    # ------------------------------------------------------------------
    @property
    def install_command_templates(self) -> dict[str, ToolTemplate]:
        sudo = self._sudo()
        return {
            "jdk": ToolTemplate(
                "jdk",
                commands=(
                    Command(
                        [*sudo, "dnf", "install", "-y", "java-21-openjdk-devel"],
                        description="install OpenJDK 21 (devel)",
                        admin=True,
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
                        [*sudo, "dnf", "install", "-y", "nodejs", "npm"],
                        description="install Node.js and npm",
                        admin=True,
                        timeout_s=1800,
                    ),
                ),
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
            ),
            "studio": ToolTemplate(
                "studio",
                commands=tuple(self._studio_commands()),
                optional=True,
                notes="Downloads the official Android Studio tarball into /opt.",
            ),
            "scrcpy": ToolTemplate(
                "scrcpy",
                commands=(
                    Command(
                        [*sudo, "dnf", "install", "-y", "scrcpy"],
                        description="install scrcpy",
                        admin=True,
                        timeout_s=900,
                    ),
                ),
                binaries=("scrcpy",),
                version_argv=("scrcpy", "--version"),
                optional=True,
            ),
        }

    def _studio_commands(self) -> list[Command]:
        url = self.env_value("AVDFLEET_STUDIO_TARBALL_URL", DEFAULT_STUDIO_TARBALL_URL)
        sha = self.env_value("AVDFLEET_STUDIO_TARBALL_SHA256") or None
        commands = self.download_file(url, STUDIO_ARCHIVE, sha256=sha)
        commands.append(
            Command(
                [
                    *self._sudo(),
                    "sh",
                    "-c",
                    f"mkdir -p {self.shell_quote(STUDIO_INSTALL_DIR)} && "
                    f"tar -xzf {self.shell_quote(STUDIO_ARCHIVE)} "
                    f"-C {self.shell_quote(STUDIO_INSTALL_DIR)} --strip-components=1",
                ],
                description="extract Android Studio to /opt/android-studio",
                admin=True,
                timeout_s=900,
            )
        )
        return commands

    # ------------------------------------------------------------------
    # Locations
    # ------------------------------------------------------------------
    @property
    def studio_config_dir(self) -> Path:
        xdg = self.env_value("XDG_CONFIG_HOME") or str(self.home() / ".config")
        return _newest_studio_dir(Path(xdg) / "Google")

    def studio_install_dirs(self) -> list[Path]:
        return [
            Path(STUDIO_INSTALL_DIR) / "jbr",
            Path("/usr/local/android-studio/jbr"),
            Path("/snap/android-studio/current/android-studio/jbr"),
            self.home() / "Android" / "android-studio" / "jbr",
        ]

    @property
    def default_sdk_root(self) -> Path:
        return self.home() / "Android" / "Sdk"

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


__all__ = ["FedoraInstallProvider"]
