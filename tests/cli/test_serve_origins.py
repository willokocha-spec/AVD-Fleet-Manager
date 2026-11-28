"""Item 9: a wildcard bind must accept the loopback browser origin.

With ``--host 0.0.0.0 --trusted-host <ip> --port 18803`` a browser on the same
machine connects to ``http://127.0.0.1:18803``; its Origin was not in the allowlist,
so the GUI's WebSocket was rejected with 403 on every non-default port.
"""

from __future__ import annotations

from avdfleet.cli.commands.serve import resolve_security_allowlists
from avdfleet.core.config import Config


def _origins(config: Config) -> set[str]:
    return set(config.allow_origins or [])


def test_wildcard_bind_allows_the_loopback_origin_on_a_custom_port() -> None:
    config = resolve_security_allowlists(
        Config(),
        "0.0.0.0",
        18803,
        trusted_hosts=["192.168.1.50"],
    )

    origins = _origins(config)
    assert "http://127.0.0.1:18803" in origins
    assert "http://localhost:18803" in origins
    assert "http://192.168.1.50:18803" in origins


def test_wildcard_bind_does_not_allow_arbitrary_origins() -> None:
    config = resolve_security_allowlists(
        Config(),
        "0.0.0.0",
        18803,
        trusted_hosts=["192.168.1.50"],
    )

    origins = _origins(config)
    assert "http://evil.example:18803" not in origins
    assert "http://127.0.0.1:9999" not in origins


def test_https_wildcard_uses_https_origins() -> None:
    config = resolve_security_allowlists(
        Config(),
        "0.0.0.0",
        18803,
        trusted_hosts=["192.168.1.50"],
        tls=True,
    )

    origins = _origins(config)
    assert "https://127.0.0.1:18803" in origins
    assert "http://127.0.0.1:18803" not in origins


def test_concrete_loopback_bind_still_adds_both_spellings() -> None:
    config = resolve_security_allowlists(Config(), "127.0.0.1", 8765)

    origins = _origins(config)
    assert "http://127.0.0.1:8765" in origins
    assert "http://localhost:8765" in origins
