"""Background boot observation armed by ``RuntimeManagerService.start``.

``_notify_boot_completed`` used to be reachable only from ``wait_boot``, so a
bound instrumentation profile was applied only when something explicitly waited:
the GUI calls ``api.startInstance`` and never waits, and
``avdfleet instances start`` returns before the guest boots.  ``start`` now arms
a small background observer that reuses the runtime's boot-wait logic and fires
the hook once.

The fake device here reports "not booted" until the test releases it, so the
ordering (start -> watcher probed -> boot reported -> hook) is deterministic
rather than a race against the default immediate-boot fake.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from tests.qa.qa_support import magisk_script
from tests.unit.test_runtime import FakeAdb, RuntimeHarness

from avdfleet.instrumentation import (
    DeviceProber,
    InstrumentationRepository,
    InstrumentationService,
    ModuleLibrary,
    TargetDevice,
)
from avdfleet.platform.runtime.boot import AdbResult
from avdfleet.store import Store

WATCHER_PREFIX = "avdfleet-boot-"


class GatedBootAdb(FakeAdb):
    """``FakeAdb`` whose boot marker is released from the test thread."""

    def __init__(self) -> None:
        super().__init__()
        self.booted = threading.Event()
        self.probe_started = threading.Event()

    def shell(
        self, serial: str, command: str, *, timeout_s: float = 30.0
    ) -> AdbResult:
        if "getprop" in command:
            self.shell_calls.append((serial, command))
            self.getprop_calls += 1
            self.probe_started.set()
            # Pace the poll loop and let the watcher notice a cancellation
            # without burning a core while the device is still booting.
            self.booted.wait(timeout=0.02)
            return AdbResult(0, "1" if self.booted.is_set() else "0")
        return super().shell(serial, command, timeout_s=timeout_s)


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _watcher_thread(name: str = "avd-01") -> threading.Thread | None:
    for thread in threading.enumerate():
        if thread.name == f"{WATCHER_PREFIX}{name}":
            return thread
    return None


def _harness(tmp_path: Path, adb: GatedBootAdb) -> RuntimeHarness:
    harness = RuntimeHarness(tmp_path, adb=adb)
    harness.add("avd-01")
    return harness


def _recording_hook() -> tuple[list[str], threading.Event, Callable[[str], None]]:
    calls: list[str] = []
    fired = threading.Event()

    def hook(name: str) -> None:
        calls.append(name)
        fired.set()

    return calls, fired, hook


def _wire_auto_profile(
    harness: RuntimeHarness, tmp_path: Path
) -> tuple[Store, InstrumentationService]:
    """Bind a profile and reach it through the real runtime hook, as the daemon does."""
    store = Store(":memory:")
    store.initialize()
    device = magisk_script()
    device.responses["getprop ro.boot.serialno"] = (0, "EMULATOR34X1\n")
    device.responses["cat /proc/sys/kernel/random/boot_id"] = (0, "unit-boot-0001\n")
    service = InstrumentationService(
        InstrumentationRepository(store.database),
        ModuleLibrary(tmp_path / "modules"),
        DeviceProber(runner=device),
        lambda name: TargetDevice(
            instance=name, serial="emulator-5600", running=True, boot_completed=True
        ),
        settings=store.settings,
    )
    profile = service.save_profile(
        profile_id=None, name="Boot automation", steps=[{"action": "verify_root"}]
    )
    service.set_auto_profile("avd-01", profile.id)
    harness.runtime.on_boot_completed(service.try_auto_profile)
    return store, service


# ---------------------------------------------------------------------------
# The hook fires from a start-only call
# ---------------------------------------------------------------------------
def test_start_without_wait_boot_fires_the_hook_once(tmp_path: Path) -> None:
    adb = GatedBootAdb()
    harness = _harness(tmp_path, adb)
    calls, fired, hook = _recording_hook()
    harness.runtime.on_boot_completed(hook)
    try:
        harness.start("avd-01")

        assert adb.probe_started.wait(timeout=5.0), "start() never armed a boot observer"
        assert calls == [], "the hook fired before the device reported boot completion"

        adb.booted.set()

        assert fired.wait(timeout=5.0), "the observer never reported boot completion"
        assert _wait_until(lambda: calls == ["avd-01"])
        # Exactly one notification for this boot: the observer stops after it.
        time.sleep(0.2)
        assert calls == ["avd-01"]
        assert _wait_until(lambda: _watcher_thread() is None), "the observer thread leaked"
        assert harness.runtime.status("avd-01").boot_completed is True
    finally:
        harness.runtime.close()


# ---------------------------------------------------------------------------
# The instrumentation claim absorbs the wait_boot duplicate
# ---------------------------------------------------------------------------
def test_start_then_wait_boot_reaches_the_hook_but_queues_one_auto_run(
    tmp_path: Path,
) -> None:
    adb = GatedBootAdb()
    harness = _harness(tmp_path, adb)
    store, service = _wire_auto_profile(harness, tmp_path)
    calls: list[str] = []
    harness.runtime.on_boot_completed(lambda name: calls.append(name))
    try:
        harness.start("avd-01")
        assert adb.probe_started.wait(timeout=5.0)

        adb.booted.set()

        # The observer reaches the hook and queues the bound profile on its own.
        assert _wait_until(
            lambda: calls == ["avd-01"] and len(service.list_runs()) == 1
        ), "a start-only boot never applied the bound profile"

        # wait_boot is also a boot observation.  The runtime may reach the hook
        # again; the once-per-boot claim in instrumentation_auto_runs is what
        # keeps the second trigger from creating a second run.
        harness.runtime.wait_boot("avd-01", timeout_s=5.0)
        assert calls == ["avd-01", "avd-01"]
        assert len(service.list_runs()) == 1
        with store.database.connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM instrumentation_auto_runs").fetchone()
        assert row is not None and int(row[0]) == 1
    finally:
        harness.runtime.close()
        store.close()


# ---------------------------------------------------------------------------
# No watcher for a plan, and none survives a stop or a daemon shutdown
# ---------------------------------------------------------------------------
def test_dry_run_start_never_arms_a_boot_observer(tmp_path: Path) -> None:
    adb = GatedBootAdb()
    harness = _harness(tmp_path, adb)
    calls, _fired, hook = _recording_hook()
    harness.runtime.on_boot_completed(hook)
    info = harness.start("avd-01", dry_run=True)

    assert info.state.value == "stopped"
    assert harness.factory.calls == []
    time.sleep(0.2)
    assert calls == []
    assert adb.probe_started.is_set() is False
    assert _watcher_thread() is None


def test_stop_during_the_boot_wait_never_fires_the_hook(tmp_path: Path) -> None:
    adb = GatedBootAdb()
    harness = _harness(tmp_path, adb)
    calls, fired, hook = _recording_hook()
    harness.runtime.on_boot_completed(hook)
    try:
        harness.start("avd-01")
        assert adb.probe_started.wait(timeout=5.0)
        assert calls == []

        stopped = harness.runtime.stop("avd-01", force=True)
        assert stopped.state.value == "stopped"

        # A boot report that arrives after the stop must not resurrect the hook.
        adb.booted.set()
        assert not fired.wait(timeout=0.5)
        assert calls == []
        assert _wait_until(lambda: _watcher_thread() is None), "the observer outlived stop()"
    finally:
        harness.runtime.close()


def test_close_stops_the_boot_observer(tmp_path: Path) -> None:
    adb = GatedBootAdb()
    harness = _harness(tmp_path, adb)
    calls, fired, hook = _recording_hook()
    harness.runtime.on_boot_completed(hook)
    try:
        harness.start("avd-01")
        assert adb.probe_started.wait(timeout=5.0)

        harness.runtime.close()

        assert _wait_until(lambda: _watcher_thread() is None), (
            "close() left the boot observer running"
        )
        adb.booted.set()
        assert not fired.wait(timeout=0.5)
        assert calls == []
    finally:
        harness.runtime.close()

