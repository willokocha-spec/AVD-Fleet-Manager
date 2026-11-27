"""``avdfleet install`` / ``uninstall`` / ``repair`` / ``status`` contract.

Everything is dispatched to an in-memory installer stub; the tests assert flag
forwarding, dry-run behaviour, manifest reporting and the documented exit codes
(including **3010** for ``RebootRequiredError`` during ``install``).

The last section covers the supplied-prompt §3.1 flags (``--no-studio``,
``--images``, ``--avd-home``, ``--proxies``, ``--download-proxy``) and the
defensive post-install actions (image downloads and proxy-pool seeding).
"""

from __future__ import annotations

import json
import os

import pytest

from avdfleet.core.errors import InstallerError, RebootRequiredError
from avdfleet.core.models import InstallManifest, StepResult, StepStatus, utcnow

from . import (
    ServiceStub,
    build_stub_container,
    invoke,
    invoke_json,
    output_text,
    prepare_env,
    register_container,
)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    return prepare_env(monkeypatch, tmp_path)


def _failed_manifest() -> InstallManifest:
    manifest = InstallManifest(avdfleet_version="0.1.0", sdk_root="<tmp>/sdk", started_at=utcnow())
    manifest.steps = {
        "sdk.cmdline": StepResult(
            step_id="sdk.cmdline", status=StepStatus.FAILED, message="download failed"
        )
    }
    return manifest


def _with_installer(monkeypatch, env, **methods) -> None:
    container = build_stub_container(
        config=env.config,
        paths=env.paths,
        overrides={"installer": ServiceStub(**methods)},
    )
    register_container(monkeypatch, container)


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def test_install_dry_run_forwards_every_flag(env) -> None:
    sdk_root = env.tmp_path / "custom-sdk"
    result, payload = invoke_json(
        [
            "install",
            "--accept-android-sdk-licenses",
            "--yes",
            "--dry-run",
            "--no-appium",
            "--no-studio",
            "--no-scrcpy",
            "--sdk-root",
            str(sdk_root),
            "--only",
            "sdk.cmdline",
            "--only",
            "tools.jdk,sdk.licenses",
            "--platform-provider",
            "debian",
        ]
    )
    assert result.exit_code == 0, output_text(result)
    assert payload["dry_run"] is True
    assert [step["id"] for step in payload["steps"]] == ["sdk.cmdline", "tools.jdk"]
    installer = env.service("installer")
    assert installer.called("plan")
    assert not installer.called("install")
    options = installer.last_call("plan")[0][0]
    assert options.accept_android_sdk_licenses is True
    assert options.yes is True
    assert options.dry_run is True
    assert options.skip_appium is True
    assert options.skip_studio is True
    assert options.skip_scrcpy is True
    assert options.sdk_root == str(sdk_root)
    assert options.only == ["sdk.cmdline", "tools.jdk", "sdk.licenses"]
    assert options.platform_provider == "debian"


def test_install_success_reports_manifest(env) -> None:
    result, payload = invoke_json(["install", "--accept-android-sdk-licenses", "-y"])
    assert result.exit_code == 0, output_text(result)
    assert payload["ok"] is True
    assert "sdk.cmdline" in payload["steps"]
    installer = env.service("installer")
    assert installer.called("install")
    options = installer.last_call("install")[0][0]
    assert options.accept_android_sdk_licenses is True
    assert options.yes is True


def test_install_reboot_required_exits_3010(monkeypatch, env) -> None:
    _with_installer(
        monkeypatch,
        env,
        install=lambda options=None: (_ for _ in ()).throw(
            RebootRequiredError(
                "a reboot is required to continue after step 'accel.whpx'",
                hint="reboot the host, then run 'avdfleet install --resume'",
            )
        ),
    )
    result = invoke(["--json", "install", "--accept-android-sdk-licenses", "-y"])
    assert result.exit_code == 3010, output_text(result)
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["exit_code"] == 3010
    assert payload["error"]["code"] == "reboot_required"
    assert payload["command"] == "install"
    assert "resume" in payload["error"]["hint"]


def test_install_resume_calls_resume(env) -> None:
    result = invoke(["--json", "install", "--resume"])
    assert result.exit_code == 0, output_text(result)
    installer = env.service("installer")
    assert installer.called("resume")
    assert not installer.called("install")


def test_install_repair_flag_calls_repair(env) -> None:
    result = invoke(["--json", "install", "--repair"])
    assert result.exit_code == 0, output_text(result)
    installer = env.service("installer")
    assert installer.called("repair")
    assert not installer.called("install")


def test_failed_install_exits_one(monkeypatch, env) -> None:
    _with_installer(monkeypatch, env, install=lambda options=None: _failed_manifest())
    result, payload = invoke_json(["install"])
    assert result.exit_code == 1, output_text(result)
    assert payload["ok"] is False
    assert payload["failed_steps"] == ["sdk.cmdline"]


def test_installer_error_exits_environment(monkeypatch, env) -> None:
    _with_installer(
        monkeypatch,
        env,
        install=lambda options=None: (_ for _ in ()).throw(InstallerError("no package manager")),
    )
    result = invoke(["--json", "install"])
    assert result.exit_code == 3, output_text(result)
    assert json.loads(result.stdout)["error"]["code"] == "installer_error"


# ---------------------------------------------------------------------------
# repair / uninstall / status
# ---------------------------------------------------------------------------


def test_repair_command_dry_run(env) -> None:
    result, payload = invoke_json(["repair", "--dry-run"])
    assert result.exit_code == 0, output_text(result)
    assert payload["dry_run"] is True
    installer = env.service("installer")
    assert installer.called("plan")
    options = installer.last_call("plan")[0][0]
    assert options.repair is True


def test_uninstall_forwards_flags(env) -> None:
    result, payload = invoke_json(["uninstall", "--dry-run", "--keep-data"])
    assert result.exit_code == 0, output_text(result)
    assert payload["ok"] is True
    installer = env.service("installer")
    _, kwargs = installer.last_call("uninstall")
    assert kwargs["dry_run"] is True
    assert kwargs["keep_data"] is True


def test_uninstall_failure_exits_one(monkeypatch, env) -> None:
    _with_installer(
        monkeypatch,
        env,
        uninstall=lambda **kwargs: __import__(
            "avdfleet.core.models", fromlist=["OperationResult"]
        ).OperationResult(ok=False, message="cannot remove SDK in use"),
    )
    result = invoke(["--json", "uninstall"])
    assert result.exit_code == 1, output_text(result)


def test_status_reports_installation(env) -> None:
    result, payload = invoke_json(["status"])
    assert result.exit_code == 0, output_text(result)
    assert payload["installed"] is True
    assert payload["host"]["os"] == "windows"
    assert payload["sdk_root"]
    assert env.service("installer").called("status")


def test_status_without_manifest(monkeypatch, env) -> None:
    _with_installer(monkeypatch, env, status=lambda: None)
    result, payload = invoke_json(["status"])
    assert result.exit_code == 0, output_text(result)
    assert payload["installed"] is False
    assert payload["manifest"] is None


# ---------------------------------------------------------------------------
# Supplied-prompt §3.1 flags and post-install actions
# ---------------------------------------------------------------------------

_IMAGE_A = "system-images;android-35;google_apis;x86_64"
_IMAGE_B = "system-images;android-36;google_apis_playstore;x86_64"
_IMAGE_C = "system-images;android-34;google_apis;x86_64"
_PROXY_URL = "http://alice:supersecret@127.0.0.1:8888"


def _completed_manifest() -> InstallManifest:
    manifest = InstallManifest(
        avdfleet_version="0.1.0",
        sdk_root="<tmp>/sdk",
        started_at=utcnow(),
        completed_at=utcnow(),
    )
    manifest.steps = {"sdk.cmdline": StepResult(step_id="sdk.cmdline", status=StepStatus.SUCCEEDED)}
    return manifest


def test_install_dry_run_forwards_spec_flags_to_install_options(env) -> None:
    avd_home = env.tmp_path / "shared-avd"
    proxies_file = env.tmp_path / "proxies.txt"
    proxies_file.write_text("http://127.0.0.1:8080\n", encoding="utf-8")
    result, payload = invoke_json(
        [
            "install",
            "--dry-run",
            "--no-studio",
            "--avd-home",
            str(avd_home),
            "--proxies",
            str(proxies_file),
            "--download-proxy",
            _PROXY_URL,
            "--images",
            _IMAGE_A,
            "--images",
            f"{_IMAGE_B},{_IMAGE_C}",
        ]
    )
    assert result.exit_code == 0, output_text(result)
    assert payload["dry_run"] is True
    installer = env.service("installer")
    assert installer.called("plan")
    assert not installer.called("install")
    options = installer.last_call("plan")[0][0]
    assert options.images == [_IMAGE_A, _IMAGE_B, _IMAGE_C]
    assert options.avd_home == str(avd_home)
    assert options.proxies_file == str(proxies_file)
    assert options.download_proxy == _PROXY_URL
    assert options.skip_studio is True
    assert options.no_studio is True
    # A dry run changes nothing and performs no post-install action.
    assert not env.service("images").called("download")
    assert not env.service("proxies").called("import_text")
    # The credential is never printed, even in the dry-run warning.
    assert "supersecret" not in output_text(result)
    assert "alice:***@" in output_text(result)


def test_install_downloads_requested_images_after_success(env) -> None:
    result, payload = invoke_json(["install", "--yes", "--images", f"{_IMAGE_A},{_IMAGE_B}"])
    assert result.exit_code == 0, output_text(result)
    assert payload["ok"] is True
    downloads = env.service("images").calls_for("download")
    assert [args[0] for args, _ in downloads] == [_IMAGE_A, _IMAGE_B]
    post = payload["post_install"]
    assert post["ok"] is True
    assert [entry["id"] for entry in post["images"]] == [_IMAGE_A, _IMAGE_B]
    assert all(entry["ok"] is True for entry in post["images"])


def test_install_image_download_failure_warns_and_exits_one(monkeypatch, env) -> None:
    def download(image_id, **kwargs):
        if image_id == "bad-image":
            raise RuntimeError("image is not in the catalogue")
        return object()

    container = build_stub_container(
        config=env.config,
        paths=env.paths,
        overrides={"images": ServiceStub(download=download)},
    )
    register_container(monkeypatch, container)
    result, payload = invoke_json(["install", "--images", "bad-image,good-image"])
    assert result.exit_code == 1, output_text(result)
    post = payload["post_install"]
    assert post["ok"] is False
    assert [entry["ok"] for entry in post["images"]] == [False, True]
    assert any("bad-image" in warning for warning in post["warnings"])


def test_install_seeds_proxy_pool_from_file(env) -> None:
    proxies_file = env.tmp_path / "proxies.txt"
    text = "socks5://u:p@127.0.0.1:1080\nhttp://127.0.0.1:8080\n"
    proxies_file.write_text(text, encoding="utf-8")
    result, payload = invoke_json(["install", "--proxies", str(proxies_file)])
    assert result.exit_code == 0, output_text(result)
    args, kwargs = env.service("proxies").last_call("import_text")
    assert args[0] == text
    assert kwargs["source"] == "install"
    proxies = payload["post_install"]["proxies"]
    assert proxies["imported"] == 1
    assert proxies["skipped"] == 0
    assert proxies["file"] == str(proxies_file)


def test_install_missing_images_service_warns_and_exits_one(monkeypatch, env) -> None:
    container = build_stub_container(config=env.config, paths=env.paths, overrides={"images": None})
    register_container(monkeypatch, container)
    result, payload = invoke_json(["install", "--images", _IMAGE_A])
    assert result.exit_code == 1, output_text(result)
    assert payload["ok"] is True  # the install itself succeeded
    post = payload["post_install"]
    assert post["ok"] is False
    assert any("images service" in warning for warning in post["warnings"])
    assert "images service is not available" in output_text(result)


def test_install_missing_proxies_service_warns_and_exits_one(monkeypatch, env) -> None:
    container = build_stub_container(config=env.config, paths=env.paths, overrides={"proxies": None})
    register_container(monkeypatch, container)
    proxies_file = env.tmp_path / "proxies.txt"
    proxies_file.write_text("http://127.0.0.1:8080\n", encoding="utf-8")
    result, payload = invoke_json(["install", "--proxies", str(proxies_file)])
    assert result.exit_code == 1, output_text(result)
    post = payload["post_install"]
    assert post["ok"] is False
    assert any("proxies service" in warning for warning in post["warnings"])


def test_install_download_proxy_is_exported_and_redacted(monkeypatch, env) -> None:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(key, raising=False)
    captured: dict[str, dict[str, str | None]] = {}

    def record_install(options=None):
        captured["env"] = {
            key: os.environ.get(key) for key in ("HTTP_PROXY", "HTTPS_PROXY", "https_proxy")
        }
        captured["options"] = options
        return _completed_manifest()

    _with_installer(monkeypatch, env, install=record_install)
    result, payload = invoke_json(["install", "--download-proxy", _PROXY_URL])
    assert result.exit_code == 0, output_text(result)
    assert payload["ok"] is True
    options = captured["options"]
    assert options.download_proxy == _PROXY_URL
    assert captured["env"] == {
        "HTTP_PROXY": _PROXY_URL,
        "HTTPS_PROXY": _PROXY_URL,
        "https_proxy": _PROXY_URL,
    }
    text = output_text(result)
    assert "supersecret" not in text
    assert "alice:***@" in text
    # The credential is exported for the run only: the environment is restored.
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "https_proxy"):
        assert os.environ.get(key) is None, f"{key} leaked into the caller environment"
