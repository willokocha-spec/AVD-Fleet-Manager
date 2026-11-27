"""Protocols (interfaces) for every avdfleet subsystem.

Wave 1 agents implement these protocols; Wave 2 integration wires them.  The
protocols intentionally use plain Python types and `core.models` only.  Method
bodies are omitted; `...` keeps them mypy-checkable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .config import Config
from .models import (
    AppiumStatus,
    AvdInfo,
    BridgeConfig,
    BundleManifest,
    DeviceProfile,
    DoctorCheck,
    DoctorReport,
    FleetPlan,
    FleetSpec,
    HostInfo,
    InstallManifest,
    InstallOptions,
    InstallPlan,
    OobeMatrix,
    OobeRecord,
    OobeResetResult,
    OperationResult,
    ProxyAssignment,
    ProxyConfig,
    ProxyImportResult,
    ProxyTestResult,
    ScriptDefinition,
    ScriptRun,
    ScriptRunRequest,
    SdkPackage,
    SdkPaths,
    Snapshot,
    SystemImage,
    Template,
)
from .paths import Paths

# ---------------------------------------------------------------------------
# Host / installer
# ---------------------------------------------------------------------------


@runtime_checkable
class HostProvider(Protocol):
    platform: str

    def detect(self) -> HostInfo: ...

    def sdk_paths(self) -> SdkPaths: ...

    def package_manager(self) -> str: ...

    def doctor_checks(self) -> list[DoctorCheck]: ...

    def fix(self, check_id: str) -> OperationResult: ...


@runtime_checkable
class Installer(Protocol):
    def plan(self, options: InstallOptions | None = None) -> InstallPlan: ...

    def install(self, options: InstallOptions | None = None) -> InstallManifest: ...

    def resume(self, options: InstallOptions | None = None) -> InstallManifest: ...

    def uninstall(self, *, dry_run: bool = False, keep_data: bool = False) -> OperationResult: ...

    def repair(self, options: InstallOptions | None = None) -> InstallManifest: ...

    def status(self) -> InstallManifest | None: ...


@runtime_checkable
class InstallStep(Protocol):
    id: str
    title: str
    requires_reboot: bool
    depends_on: Sequence[str]

    def check(self) -> bool: ...

    def run(self, context: Any) -> OperationResult: ...


# ---------------------------------------------------------------------------
# SDK / tooling
# ---------------------------------------------------------------------------


@runtime_checkable
class SdkBackend(Protocol):
    name: str

    def executable(self, tool: str) -> Path: ...

    def list_packages(self) -> list[SdkPackage]: ...

    def list_installed(self) -> list[SdkPackage]: ...

    def install_packages(
        self,
        package_ids: Sequence[str],
        *,
        accept_licenses: bool = False,
        dry_run: bool = False,
        timeout_s: int = 1800,
    ) -> OperationResult: ...

    def uninstall_packages(self, package_ids: Sequence[str], *, dry_run: bool = False) -> OperationResult: ...

    def accept_licenses(self, *, timeout_s: int = 300) -> OperationResult: ...

    def environment(self) -> dict[str, str]: ...

    def list_system_images(self) -> list[SystemImage]: ...

    def list_avds(self) -> list[str]: ...

    def create_avd(
        self,
        name: str,
        image_package: str,
        device_id: str = "pixel_7",
        *,
        config: dict[str, str] | None = None,
        force: bool = False,
    ) -> str: ...

    def delete_avd(self, name: str, *, force: bool = False) -> OperationResult: ...


@runtime_checkable
class EmulatorConsole(Protocol):
    def connect(self, port: int, *, timeout_s: float = 5.0) -> None: ...

    def command(self, command: str) -> str: ...

    def kill(self) -> None: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Images / AVDs / runtime
# ---------------------------------------------------------------------------


@runtime_checkable
class ImageCatalog(Protocol):
    def list_images(self, *, include_remote: bool = True, installed_only: bool = False) -> list[SystemImage]: ...

    def get_image(self, image_id: str) -> SystemImage: ...

    def refresh(self, *, timeout_s: int = 60) -> list[SystemImage]: ...

    def download(self, image_id: str, *, dry_run: bool = False) -> SystemImage: ...

    def import_directory(
        self,
        path: str | Path,
        *,
        name: str | None = None,
        image_id: str | None = None,
        api_level: int | None = None,
        abi: str | None = None,
    ) -> SystemImage: ...

    def oobe_matrix(self) -> OobeMatrix: ...

    def update_oobe_record(self, record: OobeRecord) -> None: ...


@runtime_checkable
class AvdManager(Protocol):
    def list_templates(self) -> list[Template]: ...

    def get_template(self, template_id: str) -> Template: ...

    def save_template(self, template: Template) -> Template: ...

    def delete_template(self, template_id: str) -> None: ...

    def list_device_profiles(self) -> list[DeviceProfile]: ...

    def list_instances(self) -> list[AvdInfo]: ...

    def get_instance(self, name: str) -> AvdInfo: ...

    def create_instance(self, template_id: str, name: str, overrides: dict[str, Any] | None = None) -> AvdInfo: ...

    def delete_instance(self, name: str, *, delete_files: bool = True) -> None: ...

    def clone_instance(self, source: str, name: str) -> AvdInfo: ...

    def bulk_deploy(self, template_id: str, *, count: int = 1, prefix: str = "avd", overrides: dict[str, Any] | None = None) -> list[AvdInfo]: ...

    def update_config(self, name: str, values: dict[str, str], *, dry_run: bool = False) -> AvdInfo: ...

    def export_yaml(self) -> str: ...

    def import_yaml(self, content: str) -> list[AvdInfo]: ...


@runtime_checkable
class RuntimeManager(Protocol):
    def list_running(self) -> list[AvdInfo]: ...

    def status(self, name: str) -> AvdInfo: ...

    def start(
        self,
        name: str,
        *,
        proxy_id: str | None = None,
        headless: bool | None = None,
        dry_run: bool = False,
    ) -> AvdInfo: ...

    def stop(self, name: str, *, timeout_s: float | None = None, force: bool = False) -> AvdInfo: ...

    def restart(self, name: str, *, proxy_id: str | None = None) -> AvdInfo: ...

    def wait_boot(self, name: str, *, timeout_s: float | None = None) -> AvdInfo: ...

    def save_snapshot(self, name: str, snapshot: str, *, golden: bool = False) -> Snapshot: ...

    def load_snapshot(self, name: str, snapshot: str) -> Snapshot: ...

    def delete_snapshot(self, name: str, snapshot: str) -> None: ...

    def list_snapshots(self, name: str) -> list[Snapshot]: ...

    def capacity(self) -> dict[str, Any]: ...

    def cleanup(self, *, all_instances: bool = False) -> OperationResult: ...


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------


@runtime_checkable
class Vault(Protocol):
    def available(self) -> bool: ...

    def unlock(self, passphrase: str | None = None) -> None: ...

    def lock(self) -> None: ...

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...

    def delete(self, key: str) -> None: ...

    def keys(self) -> list[str]: ...


@runtime_checkable
class ProxyPool(Protocol):
    def list_proxies(self, *, include_disabled: bool = False) -> list[ProxyConfig]: ...

    def get(self, proxy_id: str) -> ProxyConfig: ...

    def add(self, proxy: ProxyConfig) -> ProxyConfig: ...

    def import_text(self, text: str, *, source: str = "paste") -> ProxyImportResult: ...

    def update(self, proxy_id: str, values: dict[str, Any]) -> ProxyConfig: ...

    def delete(self, proxy_id: str) -> None: ...

    def check(self, proxy_id: str, *, timeout_s: float | None = None) -> ProxyTestResult: ...

    def check_all(self) -> list[ProxyTestResult]: ...


@runtime_checkable
class ProxyAssigner(Protocol):
    def assign(self, avd_name: str, proxy_id: str) -> ProxyAssignment: ...

    def unassign(self, avd_name: str) -> None: ...

    def get(self, avd_name: str) -> ProxyAssignment | None: ...

    def list_assignments(self) -> list[ProxyAssignment]: ...

    def auto_assign(self, avd_names: Sequence[str], *, strategy: str = "round_robin") -> list[ProxyAssignment]: ...


@runtime_checkable
class ProxyBridge(Protocol):
    def ensure(
        self,
        instance: str,
        proxy: ProxyConfig,
        *,
        listen_port: int | None = None,
    ) -> BridgeConfig: ...

    def stop(self, instance: str) -> None: ...

    def get(self, instance: str) -> BridgeConfig | None: ...

    def list_active(self) -> list[BridgeConfig]: ...

    def swap(self, instance: str, proxy: ProxyConfig) -> BridgeConfig: ...


@runtime_checkable
class ProxyVerifier(Protocol):
    def verify_proxy(self, proxy: ProxyConfig, *, timeout_s: float | None = None) -> ProxyTestResult: ...

    def verify_guest(
        self,
        avd_name: str,
        proxy: ProxyConfig,
        *,
        token_url: str | None = None,
        timeout_s: float | None = None,
    ) -> ProxyTestResult: ...


# ---------------------------------------------------------------------------
# OOBE / scripting / Appium / screen
# ---------------------------------------------------------------------------


@runtime_checkable
class OobeManager(Protocol):
    def matrix(self) -> OobeMatrix: ...

    def record_for(self, image_id: str) -> OobeRecord | None: ...

    def detect(self, avd_name: str) -> OobeRecord: ...

    def verify_image(self, image_id: str, *, timeout_s: int | None = None, keep_avd: bool = False) -> OobeRecord: ...

    def reset(self, avd_name: str, *, strategy: str | None = None, verify: bool = True) -> OobeResetResult: ...

    def ensure_golden_snapshot(self, avd_name: str, snapshot: str = "avdfleet-golden") -> Snapshot: ...


@runtime_checkable
class ScriptRunner(Protocol):
    def list_scripts(self) -> list[ScriptDefinition]: ...

    def get_script(self, script_id: str) -> ScriptDefinition: ...

    def save_script(self, script: ScriptDefinition) -> ScriptDefinition: ...

    def delete_script(self, script_id: str) -> None: ...

    def run(self, request: ScriptRunRequest) -> ScriptRun: ...

    def cancel(self, run_id: str) -> ScriptRun: ...

    def get_run(self, run_id: str) -> ScriptRun: ...

    def list_runs(self, *, limit: int = 50) -> list[ScriptRun]: ...

    def on_event(self, callback: Callable[[str, dict[str, Any]], None]) -> Callable[[], None]: ...


@runtime_checkable
class AppiumManager(Protocol):
    def status(self) -> AppiumStatus: ...

    def start(self, *, port: int | None = None) -> AppiumStatus: ...

    def stop(self) -> AppiumStatus: ...

    def build_capabilities(self, avd: AvdInfo, *, template: Template | None = None) -> dict[str, Any]: ...

    def cleanup_forwards(self, avd_name: str) -> OperationResult: ...


@runtime_checkable
class ScreenClient(Protocol):
    def available(self, avd_name: str) -> bool: ...

    def capture(self, avd_name: str) -> bytes: ...

    async def stream(self, avd_name: str) -> AsyncIterator[bytes]: ...

    def tap(self, avd_name: str, x: int, y: int) -> OperationResult: ...

    def swipe(self, avd_name: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> OperationResult: ...

    def text(self, avd_name: str, value: str) -> OperationResult: ...

    def key(self, avd_name: str, keycode: str) -> OperationResult: ...


# ---------------------------------------------------------------------------
# Store / API / fleet / portable
# ---------------------------------------------------------------------------


@runtime_checkable
class Repository(Protocol):
    def initialize(self) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class FleetReconciler(Protocol):
    def load(self, path: str | Path) -> FleetSpec: ...

    def validate(self, spec: FleetSpec) -> list[str]: ...

    def plan(self, spec: FleetSpec, *, prune: bool = False) -> FleetPlan: ...

    def apply(self, spec: FleetSpec, *, prune: bool = False, dry_run: bool = False) -> FleetPlan: ...


@runtime_checkable
class BundleBuilder(Protocol):
    def build(
        self,
        destination: str | Path,
        *,
        offline: bool = False,
        host_os: str | None = None,
        dry_run: bool = False,
    ) -> BundleManifest: ...

    def verify(self, root: str | Path) -> BundleManifest: ...

    def inspect(self, root: str | Path) -> BundleManifest: ...


@runtime_checkable
class DoctorService(Protocol):
    def run(self, *, include_slow: bool = False) -> DoctorReport: ...

    def fix(self, check_id: str) -> OperationResult: ...


class EventBusProtocol(Protocol):
    def subscribe(self, *, max_queue: int | None = None) -> Any: ...

    def emit(self, event_type: str, payload: dict[str, Any] | None = None, *, source: str = "avdfleet") -> Any: ...


class ServiceContainer(Protocol):
    config: Config
    paths: Paths

    def doctor(self) -> DoctorService: ...

    def installer(self) -> Installer: ...

    def images(self) -> ImageCatalog: ...

    def avds(self) -> AvdManager: ...

    def runtime(self) -> RuntimeManager: ...

    def proxies(self) -> ProxyPool: ...

    def assignments(self) -> ProxyAssigner: ...

    def bridge(self) -> ProxyBridge: ...

    def oobe(self) -> OobeManager: ...

    def scripts(self) -> ScriptRunner: ...

    def appium(self) -> AppiumManager: ...

    def screen(self) -> ScreenClient: ...

    def fleet(self) -> FleetReconciler: ...

    def portable(self) -> BundleBuilder: ...

    def close(self) -> None: ...


# Re-exported for backwards compatibility with generated docs/tests.
__all__ = [
    "AppiumManager",
    "AvdManager",
    "BundleBuilder",
    "DoctorService",
    "EmulatorConsole",
    "EventBusProtocol",
    "FleetReconciler",
    "HostProvider",
    "ImageCatalog",
    "Installer",
    "InstallStep",
    "OobeManager",
    "ProxyAssigner",
    "ProxyBridge",
    "ProxyPool",
    "ProxyVerifier",
    "Repository",
    "RuntimeManager",
    "ScreenClient",
    "ScriptRunner",
    "SdkBackend",
    "ServiceContainer",
    "Vault",
]
