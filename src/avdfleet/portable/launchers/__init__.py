"""Portable launchers: templates for each OS and the Python launch helper.

Run the helper with ``python -m avdfleet.portable.launchers.generator start``
(the generated scripts do exactly that); this package re-exports the public API
so the bundle builder and tests can import it directly.

The generator exports are loaded lazily on attribute access: importing the
generator eagerly here would make ``python -m avdfleet.portable.launchers.generator``
warn that the module is already imported (runpy double-import), which would leak
a Python warning into every first portable launch.
"""

from __future__ import annotations

import importlib
from typing import Any

from .templates import (
    DEFAULT_PORT,
    HEALTH_PATH,
    LINUX_LAUNCHER_FILENAME,
    MACOS_LAUNCHER_FILENAME,
    WINDOWS_LAUNCHER_FILENAME,
    LauncherSpec,
    all_launchers,
    launcher_filename,
    launcher_for_host,
    linux_launcher,
    macos_launcher,
    validate_launcher,
    windows_launcher,
)

_LAZY_GENERATOR_EXPORTS = frozenset(
    {
        "EXIT_INTERRUPTED",
        "EXIT_MISSING_RUNTIME",
        "EXIT_OK",
        "EXIT_PREFLIGHT",
        "EXIT_SERVER",
        "EXIT_USAGE",
        "USAGE",
        "HealthWait",
        "LaunchOptions",
        "LaunchPlan",
        "LauncherUsageError",
        "build_launch_plan",
        "choose_port",
        "default_spawn",
        "http_health_probe",
        "main",
        "open_ui",
        "parse_launcher_args",
        "pause_console",
        "report_failure",
        "resolve_root",
        "run_launcher",
        "serve_command",
        "show_error_dialog",
        "wait_for_health",
        "write_launchers",
    }
)


def __getattr__(name: str) -> Any:
    if name in _LAZY_GENERATOR_EXPORTS:
        module = importlib.import_module(".generator", __name__)
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LAZY_GENERATOR_EXPORTS)


__all__ = [
    "DEFAULT_PORT",
    "EXIT_INTERRUPTED",
    "EXIT_MISSING_RUNTIME",
    "EXIT_OK",
    "EXIT_PREFLIGHT",
    "EXIT_SERVER",
    "EXIT_USAGE",
    "HEALTH_PATH",
    "LINUX_LAUNCHER_FILENAME",
    "MACOS_LAUNCHER_FILENAME",
    "USAGE",
    "WINDOWS_LAUNCHER_FILENAME",
    "HealthWait",
    "LaunchOptions",
    "LaunchPlan",
    "LauncherSpec",
    "LauncherUsageError",
    "all_launchers",
    "build_launch_plan",
    "choose_port",
    "default_spawn",
    "http_health_probe",
    "launcher_filename",
    "launcher_for_host",
    "linux_launcher",
    "macos_launcher",
    "main",
    "open_ui",
    "parse_launcher_args",
    "pause_console",
    "report_failure",
    "resolve_root",
    "run_launcher",
    "serve_command",
    "show_error_dialog",
    "validate_launcher",
    "wait_for_health",
    "windows_launcher",
    "write_launchers",
]
