"""Platform install providers.

Providers translate avdfleet's installation requirements into *concrete host
commands*.  They are deliberately side-effect free: every mutating operation
returns :class:`Command` objects which the :class:`~.engine.InstallerEngine`
executes through its injectable command runner.  No provider ever shells out,
touches the filesystem for an install or reboots the host on its own - that is
what makes ``--dry-run``, unit tests and audit logs honest.

The public surface intentionally mirrors the core ``HostProvider`` protocol
while adding the package-manager, environment and virtualisation knowledge the
installer needs.  Platform state (admin, WHPX features, KVM group membership,
reboot pending) is injectable so the full unit suite runs on every OS without
touching the real machine.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

from avdfleet.core.models import (
    AcceleratorInfo,
    AcceleratorKind,
    CpuArch,
    HostInfo,
    HostOS,
)

#: Canonical Linux udev rule that grants the ``kvm`` group access to ``/dev/kvm``.
#: Mirrors the rule documented for GitHub Actions hardware-accelerated runners.
KVM_UDEV_RULE_PATH = "/etc/udev/rules.d/70-avdfleet-kvm.rules"
KVM_UDEV_RULE = 'KERNEL=="kvm", GROUP="kvm", MODE="0666", OPTIONS+="static_node=kvm"'

#: Tool template keys every provider must publish.
REQUIRED_TOOL_TEMPLATES = ("jdk", "node", "appium", "studio", "scrcpy")


class EnvScope(StrEnum):
    """Where an environment variable must be persisted."""

    PROCESS = "process"
    USER = "user"
    SYSTEM = "system"


@dataclass(frozen=True)
class Command:
    """A single host command produced by a provider.

    ``argv`` is always an argument vector - the runner never uses a shell
    unless the provider explicitly explains why (``shell=True`` is only used
    for the unavoidable ``sh -c`` / ``powershell -Command`` wrappers).  The
    metadata flags let the engine enforce admin rights and reboot semantics
    without parsing the command line.
    """

    argv: list[str]
    description: str = ""
    admin: bool = False
    requires_reboot: bool = False
    stdin: str | None = None
    timeout_s: int | None = None
    env: dict[str, str] = field(default_factory=dict)

    @property
    def display(self) -> str:
        return " ".join(shlex.quote(part) if " " in part else part for part in self.argv)

    @property
    def binary(self) -> str:
        return self.argv[0] if self.argv else ""

    def with_stdin(self, stdin: str | None) -> Command:
        return replace(self, stdin=stdin)


@dataclass(frozen=True)
class ToolTemplate:
    """Declarative install recipe for one developer tool."""

    tool: str
    commands: tuple[Command, ...] = ()
    binaries: tuple[str, ...] = ()
    version_argv: tuple[str, ...] = ()
    notes: str = ""
    optional: bool = False

    @property
    def available(self) -> bool:
        return bool(self.commands)


@dataclass(frozen=True)
class VirtualizationPlan:
    """How to obtain hardware acceleration on this host.

    ``available`` is what the provider knows about firmware/hypervisor
    capability, ``enabled`` is whether the accelerator is usable right now.
    A plan with ``requires_reboot=True`` must never be executed by a provider;
    the engine pauses the install and asks the operator to reboot.
    """

    kind: AcceleratorKind = AcceleratorKind.NONE
    available: bool = False
    enabled: bool = False
    requires_reboot: bool = False
    commands: tuple[Command, ...] = ()
    nested: bool | None = None
    notes: tuple[str, ...] = ()
    udev_rule_path: str | None = None
    udev_rule_content: str | None = None
    groups: tuple[str, ...] = ()

    @property
    def needs_change(self) -> bool:
        return bool(self.commands) and not self.enabled

    @property
    def detail(self) -> str:
        state = "enabled" if self.enabled else ("available" if self.available else "unavailable")
        return f"{self.kind.value}: {state}"


def _quote_powershell(value: str) -> str:
    """Single-quote a PowerShell literal (embedded quotes are doubled)."""
    return "'" + value.replace("'", "''") + "'"


class InstallProvider(ABC):
    """Abstract provider for one host family."""

    #: Stable provider id used by ``--platform-provider`` and the registry.
    id: str = "generic"
    platform: HostOS = HostOS.UNKNOWN
    display_name: str = "Generic host"
    #: Human name of the native package manager (``apt``, ``brew``, ``winget``...).
    package_manager_name: str = "generic"
    path_separator: str = ":"
    admin_error_hint: str = "re-run this command with administrator/sudo privileges"

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        is_admin: bool | None = None,
        which: Callable[[str], str | None] | None = None,
        arch: CpuArch | None = None,
    ) -> None:
        self._env: dict[str, str] = dict(os.environ if env is None else env)
        self._is_admin_override = is_admin
        self._which_override = which
        self._arch = arch or _detect_arch(self._env)

    # ------------------------------------------------------------------
    # Identity / discovery
    # ------------------------------------------------------------------
    @property
    def arch(self) -> CpuArch:
        return self._arch

    @property
    def is_admin(self) -> bool:
        if self._is_admin_override is not None:
            return self._is_admin_override
        try:
            return self._detect_admin()
        except Exception:  # pragma: no cover - detection must never crash a plan
            return False

    def _detect_admin(self) -> bool:
        if self.platform is HostOS.WINDOWS:
            if sys.platform != "win32":
                return False
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        geteuid = getattr(os, "geteuid", None)
        return bool(geteuid and geteuid() == 0)

    def which(self, binary: str) -> str | None:
        if not binary:
            return None
        if self._which_override is not None:
            return self._which_override(binary)
        search_path = self._env.get("PATH") or self._env.get("Path") or os.environ.get("PATH")
        return shutil.which(binary, path=search_path)

    def command_exists(self, binary: str) -> bool:
        return self.which(binary) is not None

    def env_value(self, name: str, default: str = "") -> str:
        return self._env.get(name, default)

    def home(self) -> Path:
        """Best-effort home directory using the provider's injected environment."""
        for candidate in (self._env.get("HOME"), self._env.get("USERPROFILE")):
            if candidate:
                return Path(candidate)
        try:
            return Path.home()
        except RuntimeError:  # pragma: no cover - exotic environments
            return Path("~").expanduser()

    def shell_quote(self, value: str) -> str:
        """Quote *value* for the provider's native shell."""
        if self.platform is HostOS.WINDOWS:
            return subprocess.list2cmdline([str(value)])
        return shlex.quote(str(value))

    def _display_path(self, path: str | Path) -> str:
        return str(path)

    # ------------------------------------------------------------------
    # Package management
    # ------------------------------------------------------------------
    @property
    @abstractmethod
    def package_manager(self) -> str:
        """Native package manager binary (``winget``, ``brew``, ``apt-get``...)."""

    def package_manager_ready(self) -> bool:
        return self.command_exists(self.package_manager)

    @abstractmethod
    def install_system_packages(self, names: Sequence[str]) -> list[Command]:
        """Return the commands that install native packages by name."""

    def prerequisites(self) -> list[Command]:
        """Commands required before the package manager can be used.

        For macOS this includes the Command Line Tools *before* Homebrew.
        """
        return []

    def _sudo(self) -> list[str]:
        return [] if self.is_admin else ["sudo"]

    # ------------------------------------------------------------------
    # Environment persistence
    # ------------------------------------------------------------------
    @abstractmethod
    def set_env_var(
        self,
        name: str,
        value: str,
        scope: EnvScope = EnvScope.USER,
    ) -> list[Command]:
        """Persist *name* = *value* for future shells/processes."""

    def remove_env_var(self, name: str, scope: EnvScope = EnvScope.USER) -> list[Command]:
        """Best-effort removal of a persisted environment variable."""
        return []

    def get_env_var(self, name: str, scope: EnvScope = EnvScope.PROCESS) -> str | None:
        """Read the variable as visible to a new process.

        Providers only see their injected environment snapshot; persistence is
        reported through :meth:`set_env_var` command generation (and the real
        Windows registry for user scope on Windows).
        """
        if scope is EnvScope.USER and self.platform is HostOS.WINDOWS and sys.platform == "win32":
            try:
                import winreg

                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                    value, _kind = winreg.QueryValueEx(key, name)
                    return str(value)
            except OSError:
                return None
        return self._env.get(name)

    def profile_files(self) -> list[Path]:
        """Shell profile files that receive the avdfleet environment block."""
        return []

    def add_to_path(self, entries: Sequence[str], scope: EnvScope = EnvScope.USER) -> list[Command]:
        """Return commands that prepend *entries* to ``PATH`` idempotently."""
        cleaned = [str(entry) for entry in entries if str(entry)]
        if not cleaned:
            return []
        if self.platform is HostOS.WINDOWS:
            return self._windows_set_path_commands(cleaned, scope, append=False)
        return self._posix_path_commands(cleaned)

    def remove_from_path(self, entries: Sequence[str], scope: EnvScope = EnvScope.USER) -> list[Command]:
        cleaned = [str(entry) for entry in entries if str(entry)]
        if not cleaned:
            return []
        if self.platform is HostOS.WINDOWS:
            return self._windows_set_path_commands(cleaned, scope, append=True, remove=True)
        script = self._posix_remove_block_script("PATH")
        return [Command(["sh", "-c", script], description="remove the avdfleet PATH block")]

    # -- concrete env helpers ------------------------------------------
    def _posix_env_commands(self, name: str, value: str) -> list[Command]:
        """Persist an ``export`` line inside a managed block in the shell profile."""
        commands: list[Command] = []
        export_line = f"export {name}={shlex.quote(value)}"
        for profile in self.profile_files():
            script = self._posix_upsert_block_script(
                marker=name,
                lines=[export_line],
                target=profile,
            )
            commands.append(
                Command(
                    ["sh", "-c", script],
                    description=f"persist {name} in {profile}",
                    env={"HOME": str(self.home())},
                )
            )
        return commands

    def _posix_path_commands(self, entries: Sequence[str]) -> list[Command]:
        joined = os.pathsep.join(entries)
        export_line = f'export PATH={shlex.quote(joined)}"$PATH"'
        commands: list[Command] = []
        for profile in self.profile_files():
            script = self._posix_upsert_block_script(
                marker="PATH",
                lines=[export_line],
                target=profile,
            )
            commands.append(
                Command(
                    ["sh", "-c", script],
                    description=f"prepend avdfleet tools to PATH in {profile}",
                    env={"HOME": str(self.home())},
                )
            )
        return commands

    def _posix_remove_block_script(self, marker: str) -> str:
        return self._posix_remove_block("PATH" if marker == "PATH" else marker)

    def _posix_remove_block(self, marker: str) -> str:
        start, end = self._markers(marker)
        script = [
            f"f={shlex.quote(str(self.profile_files()[0]))}" if self.profile_files() else 'f="$HOME/.profile"',
            '[ -f "$f" ] || exit 0',
            f"if grep -qF {shlex.quote(start)} \"$f\"; then",
            f"  sed -e {shlex.quote('/' + start + '/,/' + end + '/d')} \"$f\" > \"$f.avdfleet.tmp\" && mv \"$f.avdfleet.tmp\" \"$f\"",
            "fi",
        ]
        return "\n".join(script)

    @staticmethod
    def _markers(marker: str) -> tuple[str, str]:
        return (f"# >>> avdfleet {marker}", f"# <<< avdfleet {marker}")

    def _posix_upsert_block_script(self, *, marker: str, lines: Sequence[str], target: Path) -> str:
        start, end = self._markers(marker)
        script = [
            f"f={shlex.quote(str(target))}",
            'mkdir -p "$(dirname "$f")"',
            'touch "$f"',
            f"if grep -qF {shlex.quote(start)} \"$f\" 2>/dev/null; then",
            f"  sed -e {shlex.quote('/' + start + '/,/' + end + '/d')} \"$f\" > \"$f.avdfleet.tmp\" && mv \"$f.avdfleet.tmp\" \"$f\"",
            "fi",
            f"printf '%s\\n' {shlex.quote(start)} "
            + " ".join(shlex.quote(line) for line in lines)
            + f" {shlex.quote(end)} >> \"$f\"",
        ]
        return "\n".join(script)

    def _windows_env_commands(self, name: str, value: str, scope: EnvScope) -> list[Command]:
        target = "User" if scope is not EnvScope.SYSTEM else "Machine"
        script = (
            f"[Environment]::SetEnvironmentVariable("
            f"{_quote_powershell(name)},{_quote_powershell(value)},{_quote_powershell(target)})"
        )
        return [
            Command(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                description=f"persist {name} for the {target.lower()} environment",
                admin=scope is EnvScope.SYSTEM,
            )
        ]

    def _windows_set_path_commands(
        self,
        entries: Sequence[str],
        scope: EnvScope,
        *,
        append: bool,
        remove: bool = False,
    ) -> list[Command]:
        target = "User" if scope is not EnvScope.SYSTEM else "Machine"
        entries_ps = ", ".join(_quote_powershell(entry) for entry in entries)
        if remove:
            body = (
                f"$parts = $parts | Where-Object {{ {entries_ps} -notcontains $_ }}"
            )
        elif append:
            body = (
                f"foreach ($e in @({entries_ps})) {{ if ($parts -notcontains $e) {{ $parts += $e }} }}"
            )
        else:
            body = (
                f"foreach ($e in @({entries_ps})) {{ if ($parts -notcontains $e) {{ $parts = @($e) + $parts }} }}"
            )
        script = (
            f"$scope = {_quote_powershell(target)}; "
            "$p = [Environment]::GetEnvironmentVariable('Path', $scope); "
            "if ($null -eq $p) { $p = '' }; "
            "$parts = @($p -split ';' | Where-Object { $_ }); "
            f"{body}; "
            "[Environment]::SetEnvironmentVariable('Path', ($parts -join ';'), $scope)"
        )
        return [
            Command(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                description=f"update {target.lower()} PATH",
                admin=scope is EnvScope.SYSTEM,
            )
        ]

    # ------------------------------------------------------------------
    # Reboot detection
    # ------------------------------------------------------------------
    def reboot_required(self) -> bool:
        """Whether the host already has a pending reboot."""
        return False

    # ------------------------------------------------------------------
    # Virtualisation
    # ------------------------------------------------------------------
    @abstractmethod
    def virtualization_plan(self) -> VirtualizationPlan:
        """Return the acceleration plan for this host."""

    def enable_virtualization(self) -> VirtualizationPlan:
        """Alias for :meth:`virtualization_plan` (never executes or reboots)."""
        return self.virtualization_plan()

    def nested_virtualization(self) -> bool | None:
        """``True``/``False`` when known, ``None`` when undetectable."""
        return None

    def aehd_installed(self) -> bool:
        """Whether the Android Emulator Hypervisor Driver is installed."""
        return False

    def aehd_migration_plan(self) -> VirtualizationPlan:
        """Plan that removes AEHD before WHPX is enabled (Windows only)."""
        return VirtualizationPlan(
            kind=AcceleratorKind.AEHD,
            available=False,
            notes=("AEHD migration is only meaningful on Windows",),
        )

    # ------------------------------------------------------------------
    # Tool templates / locations
    # ------------------------------------------------------------------
    @property
    @abstractmethod
    def install_command_templates(self) -> dict[str, ToolTemplate]:
        """Install recipe per tool key (see :data:`REQUIRED_TOOL_TEMPLATES`)."""

    def tool_template(self, tool: str) -> ToolTemplate | None:
        return self.install_command_templates.get(tool)

    def tool_install_commands(self, tool: str) -> list[Command]:
        template = self.tool_template(tool)
        return list(template.commands) if template else []

    @property
    @abstractmethod
    def studio_config_dir(self) -> Path:
        """Android Studio user configuration directory for this host."""

    def studio_install_dirs(self) -> list[Path]:
        return []

    def java_home_roots(self) -> list[Path]:
        """Common JDK installation roots scanned when ``java`` is not on PATH."""
        if self.platform is HostOS.WINDOWS:
            program_files = Path(self.env_value("PROGRAMFILES") or "C:/Program Files")
            local_app_data = Path(self.env_value("LOCALAPPDATA") or (self.home() / "AppData" / "Local"))
            return [
                program_files / "Java",
                program_files / "Microsoft",
                program_files / "Eclipse Adoptium",
                local_app_data / "Programs" / "Eclipse Adoptium",
                local_app_data / "Programs" / "Microsoft",
            ]
        if self.platform is HostOS.MACOS:
            return [Path("/Library/Java/JavaVirtualMachines")]
        return [Path("/usr/lib/jvm"), Path("/usr/java"), Path("/opt/java")]

    def studio_jbr_home(self) -> Path | None:
        """Return the JBR home bundled with Android Studio when present."""
        for directory in self.studio_install_dirs():
            if directory.is_dir():
                return directory
        return None

    @property
    def default_sdk_root(self) -> Path:
        return self.home() / "Android" / "Sdk"

    @property
    def default_avd_home(self) -> Path:
        return self.home() / ".android" / "avd"

    # ------------------------------------------------------------------
    # Download / archive helpers (used by the SDK steps)
    # ------------------------------------------------------------------
    def download_file(self, url: str, destination: str, *, sha256: str | None = None) -> list[Command]:
        """Commands that download *url* to *destination* and verify *sha256*."""
        if self.platform is HostOS.WINDOWS:
            script = (
                "$ErrorActionPreference = 'Stop'; "
                f"Invoke-WebRequest -UseBasicParsing -Uri {_quote_powershell(url)} "
                f"-OutFile {_quote_powershell(destination)}"
            )
            commands = [
                Command(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                    description=f"download {url}",
                    timeout_s=600,
                )
            ]
            if sha256:
                verify = (
                    f"$h = (Get-FileHash -Algorithm SHA256 {_quote_powershell(destination)}).Hash; "
                    f"if ($h -ne {_quote_powershell(sha256.upper())}) {{ "
                    "Write-Error \"SHA-256 mismatch\"; exit 1 }"
                )
                commands.append(
                    Command(
                        ["powershell", "-NoProfile", "-NonInteractive", "-Command", verify],
                        description="verify download checksum",
                    )
                )
            return commands
        commands = [
            Command(
                [
                    "curl",
                    "--fail",
                    "--location",
                    "--silent",
                    "--show-error",
                    "--retry",
                    "3",
                    "--retry-delay",
                    "2",
                    "--output",
                    destination,
                    url,
                ],
                description=f"download {url}",
                timeout_s=600,
            )
        ]
        if sha256:
            commands.append(
                Command(
                    ["sh", "-c", f"printf '%s  %s\\n' {shlex.quote(sha256)} {shlex.quote(destination)} | sha256sum -c -"],
                    description="verify download checksum",
                )
            )
        return commands

    def extract_zip(self, archive: str, destination: str) -> Command:
        if self.platform is HostOS.WINDOWS:
            script = (
                f"Expand-Archive -LiteralPath {_quote_powershell(archive)} "
                f"-DestinationPath {_quote_powershell(destination)} -Force"
            )
            return Command(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                description=f"extract {archive}",
                timeout_s=600,
            )
        return Command(
            ["unzip", "-q", "-o", archive, "-d", destination],
            description=f"extract {archive}",
            timeout_s=600,
        )

    def move_path(self, source: str, destination: str) -> Command:
        if self.platform is HostOS.WINDOWS:
            script = (
                f"$dst = {_quote_powershell(destination)}; "
                f"$src = {_quote_powershell(source)}; "
                "if (Test-Path -LiteralPath $dst) { Remove-Item -LiteralPath $dst -Recurse -Force }; "
                "New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dst) | Out-Null; "
                "Move-Item -LiteralPath $src -Destination $dst"
            )
            return Command(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                description=f"move {source} to {destination}",
            )
        parent = os.path.dirname(destination) or "."
        script = (
            f"mkdir -p {shlex.quote(parent)} && rm -rf {shlex.quote(destination)} "
            f"&& mv {shlex.quote(source)} {shlex.quote(destination)}"
        )
        return Command(["sh", "-c", script], description=f"move {source} to {destination}")

    def remove_path(self, path: str) -> Command:
        if self.platform is HostOS.WINDOWS:
            script = f"if (Test-Path -LiteralPath {_quote_powershell(path)}) {{ Remove-Item -LiteralPath {_quote_powershell(path)} -Recurse -Force }}"
            return Command(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                description=f"remove {path}",
            )
        return Command(
            ["sh", "-c", f"rm -rf {shlex.quote(path)}"],
            description=f"remove {path}",
        )

    # ------------------------------------------------------------------
    # Host model
    # ------------------------------------------------------------------
    def host_info(self) -> HostInfo:
        """Minimal ``HostInfo`` derived from provider state (P1 refines it later)."""
        try:
            plan = self.virtualization_plan()
        except Exception:  # pragma: no cover - never fail plan generation
            plan = VirtualizationPlan()
        return HostInfo(
            os=self.platform,
            os_name=self.display_name,
            os_version="",
            arch=self.arch,
            machine=sys.platform,
            python_version=sys.version.split()[0],
            is_admin=self.is_admin,
            cpu_count=os.cpu_count() or 1,
            hypervisor=AcceleratorInfo(
                kind=plan.kind,
                available=plan.available,
                detail=plan.detail,
                requires_reboot=plan.requires_reboot,
                nested=plan.nested,
            ),
            acceleration_available=plan.available,
            details={
                "package_manager": self.package_manager,
                "provider": self.id,
                "dry_run_capable": True,
            },
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} id={self.id!r} platform={self.platform.value!r}>"


# ----------------------------------------------------------------------
# Shared platform probes used by more than one provider
# ----------------------------------------------------------------------
def kvm_available() -> bool:
    """Whether ``/dev/kvm`` is exposed on this host (Linux only)."""
    return Path("/dev/kvm").exists()


def kvm_group_member(group: str = "kvm") -> bool:
    """Whether the current user already belongs to the *group*."""
    if sys.platform not in {"linux", "linux2"}:
        return False
    try:
        import grp

        return any(entry.gr_name == group for entry in grp.getgrall() if entry.gr_gid in os.getgroups())
    except Exception:  # pragma: no cover - exotic/limited environments
        return False


def kvm_udev_rule_present(path: str = KVM_UDEV_RULE_PATH) -> bool:
    return Path(path).is_file()


def linux_reboot_required() -> bool:
    """Debian/Ubuntu maintain ``/var/run/reboot-required``; Fedora uses needs-restarting."""
    return Path("/var/run/reboot-required").exists()


def macos_hv_support() -> bool:
    """Query ``kern.hv_support``; only shells out on a real macOS host."""
    if sys.platform != "darwin":
        return False
    try:
        completed = subprocess.run(
            ["sysctl", "-n", "kern.hv_support"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return completed.stdout.strip() == "1"
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - macOS only
        return False


def windows_reboot_pending() -> bool:
    """Read the well-known Windows pending-reboot registry markers."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        pending_keys = (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
        )
        for key_name in pending_keys:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_name):
                    return True
            except OSError:
                continue
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager",
            ) as key:
                value, _kind = winreg.QueryValueEx(key, "PendingFileRenameOperations")
                return bool(value)
        except OSError:
            return False
    except Exception:  # pragma: no cover - registry access is best effort
        return False


def kvm_virtualization_plan(
    *,
    sudo: Sequence[str],
    kvm_available_now: bool,
    in_group: bool,
    rule_present: bool,
    nested: bool | None,
    username: str,
    family: str,
    extra_notes: Sequence[str] = (),
) -> VirtualizationPlan:
    """Build the Linux KVM plan shared by the debian/fedora/arch providers."""
    enabled = kvm_available_now and in_group and rule_present
    commands: list[Command] = []
    if not enabled:
        if not kvm_available_now:
            commands.append(
                Command(
                    ["sh", "-c", "modprobe kvm_intel 2>/dev/null || modprobe kvm_amd 2>/dev/null || true"],
                    description="load the KVM kernel module",
                    admin=True,
                    timeout_s=120,
                )
            )
        commands.extend(
            [
                Command(
                    [
                        "sh",
                        "-c",
                        f"printf '%s\\n' {shlex.quote(KVM_UDEV_RULE)} | "
                        f"tee {shlex.quote(KVM_UDEV_RULE_PATH)} >/dev/null",
                    ],
                    description=f"install KVM udev rule ({KVM_UDEV_RULE_PATH})",
                    admin=True,
                ),
                Command(
                    [*sudo, "udevadm", "control", "--reload-rules"],
                    description="reload udev rules",
                    admin=True,
                ),
                Command(
                    [*sudo, "udevadm", "trigger", "--subsystem-match=misc", "--action=change"],
                    description="apply the KVM udev rule",
                    admin=True,
                ),
                Command(
                    [*sudo, "usermod", "-aG", "kvm", username],
                    description=f"add {username} to the kvm group",
                    admin=True,
                ),
            ]
        )
    notes = [
        "KVM needs VT-x/AMD-V exposed to this OS; nested virtualisation may hide it.",
        "kvm group membership takes effect on the next login/session.",
        f"Package family: {family}",
        *extra_notes,
    ]
    return VirtualizationPlan(
        kind=AcceleratorKind.KVM,
        available=kvm_available_now,
        enabled=enabled,
        requires_reboot=False,
        commands=tuple(commands),
        nested=nested,
        notes=tuple(notes),
        udev_rule_path=KVM_UDEV_RULE_PATH,
        udev_rule_content=KVM_UDEV_RULE,
        groups=("kvm",),
    )


def _detect_arch(env: Mapping[str, str]) -> CpuArch:
    raw = (env.get("AVDFLEET_FAKE_ARCH") or "").strip().lower()
    if not raw:
        try:
            import platform as platform_module

            raw = platform_module.machine().lower()
        except Exception:  # pragma: no cover
            raw = ""
    if raw in {"x86_64", "amd64", "x64", "i386", "i686"}:
        return CpuArch.X86_64
    if raw in {"arm64", "aarch64", "arm64e"}:
        return CpuArch.ARM64
    return CpuArch.UNKNOWN


__all__ = [
    "KVM_UDEV_RULE",
    "KVM_UDEV_RULE_PATH",
    "REQUIRED_TOOL_TEMPLATES",
    "Command",
    "EnvScope",
    "InstallProvider",
    "ToolTemplate",
    "VirtualizationPlan",
    "kvm_available",
    "kvm_group_member",
    "kvm_udev_rule_present",
    "kvm_virtualization_plan",
    "linux_reboot_required",
    "macos_hv_support",
    "windows_reboot_pending",
]
