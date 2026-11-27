"""End-to-end style tests for the AVD Fleet REST API.

The suite is deliberately self-contained: :class:`StubContainer` provides fake
services that return real core pydantic models, everything runs against
pytest's ``tmp_path`` and no ``tests/fakes`` fixtures are required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import SecretStr

from avdfleet.api.app import create_app
from avdfleet.core.config import APP_VERSION, Config
from avdfleet.core.errors import NotFoundError
from avdfleet.core.events import EventBus, EventType
from avdfleet.core.models import (
    AppiumStatus,
    AvdInfo,
    BridgeConfig,
    CheckStatus,
    DeviceProfile,
    DoctorCheck,
    DoctorReport,
    FleetPlan,
    FleetSpec,
    ImageKind,
    InstancePorts,
    InstanceResult,
    InstanceState,
    OobeMatrix,
    OobeRecord,
    OobeResetResult,
    OobeStatus,
    OperationResult,
    PlanAction,
    PlanChange,
    ProxyAssignment,
    ProxyConfig,
    ProxyImportResult,
    ProxyScheme,
    ProxyTestResult,
    RunState,
    ScriptDefinition,
    ScriptRun,
    ScriptRunRequest,
    ScriptType,
    Snapshot,
    SystemImage,
    Template,
)
from avdfleet.core.paths import Paths

ORIGIN = "http://127.0.0.1:8765"
BASE_URL = "http://127.0.0.1:8765"
TOKEN = "test-token-with-plenty-of-entropy-0123456789"
ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "contracts" / "openapi.yaml"
PNG = b"\x89PNG\r\n\x1a\nfake-screenshot"


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# Fake domain data (real core models)
# ---------------------------------------------------------------------------

IMAGE = SystemImage(
    id="img-api36-google_apis-x86_64",
    api_level=36,
    tag="google_apis",
    abi="x86_64",
    kind=ImageKind.GOOGLE_APIS,
    display_name="API 36 Google APIs x86_64",
    package="system-images;android-36;google_apis;x86_64",
    installed=True,
    oobe_status=OobeStatus.VERIFIED,
)
TEMPLATE = Template(
    id="tpl-default",
    name="Default pixel",
    image_id=IMAGE.id,
    device_id="pixel_7",
    ram_mb=2048,
    headless=True,
)
RUNNING = AvdInfo(
    name="avd-01",
    template_id=TEMPLATE.id,
    image_id=IMAGE.id,
    image_name="API 36",
    state=InstanceState.RUNNING,
    pid=4242,
    ports=InstancePorts(console=5554, adb=5555, grpc=8554),
    boot_completed=True,
    screen_available=True,
)
STOPPED = AvdInfo(
    name="avd-02",
    template_id=TEMPLATE.id,
    image_id=IMAGE.id,
    state=InstanceState.STOPPED,
    ports=InstancePorts(console=5556, adb=5557),
)
PROXY = ProxyConfig(
    id="px-test",
    name="test proxy",
    scheme=ProxyScheme.HTTP,
    host="10.0.0.5",
    port=3128,
    username="alice",
    password=SecretStr("hunter2"),
    tags=["g1"],
)
SCRIPT = ScriptDefinition(
    id="scr-builtin",
    name="hello",
    type=ScriptType.ADB_SHELL,
    content="echo hello",
)
RUN = ScriptRun(
    id="run-1",
    script_id=SCRIPT.id,
    script_name=SCRIPT.name,
    type=ScriptType.ADB_SHELL,
    state=RunState.RUNNING,
    instances=["avd-01"],
    results=[InstanceResult(instance="avd-01", state=RunState.RUNNING)],
)


# ---------------------------------------------------------------------------
# Fake services
# ---------------------------------------------------------------------------


class FakeImages:
    def __init__(self) -> None:
        self.images: dict[str, SystemImage] = {IMAGE.id: IMAGE.model_copy(deep=True)}
        self.last_download_dry_run: bool | None = None

    def list_images(self, *, include_remote: bool = True, installed_only: bool = False) -> list[SystemImage]:
        return [image for image in self.images.values() if not installed_only or image.installed]

    def get_image(self, image_id: str) -> SystemImage:
        if image_id not in self.images:
            raise NotFoundError(f"system image {image_id!r} was not found")
        return self.images[image_id]

    def refresh(self, *, timeout_s: int = 60) -> list[SystemImage]:
        return list(self.images.values())

    def download(self, image_id: str, *, dry_run: bool = False) -> SystemImage:
        self.last_download_dry_run = dry_run
        image = self.get_image(image_id)
        if not dry_run:
            self.images[image_id] = image.model_copy(update={"installed": True})
        return image

    def import_directory(
        self,
        path: str | Path,
        *,
        name: str | None = None,
        image_id: str | None = None,
        api_level: int | None = None,
        abi: str | None = None,
    ) -> SystemImage:
        image = SystemImage(
            id=image_id or "img-custom-import",
            api_level=api_level or 34,
            tag="custom",
            abi=abi or "x86_64",
            kind=ImageKind.CUSTOM,
            display_name=name or "Imported image",
            installed=True,
            custom=True,
            local_path=str(path),
        )
        self.images[image.id] = image
        return image

    def oobe_matrix(self) -> OobeMatrix:
        return OobeMatrix(records=[OobeRecord(image_id=IMAGE.id, status=OobeStatus.VERIFIED)])

    def update_oobe_record(self, record: OobeRecord) -> None:
        return None


class FakeAvds:
    def __init__(self) -> None:
        self.templates: dict[str, Template] = {TEMPLATE.id: TEMPLATE.model_copy(deep=True)}
        self.instances: dict[str, AvdInfo] = {
            RUNNING.name: RUNNING.model_copy(deep=True),
            STOPPED.name: STOPPED.model_copy(deep=True),
        }

    def list_templates(self) -> list[Template]:
        return list(self.templates.values())

    def get_template(self, template_id: str) -> Template:
        if template_id not in self.templates:
            raise NotFoundError(f"template {template_id!r} was not found")
        return self.templates[template_id]

    def save_template(self, template: Template) -> Template:
        self.templates[template.id] = template
        return template

    def delete_template(self, template_id: str) -> None:
        self.templates.pop(template_id, None)

    def list_device_profiles(self) -> list[DeviceProfile]:
        return [DeviceProfile(id="pixel_7", name="Pixel 7", resolution="1080x2400", density=420)]

    def list_instances(self) -> list[AvdInfo]:
        return list(self.instances.values())

    def get_instance(self, name: str) -> AvdInfo:
        if name not in self.instances:
            raise NotFoundError(f"instance {name!r} was not found")
        return self.instances[name]

    def create_instance(self, template_id: str, name: str, overrides: dict[str, Any] | None = None) -> AvdInfo:
        info = AvdInfo(name=name, template_id=template_id, image_id=IMAGE.id, state=InstanceState.STOPPED)
        self.instances[name] = info
        return info

    def delete_instance(self, name: str, *, delete_files: bool = True) -> None:
        self.instances.pop(name, None)

    def clone_instance(self, source: str, name: str) -> AvdInfo:
        info = self.get_instance(source).model_copy(update={"name": name, "state": InstanceState.STOPPED})
        self.instances[name] = info
        return info

    def bulk_deploy(
        self,
        template_id: str,
        *,
        count: int = 1,
        prefix: str = "avd",
        overrides: dict[str, Any] | None = None,
    ) -> list[AvdInfo]:
        created = []
        for index in range(count):
            name = f"{prefix}-{index + 1:02d}"
            created.append(self.create_instance(template_id, name, overrides))
        return created

    def update_config(self, name: str, values: dict[str, str], *, dry_run: bool = False) -> AvdInfo:
        info = self.get_instance(name)
        info.config.update(values)
        return info

    def export_yaml(self) -> str:
        return "templates: []\n"

    def import_yaml(self, content: str) -> list[AvdInfo]:
        return []


class FakeRuntime:
    def __init__(self) -> None:
        self.instances: dict[str, AvdInfo] = {
            RUNNING.name: RUNNING.model_copy(deep=True),
            STOPPED.name: STOPPED.model_copy(deep=True),
        }
        self.snapshots: list[Snapshot] = []
        self.last_start: dict[str, Any] = {}
        self.last_stop: dict[str, Any] = {}

    def list_running(self) -> list[AvdInfo]:
        return [info for info in self.instances.values() if info.state is InstanceState.RUNNING]

    def status(self, name: str) -> AvdInfo:
        if name not in self.instances:
            raise NotFoundError(f"instance {name!r} was not found")
        return self.instances[name]

    def _set_state(self, name: str, state: InstanceState) -> AvdInfo:
        info = self.status(name).model_copy(update={"state": state})
        self.instances[name] = info
        return info

    def start(
        self,
        name: str,
        *,
        proxy_id: str | None = None,
        headless: bool | None = None,
        dry_run: bool = False,
    ) -> AvdInfo:
        self.last_start = {"name": name, "proxy_id": proxy_id, "headless": headless, "dry_run": dry_run}
        return self.status(name) if dry_run else self._set_state(name, InstanceState.RUNNING)

    def stop(self, name: str, *, timeout_s: float | None = None, force: bool = False) -> AvdInfo:
        self.last_stop = {"name": name, "timeout_s": timeout_s, "force": force}
        return self._set_state(name, InstanceState.STOPPED)

    def restart(self, name: str, *, proxy_id: str | None = None) -> AvdInfo:
        return self._set_state(name, InstanceState.RUNNING)

    def wait_boot(self, name: str, *, timeout_s: float | None = None) -> AvdInfo:
        return self.status(name).model_copy(update={"boot_completed": True})

    def save_snapshot(self, name: str, snapshot: str, *, golden: bool = False) -> Snapshot:
        record = Snapshot(name=snapshot, avd_name=name, is_golden=golden)
        self.snapshots.append(record)
        return record

    def load_snapshot(self, name: str, snapshot: str) -> Snapshot:
        return Snapshot(name=snapshot, avd_name=name)

    def delete_snapshot(self, name: str, snapshot: str) -> None:
        self.snapshots = [item for item in self.snapshots if item.name != snapshot]

    def list_snapshots(self, name: str) -> list[Snapshot]:
        return [item for item in self.snapshots if item.avd_name == name]

    def capacity(self) -> dict[str, Any]:
        return {
            "max_instances": 64,
            "running": 1,
            "deployed": 2,
            "ram_available_mb": 8192,
            "ram_required_mb": 2048,
            "ports": {"start": 5554, "end": 5680},
            "cpu_cores": 8,
            "can_start_more": 3,
            "warnings": [],
        }

    def cleanup(self, *, all_instances: bool = False) -> OperationResult:
        return OperationResult(ok=True, message="cleanup done")


class FakeProxies:
    def __init__(self) -> None:
        self.pool: dict[str, ProxyConfig] = {PROXY.id: PROXY.model_copy(deep=True)}

    def list_proxies(self, *, include_disabled: bool = False) -> list[ProxyConfig]:
        return [proxy for proxy in self.pool.values() if include_disabled or proxy.enabled]

    def get(self, proxy_id: str) -> ProxyConfig:
        if proxy_id not in self.pool:
            raise NotFoundError(f"proxy {proxy_id!r} was not found")
        return self.pool[proxy_id]

    def add(self, proxy: ProxyConfig) -> ProxyConfig:
        self.pool[proxy.id] = proxy
        return proxy

    def import_text(self, text: str, *, source: str = "paste") -> ProxyImportResult:
        added: list[ProxyConfig] = []
        errors: list[str] = []
        for index, line in enumerate(line.strip() for line in text.splitlines()):
            if not line:
                continue
            if "@" not in line:
                errors.append(f"line {index + 1}: missing credentials")
                continue
            proxy = ProxyConfig(
                id=f"px-import-{index + 1}",
                name=line,
                scheme=ProxyScheme.HTTP,
                host=f"10.0.0.{index + 10}",
                port=8080,
                username="imported",
                password=SecretStr("imported-secret"),
            )
            self.pool[proxy.id] = proxy
            added.append(proxy)
        return ProxyImportResult(added=added, skipped=0, errors=errors)

    def update(self, proxy_id: str, values: dict[str, Any]) -> ProxyConfig:
        current = self.get(proxy_id)
        data = current.model_dump()
        for key, value in values.items():
            if key == "password":
                data["password"] = SecretStr(str(value)) if value else None
            elif key in data:
                data[key] = value
        updated = ProxyConfig.model_validate(data)
        self.pool[proxy_id] = updated
        return updated

    def delete(self, proxy_id: str) -> None:
        self.pool.pop(proxy_id, None)

    def check(self, proxy_id: str, *, timeout_s: float | None = None) -> ProxyTestResult:
        self.get(proxy_id)
        return ProxyTestResult(proxy_id=proxy_id, ok=True, latency_ms=12.5, egress_ip="203.0.113.5")

    def check_all(self) -> list[ProxyTestResult]:
        return [self.check(proxy_id) for proxy_id in self.pool]


class FakeVerifier:
    def verify_proxy(self, proxy: ProxyConfig, *, timeout_s: float | None = None) -> ProxyTestResult:
        return ProxyTestResult(proxy_id=proxy.id, ok=True, latency_ms=5.0, egress_ip="203.0.113.9")

    def verify_guest(
        self,
        avd_name: str,
        proxy: ProxyConfig,
        *,
        token_url: str | None = None,
        timeout_s: float | None = None,
    ) -> ProxyTestResult:
        return ProxyTestResult(proxy_id=proxy.id, ok=True, latency_ms=7.5, egress_ip="203.0.113.9")


class FakeAssignments:
    def __init__(self) -> None:
        self.items: dict[str, ProxyAssignment] = {}

    def assign(self, avd_name: str, proxy_id: str) -> ProxyAssignment:
        assignment = ProxyAssignment(avd_name=avd_name, proxy_id=proxy_id)
        self.items[avd_name] = assignment
        return assignment

    def unassign(self, avd_name: str) -> None:
        self.items.pop(avd_name, None)

    def get(self, avd_name: str) -> ProxyAssignment | None:
        return self.items.get(avd_name)

    def list_assignments(self) -> list[ProxyAssignment]:
        return list(self.items.values())

    def auto_assign(self, avd_names: list[str], *, strategy: str = "round_robin") -> list[ProxyAssignment]:
        return [self.assign(name, PROXY.id) for name in avd_names]


class FakeBridge:
    def list_active(self) -> list[BridgeConfig]:
        return [BridgeConfig(instance="avd-01", listen_port=7801, upstream=PROXY.safe_url, mode="http")]

    def ensure(
        self,
        instance: str,
        proxy: ProxyConfig,
        *,
        listen_port: int | None = None,
        local_auth_token: str | None = None,
    ) -> BridgeConfig:
        return BridgeConfig(instance=instance, listen_port=listen_port or 7801)

    def local_proxy_credentials(self, instance: str) -> tuple[str, str] | None:
        return None

    def local_proxy_url(self, instance: str) -> str | None:
        return "http://avd-01:test-token@127.0.0.1:7801"

    def stop(self, instance: str) -> None:
        return None

    def get(self, instance: str) -> BridgeConfig | None:
        return None

    def swap(self, instance: str, proxy: ProxyConfig) -> BridgeConfig:
        return BridgeConfig(instance=instance, listen_port=7801)


class FakeOobe:
    def matrix(self) -> OobeMatrix:
        return OobeMatrix(records=[OobeRecord(image_id=IMAGE.id, status=OobeStatus.VERIFIED)])

    def record_for(self, image_id: str) -> OobeRecord | None:
        if image_id == IMAGE.id:
            return OobeRecord(image_id=image_id, status=OobeStatus.VERIFIED)
        return None

    def detect(self, avd_name: str) -> OobeRecord:
        return OobeRecord(image_id=IMAGE.id, status=OobeStatus.VERIFIED)

    def verify_image(
        self, image_id: str, *, timeout_s: int | None = None, keep_avd: bool = False
    ) -> OobeRecord:
        if image_id != IMAGE.id:
            raise NotFoundError(f"system image {image_id!r} was not found")
        return OobeRecord(image_id=image_id, status=OobeStatus.VERIFIED, boot_seconds=42.0)

    def reset(self, avd_name: str, *, strategy: str | None = None, verify: bool = True) -> OobeResetResult:
        return OobeResetResult(
            avd_name=avd_name,
            strategy=strategy or "snapshot",
            ok=True,
            seconds=8.0,
            activity="com.android.setupwizard/.SetupWizardActivity",
        )

    def ensure_golden_snapshot(self, avd_name: str, snapshot: str = "avdfleet-golden") -> Snapshot:
        return Snapshot(name=snapshot, avd_name=avd_name, is_golden=True)


class FakeScripts:
    def __init__(self) -> None:
        self.scripts: dict[str, ScriptDefinition] = {SCRIPT.id: SCRIPT.model_copy(deep=True)}
        self.runs: dict[str, ScriptRun] = {RUN.id: RUN.model_copy(deep=True)}
        self.submitted: list[str] = []

    def list_scripts(self) -> list[ScriptDefinition]:
        return list(self.scripts.values())

    def get_script(self, script_id: str) -> ScriptDefinition:
        if script_id not in self.scripts:
            raise NotFoundError(f"script {script_id!r} was not found")
        return self.scripts[script_id]

    def save_script(self, script: ScriptDefinition) -> ScriptDefinition:
        self.scripts[script.id] = script
        return script

    def delete_script(self, script_id: str) -> None:
        self.scripts.pop(script_id, None)

    def submit(self, request: ScriptRunRequest) -> ScriptRun:
        """Non-blocking variant used by the REST API (returns a queued run)."""
        self.submitted.append(request.script_id)
        run = ScriptRun(
            id=f"run-{len(self.runs) + 1}",
            script_id=request.script_id,
            state=RunState.QUEUED,
            instances=list(request.instances),
            dry_run=request.dry_run,
        )
        self.runs[run.id] = run
        return run

    def run(self, request: ScriptRunRequest) -> ScriptRun:
        """Blocking variant used by the CLI (not routed by the REST API)."""
        return self.submit(request)

    def cancel(self, run_id: str) -> ScriptRun:
        run = self.get_run(run_id)
        cancelled = run.model_copy(update={"state": RunState.CANCELLED})
        self.runs[run_id] = cancelled
        return cancelled

    def get_run(self, run_id: str) -> ScriptRun:
        if run_id not in self.runs:
            raise NotFoundError(f"script run {run_id!r} was not found")
        return self.runs[run_id]

    def list_runs(self, *, limit: int = 50) -> list[ScriptRun]:
        return list(self.runs.values())[:limit]

    def on_event(self, callback: Any) -> Any:
        return lambda: None


class FakeAppium:
    def status(self) -> AppiumStatus:
        return AppiumStatus(running=True, pid=100, port=4723, version="4.0.0", sessions=0)

    def start(self, *, port: int | None = None) -> AppiumStatus:
        return AppiumStatus(running=True, pid=100, port=port or 4723, version="4.0.0")

    def stop(self) -> AppiumStatus:
        return AppiumStatus(running=False)

    def build_capabilities(self, avd: AvdInfo, *, template: Template | None = None) -> dict[str, Any]:
        return {
            "platformName": "Android",
            "appium:automationName": "UiAutomator2",
            "appium:deviceName": avd.name,
            "appium:noReset": True,
        }

    def cleanup_forwards(self, avd_name: str) -> OperationResult:
        return OperationResult(ok=True)


class FakeScreen:
    def available(self, avd_name: str) -> bool:
        return True

    def capture(self, avd_name: str) -> bytes:
        return PNG

    async def stream(self, avd_name: str) -> Any:
        yield b"frame-1"
        yield b"frame-2"

    def tap(self, avd_name: str, x: int, y: int) -> OperationResult:
        return OperationResult(ok=True, message=f"tap {x},{y}")

    def swipe(
        self, avd_name: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300
    ) -> OperationResult:
        return OperationResult(ok=True, message="swipe")

    def text(self, avd_name: str, value: str) -> OperationResult:
        return OperationResult(ok=True, message="text")

    def key(self, avd_name: str, keycode: str) -> OperationResult:
        return OperationResult(ok=True, message="key")


class FakeFleet:
    def __init__(self) -> None:
        self.last_apply: dict[str, Any] = {}

    def load(self, path: str | Path) -> FleetSpec:
        return FleetSpec(name="loaded")

    def validate(self, spec: FleetSpec) -> list[str]:
        return []

    def plan(self, spec: FleetSpec, *, prune: bool = False) -> FleetPlan:
        return FleetPlan(
            spec_name=spec.name,
            changes=[PlanChange(action=PlanAction.CREATE, kind="template", name="default")],
        )

    def apply(self, spec: FleetSpec, *, prune: bool = False, dry_run: bool = False) -> FleetPlan:
        self.last_apply = {"prune": prune, "dry_run": dry_run}
        return self.plan(spec, prune=prune)


class FakeDoctor:
    def __init__(self) -> None:
        self.fixed: list[str] = []

    def run(self, *, include_slow: bool = False) -> DoctorReport:
        return DoctorReport(
            ok=True,
            checks=[DoctorCheck(id="sdk", title="SDK present", status=CheckStatus.OK)],
            summary="all good",
        )

    def fix(self, check_id: str) -> OperationResult:
        self.fixed.append(check_id)
        if check_id != "sdk":
            raise NotFoundError(f"doctor check {check_id!r} was not found")
        return OperationResult(ok=True, message=f"fixed {check_id}")


SERVICE_NAMES = (
    "doctor",
    "installer",
    "images",
    "avds",
    "runtime",
    "proxies",
    "assignments",
    "bridge",
    "oobe",
    "scripts",
    "appium",
    "screen",
    "fleet",
    "portable",
    "verifier",
)


class StubContainer:
    """Duck-typed ServiceContainer for the API tests (no filesystem services)."""

    def __init__(self, config: Config, paths: Paths) -> None:
        self.config = config
        self.paths = paths
        self.event_bus = EventBus(history=100)
        self.fakes: dict[str, Any] = {
            "doctor": FakeDoctor(),
            "images": FakeImages(),
            "avds": FakeAvds(),
            "runtime": FakeRuntime(),
            "proxies": FakeProxies(),
            "assignments": FakeAssignments(),
            "bridge": FakeBridge(),
            "oobe": FakeOobe(),
            "scripts": FakeScripts(),
            "appium": FakeAppium(),
            "screen": FakeScreen(),
            "fleet": FakeFleet(),
            "verifier": FakeVerifier(),
        }
        #: Services handed to the daemon; tests mutate this to simulate gaps.
        self._services: dict[str, Any] = dict(self.fakes)
        for name in SERVICE_NAMES:
            setattr(self, name, self._getter(name))

    def _getter(self, name: str) -> Any:
        def get_service() -> Any:
            return self._services.get(name)

        return get_service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def config() -> Config:
    return Config(
        auth_required=True,
        allow_origins=[ORIGIN],
        trusted_hosts=["127.0.0.1", "localhost"],
        serve_ui=False,
    )


@pytest.fixture()
def paths(tmp_path: Path) -> Paths:
    return Paths(root=tmp_path, portable=True).ensure()


@pytest.fixture()
def container(config: Config, paths: Paths) -> StubContainer:
    return StubContainer(config, paths)


@pytest.fixture()
def app(monkeypatch: pytest.MonkeyPatch, config: Config, paths: Paths, container: StubContainer) -> Any:
    monkeypatch.setenv("AVDFLEET_TOKEN", TOKEN)
    return create_app(config=config, container=container, paths=paths)


@pytest.fixture()
def client(app: Any) -> Any:
    with TestClient(app, base_url=BASE_URL) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Health, auth surface and OpenAPI
# ---------------------------------------------------------------------------


def test_health_is_unauthenticated_and_reports_version(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == APP_VERSION
    assert isinstance(body["uptime_s"], (int, float))
    assert body["emulator_available"] is True


def test_unknown_api_path_returns_contract_error_envelope(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist", headers=auth_headers())
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"]
    assert response.headers["content-type"].startswith("application/json")


def test_openapi_paths_and_operation_ids_match_contract(client: TestClient) -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    generated = client.app.openapi()  # type: ignore[attr-defined]
    methods = ("get", "post", "put", "patch", "delete")

    def operations(document: dict[str, Any]) -> dict[tuple[str, str], str]:
        result: dict[tuple[str, str], str] = {}
        for path, item in document["paths"].items():
            for method in methods:
                operation = item.get(method)
                if operation:
                    result[(method, path)] = operation["operationId"]
        return result

    expected = operations(contract)
    actual = operations(generated)
    assert set(actual) == set(expected), (
        f"missing={sorted(set(expected) - set(actual))} extra={sorted(set(actual) - set(expected))}"
    )
    mismatched = {key: (actual[key], expected[key]) for key in expected if actual[key] != expected[key]}
    assert not mismatched, f"operationId mismatches: {mismatched}"
    assert len(actual) == 89


def test_docs_disabled_by_environment(monkeypatch: pytest.MonkeyPatch, config: Config, paths: Paths, container: StubContainer) -> None:
    monkeypatch.setenv("AVDFLEET_TOKEN", TOKEN)
    monkeypatch.setenv("AVDFLEET_DOCS", "0")
    app = create_app(config=config, container=container, paths=paths)
    assert app.docs_url is None
    assert app.openapi_url is None
    with TestClient(app, base_url=BASE_URL) as client:
        response = client.get("/docs", headers=auth_headers())
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Every route group (one request per documented operation)
# ---------------------------------------------------------------------------


def _route_cases() -> list[tuple[str, str, dict[str, Any] | None, int]]:
    template = TEMPLATE.model_dump(mode="json")
    spec = FleetSpec(name="fleet-a").model_dump(mode="json")
    return [
        ("GET", "/api/v1/auth/check", None, 200),
        ("GET", "/api/v1/system", None, 200),
        ("GET", "/api/v1/config", None, 200),
        ("PATCH", "/api/v1/config", {"headless": False}, 200),
        ("GET", "/api/v1/doctor", None, 200),
        ("POST", "/api/v1/doctor/fix", {"check_id": "sdk"}, 200),
        ("GET", "/api/v1/capacity", None, 200),
        ("GET", "/api/v1/images", None, 200),
        ("POST", "/api/v1/images", {"path": "/tmp/custom-image", "name": "Custom"}, 201),
        ("POST", "/api/v1/images/refresh", None, 200),
        ("GET", f"/api/v1/images/{IMAGE.id}", None, 200),
        ("POST", f"/api/v1/images/{IMAGE.id}/download", {"dry_run": True}, 200),
        ("GET", "/api/v1/oobe/matrix", None, 200),
        ("POST", "/api/v1/oobe/verify", {"image_id": IMAGE.id}, 200),
        ("GET", f"/api/v1/oobe/records/{IMAGE.id}", None, 200),
        ("GET", "/api/v1/templates", None, 200),
        ("GET", f"/api/v1/templates/{TEMPLATE.id}", None, 200),
        ("PUT", f"/api/v1/templates/{TEMPLATE.id}", template, 200),
        ("POST", "/api/v1/templates", {**template, "id": "tpl-new", "name": "New"}, 201),
        ("DELETE", "/api/v1/templates/tpl-new", None, 204),
        ("GET", "/api/v1/devices", None, 200),
        ("GET", "/api/v1/instances", None, 200),
        ("POST", "/api/v1/instances", {"template_id": TEMPLATE.id, "name": "avd-03"}, 201),
        ("POST", "/api/v1/instances", {"template_id": TEMPLATE.id, "name": "avd-delete"}, 201),
        ("POST", "/api/v1/instances/bulk", {"template_id": TEMPLATE.id, "count": 2, "prefix": "bulk"}, 201),
        ("GET", "/api/v1/instances/avd-01", None, 200),
        ("POST", "/api/v1/instances/avd-02/start", {"proxy_id": PROXY.id}, 200),
        ("POST", "/api/v1/instances/avd-01/stop", {"force": True}, 200),
        ("POST", "/api/v1/instances/avd-01/restart", None, 200),
        ("POST", "/api/v1/instances/avd-01/wait-boot", {"timeout_s": 120}, 200),
        ("POST", "/api/v1/instances/avd-01/reset-oobe", {"strategy": "wipe"}, 200),
        ("GET", "/api/v1/instances/avd-01/screenshot", None, 200),
        ("PUT", "/api/v1/instances/avd-01/config", {"values": {"hw.ramSize": "3072"}}, 200),
        ("POST", "/api/v1/instances/avd-01/clone", {"name": "avd-01-clone"}, 201),
        ("GET", "/api/v1/instances/avd-01/snapshots", None, 200),
        ("POST", "/api/v1/instances/avd-01/snapshots", {"name": "snap-1"}, 201),
        ("POST", "/api/v1/instances/avd-01/snapshots/snap-1/load", None, 200),
        ("DELETE", "/api/v1/instances/avd-01/snapshots/snap-1", None, 204),
        ("POST", "/api/v1/instances/avd-01/input", {"action": "tap", "x": 100, "y": 200}, 200),
        ("GET", "/api/v1/proxies", None, 200),
        ("POST", "/api/v1/proxies", {"url": "socks5://bob:s3cret@10.0.0.9:1080"}, 201),
        ("POST", "/api/v1/proxies/import", {"text": "http://u:p@1.2.3.4:8080"}, 200),
        ("POST", "/api/v1/proxies/check-all", None, 200),
        ("GET", f"/api/v1/proxies/{PROXY.id}", None, 200),
        ("PATCH", f"/api/v1/proxies/{PROXY.id}", {"name": "renamed"}, 200),
        ("POST", f"/api/v1/proxies/{PROXY.id}/check", None, 200),
        ("POST", "/api/v1/proxies/verify-guest", {"avd_name": "avd-01", "proxy_id": PROXY.id}, 200),
        ("GET", "/api/v1/assignments", None, 200),
        ("POST", "/api/v1/assignments", {"avd_name": "avd-01", "proxy_id": PROXY.id}, 200),
        ("DELETE", "/api/v1/assignments/avd-01", None, 204),
        ("POST", "/api/v1/assignments/auto", {"instances": ["avd-01", "avd-02"]}, 200),
        ("GET", "/api/v1/bridges", None, 200),
        ("GET", "/api/v1/scripts", None, 200),
        ("POST", "/api/v1/scripts", {"id": "scr-new", "name": "new", "type": "adb_shell", "content": "echo hi"}, 201),
        ("GET", "/api/v1/scripts/scr-builtin", None, 200),
        ("PUT", "/api/v1/scripts/scr-builtin", {**SCRIPT.model_dump(mode="json")}, 200),
        ("DELETE", "/api/v1/scripts/scr-new", None, 204),
        ("POST", "/api/v1/scripts/runs", {"script_id": SCRIPT.id, "instances": ["avd-01"]}, 202),
        ("GET", "/api/v1/runs", None, 200),
        ("GET", "/api/v1/runs/run-1", None, 200),
        ("POST", "/api/v1/runs/run-1/cancel", None, 200),
        ("GET", "/api/v1/appium", None, 200),
        ("POST", "/api/v1/appium/start", {"port": 4723}, 200),
        ("POST", "/api/v1/appium/stop", None, 200),
        ("POST", "/api/v1/appium/capabilities", {"avd_name": "avd-01"}, 200),
        ("POST", "/api/v1/fleet/plan", {"spec": spec}, 200),
        ("POST", "/api/v1/fleet/apply", {"yaml": "name: fleet-b\n", "dry_run": True}, 200),
        ("GET", "/api/v1/portable/status", None, 200),
        ("GET", "/api/v1/portable/checks", None, 200),
        ("GET", "/api/v1/events", None, 200),
        ("DELETE", "/api/v1/instances/avd-delete", None, 204),
        ("DELETE", f"/api/v1/proxies/{PROXY.id}", None, 204),
    ]


def test_every_documented_operation_is_reachable(client: TestClient, container: StubContainer) -> None:
    failures: list[str] = []
    for method, path, body, expected in _route_cases():
        response = client.request(method, path, json=body, headers=auth_headers())
        if response.status_code != expected:
            failures.append(
                f"{method} {path}: expected {expected}, got {response.status_code}: {response.text[:300]}"
            )
    assert not failures, "\n".join(failures)


def test_delete_running_instance_conflicts(client: TestClient) -> None:
    response = client.delete("/api/v1/instances/avd-01", headers=auth_headers())
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_start_script_run_submits_and_returns_202(client: TestClient, container: StubContainer) -> None:
    response = client.post(
        "/api/v1/scripts/runs",
        json={"script_id": SCRIPT.id, "instances": ["avd-01"]},
        headers=auth_headers(),
    )
    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "queued"
    assert body["script_id"] == SCRIPT.id
    # The REST route must use the non-blocking submit(), not run().
    assert container.fakes["scripts"].submitted == [SCRIPT.id]

    cancelled = client.post(f"/api/v1/runs/{body['id']}/cancel", headers=auth_headers())
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"


def test_start_script_run_returns_503_when_submit_is_missing(
    monkeypatch: pytest.MonkeyPatch, config: Config, paths: Paths, container: StubContainer
) -> None:
    class BlockingOnlyScripts(FakeScripts):
        """Only the CLI/blocking run() variant: the API must not use it."""

        def __getattribute__(self, item: str) -> Any:  # noqa: ANN401 - deliberate
            if item == "submit":
                raise AttributeError(item)
            return object.__getattribute__(self, item)

    monkeypatch.setenv("AVDFLEET_TOKEN", TOKEN)
    container._services["scripts"] = BlockingOnlyScripts()
    app = create_app(config=config, container=container, paths=paths)
    with TestClient(app, base_url=BASE_URL) as client:
        response = client.post(
            "/api/v1/scripts/runs",
            json={"script_id": SCRIPT.id, "instances": ["avd-01"]},
            headers=auth_headers(),
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_fleet_plan_and_apply_use_the_core_model(client: TestClient, container: StubContainer) -> None:
    plan = client.post(
        "/api/v1/fleet/plan",
        json={"spec": {"name": "fleet-a"}},
        headers=auth_headers(),
    )
    assert plan.status_code == 200
    body = plan.json()
    assert body["spec_name"] == "fleet-a"
    assert body["summary"] == {"create": 1}
    assert body["changes"][0]["action"] == "create"
    assert body["changes"][0]["kind"] == "template"

    applied = client.post(
        "/api/v1/fleet/apply",
        json={"yaml": "name: fleet-b\n", "dry_run": True, "prune": True},
        headers=auth_headers(),
    )
    assert applied.status_code == 200
    assert applied.json()["spec_name"] == "fleet-b"
    assert container.fakes["fleet"].last_apply == {"prune": True, "dry_run": True}


def test_fleet_invalid_spec_returns_422(client: TestClient) -> None:
    response = client.post(
        "/api/v1/fleet/plan",
        json={"spec": {"templates": "not-a-list"}},
        headers=auth_headers(),
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] in {"fleet_spec_error", "invalid_request"}


def test_resource_404s_use_the_error_envelope(client: TestClient) -> None:
    checks = [
        "/api/v1/images/does-not-exist",
        "/api/v1/templates/does-not-exist",
        "/api/v1/proxies/does-not-exist",
        "/api/v1/scripts/does-not-exist",
        "/api/v1/runs/does-not-exist",
        "/api/v1/oobe/records/does-not-exist",
    ]
    for path in checks:
        response = client.get(path, headers=auth_headers())
        assert response.status_code == 404, (path, response.status_code, response.text)
        assert response.json()["error"]["code"] == "not_found", path


def test_screenshot_returns_png(client: TestClient) -> None:
    response = client.get("/api/v1/instances/avd-01/screenshot", headers=auth_headers())
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == PNG
    assert response.headers["cache-control"] == "no-store"


def test_dry_run_is_forwarded_to_services(client: TestClient, container: StubContainer) -> None:
    response = client.post(
        "/api/v1/instances/avd-02/start", json={"dry_run": True}, headers=auth_headers()
    )
    assert response.status_code == 200
    assert container.fakes["runtime"].last_start["dry_run"] is True
    response = client.post(
        f"/api/v1/images/{IMAGE.id}/download", json={"dry_run": True}, headers=auth_headers()
    )
    assert response.status_code == 200
    assert container.fakes["images"].last_download_dry_run is True


# ---------------------------------------------------------------------------
# Error envelope, redaction and hardening
# ---------------------------------------------------------------------------


def test_error_envelope_from_service_exception(client: TestClient) -> None:
    response = client.get("/api/v1/instances/missing-avd", headers=auth_headers())
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert "missing-avd" in body["error"]["message"]


def test_request_validation_returns_contract_envelope(client: TestClient) -> None:
    response = client.post("/api/v1/instances", json={"template_id": "tpl-default"}, headers=auth_headers())
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["details"]["errors"]
    assert all("input" not in item for item in body["error"]["details"]["errors"])


def test_validation_error_never_echoes_proxy_password(client: TestClient) -> None:
    response = client.post(
        "/api/v1/proxies",
        json={"host": "10.0.0.5", "port": 99999, "password": "topsecret-password-123"},
        headers=auth_headers(),
    )
    assert response.status_code == 422
    assert "topsecret-password-123" not in response.text
    assert "password" not in {key.lower() for key in response.json().get("error", {})}


def test_unexpected_error_is_generic_500_without_traceback(
    monkeypatch: pytest.MonkeyPatch, config: Config, paths: Paths, container: StubContainer
) -> None:
    class ExplodingDoctor:
        def run(self, *, include_slow: bool = False) -> Any:
            raise RuntimeError("boom: secret=hunter2")

        def fix(self, check_id: str) -> OperationResult:
            raise RuntimeError("boom")

    container._services["doctor"] = ExplodingDoctor()
    monkeypatch.setenv("AVDFLEET_TOKEN", TOKEN)
    app = create_app(config=config, container=container, paths=paths)
    with TestClient(app, base_url=BASE_URL, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/doctor", headers=auth_headers())
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["message"] == "Internal server error"
    assert body["error"]["details"]["request_id"]
    assert "Traceback" not in response.text
    assert "hunter2" not in response.text
    assert response.headers["x-request-id"] == body["error"]["details"]["request_id"]


def test_missing_service_returns_503(monkeypatch: pytest.MonkeyPatch, config: Config, paths: Paths, container: StubContainer) -> None:
    monkeypatch.setenv("AVDFLEET_TOKEN", TOKEN)
    container._services.pop("appium")
    app = create_app(config=config, container=container, paths=paths)
    with TestClient(app, base_url=BASE_URL) as client:
        response = client.get("/api/v1/appium", headers=auth_headers())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_missing_service_method_returns_501(
    monkeypatch: pytest.MonkeyPatch, config: Config, paths: Paths, container: StubContainer
) -> None:
    class LimitedProxies(FakeProxies):
        def __getattribute__(self, item: str) -> Any:  # noqa: ANN401 - deliberate
            if item == "check_all":
                raise AttributeError(item)
            return object.__getattribute__(self, item)

    monkeypatch.setenv("AVDFLEET_TOKEN", TOKEN)
    container._services["proxies"] = LimitedProxies()
    app = create_app(config=config, container=container, paths=paths)
    with TestClient(app, base_url=BASE_URL) as client:
        response = client.post("/api/v1/proxies/check-all", headers=auth_headers())
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "not_implemented"


def test_proxy_password_is_never_returned(client: TestClient) -> None:
    created = client.post(
        "/api/v1/proxies",
        json={"url": "http://alice:hunter2@10.0.0.5:3128", "name": "isolated"},
        headers=auth_headers(),
    )
    assert created.status_code == 201
    assert "hunter2" not in created.text
    created_body = created.json()
    assert created_body["has_credentials"] is True
    assert "password" not in created_body

    listed = client.get("/api/v1/proxies", headers=auth_headers())
    assert "hunter2" not in listed.text
    assert all("password" not in item for item in listed.json())

    patched = client.patch(
        "/api/v1/proxies/px-test",
        json={"password": "new-secret-value", "name": "rotated"},
        headers=auth_headers(),
    )
    assert patched.status_code == 200
    assert "new-secret-value" not in patched.text
    assert patched.json()["has_credentials"] is True


def test_security_headers_and_request_id(client: TestClient) -> None:
    response = client.get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-request-id"]

    echoed = client.get("/health", headers={"X-Request-ID": "req-abc-123"})
    assert echoed.headers["x-request-id"] == "req-abc-123"

    sanitized = client.get("/health", headers={"X-Request-ID": "bad id with spaces"})
    assert sanitized.headers["x-request-id"] != "bad id with spaces"
    assert len(sanitized.headers["x-request-id"]) == 32


def test_trusted_host_rejects_unknown_host(client: TestClient) -> None:
    response = client.get("/health", headers={"Host": "evil.example"})
    assert response.status_code == 400


def test_cors_allows_only_configured_origins(client: TestClient) -> None:
    allowed = client.options(
        "/api/v1/system",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"},
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == ORIGIN

    denied = client.options(
        "/api/v1/system",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in denied.headers


# ---------------------------------------------------------------------------
# Config, capacity and fallbacks
# ---------------------------------------------------------------------------


def test_config_patch_validates_whitelist(client: TestClient, app: Any) -> None:
    update = client.patch("/api/v1/config", json={"headless": False, "theme": "light"}, headers=auth_headers())
    assert update.status_code == 200
    assert update.json()["headless"] is False
    assert update.json()["theme"] == "light"
    assert app.state.config.headless is False

    bad_range = client.patch("/api/v1/config", json={"max_instances": 0}, headers=auth_headers())
    assert bad_range.status_code == 422
    assert bad_range.json()["error"]["code"] == "invalid_request"

    unknown_key = client.patch("/api/v1/config", json={"rm_rf": True}, headers=auth_headers())
    assert unknown_key.status_code == 422


def test_capacity_uses_runtime_service(client: TestClient) -> None:
    body = client.get("/api/v1/capacity", headers=auth_headers()).json()
    assert body["running"] == 1
    assert body["deployed"] == 2
    assert body["can_start_more"] == 3
    assert body["ports"]["start"] == 5554


def test_capacity_falls_back_without_runtime(
    monkeypatch: pytest.MonkeyPatch, config: Config, paths: Paths, container: StubContainer
) -> None:
    monkeypatch.setenv("AVDFLEET_TOKEN", TOKEN)
    container._services.pop("runtime")
    app = create_app(config=config, container=container, paths=paths)
    with TestClient(app, base_url=BASE_URL) as client:
        response = client.get("/api/v1/capacity", headers=auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["warnings"]
    assert body["deployed"] == 2  # counted from the AVD manager
    assert body["can_start_more"] >= 0


def test_capacity_falls_back_when_runtime_lacks_capacity_method(
    monkeypatch: pytest.MonkeyPatch, config: Config, paths: Paths, container: StubContainer
) -> None:
    monkeypatch.setenv("AVDFLEET_TOKEN", TOKEN)

    class PrimitiveRuntime:
        """A duck-typed runtime that does not expose ``capacity()``."""

        def list_running(self) -> list[AvdInfo]:
            return [RUNNING]

    container._services["runtime"] = PrimitiveRuntime()
    app = create_app(config=config, container=container, paths=paths)
    with TestClient(app, base_url=BASE_URL) as client:
        response = client.get("/api/v1/capacity", headers=auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["warnings"]
    assert body["running"] == 0  # no capacity data available
    assert body["deployed"] == 2  # counted from the AVD manager


def test_screenshot_returns_503_when_screen_service_missing(
    monkeypatch: pytest.MonkeyPatch, config: Config, paths: Paths, container: StubContainer
) -> None:
    monkeypatch.setenv("AVDFLEET_TOKEN", TOKEN)
    container._services.pop("screen")
    app = create_app(config=config, container=container, paths=paths)
    with TestClient(app, base_url=BASE_URL) as client:
        response = client.get("/api/v1/instances/avd-01/screenshot", headers=auth_headers())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "screen_error"


def test_portable_status_falls_back_without_service(client: TestClient) -> None:
    status = client.get("/api/v1/portable/status", headers=auth_headers())
    assert status.status_code == 200
    body = status.json()
    assert body["edition"] == "installed"
    assert body["portable"] is True
    assert {check["id"] for check in body["checks"]} >= {"data_dir_writable", "free_space"}

    checks = client.get("/api/v1/portable/checks", headers=auth_headers())
    assert checks.status_code == 200
    assert any(check["id"] == "data_dir_writable" for check in checks.json())


def test_events_endpoint_returns_recent_history(client: TestClient, app: Any) -> None:
    bus = app.state.event_bus
    bus.emit(EventType.SYSTEM, {"message": "one"})
    bus.emit(EventType.SYSTEM, {"message": "two"})
    response = client.get("/api/v1/events?limit=1", headers=auth_headers())
    assert response.status_code == 200
    events = response.json()
    assert len(events) == 1
    assert events[0]["payload"]["message"] == "two"
    assert events[0]["type"] == "system"
    assert {"id", "ts", "payload"} <= set(events[0])


# ---------------------------------------------------------------------------
# Static UI hosting
# ---------------------------------------------------------------------------


def test_static_ui_banner_when_build_absent(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "AVD Fleet"
    assert body["api"] == "/api/v1"

    missing = client.get("/some/spa/route")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_static_ui_spa_fallback(client: TestClient, app: Any, tmp_path: Path) -> None:
    static_dir = tmp_path / "ui"
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text("<html>spa-index</html>", encoding="utf-8")
    (static_dir / "assets" / "app.js").write_text("console.log('ui')", encoding="utf-8")
    monkeypatch_state = app.state
    original_dir = monkeypatch_state.static_dir
    original_serve = monkeypatch_state.serve_ui
    monkeypatch_state.static_dir = static_dir
    monkeypatch_state.serve_ui = True
    try:
        root = client.get("/")
        assert root.status_code == 200
        assert "spa-index" in root.text

        asset = client.get("/assets/app.js")
        assert asset.status_code == 200
        assert "console.log" in asset.text

        deep_link = client.get("/instances/avd-01?tab=logs")
        assert deep_link.status_code == 200
        assert "spa-index" in deep_link.text

        # API routes are never shadowed by the SPA fallback.
        api_missing = client.get("/api/v1/nope", headers=auth_headers())
        assert api_missing.status_code == 404
        assert api_missing.json()["error"]["code"] == "not_found"

        traversal = client.get("/%2e%2e/%2e%2e/contracts/openapi.yaml")
        assert "openapi:" not in traversal.text
    finally:
        monkeypatch_state.static_dir = original_dir
        monkeypatch_state.serve_ui = original_serve
