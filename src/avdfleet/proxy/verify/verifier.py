"""Host-side and in-guest proxy egress verification.

Two independent checks share one class, :class:`GuestEgressVerifier`, which
implements :class:`avdfleet.core.interfaces.ProxyVerifier`:

``verify_proxy(proxy)``
    Host-side reachability **through** the configured proxy (no emulator).  An
    HTTP/HTTPS proxy is exercised with :mod:`httpx` (Basic auth from
    :class:`~avdfleet.core.models.ProxyConfig`, no environment proxies); a
    SOCKS5 proxy is exercised with a minimal stdlib SOCKS5 handshake followed by
    an HTTP(S) GET.  The egress IP is parsed from the verification response
    when the configured URL echoes it (JSON ``ip``/``origin``/``query`` fields
    or a bare IP).

``verify_guest(avd, proxy, token_url=...)``
    Executes an HTTP request *inside* the running guest through the injected
    command runner (default: ``adb -s <serial> shell ...``)::

        adb -s <serial> shell curl -sS --max-time <N> <token_url>
        fallbacks: wget -qO- <token_url> ; toybox wget -qO- <token_url>

    The token URL is the G1 evidence URL: it carries the instance's guest token
    and its response is expected to echo the token and/or the observed egress
    IP.  The verifier never passes proxy credentials on the command line - the
    guest reaches its upstream because the emulator/bridge is configured
    separately (see :mod:`avdfleet.proxy.bridge.manager`).  Missing clients
    (``curl: inaccessible or not found``), transport errors, timeouts and the
    final failure are reported as a failed :class:`ProxyTestResult` with a
    redacted message; per-attempt details are kept in memory via
    :meth:`GuestEgressVerifier.guest_details`.

Redaction: proxy passwords and guest tokens never reach log records, argv or
result error strings.  The verifier stores the details of the last guest check
per AVD, which API/CLI callers may surface without re-running anything.

Secrets policy in code: ``ProxyConfig.url(reveal=True)`` is used exactly once,
to configure the HTTP client; every error path funnels through
:func:`_scrub`, which applies :func:`avdfleet.core.logging.redact` *and* the
proxy's own password string.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx

from avdfleet.core.config import Config
from avdfleet.core.errors import ToolError, ValidationError
from avdfleet.core.logging import get_logger, redact_text
from avdfleet.core.models import ProxyConfig, ProxyScheme, ProxyTestResult

_LOGGER = get_logger("proxy.verify")

_IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6_RE = re.compile(r"(?<![\w:])(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}(?![\w:])")
_SERIAL_RE = re.compile(r"^(?:emulator-\d{1,6}|[A-Za-z0-9._-]+:\d{1,5})$")
_MISSING_CLIENT_MARKERS = (
    "not found",
    "inaccessible or not found",
    "no such file",
    "command not found",
    "unknown command",
)
_TOKEN_QUERY_KEYS = {"token", "t", "guest_token", "avdfleet_token", "access_token"}


def _masked_command(command: Sequence[str], url: str) -> tuple[str, ...]:
    """Guest argv for details: the token URL is masked, credentials never appear."""
    masked_url = mask_token_url(url)
    return tuple(masked_url if part == url else part for part in command)


# ---------------------------------------------------------------------------
# Guest command execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """Result of one guest command attempt (already captured, never logged raw)."""

    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_s: float = 0.0

    @property
    def duration_ms(self) -> float:
        return round(self.duration_s * 1000.0, 2)


@runtime_checkable
class GuestCommandRunner(Protocol):
    """Executes a command inside the guest and returns its captured output."""

    def run(self, serial: str, command: Sequence[str], *, timeout_s: float) -> CommandResult: ...


def normalize_adb_prefix(adb: Sequence[str] | str | Path) -> tuple[str, ...]:
    """Normalise an adb executable (or ``[python, fake_adb.py]`` prefix) to argv parts."""
    if isinstance(adb, (str, Path)):
        return (str(adb),)
    parts = tuple(str(part) for part in adb)
    if not parts:
        raise ValidationError("adb path must not be empty")
    return parts


class SubprocessGuestRunner:
    """Default runner: ``adb -s <serial> shell <command...>`` via subprocess."""

    def __init__(self, adb: Sequence[str] | str | Path) -> None:
        self._adb = normalize_adb_prefix(adb)

    @property
    def adb(self) -> tuple[str, ...]:
        return self._adb

    def run(self, serial: str, command: Sequence[str], *, timeout_s: float) -> CommandResult:
        argv = (*self._adb, "-s", serial, "shell", *command)
        return self._run(argv, timeout_s=timeout_s, shell_serial=serial)

    def run_adb(self, args: Sequence[str], *, timeout_s: float = 10.0) -> CommandResult:
        """Run a plain adb command (used for serial resolution)."""
        return self._run((*self._adb, *args), timeout_s=timeout_s, shell_serial=None)

    def _run(
        self, argv: tuple[str, ...], *, timeout_s: float, shell_serial: str | None
    ) -> CommandResult:
        started = time.monotonic()
        try:
            completed = subprocess.run(  # noqa: S603 - argv-only, never shell=True
                list(argv),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(
                argv=tuple(argv),
                exit_code=None,
                timed_out=True,
                duration_s=time.monotonic() - started,
            )
        except FileNotFoundError as exc:
            raise ToolError(
                f"adb executable not found: {argv[0]}",
                hint="Pass adb_path or install Android platform-tools.",
            ) from exc
        except OSError as exc:
            raise ToolError(f"could not run adb: {exc}") from exc
        return CommandResult(
            argv=tuple(argv),
            exit_code=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            duration_s=time.monotonic() - started,
        )


# ---------------------------------------------------------------------------
# Details models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GuestAttempt:
    """One guest HTTP client attempt (curl / wget / toybox wget)."""

    label: str
    command: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    missing_client: bool
    stdout_excerpt: str = ""
    stderr_excerpt: str = ""


@dataclass(frozen=True)
class GuestEgressDetails:
    """In-memory explanation of the last guest egress check."""

    avd_name: str
    serial: str | None
    token_url_masked: str = ""
    attempts: tuple[GuestAttempt, ...] = ()
    egress_ip: str | None = None
    token_expected: str | None = None
    token_echoed: bool | None = None
    error: str = ""
    duration_ms: float = 0.0
    checked_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_ip(value: str) -> str | None:
    candidate = value.strip().strip(".,;\"'[]")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


def extract_egress_ip(payload: str) -> str | None:
    """Extract the observed egress IP from a verification response body."""
    if not payload:
        return None
    text = payload.strip()
    if text:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            for key in ("ip", "origin", "query", "address", "egress_ip", "client_ip"):
                value = parsed.get(key)
                if isinstance(value, str):
                    found = _valid_ip(value)
                    if found:
                        return found
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            found = _valid_ip(item)
                            if found:
                                return found
    for match in _IPV4_RE.findall(payload):
        found = _valid_ip(match)
        if found:
            return found
    for match in _IPV6_RE.findall(payload):
        found = _valid_ip(match)
        if found:
            return found
    return None


def _expected_token(url: str) -> str | None:
    """Derive an expected token from a ``token=``-style query parameter, if any."""
    try:
        query = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    except ValueError:
        return None
    for key, value in query:
        if key.lower() in _TOKEN_QUERY_KEYS and value:
            return value
    return None


def mask_token_url(url: str) -> str:
    """Return *url* safe for display: no userinfo, token-ish query values masked."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    netloc = parsed.netloc
    if parsed.username is not None or parsed.password is not None:
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        netloc = f"***@{host}{port}"
    query = parsed.query
    if query:
        pairs = []
        for key, value in parse_qsl(query, keep_blank_values=True):
            pairs.append((key, "***" if key.lower() in _TOKEN_QUERY_KEYS else value))
        query = "&".join(f"{key}={value}" for key, value in pairs)
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


def _excerpt(text: str, *, limit: int = 240) -> str:
    cleaned = redact_text(text.replace("\r", " ").replace("\n", " ")).strip()
    return cleaned[:limit]


def _looks_like_missing_client(result: CommandResult) -> bool:
    if result.exit_code not in (126, 127):
        combined = f"{result.stdout}\n{result.stderr}".lower()
        return any(marker in combined for marker in _MISSING_CLIENT_MARKERS)
    return True


def _discover_adb(config: Config | None) -> tuple[str, ...] | None:
    explicit = os.environ.get("AVDFLEET_ADB", "").strip()
    if explicit:
        return (explicit,)
    system = shutil.which("adb")
    if system:
        return (system,)
    roots: list[str] = []
    for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT", "AVDFLEET_SDK_ROOT"):
        value = os.environ.get(name, "").strip()
        if value:
            roots.append(value)
    if config is not None and getattr(config, "sdk_root", None):
        roots.append(str(config.sdk_root))
    executable = "adb.exe" if os.name == "nt" else "adb"
    for root in roots:
        candidate = Path(root) / "platform-tools" / executable
        if candidate.exists():
            return (str(candidate),)
    return None


def _parse_http_response(data: bytes) -> tuple[int | None, str]:
    """Return ``(status_code, body_text)`` for a raw HTTP/1.x response."""
    if not data:
        return None, ""
    head, _, body = data.partition(b"\r\n\r\n")
    if not head:
        return None, ""
    status_line = head.split(b"\r\n", 1)[0]
    parts = status_line.split(b" ", 2)
    if len(parts) < 2 or not parts[0].upper().startswith(b"HTTP/"):
        return None, body.decode("utf-8", "replace")
    try:
        status = int(parts[1])
    except ValueError:
        return None, body.decode("utf-8", "replace")
    return status, body.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


class GuestEgressVerifier:
    """Proxy reachability checks host-side and inside a running AVD guest."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        verify_url: str | None = None,
        timeout_s: float | None = None,
        runner: GuestCommandRunner | None = None,
        adb_path: Sequence[str] | str | Path | None = None,
        serial_resolver: Callable[[str], str] | None = None,
        ssl_verify: bool = True,
    ) -> None:
        self._config = config or Config()
        self._verify_url = verify_url or self._config.proxy_verify_url
        self._timeout_s = (
            float(timeout_s) if timeout_s is not None else float(self._config.proxy_verify_timeout_s)
        )
        self._ssl_verify = ssl_verify
        self._serial_resolver = serial_resolver
        self._adb_prefix = normalize_adb_prefix(adb_path) if adb_path is not None else _discover_adb(config)
        self._runner: GuestCommandRunner | None = (
            runner if runner is not None else (SubprocessGuestRunner(self._adb_prefix) if self._adb_prefix else None)
        )
        self._details_lock = threading.Lock()
        self._guest_details: dict[str, GuestEgressDetails] = {}
        self._proxy_checks: dict[str, dict[str, Any]] = {}

    # -- public API (ProxyVerifier) --------------------------------------
    def verify_proxy(self, proxy: ProxyConfig, *, timeout_s: float | None = None) -> ProxyTestResult:
        """Test host-side connectivity through *proxy* (no guest involved)."""
        timeout = self._effective_timeout(timeout_s)
        started = time.monotonic()
        if proxy.scheme is ProxyScheme.SOCKS5:
            result = self._verify_proxy_socks5(proxy, timeout)
        else:
            result = self._verify_proxy_http(proxy, timeout)
        latency_ms = round((time.monotonic() - started) * 1000.0, 2)
        result = result.model_copy(update={"latency_ms": latency_ms})
        with self._details_lock:
            self._proxy_checks[proxy.id] = {
                "proxy": proxy.safe_url,
                "ok": result.ok,
                "error": result.error,
                "egress_ip": result.egress_ip,
                "status_code": result.status_code,
                "latency_ms": result.latency_ms,
            }
        _LOGGER.info(
            "verify_proxy proxy=%s ok=%s latency_ms=%s status=%s egress_ip=%s error=%s",
            proxy.safe_url,
            result.ok,
            result.latency_ms,
            result.status_code,
            result.egress_ip,
            result.error,
        )
        return result

    def verify_guest(
        self,
        avd_name: str,
        proxy: ProxyConfig,
        *,
        token_url: str | None = None,
        timeout_s: float | None = None,
        expected_token: str | None = None,
    ) -> ProxyTestResult:
        """Run an in-guest HTTP request through the guest's configured egress path."""
        started = time.monotonic()
        timeout = self._effective_timeout(timeout_s)
        url = token_url or self._verify_url
        try:
            self._validate_token_url(url)
        except ValidationError as exc:
            return self._guest_result(
                proxy,
                ok=False,
                error=self._scrub(str(exc), proxy),
                started=started,
                details=GuestEgressDetails(avd_name=avd_name, serial=None),
            )

        serial: str | None = None
        attempts: list[GuestAttempt] = []
        error = ""
        ok = False
        egress: str | None = None
        token_expected = expected_token or _expected_token(url)
        token_echoed: bool | None = None

        try:
            serial = self._resolve_serial(avd_name)
        except (ToolError, ValidationError) as exc:
            error = str(exc)
            return self._guest_result(
                proxy,
                ok=False,
                error=self._scrub(error, proxy),
                started=started,
                details=GuestEgressDetails(
                    avd_name=avd_name,
                    serial=None,
                    token_url_masked=mask_token_url(url),
                    error=self._scrub(error, proxy),
                ),
            )

        if self._runner is None:
            error = "no adb runner available to execute a command inside the guest"
            return self._guest_result(
                proxy,
                ok=False,
                error=error,
                started=started,
                details=GuestEgressDetails(
                    avd_name=avd_name,
                    serial=serial,
                    token_url_masked=mask_token_url(url),
                    error=error,
                ),
            )

        commands = self._guest_commands(url, timeout)
        last_error = ""
        all_missing = True
        for label, command in commands:
            try:
                result = self._runner.run(serial, command, timeout_s=timeout + 5.0)
            except ToolError as exc:
                last_error = str(exc)
                all_missing = False
                attempts.append(
                    GuestAttempt(
                        label=label,
                        command=_masked_command(command, url),
                        exit_code=None,
                        timed_out=False,
                        missing_client=False,
                        stderr_excerpt=_excerpt(str(exc)),
                    )
                )
                continue
            except OSError as exc:  # pragma: no cover - runner should map these
                last_error = f"could not execute guest command: {exc}"
                all_missing = False
                continue

            missing = _looks_like_missing_client(result)
            all_missing = all_missing and missing
            attempts.append(
                GuestAttempt(
                    label=label,
                    command=_masked_command(command, url),
                    exit_code=result.exit_code,
                    timed_out=result.timed_out,
                    missing_client=missing,
                    stdout_excerpt=_excerpt(result.stdout),
                    stderr_excerpt=_excerpt(result.stderr),
                )
            )
            if result.timed_out:
                error = f"guest HTTP request timed out after {timeout:g}s (client: {label})"
                break
            if result.exit_code == 0:
                ok = True
                egress = extract_egress_ip(result.stdout) or extract_egress_ip(result.stderr)
                token_echoed = None if token_expected is None else token_expected in result.stdout
                break
            last_error = self._describe_failure(label, result)
            if missing:
                continue

        if not ok and not error:
            if all_missing:
                error = (
                    "guest has no usable HTTP client "
                    "(curl, wget and toybox wget are all missing or failed to launch)"
                )
            else:
                error = last_error or "guest HTTP request failed"

        if ok and expected_token is not None and token_echoed is False:
            ok = False
            egress = None
            error = "guest response did not contain the expected listener token"

        if ok and token_expected is not None and token_echoed is False:
            _LOGGER.debug(
                "verify_guest token not echoed by %s (listener evidence is collected separately)",
                proxy.safe_url,
            )

        details = GuestEgressDetails(
            avd_name=avd_name,
            serial=serial,
            token_url_masked=mask_token_url(url),
            attempts=tuple(attempts),
            egress_ip=egress,
            token_expected=token_expected,
            token_echoed=token_echoed,
            error=self._scrub(error, proxy),
            duration_ms=round((time.monotonic() - started) * 1000.0, 2),
        )
        return self._guest_result(
            proxy, ok=ok, error=details.error, started=started, details=details, egress_ip=egress
        )

    # -- detail accessors ------------------------------------------------
    def guest_details(self, avd_name: str) -> GuestEgressDetails | None:
        """Return the details of the last guest check for *avd_name* (in memory)."""
        with self._details_lock:
            return self._guest_details.get(avd_name)

    def proxy_check_details(self, proxy_id: str) -> dict[str, Any] | None:
        with self._details_lock:
            return dict(self._proxy_checks[proxy_id]) if proxy_id in self._proxy_checks else None

    # -- HTTP proxy verification ----------------------------------------
    def _verify_proxy_http(self, proxy: ProxyConfig, timeout: float) -> ProxyTestResult:
        proxy_url = proxy.url(reveal=True)
        try:
            with httpx.Client(
                proxy=proxy_url,
                timeout=httpx.Timeout(timeout),
                verify=self._ssl_verify,
                trust_env=False,
                follow_redirects=True,
            ) as client:
                response = client.get(self._verify_url)
        except httpx.TimeoutException:
            return self._failed(proxy, f"proxy check timed out after {timeout:g}s")
        except httpx.ProxyError as exc:
            return self._failed(proxy, self._scrub(f"proxy connection failed: {exc}", proxy))
        except httpx.HTTPError as exc:
            return self._failed(proxy, self._scrub(f"proxy check failed: {exc}", proxy))
        status = response.status_code
        if status == 407:
            return self._failed(proxy, "upstream proxy rejected the configured credentials (HTTP 407)")
        egress = extract_egress_ip(response.text) or _valid_ip(
            response.headers.get("x-egress-ip", "")
        )
        if status >= 400:
            return ProxyTestResult(
                proxy_id=proxy.id,
                ok=False,
                status_code=status,
                egress_ip=egress,
                error=f"verification URL returned HTTP {status}",
            )
        return ProxyTestResult(proxy_id=proxy.id, ok=True, status_code=status, egress_ip=egress)

    # -- SOCKS5 proxy verification (stdlib, socksio is not a dependency) --
    def _verify_proxy_socks5(self, proxy: ProxyConfig, timeout: float) -> ProxyTestResult:
        try:
            parsed = urlsplit(self._verify_url)
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            return self._failed(proxy, f"invalid verification URL: {exc}")
        if not host or parsed.scheme not in {"http", "https"}:
            return self._failed(proxy, "verification URL must be http(s) with a host")

        password = proxy.password.get_secret_value() if proxy.password is not None else ""
        username = proxy.username or ""
        sock: socket.socket | None = None
        try:
            sock = socket.create_connection((proxy.host, proxy.port), timeout=timeout)
            sock.settimeout(timeout)
            _socks5_handshake(sock, host, port, username, password)
            if parsed.scheme == "https":
                context = ssl.create_default_context() if self._ssl_verify else _unverified_context()
                sock = context.wrap_socket(sock, server_hostname=host)
                sock.settimeout(timeout)
            path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
            default_port = 443 if parsed.scheme == "https" else 80
            host_header = host if port == default_port else f"{host}:{port}"
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host_header}\r\n"
                "User-Agent: avdfleet-verify\r\n"
                "Accept: */*\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("latin-1")
            sock.sendall(request)
            data = _recv_all(sock, timeout=timeout)
        except _SocksFailure as exc:
            return self._failed(proxy, self._scrub(str(exc), proxy))
        except TimeoutError:
            return self._failed(proxy, f"SOCKS5 check timed out after {timeout:g}s")
        except ssl.SSLError as exc:
            return self._failed(proxy, self._scrub(f"TLS to verification target failed: {exc}", proxy))
        except ConnectionRefusedError:
            return self._failed(proxy, "SOCKS5 proxy refused the connection")
        except OSError as exc:
            return self._failed(proxy, self._scrub(f"SOCKS5 check failed: {exc}", proxy))
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

        status, body = _parse_http_response(data)
        if status is None:
            return self._failed(proxy, "verification target returned a malformed HTTP response")
        if status >= 400:
            return ProxyTestResult(
                proxy_id=proxy.id,
                ok=False,
                status_code=status,
                egress_ip=extract_egress_ip(body),
                error=f"verification URL returned HTTP {status}",
            )
        return ProxyTestResult(
            proxy_id=proxy.id,
            ok=True,
            status_code=status,
            egress_ip=extract_egress_ip(body),
        )

    # -- guest helpers ---------------------------------------------------
    def _resolve_serial(self, avd_name: str) -> str:
        if self._serial_resolver is not None:
            serial = self._serial_resolver(avd_name)
            if not serial:
                raise ValidationError(f"serial resolver returned no serial for AVD {avd_name!r}")
            return serial
        if _SERIAL_RE.match(avd_name):
            return avd_name
        if self._adb_prefix is None:
            raise ToolError(
                f"cannot map AVD {avd_name!r} to an adb serial: adb was not found",
                hint="Pass adb_path, a serial_resolver or use the emulator serial as the AVD name.",
            )
        return self._discover_serial_via_adb(avd_name)

    def _discover_serial_via_adb(self, avd_name: str) -> str:
        adb = self._adb_prefix
        assert adb is not None
        try:
            devices = subprocess.run(  # noqa: S603 - argv-only, never shell=True
                [*adb, "devices"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10.0,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolError(f"could not list adb devices: {exc}") from exc
        serials = [
            line.split("\t", 1)[0].strip()
            for line in (devices.stdout or "").splitlines()
            if "\tdevice" in line
        ]
        for serial in serials:
            try:
                named = subprocess.run(  # noqa: S603 - argv-only, never shell=True
                    [*adb, "-s", serial, "emu", "avd", "name"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10.0,
                    stdin=subprocess.DEVNULL,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            first_line = next(
                (line.strip() for line in (named.stdout or "").splitlines() if line.strip()), ""
            )
            if first_line and first_line == avd_name:
                return serial
        raise ToolError(
            f"could not resolve adb serial for AVD {avd_name!r}",
            hint="Start the AVD or pass serial_resolver.",
        )

    def _guest_commands(self, url: str, timeout: float) -> list[tuple[str, tuple[str, ...]]]:
        max_time = max(1, int(round(timeout)))
        return [
            ("curl", ("curl", "-sS", "--max-time", str(max_time), url)),
            ("wget", ("wget", "-qO-", url)),
            ("toybox-wget", ("toybox", "wget", "-qO-", url)),
        ]

    def _describe_failure(self, label: str, result: CommandResult) -> str:
        stderr = _excerpt(result.stderr)
        stdout = _excerpt(result.stdout)
        detail = stderr or stdout or "no output"
        return f"guest {label} failed (exit {result.exit_code}): {detail}"

    @staticmethod
    def _validate_token_url(url: str) -> None:
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise ValidationError(f"invalid token URL: {exc}") from exc
        if parsed.scheme not in {"http", "https"}:
            raise ValidationError("token URL must use http or https")
        if not parsed.hostname:
            raise ValidationError("token URL must include a host")
        if parsed.username is not None or parsed.password is not None:
            raise ValidationError(
                "token URL must not contain credentials; guest tokens belong in the URL path/query, "
                "never proxy credentials on a command line"
            )

    # -- result helpers --------------------------------------------------
    def _effective_timeout(self, timeout_s: float | None) -> float:
        timeout = self._timeout_s if timeout_s is None else float(timeout_s)
        if not 0 < timeout <= 600:
            raise ValidationError("verification timeout must be in (0, 600] seconds")
        return timeout

    def _scrub(self, text: str, proxy: ProxyConfig) -> str:
        cleaned = redact_text(str(text))
        if proxy.password is not None:
            secret = proxy.password.get_secret_value()
            if secret:
                cleaned = cleaned.replace(secret, "***")
        return cleaned

    def _failed(self, proxy: ProxyConfig, error: str) -> ProxyTestResult:
        return ProxyTestResult(proxy_id=proxy.id, ok=False, error=self._scrub(error, proxy))

    def _guest_result(
        self,
        proxy: ProxyConfig,
        *,
        ok: bool,
        error: str,
        started: float,
        details: GuestEgressDetails,
        egress_ip: str | None = None,
    ) -> ProxyTestResult:
        latency_ms = round((time.monotonic() - started) * 1000.0, 2)
        with self._details_lock:
            self._guest_details[details.avd_name] = details
        _LOGGER.info(
            "verify_guest avd=%s serial=%s proxy=%s ok=%s attempts=%s egress_ip=%s error=%s",
            details.avd_name,
            details.serial,
            proxy.safe_url,
            ok,
            len(details.attempts),
            egress_ip,
            error,
        )
        return ProxyTestResult(
            proxy_id=proxy.id,
            ok=ok,
            latency_ms=latency_ms,
            egress_ip=egress_ip,
            error=error,
        )


# ---------------------------------------------------------------------------
# Minimal blocking SOCKS5 client (verification only; the bridge has its own
# async implementation, but httpx needs the optional ``socksio`` extra, which
# AVDFleet does not depend on).
# ---------------------------------------------------------------------------


class _SocksFailure(Exception):
    """SOCKS5 handshake failure with a user-safe message."""


_SOCKS_REPLY_MESSAGES = {
    0x02: "connection not allowed by ruleset",
    0x03: "network unreachable",
    0x04: "host unreachable",
    0x05: "connection refused",
    0x06: "TTL expired",
    0x07: "command not supported",
    0x08: "address type not supported",
}


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    buffer = bytearray()
    while len(buffer) < size:
        chunk = sock.recv(size - len(buffer))
        if not chunk:
            raise _SocksFailure("SOCKS5 proxy closed the connection during the handshake")
        buffer.extend(chunk)
    return bytes(buffer)


def _socks5_handshake(sock: socket.socket, host: str, port: int, username: str, password: str) -> None:
    """Perform RFC 1928/1929 CONNECT with SOCKS5h (remote DNS) semantics."""
    methods = [0x00]
    if username or password:
        methods.append(0x02)
    sock.sendall(bytes([0x05, len(methods), *methods]))
    reply = _recv_exact(sock, 2)
    if reply[0] != 0x05:
        raise _SocksFailure("upstream does not speak SOCKS5")
    method = reply[1]
    if method == 0xFF:
        raise _SocksFailure("SOCKS5 proxy rejected all offered authentication methods")
    if method == 0x02:
        user = username.encode("utf-8")
        secret = password.encode("utf-8")
        if len(user) > 255 or len(secret) > 255:
            raise _SocksFailure("SOCKS5 credentials exceed the protocol limit")
        sock.sendall(bytes([0x01, len(user)]) + user + bytes([len(secret)]) + secret)
        auth_reply = _recv_exact(sock, 2)
        if auth_reply[1] != 0x00:
            raise _SocksFailure("SOCKS5 proxy rejected the configured credentials")
    elif method != 0x00:
        raise _SocksFailure("SOCKS5 proxy selected an unsupported authentication method")

    try:
        ipaddress.IPv4Address(host)
        address_type = 0x01
        address = socket.inet_pton(socket.AF_INET, host)
    except ValueError:
        try:
            ipaddress.IPv6Address(host)
            address_type = 0x04
            address = socket.inet_pton(socket.AF_INET6, host)
        except ValueError:
            encoded = host.encode("idna")
            if not encoded or len(encoded) > 255:
                raise _SocksFailure("verification host is not encodable for SOCKS5") from None
            address_type = 0x03
            address = bytes([len(encoded)]) + encoded
    sock.sendall(bytes([0x05, 0x01, 0x00, address_type]) + address + port.to_bytes(2, "big"))
    reply = _recv_exact(sock, 4)
    if reply[0] != 0x05:
        raise _SocksFailure("malformed SOCKS5 reply")
    if reply[1] != 0x00:
        message = _SOCKS_REPLY_MESSAGES.get(reply[1], "unknown SOCKS5 error")
        raise _SocksFailure(f"SOCKS5 connect failed: {message}")
    if reply[3] == 0x01:
        _ = _recv_exact(sock, 4)
    elif reply[3] == 0x04:
        _ = _recv_exact(sock, 16)
    elif reply[3] == 0x03:
        length = _recv_exact(sock, 1)[0]
        _ = _recv_exact(sock, length)
    else:
        raise _SocksFailure("SOCKS5 reply used an unsupported address type")
    _ = _recv_exact(sock, 2)


def _recv_all(sock: socket.socket, *, timeout: float, max_bytes: int = 1_048_576) -> bytes:
    deadline = time.monotonic() + timeout
    buffer = bytearray()
    while len(buffer) < max_bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out reading the verification response")
        sock.settimeout(remaining)
        chunk = sock.recv(65536)
        if not chunk:
            break
        buffer.extend(chunk)
    return bytes(buffer)


def _unverified_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


__all__ = [
    "CommandResult",
    "GuestAttempt",
    "GuestCommandRunner",
    "GuestEgressDetails",
    "GuestEgressVerifier",
    "SubprocessGuestRunner",
    "extract_egress_ip",
    "mask_token_url",
    "normalize_adb_prefix",
]
