"""Doctor: diagnose whether this host can run an AVD Fleet installation.

``Doctor`` implements :class:`avdfleet.core.interfaces.DoctorService` by running
a fixed, ordered list of checks and returning a
:class:`avdfleet.core.models.DoctorReport`.  A check never raises: probes
degrade to ``skip``/``warn``/``fail`` with an actionable hint instead.

Status contract
---------------
``ok``    -- the check passed.
``warn``  -- usable, but performance/robustness is reduced; ``report.ok`` stays true.
``fail``  -- the host cannot meet the requirement; ``report.ok`` becomes false.
``skip``  -- not applicable on this platform, or the slow check was not run.

``run(include_slow=False)`` skips the process-launching checks (adb, emulator,
JDK, Node, Appium) and reports them as ``skip``; pass ``include_slow=True``
(``avdfleet doctor --slow``) to execute them.

``fix(check_id)`` only changes host state when that is safe and reversible
(creating SDK directories).  Privileged or destructive actions are returned as
instructions instead: the KVM udev rule requires an explicit ``confirm=True``
action, KVM group membership and WHPX are never applied or rebooted
automatically.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import platform as _platform
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

from avdfleet.core.childenv import (
    ADB_LOCAL_TRANSPORT_MAX_PORT_ENV,
    DEFAULT_ADB_LOCAL_TRANSPORT_MAX_PORT,
    child_environment,
)
from avdfleet.core.config import Config
from avdfleet.core.models import (
    CheckStatus,
    CpuArch,
    DoctorCheck,
    DoctorReport,
    HostInfo,
    HostOS,
    OperationResult,
    SdkPaths,
    utcnow,
)
from avdfleet.core.paths import Paths
from avdfleet.core.ports import find_free_even_port, is_port_free
from avdfleet.platform.detect import (
    CommandResult,
    PlatformDetector,
    kvm_group_member,
    sdk_paths,
    tool_executable,
    which_tool,
)

FAST_CHECK_IDS: tuple[str, ...] = (
    "os",
    "arch",
    "admin",
    "hypervisor",
    "kvm_group",
    "nested",
    "ram",
    "disk",
    "filesystem",
    "path_spaces",
    "python",
    "sdk_root",
    "sdk_tools",
    "licenses",
    "ports",
    "long_paths",
    "defender",
    "clock",
)
SLOW_CHECK_IDS: tuple[str, ...] = (
    "adb",
    "adb_transport",
    "emulator",
    "emulator_accel",
    "jdk",
    "node",
    "appium",
)
CHECK_IDS: tuple[str, ...] = FAST_CHECK_IDS + SLOW_CHECK_IDS

_CHECK_TITLES: dict[str, str] = {
    "os": "Supported operating system",
    "arch": "Supported CPU architecture",
    "admin": "Administrator / sudo",
    "hypervisor": "Hardware virtualisation (WHPX/KVM/HVF)",
    "kvm_group": "KVM group membership",
    "nested": "Nested virtualisation",
    "ram": "Memory capacity",
    "disk": "Free disk space",
    "filesystem": "Data directory filesystem",
    "path_spaces": "Paths without spaces",
    "python": "Python runtime version",
    "sdk_root": "Android SDK root",
    "sdk_tools": "Android SDK tools installed",
    "licenses": "Android SDK licenses",
    "ports": "Port availability",
    "long_paths": "Windows long path support",
    "defender": "Windows Defender exclusions",
    "clock": "System clock",
    "adb": "adb available",
    "adb_transport": "adb supports more than 16 emulators",
    "emulator": "Android emulator available",
    "emulator_accel": "Emulator acceleration usable",
    "jdk": "Java JDK available",
    "node": "Node.js and npm",
    "appium": "Appium and UiAutomator2 driver",
}

MIN_TOTAL_RAM_GB = 4.0
RECOMMENDED_TOTAL_RAM_GB = 8.0
MIN_FREE_DISK_GB = 10.0
RECOMMENDED_FREE_DISK_GB = 30.0
MIN_PYTHON = (3, 11)
MAX_PYTHON_EXCLUSIVE = (3, 14)
MIN_CLOCK_YEAR = 2025
MAX_CLOCK_YEAR = 2100

_FAT_FAMILY = frozenset({"fat", "fat32", "vfat", "msdos", "exfat", "fat16"})

_SDK_SUBDIRS = ("cmdline-tools", "platform-tools", "emulator", "build-tools", "licenses", "system-images")

_UDEV_RULE = 'KERNEL=="kvm", GROUP="kvm", MODE="0666", OPTION+="static_node=kvm"\n'
_UDEV_RULE_PATH = Path("/etc/udev/rules.d/50-avdfleet-kvm.rules")


# ---------------------------------------------------------------------------
# Platform probe helpers (module-level so tests can monkeypatch one function)
# ---------------------------------------------------------------------------


def filesystem_type(path: str | Path | None) -> str:
    """Best-effort filesystem type of *path* (``ntfs``, ``ext4``, ``exfat``...)."""
    if path is None:
        return "unknown"
    ancestor = _existing_ancestor(path)
    if ancestor is None:
        return "unknown"
    if sys.platform == "win32":
        return _windows_filesystem_type(ancestor)
    for argv in (
        ["stat", "-f", "-c", "%T", str(ancestor)],
        ["stat", "-f", "%HT", str(ancestor)],
    ):
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)
            output = (completed.stdout or "").strip()
            if completed.returncode == 0 and output:
                return output
    return "unknown"


def _windows_filesystem_type(path: Path) -> str:
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        volume = ctypes.create_unicode_buffer(261)
        filesystem = ctypes.create_unicode_buffer(261)
        if not kernel32.GetVolumePathNameW(str(path), volume, 261):
            return "unknown"
        if not kernel32.GetVolumeInformationW(volume, None, 0, None, None, None, filesystem, 261):
            return "unknown"
        return filesystem.value or "unknown"
    except (AttributeError, OSError, ValueError):
        return "unknown"


def _existing_ancestor(path: str | Path) -> Path | None:
    target = Path(path).expanduser()
    with contextlib.suppress(OSError):
        target = target.absolute()
    for candidate in (target, *target.parents):
        with contextlib.suppress(OSError):
            if candidate.exists():
                return candidate
    return None


def windows_long_paths_enabled() -> bool | None:
    """Read ``LongPathsEnabled`` from the registry, or ``None`` when unavailable."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows only
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem"
        ) as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
            return bool(value)
    except OSError:
        return None


def defender_present() -> bool | None:
    """``True`` when Windows Defender is installed, ``None`` when undeterminable."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows only
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows Defender"):
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


def can_write_udev_rule() -> bool:
    """``True`` only when the udev rule can be written without escalating privileges."""
    if not sys.platform.startswith("linux"):
        return False
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return False
    with contextlib.suppress(OSError):
        if geteuid() != 0:
            return False
        return _UDEV_RULE_PATH.parent.parent.exists()
    return False


def _is_writable_dir(path: Path) -> bool:
    try:
        with tempfile.TemporaryFile(dir=path):
            return True
    except (OSError, ValueError):
        return False


def _short(text: str, limit: int = 200) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[: limit - 1]}…"


def hypervisor_fix_command(host: HostInfo) -> str:
    """The command/instruction a user (or ``doctor --fix``) should run."""
    if host.os is HostOS.WINDOWS:
        return 'dism /online /enable-feature /featurename:HypervisorPlatform /all /norestart'
    if host.os is HostOS.MACOS:
        return "sysctl -n kern.hv_support"
    if host.os is HostOS.LINUX:
        return "avdfleet doctor --fix hypervisor"
    return "avdfleet doctor --fix hypervisor"


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


def find_adb_server() -> tuple[int, dict[str, str] | None] | None:
    """Return ``(pid, environment)`` for a running adb server, if any.

    Environment inspection is best-effort: some platforms require elevated
    permissions to read another process's environment.  Returning ``None`` for
    the environment means "server exists but its environment is unreadable".
    """
    try:
        import psutil  # noqa: PLC0415 - optional diagnostics dependency
    except ImportError:  # pragma: no cover - psutil is a declared dependency
        return None
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = str(process.info.get("name") or "").lower()
            if name not in {"adb", "adb.exe"}:
                continue
            command_line = " ".join(str(part) for part in (process.info.get("cmdline") or [])).lower()
            if "fork-server" not in command_line and "server" not in command_line:
                continue
            try:
                environment = {
                    str(key): str(value) for key, value in (process.environ() or {}).items()
                }
            except (psutil.Error, OSError):
                environment = None
            return int(process.info["pid"]), environment
        except (psutil.Error, OSError, KeyError, TypeError):
            continue
    return None


@dataclass
class _Context:
    host: HostInfo
    config: Config
    paths: Paths
    sdk: SdkPaths
    environ: dict[str, str]
    include_slow: bool = False


class Doctor:
    """Host readiness checks with safe, explicit fixes.

    Parameters mirror :class:`PlatformDetector`: optional ``config``/``paths``,
    a ``detector`` or a pre-built ``host`` (useful for tests and the API), an
    ``environ`` mapping and an injectable ``command_runner``.
    """

    def __init__(
        self,
        config: Config | None = None,
        paths: Paths | None = None,
        *,
        detector: PlatformDetector | None = None,
        host: HostInfo | None = None,
        environ: Mapping[str, str] | None = None,
        command_runner: Any = None,
    ) -> None:
        self._explicit_environ = environ is not None
        self._environ: dict[str, str] = dict(os.environ if environ is None else environ)
        self._config = config
        self._paths = paths
        self._detector = detector
        self._host_override = host.model_copy(deep=True) if host is not None else None
        self._command_runner = command_runner
        self._last_host: HostInfo | None = self._host_override

    # -- public API ------------------------------------------------------
    def run(self, *, include_slow: bool = False) -> DoctorReport:
        """Run every applicable check and return a :class:`DoctorReport`."""
        context = self._context(include_slow=include_slow)
        checks: list[DoctorCheck] = []
        for check_id in FAST_CHECK_IDS:
            checks.append(self._run_check(check_id, context))
        for check_id in SLOW_CHECK_IDS:
            if include_slow:
                checks.append(self._run_check(check_id, context))
            else:
                checks.append(
                    self._make(
                        check_id,
                        CheckStatus.SKIP,
                        detail="Slow check skipped; run `avdfleet doctor --slow` to execute it.",
                    )
                )
        ok = not any(check.status is CheckStatus.FAIL for check in checks)
        report = DoctorReport(
            ok=ok,
            host=context.host,
            checks=checks,
            summary=_summarise(checks),
        )
        self._last_host = context.host.model_copy(deep=True)
        return report

    def fix(
        self,
        check_id: str,
        *,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> OperationResult:
        """Apply (or describe) the safe fix for *check_id*.

        ``OperationResult.ok`` is ``True`` only when host state actually changed
        (or a dry run confirmed what would change).  For manual, privileged or
        reboot-requiring fixes, ``ok`` is ``False`` and ``data`` carries
        ``manual=True``, ``applied=False`` and the command to run.
        """
        normalized = (check_id or "").strip()
        try:
            if normalized == "sdk_root":
                return self._fix_sdk_root(dry_run=dry_run)
            if normalized == "hypervisor":
                return self._fix_hypervisor(confirm=confirm, dry_run=dry_run)
            if normalized == "kvm_group":
                return self._fix_kvm_group()
            if normalized == "licenses":
                return self._fix_licenses()
            if normalized == "long_paths":
                return self._fix_long_paths()
            if normalized == "defender":
                return self._fix_defender()
            if normalized in _CHECK_TITLES:
                return OperationResult(
                    ok=False,
                    message=(
                        f"Check {normalized!r} cannot be fixed automatically; "
                        "follow the hint reported by `avdfleet doctor`."
                    ),
                    data={"manual": True, "applied": False, "check_id": normalized},
                )
            return OperationResult(
                ok=False,
                message=f"Unknown doctor check id: {normalized!r}",
                data={"manual": True, "applied": False, "unknown": True, "check_id": normalized},
            )
        except OSError as exc:
            return OperationResult(
                ok=False,
                message=f"Fix for {normalized!r} failed: {exc}",
                data={"applied": False, "manual": True, "error": type(exc).__name__},
            )
        except Exception as exc:  # noqa: BLE001 - fixes must never crash callers
            return OperationResult(
                ok=False,
                message=f"Fix for {normalized!r} failed unexpectedly: {type(exc).__name__}: {exc}",
                data={"applied": False, "manual": True, "error": type(exc).__name__},
            )

    # -- resolution ------------------------------------------------------
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

    def _context(self, *, include_slow: bool = False) -> _Context:
        host = self._resolve_host(refresh=False)
        config = self.config
        paths = self.paths
        environ = child_environment(self._environ)
        return _Context(
            host=host,
            config=config,
            paths=paths,
            sdk=sdk_paths(config, paths, environ=environ),
            environ=environ,
            include_slow=include_slow,
        )

    def _resolve_host(self, *, refresh: bool) -> HostInfo:
        if self._host_override is not None and not refresh:
            return self._host_override.model_copy(deep=True)
        detector = self._detector
        if detector is None:
            detector = PlatformDetector(self.config, self.paths, environ=self._environ)
        host = detector.detect(refresh=refresh)
        if self._host_override is None:
            self._last_host = host.model_copy(deep=True)
        return host

    # -- check plumbing --------------------------------------------------
    def _run_check(self, check_id: str, context: _Context) -> DoctorCheck:
        method: Callable[[_Context], DoctorCheck] = getattr(self, f"_check_{check_id}")
        try:
            return method(context)
        except Exception as exc:  # noqa: BLE001 - a broken check never breaks doctor
            return self._make(
                check_id,
                CheckStatus.SKIP,
                detail=f"Check crashed: {type(exc).__name__}: {exc}",
                hint="This is a bug in AVD Fleet; re-run with --log-level DEBUG and report it.",
            )

    def _make(
        self,
        check_id: str,
        status: CheckStatus,
        *,
        detail: str = "",
        hint: str = "",
        fixable: bool = False,
        fix_command: str = "",
    ) -> DoctorCheck:
        return DoctorCheck(
            id=check_id,
            title=_CHECK_TITLES.get(check_id, check_id),
            status=status,
            detail=detail,
            hint=hint,
            fixable=fixable,
            fix_command=fix_command,
        )

    # -- fast checks -----------------------------------------------------
    def _check_os(self, ctx: _Context) -> DoctorCheck:
        host = ctx.host
        if host.os is HostOS.UNKNOWN:
            return self._make(
                "os",
                CheckStatus.FAIL,
                detail=f"Unsupported host operating system: {_platform.system() or 'unknown'}",
                hint=(
                    "AVD Fleet supports Windows 11, macOS (Apple Silicon) and Linux "
                    "(Ubuntu LTS, Fedora, Rocky/RHEL, Arch)."
                ),
            )
        detail = " ".join(bit for bit in (host.os_name, host.os_version) if bit).strip()
        if host.is_wsl:
            return self._make(
                "os",
                CheckStatus.WARN,
                detail=f"{detail} (WSL detected)".strip(),
                hint=(
                    "Running inside WSL is not a supported accelerated-emulator host; "
                    "use a Windows terminal or a native Linux host."
                ),
            )
        if host.is_container:
            detail = f"{detail} (container)" if detail else "container"
        return self._make("os", CheckStatus.OK, detail=detail or host.os.value)

    def _check_arch(self, ctx: _Context) -> DoctorCheck:
        host = ctx.host
        arch = host.arch
        label = arch.value
        if arch is CpuArch.UNKNOWN:
            return self._make(
                "arch",
                CheckStatus.FAIL,
                detail=f"Unsupported CPU architecture: {host.machine or 'unknown'}",
                hint="Use an x86_64 host (or Apple Silicon macOS).",
            )
        if host.os is HostOS.WINDOWS:
            if arch is CpuArch.ARM64:
                return self._make(
                    "arch",
                    CheckStatus.FAIL,
                    detail="Windows on ARM64 is not supported by the Android emulator",
                    hint="Use an x86_64 Windows host.",
                )
            return self._make("arch", CheckStatus.OK, detail=f"x86_64 Windows host ({label})")
        if host.os is HostOS.MACOS:
            if arch is CpuArch.ARM64:
                return self._make(
                    "arch", CheckStatus.OK, detail="Apple Silicon (arm64) macOS host"
                )
            return self._make(
                "arch",
                CheckStatus.WARN,
                detail="Intel (x86_64) macOS host",
                hint="Current Android emulator releases target Apple Silicon; expect limitations.",
            )
        if arch is CpuArch.ARM64:
            return self._make(
                "arch",
                CheckStatus.WARN,
                detail="Linux arm64 host",
                hint="Official Android emulator builds target x86_64 Linux; expect limitations.",
            )
        return self._make("arch", CheckStatus.OK, detail=f"x86_64 Linux host ({label})")

    def _check_admin(self, ctx: _Context) -> DoctorCheck:
        host = ctx.host
        if host.is_admin:
            return self._make(
                "admin",
                CheckStatus.OK,
                detail="Running elevated (Administrator/root)",
            )
        if host.os is HostOS.WINDOWS:
            return self._make(
                "admin",
                CheckStatus.WARN,
                detail="Not running as Administrator",
                hint="Installation, WHPX enablement and service changes need an elevated terminal.",
                fix_command="Start-Process powershell -Verb RunAs",
            )
        sudo = which_tool("sudo", environ=ctx.environ)
        if sudo:
            return self._make(
                "admin",
                CheckStatus.OK,
                detail=f"Not root, but sudo is available ({sudo})",
            )
        return self._make(
            "admin",
            CheckStatus.WARN,
            detail="Not root and sudo was not found",
            hint="Installation needs root or sudo; re-run as root.",
        )

    def _check_hypervisor(self, ctx: _Context) -> DoctorCheck:
        host = ctx.host
        accelerator = host.hypervisor
        if accelerator.available:
            return self._make(
                "hypervisor",
                CheckStatus.OK,
                detail=f"{accelerator.kind.value.upper()} available: {accelerator.detail}".strip(),
            )
        detail = accelerator.detail or "No hardware acceleration detected"
        command = hypervisor_fix_command(host)
        hint = (
            "The Android emulator needs WHPX (Windows), KVM (Linux) or HVF (macOS); there is "
            "no usable software fallback. Enable the accelerator and reboot if required."
        )
        if accelerator.requires_reboot:
            hint = "The accelerator was enabled but needs a reboot before it becomes usable."
        return self._make(
            "hypervisor",
            CheckStatus.FAIL,
            detail=detail,
            hint=hint,
            fixable=True,
            fix_command=command,
        )

    def _check_kvm_group(self, ctx: _Context) -> DoctorCheck:
        host = ctx.host
        if host.os is not HostOS.LINUX:
            return self._make(
                "kvm_group",
                CheckStatus.SKIP,
                detail=f"Not applicable on {host.os.value}",
            )
        if sys.platform != "linux" or not hasattr(os, "getgroups"):
            return self._make(
                "kvm_group",
                CheckStatus.SKIP,
                detail="KVM group membership cannot be determined on this platform",
            )
        if host.is_admin:
            return self._make("kvm_group", CheckStatus.OK, detail="root has direct /dev/kvm access")
        member = kvm_group_member()
        if member is True:
            return self._make("kvm_group", CheckStatus.OK, detail="User is a member of the kvm group")
        if os.path.exists("/dev/kvm") and os.access("/dev/kvm", os.R_OK | os.W_OK):
            return self._make(
                "kvm_group",
                CheckStatus.OK,
                detail="/dev/kvm is accessible without kvm group membership",
            )
        detail = (
            "User is not in the kvm group (and /dev/kvm is not accessible)"
            if member is False
            else "The kvm group does not exist on this host"
        )
        return self._make(
            "kvm_group",
            CheckStatus.FAIL,
            detail=detail,
            hint="Run `sudo usermod -aG kvm $USER`, then log out and back in.",
            fixable=True,
            fix_command="sudo usermod -aG kvm $USER",
        )

    def _check_nested(self, ctx: _Context) -> DoctorCheck:
        nested = ctx.host.hypervisor.nested
        if nested is True:
            return self._make(
                "nested",
                CheckStatus.WARN,
                detail="Host appears to be a virtual machine (nested virtualisation)",
                hint=(
                    "Nested acceleration is best-effort; expect slower boots or no acceleration. "
                    "A physical host is recommended for fleets."
                ),
            )
        if nested is False:
            return self._make("nested", CheckStatus.OK, detail="No nested virtualisation detected")
        return self._make(
            "nested",
            CheckStatus.SKIP,
            detail="Nested-virtualisation state could not be determined",
        )

    def _check_ram(self, ctx: _Context) -> DoctorCheck:
        total = ctx.host.total_ram_gb
        ram_mb = max(512, int(ctx.config.default_ram_mb))
        reserve_mb = max(0, int(ctx.config.capacity_reserve_mb))
        if total <= 0:
            return self._make(
                "ram",
                CheckStatus.WARN,
                detail="Could not determine installed RAM",
                hint="Check that the platform can read physical memory information.",
            )
        capacity = max(0, int((total * 1024 - reserve_mb) // ram_mb))
        detail = (
            f"{total:.1f} GB total RAM; about {capacity} AVD(s) at {ram_mb} MB fit "
            f"with a {reserve_mb} MB host reserve"
        )
        if total >= RECOMMENDED_TOTAL_RAM_GB:
            return self._make("ram", CheckStatus.OK, detail=detail)
        if total >= MIN_TOTAL_RAM_GB:
            return self._make(
                "ram",
                CheckStatus.WARN,
                detail=detail,
                hint="8 GB or more is recommended for fleets; reduce max_instances/default_ram_mb.",
            )
        return self._make(
            "ram",
            CheckStatus.FAIL,
            detail=f"{total:.1f} GB total RAM is below the {MIN_TOTAL_RAM_GB:.0f} GB minimum",
            hint="Add RAM or run fewer/smaller AVDs on a larger host.",
        )

    def _check_disk(self, ctx: _Context) -> DoctorCheck:
        free = ctx.host.free_disk_gb
        if free <= 0:
            return self._make(
                "disk",
                CheckStatus.WARN,
                detail="Could not determine free disk space",
                hint="Check that the data directory is on an accessible volume.",
            )
        detail = f"{free:.1f} GB free on the data volume"
        if free >= RECOMMENDED_FREE_DISK_GB:
            return self._make("disk", CheckStatus.OK, detail=detail)
        if free >= MIN_FREE_DISK_GB:
            return self._make(
                "disk",
                CheckStatus.WARN,
                detail=detail,
                hint="30 GB or more is recommended for system images, AVDs and snapshots.",
            )
        return self._make(
            "disk",
            CheckStatus.FAIL,
            detail=f"{free:.1f} GB free is below the {MIN_FREE_DISK_GB:.0f} GB minimum",
            hint="Free disk space or move the data directory to a larger volume.",
        )

    def _check_filesystem(self, ctx: _Context) -> DoctorCheck:
        data_dir = ctx.paths.data_dir
        if data_dir is None:
            return self._make(
                "filesystem", CheckStatus.SKIP, detail="Data directory is not configured"
            )
        fstype = filesystem_type(data_dir)
        normalized = fstype.strip().lower()
        if not normalized or normalized == "unknown":
            return self._make(
                "filesystem",
                CheckStatus.SKIP,
                detail=f"Could not determine the filesystem type of {data_dir}",
            )
        if normalized in _FAT_FAMILY or "fat" in normalized or "exfat" in normalized:
            return self._make(
                "filesystem",
                CheckStatus.WARN,
                detail=f"{data_dir} is on {fstype}: FAT/exFAT cannot store emulator images safely",
                hint="Move the data/SDK directories to NTFS, APFS, ext4, xfs or btrfs.",
            )
        return self._make("filesystem", CheckStatus.OK, detail=f"{data_dir} is on {fstype}")

    def _check_path_spaces(self, ctx: _Context) -> DoctorCheck:
        suspects: dict[str, str] = {}
        for label, value in (
            ("data", ctx.paths.data_dir),
            ("SDK", ctx.sdk.sdk_root or (str(ctx.paths.sdk_root) if ctx.paths.sdk_root else "")),
            ("AVD", ctx.sdk.avd_home or (str(ctx.paths.avd_home) if ctx.paths.avd_home else "")),
        ):
            if value and " " in str(value):
                suspects[label] = str(value)
        if suspects:
            formatted = ", ".join(f"{label}: {path}" for label, path in suspects.items())
            return self._make(
                "path_spaces",
                CheckStatus.WARN,
                detail=f"Paths contain spaces ({formatted})",
                hint=(
                    "Some Android tools mishandle spaces; prefer paths such as C:\\Android or "
                    "/opt/android for the SDK and data directories."
                ),
            )
        return self._make("path_spaces", CheckStatus.OK, detail="No spaces in data, SDK or AVD paths")

    def _check_python(self, ctx: _Context) -> DoctorCheck:
        version = _platform.python_version()
        info = sys.version_info
        if info[:2] < MIN_PYTHON:
            return self._make(
                "python",
                CheckStatus.FAIL,
                detail=f"Python {version} is too old (requires {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+)",
                hint="Install Python 3.11-3.13 (uv can manage it).",
            )
        if info[:2] >= MAX_PYTHON_EXCLUSIVE:
            return self._make(
                "python",
                CheckStatus.WARN,
                detail=f"Python {version} is newer than the supported range (3.11-3.13)",
                hint="Pin Python 3.13 or earlier for best compatibility.",
            )
        return self._make("python", CheckStatus.OK, detail=f"Python {version}")

    def _check_sdk_root(self, ctx: _Context) -> DoctorCheck:
        root = ctx.sdk.sdk_root
        if not root:
            return self._make(
                "sdk_root",
                CheckStatus.FAIL,
                detail="Android SDK root is not configured",
                hint="Run `avdfleet doctor --fix sdk_root` to create it, then `avdfleet install`.",
                fixable=True,
                fix_command="avdfleet doctor --fix sdk_root",
            )
        path = Path(root)
        if not path.exists():
            return self._make(
                "sdk_root",
                CheckStatus.FAIL,
                detail=f"Android SDK root does not exist: {path}",
                hint="Run `avdfleet doctor --fix sdk_root` to create it, then `avdfleet install`.",
                fixable=True,
                fix_command="avdfleet doctor --fix sdk_root",
            )
        if not path.is_dir():
            return self._make(
                "sdk_root",
                CheckStatus.FAIL,
                detail=f"Android SDK root is not a directory: {path}",
                hint="Point sdk_root at a writable directory.",
            )
        if not _is_writable_dir(path):
            return self._make(
                "sdk_root",
                CheckStatus.FAIL,
                detail=f"Android SDK root is not writable: {path}",
                hint="Fix permissions or choose another sdk_root.",
            )
        return self._make("sdk_root", CheckStatus.OK, detail=f"Android SDK root: {path}")

    def _check_sdk_tools(self, ctx: _Context) -> DoctorCheck:
        present: dict[str, str] = {}
        missing: list[str] = []
        components = (
            ("cmdline-tools", ctx.sdk.cmdline_tools, "sdkmanager"),
            ("platform-tools", ctx.sdk.platform_tools, "adb"),
            ("emulator", ctx.sdk.emulator, "emulator"),
            ("build-tools", ctx.sdk.build_tools, "aapt2"),
        )
        for label, directory, tool in components:
            if directory:
                present[label] = str(Path(directory).name)
                continue
            resolved = tool_executable(tool, ctx.sdk, environ=ctx.environ)
            if resolved:
                present[label] = resolved
            else:
                missing.append(label)
        if missing:
            return self._make(
                "sdk_tools",
                CheckStatus.FAIL,
                detail=f"Missing Android SDK components: {', '.join(missing)}",
                hint="Run `avdfleet install` (or `avdfleet repair`) to install the toolchain.",
                fix_command="avdfleet install",
            )
        formatted = ", ".join(f"{label} ({value})" for label, value in present.items())
        return self._make("sdk_tools", CheckStatus.OK, detail=f"SDK components present: {formatted}")

    def _check_licenses(self, ctx: _Context) -> DoctorCheck:
        licenses_dir = ctx.sdk.licenses_dir
        if not licenses_dir:
            return self._make(
                "licenses",
                CheckStatus.WARN,
                detail="No accepted Android SDK licenses found",
                hint=(
                    "Accept the licenses explicitly: "
                    "`avdfleet install --accept-android-sdk-licenses --yes`."
                ),
                fix_command="avdfleet install --accept-android-sdk-licenses --yes",
            )
        try:
            accepted = sorted(
                entry.name for entry in Path(licenses_dir).iterdir() if entry.is_file()
            )
        except OSError:
            accepted = []
        if not accepted:
            return self._make(
                "licenses",
                CheckStatus.WARN,
                detail=f"License directory {licenses_dir} is empty",
                hint="Accept the licenses explicitly: `avdfleet install --accept-android-sdk-licenses`.",
                fix_command="avdfleet install --accept-android-sdk-licenses --yes",
            )
        return self._make(
            "licenses", CheckStatus.OK, detail=f"Accepted SDK licenses: {', '.join(accepted)}"
        )

    def _check_ports(self, ctx: _Context) -> DoctorCheck:
        config = ctx.config
        api_host = "127.0.0.1"
        api_free = is_port_free(config.api_port, api_host)
        appium_free = is_port_free(config.appium_port_start, api_host)
        console = find_free_even_port(config.port_range_start, config.port_range_end)
        console_pair = f"{console}/{console + 1}" if console is not None else ""
        problems: list[str] = []
        warnings: list[str] = []
        if console is None:
            problems.append(
                f"no free emulator console/adb port pair in {config.port_range_start}-{config.port_range_end}"
            )
        if not api_free:
            warnings.append(f"API port {config.api_port} is busy")
        if not appium_free:
            warnings.append(f"Appium port {config.appium_port_start} is busy")
        if problems:
            return self._make(
                "ports",
                CheckStatus.FAIL,
                detail="; ".join([*problems, *warnings]),
                hint="Stop the conflicting process or change port_range_start/port_range_end.",
            )
        if warnings:
            return self._make(
                "ports",
                CheckStatus.WARN,
                detail="; ".join(warnings),
                hint=(
                    "These ports may be held by avdfleet itself; if start-up fails, change "
                    "api_port or appium_port_start."
                ),
            )
        return self._make(
            "ports",
            CheckStatus.OK,
            detail=(
                f"API port {config.api_port}, Appium port {config.appium_port_start} and "
                f"console pair {console_pair} are free"
            ),
        )

    def _check_long_paths(self, ctx: _Context) -> DoctorCheck:
        if ctx.host.os is not HostOS.WINDOWS:
            return self._make(
                "long_paths", CheckStatus.SKIP, detail=f"Not applicable on {ctx.host.os.value}"
            )
        enabled = windows_long_paths_enabled()
        if enabled is None:
            return self._make(
                "long_paths",
                CheckStatus.SKIP,
                detail="Could not read LongPathsEnabled from the registry",
            )
        command = (
            r"reg add HKLM\SYSTEM\CurrentControlSet\Control\FileSystem "
            r"/v LongPathsEnabled /t REG_DWORD /d 1 /f"
        )
        if enabled:
            return self._make("long_paths", CheckStatus.OK, detail="Windows long path support is enabled")
        return self._make(
            "long_paths",
            CheckStatus.WARN,
            detail="Windows long path support (LongPathsEnabled) is disabled",
            hint="Deep SDK/AVD paths may fail; enable long paths and reboot/log on again.",
            fix_command=command,
        )

    def _check_defender(self, ctx: _Context) -> DoctorCheck:
        if ctx.host.os is not HostOS.WINDOWS:
            return self._make(
                "defender", CheckStatus.SKIP, detail=f"Not applicable on {ctx.host.os.value}"
            )
        present = defender_present()
        if present is None:
            return self._make(
                "defender",
                CheckStatus.SKIP,
                detail="Could not determine Windows Defender state",
            )
        if not present:
            return self._make("defender", CheckStatus.OK, detail="Windows Defender not detected")
        exclusions = sorted(
            {
                str(ctx.paths.data_dir),
                str(ctx.sdk.sdk_root),
                str(ctx.sdk.avd_home or ""),
            }
            - {""}
        )
        quoted = ",".join(f"'{path}'" for path in exclusions)
        command = f'powershell -NoProfile -Command "Add-MpPreference -ExclusionPath {quoted}"'
        return self._make(
            "defender",
            CheckStatus.WARN,
            detail="Windows Defender real-time scanning can slow emulator disk and image I/O",
            hint=(
                "Optionally add exclusions for the SDK, AVD and data directories "
                "(only for folders you trust)."
            ),
            fix_command=command,
        )

    def _check_clock(self, ctx: _Context) -> DoctorCheck:
        now = utcnow()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        if now.year < MIN_CLOCK_YEAR or now.year > MAX_CLOCK_YEAR:
            return self._make(
                "clock",
                CheckStatus.FAIL,
                detail=f"System clock looks wrong: {now.isoformat()}",
                hint="Enable automatic time synchronisation, then re-run `avdfleet doctor`.",
            )
        return self._make("clock", CheckStatus.OK, detail=f"System clock: {now.isoformat()} (UTC)")

    # -- slow checks -----------------------------------------------------
    def _check_adb(self, ctx: _Context) -> DoctorCheck:
        executable = self._resolve_tool(ctx, "adb")
        if not executable:
            return self._make(
                "adb",
                CheckStatus.FAIL,
                detail="adb was not found (platform-tools not installed)",
                hint="Run `avdfleet install` to install platform-tools.",
                fix_command="avdfleet install",
            )
        code, stdout, stderr = self._run_tool([executable, "version"], timeout=15.0)
        text = f"{stdout}\n{stderr}"
        match = re.search(r"Android Debug Bridge version ([\w.\-]+)", text)
        if code != 0:
            return self._make(
                "adb",
                CheckStatus.FAIL,
                detail=f"`adb version` failed (exit {code}): {_short(stderr or stdout)}",
                hint="Reinstall platform-tools (`avdfleet repair`).",
            )
        if match:
            return self._make("adb", CheckStatus.OK, detail=f"adb {match.group(1)} ({executable})")
        return self._make(
            "adb",
            CheckStatus.WARN,
            detail=f"adb ran but its version could not be parsed: {_short(stdout or stderr)}",
        )

    def _check_adb_transport(self, ctx: _Context) -> DoctorCheck:
        required = DEFAULT_ADB_LOCAL_TRANSPORT_MAX_PORT
        server = find_adb_server()
        if server is None:
            return self._make(
                "adb_transport",
                CheckStatus.OK,
                detail=(
                    f"No adb server is running; {ADB_LOCAL_TRANSPORT_MAX_PORT_ENV}={required} "
                    "will be applied when AVD Fleet starts one (supports 64 emulators)."
                ),
            )
        pid, environment = server
        if environment is None:
            return self._make(
                "adb_transport",
                CheckStatus.WARN,
                detail=f"An adb server (pid {pid}) is running but its environment is unreadable.",
                hint=(
                    "Restart adb from an AVD Fleet shell: `adb kill-server`, then start an "
                    "instance. Ensure ADB_LOCAL_TRANSPORT_MAX_PORT is at least "
                    f"{required}."
                ),
            )
        raw = environment.get(ADB_LOCAL_TRANSPORT_MAX_PORT_ENV)
        try:
            limit = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            limit = None
        if limit is not None and limit >= required:
            return self._make(
                "adb_transport",
                CheckStatus.OK,
                detail=f"adb server (pid {pid}) local transport limit is {limit}.",
            )
        return self._make(
            "adb_transport",
            CheckStatus.FAIL,
            detail=(
                f"adb server (pid {pid}) was started with "
                f"{ADB_LOCAL_TRANSPORT_MAX_PORT_ENV}={raw or 'unset'}; instances 17+ "
                f"will be invisible (need at least {required})."
            ),
            hint=(
                "Run `adb kill-server`, then start the fleet again so the server inherits "
                f"ADB_LOCAL_TRANSPORT_MAX_PORT={required}. This disconnects other adb clients."
            ),
        )

    def _check_emulator(self, ctx: _Context) -> DoctorCheck:
        executable = self._resolve_tool(ctx, "emulator")
        if not executable:
            return self._make(
                "emulator",
                CheckStatus.FAIL,
                detail="The Android emulator was not found",
                hint="Run `avdfleet install` to install the emulator package.",
                fix_command="avdfleet install",
            )
        code, stdout, stderr = self._run_tool([executable, "-version"], timeout=20.0)
        text = f"{stdout}\n{stderr}"
        match = re.search(r"Android emulator version ([\w.\-]+)", text)
        if code != 0:
            return self._make(
                "emulator",
                CheckStatus.FAIL,
                detail=f"`emulator -version` failed (exit {code}): {_short(stderr or stdout)}",
                hint="Reinstall the emulator (`avdfleet repair`).",
            )
        if match:
            return self._make(
                "emulator", CheckStatus.OK, detail=f"emulator {match.group(1)} ({executable})"
            )
        return self._make(
            "emulator",
            CheckStatus.WARN,
            detail=f"emulator ran but its version could not be parsed: {_short(stdout or stderr)}",
        )

    def _check_emulator_accel(self, ctx: _Context) -> DoctorCheck:
        executable = self._resolve_tool(ctx, "emulator")
        if not executable:
            return self._make(
                "emulator_accel",
                CheckStatus.SKIP,
                detail="Emulator is not installed; acceleration usability cannot be checked",
            )
        code, stdout, stderr = self._run_tool([executable, "-accel-check"], timeout=30.0)
        text = f"{stdout}\n{stderr}".strip()
        lowered = text.lower()
        first_line = _short(text.splitlines()[0] if text.splitlines() else "")
        if "is installed and usable" in lowered or "is usable" in lowered:
            return self._make("emulator_accel", CheckStatus.OK, detail=first_line or "Acceleration usable")
        if (
            "not installed" in lowered
            or "not usable" in lowered
            or "is disabled" in lowered
            or ("requires" in lowered and "reboot" in lowered)
        ):
            return self._make(
                "emulator_accel",
                CheckStatus.FAIL,
                detail=first_line or "Emulator reports no usable acceleration",
                hint="Enable WHPX, KVM or HVF (see the hypervisor check) and reboot if required.",
                fixable=True,
                fix_command=hypervisor_fix_command(ctx.host),
            )
        if code != 0:
            return self._make(
                "emulator_accel",
                CheckStatus.FAIL,
                detail=f"`emulator -accel-check` failed (exit {code}): {_short(text)}",
                hint="Enable WHPX, KVM or HVF (see the hypervisor check).",
                fixable=True,
                fix_command=hypervisor_fix_command(ctx.host),
            )
        return self._make(
            "emulator_accel",
            CheckStatus.WARN,
            detail=f"Could not interpret `emulator -accel-check` output: {first_line or _short(text)}",
        )

    def _check_jdk(self, ctx: _Context) -> DoctorCheck:
        executable = self._resolve_java(ctx)
        if not executable:
            return self._make(
                "jdk",
                CheckStatus.WARN,
                detail="Java JDK not found (required by sdkmanager/avdmanager)",
                hint=f"Install JDK {ctx.config.jdk_version} (or let `avdfleet install` do it).",
                fix_command="avdfleet install",
            )
        code, stdout, stderr = self._run_tool([executable, "-version"], timeout=20.0)
        text = stderr or stdout
        match = re.search(r'version "(\d+)(?:\.(\d+))?', text)
        if code != 0:
            return self._make(
                "jdk",
                CheckStatus.FAIL,
                detail=f"`java -version` failed (exit {code}): {_short(text)}",
                hint="Reinstall the JDK (`avdfleet repair`).",
            )
        if not match:
            return self._make(
                "jdk",
                CheckStatus.WARN,
                detail=f"java ran but its version could not be parsed: {_short(text)}",
            )
        major = int(match.group(1))
        expected = str(ctx.config.jdk_version).strip()
        if expected.isdigit() and int(expected) != major:
            return self._make(
                "jdk",
                CheckStatus.WARN,
                detail=f"JDK {major} found; configuration expects JDK {expected}",
            )
        return self._make("jdk", CheckStatus.OK, detail=f"OpenJDK {major} ({executable})")

    def _check_node(self, ctx: _Context) -> DoctorCheck:
        node = self._resolve_tool(ctx, "node")
        npm = self._resolve_tool(ctx, "npm")
        if not node:
            return self._make(
                "node",
                CheckStatus.WARN,
                detail="Node.js was not found (required for Appium automation)",
                hint=f"Install Node.js {ctx.config.node_version}+ (or let `avdfleet install` do it).",
                fix_command="avdfleet install",
            )
        code, stdout, stderr = self._run_tool([node, "--version"], timeout=15.0)
        version = (stdout or stderr).strip()
        if code != 0 or not version:
            return self._make(
                "node",
                CheckStatus.FAIL,
                detail=f"`node --version` failed (exit {code}): {_short(stderr or stdout)}",
                hint="Reinstall Node.js (`avdfleet repair`).",
            )
        if not npm:
            return self._make(
                "node",
                CheckStatus.WARN,
                detail=f"Node.js {version} found but npm is missing",
                hint="Reinstall Node.js including npm.",
            )
        npm_code, npm_out, npm_err = self._run_tool([npm, "--version"], timeout=15.0)
        npm_version = (npm_out or npm_err).strip()
        if npm_code != 0:
            return self._make(
                "node",
                CheckStatus.WARN,
                detail=f"Node.js {version} found but `npm --version` failed (exit {npm_code})",
            )
        return self._make(
            "node", CheckStatus.OK, detail=f"Node.js {version} / npm {npm_version} ({node})"
        )

    def _check_appium(self, ctx: _Context) -> DoctorCheck:
        executable = self._resolve_tool(ctx, "appium")
        if not executable:
            return self._make(
                "appium",
                CheckStatus.WARN,
                detail="Appium was not found (Appium automation unavailable until installed)",
                hint="Install Appium 4 and the UiAutomator2 driver (`avdfleet install`).",
                fix_command="avdfleet install",
            )
        code, stdout, stderr = self._run_tool([executable, "--version"], timeout=30.0)
        version = (stdout or stderr).strip().splitlines()
        version_text = version[0].strip() if version else ""
        if code != 0 or not version_text:
            return self._make(
                "appium",
                CheckStatus.WARN,
                detail=f"`appium --version` failed (exit {code}): {_short(stderr or stdout)}",
                hint="Reinstall Appium (`avdfleet repair`).",
            )
        driver_code, driver_out, driver_err = self._run_tool(
            [executable, "driver", "list", "--installed"], timeout=30.0
        )
        driver_text = f"{driver_out}\n{driver_err}".lower()
        if driver_code == 0 and "uiautomator2" in driver_text:
            return self._make(
                "appium",
                CheckStatus.OK,
                detail=f"Appium {version_text} with the UiAutomator2 driver ({executable})",
            )
        return self._make(
            "appium",
            CheckStatus.WARN,
            detail=f"Appium {version_text} found but the UiAutomator2 driver is missing",
            hint="Run `appium driver install uiautomator2` (or `avdfleet install`).",
            fix_command="appium driver install uiautomator2",
        )

    # -- tool helpers ----------------------------------------------------
    def _resolve_tool(self, ctx: _Context, name: str) -> str | None:
        return tool_executable(name, ctx.sdk, environ=ctx.environ)

    def _resolve_java(self, ctx: _Context) -> str | None:
        java_home = ctx.sdk.env.get("JAVA_HOME") or ctx.environ.get("JAVA_HOME")
        if java_home:
            for suffix in ("", ".exe"):
                candidate = Path(java_home) / "bin" / f"java{suffix}"
                if candidate.is_file():
                    return str(candidate)
        return which_tool("java", environ=ctx.environ)

    def _run_tool(self, argv: Sequence[str], *, timeout: float = 15.0) -> CommandResult:
        if self._command_runner is not None:
            try:
                result = self._command_runner(list(argv), timeout)
                return _coerce_result(result)
            except Exception as exc:  # noqa: BLE001 - injected runner must not break doctor
                return 127, "", f"command runner failed: {type(exc).__name__}: {exc}"
        env = {**os.environ, **self._environ} if self._explicit_environ else None
        return _run_command(argv, timeout, env)

    # -- fixes -----------------------------------------------------------
    def _fix_sdk_root(self, *, dry_run: bool) -> OperationResult:
        context = self._context()
        root = context.sdk.sdk_root
        if not root:
            return OperationResult(
                ok=False,
                message="Android SDK root is not configured; set sdk_root (AVDFLEET_SDK_ROOT).",
                data={"applied": False, "manual": True},
            )
        targets: list[Path] = [Path(root)]
        targets.extend(Path(root) / name for name in _SDK_SUBDIRS)
        for optional in (context.paths.data_dir, context.paths.avd_home):
            if optional is not None:
                targets.append(Path(optional))
        created: list[str] = []
        for target in targets:
            if target.exists():
                continue
            created.append(str(target))
            if dry_run:
                continue
            target.mkdir(parents=True, exist_ok=True)
        if dry_run:
            return OperationResult(
                ok=True,
                message=f"Would create {len(created)} director{'y' if len(created) == 1 else 'ies'}.",
                data={"applied": False, "dry_run": True, "action": "create_sdk_dirs", "created": created},
            )
        return OperationResult(
            ok=True,
            message=f"Created {len(created)} SDK director{'y' if len(created) == 1 else 'ies'}.",
            data={"applied": True, "action": "create_sdk_dirs", "created": created},
        )

    def _fix_hypervisor(self, *, confirm: bool, dry_run: bool) -> OperationResult:
        host = self._last_host or self._resolve_host(refresh=False)
        if host.os is HostOS.WINDOWS:
            command = hypervisor_fix_command(host)
            return OperationResult(
                ok=False,
                message=(
                    "Manual action required: enable Windows Hypervisor Platform in an "
                    "Administrator terminal, then reboot. AVD Fleet never reboots the host."
                ),
                data={
                    "applied": False,
                    "manual": True,
                    "action": "enable_whpx",
                    "command": command,
                    "requires_reboot": True,
                },
            )
        if host.os is HostOS.MACOS:
            return OperationResult(
                ok=False,
                message=(
                    "HVF is built into macOS. If kern.hv_support is 0, the host is virtualised "
                    "without nested acceleration; no software fix is available."
                ),
                data={
                    "applied": False,
                    "manual": True,
                    "action": "enable_hvf",
                    "command": hypervisor_fix_command(host),
                    "requires_reboot": False,
                },
            )
        rule_path = _UDEV_RULE_PATH
        if not confirm:
            return OperationResult(
                ok=False,
                message=(
                    "The KVM udev rule is only installed with an explicit action. Re-run with "
                    "confirmation (`avdfleet doctor --fix hypervisor --yes`)."
                ),
                data={
                    "applied": False,
                    "manual": True,
                    "requires_confirmation": True,
                    "action": "install_udev_rule",
                    "rule_path": str(rule_path),
                    "rule": _UDEV_RULE,
                    "command": "sudo avdfleet doctor --fix hypervisor --yes",
                },
            )
        if dry_run:
            return OperationResult(
                ok=True,
                message=f"Would install the KVM udev rule at {rule_path}.",
                data={
                    "applied": False,
                    "dry_run": True,
                    "action": "install_udev_rule",
                    "rule_path": str(rule_path),
                    "rule": _UDEV_RULE,
                },
            )
        if not can_write_udev_rule():
            return OperationResult(
                ok=False,
                message="Writing the udev rule requires root; re-run with sudo.",
                data={
                    "applied": False,
                    "manual": True,
                    "action": "install_udev_rule",
                    "rule_path": str(rule_path),
                    "command": "sudo avdfleet doctor --fix hypervisor --yes",
                },
            )
        rule_path.parent.mkdir(parents=True, exist_ok=True)
        rule_path.write_text(_UDEV_RULE, encoding="ascii")
        return OperationResult(
            ok=True,
            message=(
                f"Installed {rule_path}; add the user to the kvm group and reload udev rules."
            ),
            data={
                "applied": True,
                "action": "install_udev_rule",
                "rule_path": str(rule_path),
                "command": "sudo udevadm control --reload-rules && sudo udevadm trigger",
                "requires_relogin": True,
            },
        )

    def _fix_kvm_group(self) -> OperationResult:
        return OperationResult(
            ok=False,
            message=(
                "Not applied automatically: group changes only take effect for a new login "
                "session and can lock users out of a remote shell if done blindly."
            ),
            data={
                "applied": False,
                "manual": True,
                "action": "add_kvm_group",
                "command": "sudo usermod -aG kvm $USER",
                "next": "Log out and back in, then re-run `avdfleet doctor`.",
            },
        )

    def _fix_licenses(self) -> OperationResult:
        return OperationResult(
            ok=False,
            message=(
                "Android SDK license acceptance requires the explicit installer flag; "
                "AVD Fleet never accepts licenses silently."
            ),
            data={
                "applied": False,
                "manual": True,
                "action": "accept_licenses",
                "command": "avdfleet install --accept-android-sdk-licenses --yes",
            },
        )

    def _fix_long_paths(self) -> OperationResult:
        return OperationResult(
            ok=False,
            message="Manual action required: enable Windows long paths in an Administrator terminal.",
            data={
                "applied": False,
                "manual": True,
                "action": "enable_long_paths",
                "command": (
                    r"reg add HKLM\SYSTEM\CurrentControlSet\Control\FileSystem "
                    r"/v LongPathsEnabled /t REG_DWORD /d 1 /f"
                ),
                "requires_reboot": False,
            },
        )

    def _fix_defender(self) -> OperationResult:
        return OperationResult(
            ok=False,
            message=(
                "Manual action required: adding Defender exclusions needs Administrator and "
                "should only be done for folders you trust."
            ),
            data={
                "applied": False,
                "manual": True,
                "action": "defender_exclusions",
                "command": 'powershell -NoProfile -Command "Add-MpPreference -ExclusionPath ..."',
            },
        )


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


def _summarise(checks: Sequence[DoctorCheck]) -> str:
    counts = {status: 0 for status in CheckStatus}
    for check in checks:
        counts[check.status] += 1
    ok = counts[CheckStatus.OK]
    warn = counts[CheckStatus.WARN]
    fail = counts[CheckStatus.FAIL]
    skip = counts[CheckStatus.SKIP]
    parts = f"{ok} ok, {warn} warning(s), {fail} failed, {skip} skipped"
    if fail:
        return f"{fail} critical issue(s) found: {parts}"
    if warn:
        return f"All critical checks passed with {warn} warning(s): {parts}"
    return f"All checks passed: {parts}"


__all__ = [
    "CHECK_IDS",
    "FAST_CHECK_IDS",
    "SLOW_CHECK_IDS",
    "Doctor",
    "can_write_udev_rule",
    "defender_present",
    "filesystem_type",
    "hypervisor_fix_command",
    "windows_long_paths_enabled",
]
