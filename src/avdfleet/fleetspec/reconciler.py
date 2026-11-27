"""Idempotent fleet reconciler.

``FleetReconciler`` is the concrete implementation of
:class:`avdfleet.core.interfaces.FleetReconciler`.  It owns no global state: the
``AvdManager``, ``ProxyPool``, ``ProxyAssigner``, ``ImageCatalog``, optional store
and event bus are injected through the constructor (all duck-typed), which keeps the
engine testable with in-memory fakes and wires cleanly into the daemon container.

Applying a plan is idempotent: after a successful apply the same spec plans as
``noop`` only, and a second apply performs no calls.  Proxy passwords are read from
``password_env`` environment variables **at apply time**; they are never stored in
the fleet spec, the plan, an error message or a log line.

Partial failures are reported in the returned plan's ``errors`` list.  With
``on_error="continue"`` (default) the remaining changes are still attempted; with
``on_error="stop"`` execution stops at the first failure.  Pruned instances that
are running are protected unless ``force=True``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from ..core.errors import ConflictError, FleetSpecError, NotFoundError
from ..core.events import EventType
from ..core.logging import get_logger, redact_text
from ..core.models import (
    FleetPlan,
    FleetSpec,
    GpuMode,
    PlanAction,
    PlanChange,
    ProxyConfig,
    ProxyScheme,
    Template,
)
from .loader import FleetLoader, ResolvedFleet
from .planner import (
    FleetPlanner,
    call_method,
    fleet_settings_key,
    invoke_compatible,
    jsonable,
    write_store_setting,
)

_ERROR_POLICIES: tuple[str, ...] = ("continue", "stop")
_ACTIVE_STATES = frozenset({"starting", "booting", "running"})
_DEFAULT_GPU_MODE = GpuMode.SWIFTSHADER_INDIRECT


class FleetReconciler:
    """Reconcile a declarative fleet specification against live services.

    Parameters
    ----------
    avds:
        Duck-typed :class:`~avdfleet.core.interfaces.AvdManager`.
    proxies:
        Optional duck-typed :class:`~avdfleet.core.interfaces.ProxyPool`.
    assignments:
        Optional duck-typed :class:`~avdfleet.core.interfaces.ProxyAssigner`.
    images:
        Optional duck-typed :class:`~avdfleet.core.interfaces.ImageCatalog` used for
        image-requirement checks.
    store:
        Optional store exposing a ``settings`` repository (``get``/``set``) used to
        remember which instances/templates this fleet owns; without it prune falls
        back to template ownership only.
    events:
        Optional event bus (``avdfleet.core.events.EventBus`` or compatible); gets a
        ``fleet.applied`` event with counts after every non-dry-run apply.
    image_policy:
        ``"warn"`` (default), ``"fail"`` or ``"ignore"`` for missing images.
    on_error:
        ``"continue"`` (default) or ``"stop"`` for partial-failure handling.
    force:
        Default value for the ``force`` option of :meth:`apply`; when true running
        instances may be deleted by prune.
    environ:
        Optional environment mapping for ``password_env`` resolution (tests inject
        it; production reads ``os.environ`` at apply time).
    """

    def __init__(
        self,
        avds: Any,
        *,
        proxies: Any | None = None,
        assignments: Any | None = None,
        images: Any | None = None,
        store: Any | None = None,
        events: Any | None = None,
        loader: FleetLoader | None = None,
        planner: FleetPlanner | None = None,
        image_policy: str = "warn",
        on_error: str = "continue",
        force: bool = False,
        environ: Mapping[str, str] | None = None,
        logger: Any | None = None,
    ) -> None:
        if on_error not in _ERROR_POLICIES:
            raise ValueError(
                f"on_error must be one of {', '.join(_ERROR_POLICIES)}; got {on_error!r}"
            )
        self._avds = avds
        self._proxies = proxies
        self._assignments = assignments
        self._images = images
        self._store = store
        self._events = events
        self._loader = loader or FleetLoader(
            image_catalog=images,
            environ=environ,
            image_policy=image_policy,
        )
        self._planner = planner or FleetPlanner(
            avds,
            proxies=proxies,
            assignments=assignments,
            images=images,
            store=store,
            loader=self._loader,
            image_policy=image_policy,
        )
        self._image_policy = image_policy
        self._on_error = on_error
        self._force = bool(force)
        self._log = logger or get_logger("avdfleet.fleetspec")

    # -- properties ---------------------------------------------------------
    @property
    def planner(self) -> FleetPlanner:
        return self._planner

    @property
    def loader(self) -> FleetLoader:
        return self._loader

    @property
    def image_policy(self) -> str:
        return self._image_policy

    # -- core FleetReconciler protocol -------------------------------------
    def load(self, path: str | Path) -> FleetSpec:
        """Load and validate a fleet document from *path*."""
        return self._loader.load(path)

    def validate(self, spec: FleetSpec) -> list[str]:
        """Return human-readable validation problems for *spec*."""
        return self._loader.validate(spec)

    def plan(
        self,
        spec: FleetSpec | ResolvedFleet | Mapping[str, Any],
        *,
        prune: bool = False,
    ) -> FleetPlan:
        """Return the plan that reconciles *spec* against the live fleet."""
        return self._planner.plan(spec, prune=prune)

    def apply(
        self,
        spec: FleetSpec | ResolvedFleet | Mapping[str, Any] | FleetPlan | None = None,
        *,
        prune: bool = False,
        dry_run: bool = False,
        plan: FleetPlan | None = None,
        force: bool | None = None,
        on_error: str | None = None,
    ) -> FleetPlan:
        """Apply a fleet spec (or a precomputed :class:`FleetPlan`).

        Accepts the spec positionally per the ``FleetReconciler`` protocol, and also
        accepts a plan positionally (``apply(plan)``) so callers can inspect a plan
        first and then execute exactly that plan.  ``dry_run=True`` never mutates
        anything and performs no manager calls beyond planning.
        """
        if isinstance(spec, FleetPlan):
            if plan is not None:
                raise FleetSpecError(
                    "apply() received both a plan and a plan= argument",
                    hint="pass exactly one of a fleet spec or a plan",
                )
            plan = spec
            spec = None
        if plan is None:
            if spec is None:
                raise FleetSpecError(
                    "apply() requires a fleet spec or a precomputed plan",
                    hint="call plan(spec) first or pass the FleetSpec directly",
                )
            plan = self.plan(spec, prune=prune)
        elif spec is not None:
            raise FleetSpecError(
                "apply() takes either a fleet spec or a plan, not both",
                hint="pass the spec, or pass the plan produced by plan()",
            )

        if dry_run:
            self._log.info(
                "fleet %s: dry run, planned %s", plan.spec_name, jsonable(plan.summary)
            )
            return plan
        if not plan.applyable:
            self._log.warning(
                "fleet %s: plan is not applyable (%d error(s)); nothing was applied",
                plan.spec_name,
                len(plan.errors),
            )
            return plan
        credential_errors = self._preflight_credentials(plan)
        if credential_errors:
            errors = list(dict.fromkeys([*plan.errors, *credential_errors]))
            self._log.error(
                "fleet %s: cannot apply, proxy credentials are unavailable", plan.spec_name
            )
            return plan.model_copy(update={"errors": errors, "applyable": False})
        policy = on_error or self._on_error
        if policy not in _ERROR_POLICIES:
            raise ValueError(
                f"on_error must be one of {', '.join(_ERROR_POLICIES)}; got {policy!r}"
            )
        force_flag = self._force if force is None else bool(force)
        return self._execute(plan, force=force_flag, policy=policy)

    def apply_plan(
        self,
        plan: FleetPlan,
        *,
        dry_run: bool = False,
        force: bool | None = None,
        on_error: str | None = None,
    ) -> FleetPlan:
        """Execute exactly the changes of an already computed plan."""
        return self.apply(plan, dry_run=dry_run, force=force, on_error=on_error)

    # -- execution ----------------------------------------------------------
    def _execute(self, plan: FleetPlan, *, force: bool, policy: str) -> FleetPlan:
        counts = {
            "created": 0,
            "updated": 0,
            "deleted": 0,
            "noop": 0,
            "applied": 0,
            "failed": 0,
        }
        outcomes: list[str] = []
        errors: list[str] = []
        stopped = False
        for change in plan.changes:
            if stopped:
                outcomes.append("skipped")
                continue
            if change.action is PlanAction.NOOP:
                outcomes.append("noop")
                counts["noop"] += 1
                continue
            if change.kind == "image":
                # Images are downloaded/imported by the images service; the fleet
                # reconciler only reports the requirement.
                outcomes.append("advisory")
                continue
            try:
                self._apply_change(change, force=force)
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed silently
                message = redact_text(str(exc)) or type(exc).__name__
                hint = redact_text(getattr(exc, "hint", None) or "")
                error = f"{change.kind} {change.name}: {message}"
                if hint:
                    error = f"{error} (hint: {hint})"
                errors.append(error)
                counts["failed"] += 1
                outcomes.append(f"failed: {message}")
                if policy == "stop":
                    stopped = True
                continue
            outcomes.append("applied")
            counts["applied"] += 1
            if change.action is PlanAction.CREATE:
                counts["created"] += 1
            elif change.action is PlanAction.UPDATE:
                counts["updated"] += 1
            elif change.action is PlanAction.DELETE:
                counts["deleted"] += 1

        updated_changes = [
            change.model_copy(
                update={"detail": _detail_with_outcome(change.detail, outcome)}
            )
            for change, outcome in zip(plan.changes, outcomes, strict=True)
        ]
        unique_errors = list(dict.fromkeys([*plan.errors, *errors]))
        result = plan.model_copy(
            update={
                "changes": updated_changes,
                "errors": unique_errors,
                "applyable": not unique_errors,
            }
        )
        if not errors:
            self._record_ownership(plan)
        self._emit(result, counts)
        self._log.info(
            "fleet %s applied: created=%d updated=%d deleted=%d noop=%d failed=%d",
            plan.spec_name,
            counts["created"],
            counts["updated"],
            counts["deleted"],
            counts["noop"],
            counts["failed"],
        )
        return result

    def _apply_change(self, change: PlanChange, *, force: bool) -> None:
        if change.kind == "proxy":
            self._apply_proxy(change)
            return
        if change.kind == "template":
            self._apply_template(change)
            return
        if change.kind == "instance":
            self._apply_instance(change, force=force)
            return
        raise FleetSpecError(
            f"unknown plan change kind: {change.kind!r}",
            hint="the plan was produced by an incompatible fleet planner version",
        )

    # -- proxies ------------------------------------------------------------
    def _apply_proxy(self, change: PlanChange) -> None:
        if self._proxies is None:
            raise FleetSpecError(
                f"no proxy pool is available to reconcile proxy {change.name!r}",
                hint="wire ProxyPool into the fleet reconciler",
            )
        after = change.after or {}
        proxy_id = _text(after.get("id") or change.name)
        if change.action is PlanAction.CREATE:
            self._create_proxy(proxy_id, after)
        elif change.action is PlanAction.UPDATE:
            self._update_proxy(proxy_id, after)

    def _create_proxy(self, proxy_id: str, after: Mapping[str, Any]) -> None:
        if self._proxies is None:  # pragma: no cover - guarded by caller
            raise FleetSpecError("no proxy pool is available")
        config = ProxyConfig(
            id=proxy_id,
            name=proxy_id,
            scheme=_proxy_scheme(after.get("scheme")),
            host=_text(after.get("host")),
            port=int(after.get("port") or 0),
            username=_optional_text(after.get("username")),
            password=self._secret_for_env(after.get("password_env"), required=True),
            tags=[str(tag) for tag in (after.get("tags") or [])],
            enabled=True,
            source=_text(after.get("source")) or "fleet",
        )
        try:
            call_method(self._proxies, "add", config)
        except ConflictError:
            # A concurrent creation or a manually added proxy: converge via update.
            values = _proxy_update_values(after)
            password = self._secret_for_env(after.get("password_env"), required=False)
            if password is not None:
                values["password"] = password
            call_method(self._proxies, "update", proxy_id, values)

    def _update_proxy(self, proxy_id: str, after: Mapping[str, Any]) -> None:
        if self._proxies is None:  # pragma: no cover - guarded by caller
            raise FleetSpecError("no proxy pool is available")
        values = _proxy_update_values(after)
        password = self._secret_for_env(after.get("password_env"), required=False)
        if password is not None:
            values["password"] = password
        call_method(self._proxies, "update", proxy_id, values)

    def _secret_for_env(self, variable: Any, *, required: bool) -> SecretStr | None:
        name = _text(variable)
        if not name:
            return None
        value = self._loader.environment().get(name)
        if value is None or value == "":
            if required:
                raise FleetSpecError(
                    f"proxy password environment variable {name!r} is not set",
                    hint=f"export {name} before applying the fleet; passwords never live in fleet.yaml",
                    details={"variable": name},
                )
            return None
        return SecretStr(value)

    def _preflight_credentials(self, plan: FleetPlan) -> list[str]:
        """Check every proxy create can read its password before mutating anything."""
        environment = self._loader.environment()
        errors: list[str] = []
        for change in plan.changes:
            if change.kind != "proxy" or change.action is not PlanAction.CREATE:
                continue
            variable = _text((change.after or {}).get("password_env"))
            if not variable:
                continue
            if environment.get(variable):
                continue
            errors.append(
                f"proxy {change.name}: password environment variable {variable!r} is not set"
            )
        return errors

    # -- templates ----------------------------------------------------------
    def _apply_template(self, change: PlanChange) -> None:
        after = change.after or {}
        before = change.before or {}
        template_id = _text(before.get("id") or after.get("id") or after.get("name") or change.name)
        template = Template(
            id=template_id,
            name=_text(after.get("name") or template_id),
            image_id=_text(after.get("image")),
            device_id=_text(after.get("device"), "pixel_7"),
            ram_mb=int(after.get("ram_mb") or 2048),
            heap_mb=int(after.get("heap_mb") or 256),
            cpu_cores=int(after.get("cpu_cores") or 2),
            gpu_mode=_gpu_mode(after.get("gpu_mode")),
            headless=bool(after.get("headless", True)),
            proxy_id=_optional_text(after.get("proxy")),
            scripts=[str(script) for script in (after.get("scripts") or [])],
            tags=[str(tag) for tag in (after.get("tags") or [])],
        )
        call_method(self._avds, "save_template", template)

    # -- instances ----------------------------------------------------------
    def _apply_instance(self, change: PlanChange, *, force: bool) -> None:
        after = change.after or {}
        before = change.before or {}
        name = _text(after.get("name") or before.get("name") or change.name)
        if change.action is PlanAction.CREATE:
            self._create_instance(name, after)
        elif change.action is PlanAction.UPDATE:
            self._update_instance(name, after, before)
        elif change.action is PlanAction.DELETE:
            self._delete_instance(name, force=force)

    def _create_instance(self, name: str, after: Mapping[str, Any]) -> None:
        template_id = _text(after.get("template"))
        if not template_id:
            raise FleetSpecError(
                f"instance {name!r} has no template reference",
                hint="add template: TEMPLATE_NAME to the instance in fleet.yaml",
            )
        overrides: dict[str, Any] = {}
        override_ram = after.get("override_ram_mb")
        if isinstance(override_ram, int) and not isinstance(override_ram, bool):
            overrides["ram_mb"] = override_ram
        call_method(self._avds, "create_instance", template_id, name, overrides or None)
        self._sync_assignment(name, _optional_text(after.get("proxy")))

    def _update_instance(
        self, name: str, after: Mapping[str, Any], before: Mapping[str, Any]
    ) -> None:
        template_id = _text(after.get("template"))
        template_changed = template_id != _text(before.get("template"))
        if template_changed:
            self._redeploy_instance(name, template_id)
        override_ram = after.get("override_ram_mb")
        previous_override = before.get("override_ram_mb")
        if isinstance(override_ram, int) and (
            template_changed or override_ram != previous_override
        ):
            call_method(self._avds, "update_config", name, {"hw.ramSize": str(override_ram)})
        self._sync_assignment(name, _optional_text(after.get("proxy")))

    def _redeploy_instance(self, name: str, template_id: str) -> None:
        for method_name in ("recreate_instance", "redeploy_instance", "change_template"):
            method = getattr(self._avds, method_name, None)
            if not callable(method):
                continue
            keyword = {"name": name, "avd_name": name, "template_id": template_id}
            try:
                invoke_compatible(method, [name, template_id], keyword)
            except TypeError:
                invoke_compatible(method, [], keyword)
            return
        raise FleetSpecError(
            f"cannot change the template of existing instance {name!r}",
            hint=(
                "the AVD manager has no redeploy operation; delete the instance "
                "(or apply with --prune after removing it from the spec) and apply again"
            ),
            details={"instance": name, "template": template_id},
        )

    def _delete_instance(self, name: str, *, force: bool) -> None:
        try:
            info = call_method(self._avds, "get_instance", name)
        except NotFoundError:
            return  # already gone: deletion is idempotent
        except FleetSpecError:
            info = None  # manager cannot report state; let delete_instance decide
        except Exception as exc:
            raise FleetSpecError(
                f"cannot read the state of instance {name!r}: {redact_text(str(exc))}",
                hint="retry, or pass force=True to delete without a state check",
            ) from exc
        if info is not None and not force and _is_active(getattr(info, "state", None)):
            raise FleetSpecError(
                f"refusing to delete running instance {name!r}",
                hint="stop the instance first, or apply with force=True to delete running instances",
                details={"instance": name},
            )
        if self._assignments is not None:
            try:
                call_method(self._assignments, "unassign", name)
            except Exception:  # noqa: BLE001 - the delete below is authoritative
                self._log.debug("could not unassign proxy for deleted instance %s", name)
        call_method(self._avds, "delete_instance", name, delete_files=True)

    def _sync_assignment(self, name: str, proxy_id: str | None) -> None:
        if self._assignments is None:
            if proxy_id:
                raise FleetSpecError(
                    f"instance {name!r} requires proxy {proxy_id!r} but no proxy assigner is available",
                    hint="wire ProxyAssigner into the fleet reconciler",
                )
            return
        current: str | None = None
        try:
            existing = call_method(self._assignments, "get", name)
            current = _optional_text(getattr(existing, "proxy_id", None))
        except FleetSpecError:
            current = None
        except Exception:
            current = None
        if proxy_id:
            if current != proxy_id:
                call_method(self._assignments, "assign", name, proxy_id)
        elif current:
            call_method(self._assignments, "unassign", name)

    # -- bookkeeping --------------------------------------------------------
    def _record_ownership(self, plan: FleetPlan) -> None:
        if self._store is None:
            return
        instances = sorted(
            {
                _text((change.after or {}).get("name") or change.name)
                for change in plan.changes
                if change.kind == "instance"
                and change.action is not PlanAction.DELETE
                and change.after
            }
        )
        templates = sorted(
            {
                _text(
                    (change.after or {}).get("id")
                    or (change.after or {}).get("name")
                    or change.name
                )
                for change in plan.changes
                if change.kind == "template"
                and change.action is not PlanAction.DELETE
                and change.after
            }
        )
        write_store_setting(
            self._store, fleet_settings_key(plan.spec_name, "instances"), instances
        )
        write_store_setting(
            self._store, fleet_settings_key(plan.spec_name, "templates"), templates
        )

    def _emit(self, plan: FleetPlan, counts: Mapping[str, int]) -> None:
        bus = self._events
        if bus is None:
            return
        emit = getattr(bus, "emit", None)
        if not callable(emit):
            return
        payload = {
            "fleet": plan.spec_name,
            "summary": jsonable(plan.summary),
            "counts": dict(counts),
            "applyable": plan.applyable,
            "errors": list(plan.errors),
        }
        try:
            try:
                emit(EventType.FLEET_APPLIED, payload)
            except TypeError:
                emit(EventType.FLEET_APPLIED, payload, source="avdfleet.fleetspec")
        except Exception as exc:  # noqa: BLE001 - telemetry must never break an apply
            self._log.warning("could not emit fleet.applied event: %s", redact_text(str(exc)))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _proxy_scheme(value: Any) -> ProxyScheme:
    try:
        return ProxyScheme(_text(value) or "http")
    except ValueError:
        raise FleetSpecError(
            f"unsupported proxy scheme {value!r}",
            hint="use one of http, https or socks5",
        ) from None


def _gpu_mode(value: Any) -> GpuMode:
    try:
        return GpuMode(_text(value) or _DEFAULT_GPU_MODE.value)
    except ValueError:
        return _DEFAULT_GPU_MODE


def _proxy_update_values(after: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key in ("scheme", "host", "port", "username", "tags"):
        if key in after:
            values[key] = after[key]
    return values


def _is_active(state: Any) -> bool:
    if state is None:
        return False
    active = getattr(state, "active", None)
    if isinstance(active, bool):
        return active
    value = getattr(state, "value", state)
    return str(value).strip().lower() in _ACTIVE_STATES


def _detail_with_outcome(detail: str, outcome: str) -> str:
    if outcome in {"noop", "advisory"}:
        return detail
    suffix = f"result={outcome}"
    return f"{detail}; {suffix}" if detail else suffix


# Backwards/forwards compatible aliases used by container wiring and docs.
Reconciler = FleetReconciler
FleetReconcilerService = FleetReconciler

__all__ = ["FleetReconciler", "FleetReconcilerService", "Reconciler"]
