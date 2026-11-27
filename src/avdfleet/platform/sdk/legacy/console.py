"""Legacy-toolchain entry point to the emulator console client (S1b).

The console protocol is identical for both toolchains (it ships with the
emulator binary), so the implementation lives in
:mod:`avdfleet.platform.sdk.console` and is re-exported here for discoverability
next to the other legacy-tool wrappers.
"""

from __future__ import annotations

from ..console import (
    DEFAULT_COMMAND_TIMEOUT_S,
    DEFAULT_CONNECT_TIMEOUT_S,
    DEFAULT_CONSOLE_HOST,
    EmulatorConsole,
    EmulatorConsoleAuthError,
    EmulatorConsoleError,
    console_token_paths,
    resolve_console_token,
)

__all__ = [
    "DEFAULT_COMMAND_TIMEOUT_S",
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_CONSOLE_HOST",
    "EmulatorConsole",
    "EmulatorConsoleAuthError",
    "EmulatorConsoleError",
    "console_token_paths",
    "resolve_console_token",
]
