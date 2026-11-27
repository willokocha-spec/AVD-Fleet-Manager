"""Error model shared by API, CLI and services.

Every error carries a stable machine code, an HTTP status for the API and an
optional remediation hint.  Messages are always redacted before they leave the
process.
"""

from __future__ import annotations

from typing import Any


class AvdFleetError(Exception):
    """Base class for all expected avdfleet failures."""

    code: str = "avdfleet_error"
    http_status: int = 400

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details or {}

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        from .logging import redact

        payload: dict[str, Any] = {
            "error": {
                "code": self.code,
                "message": redact(self.message),
            }
        }
        if self.hint:
            payload["error"]["hint"] = redact(self.hint)
        if self.details:
            payload["error"]["details"] = redact(self.details)
        return payload


class ConfigError(AvdFleetError):
    code = "config_error"


class NotFoundError(AvdFleetError):
    code = "not_found"
    http_status = 404


class ConflictError(AvdFleetError):
    code = "conflict"
    http_status = 409


class ValidationError(AvdFleetError):
    code = "invalid_request"
    http_status = 422


class UnauthorizedError(AvdFleetError):
    code = "unauthorized"
    http_status = 401


class ForbiddenError(AvdFleetError):
    code = "forbidden"
    http_status = 403


class CapacityError(AvdFleetError):
    code = "capacity_exceeded"
    http_status = 409


class InstallerError(AvdFleetError):
    code = "installer_error"
    http_status = 500


class RebootRequiredError(InstallerError):
    code = "reboot_required"
    http_status = 409


class ToolError(AvdFleetError):
    code = "tool_error"
    http_status = 500


class CommandError(ToolError):
    code = "command_failed"

    def __init__(
        self,
        message: str,
        *,
        argv: list[str] | None = None,
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint, details={"argv": argv or [], "exit_code": exit_code})
        self.argv = argv or []
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class ProxyError(AvdFleetError):
    code = "proxy_error"
    http_status = 422


class VaultError(AvdFleetError):
    code = "vault_error"
    http_status = 500


class OobeError(AvdFleetError):
    code = "oobe_error"
    http_status = 500


class InstanceError(AvdFleetError):
    code = "instance_error"
    http_status = 409


class BootTimeoutError(InstanceError):
    code = "boot_timeout"
    http_status = 504


class ScriptError(AvdFleetError):
    code = "script_error"
    http_status = 500


class AppiumError(AvdFleetError):
    code = "appium_error"
    http_status = 500


class ScreenError(AvdFleetError):
    code = "screen_error"
    http_status = 503


class PortableError(AvdFleetError):
    code = "portable_error"
    http_status = 500


class FleetSpecError(AvdFleetError):
    code = "fleet_spec_error"
    http_status = 422
