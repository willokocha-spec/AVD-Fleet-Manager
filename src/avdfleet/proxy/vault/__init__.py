"""Secret vaults for proxy credentials.

Two backends implement :class:`avdfleet.core.interfaces.Vault`:

* :class:`~avdfleet.proxy.vault.keyring_vault.KeyringVault` -- the OS credential
  store via the optional ``keyring`` package (preferred when available);
* :class:`~avdfleet.proxy.vault_file.crypto.EncryptedFileVault` -- a
  passphrase-derived AES-256-GCM file vault (always available; zero-config
  fallback for headless hosts and the portable edition).

:func:`create_vault` selects one:

* ``backend="keyring"`` -- keyring only, raises :class:`VaultError` if absent;
* ``backend="file"`` -- file vault (passphrase from the argument, then
  ``AVDFLEET_VAULT_PASSPHRASE``, then a generated 0600 key file);
* ``backend="auto"`` (default) -- keyring when usable, otherwise file.

The file vault writes nothing unencrypted and never exposes secrets in ``repr``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...core.errors import VaultError
from ..vault_file.crypto import EncryptedFileVault, PassphraseProvider
from .keyring_vault import KeyringVault

BACKENDS: tuple[str, ...] = ("auto", "keyring", "file")

__all__ = [
    "BACKENDS",
    "EncryptedFileVault",
    "KeyringVault",
    "PassphraseProvider",
    "create_vault",
]


def create_vault(
    paths: Any,
    backend: str = "auto",
    passphrase: str | None = None,
    *,
    passphrase_provider: PassphraseProvider | None = None,
    iterations: int | None = None,
) -> Any:
    """Create the best vault available for *paths*.

    ``paths`` is an :class:`avdfleet.core.paths.Paths`-like object exposing
    ``vault_path`` (and optionally ``key_path`` for the generated-key fallback).
    The returned vault still needs an explicit :meth:`Vault.unlock` when a
    passphrase is required; file-vault operations also lazily unlock when a
    passphrase can be resolved (argument, env var, provider or key file).
    """
    requested = (backend or "auto").strip().lower()
    if requested not in BACKENDS:
        raise VaultError(
            f"unknown vault backend: {requested!r}", hint=f"expected one of {', '.join(BACKENDS)}"
        )
    if requested in ("auto", "keyring"):
        keyring_vault = KeyringVault()
        if keyring_vault.available():
            return keyring_vault
        if requested == "keyring":
            raise VaultError(
                "OS keyring backend is not available on this host",
                hint="install a keyring backend or set vault_backend = 'file'",
            )
    vault_path = getattr(paths, "vault_path", None)
    if vault_path is None:
        raise VaultError("paths object does not expose a vault_path")
    key_path = getattr(paths, "key_path", None)
    kwargs: dict[str, Any] = {
        "passphrase": passphrase,
        "passphrase_provider": passphrase_provider,
        "key_path": Path(key_path) if key_path is not None else None,
        "allow_generated_key": True,
    }
    if iterations is not None:
        kwargs["iterations"] = iterations
    return EncryptedFileVault(vault_path, **kwargs)
