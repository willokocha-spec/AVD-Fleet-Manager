"""Appium server lifecycle and capability routes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, Field

from avdfleet.core.config import API_PREFIX
from avdfleet.core.errors import AvdFleetError
from avdfleet.core.models import AppiumStatus

from ..container import ContainerAdapter
from ..deps import ContainerDep, require_auth
from ..errors import ErrorResponse
from .instances import lookup_instance

router = APIRouter(
    prefix=API_PREFIX,
    tags=["appium"],
    dependencies=[Depends(require_auth)],
    responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)


class AppiumStartRequest(BaseModel):
    port: int | None = Field(default=None, ge=1, le=65535)


class AppiumCapabilitiesRequest(BaseModel):
    avd_name: str = Field(min_length=1)


def _template_for(container: ContainerAdapter, avd: Any) -> Any | None:
    template_id = getattr(avd, "template_id", None)
    if not template_id:
        return None
    avds = container.raw_service("avds")
    if avds is None:
        return None
    getter = getattr(avds, "get_template", None)
    if not callable(getter):
        return None
    try:
        return getter(template_id)
    except (AvdFleetError, KeyError, IndexError):
        return None


@router.get(
    "/appium",
    operation_id="getAppiumStatus",
    summary="Appium server status",
    response_model=AppiumStatus,
)
def get_appium_status(container: ContainerDep) -> AppiumStatus:
    return container.appium().status()


@router.post(
    "/appium/start",
    operation_id="startAppium",
    summary="Start the Appium server",
    response_model=AppiumStatus,
)
def start_appium(
    container: ContainerDep,
    payload: Annotated[AppiumStartRequest | None, Body()] = None,
) -> AppiumStatus:
    body = payload or AppiumStartRequest()
    return container.appium().start(port=body.port)


@router.post(
    "/appium/stop",
    operation_id="stopAppium",
    summary="Stop the Appium server",
    response_model=AppiumStatus,
)
def stop_appium(container: ContainerDep) -> AppiumStatus:
    return container.appium().stop()


@router.post(
    "/appium/capabilities",
    operation_id="getAppiumCapabilities",
    summary="Build UiAutomator2 capabilities for an instance",
    response_model=dict[str, Any],
)
def get_appium_capabilities(
    container: ContainerDep,
    payload: Annotated[AppiumCapabilitiesRequest, Body(...)],
) -> dict[str, Any]:
    avd = lookup_instance(container, payload.avd_name)
    template = _template_for(container, avd)
    capabilities = container.appium().build_capabilities(avd, template=template)
    if isinstance(capabilities, Mapping):
        return dict(capabilities)
    raise TypeError(f"appium capabilities returned {type(capabilities).__name__}, expected a mapping")


__all__ = ["AppiumCapabilitiesRequest", "AppiumStartRequest", "router"]
