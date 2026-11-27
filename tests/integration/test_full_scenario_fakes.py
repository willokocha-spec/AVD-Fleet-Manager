"""Full fakes-backed scenario: boot, OOBE, bridge, scripts, screenshot, stop.

This is the closest this host can get to the §7 acceptance scenario without
hardware virtualisation.  Every Android process is a fake, but the real
services, store, bridge, supervisor, script fan-out and screenshot path are
exercised.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from avdfleet.container import build_container
from avdfleet.core.config import Config
from avdfleet.core.models import ScriptDefinition, ScriptRunRequest, ScriptType, Template
from avdfleet.core.paths import Paths


@pytest.fixture()
def live_container(fake_env, fake_state, tmp_path: Path):
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
        port_range_start=6300,
        port_range_end=6398,
        appium_port_start=5350,
        bridge_port_start=7980,
        boot_timeout_s=25,
        boot_poll_interval_s=0.1,
        capacity_reserve_mb=0,
    )
    container = build_container(config, paths)
    try:
        installed = [
            image
            for image in fake_state.installed()
            if image.startswith("system-images;") and "x86_64" in image
        ]
        package = installed[0] if installed else "system-images;android-35;google_apis_playstore;x86_64"
        image = next(
            (
                candidate
                for candidate in container.images().list_images(include_remote=True)
                if candidate.package == package
            ),
            None,
        )
        assert image is not None, f"no image for package {package}"
        container.avds().save_template(
            Template(
                id="tpl-e2e",
                name="tpl-e2e",
                image_id=image.id,
                device_id="pixel_7",
                ram_mb=512,
            )
        )
        container.avds().create_instance("tpl-e2e", "e2e-avd")
        yield container, fake_state
    finally:
        # Do not call runtime.cleanup(all_instances=True) here: under load the
        # fake console kill can block.  Closing the container stops supervisors
        # and listeners; the fake fixture reaps any surviving fake pids.
        container.close()


@pytest.mark.slow
def test_boot_oobe_bridge_script_and_stop(live_container) -> None:
    c, fake_state = live_container

    # A per-instance authenticated proxy (loopback listener is never contacted
    # by the fake emulator; what matters here is credential handling and wiring).
    proxy = c.proxies().add(
        url="http://fleet:supersecret@127.0.0.1:18081",
        name="e2e-listener",
    )
    c.assignments().assign("e2e-avd", proxy.id)

    info = c.runtime().start("e2e-avd", proxy_id=proxy.id)
    assert info.state.value in {"starting", "booting", "running"}
    booted = c.runtime().wait_boot("e2e-avd", timeout_s=25)
    assert booted.boot_completed is True
    assert booted.ports is not None
    assert booted.adb_serial == f"emulator-{booted.ports.console}"

    # Guest isolation pieces exist and never expose the upstream password.
    bridge = c.bridge().get("e2e-avd")
    assert bridge is not None
    assert "supersecret" not in bridge.model_dump_json()
    assert bridge.upstream.startswith("http://")
    assert "@" not in bridge.upstream

    # OOBE detection through the fake guest.
    record = c.oobe().detect("e2e-avd")
    assert record.activity
    assert "setupwizard" in record.activity.lower() or record.status.value in {"verified", "unverified"}

    # Screen capture through adb fallback.
    png = c.screen().capture("e2e-avd")
    assert png.startswith(b"\x89PNG")

    # Real script fan-out through the real executor and fake adb.
    script = c.scripts().save_script(
        ScriptDefinition(
            id="e2e-getprop",
            name="e2e getprop",
            type=ScriptType.ADB_SHELL,
            content="getprop ro.build.version.sdk",
            timeout_s=20,
        )
    )
    run = c.scripts().run(ScriptRunRequest(script_id=script.id, instances=["e2e-avd"], concurrency=1))
    assert run.state.value == "succeeded", run
    assert run.results and "35" in run.results[0].stdout

    # Fake emulator recorded the bridge URL, not the authenticated upstream.
    argv = [entry["argv"] for entry in fake_state.commands("emulator")]
    assert argv, "fake emulator should have been launched"
    flattened = " ".join(str(item) for entry in argv for item in entry)
    assert "supersecret" not in flattened
    assert "-http-proxy" not in flattened
    launch_options = fake_state.read()["avds"]["e2e-avd"]["launch_options"]
    listener_url = str(launch_options.get("http_proxy") or "")
    assert listener_url.startswith("http://e2e-avd:")
    assert "@127.0.0.1:" in listener_url
    assert "supersecret" not in listener_url

    stopped = c.runtime().stop("e2e-avd", force=True)
    assert stopped.state.value in {"stopped", "error"}
    assert c.bridge().get("e2e-avd") is None

    # Proxy credential never lands in API-facing views.
    view = c.proxies().to_view(proxy)
    assert "supersecret" not in str(view)
