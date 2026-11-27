"""Diff a resolved fleet against live managers and produce a ``FleetPlan``.

The planner is pure read-only: it receives duck-typed managers (``AvdManager``,
``ProxyPool``, ``ProxyAssigner`` and ``ImageCatalog`` shapes from
``avdfleet.core.interfaces``) plus an optional store for fleet ownership records,
and never mutates them.  The reconciler executes the plan it produces.

Semantics
---------
* every declared proxy/template/instance yields exactly one ``create``/``update``/
  ``noop`` change, in stable dependency order (proxies → images → templates →
  instances, creates before updates before no-ops);
* ``prune=True`` adds ``delete`` changes for instances previously deployed by this
  fleet that are no longer declared.  Templates and proxies are never deleted by
  the fleet reconciler (they may be shared); only instances are pruned;
* two consecutive plans against unchanged state are identical and contain only
  ``noop`` changes;
* image requirements are reported as ``kind="image"`` changes.  With
  ``image_policy="fail"`` a missing image makes the plan non-applyable, with
  ``"warn"`` (default) it is visible in the change detail only, and with
  ``"ignore"`` the check is skipped entirely;
* no password or password value ever appears in a plan.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core.errors import AvdFleetError, FleetSpecError, NotFoundError
from ..core.logging import redact_text
from ..core.models import (
    FleetPlan,
    FleetSpec,
    FleetTemplateSpec,
    GpuMode,
    PlanAction,
    PlanChange,
    ProxyScheme,
)
from .loader import FleetLoader, ResolvedFleet

#: Policy for a required system image that is known but not installed.
IMAGE_POLICIES: tuple[str, ...] = ("warn", "fail", "ignore")

_TEMPLATE_FIELDS: tuple[str, ...] = (
    "image",
    "device",
    "ram_mb",
    "cpu_cores",
    "heap_mb",
    "gpu_mode",
    "headless",
    "proxy",
    "scripts",
    "tags",
)
_INSTANCE_FIELDS: tuple[str, ...] = ("template", "proxy", "override_ram_mb")
_PROXY_FIELDS: tuple[str, ...] = ("scheme", "host", "port", "username", "tags")

_NON_DELETE_KIND_RANK = {"proxy": 0, "image": 1, "template": 2, "instance": 3}
_DELETE_KIND_RANK = {"instance": 0, "template": 1, "proxy": 2, "image": 3}
_ACTION_RANK = {"create": 0, "update": 1, "noop": 2, "delete": 3}
_DEFAULT_GPU_MODE = GpuMode.SWIFTSHADER_INDIRECT


# ---------------------------------------------------------------------------
# Duck-typing helpers (also used by the reconciler)
# ---------------------------------------------------------------------------


def invoke_compatible(function: Any, positional: Sequence[Any], keyword: Mapping[str, Any]) -> Any:
    """Call *function* passing only the keyword arguments its signature accepts.

    Manager implementations in the wild (fakes, alternative backends) range from
    the full protocol signature to a two-line fake.  This keeps the fleet engine
    duck-typed without swallowing real ``TypeError``s raised by the callee.
    """
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins/odd callables
        return function(*positional, **keyword)
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return function(*positional, **keyword)
    accepted = {
        name: value
        for name, value in keyword.items()
        if name in parameters
        and parameters[name].kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return function(*positional, **accepted)


def call_method(target: Any, name: str, *positional: Any, **keyword: Any) -> Any:
    """Call ``target.name(...)`` or raise a clear :class:`FleetSpecError`."""
    method = getattr(target, name, None)
    if not callable(method):
        raise FleetSpecError(
            f"the injected {type(target).__name__} does not implement {name}()",
            hint="wire the service container with a full AvdManager/ProxyPool/Assigner implementation",
        )
    return invoke_compatible(method, positional, keyword)


def jsonable(value: Any) -> Any:
    """Return a JSON-safe copy of *value*; secrets are always masked."""
    if isinstance(value, Enum):
        return jsonable(value.value)
    if hasattr(value, "get_secret_value"):
        return "***"
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return jsonable(value.model_dump(mode="json"))
    return str(value)


def fleet_settings_key(fleet_name: str, suffix: str) -> str:
    """Key used in the store settings table to remember fleet ownership."""
    return f"fleet/{fleet_name}/{suffix}"


def read_store_setting(store: Any | None, key: str, default: Any = None) -> Any:
    """Read a settings value through the store's duck-typed settings repository."""
    if store is None:
        return default
    settings = getattr(store, "settings", None)
    getter = getattr(settings, "get", None) if settings is not None else None
    if callable(getter):
        try:
            return invoke_compatible(getter, [key, default], {})
        except Exception:
            return default
    for name in ("get_setting", "settings_get"):
        method = getattr(store, name, None)
        if callable(method):
            try:
                return invoke_compatible(method, [key, default], {})
            except Exception:
                return default
    return default


def write_store_setting(store: Any | None, key: str, value: Any) -> bool:
    """Write a settings value; returns ``True`` when it was persisted."""
    if store is None:
        return False
    settings = getattr(store, "settings", None)
    setter = getattr(settings, "set", None) if settings is not None else None
    if callable(setter):
        try:
            invoke_compatible(setter, [key, value], {})
            return True
        except Exception:
            return False
    for name in ("set_setting", "settings_set"):
        method = getattr(store, name, None)
        if callable(method):
            try:
                invoke_compatible(method, [key, value], {})
                return True
            except Exception:
                return False
    return False


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    match = str(value).strip()
    if not match:
        return None
    try:
        return int(float(match))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Current state snapshot
# ---------------------------------------------------------------------------


@dataclass
class FleetState:
    """Read-only view of the managers gathered before planning."""

    templates: dict[str, Any] = field(default_factory=dict)
    instances: dict[str, Any] = field(default_factory=dict)
    proxies: dict[str, Any] = field(default_factory=dict)
    assignments: dict[str, str] = field(default_factory=dict)
    images: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class FleetPlanner:
    """Compute a :class:`~avdfleet.core.models.FleetPlan` from a fleet spec."""

    def __init__(
        self,
        avds: Any,
        *,
        proxies: Any | None = None,
        assignments: Any | None = None,
        images: Any | None = None,
        store: Any | None = None,
        loader: FleetLoader | None = None,
        image_policy: str = "warn",
    ) -> None:
        if image_policy not in IMAGE_POLICIES:
            raise ValueError(
                f"image_policy must be one of {', '.join(IMAGE_POLICIES)}; got {image_policy!r}"
            )
        self._avds = avds
        self._proxies = proxies
        self._assignments = assignments
        self._images = images
        self._store = store
        self._loader = loader or FleetLoader(image_catalog=images, image_policy=image_policy)
        self._image_policy = image_policy

    # -- properties ---------------------------------------------------------
    @property
    def image_policy(self) -> str:
        return self._image_policy

    @property
    def loader(self) -> FleetLoader:
        return self._loader

    # -- public API ---------------------------------------------------------
    def plan(self, spec: FleetSpec | ResolvedFleet | Mapping[str, Any], *, prune: bool = False) -> FleetPlan:
        """Return the plan that would reconcile *spec* against the live fleet."""
        try:
            fleet = self._resolve(spec)
        except FleetSpecError as exc:
            return self._invalid_plan(spec, exc)
        state = self._snapshot(fleet)
        errors: list[str] = list(state.errors)
        changes: list[PlanChange] = []
        changes.extend(self._proxy_changes(fleet, state, errors))
        changes.extend(self._template_changes(fleet, state))
        changes.extend(self._instance_changes(fleet, state))
        if prune:
            changes.extend(self._prune_changes(fleet, state))
        changes.extend(self._image_changes(fleet, state, errors))
        changes.sort(key=_change_order)
        deduped_errors = list(dict.fromkeys(redact_text(error) for error in errors if error))
        return build_fleet_plan(
            spec_name=fleet.name,
            changes=changes,
            applyable=not deduped_errors,
            errors=deduped_errors,
        )

    # -- resolution ---------------------------------------------------------
    def _resolve(self, spec: FleetSpec | ResolvedFleet | Mapping[str, Any]) -> ResolvedFleet:
        if isinstance(spec, ResolvedFleet):
            return spec
        if isinstance(spec, Mapping):
            spec = self._loader.from_dict(spec)
        if not isinstance(spec, FleetSpec):
            raise FleetSpecError(
                f"plan() expects a FleetSpec, got {type(spec).__name__}",
                hint="parse the fleet document with FleetLoader before planning",
            )
        return self._loader.resolve(spec, require_images=False)

    def _invalid_plan(
        self,
        spec: FleetSpec | ResolvedFleet | Mapping[str, Any],
        exc: FleetSpecError,
    ) -> FleetPlan:
        details = exc.details.get("errors") if isinstance(exc.details, Mapping) else None
        messages = [exc.message]
        if isinstance(details, list):
            for item in details:
                if isinstance(item, Mapping):
                    path = _text(item.get("path"))
                    message = _text(item.get("message"))
                    if path or message:
                        messages.append(f"{path}: {message}".strip(": "))
        unique = list(dict.fromkeys(redact_text(message) for message in messages))
        return build_fleet_plan(
            spec_name=_spec_name(spec),
            changes=[],
            applyable=False,
            errors=unique,
        )

    # -- snapshot -----------------------------------------------------------
    def _snapshot(self, fleet: ResolvedFleet) -> FleetState:
        state = FleetState()
        try:
            templates = list(call_method(self._avds, "list_templates") or [])
        except Exception as exc:
            state.errors.append(f"cannot list AVD templates: {redact_text(str(exc))}")
            templates = []
        for template in templates:
            for key in {_text(getattr(template, "id", "")), _text(getattr(template, "name", ""))}:
                if key:
                    state.templates[key] = template
        try:
            instances = list(call_method(self._avds, "list_instances") or [])
        except Exception as exc:
            state.errors.append(f"cannot list AVD instances: {redact_text(str(exc))}")
            instances = []
        for instance in instances:
            name = _text(getattr(instance, "name", ""))
            if name:
                state.instances[name] = instance
        if self._proxies is not None:
            try:
                listed = call_method(self._proxies, "list_proxies", include_disabled=True) or []
                for proxy in listed:
                    proxy_id = _text(getattr(proxy, "id", ""))
                    if proxy_id:
                        state.proxies[proxy_id] = proxy
            except Exception as exc:
                state.errors.append(f"cannot list proxies: {redact_text(str(exc))}")
        if self._assignments is not None:
            try:
                for assignment in call_method(self._assignments, "list_assignments") or []:
                    name = _text(getattr(assignment, "avd_name", ""))
                    proxy_id = _text(getattr(assignment, "proxy_id", ""))
                    if name and proxy_id:
                        state.assignments[name] = proxy_id
            except Exception as exc:
                state.errors.append(f"cannot list proxy assignments: {redact_text(str(exc))}")
        if self._images is not None:
            try:
                for image in call_method(self._images, "list_images", include_remote=True) or []:
                    image_id = _text(getattr(image, "id", ""))
                    if image_id:
                        state.images[image_id] = image
            except Exception as exc:
                state.errors.append(f"cannot list system images: {redact_text(str(exc))}")
        return state

    # -- change builders ----------------------------------------------------
    def _proxy_changes(
        self, fleet: ResolvedFleet, state: FleetState, errors: list[str]
    ) -> list[PlanChange]:
        changes: list[PlanChange] = []
        if fleet.proxies and self._proxies is None:
            errors.append(
                "the fleet declares proxies but no proxy pool is available; proxy requirements "
                "cannot be reconciled"
            )
        for proxy in fleet.proxies:
            current = state.proxies.get(proxy.id)
            before = _serialize_proxy(current) if current is not None else None
            after = _serialize_proxy_spec(proxy)
            if current is None:
                action = PlanAction.CREATE
                detail = f"create {_enum_value(proxy.scheme)} proxy {proxy.host}:{proxy.port}"
            elif _changed(before or {}, after, _PROXY_FIELDS):
                action = PlanAction.UPDATE
                detail = (
                    f"update proxy {proxy.id} "
                    f"({', '.join(_changed(before or {}, after, _PROXY_FIELDS))})"
                )
            else:
                action = PlanAction.NOOP
                detail = f"proxy {proxy.id} is unchanged"
            changes.append(
                PlanChange(
                    action=action,
                    kind="proxy",
                    name=proxy.id,
                    detail=detail,
                    before=before,
                    after=after,
                )
            )
        return changes

    def _template_changes(self, fleet: ResolvedFleet, state: FleetState) -> list[PlanChange]:
        changes: list[PlanChange] = []
        for template in fleet.templates:
            current = state.templates.get(template.name)
            before = _serialize_template(current) if current is not None else None
            after = _serialize_template_spec(template)
            if current is None:
                action = PlanAction.CREATE
                detail = f"create template {template.name} from image {template.image}"
            elif _changed(before or {}, after, _TEMPLATE_FIELDS):
                action = PlanAction.UPDATE
                changed = ", ".join(_changed(before or {}, after, _TEMPLATE_FIELDS))
                detail = f"update template {template.name} ({changed})"
            else:
                action = PlanAction.NOOP
                detail = f"template {template.name} is unchanged"
            changes.append(
                PlanChange(
                    action=action,
                    kind="template",
                    name=template.name,
                    detail=detail,
                    before=before,
                    after=after,
                )
            )
        return changes

    def _instance_changes(self, fleet: ResolvedFleet, state: FleetState) -> list[PlanChange]:
        changes: list[PlanChange] = []
        overrides = {instance.name: instance.ram_mb for instance in fleet.spec.instances}
        for instance in sorted(fleet.instances, key=lambda item: item.name):
            template = fleet.template(instance.template)
            template_ram = int(template.ram_mb) if template is not None else None
            current = state.instances.get(instance.name)
            override = overrides.get(instance.name)
            after = _serialize_instance_spec(
                instance,
                template_ram=template_ram,
                override_ram_mb=override,
            )
            if current is None:
                action = PlanAction.CREATE
                detail = f"create instance {instance.name} from template {instance.template}"
                before = None
            else:
                before = _serialize_instance(
                    current,
                    assignment=state.assignments.get(instance.name),
                    template_ram=template_ram,
                )
                changed = _changed(before, after, _INSTANCE_FIELDS)
                if changed:
                    action = PlanAction.UPDATE
                    detail = f"update instance {instance.name} ({', '.join(changed)})"
                else:
                    action = PlanAction.NOOP
                    detail = f"instance {instance.name} is unchanged"
            changes.append(
                PlanChange(
                    action=action,
                    kind="instance",
                    name=instance.name,
                    detail=detail,
                    before=before,
                    after=after,
                )
            )
        return changes

    def _prune_changes(self, fleet: ResolvedFleet, state: FleetState) -> list[PlanChange]:
        desired = {instance.name for instance in fleet.instances}
        managed = self._managed_instance_names(fleet, state)
        changes: list[PlanChange] = []
        for name in sorted(managed - desired):
            current = state.instances.get(name)
            before = (
                _serialize_instance(current, assignment=state.assignments.get(name), template_ram=None)
                if current is not None
                else {"name": name}
            )
            changes.append(
                PlanChange(
                    action=PlanAction.DELETE,
                    kind="instance",
                    name=name,
                    detail=f"delete pruned instance {name} (no longer declared in the fleet spec)",
                    before=before,
                    after=None,
                )
            )
        return changes

    def _image_changes(
        self,
        fleet: ResolvedFleet,
        state: FleetState,
        errors: list[str],
    ) -> list[PlanChange]:
        if self._images is None or self._image_policy == "ignore":
            return []
        changes: list[PlanChange] = []
        for image_id in fleet.image_ids():
            image = state.images.get(image_id)
            if image is None:
                image = self._lookup_image(image_id)
            installed = bool(getattr(image, "installed", False)) if image is not None else False
            if installed:
                action = PlanAction.NOOP
                detail = f"system image {image_id} is installed"
            else:
                action = PlanAction.CREATE
                if image is not None:
                    detail = (
                        f"system image {image_id} is known but not installed; "
                        f"run `avdfleet images download {image_id}` before applying"
                    )
                else:
                    detail = (
                        f"system image {image_id} is not in the catalogue; "
                        "download or import it before applying"
                    )
                if self._image_policy == "fail":
                    errors.append(detail)
            after = _serialize_image(image_id, image, installed=installed)
            changes.append(
                PlanChange(
                    action=action,
                    kind="image",
                    name=image_id,
                    detail=detail,
                    before=None,
                    after=after,
                )
            )
        return changes

    # -- ownership ----------------------------------------------------------
    def _managed_instance_names(self, fleet: ResolvedFleet, state: FleetState) -> set[str]:
        """Instances the fleet owns and may prune.

        Ownership is the union of:

        * names previously recorded by this fleet (store settings, survives renames);
        * instances whose template was deployed by this fleet (declared now or
          recorded from a previous apply).

        Instances created by hand with unrelated templates are never pruned.
        """
        names: set[str] = set()
        recorded = read_store_setting(
            self._store, fleet_settings_key(fleet.name, "instances"), default=[]
        )
        if isinstance(recorded, (list, tuple, set)):
            names.update(_text(item) for item in recorded if _text(item))
        template_ids = set(fleet.template_names)
        recorded_templates = read_store_setting(
            self._store, fleet_settings_key(fleet.name, "templates"), default=[]
        )
        if isinstance(recorded_templates, (list, tuple, set)):
            template_ids.update(_text(item) for item in recorded_templates if _text(item))
        for instance in state.instances.values():
            template_id = _text(getattr(instance, "template_id", ""))
            name = _text(getattr(instance, "name", ""))
            if name and template_id and template_id in template_ids:
                names.add(name)
        return names

    def _lookup_image(self, image_id: str) -> Any | None:
        catalog = self._images
        if catalog is None:
            return None
        getter = getattr(catalog, "get_image", None)
        if not callable(getter):
            return None
        try:
            return getter(image_id)
        except (NotFoundError, KeyError):
            return None
        except AvdFleetError:
            return None
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _serialize_template_spec(template: FleetTemplateSpec) -> dict[str, Any]:
    return {
        "id": template.name,
        "name": template.name,
        "image": template.image,
        "device": template.device,
        "ram_mb": int(template.ram_mb),
        "cpu_cores": int(template.cpu_cores),
        "heap_mb": int(template.heap_mb),
        "gpu_mode": _enum_value(template.gpu_mode),
        "headless": bool(template.headless),
        "proxy": template.proxy,
        "scripts": [str(script) for script in template.scripts],
        "tags": [str(tag) for tag in template.tags],
    }


def _serialize_template(template: Any) -> dict[str, Any]:
    gpu_mode = _enum_value(getattr(template, "gpu_mode", _DEFAULT_GPU_MODE))
    return {
        "id": _text(getattr(template, "id", "")),
        "name": _text(getattr(template, "name", "")),
        "image": _text(getattr(template, "image_id", "")),
        "device": _text(getattr(template, "device_id", "")),
        "ram_mb": _int_or_none(getattr(template, "ram_mb", None)),
        "cpu_cores": _int_or_none(getattr(template, "cpu_cores", None)),
        "heap_mb": _int_or_none(getattr(template, "heap_mb", None)),
        "gpu_mode": gpu_mode,
        "headless": bool(getattr(template, "headless", True)),
        "proxy": getattr(template, "proxy_id", None),
        "scripts": [str(script) for script in (getattr(template, "scripts", None) or [])],
        "tags": [str(tag) for tag in (getattr(template, "tags", None) or [])],
    }


def _serialize_instance_spec(
    instance: Any,
    *,
    template_ram: int | None,
    override_ram_mb: int | None,
) -> dict[str, Any]:
    effective_ram = instance.ram_mb if instance.ram_mb is not None else template_ram
    return {
        "name": _text(instance.name),
        "template": _text(instance.template),
        "proxy": instance.proxy,
        "ram_mb": int(effective_ram) if effective_ram is not None else None,
        "override_ram_mb": override_ram_mb,
        "metadata": jsonable(instance.metadata),
    }


def _serialize_instance(
    instance: Any,
    *,
    assignment: str | None,
    template_ram: int | None,
) -> dict[str, Any]:
    config = getattr(instance, "config", None) or {}
    override = _int_or_none(config.get("hw.ramSize")) if isinstance(config, Mapping) else None
    proxy = assignment if assignment is not None else getattr(instance, "proxy_id", None)
    return {
        "name": _text(getattr(instance, "name", "")),
        "template": _text(getattr(instance, "template_id", "")),
        "proxy": proxy,
        "ram_mb": override if override is not None else template_ram,
        "override_ram_mb": override,
        "metadata": {},
        "state": _enum_value(getattr(instance, "state", None)),
    }


def _serialize_proxy_spec(proxy: Any) -> dict[str, Any]:
    return {
        "id": _text(proxy.id),
        "scheme": _enum_value(proxy.scheme),
        "host": _text(proxy.host),
        "port": int(proxy.port),
        "username": proxy.username,
        "password_env": proxy.password_env,
        "tags": [str(tag) for tag in proxy.tags],
    }


def _serialize_proxy(proxy: Any) -> dict[str, Any]:
    return {
        "id": _text(getattr(proxy, "id", "")),
        "scheme": _enum_value(getattr(proxy, "scheme", ProxyScheme.HTTP)),
        "host": _text(getattr(proxy, "host", "")),
        "port": _int_or_none(getattr(proxy, "port", None)),
        "username": getattr(proxy, "username", None),
        "password_env": None,
        "tags": [str(tag) for tag in (getattr(proxy, "tags", None) or [])],
    }


def _serialize_image(image_id: str, image: Any | None, *, installed: bool) -> dict[str, Any]:
    return {
        "image": image_id,
        "installed": installed,
        "package": _text(getattr(image, "package", "")),
        "api_level": getattr(image, "api_level", None),
        "abi": _text(getattr(image, "abi", "")),
    }


def _changed(before: Mapping[str, Any], after: Mapping[str, Any], fields: Sequence[str]) -> list[str]:
    return [field for field in fields if before.get(field) != after.get(field)]


def _change_order(change: PlanChange) -> tuple[int, int, int, str]:
    action = change.action.value
    if action == "delete":
        return (1, 0, _DELETE_KIND_RANK.get(change.kind, 9), change.name)
    return (0, _ACTION_RANK.get(action, 9), _NON_DELETE_KIND_RANK.get(change.kind, 9), change.name)


def build_fleet_plan(
    *,
    spec_name: str,
    changes: Sequence[PlanChange],
    applyable: bool = True,
    errors: Sequence[str] = (),
) -> FleetPlan:
    """Build a :class:`FleetPlan`; ``summary`` counts are derived by the core model.

    Kept as a named helper so the planner/reconciler and API have one construction
    path for plans (the core model recomputes ``summary`` in its validator).
    """
    return FleetPlan(
        spec_name=spec_name,
        changes=list(changes),
        applyable=applyable,
        errors=list(errors),
    )


def _spec_name(spec: FleetSpec | ResolvedFleet | Mapping[str, Any]) -> str:
    if isinstance(spec, ResolvedFleet):
        return spec.name
    if isinstance(spec, FleetSpec):
        return spec.name
    if isinstance(spec, Mapping):
        value = spec.get("name")
        if isinstance(value, str) and value:
            return value
    return "fleet"


__all__ = [
    "FleetPlanner",
    "FleetState",
    "IMAGE_POLICIES",
    "build_fleet_plan",
    "call_method",
    "fleet_settings_key",
    "invoke_compatible",
    "jsonable",
    "read_store_setting",
    "write_store_setting",
]
