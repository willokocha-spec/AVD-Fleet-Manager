"""Script definition and fan-out run routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Response

from avdfleet.core.config import API_PREFIX
from avdfleet.core.errors import NotFoundError
from avdfleet.core.models import ScriptDefinition, ScriptRun, ScriptRunRequest

from ..container import ServiceUnavailable, UnsupportedOperation
from ..deps import ContainerDep, require_auth
from ..errors import ErrorResponse

router = APIRouter(
    prefix=API_PREFIX,
    tags=["scripts"],
    dependencies=[Depends(require_auth)],
    responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)


@router.get(
    "/scripts",
    operation_id="listScripts",
    summary="List script definitions",
    response_model=list[ScriptDefinition],
)
def list_scripts(container: ContainerDep) -> list[ScriptDefinition]:
    return container.scripts().list_scripts()


@router.post(
    "/scripts",
    operation_id="createScript",
    summary="Create a script definition",
    status_code=201,
    response_model=ScriptDefinition,
    responses={422: {"model": ErrorResponse}},
)
def create_script(
    container: ContainerDep,
    script: Annotated[ScriptDefinition, Body(...)],
) -> ScriptDefinition:
    saved = container.scripts().save_script(script)
    return saved if saved is not None else script


@router.get(
    "/scripts/{script_id}",
    operation_id="getScript",
    summary="Get a script definition",
    response_model=ScriptDefinition,
    responses={404: {"model": ErrorResponse}},
)
def get_script(container: ContainerDep, script_id: str) -> ScriptDefinition:
    try:
        return container.scripts().get_script(script_id)
    except (KeyError, IndexError) as exc:
        raise NotFoundError(f"script {script_id!r} was not found") from exc


@router.put(
    "/scripts/{script_id}",
    operation_id="updateScript",
    summary="Update a script definition",
    response_model=ScriptDefinition,
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def update_script(
    container: ContainerDep,
    script_id: str,
    script: Annotated[ScriptDefinition, Body(...)],
) -> ScriptDefinition:
    if script.id != script_id:
        script = script.model_copy(update={"id": script_id})
    saved = container.scripts().save_script(script)
    return saved if saved is not None else script


@router.delete(
    "/scripts/{script_id}",
    operation_id="deleteScript",
    summary="Delete a script definition",
    status_code=204,
    response_class=Response,
    responses={204: {"description": "Deleted"}, 404: {"model": ErrorResponse}},
)
def delete_script(container: ContainerDep, script_id: str) -> Response:
    container.scripts().delete_script(script_id)
    return Response(status_code=204)


@router.post(
    "/scripts/runs",
    operation_id="startScriptRun",
    summary="Start a script run across instances",
    status_code=202,
    response_model=ScriptRun,
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def start_script_run(
    container: ContainerDep,
    payload: Annotated[ScriptRunRequest, Body(...)],
) -> ScriptRun:
    """Queue a fan-out run and return immediately (HTTP 202).

    ``ScriptRunner.submit`` starts the run on a background thread; ``run()`` is
    the blocking variant used by the CLI and is deliberately not called here so
    the request returns as soon as the run is queued.
    """
    scripts = container.scripts()
    try:
        submit = scripts.submit
    except UnsupportedOperation as exc:
        raise ServiceUnavailable(
            "the scripts service does not support asynchronous runs",
            hint="this build only supports blocking runs via the CLI",
        ) from exc
    if not callable(submit):  # pragma: no cover - duck-typed stub without submit
        raise ServiceUnavailable("the scripts service does not support asynchronous runs")
    return submit(payload)


@router.get(
    "/runs",
    operation_id="listRuns",
    summary="List script runs",
    response_model=list[ScriptRun],
)
def list_runs(
    container: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[ScriptRun]:
    return container.scripts().list_runs(limit=int(limit))


@router.get(
    "/runs/{run_id}",
    operation_id="getRun",
    summary="Get one script run",
    response_model=ScriptRun,
    responses={404: {"model": ErrorResponse}},
)
def get_run(container: ContainerDep, run_id: str) -> ScriptRun:
    try:
        return container.scripts().get_run(run_id)
    except (KeyError, IndexError) as exc:
        raise NotFoundError(f"script run {run_id!r} was not found") from exc


@router.post(
    "/runs/{run_id}/cancel",
    operation_id="cancelRun",
    summary="Cancel a running script run",
    response_model=ScriptRun,
    responses={404: {"model": ErrorResponse}},
)
def cancel_run(container: ContainerDep, run_id: str) -> ScriptRun:
    try:
        return container.scripts().cancel(run_id)
    except (KeyError, IndexError) as exc:
        raise NotFoundError(f"script run {run_id!r} was not found") from exc


__all__ = ["router"]
