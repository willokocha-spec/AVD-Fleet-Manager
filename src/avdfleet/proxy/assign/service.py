"""Proxy assignment strategies for AVD instances.

Implements :class:`avdfleet.core.interfaces.ProxyAssigner` over the D1
``store.assignments`` repository (``list/get/assign/unassign``).  The repository
may either take ``assign(avd_name, proxy_id)`` (the D1 contract) or a single
:class:`~avdfleet.core.models.ProxyAssignment`; the arity is detected once at
construction so a hand-written fake keeps working.

Design rules:

* disabled proxies are never assigned;
* the assignment is 1:1 by default in practice -- strategies avoid reusing a
  proxy while an unused enabled proxy exists -- but reuse is **allowed** unless
  ``exclusive=True`` (the product only requires *one* proxy per instance, not
  one instance per proxy);
* with ``exclusive=True`` a conflict or capacity error is raised instead of
  reusing;
* every strategy is deterministic: ``round_robin``/``healthiest`` order by id,
  ``random`` uses an optional fixed seed;
* ``on_assign(avd_name, proxy_id | None)`` is an optional callback used to
  mirror the assignment onto the instance record (``instance.proxy_id``) or the
  bridge lifecycle.
"""

from __future__ import annotations

import inspect
import random
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, cast, runtime_checkable

from ...core.errors import CapacityError, ConflictError, ProxyError, ValidationError
from ...core.logging import get_logger, redact_text
from ...core.models import ProxyAssignment, ProxyConfig, ProxyHealthStatus, utcnow
from ..pool.service import ProxyPoolService

STRATEGIES: tuple[str, ...] = ("round_robin", "random", "healthiest")
_HEALTH_RANK: dict[ProxyHealthStatus, int] = {
    ProxyHealthStatus.OK: 0,
    ProxyHealthStatus.UNKNOWN: 1,
    ProxyHealthStatus.FAILED: 2,
}

AssignCallback = Callable[[str, str | None], None]


@runtime_checkable
class AssignmentRepository(Protocol):
    """Repository contract for proxy assignments (D1 store provides this)."""

    def list(self) -> list[ProxyAssignment]: ...

    def get(self, avd_name: str) -> ProxyAssignment | None: ...

    def assign(self, avd_name: str, proxy_id: str) -> ProxyAssignment: ...

    def unassign(self, avd_name: str) -> Any: ...


@runtime_checkable
class ProxyLister(Protocol):
    """The subset of the proxy pool needed by auto-assignment."""

    def list_proxies(self, *, include_disabled: bool = False) -> list[ProxyConfig]: ...

    def get(self, proxy_id: str) -> ProxyConfig: ...


class AssignmentService:
    """Assign proxies to AVD instances using deterministic strategies."""

    def __init__(
        self,
        store: Any,
        proxies: ProxyLister | None = None,
        *,
        seed: int | None = None,
        exclusive: bool = False,
        on_assign: AssignCallback | None = None,
    ) -> None:
        self._store = store
        self._repo: AssignmentRepository = getattr(store, "assignments", store)
        self._pool: ProxyLister = proxies if proxies is not None else ProxyPoolService(store)
        self._rng = random.Random(seed) if seed is not None else random.Random()
        self._exclusive = bool(exclusive)
        self._on_assign = on_assign
        self._assign_arity = _positional_arity(self._repo.assign)
        self._log = get_logger("proxy.assign")

    # -- core ProxyAssigner protocol ---------------------------------------
    def assign(self, avd_name: str, proxy_id: str) -> ProxyAssignment:
        avd = self._validate_name(avd_name)
        proxy = self._pool.get(proxy_id)
        if not proxy.enabled:
            raise ProxyError(f"proxy is disabled: {proxy_id}")
        if self._exclusive:
            for other in self._repo.list():
                if other.avd_name != avd and other.proxy_id == proxy_id:
                    raise ConflictError(f"proxy {proxy_id} is already assigned to {other.avd_name}")
        assignment = self._repo_assign(avd, proxy_id)
        self._notify(avd, proxy_id)
        return assignment

    def unassign(self, avd_name: str) -> None:
        avd = self._validate_name(avd_name)
        existing = self._repo.get(avd)
        if existing is None:
            return
        self._repo.unassign(avd)
        self._notify(avd, None)

    def get(self, avd_name: str) -> ProxyAssignment | None:
        if not isinstance(avd_name, str) or not avd_name.strip():
            return None
        return self._repo.get(avd_name.strip())

    def list_assignments(self) -> list[ProxyAssignment]:
        return list(self._repo.list())

    def auto_assign(
        self,
        avd_names: Sequence[str],
        *,
        strategy: str = "round_robin",
    ) -> list[ProxyAssignment]:
        """Assign one proxy per instance, following ``strategy``."""
        if strategy not in STRATEGIES:
            raise ValidationError(
                f"unknown assignment strategy: {strategy!r}",
                hint=f"expected one of {', '.join(STRATEGIES)}",
            )
        if isinstance(avd_names, str):
            avd_names = [avd_names]
        names = _dedupe(
            [self._validate_name(name) for name in avd_names if isinstance(name, str) and name.strip()]
        )
        if not names:
            return []
        candidates = [
            proxy
            for proxy in self._pool.list_proxies(include_disabled=False)
            if getattr(proxy, "enabled", True)
        ]
        if not candidates:
            raise ProxyError("no enabled proxies are available for assignment")
        if self._exclusive and len(candidates) < len(names):
            raise CapacityError(
                f"exclusive assignment needs {len(names)} proxies but only {len(candidates)} are enabled"
            )
        existing = {assignment.avd_name: assignment for assignment in self._repo.list()}
        target_names = set(names)
        used = {
            assignment.proxy_id
            for avd, assignment in existing.items()
            if avd not in target_names and assignment.proxy_id
        }
        ordered = self._order_candidates(candidates, strategy)
        cursor = len(existing) % len(ordered)
        assignments: list[ProxyAssignment] = []
        for name in names:
            proxy, cursor = self._pick(ordered, used, cursor)
            assignments.append(self._repo_assign(name, proxy.id))
            used.add(proxy.id)
            self._notify(name, proxy.id)
        return assignments

    # -- internals ----------------------------------------------------------
    def _order_candidates(self, candidates: list[ProxyConfig], strategy: str) -> list[ProxyConfig]:
        if strategy == "random":
            shuffled = list(candidates)
            self._rng.shuffle(shuffled)
            return shuffled
        if strategy == "healthiest":
            return sorted(candidates, key=_health_key)
        return sorted(candidates, key=lambda proxy: proxy.id)

    @staticmethod
    def _pick(
        ordered: list[ProxyConfig],
        used: set[str],
        cursor: int,
    ) -> tuple[ProxyConfig, int]:
        """Pick the next unused proxy from the rotating order; reuse only as a last resort."""
        count = len(ordered)
        for offset in range(count):
            index = (cursor + offset) % count
            candidate = ordered[index]
            if candidate.id not in used:
                return candidate, (index + 1) % count
        index = cursor % count
        return ordered[index], (index + 1) % count

    def _repo_assign(self, avd_name: str, proxy_id: str) -> ProxyAssignment:
        repo = cast(Any, self._repo)
        if self._assign_arity <= 1:
            fallback = ProxyAssignment(avd_name=avd_name, proxy_id=proxy_id, assigned_at=utcnow())
            result = repo.assign(fallback)
        else:
            result = repo.assign(avd_name, proxy_id)
        return _coerce_assignment(result, avd_name=avd_name, proxy_id=proxy_id)

    def _notify(self, avd_name: str, proxy_id: str | None) -> None:
        if self._on_assign is None:
            return
        try:
            self._on_assign(avd_name, proxy_id)
        except Exception as exc:  # a callback must never break persisted assignment state
            self._log.warning(
                "assignment callback failed for %s: %s",
                avd_name,
                redact_text(str(exc)),
            )

    @staticmethod
    def _validate_name(avd_name: str) -> str:
        if not isinstance(avd_name, str) or not avd_name.strip():
            raise ValidationError("instance name is required")
        return avd_name.strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _health_key(proxy: ProxyConfig) -> tuple[int, float, str]:
    status = proxy.health.status if proxy.health is not None else ProxyHealthStatus.UNKNOWN
    latency = (
        proxy.health.latency_ms if proxy.health and proxy.health.latency_ms is not None else float("inf")
    )
    return (_HEALTH_RANK.get(status, 1), latency, proxy.id)


def _positional_arity(func: Any) -> int:
    """Count positional parameters of a bound method (fallback: two-argument form)."""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return 2
    parameters = list(signature.parameters.values())
    if any(parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters):
        return 2
    return sum(
        1
        for parameter in parameters
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    )


def _coerce_assignment(result: Any, *, avd_name: str, proxy_id: str) -> ProxyAssignment:
    if isinstance(result, ProxyAssignment):
        return result
    if isinstance(result, Mapping):
        try:
            return ProxyAssignment.model_validate(dict(result))
        except Exception:
            pass
    if result is not None and hasattr(result, "avd_name") and hasattr(result, "proxy_id"):
        return ProxyAssignment(
            avd_name=str(result.avd_name),
            proxy_id=str(result.proxy_id),
            assigned_at=getattr(result, "assigned_at", None),
        )
    return ProxyAssignment(avd_name=avd_name, proxy_id=proxy_id, assigned_at=utcnow())


def strategy_names() -> list[str]:
    """Return the supported assignment strategies (for API validation/tests)."""
    return list(STRATEGIES)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


__all__ = [
    "STRATEGIES",
    "AssignCallback",
    "AssignmentRepository",
    "AssignmentService",
    "ProxyLister",
    "strategy_names",
]
