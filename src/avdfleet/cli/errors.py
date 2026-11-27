"""CLI error model: exit codes, redacted reporting and the command guard.

Exit codes are part of the CLI contract (BRIEF section3.9) and are shared by every
command:

======  ==================================================================
code    meaning
======  ==================================================================
0       success
1       generic failure (failed check, partially failed operation)
2       usage error (bad arguments, invalid input document)
3       environment/blocked (missing tooling, unavailable service)
4       not found (image/template/instance/proxy/run/...)
5       conflict (already running, capacity exceeded, duplicate name)
6       reboot required (the host must reboot; not during install)
3010    reboot required during install (Windows convention; the
        bootstrap script reboots and runs avdfleet install --resume)
======  ==================================================================

Expected :class:`~avdfleet.core.errors.AvdFleetError` failures are printed as a
single redacted message (never a stack trace, never a secret); unexpected
exceptions are reported the same way unless --verbose is set.
"""

from __future__ import annotations

import functools
import os
import traceback
from collections.abc import Callable
from typing import Any, TypeVar

import typer

from avdfleet.core.errors import (
    AppiumError,
    AvdFleetError,
    BootTimeoutError,
    CapacityError,
    ConfigError,
    ConflictError,
    FleetSpecError,
    InstallerError,
    InstanceError,
    NotFoundError,
    OobeError,
    PortableError,
    ProxyError,
    RebootRequiredError,
    ScreenError,
    ScriptError,
    ToolError,
    ValidationError,
    VaultError,
)
from avdfleet.core.logging import redact_text

try:  # pydantic's ValidationError is a usage error, not an internal failure
    from pydantic import ValidationError as PydanticValidationError
except Exception:  # pragma: no cover - pydantic is a hard dependency
    PydanticValidationError = ()  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_ENVIRONMENT = 3
EXIT_NOT_FOUND = 4
EXIT_CONFLICT = 5
EXIT_REBOOT = 6
EXIT_WINDOWS_REBOOT = 3010

#: Ordered from most specific to least specific; first match wins.
_EXIT_MAP: tuple[tuple[type[BaseException], int], ...] = (
    (BootTimeoutError, EXIT_ERROR),
    (NotFoundError, EXIT_NOT_FOUND),
    (ConflictError, EXIT_CONFLICT),
    (CapacityError, EXIT_CONFLICT),
    (ValidationError, EXIT_USAGE),
    (ConfigError, EXIT_USAGE),
    (FleetSpecError, EXIT_USAGE),
    (ProxyError, EXIT_USAGE),
    (ToolError, EXIT_ENVIRONMENT),
    (InstallerError, EXIT_ENVIRONMENT),
    (PortableError, EXIT_ENVIRONMENT),
    (VaultError, EXIT_ENVIRONMENT),
    (ScreenError, EXIT_ENVIRONMENT),
    (AppiumError, EXIT_ENVIRONMENT),
    (InstanceError, EXIT_CONFLICT),
    (OobeError, EXIT_ERROR),
    (ScriptError, EXIT_ERROR),
)


class CliError(AvdFleetError):
    """Base class for errors raised by the CLI itself."""

    code = "cli_error"
    http_status = 400


class CliUsageError(CliError):
    """The user's invocation or input document is invalid."""

    code = "usage_error"
    http_status = 422


class CliServiceError(CliError):
    """A service required by the command is not available in this build."""

    code = "service_unavailable"
    http_status = 503

    def __init__(
        self,
        message: str,
        *,
        service: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.service = service


class CliEnvironmentError(CliError):
    """The host environment prevents the command from running."""

    code = "environment_error"
    http_status = 503


def exit_code_for(error: BaseException, *, command: str | None = None) -> int:
    """Map any exception to the documented CLI exit code."""
    if isinstance(error, RebootRequiredError):
        # Windows may reboot once; the installer reports the reboot contract as
        # 3010 so the bootstrap script can reboot and resume.  Other commands
        # report the plain reboot code.
        root = (command or "").split(" ", 1)[0].strip().lower()
        return EXIT_WINDOWS_REBOOT if root in {"install", "repair"} else EXIT_REBOOT
    for error_type, code in _EXIT_MAP:
        if isinstance(error, error_type):
            return code
    if isinstance(error, PydanticValidationError):
        return EXIT_USAGE
    if isinstance(error, FileNotFoundError):
        return EXIT_NOT_FOUND
    if isinstance(error, (PermissionError, NotADirectoryError)):
        return EXIT_ENVIRONMENT
    if isinstance(error, AvdFleetError):
        return {
            400: EXIT_USAGE,
            401: EXIT_ENVIRONMENT,
            403: EXIT_ENVIRONMENT,
            404: EXIT_NOT_FOUND,
            409: EXIT_CONFLICT,
            422: EXIT_USAGE,
            500: EXIT_ENVIRONMENT,
            501: EXIT_ENVIRONMENT,
            503: EXIT_ENVIRONMENT,
            504: EXIT_ERROR,
        }.get(int(getattr(error, "http_status", 400)), EXIT_ERROR)
    return EXIT_ERROR


# ---------------------------------------------------------------------------
# Redacted reporting
# ---------------------------------------------------------------------------


def build_error_payload(error: BaseException, *, command: str | None = None) -> dict[str, Any]:
    """Return the machine-readable error envelope (always redacted)."""
    if isinstance(error, AvdFleetError):
        payload: dict[str, Any] = error.to_dict()
    else:
        payload = {
            "error": {
                "code": "internal_error",
                "message": redact_text(str(error)) or type(error).__name__,
            }
        }
    payload["ok"] = False
    if command:
        payload["command"] = command
    payload["exit_code"] = exit_code_for(error, command=command)
    return payload


def _looks_like_click_context(value: Any) -> bool:
    """Typer vendors click; the context class lives in typer._click.core."""
    cls = type(value)
    if cls.__name__ != "Context":
        return False
    return "click" in str(getattr(cls, "__module__", ""))


def _find_context(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    candidate = kwargs.get("ctx")
    if candidate is not None:
        return candidate
    for value in args:
        if _looks_like_click_context(value):
            return value
    return None


def _report(
    error: BaseException,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    command: str,
) -> None:
    """Print *error* to the right stream using its redacted envelope."""
    from .context import CliContext, get_context  # local import: avoids a cycle
    from .output import Output, jsonable

    ctx = _find_context(args, kwargs)
    out: Output
    verbose = False
    try:
        cli_context: CliContext = get_context(ctx)
        out = cli_context.output()
        verbose = cli_context.verbose
    except Exception:  # pragma: no cover - context recovery must never fail
        out = Output()

    payload = jsonable(build_error_payload(error, command=command))
    if out.json_output:
        out.emit_json(payload)
        return

    if isinstance(error, AvdFleetError):
        out.error(redact_text(error.message))
        if error.hint:
            out.error(f"hint: {redact_text(error.hint)}", prefix=False)
        if verbose:
            out.error(f"code: {error.code}", prefix=False)
        return

    out.error(f"unexpected error: {redact_text(str(error)) or type(error).__name__}")
    if verbose:
        formatted = traceback.format_exc()
        redacted = redact_text(formatted)
        for line in redacted.rstrip().splitlines():
            out.error(line, prefix=False)


F = TypeVar("F", bound=Callable[..., Any])


def command_errors(name: str) -> Callable[[F], F]:
    """Guard a command: no stack traces, consistent exit codes.

    Apply **below** the Typer command decorator so Typer registers the guarded
    wrapper::

        @images_app.command("list")
        @command_errors("images list")
        def images_list(ctx: typer.Context, ...): ...
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if os.environ.get("AVDFLEET_CLI_DEBUG") == "1":  # pragma: no cover - debugging aid
                return func(*args, **kwargs)
            try:
                return func(*args, **kwargs)
            except typer.Exit:
                raise
            except SystemExit:
                raise
            except AvdFleetError as exc:
                _report(exc, args, kwargs, name)
                raise typer.Exit(exit_code_for(exc, command=name)) from None
            except Exception as exc:  # noqa: BLE001 - the CLI must not leak tracebacks
                _report(exc, args, kwargs, name)
                raise typer.Exit(EXIT_ERROR) from None

        return wrapper  # type: ignore[return-value]

    return decorator


def reboot_payload(error: RebootRequiredError) -> dict[str, Any]:
    """Extract the installer manifest attached to a reboot error, if any."""
    manifest = getattr(error, "manifest", None)
    payload: dict[str, Any] = {"pending": True}
    if manifest is not None and hasattr(manifest, "model_dump"):
        payload["manifest"] = manifest.model_dump(mode="json")
    return payload


def usage_error(message: str) -> CliUsageError:
    """Convenience constructor for invalid CLI input."""
    return CliUsageError(message)


__all__ = [
    "EXIT_CONFLICT",
    "EXIT_ENVIRONMENT",
    "EXIT_ERROR",
    "EXIT_MAP_FOR_TESTS",
    "EXIT_NOT_FOUND",
    "EXIT_OK",
    "EXIT_REBOOT",
    "EXIT_USAGE",
    "EXIT_WINDOWS_REBOOT",
    "CliEnvironmentError",
    "CliError",
    "CliServiceError",
    "CliUsageError",
    "build_error_payload",
    "command_errors",
    "exit_code_for",
    "reboot_payload",
    "usage_error",
]

#: Exposed only so tests can assert the mapping table stays ordered/specific.
EXIT_MAP_FOR_TESTS = _EXIT_MAP
