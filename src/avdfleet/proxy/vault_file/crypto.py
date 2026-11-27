"""Passphrase-derived encrypted file vault.

Format (JSON, versioned)::

    {
      "format": "avdfleet-vault",
      "version": 1,
      "kdf": "pbkdf2-hmac-sha256",
      "iterations": 390000,
      "salt": "<base64, 16 bytes>",
      "nonce": "<base64, 12 bytes>",
      "ciphertext": "<base64 AES-256-GCM>",
      "created_at": "...",
      "updated_at": "..."
    }

The encryption key is derived with PBKDF2-HMAC-SHA256 (>= 390k iterations) from
the passphrase and a random per-vault salt.  The header fields that define the
KDF parameters are authenticated as AES-GCM associated data, so downgrading the
iteration count, swapping the salt or changing the format version fails
decryption.  Every write uses a fresh random nonce and is atomic
(write to a ``.tmp`` file, fsync, ``os.replace``) with mode ``0600``.

Passphrase resolution order: explicit argument, constructor passphrase, provider
callback, ``AVDFLEET_VAULT_PASSPHRASE`` environment variable, and finally (only
when explicitly enabled) a randomly generated key file with mode ``0600`` so an
unattended install without an OS keyring still works.  Plaintext is never
written to disk, and ``repr``/``str`` never expose secrets.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import secrets
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...core.errors import VaultError

try:  # pragma: no cover - declared dependency, guard a broken portable runtime
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    _CRYPTO_AVAILABLE = True
    _CRYPTO_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover
    _CRYPTO_AVAILABLE = False
    _CRYPTO_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

ENV_PASSPHRASE = "AVDFLEET_VAULT_PASSPHRASE"
FORMAT_NAME = "avdfleet-vault"
FORMAT_VERSION = 1
KDF_NAME = "pbkdf2-hmac-sha256"
KDF_ITERATIONS = 390_000
MIN_KDF_ITERATIONS = 100_000
MAX_KDF_ITERATIONS = 10_000_000
SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32
KEY_FILE_BYTES = 32
_SECRETS_ROOT_KEY = "secrets"

PassphraseProvider = Callable[[], str | None]


class EncryptedFileVault:
    """AES-256-GCM vault persisted as a single JSON file."""

    def __init__(
        self,
        path: str | Path,
        *,
        passphrase: str | None = None,
        passphrase_provider: PassphraseProvider | None = None,
        iterations: int = KDF_ITERATIONS,
        key_path: str | Path | None = None,
        allow_generated_key: bool = False,
    ) -> None:
        self._path = Path(path)
        self._key_path = Path(key_path) if key_path is not None else None
        self._passphrase = passphrase
        self._provider = passphrase_provider
        # Never create a vault weaker than the documented 390k-iteration floor.
        self._iterations = max(KDF_ITERATIONS, int(iterations))
        self._allow_generated_key = bool(allow_generated_key)
        self._locked = True
        self._key: bytes | None = None
        self._header: dict[str, Any] | None = None
        self._secrets: dict[str, str] = {}

    # -- Vault protocol -----------------------------------------------------
    def available(self) -> bool:
        return _CRYPTO_AVAILABLE

    def unlock(self, passphrase: str | None = None) -> None:
        """Derive the key and load the vault; raises :class:`VaultError` on failure."""
        _require_crypto()
        exists = self._path.exists()
        material = self._resolve_passphrase(passphrase, create_new=not exists)
        if not exists:
            salt = secrets.token_bytes(SALT_BYTES)
            self._key = _derive_key(material, salt, self._iterations)
            self._header = {
                "format": FORMAT_NAME,
                "version": FORMAT_VERSION,
                "kdf": KDF_NAME,
                "iterations": self._iterations,
                "salt": _b64encode(salt),
                "created_at": _now_iso(),
            }
            self._secrets = {}
            self._locked = False
            return
        raw = self._read_file()
        header, nonce, ciphertext = _parse_vault_bytes(raw)
        key = _derive_key(material, _b64decode(header["salt"], "salt", SALT_BYTES), int(header["iterations"]))
        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, _associated_data(header))
        except InvalidTag:
            raise VaultError(
                "incorrect vault passphrase, or the vault file is corrupted",
                hint=f"check {ENV_PASSPHRASE} / the passphrase used to create {self._path.name}",
            ) from None
        self._key = key
        self._header = header
        self._secrets = _decode_payload(plaintext)
        self._locked = False

    def lock(self) -> None:
        """Drop the in-memory key, cached passphrase and plaintext entries."""
        self._locked = True
        self._key = None
        self._header = None
        self._secrets = {}
        self._passphrase = None

    def get(self, key: str) -> str | None:
        self._ensure_unlocked()
        return self._secrets.get(key)

    def set(self, key: str, value: str) -> None:
        self._validate_key(key)
        if not isinstance(value, str):
            raise VaultError("vault values must be strings")
        self._ensure_unlocked()
        previous = self._secrets.get(key, None)
        self._secrets[key] = value
        try:
            self._persist()
        except VaultError:
            if previous is None:
                self._secrets.pop(key, None)
            else:
                self._secrets[key] = previous
            raise

    def delete(self, key: str) -> None:
        self._validate_key(key)
        self._ensure_unlocked()
        if key not in self._secrets:
            return
        previous = self._secrets.pop(key)
        try:
            self._persist()
        except VaultError:
            self._secrets[key] = previous
            raise

    def keys(self) -> list[str]:
        self._ensure_unlocked()
        return list(self._secrets)

    # -- introspection / helpers -------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def iterations(self) -> int:
        return self._iterations

    def __repr__(self) -> str:
        return (
            f"EncryptedFileVault(path={str(self._path)!r}, locked={self._locked}, "
            f"entries={len(self._secrets) if not self._locked else 0})"
        )

    __str__ = __repr__

    # -- internals ----------------------------------------------------------
    def _ensure_unlocked(self) -> None:
        if self._locked:
            self.unlock()

    def _read_file(self) -> bytes:
        try:
            return self._path.read_bytes()
        except OSError as exc:
            raise VaultError(f"could not read vault file {self._path}: {exc.strerror or exc}") from None

    def _persist(self) -> None:
        if self._key is None or self._header is None:
            raise VaultError("vault is locked")
        plaintext = json.dumps(
            {_SECRETS_ROOT_KEY: self._secrets},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        nonce = secrets.token_bytes(NONCE_BYTES)
        header = dict(self._header)
        header["updated_at"] = _now_iso()
        ciphertext = AESGCM(self._key).encrypt(nonce, plaintext, _associated_data(header))
        body = dict(header)
        body["nonce"] = _b64encode(nonce)
        body["ciphertext"] = _b64encode(ciphertext)
        try:
            payload = json.dumps(body, sort_keys=True, indent=2).encode("utf-8")
            _atomic_write(self._path, payload)
        except OSError as exc:
            raise VaultError(f"could not write vault file {self._path}: {exc.strerror or exc}") from None

    def _resolve_passphrase(self, explicit: str | None, *, create_new: bool) -> str:
        candidates: list[str | None] = [explicit, self._passphrase]
        if self._provider is not None:
            try:
                candidates.append(self._provider())
            except VaultError:
                raise
            except Exception as exc:
                raise VaultError(f"vault passphrase provider failed: {type(exc).__name__}") from None
        candidates.append(os.environ.get(ENV_PASSPHRASE))
        for candidate in candidates:
            if candidate:
                return str(candidate)
        if self._key_path is not None:
            if self._key_path.exists():
                return _read_key_file(self._key_path)
            if create_new and self._allow_generated_key:
                generated = secrets.token_urlsafe(KEY_FILE_BYTES)
                _atomic_write(self._key_path, generated.encode("utf-8"))
                return generated
        raise VaultError(
            "vault passphrase is required",
            hint=(
                f"set {ENV_PASSPHRASE}, pass a passphrase to unlock(), register an OS keyring, "
                "or allow a generated key file for this host"
            ),
        )

    @staticmethod
    def _validate_key(key: str) -> None:
        if not isinstance(key, str) or not key:
            raise VaultError("vault key must be a non-empty string")


# ---------------------------------------------------------------------------
# Crypto helpers
# ---------------------------------------------------------------------------
def _require_crypto() -> None:
    if not _CRYPTO_AVAILABLE:  # pragma: no cover - cryptography is a declared dependency
        raise VaultError(
            "encrypted file vault is unavailable: the 'cryptography' package is missing",
            hint=_CRYPTO_IMPORT_ERROR,
        )


def _derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    if iterations < MIN_KDF_ITERATIONS or iterations > MAX_KDF_ITERATIONS:
        raise VaultError(f"vault KDF iteration count is out of range: {iterations}")
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt, iterations=iterations)
    return kdf.derive(passphrase.encode("utf-8"))


def _associated_data(header: Mapping[str, Any]) -> bytes:
    """Authenticate the KDF/format parameters (not the payload or nonce)."""
    canonical = {
        "format": header["format"],
        "version": header["version"],
        "kdf": header["kdf"],
        "iterations": header["iterations"],
        "salt": header["salt"],
    }
    return json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _parse_vault_bytes(raw: bytes) -> tuple[dict[str, Any], bytes, bytes]:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise VaultError("vault file is corrupted: not valid JSON") from None
    if not isinstance(body, dict):
        raise VaultError("vault file is corrupted: unexpected structure")
    required = ("format", "version", "kdf", "iterations", "salt", "nonce", "ciphertext")
    missing = [name for name in required if name not in body]
    if missing:
        raise VaultError(f"vault file is corrupted: missing {', '.join(missing)}")
    if body["format"] != FORMAT_NAME:
        raise VaultError("vault file is corrupted: unknown format")
    if body["version"] != FORMAT_VERSION:
        raise VaultError(f"unsupported vault format version: {body['version']!r}")
    if body["kdf"] != KDF_NAME:
        raise VaultError(f"unsupported vault KDF: {body['kdf']!r}")
    if not isinstance(body["iterations"], int) or isinstance(body["iterations"], bool):
        raise VaultError("vault file is corrupted: invalid iteration count")
    _b64decode(body["salt"], "salt", SALT_BYTES)
    nonce = _b64decode(body["nonce"], "nonce", NONCE_BYTES)
    ciphertext = _b64decode(body["ciphertext"], "ciphertext", None)
    header = {name: body[name] for name in ("format", "version", "kdf", "iterations", "salt")}
    return header, nonce, ciphertext


def _decode_payload(plaintext: bytes) -> dict[str, str]:
    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise VaultError("vault payload is corrupted") from None
    if not isinstance(payload, dict):
        raise VaultError("vault payload is corrupted")
    secrets_map = payload.get(_SECRETS_ROOT_KEY)
    if not isinstance(secrets_map, dict):
        raise VaultError("vault payload is corrupted")
    result: dict[str, str] = {}
    for key, value in secrets_map.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise VaultError("vault payload is corrupted")
        result[key] = value
    return result


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64decode(value: Any, name: str, expected_length: int | None) -> bytes:
    if not isinstance(value, str):
        raise VaultError(f"vault file is corrupted: invalid {name}")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        raise VaultError(f"vault file is corrupted: invalid {name}") from None
    if expected_length is not None and len(decoded) != expected_length:
        raise VaultError(f"vault file is corrupted: invalid {name} length")
    return decoded


# ---------------------------------------------------------------------------
# Atomic file IO
# ---------------------------------------------------------------------------
def _atomic_write(path: Path, data: bytes) -> None:
    """Write *data* atomically with mode 0600 (best effort on Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def _read_key_file(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise VaultError(f"could not read vault key file {path.name}: {exc.strerror or exc}") from None
    if len(value) < 16:
        raise VaultError(f"vault key file {path.name} is corrupted")
    return value


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
