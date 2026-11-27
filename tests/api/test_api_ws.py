"""WebSocket tests: token/origin auth, event history + live flow, screen frames."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from avdfleet.api.app import create_app
from avdfleet.core.config import Config
from avdfleet.core.events import EventBus, EventType
from avdfleet.core.paths import Paths

ORIGIN = "http://127.0.0.1:8765"
BASE_URL = "http://127.0.0.1:8765"
WS_BASE = "ws://127.0.0.1:8765"
BAD_ORIGIN = "http://evil.example"
TOKEN = "ws-test-token-0123456789-abcdefghij"


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------


class ScreenStub:
    def __init__(self, frames: list[bytes] | None = None, *, available: bool = True) -> None:
        self.frames = frames if frames is not None else [b"frame-a", b"frame-b"]
        self._available = available
        self.calls: list[tuple[str, ...]] = []

    def available(self, avd_name: str) -> bool:
        self.calls.append(("available", avd_name))
        return self._available

    async def stream(self, avd_name: str) -> Any:
        self.calls.append(("stream", avd_name))
        for frame in self.frames:
            yield frame

    def capture(self, avd_name: str) -> bytes:
        return self.frames[0]

    def tap(self, avd_name: str, x: int, y: int) -> Any:
        return None

    def swipe(self, avd_name: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> Any:
        return None

    def text(self, avd_name: str, value: str) -> Any:
        return None

    def key(self, avd_name: str, keycode: str) -> Any:
        return None


class WsContainer:
    """Duck-typed container exposing only the event bus and optional screen."""

    def __init__(self, config: Config, paths: Paths, *, screen: Any | None = None) -> None:
        self.config = config
        self.paths = paths
        self.event_bus = EventBus(history=100)
        self._screen = screen

    def screen(self) -> Any:
        return self._screen


def make_config(*, auth_required: bool = True) -> Config:
    return Config(
        auth_required=auth_required,
        allow_origins=[ORIGIN],
        trusted_hosts=["127.0.0.1", "localhost"],
        serve_ui=False,
    )


def make_paths(tmp_path: Path) -> Paths:
    return Paths(root=tmp_path, portable=True).ensure()


def events_url(token: str | None = TOKEN) -> str:
    if token is None:
        return f"{WS_BASE}/ws/events"
    return f"{WS_BASE}/ws/events?" + urlencode({"token": token})


def screen_url(name: str = "avd-01", token: str | None = TOKEN, **params: str) -> str:
    query: dict[str, str] = dict(params)
    if token is not None:
        query["token"] = token
    base = f"{WS_BASE}/ws/instances/{name}/screen"
    return base + ("?" + urlencode(query) if query else "")


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


@pytest.fixture()
def app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.setenv("AVDFLEET_TOKEN", TOKEN)
    config = make_config()
    paths = make_paths(tmp_path)
    container = WsContainer(config, paths, screen=ScreenStub())
    return create_app(config=config, container=container, paths=paths)


@pytest.fixture()
def client(app: Any) -> Any:
    with TestClient(app, base_url=BASE_URL) as test_client:  # noqa: SIM117
        yield test_client


# ---------------------------------------------------------------------------
# Authentication / origin
# ---------------------------------------------------------------------------


def test_events_socket_requires_token(client: TestClient) -> None:
    expect_rejection(client, events_url(None), headers={"origin": ORIGIN}, expected_code=4401)


def test_events_socket_rejects_invalid_token(client: TestClient) -> None:
    expect_rejection(
        client, events_url("wrong-token"), headers={"origin": ORIGIN}, expected_code=4401
    )


def test_events_socket_rejects_disallowed_origin(client: TestClient) -> None:
    expect_rejection(
        client, events_url(), headers={"origin": BAD_ORIGIN}, expected_code=4403
    )


def test_events_socket_accepts_authorization_header(client: TestClient) -> None:
    headers = {"origin": ORIGIN, "Authorization": f"Bearer {TOKEN}"}
    with client.websocket_connect(events_url(None), headers=headers) as websocket:  # noqa: SIM117
        hello = websocket.receive_json()
        assert hello["type"] == "system"
        assert hello["payload"]["message"] == "connected"


def test_events_socket_rejects_without_origin_when_allowlist_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AVDFLEET_TOKEN", TOKEN)
    config = Config(auth_required=True, allow_origins=[], trusted_hosts=["127.0.0.1"], serve_ui=False)
    paths = make_paths(tmp_path)
    app = create_app(config=config, container=WsContainer(config, paths), paths=paths)
    with TestClient(app, base_url=BASE_URL) as client:  # noqa: SIM117
        # No Origin header is a non-browser client; but auth is still enforced.
        expect_rejection(client, events_url(None), expected_code=4401)
        # A browser Origin can never pass an empty allowlist.
        expect_rejection(client, events_url(), headers={"origin": ORIGIN}, expected_code=4403)


# ---------------------------------------------------------------------------
# Event history and live streaming
# ---------------------------------------------------------------------------


def test_events_history_then_live_events(client: TestClient, app: Any) -> None:
    bus = app.state.event_bus
    bus.emit(EventType.SYSTEM, {"message": "seed-1"})
    bus.emit(EventType.SYSTEM, {"message": "seed-2"})

    with client.websocket_connect(events_url(), headers={"origin": ORIGIN}) as websocket:  # noqa: SIM117
        hello = websocket.receive_json()
        assert hello["type"] == "system"
        assert hello["payload"]["history"] == 2
        assert hello["id"] and hello["ts"] and hello["seq"] is None

        first = websocket.receive_json()
        second = websocket.receive_json()
        assert [first["payload"]["message"], second["payload"]["message"]] == ["seed-1", "seed-2"]
        assert first["type"] == "system"
        assert first["seq"] == 1
        assert second["seq"] == 2
        assert first["source"] == "avdfleet"

        bus.emit(EventType.INSTANCE_STATE, {"instance": "avd-01", "state": "running"})
        live = websocket.receive_json()
        assert live["type"] == "instance.state"
        assert live["payload"] == {"instance": "avd-01", "state": "running"}
        assert live["seq"] == 3


def test_events_socket_ignores_unknown_types_gracefully(client: TestClient, app: Any) -> None:
    bus = app.state.event_bus
    with client.websocket_connect(events_url(), headers={"origin": ORIGIN}) as websocket:  # noqa: SIM117
        websocket.receive_json()  # hello
        bus.emit(EventType.LOG, {"message": "hello", "level": "info"})
        event = websocket.receive_json()
        assert event["type"] == "log"
        assert event["payload"]["message"] == "hello"


def test_events_socket_works_when_auth_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AVDFLEET_TOKEN", raising=False)
    config = make_config(auth_required=False)
    paths = make_paths(tmp_path)
    app = create_app(config=config, container=WsContainer(config, paths), paths=paths)
    with TestClient(app, base_url=BASE_URL) as client:  # noqa: SIM117
        with client.websocket_connect(events_url(None), headers={"origin": ORIGIN}) as websocket:  # noqa: SIM117
            hello = websocket.receive_json()
            assert hello["type"] == "system"


# ---------------------------------------------------------------------------
# Screen streaming
# ---------------------------------------------------------------------------


def test_screen_requires_token(client: TestClient) -> None:
    expect_rejection(
        client, screen_url(token=None), headers={"origin": ORIGIN}, expected_code=4401
    )


def test_screen_rejects_disallowed_origin(client: TestClient) -> None:
    expect_rejection(client, screen_url(), headers={"origin": BAD_ORIGIN}, expected_code=4403)


def test_screen_streams_binary_frames(client: TestClient) -> None:
    with client.websocket_connect(screen_url(), headers={"origin": ORIGIN}) as websocket:  # noqa: SIM117
        hello = websocket.receive_json()
        assert hello["type"] == "hello"
        assert hello["instance"] == "avd-01"
        assert hello["encoding"] == "binary"
        assert websocket.receive_bytes() == b"frame-a"
        assert websocket.receive_bytes() == b"frame-b"
        with pytest.raises(WebSocketDisconnect) as info:
            websocket.receive_bytes()
        assert info.value.code == 1000


def test_screen_streams_base64_frames(client: TestClient) -> None:
    with client.websocket_connect(  # noqa: SIM117
        screen_url(encoding="base64"), headers={"origin": ORIGIN}
    ) as websocket:
        hello = websocket.receive_json()
        assert hello["encoding"] == "base64"
        first = websocket.receive_json()
        assert first["type"] == "frame"
        assert first["seq"] == 1
        assert base64.b64decode(first["data"]) == b"frame-a"
        second = websocket.receive_json()
        assert base64.b64decode(second["data"]) == b"frame-b"


def test_screen_invalid_encoding_falls_back_to_binary(client: TestClient) -> None:
    with client.websocket_connect(  # noqa: SIM117
        screen_url(encoding="jpeg-please"), headers={"origin": ORIGIN}
    ) as websocket:
        hello = websocket.receive_json()
        assert hello["encoding"] == "binary"
        assert websocket.receive_bytes() == b"frame-a"


def test_screen_unavailable_when_service_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AVDFLEET_TOKEN", TOKEN)
    config = make_config()
    paths = make_paths(tmp_path)
    app = create_app(config=config, container=WsContainer(config, paths, screen=None), paths=paths)
    with TestClient(app, base_url=BASE_URL) as client:  # noqa: SIM117
        with client.websocket_connect(screen_url(), headers={"origin": ORIGIN}) as websocket:  # noqa: SIM117
            error = websocket.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "screen_unavailable"
            with pytest.raises(WebSocketDisconnect) as info:
                websocket.receive_bytes()
            assert info.value.code == 4503


def test_screen_unavailable_when_instance_not_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AVDFLEET_TOKEN", TOKEN)
    config = make_config()
    paths = make_paths(tmp_path)
    container = WsContainer(config, paths, screen=ScreenStub(available=False))
    app = create_app(config=config, container=container, paths=paths)
    with TestClient(app, base_url=BASE_URL) as client:  # noqa: SIM117
        with client.websocket_connect(screen_url(), headers={"origin": ORIGIN}) as websocket:  # noqa: SIM117
            error = websocket.receive_json()
            assert error["code"] == "screen_unavailable"
            with pytest.raises(WebSocketDisconnect) as info:
                websocket.receive_bytes()
            assert info.value.code == 4503


def test_screen_disconnect_is_clean(client: TestClient) -> None:
    with client.websocket_connect(screen_url(), headers={"origin": ORIGIN}) as websocket:  # noqa: SIM117
        websocket.receive_json()  # hello
        websocket.receive_bytes()  # frame-a
    # Exiting the context closes the socket; a second stream must still work.
    with client.websocket_connect(screen_url(), headers={"origin": ORIGIN}) as websocket:  # noqa: SIM117
        hello = websocket.receive_json()
        assert hello["type"] == "hello"
