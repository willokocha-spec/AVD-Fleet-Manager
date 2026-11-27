"""Root CLI contract: help, version, global options, JSON, errors, exit codes.

All tests run through Typer's ``CliRunner`` against the in-memory stub container
(``tests.cli``); nothing here touches the Android SDK, an emulator or the
network.  The one subprocess test checks the real ``python -m avdfleet`` entry
point under a ``cp1252`` stdout, which is the Windows legacy-console case that
previously crashed on non-ASCII help glyphs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from avdfleet.core.errors import ConflictError, NotFoundError
from avdfleet.core.models import DoctorCheck, DoctorReport

from . import (
    ServiceStub,
    build_stub_container,
    invoke,
    invoke_json,
    output_text,
    prepare_env,
    register_container,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

GROUPS = (
    "install",
    "repair",
    "uninstall",
    "status",
    "doctor",
    "serve",
    "gui",
    "images",
    "oobe",
    "avd",
    "instances",
    "proxies",
    "scripts",
    "appium",
    "screen",
    "fleet",
    "bundle",
    "portable",
    "selftest",
)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    return prepare_env(monkeypatch, tmp_path)


def _with_overrides(monkeypatch, env, overrides):
    container = build_stub_container(config=env.config, paths=env.paths, overrides=overrides)
    register_container(monkeypatch, container)
    return container


# ---------------------------------------------------------------------------
# Help / version / command tree
# ---------------------------------------------------------------------------


def test_root_help_lists_every_command_group(env) -> None:
    result = invoke(["--help"])
    assert result.exit_code == 0, output_text(result)
    text = output_text(result)
    assert "Usage: avdfleet" in text
    for group in GROUPS:
        assert group in text, f"missing command group {group!r} in root help"


def test_bare_invocation_prints_help(env) -> None:
    result = invoke([])
    assert result.exit_code == 0
    assert "Usage: avdfleet" in output_text(result)


def test_version_prints_version_and_exits_zero(env) -> None:
    result = invoke(["--version"])
    assert result.exit_code == 0
    assert "avdfleet 0.1.0" in output_text(result)


def test_help_and_version_never_build_the_container(monkeypatch) -> None:
    """Lazy CliContext: help/version must not load config or services."""
    monkeypatch.setenv("AVDFLEET_CONTAINER", "tests.cli:broken_container_factory")
    for args in (["--help"], ["--version"], ["doctor", "--help"], ["avd", "deploy", "--help"]):
        result = invoke(args)
        assert result.exit_code == 0, f"{args}: {output_text(result)}"


def test_every_group_help_exits_zero(env) -> None:
    for group in GROUPS:
        result = invoke([group, "--help"])
        assert result.exit_code == 0, f"{group} --help: {output_text(result)}"
    for nested in (["avd", "templates"], ["instances", "snapshots"], ["scripts", "runs"]):
        result = invoke([*nested, "--help"])
        assert result.exit_code == 0, f"{nested} --help: {output_text(result)}"


def test_unknown_command_is_usage_error(env) -> None:
    result = invoke(["does-not-exist"])
    assert result.exit_code == 2
    assert "No such command" in output_text(result)


def test_missing_config_file_is_usage_error(env) -> None:
    result = invoke(["--config", str(Path(env.tmp_path) / "missing.toml"), "status"])
    assert result.exit_code == 2, output_text(result)
    assert "configuration file not found" in output_text(result)


# ---------------------------------------------------------------------------
# Global --json / --verbose / --quiet
# ---------------------------------------------------------------------------


def test_global_json_doctor(env) -> None:
    result, payload = invoke_json(["doctor"])
    assert result.exit_code == 0, output_text(result)
    assert payload["ok"] is True
    assert payload["summary"] == {"ok": 2, "warn": 0, "fail": 0, "skip": 0}
    assert payload["report"]["checks"]


def test_command_level_json_option(env) -> None:
    result = invoke(["doctor", "--json"])
    assert result.exit_code == 0, output_text(result)
    payload = json.loads(result.stdout)
    assert payload["ok"] is True


def test_verbose_and_quiet_are_accepted(env) -> None:
    assert invoke(["--verbose", "doctor"]).exit_code == 0
    assert invoke(["--quiet", "doctor"]).exit_code == 0


def test_doctor_ok_report(env) -> None:
    result, payload = invoke_json(["doctor", "--slow"])
    assert result.exit_code == 0, output_text(result)
    doctor = env.service("doctor")
    assert doctor.called("run")
    _, kwargs = doctor.last_call("run")
    assert kwargs.get("include_slow") is True
    assert len(payload["report"]["checks"]) == 2


def test_doctor_failure_exits_one(monkeypatch, env) -> None:
    _with_overrides(
        monkeypatch,
        env,
        {
            "doctor": ServiceStub(
                run=lambda **kwargs: DoctorReport(
                    ok=False,
                    checks=[DoctorCheck(id="sdk_root", title="SDK root", status="fail", detail="missing")],
                )
            )
        },
    )
    result, payload = invoke_json(["doctor"])
    assert result.exit_code == 1, output_text(result)
    assert payload["ok"] is False
    assert payload["summary"]["fail"] == 1


def test_doctor_fix_applies_and_forwards(env) -> None:
    result, payload = invoke_json(["doctor", "--fix", "sdk_root"])
    assert result.exit_code == 0, output_text(result)
    doctor = env.service("doctor")
    assert doctor.called("fix")
    args, kwargs = doctor.last_call("fix")
    assert args[0] == "sdk_root"
    assert kwargs.get("dry_run") is False
    assert payload["fixes"][0]["message"].startswith("fix applied")


# ---------------------------------------------------------------------------
# Exit codes and error envelope
# ---------------------------------------------------------------------------


def test_not_found_exit_code_four(monkeypatch, env) -> None:
    _with_overrides(
        monkeypatch,
        env,
        {
            "avds": ServiceStub(
                list_instances=lambda: [],
                get_instance=lambda name: (_ for _ in ()).throw(NotFoundError("instance not found")),
            ),
            "runtime": ServiceStub(
                status=lambda name: (_ for _ in ()).throw(NotFoundError("instance not found")),
                list_running=lambda: [],
            ),
        },
    )
    result = invoke(["--json", "instances", "get", "nope"])
    assert result.exit_code == 4, output_text(result)
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["exit_code"] == 4
    assert payload["error"]["code"] == "not_found"


def test_conflict_exit_code_five(monkeypatch, env) -> None:
    _with_overrides(
        monkeypatch,
        env,
        {
            "runtime": ServiceStub(
                start=lambda name, **kwargs: (_ for _ in ()).throw(ConflictError("already running"))
            )
        },
    )
    result = invoke(["--json", "instances", "start", "avd-01"])
    assert result.exit_code == 5, output_text(result)
    assert json.loads(result.stdout)["exit_code"] == 5


def test_missing_service_is_environment_error(monkeypatch, env) -> None:
    container = env.container
    container._services.pop("doctor")
    prepare_env(monkeypatch, env.tmp_path)  # re-isolate AVDFLEET_* paths
    register_container(monkeypatch, container)
    result = invoke(["--json", "doctor"])
    assert result.exit_code == 3, output_text(result)
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "service_unavailable"


def test_unexpected_exception_is_redacted_without_traceback(monkeypatch, env) -> None:
    _with_overrides(
        monkeypatch,
        env,
        {
            "avds": ServiceStub(
                list_instances=lambda: (_ for _ in ()).throw(
                    RuntimeError("boom: http://alice:supersecret@proxy:8080")
                )
            )
        },
    )
    result = invoke(["--json", "avd", "list"])
    assert result.exit_code == 1, output_text(result)
    text = output_text(result)
    assert "supersecret" not in text
    assert "Traceback" not in text
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "internal_error"
    assert "supersecret" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# Real entry point under a legacy cp1252 console
# ---------------------------------------------------------------------------


def test_all_help_paths_work_under_cp1252_redirected_output() -> None:
    """Regression: piped/redirected help on Windows cp1252 must not traceback."""
    script = (
        "from avdfleet.cli.main import main\n"
        "paths = [[], ['install'], ['images'], ['oobe'], ['avd'], ['instances'],\n"
        "         ['proxies'], ['scripts'], ['appium'], ['screen'], ['fleet'],\n"
        "         ['bundle'], ['portable'], ['selftest'], ['serve'], ['doctor'],\n"
        "         ['status'], ['uninstall'], ['repair'], ['gui'], ['apply'], ['plan'],\n"
        "         ['avd', 'templates'], ['instances', 'snapshots'], ['scripts', 'runs']]\n"
        "codes = [main([*path, '--help']) for path in paths]\n"
        "print('CODES=' + ','.join(str(code) for code in codes))\n"
    )
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "cp1252"
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(REPO_ROOT / "src"), existing, str(REPO_ROOT)])
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(REPO_ROOT),
        env=environment,
    )
    combined = (completed.stdout or "") + (completed.stderr or "")
    assert "Traceback" not in combined, combined[-2000:]
    assert "UnicodeEncodeError" not in combined, combined[-2000:]
    assert completed.returncode == 0, combined[-2000:]
    marker = [line for line in combined.splitlines() if line.startswith("CODES=")]
    assert marker, combined[-2000:]
    codes = marker[-1].split("=", 1)[1].split(",")
    assert len(codes) == 25
    assert set(codes) == {"0"}, combined[-2000:]
