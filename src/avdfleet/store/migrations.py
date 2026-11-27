"""Forward-only schema migrations for the AVD Fleet SQLite database.

Each migration is an ordered tuple of SQL statements applied inside the
caller's transaction (:meth:`avdfleet.store.db.Database.initialize` opens
``BEGIN IMMEDIATE`` first).  SQLite DDL is transactional, so a crashed or
concurrent upgrade can never leave a half-applied schema behind; the second
process waits on the write lock and then observes the migrated version.

The ``schema_version`` table is the source of truth (``PRAGMA user_version`` is
kept in sync for external tooling).  Never edit an already-released migration --
append a new :class:`Migration` and bump :data:`SCHEMA_VERSION` instead.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..core.models import utcnow
from .errors import StoreSchemaError
from .models import encode_datetime

__all__ = [
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "Migration",
    "current_version",
    "ensure_schema",
]

SCHEMA_VERSION = 3

#: Created before any migration runs.  ``version`` is the primary key so each
#: migration is recorded at most once, even if two daemons race at startup.
SCHEMA_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class Migration:
    """One forward schema step."""

    version: int
    name: str
    statements: tuple[str, ...]


# ---------------------------------------------------------------------------
# v1 -- core entities: templates, instances, proxies, assignments, scripts,
#        images and key/value settings.
# ---------------------------------------------------------------------------

_V1_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE templates (
        id TEXT PRIMARY KEY NOT NULL,
        name TEXT NOT NULL,
        image_id TEXT NOT NULL,
        device_id TEXT NOT NULL DEFAULT 'pixel_7',
        resolution TEXT NOT NULL DEFAULT '',
        density INTEGER,
        ram_mb INTEGER NOT NULL DEFAULT 2048,
        heap_mb INTEGER NOT NULL DEFAULT 256,
        cpu_cores INTEGER NOT NULL DEFAULT 2,
        gpu_mode TEXT NOT NULL DEFAULT 'swiftshader_indirect',
        headless INTEGER NOT NULL DEFAULT 1 CHECK (headless IN (0, 1)),
        proxy_id TEXT,
        scripts TEXT NOT NULL DEFAULT '[]',
        tags TEXT NOT NULL DEFAULT '[]',
        description TEXT NOT NULL DEFAULT '',
        config TEXT NOT NULL DEFAULT '{}',
        created_at TEXT,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE instances (
        name TEXT PRIMARY KEY NOT NULL,
        template_id TEXT,
        image_id TEXT,
        image_name TEXT NOT NULL DEFAULT '',
        device_id TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'defined',
        pid INTEGER,
        ports TEXT,
        proxy_id TEXT,
        adb_serial TEXT,
        boot_completed INTEGER NOT NULL DEFAULT 0 CHECK (boot_completed IN (0, 1)),
        created_at TEXT,
        started_at TEXT,
        uptime_seconds REAL,
        error TEXT NOT NULL DEFAULT '',
        config TEXT NOT NULL DEFAULT '{}',
        grpc_endpoint TEXT,
        screen_available INTEGER NOT NULL DEFAULT 0 CHECK (screen_available IN (0, 1))
    )
    """,
    # `secret_ref` is a vault key.  There is deliberately no password column:
    # proxy credentials must never be persisted in SQLite.
    """
    CREATE TABLE proxies (
        id TEXT PRIMARY KEY NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        scheme TEXT NOT NULL DEFAULT 'http',
        host TEXT NOT NULL,
        port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
        username TEXT,
        secret_ref TEXT,
        tags TEXT NOT NULL DEFAULT '[]',
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        source TEXT NOT NULL DEFAULT '',
        health TEXT NOT NULL DEFAULT '{}',
        created_at TEXT
    )
    """,
    """
    CREATE TABLE assignments (
        avd_name TEXT PRIMARY KEY NOT NULL,
        proxy_id TEXT NOT NULL REFERENCES proxies(id) ON DELETE CASCADE ON UPDATE CASCADE,
        assigned_at TEXT
    )
    """,
    """
    CREATE TABLE scripts (
        id TEXT PRIMARY KEY NOT NULL,
        name TEXT NOT NULL,
        type TEXT NOT NULL DEFAULT 'adb_shell',
        content TEXT NOT NULL DEFAULT '',
        path TEXT,
        description TEXT NOT NULL DEFAULT '',
        timeout_s INTEGER NOT NULL DEFAULT 120,
        env TEXT NOT NULL DEFAULT '{}',
        targets TEXT NOT NULL DEFAULT '[]',
        builtin INTEGER NOT NULL DEFAULT 0 CHECK (builtin IN (0, 1)),
        tags TEXT NOT NULL DEFAULT '[]',
        created_at TEXT,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE images (
        id TEXT PRIMARY KEY NOT NULL,
        api_level INTEGER NOT NULL DEFAULT 0,
        tag TEXT NOT NULL DEFAULT '',
        abi TEXT NOT NULL DEFAULT 'x86_64',
        kind TEXT NOT NULL DEFAULT 'unknown',
        display_name TEXT NOT NULL DEFAULT '',
        package TEXT NOT NULL DEFAULT '',
        url TEXT,
        sha256 TEXT,
        size_bytes INTEGER,
        installed INTEGER NOT NULL DEFAULT 0 CHECK (installed IN (0, 1)),
        local_path TEXT,
        oobe_status TEXT NOT NULL DEFAULT 'unverified',
        custom INTEGER NOT NULL DEFAULT 0 CHECK (custom IN (0, 1)),
        notes TEXT NOT NULL DEFAULT '',
        metadata TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE settings (
        key TEXT PRIMARY KEY NOT NULL,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_templates_name ON templates(name)",
    "CREATE INDEX idx_instances_state ON instances(state)",
    "CREATE INDEX idx_instances_template ON instances(template_id)",
    "CREATE INDEX idx_proxies_name ON proxies(name)",
    "CREATE INDEX idx_proxies_enabled ON proxies(enabled)",
    "CREATE INDEX idx_assignments_proxy ON assignments(proxy_id)",
    "CREATE INDEX idx_scripts_name ON scripts(name)",
    "CREATE INDEX idx_images_catalogue ON images(api_level, abi)",
    "CREATE INDEX idx_images_kind ON images(kind)",
)

# ---------------------------------------------------------------------------
# v2 -- execution history and OOBE evidence.
# ---------------------------------------------------------------------------

_V2_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE runs (
        id TEXT PRIMARY KEY NOT NULL,
        script_id TEXT NOT NULL,
        script_name TEXT NOT NULL DEFAULT '',
        type TEXT NOT NULL DEFAULT 'adb_shell',
        state TEXT NOT NULL DEFAULT 'queued',
        instances TEXT NOT NULL DEFAULT '[]',
        results TEXT NOT NULL DEFAULT '[]',
        started_at TEXT,
        finished_at TEXT,
        error TEXT NOT NULL DEFAULT '',
        dry_run INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0, 1)),
        requested_by TEXT NOT NULL DEFAULT '',
        request TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE oobe_records (
        image_id TEXT PRIMARY KEY NOT NULL,
        api_level INTEGER NOT NULL DEFAULT 0,
        tag TEXT NOT NULL DEFAULT '',
        abi TEXT NOT NULL DEFAULT 'x86_64',
        status TEXT NOT NULL DEFAULT 'unverified',
        activity TEXT NOT NULL DEFAULT '',
        first_run_component TEXT NOT NULL DEFAULT '',
        screenshot_path TEXT NOT NULL DEFAULT '',
        boot_seconds REAL,
        reset_seconds REAL,
        checked_at TEXT,
        checked_by TEXT NOT NULL DEFAULT '',
        emulator_version TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        evidence TEXT NOT NULL DEFAULT '[]'
    )
    """,
    "CREATE INDEX idx_runs_updated ON runs(updated_at DESC)",
    "CREATE INDEX idx_runs_script ON runs(script_id, updated_at DESC)",
    "CREATE INDEX idx_runs_state ON runs(state)",
    "CREATE INDEX idx_oobe_status ON oobe_records(status)",
)

# ---------------------------------------------------------------------------
# v3 -- Root & Instrumentation: the central module library, reusable profiles
#        and deployment runs with their logs.
# ---------------------------------------------------------------------------

_V3_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE instrumentation_modules (
        id TEXT PRIMARY KEY NOT NULL,
        kind TEXT NOT NULL DEFAULT 'unknown',
        name TEXT NOT NULL DEFAULT '',
        package_name TEXT NOT NULL DEFAULT '',
        version TEXT NOT NULL DEFAULT '',
        version_code INTEGER NOT NULL DEFAULT 0,
        min_sdk INTEGER NOT NULL DEFAULT 0,
        abi TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        sha256 TEXT NOT NULL,
        size_bytes INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'upload',
        source_url TEXT NOT NULL DEFAULT '',
        file_path TEXT NOT NULL DEFAULT '',
        metadata TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX idx_instr_modules_sha ON instrumentation_modules(sha256)",
    "CREATE INDEX idx_instr_modules_kind ON instrumentation_modules(kind)",
    """
    CREATE TABLE instrumentation_profiles (
        id TEXT PRIMARY KEY NOT NULL,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 1,
        steps TEXT NOT NULL DEFAULT '[]',
        builtin INTEGER NOT NULL DEFAULT 0 CHECK (builtin IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX idx_instr_profiles_name ON instrumentation_profiles(name)",
    """
    CREATE TABLE instrumentation_runs (
        id TEXT PRIMARY KEY NOT NULL,
        profile_id TEXT NOT NULL DEFAULT '',
        label TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'queued',
        targets TEXT NOT NULL DEFAULT '[]',
        results TEXT NOT NULL DEFAULT '[]',
        error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT
    )
    """,
    "CREATE INDEX idx_instr_runs_created ON instrumentation_runs(created_at DESC)",
    "CREATE INDEX idx_instr_runs_state ON instrumentation_runs(state)",
    """
    CREATE TABLE instrumentation_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        instance TEXT NOT NULL DEFAULT '',
        level TEXT NOT NULL DEFAULT 'info',
        message TEXT NOT NULL,
        at TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_instr_logs_run ON instrumentation_logs(run_id, id)",
)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="core schema", statements=_V1_STATEMENTS),
    Migration(version=2, name="runs and oobe records", statements=_V2_STATEMENTS),
    Migration(version=3, name="root and instrumentation", statements=_V3_STATEMENTS),
)


def current_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied migration version (``0`` for a fresh database)."""
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
    return int(row[0]) if row is not None else 0


def ensure_schema(conn: sqlite3.Connection) -> int:
    """Apply every pending migration and return the resulting schema version.

    The caller must already hold a write transaction.  Re-checking the version
    inside that transaction is what makes concurrent startup safe: the losing
    process waits for the winner to commit and then finds nothing to do.
    """
    conn.execute(SCHEMA_VERSION_TABLE)
    version = current_version(conn)
    if version > SCHEMA_VERSION:
        raise StoreSchemaError(
            f"database schema version {version} is newer than this build supports "
            f"({SCHEMA_VERSION}); upgrade avdfleet",
            hint="run the newer avdfleet package or restore a matching backup",
        )
    for migration in MIGRATIONS:
        if migration.version <= version:
            continue
        # Individual statements (not executescript) so everything stays inside
        # the transaction the caller opened around migrations.
        for statement in migration.statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, encode_datetime(utcnow()) or ""),
        )
        version = migration.version
    # Keep the pragma in sync for external tooling/backups; the table remains
    # authoritative for migration bookkeeping.
    conn.execute(f"PRAGMA user_version = {int(version)}")
    return version
