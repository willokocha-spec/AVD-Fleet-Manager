"""Wave 2 integration: real service container against executable fakes.

This is the first end-to-end wiring test: no service is stubbed except the
Android tools themselves, which are the process-boundary fakes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from avdfleet.api.app import create_app
from avdfleet.container import build_container
from avdfleet.core.config import Config
from avdfleet.core.errors import NotFoundError
from avdfleet.core.models import ScriptDefinition, ScriptRunRequest, ScriptType, Template
from avdfleet.core.paths import Paths


@pytest.fixture()
def container_env(fake_env, fake_state, tmp_path: Path):
    fake_state.seed_avds()
    data = tmp_path / "data"
    paths = Paths(
        root=tmp_path,
        portable=False,
        data_dir=data,
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
        sdk_root=fake_env.paths.sdk_root,
        avd_home=fake_env.paths.avd_home,
    ).ensure()
    config = Config(
        data_dir=str(data),
        config_dir=str(tmp_path / "config"),
        cache_dir=str(tmp_path / "cache"),
        log_dir=str(tmp_path / "logs"),
        sdk_root=str(fake_env.paths.sdk_root),
        avd_home=str(fake_env.paths.avd_home),
        auth_required=False,
        max_instances=8,
        port_range_start=6100,
        port_range_end=6198,
        appium_port_start=5300,
        bridge_port_start=7950,
        boot_timeout_s=20,
    )
    container = build_container(config, paths)
    try:
        yield container
    finally:
        container.close()


def test_container_builds_all_services(container_env) -> None:
    c = container_env
    assert c.images().list_images(include_remote=False)
    assert c.avds() is not None
    assert c.runtime() is not None
    assert c.proxies().list_proxies() == []
    assert c.assignments().list_assignments() == []
    assert c.fleet() is not None
    assert c.oobe().matrix().records
    assert c.repository().health()["ok"] is True


def test_container_teardown_keeps_the_shared_appium_server(container_env) -> None:
    """The production container must not stop the fleet-wide Appium server.

    ``avdfleet appium start`` boots the server in a one-shot CLI process and
    exits, so ``Container.close()`` (called from the CLI's exit hook) used to
    kill the server it had just started - every later ``scripts run`` then had
    no Appium to talk to.  Only ``avdfleet appium stop`` ends it.
    """
    appium = container_env.appium()
    started = appium.start()
    assert started.running is True
    try:
        container_env.close()  # exactly what the CLI does when the command exits
        assert appium.status().running is True
    finally:
        appium.stop()
    assert appium.status().running is False


def test_appium_scripts_target_the_managed_server(container_env) -> None:
    """appium_python scripts receive the URL of the server this fleet runs.

    The managed port comes from the Appium port block (the fake config starts
    at 5300), so the historical hard-coded ``4723/wd/hub`` would point every
    script at a different server whenever 4723 is unavailable.
    """
    appium = container_env.appium()
    started = appium.start()
    try:
        assert started.running is True
        assert started.url
        assert started.port != 4723
        definition = ScriptDefinition(
            id="appium-url-probe",
            name="Appium URL probe",
            type=ScriptType.APPIUM_PYTHON,
            content=(
                "import os\n"
                f"expected = {started.url!r}\n"
                "url = os.environ.get('APPIUM_URL', '')\n"
                "print('APPIUM_URL=%s' % url)\n"
                "raise SystemExit(0 if url == expected else 1)\n"
            ),
        )
        scripts = container_env.scripts()
        scripts.save_script(definition)
        run = scripts.run(
            ScriptRunRequest(
                script_id=definition.id,
                instances=["avd-01"],
                concurrency=1,
                timeout_s=60,
            )
        )
        assert run.failed == 0
        assert run.succeeded == 1
    finally:
        appium.stop()


def test_container_api_smoke(container_env) -> None:
    c = container_env
    app = create_app(c.config, c, c.paths)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] in {"ok", "degraded"}
        system = client.get("/api/v1/system")
        assert system.status_code == 200, system.text
        images = client.get("/api/v1/images?include_remote=false")
        assert images.status_code == 200, images.text
        assert isinstance(images.json(), list)
        doctor = client.get("/api/v1/doctor")
        assert doctor.status_code == 200, doctor.text
        assert "checks" in doctor.json()
        matrix = client.get("/api/v1/oobe/matrix")
        assert matrix.status_code == 200, matrix.text
        proxies = client.get("/api/v1/proxies")
        assert proxies.status_code == 200, proxies.text


def test_static_ui_is_served(container_env) -> None:
    c = container_env
    app = create_app(c.config, c, c.paths)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        index = client.get("/")
        assert index.status_code == 200, index.text
        assert "text/html" in index.headers.get("content-type", "")
        assert "<div id=\"root\"" in index.text or "<script" in index.text
        assets = [line for line in index.text.split('"') if line.startswith("/assets/")]
        if assets:
            asset = client.get(assets[0])
            assert asset.status_code == 200, assets[0]
            assert asset.content


def test_seeded_instances_are_listed(container_env) -> None:
    c = container_env
    instances = c.avds().list_instances()
    names = {avd.name for avd in instances}
    assert {"avd-01", "avd-02", "avd-03"} <= names


def test_proxy_create_list_and_mask(container_env) -> None:
    c = container_env
    proxy = c.proxies().add(
        url="http://alice:supersecret@127.0.0.1:8081",
        name="listener-1",
    )
    assert proxy.username == "alice"
    view = c.proxies().to_view(proxy)
    assert view["has_credentials"] is True
    assert "supersecret" not in str(view)
    listed = c.proxies().list_proxies()
    assert len(listed) == 1
    assert "supersecret" not in str(listed[0])
    assignment = c.assignments().assign("avd-01", proxy.id)
    assert assignment.proxy_id == proxy.id
    assert c.assignments().get("avd-01").proxy_id == proxy.id
    c.proxies().delete(proxy.id)
    assert c.proxies().list_proxies() == []
    assert c.assignments().list_assignments() == []


def test_template_and_instance_lifecycle_with_fakes(container_env, fake_state) -> None:
    c = container_env
    installed_packages = set(fake_state.installed())
    images = c.images().list_images(include_remote=False, installed_only=True)
    images = [image for image in images if image.package in installed_packages]
    assert images, "fake SDK should expose at least one installed image"
    image = images[0]
    template = c.avds().save_template(
        Template(
            id="tpl-int",
            name="tpl-int",
            image_id=image.id,
            device_id="pixel_7",
            ram_mb=2048,
        )
    )
    assert template.id == "tpl-int"
    instance = c.avds().create_instance("tpl-int", "avd-int")
    assert instance.name == "avd-int"
    assert c.avds().get_instance("avd-int").name == "avd-int"
    c.avds().delete_instance("avd-int")
    with pytest.raises(NotFoundError):
        c.avds().get_instance("avd-int")
