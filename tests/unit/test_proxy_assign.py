"""Unit tests for :class:`avdfleet.proxy.assign.AssignmentService`.

Covers the three strategies (round robin, deterministic random, healthiest),
disabled-proxy skipping, reuse avoidance when enough proxies exist, optional
exclusive 1:1 mode, callbacks and the real D1 assignment repository.
"""

from __future__ import annotations

from typing import Any

import pytest

from avdfleet.core.errors import CapacityError, ConflictError, NotFoundError, ProxyError, ValidationError
from avdfleet.core.models import (
    ProxyAssignment,
    ProxyConfig,
    ProxyHealth,
    ProxyHealthStatus,
    ProxyScheme,
    utcnow,
)
from avdfleet.proxy.assign import AssignmentService
from avdfleet.proxy.pool import ProxyPoolService


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------
class FakeAssignmentRepo:
    """In-memory stand-in for ``store.repositories.AssignmentRepository``."""

    def __init__(self) -> None:
        self.items: dict[str, ProxyAssignment] = {}

    def list(self) -> list[ProxyAssignment]:
        return sorted(self.items.values(), key=lambda item: item.avd_name)

    def get(self, avd_name: str) -> ProxyAssignment | None:
        return self.items.get(avd_name)

    def assign(self, avd_name: str, proxy_id: str) -> ProxyAssignment:
        assignment = ProxyAssignment(avd_name=avd_name, proxy_id=proxy_id, assigned_at=utcnow())
        self.items[avd_name] = assignment
        return assignment

    def unassign(self, avd_name: str) -> bool:
        return self.items.pop(avd_name, None) is not None


class SingleArgumentAssignmentRepo(FakeAssignmentRepo):
    """Variant accepting a whole ``ProxyAssignment`` (arity detection path)."""

    def assign(self, assignment: ProxyAssignment) -> ProxyAssignment:  # type: ignore[override]
        self.items[assignment.avd_name] = assignment
        return assignment


class FakeStore:
    def __init__(self, repo: FakeAssignmentRepo | None = None) -> None:
        self.assignments = repo or FakeAssignmentRepo()


class PoolStub:
    """Minimal ProxyLister double backed by explicit configs."""

    def __init__(self, proxies: list[ProxyConfig]) -> None:
        self._proxies = {proxy.id: proxy for proxy in sorted(proxies, key=lambda item: item.id)}

    def list_proxies(self, *, include_disabled: bool = False) -> list[ProxyConfig]:
        return [proxy for proxy in self._proxies.values() if include_disabled or bool(proxy.enabled)]

    def get(self, proxy_id: str) -> ProxyConfig:
        proxy = self._proxies.get(proxy_id)
        if proxy is None:
            raise NotFoundError(f"proxy not found: {proxy_id}")
        return proxy


def _proxy(proxy_id: str, *, health: ProxyHealth | None = None, enabled: bool = True) -> ProxyConfig:
    return ProxyConfig(
        id=proxy_id,
        name=proxy_id,
        scheme=ProxyScheme.HTTP,
        host=f"{proxy_id}.example.com",
        port=8080,
        tags=[],
        enabled=enabled,
        health=health or ProxyHealth(),
    )


def _service(
    proxies: list[ProxyConfig] | None = None,
    *,
    seed: int | None = None,
    exclusive: bool = False,
    on_assign: Any = None,
    repo: FakeAssignmentRepo | None = None,
) -> tuple[AssignmentService, FakeAssignmentRepo]:
    assignments = repo or FakeAssignmentRepo()
    service = AssignmentService(
        FakeStore(assignments),
        PoolStub(proxies or [_proxy("p1"), _proxy("p2"), _proxy("p3")]),
        seed=seed,
        exclusive=exclusive,
        on_assign=on_assign,
    )
    return service, assignments


def test_service_implements_core_proxy_assigner_protocol() -> None:
    from avdfleet.core.interfaces import ProxyAssigner

    service, _ = _service()
    assert isinstance(service, ProxyAssigner)


# ---------------------------------------------------------------------------
# Basic assignment API
# ---------------------------------------------------------------------------
def test_assign_get_list_and_unassign() -> None:
    service, repo = _service()
    assignment = service.assign("avd-01", "p2")
    assert assignment.avd_name == "avd-01"
    assert assignment.proxy_id == "p2"
    assert assignment.assigned_at is not None
    assert service.get("avd-01") == assignment
    assert repo.items["avd-01"] == assignment
    assert service.list_assignments() == [assignment]

    service.unassign("avd-01")
    assert service.get("avd-01") is None
    assert service.list_assignments() == []
    service.unassign("avd-01")  # idempotent
    assert service.get("") is None
    assert service.get("avd-01") is None


def test_assign_rejects_missing_or_disabled_proxy() -> None:
    service, repo = _service([_proxy("p1"), _proxy("p2", enabled=False)])
    with pytest.raises(NotFoundError):
        service.assign("avd-01", "missing")
    with pytest.raises(ProxyError):
        service.assign("avd-01", "p2")
    assert repo.items == {}
    with pytest.raises(ValidationError):
        service.assign("   ", "p1")


def test_assignment_callback_mirrors_instance_proxy_id() -> None:
    instances: dict[str, str | None] = {}

    def on_assign(avd_name: str, proxy_id: str | None) -> None:
        instances[avd_name] = proxy_id

    service, _ = _service(on_assign=on_assign)
    service.assign("avd-01", "p1")
    assert instances == {"avd-01": "p1"}
    service.unassign("avd-01")
    assert instances == {"avd-01": None}


def test_assignment_callback_failure_does_not_break_assignment() -> None:
    def failing(avd_name: str, proxy_id: str | None) -> None:
        raise RuntimeError("instance repo broke")

    service, repo = _service(on_assign=failing)
    assignment = service.assign("avd-01", "p1")
    assert assignment.proxy_id == "p1"
    assert repo.items["avd-01"].proxy_id == "p1"


def test_single_argument_repository_variant_is_supported() -> None:
    repo = SingleArgumentAssignmentRepo()
    service, _ = _service(repo=repo)
    assignment = service.assign("avd-01", "p3")
    assert assignment.proxy_id == "p3"
    assert repo.items["avd-01"].proxy_id == "p3"
    assert service.auto_assign(["avd-02"], strategy="healthiest")[0].avd_name == "avd-02"


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
def test_round_robin_rotates_in_id_order() -> None:
    service, _ = _service([_proxy("p1"), _proxy("p2"), _proxy("p3")])
    assignments = service.auto_assign(["avd-01", "avd-02", "avd-03", "avd-04", "avd-05"])
    assert [assignment.avd_name for assignment in assignments] == [
        "avd-01",
        "avd-02",
        "avd-03",
        "avd-04",
        "avd-05",
    ]
    assert [assignment.proxy_id for assignment in assignments] == ["p1", "p2", "p3", "p1", "p2"]


def test_strategy_avoids_reuse_when_enough_proxies() -> None:
    proxies = [_proxy(f"p{index}") for index in range(1, 6)]
    service, _ = _service(proxies)
    assignments = service.auto_assign([f"avd-0{index}" for index in range(1, 6)], strategy="random")
    chosen = [assignment.proxy_id for assignment in assignments]
    assert len(set(chosen)) == 5


def test_random_strategy_is_deterministic_with_seed() -> None:
    proxies = [_proxy(f"p{index}") for index in range(1, 6)]
    names = [f"avd-0{index}" for index in range(1, 6)]
    first, _ = _service(proxies, seed=4242)
    second, _ = _service(proxies, seed=4242)
    first_ids = [assignment.proxy_id for assignment in first.auto_assign(names, strategy="random")]
    second_ids = [assignment.proxy_id for assignment in second.auto_assign(names, strategy="random")]
    assert first_ids == second_ids
    assert len(set(first_ids)) == 5


def test_healthiest_strategy_prefers_healthy_and_fast() -> None:
    proxies = [
        _proxy("p1", health=ProxyHealth(status=ProxyHealthStatus.FAILED)),
        _proxy("p2", health=ProxyHealth(status=ProxyHealthStatus.OK, latency_ms=40.0)),
        _proxy("p3", health=ProxyHealth(status=ProxyHealthStatus.OK, latency_ms=10.0)),
        _proxy("p4", health=ProxyHealth(status=ProxyHealthStatus.UNKNOWN)),
    ]
    service, _ = _service(proxies)
    assignments = service.auto_assign(["avd-01", "avd-02", "avd-03", "avd-04"], strategy="healthiest")
    assert [assignment.proxy_id for assignment in assignments] == ["p3", "p2", "p4", "p1"]


def test_unknown_strategy_is_rejected() -> None:
    service, _ = _service()
    with pytest.raises(ValidationError):
        service.auto_assign(["avd-01"], strategy="magic")
    assert service.auto_assign([]) == []
    assert service.auto_assign("") == []


def test_disabled_proxies_are_skipped_by_strategies() -> None:
    proxies = [_proxy("p1"), _proxy("p2", enabled=False), _proxy("p3", enabled=False)]
    service, _ = _service(proxies)
    assignments = service.auto_assign(["avd-01", "avd-02"])
    assert [assignment.proxy_id for assignment in assignments] == ["p1", "p1"]


def test_no_enabled_proxies_raises() -> None:
    service, _ = _service([_proxy("p1", enabled=False)])
    with pytest.raises(ProxyError):
        service.auto_assign(["avd-01"])


def test_existing_assignments_are_avoided() -> None:
    proxies = [_proxy("p1"), _proxy("p2")]
    repo = FakeAssignmentRepo()
    repo.items["avd-02"] = ProxyAssignment(avd_name="avd-02", proxy_id="p1")
    service, _ = _service(proxies, repo=repo)
    assignment = service.auto_assign(["avd-01"])[0]
    assert assignment.proxy_id == "p2"
    # Reassigning an AVD frees its previous proxy in the reuse budget.
    reassigned = service.auto_assign(["avd-02"])[0]
    assert reassigned.proxy_id == "p1"
    assert service.get("avd-01") is not None


# ---------------------------------------------------------------------------
# Exclusive 1:1 mode
# ---------------------------------------------------------------------------
def test_exclusive_mode_prevents_shared_proxies() -> None:
    service, repo = _service([_proxy("p1"), _proxy("p2")], exclusive=True)
    service.assign("avd-01", "p1")
    with pytest.raises(ConflictError):
        service.assign("avd-02", "p1")
    assert "avd-02" not in repo.items
    # ...but the other proxy is still available.
    assert service.assign("avd-02", "p2").proxy_id == "p2"


def test_exclusive_auto_assign_is_unique() -> None:
    proxies = [_proxy(f"p{index}") for index in range(1, 4)]
    service, _ = _service(proxies, exclusive=True)
    assignments = service.auto_assign(["avd-01", "avd-02", "avd-03"])
    assert len({assignment.proxy_id for assignment in assignments}) == 3


def test_exclusive_auto_assign_capacity_error_is_atomic() -> None:
    service, repo = _service([_proxy("p1"), _proxy("p2")], exclusive=True)
    with pytest.raises(CapacityError):
        service.auto_assign(["avd-01", "avd-02", "avd-03"])
    assert repo.items == {}


# ---------------------------------------------------------------------------
# Real D1 assignment repository integration
# ---------------------------------------------------------------------------
def test_real_store_integration(tmp_path: Any) -> None:
    store_module = pytest.importorskip("avdfleet.store")
    store = store_module.Store(tmp_path / "avdfleet.db")
    store.initialize()
    try:
        pool = ProxyPoolService(store, None)
        pool.add(host="one.example.com", port=8080)
        pool.add(host="two.example.com", port=8080)
        service = AssignmentService(store, pool)
        assignments = service.auto_assign(["avd-01", "avd-02"], strategy="round_robin")
        assert [assignment.proxy_id for assignment in assignments] == [
            pool.list_proxies()[0].id,
            pool.list_proxies()[1].id,
        ]
        assert store.assignments.get("avd-01") is not None
        assert len(service.list_assignments()) == 2

        reloaded = AssignmentService(store)
        assert reloaded.get("avd-02") is not None
        reloaded.unassign("avd-01")
        assert store.assignments.get("avd-01") is None
    finally:
        store.close()
