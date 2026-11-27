"""AVD template persistence, validation and device-profile discovery.

A *template* is the reusable recipe for one or more instances: system image,
device profile, RAM/CPU/heap, GPU mode, headless default, optional proxy and
extra ``config.ini`` keys.  This module owns:

* :class:`TemplateStore` -- persistence through an injected store repository
  (``store.templates`` with ``list/get/get_by_name/upsert/delete``) or, for
  standalone use, an atomic JSON file at ``Paths.data_dir/templates.json``;
* :class:`TemplateService` -- CRUD with validation (image exists in the
  catalogue, device id is a known profile, RAM/heap/CPU within safe bounds,
  unique name, timestamps) and device-profile discovery from the SDK backend
  with a built-in fallback catalogue;
* a deterministic default device-profile list used when no backend advertises
  ``avdmanager list device`` output (keeps the GUI usable before an SDK is
  downloaded).

The store adapter is deliberately duck typed so A1 can be developed and tested
against a fake repository (and against JSON) without importing ``store/``.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from avdfleet.core.errors import ConflictError, NotFoundError, ValidationError
from avdfleet.core.logging import get_logger
from avdfleet.core.models import DeviceProfile, GpuMode, Template, slugify, utcnow
from avdfleet.core.paths import Paths

from .configini import ConfigValidationError, validate_config_values

logger = get_logger("platform.avd.templates")

#: Type alias: ``TemplateStore`` defines a ``list`` method, so bare ``list[...]``
#: annotations inside the class would resolve to that method for type checkers.
TemplateList = list[Template]

TEMPLATES_FILENAME = "templates.json"
TEMPLATES_FORMAT_VERSION = "1"

MIN_RAM_MB = 512
MAX_RAM_MB = 65536
MIN_HEAP_MB = 16
MAX_HEAP_MB = 4096
MIN_CPU_CORES = 1
MAX_CPU_CORES = 16
MAX_NAME_LENGTH = 80
MAX_ID_LENGTH = 63
MAX_DESCRIPTION_LENGTH = 2000

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_RESOLUTION_RE = re.compile(r"^(\d{2,5})\s*[xX]\s*(\d{2,5})$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Device profiles used when the SDK backend does not advertise its own list.
DEFAULT_DEVICE_PROFILES: tuple[DeviceProfile, ...] = (
    DeviceProfile(id="pixel_7", name="Pixel 7", resolution="1080x2400", density=420, ram_mb=2048),
    DeviceProfile(id="pixel_7_pro", name="Pixel 7 Pro", resolution="1440x3120", density=560, ram_mb=3072),
    DeviceProfile(id="pixel_6", name="Pixel 6", resolution="1080x2400", density=420, ram_mb=2048),
    DeviceProfile(id="pixel_6a", name="Pixel 6a", resolution="1080x2400", density=420, ram_mb=2048),
    DeviceProfile(id="pixel_5", name="Pixel 5", resolution="1080x2340", density=440, ram_mb=2048),
    DeviceProfile(id="pixel_4", name="Pixel 4", resolution="1080x2280", density=440, ram_mb=2048),
    DeviceProfile(id="pixel_9", name="Pixel 9", resolution="1080x2424", density=420, ram_mb=4096),
    DeviceProfile(id="pixel_9_pro", name="Pixel 9 Pro", resolution="1280x2856", density=480, ram_mb=4096),
    DeviceProfile(id="pixel_tablet", name="Pixel Tablet", resolution="2560x1600", density=320, ram_mb=4096),
    DeviceProfile(id="Nexus 5X", name="Nexus 5X", resolution="1080x1920", density=420, ram_mb=2048),
    DeviceProfile(id="Nexus 6P", name="Nexus 6P", resolution="1440x2560", density=560, ram_mb=3072),
    DeviceProfile(id="medium_phone", name="Medium Phone", resolution="1080x2400", density=420, ram_mb=2048),
    DeviceProfile(id="small_phone", name="Small Phone", resolution="720x1280", density=320, ram_mb=1536),
    DeviceProfile(id="tablet", name="Tablet", resolution="1280x800", density=213, ram_mb=2048),
)


# ---------------------------------------------------------------------------
# Store adapter
# ---------------------------------------------------------------------------


def _looks_like_template_repo(candidate: Any) -> bool:
    return candidate is not None and all(
        callable(getattr(candidate, method, None))
        for method in ("list", "get", "get_by_name", "upsert", "delete")
    )


class TemplateStore:
    """Template persistence: injected repository, or ``templates.json`` fallback.

    Accepted injected stores::

        store.templates                      # a Store/Repository container
        store                                # the repository itself
        {"templates": repo}?                 # mapping form
    """

    def __init__(
        self,
        store: Any | None = None,
        *,
        paths: Paths | None = None,
        filename: str = TEMPLATES_FILENAME,
    ) -> None:
        self._repo = self._resolve_repo(store)
        self._paths = paths
        self._filename = filename
        self._lock = threading.RLock()

    @staticmethod
    def _resolve_repo(store: Any | None) -> Any | None:
        if store is None:
            return None
        if isinstance(store, Mapping):
            candidate = store.get("templates")
            if _looks_like_template_repo(candidate):
                return candidate
            raise ValidationError(
                "injected template store mapping has no usable 'templates' repository"
            )
        candidate = getattr(store, "templates", None)
        if _looks_like_template_repo(candidate):
            return candidate
        if _looks_like_template_repo(store):
            return store
        raise ValidationError(
            "injected store does not expose a templates repository "
            "(expected .templates with list/get/get_by_name/upsert/delete)",
            hint="Pass a store repository, or omit the store to use the JSON fallback.",
        )

    @property
    def backing(self) -> str:
        return "store" if self._repo is not None else "json"

    @property
    def path(self) -> Path:
        paths = self._paths
        if paths is None:
            paths = Paths.discover()
            self._paths = paths
        data_dir = paths.data_dir
        if data_dir is None:  # pragma: no cover - Paths always sets data_dir
            raise ValidationError("the avdfleet data directory is not configured")
        return Path(data_dir) / self._filename

    # -- CRUD -----------------------------------------------------------
    def list(self) -> TemplateList:
        if self._repo is not None:
            return list(self._repo.list() or [])
        with self._lock:
            return self._read_json()

    def get(self, template_id: str) -> Template:
        template_id = str(template_id or "").strip()
        if not template_id:
            raise ValidationError("template id must not be empty")
        if self._repo is not None:
            return self._repo.get(template_id)
        with self._lock:
            for template in self._read_json():
                if template.id == template_id:
                    return template
        raise NotFoundError(f"template {template_id!r} was not found")

    def get_by_name(self, name: str) -> Template | None:
        candidate = str(name or "").strip()
        if not candidate:
            return None
        if self._repo is not None:
            getter = getattr(self._repo, "get_by_name", None)
            if callable(getter):
                return getter(candidate)
            for template in self._repo.list() or []:
                if template.name == candidate:
                    return template
            return None
        with self._lock:
            matches = [item for item in self._read_json() if item.name == candidate]
        return matches[0] if matches else None

    def upsert(self, template: Template) -> Template:
        if self._repo is not None:
            saved = self._repo.upsert(template)
            return saved if isinstance(saved, Template) else template
        with self._lock:
            templates: TemplateList = self._read_json()
            for index, existing in enumerate(templates):
                if existing.id == template.id:
                    templates[index] = template
                    break
            else:
                templates.append(template)
            self._write_json(templates)
        return template

    def delete(self, template_id: str) -> None:
        template_id = str(template_id or "").strip()
        if not template_id:
            raise ValidationError("template id must not be empty")
        if self._repo is not None:
            self._repo.delete(template_id)
            return
        with self._lock:
            templates: TemplateList = self._read_json()
            kept = [template for template in templates if template.id != template_id]
            if len(kept) == len(templates):
                raise NotFoundError(f"template {template_id!r} was not found")
            self._write_json(kept)

    # -- JSON fallback ---------------------------------------------------
    def _read_json(self) -> TemplateList:
        target = self.path
        try:
            raw = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("could not read template store %s: %s", target, exc)
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("template store %s is corrupt; treating it as empty", target)
            return []
        entries: Any
        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict):
            entries = data.get("templates") or []
        else:
            entries = []
        templates: list[Template] = []
        for entry in entries if isinstance(entries, list) else []:
            try:
                templates.append(Template.model_validate(entry))
            except Exception as exc:  # noqa: BLE001 - one bad row must not hide the rest
                logger.warning("skipping invalid template entry in %s: %s", target, exc)
        return templates

    def _write_json(self, templates: Iterable[Template]) -> None:
        target = self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": TEMPLATES_FORMAT_VERSION,
            "updated_at": utcnow().isoformat(),
            "templates": [template.model_dump(mode="json") for template in templates],
        }
        temporary = target.with_name(f"{target.name}.tmp-{os.getpid()}")
        try:
            temporary.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, target)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ValidationError(
                f"could not write template store {target}: {exc}",
                hint="Check that the avdfleet data directory is writable.",
            ) from exc


# ---------------------------------------------------------------------------
# Template service
# ---------------------------------------------------------------------------


class TemplateService:
    """Create, validate, persist and delete :class:`Template` records."""

    def __init__(
        self,
        *,
        store: Any | None = None,
        paths: Paths | None = None,
        images: Any | None = None,
        backend: Any | None = None,
        device_profiles: Any | None = None,
        clock: Callable[[], datetime] = utcnow,
        strict_devices: bool = True,
    ) -> None:
        self.templates = TemplateStore(store, paths=paths)
        self.images = images
        self.backend = backend
        self._device_profiles_source = device_profiles
        self._clock = clock
        self.strict_devices = strict_devices
        self._lock = threading.RLock()

    # -- templates ------------------------------------------------------
    def list_templates(self) -> list[Template]:
        return sorted(self.templates.list(), key=lambda item: (item.name.lower(), item.id))

    def get_template(self, template_id: str) -> Template:
        return self.templates.get(template_id)

    def get_template_by_name(self, name: str) -> Template | None:
        return self.templates.get_by_name(name)

    def save_template(self, template: Template) -> Template:
        """Validate and upsert a template, preserving its original ``created_at``."""
        validated = self.validate_template(template)
        with self._lock:
            existing_by_name = self.templates.get_by_name(validated.name)
            if existing_by_name is not None and existing_by_name.id != validated.id:
                raise ConflictError(
                    f"a template named {validated.name!r} already exists",
                    hint="Choose a unique template name or update the existing template.",
                )
            existing: Template | None
            try:
                existing = self.templates.get(validated.id)
            except (NotFoundError, KeyError):
                existing = None
            now = self._clock()
            created_at = (
                existing.created_at
                if existing is not None and existing.created_at is not None
                else (validated.created_at or now)
            )
            saved = validated.model_copy(update={"created_at": created_at, "updated_at": now})
            result = self.templates.upsert(saved)
        logger.info("saved AVD template %s (%s)", saved.id, saved.name)
        return result

    def delete_template(self, template_id: str) -> None:
        template = self.templates.get(template_id)
        with self._lock:
            self.templates.delete(template.id)
        logger.info("deleted AVD template %s", template.id)

    # -- validation -----------------------------------------------------
    def validate_template(self, template: Template) -> Template:
        if not isinstance(template, Template):
            raise ValidationError("expected an AvdTemplate model")
        errors: list[str] = []
        name = (template.name or "").strip()
        if not name:
            errors.append("template name must not be empty")
        elif len(name) > MAX_NAME_LENGTH:
            errors.append(f"template name must be at most {MAX_NAME_LENGTH} characters")
        elif _CONTROL_RE.search(name):
            errors.append("template name must not contain control characters")

        template_id = (template.id or "").strip()
        if not template_id:
            template_id = slugify(name or "template", max_length=MAX_ID_LENGTH)
        if not _ID_RE.match(template_id):
            errors.append(
                f"template id {template_id!r} is invalid: use letters, digits, '.', '_' and '-'"
            )

        image_id = (template.image_id or "").strip()
        if not image_id:
            errors.append("template image_id must not be empty")
        elif _CONTROL_RE.search(image_id) or any(character.isspace() for character in image_id):
            errors.append(f"template image_id {image_id!r} is not a valid image id")

        if not MIN_RAM_MB <= int(template.ram_mb) <= MAX_RAM_MB:
            errors.append(f"ram_mb must be between {MIN_RAM_MB} and {MAX_RAM_MB}")
        if not MIN_HEAP_MB <= int(template.heap_mb) <= MAX_HEAP_MB:
            errors.append(f"heap_mb must be between {MIN_HEAP_MB} and {MAX_HEAP_MB}")
        if not MIN_CPU_CORES <= int(template.cpu_cores) <= MAX_CPU_CORES:
            errors.append(f"cpu_cores must be between {MIN_CPU_CORES} and {MAX_CPU_CORES}")

        gpu_mode = template.gpu_mode
        if not isinstance(gpu_mode, GpuMode):
            try:
                gpu_mode = GpuMode(str(gpu_mode))
            except ValueError:
                errors.append(
                    f"gpu_mode must be one of {', '.join(mode.value for mode in GpuMode)}"
                )

        resolution = (template.resolution or "").strip()
        if resolution and not _RESOLUTION_RE.match(resolution):
            errors.append("resolution must look like 1080x2400")
        if template.density is not None and not 120 <= int(template.density) <= 1000:
            errors.append("density must be between 120 and 1000")
        if template.proxy_id is not None and not str(template.proxy_id).strip():
            errors.append("proxy_id must not be empty when supplied")
        if len(template.description or "") > MAX_DESCRIPTION_LENGTH:
            errors.append(f"description must be at most {MAX_DESCRIPTION_LENGTH} characters")

        device_id = (template.device_id or "").strip()
        canonical_device = self._canonical_device(device_id, errors)
        config: dict[str, str] = {}
        try:
            config = validate_config_values(dict(template.config or {}))
        except ConfigValidationError as exc:
            errors.extend(exc.errors)

        if errors:
            raise ValidationError("; ".join(errors), details={"errors": errors})

        # Validate the image exists in the catalogue (when one is available).
        if image_id:
            self.resolve_image(image_id)

        normalised = template.model_dump()
        normalised.update(
            {
                "id": template_id,
                "name": name,
                "image_id": image_id,
                "device_id": canonical_device or device_id,
                "resolution": resolution,
                "gpu_mode": gpu_mode,
                "proxy_id": str(template.proxy_id).strip() if template.proxy_id else None,
                "scripts": [str(item).strip() for item in template.scripts if str(item).strip()],
                "tags": [str(item).strip() for item in template.tags if str(item).strip()],
                "config": config,
                "description": (template.description or "").strip(),
            }
        )
        try:
            return Template.model_validate(normalised)
        except Exception as exc:  # noqa: BLE001 - pydantic error converted to API error
            raise ValidationError(f"invalid template: {exc}") from exc

    def resolve_image(self, image_id: str) -> Any | None:
        """Return the catalogue image or ``None`` when no catalogue is configured.

        Raises :class:`ValidationError` when a catalogue is configured and the
        image id is unknown - a template pointing at a deleted image is useless.
        """
        if self.images is None:
            return None
        getter = getattr(self.images, "get_image", None)
        if callable(getter):
            try:
                return getter(image_id)
            except NotFoundError as exc:
                raise ValidationError(
                    f"system image {image_id!r} is not available",
                    hint="Download or import the image first, then retry.",
                ) from exc
            except (KeyError, IndexError) as exc:
                raise ValidationError(f"system image {image_id!r} is not available") from exc
        lister = getattr(self.images, "list_images", None)
        if callable(lister):
            for image in lister() or []:
                if getattr(image, "id", None) == image_id:
                    return image
            raise ValidationError(
                f"system image {image_id!r} is not available",
                hint="Refresh the image catalogue or download the image.",
            )
        return None

    # -- device profiles ------------------------------------------------
    def list_device_profiles(self) -> list[DeviceProfile]:
        raw = self._device_profiles_source
        if raw is None and self.backend is not None:
            getter = getattr(self.backend, "list_device_profiles", None)
            if callable(getter):
                try:
                    raw = getter()
                except Exception as exc:  # noqa: BLE001 - optional probe
                    logger.debug("SDK backend device list failed: %s", exc)
                    raw = None
        if raw is None:
            raw = DEFAULT_DEVICE_PROFILES
        elif callable(raw):
            raw = raw()
        profiles = [self._coerce_profile(item) for item in raw or []]
        return sorted((profile for profile in profiles if profile is not None), key=lambda item: item.id)

    def _coerce_profile(self, item: Any) -> DeviceProfile | None:
        if isinstance(item, DeviceProfile):
            return item
        if isinstance(item, Mapping):
            identifier = str(item.get("id") or item.get("name") or "").strip()
            if not identifier:
                return None
            return DeviceProfile(
                id=identifier,
                name=str(item.get("name") or identifier),
                resolution=str(item.get("resolution") or "1080x1920"),
                density=int(item.get("density") or 420),
                ram_mb=int(item.get("ram_mb") or 2048),
                tags=[str(tag) for tag in item.get("tags") or []],
            )
        return None

    def _canonical_device(self, device_id: str, errors: list[str]) -> str:
        candidate = (device_id or "").strip()
        if not candidate:
            errors.append("device_id must not be empty")
            return ""
        if _CONTROL_RE.search(candidate):
            errors.append("device_id must not contain control characters")
            return candidate
        profiles = self.list_device_profiles()
        if not profiles:
            return candidate
        identifiers = {profile.id: profile.id for profile in profiles}
        for profile in profiles:
            identifiers.setdefault(profile.name.lower(), profile.id)
            identifiers.setdefault(profile.id.lower(), profile.id)
        canonical = identifiers.get(candidate) or identifiers.get(candidate.lower())
        if canonical is None:
            if self.strict_devices:
                hint = ", ".join(profile.id for profile in profiles[:8])
                errors.append(f"unknown device profile {candidate!r} (known: {hint}, ...)")
            return candidate
        return canonical


__all__ = [
    "DEFAULT_DEVICE_PROFILES",
    "MAX_CPU_CORES",
    "MAX_HEAP_MB",
    "MAX_RAM_MB",
    "MIN_CPU_CORES",
    "MIN_HEAP_MB",
    "MIN_RAM_MB",
    "TEMPLATES_FILENAME",
    "TemplateService",
    "TemplateStore",
]
