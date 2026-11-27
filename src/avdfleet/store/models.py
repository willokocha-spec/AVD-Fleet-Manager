"""Storage-layer models and value codecs for the AVD Fleet SQLite store.

The shared domain models live in :mod:`avdfleet.core.models`; the store simply
persists them.  This module adds the one genuinely storage-specific model:
:class:`ProxyRecord`.  A proxy password is never written to SQLite -- the record
carries :attr:`ProxyRecord.secret_ref`, an opaque key for the encrypted vault --
and the codecs encode timestamps/JSON consistently for every table.

All helpers are deliberately defensive because a SQLite database can be edited
by hand or left behind by an interrupted upgrade: malformed JSON columns decode
to their empty value instead of crashing a list operation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from ..core.errors import ValidationError
from ..core.models import ProxyConfig, ProxyHealth, ProxyScheme, utcnow

__all__ = [
    "ProxyRecord",
    "decode_bool",
    "decode_datetime",
    "decode_dict",
    "decode_json",
    "decode_list",
    "decode_str_list",
    "encode_bool",
    "encode_datetime",
    "encode_json",
    "utcnow",
]


# ---------------------------------------------------------------------------
# Datetime codec
# ---------------------------------------------------------------------------


def encode_datetime(value: datetime | None) -> str | None:
    """Encode a datetime as an ISO-8601 UTC string (``None`` passes through).

    Naive datetimes are treated as UTC.  Normalising to UTC keeps lexical
    ordering in SQLite identical to chronological ordering, which the run list
    and OOBE matrix rely on.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def decode_datetime(value: Any) -> datetime | None:
    """Decode a stored ISO-8601 string into a timezone-aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError("invalid stored datetime value") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


# ---------------------------------------------------------------------------
# JSON codec
# ---------------------------------------------------------------------------


def _json_default(value: Any) -> Any:
    """Give ``json.dumps`` a best-effort representation for rich values."""
    if isinstance(value, datetime):
        return encode_datetime(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    raise TypeError(f"object of type {type(value).__name__} is not JSON serialisable")


def encode_json(value: Any) -> str:
    """Serialise *value* as compact UTF-8 JSON suitable for a TEXT column."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_json_default)


def decode_json(raw: Any, default: Any = None) -> Any:
    """Decode a JSON TEXT column, returning *default* for NULL/corrupt values."""
    if raw is None or raw == "":
        return default
    if not isinstance(raw, (str, bytes, bytearray)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def decode_list(raw: Any) -> list[Any]:
    """Decode a JSON array column; anything else becomes ``[]``."""
    value = decode_json(raw, [])
    return value if isinstance(value, list) else []


def decode_dict(raw: Any) -> dict[str, Any]:
    """Decode a JSON object column; anything else becomes ``{}``."""
    value = decode_json(raw, {})
    return value if isinstance(value, dict) else {}


def decode_str_list(raw: Any) -> list[str]:
    """Decode a JSON array of strings, dropping unexpected element types."""
    return [item for item in decode_list(raw) if isinstance(item, str)]


# ---------------------------------------------------------------------------
# Boolean codec (SQLite has no BOOLEAN type)
# ---------------------------------------------------------------------------


def encode_bool(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def decode_bool(value: Any) -> bool:
    if value is None:
        return False
    return bool(int(value))


# ---------------------------------------------------------------------------
# Proxy record -- the only persistent proxy representation
# ---------------------------------------------------------------------------


class ProxyRecord(BaseModel):
    """A proxy as persisted in SQLite: metadata only, never the password.

    The password (and the username/password pair together) lives in the
    encrypted vault.  ``secret_ref`` is the vault key, so no credential is ever
    written to the database, exposed in API responses or included in logs.
    """

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    id: str
    name: str = ""
    scheme: ProxyScheme = ProxyScheme.HTTP
    host: str
    port: int = Field(ge=1, le=65535)
    username: str | None = None
    secret_ref: str | None = Field(
        default=None,
        description="Opaque vault key for the password; never the password itself.",
    )
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    source: str = ""
    health: ProxyHealth = Field(default_factory=ProxyHealth)
    created_at: datetime | None = None

    @property
    def has_credentials(self) -> bool:
        """True when a username or a stored vault secret is associated."""
        return bool(self.username or self.secret_ref)

    @property
    def safe_url(self) -> str:
        """A log/API-safe URL: userinfo (if any) is present but masked."""
        userinfo = f"{quote(self.username, safe='')}:***@" if self.username else ""
        return f"{self.scheme.value}://{userinfo}{self.host}:{self.port}"

    def to_config(self) -> ProxyConfig:
        """Convert to the core :class:`ProxyConfig` (password intentionally absent).

        Callers that need the real credential resolve :attr:`secret_ref` through
        the vault; the store never hands out plaintext passwords.
        """
        return ProxyConfig(
            id=self.id,
            name=self.name,
            scheme=self.scheme,
            host=self.host,
            port=self.port,
            username=self.username,
            password=None,
            tags=list(self.tags),
            enabled=self.enabled,
            source=self.source,
            health=self.health.model_copy(deep=True),
            created_at=self.created_at,
        )

    @classmethod
    def from_config(cls, proxy: ProxyConfig, *, secret_ref: str | None = None) -> ProxyRecord:
        """Build a record from a core proxy, requiring a vault reference for secrets.

        Raises :class:`avdfleet.core.errors.ValidationError` when the source
        object still holds a plaintext password and no ``secret_ref`` is given,
        which prevents accidental storage of credentials.
        """
        password = proxy.password.get_secret_value() if proxy.password is not None else ""
        if password and not secret_ref:
            raise ValidationError(
                "plaintext proxy passwords must be stored in the encrypted vault; "
                "pass a ProxyRecord with secret_ref instead",
                hint="write the password to the vault, then upsert ProxyRecord(secret_ref=...)",
            )
        return cls(
            id=proxy.id,
            name=proxy.name,
            scheme=proxy.scheme,
            host=proxy.host,
            port=proxy.port,
            username=proxy.username,
            secret_ref=secret_ref,
            tags=list(proxy.tags),
            enabled=proxy.enabled,
            source=proxy.source,
            health=proxy.health.model_copy(deep=True),
            created_at=proxy.created_at,
        )
