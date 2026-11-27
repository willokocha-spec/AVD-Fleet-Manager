/**
 * MSW request handlers for every endpoint in `contracts/openapi.yaml`.
 *
 * The mock daemon is stateful: creates, updates, start/stop, runs, proxy health
 * and OOBE verification are reflected in subsequent reads, so pages behave like
 * they do against the real API. Passwords are never returned (only
 * `has_credentials`), mirroring the contract's redaction rules.
 *
 * Handler paths are origin-relative so the same handlers work in the browser
 * worker and in Node (tests).
 */
import { HttpResponse, delay, http } from "msw";

import type {
  AvdInfo,
  BridgeConfig,
  ImageImportRequest,
  InstanceResult,
  OobeResetResult,
  ProxyAssignment,
  ProxyTestResult,
  ProxyView,
  ScriptDefinition,
  ScriptRun,
  Snapshot,
  SystemImage,
  Template,
} from "@/lib/types";

import { getDb } from "./db";

const API = "/api/v1";

const json = <T>(data: T, status = 200): Response =>
  HttpResponse.json(data as unknown as Record<string, unknown>, { status });

function jsonError(status: number, code: string, message: string, hint?: string): Response {
  return HttpResponse.json({ error: { code, message, ...(hint ? { hint } : {}) } }, { status });
}

async function latency(): Promise<void> {
  if (import.meta.env.MODE !== "test") {
    await delay(70 + Math.floor(Math.random() * 150));
  }
}

function hasToken(request: Request): boolean {
  const header = request.headers.get("Authorization") ?? "";
  return /^Bearer\s+\S+/i.test(header);
}

function guard(request: Request): Response | null {
  return hasToken(request) ? null : jsonError(401, "unauthorized", "Missing or invalid bearer token.");
}

function param(params: Record<string, string | readonly string[] | undefined>, key: string): string {
  const value = params[key];
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value[0] ?? "";
  return "";
}

function newId(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}${Math.floor(Math.random() * 1000)
    .toString(36)
    .padStart(2, "0")}`;
}

const nowIso = (): string => new Date().toISOString();

function findInstance(name: string): AvdInfo | undefined {
  return getDb().instances.find((instance) => instance.name === name);
}

function findProxy(id: string): ProxyView | undefined {
  return getDb().proxies.find((proxy) => proxy.id === id);
}

function startInstance(instance: AvdInfo, proxyId?: string | null): AvdInfo {
  if (proxyId !== undefined && proxyId !== null) instance.proxy_id = proxyId;
  instance.state = "running";
  instance.boot_completed = true;
  instance.pid = instance.pid ?? 20_000 + Math.floor(Math.random() * 10_000);
  instance.started_at = nowIso();
  instance.uptime_seconds = 0;
  if (!instance.ports) {
    const consolePort = 5554 + getDb().instances.indexOf(instance) * 2;
    instance.ports = { console: consolePort, adb: consolePort + 1, grpc: consolePort + 3000 };
  }
  return instance;
}

function checkProxy(proxy: ProxyView): ProxyTestResult {
  const failed = !proxy.enabled || proxy.id === "px-socks-1";
  const result: ProxyTestResult = {
    proxy_id: proxy.id,
    ok: !failed,
    latency_ms: failed ? null : 60 + Math.floor(Math.random() * 120),
    egress_ip: failed ? null : `203.0.113.${20 + Math.floor(Math.random() * 60)}`,
    status_code: failed ? null : 200,
    error: failed ? (proxy.enabled ? "upstream handshake timed out" : "proxy is disabled") : "",
    checked_at: nowIso(),
  };
  proxy.health = {
    status: result.ok ? "ok" : "failed",
    latency_ms: result.latency_ms,
    checked_at: result.checked_at,
    message: result.error,
    egress_ip: result.egress_ip,
  };
  return result;
}

/** One transparent 1×1 PNG for screenshot responses. */
const SCREENSHOT_PNG = Uint8Array.from(
  atob("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="),
  (char) => char.charCodeAt(0),
);

export const handlers = [
  /* -- system ------------------------------------------------------------ */
  http.get("/health", async () => {
    await latency();
    const db = getDb();
    return json({
      status: "ok",
      version: db.system.version ?? "0.1.0",
      uptime_s: db.system.uptime_s ?? 0,
      emulator_available: false,
      database: "ok",
    });
  }),

  http.get(`${API}/auth/check`, async ({ request }) => {
    await latency();
    if (!hasToken(request)) return jsonError(401, "unauthorized", "Missing or invalid bearer token.");
    return json({ ok: true, expires_at: null });
  }),

  http.get(`${API}/system`, async ({ request }) => {
    await latency();
    return guard(request) ?? json(getDb().system);
  }),

  http.get(`${API}/config`, async ({ request }) => {
    await latency();
    return guard(request) ?? json(getDb().config);
  }),

  http.patch(`${API}/config`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const patch = (await request.json()) as Record<string, unknown>;
    const allowed = ["headless", "gpu_mode", "max_instances", "default_ram_mb", "theme", "log_level"] as const;
    for (const key of allowed) {
      if (key in patch) (getDb().config as Record<string, unknown>)[key] = patch[key];
    }
    return json(getDb().config);
  }),

  http.get(`${API}/doctor`, async ({ request }) => {
    await latency();
    return guard(request) ?? json(getDb().doctor);
  }),

  http.post(`${API}/doctor/fix`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const body = (await request.json()) as { check_id?: string };
    const check = getDb().doctor.checks.find((candidate) => candidate.id === body.check_id);
    if (!check) return jsonError(404, "not_found", `Unknown doctor check: ${body.check_id ?? ""}`);
    if (!check.fixable) {
      return json({ ok: false, message: `${check.title} cannot be fixed automatically.` });
    }
    check.status = "ok";
    check.detail = "Fixed by the mock doctor.";
    return json({ ok: true, message: `Applied fix for ${check.title}.` });
  }),

  http.get(`${API}/capacity`, async ({ request }) => {
    await latency();
    return guard(request) ?? json(getDb().capacity);
  }),

  /* -- images ------------------------------------------------------------ */
  http.post(`${API}/images/refresh`, async ({ request }) => {
    await latency();
    return guard(request) ?? json(getDb().images);
  }),

  http.get(`${API}/images`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const url = new URL(request.url);
    const installedOnly = url.searchParams.get("installed_only") === "true";
    const images = getDb().images.filter((image) => !installedOnly || image.installed);
    return json(images);
  }),

  http.post(`${API}/images`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const body = (await request.json()) as ImageImportRequest;
    if (!body.path) return jsonError(422, "invalid_request", "path is required");
    const image: SystemImage = {
      id: body.image_id ?? newId("img-custom"),
      api_level: body.api_level ?? 0,
      tag: "custom",
      abi: body.abi ?? "x86_64",
      kind: "custom",
      display_name: body.name ?? body.path.split(/[\\/]/).pop() ?? "Custom image",
      package: "",
      installed: true,
      local_path: body.path,
      oobe_status: "unverified",
      custom: true,
      notes: "Imported from a host directory.",
    };
    getDb().images.unshift(image);
    return json(image, 201);
  }),

  http.get(`${API}/images/:image_id`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const image = getDb().images.find((candidate) => candidate.id === param(params, "image_id"));
    return image ? json(image) : jsonError(404, "not_found", "Image not found");
  }),

  http.post(`${API}/images/:image_id/download`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const image = getDb().images.find((candidate) => candidate.id === param(params, "image_id"));
    if (!image) return jsonError(404, "not_found", "Image not found");
    image.installed = true;
    image.local_path = image.local_path ?? `/sdk/system-images/${image.id}`;
    return json(image);
  }),

  /* -- OOBE -------------------------------------------------------------- */
  http.get(`${API}/oobe/matrix`, async ({ request }) => {
    await latency();
    return guard(request) ?? json(getDb().oobe);
  }),

  http.post(`${API}/oobe/verify`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const body = (await request.json()) as { image_id?: string };
    const record = getDb().oobe.records.find((candidate) => candidate.image_id === body.image_id);
    if (!record) return jsonError(404, "not_found", "Unknown image");
    Object.assign(record, {
      status: "verified",
      activity: "com.google.android.setupwizard/.SetupWizardActivity",
      boot_seconds: 32.6,
      reset_seconds: 6.2,
      checked_at: nowIso(),
      checked_by: "mock-verify",
    });
    return json(record);
  }),

  http.get(`${API}/oobe/records/:image_id`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const record = getDb().oobe.records.find((candidate) => candidate.image_id === param(params, "image_id"));
    return record ? json(record) : jsonError(404, "not_found", "No OOBE record for this image");
  }),

  /* -- templates / devices ---------------------------------------------- */
  http.get(`${API}/templates`, async ({ request }) => {
    await latency();
    return guard(request) ?? json(getDb().templates);
  }),

  http.post(`${API}/templates`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const body = (await request.json()) as Template;
    const template: Template = { ...body, id: body.id || newId("tpl"), created_at: nowIso(), updated_at: nowIso() };
    getDb().templates.push(template);
    return json(template, 201);
  }),

  http.get(`${API}/templates/:template_id`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const template = getDb().templates.find((candidate) => candidate.id === param(params, "template_id"));
    return template ? json(template) : jsonError(404, "not_found", "Template not found");
  }),

  http.put(`${API}/templates/:template_id`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const template = getDb().templates.find((candidate) => candidate.id === param(params, "template_id"));
    if (!template) return jsonError(404, "not_found", "Template not found");
    const body = (await request.json()) as Template;
    Object.assign(template, body, { id: template.id, updated_at: nowIso() });
    return json(template);
  }),

  http.delete(`${API}/templates/:template_id`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const db = getDb();
    const index = db.templates.findIndex((candidate) => candidate.id === param(params, "template_id"));
    if (index < 0) return jsonError(404, "not_found", "Template not found");
    db.templates.splice(index, 1);
    return new HttpResponse(null, { status: 204 });
  }),

  http.get(`${API}/devices`, async ({ request }) => {
    await latency();
    return guard(request) ?? json(getDb().devices);
  }),

  /* -- instances --------------------------------------------------------- */
  http.get(`${API}/instances`, async ({ request }) => {
    await latency();
    return guard(request) ?? json(getDb().instances);
  }),

  http.post(`${API}/instances/bulk`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const body = (await request.json()) as { template_id?: string; count?: number; prefix?: string };
    const template = getDb().templates.find((candidate) => candidate.id === body.template_id);
    if (!template) return jsonError(422, "invalid_request", "template_id does not exist");
    const count = Math.min(Math.max(Number(body.count) || 1, 1), 64);
    const prefix = body.prefix?.trim() || "avd";
    const created: AvdInfo[] = [];
    for (let index = 0; index < count; index += 1) {
      let name = `${prefix}-${String(index + 1).padStart(2, "0")}`;
      let suffix = 1;
      while (findInstance(name)) {
        name = `${prefix}-${String(index + 1).padStart(2, "0")}-${suffix}`;
        suffix += 1;
      }
      const instance: AvdInfo = {
        name,
        template_id: template.id,
        image_id: template.image_id,
        image_name: getDb().images.find((image) => image.id === template.image_id)?.display_name ?? "",
        device_id: template.device_id,
        state: "defined",
        pid: null,
        ports: { console: 5554 + (getDb().instances.length + index) * 2, adb: 5555 + (getDb().instances.length + index) * 2 },
        proxy_id: template.proxy_id ?? null,
        adb_serial: null,
        boot_completed: false,
        created_at: nowIso(),
        started_at: null,
        uptime_seconds: null,
        config: {},
        screen_available: false,
      };
      getDb().instances.push(instance);
      created.push(instance);
    }
    return json(created, 201);
  }),

  http.post(`${API}/instances`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const body = (await request.json()) as { template_id?: string; name?: string };
    if (!body.name) return jsonError(422, "invalid_request", "name is required");
    if (findInstance(body.name)) return jsonError(409, "conflict", `Instance ${body.name} already exists`);
    const template = getDb().templates.find((candidate) => candidate.id === body.template_id);
    if (!template) return jsonError(422, "invalid_request", "template_id does not exist");
    const instance: AvdInfo = {
      name: body.name,
      template_id: template.id,
      image_id: template.image_id,
      image_name: getDb().images.find((image) => image.id === template.image_id)?.display_name ?? "",
      device_id: template.device_id,
      state: "defined",
      pid: null,
      ports: { console: 5554 + getDb().instances.length * 2, adb: 5555 + getDb().instances.length * 2 },
      proxy_id: template.proxy_id ?? null,
      adb_serial: null,
      boot_completed: false,
      created_at: nowIso(),
      started_at: null,
      uptime_seconds: null,
      config: {},
      screen_available: false,
    };
    getDb().instances.push(instance);
    return json(instance, 201);
  }),

  http.get(`${API}/instances/:name`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const instance = findInstance(param(params, "name"));
    return instance ? json(instance) : jsonError(404, "not_found", "Instance not found");
  }),

  http.delete(`${API}/instances/:name`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const db = getDb();
    const name = param(params, "name");
    const index = db.instances.findIndex((instance) => instance.name === name);
    if (index < 0) return jsonError(404, "not_found", "Instance not found");
    db.instances.splice(index, 1);
    db.assignments = db.assignments.filter((assignment) => assignment.avd_name !== name);
    delete db.snapshots[name];
    return new HttpResponse(null, { status: 204 });
  }),

  http.post(`${API}/instances/:name/start`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const instance = findInstance(param(params, "name"));
    if (!instance) return jsonError(404, "not_found", "Instance not found");
    const body = (await request.json().catch(() => ({}))) as { proxy_id?: string | null };
    return json(startInstance(instance, body.proxy_id));
  }),

  http.post(`${API}/instances/:name/stop`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const instance = findInstance(param(params, "name"));
    if (!instance) return jsonError(404, "not_found", "Instance not found");
    instance.state = "stopped";
    instance.pid = null;
    instance.boot_completed = false;
    instance.uptime_seconds = null;
    return json(instance);
  }),

  http.post(`${API}/instances/:name/restart`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const instance = findInstance(param(params, "name"));
    if (!instance) return jsonError(404, "not_found", "Instance not found");
    return json(startInstance(instance));
  }),

  http.post(`${API}/instances/:name/wait-boot`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const instance = findInstance(param(params, "name"));
    if (!instance) return jsonError(404, "not_found", "Instance not found");
    instance.state = "running";
    instance.boot_completed = true;
    return json(instance);
  }),

  http.post(`${API}/instances/:name/reset-oobe`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const instance = findInstance(param(params, "name"));
    if (!instance) return jsonError(404, "not_found", "Instance not found");
    const body = (await request.json().catch(() => ({}))) as { strategy?: string };
    const result: OobeResetResult = {
      avd_name: instance.name,
      strategy: (body.strategy as OobeResetResult["strategy"]) ?? "snapshot",
      ok: true,
      seconds: 6.3,
      activity: "com.google.android.setupwizard/.SetupWizardActivity",
      screenshot_path: `research/oobe/reset-${instance.name}.png`,
      message: "Restored from golden-oobe snapshot.",
      at: nowIso(),
    };
    instance.boot_completed = false;
    instance.state = "booting";
    return json(result);
  }),

  http.get(`${API}/instances/:name/screenshot`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const instance = findInstance(param(params, "name"));
    if (!instance) return jsonError(404, "not_found", "Instance not found");
    if (instance.state !== "running") return jsonError(503, "screen_error", "Instance is not running");
    return new HttpResponse(SCREENSHOT_PNG, { status: 200, headers: { "Content-Type": "image/png" } });
  }),

  http.put(`${API}/instances/:name/config`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const instance = findInstance(param(params, "name"));
    if (!instance) return jsonError(404, "not_found", "Instance not found");
    const body = (await request.json()) as { values?: Record<string, string> };
    instance.config = { ...(instance.config ?? {}), ...(body.values ?? {}) };
    return json(instance);
  }),

  http.post(`${API}/instances/:name/clone`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const source = findInstance(param(params, "name"));
    if (!source) return jsonError(404, "not_found", "Instance not found");
    const body = (await request.json()) as { name?: string };
    if (!body.name) return jsonError(422, "invalid_request", "name is required");
    if (findInstance(body.name)) return jsonError(409, "conflict", "An instance with that name already exists");
    const clone: AvdInfo = { ...source, name: body.name, state: "stopped", pid: null, started_at: null, boot_completed: false, created_at: nowIso() };
    getDb().instances.push(clone);
    return json(clone, 201);
  }),

  http.get(`${API}/instances/:name/snapshots`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    return json(getDb().snapshots[param(params, "name")] ?? []);
  }),

  http.post(`${API}/instances/:name/snapshots`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const name = param(params, "name");
    if (!findInstance(name)) return jsonError(404, "not_found", "Instance not found");
    const body = (await request.json()) as { name?: string; golden?: boolean };
    if (!body.name) return jsonError(422, "invalid_request", "name is required");
    const snapshot: Snapshot = {
      name: body.name,
      avd_name: name,
      created_at: nowIso(),
      size_bytes: 1_000_000_000 + Math.floor(Math.random() * 400_000_000),
      is_golden: Boolean(body.golden),
    };
    (getDb().snapshots[name] ??= []).push(snapshot);
    return json(snapshot, 201);
  }),

  http.post(`${API}/instances/:name/snapshots/:snapshot/load`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const snapshot = (getDb().snapshots[param(params, "name")] ?? []).find(
      (candidate) => candidate.name === param(params, "snapshot"),
    );
    return snapshot ? json(snapshot) : jsonError(404, "not_found", "Snapshot not found");
  }),

  http.delete(`${API}/instances/:name/snapshots/:snapshot`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const db = getDb();
    const name = param(params, "name");
    const list = db.snapshots[name] ?? [];
    const index = list.findIndex((candidate) => candidate.name === param(params, "snapshot"));
    if (index >= 0) list.splice(index, 1);
    return new HttpResponse(null, { status: 204 });
  }),

  http.post(`${API}/instances/:name/input`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    if (!findInstance(param(params, "name"))) return jsonError(404, "not_found", "Instance not found");
    return json({ ok: true, message: "Input accepted" });
  }),

  /* -- proxies ----------------------------------------------------------- */
  http.get(`${API}/proxies`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const includeDisabled = new URL(request.url).searchParams.get("include_disabled") === "true";
    return json(getDb().proxies.filter((proxy) => includeDisabled || proxy.enabled !== false));
  }),

  http.post(`${API}/proxies/import`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const body = (await request.json()) as { text?: string; source?: string };
    const added: ProxyView[] = [];
    const errors: string[] = [];
    let skipped = 0;
    for (const rawLine of (body.text ?? "").split(/\r?\n/)) {
      const line = rawLine.trim();
      if (!line || line.startsWith("#")) {
        skipped += 1;
        continue;
      }
      try {
        const candidate = line.includes("://") ? line : `http://${line}`;
        const parsed = new URL(candidate);
        const scheme = parsed.protocol.replace(":", "");
        if (!["http", "https", "socks5"].includes(scheme)) throw new Error(`unsupported scheme ${scheme}`);
        const proxy: ProxyView = {
          id: newId("px"),
          name: parsed.hostname,
          scheme: scheme as ProxyView["scheme"],
          host: parsed.hostname,
          port: Number(parsed.port || (scheme === "socks5" ? 1080 : 8080)),
          username: parsed.username ? decodeURIComponent(parsed.username) : null,
          has_credentials: Boolean(parsed.username || parsed.password),
          tags: body.source ? [body.source] : [],
          enabled: true,
          health: { status: "unknown", latency_ms: null, checked_at: null, message: "", egress_ip: null },
          created_at: nowIso(),
        };
        getDb().proxies.push(proxy);
        added.push(proxy);
      } catch (error) {
        errors.push(`${line}: ${error instanceof Error ? error.message : "invalid proxy URL"}`);
      }
    }
    return json({ added, skipped, errors });
  }),

  http.post(`${API}/proxies/check-all`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    return json(getDb().proxies.map((proxy) => checkProxy(proxy)));
  }),

  http.post(`${API}/proxies`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const body = (await request.json()) as {
      url?: string;
      name?: string;
      scheme?: string;
      host?: string;
      port?: number;
      username?: string | null;
      tags?: string[];
    };
    let scheme = body.scheme ?? "http";
    let host = body.host ?? "";
    let port = body.port ?? 8080;
    let username = body.username ?? null;
    let hasCredentials = Boolean(username);
    if (body.url) {
      try {
        const parsed = new URL(body.url.includes("://") ? body.url : `http://${body.url}`);
        scheme = parsed.protocol.replace(":", "");
        host = parsed.hostname;
        port = Number(parsed.port || (scheme === "socks5" ? 1080 : 8080));
        username = parsed.username ? decodeURIComponent(parsed.username) : null;
        hasCredentials = Boolean(parsed.username || parsed.password);
      } catch {
        return jsonError(422, "invalid_request", "url is not a valid proxy URL");
      }
    }
    if (!host) return jsonError(422, "invalid_request", "host or url is required");
    if (!["http", "https", "socks5"].includes(scheme)) {
      return jsonError(422, "invalid_request", `unsupported scheme: ${scheme}`);
    }
    const proxy: ProxyView = {
      id: newId("px"),
      name: body.name?.trim() || host,
      scheme: scheme as ProxyView["scheme"],
      host,
      port,
      username,
      has_credentials: hasCredentials,
      tags: body.tags ?? [],
      enabled: true,
      health: { status: "unknown", latency_ms: null, checked_at: null, message: "", egress_ip: null },
      created_at: nowIso(),
    };
    getDb().proxies.push(proxy);
    return json(proxy, 201);
  }),

  http.get(`${API}/proxies/:proxy_id`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const proxy = findProxy(param(params, "proxy_id"));
    return proxy ? json(proxy) : jsonError(404, "not_found", "Proxy not found");
  }),

  http.patch(`${API}/proxies/:proxy_id`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const proxy = findProxy(param(params, "proxy_id"));
    if (!proxy) return jsonError(404, "not_found", "Proxy not found");
    const patch = (await request.json()) as Record<string, unknown>;
    const mutable = ["name", "scheme", "host", "port", "username", "tags", "enabled"] as const;
    for (const key of mutable) {
      if (key in patch) (proxy as unknown as Record<string, unknown>)[key] = patch[key];
    }
    return json(proxy);
  }),

  http.delete(`${API}/proxies/:proxy_id`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const db = getDb();
    const index = db.proxies.findIndex((proxy) => proxy.id === param(params, "proxy_id"));
    if (index < 0) return jsonError(404, "not_found", "Proxy not found");
    db.proxies.splice(index, 1);
    db.assignments = db.assignments.filter((assignment) => assignment.proxy_id !== param(params, "proxy_id"));
    return new HttpResponse(null, { status: 204 });
  }),

  http.post(`${API}/proxies/:proxy_id/check`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const proxy = findProxy(param(params, "proxy_id"));
    return proxy ? json(checkProxy(proxy)) : jsonError(404, "not_found", "Proxy not found");
  }),

  http.post(`${API}/proxies/verify-guest`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const body = (await request.json()) as { avd_name?: string; proxy_id?: string };
    if (!findInstance(body.avd_name ?? "")) return jsonError(404, "not_found", "Instance not found");
    return json({
      proxy_id: body.proxy_id ?? "",
      ok: true,
      latency_ms: 118,
      egress_ip: "203.0.113.77",
      status_code: 200,
      error: "",
      checked_at: nowIso(),
    });
  }),

  http.get(`${API}/assignments`, async ({ request }) => {
    await latency();
    return guard(request) ?? json(getDb().assignments);
  }),

  http.post(`${API}/assignments/auto`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const body = (await request.json()) as { instances?: string[]; strategy?: string };
    const enabled = getDb().proxies.filter((proxy) => proxy.enabled !== false);
    const created: ProxyAssignment[] = [];
    (body.instances ?? []).forEach((instanceName, index) => {
      if (enabled.length === 0) return;
      const proxy = enabled[index % enabled.length]!;
      const instance = findInstance(instanceName);
      if (instance) {
        instance.proxy_id = proxy.id;
        const assignment: ProxyAssignment = {
          avd_name: instanceName,
          proxy_id: proxy.id,
          assigned_at: nowIso(),
        };
        getDb().assignments.push(assignment);
        created.push(assignment);
      }
    });
    return json(created);
  }),

  http.post(`${API}/assignments`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const body = (await request.json()) as { avd_name?: string; proxy_id?: string };
    const instance = findInstance(body.avd_name ?? "");
    if (!instance) return jsonError(404, "not_found", "Instance not found");
    const proxy = findProxy(body.proxy_id ?? "");
    if (!proxy) return jsonError(404, "not_found", "Proxy not found");
    instance.proxy_id = proxy.id;
    const db = getDb();
    db.assignments = db.assignments.filter((assignment) => assignment.avd_name !== instance.name);
    const assignment: ProxyAssignment = { avd_name: instance.name, proxy_id: proxy.id, assigned_at: nowIso() };
    db.assignments.push(assignment);
    return json(assignment);
  }),

  http.delete(`${API}/assignments/:avd_name`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const name = param(params, "avd_name");
    getDb().assignments = getDb().assignments.filter((assignment) => assignment.avd_name !== name);
    const instance = findInstance(name);
    if (instance) instance.proxy_id = null;
    return new HttpResponse(null, { status: 204 });
  }),

  http.get(`${API}/bridges`, async ({ request }) => {
    await latency();
    return guard(request) ?? json(getDb().bridges as BridgeConfig[]);
  }),

  /* -- scripts / runs ---------------------------------------------------- */
  http.get(`${API}/scripts`, async ({ request }) => {
    await latency();
    return guard(request) ?? json(getDb().scripts);
  }),

  http.post(`${API}/scripts/runs`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const body = (await request.json()) as { script_id?: string; instances?: string[]; dry_run?: boolean };
    const script = getDb().scripts.find((candidate) => candidate.id === body.script_id);
    if (!script) return jsonError(404, "not_found", "Script not found");
    const targets = (body.instances?.length ? body.instances : getDb().instances.map((instance) => instance.name)).slice(0, 64);
    const results: InstanceResult[] = targets.map((instance) => ({
      instance,
      state: "queued",
      exit_code: null,
      stdout: "",
      stderr: "",
      started_at: null,
      finished_at: null,
      duration_s: null,
      error: "",
    }));
    const run: ScriptRun = {
      id: newId("run").replace("run-", "run-"),
      script_id: script.id,
      script_name: script.name,
      type: script.type,
      state: "running",
      instances: targets,
      results,
      started_at: nowIso(),
      finished_at: null,
      error: "",
      dry_run: Boolean(body.dry_run),
      requested_by: "operator",
    };
    getDb().runs.unshift(run);
    return json(run, 202);
  }),

  http.post(`${API}/scripts`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const body = (await request.json()) as ScriptDefinition;
    if (!body.content && !body.path) return jsonError(422, "invalid_request", "script requires content or path");
    const script: ScriptDefinition = {
      ...body,
      id: body.id || newId("scr"),
      created_at: nowIso(),
      updated_at: nowIso(),
    };
    getDb().scripts.push(script);
    return json(script, 201);
  }),

  http.get(`${API}/scripts/:script_id`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const script = getDb().scripts.find((candidate) => candidate.id === param(params, "script_id"));
    return script ? json(script) : jsonError(404, "not_found", "Script not found");
  }),

  http.put(`${API}/scripts/:script_id`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const script = getDb().scripts.find((candidate) => candidate.id === param(params, "script_id"));
    if (!script) return jsonError(404, "not_found", "Script not found");
    const body = (await request.json()) as ScriptDefinition;
    Object.assign(script, body, { id: script.id, updated_at: nowIso() });
    return json(script);
  }),

  http.delete(`${API}/scripts/:script_id`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const db = getDb();
    const index = db.scripts.findIndex((script) => script.id === param(params, "script_id"));
    if (index < 0) return jsonError(404, "not_found", "Script not found");
    db.scripts.splice(index, 1);
    return new HttpResponse(null, { status: 204 });
  }),

  http.get(`${API}/runs`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const limit = Number(new URL(request.url).searchParams.get("limit") ?? 50);
    return json(getDb().runs.slice(0, Math.min(Math.max(limit, 1), 500)));
  }),

  http.get(`${API}/runs/:run_id`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const run = getDb().runs.find((candidate) => candidate.id === param(params, "run_id"));
    return run ? json(run) : jsonError(404, "not_found", "Run not found");
  }),

  http.post(`${API}/runs/:run_id/cancel`, async ({ request, params }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const run = getDb().runs.find((candidate) => candidate.id === param(params, "run_id"));
    if (!run) return jsonError(404, "not_found", "Run not found");
    run.state = "cancelled";
    run.finished_at = nowIso();
    run.results = (run.results ?? []).map((result) => ({
      ...result,
      state: result.state === "running" || result.state === "queued" ? "cancelled" : result.state,
    }));
    return json(run);
  }),

  /* -- appium ------------------------------------------------------------ */
  http.get(`${API}/appium`, async ({ request }) => {
    await latency();
    return guard(request) ?? json(getDb().appium);
  }),

  http.post(`${API}/appium/start`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const body = (await request.json().catch(() => ({}))) as { port?: number | null };
    const appium = getDb().appium;
    appium.running = true;
    appium.pid = 30_000 + Math.floor(Math.random() * 1000);
    appium.port = body.port ?? 4723;
    appium.url = `http://127.0.0.1:${appium.port}`;
    appium.message = "";
    return json(appium);
  }),

  http.post(`${API}/appium/stop`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const appium = getDb().appium;
    appium.running = false;
    appium.pid = null;
    appium.sessions = 0;
    return json(appium);
  }),

  http.post(`${API}/appium/capabilities`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const body = (await request.json()) as { avd_name?: string };
    const instance = findInstance(body.avd_name ?? "");
    if (!instance) return jsonError(404, "not_found", "Instance not found");
    return json({
      platformName: "Android",
      "appium:automationName": "UiAutomator2",
      "appium:deviceName": instance.name,
      "appium:udid": instance.adb_serial ?? `emulator-${instance.ports?.adb ?? 5555}`,
      "appium:systemPort": instance.ports?.system_port ?? 8200,
      "appium:noReset": false,
      "appium:chromedriverUseSystemExecutable": true,
    });
  }),

  /* -- fleet ------------------------------------------------------------- */
  http.post(`${API}/fleet/plan`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const body = (await request.json().catch(() => ({}))) as {
      spec?: { templates?: unknown[]; instances?: unknown[]; proxies?: unknown[] };
      dry_run?: boolean;
    };
    const changes = [
      ...(body.spec?.templates ?? []).map((_entry, index) => ({
        action: index === 0 ? ("update" as const) : ("create" as const),
        kind: "template" as const,
        name: `template-${index + 1}`,
        detail: index === 0 ? "resources differ" : "not present on host",
      })),
      ...(body.spec?.proxies ?? []).map((_entry, index) => ({
        action: "create" as const,
        kind: "proxy" as const,
        name: `proxy-${index + 1}`,
        detail: "new proxy",
      })),
      ...(body.spec?.instances ?? []).map((_entry, index) => ({
        action: index === 0 ? ("noop" as const) : ("create" as const),
        kind: "instance" as const,
        name: `instance-${index + 1}`,
        detail: index === 0 ? "already matches" : "will be deployed",
      })),
    ];
    return json({
      spec_name: "fleet",
      changes,
      summary: changes.reduce<Record<string, number>>((accumulator, change) => {
        accumulator[change.action] = (accumulator[change.action] ?? 0) + 1;
        return accumulator;
      }, {}),
      applyable: true,
      errors: [],
    });
  }),

  http.post(`${API}/fleet/apply`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    return json({
      spec_name: "fleet",
      changes: [{ action: "create", kind: "instance", name: "avd-06", detail: "deployed" }],
      summary: { create: 1 },
      applyable: true,
      errors: [],
    });
  }),

  /* -- portable ---------------------------------------------------------- */
  http.get(`${API}/portable/status`, async ({ request }) => {
    await latency();
    return guard(request) ?? json(getDb().portable);
  }),

  http.get(`${API}/portable/checks`, async ({ request }) => {
    await latency();
    return guard(request) ?? json(getDb().portableChecks);
  }),

  /* -- events ------------------------------------------------------------ */
  http.get(`${API}/events`, async ({ request }) => {
    await latency();
    const unauthorized = guard(request);
    if (unauthorized) return unauthorized;
    const limit = Number(new URL(request.url).searchParams.get("limit") ?? 100);
    return json(getDb().events.slice(0, Math.min(Math.max(limit, 1), 500)));
  }),
];
