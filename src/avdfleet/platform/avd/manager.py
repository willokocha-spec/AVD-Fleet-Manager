"""``AvdManagerService`` - AVD templates, instances, cloning and YAML I/O.

Implements :class:`avdfleet.core.interfaces.AvdManager`.  The service is the
definition-side authority for the fleet: it never launches an emulator (the
runtime service does) and it never talks to the Android tools directly - every
external action goes through the injected
:class:`avdfleet.core.interfaces.SdkBackend`.

Persistence strategy:

* **templates** - an injected store repository (``store.templates``) when one is
  available, otherwise ``Paths.data_dir/templates.json`` via
  :class:`~avdfleet.platform.avd.templates.TemplateStore`;
* **instances** - the backend AVD directory (``config.ini``) is the source of
  truth for names/config; the optional store ``instances`` repository carries
  avdfleet metadata (template id, image id, ports, proxy) and is mirrored in an
  in-memory map when no repository is injected.

All mutating operations validate their complete input before touching a file or
the SDK, so a rejected request is side-effect free (except for the creation
primitives that the SDK itself performs).  Every destructive operation supports
``dry_run`` where the interface allows it.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from avdfleet.core.config import Config
from avdfleet.core.errors import (
    AvdFleetError,
    CapacityError,
    ConfigError,
    ConflictError,
    InstanceError,
    NotFoundError,
    ValidationError,
)
from avdfleet.core.events import EventBus, EventSink, EventType
from avdfleet.core.logging import get_logger
from avdfleet.core.models import (
    AvdInfo,
    DeviceProfile,
    InstanceState,
    Template,
    slugify,
    utcnow,
)
from avdfleet.core.paths import Paths

from .configini import (
    ConfigIni,
    ConfigIniIssue,
    ConfigValidationError,
    apply_config_overrides,
    avd_dir_for,
    config_path_for,
    detect_config_issues,
    ini_path_for,
    load_values,
    render_template_config,
    validate_config_values,
)
from .templates import TemplateService
from .yamlio import export_avd_yaml, parse_avd_yaml

logger = get_logger("platform.avd.manager")

_AVD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$")

#: Overrides accepted by ``create_instance``/``bulk_deploy``.
OVERRIDE_KEYS: frozenset[str] = frozenset(
    {
        "ram_mb",
        "heap_mb",
        "cpu_cores",
        "gpu_mode",
        "headless",
        "device_id",
        "image_id",
        "proxy_id",
        "config",
        "resolution",
        "density",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pid_alive(pid: int | None) -> bool:
    """Cross-platform "does this PID exist" probe used to reconcile stale state."""
    if not pid or int(pid) <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - exercised on Windows hosts
        try:
            import ctypes

            process_query_limited_information = 0x1000
            still_active = 259
            handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
                process_query_limited_information, False, int(pid)
            )
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):  # type: ignore[attr-defined]
                    return False
                return exit_code.value == still_active
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - best effort only
            return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _coerce_event_sink(events: Any) -> EventSink:
    if events is None:
        return EventSink()
    if isinstance(events, EventSink):
        return events
    if hasattr(events, "on_instance_state") and hasattr(events, "emit"):
        return events  # already a sink-like helper
    if isinstance(events, EventBus) or (
        hasattr(events, "emit") and hasattr(events, "subscribe")
    ):
        return EventSink(events)
    raise ValidationError(
        "events must be an EventBus or EventSink",
        hint="Pass the daemon's EventBus so state changes reach the WebSocket hub.",
    )


def _looks_like_instance_repo(candidate: Any) -> bool:
    return candidate is not None and all(
        callable(getattr(candidate, method, None))
        for method in ("list", "get", "upsert", "delete")
    )


def _resolve_instance_repo(store: Any | None) -> Any | None:
    if store is None:
        return None
    if isinstance(store, Mapping):
        candidate = store.get("instances")
        return candidate if _looks_like_instance_repo(candidate) else None
    candidate = getattr(store, "instances", None)
    if _looks_like_instance_repo(candidate):
        return candidate
    if _looks_like_instance_repo(store):
        return store
    return None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AvdManagerService:
    """Concrete ``AvdManager`` implementation (see module docstring)."""

    def __init__(
        self,
        backend: Any | None = None,
        *,
        sdk: Any | None = None,
        store: Any | None = None,
        config: Config | None = None,
        paths: Paths | None = None,
        images: Any | None = None,
        templates: TemplateService | None = None,
        events: Any | None = None,
        device_profiles: Any | None = None,
        capacity_provider: Callable[[], Mapping[str, Any]] | None = None,
        clock: Callable[[], datetime] = utcnow,
        strict_devices: bool = True,
    ) -> None:
        self.backend = backend if backend is not None else sdk
        self.sdk = self.backend
        self.config = config or Config.defaults()
        self.paths = paths or self.config.resolved_paths()
        self.images = images
        self._clock = clock
        self._sink = _coerce_event_sink(events)
        self._lock = threading.RLock()
        self._capacity_provider = capacity_provider
        self._instances_repo = _resolve_instance_repo(store)
        self._memory_instances: dict[str, AvdInfo] = {}
        self.templates = templates or TemplateService(
            store=store,
            paths=self.paths,
            images=images,
            backend=self.backend,
            device_profiles=device_profiles,
            clock=clock,
            strict_devices=strict_devices,
        )

    # ------------------------------------------------------------------
    # Backend / path plumbing
    # ------------------------------------------------------------------
    def _require_backend(self) -> Any:
        if self.backend is None:
            raise ConfigError(
                "the Android SDK backend is not configured",
                hint="Wire an SdkBackend (legacy or android CLI) into AvdManagerService.",
            )
        return self.backend

    @property
    def avd_home(self) -> Path:
        """The effective ``ANDROID_AVD_HOME`` directory."""
        home = getattr(self._require_backend(), "avd_home", None)
        if home:
            return Path(str(home))
        if self.paths.avd_home:
            return Path(self.paths.avd_home)
        raise ConfigError(
            "the Android AVD home directory could not be resolved",
            hint="Set ANDROID_AVD_HOME or configure avd_home in avdfleet.toml.",
        )

    def avd_dir(self, name: str) -> Path:
        return avd_dir_for(self.avd_home, name)

    def config_path(self, name: str) -> Path:
        return config_path_for(self.avd_home, name)

    def ini_path(self, name: str) -> Path:
        return ini_path_for(self.avd_home, name)

    def read_config(self, name: str) -> dict[str, str]:
        """Return the persisted ``config.ini`` values (empty when absent)."""
        return load_values(self.config_path(name))

    def inspect_config(self, name: str) -> list[ConfigIniIssue]:
        """Report missing/corrupt config.ini entries for one instance."""
        self.get_instance(name)
        return detect_config_issues(self.config_path(name), require_file=True)

    # ------------------------------------------------------------------
    # Templates
    # ------------------------------------------------------------------
    def list_templates(self) -> list[Template]:
        return self.templates.list_templates()

    def get_template(self, template_id: str) -> Template:
        return self.templates.get_template(template_id)

    def get_template_by_name(self, name: str) -> Template | None:
        return self.templates.get_template_by_name(name)

    def save_template(self, template: Template) -> Template:
        saved = self.templates.save_template(template)
        self._sink.emit(
            "template.saved",
            {"template": saved.id, "name": saved.name},
            source="platform.avd",
        )
        return saved

    def delete_template(self, template_id: str) -> None:
        self.templates.delete_template(template_id)
        self._sink.emit(
            "template.deleted", {"template": template_id}, source="platform.avd"
        )

    def list_device_profiles(self) -> list[DeviceProfile]:
        return self.templates.list_device_profiles()

    # ------------------------------------------------------------------
    # Instance metadata
    # ------------------------------------------------------------------
    def _metadata(self, name: str) -> AvdInfo | None:
        if self._instances_repo is not None:
            try:
                return self._instances_repo.get(name)
            except NotFoundError:
                return None
            except (KeyError, IndexError):
                return None
            except AvdFleetError as exc:
                logger.warning("instance metadata lookup failed for %s: %s", name, exc)
        return self._memory_instances.get(name)

    def _save_metadata(self, info: AvdInfo) -> AvdInfo:
        self._memory_instances[info.name] = info
        if self._instances_repo is not None:
            try:
                saved = self._instances_repo.upsert(info)
                if isinstance(saved, AvdInfo):
                    self._memory_instances[info.name] = saved
                    return saved
            except AvdFleetError as exc:
                logger.warning("could not persist instance metadata for %s: %s", info.name, exc)
        return info

    def _delete_metadata(self, name: str) -> None:
        self._memory_instances.pop(name, None)
        if self._instances_repo is not None:
            try:
                self._instances_repo.delete(name)
            except NotFoundError:
                pass
            except AvdFleetError as exc:
                logger.warning("could not delete instance metadata for %s: %s", name, exc)

    # ------------------------------------------------------------------
    # Listing / lookup
    # ------------------------------------------------------------------
    def _validate_name(self, name: str, *, kind: str = "instance") -> str:
        candidate = (name or "").strip()
        if not candidate:
            raise ValidationError(f"{kind} name must not be empty")
        if not _AVD_NAME_RE.match(candidate):
            raise ValidationError(
                f"invalid {kind} name {candidate!r}: use letters, digits, '.', '_' and '-' "
                "(max 63 characters)"
            )
        return candidate

    def _backend_names(self) -> list[str]:
        backend = self._require_backend()
        try:
            names = backend.list_avds()
        except AvdFleetError:
            raise
        except Exception as exc:  # noqa: BLE001 - tool failure converted to API error
            raise InstanceError(f"could not list AVDs: {exc}") from exc
        result: list[str] = []
        for name in names or []:
            text = str(name).strip()
            if text and text not in result:
                result.append(text)
        return sorted(result)

    def _known_names(self) -> list[str]:
        names: set[str] = set()
        try:
            names.update(self._backend_names())
        except AvdFleetError as exc:
            logger.warning("backend AVD listing failed: %s", exc)
        if self._instances_repo is not None:
            try:
                names.update(info.name for info in self._instances_repo.list() or [])
            except AvdFleetError as exc:  # pragma: no cover - defensive
                logger.warning("instance metadata listing failed: %s", exc)
        names.update(self._memory_instances)
        return sorted(names)

    def _build_info(self, name: str) -> AvdInfo:
        metadata = self._metadata(name)
        config = load_values(self.config_path(name))
        template_id = metadata.template_id if metadata is not None else None
        template: Template | None = None
        if template_id:
            try:
                template = self.templates.get_template(template_id)
            except NotFoundError:
                template = None
        image_id = (
            metadata.image_id
            if metadata is not None and metadata.image_id
            else (template.image_id if template else None)
        )
        image_name = ""
        if metadata is not None:
            image_name = metadata.image_name
        elif image_id is not None:
            try:
                image = self.templates.resolve_image(image_id)
                image_name = str(getattr(image, "display_name", "") or "")
            except ValidationError:
                image_name = ""
        device_id = (
            metadata.device_id
            if metadata is not None and metadata.device_id
            else config.get("hw.device.name", template.device_id if template else "")
        )
        state = metadata.state if metadata is not None else InstanceState.STOPPED
        pid = metadata.pid if metadata is not None else None
        boot_completed = metadata.boot_completed if metadata is not None else False
        error = metadata.error if metadata is not None else ""
        if state.active and not _pid_alive(pid):
            # Stale metadata from an earlier daemon run: the process is gone.
            state = InstanceState.STOPPED
            pid = None
            boot_completed = False
        ports = metadata.ports if metadata is not None else None
        started_at = metadata.started_at if metadata is not None else None
        uptime = metadata.uptime_seconds if metadata is not None else None
        return AvdInfo(
            name=name,
            template_id=template_id,
            image_id=image_id,
            image_name=image_name,
            device_id=device_id or "",
            state=state,
            pid=pid,
            ports=ports,
            proxy_id=metadata.proxy_id if metadata is not None else None,
            adb_serial=metadata.adb_serial if metadata is not None else None,
            boot_completed=boot_completed,
            created_at=metadata.created_at if metadata is not None else None,
            started_at=started_at,
            uptime_seconds=uptime,
            error=error,
            config=config,
            grpc_endpoint=metadata.grpc_endpoint if metadata is not None else None,
            screen_available=metadata.screen_available if metadata is not None else False,
        )

    def list_instances(self) -> list[AvdInfo]:
        return [self._build_info(name) for name in self._known_names()]

    def get_instance(self, name: str) -> AvdInfo:
        candidate = self._validate_name(name)
        # Fast path: metadata/directories prove existence without paying for a
        # full SDK ``avdmanager list`` on every status/start/stop request.
        if self._metadata(candidate) is not None:
            return self._build_info(candidate)
        if self.avd_dir(candidate).exists() or candidate in self._backend_names():
            return self._build_info(candidate)
        raise NotFoundError(f"instance {candidate!r} was not found")

    def _exists(self, name: str) -> bool:
        return name in self._known_names() or self.avd_dir(name).exists()

    # ------------------------------------------------------------------
    # Capacity helper (deployment side only; RAM is checked by the runtime)
    # ------------------------------------------------------------------
    def capacity(self) -> dict[str, Any]:
        data = (
            dict(self._capacity_provider() or {})
            if self._capacity_provider is not None
            else {}
        )
        data.setdefault("deployed", len(self._known_names()))
        data.setdefault("max_instances", int(self.config.max_instances))
        data.setdefault(
            "running",
            sum(1 for info in self.list_instances() if info.state.active),
        )
        return data

    def _check_deploy_capacity(self, count: int, *, deployed: int | None = None) -> None:
        if deployed is not None:
            data = (
                dict(self._capacity_provider() or {})
                if self._capacity_provider is not None
                else {}
            )
            data["deployed"] = max(int(data.get("deployed") or 0), int(deployed))
            data.setdefault("max_instances", int(self.config.max_instances))
        else:
            data = self.capacity()
        maximum = int(data.get("max_instances") or self.config.max_instances)
        actual = int(data.get("deployed") or 0)
        if actual + count > maximum:
            raise CapacityError(
                f"cannot deploy {count} instance(s): {actual} deployed of a maximum {maximum}",
                hint="Delete unused instances or raise max_instances in the configuration.",
                details={"deployed": actual, "max_instances": maximum, "requested": count},
            )

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    def _apply_overrides(
        self, template: Template, overrides: Mapping[str, Any] | None
    ) -> Template:
        if not overrides:
            return self.templates.validate_template(template)
        unknown = sorted(set(overrides) - OVERRIDE_KEYS)
        if unknown:
            raise ValidationError(
                f"unsupported instance override(s): {', '.join(unknown)}",
                hint=f"supported overrides: {', '.join(sorted(OVERRIDE_KEYS))}",
            )
        values = template.model_dump()
        config = {str(key): str(value) for key, value in template.config.items()}
        for key, value in overrides.items():
            if value is None:
                continue
            if key == "config":
                try:
                    config.update(validate_config_values(value))
                except ConfigValidationError as exc:
                    raise ValidationError(
                        "invalid config override", details={"errors": exc.errors}
                    ) from exc
                continue
            values[key] = value
        values["config"] = config
        try:
            candidate = Template.model_validate(values)
        except Exception as exc:  # noqa: BLE001 - pydantic error converted
            raise ValidationError(f"invalid instance overrides: {exc}") from exc
        return self.templates.validate_template(candidate)

    def _image_for_template(self, template: Template) -> Any | None:
        image = self.templates.resolve_image(template.image_id)
        if image is not None and getattr(image, "installed", True) is False:
            raise ValidationError(
                f"system image {template.image_id!r} is not installed",
                hint="Download the image first (avdfleet images download ...).",
            )
        return image

    def _create_from_template(
        self,
        template: Template,
        name: str,
        *,
        known_names: set[str] | None = None,
        check_capacity: bool = True,
    ) -> AvdInfo:
        backend = self._require_backend()
        if check_capacity:
            self._check_deploy_capacity(
                1, deployed=len(known_names) if known_names is not None else None
            )
        if name in (known_names or set()) or self.avd_dir(name).exists():
            raise ConflictError(
                f"an AVD named {name!r} already exists",
                hint="Choose another name or delete the existing instance.",
            )
        image = self._image_for_template(template)
        try:
            values = render_template_config(template, image=image)
        except ConfigValidationError as exc:
            raise ValidationError(
                "template produced an invalid config.ini",
                details={"errors": exc.errors},
            ) from exc
        try:
            created = backend.create_avd(
                name,
                template.image_id,
                template.device_id,
                config=values,
                force=False,
            )
        except AvdFleetError:
            raise
        except Exception as exc:  # noqa: BLE001 - tool failure converted
            raise InstanceError(f"could not create AVD {name!r}: {exc}") from exc
        if isinstance(created, str) and created.strip() and created.strip() != name:
            # Some backends return the created .avd directory path; this is not
            # an error, so keep the diagnostic at debug level.
            logger.debug("backend reported AVD artifact %r for requested %r", created, name)
        try:
            apply_config_overrides(
                self.config_path(name),
                values,
                create_if_missing=True,
                backup=False,
            )
        except AvdFleetError:
            # Do not leave a half-configured AVD behind.
            try:
                backend.delete_avd(name, force=True)
            except Exception:  # noqa: BLE001 - best-effort rollback
                logger.warning("could not roll back partially created AVD %s", name)
            raise
        info = AvdInfo(
            name=name,
            template_id=template.id,
            image_id=template.image_id,
            image_name=str(getattr(image, "display_name", "") or ""),
            device_id=template.device_id,
            state=InstanceState.STOPPED,
            proxy_id=template.proxy_id,
            created_at=self._clock(),
            config=values,
        )
        saved = self._save_metadata(info)
        self._sink.on_instance_state(name, saved.state.value, template_id=template.id)
        self._sink.emit(
            EventType.INSTANCE_CREATED,
            {"instance": name, "template_id": template.id, "image_id": template.image_id},
            source="platform.avd",
        )
        logger.info("created AVD instance %s from template %s", name, template.id)
        return self._build_info(name)

    def create_instance(
        self,
        template_id: str,
        name: str,
        overrides: dict[str, Any] | None = None,
    ) -> AvdInfo:
        candidate = self._validate_name(name)
        template = self.templates.get_template(template_id)
        merged = self._apply_overrides(template, overrides)
        with self._lock:
            known = set(self._known_names())
            return self._create_from_template(merged, candidate, known_names=known)

    def create_instance_from_image(
        self,
        image: Any = None,
        name: str = "",
        *,
        image_id: str | None = None,
        device_id: str | None = None,
        overrides: Mapping[str, Any] | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> AvdInfo:
        """Create a one-off instance directly from a system image (OOBE helper).

        ``image`` accepts either a ``SystemImage`` (OOBE passes the catalogue
        object positionally) or an image id string; an explicit ``image_id``
        keyword wins when both are supplied.
        """
        candidate = self._validate_name(name)
        resolved_id = str(image_id or getattr(image, "id", "") or image).strip()
        if not resolved_id:
            raise ValidationError("system image id must not be empty")
        resolved_image = self.templates.resolve_image(resolved_id)
        if resolved_image is not None and getattr(resolved_image, "installed", True) is False:
            raise ValidationError(
                f"system image {resolved_id!r} is not installed",
                hint="Download the image first.",
            )
        profiles = self.templates.list_device_profiles()
        default_device = (
            "pixel_7"
            if any(profile.id == "pixel_7" for profile in profiles)
            else (profiles[0].id if profiles else "pixel_7")
        )
        template = Template(
            id=slugify(candidate),
            name=candidate,
            image_id=resolved_id,
            device_id=device_id or default_device,
            ram_mb=self.config.default_ram_mb,
            heap_mb=self.config.default_heap_mb,
            cpu_cores=self.config.default_cpu_cores,
            gpu_mode=self.config.gpu_mode,
            headless=self.config.headless,
            config={str(key): str(value) for key, value in (config or {}).items()},
        )
        merged = self._apply_overrides(template, overrides)
        with self._lock:
            known = set(self._known_names())
            return self._create_from_template(merged, candidate, known_names=known)

    def bulk_deploy(
        self,
        template_id: str,
        *,
        count: int = 1,
        prefix: str = "avd",
        overrides: dict[str, Any] | None = None,
    ) -> list[AvdInfo]:
        try:
            resolved_count = int(count)
        except (TypeError, ValueError) as exc:
            raise ValidationError("count must be an integer") from exc
        if not 1 <= resolved_count <= 64:
            raise ValidationError("count must be between 1 and 64")
        candidate_prefix = (prefix or "").strip()
        if not _PREFIX_RE.match(candidate_prefix):
            raise ValidationError(
                f"invalid instance prefix {prefix!r}: use letters, digits, '.', '_' and '-'",
            )
        template = self.templates.get_template(template_id)
        merged = self._apply_overrides(template, overrides)
        with self._lock:
            existing = set(self._known_names())
            self._check_deploy_capacity(resolved_count, deployed=len(existing))
            names: list[str] = []
            index = 1
            while len(names) < resolved_count:
                candidate = f"{candidate_prefix}-{index:02d}"
                if candidate not in existing:
                    names.append(candidate)
                index += 1
                if index > 1000:  # pragma: no cover - absurd guard
                    raise CapacityError("could not find enough free instance names")
            created: list[AvdInfo] = []
            try:
                for name in names:
                    created.append(
                        self._create_from_template(
                            merged,
                            name,
                            known_names=existing,
                            check_capacity=False,
                        )
                    )
                    existing.add(name)
            except Exception:
                for info in created:
                    try:
                        self.delete_instance(info.name, delete_files=True)
                    except Exception:  # noqa: BLE001 - best-effort rollback
                        logger.warning("bulk deploy rollback failed for %s", info.name)
                raise
            return created

    # ------------------------------------------------------------------
    # Deletion / clone
    # ------------------------------------------------------------------
    def delete_instance(self, name: str, *, delete_files: bool = True) -> None:
        candidate = self._validate_name(name)
        info = self.get_instance(candidate)
        if info.state.active and _pid_alive(info.pid):
            raise ConflictError(
                f"instance {candidate!r} is running",
                hint="Stop the instance before deleting it.",
            )
        with self._lock:
            if delete_files:
                backend = self._require_backend()
                try:
                    backend.delete_avd(candidate, force=False)
                except AvdFleetError as exc:
                    if self.avd_dir(candidate).exists():
                        raise InstanceError(
                            f"could not delete AVD {candidate!r}: {exc}",
                            hint="Stop the emulator and check file permissions.",
                        ) from exc
                    logger.debug("AVD %s was already absent from the SDK: %s", candidate, exc)
            self._delete_metadata(candidate)
        self._sink.emit(
            EventType.INSTANCE_DELETED, {"instance": candidate}, source="platform.avd"
        )
        logger.info("deleted AVD instance %s", candidate)

    def clone_instance(self, source: str, name: str) -> AvdInfo:
        source_name = self._validate_name(source, kind="source")
        target_name = self._validate_name(name)
        source_info = self.get_instance(source_name)
        if source_info.state.active and _pid_alive(source_info.pid):
            raise ConflictError(
                f"instance {source_name!r} is running",
                hint="Stop the instance before cloning it (files would be inconsistent).",
            )
        if self._exists(target_name):
            raise ConflictError(f"an AVD named {target_name!r} already exists")
        source_dir = self.avd_dir(source_name)
        target_dir = self.avd_dir(target_name)
        if not source_dir.is_dir():
            raise NotFoundError(
                f"AVD directory for {source_name!r} is missing: {source_dir}",
                hint="Recreate the instance; the AVD files may have been deleted externally.",
            )
        with self._lock:
            if target_dir.exists():
                raise ConflictError(f"target AVD directory already exists: {target_dir}")
            try:
                shutil.copytree(
                    source_dir,
                    target_dir,
                    ignore=shutil.ignore_patterns("*.lock", "*.tmp", "*.tmp-*", "*.pid"),
                )
            except OSError as exc:
                shutil.rmtree(target_dir, ignore_errors=True)
                raise InstanceError(
                    f"could not copy AVD directory {source_dir} -> {target_dir}: {exc}",
                    hint="Check free disk space and file permissions.",
                ) from exc
            try:
                self._rewrite_cloned_ini(source_name, target_name, source_dir, target_dir)
                config = load_values(self.config_path(target_name))
            except Exception:
                shutil.rmtree(target_dir, ignore_errors=True)
                raise
            info = AvdInfo(
                name=target_name,
                template_id=source_info.template_id,
                image_id=source_info.image_id,
                image_name=source_info.image_name,
                device_id=source_info.device_id,
                state=InstanceState.STOPPED,
                proxy_id=source_info.proxy_id,
                created_at=self._clock(),
                config=config,
            )
            saved = self._save_metadata(info)
        self._sink.emit(
            EventType.INSTANCE_CREATED,
            {"instance": target_name, "cloned_from": source_name},
            source="platform.avd",
        )
        logger.info("cloned AVD instance %s -> %s", source_name, target_name)
        return self._build_info(saved.name)

    def _rewrite_cloned_ini(
        self,
        source_name: str,
        target_name: str,
        source_dir: Path,
        target_dir: Path,
    ) -> None:
        """Write the target ``<name>.ini`` and rebase paths inside config.ini."""
        source_ini = self.ini_path(source_name)
        target_ini = self.ini_path(target_name)
        if source_ini.exists():
            document = ConfigIni.read(source_ini, strict=True)
            original_rel = document.get("path.rel", f"avd/{source_name}.avd")
            document.set("path", str(target_dir).replace("\\", "/"), validate=False)
            document.set(
                "path.rel",
                str(original_rel).replace(source_name, target_name),
                validate=False,
            )
            if document.get("avd.ini.displayname") is not None:
                document.set("avd.ini.displayname", target_name, validate=False)
            document.write(target_ini, backup=False)
        else:
            target_ini.write_text(
                "\n".join(
                    [
                        "avd.ini.encoding=UTF-8",
                        f"path={str(target_dir).replace(chr(92), '/')}",
                        f"path.rel=avd/{target_name}.avd",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
        config_file = target_dir / "config.ini"
        if not config_file.exists():
            raise InstanceError(
                f"cloned AVD is missing config.ini: {config_file}",
                hint="The source AVD directory was incomplete; recreate it before cloning.",
            )
        document = ConfigIni.read(config_file, strict=True)
        replacements = {
            str(source_dir): str(target_dir),
            source_name: target_name,
        }
        for key in document.keys():  # noqa: SIM118 - ConfigIni is not an iterable of keys
            value = document.get(key)
            if value is None:
                continue
            updated = value
            for old, new in replacements.items():
                if old and old in updated:
                    updated = updated.replace(old, new)
            if updated != value:
                document.set(key, updated, validate=False)
        document.write(config_file, backup=False)

    # ------------------------------------------------------------------
    # config.ini updates
    # ------------------------------------------------------------------
    def update_config(
        self, name: str, values: dict[str, str], *, dry_run: bool = False
    ) -> AvdInfo:
        candidate = self._validate_name(name)
        info = self.get_instance(candidate)
        if not isinstance(values, Mapping):
            raise ValidationError("config values must be an object")
        normalised = validate_config_values(values)
        path = self.config_path(candidate)
        with self._lock:
            if path.exists():
                document = ConfigIni.read(path, strict=True)
                document.update(normalised)
                if not dry_run:
                    document.write(path, backup=True)
            else:
                if dry_run:
                    document = ConfigIni.from_values(normalised)
                else:
                    raise InstanceError(
                        f"config.ini is missing for {candidate!r}: {path}",
                        hint="The AVD was created outside avdfleet; recreate it from a template.",
                    )
            merged = document.to_dict()
            persisted = info.model_copy(update={"config": merged})
            if not dry_run:
                persisted = self._save_metadata(persisted)
            else:
                persisted = persisted.model_copy(update={"config": merged})
        logger.info(
            "%s config.ini for %s (%d key(s))",
            "validated" if dry_run else "updated",
            candidate,
            len(normalised),
        )
        return self._build_info(candidate) if not dry_run else persisted

    # ------------------------------------------------------------------
    # YAML import / export
    # ------------------------------------------------------------------
    def export_yaml(self) -> str:
        return export_avd_yaml(
            self.list_templates(),
            self.list_instances(),
            generated_at=self._clock(),
        )

    def import_yaml(self, content: str) -> list[AvdInfo]:
        export = parse_avd_yaml(content)
        for saved_template in export.templates:
            self.templates.save_template(saved_template)
        created: list[AvdInfo] = []
        templates_by_id = {template.id: template for template in export.templates}
        templates_by_name = {template.name: template for template in export.templates}
        for entry in export.instances:
            name = str(entry["name"])
            if self._exists(name):
                logger.info("YAML import: instance %s already exists, skipping", name)
                continue
            template_id = str(entry.get("template_id") or "").strip()
            template: Template | None = templates_by_id.get(template_id) or templates_by_name.get(
                template_id
            )
            if template is None:
                try:
                    template = self.templates.get_template(template_id)
                except NotFoundError as exc:
                    raise ValidationError(
                        f"instance {name!r} references unknown template {template_id!r}"
                    ) from exc
            overrides: dict[str, Any] = {}
            entry_config = entry.get("config") or {}
            if entry_config:
                overrides["config"] = entry_config
            for field in ("proxy_id", "image_id", "device_id"):
                if entry.get(field):
                    overrides[field] = entry[field]
            created.append(self.create_instance(template.id, name, overrides or None))
        return created

    # ------------------------------------------------------------------
    def close(self) -> None:
        """Release resources (there are none today; part of the service contract)."""


__all__ = ["AvdManagerService", "OVERRIDE_KEYS"]
