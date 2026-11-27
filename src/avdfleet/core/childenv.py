"""Child-process environment construction and secret scrubbing.

The daemon may run with long-lived secrets in its own environment (the API
bearer token, the vault passphrase).  Those secrets must never be inherited by
emulators, Appium, user scripts, SDK tools or installer commands: a user script
could otherwise authenticate to the daemon or unlock the proxy vault.

All subprocess boundaries should build their environment through
:func:`child_environment` / :func:`sanitize_child_environment` instead of
``dict(os.environ)``.

The same helper also guarantees the adb server transport limit is present, so
instance 17+ remains visible on a default adb server.  adb only reads
``ADB_LOCAL_TRANSPORT_MAX_PORT`` when its server starts; a pre-existing server
still needs a restart, which the doctor check detects.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

__all__ = [
    "ADB_LOCAL_TRANSPORT_MAX_PORT_ENV",
    "DEFAULT_ADB_LOCAL_TRANSPORT_MAX_PORT",
    "SECRET_ENV_NAMES",
    "child_environment",
    "ensure_process_adb_environment",
    "sanitize_child_environment",
]

ADB_LOCAL_TRANSPORT_MAX_PORT_ENV = "ADB_LOCAL_TRANSPORT_MAX_PORT"

#: 64 console ports are allocated as 5554..5680; adb ports are console + 1, so
#: the top adb port is 5681.  adb's own default is 5585 (16 emulators).
DEFAULT_ADB_LOCAL_TRANSPORT_MAX_PORT = 5681

#: Exact environment names that must never reach a child process.
SECRET_ENV_NAMES: frozenset[str] = frozenset(
    {
        "AVDFLEET_TOKEN",
        "AVDFLEET_VAULT_PASSPHRASE",
    }
)

#: Reserved prefix for future daemon secrets (for example bridge tokens).
SECRET_ENV_PREFIX = "AVDFLEET_SECRET_"


def _is_secret_name(name: str) -> bool:
    upper = name.upper()
    return upper in SECRET_ENV_NAMES or upper.startswith(SECRET_ENV_PREFIX)


def sanitize_child_environment(
    base: Mapping[str, Any] | None = None,
    *,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Return a copy of *base* (default: ``os.environ``) without daemon secrets.

    Values are stringified because subprocess environments require strings.
    Secret removal happens **after** *extra* is applied, so a caller cannot
    accidentally reintroduce a secret through an explicit environment override.
    """
    source = os.environ if base is None else base
    result = {
        str(key): str(value)
        for key, value in source.items()
        if not _is_secret_name(str(key))
    }
    if extra:
        for key, value in extra.items():
            if not _is_secret_name(str(key)):
                result[str(key)] = str(value)
    return result


def child_environment(
    base: Mapping[str, Any] | None = None,
    *,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Environment for a child process: sanitized plus the adb transport limit.

    An existing larger ``ADB_LOCAL_TRANSPORT_MAX_PORT`` is preserved; a missing
    or smaller value is raised to :data:`DEFAULT_ADB_LOCAL_TRANSPORT_MAX_PORT`.
    """
    env = sanitize_child_environment(base, extra=extra)
    required = DEFAULT_ADB_LOCAL_TRANSPORT_MAX_PORT
    current: int | None = None
    key = next((name for name in env if name.upper() == ADB_LOCAL_TRANSPORT_MAX_PORT_ENV), None)
    if key is not None:
        try:
            current = int(env[key])
        except (TypeError, ValueError):
            current = None
    if current is None or current < required:
        if key is not None and key != ADB_LOCAL_TRANSPORT_MAX_PORT_ENV:
            env.pop(key, None)
        env[ADB_LOCAL_TRANSPORT_MAX_PORT_ENV] = str(required)
    return env


def ensure_process_adb_environment(
    environment: dict[str, str] | None = None,
    *,
    port_end: int | None = None,
) -> bool:
    """Set the adb transport limit on the current process environment.

    *port_end* is the highest console port in use; the required adb limit is the
    next odd port.  Returns True when the value changed.  Called by the CLI
    entry point and container construction so adb servers started by direct
    subprocesses (not using the helper) inherit it.
    """
    target = os.environ if environment is None else environment
    required = DEFAULT_ADB_LOCAL_TRANSPORT_MAX_PORT
    if port_end is not None:
        required = max(required, int(port_end) + 1)
    current: int | None = None
    key = next((name for name in target if name.upper() == ADB_LOCAL_TRANSPORT_MAX_PORT_ENV), None)
    if key is not None:
        try:
            current = int(target[key])
        except (TypeError, ValueError):
            current = None
    if current is not None and current >= required:
        return False
    if key is not None and key != ADB_LOCAL_TRANSPORT_MAX_PORT_ENV:
        target.pop(key, None)
    target[ADB_LOCAL_TRANSPORT_MAX_PORT_ENV] = str(required)
    return True
