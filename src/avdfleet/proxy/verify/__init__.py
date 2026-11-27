"""Proxy egress verification: host-side checks and in-guest HTTP evidence.

:class:`GuestEgressVerifier` implements
:class:`avdfleet.core.interfaces.ProxyVerifier`:

* :meth:`~GuestEgressVerifier.verify_proxy` - reach the verification URL through
  an assigned proxy from the host (httpx for HTTP/HTTPS proxies, a stdlib SOCKS5
  handshake for SOCKS5 proxies) and parse the egress IP when available.
* :meth:`~GuestEgressVerifier.verify_guest` - run
  ``adb -s <serial> shell curl -sS --max-time <N> <token_url>`` (falling back to
  ``wget -qO-`` and ``toybox wget -qO-``) inside a running AVD, parse the token
  echo and egress IP, and return a redacted
  :class:`~avdfleet.core.models.ProxyTestResult`.  The runner and serial
  resolution are injectable for tests and for alternate tooling backends.

Proxy credentials are never placed on the guest command line and never logged.
"""

from __future__ import annotations

from .verifier import (
    CommandResult,
    GuestAttempt,
    GuestCommandRunner,
    GuestEgressDetails,
    GuestEgressVerifier,
    SubprocessGuestRunner,
    extract_egress_ip,
    mask_token_url,
    normalize_adb_prefix,
)

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
