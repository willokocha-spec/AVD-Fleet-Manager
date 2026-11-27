"""Parse proxy-pool text into canonical proxy entries.

Accepted input forms (mixed freely in one bulk paste, one entry per line):

* ``scheme://[user:pass@]host:port`` with ``http``, ``https``, ``socks5`` or
  ``socks5h``; userinfo may be percent-encoded (``user%40x:p%20w``).
* ``[user:pass@]host:port`` -- scheme defaults to ``http``.
* CSV-ish rows: ``host:port,user,pass``, ``scheme,host,port,user,pass``,
  ``scheme,host:port,user,pass`` -- comma, semicolon or tab separated.
* The classic proxy-list row ``host:port:user:pass`` (colon separated; IPv6
  hosts must use the bracketed URL form instead).
* Blank lines and full-line comments starting with ``#`` or ``//`` are ignored.

Secrets never leave this module in plaintext: :class:`ParsedProxy` has a safe
``repr``/``str`` and parser errors are redacted.  Nothing here logs.

IDs are deterministic: ``<host-slug>-<port>-<8 hex chars>`` where the hash is
derived from scheme, host, port and username (never the password), so the same
proxy pasted twice keeps the same id and duplicate detection can fingerprint
entries without touching secrets.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

from pydantic import SecretStr

from ...core.errors import ProxyError
from ...core.logging import redact
from ...core.models import ProxyConfig, ProxyScheme, slugify

SUPPORTED_SCHEMES: frozenset[str] = frozenset({"http", "https", "socks5", "socks5h"})
DEFAULT_PORTS: dict[str, int] = {"http": 8080, "https": 8080, "socks5": 1080}
COMMENT_PREFIXES: tuple[str, ...] = ("#", "//")
MAX_LINE_LENGTH = 4096

_CSV_SPLIT = re.compile(r"[,;\t]+")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HOST_CHARS = re.compile(r"^[a-z0-9._-]+$")
_IPV6_CHARS = re.compile(r"^[0-9a-f:.]+$")
_SCHEME_IN_TEXT = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*)://")


# ---------------------------------------------------------------------------
# Parsed entry
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ParsedProxy:
    """A validated proxy endpoint plus credentials.

    The password is excluded from ``repr`` and never rendered by ``str``; use
    :meth:`url` with ``reveal=True`` when an upstream URL is genuinely needed.
    """

    scheme: ProxyScheme
    host: str
    port: int
    username: str | None = None
    password: str | None = field(default=None, repr=False)

    @property
    def fingerprint(self) -> str:
        """Stable identity (excludes the password) used for duplicate detection."""
        return proxy_fingerprint(self.scheme, self.host, self.port, self.username)

    @property
    def id(self) -> str:
        return make_proxy_id(self.scheme, self.host, self.port, self.username)

    @property
    def name(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def safe_url(self) -> str:
        return self.url()

    def url(self, *, reveal: bool = False) -> str:
        """Return the proxy URL; passwords are masked unless ``reveal=True``."""
        if reveal or not self.password:
            return proxy_url(self.scheme, self.host, self.port, self.username, self.password)
        return proxy_url(self.scheme, self.host, self.port, self.username, self.password, mask_password=True)

    def to_proxy_config(
        self,
        *,
        proxy_id: str | None = None,
        name: str | None = None,
        tags: list[str] | None = None,
        enabled: bool = True,
        source: str = "",
    ) -> ProxyConfig:
        return ProxyConfig(
            id=proxy_id or self.id,
            name=name or self.name,
            scheme=self.scheme,
            host=self.host,
            port=self.port,
            username=self.username,
            password=SecretStr(self.password) if self.password else None,
            tags=list(tags or []),
            enabled=enabled,
            source=source,
        )

    def __str__(self) -> str:
        return self.safe_url


# ---------------------------------------------------------------------------
# Aggregated batch result
# ---------------------------------------------------------------------------
@dataclass
class ParseError:
    """One rejected line; ``message`` is always redacted."""

    line: int
    message: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}"


@dataclass
class ParseResult:
    """Outcome of parsing bulk text: accepted entries plus aggregated errors."""

    entries: list[ParsedProxy] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)
    duplicates: list[ParseError] = field(default_factory=list)

    @property
    def skipped(self) -> int:
        return len(self.duplicates)

    @property
    def ok(self) -> bool:
        return not self.errors

    def messages(self) -> list[str]:
        return [str(item) for item in (*self.errors, *self.duplicates)]


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------
def proxy_fingerprint(
    scheme: ProxyScheme | str,
    host: str,
    port: int,
    username: str | None = None,
) -> str:
    """Return a stable sha256 identity for a proxy endpoint (no password)."""
    scheme_value = _canonical_scheme_name(scheme.value if isinstance(scheme, ProxyScheme) else str(scheme))
    payload = json.dumps(
        [scheme_value, str(host).lower(), int(port), username or ""],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_proxy_id(
    scheme: ProxyScheme | str,
    host: str,
    port: int,
    username: str | None = None,
) -> str:
    """Deterministic id: ``<host-slug>-<port>-<hash8>`` (slug + short hash)."""
    digest = proxy_fingerprint(scheme, host, port, username)[:8]
    base = slugify(str(host), max_length=32).strip("-._") or "proxy"
    return f"{base}-{int(port)}-{digest}"


def proxy_url(
    scheme: ProxyScheme | str,
    host: str,
    port: int,
    username: str | None = None,
    password: str | None = None,
    *,
    mask_password: bool = False,
) -> str:
    """Build a proxy URL, percent-encoding credentials and bracketing IPv6 hosts."""
    scheme_value = scheme.value if isinstance(scheme, ProxyScheme) else str(scheme).lower()
    userinfo = ""
    if username:
        userinfo = quote(username, safe="")
        if password:
            userinfo += ":***" if mask_password else ":" + quote(password, safe="")
        userinfo += "@"
    host_value = host
    if ":" in host and not host.startswith("["):
        host_value = f"[{host}]"
    return f"{scheme_value}://{userinfo}{host_value}:{int(port)}"


# ---------------------------------------------------------------------------
# Public parsing entry points
# ---------------------------------------------------------------------------
def parse_proxy_url(url: str) -> ParsedProxy:
    """Parse a single proxy URL (or ``host:port`` token) into a :class:`ParsedProxy`."""
    raw = _clean_line(url)
    if not raw:
        raise ProxyError("empty proxy line")
    if _is_comment(raw):
        raise ProxyError("comment line is not a proxy")
    return _parse_token(raw)


def parse_proxy_line(line: str, *, line_number: int | None = None) -> ParsedProxy:
    """Parse one non-empty, non-comment line, annotating errors with ``line_number``."""
    raw = _clean_line(line)
    if not raw:
        raise ProxyError("empty proxy line")
    if _is_comment(raw):
        raise ProxyError("comment line is not a proxy")
    try:
        return _parse_token(raw)
    except ProxyError as exc:
        if line_number is None:
            raise
        raise ProxyError(f"line {line_number}: {redact_text_only(exc.message)}", hint=exc.hint) from None


def parse_proxy_text(text: str) -> ParseResult:
    """Parse bulk pasted text, aggregating validation errors and duplicates."""
    result = ParseResult()
    seen: dict[str, int] = {}
    if not text:
        return result
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = _clean_line(raw_line)
        if not line or _is_comment(line):
            continue
        try:
            parsed = _parse_token(line)
        except ProxyError as exc:
            result.errors.append(ParseError(line=line_number, message=redact_text_only(exc.message)))
            continue
        fingerprint = parsed.fingerprint
        if fingerprint in seen:
            result.duplicates.append(
                ParseError(
                    line=line_number,
                    message=f"duplicate proxy (already provided on line {seen[fingerprint]})",
                )
            )
            continue
        seen[fingerprint] = line_number
        result.entries.append(parsed)
    return result


def redact_text_only(value: str) -> str:
    """Redact a parser message (defence in depth; parser messages carry no secrets)."""
    cleaned = redact(value)
    return cleaned if isinstance(cleaned, str) else str(cleaned)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _clean_line(value: str) -> str:
    if not isinstance(value, str):
        return ""
    text = value.replace("\ufeff", "").strip()
    return _CONTROL_CHARS.sub("", text)


def _is_comment(text: str) -> bool:
    return text.startswith(COMMENT_PREFIXES)


def _parse_token(text: str) -> ParsedProxy:
    if len(text) > MAX_LINE_LENGTH:
        raise ProxyError("proxy line is too long")
    if _CSV_SPLIT.search(text):
        return _parse_csv(text)
    if "://" in text or "@" in text:
        return _parse_url(text)
    colon_form = _parse_colon_form(text)
    if colon_form is not None:
        return colon_form
    return _parse_host_port_token(text, ProxyScheme.HTTP)


def _parse_url(text: str) -> ParsedProxy:
    candidate = text
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    scheme_match = _SCHEME_IN_TEXT.match(candidate)
    scheme_name = (scheme_match.group(1) if scheme_match else "").lower()
    if scheme_name not in SUPPORTED_SCHEMES:
        raise ProxyError(f"unsupported proxy scheme: {scheme_name or 'missing'!r}")
    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise ProxyError(f"invalid proxy URL: {redact_text_only(str(exc))}") from None
    if parts.path not in ("", "/"):
        raise ProxyError("proxy URL must contain a host and port only (no path)")
    if parts.query or parts.fragment:
        raise ProxyError("proxy URL must not contain a query or fragment")
    host = parts.hostname
    if not host:
        raise ProxyError("proxy URL is missing a host")
    try:
        port = parts.port
    except ValueError:
        # Never echo the raw authority: a malformed ``user:secret`` string would
        # otherwise be printed as if it were a port.
        raise ProxyError("invalid proxy port (expected a number between 1 and 65535)") from None
    if port is None:
        port = DEFAULT_PORTS[_canonical_scheme_name(scheme_name)]
    username = unquote(parts.username) if parts.username else None
    password = unquote(parts.password) if parts.password else None
    return _build(_scheme(scheme_name), host, port, username, password)


def _parse_csv(text: str) -> ParsedProxy:
    tokens = [token.strip() for token in _CSV_SPLIT.split(text) if token.strip()]
    if not tokens:
        raise ProxyError("empty proxy line")
    scheme_name = "http"
    if tokens[0].lower() in SUPPORTED_SCHEMES:
        scheme_name = tokens.pop(0).lower()
    if not tokens:
        raise ProxyError("incomplete proxy line: missing host and port")
    first = tokens.pop(0)
    username: str | None = None
    password: str | None = None
    if "://" in first:
        parsed = _parse_url(first)
        scheme, host, port = parsed.scheme, parsed.host, parsed.port
        username, password = parsed.username, parsed.password
    elif tokens and tokens[0].isdigit() and not _has_explicit_port(first):
        host = first
        port = _port_from_text(tokens.pop(0))
        scheme = _scheme(scheme_name)
    else:
        parsed = _parse_host_port_token(first, _scheme(scheme_name))
        scheme, host, port = parsed.scheme, parsed.host, parsed.port
    if tokens:
        if username is not None or password is not None:
            raise ProxyError("credentials are specified twice")
        username = unquote(tokens.pop(0)) or None
    if tokens:
        password = unquote(tokens.pop(0)) or None
    if tokens:
        raise ProxyError("too many fields; expected at most scheme, host, port, username, password")
    return _build(scheme, host, port, username, password)


def _parse_colon_form(text: str) -> ParsedProxy | None:
    """Parse the classic ``host:port:user[:pass]`` proxy-list row."""
    parts = text.split(":")
    if len(parts) < 3 or not parts[1].isdigit():
        return None
    host = parts[0].strip()
    if not host:
        return None
    port = _port_from_text(parts[1].strip())
    username = parts[2].strip() or None
    password = ":".join(parts[3:]).strip() if len(parts) > 3 else None
    if password == "":
        password = None
    return _build(ProxyScheme.HTTP, host, port, username, password)


def _parse_host_port_token(text: str, scheme: ProxyScheme) -> ParsedProxy:
    token = text.strip().rstrip("/")
    if not token:
        raise ProxyError("empty proxy endpoint")
    if token.startswith("["):
        try:
            parts = urlsplit(f"http://{token}")
            host = parts.hostname or ""
            port = parts.port
        except ValueError:
            raise ProxyError("invalid IPv6 proxy endpoint") from None
        if port is None:
            raise ProxyError("missing proxy port; use [host]:port or a full URL")
        return _build(scheme, host, port, None, None)
    if ":" not in token:
        raise ProxyError("missing proxy port; use host:port or a full URL")
    host, _, port_text = token.rpartition(":")
    host = host.strip()
    if not host:
        raise ProxyError("proxy endpoint is missing a host")
    if not port_text.strip().isdigit():
        # The malformed segment may be a mistyped credential; never echo it.
        raise ProxyError("invalid proxy port (expected a number between 1 and 65535)")
    return _build(scheme, host, _port_from_text(port_text.strip()), None, None)


def _build(
    scheme: ProxyScheme,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
) -> ParsedProxy:
    normalised_host = normalise_host(host)
    validate_port(port)
    username = username if username else None
    password = password if password else None
    if password is not None and username is None:
        raise ProxyError("proxy password requires a username")
    return ParsedProxy(
        scheme=scheme,
        host=normalised_host,
        port=port,
        username=username,
        password=password,
    )


def normalise_host(host: str) -> str:
    """Validate and canonicalise a proxy host (lowercase, no brackets/trailing dot)."""
    value = (host or "").strip()
    if not value:
        raise ProxyError("proxy host is required")
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    value = value.rstrip(".").lower()
    if not value:
        raise ProxyError("proxy host is required")
    if len(value) > 253:
        raise ProxyError("proxy host is too long")
    if _CONTROL_CHARS.search(value) or any(char.isspace() for char in value):
        raise ProxyError("proxy host contains invalid characters")
    if ":" in value:
        if not _IPV6_CHARS.fullmatch(value):
            raise ProxyError("proxy host contains invalid characters")
        return value
    if not _HOST_CHARS.fullmatch(value):
        raise ProxyError("proxy host contains invalid characters")
    if ".." in value or value.startswith("."):
        raise ProxyError("proxy host is not a valid hostname")
    return value


def validate_port(port: int) -> None:
    if not isinstance(port, int) or isinstance(port, bool):
        raise ProxyError("proxy port must be an integer")
    if not 1 <= port <= 65535:
        raise ProxyError(f"proxy port out of range: {port}")


def _port_from_text(value: str) -> int:
    try:
        port = int(value, 10)
    except (TypeError, ValueError):
        raise ProxyError(f"invalid proxy port: {value!r}") from None
    validate_port(port)
    return port


def _scheme(name: str) -> ProxyScheme:
    canonical = _canonical_scheme_name(name)
    return ProxyScheme(canonical)


def _canonical_scheme_name(name: str) -> str:
    lowered = (name or "").lower()
    return "socks5" if lowered == "socks5h" else lowered


def _has_explicit_port(token: str) -> bool:
    return ":" in token or "://" in token
