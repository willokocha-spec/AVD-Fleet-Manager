"""Portable-edition status and preflight-check routes.

The portable services are optional: when they are not wired, the API derives a
useful, honest report from the configuration and filesystem layout instead of
failing (the GUI shows the static checks with a warning).  When the container
provides ``status``/``checks``/``preflight`` hooks those results win.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from avdfleet.core.config import API_PREFIX, Config
from avdfleet.core.errors import AvdFleetError
from avdfleet.core.paths import Paths

from ..container import ContainerAdapter
from ..deps import ConfigDep, ContainerDep, PathsDep, require_auth
from ..errors import ErrorResponse

logger = logging.getLogger("avdfleet.api.routes.portable")

router = APIRouter(
    prefix=API_PREFIX,
    tags=["portable"],
    dependencies=[Depends(require_auth)],
    responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)


class CheckResultView(BaseModel):
    """``#/components/schemas/CheckResult``."""

    model_config = ConfigDict(extra="ignore")

    id: str
    title: str = ""
    status: str = "skip"
    detail: str = ""
    value: str | None = None


class PortableStatusView(BaseModel):
    """``#/components/schemas/PortableStatus``."""

    model_config = ConfigDict(extra="ignore")

    edition: str = "installed"
    root: str = ""
    portable: bool = False
    relocatable: bool = True
    host_os: str = "unknown"
    host_arch: str = "unknown"
    checks: list[CheckResultView] = Field(default_factory=list)
    leftovers: list[str] = Field(default_factory=list)


_VALID_STATUS = {"ok", "warn", "fail", "skip"}


def _host_os() -> str:
    system = platform.system().lower()
    return {"windows": "windows", "darwin": "macos", "linux": "linux"}.get(system, "unknown")


def _host_arch() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64", "x64"}:
        return "x86_64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return "unknown"


def _check_view(value: Any) -> CheckResultView:
    if isinstance(value, BaseModel):
        data: dict[str, Any] = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        data = dict(value)
    else:
        data = {"id": str(value), "title": str(value)}
    status = str(data.get("status") or "skip").lower()
    if status not in _VALID_STATUS:
        status = "warn"
    raw_value = data.get("value")
    return CheckResultView(
        id=str(data.get("id") or data.get("check_id") or "check"),
        title=str(data.get("title") or ""),
        status=status,
        detail=str(data.get("detail") or ""),
        value=None if raw_value is None else str(raw_value),
    )


def _service(container: ContainerAdapter) -> Any | None:
    """Return the portable service, or None when it cannot be initialised."""
    try:
        return container.raw_service("portable")
    except AvdFleetError as exc:
        logger.info("portable service unavailable; using the fallback report: %s", exc)
        return None


def fallback_checks(paths: Paths) -> list[CheckResultView]:
    """Filesystem checks that are meaningful even without the portable engine."""
    checks: list[CheckResultView] = []

    data_dir_path = paths.data_dir or paths.root
    data_dir = str(data_dir_path)
    writable = False
    try:
        writable = os.access(data_dir_path, os.W_OK)
    except OSError:  # pragma: no cover - defensive
        writable = False
    checks.append(
        CheckResultView(
            id="data_dir_writable",
            title="Data directory writable",
            status="ok" if writable else "fail",
            detail=data_dir,
            value=data_dir,
        )
    )

    free_gb = 0.0
    try:
        free_gb = round(shutil.disk_usage(str(paths.root)).free / (1024**3), 2)
    except (OSError, TypeError):  # pragma: no cover - unusual filesystem
        free_gb = 0.0
    checks.append(
        CheckResultView(
            id="free_space",
            title="Free disk space on the fleet root",
            status="ok" if free_gb >= 5 else "warn",
            detail=f"{free_gb:.2f} GiB free",
            value=f"{free_gb:.2f}",
        )
    )

    if paths.portable:
        try:
            marker_present = paths.portable_marker.is_file()
        except OSError:  # pragma: no cover - defensive
            marker_present = False
        checks.append(
            CheckResultView(
                id="portable_marker",
                title="Portable root marker present",
                status="ok" if marker_present else "fail",
                detail=str(paths.portable_marker),
                value=str(paths.portable_marker),
            )
        )
    else:
        checks.append(
            CheckResultView(
                id="portable_marker",
                title="Portable root marker present",
                status="skip",
                detail="installed edition: relocation is not applicable",
                value=None,
            )
        )
    return checks


def fallback_status(config: Config, paths: Paths) -> PortableStatusView:
    portable = bool(getattr(paths, "portable", False))
    relocatable = True
    if portable:
        try:
            relocatable = paths.portable_marker.is_file()
        except OSError:  # pragma: no cover - defensive
            relocatable = False
    return PortableStatusView(
        edition=str(getattr(config, "edition", "installed")),
        root=str(paths.root),
        portable=portable,
        relocatable=relocatable,
        host_os=_host_os(),
        host_arch=_host_arch(),
        checks=fallback_checks(paths),
        leftovers=[],
    )


def _status_from_service(value: Any, fallback: PortableStatusView) -> PortableStatusView:
    if isinstance(value, BaseModel):
        data: dict[str, Any] = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        data = dict(value)
    else:
        return fallback
    merged = fallback.model_dump()
    for key in ("edition", "root", "portable", "relocatable", "host_os", "host_arch"):
        if data.get(key) is not None:
            merged[key] = data[key]
    if data.get("checks") is not None:
        merged["checks"] = [_check_view(item).model_dump() for item in data["checks"]]
    if data.get("leftovers") is not None:
        merged["leftovers"] = [str(item) for item in data["leftovers"]]
    try:
        return PortableStatusView.model_validate(merged)
    except (TypeError, ValueError):  # pragma: no cover - service shape mismatch
        logger.warning("portable status from service is not usable", exc_info=True)
        return fallback


def _checks_from_service(value: Any) -> list[CheckResultView] | None:
    if not isinstance(value, (list, tuple)):
        return None
    return [_check_view(item) for item in value]


@router.get(
    "/portable/status",
    operation_id="getPortableStatus",
    summary="Portable-edition status",
    response_model=PortableStatusView,
)
def get_portable_status(
    container: ContainerDep,
    config: ConfigDep,
    paths: PathsDep,
) -> PortableStatusView:
    fallback = fallback_status(config, paths)
    service = _service(container)
    if service is None:
        return fallback
    status_fn = getattr(service, "status", None)
    if not callable(status_fn):
        return fallback
    try:
        return _status_from_service(status_fn(), fallback)
    except Exception:  # pragma: no cover - optional service failure
        logger.warning("portable status service failed; using fallback", exc_info=True)
        return fallback


@router.get(
    "/portable/checks",
    operation_id="getPortableChecks",
    summary="Portable preflight checks",
    response_model=list[CheckResultView],
)
def get_portable_checks(
    container: ContainerDep,
    paths: PathsDep,
) -> list[CheckResultView]:
    service = _service(container)
    if service is not None:
        for name in ("checks", "run_checks", "preflight"):
            candidate = getattr(service, name, None)
            if not callable(candidate):
                continue
            try:
                converted = _checks_from_service(candidate())
            except Exception:  # pragma: no cover - optional service failure
                logger.warning("portable checks service failed; using fallback", exc_info=True)
                converted = None
            if converted is not None:
                return converted
    return fallback_checks(paths)


__all__ = [
    "CheckResultView",
    "PortableStatusView",
    "fallback_checks",
    "fallback_status",
    "router",
]
