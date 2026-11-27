"""Proxy pool, assignment, bridge and guest-egress routes.

Security invariant: proxy passwords are write-only through this API.  Every
response path goes through :class:`ProxyView`, which exposes ``has_credentials``
instead of the secret; :func:`avdfleet.core.logging.redact` scrubs health/error
payloads as a second layer.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic import ValidationError as PydanticValidationError

from avdfleet.core.config import API_PREFIX
from avdfleet.core.errors import NotFoundError, ValidationError
from avdfleet.core.logging import redact
from avdfleet.core.models import (
    BridgeConfig,
    ProxyAssignment,
    ProxyConfig,
    ProxyImportResult,
    ProxyScheme,
    ProxyTestResult,
    validate_proxy_url,
)

from ..container import ServiceUnavailable, UnsupportedOperation
from ..deps import ContainerDep, require_auth
from ..errors import ErrorResponse

logger = logging.getLogger("avdfleet.api.routes.proxies")


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class ProxyView(BaseModel):
    """``#/components/schemas/ProxyView`` — password is never part of this model."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str = ""
    scheme: Literal["http", "https", "socks5"] = "http"
    host: str
    port: int
    username: str | None = None
    has_credentials: bool = False
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    health: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class ProxyCreateRequest(BaseModel):
    """``#/components/schemas/ProxyCreateRequest``."""

    model_config = ConfigDict(extra="ignore")

    url: str | None = None
    name: str | None = None
    scheme: str | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = None
    password: str | None = None
    tags: list[str] = Field(default_factory=list)


class ProxyImportRequest(BaseModel):
    text: str
    source: str = "paste"


class ProxyImportResultView(BaseModel):
    """``#/components/schemas/ProxyImportResult`` with redacted proxy views."""

    added: list[ProxyView] = Field(default_factory=list)
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)


class VerifyGuestRequest(BaseModel):
    avd_name: str = Field(min_length=1)
    proxy_id: str = Field(min_length=1)
    token_url: str | None = None
    timeout_s: float | None = Field(default=None, ge=0, le=600)


class AssignRequest(BaseModel):
    avd_name: str = Field(min_length=1)
    proxy_id: str = Field(min_length=1)


class AutoAssignRequest(BaseModel):
    instances: list[str] = Field(min_length=1)
    strategy: Literal["round_robin", "random", "healthiest"] = "round_robin"


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix=API_PREFIX,
    tags=["proxies"],
    dependencies=[Depends(require_auth)],
    responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def as_proxy_config(value: Any) -> ProxyConfig:
    """Coerce a service return value into a :class:`ProxyConfig`."""
    if isinstance(value, ProxyConfig):
        return value
    if isinstance(value, BaseModel):
        return ProxyConfig.model_validate(value.model_dump())
    if isinstance(value, Mapping):
        return ProxyConfig.model_validate(dict(value))
    raise TypeError(f"proxy service returned {type(value).__name__}, expected ProxyConfig")


def proxy_view(value: Any) -> ProxyView:
    """Build the redacted API view of a proxy record."""
    proxy = as_proxy_config(value)
    health: dict[str, Any] = {}
    if proxy.health is not None:
        raw_health = (
            proxy.health.model_dump(mode="json") if isinstance(proxy.health, BaseModel) else proxy.health
        )
        if isinstance(raw_health, Mapping):
            health = dict(redact(dict(raw_health)))
    scheme = getattr(proxy.scheme, "value", proxy.scheme)
    return ProxyView(
        id=proxy.id,
        name=proxy.name,
        scheme=scheme,  # type: ignore[arg-type]
        host=proxy.host,
        port=proxy.port,
        username=proxy.username,
        has_credentials=proxy.has_credentials,
        tags=list(proxy.tags),
        enabled=bool(proxy.enabled),
        health=health,
        created_at=proxy.created_at,
    )


def _optional_method(service: Any, name: str) -> Any | None:
    """Return a callable attribute, or None when the service lacks it."""
    try:
        candidate = getattr(service, name)
    except (AttributeError, UnsupportedOperation):
        return None
    return candidate if callable(candidate) else None


def _build_proxy(payload: ProxyCreateRequest) -> ProxyConfig:
    username = payload.username
    password = payload.password
    if payload.url:
        try:
            scheme, host, port, url_user, url_password = validate_proxy_url(payload.url)
        except ValueError as exc:
            raise ValidationError(f"invalid proxy URL: {exc}") from exc
        username = username if username is not None else url_user
        password = password if password is not None else url_password
    else:
        if not payload.host or payload.port is None:
            raise ValidationError("proxy requires either 'url' or both 'host' and 'port'")
        scheme_value = (payload.scheme or "http").lower()
        try:
            scheme = ProxyScheme(scheme_value)
        except ValueError as exc:
            raise ValidationError(f"unsupported proxy scheme {payload.scheme!r}") from exc
        host = payload.host
        port = payload.port
    name = payload.name or f"{scheme.value}://{host}:{port}"
    return ProxyConfig(
        id=f"px_{uuid.uuid4().hex[:12]}",
        name=name,
        scheme=scheme,
        host=host,
        port=port,
        username=username,
        password=SecretStr(password) if password else None,
        tags=list(payload.tags),
    )


def _import_result_view(result: Any) -> ProxyImportResultView:
    if isinstance(result, ProxyImportResult):
        return ProxyImportResultView(
            added=[proxy_view(item) for item in result.added],
            skipped=int(result.skipped),
            errors=[redact(str(item)) for item in result.errors],
        )
    if isinstance(result, BaseModel):
        result = result.model_dump()
    if isinstance(result, Mapping):
        return ProxyImportResultView(
            added=[proxy_view(item) for item in result.get("added", []) or []],
            skipped=int(result.get("skipped", 0) or 0),
            errors=[redact(str(item)) for item in (result.get("errors", []) or [])],
        )
    raise TypeError(f"proxy import returned {type(result).__name__}")


# ---------------------------------------------------------------------------
# Pool routes
# ---------------------------------------------------------------------------


@router.get(
    "/proxies",
    operation_id="listProxies",
    summary="List proxies (passwords masked)",
    response_model=list[ProxyView],
)
def list_proxies(
    container: ContainerDep,
    include_disabled: Annotated[bool, Query()] = False,
) -> list[ProxyView]:
    pool = container.proxies()
    return [proxy_view(item) for item in pool.list_proxies(include_disabled=bool(include_disabled))]


@router.post(
    "/proxies",
    operation_id="createProxy",
    summary="Add an authenticated proxy",
    status_code=201,
    response_model=ProxyView,
    responses={422: {"model": ErrorResponse}},
)
def create_proxy(
    container: ContainerDep,
    payload: Annotated[ProxyCreateRequest, Body(...)],
) -> ProxyView:
    proxy = _build_proxy(payload)
    try:
        created = container.proxies().add(proxy)
    except PydanticValidationError as exc:
        raise ValidationError("invalid proxy definition", details={"error": str(exc)}) from exc
    return proxy_view(created if created is not None else proxy)


@router.post(
    "/proxies/import",
    operation_id="importProxies",
    summary="Import proxy URLs, one per line",
    response_model=ProxyImportResultView,
)
def import_proxies(
    container: ContainerDep,
    payload: Annotated[ProxyImportRequest, Body(...)],
) -> ProxyImportResultView:
    result = container.proxies().import_text(payload.text, source=payload.source or "paste")
    return _import_result_view(result)


@router.post(
    "/proxies/check-all",
    operation_id="checkAllProxies",
    summary="Health-check every proxy",
    response_model=list[ProxyTestResult],
)
def check_all_proxies(container: ContainerDep) -> list[ProxyTestResult]:
    return container.proxies().check_all()


@router.post(
    "/proxies/verify-guest",
    operation_id="verifyGuestEgress",
    summary="Verify egress from inside a guest",
    response_model=ProxyTestResult,
)
def verify_guest_egress(
    container: ContainerDep,
    payload: Annotated[VerifyGuestRequest, Body(...)],
) -> ProxyTestResult:
    pool = container.proxies()
    try:
        proxy = as_proxy_config(pool.get(payload.proxy_id))
    except (KeyError, IndexError) as exc:
        raise NotFoundError(f"proxy {payload.proxy_id!r} was not found") from exc
    verifier = container.raw_service("verifier")
    if verifier is not None:
        verify = _optional_method(verifier, "verify_guest")
        if verify is not None:
            return verify(
                payload.avd_name,
                proxy,
                token_url=payload.token_url,
                timeout_s=payload.timeout_s,
            )
    pool_verify = _optional_method(pool, "verify_guest")
    if pool_verify is not None:
        return pool_verify(
            payload.avd_name,
            proxy,
            token_url=payload.token_url,
            timeout_s=payload.timeout_s,
        )
    raise ServiceUnavailable(
        "guest egress verification is not available in this daemon",
        hint="the proxy verification service is not enabled in the current build",
    )


@router.get(
    "/proxies/{proxy_id}",
    operation_id="getProxy",
    summary="Get one proxy (password masked)",
    response_model=ProxyView,
    responses={404: {"model": ErrorResponse}},
)
def get_proxy(container: ContainerDep, proxy_id: str) -> ProxyView:
    try:
        return proxy_view(container.proxies().get(proxy_id))
    except (KeyError, IndexError) as exc:
        raise NotFoundError(f"proxy {proxy_id!r} was not found") from exc


@router.patch(
    "/proxies/{proxy_id}",
    operation_id="updateProxy",
    summary="Update a proxy",
    response_model=ProxyView,
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def update_proxy(
    container: ContainerDep,
    proxy_id: str,
    payload: Annotated[dict[str, Any], Body(...)],
) -> ProxyView:
    """Patch fields; a new ``password`` is accepted but never echoed back."""
    try:
        updated = container.proxies().update(proxy_id, dict(payload))
    except (KeyError, IndexError) as exc:
        raise NotFoundError(f"proxy {proxy_id!r} was not found") from exc
    return proxy_view(updated)


@router.delete(
    "/proxies/{proxy_id}",
    operation_id="deleteProxy",
    summary="Delete a proxy",
    status_code=204,
    response_class=Response,
    responses={204: {"description": "Deleted"}, 404: {"model": ErrorResponse}},
)
def delete_proxy(container: ContainerDep, proxy_id: str) -> Response:
    try:
        container.proxies().delete(proxy_id)
    except (KeyError, IndexError) as exc:
        raise NotFoundError(f"proxy {proxy_id!r} was not found") from exc
    return Response(status_code=204)


@router.post(
    "/proxies/{proxy_id}/check",
    operation_id="checkProxy",
    summary="Health-check one proxy",
    response_model=ProxyTestResult,
    responses={404: {"model": ErrorResponse}},
)
def check_proxy(container: ContainerDep, proxy_id: str) -> ProxyTestResult:
    try:
        return container.proxies().check(proxy_id)
    except (KeyError, IndexError) as exc:
        raise NotFoundError(f"proxy {proxy_id!r} was not found") from exc


# ---------------------------------------------------------------------------
# Assignments and bridges
# ---------------------------------------------------------------------------


@router.get(
    "/assignments",
    operation_id="listAssignments",
    summary="List instance-to-proxy assignments",
    response_model=list[ProxyAssignment],
)
def list_assignments(container: ContainerDep) -> list[ProxyAssignment]:
    return container.assignments().list_assignments()


@router.post(
    "/assignments/auto",
    operation_id="autoAssignProxies",
    summary="Auto-assign proxies to instances",
    response_model=list[ProxyAssignment],
)
def auto_assign_proxies(
    container: ContainerDep,
    payload: Annotated[AutoAssignRequest, Body(...)],
) -> list[ProxyAssignment]:
    return container.assignments().auto_assign(list(payload.instances), strategy=payload.strategy)


@router.post(
    "/assignments",
    operation_id="assignProxy",
    summary="Assign a proxy to an instance",
    response_model=ProxyAssignment,
)
def assign_proxy(
    container: ContainerDep,
    payload: Annotated[AssignRequest, Body(...)],
) -> ProxyAssignment:
    return container.assignments().assign(payload.avd_name, payload.proxy_id)


@router.delete(
    "/assignments/{avd_name}",
    operation_id="unassignProxy",
    summary="Remove an instance's proxy assignment",
    status_code=204,
    response_class=Response,
    responses={204: {"description": "Unassigned"}},
)
def unassign_proxy(container: ContainerDep, avd_name: str) -> Response:
    container.assignments().unassign(avd_name)
    return Response(status_code=204)


@router.get(
    "/bridges",
    operation_id="listBridges",
    summary="List active local proxy bridges",
    response_model=list[BridgeConfig],
)
def list_bridges(container: ContainerDep) -> list[BridgeConfig]:
    return container.bridge().list_active()


__all__ = [
    "AssignRequest",
    "AutoAssignRequest",
    "ProxyCreateRequest",
    "ProxyImportRequest",
    "ProxyImportResultView",
    "ProxyView",
    "VerifyGuestRequest",
    "as_proxy_config",
    "proxy_view",
    "router",
]
