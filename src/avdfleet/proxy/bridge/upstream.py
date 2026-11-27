"""Upstream proxy clients for the local bridge.

Three upstream modes are supported, matching :class:`avdfleet.core.models.ProxyScheme`:

``http`` / ``https`` forward proxy
    Optional Basic authentication (``Proxy-Authorization``).  Tunnels are opened
    with ``CONNECT``; plain-HTTP requests are forwarded in absolute-URI form.
    With the ``https`` scheme the hop from the bridge to the proxy itself is
    TLS-wrapped; the target hostname travels in the CONNECT request, so the
    *upstream* resolves it (guest DNS never touches the host resolver).

``socks5`` proxy
    Optional username/password authentication (RFC 1929).  DNS policy is explicit:
    the default is SOCKS5h semantics, where the hostname is sent to the proxy
    (``ATYP=domain``) so guest DNS cannot leak.  A proxy tagged
    ``socks5-local-dns`` (or a bridge constructed with ``remote_dns=False``)
    resolves locally first (``ATYP=IPv4/IPv6``) for the rare proxy that cannot
    resolve names itself.

``direct`` (no upstream)
    Only used for tests and explicit fail-open/failover wiring; connections go
    straight from the host to the guest's target.

Credentials are injected into protocol frames at connection time.  They are
never placed into :class:`~avdfleet.core.models.BridgeConfig`, never logged and
never embedded in exception messages; :attr:`Upstream.display` is the only
representation a caller should ever serialize.
"""

from __future__ import annotations

import asyncio
import base64
import socket
import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from avdfleet.core.logging import redact_text
from avdfleet.core.models import ProxyConfig, ProxyScheme

from .server import (
    HeadTimeoutError,
    HttpHead,
    PeerClosedError,
    TunnelIdleTimeoutError,
    read_exact,
    read_http_head,
)

_SOCKS_VERSION = 0x05
_SOCKS_AUTH_VERSION = 0x01
_SOCKS_CMD_CONNECT = 0x01
_SOCKS_ATYP_IPV4 = 0x01
_SOCKS_ATYP_DOMAIN = 0x03
_SOCKS_ATYP_IPV6 = 0x04
_SOCKS_METHOD_NO_AUTH = 0x00
_SOCKS_METHOD_USERPASS = 0x02
_SOCKS_METHOD_NONE = 0xFF
_SOCKS_REPLY_SUCCESS = 0x00

_SOCKS_REPLY_MESSAGES = {
    0x01: "general SOCKS server failure",
    0x02: "connection not allowed by ruleset",
    0x03: "network unreachable",
    0x04: "host unreachable",
    0x05: "connection refused",
    0x06: "TTL expired",
    0x07: "command not supported",
    0x08: "address type not supported",
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UpstreamError(Exception):
    """Base class for upstream connection failures.

    ``http_status`` is the status the HTTP listener answers with and
    ``socks_reply`` the SOCKS5 reply code the SOCKS listener answers with.
    Messages are always safe to log (they never contain credentials).
    """

    http_status: int = 502
    socks_reply: int = 0x01

    def __init__(self, message: str) -> None:
        super().__init__(redact_text(message))
        self.message = redact_text(message)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class UpstreamAuthError(UpstreamError):
    """The upstream rejected the configured credentials."""

    http_status = 502
    socks_reply = 0x02

    def __init__(self) -> None:
        super().__init__("upstream proxy rejected authentication")


class UpstreamConnectError(UpstreamError):
    """The upstream could not reach the target (or was unreachable itself)."""

    http_status = 502
    socks_reply = 0x05


class UpstreamTimeoutError(UpstreamError):
    """Connecting through the upstream timed out."""

    http_status = 504
    socks_reply = 0x03

    def __init__(self, message: str = "upstream connection timed out") -> None:
        super().__init__(message)


class UpstreamDnsError(UpstreamError):
    """Name resolution failed (local DNS only; SOCKS5h defers to the upstream)."""

    http_status = 502
    socks_reply = 0x04


class UpstreamProtocolError(UpstreamError):
    """The upstream sent a malformed or unexpected protocol frame."""

    http_status = 502
    socks_reply = 0x01


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Credentials:
    """Upstream credentials.  ``repr`` never reveals the password."""

    username: str = ""
    password: str = field(default="", repr=False)

    @property
    def present(self) -> bool:
        return bool(self.username or self.password)

    def basic_header(self) -> str:
        token = f"{self.username}:{self.password}".encode()
        return "Basic " + base64.b64encode(token).decode("ascii")

    def socks_pair(self) -> tuple[bytes, bytes]:
        user = self.username.encode("utf-8")
        secret = self.password.encode("utf-8")
        if len(user) > 255 or len(secret) > 255:
            raise UpstreamProtocolError("SOCKS5 credentials exceed the 255-byte protocol limit")
        return user, secret


def credentials_from_proxy(proxy: ProxyConfig) -> Credentials:
    password = proxy.password.get_secret_value() if proxy.password is not None else ""
    return Credentials(username=proxy.username or "", password=password or "")


# ---------------------------------------------------------------------------
# Upstream implementations
# ---------------------------------------------------------------------------


@dataclass
class Tunnel:
    """An established byte tunnel plus any early bytes read while handshaking."""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    initial: bytes = b""


class Upstream(ABC):
    """One configured upstream proxy (or a direct connection)."""

    mode: str
    display: str
    proxy_id: str = ""
    uses_absolute_form: bool = False

    @abstractmethod
    async def open_tunnel(
        self,
        host: str,
        port: int,
        *,
        connect_timeout_s: float,
        header_timeout_s: float,
        max_header_bytes: int,
    ) -> Tunnel:
        """Open a raw tunnel to ``host:port`` through this upstream."""

    @abstractmethod
    async def open_forward(
        self,
        host: str,
        port: int,
        *,
        connect_timeout_s: float,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a plain-HTTP forwarding stream (absolute form for HTTP proxies)."""

    def forward_headers(self) -> list[tuple[bytes, bytes]]:
        """Extra headers (e.g. ``Proxy-Authorization``) for forwarding requests."""
        return []

    def identity(self) -> tuple[object, ...]:
        """Stable identity used for ``ensure`` idempotency (never serialized)."""
        return (self.mode, self.display, self.proxy_id)


async def open_host_connection(
    host: str,
    port: int,
    *,
    connect_timeout_s: float,
    ssl_context: ssl.SSLContext | None = None,
    server_hostname: str | None = None,
    what: str = "target",
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to ``host:port``, trying IPv4 addresses before IPv6 ones.

    A few Windows hosts blackhole ``::1`` instead of refusing it, so resolving
    ``localhost`` to ``::1`` first would burn the entire connect timeout on an
    unreachable address before the IPv4 attempt.  Preferring IPv4 keeps the
    bridge usable for loopback and dual-stack names without special cases.
    IPv6 remains the fallback when no IPv4 address exists.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + connect_timeout_s
    try:
        addresses = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UpstreamDnsError(f"could not resolve the {what} host") from exc
    ordered = sorted(addresses, key=lambda info: 1 if info[0] == socket.AF_INET6 else 0)
    last_error: BaseException | None = None
    for family, _type, _proto, _canonname, sockaddr in ordered:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        address_host = str(sockaddr[0])
        address_port = int(sockaddr[1])
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(
                    address_host,
                    address_port,
                    family=family,
                    ssl=ssl_context,
                    server_hostname=server_hostname if ssl_context is not None else None,
                ),
                timeout=remaining,
            )
        except TimeoutError as exc:
            last_error = exc
        except ssl.SSLError as exc:
            last_error = exc
        except OSError as exc:
            last_error = exc
    if isinstance(last_error, ssl.SSLError):
        raise UpstreamConnectError(f"TLS negotiation with the {what} failed") from last_error
    if isinstance(last_error, TimeoutError) or last_error is None:
        raise UpstreamTimeoutError(f"timed out connecting to the {what}") from last_error
    raise UpstreamConnectError(
        f"could not connect to the {what} ({type(last_error).__name__})"
    ) from last_error


class DirectUpstream(Upstream):
    """No upstream: connect straight to the guest's target (tests/failover)."""

    mode = "direct"
    uses_absolute_form = False

    def __init__(self, *, proxy_id: str = "") -> None:
        self.proxy_id = proxy_id
        self.display = ""

    def identity(self) -> tuple[object, ...]:
        return ("direct", self.proxy_id)

    async def _connect(
        self, host: str, port: int, *, connect_timeout_s: float
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await open_host_connection(
            host, port, connect_timeout_s=connect_timeout_s, what="target"
        )

    async def open_tunnel(
        self,
        host: str,
        port: int,
        *,
        connect_timeout_s: float,
        header_timeout_s: float,
        max_header_bytes: int,
    ) -> Tunnel:
        reader, writer = await self._connect(host, port, connect_timeout_s=connect_timeout_s)
        return Tunnel(reader=reader, writer=writer)

    async def open_forward(
        self, host: str, port: int, *, connect_timeout_s: float
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await self._connect(host, port, connect_timeout_s=connect_timeout_s)


class HttpUpstream(Upstream):
    """An HTTP or HTTPS forward proxy with optional Basic authentication."""

    uses_absolute_form = True

    def __init__(
        self,
        host: str,
        port: int,
        *,
        scheme: str = "http",
        credentials: Credentials | None = None,
        tls_context: ssl.SSLContext | None = None,
        proxy_id: str = "",
    ) -> None:
        self.host = host
        self.port = port
        self.scheme = scheme
        self._credentials = credentials if credentials and credentials.present else None
        self._tls_context = tls_context
        self.mode = "http"
        self.proxy_id = proxy_id
        self.display = f"{scheme}://{host}:{port}"

    def identity(self) -> tuple[object, ...]:
        username = self._credentials.username if self._credentials else ""
        password = self._credentials.password if self._credentials else ""
        return ("http", self.scheme, self.host, self.port, username, password, self.proxy_id)

    def forward_headers(self) -> list[tuple[bytes, bytes]]:
        if self._credentials is None:
            return []
        return [(b"Proxy-Authorization", self._credentials.basic_header().encode("latin-1"))]

    async def _connect_proxy(self, connect_timeout_s: float) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        context = self._tls_context if self.scheme == "https" else None
        return await open_host_connection(
            self.host,
            self.port,
            connect_timeout_s=connect_timeout_s,
            ssl_context=context,
            server_hostname=self.host if context is not None else None,
            what="upstream proxy",
        )

    def _authorization_headers(self) -> bytes:
        if self._credentials is None:
            return b""
        return b"Proxy-Authorization: " + self._credentials.basic_header().encode("latin-1") + b"\r\n"

    async def open_tunnel(
        self,
        host: str,
        port: int,
        *,
        connect_timeout_s: float,
        header_timeout_s: float,
        max_header_bytes: int,
    ) -> Tunnel:
        reader, writer = await self._connect_proxy(connect_timeout_s)
        authority = f"{host}:{port}" if ":" not in host else f"[{host}]:{port}"
        request = (
            f"CONNECT {authority} HTTP/1.1\r\n"
            f"Host: {authority}\r\n"
            f"{self._authorization_headers().decode('latin-1')}"
            "Proxy-Connection: keep-alive\r\n"
            "\r\n"
        ).encode("latin-1")
        try:
            writer.write(request)
            await writer.drain()
            head, leftover = await read_http_head(
                reader,
                max_bytes=max_header_bytes,
                limited_timeout_s=header_timeout_s,
            )
        except PeerClosedError as exc:
            await _safe_close(writer)
            raise UpstreamProtocolError("upstream proxy closed during CONNECT") from exc
        except HeadTimeoutError as exc:
            await _safe_close(writer)
            raise UpstreamTimeoutError("upstream proxy did not answer CONNECT in time") from exc
        except BaseException:
            await _safe_close(writer)
            raise
        status = _response_status(head)
        if status == 407:
            await _safe_close(writer)
            raise UpstreamAuthError()
        if not 200 <= status < 300:
            await _safe_close(writer)
            raise UpstreamConnectError(f"upstream proxy refused CONNECT (HTTP {status})")
        return Tunnel(reader=reader, writer=writer, initial=leftover)

    async def open_forward(
        self, host: str, port: int, *, connect_timeout_s: float
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await self._connect_proxy(connect_timeout_s)


class Socks5Upstream(Upstream):
    """A SOCKS5 proxy with optional username/password authentication."""

    uses_absolute_form = False

    def __init__(
        self,
        host: str,
        port: int,
        *,
        credentials: Credentials | None = None,
        remote_dns: bool = True,
        proxy_id: str = "",
    ) -> None:
        self.host = host
        self.port = port
        self.mode = "socks5"
        self.proxy_id = proxy_id
        self.remote_dns = remote_dns
        self.display = f"socks5://{host}:{port}"
        self._credentials = credentials if credentials and credentials.present else None

    def identity(self) -> tuple[object, ...]:
        username = self._credentials.username if self._credentials else ""
        password = self._credentials.password if self._credentials else ""
        return ("socks5", self.host, self.port, username, password, self.remote_dns, self.proxy_id)

    async def _connect_proxy(self, connect_timeout_s: float) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await open_host_connection(
            self.host, self.port, connect_timeout_s=connect_timeout_s, what="SOCKS5 upstream"
        )

    async def open_tunnel(
        self,
        host: str,
        port: int,
        *,
        connect_timeout_s: float,
        header_timeout_s: float,
        max_header_bytes: int,
    ) -> Tunnel:
        reader, writer = await self._connect_proxy(connect_timeout_s)
        try:
            use_auth = await self._negotiate(reader, writer, header_timeout_s)
            if use_auth:
                await self._authenticate(reader, writer, header_timeout_s)
            address_type, address = await self._encode_address(host, port)
            writer.write(
                bytes([_SOCKS_VERSION, _SOCKS_CMD_CONNECT, 0x00, address_type]) + address + port.to_bytes(2, "big")
            )
            await writer.drain()
            reply = await read_exact(reader, 4, timeout_s=header_timeout_s)
            if reply[0] != _SOCKS_VERSION:
                raise UpstreamProtocolError("malformed SOCKS5 reply from upstream")
            if reply[1] != _SOCKS_REPLY_SUCCESS:
                reason = _SOCKS_REPLY_MESSAGES.get(reply[1], "unknown SOCKS5 error")
                raise UpstreamConnectError(f"upstream SOCKS5 connect failed: {reason}")
            await _discard_socks_address(reader, reply[3], header_timeout_s)
        except TunnelIdleTimeoutError as exc:
            await _safe_close(writer)
            raise UpstreamTimeoutError("SOCKS5 upstream did not answer in time") from exc
        except BaseException:
            await _safe_close(writer)
            raise
        return Tunnel(reader=reader, writer=writer)

    async def open_forward(
        self, host: str, port: int, *, connect_timeout_s: float
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        tunnel = await self.open_tunnel(
            host,
            port,
            connect_timeout_s=connect_timeout_s,
            header_timeout_s=connect_timeout_s * 2,
            max_header_bytes=65536,
        )
        # Forwarding writes the request head immediately; any early bytes would
        # have to be replayed, and SOCKS targets never speak first in practice.
        return tunnel.reader, tunnel.writer

    async def _negotiate(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, timeout_s: float
    ) -> bool:
        """Negotiate an auth method; return True when username/password is required."""
        methods = [_SOCKS_METHOD_NO_AUTH]
        if self._credentials is not None:
            methods.append(_SOCKS_METHOD_USERPASS)
        writer.write(bytes([_SOCKS_VERSION, len(methods), *methods]))
        await writer.drain()
        reply = await read_exact(reader, 2, timeout_s=timeout_s)
        if reply[0] != _SOCKS_VERSION:
            raise UpstreamProtocolError("upstream does not speak SOCKS5")
        method = reply[1]
        if method == _SOCKS_METHOD_NONE:
            raise UpstreamAuthError()
        if method == _SOCKS_METHOD_USERPASS:
            if self._credentials is None:
                raise UpstreamProtocolError("upstream requires SOCKS5 authentication but none is configured")
            return True
        if method == _SOCKS_METHOD_NO_AUTH:
            return False
        raise UpstreamProtocolError("upstream SOCKS5 selected an unsupported auth method")

    async def _authenticate(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, timeout_s: float
    ) -> None:
        if self._credentials is None:  # pragma: no cover - only reached when negotiated
            return
        username, password = self._credentials.socks_pair()
        writer.write(
            bytes([_SOCKS_AUTH_VERSION, len(username)])
            + username
            + bytes([len(password)])
            + password
        )
        await writer.drain()
        reply = await read_exact(reader, 2, timeout_s=timeout_s)
        if reply[0] != _SOCKS_AUTH_VERSION:
            raise UpstreamProtocolError("malformed SOCKS5 authentication reply")
        if reply[1] != 0x00:
            raise UpstreamAuthError()

    async def _encode_address(self, host: str, port: int) -> tuple[int, bytes]:
        try:
            literal = socket.inet_pton(socket.AF_INET, host)
        except OSError:
            literal = b""
        if literal:
            return _SOCKS_ATYP_IPV4, literal
        try:
            literal6 = socket.inet_pton(socket.AF_INET6, host)
        except OSError:
            literal6 = b""
        if literal6:
            return _SOCKS_ATYP_IPV6, literal6
        if self.remote_dns:
            encoded = host.encode("idna")
            if not encoded or len(encoded) > 255:
                raise UpstreamProtocolError("target hostname is not encodable for SOCKS5")
            return _SOCKS_ATYP_DOMAIN, bytes([len(encoded)]) + encoded
        resolved = await _resolve_local(host, port)
        if resolved is None:
            raise UpstreamDnsError("could not resolve the target host for the SOCKS5 upstream")
        family, sockaddr = resolved
        if family == socket.AF_INET6:
            return _SOCKS_ATYP_IPV6, socket.inet_pton(socket.AF_INET6, sockaddr[0])
        return _SOCKS_ATYP_IPV4, socket.inet_pton(socket.AF_INET, sockaddr[0])


async def _resolve_local(host: str, port: int) -> tuple[int, tuple] | None:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    # Prefer IPv4 for the same reason as ``open_host_connection``: a blackholed
    # ::1 loopback must not make an IPv4-only target unreachable.
    ordered = sorted(infos, key=lambda info: 1 if info[0] == socket.AF_INET6 else 0)
    for family, _type, _proto, _canonname, sockaddr in ordered:
        if family in (socket.AF_INET, socket.AF_INET6):
            return family, sockaddr
    return None


async def _discard_socks_address(reader: asyncio.StreamReader, address_type: int, timeout_s: float) -> None:
    if address_type == _SOCKS_ATYP_IPV4:
        await read_exact(reader, 4, timeout_s=timeout_s)
    elif address_type == _SOCKS_ATYP_IPV6:
        await read_exact(reader, 16, timeout_s=timeout_s)
    elif address_type == _SOCKS_ATYP_DOMAIN:
        length = (await read_exact(reader, 1, timeout_s=timeout_s))[0]
        await read_exact(reader, length, timeout_s=timeout_s)
    else:
        raise UpstreamProtocolError("SOCKS5 reply used an unsupported address type")
    await read_exact(reader, 2, timeout_s=timeout_s)  # bound port


def _response_status(head: HttpHead) -> int:
    parts = head.start_line.split(b" ", 2)
    if len(parts) < 2 or not parts[0].upper().startswith(b"HTTP/"):
        raise UpstreamProtocolError("upstream sent a malformed HTTP response")
    try:
        return int(parts[1])
    except ValueError as exc:
        raise UpstreamProtocolError("upstream sent a malformed HTTP status") from exc


async def _safe_close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except (OSError, ConnectionError, RuntimeError):
        pass


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def build_tls_context(verify: bool) -> ssl.SSLContext:
    """Return the TLS context used for HTTPS upstreams."""
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def parse_upstream(
    proxy: ProxyConfig | None,
    *,
    remote_dns: bool = True,
    tls_verify: bool = True,
    tls_context: ssl.SSLContext | None = None,
) -> Upstream:
    """Build the :class:`Upstream` described by *proxy* (``None`` means direct)."""
    if proxy is None:
        return DirectUpstream()
    host = (proxy.host or "").strip()
    if not host:
        raise ValueError(f"proxy {proxy.id!r} has an empty host")
    if "@" in host or "/" in host or any(char.isspace() for char in host):
        raise ValueError(f"proxy {proxy.id!r} has an invalid host")
    if any(ord(char) < 0x20 for char in host):
        raise ValueError(f"proxy {proxy.id!r} has an invalid host")
    if proxy.scheme is ProxyScheme.SOCKS5:
        remote = remote_dns and "socks5-local-dns" not in proxy.tags
        return Socks5Upstream(
            host,
            proxy.port,
            credentials=credentials_from_proxy(proxy),
            remote_dns=remote,
            proxy_id=proxy.id,
        )
    if proxy.scheme in (ProxyScheme.HTTP, ProxyScheme.HTTPS):
        scheme = proxy.scheme.value
        context = None
        if scheme == "https":
            context = tls_context or build_tls_context(tls_verify)
        return HttpUpstream(
            host,
            proxy.port,
            scheme=scheme,
            credentials=credentials_from_proxy(proxy),
            tls_context=context,
            proxy_id=proxy.id,
        )
    raise ValueError(f"unsupported proxy scheme {proxy.scheme!r}")  # pragma: no cover - enum is exhaustive


def upstream_mode(upstream: Upstream) -> str:
    """Return the ``BridgeConfig.mode`` string for an upstream."""
    return "direct" if isinstance(upstream, DirectUpstream) else upstream.mode


__all__ = [
    "Credentials",
    "DirectUpstream",
    "HttpUpstream",
    "Socks5Upstream",
    "Tunnel",
    "Upstream",
    "UpstreamAuthError",
    "UpstreamConnectError",
    "UpstreamDnsError",
    "UpstreamError",
    "UpstreamProtocolError",
    "UpstreamTimeoutError",
    "build_tls_context",
    "credentials_from_proxy",
    "parse_upstream",
    "upstream_mode",
]
