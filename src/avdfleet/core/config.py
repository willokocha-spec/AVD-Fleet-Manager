"""Configuration schema and loader.

Precedence: defaults < config.toml < AVDFLEET_* environment variables < explicit
overrides.  Configuration never stores secrets; tokens and proxy passwords live
in the token file / vault.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import GpuMode, HostOS
from .paths import Paths, user_config_dir

API_PREFIX = "/api/v1"
APP_VERSION = "0.1.0"
SUPPORTED_API_RANGE = (21, 36)
DEFAULT_MAX_INSTANCES = 64
DEFAULT_API_PORT = 8765
MIN_RAM_PER_INSTANCE_MB = 1536


class Config(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # edition / paths
    edition: str = "installed"
    portable_root: str | None = None
    data_dir: str | None = None
    config_dir: str | None = None
    cache_dir: str | None = None
    log_dir: str | None = None
    sdk_root: str | None = None
    avd_home: str | None = None

    # API
    api_host: str = "127.0.0.1"
    api_port: int = DEFAULT_API_PORT
    auth_required: bool = True
    allow_origins: list[str] = Field(
        default_factory=lambda: [
            "http://127.0.0.1:8765",
            "http://localhost:8765",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    serve_ui: bool = True
    trusted_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1", "localhost"])
    token_ttl_hours: int = 24 * 30

    # fleet capacity
    max_instances: int = DEFAULT_MAX_INSTANCES
    default_ram_mb: int = 2048
    default_cpu_cores: int = 2
    default_heap_mb: int = 256
    headless: bool = True
    gpu_mode: GpuMode = GpuMode.SWIFTSHADER_INDIRECT
    port_range_start: int = 5554
    port_range_end: int = 5680
    appium_port_start: int = 4723
    bridge_port_start: int = 7800
    boot_timeout_s: int = 300
    boot_poll_interval_s: float = 2.0
    shutdown_timeout_s: float = 30.0
    snapshot_timeout_s: int = 180
    capacity_reserve_mb: int = 2048

    # tools
    sdk_channel: str = "stable"
    jdk_version: str = "21"
    node_version: str = "22"
    appium_version: str = "4"
    uiautomator2_version: str = "latest"
    scrcpy_version: str = "latest"
    studio_required: bool = False

    # networking / downloads
    download_timeout_s: int = 300
    download_retries: int = 3
    proxy_verify_url: str = "https://example.com"
    proxy_verify_timeout_s: int = 15
    request_timeout_s: float = 30.0

    # logging
    log_level: str = "INFO"
    log_json: bool = False
    log_file: str | None = None

    # misc
    telemetry: bool = False
    update_check: bool = False
    vault_backend: str = "auto"
    oobe_matrix_path: str | None = None
    theme: str = "dark"

    @field_validator("api_host")
    @classmethod
    def _loopback_default(cls, value: str) -> str:
        if not value:
            raise ValueError("api_host must not be empty")
        return value

    @field_validator("log_level")
    @classmethod
    def _log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return upper

    @field_validator("allow_origins", "trusted_hosts", mode="before")
    @classmethod
    def _split(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("api_port")
    @classmethod
    def _port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("api_port must be 1..65535")
        return value

    @model_validator(mode="after")
    def _ranges(self) -> Config:
        if self.port_range_end <= self.port_range_start:
            raise ValueError("port_range_end must be greater than port_range_start")
        if self.port_range_start % 2:
            # emulator console ports are even; keep ranges aligned.
            self.port_range_start += 1
        if self.max_instances < 1 or self.max_instances > 64:
            raise ValueError("max_instances must be 1..64")
        if self.default_ram_mb < 512:
            raise ValueError("default_ram_mb must be >= 512")
        return self

    # ------------------------------------------------------------------
    @property
    def is_portable(self) -> bool:
        return self.edition == "portable" or bool(self.portable_root)

    def resolved_paths(self) -> Paths:
        if self.is_portable:
            root = Path(self.portable_root or Path.cwd()).expanduser()
            return Paths(
                root=root,
                portable=True,
                data_dir=self.data_dir,
                config_dir=self.config_dir,
                cache_dir=self.cache_dir,
                log_dir=self.log_dir,
                sdk_root=self.sdk_root,
                avd_home=self.avd_home,
            ).ensure()
        return Paths(
            root=Path(self.data_dir).expanduser().parent if self.data_dir else Paths.discover().root,
            portable=False,
            data_dir=self.data_dir,
            config_dir=self.config_dir,
            cache_dir=self.cache_dir,
            log_dir=self.log_dir,
            sdk_root=self.sdk_root,
            avd_home=self.avd_home,
        ).ensure()

    def public_dict(self) -> dict[str, Any]:
        """Configuration safe to return over the API (no secrets are stored here)."""
        return self.model_dump(mode="json")

    # ------------------------------------------------------------------
    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        overrides: dict[str, Any] | None = None,
        environ: dict[str, str] | None = None,
    ) -> Config:
        env = dict(os.environ if environ is None else environ)
        config_path = _find_config(path, env)
        data: dict[str, Any] = {}
        if config_path and config_path.exists():
            with open(config_path, "rb") as handle:
                data.update(tomllib.load(handle))
        data.update(_env_overrides(env))
        if overrides:
            data = _deep_merge(data, overrides)
        if "portable_root" in data and data.get("portable_root"):
            data.setdefault("edition", "portable")
        return cls.model_validate(data)

    @classmethod
    def defaults(cls) -> Config:
        return cls()


def _find_config(path: str | Path | None, env: dict[str, str]) -> Path | None:
    if path:
        candidate = Path(path).expanduser()
        if not candidate.exists():
            raise FileNotFoundError(f"config file not found: {candidate}")
        return candidate
    explicit = env.get("AVDFLEET_CONFIG_FILE")
    if explicit:
        return Path(explicit).expanduser()
    portable = env.get("AVDFLEET_PORTABLE_ROOT")
    if portable:
        candidate = Path(portable).expanduser() / "config" / "config.toml"
        return candidate if candidate.exists() else None
    for candidate in (Path.cwd() / "avdfleet.toml", user_config_dir() / "config.toml"):
        if candidate.exists():
            return candidate
    return None


_ENV_MAP = {
    "edition": "EDITION",
    "portable_root": "PORTABLE_ROOT",
    "data_dir": "DATA_DIR",
    "config_dir": "CONFIG_DIR",
    "cache_dir": "CACHE_DIR",
    "log_dir": "LOG_DIR",
    "sdk_root": "SDK_ROOT",
    "avd_home": "AVD_HOME",
    "api_host": "API_HOST",
    "api_port": "API_PORT",
    "auth_required": "AUTH_REQUIRED",
    "allow_origins": "ALLOW_ORIGINS",
    "trusted_hosts": "TRUSTED_HOSTS",
    "max_instances": "MAX_INSTANCES",
    "default_ram_mb": "DEFAULT_RAM_MB",
    "headless": "HEADLESS",
    "gpu_mode": "GPU_MODE",
    "port_range_start": "PORT_RANGE_START",
    "port_range_end": "PORT_RANGE_END",
    "appium_port_start": "APPIUM_PORT_START",
    "bridge_port_start": "BRIDGE_PORT_START",
    "boot_timeout_s": "BOOT_TIMEOUT_S",
    "log_level": "LOG_LEVEL",
    "log_json": "LOG_JSON",
    "log_file": "LOG_FILE",
    "serve_ui": "SERVE_UI",
    "telemetry": "TELEMETRY",
    "vault_backend": "VAULT_BACKEND",
    "oobe_matrix_path": "OOBE_MATRIX_PATH",
}

_BOOL_FIELDS = {"auth_required", "headless", "log_json", "serve_ui", "telemetry"}
_INT_FIELDS = {
    "api_port",
    "max_instances",
    "default_ram_mb",
    "port_range_start",
    "port_range_end",
    "appium_port_start",
    "bridge_port_start",
    "boot_timeout_s",
}
_LIST_FIELDS = {"allow_origins", "trusted_hosts"}


def _env_overrides(env: dict[str, str]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for field, suffix in _ENV_MAP.items():
        key = f"AVDFLEET_{suffix}"
        if key not in env or env[key] == "":
            continue
        raw = env[key]
        if field in _BOOL_FIELDS:
            overrides[field] = raw.strip().lower() in {"1", "true", "yes", "on"}
        elif field in _INT_FIELDS:
            try:
                overrides[field] = int(raw)
            except ValueError:
                continue
        elif field in _LIST_FIELDS:
            overrides[field] = [item.strip() for item in raw.split(",") if item.strip()]
        else:
            overrides[field] = raw
    return overrides


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def platform_display_name(os_name: HostOS) -> str:
    return {
        HostOS.WINDOWS: "Windows",
        HostOS.MACOS: "macOS",
        HostOS.LINUX: "Linux",
    }.get(os_name, "Unknown OS")
