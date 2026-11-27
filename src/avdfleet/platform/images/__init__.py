"""System images: stock catalogue, SDK scanning, downloads, custom import, OOBE matrix.

Public entry points:

* :class:`ImageCatalogue` — offline catalogue reader (stock JSON, installed SDK
  directories, custom registry JSON).
* :class:`ImageService` — concrete ``core.interfaces.ImageCatalog``: listing,
  download via an injected ``SdkBackend``, directory import and OOBE persistence.
* :class:`OobeMatrixStore` — merge of the shipped seed matrix with the operator's
  persisted ``oobe_matrix.json``.
"""

from .catalogue import (
    DEFAULT_CATALOGUE_PATH,
    DEFAULT_MAX_IMPORT_BYTES,
    DEFAULT_SEED_MATRIX_PATH,
    ImageCatalogue,
    ImageError,
    ImageNotFoundError,
    OobeMatrixStore,
    atomic_write_json,
    image_kind_for_tag,
    package_id_for,
    parse_properties,
    primary_image_file,
    sanitize_tag,
)
from .service import ImageService

__all__ = [
    "DEFAULT_CATALOGUE_PATH",
    "DEFAULT_MAX_IMPORT_BYTES",
    "DEFAULT_SEED_MATRIX_PATH",
    "ImageCatalogue",
    "ImageError",
    "ImageNotFoundError",
    "ImageService",
    "OobeMatrixStore",
    "atomic_write_json",
    "image_kind_for_tag",
    "package_id_for",
    "parse_properties",
    "primary_image_file",
    "sanitize_tag",
]
