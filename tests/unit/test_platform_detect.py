"""Unit tests for host detection (``src/avdfleet/platform/detect.py``).

The tests never require an Android SDK, emulator or hardware virtualisation:
platform overrides from ``contracts/fakes-spec.md`` section 8 plus injected
command runners let them exercise Windows/macOS/Linux paths on any host.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from avdfleet.core.config import Config
from avdfleet.core.interfaces import HostProvider
from avdfleet.core.models import AcceleratorKind, CpuArch, HostInfo, HostOS
from avdfleet.core.paths import Paths
from avdfleet.platform import (
    PlatformService,
    detect_host,
    get_package_manager,
    get_provider_name,
    real_host_os,
)
from avdfleet.platform import detect as detect_module
from avdfleet.platform.detect import (
    WINDOWS_11_MIN_BUILD,
    PlatformDetector,
    clear_host_cache,
    normalise_arch,
    normalise_host_os,
    parse_windows_build,
    sdk_executables,
    sdk_paths,
    tool_executable,
    which_tool,
    windows_product_name,
)

SYSTEMINFO_HYPERVISOR = """
OS Name:                   Microsoft Windows 11 Pro
OS Version:                10.0.22631 N/A Build 22631
System Manufacturer:       Dell Inc.
System Model:              Precision 7920 Tower
Hyper-V Requirements:      A hypervisor has been detected. Features required for Hyper-V
                           will not be displayed.
"""

SYSTEMINFO_NO_HYPERVISOR = """
OS Name:                   Microsoft Windows 11 Pro
OS Version:                10.0.22631 N/A Build 22631
System Manufacturer:       Dell Inc.
System Model:              Precision 7920 Tower
Hyper-V Requirements:      VM Monitor Mode Extensions: Yes
                           Virtualization Enabled In Firmware: Yes
                           Second Level Address Translation: Yes
                           Data Execution Prevention Available: Yes
"""

SYSTEMINFO_VM = """
OS Name:                   Microsoft Windows 11 Pro
OS Version:                10.0.22631 N/A Build 22631
System Manufacturer:       VMware, Inc.
System Model:              VMware20,1
Hyper-V Requirements:      A hypervisor has been detected. Features required for Hyper-V
                           will not be displayed.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_tool_file(directory: Path, name: str) -> Path:
    """Create a placeholder executable named ``name`` (``.exe`` on Windows)."""
    directory.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if os.name == "nt" else ""
    path = directory / f"{name}{suffix}"
    path.write_text("", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o755)
    return path


def _make_sdk(tmp_path: Path) -> Path:
    sdk = tmp_path / "sdk"
    _fake_tool_file(sdk / "cmdline-tools" / "latest" / "bin", "sdkmanager")
    _fake_tool_file(sdk / "cmdline-tools" / "latest" / "bin", "avdmanager")
    _fake_tool_file(sdk / "platform-tools", "adb")
    _fake_tool_file(sdk / "emulator", "emulator")
    _fake_tool_file(sdk / "build-tools" / "34.0.0", "aapt2")
    _fake_tool_file(sdk / "build-tools" / "35.0.0", "aapt2")
    _fake_tool_file(sdk / "jbr" / "bin", "java")
    (sdk / "licenses").mkdir(parents=True, exist_ok=True)
    (sdk / "licenses" / "android-sdk-license").write_text("hash", encoding="utf-8")
    (sdk / "system-images").mkdir(parents=True, exist_ok=True)
    return sdk


def _fake_runner(responses: dict[str, tuple[int, str, str]]):
    """Build a command runner keyed on the executable basename."""

    def runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
        name = Path(str(argv[0])).stem.lower()
        if name == "sc" and len(argv) >= 3:
            key = f"sc:{argv[2]}"
        elif name == "emulator" and "-accel-check" in argv:
            key = "emulator-accel"
        elif name == "powershell":
            key = "powershell"
        else:
            key = name
        if key in responses:
            return responses[key]
        return 127, "", f"unexpected command: {name}"

    return runner


def _no_whpx_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(PlatformDetector, "_whpx_api_probe", lambda self: None)


# ---------------------------------------------------------------------------
# OS / arch overrides
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fake_os", "expected"),
    [
        ("windows", HostOS.WINDOWS),
        ("macos", HostOS.MACOS),
        ("linux", HostOS.LINUX),
    ],
)
def test_detect_honours_fake_host_os(fake_os: str, expected: HostOS) -> None:
    env = {"AVDFLEET_FAKE_HOST_OS": fake_os, "AVDFLEET_FAKE_ACCEL": "none", "PATH": ""}
    host = detect_host(environ=env)
    assert host.os is expected
    assert host.hypervisor.available is False
    assert host.acceleration_available is False
    assert host.hypervisor.kind is AcceleratorKind.NONE
    assert host.details["fake_overrides"]["AVDFLEET_FAKE_HOST_OS"] == fake_os
    assert host.cpu_count >= 1
    assert host.python_version.startswith("3.")


@pytest.mark.parametrize(
    ("fake_arch", "expected"),
    [("x86_64", CpuArch.X86_64), ("arm64", CpuArch.ARM64)],
)
def test_detect_honours_fake_arch(fake_arch: str, expected: CpuArch) -> None:
    env = {
        "AVDFLEET_FAKE_HOST_OS": "linux",
        "AVDFLEET_FAKE_ARCH": fake_arch,
        "AVDFLEET_FAKE_ACCEL": "none",
        "PATH": "",
    }
    host = detect_host(environ=env)
    assert host.arch is expected
    assert host.details["fake_overrides"]["AVDFLEET_FAKE_ARCH"] == fake_arch


def test_invalid_fake_overrides_are_ignored() -> None:
    env = {
        "AVDFLEET_FAKE_HOST_OS": "plan9",
        "AVDFLEET_FAKE_ARCH": "sparc",
        "AVDFLEET_FAKE_ACCEL": "none",
        "PATH": "",
    }
    host = detect_host(environ=env)
    assert host.os is real_host_os()
    assert host.arch in (CpuArch.X86_64, CpuArch.ARM64, CpuArch.UNKNOWN)


@pytest.mark.parametrize(
    ("value", "kind", "available"),
    [
        ("whpx", AcceleratorKind.WHPX, True),
        ("aehd", AcceleratorKind.AEHD, True),
        ("kvm", AcceleratorKind.KVM, True),
        ("hvf", AcceleratorKind.HVF, True),
        ("none", AcceleratorKind.NONE, False),
    ],
)
def test_detect_honours_fake_accel(
    value: str, kind: AcceleratorKind, available: bool
) -> None:
    env = {
        "AVDFLEET_FAKE_HOST_OS": real_host_os().value,
        "AVDFLEET_FAKE_ACCEL": value,
        "PATH": "",
    }
    host = detect_host(environ=env)
    assert host.hypervisor.kind is kind
    assert host.hypervisor.available is available
    assert host.acceleration_available is available


def test_unrecognised_fake_accel_degrades() -> None:
    env = {"AVDFLEET_FAKE_HOST_OS": "linux", "AVDFLEET_FAKE_ACCEL": "tcg", "PATH": ""}
    host = detect_host(environ=env)
    assert host.hypervisor.kind is AcceleratorKind.NONE
    assert host.hypervisor.available is False
    assert "nrecognised" in host.hypervisor.detail


def test_normalisers_accept_aliases() -> None:
    assert normalise_host_os("Darwin") is HostOS.MACOS
    assert normalise_host_os("win32") is HostOS.WINDOWS
    assert normalise_host_os("GNU/Linux") is HostOS.LINUX
    assert normalise_host_os("solaris") is HostOS.UNKNOWN
    assert normalise_host_os(HostOS.MACOS) is HostOS.MACOS
    assert normalise_arch("aarch64") is CpuArch.ARM64
    assert normalise_arch("AMD64") is CpuArch.X86_64
    assert normalise_arch("sparc") is CpuArch.UNKNOWN


# ---------------------------------------------------------------------------
# Windows product identity (the build number, not ProductName, is authoritative)
# ---------------------------------------------------------------------------


def test_parse_windows_build_accepts_probe_shapes() -> None:
    assert parse_windows_build("26200") == 26200
    assert parse_windows_build("10.0.26200") == 26200
    assert parse_windows_build(22631) == 22631
    assert parse_windows_build("") is None
    assert parse_windows_build(None) is None
    assert parse_windows_build(0) is None
    assert parse_windows_build("not-a-build") is None


@pytest.mark.parametrize(
    ("product", "build", "expected"),
    [
        ("Windows 10 Pro", "26200", "Windows 11 Pro"),
        ("Windows 10 Pro", 22631, "Windows 11 Pro"),
        ("Microsoft Windows 10 Enterprise", "10.0.26100", "Microsoft Windows 11 Enterprise"),
        ("Windows 10 Pro", "19045", "Windows 10 Pro"),
        ("Windows 11 Pro", "19045", "Windows 10 Pro"),
        ("Windows Server 2022 Standard", "20348", "Windows Server 2022 Standard"),
        ("", "26200", ""),
        (None, "26200", ""),
        ("Windows 10 Pro", None, "Windows 10 Pro"),
    ],
)
def test_windows_product_name_follows_the_build_number(
    product: str | None, build: str | int | None, expected: str
) -> None:
    assert windows_product_name(product, build) == expected
    assert WINDOWS_11_MIN_BUILD == 22000


def _fake_windows_registry(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    monkeypatch.setattr(detect_module, "_registry_value", lambda _subkey, name: values.get(name))


def test_windows_detection_reports_windows_11_from_build_26200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detect_module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        detect_module,
        "_platform",
        SimpleNamespace(release=lambda: "11", version=lambda: "10.0.26200"),
    )
    _fake_windows_registry(
        monkeypatch,
        ProductName="Windows 10 Pro",
        DisplayVersion="25H2",
        CurrentBuildNumber="26200",
    )
    detector = PlatformDetector(environ={"PATH": ""})

    os_name, os_version, details = detector._detect_os_identity(HostOS.WINDOWS, False)  # noqa: SLF001

    assert os_name == "Windows 11 Pro"
    assert os_version == "11 25H2 (build 26200)"
    assert details["windows_product"] == "Windows 11 Pro"
    # The raw registry value stays available as evidence.
    assert details["windows_product_registry"] == "Windows 10 Pro"
    assert details["windows_build_number"] == 26200


def test_windows_detection_corrects_legacy_python_release_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detect_module, "sys", SimpleNamespace(platform="win32"))
    # Python before 3.13 reports 10.0 for Windows 11.
    monkeypatch.setattr(
        detect_module,
        "_platform",
        SimpleNamespace(release=lambda: "10", version=lambda: "10.0.26200"),
    )
    _fake_windows_registry(
        monkeypatch,
        ProductName="Windows 10 Home",
        DisplayVersion="24H2",
        CurrentBuildNumber="26100",
    )
    detector = PlatformDetector(environ={"PATH": ""})

    os_name, os_version, details = detector._detect_os_identity(HostOS.WINDOWS, False)  # noqa: SLF001

    assert os_name == "Windows 11 Home"
    assert os_version == "11 24H2 (build 26100)"
    assert details["windows_release"] == "11"
    assert details["windows_release_reported"] == "10"


def test_windows_detection_keeps_genuine_windows_10(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detect_module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        detect_module,
        "_platform",
        SimpleNamespace(release=lambda: "10", version=lambda: "10.0.19045"),
    )
    _fake_windows_registry(
        monkeypatch,
        ProductName="Windows 10 Pro",
        DisplayVersion="22H2",
        CurrentBuildNumber="19045",
    )
    detector = PlatformDetector(environ={"PATH": ""})

    os_name, os_version, details = detector._detect_os_identity(HostOS.WINDOWS, False)  # noqa: SLF001

    assert os_name == "Windows 10 Pro"
    assert os_version == "10 22H2 (build 19045)"
    assert "windows_product_registry" not in details
    assert details["windows_build_number"] == 19045


def test_fake_environment_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_host_cache()
    monkeypatch.setenv("AVDFLEET_FAKE_HOST_OS", "linux")
    monkeypatch.setenv("AVDFLEET_FAKE_ACCEL", "none")
    host = detect_host()
    assert host.os is HostOS.LINUX
    assert detect_module._REAL_HOST_CACHE is None  # noqa: SLF001 - cache invariant


def test_real_host_detection_never_raises() -> None:
    clear_host_cache()
    host = detect_host()
    assert host.os in (HostOS.WINDOWS, HostOS.MACOS, HostOS.LINUX, HostOS.UNKNOWN)
    assert host.arch in (CpuArch.X86_64, CpuArch.ARM64, CpuArch.UNKNOWN)
    assert host.cpu_count >= 1
    assert host.total_ram_gb >= 0
    assert host.free_disk_gb >= 0
    assert isinstance(host.is_admin, bool)
    assert isinstance(host.is_wsl, bool)
    assert isinstance(host.is_container, bool)
    assert isinstance(host.details, dict)
    assert host.acceleration_available == host.hypervisor.available
    # A second call is served from the detector cache without re-probing.
    assert detect_host().os is host.os


def test_wsl_and_container_detection_from_environment() -> None:
    base = {"AVDFLEET_FAKE_HOST_OS": "linux", "AVDFLEET_FAKE_ACCEL": "none", "PATH": ""}
    wsl_host = detect_host(environ={**base, "WSL_DISTRO_NAME": "Ubuntu-24.04"})
    assert wsl_host.is_wsl is True
    container_host = detect_host(environ={**base, "container": "docker"})
    assert container_host.is_container is True
    assert container_host.is_wsl is False


# ---------------------------------------------------------------------------
# Windows acceleration probes
# ---------------------------------------------------------------------------


def test_windows_whpx_available_via_feature_and_systeminfo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_whpx_api(monkeypatch)
    env = {"AVDFLEET_FAKE_HOST_OS": "windows", "AVDFLEET_FAKE_ARCH": "x86_64", "PATH": ""}
    runner = _fake_runner(
        {
            "systeminfo": (0, SYSTEMINFO_HYPERVISOR, ""),
            "powershell": (0, "Enabled", ""),
            "sc:aehd": (1060, "", "service does not exist"),
            "sc:gvm": (1060, "", "service does not exist"),
        }
    )
    host = detect_host(environ=env, command_runner=runner)
    hypervisor = host.hypervisor
    assert hypervisor.kind is AcceleratorKind.WHPX
    assert hypervisor.available is True
    assert hypervisor.requires_reboot is False
    assert "WHPX" in hypervisor.detail
    assert host.acceleration_available is True
    assert hypervisor.nested is False
    probes = host.details["windows_acceleration"]["feature_probes"]
    assert probes and probes[0]["state"] == "enabled"


def test_windows_whpx_requires_reboot_when_not_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_whpx_api(monkeypatch)
    env = {"AVDFLEET_FAKE_HOST_OS": "windows", "AVDFLEET_FAKE_ARCH": "x86_64", "PATH": ""}
    runner = _fake_runner(
        {
            "systeminfo": (0, SYSTEMINFO_NO_HYPERVISOR, ""),
            "powershell": (0, "Enabled", ""),
            "sc:aehd": (1060, "", ""),
            "sc:gvm": (1060, "", ""),
        }
    )
    host = detect_host(environ=env, command_runner=runner)
    hypervisor = host.hypervisor
    assert hypervisor.kind is AcceleratorKind.WHPX
    assert hypervisor.available is False
    assert hypervisor.requires_reboot is True
    assert "reboot" in hypervisor.detail.lower()
    assert host.acceleration_available is False


def test_windows_whpx_pending_enable_requires_reboot(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_whpx_api(monkeypatch)
    env = {"AVDFLEET_FAKE_HOST_OS": "windows", "AVDFLEET_FAKE_ARCH": "x86_64", "PATH": ""}
    runner = _fake_runner(
        {
            "systeminfo": (0, SYSTEMINFO_NO_HYPERVISOR, ""),
            "powershell": (0, "EnablePending", ""),
            "sc:aehd": (1060, "", ""),
            "sc:gvm": (1060, "", ""),
        }
    )
    host = detect_host(environ=env, command_runner=runner)
    assert host.hypervisor.kind is AcceleratorKind.WHPX
    assert host.hypervisor.available is False
    assert host.hypervisor.requires_reboot is True


def test_windows_aehd_service_is_used_as_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_whpx_api(monkeypatch)
    env = {"AVDFLEET_FAKE_HOST_OS": "windows", "AVDFLEET_FAKE_ARCH": "x86_64", "PATH": ""}
    runner = _fake_runner(
        {
            "systeminfo": (0, SYSTEMINFO_NO_HYPERVISOR, ""),
            "powershell": (1, "", "requires elevation"),
            "dism": (1, "", "requires elevation"),
            "sc:aehd": (0, "SERVICE_NAME: aehd\n        STATE   : 4  RUNNING", ""),
            "sc:gvm": (1060, "", ""),
        }
    )
    host = detect_host(environ=env, command_runner=runner)
    hypervisor = host.hypervisor
    assert hypervisor.kind is AcceleratorKind.AEHD
    assert hypervisor.available is True
    assert host.acceleration_available is True


def test_windows_no_acceleration_degrades_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_whpx_api(monkeypatch)
    env = {"AVDFLEET_FAKE_HOST_OS": "windows", "AVDFLEET_FAKE_ARCH": "x86_64", "PATH": ""}
    runner = _fake_runner(
        {
            "systeminfo": (0, SYSTEMINFO_NO_HYPERVISOR, ""),
            "powershell": (1, "", "requires elevation"),
            "dism": (1, "", "requires elevation"),
            "sc:aehd": (1060, "", ""),
            "sc:gvm": (1060, "", ""),
        }
    )
    host = detect_host(environ=env, command_runner=runner)
    hypervisor = host.hypervisor
    assert hypervisor.kind is AcceleratorKind.NONE
    assert hypervisor.available is False
    assert hypervisor.requires_reboot is False
    assert hypervisor.nested is False
    assert "No hardware acceleration" in hypervisor.detail


def test_windows_hypervisor_without_whpx_evidence_is_not_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_whpx_api(monkeypatch)
    env = {"AVDFLEET_FAKE_HOST_OS": "windows", "AVDFLEET_FAKE_ARCH": "x86_64", "PATH": ""}
    runner = _fake_runner(
        {
            "systeminfo": (0, SYSTEMINFO_HYPERVISOR, ""),
            "powershell": (1, "", "requires elevation"),
            "dism": (1, "", "requires elevation"),
            "sc:aehd": (1060, "", ""),
            "sc:gvm": (1060, "", ""),
        }
    )
    host = detect_host(environ=env, command_runner=runner)
    assert host.hypervisor.kind is AcceleratorKind.WHPX
    assert host.hypervisor.available is False
    assert "accel-check" in host.hypervisor.detail


def test_windows_nested_virtual_machine_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_whpx_api(monkeypatch)
    env = {"AVDFLEET_FAKE_HOST_OS": "windows", "AVDFLEET_FAKE_ARCH": "x86_64", "PATH": ""}
    runner = _fake_runner(
        {
            "systeminfo": (0, SYSTEMINFO_VM, ""),
            "powershell": (0, "Enabled", ""),
            "sc:aehd": (1060, "", ""),
            "sc:gvm": (1060, "", ""),
        }
    )
    host = detect_host(environ=env, command_runner=runner)
    assert host.hypervisor.nested is True
    assert host.hypervisor.available is True


# ---------------------------------------------------------------------------
# macOS and Linux acceleration probes
# ---------------------------------------------------------------------------


def test_macos_apple_silicon_hvf_is_available() -> None:
    env = {
        "AVDFLEET_FAKE_HOST_OS": "macos",
        "AVDFLEET_FAKE_ARCH": "arm64",
        "PATH": "",
    }
    host = detect_host(environ=env)
    assert host.hypervisor.kind is AcceleratorKind.HVF
    assert host.hypervisor.available is True
    assert host.acceleration_available is True
    assert host.hypervisor.nested is None


def test_macos_hvf_probe_uses_sysctl() -> None:
    env = {
        "AVDFLEET_FAKE_HOST_OS": "macos",
        "AVDFLEET_FAKE_ARCH": "x86_64",
        "PATH": "",
    }
    runner = _fake_runner({"sysctl": (0, "1\n", "")})
    host = detect_host(environ=env, command_runner=runner)
    assert host.hypervisor.kind is AcceleratorKind.HVF
    assert host.hypervisor.available is True


def test_macos_hvf_unavailable_degrades() -> None:
    env = {
        "AVDFLEET_FAKE_HOST_OS": "macos",
        "AVDFLEET_FAKE_ARCH": "x86_64",
        "PATH": "",
    }
    runner = _fake_runner({"sysctl": (127, "", "sysctl not found")})
    host = detect_host(environ=env, command_runner=runner)
    assert host.hypervisor.kind is AcceleratorKind.HVF
    assert host.hypervisor.available is False
    assert "kern.hv_support" in host.hypervisor.detail


def test_linux_missing_dev_kvm_degrades(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(detect_module, "kvm_device_path", lambda: tmp_path / "missing-kvm")
    env = {"AVDFLEET_FAKE_HOST_OS": "linux", "AVDFLEET_FAKE_ARCH": "x86_64", "PATH": ""}
    host = detect_host(environ=env)
    assert host.hypervisor.kind is AcceleratorKind.NONE
    assert host.hypervisor.available is False
    assert "/dev/kvm" in host.hypervisor.detail
    assert host.acceleration_available is False


def test_linux_accessible_kvm_is_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kvm = tmp_path / "kvm"
    kvm.write_text("", encoding="utf-8")
    monkeypatch.setattr(detect_module, "kvm_device_path", lambda: kvm)
    monkeypatch.setattr(detect_module, "_kvm_accessible", lambda path: True)
    monkeypatch.setattr(detect_module, "kvm_group_member", lambda: True)
    env = {"AVDFLEET_FAKE_HOST_OS": "linux", "AVDFLEET_FAKE_ARCH": "x86_64", "PATH": ""}
    host = detect_host(environ=env)
    assert host.hypervisor.kind is AcceleratorKind.KVM
    assert host.hypervisor.available is True
    raw = host.details["linux_acceleration"]
    assert raw["kvm_path"] == str(kvm)
    assert raw["kvm_group_member"] is True
    assert raw["kvm_group"] == "kvm"


def test_linux_inaccessible_kvm_reports_group_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kvm = tmp_path / "kvm"
    kvm.write_text("", encoding="utf-8")
    monkeypatch.setattr(detect_module, "kvm_device_path", lambda: kvm)
    monkeypatch.setattr(detect_module, "_kvm_accessible", lambda path: False)
    monkeypatch.setattr(detect_module, "_linux_is_virtual_machine", lambda: True)
    env = {"AVDFLEET_FAKE_HOST_OS": "linux", "AVDFLEET_FAKE_ARCH": "x86_64", "PATH": ""}
    host = detect_host(environ=env)
    assert host.hypervisor.kind is AcceleratorKind.KVM
    assert host.hypervisor.available is False
    assert "kvm" in host.hypervisor.detail.lower()
    assert host.hypervisor.nested is True


# ---------------------------------------------------------------------------
# Probe failures never raise
# ---------------------------------------------------------------------------


def test_failing_command_runner_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_whpx_api(monkeypatch)

    def boom(argv: list[str], timeout: float) -> tuple[int, str, str]:
        raise RuntimeError("probe exploded")

    env = {"AVDFLEET_FAKE_HOST_OS": "windows", "AVDFLEET_FAKE_ARCH": "x86_64", "PATH": ""}
    host = detect_host(environ=env, command_runner=boom)
    assert host.os is HostOS.WINDOWS
    assert host.hypervisor.available is False
    assert host.acceleration_available is False
    assert "windows_acceleration" in host.details


# ---------------------------------------------------------------------------
# SDK path resolution
# ---------------------------------------------------------------------------


def test_sdk_paths_resolve_components_and_environment(tmp_path: Path) -> None:
    sdk = _make_sdk(tmp_path)
    avd_home = tmp_path / "avd"
    env = {
        "AVDFLEET_FAKE_SDK_ROOT": str(sdk),
        "ANDROID_AVD_HOME": str(avd_home),
        "JAVA_HOME": str(sdk / "jbr"),
        "PATH": "/original/bin",
    }
    resolved = sdk_paths(environ=env)
    assert Path(resolved.sdk_root) == sdk
    assert Path(resolved.cmdline_tools) == sdk / "cmdline-tools" / "latest"
    assert Path(resolved.platform_tools) == sdk / "platform-tools"
    assert Path(resolved.emulator) == sdk / "emulator"
    assert Path(resolved.build_tools).name == "35.0.0"
    assert Path(resolved.licenses_dir) == sdk / "licenses"
    assert Path(resolved.avd_home) == avd_home
    assert resolved.env["ANDROID_SDK_ROOT"] == str(sdk)
    assert resolved.env["ANDROID_HOME"] == str(sdk)
    assert resolved.env["ANDROID_AVD_HOME"] == str(avd_home)
    assert resolved.env["JAVA_HOME"] == str(sdk / "jbr")
    path_entries = resolved.env["PATH"].split(os.pathsep)
    assert str(sdk / "platform-tools") in path_entries
    assert str(sdk / "cmdline-tools" / "latest" / "bin") in path_entries
    assert path_entries[-1] == "/original/bin"


def test_sdk_paths_find_android_cli(tmp_path: Path) -> None:
    sdk = _make_sdk(tmp_path)
    _fake_tool_file(sdk / "cmdline-tools" / "latest" / "bin", "android")
    resolved = sdk_paths(environ={"AVDFLEET_FAKE_SDK_ROOT": str(sdk), "PATH": ""})
    assert resolved.android_cli is not None
    assert Path(resolved.android_cli).name.startswith("android")


def test_sdk_paths_config_root_wins_over_environment(tmp_path: Path) -> None:
    config_sdk = tmp_path / "configured-sdk"
    config_sdk.mkdir()
    env_sdk = tmp_path / "env-sdk"
    env_sdk.mkdir()
    config = Config(sdk_root=str(config_sdk))
    resolved = sdk_paths(config, environ={"AVDFLEET_FAKE_SDK_ROOT": str(env_sdk), "PATH": ""})
    assert Path(resolved.sdk_root) == config_sdk


def test_sdk_paths_use_paths_when_no_config_root(tmp_path: Path) -> None:
    paths = Paths(root=tmp_path / "root", portable=True)
    resolved = sdk_paths(None, paths, environ={"PATH": ""})
    assert Path(resolved.sdk_root) == paths.sdk_root
    assert resolved.cmdline_tools is None
    assert resolved.platform_tools is None
    assert resolved.env["ANDROID_SDK_ROOT"] == str(paths.sdk_root)


def test_sdk_paths_default_root_does_not_touch_disk() -> None:
    resolved = sdk_paths(environ={"PATH": ""})
    assert Path(resolved.sdk_root).name == "sdk"
    assert resolved.platform_tools is None
    assert resolved.emulator is None


def test_tool_executable_prefers_sdk_component(tmp_path: Path) -> None:
    sdk = _make_sdk(tmp_path)
    resolved = sdk_paths(environ={"AVDFLEET_FAKE_SDK_ROOT": str(sdk), "PATH": ""})
    adb = tool_executable("adb", resolved, environ={"PATH": ""})
    assert adb is not None
    assert Path(adb).parent == sdk / "platform-tools"
    assert tool_executable("definitely-not-a-tool", resolved, environ={"PATH": ""}) is None


def test_sdk_executables_maps_installed_tools(tmp_path: Path) -> None:
    sdk = _make_sdk(tmp_path)
    _fake_tool_file(sdk / "cmdline-tools" / "latest" / "bin", "android")
    resolved = sdk_paths(environ={"AVDFLEET_FAKE_SDK_ROOT": str(sdk), "PATH": ""})
    executables = sdk_executables(resolved, environ={"PATH": ""})
    assert set(executables) >= {"sdkmanager", "avdmanager", "adb", "emulator", "aapt2", "android"}
    assert Path(executables["adb"]).parent == sdk / "platform-tools"
    assert Path(executables["sdkmanager"]).parent == sdk / "cmdline-tools" / "latest" / "bin"
    assert Path(executables["android"]).name.startswith("android")


def test_which_tool_is_environment_scoped(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _fake_tool_file(bin_dir, "mytool")
    assert which_tool("mytool", environ={"PATH": str(bin_dir)}) is not None
    assert which_tool("mytool", environ={"PATH": ""}) is None


# ---------------------------------------------------------------------------
# Platform service / provider selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host_os", "expected"),
    [
        (HostOS.WINDOWS, "windows"),
        (HostOS.MACOS, "macos"),
        ("windows", "windows"),
        ("macos", "macos"),
        (HostOS.UNKNOWN, "unknown"),
        ("plan9", "unknown"),
    ],
)
def test_get_provider_name_for_os(host_os: Any, expected: str) -> None:
    assert get_provider_name(host_os) == expected


def test_get_provider_name_accepts_host_info() -> None:
    assert get_provider_name(HostInfo(os=HostOS.WINDOWS)) == "windows"
    assert get_provider_name(HostInfo(os=HostOS.MACOS)) == "macos"
    assert get_provider_name(HostInfo(os=HostOS.LINUX), distro="fedora") == "fedora"


@pytest.mark.parametrize(
    ("distro", "expected"),
    [
        ("ubuntu", "debian"),
        ("debian", "debian"),
        ("linuxmint", "debian"),
        ("fedora", "fedora"),
        ("rocky", "fedora"),
        ("rhel", "fedora"),
        ("arch", "arch"),
        ("manjaro", "arch"),
        ("nixos", "debian"),
    ],
)
def test_get_provider_name_for_linux_distros(distro: str, expected: str) -> None:
    assert get_provider_name(HostOS.LINUX, distro=distro) == expected


def test_get_provider_name_reads_distro_override_from_environment() -> None:
    assert get_provider_name(HostOS.LINUX, environ={"AVDFLEET_DISTRO": "fedora"}) == "fedora"
    assert get_provider_name(None, environ={"AVDFLEET_DISTRO": "arch"}) in {"arch", "windows", "macos"}


def test_get_package_manager_mapping() -> None:
    assert get_package_manager("windows") == "winget"
    assert get_package_manager("macos") == "brew"
    assert get_package_manager("debian") == "apt-get"
    assert get_package_manager("fedora") == "dnf"
    assert get_package_manager("arch") == "pacman"
    assert get_package_manager("unknown") == ""


def test_platform_service_bundles_detector_and_doctor(tmp_path: Path) -> None:
    sdk = _make_sdk(tmp_path)
    config = Config(sdk_root=str(sdk))
    paths = Paths(root=tmp_path / "root", portable=True)
    env = {
        "AVDFLEET_FAKE_HOST_OS": "windows",
        "AVDFLEET_FAKE_ARCH": "x86_64",
        "AVDFLEET_FAKE_ACCEL": "whpx",
        "PATH": "",
    }
    service = PlatformService(config, paths, environ=env)
    assert isinstance(service, HostProvider)
    host = service.detect()
    assert host.os is HostOS.WINDOWS
    assert host.acceleration_available is True
    assert service.detect() == host
    resolved = service.sdk_paths()
    assert Path(resolved.sdk_root) == sdk
    assert service.platform == "windows"
    assert service.package_manager() == "winget"
    report = service.run_doctor()
    assert report.checks
    checks = service.doctor_checks()
    assert isinstance(checks, list) and checks
    assert service.fix("not-a-check").ok is False


def test_platform_service_selects_linux_provider(tmp_path: Path) -> None:
    config = Config(sdk_root=str(tmp_path / "sdk"))
    paths = Paths(root=tmp_path / "root", portable=True)
    env = {
        "AVDFLEET_FAKE_HOST_OS": "linux",
        "AVDFLEET_FAKE_ACCEL": "kvm",
        "AVDFLEET_DISTRO": "arch",
        "PATH": "",
    }
    service = PlatformService(config, paths, environ=env)
    assert service.platform == "arch"
    assert service.package_manager() == "pacman"
