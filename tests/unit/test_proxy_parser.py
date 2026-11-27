"""Unit tests for the proxy-pool text parser.

Covers URL / host:port / CSV / colon proxy-list forms, percent-decoding,
comments, aggregated validation errors, duplicate detection, deterministic ids
and the guarantee that passwords never appear in ``repr``, ``str`` or errors.
"""

from __future__ import annotations

import pytest

from avdfleet.core.errors import ProxyError
from avdfleet.core.models import ProxyScheme
from avdfleet.proxy.pool.parser import (
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

SECRET = "sup3r-secret-pass"


# ---------------------------------------------------------------------------
# Valid inputs
# ---------------------------------------------------------------------------
VALID_CASES: list[tuple[str, str, str, int, str | None, str | None]] = [
    ("http://user:pass@proxy.example.com:8080", "http", "proxy.example.com", 8080, "user", "pass"),
    ("https://user:pass@proxy.example.com:8443", "https", "proxy.example.com", 8443, "user", "pass"),
    ("socks5://alice:pa55@10.0.0.5:1080", "socks5", "10.0.0.5", 1080, "alice", "pa55"),
    ("socks5h://alice:pa55@10.0.0.5:1080", "socks5", "10.0.0.5", 1080, "alice", "pa55"),
    ("HTTP://User:Pass@PROXY.Example.COM:8080", "http", "proxy.example.com", 8080, "User", "Pass"),
    ("http://proxy.example.com:8080/", "http", "proxy.example.com", 8080, None, None),
    ("http://user@proxy.example.com:8080", "http", "proxy.example.com", 8080, "user", None),
    ("proxy.example.com:3128", "http", "proxy.example.com", 3128, None, None),
    ("user:pass@proxy.example.com:3128", "http", "proxy.example.com", 3128, "user", "pass"),
    ("https://proxy.example.com", "https", "proxy.example.com", 8080, None, None),
    ("socks5://proxy.example.com", "socks5", "proxy.example.com", 1080, None, None),
    ("[::1]:8080", "http", "::1", 8080, None, None),
]


@pytest.mark.parametrize(("raw", "scheme", "host", "port", "username", "password"), VALID_CASES)
def test_parse_valid_lines(
    raw: str, scheme: str, host: str, port: int, username: str | None, password: str | None
) -> None:
    parsed = parse_proxy_line(raw)
    assert parsed.scheme is ProxyScheme(scheme)
    assert parsed.host == host
    assert parsed.port == port
    assert parsed.username == username
    assert parsed.password == password


def test_percent_decoded_credentials() -> None:
    parsed = parse_proxy_line("https://us%40er:p%40ss%3Aword@proxy.example.com:8443")
    assert parsed.username == "us@er"
    assert parsed.password == "p@ss:word"


def test_proxy_url_parsing_entry_points() -> None:
    assert parse_proxy_url("socks5://u:p@h.example.com:1080").scheme is ProxyScheme.SOCKS5
    assert parse_proxy_url("h.example.com:3128").host == "h.example.com"
    with pytest.raises(ProxyError):
        parse_proxy_url("# comment")
    with pytest.raises(ProxyError):
        parse_proxy_url("   ")


CSV_CASES: list[tuple[str, str, str, int, str | None, str | None]] = [
    ("1.2.3.4:8080,u,p", "http", "1.2.3.4", 8080, "u", "p"),
    ("1.2.3.4:8080;u;p", "http", "1.2.3.4", 8080, "u", "p"),
    ("1.2.3.4:8080\tu\tp", "http", "1.2.3.4", 8080, "u", "p"),
    ("http,1.2.3.4,8080,u,p", "http", "1.2.3.4", 8080, "u", "p"),
    ("socks5,1.2.3.4:1080,u,p", "socks5", "1.2.3.4", 1080, "u", "p"),
    ("host.example.com,8080,u,p", "http", "host.example.com", 8080, "u", "p"),
    ("1.2.3.4:8080,u", "http", "1.2.3.4", 8080, "u", None),
    ("1.2.3.4:8080,u%40x,p%20w", "http", "1.2.3.4", 8080, "u@x", "p w"),
    ("1.2.3.4:8080", "http", "1.2.3.4", 8080, None, None),
]


@pytest.mark.parametrize(("raw", "scheme", "host", "port", "username", "password"), CSV_CASES)
def test_parse_csv_like_rows(
    raw: str, scheme: str, host: str, port: int, username: str | None, password: str | None
) -> None:
    parsed = parse_proxy_line(raw)
    assert parsed.scheme is ProxyScheme(scheme)
    assert (parsed.host, parsed.port, parsed.username, parsed.password) == (host, port, username, password)


def test_parse_colon_proxy_list_rows() -> None:
    parsed = parse_proxy_line("1.2.3.4:8080:u:p")
    assert (parsed.host, parsed.port, parsed.username, parsed.password) == ("1.2.3.4", 8080, "u", "p")
    with_password_colons = parse_proxy_line("1.2.3.4:8080:u:p:a:ss")
    assert with_password_colons.password == "p:a:ss"
    username_only = parse_proxy_line("1.2.3.4:8080:u")
    assert username_only.username == "u" and username_only.password is None


# ---------------------------------------------------------------------------
# Invalid inputs and aggregated errors
# ---------------------------------------------------------------------------
INVALID_CASES: list[str] = [
    f"http://user:{SECRET}@host.example.com:99999",
    f"ftp://user:{SECRET}@host.example.com:8080",
    f"http://user:{SECRET}@:8080",
    f"http://:{SECRET}@host.example.com:8080",
    f"http://user:{SECRET}@host.example.com:8080/path",
    f"http://user:{SECRET}@host.example.com:8080?query=1",
    f"socks5://user:{SECRET}@host.example.com:notaport",
    f"http://user:{SECRET}",  # malformed authority must not echo the secret
    f"badhost:notaport:user:{SECRET}",  # malformed colon-list must not echo the secret
    "host-without-port",
    f"http://user:{SECRET}@host with spaces:8080",
    "",
    "# a comment",
    "// another comment",
]


@pytest.mark.parametrize("raw", INVALID_CASES)
def test_invalid_lines_raise_redacted_proxy_error(raw: str) -> None:
    with pytest.raises(ProxyError) as raised:
        parse_proxy_line(raw)
    assert SECRET not in str(raised.value)
    assert SECRET not in repr(raised.value)


def test_port_out_of_range_and_defaults() -> None:
    for raw in ("host.example.com:0", "host.example.com:65536", "host.example.com:-1"):
        with pytest.raises(ProxyError):
            parse_proxy_line(raw)
    assert parse_proxy_line("http://host.example.com").port == 8080
    assert parse_proxy_line("socks5://host.example.com").port == 1080


def test_parse_text_skips_comments_and_blank_lines() -> None:
    text = """
    # a comment

    // windows-style comment
    http://user:pass@one.example.com:8080
    proxy.example.com:3128
    """
    result = parse_proxy_text(text)
    assert result.ok
    assert [entry.host for entry in result.entries] == ["one.example.com", "proxy.example.com"]
    assert result.entries[0].username == "user"


def test_parse_text_aggregates_errors_with_line_numbers() -> None:
    text = "\n".join(
        [
            f"http://user:{SECRET}@host.example.com:8080",
            "nonsense line",
            "http://ok.example.com:8080",
            f"ftp://user:{SECRET}@host.example.com:1",
            "another nonsense line",
        ]
    )
    result = parse_proxy_text(text)
    assert isinstance(result, ParseResult)
    assert len(result.entries) == 2
    assert [error.line for error in result.errors] == [2, 4, 5]
    assert all(isinstance(error, ParseError) for error in result.errors)
    assert not result.ok
    messages = result.messages()
    assert messages and all(SECRET not in message for message in messages)
    assert SECRET not in "\n".join(messages)


def test_parse_text_empty_and_comments_only() -> None:
    assert parse_proxy_text("").entries == []
    assert parse_proxy_text("\n# only\n\n").entries == []
    assert parse_proxy_text("\n# only\n").errors == []


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------
def test_duplicates_are_detected_and_skipped() -> None:
    text = "\n".join(
        [
            "http://user:pass@one.example.com:8080",
            "http://user:other@ONE.example.com:8080",  # same endpoint+user, different password
            "http://other:pass@one.example.com:8080",  # different username -> distinct
            "socks5://user:pass@one.example.com:8080",  # different scheme -> distinct
            "proxy.example.com:3128",
            "proxy.example.com:3128",
        ]
    )
    result = parse_proxy_text(text)
    assert len(result.entries) == 4
    assert [duplicate.line for duplicate in result.duplicates] == [2, 6]
    assert result.skipped == 2
    assert "line 1" in result.duplicates[0].message
    assert not result.errors


def test_fingerprint_excludes_password_and_ignores_host_case() -> None:
    first = parse_proxy_line("http://user:one@proxy.example.com:8080")
    second = parse_proxy_line("http://user:two@PROXY.example.com:8080")
    assert first.fingerprint == second.fingerprint
    assert first.password != second.password
    assert proxy_fingerprint("socks5h", "H.example.com", 1080, "u") == proxy_fingerprint(
        ProxyScheme.SOCKS5, "h.example.com", 1080, "u"
    )


# ---------------------------------------------------------------------------
# Deterministic ids and URL rendering
# ---------------------------------------------------------------------------
def test_ids_are_deterministic_slug_plus_short_hash() -> None:
    first = parse_proxy_line("http://user:pass@proxy.example.com:8080")
    second = parse_proxy_line("http://user:different@proxy.example.com:8080")
    assert first.id == second.id  # password never influences the id
    assert first.id == make_proxy_id(ProxyScheme.HTTP, "proxy.example.com", 8080, "user")
    assert first.id.startswith("proxy.example.com-8080-")
    assert len(first.id.rsplit("-", 1)[-1]) == 8
    other_user = parse_proxy_line("http://other:pass@proxy.example.com:8080")
    assert other_user.id != first.id
    other_port = parse_proxy_line("http://user:pass@proxy.example.com:8081")
    assert other_port.id != first.id


def test_proxy_url_masks_secret_by_default() -> None:
    parsed = parse_proxy_line("http://user:pass@proxy.example.com:8080")
    assert parsed.url() == "http://user:***@proxy.example.com:8080"
    assert parsed.url(reveal=True) == "http://user:pass@proxy.example.com:8080"
    assert parsed.safe_url == parsed.url()
    assert str(parsed) == parsed.url()


def test_proxy_url_helper_brackets_ipv6_and_quotes_credentials() -> None:
    assert proxy_url("http", "::1", 8080, "u", "p") == "http://u:p@[::1]:8080"
    assert proxy_url(ProxyScheme.HTTP, "h.example.com", 3128) == "http://h.example.com:3128"
    assert (
        proxy_url("socks5", "h.example.com", 1080, "a@b", "c d") == "socks5://a%40b:c%20d@h.example.com:1080"
    )


def test_normalise_host_validation() -> None:
    assert normalise_host("Proxy.Example.COM.") == "proxy.example.com"
    assert normalise_host("[::1]") == "::1"
    for bad in ("", " ", "bad host", "bad/host", "host..example.com", ".example.com"):
        with pytest.raises(ProxyError):
            normalise_host(bad)


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------
def test_password_absent_from_repr_and_str() -> None:
    parsed = parse_proxy_line(f"http://user:{SECRET}@proxy.example.com:8080")
    assert SECRET not in repr(parsed)
    assert SECRET not in str(parsed)
    assert "***" in str(parsed)


def test_to_proxy_config_uses_secret_str_and_safe_repr() -> None:
    parsed = parse_proxy_line(f"http://user:{SECRET}@proxy.example.com:8080")
    config = parsed.to_proxy_config()
    assert config.password is not None
    assert config.password.get_secret_value() == SECRET
    assert SECRET not in repr(config)
    assert SECRET not in str(config)
    assert config.has_credentials is True
    assert config.id == parsed.id


def test_parsed_proxy_is_frozen() -> None:
    parsed = ParsedProxy(ProxyScheme.HTTP, "h.example.com", 8080)
    with pytest.raises(AttributeError):
        parsed.host = "other.example.com"  # type: ignore[misc]
