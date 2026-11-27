"""AVD template and device-profile routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Response

from avdfleet.core.config import API_PREFIX
from avdfleet.core.errors import NotFoundError
from avdfleet.core.models import DeviceProfile, Template

from ..container import ContainerAdapter
from ..deps import ContainerDep, require_auth
from ..errors import ErrorResponse

router = APIRouter(
    prefix=API_PREFIX,
    tags=["templates"],
    dependencies=[Depends(require_auth)],
    responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)


def _save(container: ContainerAdapter, template: Template) -> Template:
    avds = container.avds()
    saved = avds.save_template(template)
    return saved if saved is not None else template


@router.get(
    "/templates",
    operation_id="listTemplates",
    summary="List AVD templates",
    response_model=list[Template],
)
def list_templates(container: ContainerDep) -> list[Template]:
    return container.avds().list_templates()


@router.post(
    "/templates",
    operation_id="createTemplate",
    summary="Create an AVD template",
    status_code=201,
    response_model=Template,
    responses={422: {"model": ErrorResponse}},
)
def create_template(
    container: ContainerDep,
    template: Annotated[Template, Body(...)],
) -> Template:
    return _save(container, template)


@router.get(
    "/templates/{template_id}",
    operation_id="getTemplate",
    summary="Get an AVD template",
    response_model=Template,
    responses={404: {"model": ErrorResponse}},
)
def get_template(container: ContainerDep, template_id: str) -> Template:
    try:
        return container.avds().get_template(template_id)
    except (KeyError, IndexError) as exc:
        raise NotFoundError(f"template {template_id!r} was not found") from exc


@router.put(
    "/templates/{template_id}",
    operation_id="updateTemplate",
    summary="Update an AVD template",
    response_model=Template,
    responses={404: {"model": ErrorResponse}},
)
def update_template(
    container: ContainerDep,
    template_id: str,
    template: Annotated[Template, Body(...)],
) -> Template:
    """Save a template.

    The path id is authoritative: a mismatched body id cannot silently rename a
    template (clients normally send the same object they received from GET).
    """
    if template.id != template_id:
        template = template.model_copy(update={"id": template_id})
    return _save(container, template)


@router.delete(
    "/templates/{template_id}",
    operation_id="deleteTemplate",
    summary="Delete an AVD template",
    status_code=204,
    response_class=Response,
    responses={204: {"description": "Deleted"}, 404: {"model": ErrorResponse}},
)
def delete_template(container: ContainerDep, template_id: str) -> Response:
    container.avds().delete_template(template_id)
    return Response(status_code=204)


@router.get(
    "/devices",
    operation_id="listDeviceProfiles",
    summary="List device profiles known to the Android tooling",
    response_model=list[DeviceProfile],
)
def list_device_profiles(container: ContainerDep) -> list[DeviceProfile]:
    return container.avds().list_device_profiles()


__all__ = ["router"]
