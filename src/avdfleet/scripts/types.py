"""Shared value types, protocols and in-memory fallbacks for :mod:`avdfleet.scripts`.

The script subsystem is deliberately backend-agnostic: the service only ever
talks to duck-typed store adapters (``store.scripts`` / ``store.runs`` as
provided by :mod:`avdfleet.store`) and to command runners.  This module owns the
small value objects exchanged between those pieces plus the in-memory fallbacks
used by tests and by the CLI when no daemon database is wired yet.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..core.errors import NotFoundError
from ..core.models import (
    AvdInfo,
    InstanceResult,
    RunState,
    ScriptDefinition,
    ScriptRun,
)

__all__ = [
    "CommandResult",
    "CommandRunner",
    "EventCallback",
    "InstancePlan",
    "InstanceTarget",
    "MemoryRunStore",
    "MemoryScriptStore",
    "OutputCallback",
    "PlannedCommand",
    "RunRepositoryLike",
    "ScriptContext",
    "ScriptPlanner",
    "ScriptRepositoryLike",
    "new_run_id",
]

#: ``(instance, stream, line)`` where stream is ``"stdout"`` or ``"stderr"``.
OutputCallback = Callable[[str, str, str], None]
#: ``(event_type, payload)`` callback registered through ``ScriptService.on_event``.
EventCallback = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class InstanceTarget:
    """A resolved script target: the fleet instance name plus its adb serial."""

    name: str
    serial: str
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_avd(cls, avd: AvdInfo) -> InstanceTarget:
        serial = avd.adb_serial
        if not serial and avd.ports is not None and avd.ports.console:
            serial = f"emulator-{avd.ports.console}"
        return cls(name=avd.name, serial=serial or avd.name)


@dataclass(frozen=True)
class PlannedCommand:
    """One process invocation planned for one instance.

    ``env`` is either the complete environment for the child process or ``None``
    to inherit the daemon environment.  ``display`` is the command line that may
    be shown in a dry run / persisted with the run; it is always redacted before
    leaving the service.
    """

    argv: tuple[str, ...]
    env: dict[str, str] | None = None
    cwd: str | None = None
    shell: bool = False
    display: str = ""

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("planned command requires at least one argv element")


@dataclass
class CommandResult:
    """Outcome of running one :class:`PlannedCommand`."""

    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False
    error: str = ""
    duration_s: float = 0.0
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True)
class InstancePlan:
    """A target and the commands that should execute for it."""

    target: InstanceTarget
    commands: tuple[PlannedCommand, ...] = ()


@dataclass(frozen=True)
class ScriptContext:
    """Per-run context handed to planners/runners."""

    run_id: str
    script_id: str
    work_dir: Path
    request_env: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False


@runtime_checkable
class CommandRunner(Protocol):
    """Runs one planned command, streaming output and honouring cancel/timeout."""

    def run(
        self,
        command: PlannedCommand,
        *,
        timeout_s: float | None,
        cancel: threading.Event | None = None,
        on_output: Callable[[str, str], None] | None = None,
    ) -> CommandResult: ...


@runtime_checkable
class ScriptPlanner(Protocol):
    """Builds the commands for a script/target pair."""

    def plan(
        self,
        script: ScriptDefinition,
        target: InstanceTarget,
        *,
        context: ScriptContext,
    ) -> Sequence[PlannedCommand]: ...


@runtime_checkable
class ScriptRepositoryLike(Protocol):
    """Subset of :class:`avdfleet.store.ScriptRepository` used by the service."""

    def list(self) -> list[ScriptDefinition]: ...

    def get(self, script_id: str) -> ScriptDefinition: ...

    def upsert(self, script: ScriptDefinition) -> ScriptDefinition: ...

    def delete(self, script_id: str) -> None: ...


@runtime_checkable
class RunRepositoryLike(Protocol):
    """Subset of :class:`avdfleet.store.RunRepository` used by the service."""

    def list(self, limit: int = 50) -> list[ScriptRun]: ...

    def get(self, run_id: str) -> ScriptRun: ...

    def upsert(self, run: ScriptRun) -> ScriptRun: ...


# ---------------------------------------------------------------------------
# In-memory fallbacks (same shape as the SQLite repositories)
# ---------------------------------------------------------------------------


class MemoryScriptStore:
    """Thread-safe in-memory script store used when no SQLite store is wired."""

    def __init__(self) -> None:
        self._items: dict[str, ScriptDefinition] = {}
        self._lock = threading.RLock()

    def list(self) -> list[ScriptDefinition]:
        with self._lock:
            scripts = [script.model_copy(deep=True) for script in self._items.values()]
        return sorted(scripts, key=lambda item: (item.name.casefold(), item.id))

    def get(self, script_id: str) -> ScriptDefinition:
        with self._lock:
            script = self._items.get(script_id)
            if script is None:
                raise NotFoundError(f"script {script_id!r} not found")
            return script.model_copy(deep=True)

    def get_by_name(self, name: str) -> ScriptDefinition | None:
        with self._lock:
            for script in self._items.values():
                if script.name == name:
                    return script.model_copy(deep=True)
        return None

    def upsert(self, script: ScriptDefinition) -> ScriptDefinition:
        stored = script.model_copy(deep=True)
        with self._lock:
            self._items[stored.id] = stored
            return stored.model_copy(deep=True)

    def delete(self, script_id: str) -> None:
        with self._lock:
            if script_id not in self._items:
                raise NotFoundError(f"script {script_id!r} not found")
            del self._items[script_id]


class MemoryRunStore:
    """Thread-safe in-memory run store mirroring ``RunRepository`` semantics."""

    def __init__(self) -> None:
        self._items: dict[str, ScriptRun] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()

    def list(self, limit: int = 50) -> list[ScriptRun]:
        capped = max(0, int(limit))
        with self._lock:
            runs = [self._items[run_id].model_copy(deep=True) for run_id in reversed(self._order)]
        return runs[:capped]

    def get(self, run_id: str) -> ScriptRun:
        with self._lock:
            run = self._items.get(run_id)
            if run is None:
                raise NotFoundError(f"run {run_id!r} not found")
            return run.model_copy(deep=True)

    def upsert(self, run: ScriptRun) -> ScriptRun:
        stored = run.model_copy(deep=True)
        with self._lock:
            if stored.id not in self._items:
                self._order.append(stored.id)
            self._items[stored.id] = stored
            return stored.model_copy(deep=True)

    def finish(
        self,
        run: str | ScriptRun,
        *,
        state: RunState | None = None,
        results: Sequence[InstanceResult] | None = None,
        error: str | None = None,
        finished_at: datetime | None = None,
    ) -> ScriptRun:
        run_id = run.id if isinstance(run, ScriptRun) else run
        with self._lock:
            current = self._items.get(run_id)
            if current is None:
                raise NotFoundError(f"run {run_id!r} not found")
            updated = current.model_copy(deep=True)
            if state is not None:
                updated.state = state
            if results is not None:
                updated.results = [result.model_copy(deep=True) for result in results]
            if error is not None:
                updated.error = error
            if finished_at is not None:
                updated.finished_at = finished_at
            self._items[run_id] = updated
            return updated.model_copy(deep=True)


def new_run_id() -> str:
    """Return a fresh run identifier (kept here so tests can patch it)."""
    import uuid

    return uuid.uuid4().hex
