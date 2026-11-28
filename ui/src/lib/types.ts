/**
 * Wire types for the AVD Fleet API (`/api/v1`).
 *
 * These mirror `src/avdfleet/core/models.py` and `contracts/openapi.yaml`
 * exactly. Keep them in sync with the frozen contract — never invent fields or
 * endpoints here; unknown extra fields are ignored by the client.
 *
 * Conventions:
 * - datetimes are ISO-8601 strings (the API serialises pydantic datetimes).
 * - every object may carry additional properties in future minor versions;
 *   read only what you need.
 */

/* -------------------------------------------------------------------------- */
/* System                                                                      */
/* -------------------------------------------------------------------------- */

export type HostOS = "windows" | "macos" | "linux" | "unknown";
export type CpuArch = "x86_64" | "arm64" | "unknown";
export type AcceleratorKind = "whpx" | "aehd" | "kvm" | "hvf" | "none";
export type Edition = "installed" | "portable";
export type HealthStatus = "ok" | "degraded";

export interface AcceleratorInfo {
  kind: AcceleratorKind;
  available: boolean;
  detail?: string;
  requires_reboot?: boolean;
  nested?: boolean | null;
  raw?: Record<string, unknown>;
}

export interface HostInfo {
  os?: HostOS;
  os_name?: string;
  os_version?: string;
  arch?: CpuArch;
  machine?: string;
  python_version?: string;
  is_admin?: boolean;
  is_wsl?: boolean;
  is_container?: boolean;
  cpu_count?: number;
  total_ram_gb?: number;
  free_disk_gb?: number;
  hypervisor?: AcceleratorInfo;
  acceleration_available?: boolean;
  details?: Record<string, unknown>;
}

export interface SystemInfo extends HostInfo {
  version?: string;
  edition?: Edition;
  uptime_s?: number;
  sdk_root?: string;
  data_dir?: string;
  accelerator_ok?: boolean;
}

export interface HealthResponse {
  status: HealthStatus;
  version: string;
  uptime_s?: number;
  emulator_available?: boolean;
  database?: string;
}

export interface AuthCheck {
  ok: boolean;
  expires_at?: string | null;
}

export interface ConfigView {
  edition?: string;
  api_host?: string;
  api_port?: number;
  max_instances?: number;
  default_ram_mb?: number;
  headless?: boolean;
  gpu_mode?: string;
  sdk_root?: string;
  data_dir?: string;
  auth_required?: boolean;
  version?: string;
  theme?: string;
}

/** Whitelisted subset accepted by `PATCH /api/v1/config`. */
export interface ConfigPatch {
  headless?: boolean;
  gpu_mode?: string;
  max_instances?: number;
  default_ram_mb?: number;
  theme?: string;
  log_level?: string;
}

export interface OperationResult {
  ok: boolean;
  message?: string;
  data?: Record<string, unknown>;
}

/** Envelope returned by every non-2xx API response. */
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    hint?: string;
    details?: Record<string, unknown>;
  };
}

/* -------------------------------------------------------------------------- */
/* Doctor                                                                      */
/* -------------------------------------------------------------------------- */

export type CheckStatus = "ok" | "warn" | "fail" | "skip";

export interface DoctorCheck {
  id: string;
  title: string;
  status: CheckStatus;
  detail?: string;
  hint?: string;
  fixable?: boolean;
  fix_command?: string;
}

export interface DoctorReport {
  ok: boolean;
  host?: HostInfo;
  checks: DoctorCheck[];
  checked_at?: string;
  summary?: string;
}

/* -------------------------------------------------------------------------- */
/* Images / OOBE                                                               */
/* -------------------------------------------------------------------------- */

export type ImageKind =
  | "google_apis"
  | "google_apis_playstore"
  | "aosp"
  | "default"
  | "custom"
  | "unknown";
export type OobeStatus = "verified" | "failed" | "unverified" | "unsupported";
export type OobeResetStrategy = "snapshot" | "wipe" | "package" | "reboot";

export interface SystemImage {
  id: string;
  api_level?: number;
  tag?: string;
  abi?: string;
  kind?: ImageKind;
  display_name?: string;
  package?: string;
  url?: string | null;
  sha256?: string | null;
  size_bytes?: number | null;
  installed?: boolean;
  local_path?: string | null;
  oobe_status?: OobeStatus;
  custom?: boolean;
  notes?: string;
  metadata?: Record<string, unknown>;
}

export interface ImageImportRequest {
  path: string;
  name?: string | null;
  image_id?: string | null;
  api_level?: number | null;
  abi?: string | null;
}

export interface ListImagesQuery {
  include_remote?: boolean;
  installed_only?: boolean;
}

export interface DownloadImageRequest {
  dry_run?: boolean;
}

export interface OobeResetRequest {
  strategy?: OobeResetStrategy;
  verify?: boolean;
}

export interface OobeRecord {
  image_id: string;
  api_level?: number;
  tag?: string;
  abi?: string;
  status: OobeStatus;
  activity?: string;
  first_run_component?: string;
  screenshot_path?: string;
  boot_seconds?: number | null;
  reset_seconds?: number | null;
  checked_at?: string | null;
  checked_by?: string;
  emulator_version?: string;
  notes?: string;
  evidence?: string[];
}

export interface OobeMatrix {
  version?: string;
  generated_at?: string | null;
  host_abi?: string;
  records: OobeRecord[];
}

export interface VerifyOobeRequest {
  image_id: string;
  timeout_s?: number;
  keep_avd?: boolean;
}

/* -------------------------------------------------------------------------- */
/* Templates / devices                                                         */
/* -------------------------------------------------------------------------- */

export type GpuMode =
  | "auto"
  | "host"
  | "swiftshader_indirect"
  | "swiftshader"
  | "angle_indirect"
  | "off";

export interface DeviceProfile {
  id: string;
  name: string;
  resolution?: string;
  density?: number;
  ram_mb?: number;
  tags?: string[];
}

export interface Template {
  id: string;
  name: string;
  image_id: string;
  device_id?: string;
  resolution?: string;
  density?: number | null;
  ram_mb?: number;
  heap_mb?: number;
  cpu_cores?: number;
  gpu_mode?: GpuMode;
  headless?: boolean;
  proxy_id?: string | null;
  scripts?: string[];
  tags?: string[];
  description?: string;
  config?: Record<string, string>;
  created_at?: string | null;
  updated_at?: string | null;
}

/* -------------------------------------------------------------------------- */
/* Instances                                                                   */
/* -------------------------------------------------------------------------- */

export type InstanceState =
  | "defined"
  | "stopped"
  | "starting"
  | "booting"
  | "running"
  | "stopping"
  | "error"
  | "unknown";

export interface InstancePorts {
  console?: number;
  adb?: number;
  grpc?: number | null;
  appium_server?: number | null;
  system_port?: number | null;
  mjpeg?: number | null;
}

export interface AvdInfo {
  name: string;
  template_id?: string | null;
  image_id?: string | null;
  image_name?: string;
  device_id?: string;
  state: InstanceState;
  pid?: number | null;
  ports?: InstancePorts | null;
  proxy_id?: string | null;
  adb_serial?: string | null;
  boot_completed?: boolean;
  created_at?: string | null;
  started_at?: string | null;
  uptime_seconds?: number | null;
  error?: string;
  config?: Record<string, string>;
  grpc_endpoint?: string | null;
  screen_available?: boolean;
}

export interface InstanceCreateRequest {
  template_id: string;
  name: string;
  overrides?: Record<string, unknown>;
}

export interface BulkDeployRequest {
  template_id: string;
  count?: number;
  prefix?: string;
  overrides?: Record<string, unknown>;
}

export interface StartInstanceRequest {
  proxy_id?: string | null;
  headless?: boolean | null;
  dry_run?: boolean;
}

export interface StopInstanceRequest {
  force?: boolean;
  timeout_s?: number | null;
}

export interface WaitBootRequest {
  timeout_s?: number | null;
}

export interface ResetOobeRequest {
  strategy?: OobeResetStrategy;
  verify?: boolean;
}

export interface OobeResetResult {
  avd_name?: string;
  strategy?: OobeResetStrategy;
  ok?: boolean;
  seconds?: number | null;
  activity?: string;
  screenshot_path?: string;
  message?: string;
  at?: string;
}

export interface UpdateInstanceConfigRequest {
  values: Record<string, string>;
  dry_run?: boolean;
}

export interface CloneInstanceRequest {
  name: string;
}

export interface Snapshot {
  name: string;
  avd_name?: string;
  created_at?: string | null;
  size_bytes?: number | null;
  path?: string | null;
  is_golden?: boolean;
}

export interface CreateSnapshotRequest {
  name: string;
  golden?: boolean;
}

export type InputAction = "tap" | "swipe" | "text" | "key";

export interface InputRequest {
  action: InputAction;
  x?: number;
  y?: number;
  x1?: number;
  y1?: number;
  x2?: number;
  y2?: number;
  duration_ms?: number;
  text?: string;
  keycode?: string;
}

/* -------------------------------------------------------------------------- */
/* Proxies                                                                     */
/* -------------------------------------------------------------------------- */

export type ProxyScheme = "http" | "https" | "socks5";
export type ProxyHealthStatus = "unknown" | "ok" | "failed";

export interface ProxyHealth {
  status?: ProxyHealthStatus;
  latency_ms?: number | null;
  checked_at?: string | null;
  message?: string;
  egress_ip?: string | null;
}

/**
 * Passwords are never returned by the API. `has_credentials` tells the UI
 * whether a password (or username) is stored in the encrypted vault.
 */
export interface ProxyView {
  id: string;
  name?: string;
  scheme?: ProxyScheme;
  host: string;
  port: number;
  username?: string | null;
  has_credentials?: boolean;
  tags?: string[];
  enabled?: boolean;
  health?: ProxyHealth;
  created_at?: string | null;
}

export interface ProxyCreateRequest {
  /** `scheme://user:pass@host:port` — alternative to the discrete fields. */
  url?: string;
  name?: string;
  scheme?: ProxyScheme;
  host?: string;
  port?: number;
  username?: string | null;
  password?: string | null;
  tags?: string[];
}

export interface ProxyImportRequest {
  text: string;
  source?: string;
}

export interface ProxyImportResult {
  added: ProxyView[];
  skipped?: number;
  errors?: string[];
}

export interface ProxyTestResult {
  proxy_id: string;
  ok: boolean;
  latency_ms?: number | null;
  egress_ip?: string | null;
  status_code?: number | null;
  error?: string;
  checked_at?: string;
}

export interface VerifyGuestRequest {
  avd_name: string;
  proxy_id: string;
  token_url?: string | null;
  timeout_s?: number | null;
}

export interface ProxyAssignment {
  avd_name: string;
  proxy_id: string;
  assigned_at?: string | null;
}

export interface AssignProxyRequest {
  avd_name: string;
  proxy_id: string;
}

export type AssignStrategy = "round_robin" | "random" | "healthiest";

export interface AutoAssignRequest {
  instances: string[];
  strategy?: AssignStrategy;
}

export type BridgeMode = "http" | "socks5" | "direct";

export interface BridgeConfig {
  instance: string;
  listen_host?: string;
  listen_port: number;
  upstream?: string;
  mode?: BridgeMode;
  active?: boolean;
}

/* -------------------------------------------------------------------------- */
/* Scripts / runs                                                              */
/* -------------------------------------------------------------------------- */

export type ScriptType =
  | "adb_shell"
  | "host_python"
  | "appium_python"
  | "appium_js"
  | "inline_shell";

export interface ScriptDefinition {
  id: string;
  name: string;
  type: ScriptType;
  content?: string;
  path?: string | null;
  description?: string;
  timeout_s?: number;
  env?: Record<string, string>;
  targets?: string[];
  builtin?: boolean;
  tags?: string[];
  created_at?: string | null;
  updated_at?: string | null;
}

export type RunState = "queued" | "running" | "succeeded" | "failed" | "cancelled" | "timed_out";

export interface InstanceResult {
  instance: string;
  state: RunState;
  exit_code?: number | null;
  stdout?: string;
  stderr?: string;
  started_at?: string | null;
  finished_at?: string | null;
  duration_s?: number | null;
  error?: string;
}

export interface ScriptRunRequest {
  script_id: string;
  instances?: string[];
  concurrency?: number;
  timeout_s?: number | null;
  env?: Record<string, string>;
  dry_run?: boolean;
}

export interface ScriptRun {
  id: string;
  script_id: string;
  script_name?: string;
  type?: ScriptType;
  state: RunState;
  instances?: string[];
  results?: InstanceResult[];
  started_at?: string | null;
  finished_at?: string | null;
  error?: string;
  dry_run?: boolean;
  requested_by?: string;
  request?: Record<string, unknown>;
}

/* -------------------------------------------------------------------------- */
/* Appium                                                                      */
/* -------------------------------------------------------------------------- */

export interface AppiumStatus {
  running: boolean;
  pid?: number | null;
  port?: number | null;
  version?: string;
  drivers?: string[];
  sessions?: number;
  url?: string;
  message?: string;
}

export interface AppiumStartRequest {
  port?: number | null;
}

export interface AppiumCapabilitiesRequest {
  avd_name: string;
}

/* -------------------------------------------------------------------------- */
/* Capacity / fleet / portable                                                 */
/* -------------------------------------------------------------------------- */

export interface CapacityInfo {
  max_instances?: number;
  running?: number;
  deployed?: number;
  ram_available_mb?: number;
  ram_required_mb?: number;
  ports?: Record<string, unknown>;
  cpu_cores?: number;
  can_start_more?: number;
  warnings?: string[];
}

export interface FleetRequest {
  spec?: Record<string, unknown>;
  yaml?: string;
  path?: string;
  prune?: boolean;
  dry_run?: boolean;
}

export type PlanAction = "create" | "update" | "delete" | "noop";
export type PlanKind = "template" | "instance" | "proxy" | "image";

export interface PlanChange {
  action: PlanAction;
  kind: PlanKind;
  name: string;
  detail?: string;
  before?: Record<string, unknown> | null;
  after?: Record<string, unknown> | null;
}

export interface FleetPlan {
  spec_name?: string;
  changes?: PlanChange[];
  summary?: Record<string, number>;
  applyable?: boolean;
  errors?: string[];
}

/** Portable/environment check result (also used for `/portable/checks`). */
export interface CheckResult {
  id: string;
  title?: string;
  status: CheckStatus;
  detail?: string;
  value?: string | null;
}

export interface PortableStatus {
  edition?: string;
  root?: string;
  portable?: boolean;
  relocatable?: boolean;
  host_os?: string;
  host_arch?: string;
  checks?: CheckResult[];
  leftovers?: string[];
}

/* -------------------------------------------------------------------------- */
/* Events                                                                      */
/* -------------------------------------------------------------------------- */

export type EventType =
  | "instance.created"
  | "instance.deleted"
  | "instance.state"
  | "instance.boot"
  | "instance.log"
  | "instance.screen"
  | "proxy.created"
  | "proxy.updated"
  | "proxy.checked"
  | "instrumentation.run"
  | "script.run.started"
  | "script.run.output"
  | "script.run.finished"
  | "install.step"
  | "install.finished"
  | "oobe.result"
  | "doctor.finished"
  | "fleet.applied"
  | "system"
  | "error"
  | "log";

/** Envelope for every event on `/ws/events` and `GET /api/v1/events`. */
export interface FleetEvent {
  id: string;
  type: EventType;
  ts: string;
  payload: Record<string, unknown>;
  source?: string;
  seq?: number | null;
}

export interface InstanceStatePayload {
  instance: string;
  state: InstanceState;
  reason?: string;
}

export interface ScriptOutputPayload {
  run_id: string;
  instance: string;
  stream: "stdout" | "stderr";
  line: string;
}

export interface InstallStepPayload {
  step: string;
  status: "pending" | "running" | "succeeded" | "failed" | "skipped" | "reboot_required";
  message?: string;
}

/**
 * Payload of the `instrumentation.run` event published on `/ws/events`.
 *
 * Mirrors `InstrumentationService._emit` (`src/avdfleet/instrumentation/service.py`):
 * `started` carries `profile_id`, `finished` carries `state` and `failed`.
 */
export interface InstrumentationRunEventPayload {
  action: "started" | "finished";
  run_id: string;
  profile_id?: string;
  targets: string[];
  state?: InstrumentationRunState;
  failed?: string[];
}

/* -------------------------------------------------------------------------- */
/* Root & Instrumentation (`/api/v1/instrumentation`)                          */
/* -------------------------------------------------------------------------- */

/**
 * Mirrors `src/avdfleet/instrumentation/models.py`. The OpenAPI contract lists
 * the `/instrumentation` endpoints but describes their responses as free-form
 * objects, so these interfaces are the typed source of truth for the GUI.
 *
 * Naming notes:
 * - `RunState` above belongs to the script runner, so the instrumentation run
 *   state is exported as `InstrumentationRunState` (same wire values as the
 *   backend `RunState`).
 * - `ComponentState` keeps *installed*, *enabled* and *active* apart: a Magisk
 *   module copied onto the device is installed, the persisted switch is
 *   `enabled`, and only a framework pass after a reboot makes it `active`.
 */

export type InstrumentationRunState =
  | "queued"
  | "running"
  | "succeeded"
  | "partial"
  | "failed"
  | "cancelled";

export type ModuleKind = "xposed_apk" | "magisk_zip" | "zygisk_zip" | "lsposed_zip" | "unknown";

export type RootImplementation = "none" | "adb_root" | "magisk";

export type ComponentState = "absent" | "installed" | "enabled" | "active" | "unsupported" | "unknown";

export type StepAction =
  | "verify_root"
  | "install_magisk"
  | "complete_magisk_setup"
  | "enable_zygisk"
  | "disable_zygisk"
  | "install_lsposed"
  | "install_module"
  | "enable_module"
  | "disable_module"
  | "remove_module"
  | "enable_xposed_module"
  | "set_xposed_scope"
  | "enable_adb_root"
  | "enable_writable_system"
  | "install_lsposed_manager"
  | "reboot"
  | "remove_lsposed"
  | "remove_magisk"
  | "verify";

export interface ModuleRecord {
  id: string;
  kind: ModuleKind;
  name: string;
  package_name: string;
  version: string;
  version_code: number;
  min_sdk: number;
  abi: string;
  description: string;
  sha256: string;
  size_bytes: number;
  source: string;
  source_url: string;
  file_path: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface InstalledModule {
  identifier: string;
  kind: ModuleKind;
  version: string;
  enabled: boolean;
  active: boolean;
  source: string;
  /** Application packages this module targets in LSPosed (empty for Magisk modules). */
  scope: string[];
  notes: string[];
}

export interface CapabilityFlags {
  adb_root: boolean;
  writable_system: boolean;
  magisk: boolean;
  magisk_version: string;
  magisk_environment_ready: boolean;
  zygisk_available: boolean;
  zygisk_enabled: boolean;
  zygisk_active: boolean;
  lsposed_installed: boolean;
  lsposed_version: string;
  lsposed_active: boolean;
  reboot_required: boolean;
  notes: string[];
}

export interface DeviceInstrumentation {
  instance: string;
  api_level: number;
  abi: string;
  image_id: string;
  image_tag: string;
  serial: string;
  running: boolean;
  boot_completed: boolean;
  root_implementation: RootImplementation;
  capabilities: CapabilityFlags;
  xposed_modules: InstalledModule[];
  magisk_modules: InstalledModule[];
  compatible: boolean;
  blockers: string[];
  probed_at: string;
}

export interface ProfileStep {
  id: string;
  action: StepAction;
  module_id: string;
  module_identifier: string;
  params: Record<string, unknown>;
}

export interface AutoProfileView {
  instance: string;
  profile_id: string;
}

export interface InstrumentationProfile {
  id: string;
  name: string;
  description: string;
  revision: number;
  steps: ProfileStep[];
  builtin: boolean;
  created_at: string;
  updated_at: string;
}

export interface StepResult {
  step_id: string;
  action: StepAction;
  ok: boolean;
  message: string;
  data: Record<string, unknown>;
  /**
   * Reported by the live backend model but omitted when a run is read back from
   * SQLite (`repository._run_from_row` does not persist it): treat as optional.
   */
  state?: ComponentState;
}

export interface TargetResult {
  instance: string;
  ok: boolean;
  skipped: boolean;
  error: string;
  steps: StepResult[];
}

export interface DeploymentRun {
  id: string;
  profile_id: string;
  label: string;
  state: InstrumentationRunState;
  targets: string[];
  results: TargetResult[];
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string;
}

export interface LogEntry {
  id: number;
  run_id: string;
  instance: string;
  level: string;
  message: string;
  at: string;
}

/** Request body for create/update profile (`POST`/`PUT /instrumentation/profiles`). */
export interface StepBody {
  id?: string;
  action: StepAction;
  module_id?: string;
  module_identifier?: string;
  params?: Record<string, unknown>;
}

/* -------------------------------------------------------------------------- */
/* Toolchain acquisition (`/instrumentation/toolchain`)                        */
/* -------------------------------------------------------------------------- */

/**
 * Mirrors `ArtifactStatus` in `src/avdfleet/instrumentation/acquire.py`.
 *
 * `present` and `verified` are deliberately separate: the store reports a local
 * copy even when its SHA-256/size no longer matches the pin, and a file that
 * failed verification must never be presented as usable.
 *
 * `pinned_*` describe the release this build knows about; `size_bytes`,
 * `sha256`, `path` and `acquired_at` describe what is actually on the host and
 * stay empty while `present` is false.
 */
export type ArtifactKind = "apk" | "module_zip";

export interface ArtifactStatus {
  key: string;
  name: string;
  version: string;
  kind: ArtifactKind;
  min_api: number;
  source_url: string;
  pinned_sha256: string;
  pinned_size_bytes: number;
  present: boolean;
  verified: boolean;
  path: string;
  size_bytes: number;
  sha256: string;
  acquired_at: string;
}

/**
 * One provenance row from the daemon manifest (`manifest.json`). Every field is
 * optional because the API types the list as free-form objects.
 */
export interface ToolchainManifestEntry {
  key?: string;
  name?: string;
  version?: string;
  kind?: string;
  sha256?: string;
  size_bytes?: number;
  source_url?: string;
  licence?: string;
  acquired_at?: string;
}

/** `GET /instrumentation/toolchain`. */
export interface ToolchainView {
  artifacts: ArtifactStatus[];
  manifest: ToolchainManifestEntry[];
  /** False when the daemon has no acquisition store configured. */
  available: boolean;
}

/**
 * `POST /instrumentation/toolchain/{key}/acquire`.
 *
 * `module_id` is empty for an APK (a payload for an external ramdisk patcher,
 * not a final install) and carries the library module id for a module ZIP,
 * which the backend imports before returning.
 */
export interface ToolchainAcquireResult {
  artifact: ArtifactStatus;
  module_id: string;
}
