"""Background supervisor for externally-exiting emulator processes.

``RuntimeManager.start`` owns the :class:`subprocess.Popen`-like handle but the
daemon must notice a crash (or a user killing the emulator) even when nobody is
polling the API.  :class:`ProcessSupervisor` runs a small daemon thread that
polls the registered handles and calls a callback for each exit, optionally
restarting with backoff.

The supervisor has no knowledge of AVDs, ports or the store: it receives a
provider callable returning the currently supervised processes.  That keeps it
test level unit-testable (``run_once`` is a pure synchronous tick) and lets the
runtime manager rebuild the provider list on every tick.  ``clock``/``sleep`` are
injectable for deterministic tests; production uses the real ``time`` module.

Lifecycle rules:

* :meth:`start` is idempotent; a stopped supervisor can be started again;
* :meth:`stop` sets the stop event, wakes the loop (the loop waits on the event
  rather than sleeping blindly) and joins the thread;
* a callback exception is logged and never kills the thread;
* restart attempts are counted per instance name and reset when the manager
  re-registers the process (``reset_attempts``).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from avdfleet.core.logging import get_logger, redact

logger = get_logger("platform.runtime.supervisor")


@dataclass
class SupervisedProcess:
    """One live process handle the supervisor should watch."""

    name: str
    poll: Callable[[], int | None]
    on_exit: Callable[[int | None], None]
    label: str = ""
    expected: bool = False


@dataclass
class RestartPolicy:
    """Restart policy applied after an *unexpected* exit."""

    auto_restart: bool = False
    max_attempts: int = 3
    backoff_s: float = 0.0

    def allowed(self, attempts: int) -> bool:
        return self.auto_restart and attempts < max(1, int(self.max_attempts))


@dataclass
class ExitRecord:
    """One observed process exit."""

    name: str
    exit_code: int | None
    expected: bool = False
    at: float = 0.0
    restart_scheduled: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "exit_code": self.exit_code,
            "expected": self.expected,
            "at": self.at,
            "restart_scheduled": self.restart_scheduled,
        }


class ProcessSupervisor:
    """Watch emulator processes in a background daemon thread."""

    def __init__(
        self,
        processes_provider: Callable[[], Sequence[SupervisedProcess]],
        *,
        interval_s: float = 2.0,
        restart_policy: RestartPolicy | None = None,
        restart: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        on_exit: Callable[[ExitRecord], None] | None = None,
        name: str = "avd-process-supervisor",
    ) -> None:
        self._provider = processes_provider
        self.interval_s = max(0.01, float(interval_s))
        self.restart_policy = restart_policy or RestartPolicy()
        self._restart = restart
        self._clock = clock
        self._sleep = sleep
        self._on_exit = on_exit
        self.name = name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._ticks = 0
        self._exits = 0
        self._attempts: dict[str, int] = {}
        self._history: list[ExitRecord] = []
        self._last_error = ""

    # -- lifecycle ------------------------------------------------------
    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """Start the background loop (idempotent)."""
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            thread = threading.Thread(
                target=self._loop,
                name=self.name,
                daemon=True,
            )
            self._thread = thread
            thread.start()
        logger.debug("%s started (interval %.2fs)", self.name, self.interval_s)

    def stop(self, *, timeout_s: float = 10.0) -> None:
        """Stop the loop and join the thread (idempotent, clean shutdown)."""
        with self._lock:
            thread = self._thread
            self._thread = None
        self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout_s)))
            if thread.is_alive():  # pragma: no cover - only if a callback hangs
                logger.warning("%s did not stop within %.1fs", self.name, timeout_s)
        logger.debug("%s stopped", self.name)

    def _loop(self) -> None:
        # Event.wait doubles as a sleep that stop() can interrupt immediately.
        while not self._stop.wait(self.interval_s):
            try:
                self.run_once()
            except Exception:  # pragma: no cover - loop must survive callbacks
                logger.exception("%s tick failed", self.name)

    # -- one tick -------------------------------------------------------
    def run_once(self) -> list[ExitRecord]:
        """Check every registered process once; return the exits observed."""
        observed: list[ExitRecord] = []
        try:
            processes = list(self._provider() or [])
        except Exception as exc:  # noqa: BLE001 - provider must not kill the loop
            self._last_error = str(exc)
            logger.warning("%s process provider failed: %s", self.name, redact(str(exc)))
            return observed
        for process in processes:
            try:
                code = process.poll()
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                logger.warning(
                    "%s poll failed for %s: %s", self.name, process.name, redact(str(exc))
                )
                continue
            if code is None:
                continue
            record = self._handle_exit(process, code)
            observed.append(record)
        self._ticks += 1
        return observed

    def _handle_exit(self, process: SupervisedProcess, code: int | None) -> ExitRecord:
        record = ExitRecord(
            name=process.name,
            exit_code=code,
            expected=bool(getattr(process, "expected", False)),
            at=self._clock(),
        )
        try:
            process.on_exit(code)
        except Exception:  # noqa: BLE001 - one bad instance must not stop the loop
            logger.exception("%s exit callback failed for %s", self.name, process.name)
        if self._restart is not None:
            attempts = self._attempts.get(process.name, 0)
            if self.restart_policy.allowed(attempts):
                self._attempts[process.name] = attempts + 1
                record.restart_scheduled = True
                if self.restart_policy.backoff_s > 0:
                    self._sleep(self.restart_policy.backoff_s)
                try:
                    self._restart(process.name)
                except Exception as exc:  # noqa: BLE001
                    self._last_error = str(exc)
                    logger.warning(
                        "%s restart failed for %s: %s",
                        self.name,
                        process.name,
                        redact(str(exc)),
                    )
        self._exits += 1
        with self._lock:
            self._history.append(record)
            if len(self._history) > 200:
                del self._history[: len(self._history) - 200]
        if self._on_exit is not None:
            try:
                self._on_exit(record)
            except Exception:  # noqa: BLE001
                logger.exception("%s on_exit callback failed", self.name)
        logger.info(
            "supervisor observed exit of %s (code=%s, anticipated=%s, restart=%s)",
            process.name,
            code,
            record.expected,
            record.restart_scheduled,
        )
        return record

    # -- bookkeeping ----------------------------------------------------
    def reset_attempts(self, name: str) -> None:
        """Forget restart attempts for *name* (call when it is re-registered)."""
        with self._lock:
            self._attempts.pop(name, None)

    def attempts(self, name: str) -> int:
        return self._attempts.get(name, 0)

    def history(self) -> list[ExitRecord]:
        with self._lock:
            return list(self._history)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "ticks": self._ticks,
            "exits": self._exits,
            "interval_s": self.interval_s,
            "watched": len(self._safe_watch_count()),
            "last_error": redact(self._last_error),
        }

    def _safe_watch_count(self) -> list[Any]:
        try:
            return list(self._provider() or [])
        except Exception:  # noqa: BLE001
            return []


__all__ = [
    "ExitRecord",
    "ProcessSupervisor",
    "RestartPolicy",
    "SupervisedProcess",
]
