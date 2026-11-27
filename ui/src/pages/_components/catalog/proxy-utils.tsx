/**
 * Proxy URL parsing and secret-redaction helpers for the Proxies page.
 *
 * The API never returns proxy passwords (`has_credentials` only). Any free text
 * that originated from user input (bulk-import errors, URL previews) must pass
 * through `redactProxySecrets` before it is rendered, so an echoed URL can
 * never leak a password into the table, a toast or an error notice.
 */
import type { ProxyScheme } from "@/lib/types";
import { maskProxyUrl } from "@/lib/utils";

import { errorMessage } from "./common";

export const PROXY_SCHEMES: ProxyScheme[] = ["http", "https", "socks5"];

export interface ParsedProxyDraft {
  scheme: ProxyScheme;
  host: string;
  port: number;
  username: string | null;
  password: string | null;
  hasCredentials: boolean;
}

export type ProxyParseResult =
  | { ok: true; value: ParsedProxyDraft }
  | { ok: false; error: string };

export function defaultProxyPort(scheme: ProxyScheme): number {
  return scheme === "socks5" ? 1080 : 8080;
}

export function isProxyScheme(value: string): value is ProxyScheme {
  return (PROXY_SCHEMES as string[]).includes(value);
}

function decodePart(value: string): string | null {
  if (!value) return null;
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

/**
 * Parse one `[scheme://][user:pass@]host[:port]` proxy URL.
 * A missing scheme defaults to `http`; the usual default ports are applied.
 */
export function parseProxyInput(input: string): ProxyParseResult {
  const raw = input.trim();
  if (!raw) return { ok: false, error: "Enter a proxy URL." };

  let url: URL;
  try {
    url = new URL(raw.includes("://") ? raw : `http://${raw}`);
  } catch {
    return {
      ok: false,
      error: "Enter a valid proxy URL, for example http://user:pass@host:8080.",
    };
  }

  const scheme = url.protocol.replace(/:$/, "").toLowerCase();
  if (!isProxyScheme(scheme)) {
    return {
      ok: false,
      error: `Unsupported scheme "${scheme || "none"}". Use http, https or socks5.`,
    };
  }
  if (!url.hostname) {
    return { ok: false, error: "The proxy URL is missing a host." };
  }
  if (url.search || url.hash || (url.pathname && url.pathname !== "/")) {
    return { ok: false, error: "Proxy URLs cannot include a path, query or fragment." };
  }

  const port = url.port ? Number(url.port) : defaultProxyPort(scheme);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    return { ok: false, error: "The port must be a whole number between 1 and 65535." };
  }

  const username = decodePart(url.username);
  const password = decodePart(url.password);

  return {
    ok: true,
    value: {
      scheme,
      host: url.hostname,
      port,
      username,
      password,
      hasCredentials: Boolean(username || password),
    },
  };
}

/** Strip `user:pass@` credentials from any string before rendering it. */
export function redactProxySecrets(value: string): string {
  return value.replace(/\/\/[^/@\s]+@/g, "//***:***@");
}

/** Error text for proxy mutations with any echoed credentials masked. */
export function proxyErrorMessage(error: unknown): string {
  return redactProxySecrets(errorMessage(error));
}

/** Display form without credentials: `scheme://host:port`. */
export function proxyEndpoint(proxy: { scheme?: ProxyScheme; host: string; port: number }): string {
  return `${proxy.scheme ?? "http"}://${proxy.host}:${proxy.port}`;
}

/** Display form without credentials: `host:port`. */
export function proxyHostPort(proxy: { host: string; port: number }): string {
  return `${proxy.host}:${proxy.port}`;
}

/** `scheme://host:port` masked for previews derived from user input. */
export function maskProxyInput(input: string): string {
  return redactProxySecrets(maskProxyUrl(input.trim()));
}

/** Split a comma/newline separated tag string, preserving order, deduplicating. */
export function parseTagsInput(value: string): string[] {
  const seen = new Set<string>();
  const tags: string[] = [];
  for (const part of value.split(/[,|\n]+/)) {
    const tag = part.trim();
    if (!tag) continue;
    const key = tag.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    tags.push(tag);
  }
  return tags;
}

/** Count importable lines (non-empty, non-comment) in a bulk-import textarea. */
export function countImportEntries(value: string): number {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0 && !line.startsWith("#")).length;
}
