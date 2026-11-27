"""System-image and OOBE routes.

The contract groups OOBE under its own tag; the handlers live here because they
are image-centric and share the image catalogue service.  OOBE data has a disk
fallback (``oobe_matrix.json``) so the GUI renders the checked-in matrix even
when the OOBE service is not wired into a partial daemon.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from avdfleet.core.config import API_PREFIX, Config
from avdfleet.core.errors import AvdFleetError, NotFoundError
from avdfleet.core.models import OobeMatrix, OobeRecord, SystemImage
from avdfleet.core.paths import Paths

from ..container import ContainerAdapter
from ..deps import ConfigDep, ContainerDep, PathsDep, require_auth
from ..errors import BadRequestError, ErrorResponse

logger = logging.getLogger("avdfleet.api.routes.images")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ImageImportRequest(BaseModel):
    """``#/components/schemas/ImageImportRequest``."""

    model_config = ConfigDict(extra="ignore")

    path: str = Field(min_length=1)
    name: str | None = None
    image_id: str | None = None
    api_level: int | None = Field(default=None, ge=1, le=999)
    abi: str | None = None


class DownloadImageRequest(BaseModel):
    dry_run: bool = False


class OobeVerifyRequest(BaseModel):
    image_id: str = Field(min_length=1)
    timeout_s: int | None = Field(default=None, ge=30, le=1800)
    keep_avd: bool = False


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix=API_PREFIX,
    dependencies=[Depends(require_auth)],
    responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _matrix_path(config: Config, paths: Paths) -> Path:
    configured = getattr(config, "oobe_matrix_path", None)
    if configured:
        return Path(str(configured)).expanduser()
    return paths.oobe_matrix_path


def load_matrix_from_disk(config: Config, paths: Paths) -> OobeMatrix | None:
    """Load the checked-in OOBE matrix from disk, if present and valid."""
    path = _matrix_path(config, paths)
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("could not read OOBE matrix at %s", path, exc_info=True)
        return None
    try:
        return OobeMatrix.model_validate(data)
    except PydanticValidationError:
        logger.warning("OOBE matrix at %s is not schema-valid", path, exc_info=True)
        return None


def _oobe_target(container: ContainerAdapter) -> Any | None:
    """Return the OOBE service, or None when it cannot be initialised.

    ``matrix``/``records`` have a disk fallback, so an initialisation failure
    (for example no Android SDK yet) must not turn a read-only endpoint into a
    500 before the fallback has been tried.
    """
    try:
        return container.raw_service("oobe")
    except AvdFleetError as exc:
        logger.info("OOBE service unavailable; using the matrix file fallback: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


@router.get(
    "/images",
    operation_id="listImages",
    summary="List system images",
    response_model=list[SystemImage],
    tags=["images"],
)
def list_images(
    container: ContainerDep,
    include_remote: Annotated[bool, Query()] = True,
    installed_only: Annotated[bool, Query()] = False,
) -> list[SystemImage]:
    images = container.images()
    return images.list_images(include_remote=bool(include_remote), installed_only=bool(installed_only))


@router.post(
    "/images",
    operation_id="importImage",
    summary="Import a custom system image from a host directory",
    status_code=201,
    response_model=SystemImage,
    responses={400: {"model": ErrorResponse}},
    tags=["images"],
)
def import_image(
    container: ContainerDep,
    payload: Annotated[ImageImportRequest, Body(...)],
) -> SystemImage:
    images = container.images()
    try:
        return images.import_directory(
            payload.path,
            name=payload.name,
            image_id=payload.image_id,
            api_level=payload.api_level,
            abi=payload.abi,
        )
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        raise BadRequestError(
            f"cannot import image from {payload.path!r}",
            hint="the path must be an existing Android system-image directory",
        ) from exc


@router.post(
    "/images/refresh",
    operation_id="refreshImages",
    summary="Refresh the remote image catalogue",
    response_model=list[SystemImage],
    tags=["images"],
)
def refresh_images(container: ContainerDep) -> list[SystemImage]:
    images = container.images()
    return images.refresh()


@router.get(
    "/images/{image_id}",
    operation_id="getImage",
    summary="Get one system image",
    response_model=SystemImage,
    responses={404: {"model": ErrorResponse}},
    tags=["images"],
)
def get_image(container: ContainerDep, image_id: str) -> SystemImage:
    images = container.images()
    try:
        return images.get_image(image_id)
    except (KeyError, IndexError) as exc:
        raise NotFoundError(f"system image {image_id!r} was not found") from exc


@router.post(
    "/images/{image_id}/download",
    operation_id="downloadImage",
    summary="Download and install a system image",
    response_model=SystemImage,
    responses={404: {"model": ErrorResponse}},
    tags=["images"],
)
def download_image(
    container: ContainerDep,
    image_id: str,
    payload: Annotated[DownloadImageRequest | None, Body()] = None,
) -> SystemImage:
    images = container.images()
    dry_run = bool(payload.dry_run) if payload is not None else False
    try:
        return images.download(image_id, dry_run=dry_run)
    except (KeyError, IndexError) as exc:
        raise NotFoundError(f"system image {image_id!r} was not found") from exc


# ---------------------------------------------------------------------------
# OOBE
# ---------------------------------------------------------------------------


@router.get(
    "/oobe/matrix",
    operation_id="getOobeMatrix",
    summary="OOBE verification matrix",
    response_model=OobeMatrix,
    tags=["oobe"],
)
def get_oobe_matrix(container: ContainerDep, config: ConfigDep, paths: PathsDep) -> OobeMatrix:
    target = _oobe_target(container)
    if target is not None:
        matrix = getattr(target, "matrix", None)
        if callable(matrix):
            result = matrix()
            if result is not None:
                return result  # type: ignore[no-any-return]
    loaded = load_matrix_from_disk(config, paths)
    if loaded is not None:
        return loaded
    return OobeMatrix(version="1", records=[])


@router.post(
    "/oobe/verify",
    operation_id="verifyOobeImage",
    summary="Verify OOBE on an image (boots a temporary AVD)",
    response_model=OobeRecord,
    responses={404: {"model": ErrorResponse}},
    tags=["oobe"],
)
def verify_oobe_image(
    container: ContainerDep,
    payload: Annotated[OobeVerifyRequest, Body(...)],
) -> OobeRecord:
    oobe = container.oobe()
    return oobe.verify_image(payload.image_id, timeout_s=payload.timeout_s, keep_avd=payload.keep_avd)


@router.get(
    "/oobe/records/{image_id}",
    operation_id="getOobeRecord",
    summary="OOBE record for one image",
    response_model=OobeRecord,
    responses={404: {"model": ErrorResponse}},
    tags=["oobe"],
)
def get_oobe_record(
    container: ContainerDep,
    config: ConfigDep,
    paths: PathsDep,
    image_id: str,
) -> OobeRecord:
    target = _oobe_target(container)
    if target is not None:
        record_for = getattr(target, "record_for", None)
        if callable(record_for):
            result = record_for(image_id)
            if result is not None:
                return result  # type: ignore[no-any-return]
    loaded = load_matrix_from_disk(config, paths)
    if loaded is not None:
        record = loaded.get(image_id)
        if record is not None:
            return record
    raise NotFoundError(f"no OOBE record exists for image {image_id!r}")


__all__ = [
    "ImageImportRequest",
    "OobeVerifyRequest",
    "load_matrix_from_disk",
    "router",
]
