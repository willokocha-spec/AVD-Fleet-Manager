/**
 * Runtime configuration for the AVD Fleet GUI.
 *
 * The UI is served by the daemon on `http://127.0.0.1:8765`, so the default
 * REST base is the same-origin `/api/v1`. In `vite dev` the same paths are
 * proxied to the daemon, or answered by MSW when mocks are enabled.
 */

export const APP_NAME = "AVD Fleet";
export const UI_VERSION = "0.1.0";

/** localStorage keys (shared with the pre-paint theme script in index.html). */
export const TOKEN_STORAGE_KEY = "avdfleet.token";
export const THEME_STORAGE_KEY = "avdfleet.theme";
export const SIDEBAR_STORAGE_KEY = "avdfleet.sidebar-collapsed";

const DEFAULT_API_BASE = "/api/v1";

function env(): ImportMetaEnv {
  return import.meta.env;
}

/**
 * True when the MSW mock layer should intercept API calls. Explicit
 * `VITE_USE_MOCKS` wins; otherwise mocks are dev-only.
 */
export function mocksEnabled(): boolean {
  const flag = env().VITE_USE_MOCKS;
  if (flag === "false") return false;
  if (flag === "true") return true;
  return Boolean(env().DEV);
}

/** Configured REST base, e.g. `/api/v1` or `http://host:8765/api/v1`. */
export function apiBasePath(): string {
  const configured = env().VITE_API_BASE_URL;
  const base = (configured && configured.trim()) || DEFAULT_API_BASE;
  return base.replace(/\/+$/, "");
}

function isAbsoluteUrl(value: string): boolean {
  return /^[a-z][a-z\d+.-]*:\/\//i.test(value);
}

/** Origin the daemon is reachable on (used for unauthenticated `/health`). */
export function daemonOrigin(): string {
  const base = apiBasePath();
  if (isAbsoluteUrl(base)) {
    try {
      return new URL(base).origin;
    } catch {
      /* fall through */
    }
  }
  if (typeof window !== "undefined" && window.location?.origin) {
    return window.location.origin;
  }
  return "http://127.0.0.1:8765";
}

function resolveApiUrl(path: string): string {
  const suffix = path.startsWith("/") ? path : `/${path}`;
  const base = apiBasePath();
  const absoluteBase = isAbsoluteUrl(base) ? base : `${daemonOrigin()}${base.startsWith("/") ? "" : "/"}${base}`;
  try {
    return new URL(`${absoluteBase}${suffix}`).toString();
  } catch {
    return `${absoluteBase}${suffix}`;
  }
}

export type QueryValue = string | number | boolean | null | undefined;

/** Build an absolute API URL from a path (e.g. `/instances`) plus query params. */
export function buildApiUrl(path: string, query?: Record<string, QueryValue>): string {
  const url = new URL(resolveApiUrl(path));
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null) continue;
      url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

/** Build an absolute daemon-root URL (e.g. `/health`). */
export function buildRootUrl(path: string, query?: Record<string, QueryValue>): string {
  const url = new URL(`${daemonOrigin()}${path.startsWith("/") ? path : `/${path}`}`);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null) continue;
      url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

/** WebSocket URL for `/ws/events`, with the token in the query string. */
export function buildEventsSocketUrl(token?: string | null): string {
  const configured = env().VITE_WS_URL;
  const fallback = `${daemonOrigin().replace(/^http/i, "ws")}/ws/events`;
  const url = new URL(configured && configured.trim() ? configured : fallback);
  if (token) url.searchParams.set("token", token);
  return url.toString();
}

/** Short description of the configured daemon, for the auth screen. */
export function daemonLabel(): string {
  try {
    return new URL(daemonOrigin()).host;
  } catch {
    return daemonOrigin();
  }
}
