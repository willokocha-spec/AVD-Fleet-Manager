/**
 * WebSocket client for the daemon event stream (`/ws/events?ticket=…`).
 *
 * Browsers cannot set an `Authorization` header on a WebSocket handshake, and a
 * long-lived bearer token in the query string is written to uvicorn's request
 * log (`access_log=False` does not suppress the WebSocket request line). So
 * before every connect — including every reconnect — this client asks the
 * bearer-authenticated daemon for a short-lived, single-use ticket
 * (`POST /api/v1/auth/ws-ticket`) and passes only that ticket in the URL.
 *
 * The daemon pushes `FleetEvent` envelopes (see contracts/events.schema.json).
 * This module also adds:
 * - typed event dispatch,
 * - exponential backoff with jitter on drop,
 * - a dev-only simulated source for MSW mock mode.
 *
 * The socket factory and the ticket fetcher are injectable so tests never open a
 * real network socket.
 */

import { buildApiUrl, buildEventsSocketUrl } from "./config";
import type { FleetEvent, EventType } from "./types";
import { isRecord } from "./utils";

export type SocketStatus = "idle" | "connecting" | "open" | "reconnecting" | "closed";

export interface WebSocketLike {
  onopen: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent) => void) | null;
  onclose: ((event: CloseEvent) => void) | null;
  onerror: ((event: Event) => void) | null;
  close(code?: number, reason?: string): void;
}

export type WebSocketFactory = (url: string) => WebSocketLike;

/** Fetches one fresh single-use ticket; may reject when the daemon refuses. */
export type TicketFetcher = (token: string | null) => Promise<string>;

export interface EventSocketOptions {
  /** Bearer token used to authenticate the ticket request (never sent in the URL). */
  token?: string | null;
  /** Explicit WebSocket URL override (defaults to same-origin `/ws/events`). */
  url?: string;
  onEvent: (event: FleetEvent) => void;
  onStatus?: (status: SocketStatus, detail: { attempt: number; retryInMs?: number }) => void;
  WebSocketImpl?: WebSocketFactory;
  /** Ticket source override (tests); defaults to `POST /api/v1/auth/ws-ticket`. */
  fetchTicket?: TicketFetcher;
  /** First reconnect delay (ms). */
  minDelayMs?: number;
  /** Backoff ceiling (ms). */
  maxDelayMs?: number;
  /** Jitter fraction applied to each delay (0..1). */
  jitter?: number;
  random?: () => number;
}

export interface EventSocketHandle {
  close: () => void;
  status: () => SocketStatus;
  /** Base socket URL; the ticket is appended per connection. */
  url: string;
}

const DEFAULT_MIN_DELAY = 1_000;
const DEFAULT_MAX_DELAY = 30_000;
const DEFAULT_JITTER = 0.25;

function isFleetEvent(value: unknown): value is FleetEvent {
  if (!isRecord(value)) return false;
  return (
    typeof value.id === "string" &&
    typeof value.type === "string" &&
    typeof value.ts === "string" &&
    (value.payload === undefined || isRecord(value.payload))
  );
}

/**
 * Request one single-use ticket from the daemon.
 *
 * This is a plain `fetch` (not the typed API client) so the WebSocket module
 * stays self-contained; the request carries the same bearer header as every
 * other `/api/v1` call and the ticket response is marked `no-store` by the
 * daemon.
 */
export async function fetchEventSocketTicket(token: string | null): Promise<string> {
  const headers = new Headers({ Accept: "application/json" });
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(buildApiUrl("/auth/ws-ticket"), {
    method: "POST",
    headers,
    credentials: "same-origin",
  });
  if (!response.ok) {
    throw new Error(`the daemon refused the WebSocket ticket (HTTP ${response.status})`);
  }
  const payload: unknown = await response.json();
  if (!isRecord(payload) || typeof payload.ticket !== "string" || !payload.ticket.trim()) {
    throw new Error("the daemon returned an invalid WebSocket ticket response");
  }
  return payload.ticket.trim();
}

/**
 * Return *baseUrl* with a fresh `?ticket=` and no legacy credentials.
 *
 * Any `token`/`ticket` already present in an override URL is dropped: the token
 * must never travel in a query string, and a stale ticket would be rejected.
 */
function withTicket(baseUrl: string, ticket: string): string {
  try {
    const url = new URL(baseUrl);
    url.searchParams.delete("token");
    url.searchParams.delete("ticket");
    url.searchParams.set("ticket", ticket);
    return url.toString();
  } catch {
    // Relative/opaque override: append to the query string manually.
    const separator = baseUrl.includes("?") ? "&" : "?";
    return `${baseUrl}${separator}ticket=${encodeURIComponent(ticket)}`;
  }
}

export function createEventSocket(options: EventSocketOptions): EventSocketHandle {
  const {
    token,
    url = buildEventsSocketUrl(),
    onEvent,
    onStatus,
    WebSocketImpl,
    fetchTicket = fetchEventSocketTicket,
    minDelayMs = DEFAULT_MIN_DELAY,
    maxDelayMs = DEFAULT_MAX_DELAY,
    jitter = DEFAULT_JITTER,
    random = Math.random,
  } = options;

  const factory: WebSocketFactory | null =
    WebSocketImpl ?? (typeof WebSocket !== "undefined" ? (target) => new WebSocket(target) : null);

  let socket: WebSocketLike | null = null;
  let status: SocketStatus = "idle";
  let attempt = 0;
  let closed = false;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  /** Incremented per connect attempt; stale async work is discarded. */
  let generation = 0;

  const setStatus = (next: SocketStatus, detail?: { retryInMs?: number }): void => {
    status = next;
    onStatus?.(next, { attempt, ...detail });
  };

  const clearReconnectTimer = (): void => {
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  };

  const scheduleReconnect = (): void => {
    if (closed || reconnectTimer !== null) return;
    attempt += 1;
    const exponential = Math.min(maxDelayMs, minDelayMs * 2 ** (attempt - 1));
    const jitterMs = exponential * jitter * random();
    const retryInMs = Math.round(exponential + jitterMs);
    setStatus("reconnecting", { retryInMs });
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, retryInMs);
  };

  const connect = (): void => {
    if (closed) return;
    if (!factory) {
      setStatus("closed");
      return;
    }
    const current = ++generation;
    // Fetch a fresh ticket for every attempt: the previous one is burned.
    setStatus(attempt === 0 ? "connecting" : "reconnecting");
    void open(current);
  };

  const open = async (current: number): Promise<void> => {
    let ticket: string;
    try {
      ticket = await fetchTicket(token ?? null);
    } catch {
      if (closed || current !== generation) return;
      scheduleReconnect();
      return;
    }
    if (closed || current !== generation) return;

    const create = factory;
    if (!create) return;
    let next: WebSocketLike;
    try {
      next = create(withTicket(url, ticket));
    } catch {
      scheduleReconnect();
      return;
    }
    socket = next;

    next.onopen = () => {
      attempt = 0;
      setStatus("open");
    };

    next.onmessage = (event: MessageEvent) => {
      let parsed: unknown;
      try {
        parsed = typeof event.data === "string" ? JSON.parse(event.data) : event.data;
      } catch {
        return;
      }
      if (isFleetEvent(parsed)) {
        onEvent({
          ...parsed,
          payload: parsed.payload ?? {},
        });
      }
    };

    next.onerror = () => {
      // `close` normally follows; scheduling here too is guarded by the timer.
      scheduleReconnect();
    };

    next.onclose = () => {
      if (socket === next) socket = null;
      scheduleReconnect();
    };
  };

  connect();

  return {
    url,
    close: () => {
      closed = true;
      generation += 1; // discard an in-flight ticket request
      clearReconnectTimer();
      const current = socket;
      socket = null;
      if (current) {
        current.onopen = null;
        current.onmessage = null;
        current.onerror = null;
        current.onclose = null;
        try {
          current.close(1000, "client closed");
        } catch {
          /* ignore teardown races */
        }
      }
      setStatus("closed");
    },
    status: () => status,
  };
}

/**
 * Dev-only simulated event stream used when MSW mocks are enabled (MSW does
 * not intercept WebSockets). Emits a small rotating set of realistic events.
 */
export function createSimulatedEventSocket(options: {
  onEvent: (event: FleetEvent) => void;
  onStatus?: (status: SocketStatus) => void;
  intervalMs?: number;
}): EventSocketHandle {
  const { onEvent, onStatus, intervalMs = 12_000 } = options;
  let seq = 0;
  let timer: ReturnType<typeof setInterval> | null = null;
  const startedAt = new Date().toISOString();

  const emit = (type: EventType, payload: Record<string, unknown>): void => {
    seq += 1;
    onEvent({
      id: `mock-evt-${seq}`,
      type,
      ts: new Date().toISOString(),
      payload,
      source: "mock",
      seq,
    });
  };

  onStatus?.("open");
  emit("system", { message: "Connected to mock event stream", mock: true });

  const script: Array<[EventType, Record<string, unknown>]> = [
    ["instance.state", { instance: "avd-pixel7-02", state: "running", reason: "boot completed" }],
    ["proxy.checked", { proxy_id: "px-http-3", ok: true, latency_ms: 81 }],
    ["script.run.output", { run_id: "run-0003", instance: "avd-pixel7-01", stream: "stdout", line: "pm list packages | wc -l → 212" }],
    ["instance.boot", { instance: "avd-play-01", boot_completed: true }],
    ["doctor.finished", { ok: false, summary: "2 checks need attention" }],
  ];
  let cursor = 0;
  timer = setInterval(() => {
    const entry = script[cursor % script.length]!;
    cursor += 1;
    emit(entry[0], entry[1]);
  }, intervalMs);

  return {
    url: `mock://events?since=${encodeURIComponent(startedAt)}`,
    status: () => "open",
    close: () => {
      if (timer !== null) clearInterval(timer);
      timer = null;
      onStatus?.("closed");
    },
  };
}
