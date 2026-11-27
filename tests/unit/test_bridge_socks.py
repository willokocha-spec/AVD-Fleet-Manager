"""SOCKS5 listener and SOCKS5 upstream tests (stdlib only, loopback only)."""

from __future__ import annotations

import socket

from test_bridge import (
    TIMEOUT,
    EchoServer,
    HttpProxyDouble,
    OriginServer,
    Socks5Double,
    _recv_all,
    bridge_manager,
    connect_via_bridge,
    echo_roundtrip,
    make_http_proxy,
    make_socks_proxy,
    socks5_client_connect,
    tunnel_http_get,
)


def test_socks5_listener_forwards_through_authenticated_socks5_upstream() -> None:
    with (
        Socks5Double(username="user", password="pass") as upstream,
        EchoServer() as echo,
        bridge_manager() as bridge,
    ):
        bridge.ensure("avd-01", make_socks_proxy(upstream.port))
        socks_port = bridge.socks_port("avd-01")
        assert socks_port is not None

        code, conn = socks5_client_connect(
            socks_port,
            "localhost",
            echo.port,
            username="user",
            password="pass",
        )
        assert code == 0
        assert conn is not None
        try:
            assert echo_roundtrip(conn, b"socks-listener-to-socks-upstream") == (
                b"socks-listener-to-socks-upstream"
            )
        finally:
            conn.close()

        records = upstream.requests
        assert len(records) == 1
        # Default policy is SOCKS5h: the guest hostname travels to the proxy.
        assert records[0]["atyp"] == 0x03
        assert records[0]["host"] == "localhost"
        assert records[0]["auth_user"] == "user"


def test_socks5_upstream_rejects_wrong_credentials() -> None:
    with (
        Socks5Double(username="user", password="pass") as upstream,
        EchoServer() as echo,
        bridge_manager() as bridge,
    ):
        config = bridge.ensure(
            "avd-01", make_socks_proxy(upstream.port, password="wrong-password")
        )
        socks_port = bridge.socks_port("avd-01")
        assert socks_port is not None

        code, conn = socks5_client_connect(socks_port, "127.0.0.1", echo.port)
        assert code == 0x02  # "connection not allowed by ruleset" (auth failure)
        assert conn is None

        # The HTTP listener maps the same upstream failure to 502.
        status, tunnel = connect_via_bridge(config.listen_port, "127.0.0.1", echo.port)
        tunnel.close()
        assert status == 502


def test_socks5_upstream_local_dns_is_opt_in() -> None:
    with (
        Socks5Double() as upstream,
        EchoServer() as echo,
        bridge_manager(remote_dns=False) as bridge,
    ):
        bridge.ensure("avd-01", make_socks_proxy(upstream.port))
        socks_port = bridge.socks_port("avd-01")
        assert socks_port is not None

        code, conn = socks5_client_connect(
            socks_port, "localhost", echo.port, username="user", password="pass"
        )
        assert code == 0
        assert conn is not None
        try:
            assert echo_roundtrip(conn, b"local-dns") == b"local-dns"
        finally:
            conn.close()

        records = upstream.requests
        assert records[0]["atyp"] == 0x01
        assert records[0]["host"] == "127.0.0.1"


def test_socks5_upstream_without_auth_negotiates_no_auth() -> None:
    with Socks5Double(username=None) as upstream, EchoServer() as echo, bridge_manager() as bridge:
        # ProxyConfig carries credentials, but the proxy offers no-auth: the
        # bridge must accept the server's choice instead of failing.
        config = bridge.ensure("avd-01", make_socks_proxy(upstream.port))
        status, tunnel = connect_via_bridge(config.listen_port, "127.0.0.1", echo.port)
        assert status == 200
        try:
            assert echo_roundtrip(tunnel, b"no-auth") == b"no-auth"
        finally:
            tunnel.close()


def test_http_listener_uses_socks5_upstream_with_remote_dns() -> None:
    with Socks5Double() as upstream, EchoServer() as echo, bridge_manager() as bridge:
        config = bridge.ensure("avd-01", make_socks_proxy(upstream.port))
        status, tunnel = connect_via_bridge(config.listen_port, "localhost", echo.port)
        assert status == 200
        try:
            assert echo_roundtrip(tunnel, b"via-http-listener") == b"via-http-listener"
        finally:
            tunnel.close()
        records = upstream.requests
        assert records[0]["atyp"] == 0x03
        assert records[0]["host"] == "localhost"


def test_socks5_listener_direct_mode_connects() -> None:
    with EchoServer() as echo, OriginServer() as origin, bridge_manager() as bridge:
        bridge.ensure("avd-direct", None)
        socks_port = bridge.socks_port("avd-direct")
        assert socks_port is not None

        # domain-name target, resolved locally in direct mode
        code, conn = socks5_client_connect(socks_port, "localhost", echo.port)
        assert code == 0
        assert conn is not None
        try:
            assert echo_roundtrip(conn, b"direct-socks") == b"direct-socks"
        finally:
            conn.close()

        # HTTP through the SOCKS listener to a real origin
        code, conn = socks5_client_connect(socks_port, "127.0.0.1", origin.port)
        assert code == 0
        assert conn is not None
        try:
            status, body = tunnel_http_get(conn, "/via-socks-listener")
        finally:
            conn.close()
        assert status == 200
        assert origin.requests[0]["path"] == "/via-socks-listener"
        assert body


def test_socks5_listener_through_http_upstream() -> None:
    with HttpProxyDouble() as upstream, OriginServer() as origin, bridge_manager() as bridge:
        bridge.ensure("avd-01", make_http_proxy(upstream.port))
        socks_port = bridge.socks_port("avd-01")
        assert socks_port is not None
        code, conn = socks5_client_connect(socks_port, "127.0.0.1", origin.port)
        assert code == 0
        assert conn is not None
        try:
            http_status, body = tunnel_http_get(conn, "/socks-to-http-upstream")
        finally:
            conn.close()
        assert http_status == 200
        assert b"origin-double" in body
        assert upstream.requests[0]["method"] == "CONNECT"
        assert upstream.requests[0]["auth_ok"] is True


def test_socks5_udp_associate_and_bind_are_not_supported() -> None:
    with bridge_manager() as bridge:
        bridge.ensure("avd-01", None)
        socks_port = bridge.socks_port("avd-01")
        assert socks_port is not None
        for command in (0x03, 0x02):
            code, conn = socks5_client_connect(
                socks_port, "127.0.0.1", 9, command=command
            )
            assert code == 0x07
            assert conn is None


def test_socks5_listener_requires_token_when_configured() -> None:
    token = "socks-local-token"
    with EchoServer() as echo, bridge_manager() as bridge:
        config = bridge.ensure("avd-01", None, local_auth_token=token)
        socks_port = bridge.socks_port("avd-01")
        assert socks_port is not None
        assert token not in config.model_dump_json()

        refused, _ = socks5_client_connect(socks_port, "127.0.0.1", echo.port)
        assert refused == 0xFF
        rejected, _ = socks5_client_connect(
            socks_port, "127.0.0.1", echo.port, username="avd-01", password="nope"
        )
        assert rejected == 0xFF
        accepted, conn = socks5_client_connect(
            socks_port, "127.0.0.1", echo.port, username="avd-01", password=token
        )
        assert accepted == 0
        assert conn is not None
        try:
            assert echo_roundtrip(conn, b"authenticated") == b"authenticated"
        finally:
            conn.close()


def test_socks5_refused_target_reply_code() -> None:
    with bridge_manager() as bridge:
        bridge.ensure("avd-01", None)
        socks_port = bridge.socks_port("avd-01")
        assert socks_port is not None
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
        probe.close()
        code, conn = socks5_client_connect(socks_port, "127.0.0.1", closed_port)
        assert code == 0x05  # connection refused
        assert conn is None


def test_socks5_listener_isolated_between_instances() -> None:
    with EchoServer() as echo, OriginServer() as origin, bridge_manager() as bridge:
        bridge.ensure("avd-a", None)
        bridge.ensure("avd-b", None)
        port_a = bridge.socks_port("avd-a")
        port_b = bridge.socks_port("avd-b")
        assert port_a is not None and port_b is not None and port_a != port_b

        code, conn = socks5_client_connect(port_a, "127.0.0.1", origin.port)
        assert code == 0
        assert conn is not None
        try:
            status, _body = tunnel_http_get(conn, "/only-a")
        finally:
            conn.close()
        assert status == 200
        assert len(origin.requests) == 1
        # Instance B's listener is a distinct socket: connecting to B's port
        # works independently and does not share A's upstream state.
        code, conn_b = socks5_client_connect(port_b, "127.0.0.1", echo.port)
        assert code == 0
        assert conn_b is not None
        try:
            assert echo_roundtrip(conn_b, b"b") == b"b"
        finally:
            conn_b.close()


def test_socks5_greeting_rejects_non_socks_client() -> None:
    with EchoServer() as echo, bridge_manager() as bridge:
        bridge.ensure("avd-01", None)
        socks_port = bridge.socks_port("avd-01")
        assert socks_port is not None
        conn = socket.create_connection(("127.0.0.1", socks_port), timeout=TIMEOUT)
        try:
            conn.sendall(b"GET / HTTP/1.1\r\n\r\n")
            _recv_all(conn, timeout=0.5)  # the listener drops the bad handshake
        finally:
            conn.close()
        # The listener must still be healthy after a non-SOCKS client.
        code, healthy = socks5_client_connect(socks_port, "127.0.0.1", echo.port)
        assert code == 0
        assert healthy is not None
        try:
            assert echo_roundtrip(healthy, b"after-garbage") == b"after-garbage"
        finally:
            healthy.close()
