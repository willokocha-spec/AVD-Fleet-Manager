"""Unit tests for the encrypted file vault and the vault factory.

Asserts the security-relevant properties of :class:`EncryptedFileVault`:
round-trip through an encrypted file, wrong-passphrase and corruption failures,
versioned header with a strong PBKDF2 KDF, no plaintext on disk, atomic writes,
mode 0600 (POSIX), lock/unlock, env/callback/generated-key passphrase
resolution, and graceful keyring fallback when the optional dependency is gone.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import sys
import types
from pathlib import Path

import pytest

from avdfleet.core.errors import VaultError
from avdfleet.core.paths import Paths
from avdfleet.proxy.vault import BACKENDS, EncryptedFileVault, KeyringVault, create_vault
from avdfleet.proxy.vault_file import ENV_PASSPHRASE, FORMAT_NAME, FORMAT_VERSION, KDF_NAME

PASSPHRASE = "correct horse battery staple"
SECRET = "s3cr3t-vault-value-42"


@pytest.fixture(autouse=True)
def _clear_passphrase_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_PASSPHRASE, raising=False)


def _paths(tmp_path: Path) -> Paths:
    return Paths(root=tmp_path, portable=True).ensure()


def _file_vault(tmp_path: Path, passphrase: str | None = PASSPHRASE) -> EncryptedFileVault:
    vault = create_vault(_paths(tmp_path), backend="file", passphrase=passphrase)
    assert isinstance(vault, EncryptedFileVault)
    return vault


def _all_file_bytes(root: Path) -> list[tuple[Path, bytes]]:
    return [(path, path.read_bytes()) for path in root.rglob("*") if path.is_file()]


def test_vaults_implement_core_vault_protocol(tmp_path: Path) -> None:
    from avdfleet.core.interfaces import Vault

    file_vault = _file_vault(tmp_path)
    assert isinstance(file_vault, Vault)
    assert isinstance(KeyringVault(), Vault)


# ---------------------------------------------------------------------------
# Round-trip and header
# ---------------------------------------------------------------------------
def test_roundtrip_reopen_and_delete(tmp_path: Path) -> None:
    vault = _file_vault(tmp_path)
    vault.set("proxy/p1", SECRET)
    vault.set("proxy/p2", "another-secret")
    assert vault.get("proxy/p1") == SECRET
    assert set(vault.keys()) == {"proxy/p1", "proxy/p2"}
    assert vault.get("missing") is None

    reopened = _file_vault(tmp_path)
    assert reopened.get("proxy/p1") == SECRET
    reopened.delete("proxy/p1")
    assert reopened.get("proxy/p1") is None
    reopened.delete("proxy/p1")  # idempotent
    assert set(_file_vault(tmp_path).keys()) == {"proxy/p2"}


def test_header_is_versioned_with_strong_kdf(tmp_path: Path) -> None:
    vault = _file_vault(tmp_path)
    vault.set("k", SECRET)
    body = json.loads(_paths(tmp_path).vault_path.read_text(encoding="utf-8"))
    assert body["format"] == FORMAT_NAME
    assert body["version"] == FORMAT_VERSION
    assert body["kdf"] == KDF_NAME
    assert isinstance(body["iterations"], int)
    assert body["iterations"] >= 390_000
    assert len(base64.b64decode(body["salt"])) == 16
    assert len(base64.b64decode(body["nonce"])) == 12
    assert body["ciphertext"]
    assert "created_at" in body and "updated_at" in body


def test_no_plaintext_on_disk(tmp_path: Path) -> None:
    vault = _file_vault(tmp_path)
    vault.set("proxy/p1", SECRET)
    for path, data in _all_file_bytes(tmp_path):
        assert SECRET.encode() not in data, f"plaintext secret leaked into {path}"
        assert PASSPHRASE.encode() not in data, f"passphrase leaked into {path}"
    # Every write is atomic: no temporary files survive.
    assert not [path for path in tmp_path.rglob("*.tmp")]


def test_posix_vault_file_mode_is_0600(tmp_path: Path) -> None:
    vault = _file_vault(tmp_path)
    vault.set("proxy/p1", SECRET)
    mode = stat.S_IMODE(_paths(tmp_path).vault_path.stat().st_mode)
    if os.name != "nt":  # Windows only models the read-only bit via chmod
        assert mode == 0o600


def test_generated_key_file_is_0600_on_posix(tmp_path: Path) -> None:
    vault = create_vault(_paths(tmp_path), backend="file")
    vault.set("k", SECRET)
    mode = stat.S_IMODE(_paths(tmp_path).key_path.stat().st_mode)
    if os.name != "nt":
        assert mode == 0o600


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------
def test_wrong_passphrase_raises_vault_error(tmp_path: Path) -> None:
    vault = _file_vault(tmp_path)
    vault.set("proxy/p1", SECRET)
    wrong = "definitely the wrong passphrase"
    other = _file_vault(tmp_path, passphrase=wrong)
    with pytest.raises(VaultError) as raised:
        other.unlock(wrong)
    assert PASSPHRASE not in str(raised.value)
    assert wrong not in str(raised.value)
    assert SECRET not in str(raised.value)


def test_corrupted_json_raises_vault_error(tmp_path: Path) -> None:
    _file_vault(tmp_path).set("k", SECRET)
    _paths(tmp_path).vault_path.write_text("this is not json", encoding="utf-8")
    with pytest.raises(VaultError):
        _file_vault(tmp_path).get("k")


def test_tampered_ciphertext_raises_vault_error(tmp_path: Path) -> None:
    vault = _file_vault(tmp_path)
    vault.set("k", SECRET)
    path = _paths(tmp_path).vault_path
    body = json.loads(path.read_text(encoding="utf-8"))
    raw = bytearray(base64.b64decode(body["ciphertext"]))
    raw[0] ^= 0xFF
    body["ciphertext"] = base64.b64encode(bytes(raw)).decode("ascii")
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(VaultError):
        _file_vault(tmp_path).get("k")


def test_downgraded_kdf_parameters_are_authenticated(tmp_path: Path) -> None:
    vault = _file_vault(tmp_path)
    vault.set("k", SECRET)
    path = _paths(tmp_path).vault_path
    body = json.loads(path.read_text(encoding="utf-8"))
    body["iterations"] = 100_000  # header is AES-GCM associated data -> tag mismatch
    body["salt"] = base64.b64encode(b"\x00" * 16).decode("ascii")
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(VaultError):
        _file_vault(tmp_path).get("k")


def test_missing_required_header_field_is_corruption(tmp_path: Path) -> None:
    vault = _file_vault(tmp_path)
    vault.set("k", SECRET)
    path = _paths(tmp_path).vault_path
    body = json.loads(path.read_text(encoding="utf-8"))
    del body["salt"]
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(VaultError):
        _file_vault(tmp_path).get("k")


def test_unlock_without_available_passphrase_raises(tmp_path: Path) -> None:
    vault = EncryptedFileVault(_paths(tmp_path).vault_path)
    with pytest.raises(VaultError) as raised:
        vault.get("k")
    assert "passphrase" in str(raised.value).lower()


def test_set_requires_string_values(tmp_path: Path) -> None:
    vault = _file_vault(tmp_path)
    with pytest.raises(VaultError):
        vault.set("k", 123)  # type: ignore[arg-type]
    with pytest.raises(VaultError):
        vault.set("", "value")


# ---------------------------------------------------------------------------
# Locking and passphrase resolution
# ---------------------------------------------------------------------------
def test_lock_and_unlock(tmp_path: Path) -> None:
    vault = _file_vault(tmp_path)
    vault.set("proxy/p1", SECRET)
    assert vault.locked is False
    vault.lock()
    assert vault.locked is True
    assert "proxy/p1" not in repr(vault)
    with pytest.raises(VaultError):
        vault.get("proxy/p1")
    vault.unlock(PASSPHRASE)
    assert vault.locked is False
    assert vault.get("proxy/p1") == SECRET


def test_env_passphrase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_PASSPHRASE, PASSPHRASE)
    vault = create_vault(_paths(tmp_path), backend="file")
    vault.set("proxy/p1", SECRET)
    monkeypatch.delenv(ENV_PASSPHRASE)
    reopened = create_vault(_paths(tmp_path), backend="file")
    with pytest.raises(VaultError):
        reopened.get("proxy/p1")
    monkeypatch.setenv(ENV_PASSPHRASE, PASSPHRASE)
    assert create_vault(_paths(tmp_path), backend="file").get("proxy/p1") == SECRET


def test_explicit_passphrase_wins_over_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_PASSPHRASE, "wrong-env-passphrase")
    vault = create_vault(_paths(tmp_path), backend="file", passphrase=PASSPHRASE)
    vault.set("k", SECRET)
    assert _file_vault(tmp_path).get("k") == SECRET


def test_passphrase_provider_callback(tmp_path: Path) -> None:
    calls: list[int] = []

    def provider() -> str:
        calls.append(1)
        return PASSPHRASE

    vault = create_vault(_paths(tmp_path), backend="file", passphrase_provider=provider)
    vault.set("k", SECRET)
    assert calls
    assert create_vault(_paths(tmp_path), backend="file", passphrase_provider=provider).get("k") == SECRET


def test_provider_failure_is_a_vault_error(tmp_path: Path) -> None:
    def broken() -> str:
        raise RuntimeError("secrets backend unavailable")

    vault = create_vault(_paths(tmp_path), backend="file", passphrase_provider=broken)
    with pytest.raises(VaultError):
        vault.set("k", SECRET)


def test_generated_key_file_fallback(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    vault = create_vault(paths, backend="file")
    vault.set("proxy/p1", SECRET)
    assert paths.key_path.exists()
    assert paths.vault_path.exists()
    key_bytes = paths.key_path.read_bytes()
    assert SECRET.encode() not in key_bytes
    reopened = create_vault(paths, backend="file")
    assert reopened.get("proxy/p1") == SECRET
    for path, data in _all_file_bytes(tmp_path):
        assert SECRET.encode() not in data, f"plaintext secret leaked into {path}"


# ---------------------------------------------------------------------------
# Vault factory and keyring backend
# ---------------------------------------------------------------------------
def test_create_vault_auto_falls_back_to_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KeyringVault, "available", lambda self: False)
    vault = create_vault(_paths(tmp_path), backend="auto")
    assert isinstance(vault, EncryptedFileVault)
    vault.set("k", SECRET)
    assert vault.get("k") == SECRET


def test_create_vault_keyring_only_requires_keyring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KeyringVault, "available", lambda self: False)
    with pytest.raises(VaultError):
        create_vault(_paths(tmp_path), backend="keyring")
    with pytest.raises(VaultError):
        create_vault(_paths(tmp_path), backend="not-a-backend")
    with pytest.raises(VaultError):
        create_vault(object(), backend="file")
    assert BACKENDS == ("auto", "keyring", "file")


def test_keyring_unavailable_is_graceful(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("avdfleet.proxy.vault.keyring_vault._import_keyring", lambda: (None, None))
    vault = KeyringVault()
    assert vault.available() is False
    with pytest.raises(VaultError):
        vault.unlock()
    with pytest.raises(VaultError):
        vault.get("k")
    assert "KeyringVault" in repr(vault)
    assert SECRET not in repr(vault)
    assert "service='avdfleet'" in repr(vault)


def _install_fake_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    store: dict[tuple[str, str], str] = {}
    module = types.ModuleType("keyring")

    class FakePasswordDeleteError(Exception):
        pass

    class FakeNoKeyringError(Exception):
        pass

    backend = types.SimpleNamespace(priority=1)

    def get_password(service: str, username: str) -> str | None:
        return store.get((service, username))

    def set_password(service: str, username: str, value: str) -> None:
        store[(service, username)] = value

    def delete_password(service: str, username: str) -> None:
        if (service, username) not in store:
            raise FakePasswordDeleteError(username)
        del store[(service, username)]

    module.get_keyring = lambda: backend  # type: ignore[attr-defined]
    module.get_password = get_password  # type: ignore[attr-defined]
    module.set_password = set_password  # type: ignore[attr-defined]
    module.delete_password = delete_password  # type: ignore[attr-defined]
    errors_module = types.ModuleType("keyring.errors")
    errors_module.PasswordDeleteError = FakePasswordDeleteError  # type: ignore[attr-defined]
    errors_module.NoKeyringError = FakeNoKeyringError  # type: ignore[attr-defined]
    module.errors = errors_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", module)
    monkeypatch.setitem(sys.modules, "keyring.errors", errors_module)
    return store


def test_keyring_vault_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _install_fake_keyring(monkeypatch)
    vault = KeyringVault()
    assert vault.available() is True
    vault.set("proxy/p1", SECRET)
    assert vault.get("proxy/p1") == SECRET
    assert vault.keys() == ["proxy/p1"]
    assert ("avdfleet", "proxy/p1") in store
    assert SECRET not in repr(vault) and SECRET not in str(vault)

    vault.lock()
    with pytest.raises(VaultError):
        vault.get("proxy/p1")
    vault.unlock()
    assert vault.get("proxy/p1") == SECRET

    vault.delete("proxy/p1")
    assert vault.get("proxy/p1") is None
    assert vault.keys() == []
    vault.delete("proxy/p1")  # deleting a missing key is a no-op


def test_create_vault_auto_uses_keyring_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_keyring(monkeypatch)
    vault = create_vault(object(), backend="auto")
    assert isinstance(vault, KeyringVault)
    assert vault.available() is True


def test_keyring_reserved_index_key_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_keyring(monkeypatch)
    vault = KeyringVault()
    with pytest.raises(VaultError):
        vault.set("__avdfleet_index__", "hijack")
    with pytest.raises(VaultError):
        vault.set("", "value")
