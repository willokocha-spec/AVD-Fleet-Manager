"""Instance, snapshot, screen-input and capacity routes.

The runtime service is authoritative for live state; the AVD manager owns the
definition.  Listings merge both so a partially wired daemon still renders the
deployed fleet.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from avdfleet.core.config import API_PREFIX, Config
from avdfleet.core.errors import AvdFleetError, ConflictError, NotFoundError, ScreenError, ValidationError
from avdfleet.core.models import AvdInfo, OobeResetResult, OperationResult, Snapshot
from avdfleet.core.paths import Paths

from ..container import ContainerAdapter, ServiceUnavailable
from ..deps import ConfigDep, ContainerDep, PathsDep, require_auth
from ..errors import ErrorResponse

logger = logging.getLogger("avdfleet.api.routes.instances")

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class InstanceCreateRequest(BaseModel):
    """``#/components/schemas/InstanceCreateRequest``."""

    model_config = ConfigDict(extra="ignore")

    template_id: str = Field(min_length=1)
    name: str = Field(pattern=r"^[a-zA-Z0-9._-]{1,63}$")
    overrides: dict[str, Any] = Field(default_factory=dict)


class BulkDeployRequest(BaseModel):
    """``#/components/schemas/BulkDeployRequest``."""

    model_config = ConfigDict(extra="ignore")

    template_id: str = Field(min_length=1)
    count: int = Field(default=1, ge=1, le=64)
    prefix: str = Field(default="avd", min_length=1, max_length=40)
    overrides: dict[str, Any] = Field(default_factory=dict)


class InstanceStartRequest(BaseModel):
    proxy_id: str | None = None
    headless: bool | None = None
    dry_run: bool = False


class InstanceStopRequest(BaseModel):
    force: bool = False
    timeout_s: float | None = Field(default=None, ge=0, le=3600)


class WaitBootRequest(BaseModel):
    timeout_s: int | None = Field(default=None, ge=1, le=7200)


class ResetOobeRequest(BaseModel):
    strategy: str | None = Field(default=None, pattern=r"^(snapshot|wipe|package|reboot)$")
    verify: bool = True


class InstanceConfigUpdate(BaseModel):
    values: dict[str, str]
    dry_run: bool = False


class CloneRequest(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9._-]{1,63}$")


class SnapshotCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    golden: bool = False


class InputRequest(BaseModel):
    """``#/components/schemas/InputRequest``."""

    action: str = Field(pattern=r"^(tap|swipe|text|key)$")
    x: int | None = None
    y: int | None = None
    x1: int | None = None
    y1: int | None = None
    x2: int | None = None
    y2: int | None = None
    duration_ms: int | None = Field(default=None, ge=0, le=60000)
    text: str | None = None
    keycode: str | None = None


class CapacityInfoView(BaseModel):
    """``#/components/schemas/CapacityInfo``."""

    model_config = ConfigDict(extra="ignore")

    max_instances: int = 64
    running: int = 0
    deployed: int = 0
    ram_available_mb: int = 0
    ram_required_mb: int = 0
    ports: dict[str, Any] = Field(default_factory=dict)
    cpu_cores: int = 1
    can_start_more: int = 0
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(
    prefix=API_PREFIX,
    tags=["instances"],
    dependencies=[Depends(require_auth)],
    responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def lookup_instance(container: ContainerAdapter, name: str) -> AvdInfo:
    """Return an instance, preferring live runtime state; 404 when unknown."""
    avds = container.raw_service("avds")
    runtime = container.raw_service("runtime")
    info: AvdInfo | None = None
    if avds is not None:
        getter = getattr(avds, "get_instance", None)
        if callable(getter):
            try:
                info = getter(name)
            except (AvdFleetError, KeyError, IndexError):
                info = None
    if runtime is not None:
        status = getattr(runtime, "status", None)
        if callable(status):
            try:
                live = status(name)
            except (AvdFleetError, KeyError, IndexError):
                live = None
            if live is not None:
                info = live
    if info is None:
        raise NotFoundError(f"instance {name!r} was not found")
    return info


def list_instances_merged(container: ContainerAdapter) -> list[AvdInfo]:
    """Merge AVD definitions with live runtime state."""
    avds = container.raw_service("avds")
    runtime = container.raw_service("runtime")
    if avds is None and runtime is None:
        raise ServiceUnavailable(
            "the instance services are not available",
            hint="the runtime/AVD services are not enabled in this build",
        )
    merged: dict[str, AvdInfo] = {}
    if avds is not None:
        lister = getattr(avds, "list_instances", None)
        if callable(lister):
            for info in lister() or []:
                merged[info.name] = info
    if runtime is not None:
        lister = getattr(runtime, "list_running", None)
        if callable(lister):
            for info in lister() or []:
                merged[info.name] = info
    return sorted(merged.values(), key=lambda item: item.name)


def _is_active(info: AvdInfo) -> bool:
    return bool(getattr(getattr(info, "state", None), "active", False))


def _int_or(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _safe_service(container: ContainerAdapter, name: str) -> Any | None:
    """Return a service for an optional/fallback path, or None when unusable."""
    try:
        return container.raw_service(name)
    except AvdFleetError as exc:
        logger.info("service %s is unavailable for capacity computation: %s", name, exc)
        return None


def _capacity(container: ContainerAdapter, config: Config, paths: Paths) -> CapacityInfoView:
    max_instances = int(getattr(config, "max_instances", 64))
    default_ram_mb = max(512, int(getattr(config, "default_ram_mb", 2048)))
    runtime = _safe_service(container, "runtime")
    data: dict[str, Any] = {}
    warnings: list[str] = []

    if runtime is not None:
        capacity_fn = getattr(runtime, "capacity", None)
        if callable(capacity_fn):
            try:
                result = capacity_fn()
            except AvdFleetError as exc:
                logger.info("runtime capacity probe failed: %s", exc)
                warnings.append("the runtime service could not compute capacity; using static values")
                result = None
            if isinstance(result, BaseModel):
                data = result.model_dump(mode="json")
            elif isinstance(result, dict):
                data = dict(result)
        else:
            warnings.append("the runtime service does not expose capacity; using static values")
    else:
        runtime_getter = getattr(container.raw, "runtime", None)
        warnings.append(
            "the runtime service is unavailable; reporting static configuration"
            if runtime_getter is not None
            else "capacity service is not wired; reporting static configuration"
        )

    running = max(0, _int_or(data.get("running"), 0))
    deployed = max(0, _int_or(data.get("deployed"), 0))
    if not data:
        avds = _safe_service(container, "avds")
        lister = getattr(avds, "list_instances", None) if avds is not None else None
        if callable(lister):
            try:
                deployed = len(lister())
            except AvdFleetError:  # pragma: no cover - service failure
                logger.debug("could not count deployed instances", exc_info=True)

    ports = data.get("ports")
    if not isinstance(ports, dict):
        ports = {
            "start": int(getattr(config, "port_range_start", 5554)),
            "end": int(getattr(config, "port_range_end", 5680)),
            "appium_start": int(getattr(config, "appium_port_start", 4723)),
        }

    ram_available = _int_or(data.get("ram_available_mb"), -1)
    if ram_available < 0:
        from .system import collect_host  # local import: avoids route-module import cycle

        host = collect_host(container, paths)
        total_mb = int(max(0.0, host.total_ram_gb) * 1024)
        reserve = int(getattr(config, "capacity_reserve_mb", 2048))
        ram_available = max(0, total_mb - reserve - running * default_ram_mb)
    ram_required = max(0, _int_or(data.get("ram_required_mb"), running * default_ram_mb))
    cpu_cores = max(1, _int_or(data.get("cpu_cores"), _int_or(getattr(config, "default_cpu_cores", 2), 2)))
    can_start_more = _int_or(
        data.get("can_start_more"),
        min(max(0, max_instances - running), ram_available // default_ram_mb if ram_available else 0),
    )
    warnings.extend(str(item) for item in (data.get("warnings") or []) if item)

    return CapacityInfoView(
        max_instances=max_instances,
        running=running,
        deployed=deployed,
        ram_available_mb=ram_available,
        ram_required_mb=ram_required,
        ports=ports,
        cpu_cores=cpu_cores,
        can_start_more=max(0, can_start_more),
        warnings=warnings,
    )


def _screenshot_filename(name: str) -> str:
    return _SAFE_FILENAME.sub("_", name).strip("._") or "instance"


# ---------------------------------------------------------------------------
# Fleet definition routes
# ---------------------------------------------------------------------------


@router.get(
    "/instances",
    operation_id="listInstances",
    summary="List AVD instances",
    response_model=list[AvdInfo],
)
def list_instances(container: ContainerDep) -> list[AvdInfo]:
    return list_instances_merged(container)


@router.post(
    "/instances",
    operation_id="createInstance",
    summary="Deploy one instance from a template",
    status_code=201,
    response_model=AvdInfo,
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def create_instance(
    container: ContainerDep,
    payload: Annotated[InstanceCreateRequest, Body(...)],
) -> AvdInfo:
    avds = container.avds()
    return avds.create_instance(payload.template_id, payload.name, payload.overrides or None)


@router.post(
    "/instances/bulk",
    operation_id="bulkDeployInstances",
    summary="Deploy several instances from one template",
    status_code=201,
    response_model=list[AvdInfo],
    responses={409: {"model": ErrorResponse}},
)
def bulk_deploy_instances(
    container: ContainerDep,
    payload: Annotated[BulkDeployRequest, Body(...)],
) -> list[AvdInfo]:
    avds = container.avds()
    return avds.bulk_deploy(
        payload.template_id,
        count=payload.count,
        prefix=payload.prefix,
        overrides=payload.overrides or None,
    )


@router.get(
    "/instances/{name}",
    operation_id="getInstance",
    summary="Get one AVD instance",
    response_model=AvdInfo,
    responses={404: {"model": ErrorResponse}},
)
def get_instance(container: ContainerDep, name: str) -> AvdInfo:
    return lookup_instance(container, name)


@router.delete(
    "/instances/{name}",
    operation_id="deleteInstance",
    summary="Delete an AVD instance",
    status_code=204,
    response_class=Response,
    responses={204: {"description": "Deleted"}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def delete_instance(
    container: ContainerDep,
    name: str,
    delete_files: Annotated[bool, Query()] = True,
) -> Response:
    info = lookup_instance(container, name)
    if _is_active(info):
        raise ConflictError(
            f"instance {name!r} is running",
            hint="stop the instance before deleting it",
        )
    container.avds().delete_instance(name, delete_files=bool(delete_files))
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@router.post(
    "/instances/{name}/start",
    operation_id="startInstance",
    summary="Start an instance",
    response_model=AvdInfo,
    responses={409: {"model": ErrorResponse}},
)
def start_instance(
    container: ContainerDep,
    name: str,
    payload: Annotated[InstanceStartRequest | None, Body()] = None,
) -> AvdInfo:
    body = payload or InstanceStartRequest()
    info = container.runtime().start(
        name,
        proxy_id=body.proxy_id,
        headless=body.headless,
        dry_run=body.dry_run,
    )
    return info


@router.post(
    "/instances/{name}/stop",
    operation_id="stopInstance",
    summary="Stop an instance",
    response_model=AvdInfo,
)
def stop_instance(
    container: ContainerDep,
    name: str,
    payload: Annotated[InstanceStopRequest | None, Body()] = None,
) -> AvdInfo:
    body = payload or InstanceStopRequest()
    return container.runtime().stop(name, timeout_s=body.timeout_s, force=body.force)


@router.post(
    "/instances/{name}/restart",
    operation_id="restartInstance",
    summary="Restart an instance",
    response_model=AvdInfo,
)
def restart_instance(container: ContainerDep, name: str) -> AvdInfo:
    return container.runtime().restart(name)


@router.post(
    "/instances/{name}/wait-boot",
    operation_id="waitBoot",
    summary="Wait for an instance to finish booting",
    response_model=AvdInfo,
    responses={504: {"model": ErrorResponse}},
)
def wait_boot(
    container: ContainerDep,
    name: str,
    payload: Annotated[WaitBootRequest | None, Body()] = None,
) -> AvdInfo:
    body = payload or WaitBootRequest()
    # Boot-time automation is triggered by the runtime's boot-completed hook, so
    # every entry point (this route, the CLI, the GUI) reaches it exactly once.
    return container.runtime().wait_boot(name, timeout_s=body.timeout_s)


@router.post(
    "/instances/{name}/reset-oobe",
    operation_id="resetInstanceOobe",
    summary="Reset an instance to the out-of-box experience",
    response_model=OobeResetResult,
    tags=["instances", "oobe"],
)
def reset_instance_oobe(
    container: ContainerDep,
    name: str,
    payload: Annotated[ResetOobeRequest | None, Body()] = None,
) -> OobeResetResult:
    body = payload or ResetOobeRequest()
    return container.oobe().reset(name, strategy=body.strategy, verify=body.verify)


# ---------------------------------------------------------------------------
# Screen / config / clone
# ---------------------------------------------------------------------------


@router.get(
    "/instances/{name}/screenshot",
    operation_id="getScreenshot",
    summary="Capture a PNG screenshot",
    tags=["instances", "screen"],
    response_class=Response,
    responses={
        200: {"description": "PNG screenshot", "content": {"image/png": {}}},
        503: {"model": ErrorResponse},
    },
)
def get_screenshot(container: ContainerDep, name: str) -> Response:
    try:
        screen = container.screen()
    except AvdFleetError as exc:
        raise ScreenError(
            "screen capture is not available",
            hint="the emulator screen service could not be initialised (is the Android SDK installed?)",
        ) from exc
    try:
        data = screen.capture(name)
    except ScreenError:
        raise
    except AvdFleetError as exc:
        raise ScreenError(
            f"screen capture failed: {exc.message}",
            hint="check that the instance is running and adb can reach it",
        ) from exc
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    if not isinstance(data, bytes) or not data:
        raise ScreenError(
            "screenshot capture returned no image data",
            hint="check that the instance is running and screen sharing is available",
        )
    return Response(
        content=data,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'inline; filename="{_screenshot_filename(name)}.png"',
        },
    )


@router.put(
    "/instances/{name}/config",
    operation_id="updateInstanceConfig",
    summary="Update config.ini values",
    response_model=AvdInfo,
    responses={422: {"model": ErrorResponse}},
)
def update_instance_config(
    container: ContainerDep,
    name: str,
    payload: Annotated[InstanceConfigUpdate, Body(...)],
) -> AvdInfo:
    return container.avds().update_config(name, payload.values, dry_run=payload.dry_run)


@router.post(
    "/instances/{name}/clone",
    operation_id="cloneInstance",
    summary="Clone an instance",
    status_code=201,
    response_model=AvdInfo,
)
def clone_instance(
    container: ContainerDep,
    name: str,
    payload: Annotated[CloneRequest, Body(...)],
) -> AvdInfo:
    return container.avds().clone_instance(name, payload.name)


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


@router.get(
    "/instances/{name}/snapshots",
    operation_id="listSnapshots",
    summary="List instance snapshots",
    response_model=list[Snapshot],
)
def list_snapshots(container: ContainerDep, name: str) -> list[Snapshot]:
    return container.runtime().list_snapshots(name)


@router.post(
    "/instances/{name}/snapshots",
    operation_id="createSnapshot",
    summary="Create a snapshot",
    status_code=201,
    response_model=Snapshot,
)
def create_snapshot(
    container: ContainerDep,
    name: str,
    payload: Annotated[SnapshotCreateRequest, Body(...)],
) -> Snapshot:
    return container.runtime().save_snapshot(name, payload.name, golden=payload.golden)


@router.post(
    "/instances/{name}/snapshots/{snapshot}/load",
    operation_id="loadSnapshot",
    summary="Restore a snapshot",
    response_model=Snapshot,
)
def load_snapshot(container: ContainerDep, name: str, snapshot: str) -> Snapshot:
    return container.runtime().load_snapshot(name, snapshot)


@router.delete(
    "/instances/{name}/snapshots/{snapshot}",
    operation_id="deleteSnapshot",
    summary="Delete a snapshot",
    status_code=204,
    response_class=Response,
    responses={204: {"description": "Deleted"}},
)
def delete_snapshot(container: ContainerDep, name: str, snapshot: str) -> Response:
    container.runtime().delete_snapshot(name, snapshot)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


@router.post(
    "/instances/{name}/input",
    operation_id="sendInput",
    summary="Inject a tap/swipe/text/key event",
    response_model=OperationResult,
    responses={422: {"model": ErrorResponse}},
    tags=["instances", "screen"],
)
def send_input(
    container: ContainerDep,
    name: str,
    payload: Annotated[InputRequest, Body(...)],
) -> OperationResult:
    screen = container.screen()
    action = payload.action
    if action == "tap":
        if payload.x is None or payload.y is None:
            raise ValidationError("tap input requires x and y")
        return screen.tap(name, payload.x, payload.y)
    if action == "swipe":
        required = (payload.x1, payload.y1, payload.x2, payload.y2)
        if any(value is None for value in required):
            raise ValidationError("swipe input requires x1, y1, x2 and y2")
        return screen.swipe(
            name,
            int(payload.x1),  # type: ignore[arg-type]
            int(payload.y1),  # type: ignore[arg-type]
            int(payload.x2),  # type: ignore[arg-type]
            int(payload.y2),  # type: ignore[arg-type]
            duration_ms=payload.duration_ms or 300,
        )
    if action == "text":
        if not payload.text:
            raise ValidationError("text input requires a non-empty text value")
        return screen.text(name, payload.text)
    if not payload.keycode:
        raise ValidationError("key input requires a keycode")
    return screen.key(name, payload.keycode)


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------


@router.get(
    "/capacity",
    operation_id="getCapacity",
    summary="Fleet capacity (RAM, ports, CPU)",
    response_model=CapacityInfoView,
)
def get_capacity(container: ContainerDep, config: ConfigDep, paths: PathsDep) -> CapacityInfoView:
    return _capacity(container, config, paths)


__all__ = [
    "BulkDeployRequest",
    "CapacityInfoView",
    "CloneRequest",
    "InputRequest",
    "InstanceConfigUpdate",
    "InstanceCreateRequest",
    "InstanceStartRequest",
    "InstanceStopRequest",
    "ResetOobeRequest",
    "SnapshotCreateRequest",
    "WaitBootRequest",
    "list_instances_merged",
    "lookup_instance",
    "router",
]
