"""Single-use WebSocket tickets (audit item 2).

The GUI must never put the long-lived daemon token in a WebSocket URL: uvicorn
logs the WebSocket request line at INFO on ``uvicorn.error`` (``access_log=False``
does not cover it), so a ``?token=`` handshake writes the secret to the daemon's
output and log file.  Browsers now fetch a short-lived, single-use ticket from
``POST /api/v1/auth/ws-ticket`` and pass that instead.

These tests cover:

* issuance is bearer-authenticated only,
* a ticket is accepted once and burned on first use,
* expired/unknown tickets are rejected with ``4401``,
* the legacy ``Authorization`` header and ``?token=`` paths still work for CLI
  clients,
* a real uvicorn server's ``uvicorn.error`` output never contains the token or
  the ticket for a WebSocket handshake.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect
from websockets.sync.client import connect as ws_connect

from avdfleet.api.app import create_app
from avdfleet.api.auth import WS_TICKET_MAX_TTL_S, WebSocketTicketStore
from avdfleet.cli.commands.serve import run_uvicorn, uvicorn_log_config
from avdfleet.core.config import Config
from avdfleet.core.events import EventBus
from avdfleet.core.paths import Paths

ORIGIN = "http://127.0.0.1:8765"
BASE_URL = "http://127.0.0.1:8765"
WS_BASE = "ws://127.0.0.1:8765"
TOKEN = "ws-ticket-test-token-0123456789-abcdefghij"


# ---------------------------------------------------------------------------
# Stubs / fixtures
# ---------------------------------------------------------------------------


class TicketContainer:
    """Duck-typed container: an event bus is all these tests need."""

    def __init__(self, config: Config, paths: Paths) -> None:
        self.config = config
        self.paths = paths
        self.event_bus = EventBus(history=10)


def make_config() -> Config:
    return Config(
        auth_required=True,
        allow_origins=[ORIGIN],
        trusted_hosts=["127.0.0.1", "localhost"],
        serve_ui=False,
    )


def auth_headers(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def events_url(**params: str) -> str:
    return f"{WS_BASE}/ws/events" + (f"?{urlencode(params)}" if params else "")


@pytest.fixture()
def clock() -> list[float]:
    """Mutable monotonic clock so ticket expiry can be tested without sleeping."""
    return [1000.0]


@pytest.fixture()
def app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: list[float]) -> Any:
    monkeypatch.setenv("AVDFLEET_TOKEN", TOKEN)
    config = make_config()
    paths = Paths(root=tmp_path, portable=True).ensure()
    application = create_app(
        config=config, container=TicketContainer(config, paths), paths=paths
    )
    # Inject a store with a controllable clock: the endpoint and the WebSocket
    # handler both resolve the store from app.state, so this is the same instance.
    application.state.ws_ticket_store = WebSocketTicketStore(
        ttl_s=30.0, clock=lambda: clock[0]
    )
    return application


@pytest.fixture()
def client(app: Any) -> Iterator[TestClient]:
    with TestClient(app, base_url=BASE_URL) as test_client:
        yield test_client


def issue_ticket(client: TestClient) -> str:
    response = client.post("/api/v1/auth/ws-ticket", headers=auth_headers())
    assert response.status_code == 200, response.text
    ticket = response.json()["ticket"]
    assert isinstance(ticket, str) and ticket
    return ticket


def expect_rejection(
    client: TestClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    expected_code: int,
) -> None:
    with (
        pytest.raises((WebSocketDisconnect, WebSocketDenialResponse)) as info,
        client.websocket_connect(url, headers=headers or {}),
    ):
        pytest.fail(f"WebSocket handshake unexpectedly accepted: {url}")
    exception = info.value
    code = getattr(exception, "code", None)
    status = getattr(exception, "status_code", None)
    assert code == expected_code or status == 403, (
        f"expected close code {expected_code} or HTTP 403, got code={code} status={status}"
    )


# ---------------------------------------------------------------------------
# Issuance
# ---------------------------------------------------------------------------


def test_ticket_endpoint_requires_bearer_auth(client: TestClient) -> None:
    anonymous = client.post("/api/v1/auth/ws-ticket")
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "unauthorized"

    wrong = client.post("/api/v1/auth/ws-ticket", headers=auth_headers("not-the-token"))
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "unauthorized"

    issued = client.post("/api/v1/auth/ws-ticket", headers=auth_headers())
    assert issued.status_code == 200
    body = issued.json()
    assert isinstance(body["ticket"], str)
    assert len(body["ticket"]) >= 32
    assert isinstance(body["expires_in"], int)
    assert 0 < body["expires_in"] <= 60
    assert TOKEN not in issued.text
    assert issued.headers["cache-control"] == "no-store"


def test_ticket_is_not_derivable_from_the_token(client: TestClient) -> None:
    first = issue_ticket(client)
    second = issue_ticket(client)
    assert first != second  # every call mints fresh entropy
    assert first != TOKEN
    assert TOKEN not in first
    assert TOKEN not in second


def test_ticket_store_ttl_is_capped_below_one_minute() -> None:
    store = WebSocketTicketStore(ttl_s=600)
    assert store.ttl_s <= WS_TICKET_MAX_TTL_S
    assert WS_TICKET_MAX_TTL_S <= 60


# ---------------------------------------------------------------------------
# Redemption / burn / expiry
# ---------------------------------------------------------------------------


def test_ticket_is_accepted_once_by_a_websocket_connect(client: TestClient) -> None:
    ticket = issue_ticket(client)

    with client.websocket_connect(
        events_url(ticket=ticket), headers={"origin": ORIGIN}
    ) as websocket:
        hello = websocket.receive_json()
        assert hello["type"] == "system"
        assert hello["payload"]["message"] == "connected"

    # The same ticket is burned: a replay is rejected before accept().
    expect_rejection(
        client, events_url(ticket=ticket), headers={"origin": ORIGIN}, expected_code=4401
    )


def test_expired_ticket_is_rejected(
    client: TestClient, clock: list[float]
) -> None:
    ticket = issue_ticket(client)
    clock[0] += 31  # the store was created with a 30 s TTL
    expect_rejection(
        client, events_url(ticket=ticket), headers={"origin": ORIGIN}, expected_code=4401
    )


def test_unknown_ticket_is_rejected(client: TestClient) -> None:
    expect_rejection(
        client,
        events_url(ticket="not-a-real-ticket"),
        headers={"origin": ORIGIN},
        expected_code=4401,
    )


def test_invalid_ticket_is_not_upgraded_to_the_legacy_token_path(
    client: TestClient,
) -> None:
    # When a ticket is supplied it is authoritative: a bogus ticket must not
    # silently fall back to a valid ?token= value.
    expect_rejection(
        client,
        events_url(ticket="bogus", token=TOKEN),
        headers={"origin": ORIGIN},
        expected_code=4401,
    )


def test_authorization_header_and_legacy_query_token_still_connect(
    client: TestClient,
) -> None:
    # CLI/native clients authenticate with the header; the legacy ?token= query
    # parameter is retained for older clients and documented as deprecated.
    with client.websocket_connect(
        events_url(), headers={"origin": ORIGIN, **auth_headers()}
    ) as websocket:
        assert websocket.receive_json()["payload"]["message"] == "connected"

    with client.websocket_connect(
        events_url(token=TOKEN), headers={"origin": ORIGIN}
    ) as websocket:
        assert websocket.receive_json()["payload"]["message"] == "connected"


# ---------------------------------------------------------------------------
# Log redaction on a real uvicorn server
# ---------------------------------------------------------------------------


@contextmanager
def preserved_uvicorn_logging() -> Iterator[None]:
    """Restore uvicorn's loggers after a test applies the daemon logging config."""
    names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    state = {
        name: (
            list(logging.getLogger(name).handlers),
            list(logging.getLogger(name).filters),
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
        )
        for name in names
    }
    try:
        yield
    finally:
        for name, (handlers, filters, level, propagate) in state.items():
            target = logging.getLogger(name)
            target.handlers = handlers
            target.filters = filters
            target.setLevel(level)
            target.propagate = propagate


def _start_uvicorn(app: Any) -> tuple[Any, threading.Thread, int]:
    """Run the app under a real uvicorn server on a free loopback port."""
    import uvicorn

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(2048)
        port = int(sock.getsockname()[1])
        config = uvicorn.Config(
            app=app,
            host="127.0.0.1",
            port=port,
            log_level="info",
            access_log=False,
            log_config=uvicorn_log_config(),
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(
            target=server.run,
            kwargs={"sockets": [sock]},
            name="avdfleet-test-uvicorn",
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline and thread.is_alive():
            time.sleep(0.01)
        assert server.started, "uvicorn did not start within 20 s"
        return server, thread, port
    except BaseException:
        sock.close()
        raise


def _stop_uvicorn(server: Any, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=20)
    assert not thread.is_alive(), "uvicorn did not stop within 20 s"


def test_ws_handshake_credentials_are_absent_from_uvicorn_error_output(
    app: Any, capfd: pytest.CaptureFixture[str]
) -> None:
    """The audit defect: the token must not reach ``uvicorn.error`` output."""
    with preserved_uvicorn_logging():
        server, thread, port = _start_uvicorn(app)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0) as http:
                issued = http.post(
                    "/api/v1/auth/ws-ticket",
                    headers={"Authorization": f"Bearer {TOKEN}"},
                )
                assert issued.status_code == 200, issued.text
                ticket = issued.json()["ticket"]

            # 1) The legacy query-token handshake that produced the leak.
            with ws_connect(
                f"ws://127.0.0.1:{port}/ws/events?token={TOKEN}",
                open_timeout=10,
                proxy=None,
            ) as socket_:
                assert json.loads(socket_.recv(timeout=10))["type"] == "system"

            # 2) The browser ticket handshake that replaces it.
            with ws_connect(
                f"ws://127.0.0.1:{port}/ws/events?ticket={ticket}",
                open_timeout=10,
                proxy=None,
            ) as socket_:
                assert json.loads(socket_.recv(timeout=10))["type"] == "system"
        finally:
            _stop_uvicorn(server, thread)

        captured = capfd.readouterr()

    output = captured.err + captured.out
    # Sanity: uvicorn really logged both handshake request lines...
    assert "WebSocket /ws/events?token=***" in output
    assert "WebSocket /ws/events?ticket=***" in output
    # ...and the credentials themselves never reached the sink.
    assert TOKEN not in output
    assert ticket not in output


def test_uvicorn_log_config_redacts_every_handler() -> None:
    config = uvicorn_log_config()
    redaction = config["filters"]["avdfleet_redact"]
    assert redaction["()"].endswith("UvicornRedactionFilter")
    assert config["handlers"], "uvicorn logging config must define handlers"
    for handler in config["handlers"].values():
        assert "avdfleet_redact" in handler["filters"]


def test_run_uvicorn_uses_the_redacting_log_config(
    monkeypatch: pytest.MonkeyPatch, app: Any
) -> None:
    import uvicorn

    recorded: dict[str, Any] = {}

    def fake_run(application: Any, **kwargs: Any) -> None:
        recorded["app"] = application
        recorded["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)
    run_uvicorn(app, host="127.0.0.1", port=8765)

    kwargs = recorded["kwargs"]
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8765
    assert kwargs["access_log"] is False
    assert "avdfleet_redact" in kwargs["log_config"]["filters"]
