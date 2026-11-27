"""Loopback proxy bridge and upstream clients for per-AVD egress isolation.

One :class:`BridgeManager` owns an HTTP proxy listener (``CONNECT`` tunnels and
absolute-URI forwarding) plus a SOCKS5 listener per AVD instance, all bound to
``127.0.0.1`` on auto-allocated ports.  Each instance forwards through exactly
one authenticated upstream (HTTP/HTTPS Basic auth, SOCKS5 user/pass, or direct)
and the upstream can be hot-swapped without rebinding the listener.

Quick start::

    from avdfleet.proxy.bridge import BridgeManager, emulator_http_proxy_arg

    with BridgeManager(config) as bridge:
        cfg = bridge.ensure("avd-01", proxy_config)
        emulator_args = ["-http-proxy", emulator_http_proxy_arg(cfg)]

Design notes, integration wiring (N1/X1) and limitations (no UDP, DNS policy,
one-request-per-connection forwarding) are documented in
:mod:`avdfleet.proxy.bridge.manager`.
"""

from __future__ import annotations

from .http_proxy import HttpProxyServer
from .manager import (
    DEFAULT_BRIDGE_PORT_START,
    AsyncBridgeManager,
    BridgeManager,
    emulator_http_proxy_arg,
)
from .server import BridgeOptions
from .socks import Socks5ProxyServer
from .upstream import (
    Credentials,
    DirectUpstream,
    HttpUpstream,
    Socks5Upstream,
    Tunnel,
    Upstream,
    UpstreamAuthError,
    UpstreamConnectError,
    UpstreamDnsError,
    UpstreamError,
    UpstreamProtocolError,
    UpstreamTimeoutError,
    parse_upstream,
)

__all__ = [
    "DEFAULT_BRIDGE_PORT_START",
    "AsyncBridgeManager",
    "BridgeManager",
    "BridgeOptions",
    "Credentials",
    "DirectUpstream",
    "HttpProxyServer",
    "HttpUpstream",
    "Socks5ProxyServer",
    "Socks5Upstream",
    "Tunnel",
    "Upstream",
    "UpstreamAuthError",
    "UpstreamConnectError",
    "UpstreamDnsError",
    "UpstreamError",
    "UpstreamProtocolError",
    "UpstreamTimeoutError",
    "emulator_http_proxy_arg",
    "parse_upstream",
]
