"""Unit tests for the emulator runtime manager (N1).

Everything is faked at the process boundary: scripted ``subprocess`` handles, a
scripted ``AdbRunner``, a fake emulator console, a fake AVD service and an
injected capacity service.  No SDK, emulator, network or real sleep is involved
(contracts/fakes-spec.md §7).
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from avdfleet.core.config import Config
from avdfleet.core.errors import (
    BootTimeoutError,
    CapacityError,
    ConflictError,
    InstanceError,
    NotFoundError,
    ValidationError,
)
from avdfleet.core.events import EventBus, EventType
from avdfleet.core.models import (
    AvdInfo,
    BridgeConfig,
    GpuMode,
    InstancePorts,
    InstanceState,
    ProxyConfig,
    ProxyScheme,
    Template,
)
from avdfleet.core.paths import Paths
from avdfleet.core.ports import PortAllocator
from avdfleet.platform.avd.configini import load_values
from avdfleet.platform.runtime import (
    AdbDevice,
    AdbResult,
    CapacityService,
    ProcessSupervisor,
    RestartPolicy,
    RuntimeManagerService,
    build_emulator_argv,
    resolve_gpu_mode,
    screen_endpoint,
    validate_proxy_upstream,
)
from avdfleet.platform.runtime.launch import build_launch_spec

IMAGE_ID = "system-images;android-35;google_apis_playstore;x86_64"
FIXED_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Process fakes
# ---------------------------------------------------------------------------


class FakeProcess:
    """Stand-in for ``subprocess.Popen``."""

    _next_pid = 900000

    def __init__(self) -> None:
        FakeProcess._next_pid += 1
        self.pid = FakeProcess._next_pid
        self._code: int | None = None
        self.terminated = False
        self.killed = False
        self.signals: list[int] = []
        self.exit_on_terminate = False
        self.exit_on_kill = True

    def poll(self) -> int | None:
        return self._code

    def wait(self, timeout: float | None = None) -> int:
        return self._code if self._code is not None else 0

    def terminate(self) -> None:
        self.terminated = True
        if self.exit_on_terminate:
            self._code = -15

    def kill(self) -> None:
        self.killed = True
        if self.exit_on_kill:
            self._code = -9

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)

    def exit(self, code: int = 0) -> None:
        self._code = code


class FakeProcessFactory:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.processes: list[FakeProcess] = []
        self.exit_immediately: int | None = None

    def __call__(
        self,
        argv: Any,
        *,
        env: Any,
        cwd: Any = None,
        log_path: Any = None,
    ) -> FakeProcess:
        process = FakeProcess()
        self.processes.append(process)
        self.calls.append(
            {
                "argv": [str(item) for item in argv],
                "env": dict(env),
                "cwd": cwd,
                "log_path": log_path,
            }
        )
        if self.exit_immediately is not None:
            process.exit(self.exit_immediately)
        return process

    @property
    def last(self) -> dict[str, Any]:
        return self.calls[-1]


class FakeClock:
    """Monotonic clock that advances when the injected sleeper is called."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += max(float(seconds), 0.001)


# ---------------------------------------------------------------------------
# adb / console fakes
# ---------------------------------------------------------------------------


class FakeAdb:
    def __init__(self, *, boot_after: int = 0, fail_boot: bool = False) -> None:
        self.boot_after = boot_after
        self.fail_boot = fail_boot
        self.getprop_calls = 0
        self.pm_calls = 0
        self.devices_list: list[AdbDevice] = []
        self.emu_calls: list[tuple[str, str]] = []
        self.shell_calls: list[tuple[str, str]] = []

    def devices(self, *, timeout_s: float = 5.0) -> list[AdbDevice]:
        return list(self.devices_list)

    def shell(self, serial: str, command: str, *, timeout_s: float = 30.0) -> AdbResult:
        self.shell_calls.append((serial, command))
        if "getprop" in command:
            self.getprop_calls += 1
            if self.fail_boot:
                return AdbResult(0, "0")
            return AdbResult(0, "1" if self.getprop_calls > self.boot_after else "0")
        if command.startswith("pm path"):
            self.pm_calls += 1
            if self.fail_boot:
                return AdbResult(0, "")
            booted = self.getprop_calls > self.boot_after
            return AdbResult(0, "package:/data/app/android/base.apk" if booted else "")
        return AdbResult(0, "")

    def emu(self, serial: str, command: str, *, timeout_s: float = 30.0) -> AdbResult:
        self.emu_calls.append((serial, command))
        return AdbResult(0, "OK")


class FakeConsoleFactory:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.commands: list[str] = []
        self.connected: list[int] = []
        self.on_kill: Any = None

    def __call__(self) -> Any:
        factory = self

        class _Console:
            def connect(self, port: int, *, timeout_s: float = 5.0) -> None:
                if factory.fail:
                    raise RuntimeError("console unavailable")
                factory.connected.append(int(port))

            def command(self, command: str) -> str:
                factory.commands.append(command)
                if factory.fail:
                    raise RuntimeError("console unavailable")
                if command.strip() == "kill" and factory.on_kill is not None:
                    factory.on_kill()
                if command.startswith("snapshot list"):
                    return ""
                return "OK"

            def close(self) -> None:
                return None

        return _Console()


# ---------------------------------------------------------------------------
# AVD service / backend fakes
# ---------------------------------------------------------------------------


class FakeRuntimeBackend:
    name = "fake"

    def __init__(self, avds: FakeAvds) -> None:
        self.avds = avds

    def executable(self, tool: str) -> Path:
        return Path("/fake/sdk") / tool

    def environment(self) -> dict[str, str]:
        return {"ANDROID_AVD_HOME": str(self.avds.paths.avd_home)}


class FakeAvds:
    """Minimal duck-typed ``AvdManagerService`` for the runtime tests."""

    def __init__(self, tmp_path: Path, config: Config | None = None) -> None:
        self.paths = make_paths(tmp_path)
        self.config = config or make_config()
        self.instances: dict[str, AvdInfo] = {}
        self.templates: dict[str, Template] = {}
        self.backend = FakeRuntimeBackend(self)

    # -- paths ----------------------------------------------------------
    @property
    def avd_home(self) -> Path:
        return Path(self.paths.avd_home)

    def avd_dir(self, name: str) -> Path:
        return self.avd_home / f"{name}.avd"

    def config_path(self, name: str) -> Path:
        return self.avd_dir(name) / "config.ini"

    def read_config(self, name: str) -> dict[str, str]:
        return load_values(self.config_path(name))

    # -- instances ------------------------------------------------------
    def add_instance(
        self,
        name: str,
        *,
        template_id: str | None = "tpl",
        ram_mb: int = 1024,
    ) -> AvdInfo:
        directory = self.avd_dir(name)
        directory.mkdir(parents=True, exist_ok=True)
        values = {
            "image.sysdir.1": "system-images/android-35/google_apis_playstore/x86_64/",
            "tag.id": "google_apis_playstore",
            "abi.type": "x86_64",
            "hw.device.name": "pixel_7",
            "hw.ramSize": str(ram_mb),
            "vm.heapSize": "256",
            "hw.cpu.ncore": "1",
            "hw.gpu.mode": "swiftshader_indirect",
        }
        (directory / "config.ini").write_text(
            "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
            encoding="utf-8",
        )
        (self.avd_home / f"{name}.ini").write_text(
            f"path={directory}\npath.rel=avd/{name}.avd\n", encoding="utf-8"
        )
        info = AvdInfo(
            name=name,
            template_id=template_id,
            image_id=IMAGE_ID,
            device_id="pixel_7",
            state=InstanceState.STOPPED,
            config=dict(values),
        )
        self.instances[name] = info
        return info

    def get_instance(self, name: str) -> AvdInfo:
        if name not in self.instances:
            raise NotFoundError(f"instance {name!r} not found")
        stored = self.instances[name]
        return stored.model_copy(update={"config": self.read_config(name)})

    def list_instances(self) -> list[AvdInfo]:
        return [self.get_instance(name) for name in sorted(self.instances)]

    def get_template(self, template_id: str) -> Template:
        if template_id not in self.templates:
            raise NotFoundError(f"template {template_id!r} not found")
        return self.templates[template_id]


# ---------------------------------------------------------------------------
# Proxy fakes
# ---------------------------------------------------------------------------


class FakeProxyPool:
    def __init__(self, proxies: list[ProxyConfig]) -> None:
        self.proxies = {proxy.id: proxy for proxy in proxies}

    def get(self, proxy_id: str) -> ProxyConfig:
        if proxy_id not in self.proxies:
            raise NotFoundError(f"proxy {proxy_id!r} not found")
        return self.proxies[proxy_id]


class FakeBridge:
    def __init__(self) -> None:
        self.ensured: list[tuple[str, str, int]] = []
        self.stopped: list[str] = []
        self.tokens: dict[str, str] = {}

    def ensure(
        self,
        instance: str,
        proxy: ProxyConfig,
        *,
        listen_port: int | None = None,
        local_auth_token: str | None = None,
    ) -> BridgeConfig:
        port = 7900 + len(self.ensured)
        self.ensured.append((instance, proxy.id, port))
        if local_auth_token is not None:
            self.tokens[instance] = local_auth_token
        return BridgeConfig(
            instance=instance,
            listen_host="127.0.0.1",
            listen_port=port,
            upstream=proxy.host,
            mode="http",
        )

    def local_proxy_credentials(self, instance: str) -> tuple[str, str] | None:
        token = self.tokens.get(instance)
        return (instance, token) if token else None

    def local_proxy_url(self, instance: str) -> str | None:
        token = self.tokens.get(instance)
        if token is None:
            return None
        return f"http://{instance}:{token}@127.0.0.1:7900"

    def stop(self, instance: str) -> None:
        self.stopped.append(instance)
        self.tokens.pop(instance, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_paths(tmp_path: Path) -> Paths:
    return Paths(
        root=tmp_path,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
        sdk_root=tmp_path / "sdk",
        avd_home=tmp_path / "avd",
    ).ensure()


def make_config(**overrides: Any) -> Config:
    values: dict[str, Any] = {
        "max_instances": 4,
        "default_ram_mb": 1024,
        "default_heap_mb": 256,
        "default_cpu_cores": 1,
        "capacity_reserve_mb": 0,
        "port_range_start": 5600,
        "port_range_end": 5620,
        "appium_port_start": 14723,
        "boot_timeout_s": 5,
        "boot_poll_interval_s": 0.0,
        "shutdown_timeout_s": 1.0,
    }
    values.update(overrides)
    return Config(**values)


def make_capacity(
    config: Config,
    paths: Paths,
    allocator: PortAllocator,
    avds: FakeAvds,
    *,
    available_mb: int = 6144,
    total_mb: int = 8192,
    disk_free_gb: int = 100,
    cpus: int = 8,
) -> CapacityService:
    return CapacityService(
        config=config,
        paths=paths,
        port_allocator=allocator,
        instances=avds.list_instances,
        virtual_memory=lambda: SimpleNamespace(
            total=total_mb * 1024 * 1024, available=available_mb * 1024 * 1024
        ),
        disk_usage=lambda target: SimpleNamespace(free=disk_free_gb * 1024 * 1024 * 1024),
        cpu_count=lambda: cpus,
    )


class RuntimeHarness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        config: Config | None = None,
        adb: FakeAdb | None = None,
        process_factory: FakeProcessFactory | None = None,
        console_factory: FakeConsoleFactory | None = None,
        capacity: CapacityService | None = None,
        proxy_pool: Any = None,
        bridge: Any = None,
        direct_proxy_enabled: bool = False,
        events: Any = None,
        auto_supervision: bool = False,
    ) -> None:
        self.tmp_path = tmp_path
        self.config = config or make_config()
        self.avds = FakeAvds(tmp_path, self.config)
        self.paths = self.avds.paths
        self.adb = adb or FakeAdb()
        self.factory = process_factory or FakeProcessFactory()
        self.console = console_factory or FakeConsoleFactory()
        self.clock = FakeClock()
        self.events = events if events is not None else EventBus()
        self.allocator = PortAllocator(
            start=self.config.port_range_start,
            end=self.config.port_range_end,
            appium_start=self.config.appium_port_start,
        )
        self.capacity = capacity or make_capacity(
            self.config, self.paths, self.allocator, self.avds
        )
        self.proxy_pool = proxy_pool
        self.bridge = bridge
        self.runtime = RuntimeManagerService(
            self.avds,
            backend=self.avds.backend,
            config=self.config,
            paths=self.paths,
            events=self.events,
            adb=self.adb,
            process_factory=self.factory,
            console_factory=self.console,
            capacity=self.capacity,
            port_allocator=self.allocator,
            proxy_pool=proxy_pool,
            bridge=bridge,
            direct_proxy_enabled=direct_proxy_enabled,
            auto_supervision=auto_supervision,
            clock=self.clock,
            sleep=self.clock.sleep,
            now=lambda: FIXED_NOW,
        )

    def add(self, name: str = "avd-01", **kwargs: Any) -> AvdInfo:
        return self.avds.add_instance(name, **kwargs)

    def start(self, name: str = "avd-01", **kwargs: Any) -> AvdInfo:
        return self.runtime.start(name, **kwargs)

    def booted(self, name: str = "avd-01") -> AvdInfo:
        self.runtime.start(name)
        return self.runtime.wait_boot(name)

    def events_of(self, event_type: str) -> list[Any]:
        return [event for event in self.events.history() if event.type == event_type]


@pytest.fixture()
def harness(tmp_path: Path) -> RuntimeHarness:
    return RuntimeHarness(tmp_path)


# ---------------------------------------------------------------------------
# argv / launch construction
# ---------------------------------------------------------------------------


def test_build_argv_headless() -> None:
    ports = InstancePorts(console=5600, adb=5601, grpc=8622)
    argv = build_emulator_argv(
        Path("emulator"), "avd-01", ports, headless=True, gpu_mode="host"
    )
    assert argv[:5] == ["emulator", "-avd", "avd-01", "-port", "5600"]
    assert "-no-window" in argv
    assert "-no-audio" in argv
    assert "-gpu" in argv and argv[argv.index("-gpu") + 1] == "swiftshader_indirect"
    assert "-grpc" in argv and argv[argv.index("-grpc") + 1] == "8622"
    assert "-grpc-use-token" in argv
    assert "-no-snapshot-save" in argv
    assert "-wipe-data" not in argv


def test_build_argv_visible_uses_configured_gpu() -> None:
    ports = InstancePorts(console=5600, adb=5601, grpc=None)
    argv = build_emulator_argv(Path("emulator"), "avd-01", ports, headless=False, gpu_mode=GpuMode.HOST)
    assert "-no-window" not in argv
    assert argv[argv.index("-gpu") + 1] == "host"
    assert "-grpc" not in argv


def test_build_argv_wipe_and_proxy() -> None:
    ports = InstancePorts(console=5600, adb=5601)
    argv = build_emulator_argv(
        Path("emulator"),
        "avd-01",
        ports,
        wipe_data=True,
        proxy_upstream="http://127.0.0.1:7900",
    )
    assert "-wipe-data" in argv
    # A wipe must also disable snapshot restore: a quickboot snapshot from an
    # earlier boot still holds the provisioned userdata and would undo the wipe.
    assert "-no-snapshot" in argv
    assert argv[argv.index("-http-proxy") + 1] == "http://127.0.0.1:7900"


def test_gpu_mode_resolution() -> None:
    assert resolve_gpu_mode(True, "auto") == "swiftshader_indirect"
    assert resolve_gpu_mode(True, GpuMode.SWIFTSHADER) == "swiftshader"
    assert resolve_gpu_mode(False, "host") == "host"
    with pytest.raises(ValidationError):
        resolve_gpu_mode(True, "vulkan")


def test_proxy_upstream_rejects_credentials() -> None:
    with pytest.raises(InstanceError):
        validate_proxy_upstream("http://alice:hunter2@127.0.0.1:8080")
    with pytest.raises(InstanceError):
        build_emulator_argv(
            Path("emulator"),
            "avd-01",
            InstancePorts(console=5600, adb=5601),
            proxy_upstream="http://alice:hunter2@127.0.0.1:8080",
        )
    assert validate_proxy_upstream("127.0.0.1:8080") == "http://127.0.0.1:8080"


def test_screen_endpoint() -> None:
    assert screen_endpoint(InstancePorts(console=5600, adb=5601, grpc=8622)) == "127.0.0.1:8622"
    assert screen_endpoint(InstancePorts(console=5600, adb=5601)) is None


def test_launch_spec_redacts_argv() -> None:
    spec = build_launch_spec(
        Path("emulator"),
        "avd-01",
        InstancePorts(console=5600, adb=5601, grpc=8622),
        proxy_upstream="http://127.0.0.1:7900",
    )
    assert spec.env == {}
    assert spec.redacted_command.startswith("emulator -avd avd-01")
    assert "hunter2" not in spec.redacted_command


def test_boot_serial_parsing() -> None:
    from avdfleet.platform.runtime.boot import boot_serial, parse_devices

    assert boot_serial(InstancePorts(console=5600, adb=5601)) == "emulator-5600"
    devices = parse_devices(
        "List of devices attached\n"
        "emulator-5600\tdevice product:sdk_gphone64_x86_64 transport_id:1\n"
        "emulator-5602\toffline\n"
    )
    assert [device.serial for device in devices] == ["emulator-5600", "emulator-5602"]
    assert devices[0].console_port == 5600
    assert devices[1].state == "offline"


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


def test_start_launches_process_and_persists(harness: RuntimeHarness) -> None:
    harness.add()
    info = harness.start()
    assert info.state is InstanceState.STARTING
    assert info.pid is not None
    assert info.ports is not None and info.ports.console % 2 == 0
    assert info.adb_serial == f"emulator-{info.ports.console}"
    assert len(harness.factory.calls) == 1
    call = harness.factory.last
    assert call["argv"][:3] == ["\\fake\\sdk\\emulator", "-avd", "avd-01"] or call["argv"][:3] == [
        "/fake/sdk/emulator",
        "-avd",
        "avd-01",
    ]
    assert call["env"]["ANDROID_AVD_HOME"] == str(harness.paths.avd_home)
    assert Path(call["cwd"]) == Path(harness.paths.avd_home)
    states = [event.payload["state"] for event in harness.events_of(EventType.INSTANCE_STATE)]
    assert states == ["starting"]
    state_file = harness.paths.data_dir / "runtime-state.json"
    assert state_file.exists()
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["instances"]["avd-01"]["state"] == "starting"
    assert payload["instances"]["avd-01"]["pid"] is not None


def test_start_dry_run_does_not_spawn(harness: RuntimeHarness) -> None:
    harness.add()
    info = harness.start(dry_run=True)
    assert info.state is InstanceState.STOPPED
    assert harness.factory.calls == []
    assert "launch.argv" in info.config
    assert "-avd avd-01" in info.config["launch.argv"]
    assert harness.allocator.capacity()["in_use"] == 0


def test_start_duplicate_conflict(harness: RuntimeHarness) -> None:
    harness.add()
    harness.start()
    with pytest.raises(ConflictError):
        harness.start()


def test_start_immediate_exit_marks_error(harness: RuntimeHarness) -> None:
    harness.add()
    harness.factory.exit_immediately = 3
    with pytest.raises(InstanceError) as info:
        harness.start()
    assert "exited immediately" in str(info.value)
    assert harness.allocator.capacity()["in_use"] == 0
    status = harness.runtime.status("avd-01")
    assert status.state is InstanceState.ERROR
    assert "exited immediately" in status.error
    error_events = harness.events_of(EventType.ERROR)
    assert error_events and error_events[0].payload["instance"] == "avd-01"


def test_start_capacity_rejection(tmp_path: Path) -> None:
    config = make_config()
    harness = RuntimeHarness(tmp_path, config=config)
    harness.capacity = make_capacity(
        config, harness.paths, harness.allocator, harness.avds, available_mb=512
    )
    harness.runtime._capacity = harness.capacity
    harness.add()
    with pytest.raises(CapacityError):
        harness.start()
    assert harness.factory.calls == []
    assert harness.allocator.capacity()["in_use"] == 0


def test_start_port_uniqueness(harness: RuntimeHarness) -> None:
    harness.add("avd-01")
    harness.add("avd-02")
    first = harness.start("avd-01")
    second = harness.start("avd-02")
    assert first.ports is not None and second.ports is not None
    assert first.ports.console != second.ports.console
    assert harness.allocator.capacity()["in_use"] == 2


def test_start_without_image_rejected(
    harness: RuntimeHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No image id, no template and no config.ini image path => reject."""
    monkeypatch.setattr(
        RuntimeManagerService, "_derive_image_id", staticmethod(lambda info: "")
    )
    info = harness.add()
    harness.avds.instances[info.name] = info.model_copy(
        update={"image_id": None, "template_id": None}
    )
    with pytest.raises(ValidationError):
        harness.start()


def test_start_adopted_avd_derives_image_from_config(
    harness: RuntimeHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An AVD created by ``avdmanager`` is startable via its config.ini image path.

    Adopted AVDs have no ``image_id`` and no template row, so ``start`` used to
    reject them with "has no system image" even though the image was installed.
    The id is now derived from ``image.sysdir.1`` (the SDK package path).
    """
    monkeypatch.setattr(
        RuntimeManagerService,
        "_derive_image_id",
        staticmethod(lambda info: "system-images;android-34;google_apis;x86_64"),
    )
    info = harness.add()
    harness.avds.instances[info.name] = info.model_copy(
        update={"image_id": None, "template_id": None}
    )
    started = harness.start()
    assert started.ports is not None


def test_derive_image_id_normalises_sysdir() -> None:
    info = AvdInfo(
        name="adopted",
        config={"image.sysdir.1": "system-images\\android-34\\google_apis\\x86_64\\"},
    )
    assert RuntimeManagerService._derive_image_id(info) == (
        "system-images;android-34;google_apis;x86_64"
    )


def test_derive_image_id_rejects_non_system_image_paths() -> None:
    assert RuntimeManagerService._derive_image_id(AvdInfo(name="x")) == ""
    assert (
        RuntimeManagerService._derive_image_id(
            AvdInfo(name="x", config={"image.sysdir.1": "some\\other\\place"})
        )
        == ""
    )


def test_start_unknown_instance(harness: RuntimeHarness) -> None:
    with pytest.raises(NotFoundError):
        harness.start("nope")


# ---------------------------------------------------------------------------
# Proxy integration
# ---------------------------------------------------------------------------


def test_proxy_uses_bridge_and_never_leaks_credentials(
    harness: RuntimeHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AVDFLEET_TOKEN", "daemon-token")
    monkeypatch.setenv("AVDFLEET_VAULT_PASSPHRASE", "vault-pass")
    proxy = ProxyConfig(
        id="p1",
        scheme=ProxyScheme.HTTP,
        host="127.0.0.1",
        port=8081,
        username="alice",
        password="hunter2",
    )
    harness.proxy_pool = FakeProxyPool([proxy])
    bridge = FakeBridge()
    harness.runtime._proxy_pool = harness.proxy_pool
    harness.runtime._bridge = bridge
    harness.add()
    info = harness.start(proxy_id="p1")
    argv = " ".join(harness.factory.last["argv"])
    assert "hunter2" not in argv
    assert "alice" not in argv
    assert "-http-proxy" not in argv
    assert bridge.ensured and bridge.ensured[0][:2] == ("avd-01", "p1")
    token = bridge.tokens["avd-01"]
    assert token
    assert harness.factory.last["env"]["http_proxy"] == (
        f"http://avd-01:{token}@127.0.0.1:7900"
    )
    assert token not in argv
    assert "AVDFLEET_TOKEN" not in harness.factory.last["env"]
    assert "AVDFLEET_VAULT_PASSPHRASE" not in harness.factory.last["env"]
    assert int(harness.factory.last["env"]["ADB_LOCAL_TRANSPORT_MAX_PORT"]) >= 5681
    assert info.proxy_id == "p1"
    harness.runtime.stop("avd-01", force=True)
    assert bridge.stopped == ["avd-01", "avd-01"]


def test_proxy_tokens_are_unique_per_instance(harness: RuntimeHarness) -> None:
    proxy = ProxyConfig(id="p1", host="127.0.0.1", port=8081)
    harness.proxy_pool = FakeProxyPool([proxy])
    bridge = FakeBridge()
    harness.runtime._proxy_pool = harness.proxy_pool
    harness.runtime._bridge = bridge
    harness.add("avd-01")
    harness.add("avd-02")
    harness.start("avd-01", proxy_id="p1")
    first = bridge.tokens["avd-01"]
    first_env = harness.factory.last["env"]["http_proxy"]
    harness.start("avd-02", proxy_id="p1")
    second = bridge.tokens["avd-02"]
    second_env = harness.factory.last["env"]["http_proxy"]
    assert first != second
    assert first in first_env and second in second_env
    assert "-http-proxy" not in harness.factory.last["argv"]


def test_direct_proxy_with_credentials_refused(harness: RuntimeHarness) -> None:
    proxy = ProxyConfig(
        id="p1",
        scheme=ProxyScheme.HTTP,
        host="127.0.0.1",
        port=8081,
        username="alice",
        password="hunter2",
    )
    harness.runtime._proxy_pool = FakeProxyPool([proxy])
    harness.runtime.direct_proxy_enabled = True
    harness.add()
    with pytest.raises(InstanceError):
        harness.start(proxy_id="p1")
    assert harness.factory.calls == []


def test_proxy_without_bridge_refused(harness: RuntimeHarness) -> None:
    proxy = ProxyConfig(id="p1", host="127.0.0.1", port=8081)
    harness.runtime._proxy_pool = FakeProxyPool([proxy])
    harness.add()
    with pytest.raises(InstanceError):
        harness.start(proxy_id="p1")


def test_unknown_proxy_not_found(harness: RuntimeHarness) -> None:
    harness.runtime._proxy_pool = FakeProxyPool([])
    harness.add()
    with pytest.raises(NotFoundError):
        harness.start(proxy_id="missing")


# ---------------------------------------------------------------------------
# Boot detection
# ---------------------------------------------------------------------------


def test_wait_boot_success(harness: RuntimeHarness) -> None:
    harness.add()
    harness.start()
    info = harness.runtime.wait_boot("avd-01", timeout_s=1)
    assert info.state is InstanceState.RUNNING
    assert info.boot_completed is True
    assert info.uptime_seconds is not None
    assert harness.adb.getprop_calls >= 1
    assert harness.adb.pm_calls >= 1
    boots = harness.events_of(EventType.INSTANCE_BOOT)
    assert boots and boots[0].payload["instance"] == "avd-01"
    states = [event.payload["state"] for event in harness.events_of(EventType.INSTANCE_STATE)]
    assert states[0] == "starting"
    assert "booting" in states
    assert states[-1] == "running"


def test_wait_boot_timeout_stops_and_errors(tmp_path: Path) -> None:
    harness = RuntimeHarness(tmp_path, adb=FakeAdb(fail_boot=True))
    harness.add()
    harness.start()
    with pytest.raises(BootTimeoutError) as info:
        harness.runtime.wait_boot("avd-01", timeout_s=0.05)
    assert "did not finish booting" in str(info.value)
    status = harness.runtime.status("avd-01")
    assert status.state is InstanceState.ERROR
    assert harness.allocator.capacity()["in_use"] == 0
    process = harness.factory.processes[0]
    assert process.terminated or process.killed
    errors = harness.events_of(EventType.ERROR)
    assert errors


def test_wait_boot_not_running(harness: RuntimeHarness) -> None:
    harness.add()
    with pytest.raises(InstanceError):
        harness.runtime.wait_boot("avd-01")


def test_wait_boot_process_exit(harness: RuntimeHarness) -> None:
    harness.add()
    harness.start()
    harness.factory.processes[0].exit(2)
    with pytest.raises(InstanceError):
        harness.runtime.wait_boot("avd-01", timeout_s=0.05)


# ---------------------------------------------------------------------------
# Stop / restart
# ---------------------------------------------------------------------------


def test_stop_graceful_console_kill(harness: RuntimeHarness) -> None:
    harness.add()
    harness.start()
    process = harness.factory.processes[0]
    harness.console.on_kill = lambda: process.exit(0)
    info = harness.runtime.stop("avd-01")
    assert info.state is InstanceState.STOPPED
    assert "kill" in harness.console.commands
    assert process.killed is False
    assert harness.allocator.capacity()["in_use"] == 0
    assert harness.runtime.status("avd-01").state is InstanceState.STOPPED


def test_stop_force_terminates(harness: RuntimeHarness) -> None:
    harness.add()
    harness.start()
    process = harness.factory.processes[0]
    process.exit_on_terminate = True
    info = harness.runtime.stop("avd-01", force=True)
    assert info.state is InstanceState.STOPPED
    assert process.terminated is True
    assert "kill" not in harness.console.commands


def test_stop_escalates_to_kill(harness: RuntimeHarness) -> None:
    harness.add()
    harness.start()
    process = harness.factory.processes[0]
    process.exit_on_terminate = False
    process.exit_on_kill = True
    info = harness.runtime.stop("avd-01", force=True)
    assert info.state is InstanceState.STOPPED
    assert process.terminated is True
    assert process.killed is True


def test_stop_idempotent_when_stopped(harness: RuntimeHarness) -> None:
    harness.add()
    info = harness.runtime.stop("avd-01")
    assert info.state is InstanceState.STOPPED
    assert harness.allocator.capacity()["in_use"] == 0


def test_restart(harness: RuntimeHarness) -> None:
    harness.add()
    harness.start()
    first = harness.factory.processes[0]
    first.exit_on_terminate = True
    info = harness.runtime.restart("avd-01")
    assert info.state is InstanceState.STARTING
    assert len(harness.factory.processes) == 2
    assert harness.factory.processes[1] is not first


# ---------------------------------------------------------------------------
# Status / reconciliation
# ---------------------------------------------------------------------------


def test_status_detects_unexpected_exit(harness: RuntimeHarness) -> None:
    harness.add()
    harness.start()
    harness.factory.processes[0].exit(7)
    info = harness.runtime.status("avd-01")
    assert info.state is InstanceState.ERROR
    assert "exit code 7" in info.error
    assert harness.runtime.list_running() == []
    assert harness.allocator.capacity()["in_use"] == 0


def test_status_reconciles_external_pid(harness: RuntimeHarness) -> None:
    harness.add()
    harness.runtime._state_store.save(
        AvdInfo(
            name="avd-01",
            state=InstanceState.RUNNING,
            pid=os.getpid(),
            ports=InstancePorts(console=5600, adb=5601),
            image_id=IMAGE_ID,
        )
    )
    info = harness.runtime.status("avd-01")
    assert info.state is InstanceState.RUNNING
    running = harness.runtime.list_running()
    assert [item.name for item in running] == ["avd-01"]


def test_status_downgrades_dead_external_pid(harness: RuntimeHarness) -> None:
    harness.add()
    harness.runtime._state_store.save(
        AvdInfo(
            name="avd-01",
            state=InstanceState.RUNNING,
            pid=999_999_999,
            ports=InstancePorts(console=5600, adb=5601),
        )
    )
    info = harness.runtime.status("avd-01")
    assert info.state is InstanceState.STOPPED


def test_list_running_after_boot(harness: RuntimeHarness) -> None:
    harness.add()
    harness.booted()
    running = harness.runtime.list_running()
    assert [item.name for item in running] == ["avd-01"]
    assert running[0].state is InstanceState.RUNNING
    assert running[0].grpc_endpoint is not None


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def test_snapshot_flow(harness: RuntimeHarness) -> None:
    harness.add()
    harness.booted()
    snapshot = harness.runtime.save_snapshot("avd-01", "snap1", golden=True)
    assert snapshot.name == "snap1"
    assert snapshot.is_golden is True
    assert any(command.startswith("snapshot save snap1") for command in harness.console.commands)
    listed = harness.runtime.list_snapshots("avd-01")
    assert [item.name for item in listed] == ["snap1"]
    loaded = harness.runtime.load_snapshot("avd-01", "snap1")
    assert loaded.name == "snap1"
    assert any(command.startswith("snapshot load snap1") for command in harness.console.commands)
    harness.runtime.delete_snapshot("avd-01", "snap1")
    assert any(command.startswith("snapshot delete snap1") for command in harness.console.commands)
    assert harness.runtime.list_snapshots("avd-01") == []


def test_snapshot_requires_running(harness: RuntimeHarness) -> None:
    harness.add()
    with pytest.raises(InstanceError):
        harness.runtime.save_snapshot("avd-01", "snap1")


def test_snapshot_invalid_name(harness: RuntimeHarness) -> None:
    harness.add()
    harness.booted()
    with pytest.raises(ValidationError):
        harness.runtime.save_snapshot("avd-01", "bad name\nkill")


def test_snapshot_golden_is_single(harness: RuntimeHarness) -> None:
    harness.add()
    harness.booted()
    harness.runtime.save_snapshot("avd-01", "snap1", golden=True)
    harness.runtime.save_snapshot("avd-01", "snap2", golden=True)
    listed = {item.name: item.is_golden for item in harness.runtime.list_snapshots("avd-01")}
    assert listed == {"snap1": False, "snap2": True}


def test_snapshot_adb_fallback(tmp_path: Path) -> None:
    console = FakeConsoleFactory(fail=True)
    harness = RuntimeHarness(tmp_path, console_factory=console)
    harness.add()
    harness.booted()
    snapshot = harness.runtime.save_snapshot("avd-01", "snap1")
    assert snapshot.name == "snap1"
    assert ("emulator-5600", "snapshot save snap1") in harness.adb.emu_calls


def test_snapshot_load_unknown_creates_metadata(harness: RuntimeHarness) -> None:
    harness.add()
    harness.booted()
    snapshot = harness.runtime.load_snapshot("avd-01", "restored")
    assert snapshot.name == "restored"
    assert any(item.name == "restored" for item in harness.runtime.list_snapshots("avd-01"))


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------


def test_capacity_status_fields(tmp_path: Path) -> None:
    config = make_config()
    paths = make_paths(tmp_path)
    avds = FakeAvds(tmp_path, config)
    allocator = PortAllocator(start=5600, end=5620)
    service = make_capacity(config, paths, allocator, avds, available_mb=4096, total_mb=8192)
    status = service.status()
    assert status["max_instances"] == 4
    assert status["running"] == 0
    assert status["ram_available_mb"] == 4096
    assert status["ports"]["limit"] > 0
    assert status["can_start_more"] >= 1
    assert isinstance(status["warnings"], list)


def test_capacity_never_overcommits_ram(tmp_path: Path) -> None:
    config = make_config(capacity_reserve_mb=1024)
    paths = make_paths(tmp_path)
    avds = FakeAvds(tmp_path, config)
    allocator = PortAllocator(start=5600, end=5620)
    service = make_capacity(
        config, paths, allocator, avds, available_mb=3000, total_mb=8192
    )
    decision = service.can_start(2048)
    assert decision.ok is False
    assert any("RAM" in reason for reason in decision.reasons)
    with pytest.raises(CapacityError):
        service.ensure_can_start(2048)
    # A smaller instance still fits.
    assert service.can_start(1024).ok is True


def test_capacity_port_exhaustion(tmp_path: Path) -> None:
    config = make_config(port_range_start=5600, port_range_end=5602)
    paths = make_paths(tmp_path)
    avds = FakeAvds(tmp_path, config)
    allocator = PortAllocator(start=5600, end=5602)
    allocator.lease("one")
    allocator.lease("two")
    service = make_capacity(config, paths, allocator, avds, available_mb=8192)
    decision = service.can_start(1024)
    assert decision.ok is False
    assert any("port" in reason.lower() for reason in decision.reasons)


# ---------------------------------------------------------------------------
# Supervision
# ---------------------------------------------------------------------------


def test_supervisor_detects_external_exit(harness: RuntimeHarness) -> None:
    harness.add()
    harness.start()
    harness.factory.processes[0].exit(1)
    supervisor = ProcessSupervisor(harness.runtime._supervised_processes)
    records = supervisor.run_once()
    assert [record.name for record in records] == ["avd-01"]
    assert harness.runtime.status("avd-01").state is InstanceState.ERROR
    assert harness.events_of(EventType.ERROR)


def test_supervisor_expected_stop_is_not_an_error(harness: RuntimeHarness) -> None:
    harness.add()
    harness.start()
    record = harness.runtime._records["avd-01"]
    record.expected_stop = True
    harness.factory.processes[0].exit(0)
    supervisor = ProcessSupervisor(harness.runtime._supervised_processes)
    supervisor.run_once()
    assert harness.runtime.status("avd-01").state is InstanceState.STOPPED
    assert harness.events_of(EventType.ERROR) == []


def test_supervisor_restart_policy() -> None:
    process = FakeProcess()
    restarts: list[str] = []
    provider = lambda: [  # noqa: E731 - tiny test helper
        type(
            "Entry",
            (),
            {
                "name": "avd-01",
                "poll": staticmethod(process.poll),
                "on_exit": staticmethod(lambda code: None),
                "label": "test",
            },
        )()
    ]
    supervisor = ProcessSupervisor(
        provider,
        restart_policy=RestartPolicy(auto_restart=True, max_attempts=2),
        restart=restarts.append,
    )
    process.exit(1)
    supervisor.run_once()
    supervisor.run_once()
    supervisor.run_once()
    assert restarts == ["avd-01", "avd-01"]


def test_supervisor_thread_lifecycle() -> None:
    supervisor = ProcessSupervisor(lambda: [], interval_s=0.01)
    assert supervisor.running is False
    supervisor.start()
    assert supervisor.running is True
    deadline = time.monotonic() + 2
    while supervisor.stats["ticks"] == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    supervisor.stop(timeout_s=2)
    assert supervisor.running is False
    supervisor.stop()  # idempotent


def test_manager_supervision_autostart(harness: RuntimeHarness) -> None:
    harness.runtime._auto_supervision = True
    harness.add()
    harness.start()
    assert harness.runtime.supervisor_running is True
    harness.runtime.close()
    assert harness.runtime.supervisor_running is False


# ---------------------------------------------------------------------------
# Cleanup / persistence
# ---------------------------------------------------------------------------


def test_cleanup_stops_managed_instances(harness: RuntimeHarness) -> None:
    harness.add("avd-01")
    harness.add("avd-02")
    harness.start("avd-01")
    harness.start("avd-02")
    for process in harness.factory.processes:
        process.exit_on_terminate = True
    result = harness.runtime.cleanup()
    assert result.ok is True
    assert sorted(result.data["stopped"]) == ["avd-01", "avd-02"]
    assert harness.allocator.capacity()["in_use"] == 0
    assert harness.runtime._records == {}


def test_persisted_state_survives_new_manager(harness: RuntimeHarness, tmp_path: Path) -> None:
    harness.add()
    harness.start()
    payload = json.loads(
        (harness.paths.data_dir / "runtime-state.json").read_text(encoding="utf-8")
    )
    assert payload["instances"]["avd-01"]["state"] == "starting"
    new_factory = FakeProcessFactory()
    second = RuntimeManagerService(
        harness.avds,
        backend=harness.avds.backend,
        config=harness.config,
        paths=harness.paths,
        adb=FakeAdb(),
        process_factory=new_factory,
        console_factory=FakeConsoleFactory(),
        capacity=make_capacity(harness.config, harness.paths, harness.allocator, harness.avds),
        port_allocator=harness.allocator,
        auto_supervision=False,
    )
    # The old fake PID does not exist, so the stale STARTING state is downgraded.
    assert second.status("avd-01").state is InstanceState.STOPPED
    second.close()


def test_cleanup_all_instances_kills_external_emulators(harness: RuntimeHarness) -> None:
    harness.adb.devices_list = [AdbDevice(serial="emulator-5590", state="device")]
    result = harness.runtime.cleanup(all_instances=True)
    assert "emulator-5590" in result.data["killed_external"]
    assert ("emulator-5590", "kill") in harness.adb.emu_calls

# ---------------------------------------------------------------------------
# Integration: real legacy backend + executable fake emulator/adb/console
# (opt-in; the default unit run uses the in-process fakes above)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("AVDFLEET_FAKE_INTEGRATION") != "1",
    reason="executable-fake integration test; set AVDFLEET_FAKE_INTEGRATION=1 to run",
)
@pytest.mark.integration
def test_fake_emulator_boot_snapshot_and_stop(fake_env, tmp_path: Path) -> None:
    """N1 end-to-end against the frozen executable fakes."""
    from avdfleet.core.models import ImageKind, SdkPaths, SystemImage
    from avdfleet.platform.avd import AvdManagerService
    from avdfleet.platform.sdk.legacy.backend import LegacySdkBackend

    image = SystemImage(
        id=IMAGE_ID,
        api_level=35,
        tag="google_apis_playstore",
        abi="x86_64",
        kind=ImageKind.GOOGLE_APIS_PLAYSTORE,
        display_name="Fake Play image",
        installed=True,
    )

    class _Catalog:
        def get_image(self, image_id: str) -> SystemImage:
            if image_id != IMAGE_ID:
                raise NotFoundError(f"image {image_id!r} not found")
            return image

        def list_images(self, **kwargs: Any) -> list[SystemImage]:
            return [image]

    config = make_config(
        port_range_start=5570,
        port_range_end=5680,
        boot_poll_interval_s=0.05,
        boot_timeout_s=30,
        shutdown_timeout_s=15,
    )
    backend = LegacySdkBackend(
        config,
        SdkPaths(sdk_root=str(fake_env.sdk_root), avd_home=str(fake_env.avd_home)),
    )
    avds = AvdManagerService(
        backend=backend,
        config=config,
        paths=make_paths(tmp_path),
        images=_Catalog(),
    )
    avds.save_template(
        Template(
            id="e2e",
            name="E2E",
            image_id=IMAGE_ID,
            device_id="pixel_7",
            ram_mb=1024,
            heap_mb=256,
            cpu_cores=1,
        )
    )
    avds.create_instance("e2e", "e2e-avd")
    runtime = RuntimeManagerService(
        avds,
        backend=backend,
        config=config,
        paths=avds.paths,
        auto_supervision=False,
    )
    try:
        started = runtime.start("e2e-avd")
        assert started.state is InstanceState.STARTING
        booted = runtime.wait_boot("e2e-avd", timeout_s=30)
        assert booted.state is InstanceState.RUNNING
        assert booted.boot_completed is True
        assert booted.ports is not None
        assert booted.adb_serial == f"emulator-{booted.ports.console}"
        snapshot = runtime.save_snapshot("e2e-avd", "golden", golden=True)
        assert snapshot.is_golden is True
        listed = runtime.list_snapshots("e2e-avd")
        assert any(item.name == "golden" for item in listed)
        stopped = runtime.stop("e2e-avd", timeout_s=15)
        assert stopped.state is InstanceState.STOPPED
    finally:
        try:
            runtime.stop("e2e-avd", force=True)
        except Exception:  # noqa: BLE001 - best-effort test cleanup
            pass
        runtime.close()
