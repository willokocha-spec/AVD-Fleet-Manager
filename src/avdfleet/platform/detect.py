"""Host detection for AVD Fleet.

``PlatformDetector`` answers two questions: which host are we on, and can that
host run hardware-accelerated Android emulators?  Every probe is best effort --
a missing tool, an unreadable file or a failing subprocess degrades the affected
field to a safe default instead of raising.

Environment overrides (``contracts/fakes-spec.md`` section 8) make the unit
suite able to exercise every platform path on any host:

* ``AVDFLEET_FAKE_HOST_OS`` -- ``windows`` | ``macos`` | ``linux``
* ``AVDFLEET_FAKE_ARCH`` -- ``x86_64`` | ``arm64``
* ``AVDFLEET_FAKE_ACCEL`` -- ``whpx`` | ``aehd`` | ``kvm`` | ``hvf`` | ``none``
* ``AVDFLEET_FAKE_SDK_ROOT`` -- SDK root override
* ``ANDROID_SDK_ROOT`` / ``ANDROID_HOME`` / ``ANDROID_AVD_HOME``

Acceleration semantics (``core.models.AcceleratorInfo``):

* ``kind`` is the accelerator the platform expects or the one that was found
  (``WHPX``/``AEHD`` on Windows, ``KVM`` on Linux, ``HVF`` on macOS).
* ``available`` is ``True`` only when the accelerator is usable *now*.
* ``requires_reboot`` is ``True`` when a Windows feature/driver was enabled but
  the accelerator only becomes usable after a reboot.
* ``nested`` is ``True`` when the host itself appears to be a virtual machine
  (nested virtualisation / limited acceleration), ``False`` when it is not, and
  ``None`` when the probe could not tell.

The detector itself never runs privileged commands: WHPX is probed through the
public ``WinHvPlatform`` API and ``systeminfo``, both available without
elevation.  Checks that need Administrator (``dism``) fail gracefully and fall
back to the API probe.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from avdfleet.core.childenv import child_environment
from avdfleet.core.config import Config, platform_display_name
from avdfleet.core.models import (
    AcceleratorInfo,
    AcceleratorKind,
    CpuArch,
    HostInfo,
    HostOS,
    SdkPaths,
)
from avdfleet.core.paths import Paths, user_data_dir

CommandResult = tuple[int, str, str]
# Injectable probe runner: callable(argv, timeout) -> (exit_code, stdout, stderr).
CommandRunner = Callable[[Sequence[str], float], CommandResult]

FAKE_OS_ENV = "AVDFLEET_FAKE_HOST_OS"
FAKE_ARCH_ENV = "AVDFLEET_FAKE_ARCH"
FAKE_ACCEL_ENV = "AVDFLEET_FAKE_ACCEL"
FAKE_SDK_ROOT_ENV = "AVDFLEET_FAKE_SDK_ROOT"
FAKE_AVD_HOME_ENV = "AVDFLEET_FAKE_AVD_HOME"
ANDROID_SDK_ROOT_ENV = "ANDROID_SDK_ROOT"
ANDROID_HOME_ENV = "ANDROID_HOME"
ANDROID_AVD_HOME_ENV = "ANDROID_AVD_HOME"
JAVA_HOME_ENV = "JAVA_HOME"

_ACCEL_ALIASES: dict[str, AcceleratorKind] = {
    "whpx": AcceleratorKind.WHPX,
    "aehd": AcceleratorKind.AEHD,
    "gvm": AcceleratorKind.AEHD,
    "kvm": AcceleratorKind.KVM,
    "hvf": AcceleratorKind.HVF,
    "none": AcceleratorKind.NONE,
}

_OS_ALIASES: dict[str, HostOS] = {
    "windows": HostOS.WINDOWS,
    "win": HostOS.WINDOWS,
    "win32": HostOS.WINDOWS,
    "win64": HostOS.WINDOWS,
    "macos": HostOS.MACOS,
    "mac": HostOS.MACOS,
    "osx": HostOS.MACOS,
    "darwin": HostOS.MACOS,
    "macosx": HostOS.MACOS,
    "linux": HostOS.LINUX,
    "gnu/linux": HostOS.LINUX,
}

_ARCH_ALIASES: dict[str, CpuArch] = {
    "x86_64": CpuArch.X86_64,
    "x86-64": CpuArch.X86_64,
    "amd64": CpuArch.X86_64,
    "x64": CpuArch.X86_64,
    "arm64": CpuArch.ARM64,
    "aarch64": CpuArch.ARM64,
    "arm64-v8a": CpuArch.ARM64,
}

_VM_MARKERS = (
    "virtual machine",
    "vmware",
    "virtualbox",
    "vbox",
    "qemu",
    "kvm",
    "xen",
    "parallels",
    "hyper-v",
    "bhyve",
    "bochs",
)

_CONTAINER_MARKERS = ("docker", "kubepods", "containerd", "lxc", "libpod", "podman")

# Process-wide cache for real (non-overridden) detection.  `systeminfo` and the
# feature probes cost seconds; host facts change rarely, so one detection per
# process is plenty.  Overridden (fake) detections are never cached.
_REAL_HOST_CACHE: HostInfo | None = None


# ---------------------------------------------------------------------------
# Small normalisation helpers (also re-used by the platform providers)
# ---------------------------------------------------------------------------


def real_host_os() -> HostOS:
    """Return the operating system this interpreter runs on (cheap, no probes)."""
    if sys.platform.startswith("win") or sys.platform == "cygwin":
        return HostOS.WINDOWS
    if sys.platform == "darwin":
        return HostOS.MACOS
    if sys.platform.startswith("linux"):
        return HostOS.LINUX
    return HostOS.UNKNOWN


def normalise_host_os(value: str | HostOS | None) -> HostOS:
    """Map an OS name/alias to :class:`HostOS`; unknown values map to ``UNKNOWN``."""
    if isinstance(value, HostOS):
        return value
    if not value:
        return HostOS.UNKNOWN
    return _OS_ALIASES.get(str(value).strip().lower(), HostOS.UNKNOWN)


def normalise_arch(value: str | CpuArch | None) -> CpuArch:
    """Map a machine/arch string to :class:`CpuArch`; unknown values map to ``UNKNOWN``."""
    if isinstance(value, CpuArch):
        return value
    if not value:
        return CpuArch.UNKNOWN
    return _ARCH_ALIASES.get(str(value).strip().lower(), CpuArch.UNKNOWN)


def detect_distro_id(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the Linux distribution id (e.g. ``ubuntu``) or ``None``.

    ``AVDFLEET_DISTRO`` overrides the probe so portable launchers and tests can
    pin a provider without touching ``/etc/os-release``.
    """
    env = os.environ if environ is None else environ
    override = env.get("AVDFLEET_DISTRO")
    if override and override.strip():
        return override.strip().lower()
    try:
        release = _platform.freedesktop_os_release()
    except (OSError, AttributeError, ValueError):
        return None
    return (release.get("ID") or "").strip().lower() or None


def which_tool(name: str, *, environ: Mapping[str, str] | None = None) -> str | None:
    """Locate *name* on the requested ``PATH`` without touching the real ``PATH``.

    An explicit mapping without a ``PATH`` key falls back to the process ``PATH``;
    ``PATH=""`` in an explicit mapping disables the search (deterministic tests).
    """
    if environ is None:
        return shutil.which(name)
    if "PATH" in environ:
        return shutil.which(name, path=environ.get("PATH") or "")
    return shutil.which(name)


# ---------------------------------------------------------------------------
# Read-only probe helpers
# ---------------------------------------------------------------------------


def _read_text(path: str | Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return ""


def _existing_ancestor(path: str | Path | None) -> Path | None:
    target = Path(path).expanduser() if path is not None else Path.cwd()
    try:
        target = target.absolute()
    except OSError:  # pragma: no cover - defensive
        return None
    for candidate in (target, *target.parents):
        with contextlib.suppress(OSError):
            if candidate.exists():
                return candidate
    return None


def _free_disk_gb(path: str | Path | None) -> float:
    ancestor = _existing_ancestor(path)
    if ancestor is None:
        return 0.0
    try:
        usage = shutil.disk_usage(ancestor)
    except OSError:
        return 0.0
    return round(usage.free / (1024**3), 2)


def _total_ram_gb() -> float:
    sysconf = getattr(os, "sysconf", None)
    if sysconf is not None:
        try:
            pages = sysconf("SC_PHYS_PAGES")
            page_size = sysconf("SC_PAGE_SIZE")
            if pages > 0 and page_size > 0:
                return round(pages * page_size / (1024**3), 2)
        except (AttributeError, ValueError, OSError):
            pass
    if sys.platform == "win32":
        value = _windows_total_ram_gb()
        if value:
            return value
    try:  # optional extra: psutil is a declared dependency but never required here
        import psutil  # noqa: PLC0415

        return round(psutil.virtual_memory().total / (1024**3), 2)
    except (ImportError, OSError, AttributeError):
        pass
    for line in _read_text("/proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            with contextlib.suppress(IndexError, ValueError):
                return round(int(line.split()[1]) / (1024**2), 2)
    return 0.0


def _windows_total_ram_gb() -> float:
    try:
        from ctypes import wintypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return round(status.ullTotalPhys / (1024**3), 2)
    except (AttributeError, OSError, ValueError, TypeError):
        pass
    return 0.0


def _cpu_count() -> int:
    getaffinity = getattr(os, "sched_getaffinity", None)
    if getaffinity is not None:
        with contextlib.suppress(OSError, AttributeError):
            count = len(getaffinity(0))
            if count > 0:
                return count
    return max(1, os.cpu_count() or 1)


def _windows_native_arch() -> CpuArch:
    """Return the *native* Windows architecture (ARM64 under x64 emulation too)."""
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        is_wow64_process2 = kernel32.IsWow64Process2
        is_wow64_process2.restype = ctypes.c_int
        is_wow64_process2.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ushort),
            ctypes.POINTER(ctypes.c_ushort),
        ]
        process_machine = ctypes.c_ushort(0)
        native_machine = ctypes.c_ushort(0)
        ok = is_wow64_process2(
            kernel32.GetCurrentProcess(),
            ctypes.byref(process_machine),
            ctypes.byref(native_machine),
        )
        if ok:
            if native_machine.value == 0xAA64:  # IMAGE_FILE_MACHINE_ARM64
                return CpuArch.ARM64
            if native_machine.value == 0x8664:  # IMAGE_FILE_MACHINE_AMD64
                return CpuArch.X86_64
            if native_machine.value == 0x014C:  # IMAGE_FILE_MACHINE_I386
                return CpuArch.UNKNOWN
    except (AttributeError, OSError, ValueError, TypeError, ctypes.ArgumentError):
        pass
    return normalise_arch(_platform.machine())


def _is_admin(*, simulated: bool) -> bool:
    if simulated:
        return False
    if sys.platform == "win32":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return False
    if sys.platform != "win32":
        with contextlib.suppress(AttributeError, OSError):
            return os.geteuid() == 0
    return False


def _is_wsl(host_os: HostOS, environ: Mapping[str, str]) -> bool:
    if host_os is not HostOS.LINUX:
        return False
    if environ.get("WSL_DISTRO_NAME") or environ.get("WSL_INTEROP"):
        return True
    for candidate in ("/proc/sys/kernel/osrelease", "/proc/version"):
        if "microsoft" in _read_text(candidate).lower():
            return True
    return False


def _is_container(host_os: HostOS, environ: Mapping[str, str]) -> bool:
    marker = (environ.get("container") or "").strip().lower()
    if marker and marker not in {"none", "0", "false"}:
        return True
    if host_os is not HostOS.LINUX:
        return False
    for candidate in ("/.dockerenv", "/run/.containerenv"):
        with contextlib.suppress(OSError):
            if Path(candidate).exists():
                return True
    cgroup = _read_text("/proc/1/cgroup") + _read_text("/proc/self/cgroup")
    return any(marker in cgroup.lower() for marker in _CONTAINER_MARKERS)


# ---------------------------------------------------------------------------
# Windows acceleration probes (non-elevated)
# ---------------------------------------------------------------------------


def _parse_systeminfo(text: str) -> dict[str, Any]:
    """Parse the Hyper-V/virtualisation section of ``systeminfo`` output."""
    info: dict[str, Any] = {
        "hypervisor_detected": False,
        "firmware_virtualization": None,
        "is_virtual_machine": None,
        "system_model": "",
        "system_manufacturer": "",
        "os_name": "",
        "os_version": "",
    }
    if not text.strip():
        return info
    lower = text.lower()
    info["hypervisor_detected"] = "a hypervisor has been detected" in lower
    if "virtualization enabled in firmware: yes" in lower:
        info["firmware_virtualization"] = True
    elif "virtualization enabled in firmware: no" in lower:
        info["firmware_virtualization"] = False
    model = re.search(r"(?im)^\s*System Model\s*:\s*(.+)$", text)
    manufacturer = re.search(r"(?im)^\s*System Manufacturer\s*:\s*(.+)$", text)
    if model:
        info["system_model"] = model.group(1).strip()
    if manufacturer:
        info["system_manufacturer"] = manufacturer.group(1).strip()
    os_name = re.search(r"(?im)^\s*OS Name\s*:\s*(.+)$", text)
    os_version = re.search(r"(?im)^\s*OS Version\s*:\s*(.+)$", text)
    if os_name:
        info["os_name"] = os_name.group(1).strip()
    if os_version:
        info["os_version"] = os_version.group(1).strip()
    identity = f"{info['system_model']} {info['system_manufacturer']}".lower()
    if any(marker in identity for marker in _VM_MARKERS):
        info["is_virtual_machine"] = True
    elif info["system_model"] or info["system_manufacturer"]:
        info["is_virtual_machine"] = False
    return info


def _parse_feature_state(text: str) -> str | None:
    """Map ``dism``/``Get-WindowsOptionalFeature`` output to a feature state."""
    body = text.strip()
    if not body:
        return None
    match = re.search(r"(?im)^\s*State\s*:\s*(.+)$", body)
    value = match.group(1).strip().lower() if match else body.splitlines()[-1].strip().lower()
    if not value:
        return None
    if "pending" in value:
        return "enablepending" if "enable" in value else "disablepending"
    if value.startswith("enabled") or value == "enable":
        return "enabled"
    if value.startswith("disabled") or value == "disable":
        return "disabled"
    return None


def _registry_exists(subkey: str) -> bool | None:
    """``True``/``False`` when the HKLM key can be inspected, ``None`` otherwise."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows only
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey):
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


def _registry_value(subkey: str, name: str) -> Any:
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows only
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except OSError:
        return None


# ---------------------------------------------------------------------------
# SDK path resolution
# ---------------------------------------------------------------------------


def _first_executable(directory: Path | None, name: str) -> str | None:
    """Return ``dir/name[.exe|.cmd|.bat]`` when it exists (no PATH search)."""
    if directory is None:
        return None
    candidates = [directory / name]
    candidates.extend(directory / f"{name}{ext}" for ext in (".exe", ".cmd", ".bat", ".ps1"))
    for candidate in candidates:
        with contextlib.suppress(OSError):
            if candidate.is_file():
                return str(candidate)
    return None


def _version_key(name: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", name)
    return tuple(int(part) for part in parts) if parts else (-1,)


def _newest_version_dir(base: Path, *, requires: str | None = None) -> Path | None:
    try:
        children = [child for child in base.iterdir() if child.is_dir()]
    except OSError:
        return None
    if requires is not None:
        children = [child for child in children if (child / requires).is_dir()]
    if not children:
        return None
    return max(children, key=lambda child: _version_key(child.name))


def _find_cmdline_tools(sdk_root: Path) -> str | None:
    base = sdk_root / "cmdline-tools"
    latest = base / "latest"
    if (latest / "bin").is_dir():
        return str(latest)
    versioned = _newest_version_dir(base, requires="bin")
    if versioned is not None:
        return str(versioned)
    legacy = sdk_root / "tools"
    if (legacy / "bin").is_dir():
        return str(legacy)
    return None


def _find_java_home(sdk_root: Path, environ: Mapping[str, str]) -> str | None:
    configured = environ.get(JAVA_HOME_ENV)
    if configured:
        candidate = Path(configured).expanduser()
        if _first_executable(candidate / "bin", "java") is not None:
            return str(candidate)
    for name in ("jbr", "jdk", "java"):
        candidate = sdk_root / name
        if _first_executable(candidate / "bin", "java") is not None:
            return str(candidate)
    for candidate in sorted(sdk_root.glob("jdk-*")) if sdk_root.is_dir() else []:
        if _first_executable(candidate / "bin", "java") is not None:
            return str(candidate)
    java = which_tool("java", environ=environ)
    if java:
        return str(Path(java).resolve().parent.parent)
    return None


def _find_android_cli(sdk_root: Path, environ: Mapping[str, str]) -> str | None:
    candidates = [
        sdk_root / "cmdline-tools" / "latest" / "bin",
        sdk_root / "tools" / "bin",
        sdk_root / "android-cli" / "bin",
        sdk_root / "bin",
    ]
    for directory in candidates:
        found = _first_executable(directory, "android")
        if found:
            return found
    return which_tool("android", environ=environ)


def _path_value(environ: Mapping[str, str], key: str) -> str:
    if key in environ:
        return environ.get(key) or ""
    return os.environ.get(key, "")


def _build_sdk_env(
    *,
    sdk_root: str,
    avd_home: str,
    cmdline_tools: str | None,
    platform_tools: str | None,
    emulator: str | None,
    build_tools: str | None,
    java_home: str | None,
    environ: Mapping[str, str],
) -> dict[str, str]:
    result: dict[str, str] = {
        ANDROID_SDK_ROOT_ENV: sdk_root,
        ANDROID_HOME_ENV: sdk_root,
        ANDROID_AVD_HOME_ENV: avd_home,
    }
    if java_home:
        result[JAVA_HOME_ENV] = java_home
    additions: list[str] = []
    for directory in (
        str(Path(cmdline_tools) / "bin") if cmdline_tools else None,
        platform_tools,
        emulator,
        build_tools,
        str(Path(java_home) / "bin") if java_home else None,
    ):
        if directory and Path(directory).is_dir() and directory not in additions:
            additions.append(directory)
    original_path = _path_value(environ, "PATH")
    if additions:
        result["PATH"] = os.pathsep.join([*additions, original_path]) if original_path else os.pathsep.join(additions)
    elif original_path:
        result["PATH"] = original_path
    return result


def _resolve_sdk_root(
    config: Config | None,
    paths: Paths | None,
    environ: Mapping[str, str],
) -> str:
    candidates: list[str] = []
    if config is not None and config.sdk_root:
        candidates.append(str(config.sdk_root))
    fake_root = environ.get(FAKE_SDK_ROOT_ENV)
    if fake_root:
        candidates.append(fake_root)
    if paths is not None and paths.sdk_root:
        candidates.append(str(paths.sdk_root))
    candidates.extend(
        value
        for value in (environ.get(ANDROID_SDK_ROOT_ENV), environ.get(ANDROID_HOME_ENV))
        if value
    )
    for candidate in candidates:
        with contextlib.suppress(OSError, ValueError):
            return str(Path(candidate).expanduser())
    return str(Path(user_data_dir()) / "sdk")


def _resolve_avd_home(
    config: Config | None,
    paths: Paths | None,
    environ: Mapping[str, str],
) -> str:
    candidates: list[str] = []
    if config is not None and config.avd_home:
        candidates.append(str(config.avd_home))
    fake_home = environ.get(FAKE_AVD_HOME_ENV)
    if fake_home:
        candidates.append(fake_home)
    if paths is not None and paths.avd_home:
        candidates.append(str(paths.avd_home))
    android_home = environ.get(ANDROID_AVD_HOME_ENV)
    if android_home:
        candidates.append(android_home)
    for candidate in candidates:
        with contextlib.suppress(OSError, ValueError):
            return str(Path(candidate).expanduser())
    return str(Path(user_data_dir()) / "avd")


def sdk_paths(
    config: Config | None = None,
    paths: Paths | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> SdkPaths:
    """Resolve the Android SDK layout from config, paths and environment.

    Precedence for the root: ``Config.sdk_root`` > ``AVDFLEET_FAKE_SDK_ROOT`` >
    ``Paths.sdk_root`` > ``ANDROID_SDK_ROOT`` > ``ANDROID_HOME`` > default under
    the user data directory.  Component fields point at *installed* directories
    (``None`` when absent); ``android_cli`` is the ``android`` executable when
    the new Android CLI is present; ``env`` is the environment installer steps
    and tooling backends should apply.
    """
    env = dict(os.environ) if environ is None else dict(environ)
    sdk_root = _resolve_sdk_root(config, paths, env)
    avd_home = _resolve_avd_home(config, paths, env)
    root = Path(sdk_root)

    cmdline_tools = _find_cmdline_tools(root)
    platform_tools = str(root / "platform-tools") if (root / "platform-tools").is_dir() else None
    emulator = str(root / "emulator") if (root / "emulator").is_dir() else None
    build_tools_path = _newest_version_dir(root / "build-tools")
    build_tools = str(build_tools_path) if build_tools_path is not None else None
    licenses_dir = str(root / "licenses") if (root / "licenses").is_dir() else None
    android_cli = _find_android_cli(root, env)
    java_home = _find_java_home(root, env)

    return SdkPaths(
        sdk_root=sdk_root,
        cmdline_tools=cmdline_tools,
        platform_tools=platform_tools,
        emulator=emulator,
        build_tools=build_tools,
        avd_home=avd_home,
        licenses_dir=licenses_dir,
        android_cli=android_cli,
        env=_build_sdk_env(
            sdk_root=sdk_root,
            avd_home=avd_home,
            cmdline_tools=cmdline_tools,
            platform_tools=platform_tools,
            emulator=emulator,
            build_tools=build_tools,
            java_home=java_home,
            environ=env,
        ),
    )


def tool_executable(
    name: str,
    sdk: SdkPaths | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Locate an Android tool: SDK component directory first, then ``PATH``."""
    env = dict(os.environ) if environ is None else dict(environ)
    directories: list[Path] = []
    if sdk is not None:
        if sdk.cmdline_tools:
            directories.append(Path(sdk.cmdline_tools) / "bin")
        if sdk.platform_tools:
            directories.append(Path(sdk.platform_tools))
        if sdk.emulator:
            directories.append(Path(sdk.emulator))
        if sdk.build_tools:
            directories.append(Path(sdk.build_tools))
    for directory in directories:
        found = _first_executable(directory, name)
        if found:
            return found
    return which_tool(name, environ=env)


def sdk_executables(
    sdk: SdkPaths | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Map canonical tool names (``adb``, ``emulator``, ``sdkmanager``...) to paths.

    Only tools that can be found are returned; the helper is the convenient
    bridge between :class:`SdkPaths` directories and command execution for the
    SDK backends and installer steps.
    """
    env = dict(os.environ) if environ is None else dict(environ)
    names = ("sdkmanager", "avdmanager", "adb", "emulator", "aapt2", "fastboot", "android")
    resolved: dict[str, str] = {}
    for name in names:
        if sdk is not None and name == "android" and sdk.android_cli:
            resolved[name] = sdk.android_cli
            continue
        path = tool_executable(name, sdk, environ=env)
        if path:
            resolved[name] = path
    return resolved


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


class PlatformDetector:
    """Detect the host OS, architecture, resources and accelerator.

    Parameters
    ----------
    config, paths:
        Optional :class:`avdfleet.core.config.Config` / :class:`Paths` whose data
        and SDK locations inform disk probes and SDK path resolution.
    environ:
        Environment mapping to honour.  ``None`` means the real process
        environment; passing a mapping makes detection deterministic (and
        bypasses the process-wide real-host cache).
    command_runner:
        Injection point for tests: ``callable(argv, timeout) -> (exit, stdout, stderr)``.
        The default runs real subprocesses with a timeout.
    """

    def __init__(
        self,
        config: Config | None = None,
        paths: Paths | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        command_runner: Any = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self._explicit_environ = environ is not None
        self._environ: dict[str, str] = dict(os.environ if environ is None else environ)
        self._command_runner = command_runner
        self._cached: HostInfo | None = None

    # -- public API ------------------------------------------------------
    def detect(self, *, refresh: bool = False) -> HostInfo:
        """Return host facts, cached per instance (and process-wide for real hosts)."""
        global _REAL_HOST_CACHE  # noqa: PLW0603 - deliberate small module cache
        if self._cached is not None and not refresh:
            return self._cached.model_copy(deep=True)
        cacheable = not self._explicit_environ and not self._has_fake_overrides()
        if cacheable and not refresh and _REAL_HOST_CACHE is not None:
            self._cached = _REAL_HOST_CACHE.model_copy(deep=True)
            return self._cached.model_copy(deep=True)
        host = self._detect()
        self._cached = host
        if cacheable:
            _REAL_HOST_CACHE = host.model_copy(deep=True)
        return host.model_copy(deep=True)

    def _has_fake_overrides(self) -> bool:
        return any(
            self._environ.get(key)
            for key in (FAKE_OS_ENV, FAKE_ARCH_ENV, FAKE_ACCEL_ENV, FAKE_SDK_ROOT_ENV)
        )

    def sdk_paths(self) -> SdkPaths:
        """Resolve the SDK layout for this detector's config/paths/environment."""
        return sdk_paths(self.config, self.paths, environ=self._environ)

    @property
    def environ(self) -> dict[str, str]:
        return dict(self._environ)

    # -- detection -------------------------------------------------------
    def _detect(self) -> HostInfo:
        env = self._environ
        real_os = real_host_os()
        fake_os = _OS_ALIASES.get((env.get(FAKE_OS_ENV) or "").strip().lower())
        fake_arch = _ARCH_ALIASES.get((env.get(FAKE_ARCH_ENV) or "").strip().lower())
        host_os = fake_os or real_os
        simulated = host_os is not real_os
        arch = fake_arch or self._detect_arch(host_os, simulated)

        os_name, os_version, os_details = self._detect_os_identity(host_os, simulated)
        is_admin = _is_admin(simulated=simulated)
        is_wsl = _is_wsl(host_os, env)
        is_container = _is_container(host_os, env)
        data_path = self._data_path()

        details: dict[str, Any] = {
            "platform": _platform.platform(),
            "machine": _platform.machine(),
            "python": _platform.python_version(),
            "simulated": simulated,
            "real_os": real_os.value,
            "data_dir": str(data_path) if data_path is not None else "",
            "is_wsl": is_wsl,
            "is_container": is_container,
            **os_details,
        }
        fake_overrides = {
            key: env[key]
            for key in (FAKE_OS_ENV, FAKE_ARCH_ENV, FAKE_ACCEL_ENV, FAKE_SDK_ROOT_ENV)
            if env.get(key)
        }
        if fake_overrides:
            details["fake_overrides"] = fake_overrides

        hypervisor = self._detect_hypervisor(host_os, arch, simulated, details)
        details["accelerator"] = hypervisor.kind.value
        details["accelerator_available"] = hypervisor.available

        return HostInfo(
            os=host_os,
            os_name=os_name,
            os_version=os_version,
            arch=arch,
            machine=_platform.machine(),
            python_version=_platform.python_version(),
            is_admin=is_admin,
            is_wsl=is_wsl,
            is_container=is_container,
            cpu_count=_cpu_count(),
            total_ram_gb=_total_ram_gb(),
            free_disk_gb=_free_disk_gb(data_path),
            hypervisor=hypervisor,
            acceleration_available=hypervisor.available,
            details=details,
        )

    def _data_path(self) -> Path | None:
        if self.paths is not None and self.paths.data_dir is not None:
            return Path(self.paths.data_dir)
        if self.config is not None and self.config.data_dir:
            with contextlib.suppress(OSError, ValueError):
                return Path(self.config.data_dir).expanduser()
        override = self._environ.get("AVDFLEET_DATA_DIR")
        if override:
            with contextlib.suppress(OSError, ValueError):
                return Path(override).expanduser()
        return Path(user_data_dir())

    def _detect_arch(self, host_os: HostOS, simulated: bool) -> CpuArch:
        if host_os is HostOS.WINDOWS and not simulated and sys.platform == "win32":
            return _windows_native_arch()
        return normalise_arch(_platform.machine())

    def _detect_os_identity(
        self,
        host_os: HostOS,
        simulated: bool,
    ) -> tuple[str, str, dict[str, Any]]:
        details: dict[str, Any] = {}
        if host_os is HostOS.WINDOWS:
            if simulated or sys.platform != "win32":
                return platform_display_name(HostOS.WINDOWS), "", details
            details["windows_release"] = _platform.release()
            details["windows_version"] = _platform.version()
            details["windows_build"] = str(_platform.version()).split(".")[-1]
            product = _registry_value(r"SOFTWARE\Microsoft\Windows NT\CurrentVersion", "ProductName")
            display = _registry_value(r"SOFTWARE\Microsoft\Windows NT\CurrentVersion", "DisplayVersion")
            build = _registry_value(
                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion", "CurrentBuildNumber"
            )
            if isinstance(product, str) and product:
                details["windows_product"] = product
            if isinstance(display, str) and display:
                details["windows_display_version"] = display
            if isinstance(build, str) and build:
                details["windows_build"] = build
            os_name = details.get("windows_product") or platform_display_name(HostOS.WINDOWS)
            version_bits = [_platform.release(), details.get("windows_display_version", "")]
            os_version = " ".join(bit for bit in version_bits if bit).strip()
            if details.get("windows_build"):
                os_version = f"{os_version} (build {details['windows_build']})".strip()
            return os_name, os_version, details
        if host_os is HostOS.MACOS:
            if simulated or sys.platform != "darwin":
                return platform_display_name(HostOS.MACOS), "", details
            release = _platform.mac_ver()[0]
            details["macos_release"] = release
            return platform_display_name(HostOS.MACOS), release, details
        if host_os is HostOS.LINUX:
            if simulated or sys.platform == "win32":
                return platform_display_name(HostOS.LINUX), "", details
            try:
                release = _platform.freedesktop_os_release()
            except (OSError, AttributeError, ValueError):
                release = {}
            name = release.get("PRETTY_NAME") or release.get("NAME") or "Linux"
            version = release.get("VERSION_ID") or ""
            for key in ("ID", "ID_LIKE", "VERSION_ID", "VERSION_CODENAME", "BUILD_ID"):
                value = release.get(key)
                if value:
                    details[f"distro_{key.lower()}"] = value
            details["distro_id"] = detect_distro_id(self._environ) or release.get("ID", "")
            return name, version, details
        return _platform.system() or "Unknown", "", details

    # -- hypervisor ------------------------------------------------------
    def _detect_hypervisor(
        self,
        host_os: HostOS,
        arch: CpuArch,
        simulated: bool,
        details: dict[str, Any],
    ) -> AcceleratorInfo:
        fake = (self._environ.get(FAKE_ACCEL_ENV) or "").strip().lower()
        if fake:
            details["acceleration_override"] = fake
            kind = _ACCEL_ALIASES.get(fake)
            raw = {"override": fake}
            if kind is None:
                return AcceleratorInfo(
                    kind=AcceleratorKind.NONE,
                    available=False,
                    detail=f"Unrecognised {FAKE_ACCEL_ENV}={fake!r}; expected whpx, aehd, kvm, hvf or none",
                    raw=raw,
                )
            if kind is AcceleratorKind.NONE:
                return AcceleratorInfo(
                    kind=AcceleratorKind.NONE,
                    available=False,
                    detail=f"Acceleration disabled by {FAKE_ACCEL_ENV}=none",
                    raw=raw,
                )
            return AcceleratorInfo(
                kind=kind,
                available=True,
                detail=f"Acceleration forced by {FAKE_ACCEL_ENV}={fake}",
                raw=raw,
            )
        try:
            if host_os is HostOS.WINDOWS:
                return self._windows_accel(details)
            if host_os is HostOS.MACOS:
                return self._macos_accel(arch, simulated, details)
            if host_os is HostOS.LINUX:
                return self._linux_accel(details)
        except Exception as exc:  # noqa: BLE001 - a probe must never break detection
            details["acceleration_error"] = f"{type(exc).__name__}: {exc}"
            return AcceleratorInfo(
                kind=AcceleratorKind.NONE,
                available=False,
                detail=f"Acceleration probe failed: {type(exc).__name__}: {exc}",
            )
        return AcceleratorInfo(
            kind=AcceleratorKind.NONE,
            available=False,
            detail="Unknown operating system; cannot detect hardware acceleration",
        )

    def _windows_accel(self, details: dict[str, Any]) -> AcceleratorInfo:
        raw: dict[str, Any] = {}
        sysinfo = _parse_systeminfo(self._run_text(["systeminfo"], 30.0))
        raw["systeminfo"] = sysinfo
        nested = sysinfo["is_virtual_machine"]
        whpx_api = self._whpx_api_probe()
        raw["whpx_api"] = whpx_api
        feature_state = self._windows_feature_state(raw)
        aehd = self._windows_aehd_state(raw)
        details["windows_acceleration"] = raw

        if whpx_api is True:
            return AcceleratorInfo(
                kind=AcceleratorKind.WHPX,
                available=True,
                detail="WHPX is available (WHvGetCapability reports a hypervisor)",
                nested=nested,
                raw=raw,
            )
        if feature_state == "enabled":
            if sysinfo["hypervisor_detected"] or whpx_api is None:
                # Feature enabled and the hypervisor is running: WHPX is usable.
                if sysinfo["hypervisor_detected"]:
                    return AcceleratorInfo(
                        kind=AcceleratorKind.WHPX,
                        available=True,
                        detail=(
                            "Windows Hypervisor Platform (WHPX) feature is enabled and a "
                            "hypervisor is active"
                        ),
                        nested=nested,
                        raw=raw,
                    )
                return AcceleratorInfo(
                    kind=AcceleratorKind.WHPX,
                    available=False,
                    requires_reboot=True,
                    detail=(
                        "Windows Hypervisor Platform is enabled but not active yet; "
                        "reboot the host to activate WHPX"
                    ),
                    nested=nested,
                    raw=raw,
                )
            return AcceleratorInfo(
                kind=AcceleratorKind.WHPX,
                available=False,
                requires_reboot=True,
                detail=(
                    "Windows Hypervisor Platform is enabled but WHPX reports no active "
                    "hypervisor; reboot the host"
                ),
                nested=nested,
                raw=raw,
            )
        if feature_state == "enablepending":
            return AcceleratorInfo(
                kind=AcceleratorKind.WHPX,
                available=False,
                requires_reboot=True,
                detail="Windows Hypervisor Platform enable is pending; reboot to finish the change",
                nested=nested,
                raw=raw,
            )
        if aehd["running"]:
            return AcceleratorInfo(
                kind=AcceleratorKind.AEHD,
                available=True,
                detail=f"AEHD driver service {aehd['service']!r} is running",
                nested=nested,
                raw=raw,
            )
        if sysinfo["hypervisor_detected"]:
            if feature_state == "disabled":
                return AcceleratorInfo(
                    kind=AcceleratorKind.WHPX,
                    available=False,
                    requires_reboot=True,
                    detail=(
                        "A Hyper-V hypervisor is running but the Windows Hypervisor Platform "
                        "feature is disabled; enable it and reboot"
                    ),
                    nested=nested,
                    raw=raw,
                )
            if whpx_api is None:
                # systeminfo reports a hypervisor (often the parent hypervisor of a VM)
                # but the WHPX API could not be probed; stay conservative so the doctor
                # never claims acceleration is usable without evidence.
                return AcceleratorInfo(
                    kind=AcceleratorKind.WHPX,
                    available=False,
                    detail=(
                        "systeminfo reports a hypervisor but WHPX could not be probed "
                        "directly; verify acceleration with `emulator -accel-check`"
                    ),
                    nested=nested,
                    raw=raw,
                )
            return AcceleratorInfo(
                kind=AcceleratorKind.WHPX,
                available=False,
                detail="A hypervisor is present but Windows Hypervisor Platform is not active",
                nested=nested,
                raw=raw,
            )
        if aehd["installed"]:
            return AcceleratorInfo(
                kind=AcceleratorKind.AEHD,
                available=False,
                requires_reboot=True,
                detail=(
                    f"AEHD driver {aehd['service']!r} is installed but not running; "
                    "start the service or reboot"
                ),
                nested=nested,
                raw=raw,
            )
        return AcceleratorInfo(
            kind=AcceleratorKind.NONE,
            available=False,
            detail=(
                "No hardware acceleration detected: enable Windows Hypervisor Platform "
                "(WHPX) or install AEHD"
            ),
            nested=nested,
            raw=raw,
        )

    def _whpx_api_probe(self) -> bool | None:
        """Probe WHPX through the public WinHvPlatform API (no elevation needed).

        ``True``/``False`` when the API answered, ``None`` when it cannot be
        loaded on this host (for example a cross-architecture build host).
        """
        if sys.platform != "win32":
            return None
        try:
            library = ctypes.WinDLL("WinHvPlatform.dll")
            get_capability = library.WHvGetCapability
        except (OSError, AttributeError):
            return None
        try:
            get_capability.restype = ctypes.c_long
            get_capability.argtypes = [
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
            ]
            present = ctypes.c_int(0)
            written = ctypes.c_uint32(0)
            hresult = get_capability(
                0,  # WHvCapabilityCodeHypervisorPresent
                ctypes.byref(present),
                ctypes.sizeof(present),
                ctypes.byref(written),
            )
        except (OSError, ValueError, TypeError):
            return None
        if (hresult & 0xFFFFFFFF) != 0:
            return False
        return bool(present.value)

    def _windows_feature_state(self, raw: dict[str, Any]) -> str:
        probes: list[dict[str, Any]] = []
        raw["feature_probes"] = probes
        commands = (
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform).State",
            ],
            ["dism", "/online", "/get-featureinfo", "/featurename:HypervisorPlatform"],
        )
        for argv in commands:
            code, stdout, _stderr = self._run(argv, 20.0)
            state = _parse_feature_state(stdout)
            probes.append({"command": argv[0], "exit": code, "state": state})
            if code == 0 and state is not None:
                return state
        return "unknown"

    def _windows_aehd_state(self, raw: dict[str, Any]) -> dict[str, Any]:
        probes: list[dict[str, Any]] = []
        raw["aehd_probes"] = probes
        installed_service: str | None = None
        for service in ("aehd", "gvm"):
            code, stdout, _stderr = self._run(["sc", "query", service], 10.0)
            running = code == 0 and "RUNNING" in stdout.upper()
            key_exists = _registry_exists(rf"SYSTEM\CurrentControlSet\Services\{service}")
            installed = bool((code == 0 and "SERVICE_NAME" in stdout.upper()) or key_exists)
            probes.append(
                {"service": service, "exit": code, "running": running, "installed": installed}
            )
            if running:
                return {"service": service, "running": True, "installed": True}
            if installed and installed_service is None:
                installed_service = service
        return {"service": installed_service or "aehd", "running": False, "installed": installed_service is not None}

    def _macos_accel(self, arch: CpuArch, simulated: bool, details: dict[str, Any]) -> AcceleratorInfo:
        raw: dict[str, Any] = {"arch": arch.value, "simulated": simulated}
        details["macos_acceleration"] = raw
        if arch is CpuArch.ARM64:
            return AcceleratorInfo(
                kind=AcceleratorKind.HVF,
                available=True,
                detail="Apple Silicon host: Hypervisor.framework (HVF) is always available",
                nested=None,
                raw=raw,
            )
        code, stdout, _stderr = self._run(["sysctl", "-n", "kern.hv_support"], 10.0)
        value = stdout.strip()
        raw["kern_hv_support"] = value
        raw["sysctl_exit"] = code
        if code == 0 and value.startswith("1"):
            return AcceleratorInfo(
                kind=AcceleratorKind.HVF,
                available=True,
                detail="Hypervisor.framework (HVF) is available (kern.hv_support=1)",
                nested=None,
                raw=raw,
            )
        if code == 0 and value.startswith("0"):
            return AcceleratorInfo(
                kind=AcceleratorKind.HVF,
                available=False,
                detail="HVF is not available (kern.hv_support=0); the host is virtualised or too old",
                nested=None,
                raw=raw,
            )
        return AcceleratorInfo(
            kind=AcceleratorKind.HVF,
            available=False,
            detail="Could not determine HVF support (sysctl kern.hv_support unavailable)",
            nested=None,
            raw=raw,
        )

    def _linux_accel(self, details: dict[str, Any]) -> AcceleratorInfo:
        raw: dict[str, Any] = {}
        details["linux_acceleration"] = raw
        nested = _linux_is_virtual_machine()
        member = kvm_group_member()
        raw["nested"] = nested
        raw["kvm_group"] = kvm_group_name()
        raw["kvm_group_member"] = member
        kvm = kvm_device_path()
        raw["kvm_path"] = str(kvm)
        if not kvm.exists():
            return AcceleratorInfo(
                kind=AcceleratorKind.NONE,
                available=False,
                detail=(
                    "KVM is unavailable: /dev/kvm does not exist (enable VT-x/AMD-V in the "
                    "firmware, load the kvm module, or run on a physical host)"
                ),
                nested=nested,
                raw=raw,
            )
        if _kvm_accessible(kvm):
            return AcceleratorInfo(
                kind=AcceleratorKind.KVM,
                available=True,
                detail="KVM is available (/dev/kvm is accessible)",
                nested=nested,
                raw=raw,
            )
        return AcceleratorInfo(
            kind=AcceleratorKind.KVM,
            available=False,
            detail=(
                "/dev/kvm exists but is not accessible for the current user; "
                f"add the user to the {kvm_group_name()!r} group and install the udev rule"
            ),
            nested=nested,
            raw=raw,
        )

    # -- process helpers -------------------------------------------------
    def _run(self, argv: Sequence[str], timeout: float) -> CommandResult:
        if self._command_runner is not None:
            try:
                result = self._command_runner(list(argv), timeout)
                return _coerce_result(result)
            except Exception as exc:  # noqa: BLE001 - injected runner must not break detection
                return 127, "", f"command runner failed: {type(exc).__name__}: {exc}"
        return _run_command(argv, timeout, self._subprocess_env())

    def _run_text(self, argv: Sequence[str], timeout: float) -> str:
        code, stdout, stderr = self._run(argv, timeout)
        return stdout if code == 0 and stdout.strip() else ""

    def _subprocess_env(self) -> dict[str, str] | None:
        if not self._explicit_environ:
            return child_environment()
        return child_environment({**os.environ, **self._environ})


def _coerce_result(result: Any) -> CommandResult:
    if isinstance(result, tuple) and len(result) == 3:
        return int(result[0]), str(result[1] or ""), str(result[2] or "")
    if isinstance(result, str):
        return 0, result, ""
    return 127, "", f"unsupported command runner result: {type(result).__name__}"


def _run_command(
    argv: Sequence[str],
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, never shell=True
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=dict(env) if env is not None else None,
        )
    except FileNotFoundError:
        return 127, "", f"executable not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:g}s: {argv[0]}"
    except OSError as exc:
        return 126, "", f"{type(exc).__name__}: {exc}"
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def kvm_device_path() -> Path:
    """Path of the KVM character device (module-level for testability)."""
    return Path("/dev/kvm")


def _kvm_accessible(path: Path) -> bool:
    try:
        return os.access(path, os.R_OK | os.W_OK)
    except OSError:  # pragma: no cover - defensive
        return False


def kvm_group_name() -> str:
    return "kvm"


def kvm_group_member() -> bool | None:
    """Whether the current user is in the ``kvm`` group (``None`` if unknowable)."""
    if sys.platform != "linux" or not hasattr(os, "getgroups"):
        return None
    try:
        import grp

        group_id = grp.getgrnam(kvm_group_name()).gr_gid
    except (ImportError, KeyError):
        return None
    try:
        groups = set(os.getgroups())
    except OSError:  # pragma: no cover - defensive
        groups = set()
    with contextlib.suppress(AttributeError, OSError):
        groups.add(os.getgid())
    return group_id in groups


def _linux_is_virtual_machine() -> bool | None:
    """``True`` when Linux itself runs in a VM, ``False`` when clearly not."""
    identity: list[str] = []
    for candidate in ("/sys/class/dmi/id/product_name", "/sys/class/dmi/id/sys_vendor"):
        text = _read_text(candidate).strip().lower()
        if text:
            identity.append(text)
    cpuinfo = _read_text("/proc/cpuinfo").lower()
    if "hypervisor" in cpuinfo:
        return True
    if not identity:
        return None
    return any(marker in _VM_MARKERS for marker in identity)


def detect_host(
    config: Config | None = None,
    paths: Paths | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    command_runner: Any = None,
) -> HostInfo:
    """Convenience wrapper around :class:`PlatformDetector.detect`."""
    return PlatformDetector(
        config, paths, environ=environ, command_runner=command_runner
    ).detect()


def clear_host_cache() -> None:
    """Forget the process-wide real-host cache (tests / long-running daemons)."""
    global _REAL_HOST_CACHE  # noqa: PLW0603
    _REAL_HOST_CACHE = None


__all__ = [
    "ANDROID_AVD_HOME_ENV",
    "ANDROID_HOME_ENV",
    "ANDROID_SDK_ROOT_ENV",
    "CommandResult",
    "FAKE_ACCEL_ENV",
    "FAKE_ARCH_ENV",
    "FAKE_OS_ENV",
    "FAKE_SDK_ROOT_ENV",
    "JAVA_HOME_ENV",
    "PlatformDetector",
    "clear_host_cache",
    "detect_distro_id",
    "detect_host",
    "kvm_device_path",
    "kvm_group_member",
    "kvm_group_name",
    "normalise_arch",
    "normalise_host_os",
    "real_host_os",
    "sdk_executables",
    "sdk_paths",
    "tool_executable",
    "which_tool",
]
