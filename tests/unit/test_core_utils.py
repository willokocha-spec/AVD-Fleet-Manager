"""Core utility tests: ports, logging redaction, event bus."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from avdfleet.core.errors import CapacityError
from avdfleet.core.events import Event, EventBus, EventType
from avdfleet.core.logging import redact, redact_text, setup_logging
from avdfleet.core.ports import PortAllocator, find_free_even_port, find_free_port, is_port_free


def test_port_probe_and_allocation() -> None:
    assert is_port_free(0) is False
    allocator = PortAllocator(start=5700, end=5800, appium_start=4820, bridge_start=7900)
    ports = allocator.lease("avd-01")
    assert ports.console % 2 == 0
    assert ports.adb == ports.console + 1
    assert len({ports.console, ports.adb, ports.grpc, ports.appium_server}) == 4
    assert allocator.get("avd-01") == ports
    same = allocator.lease("avd-01")
    assert same == ports
    second = allocator.lease("avd-02")
    assert second.console != ports.console
    assert second.adb != ports.adb
    allocator.release("avd-01")
    assert allocator.get("avd-01") is None
    assert allocator.capacity()["in_use"] == 1


def test_port_range_exhaustion() -> None:
    allocator = PortAllocator(start=5900, end=5901, appium_start=4900, bridge_start=7900)
    allocator.lease("one")
    with pytest.raises(CapacityError):
        allocator.lease("two")


def test_find_free_helpers() -> None:
    port = find_free_port(5900, 5999)
    assert port is not None
    even = find_free_even_port(5900, 5999)
    assert even is not None and even % 2 == 0


REDACTION_CASES = [
    ("http://alice:s3cret@127.0.0.1:8081", "s3cret"),
    ("socks5://bob:pa55@10.1.2.3:1080", "pa55"),
    ('{"password": "hunter2"}', "hunter2"),
    ('{"token":"abcd1234"}', "abcd1234"),
    ("password=hunter2", "hunter2"),
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig", "eyJhbGciOiJIUzI1NiJ9"),
    ("--password hunter2", "hunter2"),
    ("--token abc123", "abc123"),
    ("api_key=sk-supersecretvalue", "sk-supersecretvalue"),
]


@pytest.mark.parametrize(("text", "secret"), REDACTION_CASES)
def test_redaction(text: str, secret: str) -> None:
    assert secret not in redact_text(text)
    assert "***" in redact_text(text)


def test_redaction_nested_objects() -> None:
    obj = {
        "proxy": {"username": "alice", "password": "hunter2"},
        "nested": [{"token": "abc"}, "http://u:p@h:1"],
        "safe": "hello",
    }
    cleaned = redact(obj)
    assert cleaned["proxy"]["password"] == "***"
    assert cleaned["nested"][0]["token"] == "***"
    assert "http://u:p@" not in cleaned["nested"][1]
    assert cleaned["safe"] == "hello"


def test_redaction_filter_on_logs() -> None:
    from avdfleet.core.logging import RedactingFilter

    record = logging.LogRecord(
        "x", logging.INFO, "", 0, "proxy http://alice:hunter2@localhost:8080 token=abc123", (), None
    )
    assert RedactingFilter().filter(record) is True
    message = record.getMessage()
    assert "hunter2" not in message
    assert "abc123" not in message
    assert "***" in message


def test_event_bus_sync_publish_and_history() -> None:
    bus = EventBus(history=10)
    sub = bus.subscribe()
    event = bus.emit(EventType.INSTANCE_STATE, {"instance": "avd-01", "state": "running"})
    assert event.seq == 1
    assert bus.history()[-1].type == "instance.state"
    queued = sub.queue.get_nowait()
    assert queued.payload["instance"] == "avd-01"
    assert queued.seq == 1
    sub.close()


@pytest.mark.asyncio
async def test_event_bus_async_delivery() -> None:
    bus = EventBus(history=10)
    bus.bind_loop()
    sub = bus.subscribe()

    async def publish_later() -> None:
        await asyncio.sleep(0.01)
        bus.emit("system", {"message": "hello"})

    task = asyncio.create_task(publish_later())
    event = await asyncio.wait_for(sub.get(timeout=1.0), 2.0)
    assert event is not None
    assert event.payload["message"] == "hello"
    await task
    sub.close()
    bus.close()


def test_event_model_defaults() -> None:
    event = Event(type="log", payload={"message": "hi"})
    assert event.id
    assert event.source == "avdfleet"
    assert event.ts.tzinfo is not None


def test_setup_logging_writes_file(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "test.log"
    setup_logging(level="DEBUG", log_file=log_file, console=False, force=True)
    logging.getLogger("avdfleet.test").info("proxy http://u:secret@h:1")
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = log_file.read_text(encoding="utf-8")
    assert "secret" not in text
    assert "***" in text
