"""Uniform error envelopes and exception handlers.

Every failure leaves the daemon as::

    {"error": {"code": "...", "message": "...", "hint": "...", "details": {...}}}

matching ``contracts/openapi.yaml#/components/schemas/ErrorResponse``.  Stack
traces are never returned to clients; unexpected errors are logged (with
redaction) and reported as a generic ``internal_error`` with a request id that
operators can grep for.

Security headers (``nosniff``, frame denial, referrer policy) and the
``X-Request-ID`` correlation header are attached here for error responses and by
the request middleware for successful responses.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from avdfleet.core.errors import AvdFleetError
from avdfleet.core.logging import redact

logger = logging.getLogger("avdfleet.api.errors")

REQUEST_ID_HEADER = "X-Request-ID"

#: Baseline security headers for every daemon response.
SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Permitted-Cross-Domain-Policies": "none",
}

_HTTP_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    409: "conflict",
    410: "gone",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "invalid_request",
    429: "rate_limited",
    500: "internal_error",
    501: "not_implemented",
    502: "bad_gateway",
    503: "service_unavailable",
    504: "timeout",
}

_HTTP_MESSAGES: dict[int, str] = {
    400: "Bad request",
    401: "Missing or invalid bearer token",
    403: "Forbidden",
    404: "Resource not found",
    405: "Method not allowed",
    409: "Conflicting state",
    422: "Request validation failed",
    500: "Internal server error",
    501: "Operation not implemented",
    502: "Upstream error",
    503: "A required dependency is unavailable",
    504: "Operation timed out",
}

_HTTP_HINTS: dict[int, str] = {
    401: "send 'Authorization: Bearer <token>' (see the token file in the data directory)",
    404: "the route or resource does not exist",
    405: "this HTTP method is not supported on this path",
    429: "slow down and retry later",
}


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    hint: str | None = None,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build a redacted :class:`JSONResponse` in the contract error envelope."""
    error: dict[str, Any] = {"code": code, "message": redact(message)}
    if hint:
        error["hint"] = redact(hint)
    if details:
        error["details"] = redact(details)
    merged = common_headers(request)
    if headers:
        merged.update({key: value for key, value in headers.items() if value is not None})
    return JSONResponse({"error": error}, status_code=status_code, headers=merged)


def request_id_for(request: Request) -> str:
    """Return the request correlation id, generating one if absent."""
    value = getattr(request.state, "request_id", None)
    if isinstance(value, str) and value:
        return value
    generated = uuid.uuid4().hex
    try:
        request.state.request_id = generated
    except Exception:  # pragma: no cover - state is always writable
        pass
    return generated


def common_headers(request: Request) -> dict[str, str]:
    """Security/correlation headers shared by normal and error responses."""
    headers = dict(SECURITY_HEADERS)
    headers[REQUEST_ID_HEADER] = request_id_for(request)
    if request.url.path.startswith("/api/"):
        headers["Cache-Control"] = "no-store"
    return headers


def apply_common_headers(request: Request, response: Any) -> None:
    """Attach security headers without overwriting ones a handler chose."""
    for key, value in common_headers(request).items():
        if key not in response.headers:
            response.headers[key] = value


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def avdfleet_error_handler(request: Request, exc: AvdFleetError) -> JSONResponse:
    """Map an expected :class:`AvdFleetError` to its contract status/code."""
    status = getattr(exc, "http_status", 500)
    if not isinstance(status, int) or not 400 <= status <= 599:
        status = 500
    headers = dict(getattr(exc, "headers", None) or {})
    if status == 401:
        headers.setdefault("WWW-Authenticate", 'Bearer realm="avdfleet"')
    try:
        payload = exc.to_dict()
    except Exception:  # pragma: no cover - to_dict is pure
        payload = {"error": {"code": getattr(exc, "code", "avdfleet_error"), "message": "request failed"}}
    merged = common_headers(request)
    merged.update({key: value for key, value in headers.items() if value is not None})
    return JSONResponse(payload, status_code=status, headers=merged)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Catch-all mapping for Starlette HTTP errors (404 for unknown routes etc.)."""
    status = int(getattr(exc, "status_code", 500) or 500)
    code = _HTTP_CODES.get(status, "http_error")
    detail = getattr(exc, "detail", None)
    if status >= 500:
        message = _HTTP_MESSAGES.get(status, "Internal server error")
    elif isinstance(detail, str) and detail.strip():
        message = detail
    else:
        message = _HTTP_MESSAGES.get(status, "Request failed")
    headers = dict(getattr(exc, "headers", None) or {})
    return error_response(
        request,
        status_code=status,
        code=code,
        message=message,
        hint=_HTTP_HINTS.get(status),
        headers=headers,
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return the contract 422 envelope instead of FastAPI's default body.

    The raw pydantic errors are stripped: ``input``/``ctx`` may contain the
    submitted proxy password or token and must never be reflected.
    """
    details = _validation_details(exc.errors())
    return error_response(
        request,
        status_code=422,
        code="invalid_request",
        message="Request validation failed",
        hint="check the request body, query string and path parameters",
        details={"errors": details},
    )


async def pydantic_validation_error_handler(request: Request, exc: PydanticValidationError) -> JSONResponse:
    """Map model validation errors raised inside handlers to a 422 envelope."""
    details = _validation_details(exc.errors())
    return error_response(
        request,
        status_code=422,
        code="invalid_request",
        message="Request validation failed",
        hint="check the submitted values",
        details={"errors": details},
    )


async def response_validation_error_handler(request: Request, exc: ResponseValidationError) -> JSONResponse:
    """A response model mismatch is a server bug; report generic 500 and log it."""
    request_id = request_id_for(request)
    logger.error("response validation failed for %s %s: %s", request.method, request.url.path, exc)
    return error_response(
        request,
        status_code=500,
        code="internal_error",
        message="Internal server error",
        details={"request_id": request_id},
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler: log the traceback, return a generic, redacted 500."""
    request_id = request_id_for(request)
    logger.error(
        "unhandled error in %s %s: %s",
        request.method,
        request.url.path,
        type(exc).__name__,
        exc_info=exc,
    )
    return error_response(
        request,
        status_code=500,
        code="internal_error",
        message="Internal server error",
        hint="see the daemon log for the traceback (request id is included)",
        details={"request_id": request_id},
    )


def _validation_details(errors: Sequence[Any]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for error in errors:
        loc = [str(part) for part in error.get("loc", ())]
        message = str(error.get("msg", "invalid value"))
        item: dict[str, Any] = {
            "loc": loc,
            "type": str(error.get("type", "value_error")),
            "msg": redact(message),
        }
        details.append(item)
    return details


def install_exception_handlers(app: FastAPI) -> None:
    """Register the daemon's exception handlers on *app*."""
    app.add_exception_handler(AvdFleetError, avdfleet_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(PydanticValidationError, pydantic_validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(ResponseValidationError, response_validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_error_handler)


class ErrorDetail(BaseModel):
    """OpenAPI/documentation model mirroring the contract error object."""

    model_config = ConfigDict(extra="ignore")

    code: str
    message: str
    hint: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """OpenAPI/documentation model for the contract error envelope."""

    model_config = ConfigDict(extra="ignore")

    error: ErrorDetail


class BadRequestError(AvdFleetError):
    """Generic client error for inputs the service layer rejects with a
    non-avdfleet exception (for example an unreadable image directory)."""

    code = "bad_request"
    http_status = 400


__all__ = [
    "BadRequestError",
    "ErrorDetail",
    "ErrorResponse",
    "SECURITY_HEADERS",
    "REQUEST_ID_HEADER",
    "apply_common_headers",
    "common_headers",
    "error_response",
    "install_exception_handlers",
    "request_id_for",
]
