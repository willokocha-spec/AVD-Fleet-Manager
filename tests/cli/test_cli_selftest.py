"""``avdfleet selftest`` — acceptance-sequence behaviour and reports.

The self-test is the command operators run on a real host to produce the BRIEF
§7 evidence.  These tests pin the three environments it must handle correctly:

* no ``--e2e``: quick host checks, emulator items explicitly skipped;
* ``--e2e`` without WHPX/KVM/HVF: clear SKIPs, exit 0 (or exit 3 with
  ``--require-acceleration``);
* ``--fakes``: the full sequence against the injected fake container, labelled
  ``evidence: fakes`` so it can never be mistaken for real emulator evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from avdfleet.core.models import InstanceResult, InstanceState, RunState, ScriptRun

from . import (
    ServiceStub,
    build_stub_container,
    default_instance,
    invoke,
    invoke_json,
    output_text,
    prepare_env,
    register_container,
)

#: Every real-emulator item the self-test must report on.
EMULATOR_CHECKS = (
    "image",
    "template",
    "instances",
    "boot",
    "oobe",
    "proxies",
    "scripts.adb",
    "scripts.host_python",
    "scripts.appium",
    "scripts.cancel",
    "scripts.timeout",
    "custom_image",
)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    return prepare_env(monkeypatch, tmp_path)


def _by_id(payload: dict) -> dict[str, dict]:
    return {check["id"]: check for check in payload["checks"]}


def _overrides(monkeypatch, env, overrides, *, accelerated: bool = False):
    container = build_stub_container(
        config=env.config, paths=env.paths, accelerated=accelerated, overrides=overrides
    )
    register_container(monkeypatch, container)
    return container


# ---------------------------------------------------------------------------
# Quick mode
# ---------------------------------------------------------------------------


def test_quick_mode_reports_host_checks_and_skips_emulator_items(env) -> None:
    result, payload = invoke_json(["selftest"])
    assert result.exit_code == 0, output_text(result)
    assert payload["ok"] is True
    assert payload["evidence"] == "real-emulator"
    assert payload["mode"]["e2e"] is False
    checks = _by_id(payload)
    assert checks["acceleration"]["status"] == "skip"
    assert checks["doctor"]["status"] == "pass"
    assert checks["sdk"]["status"] == "pass"
    assert checks["portable"]["status"] == "skip"
    for check_id in EMULATOR_CHECKS:
        assert checks[check_id]["status"] == "skip"
        assert "pass --e2e" in checks[check_id]["detail"]
    assert payload["summary"]["fail"] == 0
    assert payload["summary"]["total"] == 17


def test_quick_mode_human_summary(env) -> None:
    result = invoke(["selftest"])
    assert result.exit_code == 0
    text = output_text(result)
    assert "avdfleet selftest" in text
    assert "summary:" in text
    assert "selftest passed" in text


# ---------------------------------------------------------------------------
# Fakes activation is opt-in
# ---------------------------------------------------------------------------


def test_real_run_never_activates_development_fakes(env, monkeypatch) -> None:
    """A run without ``--fakes`` must not touch the development fakes.

    The installed console script has no ``tests`` package on ``sys.path``, so
    the old unconditional ``_activate_fakes()`` made ``avdfleet selftest --e2e``
    abort with an environment error on every real installation.  It only worked
    through ``python -m avdfleet``, which puts the checkout on ``sys.path``.
    """
    from avdfleet.cli.commands.selftest import SelftestRunner

    calls: list[bool] = []

    def _must_not_run(self) -> None:  # pragma: no cover - assertion path
        calls.append(True)
        raise AssertionError("_activate_fakes must not run without --fakes")

    monkeypatch.setattr(SelftestRunner, "_activate_fakes", _must_not_run)
    result, payload = invoke_json(["selftest"])
    assert result.exit_code == 0, output_text(result)
    assert calls == []
    assert payload["mode"]["fakes"] is False


def test_fakes_run_activates_development_fakes(env, monkeypatch) -> None:
    """``--fakes`` still activates the fakes (the opt-in path is preserved)."""
    from avdfleet.cli.commands.selftest import SelftestRunner

    calls: list[bool] = []
    monkeypatch.setattr(SelftestRunner, "_activate_fakes", lambda self: calls.append(True))
    result, payload = invoke_json(["selftest", "--fakes"])
    assert calls == [True]
    assert payload["mode"]["fakes"] is True


# ---------------------------------------------------------------------------
# --e2e without hardware acceleration
# ---------------------------------------------------------------------------


def test_e2e_without_acceleration_skips_real_emulator_items(env) -> None:
    result, payload = invoke_json(["selftest", "--e2e"])
    assert result.exit_code == 0, output_text(result)
    checks = _by_id(payload)
    assert checks["acceleration"]["status"] == "skip"
    assert "no WHPX/KVM/HVF" in checks["acceleration"]["detail"]
    for check_id in EMULATOR_CHECKS:
        assert checks[check_id]["status"] == "skip", check_id
        assert "acceleration" in checks[check_id]["detail"], check_id
    assert payload["summary"]["fail"] == 0
    assert payload["summary"]["pass"] >= 2  # doctor + sdk (+ host-only fleet dry run)
    assert checks["fleet"]["status"] == "pass"  # fleet dry-run is not an emulator item
    assert payload["evidence"] == "real-emulator"
    assert any("SKIP" in note or "skipped" in note.lower() for note in payload["notes"])


def test_require_acceleration_exits_environment_error(env) -> None:
    result, payload = invoke_json(["selftest", "--e2e", "--require-acceleration"])
    assert result.exit_code == 3, output_text(result)
    checks = _by_id(payload)
    assert checks["acceleration"]["status"] == "fail"
    assert payload["ok"] is False
    assert payload["exit_code"] == 3


# ---------------------------------------------------------------------------
# --fakes
# ---------------------------------------------------------------------------


def test_fakes_mode_runs_sequence_with_fakes_evidence(env) -> None:
    result, payload = invoke_json(["selftest", "--fakes"])
    assert result.exit_code == 0, output_text(result)
    assert payload["evidence"] == "fakes"
    assert payload["mode"]["fakes"] is True
    checks = _by_id(payload)
    for check_id in (
        "image",
        "template",
        "instances",
        "boot",
        "oobe",
        "proxies",
        "scripts.adb",
        "scripts.host_python",
        "scripts.cancel",
        "scripts.timeout",
        "fleet",
    ):
        assert checks[check_id]["status"] == "pass", f"{check_id}: {checks[check_id]}"
    # Fake mode self-provisions Appium (the stub start() returns a running server).
    assert checks["scripts.appium"]["status"] == "pass"
    assert env.service("appium").called("start")
    assert checks["custom_image"]["status"] == "skip"  # no --custom-image
    assert payload["summary"]["fail"] == 0
    assert payload["summary"]["pass"] >= 12
    assert any("FAKES" in note or "fakes" in note for note in payload["notes"])
    assert payload["ok"] is True


def test_fakes_env_variable(env) -> None:
    result, payload = invoke_json(["selftest"], env={"AVDFLEET_SELFTEST_FAKES": "1"})
    assert result.exit_code == 0, output_text(result)
    assert payload["evidence"] == "fakes"
    assert payload["mode"]["fakes"] is True


def test_fakes_mode_human_output_mentions_fakes(env) -> None:
    result = invoke(["selftest", "--fakes"])
    assert result.exit_code == 0
    text = output_text(result)
    assert "evidence: fakes" in text
    assert "not real-emulator evidence" in text or "FAKES" in text


# ---------------------------------------------------------------------------
# --e2e on an accelerated host
# ---------------------------------------------------------------------------


def test_accelerated_e2e_runs_full_sequence(monkeypatch, tmp_path) -> None:
    env = prepare_env(monkeypatch, tmp_path, accelerated=True)
    result, payload = invoke_json(["selftest", "--e2e"])
    assert result.exit_code == 0, output_text(result)
    checks = _by_id(payload)
    assert checks["acceleration"]["status"] == "pass"
    assert checks["image"]["status"] == "pass"
    assert checks["template"]["status"] == "pass"
    assert checks["instances"]["status"] == "pass"
    assert checks["boot"]["status"] == "pass"
    assert checks["oobe"]["status"] == "pass"
    assert checks["fleet"]["status"] == "pass"
    # The instances check caps deployment at the host capacity reported by the
    # runtime service (stub: can_start_more=4), not blindly at --count.
    instances = checks["instances"]["data"]
    assert instances["requested"] == 5
    assert len(instances["instances"]) == 4
    assert env.service("runtime").called("wait_boot")


def test_custom_image_flag(monkeypatch, tmp_path) -> None:
    env = prepare_env(monkeypatch, tmp_path, accelerated=True)
    custom = tmp_path / "custom-image"
    custom.mkdir()
    result, payload = invoke_json(["selftest", "--e2e", "--custom-image", str(custom)])
    assert result.exit_code == 0, output_text(result)
    checks = _by_id(payload)
    assert checks["custom_image"]["status"] == "pass"
    assert env.service("images").called("import_directory")
    assert env.service("avds").called("create_instance_from_image")


def test_keep_retains_created_resources(monkeypatch, tmp_path) -> None:
    env = prepare_env(monkeypatch, tmp_path, accelerated=True)
    result, payload = invoke_json(["selftest", "--e2e", "--keep"])
    assert result.exit_code == 0, output_text(result)
    assert payload["mode"]["keep"] is True
    assert env.service("avds").called("list_instances")  # created
    assert not env.service("avds").called("delete_instance")
    assert any("--keep" in note for note in payload["notes"])


# ---------------------------------------------------------------------------
# Re-runnability: previous self-test leftovers are cleaned up
# ---------------------------------------------------------------------------


def _make_orphaned_selftest_artifacts(env) -> tuple[Path, Path]:
    """Create the .avd directory + .ini pointer an interrupted run leaves."""
    avd_home = Path(env.paths.avd_home)
    orphan_dir = avd_home / "selftest-01.avd"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "config.ini").write_text("hw.ramSize=2048\n", encoding="utf-8")
    orphan_ini = avd_home / "selftest-01.ini"
    orphan_ini.write_text("path=selftest-01.avd\n", encoding="utf-8")
    return orphan_dir, orphan_ini


def test_cleanup_removes_orphaned_selftest_avd_artifacts(env) -> None:
    """An orphaned AVD that ``avd list`` cannot see must not break a rerun.

    The first self-test run can leave ``selftest-01.avd``/``selftest-01.ini``
    behind while the AVD service no longer reports the AVD; the second run then
    failed with "an AVD named 'selftest-01' already exists".  Cleanup must
    remove the on-disk leftovers before deploying.
    """
    orphan_dir, orphan_ini = _make_orphaned_selftest_artifacts(env)
    assert orphan_dir.is_dir() and orphan_ini.is_file()

    result, payload = invoke_json(["selftest", "--fakes"])
    assert result.exit_code == 0, output_text(result)
    checks = _by_id(payload)
    assert checks["instances"]["status"] == "pass", checks["instances"]
    assert not orphan_dir.exists()
    assert not orphan_ini.exists()
    assert not [note for note in payload["notes"] if note.startswith("cleanup problems:")]


def test_cleanup_keeps_non_selftest_avd_artifacts(env) -> None:
    """Cleanup is prefix-scoped: unrelated AVDs in the AVD home stay untouched."""
    avd_home = Path(env.paths.avd_home)
    device_dir = avd_home / "Device.avd"
    device_dir.mkdir()
    (device_dir / "config.ini").write_text("hw.ramSize=2048\n", encoding="utf-8")
    device_ini = avd_home / "Device.ini"
    device_ini.write_text("path=Device.avd\n", encoding="utf-8")
    lookalike = avd_home / "selftestish.avd"  # starts with 'selftest', but not 'selftest-'
    lookalike.mkdir()

    result, payload = invoke_json(["selftest", "--fakes"])
    assert result.exit_code == 0, output_text(result)
    assert _by_id(payload)["instances"]["status"] == "pass"
    assert device_dir.is_dir()
    assert device_ini.is_file()
    assert lookalike.is_dir()


def test_cleanup_scans_the_avd_service_home(env) -> None:
    """The SDK backend can resolve a different AVD home than ``paths.avd_home``.

    On a stock install the AVD service uses ``~/.android/avd`` while the CLI
    paths object points at the AVD Fleet data directory; cleanup must scan the
    service's effective home too or the real orphan survives.
    """
    service_home = env.tmp_path / "sdk-avd-home"
    service_home.mkdir()
    orphan_dir = service_home / "selftest-02.avd"
    orphan_dir.mkdir()
    orphan_ini = service_home / "selftest-02.ini"
    orphan_ini.write_text("path=selftest-02.avd\n", encoding="utf-8")
    env.service("avds").avd_home = service_home

    result, payload = invoke_json(["selftest", "--fakes"])
    assert result.exit_code == 0, output_text(result)
    assert not orphan_dir.exists()
    assert not orphan_ini.exists()
    assert not [note for note in payload["notes"] if note.startswith("cleanup problems:")]


def test_cleanup_failure_is_reported_in_notes(env, monkeypatch) -> None:
    """A failed artifact removal is reported in ``notes``, never swallowed."""
    from avdfleet.cli.commands.selftest import SelftestRunner

    orphan_dir, orphan_ini = _make_orphaned_selftest_artifacts(env)

    def _failing_remove(self, path: Path) -> None:
        raise PermissionError(f"access denied removing {path.name}")

    monkeypatch.setattr(SelftestRunner, "_remove_selftest_artifact", _failing_remove)

    result, payload = invoke_json(["selftest", "--fakes"])
    assert result.exit_code == 0, output_text(result)
    assert orphan_dir.is_dir() and orphan_ini.is_file()
    cleanup_notes = [note for note in payload["notes"] if note.startswith("cleanup problems:")]
    assert cleanup_notes, payload["notes"]
    assert "selftest-01.avd" in cleanup_notes[0]
    assert "selftest-01.ini" in cleanup_notes[0]
    assert "access denied" in cleanup_notes[0]


def test_cleanup_stops_running_orphan_emulator_before_removal(env) -> None:
    """A still-running orphan is stopped through the runtime before its files go."""
    orphan_dir, orphan_ini = _make_orphaned_selftest_artifacts(env)
    runtime = env.service("runtime")
    stop_calls: list[tuple[str, bool]] = []

    def list_running():
        return [default_instance("selftest-01", state=InstanceState.RUNNING)]

    def stop(name, **kwargs):
        stop_calls.append((name, orphan_dir.exists()))
        return default_instance(name, state=InstanceState.STOPPED)

    runtime._methods["list_running"] = list_running
    runtime._methods["stop"] = stop

    result, payload = invoke_json(["selftest", "--fakes"])
    assert result.exit_code == 0, output_text(result)
    assert not orphan_dir.exists() and not orphan_ini.exists()
    # The first stop is the pre-deploy cleanup, while the orphan still exists.
    assert stop_calls[0] == ("selftest-01", True), stop_calls


# ---------------------------------------------------------------------------
# Failure reporting
# ---------------------------------------------------------------------------


def test_failed_check_exits_one_machine_readable(monkeypatch, env) -> None:
    def always_succeeds(request):
        instances = list(getattr(request, "instances", []) or [])
        return ScriptRun(
            id="run-ok",
            script_id=str(getattr(request, "script_id", "")),
            state=RunState.SUCCEEDED,
            instances=instances,
            results=[InstanceResult(instance=name, state=RunState.SUCCEEDED) for name in instances],
        )

    _overrides(monkeypatch, env, {"scripts": ServiceStub(run=always_succeeds)})
    result, payload = invoke_json(["selftest", "--fakes"])
    assert result.exit_code == 1, output_text(result)
    checks = _by_id(payload)
    assert checks["scripts.timeout"]["status"] == "fail"
    assert payload["ok"] is False
    assert payload["exit_code"] == 1
    assert payload["summary"]["fail"] >= 1


def test_capacity_blocked_boot_is_skip_not_fail(monkeypatch, tmp_path) -> None:
    """A host that cannot fit an emulator gets BLOCKED/SKIP, exit 0 (BRIEF §6)."""
    from avdfleet.core.errors import CapacityError

    env = prepare_env(monkeypatch, tmp_path, accelerated=True)
    _overrides(
        monkeypatch,
        env,
        {
            "runtime": ServiceStub(
                start=lambda name, **kwargs: (_ for _ in ()).throw(
                    CapacityError(
                        "cannot start instance: needs 512 MB RAM but only 400 MB is available"
                    )
                ),
                wait_boot=lambda name, **kwargs: None,
                capacity=lambda: {"ram_available_mb": 400, "can_start_more": 0, "max_instances": 8},
            )
        },
        accelerated=True,
    )
    result, payload = invoke_json(["selftest", "--e2e", "--count", "1"])
    assert result.exit_code == 0, output_text(result)
    checks = _by_id(payload)
    assert checks["boot"]["status"] == "skip"
    assert "BLOCKED" in checks["boot"]["detail"]
    assert "capacity" in checks["boot"]["detail"].lower() or "RAM" in checks["boot"]["detail"]
    for dependent in ("oobe", "proxies", "scripts.adb", "scripts.host_python"):
        assert checks[dependent]["status"] == "skip", dependent
        assert "skipped" in checks[dependent]["detail"].lower()
    assert payload["summary"]["fail"] == 0


def test_template_ram_adapts_to_available_capacity(monkeypatch, tmp_path) -> None:
    env = prepare_env(monkeypatch, tmp_path, accelerated=True)
    env.service("runtime")._methods["capacity"] = lambda: {
        "ram_available_mb": 1200,
        "can_start_more": 1,
        "max_instances": 8,
    }
    result, payload = invoke_json(["selftest", "--e2e", "--count", "1"])
    assert result.exit_code == 0, output_text(result)
    checks = _by_id(payload)
    # honest default 2048 does not fit: clamp to available - 512 MiB margin.
    assert checks["template"]["data"]["ram_mb"] == 688
    assert checks["instances"]["data"]["ram_mb"] == 688
    assert checks["boot"]["status"] == "pass"


def test_report_shape(env) -> None:
    _, payload = invoke_json(["selftest"])
    for key in ("ok", "exit_code", "mode", "evidence", "acceleration", "summary", "checks", "notes", "artifacts"):
        assert key in payload, key
    assert payload["mode"]["count"] == 5
    assert payload["artifacts"]["data_dir"]
    assert isinstance(payload["checks"], list)
    for check in payload["checks"]:
        assert set(check) == {"id", "title", "status", "detail", "duration_s", "data"}
        assert check["status"] in {"pass", "fail", "skip", "warn"}
