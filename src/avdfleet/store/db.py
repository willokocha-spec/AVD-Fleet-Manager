"""SQLite lifecycle and the public :class:`Store` facade.

Threading model
---------------
The API runs blocking work in a threadpool while the script executor owns its
own worker threads, so the store must be safe to call concurrently:

* **on-disk databases** get a short-lived connection per operation (WAL mode,
  ``busy_timeout``, foreign keys on).  SQLite serialises writers; readers keep
  running against the previous WAL snapshot.  Open/close per operation means no
  connection object is ever shared between threads.
* **in-memory databases** (``":memory:"``) cannot share a WAL between
  connections, so one keeper connection is held for the lifetime of the store
  and every operation runs under a re-entrant lock.  This is the "locked
  connection" alternative and is what makes ``Store(":memory:")`` work in unit
  tests.

Writes use ``BEGIN IMMEDIATE`` so lock acquisition happens up front and cannot
fail half-way through a multi-statement change.  Migrations are applied inside
such a transaction, making startup atomic and concurrent-start safe.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from ..core.logging import redact_text
from .errors import StoreClosedError, StoreError, StoreNotInitializedError
from .migrations import SCHEMA_VERSION, ensure_schema
from .repositories import (
    AssignmentRepository,
    ImageRepository,
    InstanceRepository,
    OobeRepository,
    ProxyRepository,
    RunRepository,
    ScriptRepository,
    SettingsRepository,
    TemplateRepository,
)

__all__ = ["Database", "Store"]

_T = TypeVar("_T")

#: Extra attempts around ``BEGIN IMMEDIATE`` after the busy timeout expires.
_LOCK_RETRIES = 3
_LOCK_BACKOFF_S = 0.05


def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


class Database:
    """Owns the SQLite path, connection policy and schema lifecycle.

    Repositories receive this object and never cache connections themselves.
    """

    def __init__(self, db_path: str | Path, *, timeout: float = 30.0) -> None:
        timeout_value = float(timeout)
        if timeout_value <= 0:
            raise ValueError("timeout must be greater than zero")
        self.timeout = timeout_value

        raw = str(db_path)
        self._display_path = raw
        self._memory = False
        self._path: Path | None = None
        self._target = raw
        self._uri = False
        if raw in ("", ":memory:"):
            self._memory = True
            self._target = ":memory:"
            self._display_path = ":memory:"
        elif "mode=memory" in raw:
            # Explicit shared-cache memory URI supplied by an embedding caller.
            self._memory = True
            self._target = raw
            self._uri = True
        else:
            path = Path(raw).expanduser()
            self._path = path
            self._target = str(path)
            self._display_path = str(path)

        self._lock = threading.RLock()
        self._keeper: sqlite3.Connection | None = None
        self._initialized = False
        self._closed = False

    # -- introspection ------------------------------------------------------
    @property
    def memory(self) -> bool:
        return self._memory

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def display_path(self) -> str:
        return self._display_path

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def closed(self) -> bool:
        return self._closed

    # -- connection handling ------------------------------------------------
    def _new_connection(self, *, initialize_journal: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._target,
            timeout=self.timeout,
            isolation_level=None,
            check_same_thread=not self._memory,
            uri=self._uri,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        conn.execute("PRAGMA synchronous = NORMAL")
        if initialize_journal and not self._memory:
            self._enable_wal(conn)
        return conn

    @staticmethod
    def _enable_wal(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("PRAGMA journal_mode = WAL").fetchone()
        except sqlite3.OperationalError:
            # Network shares and some exotic filesystems reject WAL; the database
            # still works with the default rollback journal and health() reports it.
            pass

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - connection already broken
            pass

    def _ensure_open(self) -> None:
        if self._closed:
            raise StoreClosedError("the store is closed")

    def _ensure_usable(self) -> None:
        self._ensure_open()
        if not self._initialized:
            raise StoreNotInitializedError("the store is not initialized; call Store.initialize() first")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured connection; closes it afterwards (except memory)."""
        self._ensure_usable()
        if self._memory:
            with self._lock:
                keeper = self._keeper
                if keeper is None:  # pragma: no cover - guarded by _initialized
                    raise StoreNotInitializedError("the store is not initialized")
                yield keeper
            return
        conn = self._new_connection()
        try:
            yield conn
        finally:
            conn.close()

    def read(self, callback: Callable[[sqlite3.Connection], _T]) -> _T:
        """Run a read-only callback on a managed connection."""
        with self.connect() as conn:
            return callback(conn)

    def _begin_immediate(self, conn: sqlite3.Connection) -> None:
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(_LOCK_RETRIES):
            try:
                conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                if not _is_locked_error(exc):
                    raise
                last_error = exc
                if attempt + 1 < _LOCK_RETRIES:
                    time.sleep(_LOCK_BACKOFF_S * (attempt + 1))
        raise StoreError("the database is busy; retry the operation") from last_error

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a write transaction, committing on success and rolling back on error."""
        with self.connect() as conn:
            if conn.in_transaction:
                raise StoreError("nested database transactions are not supported")
            self._begin_immediate(conn)
            try:
                yield conn
            except BaseException:
                self._rollback(conn)
                raise
            try:
                conn.execute("COMMIT")
            except sqlite3.OperationalError as exc:  # pragma: no cover - WAL commits rarely fail
                self._rollback(conn)
                if _is_locked_error(exc):
                    raise StoreError("could not commit the database transaction; retry") from exc
                raise

    # -- lifecycle ----------------------------------------------------------
    def initialize(self) -> None:
        """Create the database (if needed) and apply pending migrations.

        Idempotent and safe to call from several threads; on a shared database
        file, concurrent daemons serialise on the migration write lock.
        """
        with self._lock:
            self._ensure_open()
            if self._initialized:
                return
            conn: sqlite3.Connection | None = None
            memory_keeper = False
            try:
                if self._memory:
                    conn = self._new_connection()
                    self._keeper = conn
                    memory_keeper = True
                else:
                    if self._path is None:  # pragma: no cover - construction guarantees a path
                        raise StoreError("store path is not configured")
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                    conn = self._new_connection(initialize_journal=True)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    ensure_schema(conn)
                except BaseException:
                    self._rollback(conn)
                    raise
                conn.execute("COMMIT")
            except BaseException:
                if memory_keeper:
                    self._keeper = None
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:  # pragma: no cover - cleanup must not mask the cause
                        pass
                raise
            else:
                if not memory_keeper and conn is not None:
                    conn.close()
                self._initialized = True

    def close(self) -> None:
        """Checkpoint and close the database; idempotent."""
        with self._lock:
            if self._closed:
                return
            if self._initialized:
                if self._memory:
                    if self._keeper is not None:
                        try:
                            self._keeper.execute("PRAGMA optimize")
                        except sqlite3.Error:  # pragma: no cover - best effort
                            pass
                else:
                    try:
                        self._checkpoint_on_disk()
                    except sqlite3.Error:  # pragma: no cover - best effort
                        pass
            if self._keeper is not None:
                try:
                    self._keeper.close()
                except sqlite3.Error:  # pragma: no cover - already closed
                    pass
                self._keeper = None
            self._initialized = False
            self._closed = True

    def _checkpoint_on_disk(self) -> dict[str, Any]:
        conn = self._new_connection()
        try:
            return self._checkpoint_connection(conn)
        finally:
            conn.close()

    @staticmethod
    def _checkpoint_connection(conn: sqlite3.Connection) -> dict[str, Any]:
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if mode != "wal":
            return {"ok": True, "mode": mode, "busy": 0, "wal_frames": -1, "checkpointed": -1}
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        busy, wal_frames, checkpointed = (int(row[0]), int(row[1]), int(row[2])) if row else (0, -1, -1)
        return {
            "ok": busy == 0,
            "mode": mode,
            "busy": busy,
            "wal_frames": wal_frames,
            "checkpointed": checkpointed,
        }

    # -- diagnostics --------------------------------------------------------
    def checkpoint(self) -> dict[str, Any]:
        """Force a WAL checkpoint so the main database file is complete on disk."""
        self._ensure_usable()
        if self._memory:
            return {"ok": True, "mode": "memory", "busy": 0, "wal_frames": 0, "checkpointed": 0}
        with self._lock:
            return self._checkpoint_on_disk()

    def health(self) -> dict[str, Any]:
        """Return a JSON-safe store health snapshot (never raises for I/O errors)."""
        payload: dict[str, Any] = {
            "path": self._display_path,
            "memory": self._memory,
            "initialized": self._initialized,
            "closed": self._closed,
        }
        if self._closed:
            payload.update(ok=False, error="store is closed")
            return payload
        if not self._initialized:
            payload.update(ok=False, schema_version=0, error="store is not initialized")
            return payload
        try:
            with self.connect() as conn:
                schema_version = int(
                    conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
                )
                user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
                foreign_keys = bool(int(conn.execute("PRAGMA foreign_keys").fetchone()[0]))
                busy_timeout_ms = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
                integrity = str(conn.execute("PRAGMA quick_check(1)").fetchone()[0])
                tables = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchone()[0]
                )
        except sqlite3.Error as exc:
            payload.update(ok=False, error=f"database error: {redact_text(str(exc))}")
            return payload
        payload.update(
            ok=integrity.lower() == "ok" and schema_version == SCHEMA_VERSION,
            schema_version=schema_version,
            schema_current=schema_version == SCHEMA_VERSION,
            user_version=user_version,
            journal_mode=journal_mode,
            foreign_keys=foreign_keys,
            busy_timeout_ms=busy_timeout_ms,
            integrity=integrity,
            tables=tables,
        )
        if self._path is not None:
            try:
                payload["size_bytes"] = self._path.stat().st_size
                wal_path = self._path.with_name(f"{self._path.name}-wal")
                payload["wal_bytes"] = wal_path.stat().st_size if wal_path.exists() else 0
            except OSError:
                payload["size_bytes"] = 0
                payload["wal_bytes"] = 0
        return payload

    def schema_version(self) -> int:
        """Return the applied schema version (requires an initialized store)."""
        with self.connect() as conn:
            return int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0])

    def applied_migrations(self) -> list[tuple[int, str, str]]:
        """Return ``(version, name, applied_at)`` rows in migration order."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT version, name, applied_at FROM schema_version ORDER BY version"
            ).fetchall()
            return [(int(row[0]), str(row[1]), str(row[2])) for row in rows]


class Store:
    """Thread-safe SQLite store with one repository per entity.

    The store is explicit about its lifecycle: construct it, call
    :meth:`initialize` once at startup (daemon or CLI) and :meth:`close` on
    shutdown.  Repository attributes are safe to share between threads.

    Example::

        store = Store(paths.db_path)
        store.initialize()
        store.templates.upsert(Template(id="t1", name="Play 35", image_id=image_id))
        store.close()
    """

    def __init__(self, db_path: str | Path, timeout: float = 30.0) -> None:
        self._db = Database(db_path, timeout=timeout)
        # One stateless repository instance per entity; they share the database
        # connection policy and hold no per-thread state of their own.
        self.templates = TemplateRepository(self._db)
        self.instances = InstanceRepository(self._db)
        self.proxies = ProxyRepository(self._db)
        self.assignments = AssignmentRepository(self._db)
        self.scripts = ScriptRepository(self._db)
        self.runs = RunRepository(self._db)
        self.images = ImageRepository(self._db)
        self.oobe = OobeRepository(self._db)
        self.settings = SettingsRepository(self._db)

    # -- lifecycle ----------------------------------------------------------
    def initialize(self) -> None:
        """Create/open the database and apply pending migrations (idempotent)."""
        self._db.initialize()

    def close(self) -> None:
        """Flush WAL state and close the store (idempotent)."""
        self._db.close()

    def health(self) -> dict[str, Any]:
        """Return a JSON-safe health snapshot for the API/CLI."""
        return self._db.health()

    def checkpoint(self) -> dict[str, Any]:
        """Force a WAL checkpoint (no-op result for in-memory stores)."""
        return self._db.checkpoint()

    # -- introspection ------------------------------------------------------
    @property
    def db_path(self) -> Path | str:
        return self._db.path if self._db.path is not None else ":memory:"

    @property
    def is_memory(self) -> bool:
        return self._db.memory

    @property
    def initialized(self) -> bool:
        return self._db.initialized

    @property
    def schema_version(self) -> int:
        """Applied schema version, or ``0`` while the store is not usable."""
        if self._db.closed or not self._db.initialized:
            return 0
        return self._db.schema_version()

    def applied_migrations(self) -> list[tuple[int, str, str]]:
        """Return the applied migration ledger rows."""
        return self._db.applied_migrations()

    # -- context manager ----------------------------------------------------
    def __enter__(self) -> Store:
        self.initialize()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"Store({self.db_path!r}, initialized={self.initialized})"
