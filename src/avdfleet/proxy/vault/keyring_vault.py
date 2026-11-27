"""OS-keyring backed vault.

The ``keyring`` package is an optional dependency: when it is missing (or only
the fail/null backend is configured) :meth:`KeyringVault.available` returns
``False`` and :func:`avdfleet.proxy.vault.create_vault` falls back to the
encrypted file vault.  Secrets live in the OS credential store; this vault keeps
only an index of configured keys inside keyring so :meth:`keys` can be listed.

No secret is ever stored in this module's state, formatted into an error message
or rendered by ``repr``/``str``.
"""

from __future__ import annotations

import json
from typing import Any

from ...core.errors import VaultError

DEFAULT_SERVICE = "avdfleet"
INDEX_KEY = "__avdfleet_index__"
_RESERVED_KEYS = frozenset({INDEX_KEY})
_KEYRING_IMPORT_ERROR = ""


def _import_keyring() -> tuple[Any, Any] | tuple[None, None]:
    """Import ``keyring``/``keyring.errors`` lazily so tests and hosts without it work."""
    global _KEYRING_IMPORT_ERROR
    try:
        import keyring  # type: ignore[import-not-found]
        from keyring import errors as keyring_errors  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - optional dependency
        _KEYRING_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
        return None, None
    return keyring, keyring_errors


class KeyringVault:
    """Vault protocol implementation backed by the operating-system keyring."""

    def __init__(self, *, service: str = DEFAULT_SERVICE) -> None:
        self._service = service
        self._locked = False

    # -- availability -------------------------------------------------------
    def available(self) -> bool:
        keyring, _ = _import_keyring()
        if keyring is None:
            return False
        try:
            backend = keyring.get_keyring()
        except Exception:  # pragma: no cover - broken keyring installs
            return False
        try:
            from keyring.backends.fail import Keyring as FailKeyring  # type: ignore[import-not-found]

            if isinstance(backend, FailKeyring):
                return False
        except Exception:
            pass
        try:
            return int(getattr(backend, "priority", 1)) > 0
        except (TypeError, ValueError):  # pragma: no cover - exotic backends
            return True

    @property
    def backend_name(self) -> str:
        keyring, _ = _import_keyring()
        if keyring is None:
            return "unavailable"
        try:
            backend = keyring.get_keyring()
        except Exception:  # pragma: no cover
            return "unavailable"
        return type(backend).__name__

    # -- Vault protocol -----------------------------------------------------
    def unlock(self, passphrase: str | None = None) -> None:
        """The OS keyring has no passphrase; fail early when no backend exists."""
        if not self.available():
            raise VaultError(
                "OS keyring is not available on this host",
                hint="install a keyring backend or use the encrypted file vault",
            )
        self._locked = False

    def lock(self) -> None:
        self._locked = True

    def get(self, key: str) -> str | None:
        keyring, errors = self._require_keyring()
        if key in _RESERVED_KEYS:
            raise VaultError("vault key is reserved")
        try:
            return keyring.get_password(self._service, key)
        except Exception as exc:  # pragma: no cover - backend specific
            raise VaultError(f"OS keyring read failed: {type(exc).__name__}") from None

    def set(self, key: str, value: str) -> None:
        keyring, _ = self._require_keyring()
        if not isinstance(key, str) or not key:
            raise VaultError("vault key must be a non-empty string")
        if key in _RESERVED_KEYS:
            raise VaultError("vault key is reserved")
        if not isinstance(value, str):
            raise VaultError("vault values must be strings")
        try:
            keyring.set_password(self._service, key, value)
        except Exception as exc:  # pragma: no cover - backend specific
            raise VaultError(f"OS keyring write failed: {type(exc).__name__}") from None
        self._index_add(key)

    def delete(self, key: str) -> None:
        keyring, errors = self._require_keyring()
        if key in _RESERVED_KEYS:
            raise VaultError("vault key is reserved")
        try:
            keyring.delete_password(self._service, key)
        except Exception as exc:
            delete_error = getattr(errors, "PasswordDeleteError", None)
            if delete_error is not None and isinstance(exc, delete_error):
                pass
            else:  # pragma: no cover - backend specific
                raise VaultError(f"OS keyring delete failed: {type(exc).__name__}") from None
        self._index_remove(key)

    def keys(self) -> list[str]:
        self._require_keyring()
        return self._read_index()

    # -- index bookkeeping --------------------------------------------------
    def _require_keyring(self) -> tuple[Any, Any]:
        keyring, errors = _import_keyring()
        if keyring is None:
            raise VaultError(
                "OS keyring is not available on this host",
                hint=_KEYRING_IMPORT_ERROR or "install the 'keyring' package",
            )
        if self._locked:
            raise VaultError("vault is locked; call unlock() first")
        return keyring, errors

    def _read_index(self) -> list[str]:
        keyring = self._loaded_keyring()
        try:
            raw = keyring.get_password(self._service, INDEX_KEY)
        except Exception as exc:  # pragma: no cover - backend specific
            raise VaultError(f"OS keyring index read failed: {type(exc).__name__}") from None
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except ValueError:
            raise VaultError("OS keyring vault index is corrupted") from None
        if not isinstance(payload, list) or any(not isinstance(item, str) for item in payload):
            raise VaultError("OS keyring vault index is corrupted")
        return [item for item in payload if item not in _RESERVED_KEYS]

    def _write_index(self, keys: list[str]) -> None:
        keyring = self._loaded_keyring()
        try:
            keyring.set_password(self._service, INDEX_KEY, json.dumps(sorted(keys)))
        except Exception as exc:  # pragma: no cover - backend specific
            raise VaultError(f"OS keyring index write failed: {type(exc).__name__}") from None

    def _loaded_keyring(self) -> Any:
        keyring, _ = _import_keyring()
        if keyring is None:
            raise VaultError(
                "OS keyring is not available on this host",
                hint=_KEYRING_IMPORT_ERROR or "install the 'keyring' package",
            )
        return keyring

    def _index_add(self, key: str) -> None:
        keys = self._read_index()
        if key not in keys:
            keys.append(key)
            self._write_index(keys)

    def _index_remove(self, key: str) -> None:
        keys = self._read_index()
        if key in keys:
            keys.remove(key)
            self._write_index(keys)

    # -- introspection ------------------------------------------------------
    def __repr__(self) -> str:
        return (
            f"KeyringVault(service={self._service!r}, locked={self._locked}, backend={self.backend_name!r})"
        )

    __str__ = __repr__
