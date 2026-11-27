"""Shared, dependency-free service stubs and helpers for the CLI test suite.

The CLI tests must not need the executable fakes (``tests/fakes``) or any real
service: every command is exercised through an in-memory container injected via
the documented ``AVDFLEET_CONTAINER=module:factory`` test seam.  This module is
importable from the test modules (``from . import ...`` / ``from tests.cli import
...``) and defines no pytest fixtures, so the rest of the suite is unaffected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from typer.testing import CliRunner

from avdfleet.cli.main import app
from avdfleet.core.config import APP_VERSION, Config
from avdfleet.core.errors import NotFoundError
from avdfleet.core.models import (
    AcceleratorInfo,
    AcceleratorKind,
    AppiumStatus,
    AvdInfo,
    BundleManifest,
    DoctorCheck,
    DoctorReport,
    FleetPlan,
    FleetSpec,
    GpuMode,
    HostInfo,
    HostOS,
    ImageKind,
    InstallManifest,
    InstallPlan,
    InstallStep,
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
    ProxyHealth,
    ProxyImportResult,
    ProxyScheme,
    ProxyTestResult,
    RunState,
    ScriptDefinition,
    ScriptRun,
    ScriptType,
    Snapshot,
    StepResult,
    StepStatus,
    SystemImage,
    Template,
    utcnow,
)

# ---------------------------------------------------------------------------
# Container injection via the documented environment seam
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Any] = {}

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
    "host",
    "repository",
    "store",
)


def container_factory() -> Any:
    """``AVDFLEET_CONTAINER`` factory: returns the container registered by a test."""
    return _REGISTRY["container"]


def broken_container_factory() -> Any:
    """Factory used to prove help/version never build the container."""
    raise AssertionError("the service container must not be built for help/version")


def register_container(monkeypatch: Any, container: Any) -> None:
    """Register *container* and point ``AVDFLEET_CONTAINER`` at it."""
    _REGISTRY["container"] = container
    monkeypatch.setenv("AVDFLEET_CONTAINER", "tests.cli:container_factory")


# ---------------------------------------------------------------------------
# Stub services
# ---------------------------------------------------------------------------


class ServiceStub:
    """Records calls; each ``method`` returns a value or runs a callable.

    ``ServiceStub(list_scripts=[...])`` implements ``list_scripts()`` returning
    the value; ``ServiceStub(detect=lambda name: record)`` runs the callable with
    the arguments the CLI passed.  Recording makes dispatch assertions easy.
    """

    def __init__(self, **methods: Any) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._methods: dict[str, Any] = dict(methods)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        methods = self.__dict__.get("_methods", {})
        if name not in methods:
            raise AttributeError(name)
        handler = methods[name]

        def method(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            if callable(handler):
                return handler(*args, **kwargs)
            if isinstance(handler, BaseException):
                raise handler
            return handler

        return method

    # -- assertions helpers ---------------------------------------------
    def called(self, name: str) -> bool:
        return any(entry[0] == name for entry in self.calls)

    def calls_for(self, name: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        return [(args, kwargs) for called, args, kwargs in self.calls if called == name]

    def last_call(self, name: str) -> tuple[tuple[Any, ...], dict[str, Any]]:
        matches = self.calls_for(name)
        assert matches, f"{name!r} was not called (calls: {[entry[0] for entry in self.calls]})"
        return matches[-1]


class StubContainer:
    """Duck-typed service container: getters return the registered stubs."""

    def __init__(
        self,
        *,
        config: Config,
        paths: Any,
        services: dict[str, Any] | None = None,
        host_info: HostInfo | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.host_info = host_info
        self._services = dict(services or {})
        self.close_calls = 0

    def __getattr__(self, name: str) -> Any:
        if name in SERVICE_NAMES:
            services = self.__dict__.get("_services", {})
            service = services.get(name)
            if service is None:
                raise AttributeError(name)
            return lambda: service
        raise AttributeError(name)

    def close(self) -> None:
        self.close_calls += 1


# ---------------------------------------------------------------------------
# Environment / config helpers
# ---------------------------------------------------------------------------


@dataclass
class TestEnv:
    """Isolated config/paths for one test plus its container."""

    config: Config
    paths: Any
    container: StubContainer
    tmp_path: Path

    def service(self, name: str) -> ServiceStub:
        return self.container._services[name]  # test helper: direct access is fine


def make_config(tmp_path: Path, **overrides: Any) -> tuple[Config, Any]:
    """Create an isolated installed-edition config/paths pair under *tmp_path*."""
    base = Path(tmp_path) / "avdfleet-home"
    values: dict[str, Any] = {
        "edition": "installed",
        "data_dir": str(base / "data"),
        "config_dir": str(base / "config"),
        "cache_dir": str(base / "cache"),
        "log_dir": str(base / "logs"),
        "sdk_root": str(base / "sdk"),
        "avd_home": str(base / "avd"),
        "headless": True,
        "auth_required": False,
        "max_instances": 8,
        "boot_timeout_s": 20,
    }
    values.update(overrides)
    config = Config(**values)
    paths = config.resolved_paths().ensure()
    return config, paths


def prepare_env(
    monkeypatch: Any,
    tmp_path: Path,
    *,
    accelerated: bool = False,
    host_os: HostOS = HostOS.WINDOWS,
    services: dict[str, Any] | None = None,
    overrides: dict[str, ServiceStub] | None = None,
    **config_overrides: Any,
) -> TestEnv:
    """Isolate AVDFLEET_* paths and build the default stub container."""
    config, paths = make_config(tmp_path, **config_overrides)
    for name in ("DATA_DIR", "CONFIG_DIR", "CACHE_DIR", "LOG_DIR", "SDK_ROOT", "AVD_HOME"):
        path = getattr(paths, name.lower())
        monkeypatch.setenv(f"AVDFLEET_{name}", str(path))
    monkeypatch.delenv("AVDFLEET_CONFIG_FILE", raising=False)
    monkeypatch.delenv("AVDFLEET_SELFTEST_FAKES", raising=False)
    container = build_stub_container(
        config=config,
        paths=paths,
        accelerated=accelerated,
        host_os=host_os,
        services=services,
        overrides=overrides,
    )
    register_container(monkeypatch, container)
    return TestEnv(config=config, paths=paths, container=container, tmp_path=Path(tmp_path))


def invoke(args: list[str], *, env: dict[str, str] | None = None) -> Any:
    """Invoke the Typer app through CliRunner (fresh runner per call)."""
    runner = CliRunner()
    return runner.invoke(app, list(args), env=env)


def invoke_json(args: list[str], *, env: dict[str, str] | None = None) -> tuple[Any, Any]:
    """Invoke with ``--json`` and parse stdout; returns ``(result, payload)``."""
    argv = ["--json", *args]
    result = invoke(argv, env=env)
    payload = None
    try:
        payload = json.loads(result.stdout)
    except Exception:  # pragma: no cover - callers that expect errors inspect result
        payload = None
    return result, payload


def output_text(result: Any) -> str:
    """Return stdout+stderr of a CliRunner result (works across click versions)."""
    chunks = []
    for attribute in ("stdout", "stderr"):
        value = getattr(result, attribute, "") or ""
        chunks.append(str(value))
    if not any(chunks):
        chunks.append(str(getattr(result, "output", "") or ""))
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# Default service stubs (a complete, coherent in-memory fleet)
# ---------------------------------------------------------------------------


def default_images() -> list[SystemImage]:
    return [
        SystemImage(
            id="system-images;android-35;google_apis;x86_64",
            api_level=35,
            tag="google_apis",
            abi="x86_64",
            kind=ImageKind.GOOGLE_APIS,
            display_name="Google APIs API 35",
            package="system-images;android-35;google_apis;x86_64",
            installed=True,
            oobe_status=OobeStatus.VERIFIED,
        ),
        SystemImage(
            id="system-images;android-36;google_apis_playstore;x86_64",
            api_level=36,
            tag="google_apis_playstore",
            abi="x86_64",
            kind=ImageKind.GOOGLE_APIS_PLAYSTORE,
            display_name="Google Play API 36",
            package="system-images;android-36;google_apis_playstore;x86_64",
            installed=False,
            oobe_status=OobeStatus.UNVERIFIED,
        ),
    ]


def default_template() -> Template:
    return Template(
        id="tpl-default",
        name="Default fleet",
        image_id="system-images;android-35;google_apis;x86_64",
        device_id="pixel_7",
        ram_mb=2048,
        heap_mb=256,
        cpu_cores=2,
        gpu_mode=GpuMode.SWIFTSHADER_INDIRECT,
        headless=True,
    )


def default_instance(name: str = "avd-01", **overrides: Any) -> AvdInfo:
    values: dict[str, Any] = {
        "name": name,
        "template_id": "tpl-default",
        "image_id": "system-images;android-35;google_apis;x86_64",
        "device_id": "pixel_7",
        "state": InstanceState.DEFINED,
        "adb_serial": f"emulator-{name}" if name.startswith("emulator") else "",
    }
    values.update(overrides)
    return AvdInfo(**values)


def default_proxy() -> ProxyConfig:
    return ProxyConfig(
        id="px_1",
        name="listener-1",
        scheme=ProxyScheme.HTTP,
        host="127.0.0.1",
        port=8081,
        username="alice",
        password=SecretStr("supersecret"),
        health=ProxyHealth(),
        tags=["selftest"],
    )


def _default_manifest() -> InstallManifest:
    manifest = InstallManifest(
        avdfleet_version=APP_VERSION,
        sdk_root="<tmp>/sdk",
        started_at=utcnow(),
        completed_at=utcnow(),
    )
    manifest.steps = {
        "sdk.cmdline": StepResult(
            step_id="sdk.cmdline",
            status=StepStatus.SUCCEEDED,
            message="Android command-line tools installed",
            duration_s=1.5,
        ),
        "sdk.licenses": StepResult(step_id="sdk.licenses", status=StepStatus.SUCCEEDED, duration_s=0.1),
    }
    return manifest


def _scripts_run(request: Any) -> ScriptRun:
    script_id = str(getattr(request, "script_id", ""))
    instances = list(getattr(request, "instances", []) or [])
    if "timeout" in script_id:
        return ScriptRun(
            id="run-timeout",
            script_id=script_id,
            state=RunState.TIMED_OUT,
            instances=instances,
            results=[InstanceResult(instance=name, state=RunState.TIMED_OUT) for name in instances],
        )
    return ScriptRun(
        id="run-ok",
        script_id=script_id,
        state=RunState.SUCCEEDED,
        instances=instances,
        results=[
            InstanceResult(instance=name, state=RunState.SUCCEEDED, exit_code=0, stdout="ok")
            for name in instances
        ],
    )


def build_stub_container(
    *,
    config: Config,
    paths: Any,
    accelerated: bool = False,
    host_os: HostOS = HostOS.WINDOWS,
    services: dict[str, Any] | None = None,
    overrides: dict[str, ServiceStub] | None = None,
) -> StubContainer:
    """Build a full in-memory container with a coherent default fleet."""
    hypervisor = AcceleratorInfo(
        kind=AcceleratorKind.WHPX if accelerated else AcceleratorKind.NONE,
        available=accelerated,
        detail="WHPX available" if accelerated else "no hypervisor detected",
    )
    host = HostInfo(
        os=host_os,
        os_name=host_os.value,
        os_version="test",
        arch="x86_64",
        machine="x86_64",
        python_version="3.12.0",
        total_ram_gb=16.0,
        free_disk_gb=64.0,
        cpu_count=8,
        hypervisor=hypervisor,
        acceleration_available=accelerated,
    )
    images = default_images()
    template = default_template()
    instance = default_instance()
    proxy = default_proxy()
    manifest = _default_manifest()

    def list_images(**kwargs: Any) -> list[SystemImage]:
        installed_only = bool(kwargs.get("installed_only", False))
        include_remote = bool(kwargs.get("include_remote", True))
        result = list(images)
        if installed_only or not include_remote:
            result = [image for image in result if image.installed]
        return result

    def save_template(value: Any) -> Any:
        return value

    def bulk_deploy(template_id: str, *, count: int = 1, prefix: str = "avd", overrides: Any = None) -> list[AvdInfo]:
        return [default_instance(f"{prefix}-{index:02d}") for index in range(1, int(count) + 1)]

    def create_instance(template_id: str, name: str, overrides: Any = None) -> AvdInfo:
        return default_instance(name)

    def run_script(request: Any) -> ScriptRun:
        return _scripts_run(request)

    def submit_script(request: Any) -> ScriptRun:
        return ScriptRun(
            id="run-cancel",
            script_id=str(getattr(request, "script_id", "")),
            state=RunState.RUNNING,
            instances=list(getattr(request, "instances", []) or []),
        )

    def get_run(run_id: str) -> ScriptRun:
        return ScriptRun(id=run_id, script_id="selftest", state=RunState.RUNNING)

    def cancel_run(run_id: str) -> ScriptRun:
        return ScriptRun(id=run_id, script_id="selftest", state=RunState.CANCELLED)

    def detect_oobe(name: str) -> OobeRecord:
        return OobeRecord(
            image_id="system-images;android-35;google_apis;x86_64",
            api_level=35,
            tag="google_apis",
            abi="x86_64",
            status=OobeStatus.VERIFIED,
            activity="com.google.android.setupwizard/.SetupWizardActivity",
        )

    def verify_guest(avd_name: str, proxy_value: Any = None, **kwargs: Any) -> ProxyTestResult:
        return ProxyTestResult(proxy_id=getattr(proxy_value, "id", "px_1"), ok=True, latency_ms=12.0, egress_ip="203.0.113.7")

    def fleet_plan(spec: Any = None, *, prune: bool = False) -> FleetPlan:
        return FleetPlan(
            spec_name=getattr(spec, "name", "fleet"),
            changes=[
                PlanChange(action=PlanAction.CREATE, kind="template", name="selftest-template"),
                PlanChange(action=PlanAction.CREATE, kind="instance", name="selftest-fleet-01"),
            ],
        )

    def build_bundle(destination: Any, **kwargs: Any) -> BundleManifest:
        return BundleManifest(
            version=APP_VERSION,
            host_os=host_os,
            host_arch="x86_64",
            root=str(destination),
            offline=bool(kwargs.get("offline", False)),
        )

    def default_services() -> dict[str, Any]:
        return {
            "doctor": ServiceStub(
                run=lambda **kwargs: DoctorReport(
                    ok=True,
                    host=host,
                    checks=[
                        DoctorCheck(id="python", title="Python version", status="ok"),
                        DoctorCheck(id="sdk_root", title="SDK root", status="ok"),
                    ],
                ),
                fix=lambda check_id, **kwargs: OperationResult(
                    ok=True, message=f"fix applied for {check_id}", data={"check_id": check_id}
                ),
            ),
            "installer": ServiceStub(
                plan=lambda options=None: InstallPlan(
                    host=host,
                    steps=[
                        InstallStep(id="sdk.cmdline", title="Android command-line tools"),
                        InstallStep(id="tools.jdk", title="JDK", requires_reboot=False),
                    ],
                    dry_run=bool(getattr(options, "dry_run", False)),
                ),
                install=lambda options=None: manifest,
                resume=lambda options=None: manifest,
                repair=lambda options=None: manifest,
                status=lambda: manifest,
                uninstall=lambda **kwargs: OperationResult(
                    ok=True, message="removed installer-managed files", data={"removed": ["sdk"]}
                ),
            ),
            "images": ServiceStub(
                list_images=list_images,
                get_image=lambda image_id: images[0],
                download=lambda image_id, **kwargs: images[0].model_copy(update={"installed": True}),
                import_directory=lambda path, **kwargs: SystemImage(
                    id=str(kwargs.get("image_id") or "custom-selftest"),
                    api_level=int(kwargs.get("api_level") or 35),
                    tag="custom",
                    abi=str(kwargs.get("abi") or "x86_64"),
                    kind=ImageKind.CUSTOM,
                    installed=True,
                    custom=True,
                ),
                refresh=lambda **kwargs: images,
                oobe_matrix=lambda: OobeMatrix(
                    records=[OobeRecord(image_id=images[0].id, status=OobeStatus.VERIFIED, activity="setup")]
                ),
            ),
            "avds": ServiceStub(
                list_templates=lambda: [template],
                get_template=lambda template_id: template,
                save_template=save_template,
                delete_template=lambda template_id: None,
                list_device_profiles=lambda: [],
                list_instances=lambda: [instance],
                get_instance=lambda name: default_instance(name),
                create_instance=create_instance,
                create_instance_from_image=lambda image=None, name="", **kwargs: default_instance(name),
                bulk_deploy=bulk_deploy,
                delete_instance=lambda name, **kwargs: None,
                clone_instance=lambda source, name: default_instance(name),
                update_config=lambda name, values, **kwargs: default_instance(name),
                read_config=lambda name: {"hw.ramSize": "2048"},
                export_yaml=lambda: "version: '1'\ntemplates: []\ninstances: []\n",
                import_yaml=lambda content: [default_instance("avd-imported")],
                capacity=lambda: {"max_instances": 8, "running": 0, "deployed": 1, "can_start_more": 4},
            ),
            "runtime": ServiceStub(
                list_running=lambda: [
                    default_instance("avd-01", state=InstanceState.RUNNING, boot_completed=True, ports=None)
                ],
                status=lambda name: default_instance(
                    name, state=InstanceState.RUNNING, boot_completed=True
                ),
                start=lambda name, **kwargs: default_instance(name, state=InstanceState.RUNNING),
                stop=lambda name, **kwargs: default_instance(name, state=InstanceState.STOPPED),
                restart=lambda name, **kwargs: default_instance(name, state=InstanceState.RUNNING),
                wait_boot=lambda name, **kwargs: default_instance(
                    name, state=InstanceState.RUNNING, boot_completed=True
                ),
                list_snapshots=lambda name: [
                    Snapshot(name="golden", avd_name=name, is_golden=True)
                ],
                save_snapshot=lambda name, snapshot, **kwargs: Snapshot(
                    name=snapshot, avd_name=name, is_golden=bool(kwargs.get("golden", False))
                ),
                load_snapshot=lambda name, snapshot: Snapshot(name=snapshot, avd_name=name),
                delete_snapshot=lambda name, snapshot: None,
                capacity=lambda: {
                    "max_instances": 8,
                    "running": 1,
                    "deployed": 1,
                    "ram_available_mb": 8192,
                    "can_start_more": 4,
                    "warnings": [],
                },
            ),
            "proxies": ServiceStub(
                list_proxies=lambda **kwargs: [proxy],
                get=lambda proxy_id: proxy,
                add=lambda **kwargs: proxy,
                import_text=lambda text, **kwargs: ProxyImportResult(added=[proxy], skipped=0, errors=[]),
                update=lambda proxy_id, values=None, **kwargs: proxy,
                delete=lambda proxy_id: None,
                check=lambda proxy_id, **kwargs: ProxyTestResult(
                    proxy_id=proxy_id, ok=True, latency_ms=11.0, egress_ip="203.0.113.7"
                ),
                check_all=lambda: [
                    ProxyTestResult(proxy_id="px_1", ok=True, latency_ms=11.0, egress_ip="203.0.113.7")
                ],
            ),
            "assignments": ServiceStub(
                assign=lambda avd_name, proxy_id: ProxyAssignment(avd_name=avd_name, proxy_id=proxy_id),
                unassign=lambda avd_name: None,
                get=lambda avd_name: ProxyAssignment(avd_name=avd_name, proxy_id="px_1"),
                list_assignments=lambda: [ProxyAssignment(avd_name="avd-01", proxy_id="px_1")],
                auto_assign=lambda names, **kwargs: [
                    ProxyAssignment(avd_name=name, proxy_id="px_1") for name in names
                ],
            ),
            "bridge": ServiceStub(list_active=lambda: []),
            "oobe": ServiceStub(
                matrix=lambda: OobeMatrix(
                    records=[OobeRecord(image_id=images[0].id, status=OobeStatus.VERIFIED, activity="setup")]
                ),
                detect=detect_oobe,
                verify_image=lambda image_id, **kwargs: OobeRecord(
                    image_id=image_id, status=OobeStatus.VERIFIED, activity="setup"
                ),
                reset=lambda avd_name, **kwargs: OobeResetResult(
                    avd_name=avd_name,
                    strategy="snapshot",
                    ok=True,
                    seconds=4.2,
                    activity="com.google.android.setupwizard/.SetupWizardActivity",
                ),
                record_for=lambda image_id: OobeRecord(image_id=image_id),
            ),
            "scripts": ServiceStub(
                list_scripts=lambda: [
                    ScriptDefinition(
                        id="builtin.getprop-summary",
                        name="Getprop summary",
                        type=ScriptType.ADB_SHELL,
                        content="getprop",
                        builtin=True,
                        timeout_s=30,
                    )
                ],
                get_script=lambda script_id: (_ for _ in ()).throw(NotFoundError("not found")),
                save_script=lambda definition: definition,
                delete_script=lambda script_id: None,
                load_builtins=lambda **kwargs: [],
                run=run_script,
                submit=submit_script,
                get_run=get_run,
                cancel=cancel_run,
                list_runs=lambda **kwargs: [],
            ),
            "appium": ServiceStub(
                status=lambda: AppiumStatus(running=False),
                start=lambda **kwargs: AppiumStatus(running=True, port=4723, url="http://127.0.0.1:4723"),
                stop=lambda: AppiumStatus(running=False),
                build_capabilities=lambda avd, template=None: {
                    "platformName": "Android",
                    "appium:deviceName": getattr(avd, "name", "avd"),
                    "appium:udid": f"emulator-{getattr(avd, 'name', 'avd')}",
                    "appium:systemPort": 8201,
                },
                cleanup_forwards=lambda avd_name: OperationResult(ok=True, message=f"cleaned {avd_name}"),
            ),
            "screen": ServiceStub(
                capture=lambda avd_name: b"\x89PNG\r\n\x1a\nselftest",
                tap=lambda avd_name, x, y: OperationResult(ok=True, message=f"tap {x},{y}"),
                swipe=lambda avd_name, x1, y1, x2, y2, **kwargs: OperationResult(ok=True, message="swipe"),
                text=lambda avd_name, value: OperationResult(ok=True, message="text"),
                key=lambda avd_name, keycode: OperationResult(ok=True, message=f"key {keycode}"),
                available=lambda avd_name: True,
            ),
            "fleet": ServiceStub(
                load=lambda path: FleetSpec(name="selftest"),
                validate=lambda spec: [],
                plan=fleet_plan,
                apply=lambda spec=None, **kwargs: fleet_plan(spec, prune=bool(kwargs.get("prune", False))),
            ),
            "portable": ServiceStub(
                build=build_bundle,
                verify=lambda root: build_bundle(root),
                inspect=lambda root: build_bundle(root),
                checks=lambda: [
                    {"id": "filesystem", "title": "Filesystem", "status": "ok", "detail": "ntfs"},
                    {"id": "free_space", "title": "Free space", "status": "ok", "detail": "64 GiB"},
                ],
                status=lambda: {
                    "edition": "portable",
                    "root": str(paths.root),
                    "portable": True,
                    "relocatable": True,
                    "leftovers": [],
                },
                relocate=lambda root, **kwargs: {
                    "root": str(root),
                    "old_roots": [],
                    "dry_run": bool(kwargs.get("dry_run", False)),
                    "changed_files": 0,
                    "changes": [],
                    "warnings": [],
                    "manifest_updated": False,
                },
            ),
            "verifier": ServiceStub(
                verify_proxy=lambda proxy, **kwargs: ProxyTestResult(
                    proxy_id=getattr(proxy, "id", "px_1"), ok=True, latency_ms=9.0, egress_ip="203.0.113.7"
                ),
                verify_guest=verify_guest,
            ),
            "host": ServiceStub(detect=lambda: host, doctor_checks=lambda: [], fix=lambda check_id, **kwargs: OperationResult(ok=True)),
            "repository": ServiceStub(),
            "store": ServiceStub(),
        }

    merged = default_services()
    if services:
        merged.update(services)
    if overrides:
        merged.update(overrides)
    return StubContainer(config=config, paths=paths, services=merged, host_info=host)


__all__ = [
    "ServiceStub",
    "StubContainer",
    "TestEnv",
    "build_stub_container",
    "broken_container_factory",
    "container_factory",
    "default_instance",
    "default_proxy",
    "default_template",
    "invoke",
    "invoke_json",
    "make_config",
    "output_text",
    "prepare_env",
    "register_container",
]
