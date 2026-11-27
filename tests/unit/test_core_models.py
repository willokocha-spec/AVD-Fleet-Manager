"""Core contract tests: models, errors, config, paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from avdfleet.core.config import Config
from avdfleet.core.errors import AvdFleetError, NotFoundError, ValidationError
from avdfleet.core.models import (
    AvdInfo,
    FleetPlan,
    InstancePorts,
    InstanceState,
    PlanAction,
    PlanChange,
    ProxyConfig,
    ProxyScheme,
    SystemImage,
    Template,
    validate_proxy_url,
)
from avdfleet.core.paths import Paths


def test_config_defaults_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.load(
        environ={
            "AVDFLEET_API_PORT": "9100",
            "AVDFLEET_MAX_INSTANCES": "8",
            "AVDFLEET_HEADLESS": "false",
            "AVDFLEET_ALLOW_ORIGINS": "http://a.test,http://b.test",
            "AVDFLEET_LOG_LEVEL": "debug",
        }
    )
    assert config.api_port == 9100
    assert config.max_instances == 8
    assert config.headless is False
    assert config.allow_origins == ["http://a.test", "http://b.test"]
    assert config.log_level == "DEBUG"


def test_config_toml_and_precedence(tmp_path: Path) -> None:
    path = tmp_path / "avdfleet.toml"
    path.write_text(
        'api_port = 7000\nmax_instances = 12\nallow_origins = ["http://toml.test"]\n',
        encoding="utf-8",
    )
    config = Config.load(path, environ={"AVDFLEET_API_PORT": "7001"})
    assert config.api_port == 7001
    assert config.max_instances == 12
    assert config.allow_origins == ["http://toml.test"]


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        Config(max_instances=65)
    with pytest.raises(ValueError):
        Config(port_range_start=6000, port_range_end=5900)
    config = Config(port_range_start=5555, port_range_end=5600)
    assert config.port_range_start == 5556


def test_portable_paths_stay_inside_root(tmp_path: Path) -> None:
    paths = Paths.for_portable(tmp_path)
    assert paths.portable is True
    assert paths.data_dir == tmp_path.resolve() / "data"
    assert paths.sdk_root == tmp_path.resolve() / "sdk"
    assert paths.db_path.parent == tmp_path.resolve() / "data"
    assert paths.logs_dir.is_dir()
    assert str(paths.sdk_root).startswith(str(tmp_path.resolve()))


def test_installed_paths_env_override(tmp_path: Path) -> None:
    paths = Paths(
        root=tmp_path / "root",
        portable=False,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
        sdk_root=tmp_path / "sdk",
        avd_home=tmp_path / "avd",
    ).ensure()
    assert paths.db_path == (tmp_path / "data" / "avdfleet.db").resolve()
    assert paths.token_path.name == "token"
    assert paths.sdk_root.is_dir()


def test_error_envelope_and_redaction() -> None:
    error = NotFoundError("No instance named 'x'", hint="Use avdfleet instances list")
    payload = error.to_dict()
    assert payload["error"]["code"] == "not_found"
    assert "instance" in payload["error"]["message"]

    secret = AvdFleetError("failed http://user:supersecret@host:80")
    serialized = json.dumps(secret.to_dict())
    assert "supersecret" not in serialized
    assert "***" in serialized


def test_error_http_status() -> None:
    assert NotFoundError("x").http_status == 404
    assert ValidationError("x").http_status == 422


def test_models_roundtrip_and_defaults() -> None:
    image = SystemImage(
        id="system-images;android-35;google_apis_playstore;x86_64",
        api_level=35,
        tag="google_apis_playstore",
        abi="x86_64",
    )
    dumped = image.model_dump(mode="json")
    assert SystemImage.model_validate(dumped) == image
    assert image.installed is False

    template = Template(id="tpl", name="Play 35", image_id=image.id)
    assert template.headless is True
    assert template.disk_estimate_mb > 0

    avd = AvdInfo(name="avd-01", state=InstanceState.RUNNING)
    assert avd.state.active is True
    assert InstanceState.STOPPED.active is False
    assert InstancePorts(console=5554, adb=5555).adb == 5555


def test_proxy_secret_str_is_masked() -> None:
    proxy = ProxyConfig(
        id="p1",
        scheme=ProxyScheme.HTTP,
        host="127.0.0.1",
        port=8080,
        username="alice",
        password="hunter2",
    )
    dumped = proxy.model_dump_json()
    assert "hunter2" not in dumped
    assert "**********" in dumped
    assert "hunter2" not in repr(proxy)
    assert proxy.has_credentials is True
    assert proxy.url(reveal=True) == "http://alice:hunter2@127.0.0.1:8080"
    assert proxy.url() == "http://alice:***@127.0.0.1:8080"


def test_validate_proxy_url() -> None:
    scheme, host, port, username, password = validate_proxy_url(
        "socks5://alice:hunter2@127.0.0.1:1080"
    )
    assert scheme is ProxyScheme.SOCKS5
    assert (host, port, username, password) == ("127.0.0.1", 1080, "alice", "hunter2")
    with pytest.raises(ValueError):
        validate_proxy_url("ftp://host:21")
    scheme, host, port, _, _ = validate_proxy_url("proxy.example.com:3128")
    assert (scheme, host, port) == (ProxyScheme.HTTP, "proxy.example.com", 3128)


def test_fleet_plan_summary_does_not_recurse() -> None:
    plan = FleetPlan(
        spec_name="demo",
        changes=[
            PlanChange(action=PlanAction.CREATE, kind="instance", name="a"),
            PlanChange(action=PlanAction.NOOP, kind="instance", name="b"),
        ],
    )
    assert plan.summary == {"create": 1, "noop": 1}
    assert FleetPlan.model_validate(plan.model_dump()).summary == {"create": 1, "noop": 1}
