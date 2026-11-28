"""Lifecycle automation defects at the ``instances start`` / ``wait-boot`` hook.

The hook is best-effort by design (a failure must not break instance start), but
it currently fires with no regard for the request: it queues a deployment on
every ``wait-boot`` call and even on ``dry_run`` starts. In addition, deleting a
profile leaves the settings-backed binding dangling, so the automation silently
stops with nothing in the UI to show why.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from avdfleet.api.container import ContainerAdapter
from avdfleet.api.routes.instances import (
    InstanceStartRequest,
    start_instance,
    wait_boot,
)
from avdfleet.core.models import AvdInfo, InstanceState
from avdfleet.instrumentation import DeploymentRun
from tests.qa.qa_support import magisk_script, open_service


class _Runtime:
    """Minimal runtime: start/wait-boot succeed and report a running instance."""

    def __init__(self) -> None:
        self.start_calls: list[dict[str, Any]] = []
        self.wait_boot_calls: list[str] = []

    def start(
        self,
        name: str,
        *,
        proxy_id: str | None = None,
        headless: bool | None = None,
        dry_run: bool = False,
    ) -> AvdInfo:
        self.start_calls.append(
            {"name": name, "proxy_id": proxy_id, "headless": headless, "dry_run": dry_run}
        )
        return AvdInfo(name=name, state=InstanceState.RUNNING)

    def wait_boot(self, name: str, *, timeout_s: float | None = None) -> AvdInfo:
        self.wait_boot_calls.append(name)
        return AvdInfo(name=name, state=InstanceState.RUNNING, boot_completed=True)


class _Hook:
    """Records what the route hook asks the instrumentation service to do."""

    def __init__(self) -> None:
        self.queued: list[str] = []
        self.executed: list[str] = []

    def try_auto_profile(self, instance: str) -> DeploymentRun | None:
        self.queued.append(instance)
        return DeploymentRun(
            id=f"irun_{len(self.queued)}", profile_id="prof_auto", targets=[instance]
        )

    def execute_run(self, run_id: str) -> DeploymentRun:
        self.executed.append(run_id)
        return DeploymentRun(id=run_id, profile_id="prof_auto")


class _Container:
    def __init__(self) -> None:
        self.runtime_service = _Runtime()
        self.hook = _Hook()

    def runtime(self) -> _Runtime:
        return self.runtime_service

    @property
    def instrumentation(self) -> _Hook:
        return self.hook


def test_start_then_wait_boot_queues_the_auto_profile_twice() -> None:
    """Nothing is idempotent about the hook: one boot, two deployments.

    Both lifecycle routes fire it (documented in instrumentation.md), and a
    ``wait-boot`` call is a natural follow-up to ``start``. The two runs carry
    different ids, execute concurrently and both drive the same AVD (Magisk
    module installs, ``complete_magisk_setup``, reboots).
    """
    container = _Container()
    adapter = ContainerAdapter(container)

    start_instance(adapter, "avd-01", None)
    wait_boot(adapter, "avd-01", None)

    assert container.hook.queued == ["avd-01"], (
        "start + wait-boot queued the profile "
        f"{len(container.hook.queued)} times for one boot: {container.hook.queued}"
    )


def test_dry_run_start_queues_a_real_auto_deployment() -> None:
    """``dry_run`` starts nothing, yet the hook still queues a live deployment.

    The instance is not running, so the run is recorded as failed/skipped; if it
    was already running from an earlier boot, the dry-run request launches a real
    deployment (installs and reboots) that the operator did not ask for.
    """
    container = _Container()
    adapter = ContainerAdapter(container)

    start_instance(adapter, "avd-01", InstanceStartRequest(dry_run=True))

    assert container.runtime_service.start_calls[0]["dry_run"] is True
    assert container.hook.queued == [], (
        "a dry_run start queued a real instrumentation deployment: "
        f"{container.hook.queued}"
    )


def test_deleting_a_profile_leaves_the_auto_profile_binding_dangling(
    tmp_path: Path,
) -> None:
    """``delete_profile`` does not clean ``instrumentation.auto_profiles``.

    ``GET .../auto-profile`` keeps returning the deleted id and
    ``try_auto_profile`` swallows the resulting ``NotFoundError``, so automation
    silently stops working with no error visible to the operator.
    """
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
        f"{binding['profile_id']!r} (try_auto_profile returns None silently)"
    )
