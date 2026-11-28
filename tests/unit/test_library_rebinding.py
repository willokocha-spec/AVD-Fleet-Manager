"""DNS rebinding past the download host check (audit item B).

``_check_download_hop`` resolves the host before a request is made, but httpx
resolved it *again* when connecting, so a resolver that answered a public
address on the first call and ``127.0.0.1`` on the second defeated the check.

The fix moves the authoritative check into the transport that actually opens
the socket: it resolves once, vets every answer, and connects to the literal
address it checked (keeping the original ``Host`` header and TLS SNI).  The
tests below prove:

* a rebinding answer is refused before any socket is opened, so a local server
  on ``127.0.0.1`` receives no request at all;
* a normal public host still downloads through an injected mock transport;
* a redirect to a private address is still refused, as before;
* the guarded transport really does connect to the checked IP while preserving
  the operator's authority and SNI hostname.
"""

from __future__ import annotations

import hashlib
import http.server
import io
import socket
import threading
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from avdfleet.core.errors import ValidationError
from avdfleet.instrumentation import ModuleKind
from avdfleet.instrumentation.library import (
    ModuleLibrary,
    _GuardedHTTPTransport,
)

#: A public address used by the fake resolver.  With the fix it is never
#: connected to either: the mock transport answers, or the request is refused.
PUBLIC_IP = "93.184.216.34"


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _magisk_zip() -> bytes:
    return _zip_bytes(
        {"module.prop": b"id=alpha\nname=Alpha\nversion=1.0\nversionCode=1\n"}
    )


def _stub_dns(
    monkeypatch: pytest.MonkeyPatch, host: str, answers: list[str]
) -> list[str]:
    """Answer *host* from *answers* in order; record each address served.

    All other names fall through to the real resolver so the rest of the
    environment keeps working.
    """
    calls: list[str] = []
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(
        hostname: Any, port: Any, *args: Any, **kwargs: Any
    ) -> list[tuple[Any, ...]]:
        if hostname == host:
            address = answers[min(len(calls), len(answers) - 1)]
            calls.append(address)
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (address, port if isinstance(port, int) else 80),
                )
            ]
        return real_getaddrinfo(hostname, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return calls


class RecordingClientFactory:
    """An injected ``client_factory`` whose mock transport records requests."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler
        self.requests: list[str] = []

    def __call__(self) -> httpx.Client:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(str(request.url))
            return self._handler(request)

        return httpx.Client(
            transport=httpx.MockTransport(handle), follow_redirects=True
        )


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """A local HTTP server that records every request path it receives."""

    hits: list[str] = []
    payload: bytes = b""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).hits.append(self.path)
        body = type(self).payload
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # keep pytest output quiet
        pass


@pytest.fixture()
def local_server() -> Iterator[tuple[http.server.ThreadingHTTPServer, list[str]]]:
    """A real loopback server whose request log is the no-contact evidence."""
    _RecordingHandler.hits = []
    _RecordingHandler.payload = _magisk_zip()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, _RecordingHandler.hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# The rebinding attack
# ---------------------------------------------------------------------------
def test_rebinding_to_loopback_after_the_check_is_refused_before_connecting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_server: tuple[http.server.ThreadingHTTPServer, list[str]],
) -> None:
    server, hits = local_server
    port = server.server_address[1]
    host = "rebind.example.invalid"
    url = f"http://{host}:{port}/module.zip"
    # Public when the pre-check resolves it, loopback when the connection does.
    answers = _stub_dns(monkeypatch, host, [PUBLIC_IP, "127.0.0.1"])
    library = ModuleLibrary(tmp_path / "modules", allow_insecure_urls=True)

    with pytest.raises(ValidationError) as refused:
        library.import_url(url)

    message = str(refused.value)
    assert "non-public address" in message
    assert "loopback" in message
    # Both answers were really consulted: the attack path was exercised.
    assert answers == [PUBLIC_IP, "127.0.0.1"]
    # The decisive evidence: the loopback server saw no request at all.
    assert hits == []
    # Nothing was stored because the download never happened.
    assert not (tmp_path / "modules").exists() or list(
        (tmp_path / "modules").iterdir()
    ) == []


# ---------------------------------------------------------------------------
# The ordinary path still works
# ---------------------------------------------------------------------------
def test_public_host_still_downloads_through_a_mock_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = "cdn.example.invalid"
    _stub_dns(monkeypatch, host, [PUBLIC_IP])
    url = f"https://{host}/module.zip?token=abc"
    payload = _magisk_zip()
    factory = RecordingClientFactory(
        lambda request: httpx.Response(200, content=payload)
    )
    library = ModuleLibrary(tmp_path / "modules", http_client_factory=factory)

    record = library.import_url(url)

    assert record.kind is ModuleKind.MAGISK_ZIP
    assert factory.requests == [url]
    assert record.sha256 == hashlib.sha256(payload).hexdigest()
    assert Path(record.file_path).read_bytes() == payload


def test_redirect_to_a_private_address_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = "downloads.example.invalid"
    _stub_dns(monkeypatch, host, [PUBLIC_IP])
    original = f"https://{host}/alpha.zip"
    target = "https://127.0.0.1/alpha.zip"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == original:
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(200, content=_magisk_zip())

    factory = RecordingClientFactory(handler)
    library = ModuleLibrary(tmp_path / "modules", http_client_factory=factory)

    with pytest.raises(ValidationError) as refused:
        library.import_url(original)

    message = str(refused.value)
    assert "non-public address" in message
    assert "loopback" in message
    # Only the original public hop was requested; the private one never was.
    assert factory.requests == [original]


# ---------------------------------------------------------------------------
# Mechanism: the socket target is the checked IP, Host/SNI stay the hostname
# ---------------------------------------------------------------------------
def test_guarded_transport_connects_to_the_checked_ip_but_keeps_host_and_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = "downloads.example.invalid"
    _stub_dns(monkeypatch, host, [PUBLIC_IP])
    seen: list[httpx.Request] = []

    def capture(self: Any, request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"ok", request=request)

    # The base transport would open the socket; capture the request it is
    # handed instead, which is exactly what httpcore will connect to.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", capture)
    transport = _GuardedHTTPTransport()
    request = httpx.Request("GET", f"https://{host}:8443/module.zip?sig=secret")

    response = transport.handle_request(request)

    assert response.status_code == 200
    sent = seen[0]
    # Connected to the literal checked address...
    assert sent.url.host == PUBLIC_IP
    assert sent.url.port == 8443
    # ...while the server still sees the operator's authority...
    assert sent.headers["host"] == f"{host}:8443"
    # ...and TLS verification (httpcore's server_hostname) still uses the name.
    assert sent.extensions["sni_hostname"] == host
