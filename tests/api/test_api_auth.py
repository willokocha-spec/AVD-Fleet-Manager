"""Authentication, token-store and auth-hardening tests for the daemon.

Self-contained: no tests/fakes fixtures, everything under pytest's ``tmp_path``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from avdfleet.api.app import create_app
from avdfleet.api.auth import (
    BootstrapCodeStore,
    TokenStore,
    TokenStoreError,
    extract_bearer,
    origin_allowed,
    tokens_equal,
)
from avdfleet.core.config import Config
from avdfleet.core.paths import Paths

ORIGIN = "http://127.0.0.1:8765"
BASE_URL = "http://127.0.0.1:8765"
TOKEN = "unit-test-token-0123456789-abcdefghij"


def make_config(*, auth_required: bool = True) -> Config:
    return Config(
        auth_required=auth_required,
        allow_origins=[ORIGIN],
        trusted_hosts=["127.0.0.1", "localhost"],
        serve_ui=False,
    )


class EmptyContainer:
    """Container with no services: enough for auth/system/config routes."""

    def __init__(self, config: Config, paths: Paths) -> None:
        self.config = config
        self.paths = paths


def make_paths(tmp_path: Path) -> Paths:
    return Paths(root=tmp_path, portable=True).ensure()


def auth_headers(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def auth_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.setenv("AVDFLEET_TOKEN", TOKEN)
    config = make_config()
    paths = make_paths(tmp_path)
    return create_app(config=config, container=EmptyContainer(config, paths), paths=paths)


@pytest.fixture()
def client(auth_app: Any) -> Any:
    with TestClient(auth_app, base_url=BASE_URL) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# HTTP authentication
# ---------------------------------------------------------------------------


def test_health_needs_no_token(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_missing_token_is_401_with_contract_envelope(client: TestClient) -> None:
    response = client.get("/api/v1/system")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthorized"
    assert body["error"]["message"] == "missing bearer token"
    assert body["error"]["hint"]
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]


def test_wrong_token_is_401(client: TestClient) -> None:
    response = client.get("/api/v1/system", headers=auth_headers("not-the-token"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.json()["error"]["message"] == "invalid bearer token"
    assert TOKEN not in response.text


def test_valid_token_is_accepted(client: TestClient) -> None:
    response = client.get("/api/v1/system", headers=auth_headers())
    assert response.status_code == 200
    assert response.json()["version"]

    check = client.get("/api/v1/auth/check", headers=auth_headers())
    assert check.status_code == 200
    assert check.json() == {"ok": True, "expires_at": None}

    config = client.get("/api/v1/config", headers=auth_headers())
    assert config.status_code == 200
    assert config.json()["auth_required"] is True


def test_bearer_scheme_is_case_insensitive(client: TestClient) -> None:
    response = client.get("/api/v1/system", headers={"Authorization": f"bearer {TOKEN}"})
    assert response.status_code == 200
    response = client.get("/api/v1/system", headers={"Authorization": f"BEARER {TOKEN}"})
    assert response.status_code == 200


@pytest.mark.parametrize(
    "header",
    ["Basic dXNlcjpwYXNz", "Bearer", "Bearer ", "Token abc", "abc"],
)
def test_malformed_authorization_is_401(client: TestClient, header: str) -> None:
    response = client.get("/api/v1/system", headers={"Authorization": header})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_token_never_appears_in_bodies_or_headers(client: TestClient) -> None:
    for path in ("/health", "/api/v1/config", "/api/v1/auth/check"):
        response = client.get(path, headers=auth_headers())
        assert TOKEN not in response.text
        assert TOKEN not in "".join(f"{key}:{value}" for key, value in response.headers.items())


def test_auth_disabled_allows_anonymous(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AVDFLEET_TOKEN", raising=False)
    config = make_config(auth_required=False)
    paths = make_paths(tmp_path)
    app = create_app(config=config, container=EmptyContainer(config, paths), paths=paths)
    with TestClient(app, base_url=BASE_URL) as client:
        assert client.get("/api/v1/system").status_code == 200
        assert client.get("/api/v1/auth/check").status_code == 200
        # No token file is created when authentication is disabled.
        assert not paths.token_path.exists()


# ---------------------------------------------------------------------------
# Token store
# ---------------------------------------------------------------------------


def test_token_store_creates_owner_only_file_and_persists(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    store = TokenStore(paths, environ={})
    token = store.ensure()
    assert len(token) >= 40
    assert token == paths.token_path.read_text(encoding="utf-8").strip()
    if os.name != "nt":
        mode = stat.S_IMODE(os.stat(paths.token_path).st_mode)
        assert mode == 0o600, oct(mode)

    # A second store (e.g. the CLI) reads the same token, no rotation.
    second = TokenStore(paths, environ={})
    assert second.ensure() == token
    assert second.matches(token)
    assert second.matches(" " + token + " ") is False  # no whitespace tolerance
    assert not second.matches(None)
    assert not second.matches("")
    assert not second.matches(token + "x")


def test_token_store_env_override_wins_and_never_writes(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    store = TokenStore(paths, environ={"AVDFLEET_TOKEN": TOKEN})
    assert store.token_from_environment is True
    assert store.source == "environment"
    assert store.ensure() == TOKEN
    assert store.matches(TOKEN)
    assert not store.matches("unit-test-token-0123456789-abcdefghij-different")
    assert not paths.token_path.exists()


def test_token_store_read_failure_is_clear(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    # A file where a directory is expected makes token creation impossible.
    blocking = tmp_path / "blocked"
    blocking.write_text("not a directory", encoding="utf-8")
    broken = TokenStore(paths, environ={}, token_path=blocking / "token")
    with pytest.raises(TokenStoreError):
        broken.ensure()


def test_constant_time_token_helpers() -> None:
    assert tokens_equal("abc", "abc") is True
    assert tokens_equal("abc", "abd") is False
    assert tokens_equal(None, "abc") is False
    assert tokens_equal("abc", None) is False
    assert tokens_equal("", "abc") is False
    assert tokens_equal("short", "a-much-longer-token-value") is False


def test_extract_bearer() -> None:
    assert extract_bearer({"authorization": f"Bearer {TOKEN}"}) == TOKEN
    assert extract_bearer({"authorization": "bearer  spaced-token  "}) == "spaced-token"
    assert extract_bearer({"authorization": "Basic abc"}) is None
    assert extract_bearer({}) is None
    assert extract_bearer({"authorization": "Bearer "}) is None


def test_origin_allowlist() -> None:
    allowed = ["http://127.0.0.1:8765", "http://localhost:5173"]
    assert origin_allowed(None, allowed) is True  # non-browser client
    assert origin_allowed("http://127.0.0.1:8765", allowed) is True
    assert origin_allowed("http://127.0.0.1:8765/", allowed) is True
    assert origin_allowed("http://evil.example", allowed) is False
    assert origin_allowed("http://evil.example", []) is False


# ---------------------------------------------------------------------------
# Lifespan token creation
# ---------------------------------------------------------------------------


def test_lifespan_creates_token_file_and_it_authenticates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AVDFLEET_TOKEN", raising=False)
    config = make_config()
    paths = make_paths(tmp_path)
    app = create_app(config=config, container=EmptyContainer(config, paths), paths=paths)
    with TestClient(app, base_url=BASE_URL) as client:
        assert paths.token_path.is_file()
        token = paths.token_path.read_text(encoding="utf-8").strip()
        assert client.get("/api/v1/system", headers=auth_headers(token)).status_code == 200
        assert client.get("/api/v1/system", headers=auth_headers(token + "-wrong")).status_code == 401


# ---------------------------------------------------------------------------
# One-time browser bootstrap login
# ---------------------------------------------------------------------------


def test_bootstrap_page_is_served_without_token_and_has_no_secret(client: TestClient) -> None:
    response = client.get("/auth/bootstrap")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "AVD Fleet" in response.text
    assert "localStorage" in response.text
    assert TOKEN not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_bootstrap_issue_requires_bearer_token(client: TestClient) -> None:
    assert client.post("/api/v1/auth/bootstrap-code").status_code == 401
    response = client.post("/api/v1/auth/bootstrap-code", headers=auth_headers())
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload["code"], str) and len(payload["code"]) >= 32
    assert payload["expires_in"] > 0
    assert TOKEN not in response.text


def test_bootstrap_code_is_single_use_and_returns_token(client: TestClient) -> None:
    issued = client.post("/api/v1/auth/bootstrap-code", headers=auth_headers()).json()
    code = issued["code"]
    redeemed = client.post("/api/v1/auth/bootstrap", json={"code": code})
    assert redeemed.status_code == 200
    assert redeemed.json()["token"] == TOKEN
    assert redeemed.headers["cache-control"] == "no-store"

    replay = client.post("/api/v1/auth/bootstrap", json={"code": code})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "unauthorized"
    assert TOKEN not in replay.text


def test_bootstrap_invalid_and_malformed_codes_are_rejected(client: TestClient) -> None:
    assert client.post("/api/v1/auth/bootstrap", json={"code": "nope"}).status_code == 401
    assert client.post("/api/v1/auth/bootstrap", json={}).status_code == 401
    assert client.post("/api/v1/auth/bootstrap", content=b"not-json").status_code == 401
    assert client.post("/api/v1/auth/bootstrap", json={"code": TOKEN}).status_code == 401


def test_bootstrap_code_store_expiry_and_single_use() -> None:
    now = [1000.0]

    def clock() -> float:
        return now[0]

    store = BootstrapCodeStore(ttl_s=30, clock=clock)
    code = store.issue()
    assert store.redeem(code) is True
    assert store.redeem(code) is False
    expired = store.issue()
    now[0] += 31
    assert store.redeem(expired) is False
    pending = store.issue()
    store.clear()
    assert store.redeem(pending) is False
