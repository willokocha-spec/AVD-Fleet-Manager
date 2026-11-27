"""Fleet-spec plan/apply routes.

The reconciler service owns the semantics; the API accepts the fleet document
inline (``spec``), as YAML text (``yaml``) or as a path the daemon can read
(``path``).  Everything is validated against the core :class:`FleetSpec` model
before the service sees it, and the service's :class:`FleetPlan` is returned
through the response model (``apply`` honours ``dry_run``).
"""

from __future__ import annotations

from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError

from avdfleet.core.config import API_PREFIX
from avdfleet.core.errors import FleetSpecError, NotFoundError, ValidationError
from avdfleet.core.models import FleetPlan, FleetSpec

from ..container import ContainerAdapter
from ..deps import ContainerDep, require_auth
from ..errors import ErrorResponse

router = APIRouter(
    prefix=API_PREFIX,
    tags=["fleet"],
    dependencies=[Depends(require_auth)],
    responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)


class FleetRequest(BaseModel):
    """``#/components/schemas/FleetRequest``."""

    model_config = ConfigDict(extra="ignore")

    spec: dict[str, Any] | None = None
    yaml: str | None = None
    path: str | None = None
    prune: bool = False
    dry_run: bool = False


def resolve_spec(container: ContainerAdapter, payload: FleetRequest) -> FleetSpec:
    """Parse the request into a validated :class:`FleetSpec`."""
    if payload.spec is not None:
        return _validate(payload.spec, source="request body")
    if payload.yaml:
        try:
            data = yaml.safe_load(payload.yaml)
        except yaml.YAMLError as exc:
            raise FleetSpecError(f"invalid fleet YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise FleetSpecError("fleet YAML must contain a mapping at the top level")
        return _validate(data, source="YAML")
    if payload.path:
        fleet = container.fleet()
        try:
            return fleet.load(payload.path)
        except FileNotFoundError as exc:
            raise NotFoundError(f"fleet spec file {payload.path!r} was not found") from exc
    raise ValidationError("fleet request requires one of 'spec', 'yaml' or 'path'")


def _validate(data: dict[str, Any], *, source: str) -> FleetSpec:
    try:
        return FleetSpec.model_validate(data)
    except PydanticValidationError as exc:
        messages = [
            f"{'.'.join(str(part) for part in error.get('loc', ())) or '<root>'}: {error.get('msg', 'invalid')}"
            for error in exc.errors()
        ]
        raise FleetSpecError(
            f"fleet spec from {source} is invalid",
            details={"errors": messages},
        ) from exc


@router.post(
    "/fleet/plan",
    operation_id="planFleet",
    summary="Diff a fleet spec against the current fleet",
    response_model=FleetPlan,
    responses={422: {"model": ErrorResponse}},
)
def plan_fleet(
    container: ContainerDep,
    payload: Annotated[FleetRequest, Body(...)],
) -> FleetPlan:
    fleet = container.fleet()
    spec = resolve_spec(container, payload)
    return fleet.plan(spec, prune=payload.prune)


@router.post(
    "/fleet/apply",
    operation_id="applyFleet",
    summary="Reconcile the fleet to a spec",
    response_model=FleetPlan,
    responses={422: {"model": ErrorResponse}},
)
def apply_fleet(
    container: ContainerDep,
    payload: Annotated[FleetRequest, Body(...)],
) -> FleetPlan:
    fleet = container.fleet()
    spec = resolve_spec(container, payload)
    return fleet.apply(spec, prune=payload.prune, dry_run=payload.dry_run)


__all__ = ["FleetRequest", "plan_fleet", "apply_fleet", "resolve_spec", "router"]
