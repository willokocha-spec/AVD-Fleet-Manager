"""Proxy pool: text parsing, persistence and health checks."""

from .parser import (
    DEFAULT_PORTS,
    SUPPORTED_SCHEMES,
    ParsedProxy,
    ParseError,
    ParseResult,
    make_proxy_id,
    normalise_host,
    parse_proxy_line,
    parse_proxy_text,
    parse_proxy_url,
    proxy_fingerprint,
    proxy_url,
)
from .service import (
    SECRET_KEY_PREFIX,
    ProxyPoolService,
    ProxyRecord,
    ProxyRepository,
    ProxyStore,
    proxy_id_for,
)

__all__ = [
    "DEFAULT_PORTS",
    "SECRET_KEY_PREFIX",
    "SUPPORTED_SCHEMES",
    "ParseError",
    "ParseResult",
    "ParsedProxy",
    "ProxyPoolService",
    "ProxyRecord",
    "ProxyRepository",
    "ProxyStore",
    "make_proxy_id",
    "normalise_host",
    "parse_proxy_line",
    "parse_proxy_text",
    "parse_proxy_url",
    "proxy_fingerprint",
    "proxy_id_for",
    "proxy_url",
]
