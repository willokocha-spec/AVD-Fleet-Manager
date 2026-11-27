"""Unit tests for installer providers (command shapes, detection, no real installs)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from avdfleet.core.errors import ValidationError
from avdfleet.core.models import AcceleratorKind, CpuArch, HostOS
from avdfleet.platform.installer.providers import (
    KVM_UDEV_RULE,
    KVM_UDEV_RULE_PATH,
    PROVIDER_IDS,
    REQUIRED_TOOL_TEMPLATES,
    detect_provider_id,
    get_provider,
    linux_provider_from_os_release,
    provider_class,
    resolve_provider_name,
)
from avdfleet.platform.installer.providers.arch import MULTILIB_NOTE, ArchInstallProvider
from avdfleet.platform.installer.providers.base import Command, EnvScope
from avdfleet.platform.installer.providers.debian import DebianInstallProvider
from avdfleet.platform.installer.providers.fedora import FedoraInstallProvider
from avdfleet.platform.installer.providers.macos import MacOSInstallProvider
from avdfleet.platform.installer.providers.windows import WindowsInstallProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def base_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    return {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(tmp_path / "appdata"),
        "LOCALAPPDATA": str(tmp_path / "localappdata"),
        "PROGRAMFILES": str(tmp_path / "programfiles"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "PATH": "",
        "USER": "tester",
    }


def make_windows(tmp_path: Path, **overrides: object) -> WindowsInstallProvider:
    kwargs: dict[str, object] = {
        "features": {},
        "firmware_virtualization": None,
        "aehd_installed": False,
        "reboot_pending": False,
        "nested": None,
        "env": base_env(tmp_path),
        "is_admin": True,
        "which": lambda binary: None,
        "arch": CpuArch.X86_64,
    }
    kwargs.update(overrides)
    return WindowsInstallProvider(**kwargs)  # type: ignore[arg-type]


def make_macos(tmp_path: Path, **overrides: object) -> MacOSInstallProvider:
    kwargs: dict[str, object] = {
        "command_line_tools": False,
        "homebrew": False,
        "hv_support": True,
        "reboot_pending": False,
        "nested": None,
        "env": base_env(tmp_path),
        "is_admin": True,
        "which": lambda binary: None,
        "arch": CpuArch.ARM64,
    }
    kwargs.update(overrides)
    return MacOSInstallProvider(**kwargs)  # type: ignore[arg-type]


def make_debian(tmp_path: Path, **overrides: object) -> DebianInstallProvider:
    kwargs: dict[str, object] = {
        "kvm_available_now": True,
        "in_kvm_group": False,
        "udev_rule_present": False,
        "reboot_pending": False,
        "nested": None,
        "env": base_env(tmp_path),
        "is_admin": True,
        "which": lambda binary: None,
        "arch": CpuArch.X86_64,
    }
    kwargs.update(overrides)
    return DebianInstallProvider(**kwargs)  # type: ignore[arg-type]


def make_fedora(tmp_path: Path, **overrides: object) -> FedoraInstallProvider:
    kwargs: dict[str, object] = {
        "kvm_available_now": True,
        "in_kvm_group": False,
        "udev_rule_present": False,
        "reboot_pending": False,
        "nested": None,
        "env": base_env(tmp_path),
        "is_admin": True,
        "which": lambda binary: None,
        "arch": CpuArch.X86_64,
    }
    kwargs.update(overrides)
    return FedoraInstallProvider(**kwargs)  # type: ignore[arg-type]


def make_arch(tmp_path: Path, **overrides: object) -> ArchInstallProvider:
    kwargs: dict[str, object] = {
        "kvm_available_now": True,
        "in_kvm_group": False,
        "udev_rule_present": False,
        "reboot_pending": False,
        "nested": None,
        "env": base_env(tmp_path),
        "is_admin": True,
        "which": lambda binary: None,
        "arch": CpuArch.X86_64,
    }
    kwargs.update(overrides)
    return ArchInstallProvider(**kwargs)  # type: ignore[arg-type]


def argv_text(commands: Sequence[Command]) -> str:
    return "\n".join(" ".join(command.argv) for command in commands)


# ---------------------------------------------------------------------------
# Registry / detection
# ---------------------------------------------------------------------------
def test_provider_registry_and_aliases() -> None:
    assert set(PROVIDER_IDS) == {"windows", "macos", "debian", "fedora", "arch"}
    assert resolve_provider_name("ubuntu") == "debian"
    assert resolve_provider_name("rhel") == "fedora"
    assert resolve_provider_name("manjaro") == "arch"
    assert resolve_provider_name("macos") == "macos"
    assert provider_class("windows") is WindowsInstallProvider
    with pytest.raises(ValidationError):
        resolve_provider_name("solaris")


def test_provider_detection_from_fake_os_and_os_release() -> None:
    assert detect_provider_id(environ={"AVDFLEET_FAKE_HOST_OS": "windows"}) == "windows"
    assert detect_provider_id(environ={"AVDFLEET_FAKE_HOST_OS": "macos"}) == "macos"
    ubuntu = 'ID=ubuntu\nID_LIKE=debian\n'
    assert detect_provider_id(environ={"AVDFLEET_FAKE_HOST_OS": "linux"}, os_release=ubuntu) == "debian"
    rocky = 'ID="rocky"\nID_LIKE="rhel centos fedora"\n'
    assert detect_provider_id(environ={"AVDFLEET_FAKE_HOST_OS": "linux"}, os_release=rocky) == "fedora"
    arch = 'ID=arch\n'
    assert detect_provider_id(environ={"AVDFLEET_FAKE_HOST_OS": "linux"}, os_release=arch) == "arch"
    assert linux_provider_from_os_release("ID=weird\n") is None


def test_provider_detection_env_override_and_command_probe() -> None:
    detected = detect_provider_id(
        environ={"AVDFLEET_INSTALL_PROVIDER": "fedora", "AVDFLEET_FAKE_HOST_OS": "linux"}
    )
    assert detected == "fedora"
    probed = detect_provider_id(
        environ={"AVDFLEET_FAKE_HOST_OS": "linux"},
        os_release="ID=unknown\n",
        command_exists=lambda binary: binary == "pacman",
    )
    assert probed == "arch"
    assert detect_provider_id(provider_id="debian", environ={"AVDFLEET_HOST": "x"}) == "debian"
    with pytest.raises(ValidationError):
        detect_provider_id(environ={"AVDFLEET_FAKE_HOST_OS": "beos"})


def test_get_provider_returns_concrete_instances(tmp_path: Path) -> None:
    provider = get_provider("debian", environ={"HOME": str(tmp_path)}, which=lambda b: None)
    assert isinstance(provider, DebianInstallProvider)
    assert provider.id == "debian"
    with pytest.raises(ValidationError):
        get_provider("amiga", environ={})


# ---------------------------------------------------------------------------
# Package managers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("factory", "package_manager"),
    [
        (make_windows, "winget"),
        (make_macos, "brew"),
        (make_debian, "apt-get"),
        (make_fedora, "dnf"),
        (make_arch, "pacman"),
    ],
)
def test_package_manager_ids(tmp_path: Path, factory: Callable[..., object], package_manager: str) -> None:
    provider = factory(tmp_path)
    assert provider.package_manager == package_manager


def test_windows_package_commands(tmp_path: Path) -> None:
    provider = make_windows(tmp_path)
    commands = provider.install_system_packages(["Git.Git", "Microsoft.OpenJDK.21"])
    assert len(commands) == 2
    first = commands[0].argv
    assert first[:3] == ["winget", "install", "--id"]
    assert "Git.Git" in first
    assert "--accept-package-agreements" in first
    assert "--accept-source-agreements" in first
    assert provider.install_system_packages([]) == []


def test_debian_package_commands_use_sudo_when_not_admin(tmp_path: Path) -> None:
    admin = make_debian(tmp_path, is_admin=True)
    assert admin.install_system_packages(["scrcpy"])[0].argv[:2] == ["apt-get", "install"]
    user = make_debian(tmp_path, is_admin=False)
    commands = user.install_system_packages(["scrcpy"])
    assert commands[0].argv[:3] == ["sudo", "apt-get", "install"]
    assert commands[0].admin is True
    prerequisites = user.prerequisites()
    assert prerequisites[0].argv[:3] == ["sudo", "apt-get", "update"]


def test_fedora_package_groups_and_packages(tmp_path: Path) -> None:
    provider = make_fedora(tmp_path)
    commands = provider.install_system_packages(["git", "@Development Tools"])
    texts = argv_text(commands)
    assert "dnf install -y git" in texts
    group_command = next(command for command in commands if "group" in command.argv)
    assert group_command.argv[:3] == ["dnf", "group", "install"]
    assert "Development Tools" in group_command.argv
    assert group_command.admin is True


def test_arch_pacman_and_multilib_note(tmp_path: Path) -> None:
    provider = make_arch(tmp_path)
    commands = provider.install_system_packages(["jdk21-openjdk"])
    assert commands[0].argv[0] == "pacman"
    assert commands[0].argv[1:4] == ["-S", "--needed", "--noconfirm"]
    assert "jdk21-openjdk" in commands[0].argv
    assert "multilib" in provider.multilib_note
    assert "multilib" in MULTILIB_NOTE
    plan = provider.virtualization_plan()
    assert any("multilib" in note for note in plan.notes)


def test_macos_cask_and_formula_commands(tmp_path: Path) -> None:
    provider = make_macos(tmp_path)
    commands = provider.install_system_packages(["wget", "cask:android-studio"])
    texts = [command.argv for command in commands]
    assert ["brew", "install", "wget"] in texts
    assert ["brew", "install", "--cask", "android-studio"] in texts


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def test_which_and_command_exists(tmp_path: Path) -> None:
    provider = make_debian(
        tmp_path,
        which=lambda binary: f"/usr/bin/{binary}" if binary in {"git", "curl"} else None,
    )
    assert provider.which("git") == "/usr/bin/git"
    assert provider.command_exists("curl") is True
    assert provider.command_exists("wget") is False


# ---------------------------------------------------------------------------
# Environment handling
# ---------------------------------------------------------------------------
def test_windows_env_commands(tmp_path: Path) -> None:
    provider = make_windows(tmp_path)
    commands = provider.set_env_var("ANDROID_SDK_ROOT", r"C:\Android\Sdk", EnvScope.USER)
    assert len(commands) == 1
    script = commands[0].argv[-1]
    assert "[Environment]::SetEnvironmentVariable" in script
    assert "ANDROID_SDK_ROOT" in script
    assert "C:\\Android\\Sdk" in script
    assert "'User'" in script
    assert commands[0].argv[0] == "powershell"
    machine = provider.set_env_var("ANDROID_SDK_ROOT", r"C:\Android\Sdk", EnvScope.SYSTEM)
    assert machine[0].admin is True
    assert provider.set_env_var("X", "y", EnvScope.PROCESS) == []

    path_commands = provider.add_to_path([r"C:\Android\Sdk\platform-tools"])
    assert "Path" in path_commands[0].argv[-1]
    assert "C:\\Android\\Sdk\\platform-tools" in path_commands[0].argv[-1]
    assert provider.remove_env_var("ANDROID_SDK_ROOT")


def test_posix_env_commands_use_managed_profile_block(tmp_path: Path) -> None:
    provider = make_debian(tmp_path)
    commands = provider.set_env_var("ANDROID_SDK_ROOT", "/opt/android sdk")
    assert commands and commands[0].argv[:2] == ["sh", "-c"]
    script = commands[0].argv[2]
    assert "# >>> avdfleet ANDROID_SDK_ROOT" in script
    assert "# <<< avdfleet ANDROID_SDK_ROOT" in script
    assert "export ANDROID_SDK_ROOT=" in script
    assert str(tmp_path / "home" / ".profile") in script or "$HOME/.profile" in script
    path_commands = provider.add_to_path(["/opt/android/platform-tools"])
    assert "export PATH=" in path_commands[0].argv[2]
    assert "$PATH" in path_commands[0].argv[2]
    assert provider.remove_env_var("ANDROID_SDK_ROOT")


def test_shell_quoting(tmp_path: Path) -> None:
    debian = make_debian(tmp_path)
    assert debian.shell_quote("with space") == "'with space'"
    assert debian.shell_quote("it's") == "'it'\"'\"'s'"
    windows = make_windows(tmp_path)
    assert windows.shell_quote("C:\\Program Files\\Android") == '"C:\\Program Files\\Android"'


# ---------------------------------------------------------------------------
# Admin / reboot
# ---------------------------------------------------------------------------
def test_admin_and_reboot_detection(tmp_path: Path) -> None:
    assert make_debian(tmp_path, is_admin=True).is_admin is True
    assert make_debian(tmp_path, is_admin=False).is_admin is False
    assert make_arch(tmp_path).is_admin is True
    assert make_windows(tmp_path).reboot_required() is False
    assert make_windows(tmp_path, reboot_pending=True).reboot_required() is True
    assert make_debian(tmp_path, reboot_pending=True).reboot_required() is True
    assert make_arch(tmp_path).reboot_required() is False
    assert make_macos(tmp_path, reboot_pending=True).reboot_required() is True


# ---------------------------------------------------------------------------
# Virtualisation
# ---------------------------------------------------------------------------
def test_windows_whpx_plan(tmp_path: Path) -> None:
    disabled = make_windows(tmp_path).virtualization_plan()
    assert disabled.kind is AcceleratorKind.WHPX
    assert disabled.available is True
    assert disabled.enabled is False
    assert disabled.requires_reboot is True
    texts = argv_text(disabled.commands)
    assert "Enable-WindowsOptionalFeature" in texts
    assert "HypervisorPlatform" in texts
    assert "hypervisorlaunchtype" in texts
    assert all(command.admin for command in disabled.commands)

    enabled = make_windows(tmp_path, features={"HypervisorPlatform": True}).virtualization_plan()
    assert enabled.enabled is True
    assert enabled.requires_reboot is False
    assert enabled.commands == ()

    no_firmware = make_windows(tmp_path, firmware_virtualization=False).virtualization_plan()
    assert no_firmware.available is False
    assert no_firmware.commands == ()
    assert any("firmware" in note for note in no_firmware.notes)


def test_windows_aehd_detection_and_migration(tmp_path: Path) -> None:
    provider = make_windows(tmp_path, aehd_installed=True)
    assert provider.aehd_installed() is True
    migration = provider.aehd_migration_plan()
    assert migration.kind is AcceleratorKind.AEHD
    texts = argv_text(migration.commands)
    assert "sc.exe stop aehd" in texts
    assert "sc.exe delete aehd" in texts
    assert "pnputil /delete-driver aehd.inf" in texts
    assert migration.requires_reboot is False
    assert any("WHPX" in note for note in migration.notes)
    assert make_windows(tmp_path, aehd_installed=False).aehd_installed() is False


def test_macos_hvf_plan(tmp_path: Path) -> None:
    provider = make_macos(tmp_path, hv_support=True)
    plan = provider.virtualization_plan()
    assert plan.kind is AcceleratorKind.HVF
    assert plan.available is True
    assert plan.enabled is True
    assert plan.requires_reboot is False
    assert plan.commands == ()
    unavailable = make_macos(tmp_path, hv_support=False).virtualization_plan()
    assert unavailable.available is False
    assert any("HVF" in note for note in unavailable.notes)


@pytest.mark.parametrize("factory", [make_debian, make_fedora, make_arch])
def test_linux_kvm_plan(tmp_path: Path, factory: Callable[..., object]) -> None:
    provider = factory(tmp_path)
    plan = provider.virtualization_plan()
    assert plan.kind is AcceleratorKind.KVM
    assert plan.available is True
    assert plan.enabled is False
    assert plan.requires_reboot is False
    assert plan.udev_rule_path == KVM_UDEV_RULE_PATH
    assert plan.udev_rule_path.endswith("70-avdfleet-kvm.rules")
    assert plan.udev_rule_content == KVM_UDEV_RULE
    assert 'KERNEL=="kvm", GROUP="kvm", MODE="0666"' in (plan.udev_rule_content or "")
    assert plan.groups == ("kvm",)
    texts = argv_text(plan.commands)
    assert "udevadm control --reload-rules" in texts
    assert "usermod -aG kvm tester" in texts
    assert "static_node=kvm" in texts
    assert all(command.admin for command in plan.commands)


def test_linux_kvm_plan_enabled_and_nested(tmp_path: Path) -> None:
    enabled = make_debian(
        tmp_path,
        kvm_available_now=True,
        in_kvm_group=True,
        udev_rule_present=True,
    ).virtualization_plan()
    assert enabled.enabled is True
    assert enabled.commands == ()

    unavailable = make_debian(tmp_path, kvm_available_now=False).virtualization_plan()
    assert unavailable.available is False
    assert any("modprobe" in " ".join(command.argv) for command in unavailable.commands)

    nested = make_arch(tmp_path, nested=True).virtualization_plan()
    assert nested.nested is True
    assert any("nested" in note for note in nested.notes)


def test_macos_command_line_tools_before_homebrew(tmp_path: Path) -> None:
    provider = make_macos(tmp_path, command_line_tools=False, homebrew=False)
    assert provider.package_manager_ready() is False
    prerequisites = provider.prerequisites()
    assert len(prerequisites) == 2
    clt, brew = prerequisites
    assert "xcode-select" in " ".join(clt.argv)
    assert "softwareupdate" in " ".join(clt.argv)
    assert "brew" in " ".join(brew.argv)
    assert clt.admin is True
    clt_index = next(i for i, command in enumerate(prerequisites) if "xcode-select" in " ".join(command.argv))
    brew_index = next(i for i, command in enumerate(prerequisites) if "command -v brew" in " ".join(command.argv))
    assert clt_index < brew_index
    assert make_macos(tmp_path, command_line_tools=True, homebrew=True).package_manager_ready() is True


# ---------------------------------------------------------------------------
# Tool templates
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "factory",
    [make_windows, make_macos, make_debian, make_fedora, make_arch],
)
def test_every_provider_publishes_required_tool_templates(
    tmp_path: Path, factory: Callable[..., object]
) -> None:
    provider = factory(tmp_path)
    templates = provider.install_command_templates
    for tool in REQUIRED_TOOL_TEMPLATES:
        assert tool in templates, tool
        assert templates[tool].commands, f"{provider.id}:{tool} has no commands"
    appium = templates["appium"]
    assert any("appium@4" in " ".join(command.argv) for command in appium.commands)
    driver = templates["appium_driver"]
    assert any("uiautomator2" in command.argv for command in driver.commands)
    assert templates["inspector"].optional is True
    assert templates["studio"].optional is True


def test_platform_specific_tool_commands(tmp_path: Path) -> None:
    windows = make_windows(tmp_path).install_command_templates
    assert "Google.AndroidStudio" in windows["studio"].commands[0].argv
    assert "Genymobile.scrcpy" in windows["scrcpy"].commands[0].argv
    assert "OpenJS.NodeJS.LTS" in windows["node"].commands[0].argv

    macos = make_macos(tmp_path).install_command_templates
    assert macos["jdk"].commands[0].argv[:3] == ["brew", "install", "--cask"]
    assert macos["scrcpy"].commands[0].argv == ["brew", "install", "scrcpy"]

    debian = make_debian(tmp_path).install_command_templates
    assert "openjdk-21-jdk-headless" in debian["jdk"].commands[0].argv
    nodesource = argv_text(debian["node"].commands)
    assert "nodesource" in nodesource
    assert "nodejs" in nodesource
    studio_text = argv_text(debian["studio"].commands)
    assert "android-studio" in studio_text or "avdfleet-android-studio" in studio_text

    fedora = make_fedora(tmp_path).install_command_templates
    assert "java-21-openjdk-devel" in fedora["jdk"].commands[0].argv
    assert "scrcpy" in fedora["scrcpy"].commands[0].argv

    arch = make_arch(tmp_path).install_command_templates
    assert "jdk21-openjdk" in arch["jdk"].commands[0].argv
    assert arch["scrcpy"].commands[0].argv[:2] == ["pacman", "-S"]


# ---------------------------------------------------------------------------
# Locations / downloads
# ---------------------------------------------------------------------------
def test_studio_config_dirs(tmp_path: Path) -> None:
    windows = make_windows(tmp_path)
    newest = tmp_path / "appdata" / "Google" / "AndroidStudio2024.2"
    newest.mkdir(parents=True)
    assert windows.studio_config_dir == newest

    macos = make_macos(tmp_path)
    mac_dir = tmp_path / "home" / "Library" / "Application Support" / "Google" / "AndroidStudio2024.1"
    mac_dir.mkdir(parents=True)
    assert macos.studio_config_dir == mac_dir

    debian = make_debian(tmp_path)
    linux_dir = tmp_path / "xdg" / "Google" / "AndroidStudio2023.3"
    linux_dir.mkdir(parents=True)
    assert debian.studio_config_dir == linux_dir


def test_default_sdk_roots(tmp_path: Path) -> None:
    windows = make_windows(tmp_path)
    assert windows.default_sdk_root == tmp_path / "localappdata" / "Android" / "Sdk"
    macos = make_macos(tmp_path)
    assert macos.default_sdk_root == tmp_path / "home" / "Library" / "Android" / "sdk"
    debian = make_debian(tmp_path)
    assert debian.default_sdk_root == tmp_path / "home" / "Android" / "Sdk"
    assert debian.default_avd_home == tmp_path / "home" / ".android" / "avd"


def test_download_and_extract_command_shapes(tmp_path: Path) -> None:
    linux = make_debian(tmp_path)
    download = linux.download_file("https://example.invalid/x.zip", "/tmp/x.zip", sha256="abc")
    assert download[0].argv[0] == "curl"
    assert "https://example.invalid/x.zip" in download[0].argv
    assert "sha256sum" in download[1].argv[-1]
    extract = linux.extract_zip("/tmp/x.zip", "/tmp/x")
    assert extract.argv[:2] == ["unzip", "-q"]

    windows = make_windows(tmp_path)
    win_download = windows.download_file("https://example.invalid/x.zip", r"C:\tmp\x.zip")
    assert "Invoke-WebRequest" in win_download[0].argv[-1]
    win_extract = windows.extract_zip(r"C:\tmp\x.zip", r"C:\tmp\x")
    assert "Expand-Archive" in win_extract.argv[-1]
    move = windows.move_path(r"C:\tmp\a", r"C:\tmp\b")
    assert "Move-Item" in move.argv[-1]


def test_host_info_reports_platform_and_provider(tmp_path: Path) -> None:
    provider = make_debian(tmp_path)
    info = provider.host_info()
    assert info.os is HostOS.LINUX
    assert info.is_admin is True
    assert info.details["provider"] == "debian"
    assert info.hypervisor.kind is AcceleratorKind.KVM

    windows = make_windows(tmp_path).host_info()
    assert windows.os is HostOS.WINDOWS
    assert windows.hypervisor.kind is AcceleratorKind.WHPX

    mac = make_macos(tmp_path).host_info()
    assert mac.os is HostOS.MACOS
    assert mac.hypervisor.kind is AcceleratorKind.HVF
