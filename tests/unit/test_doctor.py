"""Unit tests for the platform doctor (``src/avdfleet/platform/doctor.py``).

All checks are exercised with a synthetic :class:`HostInfo` and a temporary
paths/config tree, so the suite never needs a real SDK, emulator, hypervisor or
any network access.
"""

from __future__ import annotations

import contextlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from avdfleet.core.config import Config
from avdfleet.core.models import (
    AcceleratorInfo,
    AcceleratorKind,
    CheckStatus,
    CpuArch,
    DoctorReport,
    HostInfo,
    HostOS,
)
from avdfleet.core.paths import Paths
from avdfleet.core.ports import find_free_even_port, find_free_port
from avdfleet.platform import doctor as doctor_module
from avdfleet.platform.doctor import (
    CHECK_IDS,
    FAST_CHECK_IDS,
    SLOW_CHECK_IDS,
    Doctor,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_tool_file(directory: Path, name: str) -> Path:
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
    _fake_tool_file(sdk / "build-tools" / "35.0.0", "aapt2")
    _fake_tool_file(sdk / "jbr" / "bin", "java")
    (sdk / "licenses").mkdir(parents=True, exist_ok=True)
    (sdk / "licenses" / "android-sdk-license").write_text("license-hash", encoding="utf-8")
    return sdk


def _make_tool_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    for name in ("node", "npm", "appium"):
        _fake_tool_file(bin_dir, name)
    return bin_dir


def _free_ports() -> tuple[int, int, int]:
    """Return ``(api_port, appium_port, console_port)`` that are free right now.

    Console ports must be even and the following odd port must also be free.
    The previous approach reserved ten random ephemeral sockets and hoped that
    two adjacent ports happened to be among them, which is flaky on Linux/macOS.
    """
    console = find_free_even_port(24000, 30000)
    api_port = find_free_port(30001, 40000)
    appium_port = find_free_port(40001, 50000)
    if console is None or api_port is None or appium_port is None:
        raise RuntimeError("could not reserve free test ports")
    return api_port, appium_port, console


def _config_with_free_ports(sdk: Path, tmp_path: Path) -> Config:
    api_port, appium_port, console = _free_ports()
    return Config(
        sdk_root=str(sdk),
        data_dir=str(tmp_path / "data"),
        avd_home=str(tmp_path / "avd"),
        api_port=api_port,
        appium_port_start=appium_port,
        port_range_start=console,
        port_range_end=console + 6,
    )


def _host(**overrides: Any) -> HostInfo:
    payload: dict[str, Any] = {
        "os": HostOS.WINDOWS,
        "os_name": "Windows 11 Pro",
        "os_version": "11 (build 22631)",
        "arch": CpuArch.X86_64,
        "machine": "AMD64",
        "python_version": "3.12.0",
        "is_admin": True,
        "cpu_count": 8,
        "total_ram_gb": 64.0,
        "free_disk_gb": 512.0,
        "hypervisor": AcceleratorInfo(
            kind=AcceleratorKind.WHPX,
            available=True,
            detail="WHPX is active (test fixture)",
        ),
        "acceleration_available": True,
    }
    payload.update(overrides)
    return HostInfo(**payload)


def _unavailable_host(os_name: HostOS = HostOS.WINDOWS, **overrides: Any) -> HostInfo:
    detail = "No hardware acceleration detected (test fixture)"
    payload: dict[str, Any] = {
        "os": os_name,
        "os_name": "Windows" if os_name is HostOS.WINDOWS else os_name.value,
        "hypervisor": AcceleratorInfo(
            kind=AcceleratorKind.NONE, available=False, detail=detail
        ),
        "acceleration_available": False,
    }
    payload.update(overrides)
    return _host(**payload)


def _fake_runner(responses: dict[str, tuple[int, str, str]]):
    def runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
        name = Path(str(argv[0])).stem.lower()
        if name == "appium" and len(argv) > 1 and argv[1] == "driver":
            key = "appium-driver"
        elif name == "emulator" and "-accel-check" in argv:
            key = "emulator-accel"
        else:
            key = name
        if key in responses:
            return responses[key]
        return 127, "", f"unexpected command: {name}"

    return runner


def _checks(report: DoctorReport) -> dict[str, Any]:
    return {check.id: check for check in report.checks}


def _doctor(
    tmp_path: Path,
    *,
    host: HostInfo | None = None,
    include_sdk: bool = True,
    environ: dict[str, str] | None = None,
    command_runner: Any = None,
) -> tuple[Doctor, Config, Paths]:
    sdk = _make_sdk(tmp_path) if include_sdk else tmp_path / "sdk"
    config = _config_with_free_ports(sdk, tmp_path) if include_sdk else Config(sdk_root=str(sdk))
    paths = Paths(root=tmp_path / "root", portable=True)
    doctor = Doctor(
        config,
        paths,
        host=host if host is not None else _host(),
        environ=environ if environ is not None else {"PATH": ""},
        command_runner=command_runner,
    )
    return doctor, config, paths


# ---------------------------------------------------------------------------
# Report shape and status contract
# ---------------------------------------------------------------------------


def test_report_shape_and_slow_checks_skipped(tmp_path: Path) -> None:
    paths = Paths(root=tmp_path / "root", portable=True)
    doctor = Doctor(
        Config(sdk_root=str(tmp_path / "sdk")),
        paths,
        host=_unavailable_host(),
        environ={"PATH": ""},
    )
    report = doctor.run()
    assert isinstance(report, DoctorReport)
    assert [check.id for check in report.checks] == list(CHECK_IDS)
    assert len({check.id for check in report.checks}) == len(CHECK_IDS)
    for check in report.checks:
        assert check.id in CHECK_IDS
        assert check.title
        assert check.status in set(CheckStatus)
    assert report.checked_at.tzinfo is not None
    assert report.host.os is HostOS.WINDOWS
    assert report.summary
    assert "failed" in report.summary
    assert report.ok is False
    by_id = _checks(report)
    for slow_id in SLOW_CHECK_IDS:
        assert by_id[slow_id].status is CheckStatus.SKIP
        assert "slow" in by_id[slow_id].detail.lower()
    assert set(FAST_CHECK_IDS) == {check.id for check in report.checks} - set(SLOW_CHECK_IDS)


def test_report_ok_semantics_follow_failures(tmp_path: Path) -> None:
    _, config, paths = _doctor(tmp_path, host=_host())
    report = Doctor(config, paths, host=_host(), environ={"PATH": ""}).run()
    has_failure = any(check.status is CheckStatus.FAIL for check in report.checks)
    assert report.ok is (not has_failure)
    assert report.summary


def test_all_green_with_complete_fake_toolchain(tmp_path: Path) -> None:
    sdk = _make_sdk(tmp_path)
    bin_dir = _make_tool_bin(tmp_path)
    config = _config_with_free_ports(sdk, tmp_path)
    paths = Paths(root=tmp_path / "root", portable=True)
    runner = _fake_runner(
        {
            "adb": (0, "Android Debug Bridge version 1.0.41\nVersion 35.0.2-12345678\n", ""),
            "emulator": (0, "Android emulator version 35.1.4.0 (build_id 12345678)\n", ""),
            "emulator-accel": (0, "WHPX (10.0.22621) is installed and usable.", ""),
            "java": (0, "", 'openjdk version "21.0.6" 2025-01-21\nOpenJDK Runtime Environment\n'),
            "node": (0, "v22.14.0\n", ""),
            "npm": (0, "10.9.2\n", ""),
            "appium": (0, "4.0.0\n", ""),
            "appium-driver": (0, "uiautomator2@3.0.0\n", ""),
        }
    )
    doctor = Doctor(
        config,
        paths,
        host=_host(),
        environ={"PATH": str(bin_dir)},
        command_runner=runner,
    )
    report = doctor.run(include_slow=True)
    by_id = _checks(report)
    for check_id in (
        "os",
        "arch",
        "admin",
        "hypervisor",
        "ram",
        "disk",
        "path_spaces",
        "python",
        "sdk_root",
        "sdk_tools",
        "licenses",
        "ports",
        "clock",
        "adb",
        "emulator",
        "emulator_accel",
        "jdk",
        "node",
        "appium",
    ):
        assert by_id[check_id].status is CheckStatus.OK, (
            check_id,
            by_id[check_id].status,
            by_id[check_id].detail,
        )
    assert report.ok is True
    assert by_id["adb"].detail.startswith("adb 1.0.41")
    assert "uiautomator2" in by_id["appium"].detail.lower()


# ---------------------------------------------------------------------------
# OS / architecture / privilege checks
# ---------------------------------------------------------------------------


def test_unknown_os_and_arch_fail(tmp_path: Path) -> None:
    host = _host(os=HostOS.UNKNOWN, arch=CpuArch.UNKNOWN, acceleration_available=False)
    doctor, config, paths = _doctor(tmp_path, host=host)
    report = doctor.run()
    by_id = _checks(report)
    assert by_id["os"].status is CheckStatus.FAIL
    assert by_id["arch"].status is CheckStatus.FAIL
    assert report.ok is False


@pytest.mark.parametrize(
    ("host_os", "arch", "expected"),
    [
        (HostOS.WINDOWS, CpuArch.X86_64, CheckStatus.OK),
        (HostOS.WINDOWS, CpuArch.ARM64, CheckStatus.FAIL),
        (HostOS.MACOS, CpuArch.ARM64, CheckStatus.OK),
        (HostOS.MACOS, CpuArch.X86_64, CheckStatus.WARN),
        (HostOS.LINUX, CpuArch.X86_64, CheckStatus.OK),
        (HostOS.LINUX, CpuArch.ARM64, CheckStatus.WARN),
    ],
)
def test_arch_support_matrix(
    tmp_path: Path, host_os: HostOS, arch: CpuArch, expected: CheckStatus
) -> None:
    host = _host(os=host_os, arch=arch)
    doctor, _config, _paths = _doctor(tmp_path, host=host)
    by_id = _checks(doctor.run())
    assert by_id["arch"].status is expected


def test_admin_warnings_and_sudo_detection(tmp_path: Path) -> None:
    non_admin_windows = _unavailable_host(HostOS.WINDOWS, is_admin=False)
    doctor, _config, _paths = _doctor(tmp_path, host=non_admin_windows)
    check = _checks(doctor.run())["admin"]
    assert check.status is CheckStatus.WARN
    assert "RunAs" in check.fix_command

    bin_dir = tmp_path / "bin"
    _fake_tool_file(bin_dir, "sudo")
    non_admin_linux = _unavailable_host(HostOS.LINUX, is_admin=False)
    doctor, _config, _paths = _doctor(
        tmp_path, host=non_admin_linux, environ={"PATH": str(bin_dir)}
    )
    check = _checks(doctor.run())["admin"]
    assert check.status is CheckStatus.OK
    assert "sudo" in check.detail

    doctor, _config, _paths = _doctor(tmp_path, host=_host(is_admin=True))
    assert _checks(doctor.run())["admin"].status is CheckStatus.OK


# ---------------------------------------------------------------------------
# Hypervisor: fail + fixable/blocked behaviour
# ---------------------------------------------------------------------------


def test_hypervisor_failure_is_fixable_with_platform_command(tmp_path: Path) -> None:
    windows_doctor, _config, _paths = _doctor(tmp_path, host=_unavailable_host())
    report = windows_doctor.run()
    check = _checks(report)["hypervisor"]
    assert check.status is CheckStatus.FAIL
    assert check.fixable is True
    assert "dism" in check.fix_command
    assert "HypervisorPlatform" in check.fix_command
    assert report.ok is False

    linux_doctor, _config, _paths = _doctor(
        tmp_path,
        host=_unavailable_host(
            HostOS.LINUX,
            hypervisor=AcceleratorInfo(
                kind=AcceleratorKind.KVM,
                available=False,
                detail="/dev/kvm exists but is not accessible",
            ),
        ),
    )
    check = _checks(linux_doctor.run())["hypervisor"]
    assert check.status is CheckStatus.FAIL
    assert check.fixable is True
    assert "avdfleet doctor --fix hypervisor" in check.fix_command


def test_fix_hypervisor_windows_returns_command_and_never_reboots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("fix must not execute commands")

    monkeypatch.setattr(doctor_module, "_run_command", explode)
    doctor, _config, _paths = _doctor(tmp_path, host=_unavailable_host())
    result = doctor.fix("hypervisor")
    assert result.ok is False
    assert result.data["applied"] is False
    assert result.data["manual"] is True
    assert "dism" in result.data["command"]
    assert "HypervisorPlatform" in result.data["command"]
    assert result.data["requires_reboot"] is True


def test_fix_hypervisor_linux_requires_confirmation_then_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_module, "can_write_udev_rule", lambda: False)
    doctor, _config, _paths = _doctor(
        tmp_path,
        host=_unavailable_host(
            HostOS.LINUX,
            hypervisor=AcceleratorInfo(
                kind=AcceleratorKind.KVM,
                available=False,
                detail="/dev/kvm exists but is not accessible",
            ),
        ),
    )
    blocked = doctor.fix("hypervisor")
    assert blocked.ok is False
    assert blocked.data["requires_confirmation"] is True
    assert blocked.data["applied"] is False
    assert "kvm" in blocked.data["rule"].lower()
    assert "sudo" in blocked.data["command"]

    dry = doctor.fix("hypervisor", confirm=True, dry_run=True)
    assert dry.ok is True
    assert dry.data["dry_run"] is True
    assert dry.data["applied"] is False
    assert dry.data["rule_path"].endswith("50-avdfleet-kvm.rules")

    unprivileged = doctor.fix("hypervisor", confirm=True)
    assert unprivileged.ok is False
    assert unprivileged.data["applied"] is False
    assert "sudo" in unprivileged.data["command"]


def test_fix_hypervisor_macos_is_informational(tmp_path: Path) -> None:
    doctor, _config, _paths = _doctor(
        tmp_path,
        host=_unavailable_host(
            HostOS.MACOS,
            hypervisor=AcceleratorInfo(
                kind=AcceleratorKind.HVF,
                available=False,
                detail="HVF is not available (kern.hv_support=0)",
            ),
        ),
    )
    result = doctor.fix("hypervisor")
    assert result.ok is False
    assert result.data["action"] == "enable_hvf"
    assert result.data["requires_reboot"] is False


def test_kvm_group_check_and_fix_returns_hint(tmp_path: Path) -> None:
    doctor, _config, _paths = _doctor(tmp_path, host=_unavailable_host(HostOS.LINUX))
    report = doctor.run()
    check = _checks(report)["kvm_group"]
    assert check.status in {CheckStatus.SKIP, CheckStatus.FAIL, CheckStatus.OK}
    result = doctor.fix("kvm_group")
    assert result.ok is False
    assert result.data["applied"] is False
    assert "usermod" in result.data["command"]
    assert "kvm" in result.data["command"]
    assert "next" in result.data


def test_nested_virtualisation_warns(tmp_path: Path) -> None:
    host = _host(
        hypervisor=AcceleratorInfo(
            kind=AcceleratorKind.WHPX, available=True, detail="WHPX", nested=True
        )
    )
    doctor, _config, _paths = _doctor(tmp_path, host=host)
    by_id = _checks(doctor.run())
    assert by_id["nested"].status is CheckStatus.WARN
    assert "virtual" in by_id["nested"].detail.lower()
    assert by_id["hypervisor"].status is CheckStatus.OK


# ---------------------------------------------------------------------------
# Capacity checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ram_gb", "expected"),
    [(2.0, CheckStatus.FAIL), (4.0, CheckStatus.WARN), (8.0, CheckStatus.OK), (64.0, CheckStatus.OK)],
)
def test_ram_thresholds(tmp_path: Path, ram_gb: float, expected: CheckStatus) -> None:
    doctor, _config, _paths = _doctor(tmp_path, host=_host(total_ram_gb=ram_gb))
    assert _checks(doctor.run())["ram"].status is expected


@pytest.mark.parametrize(
    ("free_gb", "expected"),
    [(1.0, CheckStatus.FAIL), (15.0, CheckStatus.WARN), (50.0, CheckStatus.OK)],
)
def test_disk_thresholds(tmp_path: Path, free_gb: float, expected: CheckStatus) -> None:
    doctor, _config, _paths = _doctor(tmp_path, host=_host(free_disk_gb=free_gb))
    assert _checks(doctor.run())["disk"].status is expected


def test_filesystem_fat_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_module, "filesystem_type", lambda path: "exFAT")
    doctor, _config, _paths = _doctor(tmp_path, host=_host())
    check = _checks(doctor.run())["filesystem"]
    assert check.status is CheckStatus.WARN
    assert "exFAT" in check.detail
    assert "NTFS" in check.hint or "ext4" in check.hint


def test_path_spaces_warning(tmp_path: Path) -> None:
    sdk = _make_sdk(tmp_path / "with space")
    config = Config(sdk_root=str(sdk))
    paths = Paths(root=tmp_path / "root with space", portable=True)
    doctor = Doctor(config, paths, host=_host(), environ={"PATH": ""})
    check = _checks(doctor.run())["path_spaces"]
    assert check.status is CheckStatus.WARN
    assert "space" in check.detail.lower()


def test_ports_failure_and_warning_behaviour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doctor, config, _paths = _doctor(tmp_path, host=_host())
    monkeypatch.setattr(doctor_module, "find_free_even_port", lambda start, end: None)
    monkeypatch.setattr(doctor_module, "is_port_free", lambda port, host="127.0.0.1": False)
    check = _checks(doctor.run())["ports"]
    assert check.status is CheckStatus.FAIL
    assert "console" in check.detail

    monkeypatch.setattr(doctor_module, "find_free_even_port", lambda start, end: 5554)
    monkeypatch.setattr(
        doctor_module,
        "is_port_free",
        lambda port, host="127.0.0.1": port != config.api_port,
    )
    check = _checks(doctor.run())["ports"]
    assert check.status is CheckStatus.WARN
    assert str(config.api_port) in check.detail

    monkeypatch.setattr(doctor_module, "is_port_free", lambda port, host="127.0.0.1": True)
    assert _checks(doctor.run())["ports"].status is CheckStatus.OK


# ---------------------------------------------------------------------------
# Windows-only, clock and Python checks
# ---------------------------------------------------------------------------


def test_windows_long_paths_and_defender_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_module, "windows_long_paths_enabled", lambda: False)
    monkeypatch.setattr(doctor_module, "defender_present", lambda: True)
    doctor, _config, _paths = _doctor(tmp_path, host=_host())
    by_id = _checks(doctor.run())
    assert by_id["long_paths"].status is CheckStatus.WARN
    assert "LongPathsEnabled" in by_id["long_paths"].fix_command
    assert by_id["defender"].status is CheckStatus.WARN
    assert "Add-MpPreference" in by_id["defender"].fix_command

    linux_doctor, _config, _paths = _doctor(tmp_path, host=_unavailable_host(HostOS.LINUX))
    linux_by_id = _checks(linux_doctor.run())
    assert linux_by_id["long_paths"].status is CheckStatus.SKIP
    assert linux_by_id["defender"].status is CheckStatus.SKIP


def test_clock_sanity_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor_module, "utcnow", lambda: datetime(2001, 1, 1, tzinfo=UTC)
    )
    doctor, _config, _paths = _doctor(tmp_path, host=_host())
    check = _checks(doctor.run())["clock"]
    assert check.status is CheckStatus.FAIL
    assert "clock" in check.detail.lower()


def test_python_version_check_ok(tmp_path: Path) -> None:
    doctor, _config, _paths = _doctor(tmp_path, host=_host())
    check = _checks(doctor.run())["python"]
    assert check.status is CheckStatus.OK
    assert "Python" in check.detail


# ---------------------------------------------------------------------------
# SDK checks and fixes
# ---------------------------------------------------------------------------


def test_sdk_root_fix_creates_directories(tmp_path: Path) -> None:
    sdk = tmp_path / "sdk-missing"
    config = Config(sdk_root=str(sdk))
    paths = Paths(root=tmp_path / "root", portable=True)
    doctor = Doctor(config, paths, host=_host(), environ={"PATH": ""})
    report = doctor.run()
    check = _checks(report)["sdk_root"]
    assert check.status is CheckStatus.FAIL
    assert check.fixable is True
    assert "avdfleet doctor --fix sdk_root" in check.fix_command

    result = doctor.fix("sdk_root")
    assert result.ok is True
    assert result.data["applied"] is True
    assert sdk.is_dir()
    assert (sdk / "platform-tools").is_dir()
    assert (sdk / "licenses").is_dir()
    assert (sdk / "build-tools").is_dir()
    assert _checks(doctor.run())["sdk_root"].status is CheckStatus.OK


def test_sdk_root_fix_dry_run_does_not_write(tmp_path: Path) -> None:
    sdk = tmp_path / "sdk-dry"
    config = Config(sdk_root=str(sdk))
    paths = Paths(root=tmp_path / "root", portable=True)
    doctor = Doctor(config, paths, host=_host(), environ={"PATH": ""})
    result = doctor.fix("sdk_root", dry_run=True)
    assert result.ok is True
    assert result.data["dry_run"] is True
    assert result.data["applied"] is False
    assert not sdk.exists()


def test_sdk_tools_missing_names_components(tmp_path: Path) -> None:
    sdk = tmp_path / "empty-sdk"
    sdk.mkdir()
    config = Config(sdk_root=str(sdk))
    paths = Paths(root=tmp_path / "root", portable=True)
    doctor = Doctor(config, paths, host=_host(), environ={"PATH": ""})
    check = _checks(doctor.run())["sdk_tools"]
    assert check.status is CheckStatus.FAIL
    for component in ("cmdline-tools", "platform-tools", "emulator", "build-tools"):
        assert component in check.detail


def test_licenses_warning_then_ok(tmp_path: Path) -> None:
    sdk = tmp_path / "sdk-licenses"
    (sdk / "licenses").mkdir(parents=True)
    config = Config(sdk_root=str(sdk))
    paths = Paths(root=tmp_path / "root", portable=True)
    doctor = Doctor(config, paths, host=_host(), environ={"PATH": ""})
    check = _checks(doctor.run())["licenses"]
    assert check.status is CheckStatus.WARN
    assert "--accept-android-sdk-licenses" in check.fix_command
    (sdk / "licenses" / "android-sdk-license").write_text("hash", encoding="utf-8")
    assert _checks(doctor.run())["licenses"].status is CheckStatus.OK


# ---------------------------------------------------------------------------
# Slow checks: missing tools never crash, present tools are parsed
# ---------------------------------------------------------------------------


def test_missing_tools_never_crash(tmp_path: Path) -> None:
    doctor, _config, _paths = _doctor(
        tmp_path, include_sdk=False, host=_host(), environ={"PATH": ""}
    )
    report = doctor.run(include_slow=True)
    by_id = _checks(report)
    assert by_id["adb"].status is CheckStatus.FAIL
    assert by_id["emulator"].status is CheckStatus.FAIL
    assert by_id["emulator_accel"].status is CheckStatus.SKIP
    assert by_id["jdk"].status is CheckStatus.WARN
    assert by_id["node"].status is CheckStatus.WARN
    assert by_id["appium"].status is CheckStatus.WARN
    assert isinstance(report.ok, bool)


def test_emulator_accel_unusable_is_fail_with_fix(tmp_path: Path) -> None:
    sdk = _make_sdk(tmp_path)
    config = _config_with_free_ports(sdk, tmp_path)
    paths = Paths(root=tmp_path / "root", portable=True)
    runner = _fake_runner(
        {
            "emulator": (0, "Android emulator version 35.1.4.0\n", ""),
            "emulator-accel": (1, "WHPX is not installed and usable.\n", ""),
        }
    )
    doctor = Doctor(
        config, paths, host=_unavailable_host(), environ={"PATH": ""}, command_runner=runner
    )
    check = _checks(doctor.run(include_slow=True))["emulator_accel"]
    assert check.status is CheckStatus.FAIL
    assert check.fixable is True
    assert check.fix_command


def test_tool_version_parsing_with_fake_runner(tmp_path: Path) -> None:
    sdk = _make_sdk(tmp_path)
    config = _config_with_free_ports(sdk, tmp_path)
    paths = Paths(root=tmp_path / "root", portable=True)
    runner = _fake_runner(
        {
            "adb": (0, "Android Debug Bridge version 1.0.41\n", ""),
            "emulator": (0, "Android emulator version 35.1.4.0 (build_id 1)\n", ""),
            "emulator-accel": (0, "KVM (6.8.0) is installed and usable.", ""),
            "java": (0, "", 'openjdk version "21.0.6" 2025-01-21\n'),
        }
    )
    doctor = Doctor(
        config, paths, host=_host(), environ={"PATH": ""}, command_runner=runner
    )
    by_id = _checks(doctor.run(include_slow=True))
    assert by_id["adb"].status is CheckStatus.OK
    assert by_id["emulator"].status is CheckStatus.OK
    assert by_id["emulator_accel"].status is CheckStatus.OK
    assert by_id["jdk"].status is CheckStatus.OK


# ---------------------------------------------------------------------------
# Fix dispatch: manual/unknown actions never crash or reboot
# ---------------------------------------------------------------------------


def test_fix_dispatch_for_manual_actions(tmp_path: Path) -> None:
    doctor, _config, _paths = _doctor(tmp_path, host=_host())
    assert doctor.fix("unknown-check").ok is False
    assert doctor.fix("unknown-check").data["unknown"] is True

    ram_fix = doctor.fix("ram")
    assert ram_fix.ok is False
    assert ram_fix.data["manual"] is True
    assert ram_fix.data["applied"] is False

    licenses = doctor.fix("licenses")
    assert licenses.ok is False
    assert "--accept-android-sdk-licenses" in licenses.data["command"]

    long_paths = doctor.fix("long_paths")
    assert long_paths.ok is False
    assert "LongPathsEnabled" in long_paths.data["command"]

    defender = doctor.fix("defender")
    assert defender.ok is False
    assert "Add-MpPreference" in defender.data["command"]


def test_doctor_with_detector_fake_host_does_not_crash(tmp_path: Path) -> None:
    paths = Paths(root=tmp_path / "root", portable=True)
    config = Config(sdk_root=str(tmp_path / "sdk"))
    env = {
        "AVDFLEET_FAKE_HOST_OS": "linux",
        "AVDFLEET_FAKE_ARCH": "x86_64",
        "AVDFLEET_FAKE_ACCEL": "kvm",
        "PATH": "",
    }
    doctor = Doctor(config, paths, environ=env)
    report = doctor.run()
    assert report.host.os is HostOS.LINUX
    assert report.host.acceleration_available is True
    assert len(report.checks) == len(CHECK_IDS)
    assert isinstance(doctor.fix("hypervisor").data, dict)
