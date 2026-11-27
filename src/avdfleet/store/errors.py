"""Typed errors raised by the SQLite store.

They derive from :class:`avdfleet.core.errors.AvdFleetError` so the API error
handler can turn them into the standard error envelope without importing the
store.  Messages only ever contain identifiers (table/column/resource names),
never row values, SQL parameters or proxy secrets.
"""

from __future__ import annotations

from ..core.errors import AvdFleetError


class StoreError(AvdFleetError):
    """Base class for storage failures."""

    code = "store_error"
    http_status = 500


class StoreNotInitializedError(StoreError):
    """Raised when a repository is used before :meth:`Store.initialize`."""

    code = "store_not_initialized"
    http_status = 503


class StoreClosedError(StoreError):
    """Raised when a repository is used after :meth:`Store.close`."""

    code = "store_closed"
    http_status = 503


class StoreSchemaError(StoreError):
    """Raised when the database schema cannot be created or is too new."""

    code = "store_schema_error"


__all__ = [
    "StoreClosedError",
    "StoreError",
    "StoreNotInitializedError",
    "StoreSchemaError",
]
