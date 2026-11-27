"""In-process event bus used by the API WebSocket hub and service telemetry.

The bus is deliberately small: sync services can publish from worker threads and
async consumers receive events through per-subscriber queues.  Backpressure is
handled by dropping the oldest event for slow subscribers so a stalled browser
can never block the daemon.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .models import utcnow


class EventType:
    INSTANCE_CREATED = "instance.created"
    INSTANCE_DELETED = "instance.deleted"
    INSTANCE_STATE = "instance.state"
    INSTANCE_BOOT = "instance.boot"
    INSTANCE_LOG = "instance.log"
    INSTANCE_SCREEN = "instance.screen"
    PROXY_CREATED = "proxy.created"
    PROXY_UPDATED = "proxy.updated"
    PROXY_CHECKED = "proxy.checked"
    SCRIPT_RUN_STARTED = "script.run.started"
    SCRIPT_RUN_OUTPUT = "script.run.output"
    SCRIPT_RUN_FINISHED = "script.run.finished"
    INSTALL_STEP = "install.step"
    INSTALL_FINISHED = "install.finished"
    OOBE_RESULT = "oobe.result"
    DOCTOR_FINISHED = "doctor.finished"
    FLEET_APPLIED = "fleet.applied"
    SYSTEM = "system"
    ERROR = "error"
    LOG = "log"


class Event(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    type: str
    ts: datetime = Field(default_factory=utcnow)
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = "avdfleet"
    seq: int | None = None

    def model_dump_event(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class Subscription:
    def __init__(self, bus: EventBus, queue: asyncio.Queue[Event], queued_only: bool = False) -> None:
        self._bus = bus
        self._queue = queue
        self._closed = False

    @property
    def queue(self) -> asyncio.Queue[Event]:
        return self._queue

    async def get(self, timeout: float | None = None) -> Event | None:
        if timeout is None:
            return await self._queue.get()
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError:
            return None

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._bus.unsubscribe(self)

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()

    def __aiter__(self) -> AsyncIterator[Event]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Event]:
        while not self._closed:
            event = await self._queue.get()
            if event.type == "__close__":
                break
            yield event
        self.close()


class EventBus:
    def __init__(self, *, max_queue: int = 500, history: int = 200) -> None:
        self._subscribers: list[Subscription] = []
        self._lock = threading.RLock()
        self._max_queue = max_queue
        self._seq = 0
        self._history: list[Event] = []
        self._history_limit = history
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        try:
            self._loop = loop or asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def subscribe(self, *, max_queue: int | None = None) -> Subscription:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=max_queue or self._max_queue)
        sub = Subscription(self, queue)
        with self._lock:
            # Replay recent history so a newly connected GUI can render state.
            for event in self._history[-50:]:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    break
            self._subscribers.append(sub)
        return sub

    def unsubscribe(self, subscription: Subscription) -> None:
        with self._lock:
            if subscription in self._subscribers:
                self._subscribers.remove(subscription)

    # -- publishing ------------------------------------------------------
    def publish(self, event: Event) -> int:
        with self._lock:
            self._seq += 1
            event.seq = self._seq
            self._history.append(event)
            if len(self._history) > self._history_limit:
                del self._history[: len(self._history) - self._history_limit]
            subscribers = list(self._subscribers)
        delivered = 0
        for sub in subscribers:
            delivered += self._deliver(sub, event)
        return delivered

    def emit(self, event_type: str, payload: dict[str, Any] | None = None, *, source: str = "avdfleet") -> Event:
        event = Event(type=event_type, payload=payload or {}, source=source)
        self.publish(event)
        return event

    def _deliver(self, sub: Subscription, event: Event) -> int:
        queue = sub.queue
        loop = self._loop

        def _put() -> None:
            while True:
                try:
                    queue.put_nowait(event)
                    return
                except asyncio.QueueFull:
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return

        if loop is not None and loop.is_running():
            if loop is self._current_loop():
                _put()
            else:
                loop.call_soon_threadsafe(_put)
            return 1
        # No running loop (sync tests): leave the event in the queue if possible.
        try:
            queue.put_nowait(event)
            return 1
        except asyncio.QueueFull:
            return 0

    @staticmethod
    def _current_loop() -> asyncio.AbstractEventLoop | None:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def history(self) -> list[Event]:
        with self._lock:
            return list(self._history)

    def close(self) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
            self._subscribers.clear()
        for sub in subscribers:
            sub.close()
            try:
                sub.queue.put_nowait(Event(type="__close__"))
            except asyncio.QueueFull:
                pass


class EventSink:
    """Small helper for services that optionally publish to a bus."""

    def __init__(self, bus: EventBus | None = None) -> None:
        self.bus = bus

    def emit(self, event_type: str, payload: dict[str, Any] | None = None, *, source: str = "avdfleet") -> None:
        if self.bus is not None:
            self.bus.emit(event_type, payload, source=source)

    def on_instance_state(self, name: str, state: str, **extra: Any) -> None:
        self.emit(EventType.INSTANCE_STATE, {"instance": name, "state": state, **extra})

    def on_log(self, message: str, *, instance: str | None = None, level: str = "info") -> None:
        self.emit(EventType.LOG, {"message": message, "instance": instance, "level": level})

    def subscribe_callback(self, callback: Callable[[Event], None]) -> Callable[[], None]:
        """Register a plain callback; returns an unsubscribe function."""
        async def _consume(sub: Subscription) -> None:
            async for event in sub:
                callback(event)

        loop = self._loop_hack()
        if loop is None:
            raise RuntimeError("event callback subscriptions require a running event loop")
        sub = self.bus.subscribe() if self.bus else None
        if sub is None:
            return lambda: None
        task = loop.create_task(_consume(sub))

        def _unsubscribe() -> None:
            sub.close()
            task.cancel()

        return _unsubscribe

    @staticmethod
    def _loop_hack() -> asyncio.AbstractEventLoop | None:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None
