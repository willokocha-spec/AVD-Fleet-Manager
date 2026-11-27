"""Client-facing HTTP proxy listener for one AVD instance.

The guest (emulator) is pointed at ``http://127.0.0.1:<listen_port>``.  This
listener accepts two request shapes:

``CONNECT host:port``
    Opens a tunnel through the instance's upstream and then relays bytes in both
    directions with backpressure.  This is how HTTPS, and any raw TCP protocol,
    reaches the network; the upstream (or, for direct mode, the host) resolves
    the target name.

absolute-URI requests (``GET http://host/path HTTP/1.1``)
    Plain HTTP is forwarded through the upstream in absolute-URI form (for HTTP
    proxies) or rewritten to origin-form (direct/SOCKS5).  One request per
    connection: the bridge answers with ``Connection: close`` and copies the
    response until EOF, so chunked or content-length framing is preserved
    verbatim.  Chunked *request* bodies are not supported (``501``); emulator
    workloads use content-length uploads.

Origin-form requests with a ``Host`` header are accepted as a convenience and
treated as plain HTTP to that host.  ``https`` absolute targets are rejected
with a hint to use ``CONNECT``.

The listener binds loopback only.  Optionally an instance-scoped token can be
required: clients then authenticate with
``Proxy-Authorization: Basic base64("<instance>:<token>")``.  Tokens are never
embedded in :class:`~avdfleet.core.models.BridgeConfig` or log records.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
from dataclasses import dataclass
from urllib.parse import urlsplit

from avdfleet.core.logging import get_logger

from .server import (
    AsyncListener,
    BridgeOptions,
    BridgeProtocolError,
    HeaderTooLargeError,
    HeadTimeoutError,
    HttpHead,
    PeerClosedError,
    TunnelIdleTimeoutError,
    close_writer,
    pipe_until_eof,
    read_exact,
    read_http_head,
    relay_bidirectional,
    split_authority,
    split_start_line,
    upstream_error_message,
)
from .upstream import Upstream, UpstreamError

_LOGGER = get_logger("proxy.bridge.http")

_STATUS_REASONS = {
    400: "Bad Request",
    405: "Method Not Allowed",
    408: "Request Timeout",
    411: "Length Required",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}

_HOP_BY_HOP = {
    b"proxy-authorization",
    b"proxy-connection",
    b"connection",
    b"keep-alive",
    b"te",
    b"trailer",
    b"upgrade",
}

_HOP_BY_HOP_RESPONSE = {
    b"proxy-authenticate",
    b"proxy-connection",
    b"connection",
    b"keep-alive",
    b"te",
    b"trailer",
    b"upgrade",
}

_LOCAL_REALM = 'Basic realm="avdfleet-bridge"'


@dataclass(frozen=True)
class _ForwardTarget:
    host: str
    port: int
    origin_form: str
    absolute_uri: str


class HttpProxyServer:
    """One instance-scoped HTTP proxy listener."""

    def __init__(
        self,
        instance: str,
        upstream_getter: object,
        options: BridgeOptions,
        *,
        local_auth_token: str | None = None,
    ) -> None:
        # ``upstream_getter`` is a zero-argument callable returning the current
        # :class:`Upstream`.  The indirection is what makes hot swap work without
        # rebinding the listener.
        self._instance = instance
        self._get_upstream = upstream_getter
        self._options = options
        self._local_auth_token = local_auth_token
        self._listener = AsyncListener(
            self._handle,
            host=options.listen_host,
            backlog=options.backlog,
            max_connections=options.max_connections,
            on_overload=self._on_overload,
            name=f"http-bridge[{instance}]",
        )

    # -- lifecycle -------------------------------------------------------
    @property
    def port(self) -> int | None:
        return self._listener.port

    async def start(self, port: int = 0) -> int:
        return await self._listener.start(port)

    async def stop(self, *, grace_s: float | None = None) -> None:
        await self._listener.stop(
            grace_s=self._options.shutdown_grace_s if grace_s is None else grace_s
        )

    async def _on_overload(self, writer: asyncio.StreamWriter) -> None:
        await self._write_status(writer, 503, body=b"avdfleet bridge: too many connections")

    # -- per connection --------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head, leftover = await read_http_head(
                reader,
                max_bytes=self._options.max_header_bytes,
                limited_timeout_s=self._options.header_timeout_s,
                max_headers=self._options.max_headers,
            )
        except HeaderTooLargeError:
            await self._write_status(writer, 431, body=b"avdfleet bridge: request head too large")
            return
        except HeadTimeoutError:
            await self._write_status(writer, 408, body=b"avdfleet bridge: request head timed out")
            return
        except PeerClosedError:
            return
        except BridgeProtocolError:
            await self._write_status(writer, 400, body=b"avdfleet bridge: malformed request")
            return
        except (OSError, ConnectionError):
            return

        try:
            method, target, version = split_start_line(head.start_line)
        except BridgeProtocolError:
            await self._write_status(writer, 400, body=b"avdfleet bridge: malformed request line")
            return

        if not self._local_auth_ok(head):
            await self._write_status(
                writer,
                407,
                body=b"avdfleet bridge: proxy authentication required",
                extra_headers=[(b"Proxy-Authenticate", _LOCAL_REALM.encode("latin-1"))],
            )
            return

        if method == "CONNECT":
            await self._handle_connect(reader, writer, target, leftover)
        else:
            await self._handle_forward(reader, writer, method, target, version, head, leftover)

    # -- CONNECT ---------------------------------------------------------
    async def _handle_connect(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        target: str,
        early_data: bytes,
    ) -> None:
        try:
            host, port = split_authority(target, default_port=443)
        except BridgeProtocolError:
            await self._write_status(writer, 400, body=b"avdfleet bridge: malformed CONNECT target")
            return

        upstream = self._current_upstream()
        try:
            tunnel = await upstream.open_tunnel(
                host,
                port,
                connect_timeout_s=self._options.connect_timeout_s,
                header_timeout_s=self._options.header_timeout_s,
                max_header_bytes=self._options.max_header_bytes,
            )
        except UpstreamError as exc:
            _LOGGER.info(
                "bridge CONNECT failed instance=%s upstream=%s reason=%s",
                self._instance,
                upstream.display or "direct",
                upstream_error_message(exc),
            )
            await self._write_status(
                writer,
                exc.http_status,
                body=f"avdfleet bridge: {upstream_error_message(exc)}".encode(),
            )
            return
        except (TimeoutError, TunnelIdleTimeoutError):
            await self._write_status(writer, 504, body=b"avdfleet bridge: upstream timed out")
            return
        except HeadTimeoutError:
            await self._write_status(writer, 504, body=b"avdfleet bridge: upstream timed out")
            return
        except BridgeProtocolError:
            await self._write_status(writer, 502, body=b"avdfleet bridge: upstream protocol error")
            return
        except (OSError, ConnectionError):
            await self._write_status(writer, 502, body=b"avdfleet bridge: upstream connection failed")
            return

        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: avdfleet-bridge\r\n\r\n")
            if early_data:
                writer.write(early_data)
            if tunnel.initial:
                writer.write(tunnel.initial)
            await writer.drain()
            await relay_bidirectional(
                reader,
                writer,
                tunnel.reader,
                tunnel.writer,
                idle_timeout_s=self._options.idle_timeout_s,
                buffer_bytes=self._options.buffer_bytes,
            )
        finally:
            await close_writer(tunnel.writer)

    # -- absolute-URI / origin-form forwarding --------------------------
    async def _handle_forward(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        target: str,
        version: str,
        head: HttpHead,
        early_data: bytes,
    ) -> None:
        try:
            forward = _parse_forward_target(target, head)
        except _ForwardTargetError as exc:
            await self._write_status(writer, exc.status, body=f"avdfleet bridge: {exc}".encode())
            return

        if _is_chunked_request(head):
            await self._write_status(
                writer, 501, body=b"avdfleet bridge: chunked request bodies are not supported"
            )
            return
        try:
            body_length = _content_length(head)
        except _ForwardTargetError as exc:
            await self._write_status(writer, exc.status, body=f"avdfleet bridge: {exc}".encode())
            return

        upstream = self._current_upstream()
        request_target = forward.absolute_uri if upstream.uses_absolute_form else forward.origin_form
        request_headers = _request_headers(head, upstream, forward)
        expects_continue = any(
            value.lower().strip() == "100-continue" for value in head.header_values("expect")
        )
        if expects_continue and body_length > 0:
            writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            await writer.drain()

        try:
            upstream_reader, upstream_writer = await upstream.open_forward(
                forward.host,
                forward.port,
                connect_timeout_s=self._options.connect_timeout_s,
            )
        except UpstreamError as exc:
            await self._write_status(
                writer, exc.http_status, body=f"avdfleet bridge: {upstream_error_message(exc)}".encode()
            )
            return
        except TimeoutError:
            await self._write_status(writer, 504, body=b"avdfleet bridge: upstream timed out")
            return
        except BridgeProtocolError:
            await self._write_status(writer, 502, body=b"avdfleet bridge: upstream protocol error")
            return
        except (OSError, ConnectionError):
            await self._write_status(writer, 502, body=b"avdfleet bridge: upstream connection failed")
            return

        responded = False
        try:
            request_head = _serialize_request_head(method, request_target, version, request_headers)
            upstream_writer.write(request_head)
            await upstream_writer.drain()
            await _send_body(
                reader,
                upstream_writer,
                length=body_length,
                early=early_data,
                timeout_s=self._body_timeout,
            )

            response_head, leftover = await _read_upstream_response_head(
                upstream_reader, self._options, client_writer=writer
            )
            if response_head is None:
                return
            if response_head.status == 407:
                responded = True
                await self._write_status(
                    writer, 502, body=b"avdfleet bridge: upstream proxy authentication failed"
                )
                return
            responded = True
            writer.write(_serialize_response_head(response_head))
            if leftover:
                writer.write(leftover)
            await writer.drain()
            await pipe_until_eof(
                upstream_reader,
                writer,
                idle_timeout_s=self._options.idle_timeout_s,
                buffer_bytes=self._options.buffer_bytes,
            )
        except UpstreamError as exc:
            _LOGGER.info(
                "bridge forward failed instance=%s upstream=%s reason=%s",
                self._instance,
                upstream.display or "direct",
                upstream_error_message(exc),
            )
            if not responded:
                await self._write_status(
                    writer,
                    exc.http_status,
                    body=f"avdfleet bridge: {upstream_error_message(exc)}".encode(),
                )
        except (TimeoutError, TunnelIdleTimeoutError):
            if not responded:
                await self._write_status(writer, 504, body=b"avdfleet bridge: upstream timed out")
        except HeadTimeoutError:
            if not responded:
                await self._write_status(writer, 504, body=b"avdfleet bridge: upstream timed out")
        except (OSError, ConnectionError):
            if not responded:
                await self._write_status(writer, 502, body=b"avdfleet bridge: upstream connection failed")
        except BridgeProtocolError:
            if not responded:
                await self._write_status(writer, 502, body=b"avdfleet bridge: malformed upstream response")
        finally:
            await close_writer(upstream_writer)

    # -- helpers ---------------------------------------------------------
    @property
    def _body_timeout(self) -> float:
        return self._options.idle_timeout_s if self._options.idle_timeout_s is not None else 60.0

    def _current_upstream(self) -> Upstream:
        return self._get_upstream()  # type: ignore[operator]

    def _local_auth_ok(self, head: HttpHead) -> bool:
        if self._local_auth_token is None:
            return True
        value = head.header("proxy-authorization")
        if not value:
            return False
        scheme, separator, encoded = value.partition(" ")
        if not separator or scheme.lower() != "basic":
            return False
        try:
            decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return False
        username, separator, password = decoded.partition(":")
        if not separator:
            return False
        return hmac.compare_digest(username, self._instance) and hmac.compare_digest(
            password, self._local_auth_token
        )

    async def _write_status(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        *,
        body: bytes | None = None,
        extra_headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        reason = _STATUS_REASONS.get(status, "Error")
        payload = body if body is not None else b""
        lines = [
            f"HTTP/1.1 {status} {reason}".encode("latin-1"),
            b"Content-Type: text/plain; charset=utf-8",
            f"Content-Length: {len(payload)}".encode("latin-1"),
            b"Connection: close",
            b"Proxy-Agent: avdfleet-bridge",
        ]
        for name, value in extra_headers or []:
            lines.append(name + b": " + value)
        try:
            writer.write(b"\r\n".join(lines) + b"\r\n\r\n" + payload)
            await writer.drain()
        except (OSError, ConnectionError):
            pass


# ---------------------------------------------------------------------------
# Request analysis helpers
# ---------------------------------------------------------------------------


class _ForwardTargetError(Exception):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _parse_forward_target(target: str, head: HttpHead) -> _ForwardTarget:
    if target.startswith("http://") or target.startswith("https://"):
        try:
            parsed = urlsplit(target)
            host = parsed.hostname
            port = parsed.port or 80
        except ValueError as exc:
            raise _ForwardTargetError("malformed absolute request URI") from exc
        if parsed.scheme == "https":
            raise _ForwardTargetError("HTTPS requests must use CONNECT")
        if not host:
            raise _ForwardTargetError("absolute request URI has no host")
        origin_form = parsed.path or "/"
        if parsed.query:
            origin_form = f"{origin_form}?{parsed.query}"
        return _ForwardTarget(host=host, port=port, origin_form=origin_form, absolute_uri=target)

    if target.startswith("/"):
        host_header = head.header("host")
        if not host_header:
            raise _ForwardTargetError("origin-form request is missing a Host header")
        try:
            host, port = split_authority(host_header, default_port=80)
        except BridgeProtocolError as exc:
            raise _ForwardTargetError("malformed Host header") from exc
        return _ForwardTarget(
            host=host,
            port=port,
            origin_form=target,
            absolute_uri=f"http://{host_header}{target}",
        )

    raise _ForwardTargetError("unsupported request target")


def _is_chunked_request(head: HttpHead) -> bool:
    values = head.header_values("transfer-encoding")
    return any("chunked" in value.lower() for value in values)


def _content_length(head: HttpHead) -> int:
    values = head.header_values("content-length")
    if not values:
        return 0
    unique = {value.strip() for value in values}
    if len(unique) != 1:
        raise _ForwardTargetError("conflicting Content-Length headers")
    raw = next(iter(unique))
    if not raw.isdigit():
        raise _ForwardTargetError("invalid Content-Length header")
    length = int(raw)
    if length < 0:
        raise _ForwardTargetError("invalid Content-Length header")
    return length


def _request_headers(
    head: HttpHead, upstream: Upstream, forward: _ForwardTarget
) -> list[tuple[bytes, bytes]]:
    headers: list[tuple[bytes, bytes]] = []
    has_host = False
    for name, value in head.headers:
        lower = name.lower()
        if lower in _HOP_BY_HOP:
            continue
        if lower == b"expect":
            # The bridge answers 100-continue itself so request bodies can be
            # streamed immediately.
            continue
        if lower == b"host":
            has_host = True
        headers.append((name, value))
    if not has_host:
        host_value = forward.host if forward.port == 80 else f"{forward.host}:{forward.port}"
        headers.append((b"Host", host_value.encode("latin-1")))
    headers.append((b"Connection", b"close"))
    if upstream.uses_absolute_form:
        headers.extend(upstream.forward_headers())
    return headers


def _serialize_request_head(
    method: str, target: str, version: str, headers: list[tuple[bytes, bytes]]
) -> bytes:
    lines = [f"{method} {target} {version}".encode("latin-1")]
    lines.extend(name + b": " + value for name, value in headers)
    return b"\r\n".join(lines) + b"\r\n\r\n"


@dataclass(frozen=True)
class _ResponseHead:
    status: int
    raw: bytes


def _serialize_response_head(head: _ResponseHead) -> bytes:
    """Forward a response head verbatim except for hop-by-hop proxy headers."""
    raw_head = head.raw
    start_line, _, rest = raw_head.partition(b"\r\n")
    lines = [start_line]
    for line in rest.split(b"\r\n"):
        if not line:
            continue
        name, _, _value = line.partition(b":")
        if name.strip().lower() in _HOP_BY_HOP_RESPONSE:
            continue
        lines.append(line)
    lines.append(b"Connection: close")
    return b"\r\n".join(lines) + b"\r\n\r\n"


async def _read_upstream_response_head(
    reader: asyncio.StreamReader,
    options: BridgeOptions,
    *,
    client_writer: asyncio.StreamWriter,
) -> tuple[_ResponseHead | None, bytes]:
    """Read the final response head, forwarding 1xx interim responses to the client."""
    while True:
        parsed, leftover = await read_http_head(
            reader,
            max_bytes=options.max_header_bytes,
            limited_timeout_s=options.header_timeout_s,
            max_headers=options.max_headers,
        )
        parts = parsed.start_line.split(b" ", 2)
        if len(parts) < 2 or not parts[0].upper().startswith(b"HTTP/"):
            raise BridgeProtocolError("upstream sent a malformed response")
        try:
            status = int(parts[1])
        except ValueError as exc:
            raise BridgeProtocolError("upstream sent a malformed status") from exc
        raw = _rebuild_head(parsed)
        if 100 <= status < 200:
            if status == 101:
                raise BridgeProtocolError("upstream protocol upgrades are not supported")
            try:
                client_writer.write(raw)
                await client_writer.drain()
            except (OSError, ConnectionError):
                return None, b""
            continue
        return _ResponseHead(status=status, raw=raw), leftover


def _rebuild_head(head: HttpHead) -> bytes:
    lines = [head.start_line]
    lines.extend(name + b": " + value for name, value in head.headers)
    return b"\r\n".join(lines) + b"\r\n\r\n"


async def _send_body(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    length: int,
    early: bytes,
    timeout_s: float,
) -> None:
    if length <= 0:
        return
    sent = 0
    if early:
        chunk = early[:length]
        writer.write(chunk)
        await writer.drain()
        sent += len(chunk)
    while sent < length:
        try:
            chunk = await read_exact(reader, min(length - sent, 65536), timeout_s=timeout_s)
        except PeerClosedError as exc:
            raise OSError("client closed during request body") from exc
        writer.write(chunk)
        await writer.drain()
        sent += len(chunk)


__all__ = ["HttpProxyServer"]
