"""Lifecycle automation: the runtime boot-completed hook is the one trigger.

The contract under test:

* ``start`` queues nothing (it returns before the guest is booted);
* every path that observes boot completion -- the GUI's start-then-wait-boot
  sequence and the CLI's direct ``wait-boot`` -- reaches the same runtime hook;
* the hook queues exactly one run per kernel boot, enforced by the
  ``instrumentation_auto_runs`` composite primary key, so a repeated
  ``wait-boot`` and two concurrent triggers both produce one run;
* a ``dry_run`` start never queues.

The harness is the real :class:`RuntimeManagerService` with scripted processes
and adb, wired to a real :class:`InstrumentationService` whose only fake is the
``adb`` boundary -- no emulator, no real adb, no network.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from avdfleet.api.container import ContainerAdapter
from avdfleet.api.routes.instances import (
    InstanceStartRequest,
    start_instance,
    wait_boot,
)
from avdfleet.instrumentation import (
    DeploymentRun,
    DeviceProber,
    InstrumentationRepository,
    InstrumentationService,
    ModuleLibrary,
    TargetDevice,
)
from avdfleet.store import Store
from tests.qa.qa_support import ScriptedAdb, magisk_script, open_service
from tests.unit.test_runtime import RuntimeHarness

BOOT_SERIALNO = "EMULATOR34X1"
BOOT_ID = "qa-boot-0001"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
class _Container:
    """The two services the boot hook and the instance routes need."""

    def __init__(self, runtime: RuntimeHarness, service: InstrumentationService) -> None:
        self.harness = runtime
        self.service = service

    def runtime(self) -> Any:
        return self.harness.runtime

    @property
    def instrumentation(self) -> InstrumentationService:
        return self.service


def _device(*, boot_id: str = BOOT_ID) -> ScriptedAdb:
    """A Magisk-rooted device that also reports a stable kernel boot identity."""
    script = magisk_script()
    script.responses["getprop ro.boot.serialno"] = (0, f"{BOOT_SERIALNO}\n")
    script.responses["cat /proc/sys/kernel/random/boot_id"] = (0, f"{boot_id}\n")
    return script


def _resolver(name: str) -> TargetDevice:
    return TargetDevice(
        instance=name,
        serial="emulator-5600",
        running=True,
        boot_completed=True,
    )


@contextmanager
def _wired(tmp_path: Path) -> Iterator[tuple[RuntimeHarness, InstrumentationService, Store]]:
    """Real runtime + real instrumentation service, wired exactly as the container does."""
    harness = RuntimeHarness(tmp_path)
    store = Store(":memory:")
    store.initialize()
    service = InstrumentationService(
        InstrumentationRepository(store.database),
        ModuleLibrary(tmp_path / "modules"),
        DeviceProber(runner=_device()),
        _resolver,
        settings=store.settings,
    )
    profile = service.save_profile(
        profile_id=None, name="Boot automation", steps=[{"action": "verify_root"}]
    )
    service.set_auto_profile("avd-01", profile.id)
    harness.runtime.on_boot_completed(service.try_auto_profile)
    try:
        yield harness, service, store
    finally:
        harness.runtime.close()
        store.close()


def _runs(service: InstrumentationService) -> list[DeploymentRun]:
    return service.list_runs()


def _auto_run_count(store: Store) -> int:
    with store.database.connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM instrumentation_auto_runs").fetchone()
    return int(row[0]) if row is not None else 0


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def test_gui_start_then_wait_boot_queues_exactly_one_run(tmp_path: Path) -> None:
    """The GUI starts through ``api.startInstance``; boot completion is the trigger.

    ``start`` must not queue: the emulator process has only just been spawned.
    The subsequent ``wait-boot`` route reaches the runtime hook and queues once.
    """
    with _wired(tmp_path) as (harness, service, store):
        harness.add("avd-01")
        adapter = ContainerAdapter(_Container(harness, service))

        start_instance(adapter, "avd-01", None)
        assert _runs(service) == [], "start queued a profile before the device booted"
        assert _auto_run_count(store) == 0

        wait_boot(adapter, "avd-01", None)

        runs = _runs(service)
        assert len(runs) == 1, f"expected one auto run, got {[run.id for run in runs]}"
        assert runs[0].targets == ["avd-01"]
        assert runs[0].state.value == "queued"
        assert _auto_run_count(store) == 1


def test_cli_wait_boot_without_the_route_queues_exactly_one_run(tmp_path: Path) -> None:
    """``avdfleet instances wait-boot`` calls ``runtime.wait_boot`` directly."""
    with _wired(tmp_path) as (harness, service, store):
        harness.add("avd-01")
        harness.start("avd-01")
        assert _runs(service) == []

        # No route, no ContainerAdapter: the CLI talks to the runtime service.
        harness.runtime.wait_boot("avd-01")

        runs = _runs(service)
        assert len(runs) == 1, f"the CLI path queued {len(runs)} runs"
        assert runs[0].targets == ["avd-01"]
        assert _auto_run_count(store) == 1


def test_a_second_wait_boot_on_the_same_boot_queues_none(tmp_path: Path) -> None:
    """The boot identity -- not the run state -- is what makes this idempotent."""
    with _wired(tmp_path) as (harness, service, store):
        harness.add("avd-01")
        adapter = ContainerAdapter(_Container(harness, service))

        start_instance(adapter, "avd-01", None)
        wait_boot(adapter, "avd-01", None)
        wait_boot(adapter, "avd-01", None)

        assert len(_runs(service)) == 1
        assert _auto_run_count(store) == 1


def test_a_dry_run_start_queues_nothing(tmp_path: Path) -> None:
    """``dry_run`` starts no process, so there is no boot to react to."""
    with _wired(tmp_path) as (harness, service, store):
        harness.add("avd-01")
        adapter = ContainerAdapter(_Container(harness, service))

        start_instance(adapter, "avd-01", InstanceStartRequest(dry_run=True))

        assert _runs(service) == []
        assert _auto_run_count(store) == 0
        assert harness.factory.calls == []


def test_two_concurrent_triggers_produce_one_run(tmp_path: Path) -> None:
    """Both triggers pass the (absent) read check; the database picks the winner."""
    with _wired(tmp_path) as (_harness, service, store):
        barrier = threading.Barrier(2, timeout=10)

        def trigger() -> DeploymentRun | None:
            barrier.wait()
            return service.try_auto_profile("avd-01")

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = [pool.submit(trigger), pool.submit(trigger)]
            runs = [outcome.result() for outcome in outcomes]

        queued = [run for run in runs if run is not None]
        assert len(queued) == 1, f"concurrent triggers queued {len(queued)} runs"
        assert len(_runs(service)) == 1
        assert _auto_run_count(store) == 1


# ---------------------------------------------------------------------------
# Binding lifecycle
# ---------------------------------------------------------------------------
def test_deleting_a_profile_clears_the_auto_profile_binding(tmp_path: Path) -> None:
    """A deleted profile must not leave the binding pointing at a missing id."""
    with open_service(tmp_path, magisk_script()) as service:
        profile = service.save_profile(
            profile_id=None, name="Bound then deleted", steps=[{"action": "verify_root"}]
        )
        service.set_auto_profile("avd-01", profile.id)
        assert service.get_auto_profile("avd-01")["profile_id"] == profile.id

        service.delete_profile(profile.id)

        binding = service.get_auto_profile("avd-01")

    assert binding["profile_id"] == "", (
        "the deleted profile is still bound as the start-time automation: "
        f"{binding['profile_id']!r}"
    )
