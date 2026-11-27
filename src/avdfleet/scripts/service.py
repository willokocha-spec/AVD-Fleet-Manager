"""``ScriptService`` — persistent script library plus fan-out run orchestration.

The service owns four concerns:

1. CRUD for :class:`~avdfleet.core.models.ScriptDefinition` records and for
   :class:`~avdfleet.core.models.ScriptRun` history, persisted through an
   injected store adapter (``store.scripts`` / ``store.runs``, both duck-typed;
   the SQLite repositories from :mod:`avdfleet.store` match exactly).  With no
   store the service falls back to thread-safe in-memory repositories.
2. Seeding and serving the built-in script library.
3. Executing a run across many instances with bounded concurrency, live output
   events (``script.run.output`` on the :class:`~avdfleet.core.events.EventBus`),
   per-instance timeout, cancellation and combined results.
4. Lifecycle hooks and an ``on_event`` callback interface for API/CLI/runtime
   consumers.

``run()`` executes synchronously and returns the finished
:class:`ScriptRun` (CLI and tests).  ``submit()`` returns immediately with a
queued/running run and executes in a background thread (REST ``202 Accepted``).
Cancellation works both before a submitted run starts and while it is running.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..core.config import Config
from ..core.errors import NotFoundError, ScriptError, ValidationError
from ..core.events import EventBus, EventSink, EventType
from ..core.logging import get_logger, redact, redact_text
from ..core.models import (
    AvdInfo,
    InstanceResult,
    RunState,
    ScriptDefinition,
    ScriptRun,
    ScriptRunRequest,
    utcnow,
)
from ..core.paths import Paths
from .builtins import install_builtins
from .executor import FanOutExecutor
from .runners import CommandBuilder
from .types import (
    EventCallback,
    InstanceTarget,
    MemoryRunStore,
    MemoryScriptStore,
    RunRepositoryLike,
    ScriptContext,
    ScriptRepositoryLike,
    new_run_id,
)

logger = get_logger("scripts.service")

__all__ = ["ScriptService"]

_HOOKS = ("before_run", "after_run")


class _RunControl:
    """Per-run cancellation and finalisation state shared across threads."""

    __slots__ = ("cancel", "finalized", "lock", "started")

    def __init__(self) -> None:
        self.cancel = threading.Event()
        self.lock = threading.RLock()
        self.started = False
        self.finalized = False


class ScriptService:
    """Concrete implementation of :class:`avdfleet.core.interfaces.ScriptRunner`."""

    def __init__(
        self,
        *,
        store: Any | None = None,
        scripts: ScriptRepositoryLike | None = None,
        runs: RunRepositoryLike | None = None,
        event_bus: EventBus | None = None,
        event_sink: EventSink | None = None,
        config: Config | None = None,
        paths: Paths | None = None,
        work_dir: str | Path | None = None,
        instance_resolver: Callable[[str], Any] | None = None,
        command_builder: CommandBuilder | None = None,
        executor: FanOutExecutor | None = None,
        adb_command: Sequence[str] | None = None,
        python_command: Sequence[str] | None = None,
        node_command: Sequence[str] | None = None,
        which: Callable[[str], str | None] | None = None,
        appium_url: str | None = None,
        appium_url_resolver: Callable[[InstanceTarget], str] | None = None,
        environment: Mapping[str, str] | None = None,
        requested_by: str = "",
        source: str = "scripts",
        load_builtins_on_init: bool = True,
    ) -> None:
        resolved_scripts = scripts if scripts is not None else getattr(store, "scripts", None)
        resolved_runs = runs if runs is not None else getattr(store, "runs", None)
        self._scripts: ScriptRepositoryLike = resolved_scripts or MemoryScriptStore()
        self._runs: RunRepositoryLike = resolved_runs or MemoryRunStore()
        self._config = config or Config()
        self._paths: Paths | None
        if paths is not None:
            self._paths = paths
        elif config is not None:
            self._paths = config.resolved_paths()
        else:
            self._paths = None
        if work_dir is not None:
            self._work_dir = Path(work_dir)
        elif self._paths is not None:
            self._work_dir = Path(self._paths.artifacts_dir) / "script-runs"
        else:  # pragma: no cover - callers should inject paths in the daemon
            import tempfile

            self._work_dir = Path(tempfile.gettempdir()) / "avdfleet" / "script-runs"

        self._sink = event_sink if event_sink is not None else EventSink(event_bus)
        self._instance_resolver = instance_resolver
        self._builder = command_builder or CommandBuilder(
            adb_command=adb_command,
            python_command=python_command,
            node_command=node_command,
            which=which,
            base_env=environment,
            appium_url=appium_url,
            appium_url_resolver=appium_url_resolver,
        )
        self._executor = executor or FanOutExecutor()
        self._source = source
        self.requested_by = requested_by

        self._lock = threading.RLock()
        self._controls: dict[str, _RunControl] = {}
        self._threads: set[threading.Thread] = set()
        self._event_listeners: list[EventCallback] = []
        self._hooks: dict[str, list[Callable[..., Any]]] = {name: [] for name in _HOOKS}
        self._closed = False

        if load_builtins_on_init:
            try:
                install_builtins(self._scripts)
            except Exception:  # noqa: BLE001 - an uninitialised store must not break init
                logger.warning("could not seed built-in scripts", exc_info=True)

    # ------------------------------------------------------------------
    # Script library CRUD
    # ------------------------------------------------------------------
    def list_scripts(self) -> list[ScriptDefinition]:
        return list(self._scripts.list())

    def get_script(self, script_id: str) -> ScriptDefinition:
        return self._scripts.get(script_id)

    def save_script(self, script: ScriptDefinition) -> ScriptDefinition:
        if not isinstance(script, ScriptDefinition):
            raise ValidationError("script must be a ScriptDefinition")
        if not script.id.strip():
            raise ValidationError("script id must not be empty")
        if not script.name.strip():
            raise ValidationError("script name must not be empty")
        return self._scripts.upsert(script)

    def delete_script(self, script_id: str) -> None:
        self._scripts.delete(script_id)

    def load_builtins(self, *, overwrite: bool = False) -> list[ScriptDefinition]:
        """Seed the built-in library, optionally replacing edited builtins."""
        return install_builtins(self._scripts, overwrite=overwrite)

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------
    def run(self, request: ScriptRunRequest) -> ScriptRun:
        """Execute a run synchronously and return its final state."""
        run, script = self._prepare_run(request)
        self._execute(run, script, request)
        return self._snapshot(run)

    def submit(self, request: ScriptRunRequest) -> ScriptRun:
        """Start a run in a background thread and return the queued snapshot."""
        run, script = self._prepare_run(request)
        thread = threading.Thread(
            target=self._execute_guarded,
            args=(run, script, request),
            name=f"avdfleet-script-run-{run.id[:8]}",
            daemon=True,
        )
        with self._lock:
            self._threads.add(thread)
        thread.start()
        return self._snapshot(run)

    def cancel(self, run_id: str) -> ScriptRun:
        with self._lock:
            control = self._controls.get(run_id)
        run = self._runs.get(run_id)
        if run.state.terminal:
            return run
        if control is None:
            # A run persisted by a previous daemon process: mark it cancelled.
            return self._finish_run(run, RunState.CANCELLED, error="cancelled", control=None)
        control.cancel.set()
        with control.lock:
            started = control.started
            finalized = control.finalized
        if not started and not finalized:
            return self._finish_run(
                run,
                RunState.CANCELLED,
                error="cancelled before start",
                control=control,
            )
        return self._snapshot(run)

    def get_run(self, run_id: str) -> ScriptRun:
        return self._runs.get(run_id)

    def list_runs(self, *, limit: int = 50) -> list[ScriptRun]:
        if limit < 1:
            raise ValidationError("limit must be a positive integer")
        return list(self._runs.list(min(int(limit), 1000)))

    # ------------------------------------------------------------------
    # Hooks / events
    # ------------------------------------------------------------------
    def add_hook(self, name: str, callback: Callable[..., Any]) -> Callable[[], None]:
        """Register a lifecycle hook (``before_run`` / ``after_run``)."""
        if name not in _HOOKS:
            raise ValidationError(f"unknown script hook {name!r}", hint=f"valid hooks: {', '.join(_HOOKS)}")
        if not callable(callback):
            raise ValidationError("hook callback must be callable")
        with self._lock:
            self._hooks[name].append(callback)

        def _remove() -> None:
            with self._lock:
                if callback in self._hooks[name]:
                    self._hooks[name].remove(callback)

        return _remove

    def on_event(self, callback: EventCallback) -> Callable[[], None]:
        """Register a callback receiving ``(event_type, payload)``; returns unsubscribe."""
        if not callable(callback):
            raise ValidationError("event callback must be callable")
        with self._lock:
            self._event_listeners.append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                if callback in self._event_listeners:
                    self._event_listeners.remove(callback)

        return _unsubscribe

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def close(self) -> None:
        """Cancel every active run and wait (briefly) for worker threads."""
        with self._lock:
            self._closed = True
            controls = list(self._controls.values())
            threads = list(self._threads)
        for control in controls:
            control.cancel.set()
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=5.0)
        with self._lock:
            self._threads.clear()
        logger.debug("script service closed")

    def __enter__(self) -> ScriptService:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Run internals
    # ------------------------------------------------------------------
    def _prepare_run(self, request: ScriptRunRequest) -> tuple[ScriptRun, ScriptDefinition]:
        if self._closed:
            raise ScriptError("script service is closed")
        if not isinstance(request, ScriptRunRequest):
            raise ValidationError("request must be a ScriptRunRequest")
        if request.timeout_s is not None and request.timeout_s <= 0:
            raise ValidationError("timeout_s must be a positive number of seconds")
        script = self._scripts.get(request.script_id)
        names = [str(name).strip() for name in (request.instances or script.targets) if str(name).strip()]
        if not names:
            raise ValidationError(
                "script run requires at least one target instance",
                hint="pass 'instances' or set 'targets' on the script definition",
            )
        deduped = list(dict.fromkeys(names))
        self._work_dir.mkdir(parents=True, exist_ok=True)
        run = ScriptRun(
            id=new_run_id(),
            script_id=script.id,
            script_name=script.name,
            type=script.type,
            state=RunState.QUEUED,
            instances=deduped,
            results=[InstanceResult(instance=name, state=RunState.QUEUED) for name in deduped],
            dry_run=request.dry_run,
            requested_by=self.requested_by,
            request=self._redact_request(request),
        )
        control = _RunControl()
        with self._lock:
            self._controls[run.id] = control
        self._persist(run)
        return run, script

    def _execute_guarded(self, run: ScriptRun, script: ScriptDefinition, request: ScriptRunRequest) -> None:
        try:
            self._execute(run, script, request)
        finally:
            with self._lock:
                self._controls.pop(run.id, None)
                self._threads.discard(threading.current_thread())

    def _execute(self, run: ScriptRun, script: ScriptDefinition, request: ScriptRunRequest) -> None:
        with self._lock:
            control = self._controls.get(run.id)
        if control is None:  # pragma: no cover - only after close()
            return
        try:
            with control.lock:
                if control.finalized:
                    return
                control.started = True
            self._call_hooks("before_run", run)
            if control.cancel.is_set():
                self._finish_run(run, RunState.CANCELLED, error="cancelled before start", control=control)
                return
            run.state = RunState.RUNNING
            run.started_at = utcnow()
            self._persist(run)
            self._emit(
                EventType.SCRIPT_RUN_STARTED,
                {
                    "run_id": run.id,
                    "script_id": script.id,
                    "script_name": script.name,
                    "instances": list(run.instances),
                    "dry_run": run.dry_run,
                },
            )
            if run.dry_run:
                self._execute_dry_run(run, script, request, control)
            else:
                self._execute_fan_out(run, script, request, control)
        except Exception as exc:  # noqa: BLE001 - the run record must capture failures
            logger.exception("script run %s failed", run.id)
            self._finish_run(run, RunState.FAILED, error=redact_text(f"script run failed: {exc}"), control=control)
        finally:
            with self._lock:
                self._controls.pop(run.id, None)
                self._threads.discard(threading.current_thread())

    def _execute_fan_out(
        self,
        run: ScriptRun,
        script: ScriptDefinition,
        request: ScriptRunRequest,
        control: _RunControl,
    ) -> None:
        timeout_s = request.timeout_s if request.timeout_s is not None else script.timeout_s
        targets: list[InstanceTarget] = []
        failures: dict[str, InstanceResult] = {}
        for name in run.instances:
            try:
                targets.append(self._resolve_target(name))
            except Exception as exc:  # noqa: BLE001 - one bad target must not abort the fleet
                failures[name] = InstanceResult(
                    instance=name,
                    state=RunState.FAILED,
                    error=redact_text(str(exc)),
                    finished_at=utcnow(),
                    duration_s=0.0,
                )
        run_dir = self._run_dir(run.id)

        def plan_factory(target: InstanceTarget) -> list[Any]:
            context = ScriptContext(
                run_id=run.id,
                script_id=script.id,
                work_dir=run_dir / _safe_instance(target.name),
                request_env=dict(request.env),
                dry_run=False,
            )
            return self._builder.build(script, target, context=context)

        def on_output(target: InstanceTarget, stream: str, line: str) -> None:
            self._emit(
                EventType.SCRIPT_RUN_OUTPUT,
                {"run_id": run.id, "instance": target.name, "stream": stream, "line": line},
            )

        results_by_instance: dict[str, InstanceResult] = {}
        results_by_instance.update(failures)
        if targets:
            executed = self._executor.execute(
                targets,
                plan_factory,
                concurrency=request.concurrency,
                timeout_s=timeout_s,
                cancel=control.cancel,
                on_output=on_output,
                on_instance_done=lambda result: self._record_result(run, result),
            )
            for result in executed:
                results_by_instance[result.instance] = result
        merged = [
            results_by_instance.get(name)
            or InstanceResult(instance=name, state=RunState.FAILED, error="instance produced no result")
            for name in run.instances
        ]
        with self._lock:
            run.results = merged
        state = _aggregate_state(merged)
        error = ""
        if state is not RunState.SUCCEEDED:
            messages = [result.error for result in merged if result.error]
            error = messages[0] if messages else f"script run {state.value}"
        self._finish_run(run, state, error=error, control=control)

    def _execute_dry_run(
        self,
        run: ScriptRun,
        script: ScriptDefinition,
        request: ScriptRunRequest,
        control: _RunControl,
    ) -> None:
        run_dir = self._run_dir(run.id)
        planned: dict[str, list[str]] = {}
        results: list[InstanceResult] = []
        for name in run.instances:
            started = utcnow()
            try:
                target = self._resolve_target(name)
                context = ScriptContext(
                    run_id=run.id,
                    script_id=script.id,
                    work_dir=run_dir / _safe_instance(name),
                    request_env=dict(request.env),
                    dry_run=True,
                )
                commands = self._builder.build(script, target, context=context)
                displays = [command.display for command in commands]
                planned[name] = displays
                results.append(
                    InstanceResult(
                        instance=name,
                        state=RunState.SUCCEEDED,
                        exit_code=0,
                        stdout="\n".join(displays),
                        started_at=started,
                        finished_at=utcnow(),
                        duration_s=0.0,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - report per instance
                results.append(
                    InstanceResult(
                        instance=name,
                        state=RunState.FAILED,
                        error=redact_text(str(exc)),
                        started_at=started,
                        finished_at=utcnow(),
                        duration_s=0.0,
                    )
                )
        payload = dict(run.request)
        payload["planned_commands"] = planned
        payload["dry_run"] = True
        with self._lock:
            run.request = payload
            run.results = results
        state = RunState.SUCCEEDED if all(result.state is RunState.SUCCEEDED for result in results) else RunState.FAILED
        self._finish_run(run, state, error="" if state is RunState.SUCCEEDED else "dry-run planning failed", control=control)

    def _record_result(self, run: ScriptRun, result: InstanceResult) -> None:
        with self._lock:
            current = list(run.results)
            for index, existing in enumerate(current):
                if existing.instance == result.instance:
                    current[index] = result
                    break
            else:  # pragma: no cover - every instance is pre-seeded
                current.append(result)
            run.results = current
            self._persist(run)

    def _finish_run(
        self,
        run: ScriptRun,
        state: RunState,
        *,
        error: str = "",
        control: _RunControl | None,
    ) -> ScriptRun:
        lock = control.lock if control is not None else self._lock
        with lock:
            if control is not None:
                if control.finalized:
                    return self._snapshot(run)
                control.finalized = True
            elif run.state.terminal:
                return self._snapshot(run)
            run.state = state
            run.finished_at = utcnow()
            if error:
                run.error = redact_text(error)
            self._persist_final(run)
        self._call_hooks("after_run", run)
        self._emit(
            EventType.SCRIPT_RUN_FINISHED,
            {
                "run_id": run.id,
                "script_id": run.script_id,
                "script_name": run.script_name,
                "state": run.state.value,
                "succeeded": run.succeeded,
                "failed": run.failed,
                "total": len(run.results),
                "dry_run": run.dry_run,
                "error": run.error,
                "results": [
                    {
                        "instance": result.instance,
                        "state": result.state.value,
                        "exit_code": result.exit_code,
                        "duration_s": result.duration_s,
                    }
                    for result in run.results
                ],
            },
        )
        return self._snapshot(run)

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------
    def _persist(self, run: ScriptRun) -> None:
        try:
            self._runs.upsert(run)
        except Exception:  # noqa: BLE001 - execution must survive a storage hiccup
            logger.warning("failed to persist script run %s", run.id, exc_info=True)

    def _persist_final(self, run: ScriptRun) -> None:
        finish = getattr(self._runs, "finish", None)
        if callable(finish):
            try:
                finish(
                    run,
                    state=run.state,
                    results=list(run.results),
                    error=run.error,
                    finished_at=run.finished_at,
                )
                return
            except Exception:  # noqa: BLE001 - fall back to the generic upsert
                logger.warning("run finish() failed for %s; falling back to upsert", run.id, exc_info=True)
        self._persist(run)

    def _redact_request(self, request: ScriptRunRequest) -> dict[str, Any]:
        payload = request.model_dump(mode="json")
        redacted = redact(payload)
        return redacted if isinstance(redacted, dict) else payload

    # ------------------------------------------------------------------
    # Target / path / event helpers
    # ------------------------------------------------------------------
    def _resolve_target(self, name: str) -> InstanceTarget:
        resolver = self._instance_resolver
        if resolver is None:
            return InstanceTarget(name=name, serial=name)
        value = resolver(name)
        if value is None:
            raise NotFoundError(f"instance {name!r} not found")
        if isinstance(value, InstanceTarget):
            return value
        if isinstance(value, AvdInfo):
            return InstanceTarget.from_avd(value)
        if isinstance(value, str):
            return InstanceTarget(name=name, serial=value)
        raise ScriptError(f"instance resolver returned an unsupported value for {name!r}")

    def _run_dir(self, run_id: str) -> Path:
        directory = self._work_dir / run_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _call_hooks(self, name: str, run: ScriptRun) -> None:
        with self._lock:
            hooks = list(self._hooks[name])
        for hook in hooks:
            try:
                hook(run)
            except Exception:  # noqa: BLE001 - hooks must not break a run
                logger.warning("script %s hook failed", name, exc_info=True)

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        safe_payload = redact(payload)
        if not isinstance(safe_payload, dict):  # pragma: no cover - defensive
            safe_payload = payload
        try:
            self._sink.emit(event_type, safe_payload, source=self._source)
        except Exception:  # noqa: BLE001 - telemetry is best effort
            logger.debug("event bus emit failed", exc_info=True)
        with self._lock:
            listeners = list(self._event_listeners)
        for listener in listeners:
            try:
                listener(event_type, safe_payload)
            except Exception:  # noqa: BLE001 - listeners must not break a run
                logger.warning("script event listener failed", exc_info=True)

    @staticmethod
    def _snapshot(run: ScriptRun) -> ScriptRun:
        return run.model_copy(deep=True)


def _safe_instance(name: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "-" for char in name).strip("-._")
    return safe or "instance"


def _aggregate_state(results: Sequence[InstanceResult]) -> RunState:
    states = [result.state for result in results]
    if any(state is RunState.CANCELLED for state in states):
        return RunState.CANCELLED
    if any(state is RunState.FAILED for state in states):
        return RunState.FAILED
    if any(state is RunState.TIMED_OUT for state in states):
        return RunState.TIMED_OUT
    if states and all(state is RunState.SUCCEEDED for state in states):
        return RunState.SUCCEEDED
    return RunState.FAILED
