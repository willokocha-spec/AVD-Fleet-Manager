"""Unit tests for :class:`avdfleet.proxy.pool.ProxyPoolService`.

The service is exercised against in-memory store/vault doubles and (when the D1
store package is present) against the real SQLite repository.  The tests assert
the two central invariants: passwords live **only** in the vault and never leak
through repr/str/errors/views, and health checks persist through
``update_health``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from avdfleet.core.config import Config
from avdfleet.core.errors import ConflictError, NotFoundError, ValidationError, VaultError
from avdfleet.core.models import ProxyConfig, ProxyHealth, ProxyHealthStatus, ProxyScheme, ProxyTestResult
from avdfleet.proxy.pool import ProxyPoolService

SECRET = "s3cr3t-proxy-pass"
VERIFY_URL = "https://verify.example.invalid"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------
class FakeVault:
    """Minimal in-memory Vault protocol implementation."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.locked = False
        self.available_flag = True

    def available(self) -> bool:
        return self.available_flag

    def unlock(self, passphrase: str | None = None) -> None:
        self.locked = False

    def lock(self) -> None:
        self.locked = True

    def _check(self) -> None:
        if self.locked:
            raise VaultError("vault is locked")

    def get(self, key: str) -> str | None:
        self._check()
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self._check()
        self.data[key] = value

    def delete(self, key: str) -> None:
        self._check()
        self.data.pop(key, None)

    def keys(self) -> list[str]:
        self._check()
        return list(self.data)


class InMemoryProxyRepo:
    """In-memory stand-in for ``store.repositories.ProxyRepository``."""

    def __init__(self) -> None:
        self.records: dict[str, Any] = {}
        self.health_updates: list[tuple[str, ProxyHealth]] = []
        self.upserts = 0
        self.deleted: list[str] = []

    def list(self, include_disabled: bool = False) -> list[Any]:
        records = [record for record in self.records.values() if include_disabled or bool(record.enabled)]
        return sorted(records, key=lambda record: (str(record.name), str(record.id)))

    def get(self, proxy_id: str) -> Any | None:
        return self.records.get(proxy_id)

    def get_by_name(self, name: str) -> Any | None:
        for record in self.records.values():
            if record.name == name:
                return record
        return None

    def upsert(self, record: Any) -> Any:
        self.upserts += 1
        self.records[record.id] = record
        return record

    def update_health(self, proxy_id: str, health: ProxyHealth) -> Any:
        record = self.records.get(proxy_id)
        if record is None:
            raise NotFoundError(f"proxy {proxy_id!r} not found")
        record.health = health
        self.health_updates.append((proxy_id, health))
        return record

    def delete(self, proxy_id: str) -> None:
        if proxy_id not in self.records:
            raise NotFoundError(f"proxy {proxy_id!r} not found")
        self.deleted.append(proxy_id)
        del self.records[proxy_id]


class FakeStore:
    """Store double exposing a ``.proxies`` repository (the D1 shape)."""

    def __init__(self) -> None:
        self.proxies = InMemoryProxyRepo()


def _config(**overrides: Any) -> Config:
    values: dict[str, Any] = {
        "proxy_verify_url": VERIFY_URL,
        "proxy_verify_timeout_s": 15,
    }
    values.update(overrides)
    return Config(**values)


def _service(
    vault: FakeVault | None = None,
    *,
    config: Config | None = None,
    client_factory: Any = None,
) -> tuple[ProxyPoolService, FakeStore, FakeVault]:
    store = FakeStore()
    effective_vault = vault if vault is not None else FakeVault()
    service = ProxyPoolService(
        store,
        effective_vault,
        config or _config(),
        client_factory=client_factory,
    )
    return service, store, effective_vault


def _add_sample(service: ProxyPoolService, **overrides: Any) -> ProxyConfig:
    fields: dict[str, Any] = {
        "url": f"http://user1:{SECRET}@gateway.example.com:8080",
        "name": "gateway",
    }
    fields.update(overrides)
    return service.add(**fields)


def _mock_client(captured: dict[str, Any], response_factory: Any) -> Any:
    def factory(upstream: str, timeout: float) -> httpx.Client:
        captured["upstream"] = upstream
        captured["timeout"] = timeout
        transport = httpx.MockTransport(response_factory)
        return httpx.Client(transport=transport, timeout=timeout)

    return factory


# ---------------------------------------------------------------------------
# add / storage
# ---------------------------------------------------------------------------
def test_service_implements_core_proxy_pool_protocol() -> None:
    from avdfleet.core.interfaces import ProxyPool

    service, _, _ = _service()
    assert isinstance(service, ProxyPool)


def test_add_stores_password_only_in_vault() -> None:
    service, store, vault = _service()
    config = _add_sample(service)

    assert config.password is None
    assert SECRET not in repr(config) and SECRET not in str(config)
    record = store.proxies.records[config.id]
    assert record.secret_ref == f"proxy/{config.id}"
    assert vault.data[record.secret_ref] == SECRET
    assert not hasattr(record, "password")
    assert SECRET not in repr(record)

    view = service.to_view(config)
    assert view["has_credentials"] is True
    assert view["username"] == "user1"
    assert "password" not in view
    assert SECRET not in json.dumps(view)


def test_add_accepts_proxy_config_and_keyword_fields() -> None:
    service, store, vault = _service()
    manual = ProxyConfig(
        id="manual-id",
        name="manual",
        scheme=ProxyScheme.SOCKS5,
        host="10.0.0.2",
        port=1080,
        username="u2",
        password=SECRET,  # type: ignore[arg-type] - pydantic coerces to SecretStr
        tags=["a"],
    )
    added = service.add(manual)
    assert added.id == "manual-id"
    record = store.proxies.records["manual-id"]
    assert record.scheme == ProxyScheme.SOCKS5
    assert record.secret_ref == "proxy/manual-id"

    deterministic = service.add(host="10.0.0.3", port=3128, scheme="socks5h")
    assert deterministic.id.startswith("10.0.0.3-3128-")
    assert deterministic.scheme is ProxyScheme.SOCKS5
    assert service.get(deterministic.id).name == "10.0.0.3:3128"
    assert f"proxy/{deterministic.id}" not in vault.data


def test_add_rejects_duplicates_and_leaks_no_secret() -> None:
    service, store, _ = _service()
    _add_sample(service)
    with pytest.raises(ConflictError) as raised:
        _add_sample(service)
    assert SECRET not in str(raised.value)
    assert len(store.proxies.records) == 1

    with pytest.raises(ConflictError):
        service.add(url="http://user1:other@other.example.com:8080", name="gateway")


def test_add_validation_errors() -> None:
    service, store, _ = _service()
    with pytest.raises((ValidationError, VaultError)):
        service.add(host="bad host", port=8080)
    with pytest.raises(ValidationError):
        service.add(host="h.example.com", port=0)
    with pytest.raises(ValidationError):
        service.add(host="h.example.com", port=8080, scheme="ftp")
    with pytest.raises(ValidationError):
        service.add(host="h.example.com", port=8080, password=SECRET)
    assert store.proxies.records == {}


def test_password_requires_a_vault() -> None:
    store = FakeStore()
    service = ProxyPoolService(store, None, _config())
    with pytest.raises(VaultError):
        service.add(url=f"http://u:{SECRET}@h.example.com:8080")
    assert store.proxies.records == {}
    anonymous = service.add(host="h.example.com", port=8080)
    assert anonymous.id in store.proxies.records
    assert service.to_view(anonymous)["has_credentials"] is False


def test_import_text_partial_success_and_duplicates() -> None:
    service, store, vault = _service()
    text = "\n".join(
        [
            f"http://u1:{SECRET}@one.example.com:8080",
            "socks5://u2:other@two.example.com:1080",
            f"http://u1:{SECRET}@ONE.example.com:8080",  # duplicate inside the paste
            "this is not a proxy",
            f"ftp://u:{SECRET}@three.example.com:1",  # invalid scheme
        ]
    )
    result = service.import_text(text, source="paste")
    assert len(result.added) == 2
    assert result.skipped == 1
    assert result.errors and all(SECRET not in error for error in result.errors)
    assert all(config.password is None for config in result.added)
    assert len(vault.data) == 2
    assert all(record.source == "paste" for record in store.proxies.records.values())

    again = service.import_text(text)
    assert again.added == []
    assert again.skipped == 3
    assert SECRET not in "\n".join(again.errors)


def test_import_text_allows_many_credentials_behind_one_gateway() -> None:
    """Rotating providers export many credentials for a single ``host:port``.

    Every credential is a distinct proxy, so the importer must not reject the
    second and later entries as duplicate names (it used to import only one).
    """
    service, _store, vault = _service()
    text = "\n".join(
        f"http://session-{index:02d}-long-rotating-username:{SECRET}@gate.example.com:8080"
        for index in range(3)
    )
    result = service.import_text(text, source="export")

    assert result.added and len(result.added) == 3
    assert result.errors == []
    assert [config.name for config in result.added] == [
        "gate.example.com:8080",
        "gate.example.com:8080 #2",
        "gate.example.com:8080 #3",
    ]
    assert len({config.id for config in result.added}) == 3
    assert len(vault.data) == 3


def test_list_and_views_respect_disabled_flag() -> None:
    service, _, _ = _service()
    enabled = service.add(url="http://u:p@one.example.com:8080", name="one")
    disabled = service.add(url="http://u:p@two.example.com:8080", name="two", enabled=False)
    assert [config.id for config in service.list_proxies()] == [enabled.id]
    assert {config.id for config in service.list_proxies(include_disabled=True)} == {enabled.id, disabled.id}
    assert [view["id"] for view in service.list_views()] == [enabled.id]
    assert len(service.list_views(include_disabled=True)) == 2
    assert service.get(disabled.id).enabled is False
    assert service.view(enabled.id)["name"] == "one"
    assert service.view(service.get(enabled.id))["id"] == enabled.id


def test_get_missing_proxy_raises_not_found() -> None:
    service, _, _ = _service()
    with pytest.raises(NotFoundError):
        service.get("missing")
    with pytest.raises(NotFoundError):
        service.view("missing")


# ---------------------------------------------------------------------------
# update / delete
# ---------------------------------------------------------------------------
def test_update_merge_and_password_semantics() -> None:
    service, store, vault = _service()
    config = _add_sample(service)
    secret_ref = store.proxies.records[config.id].secret_ref

    unchanged = service.update(config.id, {"name": "renamed", "tags": ["new"]})
    assert unchanged.name == "renamed"
    assert unchanged.tags == ["new"]
    assert store.proxies.records[config.id].secret_ref == secret_ref
    assert vault.data[secret_ref] == SECRET

    kept = service.update(config.id, {"password": ""})
    assert kept.name == "renamed"
    assert store.proxies.records[config.id].secret_ref == secret_ref

    replaced = service.update(config.id, {"password": "new-pass"})
    assert store.proxies.records[config.id].secret_ref == secret_ref
    assert vault.data[secret_ref] == "new-pass"
    assert replaced.password is None

    removed = service.update(config.id, {"password": None})
    assert removed.password is None
    assert store.proxies.records[config.id].secret_ref is None
    assert vault.data == {}


def test_update_url_changes_endpoint_and_credentials() -> None:
    service, store, vault = _service()
    config = _add_sample(service)
    updated = service.update(
        config.id,
        {"url": f"socks5://user2:{SECRET}@other.example.com:1080"},
    )
    assert updated.scheme is ProxyScheme.SOCKS5
    assert updated.host == "other.example.com"
    assert updated.port == 1080
    assert updated.username == "user2"
    record = store.proxies.records[config.id]
    assert vault.data[record.secret_ref] == SECRET


def test_update_validation_is_atomic() -> None:
    service, store, _ = _service()
    config = _add_sample(service)
    before = store.proxies.records[config.id]
    with pytest.raises(ValidationError):
        service.update(config.id, {"port": 70000})
    assert store.proxies.records[config.id].port == 8080
    assert before.name == store.proxies.records[config.id].name
    with pytest.raises(NotFoundError):
        service.update("missing", {"name": "x"})


def test_delete_removes_record_and_secret() -> None:
    service, store, vault = _service()
    config = _add_sample(service)
    assert vault.data
    service.delete(config.id)
    assert config.id not in store.proxies.records
    assert vault.data == {}
    with pytest.raises(NotFoundError):
        service.delete(config.id)


# ---------------------------------------------------------------------------
# health checks
# ---------------------------------------------------------------------------
def test_check_success_uses_proxy_and_persists_health() -> None:
    captured: dict[str, Any] = {}
    factory = _mock_client(
        captured,
        lambda request: httpx.Response(200, json={"origin": "203.0.113.7"}),
    )
    service, store, _ = _service(client_factory=factory)
    config = _add_sample(service)

    result = service.check(config.id)
    assert isinstance(result, ProxyTestResult)
    assert result.ok is True
    assert result.status_code == 200
    assert result.latency_ms is not None
    assert result.egress_ip == "203.0.113.7"
    assert result.error == ""
    assert SECRET not in repr(result) and SECRET not in str(result)
    assert captured["upstream"] == f"http://user1:{SECRET}@gateway.example.com:8080"
    assert captured["timeout"] == 15.0
    assert store.proxies.health_updates[-1][0] == config.id
    assert store.proxies.health_updates[-1][1].status is ProxyHealthStatus.OK
    assert service.get(config.id).health.status is ProxyHealthStatus.OK
    assert service.get(config.id).health.egress_ip == "203.0.113.7"


def test_check_timeout_override() -> None:
    captured: dict[str, Any] = {}
    factory = _mock_client(captured, lambda request: httpx.Response(204))
    service, _, _ = _service(config=_config(proxy_verify_timeout_s=7), client_factory=factory)
    config = _add_sample(service)
    assert service.check(config.id).ok is True
    assert captured["timeout"] == 7.0
    service.check(config.id, timeout_s=2.5)
    assert captured["timeout"] == 2.5


def test_check_http_failure_marks_health_failed() -> None:
    captured: dict[str, Any] = {}
    factory = _mock_client(captured, lambda request: httpx.Response(502, text="bad gateway"))
    service, store, _ = _service(client_factory=factory)
    config = _add_sample(service)
    result = service.check(config.id)
    assert result.ok is False
    assert result.status_code == 502
    assert "502" in result.error
    assert store.proxies.health_updates[-1][1].status is ProxyHealthStatus.FAILED


def test_check_transport_error_redacts_vaulted_password() -> None:
    captured: dict[str, Any] = {}

    def failing_factory(upstream: str, timeout: float) -> httpx.Client:
        captured["upstream"] = upstream
        raise httpx.ConnectError(f"cannot connect through {upstream}")

    service, store, _ = _service(client_factory=failing_factory)
    config = _add_sample(service)
    result = service.check(config.id)
    assert result.ok is False
    assert SECRET not in result.error
    assert "***" in result.error
    assert "ConnectError" in result.error
    assert store.proxies.health_updates[-1][1].status is ProxyHealthStatus.FAILED


def test_check_locked_vault_does_not_leak_and_marks_failure() -> None:
    captured: dict[str, Any] = {}
    factory = _mock_client(captured, lambda request: httpx.Response(200))
    service, store, vault = _service(client_factory=factory)
    config = _add_sample(service)
    vault.lock()
    result = service.check(config.id)
    assert result.ok is False
    assert captured == {}  # the transport is never attempted with a locked vault
    assert SECRET not in result.error
    assert store.proxies.health_updates[-1][1].status is ProxyHealthStatus.FAILED


def test_check_all_skips_disabled_and_uses_real_httpx_path() -> None:
    respx = pytest.importorskip("respx")
    service, _, _ = _service()
    enabled = service.add(url="http://u:p@one.example.com:8080", name="enabled")
    service.add(url="http://u:p@two.example.com:8080", name="disabled", enabled=False)
    with respx.mock:
        route = respx.get(VERIFY_URL).mock(return_value=httpx.Response(200, json={"origin": "198.51.100.9"}))
        results = service.check_all()
    assert [result.proxy_id for result in results] == [enabled.id]
    assert results[0].ok is True
    assert results[0].egress_ip == "198.51.100.9"
    assert route.called


def test_upstream_url_hydrates_secret_but_views_never_do() -> None:
    service, _, _ = _service()
    config = _add_sample(service)
    assert service.upstream_url(config.id) == f"http://user1:{SECRET}@gateway.example.com:8080"
    view_json = json.dumps(service.list_views(include_disabled=True))
    assert SECRET not in view_json
    with pytest.raises(NotFoundError):
        service.upstream_url("missing")


def test_get_with_credentials_for_bridge_and_verifier() -> None:
    service, _, _ = _service()
    config = _add_sample(service)
    internal = service.get_with_credentials(config.id)
    assert internal.password is not None
    assert internal.password.get_secret_value() == SECRET
    assert SECRET not in repr(internal) and SECRET not in str(internal)
    # The public accessor stays password-free.
    assert service.get(config.id).password is None
    with pytest.raises(NotFoundError):
        service.get_with_credentials("missing")


# ---------------------------------------------------------------------------
# Real D1 store integration (skipped only if the store package is absent)
# ---------------------------------------------------------------------------
def test_real_store_integration(tmp_path: Any) -> None:
    store_module = pytest.importorskip("avdfleet.store")
    store = store_module.Store(tmp_path / "avdfleet.db")
    store.initialize()
    try:
        vault = FakeVault()
        service = ProxyPoolService(store, vault, _config())
        config = service.add(
            url=f"http://user1:{SECRET}@gateway.example.com:8080", name="gateway", tags=["eu"]
        )
        assert len(service.list_proxies()) == 1
        record = store.proxies.get(config.id)
        assert record.secret_ref == f"proxy/{config.id}"

        reloaded = ProxyPoolService(store, vault, _config())
        assert reloaded.get(config.id).name == "gateway"
        assert reloaded.to_view(config.id)["has_credentials"] is True
        assert reloaded.upstream_url(config.id) == f"http://user1:{SECRET}@gateway.example.com:8080"

        captured: dict[str, Any] = {}
        checking = ProxyPoolService(
            store,
            vault,
            _config(),
            client_factory=_mock_client(captured, lambda request: httpx.Response(200)),
        )
        assert checking.check(config.id).ok is True
        assert store.proxies.get(config.id).health.status is ProxyHealthStatus.OK

        updated = reloaded.update(config.id, {"name": "gateway-2", "password": None})
        assert updated.name == "gateway-2"
        assert store.proxies.get(config.id).secret_ref is None
        reloaded.delete(config.id)
        assert reloaded.list_proxies(include_disabled=True) == []
    finally:
        store.close()
