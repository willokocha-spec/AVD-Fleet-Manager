"""SQLite repositories for every AVD Fleet entity.

Each repository owns one table group.  They share the connection strategy of
:class:`avdfleet.store.db.Database` -- short-lived WAL connections for on-disk
databases, one locked connection for ``:memory:`` -- so the same ``Store`` can be
used from FastAPI threadpool workers, script executor threads and CLI commands
without sharing cursors.

Conventions
-----------
* Public methods take and return :mod:`avdfleet.core.models` objects, except
  proxies which use :class:`avdfleet.store.models.ProxyRecord`: that record keeps
  the password in the encrypted vault (``secret_ref``) instead of in SQLite.
* Missing rows raise :class:`avdfleet.core.errors.NotFoundError`; conflicting
  identifiers raise :class:`avdfleet.core.errors.ConflictError`.
* Writes use a single ``BEGIN IMMEDIATE`` transaction, including multi-table
  changes such as renaming an instance and moving its proxy assignment.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any

from ..core.errors import ConflictError, NotFoundError, ValidationError
from ..core.models import (
    AvdInfo,
    InstancePorts,
    InstanceResult,
    OobeRecord,
    ProxyAssignment,
    ProxyConfig,
    ProxyHealth,
    ProxyTestResult,
    RunState,
    ScriptDefinition,
    ScriptRun,
    SystemImage,
    Template,
    utcnow,
)
from .errors import StoreError
from .models import (
    ProxyRecord,
    decode_bool,
    decode_datetime,
    decode_dict,
    decode_json,
    decode_list,
    decode_str_list,
    encode_bool,
    encode_datetime,
    encode_json,
)

if TYPE_CHECKING:
    from .db import Database

__all__ = [
    "AssignmentRepository",
    "ImageRepository",
    "InstanceRepository",
    "OobeRepository",
    "ProxyRepository",
    "RunRepository",
    "ScriptRepository",
    "SettingsRepository",
    "TemplateRepository",
]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _require_text(value: Any, field: str) -> str:
    """Validate a required identifier/name and return it unchanged."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value


def _enum_value(value: Any) -> Any:
    """Return the primitive value of an enum member, or the value itself."""
    return value.value if isinstance(value, Enum) else value


def _fetch(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def _require_row(row: sqlite3.Row | None, message: str) -> sqlite3.Row:
    """Defensive guard for rows that must exist immediately after a write."""
    if row is None:  # pragma: no cover - indicates a bug in the repository SQL
        raise StoreError(message)
    return row


def _placeholders(columns: Sequence[str]) -> str:
    return ", ".join("?" for _ in columns)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_TEMPLATE_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "image_id",
    "device_id",
    "resolution",
    "density",
    "ram_mb",
    "heap_mb",
    "cpu_cores",
    "gpu_mode",
    "headless",
    "proxy_id",
    "scripts",
    "tags",
    "description",
    "config",
    "created_at",
    "updated_at",
)
_TEMPLATE_SELECT = f"SELECT {', '.join(_TEMPLATE_COLUMNS)} FROM templates"
_TEMPLATE_UPSERT = f"""
INSERT INTO templates ({", ".join(_TEMPLATE_COLUMNS)})
VALUES ({_placeholders(_TEMPLATE_COLUMNS)})
ON CONFLICT(id) DO UPDATE SET
    name = excluded.name,
    image_id = excluded.image_id,
    device_id = excluded.device_id,
    resolution = excluded.resolution,
    density = excluded.density,
    ram_mb = excluded.ram_mb,
    heap_mb = excluded.heap_mb,
    cpu_cores = excluded.cpu_cores,
    gpu_mode = excluded.gpu_mode,
    headless = excluded.headless,
    proxy_id = excluded.proxy_id,
    scripts = excluded.scripts,
    tags = excluded.tags,
    description = excluded.description,
    config = excluded.config,
    created_at = COALESCE(templates.created_at, excluded.created_at),
    updated_at = excluded.updated_at
"""


def _template_params(template: Template, created_at: str | None, updated_at: str | None) -> tuple[Any, ...]:
    return (
        template.id,
        template.name,
        template.image_id,
        template.device_id or "",
        template.resolution or "",
        template.density,
        template.ram_mb,
        template.heap_mb,
        template.cpu_cores,
        _enum_value(template.gpu_mode),
        encode_bool(template.headless),
        template.proxy_id,
        encode_json(list(template.scripts)),
        encode_json(list(template.tags)),
        template.description or "",
        encode_json(dict(template.config)),
        created_at,
        updated_at,
    )


def _template_from_row(row: sqlite3.Row) -> Template:
    return Template(
        id=row["id"],
        name=row["name"],
        image_id=row["image_id"],
        device_id=row["device_id"],
        resolution=row["resolution"],
        density=row["density"],
        ram_mb=row["ram_mb"],
        heap_mb=row["heap_mb"],
        cpu_cores=row["cpu_cores"],
        gpu_mode=row["gpu_mode"],
        headless=decode_bool(row["headless"]),
        proxy_id=row["proxy_id"],
        scripts=decode_str_list(row["scripts"]),
        tags=decode_str_list(row["tags"]),
        description=row["description"],
        config=decode_dict(row["config"]),
        created_at=decode_datetime(row["created_at"]),
        updated_at=decode_datetime(row["updated_at"]),
    )


class TemplateRepository:
    """CRUD for AVD templates (``templates`` table)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def list(self) -> list[Template]:
        """Return every template ordered by name."""
        with self._db.connect() as conn:
            rows = conn.execute(f"{_TEMPLATE_SELECT} ORDER BY name COLLATE NOCASE, id").fetchall()
            return [_template_from_row(row) for row in rows]

    def get(self, template_id: str) -> Template:
        """Return one template or raise :class:`NotFoundError`."""
        _require_text(template_id, "template id")
        with self._db.connect() as conn:
            row = _fetch(conn, f"{_TEMPLATE_SELECT} WHERE id = ?", (template_id,))
            if row is None:
                raise NotFoundError(f"template {template_id!r} not found")
            return _template_from_row(row)

    def get_by_name(self, name: str) -> Template | None:
        """Return the most recently updated template with *name*, if any."""
        if not isinstance(name, str) or not name.strip():
            return None
        with self._db.connect() as conn:
            row = _fetch(
                conn,
                f"{_TEMPLATE_SELECT} WHERE name = ? ORDER BY updated_at DESC, id LIMIT 1",
                (name,),
            )
            return _template_from_row(row) if row is not None else None

    def upsert(self, template: Template) -> Template:
        """Insert or fully update a template; timestamps are store-managed."""
        _require_text(template.id, "template id")
        _require_text(template.name, "template name")
        _require_text(template.image_id, "template image_id")
        now = utcnow()
        created_at = encode_datetime(template.created_at) or encode_datetime(now)
        updated_at = encode_datetime(now)
        with self._db.transaction() as conn:
            conn.execute(_TEMPLATE_UPSERT, _template_params(template, created_at, updated_at))
            row = _fetch(conn, f"{_TEMPLATE_SELECT} WHERE id = ?", (template.id,))
            return _template_from_row(_require_row(row, "template upsert did not persist"))

    def delete(self, template_id: str) -> None:
        """Delete a template; raises :class:`NotFoundError` if absent."""
        _require_text(template_id, "template id")
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))
            if cursor.rowcount == 0:
                raise NotFoundError(f"template {template_id!r} not found")


# ---------------------------------------------------------------------------
# Instances
# ---------------------------------------------------------------------------

_INSTANCE_COLUMNS: tuple[str, ...] = (
    "name",
    "template_id",
    "image_id",
    "image_name",
    "device_id",
    "state",
    "pid",
    "ports",
    "proxy_id",
    "adb_serial",
    "boot_completed",
    "created_at",
    "started_at",
    "uptime_seconds",
    "error",
    "config",
    "grpc_endpoint",
    "screen_available",
)
_INSTANCE_SELECT = f"SELECT {', '.join(_INSTANCE_COLUMNS)} FROM instances"
_INSTANCE_UPSERT = f"""
INSERT INTO instances ({", ".join(_INSTANCE_COLUMNS)})
VALUES ({_placeholders(_INSTANCE_COLUMNS)})
ON CONFLICT(name) DO UPDATE SET
    template_id = excluded.template_id,
    image_id = excluded.image_id,
    image_name = excluded.image_name,
    device_id = excluded.device_id,
    state = excluded.state,
    pid = excluded.pid,
    ports = excluded.ports,
    proxy_id = excluded.proxy_id,
    adb_serial = excluded.adb_serial,
    boot_completed = excluded.boot_completed,
    created_at = COALESCE(instances.created_at, excluded.created_at),
    started_at = excluded.started_at,
    uptime_seconds = excluded.uptime_seconds,
    error = excluded.error,
    config = excluded.config,
    grpc_endpoint = excluded.grpc_endpoint,
    screen_available = excluded.screen_available
"""


def _instance_params(instance: AvdInfo, created_at: str | None) -> tuple[Any, ...]:
    ports = None
    if instance.ports is not None:
        ports = encode_json(instance.ports.model_dump(mode="json"))
    return (
        instance.name,
        instance.template_id,
        instance.image_id,
        instance.image_name or "",
        instance.device_id or "",
        _enum_value(instance.state),
        instance.pid,
        ports,
        instance.proxy_id,
        instance.adb_serial,
        encode_bool(instance.boot_completed),
        created_at,
        encode_datetime(instance.started_at),
        instance.uptime_seconds,
        instance.error or "",
        encode_json(dict(instance.config)),
        instance.grpc_endpoint,
        encode_bool(instance.screen_available),
    )


def _instance_from_row(row: sqlite3.Row) -> AvdInfo:
    ports = None
    if row["ports"] is not None:
        ports = InstancePorts.model_validate(decode_dict(row["ports"]))
    return AvdInfo(
        name=row["name"],
        template_id=row["template_id"],
        image_id=row["image_id"],
        image_name=row["image_name"],
        device_id=row["device_id"],
        state=row["state"],
        pid=row["pid"],
        ports=ports,
        proxy_id=row["proxy_id"],
        adb_serial=row["adb_serial"],
        boot_completed=decode_bool(row["boot_completed"]),
        created_at=decode_datetime(row["created_at"]),
        started_at=decode_datetime(row["started_at"]),
        uptime_seconds=row["uptime_seconds"],
        error=row["error"],
        config=decode_dict(row["config"]),
        grpc_endpoint=row["grpc_endpoint"],
        screen_available=decode_bool(row["screen_available"]),
    )


class InstanceRepository:
    """CRUD for deployed AVD instances (``instances`` table)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def list(self) -> list[AvdInfo]:
        """Return every instance ordered by name."""
        with self._db.connect() as conn:
            rows = conn.execute(f"{_INSTANCE_SELECT} ORDER BY name COLLATE NOCASE, name").fetchall()
            return [_instance_from_row(row) for row in rows]

    def get(self, name: str) -> AvdInfo:
        """Return one instance or raise :class:`NotFoundError`."""
        _require_text(name, "instance name")
        with self._db.connect() as conn:
            row = _fetch(conn, f"{_INSTANCE_SELECT} WHERE name = ?", (name,))
            if row is None:
                raise NotFoundError(f"instance {name!r} not found")
            return _instance_from_row(row)

    def upsert(self, instance: AvdInfo) -> AvdInfo:
        """Insert or fully update an instance snapshot (state, ports, error, ...)."""
        _require_text(instance.name, "instance name")
        now = utcnow()
        created_at = encode_datetime(instance.created_at) or encode_datetime(now)
        with self._db.transaction() as conn:
            conn.execute(_INSTANCE_UPSERT, _instance_params(instance, created_at))
            row = _fetch(conn, f"{_INSTANCE_SELECT} WHERE name = ?", (instance.name,))
            return _instance_from_row(_require_row(row, "instance upsert did not persist"))

    def delete(self, name: str) -> None:
        """Delete an instance and any proxy assignment for it."""
        _require_text(name, "instance name")
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM instances WHERE name = ?", (name,))
            if cursor.rowcount == 0:
                raise NotFoundError(f"instance {name!r} not found")
            # Assignments are keyed by AVD name rather than an FK because an
            # instance row may be absent (pre-created AVDs, imported fleets).
            conn.execute("DELETE FROM assignments WHERE avd_name = ?", (name,))

    def rename(self, old_name: str, new_name: str) -> AvdInfo:
        """Rename an instance, moving its proxy assignment atomically."""
        _require_text(old_name, "instance name")
        _require_text(new_name, "instance name")
        if old_name == new_name:
            return self.get(new_name)
        with self._db.transaction() as conn:
            if _fetch(conn, "SELECT 1 FROM instances WHERE name = ?", (old_name,)) is None:
                raise NotFoundError(f"instance {old_name!r} not found")
            if _fetch(conn, "SELECT 1 FROM instances WHERE name = ?", (new_name,)) is not None:
                raise ConflictError(f"instance {new_name!r} already exists")
            if _fetch(conn, "SELECT 1 FROM assignments WHERE avd_name = ?", (new_name,)) is not None:
                raise ConflictError(f"proxy assignment for {new_name!r} already exists")
            conn.execute("UPDATE instances SET name = ? WHERE name = ?", (new_name, old_name))
            conn.execute("UPDATE assignments SET avd_name = ? WHERE avd_name = ?", (new_name, old_name))
            row = _fetch(conn, f"{_INSTANCE_SELECT} WHERE name = ?", (new_name,))
            return _instance_from_row(_require_row(row, "instance rename did not persist"))


# ---------------------------------------------------------------------------
# Proxies
# ---------------------------------------------------------------------------

_PROXY_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "scheme",
    "host",
    "port",
    "username",
    "secret_ref",
    "tags",
    "enabled",
    "source",
    "health",
    "created_at",
)
_PROXY_SELECT = f"SELECT {', '.join(_PROXY_COLUMNS)} FROM proxies"
# `secret_ref` is a vault key, never a password; see store.models.ProxyRecord.
_PROXY_UPSERT = f"""
INSERT INTO proxies ({", ".join(_PROXY_COLUMNS)})
VALUES ({_placeholders(_PROXY_COLUMNS)})
ON CONFLICT(id) DO UPDATE SET
    name = excluded.name,
    scheme = excluded.scheme,
    host = excluded.host,
    port = excluded.port,
    username = excluded.username,
    secret_ref = excluded.secret_ref,
    tags = excluded.tags,
    enabled = excluded.enabled,
    source = excluded.source,
    health = excluded.health,
    created_at = COALESCE(proxies.created_at, excluded.created_at)
"""


def _proxy_params(record: ProxyRecord, created_at: str | None) -> tuple[Any, ...]:
    return (
        record.id,
        record.name or "",
        _enum_value(record.scheme),
        record.host,
        record.port,
        record.username,
        record.secret_ref,
        encode_json(list(record.tags)),
        encode_bool(record.enabled),
        record.source or "",
        encode_json(record.health.model_dump(mode="json")),
        created_at,
    )


def _proxy_from_row(row: sqlite3.Row) -> ProxyRecord:
    return ProxyRecord(
        id=row["id"],
        name=row["name"],
        scheme=row["scheme"],
        host=row["host"],
        port=row["port"],
        username=row["username"],
        secret_ref=row["secret_ref"],
        tags=decode_str_list(row["tags"]),
        enabled=decode_bool(row["enabled"]),
        source=row["source"],
        health=ProxyHealth.model_validate(decode_dict(row["health"])),
        created_at=decode_datetime(row["created_at"]),
    )


def _coerce_proxy(proxy: ProxyRecord | ProxyConfig) -> ProxyRecord:
    """Accept either representation, but never a plaintext password."""
    if isinstance(proxy, ProxyRecord):
        return proxy
    if isinstance(proxy, ProxyConfig):
        password = proxy.password.get_secret_value() if proxy.password is not None else ""
        if password:
            raise ValidationError(
                "plaintext proxy passwords must not be written to the database",
                hint="store the password in the encrypted vault and upsert ProxyRecord(secret_ref=...)",
            )
        return ProxyRecord.from_config(proxy)
    raise ValidationError("proxy must be a ProxyRecord or ProxyConfig")


class ProxyRepository:
    """CRUD for proxies (``proxies`` table); passwords stay in the vault."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def list(self, include_disabled: bool = False) -> list[ProxyRecord]:
        """Return proxies ordered by name, optionally including disabled ones."""
        sql = _PROXY_SELECT
        if not include_disabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY name COLLATE NOCASE, id"
        with self._db.connect() as conn:
            rows = conn.execute(sql).fetchall()
            return [_proxy_from_row(row) for row in rows]

    def get(self, proxy_id: str) -> ProxyRecord:
        """Return one proxy or raise :class:`NotFoundError`."""
        _require_text(proxy_id, "proxy id")
        with self._db.connect() as conn:
            row = _fetch(conn, f"{_PROXY_SELECT} WHERE id = ?", (proxy_id,))
            if row is None:
                raise NotFoundError(f"proxy {proxy_id!r} not found")
            return _proxy_from_row(row)

    def get_by_name(self, name: str) -> ProxyRecord | None:
        """Return the most recently created proxy with *name*, if any."""
        if not isinstance(name, str) or not name.strip():
            return None
        with self._db.connect() as conn:
            row = _fetch(
                conn,
                f"{_PROXY_SELECT} WHERE name = ? ORDER BY created_at DESC, id LIMIT 1",
                (name,),
            )
            return _proxy_from_row(row) if row is not None else None

    def upsert(self, proxy: ProxyRecord | ProxyConfig) -> ProxyRecord:
        """Insert or update a proxy record (never including a password)."""
        record = _coerce_proxy(proxy)
        _require_text(record.id, "proxy id")
        _require_text(record.host, "proxy host")
        now = utcnow()
        created_at = encode_datetime(record.created_at) or encode_datetime(now)
        with self._db.transaction() as conn:
            conn.execute(_PROXY_UPSERT, _proxy_params(record, created_at))
            row = _fetch(conn, f"{_PROXY_SELECT} WHERE id = ?", (record.id,))
            return _proxy_from_row(_require_row(row, "proxy upsert did not persist"))

    def update_health(self, proxy_id: str, health: ProxyHealth | ProxyTestResult) -> ProxyRecord:
        """Persist the latest health check for a proxy."""
        _require_text(proxy_id, "proxy id")
        payload = health.as_health() if isinstance(health, ProxyTestResult) else health
        if not isinstance(payload, ProxyHealth):
            raise ValidationError("health must be a ProxyHealth or ProxyTestResult")
        encoded = encode_json(payload.model_dump(mode="json"))
        with self._db.transaction() as conn:
            cursor = conn.execute("UPDATE proxies SET health = ? WHERE id = ?", (encoded, proxy_id))
            if cursor.rowcount == 0:
                raise NotFoundError(f"proxy {proxy_id!r} not found")
            row = _fetch(conn, f"{_PROXY_SELECT} WHERE id = ?", (proxy_id,))
            return _proxy_from_row(_require_row(row, "proxy health update did not persist"))

    def delete(self, proxy_id: str) -> None:
        """Delete a proxy; its assignments are removed by the FK cascade."""
        _require_text(proxy_id, "proxy id")
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM proxies WHERE id = ?", (proxy_id,))
            if cursor.rowcount == 0:
                raise NotFoundError(f"proxy {proxy_id!r} not found")


# ---------------------------------------------------------------------------
# Proxy assignments
# ---------------------------------------------------------------------------

_ASSIGNMENT_COLUMNS: tuple[str, ...] = ("avd_name", "proxy_id", "assigned_at")
_ASSIGNMENT_SELECT = f"SELECT {', '.join(_ASSIGNMENT_COLUMNS)} FROM assignments"


def _assignment_from_row(row: sqlite3.Row) -> ProxyAssignment:
    return ProxyAssignment(
        avd_name=row["avd_name"],
        proxy_id=row["proxy_id"],
        assigned_at=decode_datetime(row["assigned_at"]),
    )


class AssignmentRepository:
    """One authenticated proxy per AVD (``assignments`` table)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def list(self) -> list[ProxyAssignment]:
        """Return every assignment ordered by AVD name."""
        with self._db.connect() as conn:
            rows = conn.execute(f"{_ASSIGNMENT_SELECT} ORDER BY avd_name COLLATE NOCASE").fetchall()
            return [_assignment_from_row(row) for row in rows]

    def get(self, avd_name: str) -> ProxyAssignment | None:
        """Return the assignment for *avd_name*, or ``None``."""
        if not isinstance(avd_name, str) or not avd_name.strip():
            return None
        with self._db.connect() as conn:
            row = _fetch(conn, f"{_ASSIGNMENT_SELECT} WHERE avd_name = ?", (avd_name,))
            return _assignment_from_row(row) if row is not None else None

    def assign(self, avd_name: str, proxy_id: str, *, assigned_at: datetime | None = None) -> ProxyAssignment:
        """Assign (or reassign) an AVD to an existing proxy."""
        _require_text(avd_name, "instance name")
        _require_text(proxy_id, "proxy id")
        timestamp = encode_datetime(assigned_at or utcnow())
        with self._db.transaction() as conn:
            if _fetch(conn, "SELECT 1 FROM proxies WHERE id = ?", (proxy_id,)) is None:
                raise NotFoundError(f"proxy {proxy_id!r} not found")
            conn.execute(
                """
                INSERT INTO assignments (avd_name, proxy_id, assigned_at)
                VALUES (?, ?, ?)
                ON CONFLICT(avd_name) DO UPDATE SET
                    proxy_id = excluded.proxy_id,
                    assigned_at = excluded.assigned_at
                """,
                (avd_name, proxy_id, timestamp),
            )
            row = _fetch(conn, f"{_ASSIGNMENT_SELECT} WHERE avd_name = ?", (avd_name,))
            return _assignment_from_row(_require_row(row, "proxy assignment did not persist"))

    def unassign(self, avd_name: str) -> bool:
        """Remove an assignment; returns ``True`` when one existed."""
        if not isinstance(avd_name, str) or not avd_name.strip():
            return False
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM assignments WHERE avd_name = ?", (avd_name,))
            return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------

_SCRIPT_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "type",
    "content",
    "path",
    "description",
    "timeout_s",
    "env",
    "targets",
    "builtin",
    "tags",
    "created_at",
    "updated_at",
)
_SCRIPT_SELECT = f"SELECT {', '.join(_SCRIPT_COLUMNS)} FROM scripts"
_SCRIPT_UPSERT = f"""
INSERT INTO scripts ({", ".join(_SCRIPT_COLUMNS)})
VALUES ({_placeholders(_SCRIPT_COLUMNS)})
ON CONFLICT(id) DO UPDATE SET
    name = excluded.name,
    type = excluded.type,
    content = excluded.content,
    path = excluded.path,
    description = excluded.description,
    timeout_s = excluded.timeout_s,
    env = excluded.env,
    targets = excluded.targets,
    builtin = excluded.builtin,
    tags = excluded.tags,
    created_at = COALESCE(scripts.created_at, excluded.created_at),
    updated_at = excluded.updated_at
"""


def _script_params(
    script: ScriptDefinition, created_at: str | None, updated_at: str | None
) -> tuple[Any, ...]:
    return (
        script.id,
        script.name,
        _enum_value(script.type),
        script.content or "",
        script.path,
        script.description or "",
        script.timeout_s,
        encode_json(dict(script.env)),
        encode_json(list(script.targets)),
        encode_bool(script.builtin),
        encode_json(list(script.tags)),
        created_at,
        updated_at,
    )


def _script_from_row(row: sqlite3.Row) -> ScriptDefinition:
    return ScriptDefinition(
        id=row["id"],
        name=row["name"],
        type=row["type"],
        content=row["content"],
        path=row["path"],
        description=row["description"],
        timeout_s=row["timeout_s"],
        env=decode_dict(row["env"]),
        targets=decode_str_list(row["targets"]),
        builtin=decode_bool(row["builtin"]),
        tags=decode_str_list(row["tags"]),
        created_at=decode_datetime(row["created_at"]),
        updated_at=decode_datetime(row["updated_at"]),
    )


class ScriptRepository:
    """CRUD for script definitions (``scripts`` table)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def list(self) -> list[ScriptDefinition]:
        """Return every script ordered by name."""
        with self._db.connect() as conn:
            rows = conn.execute(f"{_SCRIPT_SELECT} ORDER BY name COLLATE NOCASE, id").fetchall()
            return [_script_from_row(row) for row in rows]

    def get(self, script_id: str) -> ScriptDefinition:
        """Return one script or raise :class:`NotFoundError`."""
        _require_text(script_id, "script id")
        with self._db.connect() as conn:
            row = _fetch(conn, f"{_SCRIPT_SELECT} WHERE id = ?", (script_id,))
            if row is None:
                raise NotFoundError(f"script {script_id!r} not found")
            return _script_from_row(row)

    def get_by_name(self, name: str) -> ScriptDefinition | None:
        """Return the most recently updated script with *name*, if any."""
        if not isinstance(name, str) or not name.strip():
            return None
        with self._db.connect() as conn:
            row = _fetch(
                conn,
                f"{_SCRIPT_SELECT} WHERE name = ? ORDER BY updated_at DESC, id LIMIT 1",
                (name,),
            )
            return _script_from_row(row) if row is not None else None

    def upsert(self, script: ScriptDefinition) -> ScriptDefinition:
        """Insert or fully update a script definition."""
        _require_text(script.id, "script id")
        _require_text(script.name, "script name")
        now = utcnow()
        created_at = encode_datetime(script.created_at) or encode_datetime(now)
        updated_at = encode_datetime(now)
        with self._db.transaction() as conn:
            conn.execute(_SCRIPT_UPSERT, _script_params(script, created_at, updated_at))
            row = _fetch(conn, f"{_SCRIPT_SELECT} WHERE id = ?", (script.id,))
            return _script_from_row(_require_row(row, "script upsert did not persist"))

    def delete(self, script_id: str) -> None:
        """Delete a script; run history keeps its own script name snapshot."""
        _require_text(script_id, "script id")
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM scripts WHERE id = ?", (script_id,))
            if cursor.rowcount == 0:
                raise NotFoundError(f"script {script_id!r} not found")


# ---------------------------------------------------------------------------
# Script runs
# ---------------------------------------------------------------------------

_RUN_COLUMNS: tuple[str, ...] = (
    "id",
    "script_id",
    "script_name",
    "type",
    "state",
    "instances",
    "results",
    "started_at",
    "finished_at",
    "error",
    "dry_run",
    "requested_by",
    "request",
    "created_at",
    "updated_at",
)
_RUN_SELECT = f"SELECT {', '.join(_RUN_COLUMNS)} FROM runs"
_RUN_UPSERT = f"""
INSERT INTO runs ({", ".join(_RUN_COLUMNS)})
VALUES ({_placeholders(_RUN_COLUMNS)})
ON CONFLICT(id) DO UPDATE SET
    script_id = excluded.script_id,
    script_name = excluded.script_name,
    type = excluded.type,
    state = excluded.state,
    instances = excluded.instances,
    results = excluded.results,
    started_at = excluded.started_at,
    finished_at = excluded.finished_at,
    error = excluded.error,
    dry_run = excluded.dry_run,
    requested_by = excluded.requested_by,
    request = excluded.request,
    created_at = runs.created_at,
    updated_at = excluded.updated_at
"""


def _dump_results(results: Sequence[InstanceResult]) -> str:
    return encode_json(
        [
            result.model_dump(mode="json") if isinstance(result, InstanceResult) else result
            for result in results
        ]
    )


def _run_params(run: ScriptRun, created_at: str, updated_at: str) -> tuple[Any, ...]:
    return (
        run.id,
        run.script_id,
        run.script_name or "",
        _enum_value(run.type),
        _enum_value(run.state),
        encode_json(list(run.instances)),
        _dump_results(run.results),
        encode_datetime(run.started_at),
        encode_datetime(run.finished_at),
        run.error or "",
        encode_bool(run.dry_run),
        run.requested_by or "",
        encode_json(dict(run.request)),
        created_at,
        updated_at,
    )


def _result_dicts(raw: Any) -> list[InstanceResult]:
    """Decode the results JSON array into validated per-instance results."""
    return [InstanceResult.model_validate(item) for item in decode_list(raw) if isinstance(item, dict)]


def _run_from_row(row: sqlite3.Row) -> ScriptRun:
    return ScriptRun(
        id=row["id"],
        script_id=row["script_id"],
        script_name=row["script_name"],
        type=row["type"],
        state=row["state"],
        instances=decode_str_list(row["instances"]),
        results=_result_dicts(row["results"]),
        started_at=decode_datetime(row["started_at"]),
        finished_at=decode_datetime(row["finished_at"]),
        error=row["error"],
        dry_run=decode_bool(row["dry_run"]),
        requested_by=row["requested_by"],
        request=decode_dict(row["request"]),
    )


class RunRepository:
    """Persisted script runs (``runs`` table) with per-instance results."""

    def __init__(self, db: Database) -> None:
        self._db = db
        # Windows clocks can have ~15 ms resolution.  A run that is updated twice
        # inside one tick would otherwise get an identical updated_at and lose its
        # "newest first" position.  Keep a process-local strictly increasing stamp.
        import threading

        self._stamp_lock = threading.RLock()
        self._last_stamp: datetime | None = None

    def _next_stamp(self) -> datetime:
        now = utcnow()
        with self._stamp_lock:
            if self._last_stamp is not None and now <= self._last_stamp:
                now = self._last_stamp + timedelta(microseconds=1)
            self._last_stamp = now
            return now

    def list(self, limit: int = 50) -> list[ScriptRun]:
        """Return the most recently updated runs, newest first."""
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValidationError("limit must be a non-negative integer")
        capped = min(limit, 1000)
        with self._db.connect() as conn:
            rows = conn.execute(
                f"{_RUN_SELECT} ORDER BY updated_at DESC, id DESC LIMIT ?",
                (capped,),
            ).fetchall()
            return [_run_from_row(row) for row in rows]

    def get(self, run_id: str) -> ScriptRun:
        """Return one run or raise :class:`NotFoundError`."""
        _require_text(run_id, "run id")
        with self._db.connect() as conn:
            row = _fetch(conn, f"{_RUN_SELECT} WHERE id = ?", (run_id,))
            if row is None:
                raise NotFoundError(f"run {run_id!r} not found")
            return _run_from_row(row)

    def upsert(self, run: ScriptRun) -> ScriptRun:
        """Insert or fully update a run (used for progress and result snapshots)."""
        _require_text(run.id, "run id")
        _require_text(run.script_id, "run script_id")
        now = encode_datetime(self._next_stamp()) or ""
        with self._db.transaction() as conn:
            conn.execute(_RUN_UPSERT, _run_params(run, created_at=now, updated_at=now))
            row = _fetch(conn, f"{_RUN_SELECT} WHERE id = ?", (run.id,))
            return _run_from_row(_require_row(row, "run upsert did not persist"))

    def finish(
        self,
        run: str | ScriptRun,
        *,
        state: RunState | None = None,
        results: Sequence[InstanceResult] | None = None,
        error: str | None = None,
        finished_at: datetime | None = None,
    ) -> ScriptRun:
        """Mark a run finished and persist its final state/results.

        ``run`` may be an id or the live :class:`ScriptRun`; explicit keyword
        arguments win over the values taken from that object.
        """
        run_id = run.id if isinstance(run, ScriptRun) else _require_text(run, "run id")
        now = self._next_stamp()
        with self._db.transaction() as conn:
            row = _fetch(conn, f"{_RUN_SELECT} WHERE id = ?", (run_id,))
            if row is None:
                raise NotFoundError(f"run {run_id!r} not found")
            existing = _run_from_row(row)
            final_state = state
            if final_state is None and isinstance(run, ScriptRun):
                final_state = run.state
            if final_state is None:
                final_state = existing.state
            final_results = results
            if final_results is None and isinstance(run, ScriptRun):
                final_results = run.results
            if final_results is None:
                final_results = existing.results
            final_error = error
            if final_error is None and isinstance(run, ScriptRun):
                final_error = run.error
            if final_error is None:
                final_error = existing.error
            final_finished = finished_at
            if final_finished is None and isinstance(run, ScriptRun):
                final_finished = run.finished_at
            if final_finished is None:
                final_finished = existing.finished_at or now
            conn.execute(
                """
                UPDATE runs
                SET state = ?, results = ?, error = ?, finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    _enum_value(final_state),
                    _dump_results(final_results),
                    final_error or "",
                    encode_datetime(final_finished),
                    encode_datetime(now),
                    run_id,
                ),
            )
            updated = _fetch(conn, f"{_RUN_SELECT} WHERE id = ?", (run_id,))
            return _run_from_row(_require_row(updated, "run finish did not persist"))


# ---------------------------------------------------------------------------
# System images
# ---------------------------------------------------------------------------

_IMAGE_COLUMNS: tuple[str, ...] = (
    "id",
    "api_level",
    "tag",
    "abi",
    "kind",
    "display_name",
    "package",
    "url",
    "sha256",
    "size_bytes",
    "installed",
    "local_path",
    "oobe_status",
    "custom",
    "notes",
    "metadata",
)
_IMAGE_SELECT = f"SELECT {', '.join(_IMAGE_COLUMNS)} FROM images"
_IMAGE_UPSERT = f"""
INSERT INTO images ({", ".join(_IMAGE_COLUMNS)})
VALUES ({_placeholders(_IMAGE_COLUMNS)})
ON CONFLICT(id) DO UPDATE SET
    api_level = excluded.api_level,
    tag = excluded.tag,
    abi = excluded.abi,
    kind = excluded.kind,
    display_name = excluded.display_name,
    package = excluded.package,
    url = excluded.url,
    sha256 = excluded.sha256,
    size_bytes = excluded.size_bytes,
    installed = excluded.installed,
    local_path = excluded.local_path,
    oobe_status = excluded.oobe_status,
    custom = excluded.custom,
    notes = excluded.notes,
    metadata = excluded.metadata
"""


def _image_params(image: SystemImage) -> tuple[Any, ...]:
    return (
        image.id,
        image.api_level,
        image.tag or "",
        image.abi or "",
        _enum_value(image.kind),
        image.display_name or "",
        image.package or "",
        image.url,
        image.sha256,
        image.size_bytes,
        encode_bool(image.installed),
        image.local_path,
        _enum_value(image.oobe_status),
        encode_bool(image.custom),
        image.notes or "",
        encode_json(dict(image.metadata)),
    )


def _image_from_row(row: sqlite3.Row) -> SystemImage:
    return SystemImage(
        id=row["id"],
        api_level=row["api_level"],
        tag=row["tag"],
        abi=row["abi"],
        kind=row["kind"],
        display_name=row["display_name"],
        package=row["package"],
        url=row["url"],
        sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        installed=decode_bool(row["installed"]),
        local_path=row["local_path"],
        oobe_status=row["oobe_status"],
        custom=decode_bool(row["custom"]),
        notes=row["notes"],
        metadata=decode_dict(row["metadata"]),
    )


class ImageRepository:
    """Catalogue of stock/imported system images (``images`` table)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def list(self) -> list[SystemImage]:
        """Return every known image, newest API level first."""
        with self._db.connect() as conn:
            rows = conn.execute(
                f"{_IMAGE_SELECT} ORDER BY api_level DESC, tag COLLATE NOCASE, abi, id"
            ).fetchall()
            return [_image_from_row(row) for row in rows]

    def get(self, image_id: str) -> SystemImage:
        """Return one image or raise :class:`NotFoundError`."""
        _require_text(image_id, "image id")
        with self._db.connect() as conn:
            row = _fetch(conn, f"{_IMAGE_SELECT} WHERE id = ?", (image_id,))
            if row is None:
                raise NotFoundError(f"image {image_id!r} not found")
            return _image_from_row(row)

    def upsert(self, image: SystemImage) -> SystemImage:
        """Insert or update an image catalogue entry."""
        _require_text(image.id, "image id")
        with self._db.transaction() as conn:
            conn.execute(_IMAGE_UPSERT, _image_params(image))
            row = _fetch(conn, f"{_IMAGE_SELECT} WHERE id = ?", (image.id,))
            return _image_from_row(_require_row(row, "image upsert did not persist"))

    def delete(self, image_id: str) -> None:
        """Delete an image catalogue entry (OOBE evidence is kept separately)."""
        _require_text(image_id, "image id")
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
            if cursor.rowcount == 0:
                raise NotFoundError(f"image {image_id!r} not found")


# ---------------------------------------------------------------------------
# OOBE records
# ---------------------------------------------------------------------------

_OOBE_COLUMNS: tuple[str, ...] = (
    "image_id",
    "api_level",
    "tag",
    "abi",
    "status",
    "activity",
    "first_run_component",
    "screenshot_path",
    "boot_seconds",
    "reset_seconds",
    "checked_at",
    "checked_by",
    "emulator_version",
    "notes",
    "evidence",
)
_OOBE_SELECT = f"SELECT {', '.join(_OOBE_COLUMNS)} FROM oobe_records"
_OOBE_UPSERT = f"""
INSERT INTO oobe_records ({", ".join(_OOBE_COLUMNS)})
VALUES ({_placeholders(_OOBE_COLUMNS)})
ON CONFLICT(image_id) DO UPDATE SET
    api_level = excluded.api_level,
    tag = excluded.tag,
    abi = excluded.abi,
    status = excluded.status,
    activity = excluded.activity,
    first_run_component = excluded.first_run_component,
    screenshot_path = excluded.screenshot_path,
    boot_seconds = excluded.boot_seconds,
    reset_seconds = excluded.reset_seconds,
    checked_at = excluded.checked_at,
    checked_by = excluded.checked_by,
    emulator_version = excluded.emulator_version,
    notes = excluded.notes,
    evidence = excluded.evidence
"""


def _oobe_params(record: OobeRecord) -> tuple[Any, ...]:
    return (
        record.image_id,
        record.api_level,
        record.tag or "",
        record.abi or "",
        _enum_value(record.status),
        record.activity or "",
        record.first_run_component or "",
        record.screenshot_path or "",
        record.boot_seconds,
        record.reset_seconds,
        encode_datetime(record.checked_at),
        record.checked_by or "",
        record.emulator_version or "",
        record.notes or "",
        encode_json(list(record.evidence)),
    )


def _oobe_from_row(row: sqlite3.Row) -> OobeRecord:
    return OobeRecord(
        image_id=row["image_id"],
        api_level=row["api_level"],
        tag=row["tag"],
        abi=row["abi"],
        status=row["status"],
        activity=row["activity"],
        first_run_component=row["first_run_component"],
        screenshot_path=row["screenshot_path"],
        boot_seconds=row["boot_seconds"],
        reset_seconds=row["reset_seconds"],
        checked_at=decode_datetime(row["checked_at"]),
        checked_by=row["checked_by"],
        emulator_version=row["emulator_version"],
        notes=row["notes"],
        evidence=decode_str_list(row["evidence"]),
    )


class OobeRepository:
    """Out-of-box-experience evidence keyed by image id (``oobe_records`` table)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def list(self) -> list[OobeRecord]:
        """Return every recorded OOBE result ordered by image id."""
        with self._db.connect() as conn:
            rows = conn.execute(f"{_OOBE_SELECT} ORDER BY image_id").fetchall()
            return [_oobe_from_row(row) for row in rows]

    def get(self, image_id: str) -> OobeRecord | None:
        """Return the record for *image_id*, or ``None`` when unverified/unknown."""
        if not isinstance(image_id, str) or not image_id.strip():
            return None
        with self._db.connect() as conn:
            row = _fetch(conn, f"{_OOBE_SELECT} WHERE image_id = ?", (image_id,))
            return _oobe_from_row(row) if row is not None else None

    def upsert(self, record: OobeRecord) -> OobeRecord:
        """Insert or replace the OOBE evidence for an image."""
        _require_text(record.image_id, "image id")
        with self._db.transaction() as conn:
            conn.execute(_OOBE_UPSERT, _oobe_params(record))
            row = _fetch(conn, f"{_OOBE_SELECT} WHERE image_id = ?", (record.image_id,))
            return _oobe_from_row(_require_row(row, "oobe upsert did not persist"))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_SETTING_SELECT = "SELECT key, value FROM settings"


class SettingsRepository:
    """JSON key/value settings (``settings`` table)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, key: str, default: Any = None) -> Any:
        """Return a decoded setting value, or *default* when the key is absent."""
        _require_text(key, "setting key")
        with self._db.connect() as conn:
            row = _fetch(conn, "SELECT value FROM settings WHERE key = ?", (key,))
            if row is None:
                return default
            return decode_json(row["value"], default)

    def set(self, key: str, value: Any) -> None:
        """Create/update a setting; *value* must be JSON-serialisable."""
        _require_text(key, "setting key")
        try:
            encoded = encode_json(value)
        except TypeError:
            raise ValidationError(f"setting {key!r} is not JSON-serialisable") from None
        now = encode_datetime(utcnow()) or ""
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, encoded, now),
            )

    def all(self) -> dict[str, Any]:
        """Return every setting as a decoded mapping."""
        with self._db.connect() as conn:
            rows = conn.execute(f"{_SETTING_SELECT} ORDER BY key").fetchall()
            return {row["key"]: decode_json(row["value"]) for row in rows}

    def delete(self, key: str) -> bool:
        """Delete a setting; returns ``True`` when it existed."""
        if not isinstance(key, str) or not key.strip():
            return False
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            return cursor.rowcount > 0
