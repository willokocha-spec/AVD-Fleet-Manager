"""Domain models shared by every avdfleet subsystem.

This module is deliberately dependency-free apart from pydantic.  It must never
import from another avdfleet package so that `avdfleet.core` can be used by the
installer bootstrap, the API, tests and the portable launcher alike.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


def slugify(value: str, *, max_length: int = 63) -> str:
    """Return a DNS-label-ish identifier safe for AVD names and file names."""
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = value.strip("-._") or "item"
    return value[:max_length]


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)


# ---------------------------------------------------------------------------
# Host / platform
# ---------------------------------------------------------------------------


class HostOS(StrEnum):
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"
    UNKNOWN = "unknown"


class CpuArch(StrEnum):
    X86_64 = "x86_64"
    ARM64 = "arm64"
    UNKNOWN = "unknown"


class AcceleratorKind(StrEnum):
    WHPX = "whpx"
    AEHD = "aehd"
    KVM = "kvm"
    HVF = "hvf"
    NONE = "none"


class AcceleratorInfo(_Base):
    kind: AcceleratorKind = AcceleratorKind.NONE
    available: bool = False
    detail: str = ""
    requires_reboot: bool = False
    nested: bool | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class HostInfo(_Base):
    os: HostOS = HostOS.UNKNOWN
    os_name: str = ""
    os_version: str = ""
    arch: CpuArch = CpuArch.UNKNOWN
    machine: str = ""
    python_version: str = ""
    is_admin: bool = False
    is_wsl: bool = False
    is_container: bool = False
    cpu_count: int = 1
    total_ram_gb: float = 0.0
    free_disk_gb: float = 0.0
    hypervisor: AcceleratorInfo = Field(default_factory=AcceleratorInfo)
    acceleration_available: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class SdkPaths(_Base):
    sdk_root: str = ""
    cmdline_tools: str | None = None
    platform_tools: str | None = None
    emulator: str | None = None
    build_tools: str | None = None
    avd_home: str | None = None
    licenses_dir: str | None = None
    android_cli: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class SdkPackage(_Base):
    id: str
    version: str = ""
    description: str = ""
    installed: bool = False
    path: str | None = None
    optional: bool = False


# ---------------------------------------------------------------------------
# Images / OOBE
# ---------------------------------------------------------------------------


class ImageKind(StrEnum):
    GOOGLE_APIS = "google_apis"
    GOOGLE_APIS_PLAYSTORE = "google_apis_playstore"
    AOSP = "aosp"
    DEFAULT = "default"
    CUSTOM = "custom"
    UNKNOWN = "unknown"


class OobeStatus(StrEnum):
    VERIFIED = "verified"
    FAILED = "failed"
    UNVERIFIED = "unverified"
    UNSUPPORTED = "unsupported"


class GpuMode(StrEnum):
    AUTO = "auto"
    HOST = "host"
    SWIFTSHADER_INDIRECT = "swiftshader_indirect"
    SWIFTSHADER = "swiftshader"
    ANGLE_INDIRECT = "angle_indirect"
    OFF = "off"


class SystemImage(_Base):
    id: str
    api_level: int = 0
    tag: str = ""
    abi: str = "x86_64"
    kind: ImageKind = ImageKind.UNKNOWN
    display_name: str = ""
    package: str = ""
    url: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    installed: bool = False
    local_path: str | None = None
    oobe_status: OobeStatus = OobeStatus.UNVERIFIED
    custom: bool = False
    notes: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def host_abi(self) -> str:
        return self.abi


class OobeRecord(_Base):
    image_id: str
    api_level: int = 0
    tag: str = ""
    abi: str = "x86_64"
    status: OobeStatus = OobeStatus.UNVERIFIED
    activity: str = ""
    first_run_component: str = ""
    screenshot_path: str = ""
    boot_seconds: float | None = None
    reset_seconds: float | None = None
    checked_at: datetime | None = None
    checked_by: str = ""
    emulator_version: str = ""
    notes: str = ""
    evidence: list[str] = Field(default_factory=list)


class OobeMatrix(_Base):
    version: str = "1"
    generated_at: datetime | None = None
    host_abi: str = "x86_64"
    records: list[OobeRecord] = Field(default_factory=list)

    def get(self, image_id: str) -> OobeRecord | None:
        for record in self.records:
            if record.image_id == image_id:
                return record
        return None


# ---------------------------------------------------------------------------
# Devices / templates / AVDs
# ---------------------------------------------------------------------------


class DeviceProfile(_Base):
    id: str
    name: str
    resolution: str = "1080x1920"
    density: int = 420
    ram_mb: int = 2048
    tags: list[str] = Field(default_factory=list)


class Template(_Base):
    id: str
    name: str
    image_id: str
    device_id: str = "pixel_7"
    resolution: str = ""
    density: int | None = None
    ram_mb: int = 2048
    heap_mb: int = 256
    cpu_cores: int = 2
    gpu_mode: GpuMode = GpuMode.SWIFTSHADER_INDIRECT
    headless: bool = True
    proxy_id: str | None = None
    scripts: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    config: dict[str, str] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def disk_estimate_mb(self) -> int:
        return max(2048, self.ram_mb * 2 + 3072)


class InstanceState(StrEnum):
    DEFINED = "defined"
    STOPPED = "stopped"
    STARTING = "starting"
    BOOTING = "booting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"
    UNKNOWN = "unknown"

    @property
    def active(self) -> bool:
        return self in {InstanceState.STARTING, InstanceState.BOOTING, InstanceState.RUNNING}


class InstancePorts(_Base):
    console: int
    adb: int
    grpc: int | None = None
    appium_server: int | None = None
    system_port: int | None = None
    mjpeg: int | None = None


class AvdInfo(_Base):
    name: str
    template_id: str | None = None
    image_id: str | None = None
    image_name: str = ""
    device_id: str = ""
    state: InstanceState = InstanceState.DEFINED
    pid: int | None = None
    ports: InstancePorts | None = None
    proxy_id: str | None = None
    adb_serial: str | None = None
    boot_completed: bool = False
    created_at: datetime | None = None
    started_at: datetime | None = None
    uptime_seconds: float | None = None
    error: str = ""
    config: dict[str, str] = Field(default_factory=dict)
    grpc_endpoint: str | None = None
    screen_available: bool = False


class Snapshot(_Base):
    name: str
    avd_name: str
    created_at: datetime | None = None
    size_bytes: int | None = None
    path: str | None = None
    is_golden: bool = False


class AvdTemplateExport(_Base):
    version: str = "1"
    templates: list[Template] = Field(default_factory=list)
    instances: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------


class ProxyScheme(StrEnum):
    HTTP = "http"
    HTTPS = "https"
    SOCKS5 = "socks5"

    @property
    def is_socks(self) -> bool:
        return self is ProxyScheme.SOCKS5


class ProxyHealthStatus(StrEnum):
    UNKNOWN = "unknown"
    OK = "ok"
    FAILED = "failed"


class ProxyHealth(_Base):
    status: ProxyHealthStatus = ProxyHealthStatus.UNKNOWN
    latency_ms: float | None = None
    checked_at: datetime | None = None
    message: str = ""
    egress_ip: str | None = None


class ProxyConfig(_Base):
    id: str
    name: str = ""
    scheme: ProxyScheme = ProxyScheme.HTTP
    host: str
    port: int = Field(ge=1, le=65535)
    username: str | None = None
    password: SecretStr | None = Field(default=None, repr=False)
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    source: str = ""
    health: ProxyHealth = Field(default_factory=ProxyHealth)
    created_at: datetime | None = None

    @property
    def has_credentials(self) -> bool:
        return bool(self.username or (self.password and self.password.get_secret_value()))

    def url(self, *, reveal: bool = False) -> str:
        userinfo = ""
        if self.username:
            password = self.password.get_secret_value() if self.password else ""
            if reveal and password:
                userinfo = f"{quote(self.username, safe='')}:{quote(password, safe='')}@"
            else:
                userinfo = f"{quote(self.username, safe='')}:***@"
        return f"{self.scheme.value}://{userinfo}{self.host}:{self.port}"

    @property
    def safe_url(self) -> str:
        return self.url(reveal=False)

    def redacted(self) -> ProxyConfig:
        return self.model_copy(update={"password": None})


class ProxyTestResult(_Base):
    proxy_id: str
    ok: bool
    latency_ms: float | None = None
    egress_ip: str | None = None
    status_code: int | None = None
    error: str = ""
    checked_at: datetime = Field(default_factory=utcnow)

    def as_health(self) -> ProxyHealth:
        return ProxyHealth(
            status=ProxyHealthStatus.OK if self.ok else ProxyHealthStatus.FAILED,
            latency_ms=self.latency_ms,
            checked_at=self.checked_at,
            message=self.error,
            egress_ip=self.egress_ip,
        )


class ProxyAssignment(_Base):
    avd_name: str
    proxy_id: str
    assigned_at: datetime | None = None


class ProxyImportResult(_Base):
    added: list[ProxyConfig] = Field(default_factory=list)
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)


class BridgeConfig(_Base):
    instance: str
    listen_host: str = "127.0.0.1"
    listen_port: int
    upstream: str = ""
    mode: Literal["http", "socks5", "direct"] = "http"
    active: bool = True


# ---------------------------------------------------------------------------
# OOBE and scripts
# ---------------------------------------------------------------------------


class OobeResetStrategy(StrEnum):
    SNAPSHOT = "snapshot"
    WIPE = "wipe"
    PACKAGE = "package"
    REBOOT = "reboot"


class OobeResetResult(_Base):
    avd_name: str
    strategy: OobeResetStrategy
    ok: bool
    seconds: float | None = None
    activity: str = ""
    screenshot_path: str = ""
    message: str = ""
    at: datetime = Field(default_factory=utcnow)


class ScriptType(StrEnum):
    ADB_SHELL = "adb_shell"
    HOST_PYTHON = "host_python"
    APPIUM_PYTHON = "appium_python"
    APPIUM_JS = "appium_js"
    INLINE_SHELL = "inline_shell"


class ScriptDefinition(_Base):
    id: str
    name: str
    type: ScriptType = ScriptType.ADB_SHELL
    content: str = ""
    path: str | None = None
    description: str = ""
    timeout_s: int = 120
    env: dict[str, str] = Field(default_factory=dict)
    targets: list[str] = Field(default_factory=list)
    builtin: bool = False
    tags: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def _has_body(self) -> ScriptDefinition:
        if not self.content and not self.path:
            raise ValueError("script requires content or path")
        return self


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def terminal(self) -> bool:
        return self in {
            RunState.SUCCEEDED,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.TIMED_OUT,
        }


class InstanceResult(_Base):
    instance: str
    state: RunState = RunState.QUEUED
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_s: float | None = None
    error: str = ""


class ScriptRunRequest(_Base):
    script_id: str
    instances: list[str] = Field(default_factory=list)
    concurrency: int = 5
    timeout_s: int | None = None
    env: dict[str, str] = Field(default_factory=dict)
    dry_run: bool = False

    @field_validator("concurrency")
    @classmethod
    def _concurrency(cls, value: int) -> int:
        if not 1 <= value <= 64:
            raise ValueError("concurrency must be between 1 and 64")
        return value


class ScriptRun(_Base):
    id: str
    script_id: str
    script_name: str = ""
    type: ScriptType = ScriptType.ADB_SHELL
    state: RunState = RunState.QUEUED
    instances: list[str] = Field(default_factory=list)
    results: list[InstanceResult] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str = ""
    dry_run: bool = False
    requested_by: str = ""
    request: dict[str, Any] = Field(default_factory=dict)

    @property
    def succeeded(self) -> int:
        return sum(1 for result in self.results if result.state is RunState.SUCCEEDED)

    @property
    def failed(self) -> int:
        return sum(
            1
            for result in self.results
            if result.state in {RunState.FAILED, RunState.TIMED_OUT, RunState.CANCELLED}
        )


# ---------------------------------------------------------------------------
# Appium / screen
# ---------------------------------------------------------------------------


class AppiumStatus(_Base):
    running: bool = False
    pid: int | None = None
    port: int | None = None
    version: str = ""
    drivers: list[str] = Field(default_factory=list)
    sessions: int = 0
    url: str = ""
    message: str = ""


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    REBOOT_REQUIRED = "reboot_required"

    @property
    def terminal(self) -> bool:
        return self in {
            StepStatus.SUCCEEDED,
            StepStatus.FAILED,
            StepStatus.SKIPPED,
            StepStatus.REBOOT_REQUIRED,
        }


class InstallStep(_Base):
    id: str
    title: str
    description: str = ""
    phase: str = "core"
    requires_reboot: bool = False
    critical: bool = True
    depends_on: list[str] = Field(default_factory=list)


class StepResult(_Base):
    step_id: str
    status: StepStatus = StepStatus.PENDING
    message: str = ""
    output: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_s: float | None = None


class InstallOptions(_Base):
    accept_android_sdk_licenses: bool = False
    yes: bool = False
    dry_run: bool = False
    repair: bool = False
    skip_appium: bool = False
    skip_studio: bool = False
    skip_scrcpy: bool = False
    sdk_root: str | None = None
    only: list[str] = Field(default_factory=list)
    platform_provider: str | None = None
    # Supplied-prompt §3.1 flags:
    images: list[str] = Field(default_factory=list)
    avd_home: str | None = None
    proxies_file: str | None = None
    download_proxy: str | None = Field(default=None, repr=False)
    no_studio: bool = False


class InstallPlan(_Base):
    host: HostInfo = Field(default_factory=HostInfo)
    steps: list[InstallStep] = Field(default_factory=list)
    dry_run: bool = False
    already_installed: bool = False
    requires_reboot: bool = False


class InstallManifest(_Base):
    version: str = "1"
    avdfleet_version: str = ""
    host: HostInfo = Field(default_factory=HostInfo)
    sdk_root: str = ""
    steps: dict[str, StepResult] = Field(default_factory=dict)
    started_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    resumed_count: int = 0


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


class CheckStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


class DoctorCheck(_Base):
    id: str
    title: str
    status: CheckStatus = CheckStatus.SKIP
    detail: str = ""
    hint: str = ""
    fixable: bool = False
    fix_command: str = ""


class DoctorReport(_Base):
    ok: bool = False
    host: HostInfo = Field(default_factory=HostInfo)
    checks: list[DoctorCheck] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utcnow)
    summary: str = ""


# ---------------------------------------------------------------------------
# Fleet spec / plan
# ---------------------------------------------------------------------------


class FleetTemplateSpec(_Base):
    name: str
    image: str
    device: str = "pixel_7"
    ram_mb: int = 2048
    cpu_cores: int = 2
    heap_mb: int = 256
    gpu_mode: GpuMode = GpuMode.SWIFTSHADER_INDIRECT
    headless: bool = True
    proxy: str | None = None
    scripts: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class FleetProxySpec(_Base):
    id: str
    scheme: ProxyScheme = ProxyScheme.HTTP
    host: str
    port: int
    username: str | None = None
    password_env: str | None = None
    tags: list[str] = Field(default_factory=list)


class FleetInstanceSpec(_Base):
    name: str
    template: str
    proxy: str | None = None
    ram_mb: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FleetSpec(_Base):
    apiVersion: str = "avdfleet/v1"
    kind: str = "Fleet"
    name: str = "fleet"
    templates: list[FleetTemplateSpec] = Field(default_factory=list)
    proxies: list[FleetProxySpec] = Field(default_factory=list)
    instances: list[FleetInstanceSpec] = Field(default_factory=list)
    capacity: int = 64
    settings: dict[str, Any] = Field(default_factory=dict)


class PlanAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    NOOP = "noop"


class PlanChange(_Base):
    action: PlanAction
    kind: Literal["template", "instance", "proxy", "image"]
    name: str
    detail: str = ""
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


class FleetPlan(_Base):
    spec_name: str = "fleet"
    changes: list[PlanChange] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)
    applyable: bool = True
    errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _summary(self) -> FleetPlan:
        counts: dict[str, int] = {}
        for change in self.changes:
            counts[change.action.value] = counts.get(change.action.value, 0) + 1
        # Assign without triggering validate_assignment (which would recurse).
        object.__setattr__(self, "summary", counts)
        return self


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------


class BundleEntry(_Base):
    path: str
    sha256: str
    size: int
    executable: bool = False


class BundleManifest(_Base):
    format_version: str = "1"
    name: str = "avdfleet-portable"
    version: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    host_os: HostOS = HostOS.UNKNOWN
    host_arch: CpuArch = CpuArch.UNKNOWN
    root: str = ""
    offline: bool = False
    entries: list[BundleEntry] = Field(default_factory=list)
    tool_versions: dict[str, str] = Field(default_factory=dict)
    python_version: str = ""
    total_bytes: int = 0
    warnings: list[str] = Field(default_factory=list)

    def checksum_map(self) -> dict[str, str]:
        return {entry.path: entry.sha256 for entry in self.entries}


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


class OperationResult(_Base):
    ok: bool = True
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


def sha256_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_proxy_url(value: str) -> tuple[ProxyScheme, str, int, str | None, str | None]:
    """Parse a proxy URL, returning scheme, host, port, username, password."""
    candidate = value.strip()
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    parts = urlsplit(candidate)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https", "socks5", "socks5h"}:
        raise ValueError(f"unsupported proxy scheme: {scheme!r}")
    if scheme == "socks5h":
        scheme = "socks5"
    if not parts.hostname:
        raise ValueError("proxy URL is missing a host")
    try:
        port = parts.port or (1080 if scheme == "socks5" else 8080)
    except ValueError as exc:
        raise ValueError(f"invalid proxy port: {parts.port}") from exc
    username = parts.username
    password = parts.password
    return ProxyScheme(scheme), parts.hostname, port, username, password


def rebuild_url(
    scheme: str, host: str, port: int, username: str | None, password: str | None
) -> str:
    userinfo = ""
    if username:
        userinfo = quote(username, safe="")
        if password:
            userinfo += ":" + quote(password, safe="")
        userinfo += "@"
    return urlunsplit((scheme, f"{userinfo}{host}:{port}", "", "", ""))
