"""Client-facing SOCKS5 listener for one AVD instance.

A guest that supports SOCKS5 (or an app/test harness) can use the listener
instead of the HTTP listener; both forward through the *same* per-instance
upstream, so isolation is identical.  Implemented subset:

* ``CONNECT`` (TCP) with ``ATYP`` IPv4 / IPv6 / domain-name targets.
* Optional username/password authentication (RFC 1929); when enabled, the
  username is the instance name and the password is the instance token.
* No auth is required by default: the listener binds loopback only.

Not implemented (answered with the SOCKS5 ``command not supported`` reply):

* ``UDP ASSOCIATE`` - UDP is deliberately not proxied (see bridge package
  docstring); QUIC/DNS-over-UDP traffic is outside the bridge's isolation
  guarantee and must be handled by the emulator network configuration.
* ``BIND``.

Upstream DNS policy is inherited from the configured upstream (SOCKS5 upstream
uses SOCKS5h semantics by default; HTTP CONNECT resolves at the upstream proxy;
direct mode resolves locally).
"""

from __future__ import annotations

import asyncio
import hmac
import socket

from avdfleet.core.logging import get_logger

from .server import (
    AsyncListener,
    BridgeOptions,
    BridgeProtocolError,
    HeadTimeoutError,
    PeerClosedError,
    TunnelIdleTimeoutError,
    close_writer,
    read_exact,
    relay_bidirectional,
    upstream_error_message,
)
from .upstream import UpstreamError

_LOGGER = get_logger("proxy.bridge.socks")

_VERSION = 0x05
_AUTH_VERSION = 0x01
_METHOD_NO_AUTH = 0x00
_METHOD_USERPASS = 0x02
_METHOD_NONE = 0xFF
_CMD_CONNECT = 0x01
_CMD_BIND = 0x02
_CMD_UDP_ASSOCIATE = 0x03
_ATYP_IPV4 = 0x01
_ATYP_DOMAIN = 0x03
_ATYP_IPV6 = 0x04

_REPLY_SUCCESS = 0x00
_REPLY_GENERAL_FAILURE = 0x01
_REPLY_COMMAND_NOT_SUPPORTED = 0x07
_REPLY_ADDRESS_TYPE_NOT_SUPPORTED = 0x08


class Socks5ProxyServer:
    """One instance-scoped SOCKS5 listener."""

    def __init__(
        self,
        instance: str,
        upstream_getter: object,
        options: BridgeOptions,
        *,
        local_auth_token: str | None = None,
    ) -> None:
        self._instance = instance
        self._get_upstream = upstream_getter
        self._options = options
        self._local_auth_token = local_auth_token
        self._listener = AsyncListener(
            self._handle,
            host=options.listen_host,
            backlog=options.backlog,
            max_connections=options.max_connections,
            name=f"socks5-bridge[{instance}]",
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

    # -- per connection --------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._handshake(reader, writer)
        except (HeadTimeoutError, TimeoutError):
            return
        except (PeerClosedError, BridgeProtocolError, OSError, ConnectionError):
            return

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        header = await read_exact(reader, 2, timeout_s=self._options.header_timeout_s)
        if header[0] != _VERSION:
            return
        method_count = header[1]
        methods = await read_exact(reader, method_count, timeout_s=self._options.header_timeout_s)
        selected = self._select_method(methods)
        writer.write(bytes([_VERSION, selected]))
        await writer.drain()
        if selected == _METHOD_NONE:
            return
        if selected == _METHOD_USERPASS and not await self._authenticate(reader, writer):
            return
        await self._read_request(reader, writer)

    def _select_method(self, methods: bytes) -> int:
        offered = set(methods)
        if self._local_auth_token is not None:
            return _METHOD_USERPASS if _METHOD_USERPASS in offered else _METHOD_NONE
        return _METHOD_NO_AUTH if _METHOD_NO_AUTH in offered else _METHOD_NONE

    async def _authenticate(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        try:
            version = (await read_exact(reader, 1, timeout_s=self._options.header_timeout_s))[0]
            if version != _AUTH_VERSION:
                return False
            username_length = (await read_exact(reader, 1, timeout_s=self._options.header_timeout_s))[0]
            username = await read_exact(reader, username_length, timeout_s=self._options.header_timeout_s)
            password_length = (await read_exact(reader, 1, timeout_s=self._options.header_timeout_s))[0]
            password = await read_exact(reader, password_length, timeout_s=self._options.header_timeout_s)
        except (PeerClosedError, HeadTimeoutError):
            return False
        token = self._local_auth_token or ""
        accepted = hmac.compare_digest(username.decode("utf-8", "replace"), self._instance) and hmac.compare_digest(
            password.decode("utf-8", "replace"), token
        )
        writer.write(bytes([_AUTH_VERSION, 0x00 if accepted else 0x01]))
        await writer.drain()
        return accepted

    async def _read_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        header = await read_exact(reader, 4, timeout_s=self._options.header_timeout_s)
        if header[0] != _VERSION:
            await self._reply(writer, _REPLY_GENERAL_FAILURE)
            return
        command = header[1]
        address_type = header[3]
        if command in {_CMD_BIND, _CMD_UDP_ASSOCIATE}:
            await self._reply(writer, _REPLY_COMMAND_NOT_SUPPORTED)
            return
        if command != _CMD_CONNECT:
            await self._reply(writer, _REPLY_COMMAND_NOT_SUPPORTED)
            return
        try:
            host = await self._read_address(reader, address_type)
        except BridgeProtocolError:
            await self._reply(writer, _REPLY_ADDRESS_TYPE_NOT_SUPPORTED)
            return
        except (PeerClosedError, HeadTimeoutError):
            return
        port_bytes = await read_exact(reader, 2, timeout_s=self._options.header_timeout_s)
        port = int.from_bytes(port_bytes, "big")
        if not host or not 1 <= port <= 65535:
            await self._reply(writer, _REPLY_GENERAL_FAILURE)
            return

        upstream = self._get_upstream()  # type: ignore[operator]
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
                "bridge SOCKS5 connect failed instance=%s upstream=%s reason=%s",
                self._instance,
                upstream.display or "direct",
                upstream_error_message(exc),
            )
            await self._reply(writer, exc.socks_reply)
            return
        except TimeoutError:
            await self._reply(writer, 0x04)  # host unreachable
            return
        except BridgeProtocolError:
            await self._reply(writer, _REPLY_GENERAL_FAILURE)
            return
        except (OSError, ConnectionError):
            await self._reply(writer, 0x05)  # connection refused
            return

        try:
            await self._reply(writer, _REPLY_SUCCESS)
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
        except TunnelIdleTimeoutError:
            pass
        finally:
            await close_writer(tunnel.writer)

    async def _read_address(self, reader: asyncio.StreamReader, address_type: int) -> str:
        if address_type == _ATYP_IPV4:
            raw = await read_exact(reader, 4, timeout_s=self._options.header_timeout_s)
            return socket.inet_ntop(socket.AF_INET, raw)
        if address_type == _ATYP_IPV6:
            raw = await read_exact(reader, 16, timeout_s=self._options.header_timeout_s)
            return socket.inet_ntop(socket.AF_INET6, raw)
        if address_type == _ATYP_DOMAIN:
            length = (await read_exact(reader, 1, timeout_s=self._options.header_timeout_s))[0]
            raw = await read_exact(reader, length, timeout_s=self._options.header_timeout_s)
            return raw.decode("idna")
        raise BridgeProtocolError("unsupported SOCKS5 address type")

    async def _reply(self, writer: asyncio.StreamWriter, code: int) -> None:
        writer.write(bytes([_VERSION, code, 0x00, _ATYP_IPV4, 0, 0, 0, 0, 0, 0]))
        try:
            await writer.drain()
        except (OSError, ConnectionError):
            pass


__all__ = ["Socks5ProxyServer"]
