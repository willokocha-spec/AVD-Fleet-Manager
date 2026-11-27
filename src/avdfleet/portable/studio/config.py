"""Portable Android Studio configuration.

Android Studio honours two environment variables that make a portable setup
possible without touching the user profile:

* ``STUDIO_PROPERTIES`` — path to a properties file with ``idea.config.path``,
  ``idea.system.path``, ``idea.plugins.path`` and ``idea.log.path`` overrides.
* ``STUDIO_VM_OPTIONS`` — path to a ``studio.vmoptions`` file (JVM options).

Both files are generated under ``<bundle>/studio/`` and all mutable Studio state
is redirected into ``<bundle>/data/studio/`` so the filesystem-diff check does not
see writes outside the bundle.  Some locations cannot be redirected; they are
documented as data per OS (:data:`STUDIO_LEFTOVERS`) and surfaced by the CLI/API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from avdfleet.portable.env.model import PortablePaths, host_os_name

STUDIO_PROPERTIES_FILENAME = "idea.properties"
STUDIO_VMOPTIONS_FILENAME = "studio.vmoptions"
DEFAULT_STUDIO_HEAP_MB = 2048

__all__ = [
    "DEFAULT_STUDIO_HEAP_MB",
    "STUDIO_LEFTOVERS",
    "STUDIO_PROPERTIES_FILENAME",
    "STUDIO_VMOPTIONS_FILENAME",
    "StudioConfigResult",
    "StudioLeftover",
    "StudioPaths",
    "build_studio_properties",
    "build_studio_vmoptions",
    "leftover_documentation",
    "studio_env",
    "studio_leftovers",
    "write_studio_config",
]


@dataclass(frozen=True)
class StudioPaths:
    """Bundle-local Studio paths (static files vs mutable runtime state)."""

    root: Path
    properties: Path
    vmoptions: Path
    config: Path
    system: Path
    plugins: Path
    log: Path
    caches: Path

    @classmethod
    def for_root(cls, root: str | Path) -> StudioPaths:
        base = PortablePaths.for_root(root)
        state = base.data / "studio"
        return cls(
            root=base.root,
            properties=base.studio / STUDIO_PROPERTIES_FILENAME,
            vmoptions=base.studio / STUDIO_VMOPTIONS_FILENAME,
            config=state / "config",
            system=state / "system",
            plugins=state / "plugins",
            log=state / "log",
            caches=state / "caches",
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "root": str(self.root),
            "properties": str(self.properties),
            "vmoptions": str(self.vmoptions),
            "config": str(self.config),
            "system": str(self.system),
            "plugins": str(self.plugins),
            "log": str(self.log),
            "caches": str(self.caches),
        }


def _forward_slash(path: Path) -> str:
    return path.as_posix()


def build_studio_properties(root: str | Path) -> str:
    """``idea.properties`` overrides keeping Studio state inside the bundle."""
    paths = StudioPaths.for_root(root)
    lines = [
        "# AVD Fleet portable Android Studio properties (generated)",
        "# Point STUDIO_PROPERTIES at this file (an idea.properties file) to keep Studio state inside the bundle.",
        "# Main state directories are redirected into <bundle>/data/studio so that a",
        "# filesystem diff after running the portable edition stays clean.",
        f"idea.config.path={_forward_slash(paths.config)}",
        f"idea.system.path={_forward_slash(paths.system)}",
        f"idea.plugins.path={_forward_slash(paths.plugins)}",
        f"idea.log.path={_forward_slash(paths.log)}",
        f"idea.caches.path={_forward_slash(paths.caches)}",
        "# Keep memory conservative for USB/removable volumes (vmoptions carries -Xmx).",
        "idea.cycle.buffer.size=disabled",
        "",
    ]
    return "\n".join(lines)


def build_studio_vmoptions(root: str | Path, *, heap_mb: int = DEFAULT_STUDIO_HEAP_MB) -> str:
    """``studio.vmoptions`` best effort: JVM mirror of the properties overrides."""
    paths = StudioPaths.for_root(root)
    lines = [
        "# AVD Fleet portable Android Studio VM options (generated)",
        "# Point STUDIO_VM_OPTIONS at this file; values mirror idea.properties.",
        f"-Xmx{heap_mb}m",
        "-XX:ReservedCodeCacheSize=512m",
        f'-Didea.config.path="{_forward_slash(paths.config)}"',
        f'-Didea.system.path="{_forward_slash(paths.system)}"',
        f'-Didea.plugins.path="{_forward_slash(paths.plugins)}"',
        f'-Didea.log.path="{_forward_slash(paths.log)}"',
        f'-Djava.io.tmpdir="{_forward_slash(paths.root / "tmp")}"',
        "",
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class StudioConfigResult:
    root: Path
    dry_run: bool = False
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.written)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "dry_run": self.dry_run,
            "written": list(self.written),
            "unchanged": list(self.unchanged),
            "warnings": list(self.warnings),
        }


def write_studio_config(
    root: str | Path,
    *,
    dry_run: bool = False,
    heap_mb: int = DEFAULT_STUDIO_HEAP_MB,
) -> StudioConfigResult:
    """Write ``idea.properties``/``studio.vmoptions`` (idempotent)."""
    paths = StudioPaths.for_root(root)
    payloads = {
        paths.properties: build_studio_properties(root),
        paths.vmoptions: build_studio_vmoptions(root, heap_mb=heap_mb),
    }
    written: list[str] = []
    unchanged: list[str] = []
    warnings: list[str] = []
    for path, content in payloads.items():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = None
        if existing == content:
            unchanged.append(str(path))
            continue
        if dry_run:
            written.append(str(path))
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
        except OSError as exc:
            warnings.append(f"Could not write {path}: {exc}")
            continue
        written.append(str(path))
    return StudioConfigResult(
        root=Path(root),
        dry_run=dry_run,
        written=written,
        unchanged=unchanged,
        warnings=warnings,
    )


def studio_env(root: str | Path, *, require_files: bool = False) -> dict[str, str]:
    """Environment variables that redirect Android Studio via the bundle."""
    paths = StudioPaths.for_root(root)
    if require_files and not paths.properties.is_file():
        return {}
    env = {"STUDIO_PROPERTIES": str(paths.properties)}
    if paths.vmoptions.is_file() or not require_files:
        env["STUDIO_VM_OPTIONS"] = str(paths.vmoptions)
    return env


@dataclass(frozen=True)
class StudioLeftover:
    """A documented path that portable Studio/emulator may write outside the bundle."""

    host_os: str
    path: str
    kind: str
    note: str
    unavoidable: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "host_os": self.host_os,
            "path": self.path,
            "kind": self.kind,
            "note": self.note,
            "unavoidable": self.unavoidable,
        }


#: The leftovers list required by the brief (§3.10) as data, per host.
STUDIO_LEFTOVERS: tuple[StudioLeftover, ...] = (
    StudioLeftover(
        "all",
        "~/.gradle and Gradle user home",
        "cache",
        "Gradle is used by Android build scripts invoked from Studio; the bundled env sets "
        "GRADLE_USER_HOME to <root>/cache/gradle for child processes, but Studio's own "
        "Gradle integration can still touch the default location.",
    ),
    StudioLeftover(
        "all",
        "~/.android/adbkey (and adb_keys)",
        "config",
        "adb reads/writes its authentication keys from the user profile on some "
        "platform-tools releases; set ADB_VENDOR_KEYS to a bundled key to avoid it.",
    ),
    StudioLeftover(
        "all",
        "OS temp directories used by Studio/JVM crash dumps",
        "temp",
        "JVM hs_err/hdmp crash files and some native caches follow the real OS temp dir "
        "when Studio's own launcher overrides TMPDIR.",
    ),
    StudioLeftover(
        "windows",
        "%APPDATA%\\Google\\AndroidStudio<version>",
        "config",
        "Studio falls back to the roaming profile for a few plugin/update settings that "
        "idea.properties does not cover.",
    ),
    StudioLeftover(
        "windows",
        "%LOCALAPPDATA%\\Google\\AndroidStudio<version>",
        "cache",
        "Crash logs, update metadata and plugin indexes may be written here despite "
        "STUDIO_PROPERTIES.",
    ),
    StudioLeftover(
        "windows",
        "HKCU\\Software\\Google\\Android Studio*",
        "registry",
        "Windows registry settings (file associations, recent projects, update channel) "
        "cannot be redirected without a sandbox/container; they do not contain secrets.",
    ),
    StudioLeftover(
        "windows",
        "%APPDATA%\\JetBrains and %LOCALAPPDATA%\\JetBrains",
        "cache",
        "Shared JetBrains components (marketplace, Toolbox integration) can create these "
        "directories.",
    ),
    StudioLeftover(
        "all",
        "~/.java/.userPrefs",
        "config",
        "The Java preferences API (used by sdkmanager/avdmanager/Studio) writes to the "
        "user profile on some JDKs; there is no portable override short of -Djava.util.prefs.",
    ),
    StudioLeftover(
        "windows",
        "%USERPROFILE%\\.android and %USERPROFILE%\\.gradle",
        "config",
        "Legacy Android tools fall back to the profile home; the launcher sets "
        "ANDROID_USER_HOME/ANDROID_AVD_HOME but adb/gradle may still use the profile.",
    ),
    StudioLeftover(
        "windows",
        "%USERPROFILE%\\AndroidStudioProjects",
        "config",
        "Android Studio's default project directory is the user profile; the bundle cannot "
        "redirect project locations chosen by the user.",
    ),
    StudioLeftover(
        "windows",
        "%APPDATA%\\uv and %LOCALAPPDATA%\\uv",
        "cache",
        "If uv is invoked outside the launcher environment its managed Pythons/cache land "
        "in the profile; the launcher exports UV_* to keep them inside the bundle.",
    ),
    StudioLeftover(
        "macos",
        "~/Library/Application Support/Google/AndroidStudio<version>",
        "config",
        "Studio plist/plugin update state ignores idea.properties for some components; the "
        "main config/system/plugins/log paths are redirected via STUDIO_PROPERTIES.",
    ),
    StudioLeftover(
        "macos",
        "~/Library/Caches/Google/AndroidStudio<version>",
        "cache",
        "Native caches and crash reports can land in the user cache directory.",
    ),
    StudioLeftover(
        "macos",
        "~/Library/Logs/Google/AndroidStudio<version>",
        "log",
        "Crash/update logs may be written here by the launcher before idea.log.path applies.",
    ),
    StudioLeftover(
        "macos",
        "~/Library/Preferences/com.google.android.studio*.plist",
        "config",
        "macOS preference plists (recent files, update settings) are outside "
        "idea.properties' control.",
    ),
    StudioLeftover(
        "macos",
        "~/Library/Preferences/com.apple.LaunchServices.QuarantineEventsV2",
        "database",
        "macOS records downloaded/quarantined files in a system database; "
        "xattr -dr removes the attributes but not the historical entries.",
    ),
    StudioLeftover(
        "linux",
        "~/.config/Google/AndroidStudio<version>",
        "config",
        "Studio user config fallback when STUDIO_PROPERTIES is not honoured by a plugin.",
    ),
    StudioLeftover(
        "linux",
        "~/.cache/Google/AndroidStudio<version>",
        "cache",
        "Native caches/crash reports may use the XDG cache directory.",
    ),
    StudioLeftover(
        "linux",
        "~/.local/share/Google/AndroidStudio<version>",
        "cache",
        "Plugin download/update metadata can fall back to the XDG data directory.",
    ),
    StudioLeftover(
        "macos",
        "~/.cache/uv and ~/.local/share/uv",
        "cache",
        "uv's XDG defaults on macOS; the launcher exports UV_CACHE_DIR/UV_PYTHON_INSTALL_DIR "
        "so uv-invoked code stays inside the bundle.",
    ),
    StudioLeftover(
        "linux",
        "~/.local/bin (uv/python shims)",
        "config",
        "uv installs executable shims into the XDG bin directory by default; the launcher "
        "points UV_TOOL_BIN_DIR inside the bundle instead.",
    ),
    StudioLeftover(
        "linux",
        "~/.local/share/recently-used.xbel",
        "database",
        "GTK recent-files list is written when Studio opens project files through the "
        "GTK file chooser.",
    ),
    StudioLeftover(
        "linux",
        "$XDG_RUNTIME_DIR",
        "temp",
        "Display sockets and single-instance locks are created in the runtime directory.",
    ),
)


def studio_leftovers(host_os: str | None = None) -> list[StudioLeftover]:
    """Documented Studio leftovers for *host_os* (all hosts when None)."""
    if host_os is None:
        return list(STUDIO_LEFTOVERS)
    host = host_os if host_os in {"windows", "macos", "linux"} else "linux"
    return [item for item in STUDIO_LEFTOVERS if item.host_os in {"all", host}]


def leftover_documentation(host_os: str | None = None) -> list[dict[str, Any]]:
    """JSON-friendly leftovers list (API ``PortableStatus.leftovers``)."""
    return [item.as_dict() for item in studio_leftovers(host_os if host_os is not None else host_os_name())]
