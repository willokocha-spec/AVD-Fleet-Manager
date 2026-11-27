#!/usr/bin/env python3
"""AVD Fleet gate G1: loopback-only HTTP + SOCKS5 test-listener harness.

Purpose
-------
Gate G1 (BRIEF.md sections 3.4 and 7 item 3) requires five isolated, authenticated
egress listeners, one per AVD, and proof that each AVD's token is seen *only* on its
assigned listener.  This module is the local, hermetic half of that test:

* five listeners in one process: three HTTP forward proxies and two SOCKS5 proxies
  (HTTP CONNECT tunnelling and SOCKS5 username/password auth are implemented),
* one neutral echo origin (plain HTTP, optional TLS) that the listeners front, so no
  internet access is required and every observed request is attributable,
* token observation on request URIs, headers, bodies, CONNECT authorities, SOCKS5
  credentials/destinations and tunnelled plaintext,
* redacted public logs plus raw logs kept only in memory for assertions,
* :func:`verify_isolation`, a pass/fail report builder for the gate assertions,
* a self-test (run this file as ``__main__``) that starts five listeners, prints their
  URLs with credentials, exercises every channel including negative controls, and
  returns exit code 0 only when all checks pass.

Safety properties
-----------------
* Binds only to loopback (``127.0.0.1``); a non-loopback bind address is rejected.
* No third-party dependencies; Python 3.11+ standard library only.
* Public/report output never contains raw tokens or passwords.  The raw event API
  (``logs()`` / ``origin_logs()``) is an explicitly privileged test-fixture surface;
  use the redacted variants (``redacted_logs`` / ``export_evidence``) for anything
  written to disk or CI.
* Everything here is a test fixture.  Generated credentials and tokens are ephemeral
  and must never be reused for a real proxy or a real API token.

Usage
-----
Run the self-test (default)::

    python research/g1/listeners.py

Keep five listeners alive for manual/guest testing::

    python research/g1/listeners.py --serve
    python research/g1/listeners.py --serve --no-tls   # plain-HTTP origin only

Embed in an acceptance test::

    from listeners import ListenerHarness, verify_isolation

    harness = ListenerHarness.standard().start()
    try:
        for name in harness.tokens:
            proxy_url = harness.proxy_url(name)      # e.g. http://u:p@127.0.0.1:PORT
            probe_url = harness.token_url(name)      # contains this listener's token
            ...  # configure the guest + bridge, make the guest GET probe_url
        report = verify_isolation(harness.tokens, harness.listener_logs(),
                                  credentials=harness.credentials,
                                  origin_logs=harness.origin_logs())
        assert report["passed"], report["failures"]
    finally:
        harness.stop()

The guest request protocol, evidence layout and pass/fail criteria for the real
accelerated-host run are specified in ``research/g1/gate.md``.

Wire protocol / assumptions
---------------------------
* Guest proxy configuration for an HTTP listener: ``http://user:pass@127.0.0.1:PORT``
  (or the instance bridge's upstream).  HTTP requests use absolute-form request
  targets (``GET http://host/path HTTP/1.1``); HTTPS uses CONNECT.
* SOCKS5 listeners implement RFC 1928 CONNECT plus RFC 1929 username/password auth.
  BIND and UDP ASSOCIATE are rejected (code 0x07) and logged: a UDP ASSOCIATE attempt
  from a guest is evidence that the guest tried to leak around the proxy.
* The harness maps every non-origin destination to the local echo origin by default
  (``resolve_override=True``).  Token URLs use the reserved ``.test`` TLD, which does
  not resolve, so a guest that bypasses the proxy cannot reach the origin.
* Optional TLS: the echo origin has a second HTTPS port using a self-signed test
  certificate embedded below (override with ``AVDFLEET_G1_TLS_CERT`` /
  ``AVDFLEET_G1_TLS_KEY``).  It exists so CONNECT tunnelling and TLS origins can be
  exercised; test clients must use ``CERT_NONE`` or trust the embedded cert.  A guest
  browser will not trust it, so guest acceptance proof uses the plain HTTP origin;
  TLS is optional plumbing evidence.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import secrets
import select
import shutil
import socket
import socketserver
import ssl
import struct
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "DEFAULT_BASE_DOMAIN",
    "DEFAULT_ORIGIN_PATH",
    "DEFAULT_SPECS",
    "EchoOrigin",
    "HTTPProxyListener",
    "ListenerHarness",
    "Redactor",
    "SOCKS5ProxyListener",
    "TlsMaterial",
    "TokenRegistry",
    "main",
    "run_selftest",
    "verify_isolation",
]

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_BASE_DOMAIN = "g1.test"
DEFAULT_ORIGIN_PATH = "/g1/origin"
DEFAULT_SPECS: tuple[tuple[str, str], ...] = (
    ("L1", "http"),
    ("L2", "http"),
    ("L3", "http"),
    ("L4", "socks5"),
    ("L5", "socks5"),
)
PROXY_REALM = "avdfleet-g1"
HANDLER_TIMEOUT = 30.0
CONNECT_TIMEOUT = 5.0
RELAY_TIMEOUT = 30.0
MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 128 * 1024
MAX_RELAY_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 2000
TLS_CERT_ENV = "AVDFLEET_G1_TLS_CERT"
TLS_KEY_ENV = "AVDFLEET_G1_TLS_KEY"

# ---------------------------------------------------------------------------
# Embedded self-signed certificate for the optional HTTPS echo origin.
# Test-only material: CN=avdfleet-g1-test, SAN localhost / *.g1.test /
# *.avdfleet.test, valid 2025-01-01 .. 2035-01-01.  Never use outside tests.
# ---------------------------------------------------------------------------
_TEST_TLS_CERT_PEM = """\
-----BEGIN CERTIFICATE-----
MIIDRDCCAiygAwIBAgIQNOtXziM8l7JKnxwc2nrf1jANBgkqhkiG9w0BAQsFADAb
MRkwFwYDVQQDDBBhdmRmbGVldC1nMS10ZXN0MB4XDTI1MDEwMTAwMDAwMFoXDTM1
MDEwMTAwMDAwMFowGzEZMBcGA1UEAwwQYXZkZmxlZXQtZzEtdGVzdDCCASIwDQYJ
KoZIhvcNAQEBBQADggEPADCCAQoCggEBAM8dkqDV8qJTdYxoN1UovCWgRM0tAUVz
djl+ou8CH31tvtATDojwUL0sfQNwDUf5mxVB13Pckk6GengVkg4WLIHu86+LmGKA
rdCef/d74+4Hj6L/OXdwMwlS6x+IUWSQvouINSfMvg7PTBTZywlkw4woPnhftDYk
/i9zCY7iias4ebNPQSqnnA32nx1Vg8+v1e/WlyxjvYBNdvM2CU497QN0DXKSxdU8
gMkHXvpzZ8++nd4qFKu2ZYveyXGKS84e8jkin5sLj0us8+3wVZuoTFQhxFKpIVvQ
JRYDvGVE3SSdhqFvbrgrzN+kB56va618nd1XYni6/Jw5lcDd6v4RegkCAwEAAaOB
gzCBgDAOBgNVHQ8BAf8EBAMCBaAwHQYDVR0lBBYwFAYIKwYBBQUHAwIGCCsGAQUF
BwMBMDAGA1UdEQQpMCeCCWxvY2FsaG9zdIIJKi5nMS50ZXN0gg8qLmF2ZGZsZWV0
LnRlc3QwHQYDVR0OBBYEFNYwXTuOtt+XVfCww878TkFD7ItEMA0GCSqGSIb3DQEB
CwUAA4IBAQCaJgglLDCe1F8nHqe94ajCz3D6ODUMGwG6eAs+QegunE0qsDT7MQ8H
W23ZfdIou+YHckT4hFEUp0i9CSUzc/quBk9TBwaLiN1DR0mVUxz0fUg/5sjKpAMx
vbVzzIb4B9me4Rw5N2LHaNrdZLLQvqGx8VzPm1ulxl4r4ycfbCwcPNsw0EPWxETC
7LeZ5RLccBG/joIoaWwzJEaZk03G/ScbzZmJ6KM9T3UnWu5cGRnCKG08+HpPMa8W
dm+txhmP17A9blYBTmDzyUwFPwlQ74uMvAU4jlMEQeb5/XuMlbBsrIXi8QpHhjph
w1wzpdAELLcQwUjMWf2dKpQurvPE/R2I
-----END CERTIFICATE-----
"""
_TEST_TLS_KEY_PEM = """\
-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEAzx2SoNXyolN1jGg3VSi8JaBEzS0BRXN2OX6i7wIffW2+0BMO
iPBQvSx9A3ANR/mbFUHXc9ySToZ6eBWSDhYsge7zr4uYYoCt0J5/93vj7gePov85
d3AzCVLrH4hRZJC+i4g1J8y+Ds9MFNnLCWTDjCg+eF+0NiT+L3MJjuKJqzh5s09B
KqecDfafHVWDz6/V79aXLGO9gE128zYJTj3tA3QNcpLF1TyAyQde+nNnz76d3ioU
q7Zli97JcYpLzh7yOSKfmwuPS6zz7fBVm6hMVCHEUqkhW9AlFgO8ZUTdJJ2GoW9u
uCvM36QHnq9rrXyd3VdieLr8nDmVwN3q/hF6CQIDAQABAoIBAC+P8P4yY5fXZqUp
wZsy0ZIqbeJYNXSHgVzOsggFCso5C5qo5auY1FG9B7BkaQhgT7bf+eRHgOm7hCBT
g8jsWqg0AY4Mlp/4QPIpk+V3c+5Tmi6qQb4l6/J2Mjx5h4d6nedZDivnnrWz8RrQ
sQmlNCrZiw8Fm1Lbx+uJlWZUq5y/32KNhpVgWkcjv2PdDyeE18EEKl7DwFqACWia
0RI/7Fa9nZOpbwJyFekFkAWzF8hL284DXMVtVA2YZAfKL7SYmHqKqIww/EO2eC4W
5tj+z/W7Gwey5s9g9Hx2McTjod/4SDq+g4hZ4jOtvvmfymNaCp3ET8ho9v5uCMsI
rbxxGskCgYEA1yzj9jxV/X99aRK8zTbP+eB9Heip+E4cisKNAoCenoojlkO+e0Ek
qd5AIPy/k+3dg7ba9s4mki0NHbLEWzEVaFVN67aL7pYReO61/SCdXMSY6r2uvkAi
ChgQSDGdAV8Tw7Jikk0ci2wckoCH7wWVlqLrMEqfIfyDEbM5e/0v7c8CgYEA9mk2
6qxWl5dRd4CaopVniyEB2Ul5FFJ4KXL2FlUJ6j1n7xTa2IL/Gdl4mrU4lQ7TY/Hp
c6Z7WsEG6x8LweQIICL8jjfSQSKyYLCoUO4R5MRiKASqq3a4sBx9Q8XiMI5a6aGK
SazunwN35jX0hl27UCuS/IS1+XTL3AtwpGVLKKcCgYAL44IQ59KEeBfQx2VBHWcu
UjX3ghRqEL7I9+jcm4ioD8/ZjJKosre43vvCFtTq8niLv32np0pAR1bzLh5lP3gE
VLpCbMqcexQqD15kxvmK153vUl2mS1rWHfBAXOVk3wDzjhaeKL6MAYQuoAVisndK
ZGQEPgs+0HnZb4KclCeixwKBgQDSH9qyt4+H6YORElvLNPh9DgKTljHoAINzZRMw
7AdudjrBB9mvM9Xx5x8PXmYHxgLBgbL/6gHa3CvYKDtQdfHv5aXUaQmkfb/67+o4
SSt9F3JUidY4LR9FHqY7Ou2tVld0T0qrlQpPExgVegEw65ElxmKrb0f2l4CX/jFq
Hu6+lQKBgAH1942mBh78h8j5O5c5OMA4++S5XTGfSNci4/mRHU6MpltrSW6UiORl
cUmWEUetbaSWghskQX12jsj9G5v9vXn6hqyCRadWk+DCcF2vomYDD6QTJyklptTE
esYMbJkJNRkxbHIiBLiQ+lbjUFBRV2lxpHydosPd8obS0v9kEW2k
-----END RSA PRIVATE KEY-----
"""

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    """UTC ISO-8601 timestamp with millisecond resolution."""
    now = time.time()
    whole = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
    return f"{whole}.{int(now * 1000) % 1000:03d}Z"


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _cred_fingerprint(username: str, password: str) -> str:
    """Non-reversible test credential fingerprint (safe to log and compare)."""
    return _sha16("avdfleet-g1-cred\x00" + username + "\x00" + password)


def _clip(text: str, limit: int = 400) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...<clipped>"


def _digest_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8", "replace"), right.encode("utf-8", "replace"))


def _close_quiet(sock: socket.socket | None) -> None:
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows: ACLs are inherited from the per-user temp directory.


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() in {"localhost", "localhost.localdomain"}


def _ipv4_loopback(host: str) -> str:
    """Normalise a bind host to an IPv4 loopback literal; reject anything else.

    The harness servers are AF_INET only; ``localhost`` is accepted as a convenience
    and normalised to ``127.0.0.1``.
    """
    candidate = (host or "").strip()
    if candidate.lower() in {"localhost", "localhost.localdomain"}:
        return LOOPBACK_HOST
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise ValueError(
            f"bind host must be an IPv4 loopback literal or 'localhost' (got {host!r})"
        ) from exc
    if address.version != 4 or not address.is_loopback:
        raise ValueError(f"bind host must be an IPv4 loopback address (got {host!r})")
    return str(address)


def _basic_b64(username: str, password: str) -> str:
    return base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")


def _basic_header(username: str, password: str) -> str:
    return "Basic " + _basic_b64(username, password)


def _parse_basic_header(value: str | None) -> tuple[str, str] | None:
    if not value:
        return None
    parts = value.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        return None
    try:
        raw = base64.b64decode(parts[1].strip(), validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return None
    if ":" not in raw:
        return None
    username, _, password = raw.partition(":")
    return username, password


def _host_port(authority: str, default_port: int) -> tuple[str, int]:
    """Parse ``host[:port]`` or ``[v6]:port``; raise ValueError when malformed."""
    authority = (authority or "").strip()
    if not authority:
        raise ValueError("empty authority")
    if authority.startswith("["):
        end = authority.find("]")
        if end < 0:
            raise ValueError(f"malformed IPv6 authority: {authority!r}")
        host = authority[1:end]
        rest = authority[end + 1 :]
        if rest.startswith(":"):
            port = int(rest[1:])
        elif rest:
            raise ValueError(f"malformed authority: {authority!r}")
        else:
            port = default_port
    else:
        if authority.count(":") == 1:
            host, port_text = authority.rsplit(":", 1)
            port = int(port_text)
        else:
            host = authority
            port = default_port
    if not host or not (0 < port < 65536):
        raise ValueError(f"malformed authority: {authority!r}")
    return host, port


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("connection closed while reading a fixed-size frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_until(sock: socket.socket, marker: bytes, limit: int) -> tuple[bytes, bytes]:
    """Read until *marker*; return (through_marker, leftover_after_marker)."""
    buffer = b""
    while marker not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            return buffer, b""
        buffer += chunk
        if len(buffer) > limit:
            raise ValueError("header block exceeded limit")
    index = buffer.index(marker) + len(marker)
    return buffer[:index], buffer[index:]


def _split_http_response(raw: bytes) -> tuple[int, dict[str, str], bytes]:
    head, _, body = raw.partition(b"\r\n\r\n")
    status = 0
    headers: dict[str, str] = {}
    lines = head.split(b"\r\n") if head else []
    if lines:
        parts = lines[0].split(b" ", 2)
        if len(parts) >= 2:
            try:
                status = int(parts[1])
            except ValueError:
                status = 0
    for line in lines[1:]:
        key, _, value = line.partition(b":")
        if key:
            headers[key.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    return status, headers, body


def _json_error(message: str) -> bytes:
    return json.dumps({"g1": True, "error": message}, sort_keys=True).encode("utf-8")


# ---------------------------------------------------------------------------
# TLS material (optional; embedded self-signed test certificate)
# ---------------------------------------------------------------------------


class TlsMaterial:
    """PEM paths for the optional HTTPS echo origin.

    Resolution order: explicit arguments, then ``AVDFLEET_G1_TLS_CERT`` /
    ``AVDFLEET_G1_TLS_KEY``, then the embedded self-signed certificate written to a
    private temporary directory.  The embedded key is test-only material.
    """

    def __init__(self, certfile: str | None = None, keyfile: str | None = None) -> None:
        env_cert = os.environ.get(TLS_CERT_ENV)
        env_key = os.environ.get(TLS_KEY_ENV)
        certfile = certfile or (os.path.abspath(env_cert) if env_cert else None)
        keyfile = keyfile or (os.path.abspath(env_key) if env_key else None)
        self._tempdir: Path | None = None
        if certfile or keyfile:
            if not (certfile and keyfile):
                raise ValueError(f"both {TLS_CERT_ENV} and {TLS_KEY_ENV} must be set together")
            cert_path, key_path = Path(certfile), Path(keyfile)
            if not cert_path.is_file() or not key_path.is_file():
                raise FileNotFoundError("configured TLS cert/key file is missing")
        else:
            self._tempdir = Path(tempfile.mkdtemp(prefix="avdfleet-g1-tls-"))
            cert_path = self._tempdir / "cert.pem"
            key_path = self._tempdir / "key.pem"
            cert_path.write_text(_TEST_TLS_CERT_PEM, encoding="ascii")
            key_path.write_text(_TEST_TLS_KEY_PEM, encoding="ascii")
            _chmod_private(cert_path)
            _chmod_private(key_path)
        self.certfile = str(cert_path)
        self.keyfile = str(key_path)
        self._context: ssl.SSLContext | None = None
        self._lock = threading.Lock()

    def server_context(self) -> ssl.SSLContext:
        with self._lock:
            if self._context is None:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(self.certfile, self.keyfile)
                self._context = context
            return self._context

    @staticmethod
    def client_context() -> ssl.SSLContext:
        """Insecure client context for the self-signed test origin (test only)."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    def close(self) -> None:
        if self._tempdir is not None:
            shutil.rmtree(self._tempdir, ignore_errors=True)
            self._tempdir = None


# ---------------------------------------------------------------------------
# Redaction and token registry
# ---------------------------------------------------------------------------


class Redactor:
    """Replace raw test secrets with stable, assertion-safe placeholders.

    Raw values remain available through the listeners' raw event API; this class is
    only for the "normal output" and on-disk redacted evidence paths.
    """

    def __init__(self) -> None:
        self._secrets: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def add_secret(self, value: str, replacement: str) -> None:
        if not value:
            return
        with self._lock:
            self._secrets.append((value, replacement))
            self._secrets.sort(key=lambda item: len(item[0]), reverse=True)

    def add_token(self, name: str, token: str) -> None:
        self.add_secret(token, f"<token:{name}>")

    def add_credentials(self, username: str, password: str) -> None:
        self.add_secret(f"{username}:{password}", f"{username}:***")
        self.add_secret(_basic_b64(username, password), "<basic-auth-redacted>")
        if len(password) >= 8:
            self.add_secret(password, "***")

    def redact_text(self, text: str) -> str:
        result = str(text)
        with self._lock:
            secrets = list(self._secrets)
        for raw, replacement in secrets:
            if raw and raw in result:
                result = result.replace(raw, replacement)
        return result

    def redact_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in event.items():
            if key == "token" and value not in (None, "", "<redacted>"):
                out[key] = "<redacted>"
            elif isinstance(value, str):
                out[key] = self.redact_text(value)
            elif isinstance(value, Mapping):
                out[key] = self.redact_event(value)
            elif isinstance(value, (list, tuple)):
                out[key] = [
                    self.redact_event(item) if isinstance(item, Mapping) else self.redact_text(str(item))
                    for item in value
                ]
            else:
                out[key] = value
        return out


class TokenRegistry:
    """All tokens known to the harness; each listener scans for every token.

    Scanning for *all* tokens (not just the listener's own) is what makes a
    cross-listener sighting detectable rather than invisible.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}
        self._lock = threading.Lock()

    def register(self, listener_name: str, token: str) -> None:
        with self._lock:
            self._tokens[listener_name] = token

    @property
    def max_token_len(self) -> int:
        with self._lock:
            return max((len(token) for token in self._tokens.values()), default=1)

    def entries(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._tokens.items())

    def scan(self, text: str) -> list[tuple[str, str]]:
        if not text:
            return []
        hits: list[tuple[str, str]] = []
        for name, token in self.entries():
            if token and token in text:
                hits.append((name, token))
        return hits


# ---------------------------------------------------------------------------
# Loopback socket servers
# ---------------------------------------------------------------------------


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _TLSThreadingHTTPServer(_ThreadingHTTPServer):
    ssl_context: ssl.SSLContext | None = None

    def get_request(self) -> tuple[socket.socket, Any]:
        sock, address = super().get_request()
        context = self.ssl_context
        if context is None:
            return sock, address
        try:
            return context.wrap_socket(sock, server_side=True), address
        except (ssl.SSLError, OSError):
            _close_quiet(sock)
            raise


class _ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------------------
# Echo origin: the neutral upstream used by every listener
# ---------------------------------------------------------------------------


class _OriginHTTPHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "avdfleet-g1-origin/1.0"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - stdlib name
        pass

    @property
    def origin(self) -> "EchoOrigin":
        return self.server.origin  # type: ignore[attr-defined]

    def _serve(self) -> None:
        origin = self.origin
        body = b""
        content_length = self.headers.get("Content-Length")
        if content_length:
            try:
                length = int(content_length)
            except ValueError:
                length = 0
            if 0 < length <= origin.max_body_bytes:
                body = self.rfile.read(length)
        parsed = urllib.parse.urlsplit(self.path)
        is_tls = isinstance(self.connection, ssl.SSLSocket)
        origin.record(
            origin="https" if is_tls else "http",
            method=self.command,
            path=parsed.path,
            query=parsed.query,
            host_header=self.headers.get("Host", ""),
            via_listener=self.headers.get("X-Avdfleet-G1-Listener", ""),
            remote=self.client_address[0] if self.client_address else "",
            body_bytes=len(body),
        )
        payload = {
            "g1": "ok",
            "origin": "https" if is_tls else "http",
            "method": self.command,
            "path": self.path,
            "via": self.headers.get("X-Avdfleet-G1-Listener", ""),
        }
        raw = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)
        self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        self._serve()

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib name
        self._serve()

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        self._serve()

    def do_PUT(self) -> None:  # noqa: N802 - stdlib name
        self._serve()

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib name
        self._serve()

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib name
        self._serve()


class EchoOrigin:
    """Loopback echo origin used behind the listeners; records every request."""

    def __init__(
        self,
        bind: str = LOOPBACK_HOST,
        *,
        tls_material: TlsMaterial | None = None,
        redactor: Redactor | None = None,
        capacity: int = MAX_EVENTS,
        max_body_bytes: int = MAX_BODY_BYTES,
    ) -> None:
        if not _is_loopback(bind):
            raise ValueError("G1 echo origin must bind to loopback only")
        self.bind = _ipv4_loopback(bind)
        self.max_body_bytes = max_body_bytes
        self.redactor = redactor or Redactor()
        self.capacity = capacity
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.tls_error = ""
        self._servers: list[socketserver.BaseServer] = []
        self._threads: list[threading.Thread] = []
        self._http_server = _ThreadingHTTPServer((bind, 0), _OriginHTTPHandler)
        self._http_server.origin = self  # type: ignore[attr-defined]
        self.http_port = int(self._http_server.server_address[1])
        self._servers.append(self._http_server)
        self.https_port: int | None = None
        if tls_material is not None:
            try:
                https_server = _TLSThreadingHTTPServer((bind, 0), _OriginHTTPHandler)
                https_server.origin = self  # type: ignore[attr-defined]
                https_server.ssl_context = tls_material.server_context()
                self.https_port = int(https_server.server_address[1])
                self._servers.append(https_server)
            except (OSError, ssl.SSLError, ValueError, RuntimeError) as exc:
                self.tls_error = f"{type(exc).__name__}: {exc}"
        for index, server in enumerate(self._servers):
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.05},
                name=f"g1-origin-{index}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    # -- logging -----------------------------------------------------------

    def record(self, **fields: Any) -> dict[str, Any]:
        event: dict[str, Any] = {"ts": _iso_now(), "origin": "http"}
        event.update(fields)
        with self._lock:
            self._events.append(event)
            if len(self._events) > self.capacity:
                del self._events[: len(self._events) - self.capacity]
        return event

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(event) for event in self._events]

    def redacted_events(self) -> list[dict[str, Any]]:
        return [self.redactor.redact_event(event) for event in self.events()]

    # -- lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        for server in self._servers:
            try:
                server.shutdown()
            except Exception:  # pragma: no cover - defensive shutdown path
                pass
            try:
                server.server_close()
            except Exception:  # pragma: no cover
                pass
        for thread in self._threads:
            thread.join(timeout=2.0)

    @property
    def http_url(self) -> str:
        return f"http://{self.bind}:{self.http_port}{DEFAULT_ORIGIN_PATH}"

    @property
    def https_url(self) -> str | None:
        if self.https_port is None:
            return None
        return f"https://{self.bind}:{self.https_port}{DEFAULT_ORIGIN_PATH}"


# ---------------------------------------------------------------------------
# Shared listener machinery
# ---------------------------------------------------------------------------


class _ListenerBase:
    """Common state: event log, token observation, credentials, sockets."""

    kind = "listener"

    def __init__(
        self,
        *,
        name: str,
        host: str,
        port: int,
        username: str,
        password: str,
        token: str,
        registry: TokenRegistry,
        redactor: Redactor,
        upstream_resolver: Any,
        credential_lookup: Any,
        capacity: int = MAX_EVENTS,
        max_body_bytes: int = MAX_BODY_BYTES,
    ) -> None:
        if not _is_loopback(host):
            raise ValueError("G1 listener must bind to loopback only")
        self.name = name
        self.host = _ipv4_loopback(host)
        self.port = int(port or 0)
        self.username = username
        self.password = password
        self.token = token
        self.registry = registry
        self.redactor = redactor
        self.upstream_resolver = upstream_resolver
        self.credential_lookup = credential_lookup
        self.capacity = capacity
        self.max_body_bytes = max_body_bytes
        self.cred_fp = _cred_fingerprint(username, password)
        self._events: list[dict[str, Any]] = []
        self._events_lock = threading.RLock()
        self._active: set[socket.socket] = set()
        self._active_lock = threading.Lock()
        self._server: socketserver.BaseServer | None = None
        self._thread: threading.Thread | None = None

    # -- event log ---------------------------------------------------------

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "ts": _iso_now(),
            "listener": self.name,
            "kind": self.kind,
            "event": event,
        }
        record.update(fields)
        with self._events_lock:
            self._events.append(record)
            if len(self._events) > self.capacity:
                del self._events[: len(self._events) - self.capacity]
        return record

    def observe(self, text: str, *, source: str, detail: str | None = None) -> list[tuple[str, str]]:
        """Record a token sighting for every known token present in *text*."""
        hits = self.registry.scan(text)
        for token_name, token in hits:
            self.record(
                "token_sighting",
                token_name=token_name,
                token=token,
                source=source,
                detail=_clip(detail if detail is not None else text),
            )
        return hits

    def events(self) -> list[dict[str, Any]]:
        with self._events_lock:
            return [dict(event) for event in self._events]

    def redacted_events(self) -> list[dict[str, Any]]:
        return [self.redactor.redact_event(event) for event in self.events()]

    # -- sockets -----------------------------------------------------------

    def _register_socket(self, *socks: socket.socket) -> None:
        with self._active_lock:
            for sock in socks:
                self._active.add(sock)

    def _unregister_socket(self, *socks: socket.socket) -> None:
        with self._active_lock:
            for sock in socks:
                self._active.discard(sock)

    def _close_active(self) -> None:
        with self._active_lock:
            sockets = list(self._active)
            self._active.clear()
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            _close_quiet(sock)

    # -- credentials -------------------------------------------------------

    def matches_credentials(self, username: str, password: str) -> bool:
        return _digest_equal(username, self.username) and _digest_equal(password, self.password)

    def find_credential_owner(self, username: str, password: str) -> str | None:
        if self.credential_lookup is None:
            return None
        return self.credential_lookup(username, password)

    def record_auth_success(self, scheme: str, username: str) -> None:
        self.record("auth_ok", scheme=scheme, username=_clip(username, 128), cred_fp=self.cred_fp)

    def record_auth_failure(self, scheme: str, username: str, reason: str, matched: str | None = None) -> None:
        fields: dict[str, Any] = {
            "scheme": scheme,
            "username": _clip(username or "", 128),
            "reason": reason,
        }
        if matched:
            fields["matched_listener"] = matched
        self.record("auth_failure", **fields)

    # -- upstream / url ----------------------------------------------------

    def upstream_connect(self, host: str, port: int) -> tuple[socket.socket, tuple[str, int]]:
        target = self.upstream_resolver(host, port) if self.upstream_resolver else (host, port)
        sock = socket.create_connection(target, timeout=CONNECT_TIMEOUT)
        sock.settimeout(RELAY_TIMEOUT)
        return sock, target

    def url(self, *, include_credentials: bool = True, redact: bool = False) -> str:
        scheme = "socks5" if self.kind == "socks5" else "http"
        credentials = ""
        if include_credentials:
            username = urllib.parse.quote(self.username, safe="")
            secret = "***" if redact else urllib.parse.quote(self.password, safe="")
            credentials = f"{username}:{secret}@"
        return f"{scheme}://{credentials}{self.host}:{self.port}"

    # -- lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        server = self._server
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # pragma: no cover - defensive shutdown path
                pass
            try:
                server.server_close()
            except Exception:  # pragma: no cover
                pass
        self._close_active()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# HTTP forward proxy listener
# ---------------------------------------------------------------------------

_HOP_BY_HOP = {
    "connection",
    "proxy-connection",
    "proxy-authorization",
    "keep-alive",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "expect",
}


def _relay_sockets(
    client: socket.socket,
    upstream: socket.socket,
    listener: _ListenerBase,
    *,
    label: str,
    timeout: float = RELAY_TIMEOUT,
    max_bytes: int = MAX_RELAY_BYTES,
) -> dict[str, int]:
    """Bidirectional relay that scans client->upstream bytes for tokens."""
    totals = {"client_to_upstream": 0, "upstream_to_client": 0}
    max_token_len = listener.registry.max_token_len
    carry = b""
    open_for_read = {client: True, upstream: True}
    deadline = time.monotonic() + timeout
    listener._register_socket(client, upstream)
    try:
        while any(open_for_read.values()):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                listener.record("relay_timeout", label=label, totals=dict(totals))
                break
            readers = [sock for sock, alive in open_for_read.items() if alive]
            try:
                ready, _, _ = select.select(readers, [], [], min(remaining, 0.5))
            except (OSError, ValueError) as exc:
                listener.record("relay_error", label=label, error=type(exc).__name__)
                break
            if not ready:
                continue
            for source in ready:
                destination = upstream if source is client else client
                try:
                    data = source.recv(65536)
                except (ConnectionError, OSError):
                    open_for_read[source] = False
                    continue
                if not data:
                    open_for_read[source] = False
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                if source is client:
                    text = (carry + data).decode("utf-8", "replace")
                    listener.observe(text, source=f"relay:{label}")
                    carry = (carry + data)[-(max_token_len - 1) :] if max_token_len > 1 else b""
                    totals["client_to_upstream"] += len(data)
                else:
                    totals["upstream_to_client"] += len(data)
                try:
                    destination.sendall(data)
                except OSError:
                    open_for_read[source] = False
                if totals["client_to_upstream"] + totals["upstream_to_client"] > max_bytes:
                    listener.record("relay_limit", label=label, totals=dict(totals))
                    open_for_read = dict.fromkeys(open_for_read, False)
                    break
    finally:
        listener._unregister_socket(client, upstream)
    return totals


class _HTTPProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "avdfleet-g1-http/1.0"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - stdlib name
        pass

    @property
    def listener(self) -> "HTTPProxyListener":
        return self.server.listener  # type: ignore[attr-defined]

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(HANDLER_TIMEOUT)
        self.listener._register_socket(self.connection)

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            self.listener._unregister_socket(self.connection)

    # -- auth --------------------------------------------------------------

    def _authenticate(self) -> bool:
        listener = self.listener
        supplied = self.headers.get("Proxy-Authorization") or self.headers.get("Authorization")
        credentials = _parse_basic_header(supplied)
        if credentials is None:
            reason = "missing_authorization" if not supplied else "malformed_basic_authorization"
            listener.record_auth_failure("basic", "", reason)
            self._send_proxy_auth_required()
            return False
        username, password = credentials
        if listener.matches_credentials(username, password):
            listener.record_auth_success("basic", username)
            return True
        matched = listener.find_credential_owner(username, password)
        listener.record_auth_failure("basic", username, "bad_credentials", matched)
        self._send_proxy_auth_required()
        return False

    def _send_proxy_auth_required(self) -> None:
        body = _json_error("proxy authentication required")
        self.send_response(407)
        self.send_header("Proxy-Authenticate", f'Basic realm="{PROXY_REALM}"')
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def _send_error_json(self, code: int, message: str) -> None:
        body = _json_error(message)
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            self.wfile.flush()
        except OSError:
            pass
        self.close_connection = True

    # -- request helpers ---------------------------------------------------

    def _read_body(self) -> tuple[bytes | None, int]:
        """Return (initial_body, remaining_bytes); None means unsupported encoding."""
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer_encoding:
            return None, 0
        content_length = self.headers.get("Content-Length")
        if not content_length:
            return b"", 0
        try:
            length = int(content_length)
        except ValueError:
            return None, 0
        if length <= 0:
            return b"", 0
        read_length = min(length, self.listener.max_body_bytes)
        body = self.rfile.read(read_length)
        return body, length - len(body)

    def _target(self) -> tuple[str, str, int, str] | None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.scheme in ("http", "https"):
            if not parsed.hostname:
                return None
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            request_path = parsed.path or "/"
            if parsed.query:
                request_path += "?" + parsed.query
            return parsed.scheme, parsed.hostname, port, request_path
        host_header = self.headers.get("Host")
        if not host_header:
            return None
        try:
            host, port = _host_port(host_header, 80)
        except ValueError:
            return None
        return "http", host, port, self.path

    def _upstream_headers(self) -> str:
        listener = self.listener
        lines: list[str] = []
        for key, value in self.headers.items():
            if key.lower() in _HOP_BY_HOP:
                continue
            lines.append(f"{key}: {value}")
        lines.append(f"X-Avdfleet-G1-Listener: {listener.name}")
        lines.append("Connection: close")
        return "\r\n".join(lines) + "\r\n\r\n"

    # -- forwarding --------------------------------------------------------

    def _forward_request(self) -> None:
        listener = self.listener
        try:
            if not self._authenticate():
                return
            body, remaining = self._read_body()
            if body is None:
                listener.record("http_rejected", reason="chunked_request_body_unsupported", target=self.path)
                self._send_error_json(501, "chunked request bodies are not supported by the G1 listener")
                return
            request_text = "\n".join(
                (self.requestline, self.path, str(self.headers), body.decode("utf-8", "replace"))
            )
            listener.observe(request_text, source="http_request")
            target = self._target()
            if target is None:
                listener.record("http_rejected", reason="absolute_uri_required", target=self.path)
                self._send_error_json(400, "HTTP proxy requests must use an absolute URI")
                return
            scheme, host, port, request_path = target
            if scheme == "https":
                listener.record("http_rejected", reason="https_requires_connect", target=self.path)
                self._send_error_json(501, "HTTPS proxy requests must use CONNECT")
                return
            try:
                upstream, upstream_target = listener.upstream_connect(host, port)
            except OSError as exc:
                listener.record(
                    "upstream_error",
                    target=self.path,
                    error=type(exc).__name__,
                )
                self._send_error_json(502, "bad gateway (test origin unreachable)")
                return
            listener.record(
                "http_request",
                method=self.command,
                target=self.path,
                upstream=f"{upstream_target[0]}:{upstream_target[1]}",
            )
            try:
                request_line = f"{self.command} {request_path} HTTP/1.1\r\n"
                upstream.sendall((request_line + self._upstream_headers()).encode("latin-1", "replace"))
                if body:
                    upstream.sendall(body)
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    listener.observe(chunk.decode("utf-8", "replace"), source="http_body")
                    upstream.sendall(chunk)
                    remaining -= len(chunk)
                head, leftover = _recv_until(upstream, b"\r\n\r\n", MAX_HEADER_BYTES)
                if not head:
                    listener.record("upstream_error", target=self.path, error="empty_response")
                    self._send_error_json(502, "bad gateway (empty response)")
                    return
                self.wfile.write(head)
                if leftover:
                    self.wfile.write(leftover)
                body_bytes = len(leftover)
                while True:
                    chunk = upstream.recv(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    body_bytes += len(chunk)
                    if body_bytes > MAX_RELAY_BYTES:
                        listener.record("relay_limit", label="http_response", bytes=body_bytes)
                        break
                self.wfile.flush()
                listener.record(
                    "http_response",
                    target=self.path,
                    status_line=head.split(b"\r\n", 1)[0].decode("latin-1", "replace"),
                    body_bytes=body_bytes,
                )
            finally:
                _close_quiet(upstream)
        except (BrokenPipeError, ConnectionResetError):
            listener.record("client_disconnect", phase="http_forward", target=self.path)
        except (OSError, ValueError) as exc:
            listener.record("error", phase="http_forward", error=f"{type(exc).__name__}: {exc}")
            self._send_error_json(502, "bad gateway")
        finally:
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        self._forward_request()

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib name
        self._forward_request()

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        self._forward_request()

    def do_PUT(self) -> None:  # noqa: N802 - stdlib name
        self._forward_request()

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib name
        self._forward_request()

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib name
        self._forward_request()

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib name
        self._forward_request()

    # -- CONNECT tunnel ----------------------------------------------------

    def do_CONNECT(self) -> None:  # noqa: N802 - stdlib name
        listener = self.listener
        try:
            if not self._authenticate():
                return
            authority = self.path
            listener.observe(self.requestline, source="connect_authority", detail=authority)
            try:
                host, port = _host_port(authority, 443)
            except ValueError:
                listener.record("http_rejected", reason="malformed_connect_authority", authority=authority)
                self._send_error_json(400, "malformed CONNECT authority")
                return
            try:
                upstream, upstream_target = listener.upstream_connect(host, port)
            except OSError as exc:
                listener.record("upstream_error", phase="connect", authority=authority, error=type(exc).__name__)
                self._send_error_json(502, "bad gateway (test origin unreachable)")
                return
            listener.record(
                "http_connect",
                authority=authority,
                upstream=f"{upstream_target[0]}:{upstream_target[1]}",
            )
            try:
                self.send_response(200, "Connection Established")
                self.send_header("Proxy-Agent", "avdfleet-g1")
                self.end_headers()
                self.wfile.flush()
                totals = _relay_sockets(self.connection, upstream, listener, label="connect")
                listener.record("relay_closed", label="connect", totals=dict(totals))
            finally:
                _close_quiet(upstream)
        except (BrokenPipeError, ConnectionResetError):
            listener.record("client_disconnect", phase="connect")
        except (OSError, ValueError) as exc:
            listener.record("error", phase="connect", error=f"{type(exc).__name__}: {exc}")
        finally:
            self.close_connection = True


class HTTPProxyListener(_ListenerBase):
    kind = "http"

    def start(self) -> "HTTPProxyListener":
        server = _ThreadingHTTPServer((self.host, self.port), _HTTPProxyHandler)
        server.listener = self  # type: ignore[attr-defined]
        self._server = server
        self.port = int(server.server_address[1])
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name=f"g1-{self.name}-http",
            daemon=True,
        )
        self._thread.start()
        return self


# ---------------------------------------------------------------------------
# SOCKS5 proxy listener (RFC 1928 CONNECT + RFC 1929 auth)
# ---------------------------------------------------------------------------


class _SOCKS5Handler(socketserver.StreamRequestHandler):
    def setup(self) -> None:
        super().setup()
        self.request.settimeout(HANDLER_TIMEOUT)
        self.server.listener._register_socket(self.request)  # type: ignore[attr-defined]

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            self.server.listener._unregister_socket(self.request)  # type: ignore[attr-defined]

    def handle(self) -> None:
        listener: SOCKS5ProxyListener = self.server.listener  # type: ignore[attr-defined]
        try:
            self._serve(listener)
        except (ConnectionError, socket.timeout):
            listener.record("client_disconnect", phase="socks5")
        except (OSError, ValueError) as exc:
            listener.record("error", phase="socks5", error=f"{type(exc).__name__}: {exc}")

    def _send_reply(self, code: int, *, address: bytes = b"\x00\x00\x00\x00", port: int = 0) -> None:
        self.request.sendall(b"\x05" + bytes([code]) + b"\x00\x01" + address + struct.pack("!H", port))

    def _read_address(self, atyp: int) -> tuple[str, int]:
        if atyp == 0x01:
            host = socket.inet_ntoa(_recv_exact(self.request, 4))
        elif atyp == 0x03:
            length = _recv_exact(self.request, 1)[0]
            host = _recv_exact(self.request, length).decode("ascii", "replace")
        elif atyp == 0x04:
            host = socket.inet_ntop(socket.AF_INET6, _recv_exact(self.request, 16))
        else:
            raise ValueError(f"unsupported SOCKS5 address type {atyp}")
        port = struct.unpack("!H", _recv_exact(self.request, 2))[0]
        return host, port

    def _serve(self, listener: "SOCKS5ProxyListener") -> None:
        version, method_count = _recv_exact(self.request, 2)
        methods = list(_recv_exact(self.request, method_count))
        listener.record("socks_hello", version=version, methods=methods, auth_offered=0x02 in methods)
        if version != 5:
            listener.record("socks_rejected", reason=f"bad_version_{version}")
            return
        if 0x02 not in methods:
            listener.record("socks_rejected", reason="username_password_not_offered")
            self.request.sendall(b"\x05\xff")
            return
        self.request.sendall(b"\x05\x02")
        auth_version, username_length = _recv_exact(self.request, 2)
        username = _recv_exact(self.request, username_length).decode("utf-8", "replace")
        password_length = _recv_exact(self.request, 1)[0]
        password = _recv_exact(self.request, password_length).decode("utf-8", "replace")
        if auth_version != 1:
            listener.record_auth_failure("socks5", username, f"bad_auth_version_{auth_version}")
            self.request.sendall(b"\x01\x01")
            return
        if not listener.matches_credentials(username, password):
            matched = listener.find_credential_owner(username, password)
            listener.record_auth_failure("socks5", username, "bad_credentials", matched)
            self.request.sendall(b"\x01\x01")
            return
        listener.record_auth_success("socks5", username)
        listener.observe(f"{username}\n{password}", source="socks_credentials")
        self.request.sendall(b"\x01\x00")

        version, command, _reserved, address_type = _recv_exact(self.request, 4)
        if version != 5:
            listener.record("socks_rejected", reason=f"bad_request_version_{version}")
            self._send_reply(0x07)
            return
        host, port = self._read_address(address_type)
        listener.record("socks_request", command=command, host=host, port=port)
        if command != 0x01:
            reason = "udp_associate_attempt" if command == 0x03 else f"command_{command}"
            listener.record("socks_rejected", reason=reason, host=host, port=port)
            self._send_reply(0x07)
            return
        listener.observe(host, source="socks_dest")
        try:
            upstream, upstream_target = listener.upstream_connect(host, port)
        except OSError as exc:
            listener.record("upstream_error", phase="socks5", host=host, port=port, error=type(exc).__name__)
            self._send_reply(0x05)
            return
        self._send_reply(0x00)
        listener.record(
            "socks_connected",
            host=host,
            port=port,
            upstream=f"{upstream_target[0]}:{upstream_target[1]}",
        )
        try:
            totals = _relay_sockets(self.request, upstream, listener, label="socks5")
            listener.record("relay_closed", label="socks5", totals=dict(totals))
        finally:
            _close_quiet(upstream)


class SOCKS5ProxyListener(_ListenerBase):
    kind = "socks5"

    def start(self) -> "SOCKS5ProxyListener":
        server = _ThreadingTCPServer((self.host, self.port), _SOCKS5Handler)
        server.listener = self  # type: ignore[attr-defined]
        self._server = server
        self.port = int(server.server_address[1])
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name=f"g1-{self.name}-socks5",
            daemon=True,
        )
        self._thread.start()
        return self


# ---------------------------------------------------------------------------
# Harness: five listeners + origin + generated fixtures
# ---------------------------------------------------------------------------


def _normalize_specs(specs: Sequence[Any] | Mapping[str, str] | None) -> list[dict[str, Any]]:
    if specs is None:
        specs = DEFAULT_SPECS
    if isinstance(specs, Mapping):
        raw_specs: list[Any] = list(specs.items())
    else:
        raw_specs = list(specs)
    normalized: list[dict[str, Any]] = []
    for item in raw_specs:
        if isinstance(item, Mapping):
            entry = dict(item)
            name = str(entry.get("name", ""))
            kind = str(entry.get("kind", "http")).lower()
        else:
            name, kind = item
            entry = {}
        kind = str(kind).lower()
        if kind not in {"http", "socks5"}:
            raise ValueError(f"listener {name!r}: unsupported kind {kind!r}")
        if not name:
            raise ValueError("listener name must be non-empty")
        normalized.append(
            {
                "name": str(name),
                "kind": kind,
                "port": int(entry.get("port", 0) or 0),
                "username": entry.get("username"),
                "password": entry.get("password"),
                "token": entry.get("token"),
            }
        )
    names = [entry["name"] for entry in normalized]
    if len(names) != len(set(names)):
        raise ValueError(f"listener names must be unique: {names}")
    return normalized


class ListenerHarness:
    """Five authenticated loopback listeners plus a hermetic echo origin.

    Fixtures (tokens and generated credentials) are available to tests through
    ``tokens`` / ``credentials``; printed and exported output is redacted by default.
    """

    def __init__(
        self,
        specs: Sequence[Any] | Mapping[str, str] | None = None,
        *,
        bind: str = LOOPBACK_HOST,
        tls: bool = True,
        resolve_override: bool = True,
        base_domain: str = DEFAULT_BASE_DOMAIN,
        capacity: int = MAX_EVENTS,
        max_body_bytes: int = MAX_BODY_BYTES,
    ) -> None:
        if not _is_loopback(bind):
            raise ValueError("the G1 harness must bind to loopback only (got %r)" % (bind,))
        self.bind = _ipv4_loopback(bind)
        self.base_domain = base_domain.strip(".") or DEFAULT_BASE_DOMAIN
        self.resolve_override = resolve_override
        self.capacity = capacity
        self.max_body_bytes = max_body_bytes
        self.specs = _normalize_specs(specs)
        self.tokens: dict[str, str] = {}
        self.credentials: dict[str, tuple[str, str]] = {}
        self.registry = TokenRegistry()
        self.redactor = Redactor()
        for entry in self.specs:
            name = entry["name"]
            token = entry["token"] or ("g1t-" + secrets.token_hex(12))
            username = entry["username"] or ("g1u-" + secrets.token_hex(4))
            password = entry["password"] or ("g1p-" + secrets.token_hex(10))
            self.tokens[name] = token
            self.credentials[name] = (username, password)
            self.registry.register(name, token)
            self.redactor.add_token(name, token)
            self.redactor.add_credentials(username, password)
        self.origin: EchoOrigin | None = None
        self.listeners: dict[str, _ListenerBase] = {}
        self.tls_material: TlsMaterial | None = None
        self.tls_note = ""
        self._started = False
        self._wanted_tls = tls

    # -- construction ------------------------------------------------------

    @classmethod
    def standard(cls, **kwargs: Any) -> "ListenerHarness":
        return cls(DEFAULT_SPECS, **kwargs)

    def start(self) -> "ListenerHarness":
        if self._started:
            return self
        if self._wanted_tls:
            try:
                self.tls_material = TlsMaterial()
            except (OSError, ValueError, ssl.SSLError) as exc:
                self.tls_note = f"TLS disabled: {type(exc).__name__}: {exc}"
        self.origin = EchoOrigin(
            self.bind,
            tls_material=self.tls_material,
            redactor=self.redactor,
            capacity=self.capacity,
            max_body_bytes=self.max_body_bytes,
        )
        if self.tls_material is not None and self.origin.tls_error:
            self.tls_note = f"TLS disabled: {self.origin.tls_error}"
        created: list[_ListenerBase] = []
        try:
            for entry in self.specs:
                name = entry["name"]
                kind = entry["kind"]
                username, password = self.credentials[name]
                listener_class = HTTPProxyListener if kind == "http" else SOCKS5ProxyListener
                listener = listener_class(
                    name=name,
                    host=self.bind,
                    port=entry["port"],
                    username=username,
                    password=password,
                    token=self.tokens[name],
                    registry=self.registry,
                    redactor=self.redactor,
                    upstream_resolver=self._resolve_upstream,
                    credential_lookup=self._find_credential_owner,
                    capacity=self.capacity,
                    max_body_bytes=self.max_body_bytes,
                )
                listener.start()
                self.listeners[name] = listener
                created.append(listener)
        except Exception:
            for listener in created:
                listener.stop()
            if self.origin is not None:
                self.origin.stop()
            if self.tls_material is not None:
                self.tls_material.close()
            raise
        self._started = True
        return self

    # -- helpers -----------------------------------------------------------

    def _resolve_upstream(self, host: str, port: int) -> tuple[str, int]:
        origin = self.origin
        if origin is None or not self.resolve_override:
            return host, port
        if _is_loopback(host) and port in {origin.http_port, origin.https_port}:
            return host, port
        if port == 443 and origin.https_port is not None:
            return self.bind, origin.https_port
        return self.bind, origin.http_port

    def _find_credential_owner(self, username: str, password: str) -> str | None:
        for name, (expected_user, expected_password) in self.credentials.items():
            if _digest_equal(username, expected_user) and _digest_equal(password, expected_password):
                return name
        return None

    def proxy_url(self, name: str, *, redact: bool = False) -> str:
        return self.listeners[name].url(include_credentials=True, redact=redact)

    def basic_authorization(self, name: str) -> str:
        username, password = self.credentials[name]
        return _basic_header(username, password)

    def token_url(self, name: str, *, base_domain: str | None = None) -> str:
        token = self.tokens[name]
        domain = (base_domain or self.base_domain).strip(".") or DEFAULT_BASE_DOMAIN
        host = f"{token}.{domain}"
        return (
            f"http://{host}{DEFAULT_ORIGIN_PATH}?probe={urllib.parse.quote(name, safe='')}"
            f"&token={urllib.parse.quote(token, safe='')}"
        )

    def probe_url_for(self, name: str, *, redact_tokens: bool = True) -> str:
        if not redact_tokens:
            return self.token_url(name)
        return (
            f"http://<token:{name}>.{self.base_domain}{DEFAULT_ORIGIN_PATH}"
            f"?probe={urllib.parse.quote(name, safe='')}&token=<token:{name}>"
        )

    def describe(self, *, redact: bool = True, show_tokens: bool = False) -> str:
        lines = [
            "AVD Fleet G1 test-listener harness -- LOCAL TEST FIXTURES ONLY",
            f"bind={self.bind} listeners={len(self.listeners)} "
            f"origin_http={self.origin.http_url if self.origin else 'not-started'} "
            f"origin_https={self.origin.https_url if self.origin and self.origin.https_url else 'disabled'}",
        ]
        for name, listener in self.listeners.items():
            lines.append(f"  {name}  {listener.kind:6s}  {listener.url(include_credentials=True, redact=redact)}")
        lines.append("probe URLs (guest GETs these; each contains that listener's token):")
        for name in self.listeners:
            lines.append(f"  {name}  {self.probe_url_for(name, redact_tokens=not show_tokens)}")
        if self.tls_note:
            lines.append(f"note: {self.tls_note}")
        return "\n".join(lines)

    # -- logs and reports --------------------------------------------------

    def listener_logs(self) -> dict[str, list[dict[str, Any]]]:
        """Raw (privileged) per-listener events; callers must not publish them."""
        return {name: listener.events() for name, listener in self.listeners.items()}

    def logs(self) -> dict[str, list[dict[str, Any]]]:
        return self.listener_logs()

    def redacted_logs(self) -> dict[str, list[dict[str, Any]]]:
        return {name: listener.redacted_events() for name, listener in self.listeners.items()}

    def origin_logs(self) -> list[dict[str, Any]]:
        return self.origin.events() if self.origin is not None else []

    def redacted_origin_logs(self) -> list[dict[str, Any]]:
        return self.origin.redacted_events() if self.origin is not None else []

    def verify(self) -> dict[str, Any]:
        return verify_isolation(
            self.tokens,
            self.listener_logs(),
            credentials=self.credentials,
            origin_logs=self.origin_logs(),
        )

    # -- evidence export ---------------------------------------------------

    def export_evidence(self, directory: str | os.PathLike[str], *, include_raw: bool = True) -> dict[str, Any]:
        """Write redacted + optionally raw evidence for gate G1.

        Filenames containing ``SENSITIVE`` hold raw tokens and must never be
        committed; they are chmod 0600 where the platform supports it.
        """
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        files: dict[str, str] = {}

        def write_json(filename: str, payload: Any) -> None:
            path = root / filename
            path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            files[filename] = str(path)

        def write_jsonl(filename: str, rows: Iterable[Mapping[str, Any]], *, secure: bool = False) -> None:
            path = root / filename
            with path.open("w", encoding="utf-8", newline="\n") as handle:
                for row in rows:
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
            if secure:
                _chmod_private(path)
            files[filename] = str(path)

        metadata = {
            "schema": "avdfleet.g1.listeners/v1",
            "generated_at": _iso_now(),
            "bind": self.bind,
            "base_domain": self.base_domain,
            "tls_enabled": bool(self.origin is not None and self.origin.https_port is not None),
            "tls_note": self.tls_note,
            "origin": {
                "http_url": self.origin.http_url if self.origin else None,
                "https_url": self.origin.https_url if self.origin else None,
            },
            "listeners": [
                {
                    "name": name,
                    "kind": listener.kind,
                    "host": listener.host,
                    "port": listener.port,
                    "username": listener.username,
                    "url_redacted": listener.url(include_credentials=True, redact=True),
                    "token_placeholder": f"<token:{name}>",
                }
                for name, listener in self.listeners.items()
            ],
        }
        write_json("listeners.json", metadata)
        write_jsonl("listeners.redacted.jsonl", (event for events in self.redacted_logs().values() for event in events))
        write_jsonl("origin.redacted.jsonl", self.redacted_origin_logs())
        report = self.verify()
        write_json("verify-isolation.json", report)
        if include_raw:
            write_jsonl(
                "listeners.raw.SENSITIVE.jsonl",
                (event for events in self.listener_logs().values() for event in events),
                secure=True,
            )
            write_jsonl("origin.raw.SENSITIVE.jsonl", self.origin_logs(), secure=True)
        checksums: list[dict[str, str]] = []
        for filename in sorted(files):
            digest = hashlib.sha256((root / filename).read_bytes()).hexdigest()
            checksums.append({"file": filename, "sha256": digest})
        write_json("checksums.json", {"generated_at": _iso_now(), "files": checksums})
        return {
            "directory": str(root),
            "files": sorted(files.values()),
            "include_raw": include_raw,
            "tls_enabled": metadata["tls_enabled"],
        }

    # -- lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        for listener in self.listeners.values():
            listener.stop()
        self.listeners.clear()
        if self.origin is not None:
            self.origin.stop()
            self.origin = None
        if self.tls_material is not None:
            self.tls_material.close()
            self.tls_material = None
        self._started = False

    def __enter__(self) -> "ListenerHarness":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# verify_isolation: the G1 pass/fail report
# ---------------------------------------------------------------------------


def _coerce_tokens(
    tokens: Any,
) -> tuple[dict[str, str] | None, dict[str, str], list[str], str]:
    """Return (expected_by_listener, raw_token_to_listener, token_values, mode)."""
    if isinstance(tokens, Mapping):
        expected = {str(name): str(value) for name, value in tokens.items()}
        index: dict[str, str] = {}
        for name, raw in expected.items():
            index.setdefault(raw, name)
        return expected, index, [], "mapping"
    sequence = list(tokens or [])
    if sequence and all(isinstance(item, (tuple, list)) and len(item) == 2 for item in sequence):
        expected = {str(item[0]): str(item[1]) for item in sequence}
        index = {}
        for name, raw in expected.items():
            index.setdefault(raw, name)
        return expected, index, [], "mapping"
    if all(isinstance(item, str) for item in sequence):
        return None, {}, sequence, "derived"
    raise TypeError("tokens must be a mapping listener->token, a sequence of (listener, token), or a sequence of tokens")


def _normalize_log_buckets(listener_logs: Any) -> list[tuple[str, list[Any]]]:
    if listener_logs is None:
        return []
    if isinstance(listener_logs, Mapping):
        return [(str(name), list(events or [])) for name, events in listener_logs.items()]
    buckets: dict[str, list[Any]] = {}
    for event in listener_logs:
        if isinstance(event, Mapping):
            listener = str(event.get("listener", "<unknown>"))
            buckets.setdefault(listener, []).append(event)
    return list(buckets.items())


def verify_isolation(
    tokens: Any,
    listener_logs: Any,
    *,
    credentials: Mapping[str, tuple[str, str]] | None = None,
    origin_logs: Any = None,
    require_all: bool = True,
) -> dict[str, Any]:
    """Build a G1 isolation pass/fail report from tokens and listener logs.

    Parameters
    ----------
    tokens:
        Canonical form is ``{listener_name: raw_token}``.  A sequence of
        ``(listener_name, token)`` pairs is also accepted.  A bare sequence of tokens
        derives ownership from the observations and asserts each token appears on
        exactly one listener (use the mapping form for the real gate).
    listener_logs:
        ``{listener_name: [events]}`` as returned by ``harness.listener_logs()``, or a
        flat iterable of event dicts.  Token-sighting events need either the raw
        ``token`` value or the assertion-safe ``token_name`` field; redacted logs
        (``harness.redacted_logs()``) work because ``token_name`` is preserved.
        Simple strings inside a bucket are treated as observed token values.
    credentials:
        Optional ``{listener_name: (username, password)}``; the report then flags a
        listener's credential fingerprint appearing on another listener.
    origin_logs:
        Optional echo-origin events.  When present and unredacted, a token in the
        origin path/query without the matching ``X-Avdfleet-G1-Listener`` attribution
        is flagged as a bypass (plain forward-proxy requests only; CONNECT payloads
        cannot carry the attribution header).

    Returns
    -------
    dict
        JSON-serializable report: ``passed``, ``checks``, ``failures``,
        ``observations`` (token name -> listener -> count) and metadata.  The report
        never contains raw token or password values.
    """
    expected, token_index, token_values, mode = _coerce_tokens(tokens)
    failures: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    notes: list[str] = []

    def add_failure(code: str, **details: Any) -> None:
        entry: dict[str, Any] = {"code": code}
        entry.update(details)
        failures.append(entry)

    buckets = _normalize_log_buckets(listener_logs)
    mapping_input = isinstance(listener_logs, Mapping)
    bucket_names = {name for name, _ in buckets}

    observations: dict[tuple[str, str], int] = {}
    auth_events: list[tuple[str, str]] = []
    seen_listeners: set[str] = set()

    derived_raw_labels: dict[str, str] = {}
    if mode == "derived":
        for index, raw in enumerate(dict.fromkeys(token_values)):
            derived_raw_labels[raw] = f"T{index + 1}"

    def resolve_mapping(token_name: Any, raw_token: Any) -> str | None:
        assert expected is not None
        if token_name is not None and str(token_name) in expected:
            return str(token_name)
        if raw_token is not None:
            raw = str(raw_token)
            if raw in token_index:
                return token_index[raw]
            if raw not in {"", "<redacted>", "***"}:
                return f"<unknown:{_sha16(raw)}>"
        if token_name is not None and str(token_name):
            return f"<unknown-name:{_sha16(str(token_name))}>"
        return None

    def resolve_derived(raw_token: Any) -> str | None:
        if raw_token is None:
            return None
        raw = str(raw_token)
        if raw in derived_raw_labels:
            return derived_raw_labels[raw]
        if raw in {"", "<redacted>", "***"}:
            return None
        return f"<unknown:{_sha16(raw)}>"

    for bucket_listener, events in buckets:
        for event in events:
            if isinstance(event, str):
                token_id = resolve_mapping(None, event) if mode == "mapping" else resolve_derived(event)
                if token_id:
                    observations[(token_id, bucket_listener)] = observations.get((token_id, bucket_listener), 0) + 1
                    seen_listeners.add(bucket_listener)
                continue
            if not isinstance(event, Mapping):
                continue
            listener = str(event.get("listener") or bucket_listener or "<unknown>")
            token_name = event.get("token_name") or event.get("assigned_listener")
            raw_token = event.get("token")
            event_name = event.get("event")
            if event_name == "token_sighting" or (raw_token is not None and token_name is not None):
                token_id = resolve_mapping(token_name, raw_token) if mode == "mapping" else resolve_derived(raw_token)
                if token_id:
                    observations[(token_id, listener)] = observations.get((token_id, listener), 0) + 1
                    seen_listeners.add(listener)
            elif event_name in {"auth_ok", "auth_failure", "socks_auth_ok", "socks_auth_failure"} or "cred_fp" in event:
                fingerprint = event.get("cred_fp")
                if fingerprint:
                    auth_events.append((listener, str(fingerprint)))

    # In mapping mode ownership comes from the caller; in derived mode each token
    # value must be observed on exactly one listener.
    expected_raw: dict[str, str] = {}
    if mode == "derived":
        label_to_raw = {label: raw for raw, label in derived_raw_labels.items()}
        derived_expected: dict[str, str] = {}
        for raw, label in derived_raw_labels.items():
            listeners = sorted({listener for (token_id, listener) in observations if token_id == label})
            if not listeners:
                if require_all:
                    add_failure("token_not_observed", token=label)
                continue
            if len(listeners) > 1:
                add_failure("token_seen_on_multiple_listeners", token=label, listeners=listeners)
                continue
            derived_expected[listeners[0]] = label
        for listener, label in derived_expected.items():
            expected_raw[listener] = label_to_raw[label]
        for (token_id, listener), count in sorted(observations.items()):
            if token_id.startswith("<unknown"):
                add_failure("unknown_token_sighting", listener=listener, token=token_id, count=count)
        owners: dict[str, list[str]] = {}
        for listener, label in derived_expected.items():
            owners.setdefault(listener, []).append(label)
        for listener, labels in owners.items():
            if len(labels) > 1:
                add_failure("multiple_tokens_on_listener", listener=listener, tokens=sorted(labels))
        expected = derived_expected
    else:
        expected = expected or {}
        if expected:
            raw_values = list(expected.values())
            if len(set(raw_values)) != len(raw_values):
                add_failure("duplicate_token_values")
            if mapping_input and require_all:
                for name in expected:
                    if name not in bucket_names:
                        add_failure("missing_listener_log", listener=name)
            if require_all:
                for token_id in expected:
                    own = sum(
                        count
                        for (seen_token, seen_listener), count in observations.items()
                        if seen_token == token_id and seen_listener == token_id
                    )
                    if own == 0:
                        add_failure("missing_own_sighting", token=token_id)
            for (token_id, listener), count in sorted(observations.items()):
                if token_id not in expected:
                    add_failure("unknown_token_sighting", listener=listener, token=token_id, count=count)
                    continue
                if listener != token_id:
                    add_failure(
                        "token_on_wrong_listener",
                        token=token_id,
                        listener=listener,
                        count=count,
                    )
            expected_raw = {name: raw for name, raw in expected.items() if raw}

    if not expected:
        add_failure("no_tokens_configured")

    if credentials:
        credential_owner: dict[str, str] = {}
        for name, pair in dict(credentials).items():
            try:
                username, password = pair
            except (TypeError, ValueError):
                notes.append(f"credential entry {name!r} ignored: expected (username, password)")
                continue
            credential_owner[_cred_fingerprint(str(username), str(password))] = str(name)
        for listener, fingerprint in auth_events:
            owner = credential_owner.get(fingerprint)
            if owner is not None and owner != listener:
                add_failure("credential_cross_listener", credential_owner=owner, listener=listener)

    if origin_logs is not None and expected_raw:
        rows = _normalize_log_buckets(origin_logs) if isinstance(origin_logs, Mapping) else list(origin_logs or [])
        origin_checked = 0
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            text = f"{row.get('path', '')}?{row.get('query', '')}"
            via = str(row.get("via_listener") or "")
            for listener_name, raw in expected_raw.items():
                if not raw or raw in {"<redacted>", "***"} or raw not in text:
                    continue
                origin_checked += 1
                if not via:
                    add_failure(
                        "origin_unattributed", token=listener_name, origin_path=str(row.get("path", ""))
                    )
                elif via != listener_name:
                    add_failure("origin_wrong_listener", token=listener_name, via_listener=via)
        if origin_checked == 0 and any(str(row.get("path", "")) for row in rows if isinstance(row, Mapping)):
            notes.append("origin attribution check skipped (origin logs redacted or no token in path/query)")

    if not observations:
        add_failure("no_token_sightings")

    observation_view: dict[str, dict[str, int]] = {}
    for (token_id, listener), count in sorted(observations.items()):
        observation_view.setdefault(str(token_id), {})[str(listener)] = count

    def has_failure(*codes: str) -> bool:
        return any(failure["code"] in codes for failure in failures)

    checks = [
        {
            "name": "tokens_configured",
            "passed": not has_failure("no_tokens_configured", "duplicate_token_values"),
            "detail": f"{len(expected or {})} assigned token(s), mode={mode}",
        },
        {
            "name": "every_token_observed_on_own_listener",
            "passed": not has_failure("missing_own_sighting", "token_not_observed", "missing_listener_log"),
            "detail": "each assigned token seen at least once on its listener",
        },
        {
            "name": "no_token_on_wrong_listener",
            "passed": not has_failure("token_on_wrong_listener"),
            "detail": "all token sightings are attributable to the assigned listener",
        },
        {
            "name": "all_sightings_attributed",
            "passed": not has_failure("unknown_token_sighting", "token_seen_on_multiple_listeners", "multiple_tokens_on_listener"),
            "detail": "no unknown/unowned token sightings",
        },
        {
            "name": "credentials_isolated",
            "passed": not has_failure("credential_cross_listener"),
            "detail": "no listener credential fingerprint appeared on another listener",
        },
        {
            "name": "origin_requests_attributed",
            "passed": not has_failure("origin_unattributed", "origin_wrong_listener"),
            "detail": "token-bearing origin requests carry the assigned listener attribution",
        },
        {
            "name": "sightings_present",
            "passed": bool(observations),
            "detail": f"{sum(observations.values())} token sightings observed",
        },
    ]

    report: dict[str, Any] = {
        "schema": "avdfleet.g1.verify-isolation/v1",
        "generated_at": _iso_now(),
        "passed": not failures and all(check["passed"] for check in checks),
        "mode": mode,
        "tokens_checked": len(expected or {}),
        "listeners": sorted(set(expected or {}) | seen_listeners),
        "observations": observation_view,
        "checks": checks,
        "failures": failures,
        "notes": notes,
    }

    # Final safety: the report itself must not contain raw token values.
    serialized = json.dumps(report, sort_keys=True)
    leaked = [
        raw
        for raw in expected_raw.values()
        if raw and raw not in {"<redacted>", "***"} and raw in serialized
    ]
    if leaked:
        report["failures"].append({"code": "report_contains_raw_token", "count": len(leaked)})
        report["passed"] = False
    return report


# ---------------------------------------------------------------------------
# Self-test client helpers
# ---------------------------------------------------------------------------


class Socks5Error(RuntimeError):
    def __init__(self, message: str, *, stage: str) -> None:
        super().__init__(message)
        self.stage = stage


def _http_exchange(host: str, port: int, request: bytes, timeout: float = 10.0) -> bytes:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(request)
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = sock.recv(65536)
            except (socket.timeout, ConnectionError, OSError):
                break
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_RELAY_BYTES:
                break
        return b"".join(chunks)


def _read_all(sock: socket.socket, timeout: float = 10.0) -> bytes:
    sock.settimeout(timeout)
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = sock.recv(65536)
        except (socket.timeout, ConnectionError, OSError):
            break
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_RELAY_BYTES:
            break
    return b"".join(chunks)


def _open_connect_tunnel(
    host: str,
    port: int,
    authorization: str,
    authority: str,
    timeout: float = 10.0,
) -> tuple[socket.socket, int]:
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        request = (
            f"CONNECT {authority} HTTP/1.1\r\n"
            f"Host: {authority}\r\n"
            f"Proxy-Authorization: {authorization}\r\n\r\n"
        ).encode("latin-1")
        sock.sendall(request)
        head, leftover = _recv_until(sock, b"\r\n\r\n", MAX_HEADER_BYTES)
        status, _headers, _body = _split_http_response(head)
        if leftover:
            raise RuntimeError("unexpected bytes after CONNECT response headers")
        return sock, status
    except Exception:
        _close_quiet(sock)
        raise


def _socks5_negotiate(
    host: str,
    port: int,
    username: str,
    password: str,
    dest_host: str,
    dest_port: int,
    *,
    timeout: float = 10.0,
) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall(b"\x05\x02\x00\x02")
        version, method = _recv_exact(sock, 2)
        if version != 5:
            raise Socks5Error(f"bad greeting version {version}", stage="greeting")
        if method == 0xFF:
            raise Socks5Error("server rejected all auth methods", stage="greeting")
        if method != 0x02:
            raise Socks5Error(f"server selected unexpected method {method}", stage="greeting")
        username_bytes = username.encode("utf-8")
        password_bytes = password.encode("utf-8")
        sock.sendall(
            b"\x01"
            + bytes([len(username_bytes)])
            + username_bytes
            + bytes([len(password_bytes)])
            + password_bytes
        )
        auth_version, status = _recv_exact(sock, 2)
        if auth_version != 1 or status != 0:
            raise Socks5Error("SOCKS5 username/password auth rejected", stage="auth")
        dest_bytes = dest_host.encode("ascii")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(dest_bytes)]) + dest_bytes + struct.pack("!H", dest_port))
        reply_version, reply_code, _reserved, address_type = _recv_exact(sock, 4)
        if reply_version != 5 or reply_code != 0:
            raise Socks5Error(f"CONNECT rejected (code {reply_code})", stage="request")
        if address_type == 0x01:
            _recv_exact(sock, 4 + 2)
        elif address_type == 0x03:
            length = _recv_exact(sock, 1)[0]
            _recv_exact(sock, length + 2)
        elif address_type == 0x04:
            _recv_exact(sock, 16 + 2)
        else:
            raise Socks5Error(f"unexpected address type {address_type}", stage="request")
        return sock
    except Exception:
        _close_quiet(sock)
        raise


def _socks5_offer_no_auth(host: str, port: int, *, timeout: float = 10.0) -> int:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(b"\x05\x01\x00")
        _version, method = _recv_exact(sock, 2)
        return method


def _socks5_udp_associate(host: str, port: int, username: str, password: str, *, timeout: float = 10.0) -> int:
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall(b"\x05\x02\x00\x02")
        _version, method = _recv_exact(sock, 2)
        if method != 0x02:
            return 0xFF
        username_bytes = username.encode("utf-8")
        password_bytes = password.encode("utf-8")
        sock.sendall(
            b"\x01"
            + bytes([len(username_bytes)])
            + username_bytes
            + bytes([len(password_bytes)])
            + password_bytes
        )
        _auth_version, status = _recv_exact(sock, 2)
        if status != 0:
            return 0xFE
        sock.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")
        _version, reply_code, _reserved, _atyp = _recv_exact(sock, 4)
        return reply_code
    finally:
        _close_quiet(sock)


def _count_sightings(listener: _ListenerBase, token_name: str | None = None) -> int:
    count = 0
    for event in listener.events():
        if event.get("event") != "token_sighting":
            continue
        if token_name is None or event.get("token_name") == token_name:
            count += 1
    return count


def _has_event(listener: _ListenerBase, event_name: str) -> bool:
    return any(event.get("event") == event_name for event in listener.events())


def _inner_get(host_header: str, path: str) -> bytes:
    return (
        f"GET {path} HTTP/1.1\r\nHost: {host_header}\r\nConnection: close\r\n\r\n"
    ).encode("latin-1")


def _check(checks: list[dict[str, Any]], name: str, passed: bool, detail: str = "", skipped: bool = False) -> None:
    checks.append(
        {
            "name": name,
            "passed": bool(passed) or skipped,
            "skipped": bool(skipped),
            "detail": detail,
        }
    )


def _banner(harness: ListenerHarness, *, redact: bool = False, show_tokens: bool = False) -> None:
    print("=" * 78)
    print("AVD Fleet G1 test-listener harness -- LOCAL TEST FIXTURES ONLY")
    print("=" * 78)
    print(f"bind={harness.bind}  listeners={len(harness.listeners)}")
    origin = harness.origin
    if origin is not None:
        print(f"origin (HTTP) : {origin.http_url}")
        print(f"origin (HTTPS): {origin.https_url or '(disabled: ' + (harness.tls_note or '--no-tls') + ')'}")
    print("")
    print("Listener URLs (ephemeral credentials; use ONLY for G1 test traffic):")
    for name, listener in harness.listeners.items():
        print(f"  {name}  {listener.kind:6s}  {listener.url(include_credentials=True, redact=redact)}")
    print("")
    print("Probe URLs (the guest GETs these; each contains that listener's token):")
    for name in harness.listeners:
        print(f"  {name}  {harness.probe_url_for(name, redact_tokens=not show_tokens)}")
    print("")
    if show_tokens:
        print("RAW TOKENS (test-only; do not paste into shared logs):")
        for name, token in harness.tokens.items():
            print(f"  {name}  {token}")
        print("")
    print("Tokens are redacted in normal output and in exported evidence; read")
    print("harness.tokens in-process for assertions. Never reuse these fixtures.")
    print("=" * 78)
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def run_selftest(
    *,
    bind: str = LOOPBACK_HOST,
    tls: bool = True,
    timeout: float = 10.0,
    verbose: bool = False,
    show_banner: bool = True,
    redact: bool = False,
    show_tokens: bool = False,
    keep: bool = False,
) -> dict[str, Any]:
    """Start five listeners and self-test every channel plus verify_isolation."""
    checks: list[dict[str, Any]] = []
    harness = ListenerHarness.standard(bind=bind, tls=tls)
    try:
        harness.start()
    except Exception as exc:  # pragma: no cover - environment failure path
        return {
            "passed": False,
            "checks": [{"name": "harness_start", "passed": False, "detail": f"{type(exc).__name__}: {exc}"}],
            "verify_report": None,
            "tls": False,
        }
    if show_banner:
        _banner(harness, redact=redact, show_tokens=show_tokens)
    try:
        _check(checks, "five_listeners", len(harness.listeners) == 5, f"got {len(harness.listeners)}")
        kinds = [listener.kind for listener in harness.listeners.values()]
        _check(checks, "both_listener_kinds", "http" in kinds and "socks5" in kinds, f"kinds={kinds}")
        _check(
            checks,
            "loopback_only",
            all(listener.host == LOOPBACK_HOST for listener in harness.listeners.values()),
            f"bind={harness.bind}",
        )
        _check(
            checks,
            "unique_tokens",
            len(set(harness.tokens.values())) == len(harness.tokens),
            "each listener owns a distinct token",
        )
        _check(
            checks,
            "unique_credentials",
            len({pair for pair in harness.credentials.values()}) == len(harness.credentials),
            "each listener owns distinct credentials",
        )

        domain = harness.base_domain
        tls_available = harness.origin is not None and harness.origin.https_port is not None
        if harness.tls_note:
            checks.append({"name": "tls_optional", "passed": True, "skipped": True, "detail": harness.tls_note})

        for name, listener in harness.listeners.items():
            token = harness.tokens[name]
            token_host = f"{token}.{domain}"
            authorization = harness.basic_authorization(name)
            url = harness.token_url(name)
            before = _count_sightings(listener, name)
            try:
                if listener.kind == "http":
                    bad_authorization = _basic_header(
                        harness.credentials[name][0], harness.credentials[name][1] + "-wrong"
                    )
                    bad_request = (
                        f"GET {url} HTTP/1.1\r\nHost: {token_host}\r\n"
                        f"Proxy-Authorization: {bad_authorization}\r\nConnection: close\r\n\r\n"
                    ).encode("ascii")
                    raw = _http_exchange(listener.host, listener.port, bad_request, timeout)
                    bad_status, _bad_headers, _bad_body = _split_http_response(raw)
                    _check(
                        checks,
                        f"{name}_http_auth_rejected",
                        bad_status == 407 and _count_sightings(listener, name) == before,
                        f"status={bad_status}",
                    )
                    forward_request = (
                        f"GET {url} HTTP/1.1\r\nHost: {token_host}\r\n"
                        f"Proxy-Authorization: {authorization}\r\nConnection: close\r\n\r\n"
                    ).encode("ascii")
                    raw = _http_exchange(listener.host, listener.port, forward_request, timeout)
                    status, _headers, body = _split_http_response(raw)
                    payload: dict[str, Any] = {}
                    try:
                        payload = json.loads(body.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        payload = {}
                    _check(
                        checks,
                        f"{name}_http_forward",
                        status == 200 and payload.get("g1") == "ok" and payload.get("via") == name,
                        f"status={status} via={payload.get('via')!r}",
                    )
                    _check(
                        checks,
                        f"{name}_http_token_sighted",
                        _count_sightings(listener, name) > before,
                        f"sightings={_count_sightings(listener, name)}",
                    )
                    try:
                        tunnel, connect_status = _open_connect_tunnel(
                            listener.host, listener.port, authorization, f"{token_host}:80", timeout
                        )
                    except OSError as exc:
                        tunnel = None
                        connect_status = 0
                        checks.append(
                            {
                                "name": f"{name}_connect",
                                "passed": False,
                                "detail": f"connect failed: {type(exc).__name__}: {exc}",
                            }
                        )
                    if tunnel is not None:
                        try:
                            tunnel.sendall(_inner_get(token_host, f"{DEFAULT_ORIGIN_PATH}?probe=connect"))
                            inner_raw = _read_all(tunnel, timeout)
                        finally:
                            _close_quiet(tunnel)
                        inner_status, _inner_headers, inner_body = _split_http_response(inner_raw)
                        inner_payload: dict[str, Any] = {}
                        try:
                            inner_payload = json.loads(inner_body.decode("utf-8"))
                        except (ValueError, UnicodeDecodeError):
                            inner_payload = {}
                        _check(
                            checks,
                            f"{name}_connect",
                            connect_status == 200 and inner_status == 200 and inner_payload.get("g1") == "ok",
                            f"connect={connect_status} inner={inner_status}",
                        )
                        _check(
                            checks,
                            f"{name}_connect_token_sighted",
                            _has_event(listener, "http_connect") and _count_sightings(listener, name) > before,
                            "token observed on CONNECT authority",
                        )
                    if tls_available:
                        try:
                            tls_tunnel, tls_connect_status = _open_connect_tunnel(
                                listener.host, listener.port, authorization, f"{token_host}:443", timeout
                            )
                        except OSError as exc:
                            checks.append(
                                {
                                    "name": f"{name}_tls_connect",
                                    "passed": False,
                                    "detail": f"connect failed: {type(exc).__name__}: {exc}",
                                }
                            )
                        else:
                            tls_status = 0
                            tls_payload: dict[str, Any] = {}
                            try:
                                context = TlsMaterial.client_context()
                                with context.wrap_socket(tls_tunnel, server_hostname=token_host) as tls_sock:
                                    tls_sock.sendall(
                                        _inner_get(token_host, f"{DEFAULT_ORIGIN_PATH}?probe=tls")
                                    )
                                    tls_raw = _read_all(tls_sock, timeout)
                                tls_status, _tls_headers, tls_body = _split_http_response(tls_raw)
                                try:
                                    tls_payload = json.loads(tls_body.decode("utf-8"))
                                except (ValueError, UnicodeDecodeError):
                                    tls_payload = {}
                            finally:
                                _close_quiet(tls_tunnel)
                            _check(
                                checks,
                                f"{name}_tls_connect",
                                tls_connect_status == 200
                                and tls_status == 200
                                and tls_payload.get("origin") == "https",
                                f"connect={tls_connect_status} tls_status={tls_status}",
                            )
                    else:
                        checks.append(
                            {
                                "name": f"{name}_tls_connect",
                                "passed": True,
                                "skipped": True,
                                "detail": "TLS origin disabled",
                            }
                        )
                else:
                    try:
                        _socks5_negotiate(
                            listener.host,
                            listener.port,
                            harness.credentials[name][0],
                            harness.credentials[name][1] + "-wrong",
                            token_host,
                            80,
                            timeout=timeout,
                        )
                        socks_auth_rejected = False
                    except Socks5Error as exc:
                        socks_auth_rejected = exc.stage in {"auth", "greeting"}
                    _check(
                        checks,
                        f"{name}_socks_auth_rejected",
                        socks_auth_rejected and _count_sightings(listener, name) == before,
                        f"rejected={socks_auth_rejected}",
                    )
                    _check(
                        checks,
                        f"{name}_socks_no_auth_rejected",
                        _socks5_offer_no_auth(listener.host, listener.port, timeout=timeout) == 0xFF,
                        "server requires username/password",
                    )
                    socks_sock = _socks5_negotiate(
                        listener.host,
                        listener.port,
                        harness.credentials[name][0],
                        harness.credentials[name][1],
                        token_host,
                        80,
                        timeout=timeout,
                    )
                    try:
                        socks_sock.sendall(_inner_get(token_host, f"{DEFAULT_ORIGIN_PATH}?probe=socks"))
                        socks_raw = _read_all(socks_sock, timeout)
                    finally:
                        _close_quiet(socks_sock)
                    socks_status, _socks_headers, socks_body = _split_http_response(socks_raw)
                    socks_payload: dict[str, Any] = {}
                    try:
                        socks_payload = json.loads(socks_body.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        socks_payload = {}
                    _check(
                        checks,
                        f"{name}_socks_connect",
                        socks_status == 200 and socks_payload.get("g1") == "ok",
                        f"inner_status={socks_status}",
                    )
                    _check(
                        checks,
                        f"{name}_socks_token_sighted",
                        _count_sightings(listener, name) > before,
                        f"sightings={_count_sightings(listener, name)}",
                    )
                    udp_reply = _socks5_udp_associate(
                        listener.host,
                        listener.port,
                        harness.credentials[name][0],
                        harness.credentials[name][1],
                        timeout=timeout,
                    )
                    _check(
                        checks,
                        f"{name}_socks_udp_rejected",
                        udp_reply == 0x07 and _has_event(listener, "socks_rejected"),
                        f"reply={udp_reply}",
                    )
            except Exception as exc:  # selftest must report, not crash
                checks.append(
                    {
                        "name": f"{name}_channel_error",
                        "passed": False,
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                )

        # Each listener observed only its own token (positive run has no leaks).
        for name, _listener in harness.listeners.items():
            for other_name in harness.tokens:
                if other_name == name:
                    continue
                other = harness.listeners[other_name]
                # A healthy run must not have this listener's token on the *other* listener.
                leaked = [
                    event
                    for event in other.events()
                    if event.get("event") == "token_sighting" and event.get("token_name") == name
                ]
                _check(
                    checks,
                    f"no_cross_sighting_{name}_on_{other_name}",
                    not leaked,
                    f"count={len(leaked)}",
                )

        report = harness.verify()
        _check(
            checks,
            "verify_isolation_passes",
            bool(report.get("passed")),
            f"failures={[failure['code'] for failure in report.get('failures', [])]}",
        )
        serialized_report = json.dumps(report, sort_keys=True)
        raw_token_leak = any(
            token and token in serialized_report for token in harness.tokens.values()
        )
        _check(checks, "report_has_no_raw_tokens", not raw_token_leak)

        redacted_report = verify_isolation(harness.tokens, harness.redacted_logs())
        _check(
            checks,
            "verify_accepts_redacted_logs",
            bool(redacted_report.get("passed")),
            f"failures={[failure['code'] for failure in redacted_report.get('failures', [])]}",
        )

        derived_report = verify_isolation(list(harness.tokens.values()), harness.listener_logs())
        _check(
            checks,
            "verify_accepts_token_sequence",
            bool(derived_report.get("passed")),
            f"failures={[failure['code'] for failure in derived_report.get('failures', [])]}",
        )

        leak_logs = {
            name: [dict(event) for event in listener.events()]
            for name, listener in harness.listeners.items()
        }
        victim = next(
            (event for event in leak_logs["L1"] if event.get("event") == "token_sighting"),
            None,
        )
        if victim is not None:
            leak_logs["L2"].append(dict(victim, listener="L2"))
            leak_report = verify_isolation(harness.tokens, leak_logs)
            _check(
                checks,
                "detects_cross_listener_leak",
                not leak_report.get("passed")
                and any(failure["code"] == "token_on_wrong_listener" for failure in leak_report["failures"]),
                f"failures={[failure['code'] for failure in leak_report.get('failures', [])]}",
            )
        else:
            checks.append(
                {"name": "detects_cross_listener_leak", "passed": False, "detail": "no sighting to duplicate"}
            )

        missing_logs = {
            name: [
                dict(event)
                for event in listener.events()
                if not (event.get("event") == "token_sighting" and event.get("token_name") == "L3")
            ]
            for name, listener in harness.listeners.items()
        }
        missing_report = verify_isolation(harness.tokens, missing_logs)
        _check(
            checks,
            "detects_missing_own_sighting",
            not missing_report.get("passed")
            and any(failure["code"] == "missing_own_sighting" for failure in missing_report["failures"]),
            f"failures={[failure['code'] for failure in missing_report.get('failures', [])]}",
        )

        credential_logs = {
            name: [dict(event) for event in listener.events()]
            for name, listener in harness.listeners.items()
        }
        auth_event = next(
            (event for event in credential_logs["L1"] if event.get("event") == "auth_ok"),
            None,
        )
        if auth_event is not None:
            credential_logs["L2"].append(dict(auth_event, listener="L2"))
            credential_report = verify_isolation(
                harness.tokens, credential_logs, credentials=harness.credentials
            )
            _check(
                checks,
                "detects_cross_listener_credential",
                not credential_report.get("passed")
                and any(failure["code"] == "credential_cross_listener" for failure in credential_report["failures"]),
                f"failures={[failure['code'] for failure in credential_report.get('failures', [])]}",
            )
        else:
            checks.append(
                {
                    "name": "detects_cross_listener_credential",
                    "passed": False,
                    "detail": "no auth_ok event to duplicate",
                }
            )

        origin_logs = harness.origin_logs()
        bypass_token = harness.tokens["L1"]
        origin_logs.append(
            {
                "ts": _iso_now(),
                "origin": "http",
                "method": "GET",
                "path": DEFAULT_ORIGIN_PATH,
                "query": f"probe=bypass&token={bypass_token}",
                "host_header": f"{bypass_token}.{domain}",
                "via_listener": "",
                "remote": "127.0.0.1",
            }
        )
        bypass_report = verify_isolation(harness.tokens, harness.listener_logs(), origin_logs=origin_logs)
        _check(
            checks,
            "detects_origin_bypass",
            not bypass_report.get("passed")
            and any(failure["code"].startswith("origin_") for failure in bypass_report["failures"]),
            f"failures={[failure['code'] for failure in bypass_report.get('failures', [])]}",
        )

        passed = all(check["passed"] for check in checks)
        return {
            "passed": passed,
            "checks": checks,
            "verify_report": report,
            "tls": bool(tls_available),
            "tls_note": harness.tls_note,
            "listener_kinds": {name: listener.kind for name, listener in harness.listeners.items()},
            "ports": {name: listener.port for name, listener in harness.listeners.items()},
        }
    finally:
        if not keep:
            harness.stop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AVD Fleet gate G1 loopback HTTP+SOCKS5 listener harness (self-test by default).",
    )
    parser.add_argument("--serve", action="store_true", help="start five listeners and keep serving until Ctrl+C")
    parser.add_argument("--bind", default=LOOPBACK_HOST, help="loopback bind address (default 127.0.0.1)")
    parser.add_argument("--no-tls", action="store_true", help="disable the optional HTTPS echo origin")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-channel client timeout in seconds")
    parser.add_argument("--json", action="store_true", help="print the self-test report as JSON")
    parser.add_argument("--verbose", action="store_true", help="print one line per self-test check")
    parser.add_argument("--no-banner", action="store_true", help="suppress the fixture/URL banner")
    parser.add_argument("--redact", action="store_true", help="redact listener credentials in printed URLs")
    parser.add_argument("--show-tokens", action="store_true", help="show raw tokens in the banner (test only)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        bind = _ipv4_loopback(args.bind)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.serve:
        harness = ListenerHarness.standard(bind=bind, tls=not args.no_tls)
        try:
            harness.start()
        except Exception as exc:
            print(f"error: failed to start the G1 harness: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        try:
            _banner(harness, redact=args.redact, show_tokens=args.show_tokens)
            print("")
            print("Serve mode: five listeners are live. Press Ctrl+C to stop.")
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("")
            print("Stopping listeners.")
        finally:
            harness.stop()
        return 0
    result = run_selftest(
        bind=bind,
        tls=not args.no_tls,
        timeout=args.timeout,
        verbose=args.verbose and not args.json,
        show_banner=not args.no_banner and not args.json,
        redact=args.redact,
        show_tokens=args.show_tokens,
        keep=False,
    )
    if args.json:
        # Machine-readable mode: stdout is exactly one JSON document.
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["passed"] else 1
    for check in result["checks"]:
        if check.get("skipped"):
            status = "SKIP"
        elif check.get("passed"):
            status = "PASS"
        else:
            status = "FAIL"
        line = f"[{status}] {check['name']}"
        if args.verbose and check.get("detail"):
            line += f" -- {check['detail']}"
        print(line)
    print("")
    print("SELF-TEST RESULT:", "PASS" if result["passed"] else "FAIL")
    if result.get("tls_note"):
        print("TLS note:", result["tls_note"])
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
