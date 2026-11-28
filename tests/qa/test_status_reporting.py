"""Defects where a step reports success the device state does not support.

Each test encodes the behaviour the docs and the feature's own "installed /
enabled / active" promise require; the assertion fails against the shipped code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from avdfleet.instrumentation import ProfileStep, RunState, StepAction
from tests.qa.qa_support import (
    FakeClock,
    ScriptedAdb,
    magisk_script,
    open_service,
    target,
    zip_bytes,
)


def test_enable_module_claims_success_for_a_module_that_is_not_installed(
    tmp_path: Path,
) -> None:
    """``enable_module`` runs ``rm -f``, which succeeds even when the module is absent.

    The step then reports ``ok=True`` / state ``enabled``. Nothing on the device
    was checked, so a profile can "enable" a module that was never installed and
    the run is reported as successful.
    """
    adb = ScriptedAdb(default=(0, ""))  # every shell command "succeeds"
    with open_service(tmp_path, adb) as service:
        step = ProfileStep(
            id="enable-ghost",
            action=StepAction.ENABLE_MODULE,
            module_identifier="ghost_module",
        )

        result = service._step_enable_module(step, target())

    assert result.ok is False, (
        "enable_module reported success although it never verified that "
        f"/data/adb/modules/{step.module_identifier} exists (message: {result.message!r})"
    )


def test_install_module_without_a_module_id_claims_success_without_checking_presence(
    tmp_path: Path,
) -> None:
    """A Magisk ZIP whose ``module.prop`` has no ``id`` skips the presence probe.

    ``_install_magisk_module`` sets ``ok = install.ok and (present or not
    identifier)``, so with no identifier the exit status of
    ``magisk --install-module`` is the only evidence. A failed install that still
    exits 0 (or a no-op) is reported as ``enabled`` and the run succeeds.
    """
    adb = magisk_script()
    with open_service(tmp_path, adb) as service:
        record = service.import_upload(
            "no-id.zip",
            zip_bytes({"module.prop": b"name=No identifier\nversion=1.0\n"}),
        )
        assert record.metadata.get("module_prop", {}).get("id", "") == ""
        adb.responses["/data/local/tmp/no-id.zip"] = (0, "")
        adb.responses["magisk --install-module /data/local/tmp/no-id.zip"] = (0, "")
        step = ProfileStep(
            id="install", action=StepAction.INSTALL_MODULE, module_id=record.id
        )

        result = service._step_install_module(step, target())

    checks = [command for command in adb.commands if command.startswith("[ -d /data/adb/modules")]
    assert checks == [], f"unexpected presence check: {checks}"
    assert result.ok is False, (
        "install_module reported the module as installed while nothing on the "
        f"device was inspected for it (message: {result.message!r})"
    )


def test_reboot_step_reports_success_after_the_reboot_command_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exit status of ``adb reboot`` is ignored, and a stale boot flag passes.

    When the reboot request fails (device offline, adbd error) but
    ``sys.boot_completed`` still reads ``1`` from the boot that is already
    running, the step reports "the AVD rebooted and finished booting".
    """
    adb = ScriptedAdb(
        {
            "reboot": (1, "error: device 'emulator-5554' not found"),
            "getprop sys.boot_completed": (0, "1\n"),
        }
    )
    clock = FakeClock()
    monkeypatch.setattr("avdfleet.instrumentation.service.time", clock)
    with open_service(tmp_path, adb) as service:
        step = ProfileStep(
            id="reboot", action=StepAction.REBOOT, params={"timeout_s": 30}
        )

        result = service._step_reboot(step, target())

    assert result.ok is False, (
        "reboot reported success although the reboot command failed; the "
        f"boot flag observed belongs to the pre-reboot device (message: {result.message!r})"
    )


def test_verify_ignores_an_explicit_false_expectation(tmp_path: Path) -> None:
    """``verify`` only asserts ``expect[key] is True``; ``false`` is dropped.

    A profile saved with ``expect: {"magisk": false}`` (the editor accepts any
    JSON object) therefore reports "all expected components are operational" on
    a Magisk-rooted device -- the opposite of what it was asked to check.
    """
    adb = magisk_script()
    with open_service(tmp_path, adb) as service:
        step = ProfileStep(
            id="verify",
            action=StepAction.VERIFY,
            params={"expect": {"magisk": False}},
        )

        result = service._step_verify(step, target())

    assert result.ok is False, (
        "verify passed although the device contradicts an explicit "
        f"expect.magisk=False (message: {result.message!r})"
    )


def test_remove_lsposed_reports_absent_after_its_presence_check_failed(
    tmp_path: Path,
) -> None:
    """A failed ``[ -d ... ]`` probe is read as "LSPosed is not installed".

    On an image with no usable root the probe command exits non-zero for lack of
    permission, not because the directory is absent; the step still returns
    ``ok=True`` with state ``absent`` and the run is considered successful.
    """
    adb = ScriptedAdb(
        {
            "getprop": (0, "[ro.build.type]: [user]\n"),
            "su -c id": (1, "su: not found\n"),
            "root": (1, "adbd cannot run as root in production builds\n"),
            "id": (1, "uid=2000(shell)\n"),
        }
    )
    with open_service(tmp_path, adb) as service:
        step = ProfileStep(id="remove-lsposed", action=StepAction.REMOVE_LSPOSED)

        result = service._step_remove_lsposed(step, target())

    assert result.ok is False, (
        "remove_lsposed reported 'LSPosed is not installed' although the "
        f"presence check itself failed for lack of root (message: {result.message!r})"
    )


def test_failed_step_with_continue_on_error_still_marks_the_run_succeeded(
    tmp_path: Path,
) -> None:
    """``continue_on_error`` skips the stop but also erases the failure verdict.

    ``_execute_target`` returns ``ok=True`` after the loop as long as no step was
    *fatal*, so a failed step recorded in ``result.steps`` (``ok=False``) does not
    make the target fail; the run is then persisted as ``succeeded``.
    """
    adb = ScriptedAdb(
        {
            "getprop": (0, "[ro.build.type]: [user]\n"),
            "su -c id": (1, "su: not found\n"),
            "root": (1, "adbd cannot run as root in production builds\n"),
            "id": (1, "uid=2000(shell)\n"),
        }
    )
    with open_service(tmp_path, adb) as service:
        profile = service.save_profile(
            profile_id=None,
            name="Continue on error",
            steps=[
                {"action": "verify_root", "params": {"continue_on_error": True}},
                {"action": "verify", "params": {}},
            ],
        )
        run = service.start_run(profile_id=profile.id, targets=["avd-01"])

        finished = service.execute_run(run.id)

    first_step = finished.results[0].steps[0]
    assert first_step.ok is False  # the step really did fail
    assert finished.state is not RunState.SUCCEEDED, (
        "a run whose first step failed is reported as "
        f"{finished.state} because continue_on_error hides the failure from the target verdict"
    )
