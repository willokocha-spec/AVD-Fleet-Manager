"""Redirect safety for :meth:`ModuleLibrary.import_url` (audit items 7 and 8).

Every case is driven through ``httpx.MockTransport``: the transport records each
request it is asked to send, so a test can prove that an insecure or non-public
redirect target was never contacted at all.
"""

from __future__ import annotations

import hashlib
import io
import socket
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from avdfleet.core.errors import ValidationError
from avdfleet.instrumentation.library import ModuleLibrary, _default_http_get

#: A public address used by the fake resolver below.  It is never contacted:
#: ``httpx.MockTransport`` answers every request from the test itself.
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


class RecordingClientFactory:
    """A ``client_factory`` whose transport records every request URL.

    The client is built with ``follow_redirects=True`` on purpose: the library
    must disable redirect following itself instead of relying on the default.
    """

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


def _redirecting_handler(
    routes: dict[str, str], payload: bytes
) -> Callable[[httpx.Request], httpx.Response]:
    """Answer *routes* entries with a 302 and everything else with *payload*."""

    def handler(request: httpx.Request) -> httpx.Response:
        target = routes.get(str(request.url))
        if target is not None:
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(200, content=payload)

    return handler


@pytest.fixture()
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the test-only ``*.example.invalid`` hosts to a public address.

    Unit tests must not depend on real DNS, and an ``.invalid`` host never
    resolves.  Patching only those names keeps the resolver honest for every
    other host.
    """
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(
        host: str, port: Any, *args: Any, **kwargs: Any
    ) -> list[tuple[Any, ...]]:
        if isinstance(host, str) and host.endswith(".example.invalid"):
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (PUBLIC_IP, port if isinstance(port, int) else 443),
                )
            ]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


# ---------------------------------------------------------------------------
# Scheme downgrade
# ---------------------------------------------------------------------------
def test_https_to_http_redirect_is_refused_before_any_http_request(
    tmp_path: Path, public_dns: None
) -> None:
    original = "https://downloads.example.invalid/alpha.zip?sig=super-secret-signature"
    insecure = "http://downloads.example.invalid/alpha.zip?sig=super-secret-signature"
    factory = RecordingClientFactory(
        _redirecting_handler({original: insecure}, _magisk_zip())
    )
    library = ModuleLibrary(tmp_path / "modules", http_client_factory=factory)

    with pytest.raises(ValidationError) as refused:
        library.import_url(original)

    message = str(refused.value)
    assert "insecure" in message
    # The signature must not be echoed back: query values are redacted.
    assert "super-secret-signature" not in message
    assert "sig=<redacted>" in message
    # Nothing may be sent to the http URL; only the original https request ran.
    assert factory.requests == [original]
    assert not any(url.startswith("http://") for url in factory.requests)


# ---------------------------------------------------------------------------
# Redirect limit
# ---------------------------------------------------------------------------
def test_redirect_chain_of_six_hops_is_refused(tmp_path: Path, public_dns: None) -> None:
    urls = [f"https://hop{index}.example.invalid/module.zip" for index in range(7)]
    routes = {urls[index]: urls[index + 1] for index in range(6)}
    factory = RecordingClientFactory(_redirecting_handler(routes, _magisk_zip()))
    library = ModuleLibrary(tmp_path / "modules", http_client_factory=factory)

    with pytest.raises(ValidationError) as refused:
        library.import_url(urls[0])

    assert "too many redirects" in str(refused.value)
    # Five redirects are followed; the sixth is refused without fetching it.
    assert factory.requests == urls[:6]
    assert urls[6] not in factory.requests


# ---------------------------------------------------------------------------
# Non-public destinations
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("target", "reason"),
    [
        ("https://127.0.0.1/private/module.zip", "loopback"),
        ("https://169.254.169.254/latest/meta-data/", "link-local"),
        ("https://10.0.0.5/internal/module.zip", "private"),
        ("https://[::1]/private/module.zip", "loopback"),
    ],
)
def test_redirect_to_non_public_address_is_refused(
    tmp_path: Path, public_dns: None, target: str, reason: str
) -> None:
    original = "https://downloads.example.invalid/module.zip"
    factory = RecordingClientFactory(
        _redirecting_handler({original: target}, _magisk_zip())
    )
    library = ModuleLibrary(tmp_path / "modules", http_client_factory=factory)

    with pytest.raises(ValidationError) as refused:
        library.import_url(original)

    message = str(refused.value)
    assert "non-public address" in message
    assert reason in message
    assert factory.requests == [original]


@pytest.mark.parametrize(
    "target",
    [
        "https://127.0.0.1/private/module.zip",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.5/internal/module.zip",
        "https://[::1]/private/module.zip",
    ],
)
def test_allow_private_hosts_permits_those_redirects(
    tmp_path: Path, target: str
) -> None:
    original = "https://downloads.example.invalid/module.zip"
    payload = _magisk_zip()
    factory = RecordingClientFactory(
        _redirecting_handler({original: target}, payload)
    )
    library = ModuleLibrary(
        tmp_path / "modules",
        http_client_factory=factory,
        allow_private_hosts=True,
    )

    record = library.import_url(original)

    assert factory.requests == [original, target]
    assert record.sha256 == hashlib.sha256(payload).hexdigest()
    assert Path(record.file_path).read_bytes() == payload


def test_redirect_to_hostname_resolving_to_loopback_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A public-looking hostname must not launder a loopback destination."""
    original = "https://downloads.example.invalid/module.zip"
    target = "https://rebind.example.invalid/module.zip"
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(
        host: str, port: Any, *args: Any, **kwargs: Any
    ) -> list[tuple[Any, ...]]:
        address = "127.0.0.1" if host == "rebind.example.invalid" else PUBLIC_IP
        if host in {"downloads.example.invalid", "rebind.example.invalid"}:
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (address, port if isinstance(port, int) else 443),
                )
            ]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    factory = RecordingClientFactory(
        _redirecting_handler({original: target}, _magisk_zip())
    )
    library = ModuleLibrary(tmp_path / "modules", http_client_factory=factory)

    with pytest.raises(ValidationError) as refused:
        library.import_url(original)

    assert "loopback" in str(refused.value)
    assert factory.requests == [original]


def test_allow_private_hosts_does_not_allow_an_https_downgrade(
    tmp_path: Path, public_dns: None
) -> None:
    original = "https://downloads.example.invalid/alpha.zip?sig=super-secret"
    insecure = "http://127.0.0.1/alpha.zip?sig=super-secret"
    factory = RecordingClientFactory(
        _redirecting_handler({original: insecure}, _magisk_zip())
    )
    library = ModuleLibrary(
        tmp_path / "modules",
        http_client_factory=factory,
        allow_private_hosts=True,
    )

    with pytest.raises(ValidationError) as refused:
        library.import_url(original)

    assert "insecure" in str(refused.value)
    assert factory.requests == [original]


# ---------------------------------------------------------------------------
# Ordinary https redirects still work
# ---------------------------------------------------------------------------
def test_two_hop_https_redirect_downloads_the_bytes(
    tmp_path: Path, public_dns: None
) -> None:
    original = "https://downloads.example.invalid/redirect.zip"
    middle = "https://cdn.example.invalid/alpha.zip?token=abc"
    payload = _magisk_zip()
    factory = RecordingClientFactory(
        _redirecting_handler({original: middle}, payload)
    )
    library = ModuleLibrary(tmp_path / "modules", http_client_factory=factory)

    record = library.import_url(original)

    assert factory.requests == [original, middle]
    assert record.sha256 == hashlib.sha256(payload).hexdigest()
    assert Path(record.file_path).read_bytes() == payload
    assert record.source_url == original
    assert record.source == "url"


def test_size_cap_is_still_enforced(public_dns: None) -> None:
    url = "https://downloads.example.invalid/module.zip"
    payload = b"x" * 32
    factory = RecordingClientFactory(
        lambda request: httpx.Response(200, content=payload)
    )

    with pytest.raises(ValidationError) as refused:
        _default_http_get(url, 16, client_factory=factory)

    assert "byte limit" in str(refused.value)
