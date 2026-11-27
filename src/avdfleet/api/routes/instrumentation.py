"""Root & Instrumentation REST routes.

Adapters only: every route resolves the service from the container and delegates.
Deployments run on a background thread by default so the HTTP response returns as
soon as the run is queued; callers (and the test suite) can pass
``background: false`` to execute synchronously and receive the finished run.
"""

from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from avdfleet.core.config import API_PREFIX
from avdfleet.core.errors import ValidationError
from avdfleet.core.logging import get_logger
from avdfleet.instrumentation import ModuleKind, StepAction
from avdfleet.instrumentation.models import RunState

from ..container import ContainerAdapter, ServiceUnavailable
from ..deps import ContainerDep, require_auth
from ..errors import ErrorResponse

logger = get_logger("avdfleet.api.instrumentation")

router = APIRouter(
    prefix=API_PREFIX,
    tags=["instrumentation"],
    dependencies=[Depends(require_auth)],
    responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)


def _service(container: ContainerAdapter) -> Any:
    service = container.raw_service("instrumentation")
    if service is None:
        raise ServiceUnavailable(
            "the Root & Instrumentation service is not available in this build"
        )
    return service


def _split_targets(values: list[str] | None) -> list[str]:
    names: list[str] = []
    for value in values or []:
        for part in str(value).split(","):
            name = part.strip()
            if name and name not in names:
                names.append(name)
    return names


class ScanRequest(BaseModel):
    targets: list[str] = Field(default_factory=list)


class StepBody(BaseModel):
    id: str = ""
    action: StepAction
    module_id: str = ""
    module_identifier: str = ""
    params: dict[str, Any] = Field(default_factory=dict)


class ProfileBody(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    steps: list[StepBody] = Field(min_length=1)


class DeployRequest(BaseModel):
    profile_id: str = Field(min_length=1)
    targets: list[str] = Field(min_length=1)
    label: str = ""
    #: Execute before responding; the GUI uses the default (background) mode.
    background: bool = True


class ImportPathRequest(BaseModel):
    path: str = ""
    url: str = ""


class CloneProfileRequest(BaseModel):
    name: str = ""


class AcquireRequest(BaseModel):
    confirm: bool = False


class AutoProfileRequest(BaseModel):
    profile_id: str = ""


# ---------------------------------------------------------------------------
# Capability scanning
# ---------------------------------------------------------------------------


@router.get(
    "/instrumentation/capabilities",
    operation_id="getInstrumentationCapabilities",
    summary="Probe root/instrumentation capabilities",
)
def get_capabilities(
    container: ContainerDep, targets: list[str] | None = Query(default=None)
) -> dict[str, Any]:
    service = _service(container)
    selected = _split_targets(targets)
    if not selected:
        raise ValidationError(
            "select at least one AVD",
            hint="pass targets=<name> (repeatable or comma separated)",
        )
    return {"devices": [device.model_dump(mode="json") for device in service.scan(selected)]}


@router.post(
    "/instrumentation/scan",
    operation_id="scanInstrumentation",
    summary="Probe several AVDs and return their instrumentation state",
)
def post_scan(container: ContainerDep, payload: ScanRequest) -> dict[str, Any]:
    service = _service(container)
    selected = _split_targets(payload.targets)
    if not selected:
        raise ValidationError("select at least one AVD to scan")
    return {"devices": [device.model_dump(mode="json") for device in service.scan(selected)]}


# ---------------------------------------------------------------------------
# Module library
# ---------------------------------------------------------------------------


@router.get(
    "/instrumentation/modules",
    operation_id="listInstrumentationModules",
    summary="List the central module library",
)
def list_modules(container: ContainerDep, kind: str | None = Query(default=None)) -> dict[str, Any]:
    service = _service(container)
    module_kind = None
    if kind:
        try:
            module_kind = ModuleKind(kind)
        except ValueError as exc:
            raise ValidationError(
                f"unknown module kind {kind!r}",
                hint="use one of: " + ", ".join(item.value for item in ModuleKind),
            ) from exc
    return {
        "modules": [
            record.model_dump(mode="json") for record in service.list_modules(module_kind)
        ]
    }


@router.post(
    "/instrumentation/modules/import",
    operation_id="importInstrumentationModule",
    summary="Import an APK or ZIP into the module library",
)
async def import_module(container: ContainerDep, request: Request) -> Any:
    service = _service(container)
    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            raise ValidationError("no file was uploaded", hint="use the 'file' form field")
        data = await upload.read()  # type: ignore[union-attr]
        record = service.import_upload(getattr(upload, "filename", "") or "module.bin", data)
        return record
    payload = ImportPathRequest.model_validate(await request.json())
    if payload.url:
        return service.import_url(payload.url)
    if payload.path:
        return service.import_path(payload.path)
    raise ValidationError(
        "nothing to import",
        hint="send a multipart 'file', or JSON with 'path' or 'url'",
    )


@router.delete(
    "/instrumentation/modules/{module_id}",
    operation_id="deleteInstrumentationModule",
    summary="Remove a module from the library",
    status_code=204,
)
def delete_module(container: ContainerDep, module_id: str) -> Response:
    _service(container).delete_module(module_id)
    return Response(status_code=204)


@router.get(
    "/instrumentation/toolchain",
    operation_id="getInstrumentationToolchain",
    summary="Pinned upstream artifacts and their local status",
)
def get_toolchain(container: ContainerDep) -> dict[str, Any]:
    return _service(container).toolchain_status()


@router.post(
    "/instrumentation/toolchain/{key}/acquire",
    operation_id="acquireInstrumentationToolchain",
    summary="Download and verify a pinned upstream artifact",
)
def acquire_toolchain(
    container: ContainerDep, key: str, payload: AcquireRequest | None = None
) -> Any:
    confirm = payload.confirm if payload is not None else False
    return _service(container).acquire_toolchain(key, confirm=confirm)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@router.get(
    "/instrumentation/profiles",
    operation_id="listInstrumentationProfiles",
    summary="List instrumentation profiles",
)
def list_profiles(container: ContainerDep) -> dict[str, Any]:
    service = _service(container)
    return {
        "profiles": [profile.model_dump(mode="json") for profile in service.list_profiles()]
    }


@router.post(
    "/instrumentation/profiles",
    operation_id="createInstrumentationProfile",
    summary="Create an instrumentation profile",
)
def create_profile(container: ContainerDep, payload: ProfileBody) -> Any:
    service = _service(container)
    return service.save_profile(
        profile_id=None,
        name=payload.name,
        description=payload.description,
        steps=[step.model_dump(mode="json") for step in payload.steps],
    )


@router.put(
    "/instrumentation/profiles/{profile_id}",
    operation_id="updateInstrumentationProfile",
    summary="Update an instrumentation profile",
)
def update_profile(container: ContainerDep, profile_id: str, payload: ProfileBody) -> Any:
    service = _service(container)
    return service.save_profile(
        profile_id=profile_id,
        name=payload.name,
        description=payload.description,
        steps=[step.model_dump(mode="json") for step in payload.steps],
    )


@router.delete(
    "/instrumentation/profiles/{profile_id}",
    operation_id="deleteInstrumentationProfile",
    summary="Delete an instrumentation profile",
    status_code=204,
)
def delete_profile(container: ContainerDep, profile_id: str) -> Response:
    _service(container).delete_profile(profile_id)
    return Response(status_code=204)


@router.post(
    "/instrumentation/profiles/{profile_id}/clone",
    operation_id="cloneInstrumentationProfile",
    summary="Copy a profile into an editable one",
)
def clone_profile(
    container: ContainerDep, profile_id: str, payload: CloneProfileRequest | None = None
) -> Any:
    name = payload.name if payload is not None else ""
    return _service(container).clone_profile(profile_id, name=name)


@router.get(
    "/instrumentation/instances/{name}/auto-profile",
    operation_id="getInstrumentationAutoProfile",
    summary="Profile applied automatically when the AVD boots",
)
def get_auto_profile(container: ContainerDep, name: str) -> Any:
    return _service(container).get_auto_profile(name)


@router.put(
    "/instrumentation/instances/{name}/auto-profile",
    operation_id="setInstrumentationAutoProfile",
    summary="Bind or clear the profile applied when the AVD boots",
)
def set_auto_profile(
    container: ContainerDep, name: str, payload: AutoProfileRequest
) -> Any:
    return _service(container).set_auto_profile(name, payload.profile_id or None)


# ---------------------------------------------------------------------------
# Deployments
# ---------------------------------------------------------------------------


def _execute_in_background(service: Any, run_id: str) -> None:
    try:
        service.execute_run(run_id)
    except Exception:  # noqa: BLE001 - background failure is logged, not raised
        logger.exception("instrumentation deployment %s crashed", run_id)


@router.post(
    "/instrumentation/deployments",
    operation_id="createInstrumentationDeployment",
    summary="Deploy a profile to selected AVDs",
)
def create_deployment(container: ContainerDep, payload: DeployRequest) -> Any:
    service = _service(container)
    run = service.start_run(
        profile_id=payload.profile_id,
        targets=payload.targets,
        label=payload.label,
    )
    if payload.background:
        thread = threading.Thread(
            target=_execute_in_background,
            args=(service, run.id),
            name=f"avdfleet-instrumentation-{run.id}",
            daemon=True,
        )
        thread.start()
        return run
    return service.execute_run(run.id)


@router.get(
    "/instrumentation/deployments",
    operation_id="listInstrumentationDeployments",
    summary="List recent deployments",
)
def list_deployments(container: ContainerDep, limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    service = _service(container)
    return {"runs": [run.model_dump(mode="json") for run in service.list_runs(limit)]}


@router.get(
    "/instrumentation/deployments/{run_id}",
    operation_id="getInstrumentationDeployment",
    summary="Deployment status",
)
def get_deployment(container: ContainerDep, run_id: str) -> Any:
    return _service(container).get_run(run_id)


@router.post(
    "/instrumentation/deployments/{run_id}/cancel",
    operation_id="cancelInstrumentationDeployment",
    summary="Cancel a running deployment",
)
def cancel_deployment(container: ContainerDep, run_id: str) -> Any:
    return _service(container).cancel_run(run_id)


@router.get(
    "/instrumentation/deployments/{run_id}/logs",
    operation_id="getInstrumentationDeploymentLogs",
    summary="Deployment logs",
)
def get_deployment_logs(
    container: ContainerDep,
    run_id: str,
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, Any]:
    service = _service(container)
    service.get_run(run_id)  # 404 when unknown, before returning an empty log list
    return {"logs": [entry.model_dump(mode="json") for entry in service.logs(run_id, limit)]}


__all__ = ["router"]


# ``RunState`` is re-exported for contract tests that assert on the enum surface.
_STATES = RunState
