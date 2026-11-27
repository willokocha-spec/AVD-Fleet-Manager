"""Data model for the portable edition.

Everything here is deliberately stdlib + pydantic only: the launchers, bundle
builder, preflight checks and relocation code all import it, and the launcher
must stay importable with a bare bundle on a machine that has no daemon running.

The portable edition keeps *all* mutable state under one bundle root::

    <root>/avdfleet-portable.json   bundle manifest / root marker
    <root>/bin                      bundled helper executables (PATH head)
    <root>/python                   pinned Python runtime
    <root>/lib                      bundled plain-Python libraries
    <root>/venv                     pre-built virtual environment (optional)
    <root>/sdk                      pinned Android command line tools
    <root>/node / <root>/jdk        optional Node/JDK runtimes
    <root>/ui                       built web UI
    <root>/data config cache logs tmp avd android studio
    <root>/launchers                generated OS launchers

``env/builder.py`` turns this layout into the environment dictionary that every
child process (daemon, emulator, adb, appium, Studio) runs with.
"""

from __future__ import annotations

import platform as _platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from avdfleet.core.paths import PORTABLE_MARKER

#: File written at the bundle root by the bundle builder and read on every launch.
MARKER_NAME = PORTABLE_MARKER
#: Manifest schema version (contracts/bundle.schema.json).
MANIFEST_FORMAT_VERSION = "1"
#: Name used for the root state file that supports root-change detection.
STATE_FILE_NAME = "portable-state.json"


def host_os_name(platform_name: str | None = None) -> str:
    """Map ``sys.platform`` to the bundle vocabulary (windows/macos/linux/unknown)."""
    value = sys.platform if platform_name is None else platform_name
    if value.startswith("win"):
        return "windows"
    if value == "darwin":
        return "macos"
    if value.startswith("linux"):
        return "linux"
    return "unknown"


def host_arch_name(machine: str | None = None) -> str:
    """Map a machine string to the bundle vocabulary (x86_64/arm64/unknown)."""
    value = (_platform.machine() if machine is None else machine).strip().lower()
    if value in {"amd64", "x86_64", "x64", "i386", "i686", "x86"}:
        return "x86_64"
    if value in {"arm64", "aarch64", "arm64e"}:
        return "arm64"
    return "unknown"


@dataclass(frozen=True)
class PortablePaths:
    """Every well-known location inside a portable bundle root."""

    root: Path
    bin: Path
    python: Path
    lib: Path
    venv: Path
    sdk: Path
    node: Path
    jdk: Path
    ui: Path
    launchers: Path
    data: Path
    config: Path
    cache: Path
    logs: Path
    tmp: Path
    avd: Path
    android: Path
    studio: Path

    @classmethod
    def for_root(cls, root: str | Path) -> PortablePaths:
        base = Path(root).expanduser().resolve()
        return cls(
            root=base,
            bin=base / "bin",
            python=base / "python",
            lib=base / "lib",
            venv=base / "venv",
            sdk=base / "sdk",
            node=base / "node",
            jdk=base / "jdk",
            ui=base / "ui",
            launchers=base / "launchers",
            data=base / "data",
            config=base / "config",
            cache=base / "cache",
            logs=base / "logs",
            tmp=base / "tmp",
            avd=base / "avd",
            android=base / "android",
            studio=base / "studio",
        )

    # -- derived locations --------------------------------------------------
    @property
    def marker(self) -> Path:
        return self.root / MARKER_NAME

    @property
    def state_file(self) -> Path:
        return self.config / STATE_FILE_NAME

    @property
    def sdk_cmdline_tools(self) -> Path:
        return self.sdk / "cmdline-tools" / "latest" / "bin"

    @property
    def sdk_platform_tools(self) -> Path:
        return self.sdk / "platform-tools"

    @property
    def sdk_emulator(self) -> Path:
        return self.sdk / "emulator"

    @property
    def sdk_build_tools(self) -> Path:
        return self.sdk / "build-tools"

    @property
    def downloads(self) -> Path:
        return self.cache / "downloads"

    @property
    def studio_properties(self) -> Path:
        return self.studio / "idea.properties"

    @property
    def studio_vmoptions(self) -> Path:
        return self.studio / "studio.vmoptions"

    # -- serialization ------------------------------------------------------
    def core_paths(self) -> dict[str, Path]:
        """The directories tracked for root-change detection and config generation."""
        return {
            "data": self.data,
            "config": self.config,
            "cache": self.cache,
            "logs": self.logs,
            "sdk": self.sdk,
            "avd": self.avd,
        }

    def as_dict(self) -> dict[str, str]:
        return {
            "root": str(self.root),
            "data_dir": str(self.data),
            "config_dir": str(self.config),
            "cache_dir": str(self.cache),
            "log_dir": str(self.logs),
            "sdk_root": str(self.sdk),
            "avd_home": str(self.avd),
            "android_home": str(self.android),
            "tmp_dir": str(self.tmp),
            "ui_dir": str(self.ui),
            "studio_dir": str(self.studio),
        }

    def executable_dirs(self, host_os: str, *, include_missing: bool = False) -> list[Path]:
        """Directories to prepend to ``PATH`` for a bundled toolchain.

        Deterministic order: virtualenv, helper bin, Python, SDK, Node, JDK.
        Missing directories are skipped unless ``include_missing`` is set (the
        helper ``bin`` directory is always present conceptually).
        """
        windows = host_os == "windows"
        candidates: list[Path] = []
        if windows:
            candidates.extend(
                [
                    self.venv / "Scripts",
                    self.bin,
                    self.python,
                    self.python / "Scripts",
                    self.sdk_cmdline_tools,
                    self.sdk_platform_tools,
                    self.sdk_emulator,
                    self.sdk_build_tools,
                    self.sdk / "tools" / "bin",
                    self.node,
                    self.jdk / "bin",
                ]
            )
        else:
            candidates.extend(
                [
                    self.venv / "bin",
                    self.bin,
                    self.python / "bin",
                    self.sdk_cmdline_tools,
                    self.sdk_platform_tools,
                    self.sdk_emulator,
                    self.sdk_build_tools,
                    self.sdk / "tools" / "bin",
                    self.node / "bin",
                    self.jdk / "bin",
                    self.jdk / "Contents" / "Home" / "bin",
                ]
            )
        if include_missing:
            return candidates
        return [path for path in candidates if path.is_dir()]


class RootMarker(BaseModel):
    """The subset of ``avdfleet-portable.json`` needed to validate a bundle root.

    The full manifest (with per-file checksums) lives in ``bundle/manifest.py``;
    the marker is read on every launch, so it is intentionally a light model with
    unknown keys ignored for forward compatibility.
    """

    model_config = ConfigDict(extra="ignore")

    format_version: str = ""
    name: str = ""
    version: str = ""
    created_at: str = ""
    host_os: str = "unknown"
    host_arch: str = "unknown"
    root: str = ""
    offline: bool = False
    python_version: str = ""
    total_bytes: int = 0
    warnings: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


@dataclass(frozen=True)
class RootValidation:
    """Result of validating that a directory really is a portable bundle root."""

    root: Path
    marker: RootMarker
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "marker": self.marker.to_dict(),
            "warnings": list(self.warnings),
            "valid": True,
        }


class PortableState(BaseModel):
    """Last successfully launched root/paths, used to detect relocation."""

    model_config = ConfigDict(extra="ignore")

    format_version: str = "1"
    recorded_at: str = ""
    root: str = ""
    host_os: str = "unknown"
    host_arch: str = "unknown"
    paths: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class RootChange:
    """Difference between the recorded bundle root/paths and the current ones."""

    current_root: Path
    recorded_root: str | None
    state_file: Path
    relocated: bool
    changed_paths: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def old_roots(self) -> list[str]:
        """Every stale absolute root that relocation should rewrite."""
        roots: list[str] = []
        if self.recorded_root and self.relocated:
            roots.append(self.recorded_root)
        return roots

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_root": str(self.current_root),
            "recorded_root": self.recorded_root,
            "state_file": str(self.state_file),
            "relocated": self.relocated,
            "changed_paths": dict(self.changed_paths),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class PortableEnvironment:
    """Bundle root + the exact environment every portable child process uses."""

    root: Path
    marker: RootMarker
    paths: PortablePaths
    env: dict[str, str]
    host_os: str
    host_arch: str
    python_version: str = ""
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # -- convenience accessors ---------------------------------------------
    @property
    def data_dir(self) -> Path:
        return self.paths.data

    @property
    def config_dir(self) -> Path:
        return self.paths.config

    @property
    def cache_dir(self) -> Path:
        return self.paths.cache

    @property
    def log_dir(self) -> Path:
        return self.paths.logs

    @property
    def sdk_root(self) -> Path:
        return self.paths.sdk

    @property
    def avd_home(self) -> Path:
        return self.paths.avd

    @property
    def android_home(self) -> Path:
        return self.paths.android

    @property
    def marker_path(self) -> Path:
        return self.paths.marker

    def to_dict(self, *, include_env: bool = True) -> dict[str, Any]:
        """API-friendly payload, matching the fields used by ``PortableStatus``."""
        payload: dict[str, Any] = {
            "edition": "portable",
            "root": str(self.root),
            "portable": True,
            "relocatable": True,
            "host_os": self.host_os,
            "host_arch": self.host_arch,
            "python_version": self.python_version,
            "marker": self.marker.to_dict(),
            "paths": self.paths.as_dict(),
            "warnings": list(self.warnings),
            "notes": list(self.notes),
        }
        if include_env:
            payload["env"] = dict(self.env)
        return payload


# ---------------------------------------------------------------------------
# Isolation documentation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IsolationNote:
    """A location the portable edition cannot redirect into the bundle root."""

    host_os: str
    target: str
    kind: str
    reason: str
    mitigation: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "host_os": self.host_os,
            "target": self.target,
            "kind": self.kind,
            "reason": self.reason,
            "mitigation": self.mitigation,
        }


#: Locations that stay outside the bundle even with every supported override.
#: ``host_os == "all"`` entries apply to every host.  This is documentation as
#: data: the API surfaces it verbatim and the filesystem-diff test uses it to
#: explain undocumented writes.
NON_ISOLATABLE: tuple[IsolationNote, ...] = (
    IsolationNote(
        "all",
        "system temp directories (TMP/TEMP/TMPDIR)",
        "write",
        "Third-party tools (adb, emulator, appium, driver installers) create short-lived "
        "files in the OS temp directory; the launcher redirects TMP/TEMP/TMPDIR into "
        "the bundle for child processes, but system-level tools (AV scans, graphics "
        "drivers, OS update services) still use the real temp dir.",
        "Bundle launcher exports TMP/TEMP/TMPDIR into <root>/tmp; undocumented writes "
        "there are only possible from code that ignores the environment.",
    ),
    IsolationNote(
        "all",
        "user home directory (~ / %USERPROFILE%)",
        "read",
        "Python, Java, OpenSSL and system libraries read home-relative config "
        "(certificates, rc files). Overriding HOME/USERPROFILE breaks OS tooling, so it "
        "is intentionally left alone; adb keys may be read from ~/.android.",
        "ANDROID_USER_HOME/ANDROID_AVD_HOME are redirected; pass ADB_VENDOR_KEYS for "
        "adb auth if writing a bundled adbkey is not possible.",
    ),
    IsolationNote(
        "all",
        "network and OS-level caches, crash reports and telemetry",
        "write",
        "The emulator/Studio/JDK may write crash dumps, shader caches and telemetry "
        "outside the bundle; these are not configurable through environment variables "
        "on every release.",
        "Documented leftovers; disable Studio telemetry and update checking in config.",
    ),
    IsolationNote(
        "windows",
        "HKCU\\Software\\Google\\Android Studio* and HKCU\\Software\\JavaSoft",
        "write",
        "Android Studio and the JRE store some settings in the Windows registry; there "
        "is no portable-mode registry redirection without a sandbox/container.",
        "STUDIO_PROPERTIES redirects config/cache/system paths; registry settings may "
        "still be created (documented leftover).",
    ),
    IsolationNote(
        "windows",
        "%APPDATA%\\Google\\AndroidStudio* and %LOCALAPPDATA%\\Google\\AndroidStudio*",
        "write",
        "Studio falls back to the user profile for a few caches (plugin index, "
        "crash logs, update metadata) not covered by idea.properties.",
        "Main config/system/plugins/log paths are redirected into the bundle via "
        "STUDIO_PROPERTIES and STUDIO_VM_OPTIONS (best effort).",
    ),
    IsolationNote(
        "windows",
        "NTFS alternate data streams (Zone.Identifier / Mark-of-the-Web)",
        "write",
        "Extracting a downloaded zip attaches a Zone.Identifier ADS to every file; the "
        "launcher detects it once and removes it with Unblock-File, but the ADS metadata "
        "lives on the volume beside the bundle, not inside a file.",
        "First-run Unblock-File in the .cmd launcher; no bundle file content is changed.",
    ),
    IsolationNote(
        "windows",
        "%USERPROFILE%\\.android, %USERPROFILE%\\.gradle, %APPDATA%\\npm-cache",
        "write",
        "Legacy tools (adb keys, gradle, npm) default to the user profile when their "
        "own environment variables are not set by a bundled tool.",
        "Launcher sets ANDROID_USER_HOME/ANDROID_AVD_HOME; Java/Node tools run from the "
        "bundle receive redirected caches where supported.",
    ),
    IsolationNote(
        "macos",
        "~/Library/{Application Support,Caches,Logs,Preferences}/Google/AndroidStudio*",
        "write",
        "Some Studio caches, plist preferences and crash reports ignore idea.properties.",
        "STUDIO_PROPERTIES/STUDIO_VM_OPTIONS redirect the main dirs; macOS plists are a "
        "documented leftover.",
    ),
    IsolationNote(
        "macos",
        "~/Library/Preferences/com.apple.LaunchServices.QuarantineEventsV2",
        "write",
        "macOS records quarantined downloads in a system database; removing the "
        "com.apple.quarantine xattr cannot rewrite historical entries.",
        "Documented leftover. Launcher checks xattrs and prints the xattr -dr command.",
    ),
    IsolationNote(
        "macos",
        "~/.android and Java user preferences (~/Library/Preferences/*.plist)",
        "write",
        "adb keys and the JDK preferences API fall back to the user profile.",
        "ANDROID_USER_HOME is redirected; Java prefs are a documented leftover.",
    ),
    IsolationNote(
        "linux",
        "~/.config/Google/AndroidStudio* and ~/.cache/Google/AndroidStudio*",
        "write",
        "Studio's user config/cache is normally redirected by idea.properties, but "
        "plugin installers and crash reporting can fall back to XDG dirs.",
        "STUDIO_PROPERTIES redirects the main dirs; leftover paths are documented.",
    ),
    IsolationNote(
        "linux",
        "~/.android, ~/.java/.userPrefs, $XDG_RUNTIME_DIR",
        "write",
        "adb keys, Java preferences and runtime sockets live outside the bundle.",
        "ANDROID_USER_HOME/ANDROID_AVD_HOME are redirected; documented leftovers.",
    ),
)


def isolation_notes(host_os: str | None = None) -> list[IsolationNote]:
    """Return the documented non-isolatable locations for *host_os* (all if None)."""
    if host_os is None:
        return list(NON_ISOLATABLE)
    wanted = host_os if host_os in {"windows", "macos", "linux"} else None
    return [note for note in NON_ISOLATABLE if note.host_os in {"all", wanted}]


def isolation_summary(host_os: str | None = None) -> list[str]:
    """Human-readable one-liners for the notes, suitable for launcher output/docs."""
    return [f"{note.target}: {note.reason}" for note in isolation_notes(host_os)]


__all__ = [
    "MANIFEST_FORMAT_VERSION",
    "MARKER_NAME",
    "NON_ISOLATABLE",
    "STATE_FILE_NAME",
    "IsolationNote",
    "PortableEnvironment",
    "PortablePaths",
    "PortableState",
    "RootChange",
    "RootMarker",
    "RootValidation",
    "host_arch_name",
    "host_os_name",
    "isolation_notes",
    "isolation_summary",
]
