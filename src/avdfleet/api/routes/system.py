"""System routes: liveness, auth check, host/runtime info, configuration, doctor."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from avdfleet.core.config import API_PREFIX, APP_VERSION, Config
from avdfleet.core.errors import ValidationError
from avdfleet.core.models import DoctorReport, HostInfo, OperationResult
from avdfleet.core.paths import Paths

from ..container import ContainerAdapter, ServiceUnavailable
from ..deps import ConfigDep, ContainerDep, PathsDep, require_auth
from ..errors import ErrorResponse

logger = logging.getLogger("avdfleet.api.routes.system")

_WINDOWS_RELEASE_PREFIXES = ("microsoft", "wsl")


# ---------------------------------------------------------------------------
# Response models (mirror contracts/openapi.yaml schemas)
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """``#/components/schemas/HealthResponse``."""

    model_config = ConfigDict(extra="ignore")

    status: Literal["ok", "degraded"] = "ok"
    version: str = APP_VERSION
    uptime_s: float | None = None
    emulator_available: bool | None = None
    database: str | None = None


class AuthCheck(BaseModel):
    """``#/components/schemas/AuthCheck``."""

    ok: bool = True
    expires_at: str | None = None


class AcceleratorInfoView(BaseModel):
    kind: str = "none"
    available: bool = False
    detail: str = ""
    requires_reboot: bool = False
    nested: bool | None = None


class HostInfoView(BaseModel):
    """``#/components/schemas/HostInfo``."""

    model_config = ConfigDict(extra="ignore")

    os: str = "unknown"
    os_name: str = ""
    os_version: str = ""
    arch: str = "unknown"
    machine: str = ""
    python_version: str = ""
    is_admin: bool = False
    is_wsl: bool = False
    is_container: bool = False
    cpu_count: int = 1
    total_ram_gb: float = 0.0
    free_disk_gb: float = 0.0
    hypervisor: AcceleratorInfoView = Field(default_factory=AcceleratorInfoView)
    acceleration_available: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class SystemInfoView(HostInfoView):
    """``#/components/schemas/SystemInfo`` (HostInfo + daemon fields)."""

    version: str = APP_VERSION
    edition: str = "installed"
    uptime_s: float = 0.0
    sdk_root: str = ""
    data_dir: str = ""
    accelerator_ok: bool = False


class ConfigView(BaseModel):
    """``#/components/schemas/ConfigView`` (never contains secrets)."""

    model_config = ConfigDict(extra="ignore")

    edition: str = "installed"
    api_host: str = "127.0.0.1"
    api_port: int = 8765
    max_instances: int = 64
    default_ram_mb: int = 2048
    headless: bool = True
    gpu_mode: str = "swiftshader_indirect"
    sdk_root: str = ""
    data_dir: str = ""
    auth_required: bool = True
    version: str = APP_VERSION
    theme: str = "dark"


class ConfigPatch(BaseModel):
    """``#/components/schemas/ConfigPatch`` (whitelisted, extra keys rejected)."""

    model_config = ConfigDict(extra="forbid")

    headless: bool | None = None
    gpu_mode: str | None = None
    max_instances: int | None = Field(default=None, ge=1, le=64)
    default_ram_mb: int | None = Field(default=None, ge=512)
    theme: str | None = None
    log_level: str | None = None


class DoctorFixRequest(BaseModel):
    check_id: str


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

#: Unauthenticated liveness probe (contract: ``security: []``).
health_router = APIRouter(tags=["system"])

#: Authenticated system surface.
router = APIRouter(
    prefix=API_PREFIX,
    tags=["system"],
    dependencies=[Depends(require_auth)],
    responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uptime(request: Request) -> float:
    started = getattr(request.app.state, "start_monotonic", None)
    if not isinstance(started, (int, float)):
        return 0.0
    return max(0.0, time.monotonic() - float(started))


def _emulator_available(container: ContainerAdapter) -> bool | None:
    """Best-effort emulator availability without importing platform internals.

    Order: an explicit probe (``host().emulator_available()``), then the
    container's host snapshot/emulator path when it exposes them, then the mere
    presence of a runtime service as a coarse fallback.
    """
    probe = container.probe("host", "emulator_available")
    if probe is not None:
        try:
            return bool(probe())
        except Exception:  # pragma: no cover - host probe failure
            logger.debug("host emulator probe failed", exc_info=True)
            return None
    raw = container.raw
    host_info = getattr(raw, "host_info", None)
    if host_info is not None:
        available = bool(getattr(host_info, "acceleration_available", False))
        emulator = getattr(raw, "emulator_path", None)
        return available and bool(emulator)
    for attr in ("runtime", "host"):
        try:
            if getattr(raw, attr, None) is not None:
                return True
        except Exception:  # pragma: no cover - defensive
            continue
    return None


def _database_state(container: ContainerAdapter) -> str | None:
    for attr in ("repository", "store"):
        try:
            if getattr(container.raw, attr, None) is not None:
                return "ok"
        except Exception:  # pragma: no cover - defensive
            continue
    return "not_configured"


def _coerce_host(value: Any) -> HostInfoView | None:
    if value is None:
        return None
    if isinstance(value, HostInfoView):
        return value
    try:
        if isinstance(value, HostInfo):
            return HostInfoView.model_validate(value.model_dump(mode="json"))
        if isinstance(value, BaseModel):
            return HostInfoView.model_validate(value.model_dump(mode="json"))
        if isinstance(value, dict):
            return HostInfoView.model_validate(value)
    except PydanticValidationError:
        logger.debug("host info could not be coerced", exc_info=True)
    return None


def _host_fallback(paths: Paths) -> HostInfoView:
    """Conservative host snapshot used when no host provider is wired.

    This intentionally does not probe hypervisors (that belongs to
    ``platform/detect``); it reports the boring facts the API contract needs so
    the GUI can render something useful in a partially wired daemon.
    """
    system = platform.system().lower()
    os_kind = {"windows": "windows", "darwin": "macos", "linux": "linux"}.get(system, "unknown")
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64", "x64", "i386", "i686"}:
        arch = "x86_64"
    elif machine in {"arm64", "aarch64"}:
        arch = "arm64"
    else:
        arch = "unknown"

    total_ram_gb = 0.0
    try:  # psutil is an optional convenience; never required for the fallback
        import psutil  # noqa: PLC0415 - optional dependency at call time

        total_ram_gb = round(psutil.virtual_memory().total / (1024**3), 2)
    except Exception:  # pragma: no cover - psutil unavailable/blocked
        total_ram_gb = 0.0

    free_disk_gb = 0.0
    try:
        free_disk_gb = round(shutil.disk_usage(str(paths.data_dir)).free / (1024**3), 2)
    except (OSError, TypeError):  # pragma: no cover - unusual filesystem
        free_disk_gb = 0.0

    is_admin = False
    if os.name == "nt":
        try:  # pragma: no cover - platform specific
            import ctypes  # noqa: PLC0415

            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:
            is_admin = False
    else:
        try:
            geteuid = getattr(os, "geteuid", None)
            is_admin = bool(geteuid is not None and geteuid() == 0)
        except OSError:  # pragma: no cover - permission probe failure
            is_admin = False

    release = platform.release().lower()
    is_wsl = any(prefix in release for prefix in _WINDOWS_RELEASE_PREFIXES) or bool(
        os.environ.get("WSL_DISTRO_NAME")
    )
    is_container = False
    try:
        is_container = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
    except OSError:  # pragma: no cover - filesystem failure
        is_container = False

    return HostInfoView(
        os=os_kind,
        os_name=platform.system() or "",
        os_version=platform.version() or "",
        arch=arch,
        machine=platform.machine() or "",
        python_version=platform.python_version(),
        is_admin=is_admin,
        is_wsl=is_wsl,
        is_container=is_container,
        cpu_count=os.cpu_count() or 1,
        total_ram_gb=total_ram_gb,
        free_disk_gb=free_disk_gb,
        hypervisor=AcceleratorInfoView(
            kind="none",
            available=False,
            detail="host detection service is not wired; hypervisor support was not probed",
        ),
        acceleration_available=False,
        details={"fallback": True, "platform_release": platform.release()},
    )


def collect_host(container: ContainerAdapter, paths: Paths) -> HostInfoView:
    """Return host information from the host provider, with a safe fallback."""
    target = container.raw_service("host")
    if target is not None:
        detect = getattr(target, "detect", None)
        if callable(detect):
            try:
                coerced = _coerce_host(detect())
            except Exception:  # pragma: no cover - host provider failure
                logger.warning("host provider failed; using fallback host info", exc_info=True)
                coerced = None
            if coerced is not None:
                return coerced
    return _host_fallback(paths)


def config_view(config: Config, paths: Paths) -> ConfigView:
    """Build the public configuration view (no secrets live in Config)."""
    gpu_mode = getattr(config, "gpu_mode", "swiftshader_indirect")
    return ConfigView(
        edition=str(getattr(config, "edition", "installed")),
        api_host=str(getattr(config, "api_host", "127.0.0.1")),
        api_port=int(getattr(config, "api_port", 8765)),
        max_instances=int(getattr(config, "max_instances", 64)),
        default_ram_mb=int(getattr(config, "default_ram_mb", 2048)),
        headless=bool(getattr(config, "headless", True)),
        gpu_mode=str(getattr(gpu_mode, "value", gpu_mode)),
        sdk_root=str(getattr(config, "sdk_root", None) or paths.sdk_root),
        data_dir=str(paths.data_dir),
        auth_required=bool(getattr(config, "auth_required", True)),
        version=APP_VERSION,
        theme=str(getattr(config, "theme", "dark")),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@health_router.get(
    "/health",
    operation_id="getHealth",
    summary="Liveness probe (unauthenticated)",
    response_model=HealthResponse,
    response_model_exclude_none=True,
)
def get_health(
    request: Request,
    container: ContainerDep,
    config: ConfigDep,
) -> HealthResponse:
    """Unauthenticated liveness probe used by launchers and the UI."""
    return HealthResponse(
        status="ok",
        version=APP_VERSION,
        uptime_s=round(_uptime(request), 3),
        emulator_available=_emulator_available(container),
        database=_database_state(container),
    )


@router.get(
    "/auth/check",
    operation_id="checkAuth",
    summary="Validate the bearer token",
    response_model=AuthCheck,
)
def check_auth() -> AuthCheck:
    """Confirm the presented token (enforced by the router dependency)."""
    return AuthCheck(ok=True, expires_at=None)


@router.get(
    "/system",
    operation_id="getSystem",
    summary="Host and runtime information",
    response_model=SystemInfoView,
)
def get_system(
    request: Request,
    container: ContainerDep,
    config: ConfigDep,
    paths: PathsDep,
) -> SystemInfoView:
    host = collect_host(container, paths)
    return SystemInfoView(
        **host.model_dump(),
        version=APP_VERSION,
        edition=str(getattr(config, "edition", "installed")),
        uptime_s=round(_uptime(request), 3),
        sdk_root=str(paths.sdk_root),
        data_dir=str(paths.data_dir),
        accelerator_ok=host.acceleration_available,
    )


@router.get(
    "/config",
    operation_id="getConfig",
    summary="Effective configuration (no secrets)",
    response_model=ConfigView,
)
def get_configuration(config: ConfigDep, paths: PathsDep) -> ConfigView:
    return config_view(config, paths)


@router.patch(
    "/config",
    operation_id="updateConfig",
    summary="Update a whitelisted subset of settings",
    response_model=ConfigView,
    responses={422: {"model": ErrorResponse}},
)
def update_configuration(
    request: Request,
    patch: Annotated[ConfigPatch, Body(...)],
    container: ContainerDep,
    config: ConfigDep,
    paths: PathsDep,
) -> ConfigView:
    """Apply a whitelisted configuration patch.

    The patch is validated against the core :class:`Config` model.  It is applied
    to the running daemon immediately; persistence is delegated to the container
    when it exposes an ``apply_config``/``update_config`` hook (the integrated
    daemon persists ``config.toml``), otherwise the change lasts for the daemon's
    lifetime and is reported back in the response.
    """
    values = patch.model_dump(exclude_none=True)
    if not values:
        return config_view(config, paths)
    try:
        updated = Config.model_validate({**config.model_dump(), **values})
    except PydanticValidationError as exc:
        details = [
            {"loc": [str(part) for part in error.get("loc", ())], "msg": str(error.get("msg", "invalid"))}
            for error in exc.errors()
        ]
        raise ValidationError("invalid configuration value", details={"errors": details}) from exc

    container_config = getattr(container.raw, "config", None)
    hook = None
    for name in ("apply_config", "update_config"):
        candidate = getattr(container.raw, name, None)
        if callable(candidate):
            hook = candidate
            break
    if hook is not None:
        result = hook(values)
        if isinstance(result, Config):
            updated = result
    request.app.state.config = updated
    if isinstance(container_config, Config):
        try:
            for key, value in values.items():
                setattr(container_config, key, value)
        except (AttributeError, ValueError):  # pragma: no cover - validated already
            logger.debug("container config does not accept live updates", exc_info=True)
    return config_view(updated, paths)


@router.get(
    "/doctor",
    operation_id="runDoctor",
    summary="Run environment doctor checks",
    response_model=DoctorReport,
)
def run_doctor(
    container: ContainerDep,
    slow: Annotated[bool, Query(description="Include slow checks")] = False,
) -> DoctorReport:
    doctor = container.doctor()
    return doctor.run(include_slow=bool(slow))


@router.post(
    "/doctor/fix",
    operation_id="fixDoctorCheck",
    summary="Attempt to fix a doctor check",
    response_model=OperationResult,
    responses={404: {"model": ErrorResponse}},
)
def fix_doctor_check(
    container: ContainerDep,
    payload: Annotated[DoctorFixRequest, Body(...)],
) -> OperationResult:
    doctor = container.doctor()
    result = doctor.fix(payload.check_id)
    if result is None:  # pragma: no cover - defensive for duck-typed stubs
        raise ServiceUnavailable("doctor fix returned no result")
    return result


__all__ = [
    "AcceleratorInfoView",
    "AuthCheck",
    "ConfigPatch",
    "ConfigView",
    "DoctorFixRequest",
    "HealthResponse",
    "HostInfoView",
    "SystemInfoView",
    "collect_host",
    "config_view",
    "health_router",
    "router",
]
