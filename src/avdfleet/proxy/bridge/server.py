"""Shared plumbing for the loopback bridge listeners.

The bridge is a pair of listener kinds (HTTP proxy and SOCKS5) that both forward
guest TCP connections through exactly one configured upstream.  This module owns
the pieces that must behave identically in both listeners:

* :class:`BridgeOptions` - validated tuning knobs (connection/header/idle
  timeouts, header limits, loopback-only bind, SOCKS enablement, DNS policy).
* HTTP head framing (:func:`read_http_head`, :func:`parse_http_head`) with hard
  size limits so a malicious guest cannot exhaust host memory.
* :func:`relay_bidirectional` - backpressured, full-duplex tunnel pump with an
  optional idle timeout.
* :class:`AsyncListener` - accept loop that tracks per-connection tasks and can
  shut down gracefully (stop accepting, drain briefly, then cancel).

None of these helpers ever receives credentials; upstream authentication lives
in :mod:`avdfleet.proxy.bridge.upstream` and is injected into protocol frames,
never into log records.
"""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from avdfleet.core.errors import ProxyError, ValidationError
from avdfleet.core.logging import get_logger, redact_text

_LOGGER = get_logger("proxy.bridge")

DEFAULT_BRIDGE_PORT_START = 7800
DEFAULT_BRIDGE_PORT_SPAN = 1024
MAX_HEADER_BYTES = 65536
MAX_HEADER_COUNT = 200
DEFAULT_BUFFER_BYTES = 65536
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


# ---------------------------------------------------------------------------
# Protocol failures (not user facing; mapped to HTTP status / SOCKS replies)
# ---------------------------------------------------------------------------


class BridgeProtocolError(Exception):
    """Malformed peer framing; the listener answers with a protocol error."""


class HeaderTooLargeError(BridgeProtocolError):
    """The peer sent a request/response head larger than the configured limit."""


class HeaderCountError(BridgeProtocolError):
    """The peer sent more header fields than the configured limit."""


class HeadTimeoutError(BridgeProtocolError):
    """The peer did not finish its head within the configured timeout."""


class PeerClosedError(BridgeProtocolError):
    """The peer closed before the head was complete."""


class TunnelIdleTimeoutError(Exception):
    """A relay direction saw no traffic for the configured idle timeout."""


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


def _is_loopback_host(host: str) -> bool:
    if host.lower() in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class BridgeOptions:
    """Tunables shared by the HTTP and SOCKS5 listeners.

    ``listen_host`` is validated to be a loopback address: the bridge is a
    trust boundary for the local host, never an open proxy.  ``port_start`` is
    the first port considered by the allocator; ``port_end`` defaults to
    ``port_start + 1024``.  ``remote_dns`` selects SOCKS5h semantics for SOCKS5
    upstreams (hostname resolved by the upstream proxy) versus local DNS.
    """

    listen_host: str = "127.0.0.1"
    port_start: int | None = None
    port_end: int | None = None
    enable_socks: bool = True
    connect_timeout_s: float = 15.0
    header_timeout_s: float = 15.0
    idle_timeout_s: float | None = 300.0
    shutdown_grace_s: float = 1.0
    max_header_bytes: int = MAX_HEADER_BYTES
    max_headers: int = MAX_HEADER_COUNT
    max_connections: int = 256
    backlog: int = 64
    buffer_bytes: int = DEFAULT_BUFFER_BYTES
    remote_dns: bool = True
    tls_verify: bool = True

    def __post_init__(self) -> None:
        host = (self.listen_host or "").strip()
        if not host:
            raise ValidationError("bridge listen_host must not be empty")
        if not _is_loopback_host(host):
            raise ValidationError(
                f"bridge must bind to a loopback address, not {host!r}",
                hint="These listeners are an authenticated-egress boundary; binding beyond "
                "127.0.0.1 would create an open proxy.",
            )
        if self.port_start is not None and not 1 <= self.port_start <= 65535:
            raise ValidationError("bridge port_start must be 1..65535")
        if self.port_end is not None:
            if not 1 <= self.port_end <= 65535:
                raise ValidationError("bridge port_end must be 1..65535")
            if self.port_start is not None and self.port_end < self.port_start:
                raise ValidationError("bridge port_end must be >= port_start")
        if self.connect_timeout_s <= 0 or self.header_timeout_s <= 0:
            raise ValidationError("bridge connection/header timeouts must be positive")
        if self.idle_timeout_s is not None and self.idle_timeout_s <= 0:
            raise ValidationError("bridge idle_timeout_s must be positive or None")
        if self.shutdown_grace_s < 0:
            raise ValidationError("bridge shutdown_grace_s must be >= 0")
        if self.max_header_bytes < 1024:
            raise ValidationError("bridge max_header_bytes must be >= 1024")
        if self.max_headers < 1:
            raise ValidationError("bridge max_headers must be >= 1")
        if self.max_connections < 1:
            raise ValidationError("bridge max_connections must be >= 1")
        if self.buffer_bytes < 1024:
            raise ValidationError("bridge buffer_bytes must be >= 1024")

    @property
    def effective_port_start(self) -> int:
        return self.port_start if self.port_start is not None else DEFAULT_BRIDGE_PORT_START

    @property
    def effective_port_end(self) -> int:
        if self.port_end is not None:
            return self.port_end
        return min(self.effective_port_start + DEFAULT_BRIDGE_PORT_SPAN - 1, 65535)


# ---------------------------------------------------------------------------
# HTTP head framing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpHead:
    """A parsed HTTP message head (request or response)."""

    start_line: bytes
    headers: tuple[tuple[bytes, bytes], ...]

    def header(self, name: bytes | str) -> str | None:
        """Return the first header value with a case-insensitive name match."""
        needle = name.encode("latin-1") if isinstance(name, str) else name
        needle = needle.lower()
        for key, value in self.headers:
            if key.lower() == needle:
                return value.decode("latin-1")
        return None

    def header_values(self, name: bytes | str) -> list[str]:
        needle = name.encode("latin-1") if isinstance(name, str) else name
        needle = needle.lower()
        return [value.decode("latin-1") for key, value in self.headers if key.lower() == needle]


async def _read_chunk(reader: asyncio.StreamReader, *, timeout_s: float | None, size: int) -> bytes:
    try:
        if timeout_s is None:
            return await reader.read(size)
        return await asyncio.wait_for(reader.read(size), timeout=timeout_s)
    except TimeoutError as exc:
        raise HeadTimeoutError("timed out reading peer head") from exc


async def read_http_head(
    reader: asyncio.StreamReader,
    *,
    max_bytes: int = MAX_HEADER_BYTES,
    limited_timeout_s: float | None = None,
    max_headers: int = MAX_HEADER_COUNT,
) -> tuple[HttpHead, bytes]:
    """Read one HTTP head and return ``(head, leftover_bytes)``.

    Reads in bounded chunks until the CRLFCRLF terminator.  A peer that sends a
    larger head than *max_bytes* raises :class:`HeaderTooLargeError` instead of
    growing an unbounded buffer.  Any bytes after the terminator are returned so
    callers (CONNECT tunnels in particular) do not drop early data.
    """
    buffer = bytearray()
    while True:
        if len(buffer) > max_bytes:
            raise HeaderTooLargeError("HTTP head exceeded the configured maximum size")
        try:
            chunk = await _read_chunk(reader, timeout_s=limited_timeout_s, size=4096)
        except HeadTimeoutError:
            raise
        if not chunk:
            if not buffer:
                raise PeerClosedError("peer closed before sending a request head")
            raise PeerClosedError("peer closed mid-head")
        buffer.extend(chunk)
        index = buffer.find(b"\r\n\r\n")
        if index >= 0:
            if index > max_bytes:
                raise HeaderTooLargeError("HTTP head exceeded the configured maximum size")
            head = bytes(buffer[:index])
            leftover = bytes(buffer[index + 4 :])
            return parse_http_head(head, max_headers=max_headers), leftover
        if len(buffer) > max_bytes:
            raise HeaderTooLargeError("HTTP head exceeded the configured maximum size")


def parse_http_head(raw: bytes, *, max_headers: int = MAX_HEADER_COUNT) -> HttpHead:
    """Parse a raw head (without the trailing CRLFCRLF) into start line + headers."""
    lines = raw.split(b"\r\n")
    if not lines or not lines[0]:
        raise BridgeProtocolError("empty start line")
    start_line = lines[0]
    headers: list[tuple[bytes, bytes]] = []
    for line in lines[1:]:
        if not line:
            continue
        if line[:1] in (b" ", b"\t"):
            raise BridgeProtocolError("obsolete header line folding is not supported")
        name, separator, value = line.partition(b":")
        if not separator or not name:
            raise BridgeProtocolError("malformed header line")
        if len(headers) >= max_headers:
            raise HeaderCountError("too many header fields")
        name = name.strip()
        if not name or any(byte < 0x21 or byte == 0x7F for byte in name):
            raise BridgeProtocolError("invalid header name")
        headers.append((name, value.strip()))
    return HttpHead(start_line=start_line, headers=tuple(headers))


def split_start_line(start_line: bytes) -> tuple[str, str, str]:
    """Split ``METHOD target VERSION`` and validate the HTTP version."""
    parts = start_line.split(b" ")
    if len(parts) != 3:
        raise BridgeProtocolError("malformed request line")
    method, target, version = (part.decode("latin-1") for part in parts)
    if not method or any(char.isspace() for char in target) or not target:
        raise BridgeProtocolError("malformed request line")
    if version not in {"HTTP/1.0", "HTTP/1.1"}:
        raise BridgeProtocolError(f"unsupported HTTP version {version!r}")
    return method.upper(), target, version


def split_authority(authority: str, *, default_port: int | None = None) -> tuple[str, int]:
    """Parse ``host:port`` (including bracketed IPv6) into a resolvable pair."""
    if not authority or "@" in authority or "/" in authority or " " in authority:
        raise BridgeProtocolError("malformed authority")
    host: str
    port_text: str
    if authority.startswith("["):
        end = authority.find("]")
        if end < 0:
            raise BridgeProtocolError("malformed IPv6 authority")
        host = authority[1:end]
        rest = authority[end + 1 :]
        if rest and not rest.startswith(":"):
            raise BridgeProtocolError("malformed authority")
        port_text = rest[1:] if rest.startswith(":") else ""
    else:
        host, separator, port_text = authority.rpartition(":")
        if not separator:
            if default_port is None:
                raise BridgeProtocolError("authority is missing a port")
            host, port_text = authority, str(default_port)
    if not host:
        raise BridgeProtocolError("authority is missing a host")
    if not port_text.isdigit():
        raise BridgeProtocolError("authority port is not numeric")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise BridgeProtocolError("authority port is out of range")
    return host, port


# ---------------------------------------------------------------------------
# Relay / streaming
# ---------------------------------------------------------------------------


async def read_exact(reader: asyncio.StreamReader, size: int, *, timeout_s: float | None) -> bytes:
    """Read exactly *size* bytes or raise :class:`PeerClosedError`."""
    buffer = bytearray()
    while len(buffer) < size:
        try:
            chunk = await _read_chunk(reader, timeout_s=timeout_s, size=size - len(buffer))
        except HeadTimeoutError as exc:
            raise TunnelIdleTimeoutError("timed out reading from peer") from exc
        if not chunk:
            raise PeerClosedError("peer closed unexpectedly")
        buffer.extend(chunk)
    return bytes(buffer)


async def pipe_until_eof(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    idle_timeout_s: float | None,
    buffer_bytes: int,
) -> None:
    """Copy *reader* to *writer* until EOF or an idle timeout (backpressured)."""
    while True:
        try:
            chunk = await _read_chunk(reader, timeout_s=idle_timeout_s, size=buffer_bytes)
        except HeadTimeoutError as exc:
            raise TunnelIdleTimeoutError("tunnel idle timeout") from exc
        if not chunk:
            return
        writer.write(chunk)
        await writer.drain()


async def relay_bidirectional(
    reader_a: asyncio.StreamReader,
    writer_a: asyncio.StreamWriter,
    reader_b: asyncio.StreamReader,
    writer_b: asyncio.StreamWriter,
    *,
    idle_timeout_s: float | None,
    buffer_bytes: int = DEFAULT_BUFFER_BYTES,
) -> None:
    """Full-duplex, backpressured relay; closes the tunnel when either side ends."""

    async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await pipe_until_eof(reader, writer, idle_timeout_s=idle_timeout_s, buffer_bytes=buffer_bytes)
        except (TunnelIdleTimeoutError, OSError, ConnectionError):
            # Either end disconnecting (or an idle tunnel) ends the tunnel.
            return

    task_a = asyncio.create_task(pump(reader_a, writer_b))
    task_b = asyncio.create_task(pump(reader_b, writer_a))
    try:
        done, pending = await asyncio.wait({task_a, task_b}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task.cancelled():
                continue
            exception = task.exception()
            if exception is not None and not isinstance(exception, (TunnelIdleTimeoutError, OSError)):
                _LOGGER.debug("bridge relay direction failed: %s", type(exception).__name__)
    finally:
        for task in (task_a, task_b):
            if not task.done():
                task.cancel()
        await asyncio.gather(task_a, task_b, return_exceptions=True)


async def close_writer(writer: asyncio.StreamWriter) -> None:
    """Best-effort graceful close of a stream writer."""
    try:
        writer.close()
        await writer.wait_closed()
    except (OSError, ConnectionError, RuntimeError):
        pass


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]
OverloadHandler = Callable[[asyncio.StreamWriter], Awaitable[None]]


class AsyncListener:
    """An asyncio TCP listener that tracks its connections for graceful stop."""

    def __init__(
        self,
        handler: Handler,
        *,
        host: str,
        backlog: int = 64,
        max_connections: int = 256,
        on_overload: OverloadHandler | None = None,
        name: str = "bridge",
    ) -> None:
        self.host = host
        self._handler = handler
        self._backlog = backlog
        self._max_connections = max_connections
        self._on_overload = on_overload
        self._name = name
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def port(self) -> int | None:
        if not self._server or not self._server.sockets:
            return None
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def connection_count(self) -> int:
        return len(self._tasks)

    async def start(self, port: int = 0) -> int:
        if self._server is not None:
            raise RuntimeError(f"{self._name} listener already started")
        self._server = await asyncio.start_server(
            self._accept, host=self.host, port=port, backlog=self._backlog, reuse_address=True
        )
        bound = self.port
        if bound is None:  # pragma: no cover - defensive
            raise ProxyError("bridge listener did not report a bound port")
        return bound

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            if len(self._tasks) > self._max_connections:
                if self._on_overload is not None:
                    await self._on_overload(writer)
                await close_writer(writer)
                return
            await self._handler(reader, writer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # defensive: a bad connection must not kill the listener
            _LOGGER.debug(
                "%s listener connection failed: %s: %s",
                self._name,
                type(exc).__name__,
                redact_text(str(exc)),
            )
        finally:
            if task is not None:
                self._tasks.discard(task)
            await close_writer(writer)

    async def stop(self, *, grace_s: float = 1.0) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except (OSError, ConnectionError):
                pass
        pending = set(self._tasks)
        if pending:
            if grace_s > 0:
                done, still_pending = await asyncio.wait(pending, timeout=grace_s)
                pending = set(still_pending)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()


def upstream_error_message(exc: BaseException) -> str:
    """Return a safe (credential-free) one-line description of a bridge failure."""
    return redact_text(str(exc)) or type(exc).__name__


def header_pairs_to_bytes(headers: Sequence[tuple[str, str]]) -> bytes:
    """Serialize ``[(name, value)]`` into CRLF header bytes."""
    return b"".join(f"{name}: {value}\r\n".encode("latin-1", "replace") for name, value in headers)


__all__ = [
    "DEFAULT_BRIDGE_PORT_START",
    "AsyncListener",
    "BridgeOptions",
    "BridgeProtocolError",
    "HeadTimeoutError",
    "HeaderCountError",
    "HeaderTooLargeError",
    "HttpHead",
    "PeerClosedError",
    "TunnelIdleTimeoutError",
    "close_writer",
    "header_pairs_to_bytes",
    "parse_http_head",
    "pipe_until_eof",
    "read_exact",
    "read_http_head",
    "relay_bidirectional",
    "split_authority",
    "split_start_line",
    "upstream_error_message",
]
