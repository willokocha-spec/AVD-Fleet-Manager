"""Hypervisor availability check.

The portable bundle can start without acceleration (the GUI/API still work), but
Android emulators cannot boot without WHPX/KVM/HVF.  Detection is injected via a
``provider`` callable in tests and via a ``runner`` callable for the per-OS
probes, so no test ever touches WMI, sysctl or /dev/kvm.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from avdfleet.core.models import CheckStatus
from avdfleet.portable.env.model import host_os_name

from . import CheckResult

__all__ = [
    "HypervisorInfo",
    "detect_hypervisor",
    "hypervisor_check",
]

_Runner = Callable[[list[str]], tuple[int, str]]

_WINDOWS_HINT = (
    "Enable virtualisation in the firmware, then enable the 'Windows Hypervisor Platform' "
    "feature (DISM /Enable-Feature /FeatureName:HypervisorPlatform) and reboot. AEHD can be "
    "used as a fallback on Intel/AMD hosts without Hyper-V."
)
_LINUX_HINT = (
    "Ensure /dev/kvm exists and is writable: load kvm_intel/kvm_amd, then add the user to the "
    "'kvm' group (sudo usermod -aG kvm $USER) and log in again."
)
_MACOS_HINT = "HVF requires a Mac with hardware virtualisation support (all Apple Silicon Macs)."


@dataclass(frozen=True)
class HypervisorInfo:
    kind: str = "unknown"
    available: bool = False
    detail: str = ""
    requires_reboot: bool = False
    nested: bool | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "available": self.available,
            "detail": self.detail,
            "requires_reboot": self.requires_reboot,
            "nested": self.nested,
        }


def _default_runner(argv: list[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    output = ((completed.stdout or "") + (completed.stderr or "")).strip()
    return completed.returncode, output


def _read_linux_cpuinfo() -> str:
    try:
        return Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _linux_cpu_has_virtualization(cpuinfo: str) -> bool:
    flags = []
    for line in cpuinfo.splitlines():
        if line.lower().startswith("flags") or line.lower().startswith("features"):
            _, _, value = line.partition(":")
            flags.extend(value.split())
    return any(flag in {"vmx", "svm"} for flag in flags)


def _windows_command() -> list[str]:
    script = (
        "$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1; "
        "$cs = Get-CimInstance Win32_ComputerSystem; "
        "'firmware=' + [bool]$cpu.VirtualizationFirmwareEnabled; "
        "'hypervisor=' + [bool]$cs.HypervisorPresent"
    )
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]


def _parse_windows_output(output: str) -> dict[str, bool]:
    parsed: dict[str, bool] = {}
    for line in output.splitlines():
        key, _, value = line.strip().partition("=")
        if key in {"firmware", "hypervisor"}:
            parsed[key] = value.strip().lower() in {"true", "1", "yes"}
    return parsed


def detect_hypervisor(
    *,
    host_os: str | None = None,
    runner: _Runner | None = None,
    kvm_device: str | Path | None = None,
    cpuinfo: str | None = None,
) -> HypervisorInfo:
    """Best-effort host hypervisor detection with injectable probes."""
    host = host_os or host_os_name()
    run = runner or _default_runner

    if host == "windows":
        code, output = run(_windows_command())
        if code != 0 or not output:
            return HypervisorInfo(
                kind="whpx",
                available=False,
                detail="Could not query Win32_ComputerSystem/Win32_Processor (WMI unavailable).",
            )
        flags = _parse_windows_output(output)
        if flags.get("hypervisor"):
            return HypervisorInfo(
                kind="whpx",
                available=True,
                detail="A hypervisor is present (Hyper-V/WHPX); the emulator can use WHPX.",
            )
        if flags.get("firmware"):
            return HypervisorInfo(
                kind="whpx",
                available=False,
                requires_reboot=True,
                detail=(
                    "Virtualisation is enabled in the firmware but no hypervisor is running; "
                    "enable the Windows Hypervisor Platform feature."
                ),
            )
        return HypervisorInfo(
            kind="none",
            available=False,
            nested=False,
            detail=(
                "Virtualisation is disabled or not exposed to this machine "
                "(nested virtualisation is not supported on most cloud hosts)."
            ),
        )

    if host == "linux":
        device = Path(kvm_device) if kvm_device is not None else Path("/dev/kvm")
        if device.exists():
            readable = os.access(device, os.R_OK)
            writable = os.access(device, os.W_OK)
            if readable and writable:
                return HypervisorInfo(
                    kind="kvm",
                    available=True,
                    detail=f"KVM is available and accessible via {device}.",
                )
            return HypervisorInfo(
                kind="kvm",
                available=False,
                detail=f"{device} exists but is not readable/writable by this user.",
            )
        text = cpuinfo if cpuinfo is not None else _read_linux_cpuinfo()
        if _linux_cpu_has_virtualization(text):
            return HypervisorInfo(
                kind="kvm",
                available=False,
                detail=(
                    "The CPU exposes VT-x/AMD-V but /dev/kvm is missing; the kvm kernel module or "
                    "virtualisation support is not enabled."
                ),
            )
        return HypervisorInfo(
            kind="none",
            available=False,
            nested=False,
            detail=(
                "No /dev/kvm and no VT-x/AMD-V flags in /proc/cpuinfo; the emulator cannot be "
                "hardware accelerated on this host."
            ),
        )

    if host == "macos":
        code, output = run(["sysctl", "-n", "kern.hv_support"])
        value = output.strip().splitlines()[-1] if output.strip() else ""
        if code != 0 or value not in {"0", "1"}:
            return HypervisorInfo(
                kind="unknown",
                available=False,
                detail="Could not query kern.hv_support (sysctl unavailable or denied).",
            )
        if value == "1":
            return HypervisorInfo(
                kind="hvf",
                available=True,
                detail="Hypervisor.framework (HVF) is available.",
            )
        return HypervisorInfo(
            kind="hvf",
            available=False,
            detail="kern.hv_support is 0; this Mac cannot hardware-accelerate the emulator.",
        )

    return HypervisorInfo(
        kind="unknown",
        available=False,
        detail="Hypervisor detection is not implemented for this host OS.",
    )


def _hint_for(kind: str) -> str:
    if kind == "whpx":
        return _WINDOWS_HINT
    if kind == "kvm":
        return _LINUX_HINT
    if kind == "hvf":
        return _MACOS_HINT
    return "Enable hardware virtualisation (WHPX/KVM/HVF) to boot AVDs."


def hypervisor_check(
    *,
    host_os: str | None = None,
    provider: Callable[[], HypervisorInfo] | None = None,
    runner: _Runner | None = None,
    kvm_device: str | Path | None = None,
    cpuinfo: str | None = None,
) -> CheckResult:
    """Report whether a usable hypervisor is available (never fails the GUI launch)."""
    host = host_os or host_os_name()
    try:
        if provider is not None:
            info = provider()
        else:
            info = detect_hypervisor(
                host_os=host, runner=runner, kvm_device=kvm_device, cpuinfo=cpuinfo
            )
    except Exception as exc:  # pragma: no cover - defensive: third-party probes may raise
        return CheckResult(
            id="hypervisor",
            title="Hypervisor availability",
            status=CheckStatus.SKIP,
            detail=f"Hypervisor detection failed: {exc}",
        )
    if info.available:
        return CheckResult(
            id="hypervisor",
            title="Hypervisor availability",
            status=CheckStatus.OK,
            detail=info.detail or f"{info.kind} is available.",
            value=info.kind,
        )
    if info.kind == "unknown":
        return CheckResult(
            id="hypervisor",
            title="Hypervisor availability",
            status=CheckStatus.SKIP,
            detail=info.detail or "Hypervisor support could not be determined on this host.",
            value=info.kind,
        )
    detail = info.detail or "No hardware hypervisor is available."
    if info.requires_reboot:
        detail += " A reboot is required after enabling it."
    return CheckResult(
        id="hypervisor",
        title="Hypervisor availability",
        status=CheckStatus.WARN,
        detail=f"{detail} AVDs cannot boot until this is fixed. {_hint_for(info.kind)}",
        value=info.kind,
    )
