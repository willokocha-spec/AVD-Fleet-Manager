"""Unit tests for the script process runner and the bounded fan-out executor.

Everything here runs real subprocesses (the current Python interpreter) so the
timeout, cancellation, streaming and aggregation paths are exercised at the same
process boundary the production code uses.  No Android tools are required.
"""

from __future__ import annotations

import sys
import threading
import time

import pytest

from avdfleet.core.errors import ScriptError
from avdfleet.core.models import RunState
from avdfleet.scripts.executor import FanOutExecutor, SubprocessRunner
from avdfleet.scripts.types import InstanceTarget, PlannedCommand

PYTHON = sys.executable


def _targets(count: int = 4) -> list[InstanceTarget]:
    return [
        InstanceTarget(name=f"avd-{index:02d}", serial=f"emulator-{5554 + index * 2}")
        for index in range(count)
    ]


def _python(code: str) -> PlannedCommand:
    return PlannedCommand(argv=(PYTHON, "-u", "-c", code))


def _sleep(seconds: float, text: str = "") -> PlannedCommand:
    return _python(f"import time, sys\ntime.sleep({seconds})\nprint({text!r})\nsys.stdout.flush()")


# ---------------------------------------------------------------------------
# SubprocessRunner
# ---------------------------------------------------------------------------


def test_runner_streams_lines_and_redacts_secrets() -> None:
    runner = SubprocessRunner(poll_interval_s=0.01)
    seen: list[tuple[str, str]] = []
    result = runner.run(
        _python("print('hello fleet')\nprint('token=super-secret')\nprint('done')"),
        timeout_s=30,
        on_output=lambda stream, line: seen.append((stream, line)),
    )
    assert result.exit_code == 0
    assert result.timed_out is False and result.cancelled is False
    assert "hello fleet" in result.stdout
    assert "done" in result.stdout
    assert "super-secret" not in result.stdout
    assert "token=***" in result.stdout
    assert ("stdout", "hello fleet") in seen
    assert not any("super-secret" in line for _, line in seen)


def test_runner_reports_stderr_and_exit_code() -> None:
    runner = SubprocessRunner(poll_interval_s=0.01)
    result = runner.run(
        _python("import sys\nprint('out')\nprint('bad thing', file=sys.stderr)\nsys.exit(3)"),
        timeout_s=30,
    )
    assert result.exit_code == 3
    assert result.error == ""
    assert "out" in result.stdout
    assert "bad thing" in result.stderr


def test_runner_timeout_marks_timed_out() -> None:
    runner = SubprocessRunner(poll_interval_s=0.01, grace_s=0.5)
    started = time.monotonic()
    result = runner.run(_python("import time; time.sleep(30)"), timeout_s=0.4)
    assert result.timed_out is True
    assert result.cancelled is False
    assert result.exit_code is None
    assert "timed out" in result.error
    assert time.monotonic() - started < 10.0


def test_runner_cancel_kills_process() -> None:
    runner = SubprocessRunner(poll_interval_s=0.01, grace_s=0.5)
    cancel = threading.Event()
    timer = threading.Timer(0.4, cancel.set)
    timer.start()
    try:
        started = time.monotonic()
        result = runner.run(_python("import time; time.sleep(30)"), timeout_s=60, cancel=cancel)
    finally:
        timer.cancel()
    assert result.cancelled is True
    assert result.timed_out is False
    assert result.exit_code is None
    assert time.monotonic() - started < 10.0


def test_runner_already_cancelled_returns_without_process() -> None:
    runner = SubprocessRunner()
    cancel = threading.Event()
    cancel.set()
    result = runner.run(_python("print('must not run')"), timeout_s=10, cancel=cancel)
    assert result.cancelled is True
    assert "must not run" not in result.stdout


def test_runner_missing_executable_is_an_error_not_an_exception() -> None:
    runner = SubprocessRunner()
    result = runner.run(PlannedCommand(argv=("avdfleet-definitely-not-a-real-binary",)), timeout_s=5)
    assert result.exit_code is None
    assert "failed to start" in result.error


# ---------------------------------------------------------------------------
# FanOutExecutor
# ---------------------------------------------------------------------------


def test_fan_out_runs_targets_in_parallel_and_keeps_order() -> None:
    targets = _targets(3)

    def factory(target: InstanceTarget) -> list[PlannedCommand]:
        return [_sleep(0.3, target.name)]

    executor = FanOutExecutor()
    started = time.monotonic()
    serial_results = executor.execute(targets, factory, concurrency=1, timeout_s=30)
    serial_elapsed = time.monotonic() - started

    started = time.monotonic()
    parallel_results = executor.execute(targets, factory, concurrency=3, timeout_s=30)
    parallel_elapsed = time.monotonic() - started

    assert serial_elapsed >= 0.8
    assert parallel_elapsed < serial_elapsed
    assert [result.instance for result in parallel_results] == [target.name for target in targets]
    assert all(result.state is RunState.SUCCEEDED for result in parallel_results)
    assert all(result.exit_code == 0 for result in parallel_results)
    assert all(result.duration_s is not None and result.duration_s >= 0.2 for result in parallel_results)
    assert [result.instance for result in serial_results] == [target.name for target in targets]


def test_fan_out_cancel_during_run() -> None:
    targets = _targets(4)
    cancel = threading.Event()
    timer = threading.Timer(0.5, cancel.set)
    timer.start()
    try:
        started = time.monotonic()
        results = FanOutExecutor().execute(
            targets,
            lambda target: [_sleep(30)],
            concurrency=4,
            timeout_s=60,
            cancel=cancel,
        )
    finally:
        timer.cancel()
    assert time.monotonic() - started < 15.0
    assert all(result.state is RunState.CANCELLED for result in results)
    assert all(result.exit_code is None for result in results)


def test_fan_out_per_instance_timeout() -> None:
    targets = _targets(2)
    results = FanOutExecutor().execute(
        targets,
        lambda target: [_sleep(30)],
        concurrency=2,
        timeout_s=0.5,
    )
    assert all(result.state is RunState.TIMED_OUT for result in results)
    assert all(result.exit_code is None for result in results)
    assert all("timed out" in result.error for result in results)


def test_fan_out_isolates_planning_failures() -> None:
    targets = _targets(3)

    def factory(target: InstanceTarget) -> list[PlannedCommand]:
        if target.name == "avd-01":
            raise ScriptError("adb executable not found")
        return [_python(f"print({target.name!r})")]

    results = FanOutExecutor().execute(targets, factory, concurrency=3, timeout_s=30)
    by_name = {result.instance: result for result in results}
    assert by_name["avd-00"].state is RunState.SUCCEEDED
    assert by_name["avd-02"].state is RunState.SUCCEEDED
    assert by_name["avd-01"].state is RunState.FAILED
    assert "adb executable not found" in by_name["avd-01"].error


def test_fan_out_failed_exit_code_is_failed() -> None:
    target = _targets(1)[0]
    results = FanOutExecutor().execute(
        [target],
        lambda _target: [_python("import sys; sys.exit(7)")],
        concurrency=1,
        timeout_s=30,
    )
    assert results[0].state is RunState.FAILED
    assert results[0].exit_code == 7


def test_fan_out_streams_output_per_instance() -> None:
    targets = _targets(2)
    chunks: list[tuple[str, str, str]] = []

    def factory(target: InstanceTarget) -> list[PlannedCommand]:
        return [_python(f"print('line-{target.name}')")]

    results = FanOutExecutor().execute(
        targets,
        factory,
        concurrency=2,
        timeout_s=30,
        on_output=lambda target, stream, line: chunks.append((target.name, stream, line)),
    )
    assert all(result.state is RunState.SUCCEEDED for result in results)
    assert {name for name, _, _ in chunks} == {target.name for target in targets}
    assert all(stream == "stdout" for _, stream, _ in chunks)
    assert f"line-{targets[0].name}" in {line for _, _, line in chunks}


def test_fan_out_completion_callback_fires_per_instance() -> None:
    targets = _targets(3)
    completed: list[str] = []
    results = FanOutExecutor().execute(
        targets,
        lambda target: [_python(f"print({target.name!r})")],
        concurrency=3,
        timeout_s=30,
        on_instance_done=lambda result: completed.append(result.instance),
    )
    assert len(results) == 3
    assert sorted(completed) == sorted(target.name for target in targets)


def test_fan_out_rejects_empty_target_list() -> None:
    assert FanOutExecutor().execute([], lambda _target: []) == []


@pytest.mark.parametrize("concurrency", [1, 2, 64])
def test_fan_out_clamps_worker_count(concurrency: int) -> None:
    targets = _targets(2)
    results = FanOutExecutor().execute(
        targets,
        lambda target: [_python(f"print({target.name!r})")],
        concurrency=concurrency,
        timeout_s=30,
    )
    assert all(result.state is RunState.SUCCEEDED for result in results)
