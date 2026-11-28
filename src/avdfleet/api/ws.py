"""WebSocket endpoints: the live event hub and per-instance screen streaming.

Authentication
--------------
Both sockets require the daemon bearer token.  Browsers cannot set an
``Authorization`` header on a WebSocket handshake, so the GUI first obtains a
short-lived, **single-use ticket** from ``POST /api/v1/auth/ws-ticket`` (bearer
authenticated) and passes it as ``?ticket=``.  A ticket is burned on the first
handshake that presents it and rejected (``4401``) on replay or expiry.  When a
``ticket`` parameter is present it is authoritative: an invalid ticket is never
silently upgraded to another credential.

CLI/native clients may instead send ``Authorization: Bearer <token>``.  The
legacy ``?token=`` query parameter is retained for older clients but **deprecated**
for the WebSocket: uvicorn logs the WebSocket request line (including the query
string) at INFO on ``uvicorn.error``, so a token in the URL is written to daemon
output.  ``serve.py`` attaches a redaction filter to uvicorn's loggers as
defence in depth for both forms.

``Origin`` is checked against ``config.allow_origins`` whenever it is present;
browsers always send it, so a malicious page cannot open a socket.  A request
without ``Origin`` is treated as a non-browser client and accepted.

Close codes (documented contract)
---------------------------------
* ``4401`` — missing/invalid bearer token.  Sent *before* ``accept()``, so
  browsers and WebSocket clients see a rejected handshake (HTTP 403); native
  clients may observe the 4401 code.
* ``4403`` — ``Origin`` not in ``config.allow_origins`` (same handshake
  rejection semantics).
* ``4400`` — malformed request parameters (unused today, reserved).
* ``4503`` — screen streaming is unavailable (no screen service, instance not
  running, or the stream failed for a dependency reason).  The socket is
  accepted first so an error frame can be delivered.
* ``1011`` — unexpected internal error while streaming.
* ``1000`` — normal close (client disconnect, stream ended, daemon shutdown).

Frame protocol
--------------
``/ws/events``
    Every server frame is a JSON ``Event`` envelope exactly as defined by
    ``contracts/events.schema.json``.  After ``accept()`` the server sends one
    ``system`` event announcing the connection (payload contains a ``message``
    and the number of replayed history events), then up to 50 recent history
    events, then live events as they are published.  A ``system`` event with
    ``payload.ping == true`` is sent every :data:`WS_PING_INTERVAL_S` seconds so
    proxies do not idle-timeout the connection.  Incoming text from the client is
    ignored (the socket is a broadcast channel).

``/ws/instances/{name}/screen``
    Default mode sends raw PNG/JPEG bytes in binary frames.  With
    ``?encoding=base64`` each JSON frame is
    ``{"type": "frame", "seq": N, "mime": "image/png", "data": "<base64>"}``.
    The first frame is ``{"type": "hello", "instance": name, "encoding": ...}``;
    fatal errors are sent as ``{"type": "error", "code": ..., "message": ...}``
    immediately before the documented close code.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from avdfleet.core.config import Config
from avdfleet.core.errors import AvdFleetError, ScreenError
from avdfleet.core.events import Event, EventType
from avdfleet.core.logging import redact

from .auth import TokenStore, origin_allowed, token_for_websocket, websocket_tickets
from .container import ContainerAdapter

logger = logging.getLogger("avdfleet.api.ws")

router = APIRouter()

#: App-level ping cadence; must stay below common proxy idle timeouts (60s).
WS_PING_INTERVAL_S = 25.0
#: How many recent events are replayed to a newly connected client.
WS_HISTORY_LIMIT = 50
#: Maximum events buffered for one slow subscriber before the bus drops old ones.
WS_SUBSCRIBER_QUEUE = 256

CLOSE_NORMAL = 1000
CLOSE_INTERNAL = 1011
CLOSE_BAD_REQUEST = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_SCREEN_UNAVAILABLE = 4503

_SCREEN_AVAILABLE_ERRORS = (AvdFleetError, OSError, RuntimeError)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _config(websocket: WebSocket) -> Config:
    return websocket.app.state.config  # type: ignore[no-any-return]


def _token_store(websocket: WebSocket) -> TokenStore | None:
    return getattr(websocket.app.state, "token_store", None)


def _container(websocket: WebSocket) -> ContainerAdapter | None:
    return getattr(websocket.app.state, "container", None)


def _event_bus(websocket: WebSocket) -> Any | None:
    bus = getattr(websocket.app.state, "event_bus", None)
    if bus is not None:
        return bus
    container = _container(websocket)
    if container is not None:
        return container.event_bus
    return None


def _query_value(websocket: WebSocket, name: str) -> str | None:
    """Return a stripped, non-empty query parameter value, if present."""
    try:
        value = websocket.query_params.get(name)
    except AttributeError:  # pragma: no cover - defensive
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _redeem_ticket(websocket: WebSocket, ticket: str) -> bool:
    """Burn *ticket*; return True only for a fresh, valid ticket."""
    try:
        return websocket_tickets(websocket.app.state).redeem(ticket)
    except Exception:  # pragma: no cover - a store failure must not crash the handshake
        logger.warning("WebSocket ticket store failed", exc_info=True)
        return False


async def _reject(websocket: WebSocket, code: int, reason: str) -> None:
    """Close before ``accept()``; this rejects the WebSocket handshake."""
    logger.info("rejecting WebSocket %s: %s (%s)", websocket.url.path, reason, code)
    with contextlib.suppress(RuntimeError):
        await websocket.close(code=code, reason=reason)


async def _safe_send_json(websocket: WebSocket, payload: Mapping[str, Any]) -> bool:
    try:
        await websocket.send_json(dict(payload))
        return True
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):  # pragma: no cover - disconnect
        return False


async def _safe_send_bytes(websocket: WebSocket, frame: bytes) -> bool:
    try:
        await websocket.send_bytes(frame)
        return True
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):  # pragma: no cover - disconnect
        return False


async def _safe_close(websocket: WebSocket, code: int, reason: str) -> None:
    with contextlib.suppress(WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        await websocket.close(code=code, reason=reason)


async def _authorize(websocket: WebSocket) -> bool:
    """Validate Origin and credentials, closing the handshake on failure.

    A ``?ticket=`` parameter is authoritative when present and is burned on
    first use.  Otherwise the ``Authorization: Bearer`` header is used, with the
    legacy ``?token=`` query parameter still accepted for older clients.
    """
    config = _config(websocket)
    origin = websocket.headers.get("origin")
    if not origin_allowed(origin, list(getattr(config, "allow_origins", []) or [])):
        await _reject(websocket, CLOSE_FORBIDDEN, "origin not allowed")
        return False
    if not bool(getattr(config, "auth_required", True)):
        return True
    ticket = _query_value(websocket, "ticket")
    if ticket is not None:
        if not _redeem_ticket(websocket, ticket):
            await _reject(websocket, CLOSE_UNAUTHORIZED, "invalid or expired ticket")
            return False
        return True
    store = _token_store(websocket)
    token = token_for_websocket(websocket)
    authenticated = False
    if store is not None:
        try:
            authenticated = store.matches(token)
        except AvdFleetError:
            logger.warning("WebSocket token store failed", exc_info=True)
            authenticated = False
    if not authenticated:
        await _reject(websocket, CLOSE_UNAUTHORIZED, "invalid or missing token")
        return False
    return True


def _event_payload(event: Any) -> dict[str, Any] | None:
    """Serialise an event object from any supported bus implementation."""
    dump = getattr(event, "model_dump_event", None)
    if callable(dump):
        return dict(dump())
    if hasattr(event, "model_dump"):
        return dict(event.model_dump(mode="json"))
    if isinstance(event, Mapping):
        return dict(event)
    return None


def _event_seq(event: Any) -> int | None:
    seq = getattr(event, "seq", None)
    return seq if isinstance(seq, int) else None


def _system_event(payload: dict[str, Any]) -> dict[str, Any]:
    return Event(type=EventType.SYSTEM, payload=payload).model_dump_event()


async def _next_event(subscription: Any, timeout: float) -> Any | None:
    """Wait for the next bus event, returning None on timeout."""
    getter = getattr(subscription, "get", None)
    if callable(getter):
        try:
            return await getter(timeout)
        except TypeError:  # pragma: no cover - subscription.get(timeout=...) variant
            pass
    queue = getattr(subscription, "queue", None)
    if queue is None:  # pragma: no cover - unsupported bus implementation
        await asyncio.sleep(timeout)
        return None
    try:
        return await asyncio.wait_for(queue.get(), timeout)
    except TimeoutError:
        return None


# ---------------------------------------------------------------------------
# Event hub socket
# ---------------------------------------------------------------------------


async def _event_sender(
    websocket: WebSocket,
    subscription: Any,
    last_seq: int,
) -> None:
    while True:
        event = await _next_event(subscription, WS_PING_INTERVAL_S)
        if event is None:
            if not await _safe_send_json(websocket, _system_event({"ping": True})):
                return
            continue
        if getattr(event, "type", None) == "__close__":
            await _safe_close(websocket, CLOSE_NORMAL, "daemon shutting down")
            return
        seq = _event_seq(event)
        if seq is not None and seq <= last_seq:
            continue  # already delivered as replayed history
        payload = _event_payload(event)
        if payload is None:  # pragma: no cover - unreachable for core EventBus
            continue
        if not await _safe_send_json(websocket, payload):
            return
        if seq is not None:
            last_seq = seq


async def _event_receiver(websocket: WebSocket) -> None:
    """Drain client frames and detect disconnects (the socket is broadcast-only)."""
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect(code=int(message.get("code") or CLOSE_NORMAL))


@router.websocket("/ws/events")
async def events_socket(websocket: WebSocket) -> None:
    """Stream daemon events (history then live) to an authenticated client."""
    if not await _authorize(websocket):
        return
    await websocket.accept()

    bus = _event_bus(websocket)
    history: list[Any] = []
    if bus is not None:
        history_fn = getattr(bus, "history", None)
        if callable(history_fn):
            try:
                history = list(history_fn() or [])[-WS_HISTORY_LIMIT:]
            except Exception:  # pragma: no cover - defensive
                logger.warning("event bus history failed", exc_info=True)
                history = []

    last_seq = 0
    try:
        await websocket.send_json(_system_event({"message": "connected", "history": len(history)}))
        for event in history:
            payload = _event_payload(event)
            if payload is None:  # pragma: no cover - defensive
                continue
            if not await _safe_send_json(websocket, payload):
                return
            seq = _event_seq(event)
            if seq is not None:
                last_seq = max(last_seq, seq)

        subscription = None
        if bus is not None:
            subscribe = getattr(bus, "subscribe", None)
            if callable(subscribe):
                subscription = subscribe(max_queue=WS_SUBSCRIBER_QUEUE)
        try:
            sender = asyncio.create_task(
                _event_sender(websocket, subscription, last_seq), name="avdfleet-ws-events-sender"
            )
            receiver = asyncio.create_task(
                _event_receiver(websocket), name="avdfleet-ws-events-receiver"
            )
            done, pending = await asyncio.wait(
                {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(BaseException):
                    await task
            for task in done:
                if task.cancelled():
                    continue
                error = task.exception()
                if error is not None and not isinstance(error, WebSocketDisconnect):
                    logger.warning("event WebSocket task failed", exc_info=error)
        finally:
            if subscription is not None:
                close = getattr(subscription, "close", None)
                if callable(close):
                    with contextlib.suppress(Exception):
                        close()
    except WebSocketDisconnect:
        pass
    except Exception:  # pragma: no cover - transport failure
        logger.exception("event WebSocket failed")


# ---------------------------------------------------------------------------
# Screen socket
# ---------------------------------------------------------------------------


async def _iter_frames(stream: Any) -> AsyncIterator[bytes]:
    """Iterate an async (or, defensively, sync) frame source."""
    if hasattr(stream, "__aiter__"):
        async for frame in stream:
            yield _frame_bytes(frame)
        return
    if hasattr(stream, "__iter__"):
        for frame in stream:
            yield _frame_bytes(frame)
        return
    raise ScreenError("the screen service returned a non-iterable stream")


def _frame_bytes(frame: Any) -> bytes:
    if isinstance(frame, (bytes, bytearray, memoryview)):
        return bytes(frame)
    if isinstance(frame, str):
        return frame.encode("utf-8")
    raise ScreenError("the screen stream produced a non-binary frame")


async def _screen_producer(
    websocket: WebSocket,
    screen: Any,
    name: str,
    encoding: str,
) -> None:
    seq = 0
    stream: Any | None = None
    try:
        stream_fn = getattr(screen, "stream", None)
        if not callable(stream_fn):
            await _safe_send_json(
                websocket,
                {"type": "error", "code": "screen_unavailable", "message": "screen streaming is not supported"},
            )
            await _safe_close(websocket, CLOSE_SCREEN_UNAVAILABLE, "screen unavailable")
            return
        stream = stream_fn(name)
        if inspect.isawaitable(stream):
            stream = await stream
        async for frame in _iter_frames(stream):
            seq += 1
            if encoding == "base64":
                sent = await _safe_send_json(
                    websocket,
                    {
                        "type": "frame",
                        "seq": seq,
                        "mime": "image/png",
                        "data": base64.b64encode(frame).decode("ascii"),
                    },
                )
            else:
                sent = await _safe_send_bytes(websocket, frame)
            if not sent:
                return
        await _safe_close(websocket, CLOSE_NORMAL, "stream ended")
    except WebSocketDisconnect:
        pass
    except AvdFleetError as exc:
        await _safe_send_json(
            websocket,
            {"type": "error", "code": getattr(exc, "code", "screen_error"), "message": redact(exc.message)},
        )
        await _safe_close(websocket, CLOSE_SCREEN_UNAVAILABLE, "screen stream failed")
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover - unexpected stream failure
        logger.exception("screen stream failed for instance %s", name)
        await _safe_send_json(websocket, {"type": "error", "code": "screen_error", "message": "screen stream failed"})
        await _safe_close(websocket, CLOSE_INTERNAL, "screen stream failed")
    finally:
        aclose = getattr(stream, "aclose", None)
        if callable(aclose):
            with contextlib.suppress(Exception):
                await aclose()


@router.websocket("/ws/instances/{name}/screen")
async def instance_screen(websocket: WebSocket, name: str) -> None:
    """Stream emulator screen frames for one instance."""
    if not await _authorize(websocket):
        return
    await websocket.accept()

    encoding = (websocket.query_params.get("encoding") or "binary").strip().lower()
    if encoding not in {"binary", "base64"}:
        encoding = "binary"

    container = _container(websocket)
    if container is None:
        await _safe_send_json(
            websocket,
            {"type": "error", "code": "screen_unavailable", "message": "the daemon is still starting"},
        )
        await _safe_close(websocket, CLOSE_SCREEN_UNAVAILABLE, "screen unavailable")
        return
    try:
        screen = container.raw_service("screen")
    except AvdFleetError:
        logger.info("screen service is unavailable", exc_info=True)
        screen = None
    if screen is None:
        await _safe_send_json(
            websocket,
            {"type": "error", "code": "screen_unavailable", "message": "screen sharing is not available"},
        )
        await _safe_close(websocket, CLOSE_SCREEN_UNAVAILABLE, "screen unavailable")
        return

    available = getattr(screen, "available", None)
    if callable(available):
        try:
            is_available = await run_in_threadpool(available, name)
        except _SCREEN_AVAILABLE_ERRORS as exc:
            logger.info("screen availability check failed for %s: %s", name, type(exc).__name__)
            is_available = False
        if not is_available:
            await _safe_send_json(
                websocket,
                {"type": "error", "code": "screen_unavailable", "message": "the instance screen is not available"},
            )
            await _safe_close(websocket, CLOSE_SCREEN_UNAVAILABLE, "screen unavailable")
            return

    await websocket.send_json({"type": "hello", "instance": name, "encoding": encoding})

    producer = asyncio.create_task(
        _screen_producer(websocket, screen, name, encoding), name="avdfleet-ws-screen-producer"
    )
    receiver = asyncio.create_task(_event_receiver(websocket), name="avdfleet-ws-screen-receiver")
    try:
        done, pending = await asyncio.wait({producer, receiver}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(BaseException):
                await task
        for task in done:
            if task.cancelled():
                continue
            error = task.exception()
            if error is not None and not isinstance(error, WebSocketDisconnect):
                logger.warning("screen WebSocket task failed", exc_info=error)
    except WebSocketDisconnect:
        pass
    finally:
        for task in (producer, receiver):
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task


__all__ = [
    "CLOSE_BAD_REQUEST",
    "CLOSE_FORBIDDEN",
    "CLOSE_INTERNAL",
    "CLOSE_NORMAL",
    "CLOSE_SCREEN_UNAVAILABLE",
    "CLOSE_UNAUTHORIZED",
    "WS_HISTORY_LIMIT",
    "WS_PING_INTERVAL_S",
    "router",
]
