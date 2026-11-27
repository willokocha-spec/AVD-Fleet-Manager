"""Process execution and bounded fan-out for the script subsystem.

Two layers live here:

* :class:`SubprocessRunner` executes exactly one :class:`PlannedCommand` with a
  per-command timeout, cooperative cancellation and line-oriented output
  streaming.  Every line is redacted before it is stored or emitted.
* :class:`FanOutExecutor` runs one plan factory per target on a bounded
  ``ThreadPoolExecutor`` (1..64 workers), propagates cancellation via a
  thread-safe :class:`threading.Event`, applies a per-instance timeout and
  aggregates :class:`avdfleet.core.models.InstanceResult` records in request
  order.

Nothing in this module imports another avdfleet service: callers inject the
command runner so the executor can be tested with fake processes.
"""

from __future__ import annotations

import contextlib
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..core.logging import get_logger, redact_text
from ..core.models import InstanceResult, RunState, utcnow
from .types import CommandResult, CommandRunner, InstanceTarget, PlannedCommand

try:  # psutil is a declared dependency; keep a stdlib fallback for odd runtimes.
    import psutil as _psutil
except ImportError:  # pragma: no cover - only on a broken install
    _psutil = None  # type: ignore[assignment]

logger = get_logger("scripts.executor")

__all__ = ["FanOutExecutor", "SubprocessRunner", "terminate_process"]


def terminate_process(process: subprocess.Popen[str], *, grace_s: float = 2.0) -> None:
    """Terminate *process* and its children, escalating to kill after ``grace_s``."""
    if process.poll() is not None:
        return
    if _psutil is not None:
        try:
            parent = _psutil.Process(process.pid)
            children = parent.children(recursive=True)
            for child in children:
                with contextlib.suppress(_psutil.Error):
                    child.terminate()
            with contextlib.suppress(_psutil.Error):
                parent.terminate()
            _, alive = _psutil.wait_procs([parent, *children], timeout=grace_s)
            for remaining in alive:
                with contextlib.suppress(_psutil.Error):
                    remaining.kill()
            return
        except _psutil.Error:  # pragma: no cover - process vanished concurrently
            pass
    process.terminate()
    try:
        process.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        process.kill()


class SubprocessRunner:
    """Default :class:`CommandRunner` backed by :class:`subprocess.Popen`.

    ``on_output`` receives ``(stream, line)`` callbacks from two reader threads;
    callback failures never break the run.  Output is redacted with
    :func:`avdfleet.core.logging.redact_text` before it is reported.
    """

    def __init__(
        self,
        *,
        grace_s: float = 2.0,
        poll_interval_s: float = 0.02,
        max_output_chars: int = 1_000_000,
        join_timeout_s: float = 5.0,
    ) -> None:
        self._grace_s = grace_s
        self._poll_interval_s = poll_interval_s
        self._max_output_chars = max_output_chars
        self._join_timeout_s = join_timeout_s

    def run(
        self,
        command: PlannedCommand,
        *,
        timeout_s: float | None,
        cancel: threading.Event | None = None,
        on_output: Callable[[str, str], None] | None = None,
    ) -> CommandResult:
        started_at = utcnow()
        started_clock = time.monotonic()
        if cancel is not None and cancel.is_set():
            return CommandResult(
                cancelled=True,
                error="cancelled before start",
                started_at=started_at,
                finished_at=utcnow(),
            )

        args = command.argv[0] if command.shell else list(command.argv)
        try:
            process = subprocess.Popen(
                args,
                shell=command.shell,
                cwd=command.cwd,
                env=command.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            message = redact_text(f"failed to start {command.argv[0]!r}: {exc}")
            return CommandResult(
                error=message,
                started_at=started_at,
                finished_at=utcnow(),
                duration_s=time.monotonic() - started_clock,
            )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        truncated = {"reported": False}

        def _pump(stream, stream_name: str, sink: list[str], counter: list[int]) -> None:
            try:
                for raw_line in iter(stream.readline, ""):
                    safe_line = redact_text(raw_line.rstrip("\r\n"))
                    if not truncated["reported"] and counter[0] + len(safe_line) > self._max_output_chars:
                        sink.append("... output truncated ...")
                        truncated["reported"] = True
                    if not truncated["reported"]:
                        sink.append(safe_line)
                        counter[0] += len(safe_line)
                    if on_output is not None:
                        try:
                            on_output(stream_name, safe_line)
                        except Exception:  # noqa: BLE001 - callbacks must not kill the run
                            logger.debug("script output callback failed", exc_info=True)
            finally:
                with contextlib.suppress(Exception):
                    stream.close()

        readers = [
            threading.Thread(
                target=_pump,
                args=(process.stdout, "stdout", stdout_lines, [0]),
                name="avdfleet-script-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=_pump,
                args=(process.stderr, "stderr", stderr_lines, [0]),
                name="avdfleet-script-stderr",
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()

        cancelled = False
        timed_out = False
        deadline = None if timeout_s is None else started_clock + float(timeout_s)
        while True:
            if process.poll() is not None:
                break
            if cancel is not None and cancel.is_set():
                cancelled = True
                terminate_process(process, grace_s=self._grace_s)
                self._reap(process)
                break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                terminate_process(process, grace_s=self._grace_s)
                self._reap(process)
                break
            time.sleep(self._poll_interval_s)

        for reader in readers:
            reader.join(timeout=self._join_timeout_s)
        with contextlib.suppress(Exception):
            process.wait(timeout=self._grace_s)

        exit_code = None if cancelled or timed_out else process.returncode
        error = ""
        if timed_out:
            error = redact_text(f"timed out after {float(timeout_s or 0.0):g}s")
        elif cancelled:
            error = "cancelled"
        finished_at = utcnow()
        return CommandResult(
            exit_code=exit_code,
            stdout="\n".join(stdout_lines),
            stderr="\n".join(stderr_lines),
            timed_out=timed_out,
            cancelled=cancelled,
            error=error,
            duration_s=time.monotonic() - started_clock,
            started_at=started_at,
            finished_at=finished_at,
        )

    def _reap(self, process: subprocess.Popen[str]) -> None:
        try:
            process.wait(timeout=self._grace_s)
        except subprocess.TimeoutExpired:  # pragma: no cover - terminate escalated
            with contextlib.suppress(Exception):
                process.kill()
            with contextlib.suppress(Exception):
                process.wait(timeout=self._grace_s)


class FanOutExecutor:
    """Runs a plan factory for every instance with bounded concurrency."""

    def __init__(self, runner: CommandRunner | None = None, *, max_workers: int = 64) -> None:
        self._runner = runner or SubprocessRunner()
        self._max_workers = max(1, min(int(max_workers), 64))

    def execute(
        self,
        targets: Sequence[InstanceTarget],
        plan_factory: Callable[[InstanceTarget], Sequence[PlannedCommand]],
        *,
        concurrency: int = 5,
        timeout_s: float | None = None,
        cancel: threading.Event | None = None,
        on_output: Callable[[InstanceTarget, str, str], None] | None = None,
        on_instance_done: Callable[[InstanceResult], None] | None = None,
    ) -> list[InstanceResult]:
        """Execute *targets* and return results in the same order as the input."""
        ordered = list(targets)
        if not ordered:
            return []
        cancel_event = cancel or threading.Event()
        workers = max(1, min(int(concurrency), len(ordered), self._max_workers))
        results: dict[int, InstanceResult] = {}

        def _work(index: int, target: InstanceTarget) -> InstanceResult:
            result = InstanceResult(instance=target.name, state=RunState.RUNNING, started_at=utcnow())
            if cancel_event.is_set():
                result.state = RunState.CANCELLED
                result.error = "cancelled before start"
                result.finished_at = utcnow()
                result.duration_s = 0.0
                return result

            started_clock = time.monotonic()
            stdout_parts: list[str] = []
            stderr_parts: list[str] = []
            exit_code: int | None = 0
            error = ""
            cancelled = False
            timed_out = False
            try:
                commands = list(plan_factory(target))
            except Exception as exc:  # noqa: BLE001 - reported per instance
                result.state = RunState.FAILED
                result.error = redact_text(f"failed to plan commands: {exc}")
                result.finished_at = utcnow()
                result.duration_s = time.monotonic() - started_clock
                return result
            if not commands:
                result.state = RunState.FAILED
                result.error = "no commands planned for instance"
                result.finished_at = utcnow()
                result.duration_s = time.monotonic() - started_clock
                return result

            for command in commands:
                if cancel_event.is_set():
                    cancelled = True
                    break
                remaining: float | None = None
                if timeout_s is not None:
                    remaining = float(timeout_s) - (time.monotonic() - started_clock)
                    if remaining <= 0:
                        timed_out = True
                        break

                def _forward(stream: str, line: str) -> None:
                    if on_output is None:
                        return
                    try:
                        on_output(target, stream, line)
                    except Exception:  # noqa: BLE001 - streaming must never break execution
                        logger.debug("fan-out output callback failed", exc_info=True)

                command_result = self._runner.run(
                    command,
                    timeout_s=remaining,
                    cancel=cancel_event,
                    on_output=_forward,
                )
                stdout_parts.append(command_result.stdout)
                stderr_parts.append(command_result.stderr)
                if command_result.cancelled:
                    cancelled = True
                elif command_result.timed_out:
                    timed_out = True
                elif command_result.error:
                    error = command_result.error
                    exit_code = command_result.exit_code
                if command_result.exit_code is not None:
                    exit_code = command_result.exit_code
                if cancelled or timed_out or command_result.error or exit_code not in (0, None):
                    break

            result.stdout = "\n".join(part for part in stdout_parts if part)
            result.stderr = "\n".join(part for part in stderr_parts if part)
            if cancelled:
                result.state = RunState.CANCELLED
                result.error = "cancelled"
                result.exit_code = None
            elif timed_out:
                result.state = RunState.TIMED_OUT
                result.error = error or redact_text(f"timed out after {float(timeout_s or 0.0):g}s")
                result.exit_code = None
            elif error or exit_code not in (0, None):
                result.state = RunState.FAILED
                result.error = error or redact_text(f"process exited with code {exit_code}")
                result.exit_code = exit_code
            else:
                result.state = RunState.SUCCEEDED
                result.exit_code = exit_code
            result.finished_at = utcnow()
            result.duration_s = time.monotonic() - started_clock
            return result

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="avdfleet-script") as pool:
            future_index = {
                pool.submit(_work, index, target): index for index, target in enumerate(ordered)
            }
            for future in as_completed(future_index):
                index = future_index[future]
                try:
                    result = future.result()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.exception("script worker crashed")
                    result = InstanceResult(
                        instance=ordered[index].name,
                        state=RunState.FAILED,
                        error=redact_text(str(exc)),
                        finished_at=utcnow(),
                    )
                results[index] = result
                if on_instance_done is not None:
                    try:
                        on_instance_done(result)
                    except Exception:  # noqa: BLE001 - persistence callbacks are best effort
                        logger.debug("fan-out completion callback failed", exc_info=True)
        return [results[index] for index in range(len(ordered))]
