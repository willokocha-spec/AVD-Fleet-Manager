"""Concurrency defect: a cancelled run can still be executed by the worker.

``execute_run`` never checks the run's persisted state. The API's background
path is ``start_run`` (state ``queued``) followed by ``execute_run`` on a new
thread; ``cancel_run`` on a queued run has no cancellation event to set, so it
writes ``cancelled`` to the database and returns that to the caller. The worker
thread then loads the same row, overwrites the state with ``running`` and runs
every step -- the cancellation is silently lost.
"""

from __future__ import annotations

from pathlib import Path

from avdfleet.instrumentation import RunState
from tests.qa.qa_support import magisk_script, open_service


def test_cancelled_queued_run_is_still_executed_by_the_worker(tmp_path: Path) -> None:
    adb = magisk_script()
    with open_service(tmp_path, adb) as service:
        profile = service.save_profile(
            profile_id=None, name="Cancel race", steps=[{"action": "verify_root"}]
        )
        run = service.start_run(profile_id=profile.id, targets=["avd-01"])

        # The API answers POST /deployments before its worker thread necessarily
        # enters execute_run; a cancel in that window takes this branch.
        cancelled = service.cancel_run(run.id)
        assert cancelled.state is RunState.CANCELLED

        # What the already-started worker thread does next.
        executed = service.execute_run(run.id)

    assert executed.state is RunState.CANCELLED, (
        "execute_run ran a cancelled deployment and replaced the cancelled state "
        f"with {executed.state} (results: {[r.instance for r in executed.results]})"
    )
