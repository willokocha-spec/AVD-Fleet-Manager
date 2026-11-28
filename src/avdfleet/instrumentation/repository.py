"""SQLite persistence for the Root & Instrumentation subsystem.

Kept inside the ``instrumentation`` package (rather than ``store.repositories``)
so the feature can be read in one place; it uses the same :class:`Database`
handle and the same encoding helpers as every other repository.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import ConflictError, NotFoundError
from ..core.models import utcnow
from ..store.db import Database
from ..store.models import (
    decode_datetime,
    decode_json,
    encode_datetime,
    encode_json,
)
from .models import (
    ComponentState,
    DeploymentRun,
    InstrumentationProfile,
    LogEntry,
    ModuleKind,
    ModuleRecord,
    ProfileStep,
    RunState,
    StepAction,
    StepResult,
    TargetResult,
)

_MODULE_COLUMNS = (
    "id, kind, name, package_name, version, version_code, min_sdk, abi, description,"
    " sha256, size_bytes, source, source_url, file_path, metadata, created_at, updated_at"
)
_MODULE_INSERT = (
    "INSERT INTO instrumentation_modules (" + _MODULE_COLUMNS + ")"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_PROFILE_COLUMNS = "id, name, description, revision, steps, builtin, created_at, updated_at"
_PROFILE_INSERT = (
    "INSERT INTO instrumentation_profiles (" + _PROFILE_COLUMNS + ")"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
_PROFILE_UPSERT = (
    "INSERT INTO instrumentation_profiles (" + _PROFILE_COLUMNS + ")"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    " ON CONFLICT(id) DO UPDATE SET name = excluded.name, description = excluded.description,"
    " revision = excluded.revision, steps = excluded.steps, builtin = excluded.builtin,"
    " updated_at = excluded.updated_at"
)
_RUN_COLUMNS = (
    "id, profile_id, label, state, targets, results, error, created_at, started_at, finished_at"
)
_RUN_INSERT = (
    "INSERT INTO instrumentation_runs (" + _RUN_COLUMNS + ")"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_RUN_UPSERT = (
    "INSERT INTO instrumentation_runs (" + _RUN_COLUMNS + ")"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    " ON CONFLICT(id) DO UPDATE SET state = excluded.state, results = excluded.results,"
    " error = excluded.error, started_at = excluded.started_at, finished_at = excluded.finished_at"
)


def _module_from_row(row: Any) -> ModuleRecord:
    return ModuleRecord(
        id=row["id"],
        kind=ModuleKind(row["kind"]),
        name=row["name"],
        package_name=row["package_name"],
        version=row["version"],
        version_code=int(row["version_code"] or 0),
        min_sdk=int(row["min_sdk"] or 0),
        abi=row["abi"],
        description=row["description"],
        sha256=row["sha256"],
        size_bytes=int(row["size_bytes"] or 0),
        source=row["source"],
        source_url=row["source_url"],
        file_path=row["file_path"],
        metadata=decode_json(row["metadata"], {}) or {},
        created_at=decode_datetime(row["created_at"]) or ModuleRecord.model_fields["created_at"].default,
        updated_at=decode_datetime(row["updated_at"]) or ModuleRecord.model_fields["updated_at"].default,
    )


def _module_params(record: ModuleRecord) -> tuple[Any, ...]:
    return (
        record.id,
        str(record.kind),
        record.name,
        record.package_name,
        record.version,
        record.version_code,
        record.min_sdk,
        record.abi,
        record.description,
        record.sha256,
        record.size_bytes,
        record.source,
        record.source_url,
        record.file_path,
        encode_json(record.metadata),
        encode_datetime(record.created_at),
        encode_datetime(record.updated_at),
    )


def _profile_from_row(row: Any) -> InstrumentationProfile:
    raw_steps = decode_json(row["steps"], []) or []
    steps = [
        ProfileStep(
            id=str(item.get("id", "")),
            action=StepAction(item.get("action", StepAction.VERIFY)),
            module_id=str(item.get("module_id", "") or ""),
            module_identifier=str(item.get("module_identifier", "") or ""),
            params=dict(item.get("params") or {}),
        )
        for item in raw_steps
        if isinstance(item, dict)
    ]
    return InstrumentationProfile(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        revision=int(row["revision"] or 1),
        steps=steps,
        builtin=bool(row["builtin"]),
        created_at=decode_datetime(row["created_at"]) or utcnow(),
        updated_at=decode_datetime(row["updated_at"]) or utcnow(),
    )


def _profile_params(profile: InstrumentationProfile) -> tuple[Any, ...]:
    return (
        profile.id,
        profile.name,
        profile.description,
        profile.revision,
        encode_json([step.model_dump(mode="json") for step in profile.steps]),
        1 if profile.builtin else 0,
        encode_datetime(profile.created_at),
        encode_datetime(profile.updated_at),
    )


def _component_state(value: Any) -> ComponentState:
    """Decode a persisted component state, defaulting to ``unknown``.

    ``state`` is part of every freshly executed step result; persisting it keeps
    the "installed / enabled / active" distinction alive across reloads instead of
    silently degrading a re-read run.
    """
    if isinstance(value, ComponentState):
        return value
    try:
        return ComponentState(str(value))
    except (TypeError, ValueError):
        return ComponentState.UNKNOWN


def _run_from_row(row: Any) -> DeploymentRun:
    raw_results = decode_json(row["results"], []) or []
    results: list[TargetResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        steps = [
            StepResult(
                step_id=str(step.get("step_id", "")),
                action=StepAction(step.get("action", StepAction.VERIFY)),
                ok=bool(step.get("ok")),
                state=_component_state(step.get("state")),
                message=str(step.get("message", "")),
                data=dict(step.get("data") or {}),
            )
            for step in item.get("steps") or []
            if isinstance(step, dict)
        ]
        results.append(
            TargetResult(
                instance=str(item.get("instance", "")),
                ok=bool(item.get("ok")),
                skipped=bool(item.get("skipped")),
                error=str(item.get("error", "")),
                steps=steps,
            )
        )
    return DeploymentRun(
        id=row["id"],
        profile_id=row["profile_id"],
        label=row["label"],
        state=RunState(row["state"]),
        targets=list(decode_json(row["targets"], []) or []),
        results=results,
        error=row["error"],
        created_at=decode_datetime(row["created_at"]) or utcnow(),
        started_at=decode_datetime(row["started_at"]),
        finished_at=decode_datetime(row["finished_at"]),
    )


def _run_params(run: DeploymentRun) -> tuple[Any, ...]:
    return (
        run.id,
        run.profile_id,
        run.label,
        str(run.state),
        encode_json(run.targets),
        encode_json([result.model_dump(mode="json") for result in run.results]),
        run.error,
        encode_datetime(run.created_at),
        encode_datetime(run.started_at),
        encode_datetime(run.finished_at),
    )


class InstrumentationRepository:
    """Storage for modules, profiles, deployment runs and their logs."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # -- module library --------------------------------------------------
    def list_modules(self, kind: ModuleKind | None = None) -> list[ModuleRecord]:
        query = f"SELECT {_MODULE_COLUMNS} FROM instrumentation_modules"
        params: tuple[Any, ...] = ()
        if kind is not None:
            query += " WHERE kind = ?"
            params = (str(kind),)
        query += " ORDER BY created_at DESC, id DESC"
        with self._db.connect() as conn:
            return [_module_from_row(row) for row in conn.execute(query, params).fetchall()]

    def get_module(self, module_id: str) -> ModuleRecord | None:
        with self._db.connect() as conn:
            row = conn.execute(
                f"SELECT {_MODULE_COLUMNS} FROM instrumentation_modules WHERE id = ?",
                (module_id,),
            ).fetchone()
            return _module_from_row(row) if row is not None else None

    def get_module_by_sha(self, sha256: str) -> ModuleRecord | None:
        with self._db.connect() as conn:
            row = conn.execute(
                f"SELECT {_MODULE_COLUMNS} FROM instrumentation_modules WHERE sha256 = ?",
                (sha256,),
            ).fetchone()
            return _module_from_row(row) if row is not None else None

    def add_module(self, record: ModuleRecord) -> ModuleRecord:
        existing = self.get_module_by_sha(record.sha256)
        if existing is not None:
            raise ConflictError(
                f"module already imported as {existing.name or existing.id}",
                hint=f"reuse module id {existing.id} instead of importing it again",
            )
        with self._db.transaction() as conn:
            conn.execute(_MODULE_INSERT, _module_params(record))
        return record

    def delete_module(self, module_id: str) -> None:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM instrumentation_modules WHERE id = ?", (module_id,)
            )
        if cursor.rowcount == 0:
            raise NotFoundError(f"module not found: {module_id}")

    # -- profiles --------------------------------------------------------
    def list_profiles(self) -> list[InstrumentationProfile]:
        with self._db.connect() as conn:
            rows = conn.execute(
                f"SELECT {_PROFILE_COLUMNS} FROM instrumentation_profiles"
                " ORDER BY builtin DESC, name ASC"
            ).fetchall()
            return [_profile_from_row(row) for row in rows]

    def get_profile(self, profile_id: str) -> InstrumentationProfile | None:
        with self._db.connect() as conn:
            row = conn.execute(
                f"SELECT {_PROFILE_COLUMNS} FROM instrumentation_profiles WHERE id = ?",
                (profile_id,),
            ).fetchone()
            return _profile_from_row(row) if row is not None else None

    def get_profile_by_name(self, name: str) -> InstrumentationProfile | None:
        with self._db.connect() as conn:
            row = conn.execute(
                f"SELECT {_PROFILE_COLUMNS} FROM instrumentation_profiles WHERE name = ?",
                (name,),
            ).fetchone()
            return _profile_from_row(row) if row is not None else None

    def save_profile(self, profile: InstrumentationProfile) -> InstrumentationProfile:
        clash = self.get_profile_by_name(profile.name)
        if clash is not None and clash.id != profile.id:
            raise ConflictError(
                f"a profile named {profile.name!r} already exists",
                hint=f"choose another name or edit {clash.id}",
            )
        with self._db.transaction() as conn:
            conn.execute(_PROFILE_UPSERT, _profile_params(profile))
        return profile

    def add_profile(self, profile: InstrumentationProfile) -> InstrumentationProfile:
        if self.get_profile_by_name(profile.name) is not None:
            raise ConflictError(f"a profile named {profile.name!r} already exists")
        with self._db.transaction() as conn:
            conn.execute(_PROFILE_INSERT, _profile_params(profile))
        return profile

    def delete_profile(self, profile_id: str) -> None:
        profile = self.get_profile(profile_id)
        if profile is None:
            raise NotFoundError(f"profile not found: {profile_id}")
        if profile.builtin:
            raise ConflictError(f"profile {profile.name!r} is built in and cannot be deleted")
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM instrumentation_profiles WHERE id = ?", (profile_id,))

    # -- runs + logs -----------------------------------------------------
    def create_run(self, run: DeploymentRun) -> DeploymentRun:
        with self._db.transaction() as conn:
            conn.execute(_RUN_INSERT, _run_params(run))
        return run

    def save_run(self, run: DeploymentRun) -> DeploymentRun:
        with self._db.transaction() as conn:
            conn.execute(_RUN_UPSERT, _run_params(run))
        return run

    def get_run(self, run_id: str) -> DeploymentRun | None:
        with self._db.connect() as conn:
            row = conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM instrumentation_runs WHERE id = ?", (run_id,)
            ).fetchone()
            return _run_from_row(row) if row is not None else None

    def list_runs(self, limit: int = 50) -> list[DeploymentRun]:
        safe_limit = max(1, min(int(limit), 500))
        with self._db.connect() as conn:
            rows = conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM instrumentation_runs"
                " ORDER BY created_at DESC, id DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
            return [_run_from_row(row) for row in rows]

    def append_log(self, entry: LogEntry) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO instrumentation_logs (run_id, instance, level, message, at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    entry.run_id,
                    entry.instance,
                    entry.level,
                    entry.message,
                    encode_datetime(entry.at),
                ),
            )

    def list_logs(self, run_id: str, limit: int = 500) -> list[LogEntry]:
        safe_limit = max(1, min(int(limit), 5000))
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT id, run_id, instance, level, message, at FROM instrumentation_logs"
                " WHERE run_id = ? ORDER BY id ASC LIMIT ?",
                (run_id, safe_limit),
            ).fetchall()
            return [
                LogEntry(
                    id=int(row["id"]),
                    run_id=row["run_id"],
                    instance=row["instance"],
                    level=row["level"],
                    message=row["message"],
                    at=decode_datetime(row["at"]) or utcnow(),
                )
                for row in rows
            ]


__all__ = ["InstrumentationRepository"]
