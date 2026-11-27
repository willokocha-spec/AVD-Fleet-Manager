"""Proxy subsystem: pool parsing/storage, encrypted vaults and assignment strategies.

Public surface used by the service container (H1/INT)::

    from avdfleet.proxy import AssignmentService, ProxyPoolService, create_vault

    vault = create_vault(paths, config.vault_backend)
    proxies = ProxyPoolService(store, vault, config)
    assignments = AssignmentService(store, proxies)

The local HTTP/HTTPS/SOCKS bridge (``proxy.bridge``) and guest egress verifier
(``proxy.verify``) are separate packages owned by other agents; they need a
credential-bearing config and consume
:meth:`ProxyPoolService.get_with_credentials` (internal only) or
:meth:`ProxyPoolService.upstream_url`.  Everything API-facing goes through
:meth:`ProxyPoolService.to_view` / :meth:`ProxyPoolService.list_views`, which
never include the password.
"""

from .assign import AssignmentService, strategy_names
from .pool import (
    ParsedProxy,
    ParseError,
    ParseResult,
    ProxyPoolService,
    ProxyRecord,
    ProxyRepository,
    ProxyStore,
    make_proxy_id,
    parse_proxy_line,
    parse_proxy_text,
    parse_proxy_url,
    proxy_fingerprint,
)
from .vault import EncryptedFileVault, KeyringVault, create_vault

__all__ = [
    "AssignmentService",
    "EncryptedFileVault",
    "KeyringVault",
    "ParseError",
    "ParseResult",
    "ParsedProxy",
    "ProxyPoolService",
    "ProxyRecord",
    "ProxyRepository",
    "ProxyStore",
    "create_vault",
    "make_proxy_id",
    "parse_proxy_line",
    "parse_proxy_text",
    "parse_proxy_url",
    "proxy_fingerprint",
    "strategy_names",
]
