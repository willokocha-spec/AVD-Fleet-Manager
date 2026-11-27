"""HTTP listener wire-protocol tests: CONNECT tunnels and absolute-URI forwarding."""

from __future__ import annotations

import socket
from pathlib import Path

from test_bridge import (
    TIMEOUT,
    EchoServer,
    HttpProxyDouble,
    HttpsProxyDouble,
    OriginServer,
    SilentServer,
    _recv_all,
    bridge_manager,
    connect_via_bridge,
    echo_roundtrip,
    free_bridge_start,
    generate_self_signed_cert,
    http_get_via_bridge,
    make_http_proxy,
    make_https_proxy,
    tunnel_http_get,
)

from avdfleet.proxy.bridge.server import BridgeOptions


def _read_response(sock: socket.socket) -> tuple[int, bytes, dict[str, str]]:
    data = _recv_all(sock)
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1", "replace").split("\r\n")
    status = int(lines[0].split(" ")[1]) if len(lines) and len(lines[0].split(" ")) > 1 else 0
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name:
            headers[name.strip().lower()] = value.strip()
    return status, body, headers


def test_connect_tunnel_through_authenticated_http_upstream() -> None:
    with HttpProxyDouble() as upstream, OriginServer() as origin, bridge_manager() as bridge:
        config = bridge.ensure("avd-01", make_http_proxy(upstream.port))
        status, tunnel = connect_via_bridge(config.listen_port, "127.0.0.1", origin.port)
        assert status == 200
        try:
            http_status, body = tunnel_http_get(tunnel, "/via-connect")
            assert http_status == 200
            assert b"origin-double" in body
        finally:
            tunnel.close()

        records = upstream.requests
        assert len(records) == 1
        assert records[0]["method"] == "CONNECT"
        assert records[0]["target"] == f"127.0.0.1:{origin.port}"
        assert records[0]["auth_ok"] is True
        assert records[0]["headers"]["proxy-authorization"].startswith("Basic ")


def test_absolute_uri_forwarding_uses_upstream_auth() -> None:
    with HttpProxyDouble() as upstream, OriginServer() as origin, bridge_manager() as bridge:
        config = bridge.ensure("avd-01", make_http_proxy(upstream.port))
        url = origin.url("/absolute")
        status, body = http_get_via_bridge(config.listen_port, url)
        assert status == 200
        assert body

        records = upstream.requests
        assert len(records) == 1
        assert records[0]["method"] == "GET"
        assert records[0]["target"] == url
        assert records[0]["auth_ok"] is True
        assert origin.requests[0]["path"] == "/absolute"


def test_origin_form_request_is_forwarded_through_upstream() -> None:
    with HttpProxyDouble() as upstream, OriginServer() as origin, bridge_manager() as bridge:
        config = bridge.ensure("avd-01", make_http_proxy(upstream.port))
        conn = socket.create_connection(("127.0.0.1", config.listen_port), timeout=TIMEOUT)
        try:
            conn.sendall(
                f"GET /origin-form HTTP/1.1\r\nHost: 127.0.0.1:{origin.port}\r\n"
                "Connection: close\r\n\r\n".encode()
            )
            status, body, _headers = _read_response(conn)
        finally:
            conn.close()
        assert status == 200
        assert origin.requests[0]["path"] == "/origin-form"
        assert (
            upstream.requests[0]["target"] == f"http://127.0.0.1:{origin.port}/origin-form"
        )


def test_wrong_upstream_credentials_rejected_on_connect() -> None:
    with HttpProxyDouble() as upstream, OriginServer() as origin, bridge_manager() as bridge:
        config = bridge.ensure(
            "avd-01", make_http_proxy(upstream.port, password="wrong-password")
        )
        status, tunnel = connect_via_bridge(config.listen_port, "127.0.0.1", origin.port)
        tunnel.close()
        assert status == 502
        records = upstream.requests
        assert records and records[0]["auth_ok"] is False


def test_wrong_upstream_credentials_rejected_on_absolute_forwarding() -> None:
    with HttpProxyDouble() as upstream, OriginServer() as origin, bridge_manager() as bridge:
        config = bridge.ensure(
            "avd-01", make_http_proxy(upstream.port, password="wrong-password")
        )
        status, body = http_get_via_bridge(config.listen_port, origin.url("/denied"))
        assert status == 502
        assert b"authentication" in body.lower()
        assert origin.requests == []


def test_connect_tunnel_is_bidirectional() -> None:
    with EchoServer() as echo, bridge_manager() as bridge:
        config = bridge.ensure("avd-01", None)  # direct mode
        status, tunnel = connect_via_bridge(config.listen_port, "127.0.0.1", echo.port)
        assert status == 200
        try:
            assert echo_roundtrip(tunnel, b"ping-through-tunnel") == b"ping-through-tunnel"
            assert echo_roundtrip(tunnel, bytes(range(256))) == bytes(range(256))
        finally:
            tunnel.close()
        assert echo.records


def test_tunnel_relays_large_payload_with_backpressure() -> None:
    payload = bytes(range(256)) * 4096  # 1 MiB, well past the 64 KiB relay chunk
    with EchoServer() as echo, bridge_manager() as bridge:
        config = bridge.ensure("avd-01", None)
        status, tunnel = connect_via_bridge(config.listen_port, "127.0.0.1", echo.port)
        assert status == 200
        try:
            assert echo_roundtrip(tunnel, payload) == payload
        finally:
            tunnel.close()


def test_direct_mode_connects_without_upstream() -> None:
    with EchoServer() as echo, OriginServer() as origin, bridge_manager() as bridge:
        config = bridge.ensure("avd-direct", None)
        assert config.mode == "direct"
        status, tunnel = connect_via_bridge(config.listen_port, "127.0.0.1", echo.port)
        assert status == 200
        try:
            assert echo_roundtrip(tunnel, b"direct-echo") == b"direct-echo"
        finally:
            tunnel.close()

        status, body = http_get_via_bridge(config.listen_port, origin.url("/direct-forward"))
        assert status == 200
        assert origin.requests[0]["path"] == "/direct-forward"


def test_header_limit_returns_431() -> None:
    with bridge_manager(max_header_bytes=2048) as bridge:
        config = bridge.ensure("avd-01", None)
        status, tunnel = connect_via_bridge(
            config.listen_port,
            "127.0.0.1",
            9,
            extra_headers={"X-Oversized": "A" * 4096},
        )
        tunnel.close()
        assert status == 431


def test_silent_upstream_times_out_with_504() -> None:
    with (
        SilentServer() as silent,
        OriginServer() as origin,
        bridge_manager(header_timeout_s=0.4, connect_timeout_s=2.0) as bridge,
    ):
        config = bridge.ensure("avd-01", make_http_proxy(silent.port))
        status, tunnel = connect_via_bridge(config.listen_port, "127.0.0.1", origin.port)
        tunnel.close()
        # 504 (not 502) proves the bridge connected to the upstream and then
        # timed out waiting for the CONNECT response head.
        assert status == 504


def test_chunked_request_body_is_rejected_with_501() -> None:
    with OriginServer() as origin, bridge_manager() as bridge:
        config = bridge.ensure("avd-01", None)
        conn = socket.create_connection(("127.0.0.1", config.listen_port), timeout=TIMEOUT)
        try:
            conn.sendall(
                (
                    f"POST http://127.0.0.1:{origin.port}/upload HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{origin.port}\r\n"
                    "Transfer-Encoding: chunked\r\n"
                    "Connection: close\r\n\r\n"
                    "5\r\nhello\r\n0\r\n\r\n"
                ).encode()
            )
            status, _body, headers = _read_response(conn)
        finally:
            conn.close()
        assert status == 501
        assert headers["connection"] == "close"
        assert origin.requests == []


def test_connect_to_refused_target_is_502() -> None:
    with bridge_manager() as bridge:
        config = bridge.ensure("avd-01", None)
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
        probe.close()
        status, tunnel = connect_via_bridge(config.listen_port, "127.0.0.1", closed_port)
        tunnel.close()
        assert status == 502


def test_https_absolute_form_is_rejected_with_hint() -> None:
    with bridge_manager() as bridge:
        config = bridge.ensure("avd-01", None)
        status, body = http_get_via_bridge(config.listen_port, "https://example.invalid/")
        assert status == 400
        assert b"CONNECT" in body


def test_malformed_request_line_is_400() -> None:
    with bridge_manager() as bridge:
        config = bridge.ensure("avd-01", None)
        conn = socket.create_connection(("127.0.0.1", config.listen_port), timeout=TIMEOUT)
        try:
            conn.sendall(b"NOT-HTTP\r\n\r\n")
            status, _body, _headers = _read_response(conn)
        finally:
            conn.close()
        assert status == 400


def test_disabled_socks_listener_has_no_second_port() -> None:
    options = BridgeOptions(
        port_start=free_bridge_start(),
        enable_socks=False,
        connect_timeout_s=3.0,
        header_timeout_s=2.0,
        idle_timeout_s=5.0,
        shutdown_grace_s=0.2,
    )
    from avdfleet.proxy.bridge import BridgeManager

    manager = BridgeManager(options=options)
    try:
        config = manager.ensure("avd-01", None)
        assert config.listen_port > 0
        assert manager.socks_port("avd-01") is None
        assert manager.socks_url("avd-01") is None
    finally:
        manager.close()


# ---------------------------------------------------------------------------
# HTTPS upstream (TLS hop between bridge and proxy)
# ---------------------------------------------------------------------------


def test_connect_tunnel_through_https_upstream(tmp_path: Path) -> None:
    cert, key = generate_self_signed_cert(tmp_path)
    with (
        HttpsProxyDouble(cert, key) as upstream,
        OriginServer() as origin,
        bridge_manager(tls_verify=False) as bridge,
    ):
        config = bridge.ensure("avd-01", make_https_proxy(upstream.port))
        assert config.upstream == f"https://127.0.0.1:{upstream.port}"
        assert config.mode == "http"  # BridgeConfig.mode is the upstream family

        status, tunnel = connect_via_bridge(config.listen_port, "127.0.0.1", origin.port)
        assert status == 200
        try:
            http_status, body = tunnel_http_get(tunnel, "/via-https-upstream")
            assert http_status == 200
            assert b"origin-double" in body
        finally:
            tunnel.close()

        records = upstream.requests
        assert records[0]["method"] == "CONNECT"
        assert records[0]["auth_ok"] is True


def test_absolute_forwarding_through_https_upstream(tmp_path: Path) -> None:
    cert, key = generate_self_signed_cert(tmp_path)
    with (
        HttpsProxyDouble(cert, key) as upstream,
        OriginServer() as origin,
        bridge_manager(tls_verify=False) as bridge,
    ):
        config = bridge.ensure("avd-01", make_https_proxy(upstream.port))
        status, body = http_get_via_bridge(config.listen_port, origin.url("/https-abs"))
        assert status == 200
        assert origin.requests[0]["path"] == "/https-abs"
        assert upstream.requests[0]["target"] == origin.url("/https-abs")


def test_https_upstream_certificate_is_verified_by_default(tmp_path: Path) -> None:
    cert, key = generate_self_signed_cert(tmp_path)
    with (
        HttpsProxyDouble(cert, key) as upstream,
        OriginServer() as origin,
        bridge_manager() as bridge,  # tls_verify=True default
    ):
        config = bridge.ensure("avd-01", make_https_proxy(upstream.port))
        status, tunnel = connect_via_bridge(config.listen_port, "127.0.0.1", origin.port)
        tunnel.close()
        assert status == 502
        assert origin.requests == []

