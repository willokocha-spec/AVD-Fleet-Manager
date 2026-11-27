"""Debian/Ubuntu install provider (apt).

Also used as the fallback for Ubuntu derivatives (Linux Mint, Pop!_OS) detected
through ``/etc/os-release``.  KVM acceleration is configured through a udev
rule plus ``kvm`` group membership; that needs sudo but no reboot.
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

#: Override the Android Studio tarball with AVDFLEET_STUDIO_TARBALL_URL/SHA256
#: (R1 pins exact versions; these values only seed a usable default).
DEFAULT_STUDIO_TARBALL_URL = (
    "https://redirector.gvt1.com/edgedl/android/studio/ide-zips/2024.2.1.12/"
    "android-studio-2024.2.1.12-linux.tar.gz"
)
STUDIO_ARCHIVE = "/tmp/avdfleet-android-studio.tar.gz"
STUDIO_INSTALL_DIR = "/opt/android-studio"

#: NodeSource setup keeps the distro Node (18 on Ubuntu 24.04) from being too old for Appium 4.
NODE_MAJOR = "22"


class DebianInstallProvider(InstallProvider):
    id = "debian"
    platform = HostOS.LINUX
    display_name = "Debian/Ubuntu"
    package_manager_name = "apt-get"
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
        return "apt-get"

    def reboot_required(self) -> bool:
        if self._reboot_override is not None:
            return self._reboot_override
        return linux_reboot_required()

    def nested_virtualization(self) -> bool | None:
        return self._nested

    def kvm_ready(self) -> bool:
        return self._kvm_available_now() and self._in_group() and self._rule_present()

    def _kvm_available_now(self) -> bool:
        return self._kvm_available if self._kvm_available is not None else kvm_available()

    def _in_group(self) -> bool:
        return self._in_kvm_group if self._in_kvm_group is not None else kvm_group_member("kvm")

    def _rule_present(self) -> bool:
        if self._udev_rule_present is not None:
            return self._udev_rule_present
        return kvm_udev_rule_present()

    def username(self) -> str:
        return self.env_value("SUDO_USER") or self.env_value("USER") or self.env_value("LOGNAME") or "avdfleet"

    # ------------------------------------------------------------------
    # Package management
    # ------------------------------------------------------------------
    def prerequisites(self) -> list[Command]:
        return [
            Command(
                [*self._sudo(), "apt-get", "update", "-y"],
                description="refresh the apt package index",
                admin=True,
                timeout_s=900,
            )
        ]

    def install_system_packages(self, names: Sequence[str]) -> list[Command]:
        if not names:
            return []
        return [
            Command(
                [*self._sudo(), "apt-get", "install", "-y", "--no-install-recommends", *names],
                description=f"install Debian packages: {', '.join(names)}",
                admin=True,
                timeout_s=1800,
            )
        ]

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
        return [self.home() / ".profile"]

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
            family="debian",
            extra_notes=(
                "On Debian/Ubuntu install the 'cpu-checker' package and run 'kvm-ok' to confirm VT-x/AMD-V.",
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
                        [*sudo, "apt-get", "install", "-y", "openjdk-21-jdk-headless"],
                        description="install OpenJDK 21 (headless)",
                        admin=True,
                        timeout_s=1800,
                    ),
                ),
                binaries=("java", "javac"),
                version_argv=("java", "-version"),
            ),
            "node": ToolTemplate(
                "node",
                commands=tuple(self._nodesource_commands()),
                binaries=("node", "npm"),
                version_argv=("node", "--version"),
                notes="Installs the NodeSource signed apt repository, then Node 22 (distro Node is too old for Appium 4).",
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
                notes="Uses the user npm prefix when writable; run as root or configure a prefix otherwise.",
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
                notes="Downloads the official Android Studio tarball into /opt (override with AVDFLEET_STUDIO_TARBALL_URL).",
            ),
            "scrcpy": ToolTemplate(
                "scrcpy",
                commands=(
                    Command(
                        [*sudo, "apt-get", "install", "-y", "scrcpy"],
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

    def _nodesource_commands(self) -> list[Command]:
        sudo = self._sudo()
        keyring = "/etc/apt/keyrings/nodesource.gpg"
        source_list = "/etc/apt/sources.list.d/nodesource.list"
        return [
            Command(
                [*sudo, "install", "-d", "-m", "0755", "/etc/apt/keyrings"],
                description="ensure the apt keyrings directory exists",
                admin=True,
            ),
            Command(
                [
                    "sh",
                    "-c",
                    "curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key "
                    f"| gpg --dearmor --yes -o {keyring}",
                ],
                description="install the NodeSource signing key",
                admin=True,
                timeout_s=300,
            ),
            Command(
                [
                    "sh",
                    "-c",
                    f"printf '%s\\n' 'deb [signed-by={keyring}] "
                    f"https://deb.nodesource.com/node_{NODE_MAJOR}.x nodistro main' "
                    f"| tee {source_list} >/dev/null",
                ],
                description="add the NodeSource apt repository",
                admin=True,
            ),
            Command(
                [*sudo, "apt-get", "update", "-y"],
                description="refresh the apt index after adding NodeSource",
                admin=True,
                timeout_s=900,
            ),
            Command(
                [*sudo, "apt-get", "install", "-y", "nodejs"],
                description="install Node.js from NodeSource",
                admin=True,
                timeout_s=1800,
            ),
        ]

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


__all__ = ["DEFAULT_STUDIO_TARBALL_URL", "DebianInstallProvider", "NODE_MAJOR"]
