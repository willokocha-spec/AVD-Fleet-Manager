"""Encrypted file vault backend (passphrase-derived, AES-256-GCM)."""

from .crypto import (
    ENV_PASSPHRASE,
    FORMAT_NAME,
    FORMAT_VERSION,
    KDF_ITERATIONS,
    KDF_NAME,
    MAX_KDF_ITERATIONS,
    MIN_KDF_ITERATIONS,
    NONCE_BYTES,
    SALT_BYTES,
    EncryptedFileVault,
)

__all__ = [
    "ENV_PASSPHRASE",
    "FORMAT_NAME",
    "FORMAT_VERSION",
    "KDF_ITERATIONS",
    "KDF_NAME",
    "MAX_KDF_ITERATIONS",
    "MIN_KDF_ITERATIONS",
    "NONCE_BYTES",
    "SALT_BYTES",
    "EncryptedFileVault",
]
