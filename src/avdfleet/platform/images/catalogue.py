"""Stock system-image catalogue, installed-SDK scanning and custom-image registry.

The catalogue is deliberately offline: it is a checked-in JSON snapshot of stock
Android SDK system-image package identifiers, plus directory scanning of the local
SDK.  No HTTP client is imported here and nothing is fetched at runtime; the SDK
backend owns downloads (``sdkmanager``/``android sdk install``).

Repository URLs, sizes and SHA-256 values in ``catalogue.json`` are optional and
only populated once independently verified (BRIEF §3.3, PLAN.md).  Everything that
ships in this package is therefore marked unverified.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from ...core.errors import AvdFleetError, NotFoundError
from ...core.logging import get_logger
from ...core.models import (
    ImageKind,
    OobeMatrix,
    OobeRecord,
    OobeStatus,
    SystemImage,
    utcnow,
)
from ...core.paths import Paths

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CATALOGUE_PATH = Path(__file__).with_name("catalogue.json")
DEFAULT_SEED_MATRIX_PATH = Path(__file__).with_name("oobe_matrix.seed.json")

CUSTOM_REGISTRY_FILENAME = "custom_images.json"
CUSTOM_REGISTRY_VERSION = 1

#: Safety cap for a single imported image directory (all recognised files).
DEFAULT_MAX_IMPORT_BYTES = 16 * 1024**3

SYSTEM_IMAGE_FILENAME = "system.img"
SYSTEM_IMAGE_QCOW2_FILENAME = "system.img.qcow2"
#: Preference order: raw ``system.img`` wins over a qcow2 overlay.
PRIMARY_IMAGE_FILENAMES = (SYSTEM_IMAGE_FILENAME, SYSTEM_IMAGE_QCOW2_FILENAME)

OPTIONAL_IMAGE_FILENAMES = (
    "vendor.img",
    "userdata.img",
    "ramdisk.img",
    "kernel-ranchu",
    "kernel-ranchu-64",
    "encryptionkey.img",
    "build.prop",
    "source.properties",
    "NOTICE.txt",
)
IMPORTABLE_SUFFIXES = (".img", ".qcow2")

ABI_VALUES = ("x86_64", "arm64-v8a", "x86", "armeabi-v7a", "riscv64")

#: Stable image ids are SDK package ids: lowercase, semicolon separated.
IMAGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._;+-]{0,159}$")
API_DIRECTORY_RE = re.compile(r"^android-(\d{1,3})$")
API_PATH_RE = re.compile(r"(?:^|/)android-(\d{1,3})(?:/|$)")
TAG_SANITIZE_RE = re.compile(r"[^a-z0-9_.-]+")
TAG_COLLAPSE_RE = re.compile(r"_{2,}")


class ImageError(AvdFleetError):
    """A system-image operation failed in a way the caller can fix.

    ``core.errors`` does not (yet) define an image-specific error; this subclass
    keeps a stable machine code (``image_error``) and the HTTP 400 status used by
    the ``/api/v1/images`` routes for bad import/download requests while remaining
    an ``AvdFleetError`` for the generic API error envelope.
    """

    code = "image_error"
    http_status = 400


class ImageNotFoundError(NotFoundError, ImageError):
    """Unknown image id.

    The OpenAPI contract requires HTTP 404 for ``/api/v1/images/{image_id}`` (which
    ``NotFoundError`` provides) while callers of the image package may reasonably
    catch ``ImageError``; this subclass satisfies both without touching ``core``.
    """

    code = "not_found"
    http_status = 404


# ---------------------------------------------------------------------------
# Small helpers (shared with ImageService)
# ---------------------------------------------------------------------------


class _WarningSink:
    """Collect non-fatal catalogue problems and keep them visible in logs."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or get_logger("avdfleet.images")
        self.items: list[str] = []

    def add(self, message: str) -> None:
        self.items.append(message)
        self._logger.warning("%s", message)


def package_id_for(api_level: int, tag: str, abi: str) -> str:
    """Return the canonical SDK package id for an image coordinate triple."""
    return f"system-images;android-{int(api_level)};{tag};{abi}"


def parse_properties(path: str | Path) -> dict[str, str]:
    """Parse a Java-style ``key=value`` file (``source.properties``/``build.prop``).

    Missing or unreadable files yield an empty mapping: callers treat metadata as
    optional so a stock-SDK layout without ``build.prop`` still scans.
    """
    candidate = Path(path)
    if not candidate.is_file():
        return {}
    try:
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    properties: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if key:
            properties[key] = value.strip()
    return properties


def primary_image_file(directory: str | Path) -> Path | None:
    """Return the primary disk image in *directory* (raw preferred), if any."""
    folder = Path(directory)
    if not folder.is_dir():
        return None
    for filename in PRIMARY_IMAGE_FILENAMES:
        candidate = folder / filename
        if candidate.is_file() and not _is_link(candidate):
            return candidate
    return None


def atomic_write_json(path: str | Path, payload: Any) -> Path:
    """Write JSON to *path* atomically (same-directory temp file + ``os.replace``).

    The destination is either the old file or the complete new file, never a
    partially written one, which is what the custom registry and OOBE matrix
    require when the daemon can be interrupted at any point.
    """
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=False, default=str) + "\n"
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except OSError:
        _unlink_quietly(temporary)
        raise
    return destination


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - best-effort cleanup
        pass


def _load_json(path: str | Path) -> tuple[Any | None, str | None]:
    """Return ``(data, error)``; ``(None, None)`` means the file does not exist."""
    candidate = Path(path)
    try:
        text = candidate.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"cannot read {candidate}: {exc}"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON in {candidate}: {exc}"


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction()) if callable(is_junction) else False


def image_kind_for_tag(tag: str, *, custom: bool = False) -> ImageKind:
    """Map an SDK image tag to the contract's ``ImageKind`` enum."""
    lowered = (tag or "").strip().lower()
    if custom or "custom" in lowered:
        return ImageKind.CUSTOM
    if "playstore" in lowered:
        return ImageKind.GOOGLE_APIS_PLAYSTORE
    if lowered.startswith("google_apis"):
        return ImageKind.GOOGLE_APIS
    if lowered.startswith(("aosp", "atd")):
        return ImageKind.AOSP
    if lowered.startswith("default"):
        return ImageKind.DEFAULT
    return ImageKind.UNKNOWN


def sanitize_tag(tag: str, *, fallback: str = "avdfleet_custom") -> str:
    """Return a filesystem/package-safe tag, falling back when nothing is left."""
    lowered = (tag or "").strip().lower()
    cleaned = TAG_SANITIZE_RE.sub("_", lowered)
    cleaned = TAG_COLLAPSE_RE.sub("_", cleaned)
    cleaned = cleaned.strip("._-")[:40].strip("._-")
    return cleaned or fallback


def _image_key(image: SystemImage) -> str:
    return (image.package or image.id or "").strip()


def _sort_key(image: SystemImage) -> tuple[int, str, str, str]:
    return (-int(image.api_level), image.tag, image.abi, image.id)


def _sorted_images(images: Any) -> list[SystemImage]:
    return sorted(images, key=_sort_key)


def _int_value(value: str | None) -> int | None:
    if not value:
        return None
    match = re.fullmatch(r"\s*(\d{1,3})\s*", value)
    if match is None:
        return None
    number = int(match.group(1))
    return number if 1 <= number <= 999 else None


def _coerce_image(value: Any) -> SystemImage | None:
    """Validate a backend/JSON image record into ``SystemImage`` (or ``None``)."""
    if isinstance(value, SystemImage):
        return value.model_copy(deep=True)
    if isinstance(value, dict):
        data = dict(value)
        if not data.get("id") and data.get("package"):
            data["id"] = data["package"]
        try:
            return SystemImage.model_validate(data)
        except Exception:
            return None
    return None


def _append_note(notes: str, message: str) -> str:
    if not message or message in notes:
        return notes
    return f"{notes} {message}".strip() if notes else message


def _merge_installed(base: SystemImage, local: SystemImage, *, installed: bool = True) -> SystemImage:
    metadata = {**base.metadata, **local.metadata}
    update: dict[str, Any] = {
        "installed": bool(installed),
        "local_path": local.local_path or base.local_path,
        "metadata": metadata,
    }
    if not installed:
        # Keep the stock/available entry's installed state; only enrich metadata.
        update["local_path"] = base.local_path
    if not base.package:
        update["package"] = local.package
    if not base.display_name:
        update["display_name"] = local.display_name
    if not base.tag:
        update["tag"] = local.tag
    if not base.abi:
        update["abi"] = local.abi
    if not base.api_level:
        update["api_level"] = local.api_level
    if base.kind is ImageKind.UNKNOWN and local.kind is not ImageKind.UNKNOWN:
        update["kind"] = local.kind
    return base.model_copy(update=update)


def _enrich(image: SystemImage) -> SystemImage:
    """Fill contract fields a backend/scan may have left empty."""
    update: dict[str, Any] = {}
    if not image.display_name:
        tag_label = image.tag.replace("_", " ").title() if image.tag else "Android"
        update["display_name"] = f"{tag_label} API {image.api_level} ({image.abi})"
    if image.kind is ImageKind.UNKNOWN and image.tag:
        update["kind"] = image_kind_for_tag(image.tag)
    if not image.package and image.api_level and image.tag and image.abi:
        update["package"] = package_id_for(image.api_level, image.tag, image.abi)
    return image.model_copy(update=update) if update else image


def _sorted_child_directories(path: Path) -> list[Path]:
    try:
        children = list(path.iterdir())
    except OSError:
        return []
    return sorted((child for child in children if child.is_dir()), key=lambda child: child.name)


# ---------------------------------------------------------------------------
# ImageCatalogue
# ---------------------------------------------------------------------------


class ImageCatalogue:
    """Offline catalogue: stock JSON snapshot, installed SDK dirs, custom registry.

    Parameters
    ----------
    data_dir:
        A :class:`~avdfleet.core.paths.Paths` instance (preferred) or the data
        directory itself.  ``custom_images.json`` lives directly in it.
    sdk_root:
        Android SDK root to scan; defaults to ``Paths.sdk_root`` when a
        ``Paths`` instance is supplied.
    catalogue_path:
        Override for the checked-in stock catalogue (used by tests/fakes).
    custom_registry_path:
        Override for the custom-image registry JSON.
    sdk:
        Optional duck-typed ``SdkBackend``: when it exposes
        ``list_system_images()`` that method is preferred over directory scanning.
    """

    def __init__(
        self,
        data_dir: Paths | str | Path,
        *,
        sdk_root: str | Path | None = None,
        catalogue_path: str | Path | None = None,
        custom_registry_path: str | Path | None = None,
        sdk: Any | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if isinstance(data_dir, Paths):
            base_dir = Path(data_dir.data_dir or data_dir.root)
            resolved_sdk_root = sdk_root if sdk_root is not None else data_dir.sdk_root
        else:
            base_dir = Path(data_dir).expanduser()
            resolved_sdk_root = sdk_root
        self.data_dir = base_dir
        self.sdk_root = Path(resolved_sdk_root).expanduser() if resolved_sdk_root else None
        self.catalogue_path = (
            Path(catalogue_path).expanduser() if catalogue_path is not None else DEFAULT_CATALOGUE_PATH
        )
        self.custom_registry_path = (
            Path(custom_registry_path).expanduser()
            if custom_registry_path is not None
            else self.data_dir / CUSTOM_REGISTRY_FILENAME
        )
        self.sdk = sdk
        self._logger = logger or get_logger("avdfleet.images.catalogue")
        self._sink = _WarningSink(self._logger)

    # -- diagnostics --------------------------------------------------------
    @property
    def warnings(self) -> list[str]:
        return list(self._sink.items)

    # -- stock catalogue ----------------------------------------------------
    def stock_images(self) -> list[SystemImage]:
        """Read the checked-in stock catalogue (offline, no downloads)."""
        data, error = _load_json(self.catalogue_path)
        if error:
            self._sink.add(f"stock image catalogue is unreadable: {error}")
            return []
        if data is None:
            self._sink.add(f"stock image catalogue is missing: {self.catalogue_path}")
            return []
        if isinstance(data, dict):
            entries = data.get("images", [])
            if not isinstance(entries, list):
                self._sink.add(f"stock image catalogue has no image list: {self.catalogue_path}")
                return []
        elif isinstance(data, list):
            entries = data
        else:
            self._sink.add(f"stock image catalogue has an unexpected shape: {self.catalogue_path}")
            return []
        images: list[SystemImage] = []
        for index, entry in enumerate(entries):
            image = _coerce_image(entry)
            if image is None or not image.id.strip():
                self._sink.add(f"ignoring invalid stock image record #{index} in {self.catalogue_path.name}")
                continue
            images.append(_enrich(image))
        return _sorted_images(images)

    # -- installed SDK images ----------------------------------------------
    def scan_installed(self, *, sdk_root: str | Path | None = None) -> list[SystemImage]:
        """Scan ``<sdk_root>/system-images/android-<api>/<tag>/<abi>/`` layouts."""
        root = Path(sdk_root).expanduser() if sdk_root is not None else self.sdk_root
        if root is None:
            return []
        base = Path(root) / "system-images"
        if not base.is_dir():
            return []
        images: list[SystemImage] = []
        for api_dir in _sorted_child_directories(base):
            match = API_DIRECTORY_RE.match(api_dir.name)
            if match is None:
                self._sink.add(f"ignoring unexpected directory under system-images: {api_dir.name}")
                continue
            api_from_path = int(match.group(1))
            for tag_dir in _sorted_child_directories(api_dir):
                for abi_dir in _sorted_child_directories(tag_dir):
                    primary = primary_image_file(abi_dir)
                    if primary is None:
                        # Empty/partial directories are normal while sdkmanager is
                        # filling them; only report directories that have files.
                        if any(abi_dir.iterdir()):
                            self._sink.add(f"ignoring system image without system.img: {abi_dir}")
                        continue
                    properties = parse_properties(abi_dir / "source.properties")
                    api_level = _int_value(_first_property(properties, "AndroidVersion.ApiLevel"))
                    api_level = api_level if api_level is not None else api_from_path
                    tag = _first_property(properties, "SystemImage.TagId") or tag_dir.name
                    abi = _first_property(properties, "SystemImage.Abi") or abi_dir.name
                    if abi not in ABI_VALUES:
                        self._sink.add(f"ignoring system image with unsupported ABI {abi!r}: {abi_dir}")
                        continue
                    if not tag:
                        self._sink.add(f"ignoring system image without a tag: {abi_dir}")
                        continue
                    package = package_id_for(api_level, tag, abi)
                    images.append(
                        SystemImage(
                            id=package,
                            api_level=api_level,
                            tag=tag,
                            abi=abi,
                            kind=image_kind_for_tag(tag),
                            display_name=_display_name_from_properties(properties, tag, api_level, abi),
                            package=package,
                            installed=True,
                            local_path=str(abi_dir),
                            metadata={
                                "source": "sdk-scan",
                                "primary_file": primary.name,
                                "source_properties": properties,
                            },
                        )
                    )
        return images

    def installed_images(self) -> list[SystemImage]:
        """Installed images: SDK backend when available, then filesystem evidence."""
        scanned = {_image_key(image): image for image in self.scan_installed() if _image_key(image)}
        backend_images = self._backend_images()
        if backend_images is None:
            return _sorted_images(scanned.values())

        merged: dict[str, SystemImage] = {}
        for image in backend_images:
            key = _image_key(image)
            if not key:
                self._sink.add("ignoring SDK backend image without id or package")
                continue
            # A backend may return the full available catalogue (explicit
            # installed=False) or only installed images (field omitted).
            if "installed" not in image.model_fields_set:
                image = image.model_copy(update={"installed": True})
            local = scanned.get(key) or _match_scanned(scanned.values(), image)
            if local is not None:
                image = _merge_installed(image, local)
            image = _enrich(image)
            image = image.model_copy(update={"installed": bool(image.installed)})
            merged[_image_key(image)] = image
        for key, image in scanned.items():
            merged.setdefault(key, _enrich(image))
        return _sorted_images(merged.values())

    def _backend_images(self) -> list[SystemImage] | None:
        backend = self.sdk
        if backend is None:
            return None
        lister = getattr(backend, "list_system_images", None)
        if not callable(lister):
            return None
        try:
            raw = lister()
        except AvdFleetError as exc:
            self._sink.add(
                f"SDK backend cannot list system images ({exc.message}); falling back to scanning {self.sdk_root}"
            )
            return None
        except Exception as exc:
            self._sink.add(
                f"SDK backend cannot list system images ({exc!r}); falling back to scanning {self.sdk_root}"
            )
            return None
        if raw is None:
            return []
        images: list[SystemImage] = []
        for index, item in enumerate(raw):
            image = _coerce_image(item)
            if image is None:
                self._sink.add(f"ignoring invalid SDK backend image record #{index}")
                continue
            images.append(image)
        return images

    # -- custom registry ----------------------------------------------------
    def custom_images(self) -> list[SystemImage]:
        """Read ``custom_images.json`` from the data directory."""
        raw_entries = self._registry_entries()
        images: list[SystemImage] = []
        for index, entry in enumerate(raw_entries):
            image = _coerce_image(entry)
            if image is None or not image.id.strip():
                self._sink.add(
                    f"ignoring invalid custom image record #{index} in {self.custom_registry_path.name}"
                )
                continue
            images.append(self._custom_record_state(image))
        return _sorted_images(images)

    def register_custom_image(self, image: SystemImage, *, force: bool = False) -> SystemImage:
        """Persist a custom image record atomically; refuses duplicates without *force*."""
        record = image.model_copy(update={"custom": True})
        record_id = (record.id or record.package or "").strip()
        if not record_id:
            raise ImageError("custom image record requires an id or package")
        if not record.id:
            record = record.model_copy(update={"id": record_id})
        existing_entries = self._registry_entries()
        existing = [candidate for candidate in (_coerce_image(entry) for entry in existing_entries) if candidate]
        conflict = _find_conflict(existing, record)
        if conflict is not None and not force:
            raise ImageError(
                f"custom image {record_id!r} is already registered",
                hint="pass force=True to replace the existing registration",
                details={"existing_id": conflict.id},
            )
        kept: list[dict[str, Any]] = []
        for entry in existing_entries:
            candidate = _coerce_image(entry)
            if candidate is not None and _find_conflict([candidate], record) is not None:
                continue
            if isinstance(entry, dict):
                kept.append(dict(entry))
        kept.append(record.model_dump(mode="json", exclude_none=True))
        atomic_write_json(
            self.custom_registry_path,
            {
                "version": CUSTOM_REGISTRY_VERSION,
                "updated_at": utcnow().isoformat(),
                "images": kept,
            },
        )
        return record

    def _registry_entries(self) -> list[Any]:
        data, error = _load_json(self.custom_registry_path)
        if error:
            self._sink.add(f"custom image registry is unreadable: {error}")
            return []
        if data is None:
            return []
        if isinstance(data, dict):
            entries = data.get("images", [])
        elif isinstance(data, list):
            entries = data
        else:
            self._sink.add(f"custom image registry has an unexpected shape: {self.custom_registry_path}")
            return []
        if not isinstance(entries, list):
            self._sink.add(f"custom image registry has no image list: {self.custom_registry_path}")
            return []
        return entries

    def _custom_record_state(self, image: SystemImage) -> SystemImage:
        local = Path(image.local_path).expanduser() if image.local_path else None
        installed = local is not None and local.is_dir() and primary_image_file(local) is not None
        notes = image.notes
        if not installed:
            notes = _append_note(
                notes,
                (
                    f"registered image directory is missing or incomplete: {local}"
                    if local is not None
                    else "no local image directory is registered"
                ),
            )
        kind = image.kind if image.kind is not ImageKind.UNKNOWN else image_kind_for_tag(image.tag, custom=True)
        return image.model_copy(
            update={
                "custom": True,
                "installed": installed,
                "notes": notes,
                "kind": kind,
            }
        )

    # -- merged view --------------------------------------------------------
    def all_images(self) -> list[SystemImage]:
        """Merge stock catalogue, installed images and the custom registry.

        Precedence: stock metadata < installed SDK evidence < custom registry.
        """
        merged: dict[str, SystemImage] = {}
        for image in self.stock_images():
            merged[_image_key(image)] = image
        for image in self.installed_images():
            key = _image_key(image)
            existing = merged.get(key)
            if existing is not None:
                merged[key] = _merge_installed(existing, image, installed=image.installed)
            else:
                # Backend-only available entries are still useful in the full
                # catalogue even when they are not installed.
                merged[key] = image
        for image in self.custom_images():
            # A custom record can share the SDK package id with a scanned/stock
            # entry (e.g. a re-imported Google image); the custom record wins and
            # keeps its own explicit id.
            for key, candidate in list(merged.items()):
                if image.package and candidate.package == image.package:
                    merged.pop(key, None)
            merged[(image.id or image.package).strip()] = image
        return _sorted_images(merged.values())

    def reload(self) -> None:
        """Re-read sources on next access.

        The catalogue is stateless (JSON/directories are consulted per call), so
        this is intentionally a no-op kept for API symmetry with ``refresh()``.
        """


def _first_property(properties: dict[str, str], *names: str) -> str | None:
    """Case-insensitive ``key=value`` lookup, first non-empty match wins."""
    lowered = {key.lower(): value for key, value in properties.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value:
            return value
    return None


def _display_name_from_properties(
    properties: dict[str, str], tag: str, api_level: int, abi: str
) -> str:
    tag_display = _first_property(properties, "SystemImage.TagDisplay")
    label = tag_display or tag.replace("_", " ").title()
    return f"{label} API {api_level} ({abi})"


def _match_scanned(candidates: Any, image: SystemImage) -> SystemImage | None:
    package = image.package.strip()
    coordinate = (image.api_level, image.tag, image.abi)
    for candidate in candidates:
        if package and candidate.package == package:
            return candidate
        if candidate.api_level and (candidate.api_level, candidate.tag, candidate.abi) == coordinate:
            return candidate
    return None


def _find_conflict(existing: list[SystemImage], record: SystemImage) -> SystemImage | None:
    record_id = (record.id or "").strip()
    record_package = (record.package or "").strip()
    for candidate in existing:
        if record_id and candidate.id == record_id:
            return candidate
        if record_package and candidate.package == record_package:
            return candidate
    return None


# ---------------------------------------------------------------------------
# OOBE matrix persistence
# ---------------------------------------------------------------------------


class OobeMatrixStore:
    """Merge the shipped OOBE seed with the operator's persisted matrix.

    The seed file marks every real-world record ``unverified`` (this repository
    has no accelerated emulator; see BRIEF §6 / PLAN.md).  Records persisted by a
    real verification run override the seed entry with the same ``image_id``.
    """

    def __init__(
        self,
        matrix_path: str | Path,
        *,
        seed_path: str | Path | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.matrix_path = Path(matrix_path).expanduser()
        self.seed_path = (
            Path(seed_path).expanduser() if seed_path is not None else DEFAULT_SEED_MATRIX_PATH
        )
        self._logger = logger or get_logger("avdfleet.images.oobe")
        self._sink = _WarningSink(self._logger)

    @property
    def warnings(self) -> list[str]:
        return list(self._sink.items)

    def load(self) -> OobeMatrix:
        records: dict[str, OobeRecord] = {}
        for record in self._seed_records():
            if record.image_id:
                records[record.image_id] = record
        persisted = self._persisted_matrix()
        version = "1"
        host_abi = "x86_64"
        generated_at = None
        if persisted is not None:
            version = persisted.version or version
            host_abi = persisted.host_abi or host_abi
            generated_at = persisted.generated_at
            for record in persisted.records:
                if record.image_id:
                    records[record.image_id] = record
        return OobeMatrix(
            version=version,
            generated_at=generated_at,
            host_abi=host_abi,
            records=_sorted_records(records.values()),
        )

    def update(self, record: OobeRecord) -> OobeMatrix:
        if not (record.image_id or "").strip():
            raise ImageError("OOBE record requires a non-empty image_id")
        matrix = self.load()
        records = {existing.image_id: existing for existing in matrix.records if existing.image_id}
        previous = records.get(record.image_id)
        if (
            previous is not None
            and previous.status is OobeStatus.VERIFIED
            and record.status is not OobeStatus.VERIFIED
        ):
            # A verified image must not lose its capability evidence because a
            # later boot observed an already-provisioned AVD.  The Setup Wizard
            # only appears on pristine userdata, so "launcher visible" describes
            # the AVD's data state, not the image's ability to reach OOBE.
            record = previous.model_copy(deep=True)
        records[record.image_id] = record.model_copy(deep=True)
        updated = OobeMatrix(
            version=matrix.version or "1",
            generated_at=utcnow(),
            host_abi=matrix.host_abi,
            records=_sorted_records(records.values()),
        )
        try:
            atomic_write_json(self.matrix_path, updated.model_dump(mode="json"))
        except OSError as exc:
            raise ImageError(
                f"cannot persist OOBE matrix at {self.matrix_path}: {exc}",
                hint="check that the avdfleet data directory is writable",
            ) from exc
        return updated

    def _seed_records(self) -> list[OobeRecord]:
        data, error = _load_json(self.seed_path)
        if error:
            self._sink.add(f"OOBE seed matrix is unreadable: {error}")
            return []
        if data is None:
            self._sink.add(f"OOBE seed matrix is missing: {self.seed_path}")
            return []
        matrix = self._validate(data, self.seed_path)
        if matrix is None:
            return []
        for record in matrix.records:
            if record.status is OobeStatus.VERIFIED and not record.evidence:
                record.status = OobeStatus.UNVERIFIED
                record.notes = _append_note(record.notes, "verified status has no evidence; treated as unverified")
        return list(matrix.records)

    def _persisted_matrix(self) -> OobeMatrix | None:
        data, error = _load_json(self.matrix_path)
        if error:
            self._sink.add(f"persisted OOBE matrix is unreadable ({error}); using the seed matrix only")
            return None
        if data is None:
            return None
        return self._validate(data, self.matrix_path)

    def _validate(self, data: Any, source: Path) -> OobeMatrix | None:
        try:
            return OobeMatrix.model_validate(data)
        except Exception as exc:
            self._sink.add(f"ignoring invalid OOBE matrix {source}: {exc}")
            return None


def _sorted_records(records: Any) -> list[OobeRecord]:
    return sorted(
        records,
        key=lambda record: (int(record.api_level), record.tag, record.abi, record.image_id),
    )


__all__ = [
    "ABI_VALUES",
    "CUSTOM_REGISTRY_FILENAME",
    "CUSTOM_REGISTRY_VERSION",
    "DEFAULT_CATALOGUE_PATH",
    "DEFAULT_MAX_IMPORT_BYTES",
    "DEFAULT_SEED_MATRIX_PATH",
    "IMPORTABLE_SUFFIXES",
    "ImageCatalogue",
    "ImageError",
    "ImageNotFoundError",
    "OobeMatrixStore",
    "OPTIONAL_IMAGE_FILENAMES",
    "PRIMARY_IMAGE_FILENAMES",
    "SYSTEM_IMAGE_FILENAME",
    "SYSTEM_IMAGE_QCOW2_FILENAME",
    "atomic_write_json",
    "image_kind_for_tag",
    "package_id_for",
    "parse_properties",
    "primary_image_file",
    "sanitize_tag",
]
