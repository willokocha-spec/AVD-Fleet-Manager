"""Android SDK tooling backends.

Two interchangeable implementations of
:class:`avdfleet.core.interfaces.SdkBackend` live behind one selector:

* :class:`~avdfleet.platform.sdk.androidcli.backend.AndroidCliBackend` -- the new
  ``android`` command-line interface (preferred when installed and supported);
* :class:`~avdfleet.platform.sdk.legacy.backend.LegacySdkBackend` -- the
  classic ``sdkmanager``/``avdmanager``/``adb``/``emulator`` toolchain.

Typical consumer code (A1/N1/I2)::

    from avdfleet.platform.sdk import get_backend

    backend = get_backend(config, host)          # prefer="auto"
    backend.name                                 # "androidcli" or "legacy"
    backend.fallback_reason                      # non-empty when degraded
    backend.list_system_images()
    backend.create_avd("avd-01", image.id, device_id="pixel_7")

The :class:`~avdfleet.platform.sdk.console.EmulatorConsole` TCP client is shared
by both toolchains (it ships with the emulator binary).
"""

from __future__ import annotations

from .androidcli import AndroidCliBackend, AndroidCliClient
from .base import (
    BaseSdkBackend,
    Runner,
    SdkError,
    ToolNotFoundError,
    ToolResult,
    get_backend,
    run_tool,
)
from .console import (
    EmulatorConsole,
    EmulatorConsoleAuthError,
    EmulatorConsoleError,
    console_token_paths,
    resolve_console_token,
)
from .legacy import Adb, AdbDevice, AvdManager, Emulator, LegacySdkBackend, SdkManager

__all__ = [
    "Adb",
    "AdbDevice",
    "AndroidCliBackend",
    "AndroidCliClient",
    "AvdManager",
    "BaseSdkBackend",
    "Emulator",
    "EmulatorConsole",
    "EmulatorConsoleAuthError",
    "EmulatorConsoleError",
    "LegacySdkBackend",
    "Runner",
    "SdkError",
    "SdkManager",
    "ToolNotFoundError",
    "ToolResult",
    "console_token_paths",
    "get_backend",
    "resolve_console_token",
    "run_tool",
]
