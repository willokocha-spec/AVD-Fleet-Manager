"""Regression tests for ``RuntimeManagerService.stop`` process verification.

The emulator is a two-process tree on Windows: ``emulator.exe`` (the process the
runtime records) launches ``qemu-system-*.exe``.  ``stop`` used to report
``stopped`` as soon as the recorded launcher exited, so a force stop (or a failed
console kill) orphaned qemu while the API said the instance was stopped.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import psutil
import pytest
from tests.unit.test_runtime import RuntimeHarness

from avdfleet.core.errors import InstanceError
from avdfleet.core.models import AvdInfo, InstancePorts, InstanceState

WINDOWS = os.name == "nt"
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if WINDOWS else 0


def _spawn_sleeper(*extra_args: str) -> subprocess.Popen[bytes]:
    """Start a real, long-running process (optionally with AVD-identifying argv)."""
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if WINDOWS:
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", *extra_args],
        **kwargs,  # type: ignore[arg-type]
    )


def _terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive cleanup
        pass


def _wait_pidfile(path: Path, timeout_s: float = 10.0) -> int:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        time.sleep(0.02)
    raise AssertionError(f"helper did not write its child pid to {path}")


class _ProcessTreeFactory:
    """Launch a real parent/child pair shaped like emulator.exe -> qemu."""

    def __init__(self, pidfile: Path, avd_name: str = "avd-01") -> None:
        self.pidfile = pidfile
        self.avd_name = avd_name
        self.process: subprocess.Popen[bytes] | None = None

    def __call__(
        self,
        argv: object,
        *,
        env: object,
        cwd: object = None,
        log_path: object = None,
    ) -> subprocess.Popen[bytes]:
        script = f"""
import subprocess, sys, time
kwargs = {{
    "stdin": subprocess.DEVNULL,
    "stdout": subprocess.DEVNULL,
    "stderr": subprocess.DEVNULL,
}}
if sys.platform == "win32":
    kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
else:
    kwargs["start_new_session"] = True
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)", "-avd", {self.avd_name!r}],
    **kwargs,
)
with open({str(self.pidfile)!r}, "w", encoding="utf-8") as handle:
    handle.write(str(child.pid))
time.sleep(60)
"""
        parent_kwargs: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if WINDOWS:
            parent_kwargs["creationflags"] = _CREATE_NO_WINDOW
        else:
            parent_kwargs["start_new_session"] = True
        self.process = subprocess.Popen(
            [sys.executable, "-c", script, "-avd", self.avd_name],
            **parent_kwargs,  # type: ignore[arg-type]
        )
        return self.process


@pytest.fixture()
def harness(tmp_path: Path) -> RuntimeHarness:
    return RuntimeHarness(tmp_path)


def test_stop_never_reports_stopped_while_recorded_process_lives(
    harness: RuntimeHarness,
) -> None:
    harness.add()
    harness.start()
    process = harness.factory.processes[0]
    process.exit_on_terminate = False
    process.exit_on_kill = False

    with pytest.raises(InstanceError, match="could not stop"):
        harness.runtime.stop("avd-01", force=True, timeout_s=0.2)

    assert process.poll() is None, "stop returned even though the process never exited"
    current = harness.runtime.status("avd-01")
    assert current.state is InstanceState.ERROR
    assert current.pid == process.pid

    process.exit(0)
    stopped = harness.runtime.stop("avd-01", force=True, timeout_s=0.2)
    assert stopped.state is InstanceState.STOPPED
    assert stopped.pid is None


def test_stop_does_not_touch_a_recycled_pid(harness: RuntimeHarness) -> None:
    unrelated = _spawn_sleeper()
    try:
        harness.add()
        harness.runtime._state_store.save(
            AvdInfo(
                name="avd-01",
                state=InstanceState.RUNNING,
                pid=unrelated.pid,
                ports=InstancePorts(console=5600, adb=5601),
                # A recorded start time that cannot match the recycled process.
                started_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )

        info = harness.runtime.stop("avd-01", timeout_s=0.1)

        assert info.state is InstanceState.STOPPED
        assert info.pid is None
        assert unrelated.poll() is None, "a recycled pid must never be signalled"
        assert harness.runtime.status("avd-01").state is InstanceState.STOPPED
    finally:
        _terminate(unrelated)


def test_stop_identifies_external_process_by_avd_command_line(
    harness: RuntimeHarness,
) -> None:
    external = _spawn_sleeper("-avd", "avd-01")
    try:
        harness.add()
        # Real waits: the external path bounds its own grace period.
        harness.runtime._clock = time.monotonic
        harness.runtime._sleep = time.sleep
        harness.runtime._state_store.save(
            AvdInfo(
                name="avd-01",
                state=InstanceState.RUNNING,
                pid=external.pid,
                ports=InstancePorts(console=5600, adb=5601),
            )
        )

        info = harness.runtime.stop("avd-01", timeout_s=0.2, force=True)

        assert info.state is InstanceState.STOPPED
        assert info.pid is None
        assert external.wait(timeout=5) is not None
    finally:
        _terminate(external)


def test_stop_stops_name_matched_worker_without_runtime_state(
    harness: RuntimeHarness,
) -> None:
    """A daemon with no record of the instance must not claim it is stopped."""
    worker = _spawn_sleeper("-avd", "avd-01")
    try:
        harness.add()
        harness.runtime._clock = time.monotonic
        harness.runtime._sleep = time.sleep

        info = harness.runtime.stop("avd-01", timeout_s=0.2, force=True)

        assert info.state is InstanceState.STOPPED
        assert worker.wait(timeout=5) is not None
    finally:
        _terminate(worker)


def test_stop_clears_stale_pid_but_stops_name_matched_worker(
    harness: RuntimeHarness,
) -> None:
    unrelated = _spawn_sleeper()
    worker = _spawn_sleeper("-avd", "avd-01")
    try:
        harness.add()
        harness.runtime._clock = time.monotonic
        harness.runtime._sleep = time.sleep
        harness.runtime._state_store.save(
            AvdInfo(
                name="avd-01",
                state=InstanceState.RUNNING,
                pid=unrelated.pid,
                ports=InstancePorts(console=5600, adb=5601),
                started_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )

        info = harness.runtime.stop("avd-01", timeout_s=0.2, force=True)

        assert info.state is InstanceState.STOPPED
        assert unrelated.poll() is None, "the stale pid must not be signalled"
        assert worker.wait(timeout=5) is not None
    finally:
        _terminate(unrelated)
        _terminate(worker)


def test_stop_kills_qemu_like_child_before_reporting_stopped(
    tmp_path: Path,
) -> None:
    pidfile = tmp_path / "child.pid"
    factory = _ProcessTreeFactory(pidfile)
    harness = RuntimeHarness(tmp_path, process_factory=factory)  # type: ignore[arg-type]
    child_pid: int | None = None
    parent = None
    try:
        harness.add()
        harness.start()
        parent = factory.process
        child_pid = _wait_pidfile(pidfile)
        assert parent is not None and parent.poll() is None

        # The fake clock would race the real OS here; use real time for stop.
        harness.runtime._clock = time.monotonic
        harness.runtime._sleep = time.sleep

        info = harness.runtime.stop("avd-01", force=True, timeout_s=1.0)

        assert info.state is InstanceState.STOPPED
        assert parent.poll() is not None

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                child_alive = psutil.Process(child_pid).is_running()
            except psutil.NoSuchProcess:
                child_alive = False
            if not child_alive:
                break
            time.sleep(0.02)
        assert not child_alive, "stop reported stopped while the qemu-like child was alive"
    finally:
        if child_pid is not None:
            try:
                psutil.Process(child_pid).kill()
            except psutil.NoSuchProcess:
                pass
        _terminate(parent)
