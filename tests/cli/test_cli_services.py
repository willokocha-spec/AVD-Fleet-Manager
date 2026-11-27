"""Dispatch smoke tests for every service-backed command group.

Each command is invoked through ``CliRunner`` against the in-memory container in
``tests.cli``; the assertions check (a) the JSON/output shape, (b) that the right
service method was called with the documented arguments, and (c) the exit codes
for error paths.  No fakes fixtures, SDK, emulator or network are involved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from avdfleet.core.errors import NotFoundError
from avdfleet.core.models import AvdInfo, InstanceState, ProxyTestResult

from . import (
    ServiceStub,
    build_stub_container,
    invoke,
    invoke_json,
    output_text,
    prepare_env,
    register_container,
)

PNG = b"\x89PNG\r\n\x1a\nselftest"


@pytest.fixture()
def env(monkeypatch, tmp_path):
    return prepare_env(monkeypatch, tmp_path)


def _overrides(monkeypatch, env, overrides):
    container = build_stub_container(config=env.config, paths=env.paths, overrides=overrides)
    register_container(monkeypatch, container)
    return container


def _ok(args: list[str]) -> tuple[object, object]:
    result, payload = invoke_json(args)
    assert result.exit_code == 0, f"{args}: {output_text(result)}"
    assert payload is not None, output_text(result)
    return result, payload


# ---------------------------------------------------------------------------
# images / oobe
# ---------------------------------------------------------------------------


def test_images_list_json_shape(env) -> None:
    _, payload = _ok(["images", "list"])
    assert isinstance(payload, list) and len(payload) == 2
    assert payload[0]["id"].startswith("system-images;android-35")
    assert payload[0]["installed"] is True
    assert payload[0]["oobe_status"] == "verified"
    images = env.service("images")
    _, kwargs = images.last_call("list_images")
    assert kwargs["include_remote"] is True


def test_images_list_installed_only(env) -> None:
    _, payload = _ok(["images", "list", "--installed"])
    assert len(payload) == 1
    assert payload[0]["installed"] is True
    _, kwargs = env.service("images").last_call("list_images")
    assert kwargs["installed_only"] is True


def test_images_download_dry_run(env) -> None:
    _, payload = _ok(["images", "download", "system-images;android-35;google_apis;x86_64", "--dry-run"])
    assert payload["installed"] is True
    args, kwargs = env.service("images").last_call("download")
    assert args[0] == "system-images;android-35;google_apis;x86_64"
    assert kwargs["dry_run"] is True


def test_images_import_forwards_metadata(env) -> None:
    _, payload = _ok(
        [
            "images",
            "import",
            str(env.tmp_path / "custom-image"),
            "--name",
            "My image",
            "--id",
            "custom-1",
            "--api-level",
            "34",
            "--abi",
            "arm64-v8a",
        ]
    )
    assert payload["custom"] is True
    args, kwargs = env.service("images").last_call("import_directory")
    assert args[0] == env.tmp_path / "custom-image"
    assert kwargs["name"] == "My image"
    assert kwargs["image_id"] == "custom-1"
    assert kwargs["api_level"] == 34
    assert kwargs["abi"] == "arm64-v8a"


def test_images_refresh(env) -> None:
    _, payload = _ok(["images", "refresh"])
    assert isinstance(payload, list)
    assert env.service("images").called("refresh")


def test_images_get_not_found_exit_four(monkeypatch, env) -> None:
    _overrides(
        monkeypatch,
        env,
        {"images": ServiceStub(get_image=lambda image_id: (_ for _ in ()).throw(NotFoundError("nope")))},
    )
    result = invoke(["--json", "images", "get", "missing"])
    assert result.exit_code == 4, output_text(result)
    assert json.loads(result.stdout)["error"]["code"] == "not_found"


def test_oobe_matrix_and_verify_and_reset(env) -> None:
    _, matrix = _ok(["oobe", "matrix"])
    assert matrix["records"][0]["status"] == "verified"
    _, verify = _ok(["oobe", "verify", "system-images;android-35;google_apis;x86_64", "--keep-avd"])
    assert verify["activity"] == "setup"
    args, kwargs = env.service("oobe").last_call("verify_image")
    assert kwargs["keep_avd"] is True
    _, reset = _ok(["oobe", "reset", "avd-01", "--strategy", "wipe"])
    assert reset["ok"] is True
    args, kwargs = env.service("oobe").last_call("reset")
    assert args[0] == "avd-01"
    assert kwargs["strategy"] == "wipe"


# ---------------------------------------------------------------------------
# avd templates / instances / config / export / import
# ---------------------------------------------------------------------------


def test_avd_templates_list(env) -> None:
    _, payload = _ok(["avd", "templates", "list"])
    assert payload[0]["id"] == "tpl-default"


def test_avd_templates_create_and_delete(env) -> None:
    _, payload = _ok(
        [
            "avd",
            "templates",
            "create",
            "--name",
            "Fleet A",
            "--image",
            "system-images;android-35;google_apis;x86_64",
            "--ram",
            "4096",
            "--cpu",
            "4",
            "--proxy",
            "px_1",
            "--windowed",
        ]
    )
    assert payload[0]["id"] == "fleet-a"
    avds = env.service("avds")
    template = avds.last_call("save_template")[0][0]
    assert template.ram_mb == 4096
    assert template.cpu_cores == 4
    assert template.proxy_id == "px_1"
    assert template.headless is False
    _ok(["avd", "templates", "delete", "fleet-a"])
    assert avds.last_call("delete_template")[0][0] == "fleet-a"


def test_avd_list_merges_runtime(env) -> None:
    _, payload = _ok(["avd", "list"])
    assert [item["name"] for item in payload] == ["avd-01"]
    assert payload[0]["state"] == "running"


def test_avd_create_delete_clone_deploy(env) -> None:
    _, created = _ok(["avd", "create", "avd-new", "--template", "tpl-default", "--ram", "3072"])
    assert created["name"] == "avd-new"
    args, _ = env.service("avds").last_call("create_instance")
    assert args[:2] == ("tpl-default", "avd-new")
    assert args[2] == {"ram_mb": 3072}

    _, cloned = _ok(["avd", "clone", "avd-01", "--name", "avd-clone"])
    assert cloned["name"] == "avd-clone"

    _, deployed = _ok(["avd", "deploy", "tpl-default", "--count", "3", "--prefix", "fleet"])
    assert [item["name"] for item in deployed] == ["fleet-01", "fleet-02", "fleet-03"]
    args, kwargs = env.service("avds").last_call("bulk_deploy")
    assert args[0] == "tpl-default"
    assert kwargs["count"] == 3
    assert kwargs["prefix"] == "fleet"

    _ok(["avd", "delete", "avd-new", "--keep-files"])
    args, kwargs = env.service("avds").last_call("delete_instance")
    assert args[0] == "avd-new"
    assert kwargs["delete_files"] is False


def test_avd_config_read_and_write(env) -> None:
    _, read = _ok(["avd", "config", "avd-01"])
    assert read["config"]["hw.ramSize"] == "2048"
    _, written = _ok(["avd", "config", "avd-01", "--set", "hw.ramSize=4096", "--dry-run"])
    assert written["dry_run"] is True
    args, kwargs = env.service("avds").last_call("update_config")
    assert args[1] == {"hw.ramSize": "4096"}
    assert kwargs["dry_run"] is True


def test_avd_export_and_import(env, tmp_path) -> None:
    destination = tmp_path / "fleet.yaml"
    _ok(["avd", "export", "--output", str(destination)])
    assert destination.is_file()
    source = tmp_path / "import.yaml"
    source.write_text("version: '1'\n", encoding="utf-8")
    _, imported = _ok(["avd", "import", str(source)])
    assert imported["instances"][0]["name"] == "avd-imported"
    assert env.service("avds").called("import_yaml")


# ---------------------------------------------------------------------------
# instances: lifecycle, screenshots, snapshots, capacity
# ---------------------------------------------------------------------------


def test_instances_lifecycle_dispatch(env) -> None:
    _, payload = _ok(["instances", "list"])
    assert payload[0]["name"] == "avd-01"
    _ok(["instances", "get", "avd-01"])

    _ok(["instances", "start", "avd-01", "--proxy", "px_1", "--windowed", "--dry-run"])
    args, kwargs = env.service("runtime").last_call("start")
    assert args[0] == "avd-01"
    assert kwargs == {"proxy_id": "px_1", "headless": False, "dry_run": True}

    _ok(["instances", "stop", "avd-01", "--force", "--timeout", "5"])
    args, kwargs = env.service("runtime").last_call("stop")
    assert kwargs == {"timeout_s": 5.0, "force": True}

    _ok(["instances", "restart", "avd-01"])
    _ok(["instances", "wait-boot", "avd-01", "--timeout", "30"])
    _, booted = invoke_json(["instances", "wait-boot", "avd-01"])
    assert booted["boot_completed"] is True

    _, reset = _ok(["instances", "reset-oobe", "avd-01", "--no-verify"])
    assert reset["ok"] is True
    _, kwargs = env.service("oobe").last_call("reset")
    assert kwargs["verify"] is False


def test_instances_screenshot_writes_png(env) -> None:
    _, payload = _ok(["instances", "screenshot", "avd-01"])
    output = Path(payload["output"])
    assert output.is_file()
    assert output.read_bytes() == PNG
    assert env.service("screen").called("capture")


def test_instances_snapshots(env) -> None:
    _, listed = _ok(["instances", "snapshots", "list", "avd-01"])
    assert listed[0]["name"] == "golden"
    _ok(["instances", "snapshots", "save", "avd-01", "golden-2", "--golden"])
    args, kwargs = env.service("runtime").last_call("save_snapshot")
    assert args == ("avd-01", "golden-2")
    assert kwargs["golden"] is True
    _ok(["instances", "snapshots", "load", "avd-01", "golden-2"])
    _ok(["instances", "snapshots", "delete", "avd-01", "golden-2"])
    assert env.service("runtime").called("delete_snapshot")


def test_instances_config_clone_delete_capacity(env) -> None:
    _ok(["instances", "config", "avd-01", "--set", "hw.heapSize=512"])
    assert env.service("avds").called("update_config")
    _, cloned = _ok(["instances", "clone", "avd-01", "--name", "avd-copy"])
    assert cloned["name"] == "avd-copy"
    _, capacity = _ok(["instances", "capacity"])
    assert capacity["can_start_more"] == 4
    env.service("runtime")._methods["status"] = lambda name: AvdInfo(name=name, state=InstanceState.STOPPED)
    _ok(["instances", "delete", "avd-copy"])
    assert env.service("avds").last_call("delete_instance")[0][0] == "avd-copy"


def test_instances_delete_running_conflict(monkeypatch, env) -> None:
    _overrides(
        monkeypatch,
        env,
        {"runtime": ServiceStub(status=lambda name: AvdInfo(name=name, state="running"))},
    )
    result = invoke(["--json", "instances", "delete", "avd-01"])
    assert result.exit_code == 5, output_text(result)
    assert json.loads(result.stdout)["error"]["code"] == "conflict"


# ---------------------------------------------------------------------------
# proxies
# ---------------------------------------------------------------------------


def test_proxies_list_masks_password(env) -> None:
    result, payload = _ok(["proxies", "list"])
    assert payload[0]["has_credentials"] is True
    assert payload[0]["username"] == "alice"
    assert "password" not in payload[0]
    assert "supersecret" not in output_text(result)


def test_proxies_add_import_check_delete(env) -> None:
    _, added = _ok(
        ["proxies", "add", "http://bob:hunter2@127.0.0.1:9000", "--name", "listener-2", "--tag", "fast"]
    )
    assert added["id"] == "px_1"
    args, kwargs = env.service("proxies").last_call("add")
    assert kwargs["url"] == "http://bob:hunter2@127.0.0.1:9000"
    assert kwargs["name"] == "listener-2"
    assert kwargs["tags"] == ["fast"]

    proxies_file = env.tmp_path / "proxies.txt"
    proxies_file.write_text("socks5://u:p@127.0.0.1:1080\nhttp://127.0.0.1:8080\n", encoding="utf-8")
    import_result = invoke(["--json", "proxies", "import", str(proxies_file)])
    assert import_result.exit_code == 0, output_text(import_result)
    imported = json.loads(import_result.stdout)
    assert imported["added"][0]["has_credentials"] is True
    assert "hunter2" not in output_text(import_result)
    _, kwargs = env.service("proxies").last_call("import_text")
    assert kwargs["source"] == "import"

    _, checked = _ok(["proxies", "check", "px_1"])
    assert checked[0]["ok"] is True
    _, checked_all = _ok(["proxies", "check-all"])
    assert checked_all[0]["egress_ip"] == "203.0.113.7"
    _ok(["proxies", "delete", "px_1"])
    assert env.service("proxies").called("delete")


def test_proxies_check_failure_exits_one(monkeypatch, env) -> None:
    _overrides(
        monkeypatch,
        env,
        {
            "proxies": ServiceStub(
                check=lambda proxy_id, **kwargs: ProxyTestResult(
                    proxy_id=proxy_id, ok=False, error="connection refused"
                )
            )
        },
    )
    result = invoke(["--json", "proxies", "check", "px_1"])
    assert result.exit_code == 1, output_text(result)
    assert json.loads(result.stdout)[0]["ok"] is False


def test_proxies_assign_unassign_auto_assign(env) -> None:
    _, assignment = _ok(["proxies", "assign", "avd-01", "px_1"])
    assert assignment["proxy_id"] == "px_1"
    args, _ = env.service("assignments").last_call("assign")
    assert args == ("avd-01", "px_1")
    _ok(["proxies", "unassign", "avd-01"])
    _, auto = _ok(["proxies", "auto-assign", "--instances", "avd-01,avd-02", "--strategy", "round_robin"])
    assert len(auto) == 2
    args, kwargs = env.service("assignments").last_call("auto_assign")
    assert args[0] == ["avd-01", "avd-02"]
    assert kwargs["strategy"] == "round_robin"


def test_proxies_verify_host_and_guest(env) -> None:
    _, host_result = _ok(["proxies", "verify", "px_1"])
    assert host_result[0]["ok"] is True
    args, _ = env.service("verifier").last_call("verify_proxy")
    assert args[0].id == "px_1"

    _, guest_result = _ok(["proxies", "verify", "px_1", "--guest", "avd-01"])
    assert guest_result[0]["ok"] is True
    args, kwargs = env.service("verifier").last_call("verify_guest")
    assert args[0] == "avd-01"
    assert args[1].id == "px_1"


def test_proxies_bridges(env) -> None:
    _, payload = _ok(["proxies", "bridges"])
    assert payload == []
    assert env.service("bridge").called("list_active")


# ---------------------------------------------------------------------------
# scripts
# ---------------------------------------------------------------------------


def test_scripts_list_and_create(env) -> None:
    _, listed = _ok(["scripts", "list"])
    assert listed[0]["builtin"] is True
    _, saved = _ok(
        [
            "scripts",
            "create",
            "--name",
            "My script",
            "--type",
            "host_python",
            "--content",
            "print('hi')",
            "--timeout",
            "30",
        ]
    )
    assert saved["id"] == "my-script"
    args, _ = env.service("scripts").last_call("save_script")
    definition = args[0]
    assert definition.type.value == "host_python"
    assert definition.content == "print('hi')"


def test_scripts_run_synchronous(env) -> None:
    _, run = _ok(
        [
            "scripts",
            "run",
            "builtin.getprop-summary",
            "--instances",
            "avd-01,avd-02",
            "--concurrency",
            "2",
            "--timeout",
            "45",
        ]
    )
    assert run["state"] == "succeeded"
    assert run["summary"]["succeeded"] == 2
    args, _ = env.service("scripts").last_call("run")
    request = args[0]
    assert request.instances == ["avd-01", "avd-02"]
    assert request.concurrency == 2
    assert request.timeout_s == 45


def test_scripts_run_detached_submits(env) -> None:
    _, run = _ok(["scripts", "run", "builtin.getprop-summary", "--instances", "avd-01", "--detach"])
    assert run["state"] == "running"
    assert env.service("scripts").called("submit")
    assert not env.service("scripts").called("run")


def test_scripts_run_failure_exits_one(monkeypatch, env) -> None:
    from avdfleet.core.models import InstanceResult, RunState, ScriptRun

    _overrides(
        monkeypatch,
        env,
        {
            "scripts": ServiceStub(
                run=lambda request: ScriptRun(
                    id="run-fail",
                    script_id="builtin.getprop-summary",
                    state=RunState.FAILED,
                    instances=list(getattr(request, "instances", []) or []),
                    results=[InstanceResult(instance="avd-01", state=RunState.FAILED, exit_code=2)],
                )
            )
        },
    )
    result = invoke(["--json", "scripts", "run", "builtin.getprop-summary", "--instances", "avd-01"])
    assert result.exit_code == 1, output_text(result)


def test_scripts_runs_status_and_cancel(env) -> None:
    _, runs = _ok(["scripts", "runs", "list"])
    assert runs == []
    _, status = _ok(["scripts", "runs", "status", "run-cancel"])
    assert status["id"] == "run-cancel"
    _, cancelled = _ok(["scripts", "runs", "cancel", "run-cancel"])
    assert cancelled["state"] == "cancelled"
    assert env.service("scripts").called("cancel")


# ---------------------------------------------------------------------------
# appium / screen
# ---------------------------------------------------------------------------


def test_appium_lifecycle(env) -> None:
    _, status = _ok(["appium", "status"])
    assert status["running"] is False
    _, started = _ok(["appium", "start", "--port", "4800"])
    assert started["running"] is True
    args, kwargs = env.service("appium").last_call("start")
    assert kwargs["port"] == 4800
    _ok(["appium", "stop"])
    _, capabilities = _ok(["appium", "capabilities", "avd-01"])
    assert capabilities["platformName"] == "Android"
    args, kwargs = env.service("appium").last_call("build_capabilities")
    assert kwargs["template"].id == "tpl-default"
    _ok(["appium", "cleanup", "avd-01"])
    assert env.service("appium").called("cleanup_forwards")


def test_screen_capture_and_input(env) -> None:
    result, payload = _ok(["screen", "capture", "avd-01"])
    target = Path(payload["output"])
    assert target.read_bytes() == PNG
    _, tap = _ok(["screen", "tap", "avd-01", "10", "20"])
    assert "10,20" in tap["message"]
    _ok(["screen", "swipe", "avd-01", "1", "2", "3", "4", "--duration", "500"])
    args, kwargs = env.service("screen").last_call("swipe")
    assert args == ("avd-01", 1, 2, 3, 4)
    assert kwargs["duration_ms"] == 500
    _ok(["screen", "text", "avd-01", "hello"])
    _ok(["screen", "key", "avd-01", "HOME"])


def test_screen_capture_empty_payload_is_usage_error(monkeypatch, env) -> None:
    _overrides(monkeypatch, env, {"screen": ServiceStub(capture=lambda name: b"")})
    result = invoke(["--json", "screen", "capture", "avd-01"])
    assert result.exit_code == 2, output_text(result)
    assert json.loads(result.stdout)["error"]["code"] == "usage_error"


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


def test_serve_builds_app_and_prints_url_token_path(monkeypatch, env) -> None:
    import uvicorn

    recorded: dict[str, object] = {}

    def fake_run(app, **kwargs):
        recorded["app"] = app
        recorded["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)
    result, payload = invoke_json(
        ["serve", "--host", "127.0.0.1", "--port", "8899", "--no-open"]
    )
    assert result.exit_code == 0, output_text(result)
    assert payload["url"] == "http://127.0.0.1:8899"
    assert payload["token_path"].endswith("token")
    assert payload["api_prefix"] == "/api/v1"
    assert recorded["kwargs"]["host"] == "127.0.0.1"
    assert recorded["kwargs"]["port"] == 8899
    assert payload["remote"] is False
    assert payload["tls"] is False


def _capture_uvicorn(monkeypatch) -> dict[str, object]:
    """Replace uvicorn.run with a recorder and return the recorded dict."""
    import uvicorn

    recorded: dict[str, object] = {}

    def fake_run(app, **kwargs):
        recorded["app"] = app
        recorded["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)
    return recorded


def test_serve_remote_binding_requires_allow_remote(monkeypatch, env) -> None:
    recorded = _capture_uvicorn(monkeypatch)
    result = invoke(["--json", "serve", "--host", "0.0.0.0", "--port", "8909", "--no-open"])
    assert result.exit_code == 2, output_text(result)
    assert recorded == {}  # the daemon is never started
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "usage_error"
    assert "allow-remote" in payload["error"]["hint"]
    assert "non-loopback" in payload["error"]["message"]


def test_serve_remote_binding_warns_with_allow_remote(monkeypatch, env) -> None:
    recorded = _capture_uvicorn(monkeypatch)
    result, payload = invoke_json(
        ["serve", "--host", "0.0.0.0", "--port", "8909", "--no-open", "--allow-remote"]
    )
    assert result.exit_code == 0, output_text(result)
    text = output_text(result)
    assert "warning" in text.lower()
    assert "non-loopback" in text
    assert recorded["kwargs"]["host"] == "0.0.0.0"
    assert payload["remote"] is True
    # Wildcard binds still advertise the reachable loopback URL.
    assert payload["url"] == "http://127.0.0.1:8909"


def test_serve_loopback_does_not_warn(monkeypatch, env) -> None:
    _capture_uvicorn(monkeypatch)
    result = invoke(["serve", "--no-open"])
    assert result.exit_code == 0, output_text(result)
    assert "non-loopback" not in output_text(result)


def test_serve_tls_requires_both_files(env) -> None:
    result = invoke(["--json", "serve", "--no-open", "--tls-cert", str(env.tmp_path / "cert.pem")])
    assert result.exit_code == 2, output_text(result)
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "usage_error"
    assert "together" in payload["error"]["message"]


def test_serve_tls_missing_file_is_usage_error(env) -> None:
    result = invoke(
        [
            "--json",
            "serve",
            "--no-open",
            "--tls-cert",
            str(env.tmp_path / "missing-cert.pem"),
            "--tls-key",
            str(env.tmp_path / "missing-key.pem"),
        ]
    )
    assert result.exit_code == 2, output_text(result)
    assert "not found" in output_text(result)


def test_serve_tls_pass_through_to_uvicorn(monkeypatch, env) -> None:
    cert = env.tmp_path / "cert.pem"
    key = env.tmp_path / "key.pem"
    cert.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    key.write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    recorded = _capture_uvicorn(monkeypatch)
    result, payload = invoke_json(
        ["serve", "--no-open", "--tls-cert", str(cert), "--tls-key", str(key)]
    )
    assert result.exit_code == 0, output_text(result)
    kwargs = recorded["kwargs"]
    assert kwargs["ssl_certfile"] == str(cert)
    assert kwargs["ssl_keyfile"] == str(key)
    assert payload["tls"] is True
    assert payload["url"].startswith("https://")


# ---------------------------------------------------------------------------
# fleet / bundle / portable
# ---------------------------------------------------------------------------


def _fleet_file(tmp_path: Path) -> Path:
    path = tmp_path / "fleet.yaml"
    path.write_text(
        "apiVersion: avdfleet/v1\nkind: Fleet\nname: selftest\ntemplates: []\ninstances: []\n",
        encoding="utf-8",
    )
    return path


def test_fleet_plan_apply_validate(env, tmp_path) -> None:
    path = _fleet_file(tmp_path)
    _, plan = _ok(["fleet", "plan", "-f", str(path), "--prune"])
    assert plan["summary"]["create"] == 2
    args, kwargs = env.service("fleet").last_call("plan")
    assert kwargs["prune"] is True
    assert env.service("fleet").last_call("load")[0][0] == path

    _, applied = _ok(["fleet", "apply", "-f", str(path), "--dry-run"])
    assert applied["summary"]["create"] == 2
    _, kwargs = env.service("fleet").last_call("apply")
    assert kwargs["dry_run"] is True

    _, validated = _ok(["fleet", "validate", "-f", str(path)])
    assert validated["ok"] is True


def test_fleet_missing_file_is_usage_error(env) -> None:
    result = invoke(["--json", "fleet", "plan", "-f", str(env.tmp_path / "missing.yaml")])
    assert result.exit_code == 2, output_text(result)


# ---------------------------------------------------------------------------
# top-level apply/plan aliases (supplied prompt, BRIEF section2)
# ---------------------------------------------------------------------------


def test_top_level_plan_alias_routes_to_fleet_plan(env, tmp_path) -> None:
    path = _fleet_file(tmp_path)
    _, payload = _ok(["plan", "-f", str(path), "--prune"])
    assert payload["summary"]["create"] == 2
    args, kwargs = env.service("fleet").last_call("plan")
    assert args[0].name == "selftest"
    assert kwargs["prune"] is True
    assert env.service("fleet").last_call("load")[0][0] == path


def test_top_level_apply_alias_routes_to_fleet_apply(env, tmp_path) -> None:
    path = _fleet_file(tmp_path)
    _, payload = _ok(["apply", "-f", str(path), "--prune", "--dry-run"])
    assert payload["summary"]["create"] == 2
    _, kwargs = env.service("fleet").last_call("apply")
    assert kwargs["prune"] is True
    assert kwargs["dry_run"] is True
    # The nested spellings keep working unchanged.
    _ok(["fleet", "apply", "-f", str(path), "--dry-run"])
    _ok(["fleet", "plan", "-f", str(path)])


def test_top_level_apply_missing_file_is_usage_error(env) -> None:
    result = invoke(["--json", "apply", "-f", str(env.tmp_path / "missing.yaml")])
    assert result.exit_code == 2, output_text(result)
    assert json.loads(result.stdout)["error"]["code"] == "usage_error"


def test_bundle_build_dry_run(env, tmp_path) -> None:
    destination = tmp_path / "portable"
    _, payload = _ok(["bundle", "--offline", "--output", str(destination), "--dry-run"])
    assert payload["offline"] is True
    args, kwargs = env.service("portable").last_call("build")
    assert args[0] == destination
    assert kwargs["offline"] is True
    assert kwargs["dry_run"] is True


def test_portable_checks_verify_relocate_status(env, tmp_path) -> None:
    _, checks = _ok(["portable", "checks", "--root", str(tmp_path)])
    assert checks["ok"] is True
    assert len(checks["checks"]) == 2

    _, verified = _ok(["portable", "verify", str(tmp_path)])
    assert verified["root"] == str(tmp_path)

    _, relocated = _ok(["portable", "relocate", str(tmp_path), "--dry-run"])
    assert relocated["dry_run"] is True

    _, status = _ok(["portable", "status"])
    assert status["portable"] is True
    assert env.service("portable").called("status")


def test_portable_checks_failure_exits_one(monkeypatch, env, tmp_path) -> None:
    _overrides(
        monkeypatch,
        env,
        {
            "portable": ServiceStub(
                checks=lambda: [{"id": "filesystem", "title": "FS", "status": "fail", "detail": "FAT32"}]
            )
        },
    )
    result = invoke(["--json", "portable", "checks", "--root", str(tmp_path)])
    assert result.exit_code == 1, output_text(result)
    assert json.loads(result.stdout)["ok"] is False
