"""Unit tests for child-process environment hardening and adb limits."""

from __future__ import annotations

import os

from avdfleet.core.childenv import (
    ADB_LOCAL_TRANSPORT_MAX_PORT_ENV,
    DEFAULT_ADB_LOCAL_TRANSPORT_MAX_PORT,
    child_environment,
    ensure_process_adb_environment,
    sanitize_child_environment,
)


def test_sanitizer_removes_daemon_secrets_and_reserved_prefix() -> None:
    env = {
        "PATH": "/bin",
        "AVDFLEET_TOKEN": "bearer-secret",
        "AVDFLEET_VAULT_PASSPHRASE": "vault-secret",
        "AVDFLEET_SECRET_BRIDGE": "future-secret",
        "AVDFLEET_SERIAL": "emulator-5554",
    }
    clean = sanitize_child_environment(env)
    assert clean == {"PATH": "/bin", "AVDFLEET_SERIAL": "emulator-5554"}


def test_sanitizer_cannot_reintroduce_secret_through_extra() -> None:
    clean = sanitize_child_environment(
        {"PATH": "/bin"},
        extra={"AVDFLEET_TOKEN": "reintroduced", "APPIUM_URL": "http://127.0.0.1:4723"},
    )
    assert "AVDFLEET_TOKEN" not in clean
    assert clean["APPIUM_URL"] == "http://127.0.0.1:4723"


def test_sanitizer_matches_secret_names_case_insensitively() -> None:
    clean = sanitize_child_environment({"avdfleet_token": "x", "AVDFLEET_VAULT_PASSPHRASE": "y"})
    assert clean == {}


def test_child_environment_sets_adb_local_transport_limit() -> None:
    env = child_environment({})
    assert env[ADB_LOCAL_TRANSPORT_MAX_PORT_ENV] == str(DEFAULT_ADB_LOCAL_TRANSPORT_MAX_PORT)
    assert DEFAULT_ADB_LOCAL_TRANSPORT_MAX_PORT >= 5681


def test_child_environment_preserves_a_higher_adb_limit() -> None:
    env = child_environment({ADB_LOCAL_TRANSPORT_MAX_PORT_ENV: "9999"})
    assert env[ADB_LOCAL_TRANSPORT_MAX_PORT_ENV] == "9999"


def test_child_environment_raises_a_lower_adb_limit() -> None:
    env = child_environment({ADB_LOCAL_TRANSPORT_MAX_PORT_ENV: "5585"})
    assert env[ADB_LOCAL_TRANSPORT_MAX_PORT_ENV] == str(DEFAULT_ADB_LOCAL_TRANSPORT_MAX_PORT)


def test_ensure_process_environment_is_idempotent(monkeypatch) -> None:
    monkeypatch.delenv(ADB_LOCAL_TRANSPORT_MAX_PORT_ENV, raising=False)
    assert ensure_process_adb_environment() is True
    assert os.environ[ADB_LOCAL_TRANSPORT_MAX_PORT_ENV] == str(
        DEFAULT_ADB_LOCAL_TRANSPORT_MAX_PORT
    )
    assert ensure_process_adb_environment() is False
