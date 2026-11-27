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

import pytest

from avdfleet.core.models import InstanceResult, RunState, ScriptRun

from . import (
    ServiceStub,
    build_stub_container,
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
