"""Local proxy bridge tests plus the shared stdlib network doubles.

Everything here is stdlib only and binds loopback ephemeral ports - no external
network is used.  The task's write scope does not include a conftest/helper
module, so the threaded doubles used by the sibling bridge and verifier tests
live in this first-collected module and are imported as::

    from test_bridge import HttpProxyDouble, OriginServer, ...

pytest's default ``prepend`` import mode puts ``tests/unit`` on ``sys.path`` for
these modules, so the sibling imports resolve in a full run and in single-file
runs alike.  Each double is a real TCP server in a daemon thread, so the tests
exercise the actual wire protocol (CONNECT, absolute-URI, SOCKS5 handshakes).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import socket
import socketserver
import ssl
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from avdfleet.core.errors import ProxyError, ValidationError
from avdfleet.core.interfaces import ProxyBridge
from avdfleet.core.models import ProxyConfig
from avdfleet.core.ports import PortAllocator, find_free_port, is_port_free
from avdfleet.proxy.bridge import (
    BridgeManager,
    emulator_http_proxy_arg,
    parse_upstream,
)
from avdfleet.proxy.bridge.manager import AsyncBridgeManager
from avdfleet.proxy.bridge.server import BridgeOptions

TIMEOUT = 5.0


def generate_self_signed_cert(tmp_path: Path) -> tuple[Path, Path]:
    """Create a self-signed cert for 127.0.0.1 (TLS tests only, never trusted)."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:  # pragma: no cover - cryptography is a runtime dependency
        pytest.skip("cryptography is not available")
    import datetime
    import ipaddress

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "test-cert.pem"
    key_path = tmp_path / "test-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def server_ssl_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def free_bridge_start() -> int:
    """A free port to start the bridge allocator scan from."""
    port = find_free_port(24000, 60000)
    assert port is not None
    return port


def _recv_exact(conn: socket.socket, size: int) -> bytes:
    buffer = bytearray()
    while len(buffer) < size:
        chunk = conn.recv(size - len(buffer))
        if not chunk:
            return b""
        buffer.extend(chunk)
    return bytes(buffer)


def _recv_head(conn: socket.socket, *, limit: int = 65536) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > limit:
            break
    return bytes(data)


def _recv_all(conn: socket.socket, *, timeout: float = TIMEOUT) -> bytes:
    conn.settimeout(timeout)
    data = bytearray()
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data.extend(chunk)
    except TimeoutError:
        pass
    return bytes(data)


def _parse_head(head: bytes) -> tuple[str, str, dict[str, str]]:
    lines = head.decode("latin-1", "replace").split("\r\n")
    request_line = lines[0] if lines else ""
    parts = request_line.split(" ")
    method = parts[0] if parts else ""
    target = parts[1] if len(parts) > 1 else ""
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name:
            headers[name.strip().lower()] = value.strip()
    return method, target, headers


def _send_response(
    conn: socket.socket,
    status: int = 200,
    body: bytes = b"",
    *,
    headers: tuple[tuple[str, str], ...] = (),
) -> None:
    reasons = {200: "OK", 407: "Proxy Authentication Required", 502: "Bad Gateway"}
    lines = [
        f"HTTP/1.1 {status} {reasons.get(status, 'Error')}",
        "Content-Type: application/json",
        f"Content-Length: {len(body)}",
        "Connection: close",
    ]
    lines.extend(f"{name}: {value}" for name, value in headers)
    with contextlib.suppress(OSError):
        conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    with contextlib.suppress(OSError):
        dst.shutdown(socket.SHUT_WR)


def _relay(left: socket.socket, right: socket.socket) -> None:
    thread = threading.Thread(target=_pump, args=(right, left), daemon=True)
    thread.start()
    _pump(left, right)
    thread.join(timeout=TIMEOUT)


def _connect_target(host: str, port: int) -> socket.socket:
    """Connect to a target, preferring IPv4 (this host blackholes ``::1``)."""
    try:
        socket.inet_pton(socket.AF_INET, host)
    except OSError:
        pass
    else:
        return socket.create_connection((host, port), timeout=TIMEOUT)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OSError("target name resolution failed") from exc
    ordered = sorted(infos, key=lambda info: 1 if info[0] == socket.AF_INET6 else 0)
    last_error: OSError | None = None
    for family, sock_type, proto, _canonname, sockaddr in ordered:
        candidate = socket.socket(family, sock_type, proto)
        try:
            candidate.settimeout(TIMEOUT)
            candidate.connect(sockaddr)
            return candidate
        except OSError as exc:
            last_error = exc
            candidate.close()
    raise last_error or OSError("could not connect to the target")


def _status_code(data: bytes) -> int:
    first_line = data.split(b"\r\n", 1)[0]
    parts = first_line.split(b" ")
    if len(parts) < 2:
        return 0
    try:
        return int(parts[1])
    except ValueError:
        return 0


def _response_body(data: bytes) -> bytes:
    return data.partition(b"\r\n\r\n")[2]


# ---------------------------------------------------------------------------
# Threaded test servers
# ---------------------------------------------------------------------------


class _ThreadedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        handler_cls: type[socketserver.BaseRequestHandler],
        *,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        super().__init__(("127.0.0.1", 0), handler_cls)
        self.records: list[dict[str, object]] = []
        self.records_lock = threading.Lock()
        self._ssl_context = ssl_context
        self._thread: threading.Thread | None = None

    def get_request(self) -> tuple[socket.socket, object]:
        sock, address = super().get_request()
        if self._ssl_context is not None:
            sock = self._ssl_context.wrap_socket(sock, server_side=True)
        return sock, address

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def record(self, item: dict[str, object]) -> None:
        with self.records_lock:
            self.records.append(item)

    def snapshot(self) -> list[dict[str, object]]:
        with self.records_lock:
            return [dict(item) for item in self.records]

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self.shutdown()
        with contextlib.suppress(Exception):
            self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=TIMEOUT)


class _BaseHandler(socketserver.BaseRequestHandler):
    server: _ThreadedServer


class _OriginHandler(_BaseHandler):
    def handle(self) -> None:
        head = _recv_head(self.request)
        if not head:
            return
        method, target, headers = _parse_head(head)
        parsed = urlsplit(target)
        path = parsed.path or "/"
        self.server.record(
            {"method": method, "target": target, "path": path, "headers": headers}
        )
        if path == "/notfound":
            _send_response(self.request, 404, b'{"error": "not found"}')
            return
        body = json.dumps(
            {"ip": "203.0.113.7", "path": path, "origin": "origin-double", "method": method}
        ).encode()
        _send_response(self.request, 200, body)


class _EchoHandler(_BaseHandler):
    def handle(self) -> None:
        while True:
            try:
                data = self.request.recv(65536)
            except OSError:
                return
            if not data:
                return
            self.server.record({"echo_bytes": len(data)})
            try:
                self.request.sendall(data)
            except OSError:
                return


class _SilentHandler(_BaseHandler):
    def handle(self) -> None:
        self.server.record({"silent": True})  # type: ignore[attr-defined]
        while not self.server.stopping.wait(0.02):  # type: ignore[attr-defined]
            pass


class _HttpProxyHandler(_BaseHandler):
    def handle(self) -> None:
        conn = self.request
        head = _recv_head(conn)
        if not head:
            return
        method, target, headers = _parse_head(head)
        required: str | None = self.server.required_auth  # type: ignore[attr-defined]
        auth = headers.get("proxy-authorization", "")
        auth_ok = required is None or auth == required
        self.server.record(
            {"method": method, "target": target, "auth_ok": auth_ok, "auth": auth, "headers": headers}
        )
        if not auth_ok:
            _send_response(
                conn,
                407,
                b"{}",
                headers=(("Proxy-Authenticate", 'Basic realm="test-upstream"'),),
            )
            return
        if method == "CONNECT":
            host, _, port_text = target.rpartition(":")
            origin: socket.socket | None = None
            try:
                origin = _connect_target(host.strip("[]"), int(port_text))
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            except (OSError, ValueError):
                _send_response(conn, 502, b"{}")
                return
            try:
                _relay(conn, origin)
            finally:
                with contextlib.suppress(OSError):
                    origin.close()
            return

        try:
            parsed = urlsplit(target)
            host = parsed.hostname
            port = parsed.port or 80
            path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
            if not host:
                _send_response(conn, 502, b"{}")
                return
            origin = _connect_target(host, port)
        except (OSError, ValueError):
            _send_response(conn, 502, b"{}")
            return
        try:
            forwarded = (
                f"{method} {path} HTTP/1.1\r\nHost: {parsed.netloc}\r\nConnection: close\r\n\r\n"
            ).encode("latin-1")
            origin.sendall(forwarded)
            data = _recv_all(origin)
            conn.sendall(data)
        finally:
            with contextlib.suppress(OSError):
                origin.close()


class _Socks5Handler(_BaseHandler):
    def handle(self) -> None:
        conn = self.request
        header = _recv_exact(conn, 2)
        if len(header) < 2:
            return
        methods = _recv_exact(conn, header[1])
        credentials: tuple[str, str] | None = self.server.credentials  # type: ignore[attr-defined]
        candidate = 0x00 if credentials is None else 0x02
        chosen = candidate if candidate in methods else 0xFF
        conn.sendall(bytes([0x05, chosen]))
        if chosen == 0xFF:
            return
        username = ""
        if chosen == 0x02:
            auth_header = _recv_exact(conn, 2)
            username_bytes = _recv_exact(conn, auth_header[1])
            password_header = _recv_exact(conn, 1)
            password_bytes = _recv_exact(conn, password_header[0])
            username = username_bytes.decode("utf-8", "replace")
            accepted = (username, password_bytes.decode("utf-8", "replace")) == credentials
            conn.sendall(bytes([0x01, 0x00 if accepted else 0x01]))
            if not accepted:
                return
        request = _recv_exact(conn, 4)
        if len(request) < 4:
            return
        command, address_type = request[1], request[3]
        if address_type == 0x01:
            host = socket.inet_ntoa(_recv_exact(conn, 4))
        elif address_type == 0x03:
            host = _recv_exact(conn, _recv_exact(conn, 1)[0]).decode("utf-8", "replace")
        elif address_type == 0x04:
            host = socket.inet_ntop(socket.AF_INET6, _recv_exact(conn, 16))
        else:
            conn.sendall(bytes([0x05, 0x08, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
            return
        port = int.from_bytes(_recv_exact(conn, 2), "big")
        self.server.record(
            {"atyp": address_type, "host": host, "port": port, "auth_user": username, "cmd": command}
        )
        if command != 0x01:
            conn.sendall(bytes([0x05, 0x07, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
            return
        try:
            upstream = _connect_target(host, port)
        except OSError:
            conn.sendall(bytes([0x05, 0x05, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
            return
        try:
            conn.sendall(bytes([0x05, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
            _relay(conn, upstream)
        finally:
            with contextlib.suppress(OSError):
                upstream.close()


# ---------------------------------------------------------------------------
# Public doubles used by the sibling test modules
# ---------------------------------------------------------------------------


class OriginServer:
    """An HTTP(S) origin that reports its own view and the client's egress IP."""

    def __init__(self, *, ssl_context: ssl.SSLContext | None = None) -> None:
        self._server = _ThreadedServer(_OriginHandler, ssl_context=ssl_context)
        self._scheme = "https" if ssl_context is not None else "http"

    @property
    def port(self) -> int:
        return self._server.port

    def url(self, path: str = "/") -> str:
        return f"{self._scheme}://127.0.0.1:{self.port}{path}"

    @property
    def requests(self) -> list[dict[str, object]]:
        return self._server.snapshot()

    def start(self) -> OriginServer:
        self._server.start()
        return self

    def stop(self) -> None:
        self._server.stop()

    def __enter__(self) -> OriginServer:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


class EchoServer:
    """A TCP echo server, used to prove tunnels are bidirectional."""

    def __init__(self) -> None:
        self._server = _ThreadedServer(_EchoHandler)

    @property
    def port(self) -> int:
        return self._server.port

    @property
    def records(self) -> list[dict[str, object]]:
        return self._server.snapshot()

    def start(self) -> EchoServer:
        self._server.start()
        return self

    def stop(self) -> None:
        self._server.stop()

    def __enter__(self) -> EchoServer:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


class SilentServer:
    """Accepts TCP connections and never answers (upstream timeout tests)."""

    def __init__(self) -> None:
        self._server = _ThreadedServer(_SilentHandler)
        self._server.stopping = threading.Event()

    @property
    def port(self) -> int:
        return self._server.port

    @property
    def connections(self) -> int:
        return len(self._server.snapshot())

    def start(self) -> SilentServer:
        self._server.start()
        return self

    def stop(self) -> None:
        self._server.stopping.set()  # type: ignore[attr-defined]
        self._server.stop()

    def __enter__(self) -> SilentServer:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


class HttpProxyDouble:
    """A tiny HTTP/HTTPS forward proxy with optional Basic auth."""

    def __init__(
        self,
        *,
        username: str | None = "user",
        password: str | None = "pass",
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._server = _ThreadedServer(_HttpProxyHandler, ssl_context=ssl_context)
        self._server.required_auth = (
            "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode("latin-1")
            if username is not None
            else None
        )

    @property
    def port(self) -> int:
        return self._server.port

    @property
    def requests(self) -> list[dict[str, object]]:
        return self._server.snapshot()

    def start(self) -> HttpProxyDouble:
        self._server.start()
        return self

    def stop(self) -> None:
        self._server.stop()

    def __enter__(self) -> HttpProxyDouble:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


class HttpsProxyDouble(HttpProxyDouble):
    """An HTTP forward proxy whose hop from the bridge is TLS-wrapped."""

    def __init__(
        self,
        cert_path: Path,
        key_path: Path,
        *,
        username: str | None = "user",
        password: str | None = "pass",
    ) -> None:
        super().__init__(
            username=username,
            password=password,
            ssl_context=server_ssl_context(cert_path, key_path),
        )


class TlsOriginServer(OriginServer):
    """An HTTPS origin with a self-signed certificate (verifier TLS tests)."""

    def __init__(self, cert_path: Path, key_path: Path) -> None:
        super().__init__(ssl_context=server_ssl_context(cert_path, key_path))


class Socks5Double:
    """A tiny SOCKS5 proxy with optional username/password auth."""

    def __init__(
        self, *, username: str | None = "user", password: str | None = "pass"
    ) -> None:
        self._server = _ThreadedServer(_Socks5Handler)
        self._server.credentials = (username, password) if username is not None else None

    @property
    def port(self) -> int:
        return self._server.port

    @property
    def requests(self) -> list[dict[str, object]]:
        return self._server.snapshot()

    def start(self) -> Socks5Double:
        self._server.start()
        return self

    def stop(self) -> None:
        self._server.stop()

    def __enter__(self) -> Socks5Double:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# Bridge proxy configs and lifecycle helper
# ---------------------------------------------------------------------------


def make_http_proxy(
    port: int,
    *,
    username: str | None = "user",
    password: str | None = "pass",
    proxy_id: str = "proxy-http",
) -> ProxyConfig:
    return ProxyConfig(
        id=proxy_id,
        name=proxy_id,
        scheme="http",
        host="127.0.0.1",
        port=port,
        username=username,
        password=password,
    )


def make_https_proxy(
    port: int,
    *,
    username: str | None = "user",
    password: str | None = "pass",
    proxy_id: str = "proxy-https",
) -> ProxyConfig:
    return ProxyConfig(
        id=proxy_id,
        name=proxy_id,
        scheme="https",
        host="127.0.0.1",
        port=port,
        username=username,
        password=password,
    )


def make_socks_proxy(
    port: int,
    *,
    username: str | None = "user",
    password: str | None = "pass",
    proxy_id: str = "proxy-socks",
) -> ProxyConfig:
    return ProxyConfig(
        id=proxy_id,
        name=proxy_id,
        scheme="socks5",
        host="127.0.0.1",
        port=port,
        username=username,
        password=password,
    )


@contextlib.contextmanager
def bridge_manager(**option_overrides: object) -> Iterator[BridgeManager]:
    """A started BridgeManager with short test timeouts, closed on exit."""
    options_overrides = {
        "port_start": option_overrides.pop("port_start", None) or free_bridge_start(),
        "connect_timeout_s": option_overrides.pop("connect_timeout_s", 3.0),
        "header_timeout_s": option_overrides.pop("header_timeout_s", 2.0),
        "idle_timeout_s": option_overrides.pop("idle_timeout_s", 5.0),
        "shutdown_grace_s": option_overrides.pop("shutdown_grace_s", 0.2),
    }
    options_overrides.update(option_overrides)
    options = BridgeOptions(**options_overrides)  # type: ignore[arg-type]
    manager = BridgeManager(options=options)
    manager.start()
    try:
        yield manager
    finally:
        manager.close()


# ---------------------------------------------------------------------------
# Client helpers
# ---------------------------------------------------------------------------


def http_get_via_bridge(
    bridge_port: int,
    url: str,
    *,
    timeout: float = TIMEOUT,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    """Send an absolute-URI GET through the bridge's HTTP listener."""
    conn = socket.create_connection(("127.0.0.1", bridge_port), timeout=timeout)
    try:
        host = urlsplit(url).netloc
        lines = [f"GET {url} HTTP/1.1", f"Host: {host}", "Connection: close"]
        for name, value in (extra_headers or {}).items():
            lines.append(f"{name}: {value}")
        conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        data = _recv_all(conn)
    finally:
        conn.close()
    return _status_code(data), _response_body(data)


def connect_via_bridge(
    bridge_port: int,
    target_host: str,
    target_port: int,
    *,
    timeout: float = TIMEOUT,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, socket.socket]:
    """Open a CONNECT tunnel through the bridge's HTTP listener."""
    conn = socket.create_connection(("127.0.0.1", bridge_port), timeout=timeout)
    try:
        lines = [
            f"CONNECT {target_host}:{target_port} HTTP/1.1",
            f"Host: {target_host}:{target_port}",
        ]
        for name, value in (extra_headers or {}).items():
            lines.append(f"{name}: {value}")
        conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        data = _recv_head(conn)
    except OSError:
        conn.close()
        raise
    status = _status_code(data)
    if status != 200:
        conn.close()
        return status, socket.socket()
    return status, conn


def tunnel_http_get(conn: socket.socket, path: str = "/", host: str = "origin") -> tuple[int, bytes]:
    conn.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
    data = _recv_all(conn)
    return _status_code(data), _response_body(data)


def echo_roundtrip(conn: socket.socket, payload: bytes, *, timeout: float = TIMEOUT) -> bytes:
    conn.sendall(payload)
    conn.settimeout(timeout)
    received = bytearray()
    while len(received) < len(payload):
        chunk = conn.recv(len(payload) - len(received))
        if not chunk:
            break
        received.extend(chunk)
    return bytes(received)


def socks5_client_connect(
    socks_port: int,
    target_host: str,
    target_port: int,
    *,
    username: str | None = None,
    password: str | None = None,
    command: int = 0x01,
    timeout: float = TIMEOUT,
) -> tuple[int, socket.socket | None]:
    """Minimal SOCKS5 client: returns ``(reply_code, conn)`` (0 = success)."""
    conn = socket.create_connection(("127.0.0.1", socks_port), timeout=timeout)
    try:
        methods = [0x00, 0x02] if username is not None else [0x00]
        conn.sendall(bytes([0x05, len(methods), *methods]))
        reply = _recv_exact(conn, 2)
        if len(reply) < 2:
            conn.close()
            return 0xFF, None
        method = reply[1]
        if method == 0xFF:
            conn.close()
            return 0xFF, None
        if method == 0x02:
            if username is None:
                conn.close()
                return 0xFF, None
            user = username.encode()
            secret = (password or "").encode()
            conn.sendall(bytes([0x01, len(user)]) + user + bytes([len(secret)]) + secret)
            auth_reply = _recv_exact(conn, 2)
            if len(auth_reply) < 2 or auth_reply[1] != 0:
                conn.close()
                return 0xFF, None
        try:
            socket.inet_pton(socket.AF_INET, target_host)
            address = bytes([0x01]) + socket.inet_pton(socket.AF_INET, target_host)
        except OSError:
            encoded = target_host.encode("idna")
            address = bytes([0x03, len(encoded)]) + encoded
        conn.sendall(
            bytes([0x05, command, 0x00]) + address + target_port.to_bytes(2, "big")
        )
        head = _recv_exact(conn, 4)
        if len(head) < 4:
            conn.close()
            return 0xFF, None
        code = head[1]
        if head[3] == 0x01:
            remainder = 4 + 2
        elif head[3] == 0x04:
            remainder = 16 + 2
        elif head[3] == 0x03:
            length = _recv_exact(conn, 1)
            remainder = (length[0] if length else 0) + 2
        else:
            conn.close()
            return 0xFF, None
        _recv_exact(conn, remainder)
        if code != 0:
            conn.close()
            return code, None
        return 0, conn
    except OSError:
        conn.close()
        raise


# ---------------------------------------------------------------------------
# Bridge manager / ProxyBridge tests
# ---------------------------------------------------------------------------


def test_ensure_allocates_loopback_pair_and_is_idempotent() -> None:
    with HttpProxyDouble() as upstream, OriginServer(), bridge_manager() as bridge:
        assert isinstance(bridge, ProxyBridge)
        proxy = make_http_proxy(upstream.port, password="ensure-pass")
        first = bridge.ensure("avd-01", proxy)

        assert first.instance == "avd-01"
        assert first.listen_host == "127.0.0.1"
        assert first.listen_port > 0
        assert first.mode == "http"
        assert first.active is True
        assert first.upstream == f"http://127.0.0.1:{upstream.port}"
        assert "@" not in first.upstream
        assert "ensure-pass" not in first.model_dump_json()
        assert emulator_http_proxy_arg(first) == f"http://127.0.0.1:{first.listen_port}"
        assert bridge.bridge_url("avd-01") == f"http://127.0.0.1:{first.listen_port}"
        socks_port = bridge.socks_port("avd-01")
        assert socks_port is not None and socks_port != first.listen_port
        assert bridge.socks_url("avd-01") == f"socks5://127.0.0.1:{socks_port}"

        again = bridge.ensure("avd-01", proxy)
        assert again.listen_port == first.listen_port
        assert again.upstream == first.upstream
        assert [cfg.instance for cfg in bridge.list_active()] == ["avd-01"]
        assert bridge.get("avd-01") is not None
        assert bridge.get("missing") is None
        assert bridge.active_ports()["avd-01"] == (first.listen_port, socks_port)
        assert is_port_free(first.listen_port) is False
        assert is_port_free(socks_port) is False


def test_stop_releases_ports_and_is_idempotent() -> None:
    with bridge_manager() as bridge:
        config = bridge.ensure("avd-01", None)
        http_port, socks_port = config.listen_port, bridge.socks_port("avd-01")
        assert socks_port is not None
        assert is_port_free(http_port) is False
        assert is_port_free(socks_port) is False
        assert bridge.list_active()

        bridge.stop("avd-01")
        assert bridge.get("avd-01") is None
        assert bridge.list_active() == []
        assert is_port_free(http_port)
        assert is_port_free(socks_port)

        bridge.stop("avd-01")  # idempotent
        assert bridge.get("avd-01") is None


def test_stop_with_active_tunnel_closes_it_and_releases_ports() -> None:
    with EchoServer() as echo, bridge_manager() as bridge:
        config = bridge.ensure("avd-01", None)
        http_port = config.listen_port
        socks_port = bridge.socks_port("avd-01")
        assert socks_port is not None
        status, tunnel = connect_via_bridge(http_port, "127.0.0.1", echo.port)
        assert status == 200
        try:
            bridge.stop("avd-01")
            tunnel.settimeout(2.0)
            with contextlib.suppress(OSError):
                assert tunnel.recv(1) == b""
        finally:
            tunnel.close()
        assert is_port_free(http_port)
        assert is_port_free(socks_port)


def test_close_releases_ports_and_manager_is_unusable() -> None:
    options = BridgeOptions(
        port_start=free_bridge_start(),
        connect_timeout_s=3.0,
        header_timeout_s=2.0,
        idle_timeout_s=5.0,
        shutdown_grace_s=0.2,
    )
    manager = BridgeManager(options=options)
    try:
        config = manager.ensure("avd-01", None)
        http_port = config.listen_port
        socks_port = manager.socks_port("avd-01")
        assert socks_port is not None
    finally:
        manager.close()
    assert manager.closed
    assert is_port_free(http_port)
    assert is_port_free(socks_port)
    with pytest.raises(ProxyError):
        manager.ensure("avd-02", None)


def test_direct_mode_has_no_upstream() -> None:
    with bridge_manager() as bridge:
        config = bridge.ensure("avd-direct", None)
        assert config.mode == "direct"
        assert config.upstream == ""
        assert config.listen_port > 0


def test_two_instances_are_isolated_on_distinct_ports_and_upstreams() -> None:
    with (
        HttpProxyDouble() as upstream_a,
        HttpProxyDouble() as upstream_b,
        OriginServer() as origin,bridge_manager() as bridge
    ):
        config_a = bridge.ensure("avd-a", make_http_proxy(upstream_a.port, proxy_id="a"))
        config_b = bridge.ensure("avd-b", make_http_proxy(upstream_b.port, proxy_id="b"))

        socks_a = bridge.socks_port("avd-a")
        socks_b = bridge.socks_port("avd-b")
        assert config_a.listen_port != config_b.listen_port
        assert socks_a != socks_b
        assert {config_a.listen_port, config_b.listen_port} & {socks_a, socks_b} == set()

        assert http_get_via_bridge(config_a.listen_port, origin.url("/from-a"))[0] == 200
        assert len(upstream_a.requests) == 1
        assert upstream_b.requests == []

        assert http_get_via_bridge(config_b.listen_port, origin.url("/from-b"))[0] == 200
        assert len(upstream_b.requests) == 1
        assert len(upstream_a.requests) == 1  # isolation: B's traffic never hit A
        targets = [record["target"] for record in upstream_b.requests]
        assert targets[0] == origin.url("/from-b")


def test_hot_swap_keeps_listener_port_and_routes_new_connections() -> None:
    with (
        HttpProxyDouble() as upstream_a,
        HttpProxyDouble() as upstream_b,
        OriginServer() as origin,bridge_manager() as bridge
    ):
        config = bridge.ensure("avd-01", make_http_proxy(upstream_a.port, proxy_id="a"))
        port = config.listen_port
        assert http_get_via_bridge(port, origin.url("/before"))[0] == 200
        assert len(upstream_a.requests) == 1
        assert upstream_b.requests == []

        swapped = bridge.swap("avd-01", make_http_proxy(upstream_b.port, proxy_id="b"))
        assert swapped.listen_port == port  # listener is never rebound
        assert swapped.upstream == f"http://127.0.0.1:{upstream_b.port}"
        assert "avd-01" in bridge.list_active()[0].instance

        assert http_get_via_bridge(port, origin.url("/after"))[0] == 200
        assert len(upstream_a.requests) == 1
        assert len(upstream_b.requests) == 1

        # ensure() with a changed proxy also hot-swaps in place.
        ensured = bridge.ensure("avd-01", make_http_proxy(upstream_a.port, proxy_id="a2"))
        assert ensured.listen_port == port
        assert ensured.upstream == f"http://127.0.0.1:{upstream_a.port}"


def test_hot_swap_to_direct_mode() -> None:
    with HttpProxyDouble() as upstream, OriginServer() as origin, bridge_manager() as bridge:
        config = bridge.ensure("avd-01", make_http_proxy(upstream.port))
        port = config.listen_port
        direct = bridge.swap("avd-01", None)
        assert direct.mode == "direct"
        assert direct.upstream == ""
        status, body = http_get_via_bridge(port, origin.url("/direct"))
        assert status == 200
        assert json.loads(body)["path"] == "/direct"
        assert upstream.requests == []


def test_ensure_with_explicit_port_and_busy_port_rejected() -> None:
    requested = free_bridge_start()
    with bridge_manager() as bridge:
        config = bridge.ensure("avd-01", None, listen_port=requested, socks_port=requested + 1)
        assert config.listen_port == requested

        auto = bridge.ensure("avd-02", None)
        assert auto.listen_port not in {requested, requested + 1}
        # Explicit busy port is a hard error, not a silent fallback.
        with pytest.raises(ProxyError):
            bridge.ensure("avd-03", None, listen_port=requested)
        assert bridge.get("avd-03") is None
        with pytest.raises(ProxyError):
            bridge.ensure("avd-04", None, socks_port=requested + 1)
        assert bridge.get("avd-04") is None


def test_validation_errors() -> None:
    with pytest.raises(ValidationError):
        BridgeOptions(listen_host="0.0.0.0")
    with pytest.raises(ValidationError):
        BridgeOptions(connect_timeout_s=0)
    with bridge_manager() as bridge:
        with pytest.raises(ValidationError):
            bridge.ensure("", None)
        with pytest.raises(ValidationError):
            bridge.ensure("avd-01", None, listen_port=0)
        with pytest.raises(ValidationError):
            bridge.ensure("avd-01", None, local_auth_token="")
        with pytest.raises(ProxyError):
            bridge.ensure("avd-01", ProxyConfig(id="bad", scheme="http", host="", port=1))
        with pytest.raises(ProxyError):
            bridge.ensure("avd-01", ProxyConfig(id="bad2", scheme="http", host="bad host", port=1))
        assert bridge.list_active() == []


def test_credentials_never_in_config_repr_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="avdfleet")
    secret = "sup3r-s3cret-password"
    with HttpProxyDouble() as upstream, OriginServer() as origin, bridge_manager() as bridge:
        proxy = make_http_proxy(upstream.port, username="bridge-user", password=secret)
        upstream_client = parse_upstream(proxy)
        assert secret not in repr(upstream_client)
        assert secret not in upstream_client.display
        assert "@" not in upstream_client.display

        config = bridge.ensure("avd-01", proxy)
        assert secret not in str(config)
        assert secret not in config.model_dump_json()
        assert "bridge-user" not in config.model_dump_json()

        swapped = bridge.swap(
            "avd-01",
            make_http_proxy(upstream.port, username="bridge-user", password=secret + "-2"),
        )
        assert secret not in str(swapped)
        assert secret not in swapped.model_dump_json()

        # A failing connection path must not log credentials either.
        failing = bridge.ensure(
            "avd-02",
            make_http_proxy(upstream.port, password="wrong-pass", proxy_id="broken"),
        )
        status, _ = connect_via_bridge(failing.listen_port, "127.0.0.1", origin.port)
        assert status == 502
    assert secret not in caplog.text
    assert "wrong-pass" not in caplog.text
    assert "bridge-user" not in caplog.text


def test_local_auth_token_guards_both_listeners() -> None:
    token = "local-listener-token"
    with OriginServer() as origin, bridge_manager() as bridge:
        config = bridge.ensure("avd-01", None, local_auth_token=token)
        port = config.listen_port
        socks_port = bridge.socks_port("avd-01")
        assert socks_port is not None
        assert token not in config.model_dump_json()
        assert bridge.local_proxy_credentials("avd-01") == ("avd-01", token)

        # HTTP listener: no auth -> 407, correct Basic auth -> 200.
        denied, _ = http_get_via_bridge(port, origin.url("/denied"))
        assert denied == 407
        credentials = base64.b64encode(f"avd-01:{token}".encode()).decode()
        allowed, body = http_get_via_bridge(
            port,
            origin.url("/allowed"),
            extra_headers={"Proxy-Authorization": f"Basic {credentials}"},
        )
        assert allowed == 200
        assert json.loads(body)["path"] == "/allowed"

        # SOCKS5 listener: no-auth offer is refused, credentials work.
        refused, _ = socks5_client_connect(socks_port, "127.0.0.1", origin.port)
        assert refused == 0xFF
        accepted, conn = socks5_client_connect(
            socks_port,
            "127.0.0.1",
            origin.port,
            username="avd-01",
            password=token,
        )
        assert accepted == 0
        assert conn is not None
        conn.close()


def test_port_allocator_bridge_start_is_used() -> None:
    start = free_bridge_start()
    allocator = PortAllocator(start=start + 100, end=start + 160, appium_start=start + 200, bridge_start=start)
    manager = BridgeManager(port_allocator=allocator)
    try:
        config = manager.ensure("avd-01", None)
        assert manager.options.port_start == start
        assert config.listen_port >= start
    finally:
        manager.close()


def test_async_engine_standalone() -> None:
    async def scenario() -> None:
        options = BridgeOptions(
            port_start=free_bridge_start(),
            connect_timeout_s=3.0,
            header_timeout_s=2.0,
            idle_timeout_s=5.0,
            shutdown_grace_s=0.2,
        )
        manager = AsyncBridgeManager(options=options)
        async with manager:
            config = await manager.ensure("avd-01", None)
            assert config.mode == "direct"
            assert await manager.get("avd-01") is not None
            assert len(await manager.list_active()) == 1
            assert await manager.socks_port("avd-01") is not None
            await manager.swap("avd-01", None)
            await manager.stop("avd-01")
            assert await manager.get("avd-01") is None
        with pytest.raises(ProxyError):
            await manager.ensure("avd-02", None)

    asyncio.run(scenario())


def test_sync_and_async_facades_share_the_engine() -> None:
    async def scenario(manager: BridgeManager) -> None:
        config = await manager.aensure("avd-async", None)
        assert config.mode == "direct"
        assert await manager.aget("avd-async") is not None
        assert [cfg.instance for cfg in await manager.alist_active()] == ["avd-async"]
        await manager.aswap("avd-async", None)
        await manager.astop("avd-async")
        assert await manager.aget("avd-async") is None

    with bridge_manager() as bridge:
        asyncio.run(scenario(bridge))
        # The sync facade keeps working after an async excursion.
        assert bridge.ensure("avd-sync", None).mode == "direct"
        assert len(bridge.list_active()) == 1


def test_timeouts_are_configurable_and_idle_tunnel_closes() -> None:
    """An idle CONNECT tunnel is closed by the idle timeout instead of leaking."""
    with EchoServer() as echo, bridge_manager(idle_timeout_s=1.0) as bridge:
        config = bridge.ensure("avd-01", None)
        status, conn = connect_via_bridge(config.listen_port, "127.0.0.1", echo.port)
        assert status == 200
        assert echo_roundtrip(conn, b"alive") == b"alive"
        time.sleep(2.0)
        conn.settimeout(2.0)
        with contextlib.suppress(OSError):
            assert conn.recv(1) == b""
        conn.close()


def test_listener_config_is_loopback_only() -> None:
    with bridge_manager() as bridge:
        config = bridge.ensure("avd-01", None)
        assert config.listen_host == "127.0.0.1"
        assert is_port_free(config.listen_port) is False
        with pytest.raises(ValidationError):
            BridgeOptions(listen_host="::")
        with pytest.raises(ValidationError):
            BridgeOptions(listen_host="example.com")
        with pytest.raises(ValidationError):
            BridgeOptions(listen_host="0.0.0.0")


def test_logs_do_not_contain_listener_tokens(caplog: pytest.LogCaptureFixture) -> None:
    token = "guest-listener-token-123"
    caplog.set_level(logging.DEBUG, logger="avdfleet")
    with OriginServer() as origin, bridge_manager() as bridge:
        config = bridge.ensure("avd-01", None, local_auth_token=token)
        credentials = base64.b64encode(f"avd-01:{token}".encode()).decode()
        assert (
            http_get_via_bridge(
                config.listen_port,
                origin.url("/probe"),
                extra_headers={"Proxy-Authorization": f"Basic {credentials}"},
            )[0]
            == 200
        )
    assert token not in caplog.text
