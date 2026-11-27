/**
 * Typed client for the AVD Fleet REST API (`/api/v1`).
 *
 * - The bearer token is read from `localStorage["avdfleet.token"]`.
 * - Non-2xx responses are parsed from the standard error envelope into an
 *   `ApiError` carrying `status`, `code`, `hint` and `details`.
 * - 401 responses notify subscribers via `onUnauthorized`, which the auth
 *   provider uses to drop the session and show the connect screen.
 * - Every request is bounded by a timeout and can be cancelled by the caller.
 *
 * Endpoint coverage is 1:1 with `contracts/openapi.yaml`. The Root &
 * Instrumentation namespace at the bottom is typed from
 * `src/avdfleet/instrumentation/models.py`, because the contract describes those
 * responses as free-form objects. Do not add methods for endpoints that are not
 * in the contract.
 */

import {
  TOKEN_STORAGE_KEY,
  buildApiUrl,
  buildRootUrl,
  type QueryValue,
} from "./config";
import type {
  AppiumCapabilitiesRequest,
  AppiumStartRequest,
  AppiumStatus,
  AuthCheck,
  AutoAssignRequest,
  AvdInfo,
  BridgeConfig,
  BulkDeployRequest,
  CapacityInfo,
  CloneInstanceRequest,
  ConfigPatch,
  ConfigView,
  CreateSnapshotRequest,
  DeploymentRun,
  DeviceInstrumentation,
  DeviceProfile,
  DoctorReport,
  DownloadImageRequest,
  FleetPlan,
  FleetRequest,
  HealthResponse,
  ImageImportRequest,
  InputRequest,
  InstanceCreateRequest,
  InstrumentationProfile,
  ListImagesQuery,
  LogEntry,
  ModuleKind,
  ModuleRecord,
  OobeMatrix,
  OobeRecord,
  OobeResetRequest,
  OobeResetResult,
  OperationResult,
  PortableStatus,
  ProxyAssignment,
  ProxyCreateRequest,
  ProxyImportRequest,
  ProxyImportResult,
  ProxyTestResult,
  ProxyView,
  ScriptDefinition,
  ScriptRun,
  ScriptRunRequest,
  Snapshot,
  StartInstanceRequest,
  StepBody,
  StopInstanceRequest,
  SystemImage,
  SystemInfo,
  Template,
  ToolchainAcquireResult,
  ToolchainView,
  UpdateInstanceConfigRequest,
  VerifyGuestRequest,
  VerifyOobeRequest,
  WaitBootRequest,
} from "./types";

const DEFAULT_TIMEOUT_MS = 15_000;
const HEALTH_TIMEOUT_MS = 4_000;

/* -------------------------------------------------------------------------- */
/* Errors                                                                      */
/* -------------------------------------------------------------------------- */

export type ApiErrorKind = "http" | "network" | "timeout" | "aborted" | "parse";

export interface ApiErrorInit {
  kind: ApiErrorKind;
  status?: number;
  code?: string;
  hint?: string;
  details?: Record<string, unknown>;
  url?: string;
  cause?: unknown;
}

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number;
  readonly code: string;
  readonly hint: string | undefined;
  readonly details: Record<string, unknown> | undefined;
  readonly url: string | undefined;

  constructor(message: string, init: ApiErrorInit) {
    super(message, init.cause !== undefined ? { cause: init.cause } : undefined);
    this.name = "ApiError";
    this.kind = init.kind;
    this.status = init.status ?? 0;
    this.code = init.code ?? init.kind;
    this.hint = init.hint;
    this.details = init.details;
    this.url = init.url;
  }

  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  get isNetworkError(): boolean {
    return this.kind === "network" || this.kind === "timeout";
  }
}

/* -------------------------------------------------------------------------- */
/* Token storage + unauthorized signal                                         */
/* -------------------------------------------------------------------------- */

export function getStoredToken(): string | null {
  try {
    const value = window.localStorage.getItem(TOKEN_STORAGE_KEY);
    return value && value.trim() ? value.trim() : null;
  } catch {
    return null;
  }
}

export function setStoredToken(token: string | null): void {
  try {
    if (token && token.trim()) {
      window.localStorage.setItem(TOKEN_STORAGE_KEY, token.trim());
    } else {
      window.localStorage.removeItem(TOKEN_STORAGE_KEY);
    }
  } catch {
    /* storage may be unavailable (private mode); the in-memory state still works */
  }
}

export function clearStoredToken(): void {
  setStoredToken(null);
}

type UnauthorizedListener = () => void;
const unauthorizedListeners = new Set<UnauthorizedListener>();

/** Subscribe to 401 responses. Returns an unsubscribe function. */
export function onUnauthorized(listener: UnauthorizedListener): () => void {
  unauthorizedListeners.add(listener);
  return () => {
    unauthorizedListeners.delete(listener);
  };
}

function emitUnauthorized(): void {
  for (const listener of [...unauthorizedListeners]) {
    try {
      listener();
    } catch {
      /* a broken listener must never mask the API error */
    }
  }
}

/* -------------------------------------------------------------------------- */
/* Transport                                                                   */
/* -------------------------------------------------------------------------- */

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface RequestOptions {
  method?: HttpMethod;
  body?: unknown;
  query?: Record<string, QueryValue>;
  timeoutMs?: number;
  signal?: AbortSignal;
  /** Attach the bearer token (default true). `/health` and login use false. */
  auth?: boolean;
  /** Resolve against the daemon root instead of `/api/v1` (only `/health`). */
  root?: boolean;
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) {
    if (error.name === "AbortError" || error.name === "TimeoutError") {
      return "the request timed out";
    }
    return error.message;
  }
  return String(error);
}

function parseErrorBody(body: unknown): {
  code?: string;
  message?: string;
  hint?: string;
  details?: Record<string, unknown>;
} {
  if (typeof body !== "object" || body === null) return {};
  const envelope = (body as { error?: unknown }).error;
  if (typeof envelope !== "object" || envelope === null) return {};
  const record = envelope as Record<string, unknown>;
  return {
    code: typeof record.code === "string" ? record.code : undefined,
    message: typeof record.message === "string" ? record.message : undefined,
    hint: typeof record.hint === "string" ? record.hint : undefined,
    details:
      typeof record.details === "object" && record.details !== null
        ? (record.details as Record<string, unknown>)
        : undefined,
  };
}

/**
 * AbortSignal works for real browser fetches, but in hybrid environments (jsdom
 * DOM + Node undici fetch) the two realms' AbortSignal classes are distinct and
 * undici throws on the foreign signal. Probe with the same `Request`
 * implementation fetch uses and fall back to race-based cancellation.
 */
function canPassAbortSignal(url: string): boolean {
  try {
    if (typeof AbortController === "undefined" || typeof Request === "undefined") return false;
    const probe = new AbortController();
    new Request(url, { method: "GET", signal: probe.signal });
    return true;
  } catch {
    return false;
  }
}

async function httpFetch(path: string, options: RequestOptions = {}): Promise<Response> {
  const { method = "GET", body, query, timeoutMs = DEFAULT_TIMEOUT_MS, signal, auth = true, root = false } = options;
  const url = root ? buildRootUrl(path, query) : buildApiUrl(path, query);
  const headers = new Headers({ Accept: "application/json" });
  if (auth) {
    const token = getStoredToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
  }
  let payload: string | FormData | undefined;
  if (body !== undefined) {
    if (body instanceof FormData) {
      // Multipart uploads: the browser must set the boundary itself, so no
      // Content-Type header is written here.
      payload = body;
    } else {
      headers.set("Content-Type", "application/json");
      payload = JSON.stringify(body);
    }
  }

  // In a browser the global fetch and window.fetch are the same function, so a
  // signal created here is realm-compatible and can abort the real request.
  // Under jsdom the fetch is Node's undici fetch while AbortController comes
  // from jsdom, and passing that signal throws a cross-realm TypeError. In
  // that case we bound the request with Promise.race instead (the underlying
  // fetch settles into a no-op), keeping timeout/abort semantics identical.
  const nativeSignalSupport = canPassAbortSignal(url);
  const controller = nativeSignalSupport ? new AbortController() : null;
  let timedOut = false;
  let abortedByCaller = false;
  let cleanup: () => void = () => undefined;

  const boundary = new Promise<never>((_resolve, reject) => {
    const timer = setTimeout(() => {
      timedOut = true;
      controller?.abort();
      reject(
        new ApiError(`The daemon did not respond within ${(timeoutMs / 1000).toFixed(0)} s`, {
          kind: "timeout",
          url,
        }),
      );
    }, timeoutMs);
    const onCallerAbort = (): void => {
      abortedByCaller = true;
      controller?.abort();
      reject(new ApiError("Request cancelled", { kind: "aborted", url }));
    };
    cleanup = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onCallerAbort);
    };
    if (signal) {
      if (signal.aborted) {
        onCallerAbort();
      } else {
        signal.addEventListener("abort", onCallerAbort, { once: true });
      }
    }
  });

  let response: Response;
  try {
    response = await Promise.race([
      fetch(url, {
        method,
        headers,
        body: payload,
        credentials: "same-origin",
        ...(controller ? { signal: controller.signal } : {}),
      }),
      boundary,
    ]);
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (timedOut) {
      throw new ApiError(`The daemon did not respond within ${(timeoutMs / 1000).toFixed(0)} s`, {
        kind: "timeout",
        url,
        cause: error,
      });
    }
    if (abortedByCaller || signal?.aborted) {
      throw new ApiError("Request cancelled", { kind: "aborted", url, cause: error });
    }
    throw new ApiError(`Cannot reach the AVD Fleet daemon: ${errorMessage(error)}`, {
      kind: "network",
      url,
      cause: error,
    });
  } finally {
    cleanup();
  }

  if (response.status === 401) emitUnauthorized();

  if (!response.ok) {
    let body: unknown;
    try {
      const text = await response.text();
      body = text ? JSON.parse(text) : undefined;
    } catch {
      body = undefined;
    }
    const parsed = parseErrorBody(body);
    throw new ApiError(parsed.message ?? `Request failed with HTTP ${response.status}`, {
      kind: "http",
      status: response.status,
      code: parsed.code ?? `http_${response.status}`,
      hint: parsed.hint,
      details: parsed.details,
      url,
    });
  }
  return response;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await httpFetch(path, options);
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  if (!text) return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch (error) {
    throw new ApiError("The daemon returned malformed JSON", {
      kind: "parse",
      status: response.status,
      code: "malformed_response",
      url: response.url,
      cause: error,
    });
  }
}

/* -------------------------------------------------------------------------- */
/* Typed API surface                                                           */
/* -------------------------------------------------------------------------- */

export const api = {
  // -- system ---------------------------------------------------------------
  health: () => request<HealthResponse>("/health", { auth: false, root: true, timeoutMs: HEALTH_TIMEOUT_MS }),
  checkAuth: (timeoutMs = HEALTH_TIMEOUT_MS) =>
    request<AuthCheck>("/auth/check", { auth: true, timeoutMs }),
  getSystem: () => request<SystemInfo>("/system"),
  getConfig: () => request<ConfigView>("/config"),
  updateConfig: (patch: ConfigPatch) => request<ConfigView>("/config", { method: "PATCH", body: patch }),
  runDoctor: (slow = false) => request<DoctorReport>("/doctor", { query: { slow } }),
  fixDoctorCheck: (checkId: string) =>
    request<OperationResult>("/doctor/fix", { method: "POST", body: { check_id: checkId } }),
  getCapacity: () => request<CapacityInfo>("/capacity"),

  // -- images ---------------------------------------------------------------
  listImages: (query: ListImagesQuery = {}) =>
    request<SystemImage[]>("/images", {
      query: {
        include_remote: query.include_remote ?? true,
        installed_only: query.installed_only ?? false,
      },
    }),
  importImage: (payload: ImageImportRequest) =>
    request<SystemImage>("/images", { method: "POST", body: payload }),
  refreshImages: () => request<SystemImage[]>("/images/refresh", { method: "POST", timeoutMs: 60_000 }),
  getImage: (imageId: string) => request<SystemImage>(`/images/${encodeURIComponent(imageId)}`),
  downloadImage: (imageId: string, payload: DownloadImageRequest = {}) =>
    request<SystemImage>(`/images/${encodeURIComponent(imageId)}/download`, {
      method: "POST",
      body: payload,
      timeoutMs: 10 * 60_000,
    }),

  // -- oobe -----------------------------------------------------------------
  getOobeMatrix: () => request<OobeMatrix>("/oobe/matrix"),
  verifyOobeImage: (payload: VerifyOobeRequest) =>
    request<OobeRecord>("/oobe/verify", { method: "POST", body: payload, timeoutMs: 30 * 60_000 }),
  getOobeRecord: (imageId: string) =>
    request<OobeRecord>(`/oobe/records/${encodeURIComponent(imageId)}`),

  // -- templates / devices --------------------------------------------------
  listTemplates: () => request<Template[]>("/templates"),
  getTemplate: (templateId: string) =>
    request<Template>(`/templates/${encodeURIComponent(templateId)}`),
  createTemplate: (template: Template) =>
    request<Template>("/templates", { method: "POST", body: template }),
  updateTemplate: (templateId: string, template: Template) =>
    request<Template>(`/templates/${encodeURIComponent(templateId)}`, {
      method: "PUT",
      body: template,
    }),
  deleteTemplate: (templateId: string) =>
    request<void>(`/templates/${encodeURIComponent(templateId)}`, { method: "DELETE" }),
  listDeviceProfiles: () => request<DeviceProfile[]>("/devices"),

  // -- instances ------------------------------------------------------------
  listInstances: () => request<AvdInfo[]>("/instances"),
  getInstance: (name: string) => request<AvdInfo>(`/instances/${encodeURIComponent(name)}`),
  createInstance: (payload: InstanceCreateRequest) =>
    request<AvdInfo>("/instances", { method: "POST", body: payload }),
  bulkDeployInstances: (payload: BulkDeployRequest) =>
    request<AvdInfo[]>("/instances/bulk", { method: "POST", body: payload, timeoutMs: 120_000 }),
  deleteInstance: (name: string, deleteFiles = true) =>
    request<void>(`/instances/${encodeURIComponent(name)}`, {
      method: "DELETE",
      query: { delete_files: deleteFiles },
    }),
  startInstance: (name: string, payload: StartInstanceRequest = {}) =>
    request<AvdInfo>(`/instances/${encodeURIComponent(name)}/start`, {
      method: "POST",
      body: payload,
      timeoutMs: 120_000,
    }),
  stopInstance: (name: string, payload: StopInstanceRequest = {}) =>
    request<AvdInfo>(`/instances/${encodeURIComponent(name)}/stop`, {
      method: "POST",
      body: payload,
      timeoutMs: 120_000,
    }),
  restartInstance: (name: string) =>
    request<AvdInfo>(`/instances/${encodeURIComponent(name)}/restart`, {
      method: "POST",
      timeoutMs: 180_000,
    }),
  waitBoot: (name: string, payload: WaitBootRequest = {}) =>
    request<AvdInfo>(`/instances/${encodeURIComponent(name)}/wait-boot`, {
      method: "POST",
      body: payload,
      timeoutMs: 10 * 60_000,
    }),
  resetInstanceOobe: (name: string, payload: OobeResetRequest = {}) =>
    request<OobeResetResult>(`/instances/${encodeURIComponent(name)}/reset-oobe`, {
      method: "POST",
      body: payload,
      timeoutMs: 10 * 60_000,
    }),
  getScreenshot: async (name: string): Promise<Blob> => {
    const response = await httpFetch(`/instances/${encodeURIComponent(name)}/screenshot`, {
      timeoutMs: 30_000,
    });
    return response.blob();
  },
  updateInstanceConfig: (name: string, payload: UpdateInstanceConfigRequest) =>
    request<AvdInfo>(`/instances/${encodeURIComponent(name)}/config`, { method: "PUT", body: payload }),
  cloneInstance: (name: string, payload: CloneInstanceRequest) =>
    request<AvdInfo>(`/instances/${encodeURIComponent(name)}/clone`, { method: "POST", body: payload }),
  listSnapshots: (name: string) =>
    request<Snapshot[]>(`/instances/${encodeURIComponent(name)}/snapshots`),
  createSnapshot: (name: string, payload: CreateSnapshotRequest) =>
    request<Snapshot>(`/instances/${encodeURIComponent(name)}/snapshots`, {
      method: "POST",
      body: payload,
      timeoutMs: 120_000,
    }),
  loadSnapshot: (name: string, snapshot: string) =>
    request<Snapshot>(
      `/instances/${encodeURIComponent(name)}/snapshots/${encodeURIComponent(snapshot)}/load`,
      { method: "POST", timeoutMs: 120_000 },
    ),
  deleteSnapshot: (name: string, snapshot: string) =>
    request<void>(`/instances/${encodeURIComponent(name)}/snapshots/${encodeURIComponent(snapshot)}`, {
      method: "DELETE",
    }),
  sendInput: (name: string, payload: InputRequest) =>
    request<OperationResult>(`/instances/${encodeURIComponent(name)}/input`, {
      method: "POST",
      body: payload,
    }),

  // -- proxies --------------------------------------------------------------
  listProxies: (includeDisabled = false) =>
    request<ProxyView[]>("/proxies", { query: { include_disabled: includeDisabled } }),
  getProxy: (proxyId: string) => request<ProxyView>(`/proxies/${encodeURIComponent(proxyId)}`),
  createProxy: (payload: ProxyCreateRequest) =>
    request<ProxyView>("/proxies", { method: "POST", body: payload }),
  updateProxy: (proxyId: string, patch: Partial<ProxyCreateRequest> & Record<string, unknown>) =>
    request<ProxyView>(`/proxies/${encodeURIComponent(proxyId)}`, { method: "PATCH", body: patch }),
  deleteProxy: (proxyId: string) =>
    request<void>(`/proxies/${encodeURIComponent(proxyId)}`, { method: "DELETE" }),
  importProxies: (payload: ProxyImportRequest) =>
    request<ProxyImportResult>("/proxies/import", { method: "POST", body: payload }),
  checkProxy: (proxyId: string) =>
    request<ProxyTestResult>(`/proxies/${encodeURIComponent(proxyId)}/check`, {
      method: "POST",
      timeoutMs: 60_000,
    }),
  checkAllProxies: () =>
    request<ProxyTestResult[]>("/proxies/check-all", { method: "POST", timeoutMs: 120_000 }),
  verifyGuestEgress: (payload: VerifyGuestRequest) =>
    request<ProxyTestResult>("/proxies/verify-guest", {
      method: "POST",
      body: payload,
      timeoutMs: 120_000,
    }),
  listAssignments: () => request<ProxyAssignment[]>("/assignments"),
  assignProxy: (payload: { avd_name: string; proxy_id: string }) =>
    request<ProxyAssignment>("/assignments", { method: "POST", body: payload }),
  unassignProxy: (avdName: string) =>
    request<void>(`/assignments/${encodeURIComponent(avdName)}`, { method: "DELETE" }),
  autoAssignProxies: (payload: AutoAssignRequest) =>
    request<ProxyAssignment[]>("/assignments/auto", { method: "POST", body: payload }),
  listBridges: () => request<BridgeConfig[]>("/bridges"),

  // -- scripts / runs -------------------------------------------------------
  listScripts: () => request<ScriptDefinition[]>("/scripts"),
  getScript: (scriptId: string) =>
    request<ScriptDefinition>(`/scripts/${encodeURIComponent(scriptId)}`),
  createScript: (script: ScriptDefinition) =>
    request<ScriptDefinition>("/scripts", { method: "POST", body: script }),
  updateScript: (scriptId: string, script: ScriptDefinition) =>
    request<ScriptDefinition>(`/scripts/${encodeURIComponent(scriptId)}`, {
      method: "PUT",
      body: script,
    }),
  deleteScript: (scriptId: string) =>
    request<void>(`/scripts/${encodeURIComponent(scriptId)}`, { method: "DELETE" }),
  startScriptRun: (payload: ScriptRunRequest) =>
    request<ScriptRun>("/scripts/runs", { method: "POST", body: payload }),
  listRuns: (limit = 50) => request<ScriptRun[]>("/runs", { query: { limit } }),
  getRun: (runId: string) => request<ScriptRun>(`/runs/${encodeURIComponent(runId)}`),
  cancelRun: (runId: string) =>
    request<ScriptRun>(`/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" }),

  // -- appium ---------------------------------------------------------------
  getAppiumStatus: () => request<AppiumStatus>("/appium"),
  startAppium: (payload: AppiumStartRequest = {}) =>
    request<AppiumStatus>("/appium/start", { method: "POST", body: payload, timeoutMs: 60_000 }),
  stopAppium: () => request<AppiumStatus>("/appium/stop", { method: "POST", timeoutMs: 30_000 }),
  getAppiumCapabilities: (payload: AppiumCapabilitiesRequest) =>
    request<Record<string, unknown>>("/appium/capabilities", { method: "POST", body: payload }),

  // -- fleet ----------------------------------------------------------------
  planFleet: (payload: FleetRequest) =>
    request<FleetPlan>("/fleet/plan", { method: "POST", body: payload, timeoutMs: 60_000 }),
  applyFleet: (payload: FleetRequest) =>
    request<FleetPlan>("/fleet/apply", { method: "POST", body: payload, timeoutMs: 10 * 60_000 }),

  // -- portable -------------------------------------------------------------
  getPortableStatus: () => request<PortableStatus>("/portable/status"),
  getPortableChecks: () =>
    request<import("./types").CheckResult[]>("/portable/checks"),

  // -- events ---------------------------------------------------------------
  listEvents: (limit = 100) =>
    request<import("./types").FleetEvent[]>("/events", { query: { limit } }),
} as const;

export type ApiClient = typeof api;

/** Convenience: `/health` lives at the daemon root, not under `/api/v1`. */
export const healthUrl = (): string => buildRootUrl("/health");

/* -------------------------------------------------------------------------- */
/* Root & Instrumentation                                                      */
/* -------------------------------------------------------------------------- */

/**
 * Typed client for `/api/v1/instrumentation`.
 *
 * Implemented by `src/avdfleet/api/routes/instrumentation.py`; the shapes come
 * from `src/avdfleet/instrumentation/models.py` because the OpenAPI contract
 * declares the responses as free-form objects. Every call goes through the
 * shared transport: bearer token, timeout and `ApiError` parsing.
 */
export const instrumentationApi = {
  /** `GET /instrumentation/capabilities?targets=a,b` — probe without scanning twice. */
  getCapabilities: (targets: string[]) =>
    request<{ devices: DeviceInstrumentation[] }>("/instrumentation/capabilities", {
      query: { targets: targets.join(",") },
      timeoutMs: 120_000,
    }),

  /** `POST /instrumentation/scan` — probe every selected AVD. */
  scan: (targets: string[]) =>
    request<{ devices: DeviceInstrumentation[] }>("/instrumentation/scan", {
      method: "POST",
      body: { targets },
      timeoutMs: 180_000,
    }),

  /** `GET /instrumentation/modules?kind=` — the central module library. */
  listModules: (kind?: ModuleKind) =>
    request<{ modules: ModuleRecord[] }>("/instrumentation/modules", { query: { kind } }),

  /** `POST /instrumentation/modules/import` — multipart `file` field. */
  importModuleFile: (file: File) => {
    const form = new FormData();
    form.append("file", file, file.name);
    return request<ModuleRecord>("/instrumentation/modules/import", {
      method: "POST",
      body: form,
      timeoutMs: 5 * 60_000,
    });
  },

  /** `POST /instrumentation/modules/import` — JSON `{"path": "…"}` (daemon host path). */
  importModulePath: (path: string) =>
    request<ModuleRecord>("/instrumentation/modules/import", { method: "POST", body: { path } }),

  /** `POST /instrumentation/modules/import` — JSON `{"url": "https://…"}`. */
  importModuleUrl: (url: string) =>
    request<ModuleRecord>("/instrumentation/modules/import", {
      method: "POST",
      body: { url },
      timeoutMs: 5 * 60_000,
    }),

  deleteModule: (moduleId: string) =>
    request<void>(`/instrumentation/modules/${encodeURIComponent(moduleId)}`, { method: "DELETE" }),

  listProfiles: () =>
    request<{ profiles: InstrumentationProfile[] }>("/instrumentation/profiles"),

  createProfile: (body: { name: string; description: string; steps: StepBody[] }) =>
    request<InstrumentationProfile>("/instrumentation/profiles", { method: "POST", body }),

  updateProfile: (
    profileId: string,
    body: { name: string; description: string; steps: StepBody[] },
  ) =>
    request<InstrumentationProfile>(
      `/instrumentation/profiles/${encodeURIComponent(profileId)}`,
      { method: "PUT", body },
    ),

  deleteProfile: (profileId: string) =>
    request<void>(`/instrumentation/profiles/${encodeURIComponent(profileId)}`, {
      method: "DELETE",
    }),

  /** `POST /instrumentation/profiles/{id}/clone` — copy any profile into an editable one. */
  cloneProfile: (profileId: string, name = "") =>
    request<InstrumentationProfile>(
      `/instrumentation/profiles/${encodeURIComponent(profileId)}/clone`,
      { method: "POST", body: { name } },
    ),

  createDeployment: (body: { profile_id: string; targets: string[]; label?: string }) =>
    request<DeploymentRun>("/instrumentation/deployments", { method: "POST", body }),

  listDeployments: (limit = 50) =>
    request<{ runs: DeploymentRun[] }>("/instrumentation/deployments", { query: { limit } }),

  getDeployment: (runId: string) =>
    request<DeploymentRun>(`/instrumentation/deployments/${encodeURIComponent(runId)}`),

  cancelDeployment: (runId: string) =>
    request<DeploymentRun>(
      `/instrumentation/deployments/${encodeURIComponent(runId)}/cancel`,
      { method: "POST" },
    ),

  listDeploymentLogs: (runId: string, limit = 500) =>
    request<{ logs: LogEntry[] }>(
      `/instrumentation/deployments/${encodeURIComponent(runId)}/logs`,
      { query: { limit } },
    ),

  /**
   * `GET /instrumentation/toolchain` — pinned GPL-3.0 artifacts and whether a
   * verified copy is already on the daemon. `available: false` means the daemon
   * has no acquisition store configured; nothing can be downloaded.
   */
  getToolchain: () => request<ToolchainView>("/instrumentation/toolchain"),

  /**
   * `POST /instrumentation/toolchain/{key}/acquire` — download, verify and (for
   * a module ZIP) import the pinned release. Without `confirm: true` the API
   * answers HTTP 409 with a consent message and performs no download.
   */
  acquireToolchain: (key: string, confirm: boolean) =>
    request<ToolchainAcquireResult>(
      `/instrumentation/toolchain/${encodeURIComponent(key)}/acquire`,
      { method: "POST", body: { confirm }, timeoutMs: 10 * 60_000 },
    ),
} as const;

export type InstrumentationApiClient = typeof instrumentationApi;
