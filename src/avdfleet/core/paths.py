"""Filesystem layout provider.

Installed edition uses platform-native application directories (and honours
XDG/AVDFLEET overrides).  Portable edition keeps everything under one root so a
filesystem diff after running can be asserted clean.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

APP_NAME = "avdfleet"
PORTABLE_MARKER = "avdfleet-portable.json"


def _as_path(value: Path | str | None, name: str) -> Path:
    if value is None:
        raise RuntimeError(f"paths.{name} was not initialised")
    return cast(Path, value)


def _env_path(*names: str) -> Path | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return Path(value).expanduser()
    return None


def user_data_dir(app: str = APP_NAME) -> Path:
    override = _env_path("AVDFLEET_DATA_DIR")
    if override:
        return override
    if sys.platform == "win32":
        base = _env_path("LOCALAPPDATA", "APPDATA") or Path.home() / "AppData" / "Local"
        return base / app
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app
    base = _env_path("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return base / app


def user_config_dir(app: str = APP_NAME) -> Path:
    override = _env_path("AVDFLEET_CONFIG_DIR")
    if override:
        return override
    if sys.platform == "win32":
        base = _env_path("APPDATA") or Path.home() / "AppData" / "Roaming"
        return base / app
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app
    base = _env_path("XDG_CONFIG_HOME") or Path.home() / ".config"
    return base / app


def user_cache_dir(app: str = APP_NAME) -> Path:
    override = _env_path("AVDFLEET_CACHE_DIR")
    if override:
        return override
    if sys.platform == "win32":
        base = _env_path("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return base / app / "Cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / app
    base = _env_path("XDG_CACHE_HOME") or Path.home() / ".cache"
    return base / app


def user_log_dir(app: str = APP_NAME) -> Path:
    override = _env_path("AVDFLEET_LOG_DIR")
    if override:
        return override
    if sys.platform == "win32":
        base = _env_path("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return base / app / "Logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / app
    base = _env_path("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return base / app / "logs"


@dataclass
class Paths:
    root: Path | str
    portable: bool = False
    data_dir: Path | str | None = None
    config_dir: Path | str | None = None
    cache_dir: Path | str | None = None
    log_dir: Path | str | None = None
    sdk_root: Path | str | None = None
    avd_home: Path | str | None = None

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()

        def coerce(value: Path | str | None) -> Path | None:
            return Path(value).expanduser() if value is not None else None

        self.data_dir = coerce(self.data_dir)
        self.config_dir = coerce(self.config_dir)
        self.cache_dir = coerce(self.cache_dir)
        self.log_dir = coerce(self.log_dir)
        self.sdk_root = coerce(self.sdk_root)
        self.avd_home = coerce(self.avd_home)
        if self.portable:
            self.data_dir = Path(self.data_dir or self.root / "data")
            self.config_dir = Path(self.config_dir or self.root / "config")
            self.cache_dir = Path(self.cache_dir or self.root / "cache")
            self.log_dir = Path(self.log_dir or self.root / "logs")
            self.sdk_root = Path(self.sdk_root or self.root / "sdk")
            self.avd_home = Path(self.avd_home or self.root / "avd")
        else:
            self.data_dir = Path(self.data_dir or user_data_dir())
            self.config_dir = Path(self.config_dir or user_config_dir())
            self.cache_dir = Path(self.cache_dir or user_cache_dir())
            self.log_dir = Path(self.log_dir or user_log_dir())
            self.sdk_root = Path(
                self.sdk_root or _env_path("ANDROID_SDK_ROOT", "ANDROID_HOME") or self.data_dir / "sdk"
            )
            self.avd_home = Path(self.avd_home or _env_path("ANDROID_AVD_HOME") or self.data_dir / "avd")

    # -- core paths ---------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return _as_path(self.data_dir, "data_dir") / "avdfleet.db"

    @property
    def token_path(self) -> Path:
        return _as_path(self.data_dir, "data_dir") / "token"

    @property
    def vault_path(self) -> Path:
        return _as_path(self.data_dir, "data_dir") / "secrets.vault"

    @property
    def key_path(self) -> Path:
        return _as_path(self.data_dir, "data_dir") / "secrets.key"

    @property
    def manifest_path(self) -> Path:
        return _as_path(self.data_dir, "data_dir") / "install-manifest.json"

    @property
    def config_path(self) -> Path:
        return _as_path(self.config_dir, "config_dir") / "config.toml"

    @property
    def oobe_matrix_path(self) -> Path:
        return _as_path(self.data_dir, "data_dir") / "oobe_matrix.json"

    @property
    def screenshots_dir(self) -> Path:
        return _as_path(self.data_dir, "data_dir") / "screenshots"

    @property
    def snapshots_dir(self) -> Path:
        return _as_path(self.data_dir, "data_dir") / "snapshots"

    @property
    def artifacts_dir(self) -> Path:
        return _as_path(self.data_dir, "data_dir") / "artifacts"

    @property
    def logs_dir(self) -> Path:
        return _as_path(self.log_dir, "log_dir")

    @property
    def downloads_dir(self) -> Path:
        return _as_path(self.cache_dir, "cache_dir") / "downloads"

    @property
    def ui_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / "api" / "static"

    @property
    def tools_dir(self) -> Path:
        return _as_path(self.data_dir, "data_dir") / "tools"

    @property
    def bin_dir(self) -> Path:
        if self.portable:
            return Path(self.root) / "bin"
        return _as_path(self.data_dir, "data_dir") / "bin"

    @property
    def portable_marker(self) -> Path:
        return Path(self.root) / PORTABLE_MARKER

    def ensure(self) -> Paths:
        for folder in (
            _as_path(self.data_dir, "data_dir"),
            _as_path(self.config_dir, "config_dir"),
            _as_path(self.cache_dir, "cache_dir"),
            _as_path(self.log_dir, "log_dir"),
            self.screenshots_dir,
            self.snapshots_dir,
            self.artifacts_dir,
            _as_path(self.sdk_root, "sdk_root"),
            _as_path(self.avd_home, "avd_home"),
        ):
            folder.mkdir(parents=True, exist_ok=True)
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "portable": self.portable,
            "data_dir": str(self.data_dir),
            "config_dir": str(self.config_dir),
            "cache_dir": str(self.cache_dir),
            "log_dir": str(self.log_dir),
            "sdk_root": str(self.sdk_root),
            "avd_home": str(self.avd_home),
        }

    # -- constructors -------------------------------------------------------
    @classmethod
    def for_portable(cls, root: str | Path) -> Paths:
        root_path = Path(root).expanduser().resolve()
        return cls(root=root_path, portable=True).ensure()

    @classmethod
    def discover(cls) -> Paths:
        portable_root = _env_path("AVDFLEET_PORTABLE_ROOT")
        if portable_root:
            return cls.for_portable(portable_root)
        # A portable marker next to the executable wins when not installed as a tool.
        candidate = Path(os.environ.get("AVDFLEET_ROOT", "")) if os.environ.get("AVDFLEET_ROOT") else None
        if candidate and (candidate / PORTABLE_MARKER).exists():
            return cls.for_portable(candidate)
        return cls(root=user_data_dir().parent).ensure()
