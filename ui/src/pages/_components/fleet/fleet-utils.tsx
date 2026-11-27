/**
 * Shared, page-private helpers for the Fleet and Instance detail pages.
 *
 * Everything here is presentation/state plumbing only — all daemon access goes
 * through `@/lib/api`. The hooks centralise:
 *
 * - silent polling (`usePolledData`) so a 5 s fleet refresh never flashes a
 *   loading skeleton over data that is already on screen,
 * - optimistic overlay of live `instance.state` WebSocket events,
 * - bounded-concurrency bulk operations with per-instance failure reporting.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { useEventStream } from "@/app/EventStream";
import { ApiError, api } from "@/lib/api";
import { Badge, StatusPill } from "@/lib/components";
import type {
  AvdInfo,
  FleetEvent,
  InstanceState,
  InstanceStatePayload,
  OobeResetStrategy,
  ProxyView,
} from "@/lib/types";
import { formatDuration } from "@/lib/utils";

/* -------------------------------------------------------------------------- */
/* State filtering                                                             */
/* -------------------------------------------------------------------------- */

export type FleetStateFilter = "all" | "running" | "booting" | "stopped" | "error" | "defined";

export interface FleetFilterOption {
  id: FleetStateFilter;
  label: string;
  description: string;
}

/** Filters required by the operator workflow: running/booting/stopped/error/defined. */
export const FLEET_FILTERS: readonly FleetFilterOption[] = [
  { id: "all", label: "All", description: "Every instance on this host" },
  { id: "running", label: "Running", description: "Booted and ready for automation" },
  { id: "booting", label: "Booting", description: "Starting, booting or shutting down" },
  { id: "stopped", label: "Stopped", description: "Powered off but still defined" },
  { id: "error", label: "Error", description: "Failed to start or crashed" },
  { id: "defined", label: "Defined", description: "Created but never started" },
];

const TRANSITIONAL: ReadonlySet<InstanceState> = new Set<InstanceState>(["starting", "booting", "stopping"]);

export function isTransitional(state: InstanceState | null | undefined): boolean {
  return state !== null && state !== undefined && TRANSITIONAL.has(state);
}

export function isRunning(state: InstanceState | null | undefined): boolean {
  return state === "running";
}

export function canStart(state: InstanceState | null | undefined): boolean {
  return state === "defined" || state === "stopped" || state === "error" || state === "unknown";
}

export function canStop(state: InstanceState | null | undefined): boolean {
  return state === "running" || state === "booting" || state === "starting";
}

export function canRestart(state: InstanceState | null | undefined): boolean {
  return state === "running" || state === "booting" || state === "starting";
}

export function matchesFilter(state: InstanceState, filter: FleetStateFilter): boolean {
  switch (filter) {
    case "all":
      return true;
    case "running":
      return state === "running";
    case "booting":
      return state === "starting" || state === "booting" || state === "stopping";
    case "stopped":
      return state === "stopped";
    case "error":
      return state === "error";
    case "defined":
      return state === "defined";
    default:
      return true;
  }
}

export function countInstances(instances: readonly AvdInfo[], filter: FleetStateFilter): number {
  return instances.filter((instance) => matchesFilter(instance.state, filter)).length;
}

/** Case-insensitive match over the identifiers an operator is likely to paste. */
export function searchInstances(instances: readonly AvdInfo[], query: string): AvdInfo[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [...instances];
  return instances.filter((instance) => {
    const haystack = [
      instance.name,
      instance.image_name,
      instance.image_id,
      instance.device_id,
      instance.template_id,
      instance.adb_serial,
      instance.proxy_id,
    ];
    return haystack.some((value) => typeof value === "string" && value.toLowerCase().includes(needle));
  });
}

/* -------------------------------------------------------------------------- */
/* Small presentational leaves                                                 */
/* -------------------------------------------------------------------------- */

/** Status pill for an instance state, pulsing while transitional. */
export function InstanceStatePill({ instance }: { instance: AvdInfo }) {
  const errorTitle = instance.state === "error" ? instance.error : undefined;
  return (
    <StatusPill
      status={instance.state}
      pulse={isTransitional(instance.state)}
      title={errorTitle ?? instance.state}
    />
  );
}

/** Proxy badge (or a "Direct" badge when no proxy is assigned). */
export function ProxyBadge({
  proxyId,
  proxies,
}: {
  proxyId: string | null | undefined;
  proxies: readonly ProxyView[] | null;
}) {
  if (!proxyId) {
    return (
      <Badge tone="neutral" icon="proxies" title="No proxy assigned — guest traffic leaves directly">
        Direct
      </Badge>
    );
  }
  const proxy = proxies?.find((candidate) => candidate.id === proxyId);
  const health = proxy?.health?.status;
  const tone = health === "failed" ? "danger" : health === "ok" ? "success" : "info";
  const label = proxy?.name?.trim() || (proxy ? `${proxy.host}:${proxy.port}` : proxyId);
  const title = proxy
    ? `${proxy.scheme ?? "http"}://${proxy.host}:${proxy.port} · health ${health ?? "unknown"}`
    : `Proxy ${proxyId}`;
  return (
    <Link
      to="/proxies"
      onClick={(event) => event.stopPropagation()}
      style={{ textDecoration: "none", display: "inline-flex" }}
      title={`Open proxies · ${title}`}
    >
      <Badge tone={tone} icon="proxies" title={title}>
        {label}
      </Badge>
    </Link>
  );
}

/** Uptime that keeps ticking locally while the instance is running. */
export function uptimeSeconds(instance: AvdInfo): number | null {
  if (instance.state !== "running") return instance.uptime_seconds ?? null;
  if (instance.started_at) {
    const started = Date.parse(instance.started_at);
    if (!Number.isNaN(started)) return Math.max(0, Math.round((Date.now() - started) / 1000));
  }
  return instance.uptime_seconds ?? null;
}

export function LiveUptime({ instance }: { instance: AvdInfo }) {
  const [, forceTick] = useState(0);
  const running = instance.state === "running";
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => forceTick((value) => value + 1), 1_000);
    return () => window.clearInterval(timer);
  }, [running]);

  const seconds = uptimeSeconds(instance);
  if (seconds === null) return <>—</>;
  const startedTitle = instance.started_at
    ? `Started ${new Date(instance.started_at).toLocaleString()}`
    : undefined;
  return <span title={startedTitle}>{formatDuration(seconds)}</span>;
}

/** Ordered console/adb/grpc/system-port entries for a port cell or list. */
export function portEntries(instance: AvdInfo): Array<[label: string, value: string]> {
  const ports = instance.ports;
  if (!ports) return [];
  const entries: Array<[string, string]> = [];
  if (ports.console !== undefined) entries.push(["Console", String(ports.console)]);
  if (ports.adb !== undefined) entries.push(["ADB", String(ports.adb)]);
  if (ports.grpc !== undefined && ports.grpc !== null) entries.push(["gRPC", String(ports.grpc)]);
  if (ports.appium_server !== undefined && ports.appium_server !== null) {
    entries.push(["Appium", String(ports.appium_server)]);
  }
  if (ports.system_port !== undefined && ports.system_port !== null) {
    entries.push(["System", String(ports.system_port)]);
  }
  if (ports.mjpeg !== undefined && ports.mjpeg !== null) entries.push(["MJPEG", String(ports.mjpeg)]);
  return entries;
}

/** Compact "5554 · 5555" style port summary used inside table cells. */
export function portSummary(instance: AvdInfo): string {
  const entries = portEntries(instance);
  if (entries.length === 0) return "—";
  return entries.map(([label, value]) => `${label} ${value}`).join(" · ");
}

/* -------------------------------------------------------------------------- */
/* OOBE reset strategies                                                       */
/* -------------------------------------------------------------------------- */

export interface OobeStrategyOption {
  id: OobeResetStrategy;
  label: string;
  description: string;
}

export const OOBE_STRATEGIES: readonly OobeStrategyOption[] = [
  {
    id: "snapshot",
    label: "Snapshot restore",
    description: "Fastest: reload the instance's golden OOBE snapshot and relaunch the Setup Wizard.",
  },
  {
    id: "wipe",
    label: "Wipe data",
    description: "Cleanest: delete the user-data partition and cold-boot into a factory-fresh Setup Wizard.",
  },
  {
    id: "package",
    label: "Reset Setup Wizard package",
    description: "Clear the first-run package state without wiping the rest of the user data.",
  },
  {
    id: "reboot",
    label: "Reboot only",
    description: "Restart the emulator without clearing data; useful when the Setup Wizard is stuck.",
  },
];

/* -------------------------------------------------------------------------- */
/* Errors + bulk operations                                                    */
/* -------------------------------------------------------------------------- */

export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    return error.hint ? `${error.message} (${error.hint})` : error.message;
  }
  if (error instanceof Error) return error.message;
  return String(error);
}

export function isNotFound(error: Error | null | undefined): boolean {
  return error instanceof ApiError && error.status === 404;
}

export interface BulkFailure {
  name: string;
  message: string;
}

export interface BulkResult {
  succeeded: string[];
  failed: BulkFailure[];
}

/**
 * Run `operation` for every name with a bounded worker pool. Failures are
 * collected per instance instead of aborting the whole batch — one broken AVD
 * must not block the others.
 */
export async function runBulk(
  names: readonly string[],
  operation: (name: string) => Promise<unknown>,
  concurrency = 4,
): Promise<BulkResult> {
  const queue = [...names];
  const succeeded: string[] = [];
  const failed: BulkFailure[] = [];

  const worker = async (): Promise<void> => {
    for (let name = queue.shift(); name !== undefined; name = queue.shift()) {
      try {
        await operation(name);
        succeeded.push(name);
      } catch (error) {
        failed.push({ name, message: describeError(error) });
      }
    }
  };

  const workers = Math.max(1, Math.min(concurrency, queue.length));
  await Promise.all(Array.from({ length: workers }, () => worker()));
  succeeded.sort((left, right) => left.localeCompare(right));
  failed.sort((left, right) => left.name.localeCompare(right.name));
  return { succeeded, failed };
}

/* -------------------------------------------------------------------------- */
/* Polling                                                                     */
/* -------------------------------------------------------------------------- */

export interface PolledData<T> {
  data: T | null;
  error: Error | null;
  /** Only true for the first load; background refreshes keep the data visible. */
  loading: boolean;
  refreshing: boolean;
  lastUpdatedAt: number | null;
  refresh: () => Promise<void>;
  setData: (updater: T | ((current: T | null) => T | null)) => void;
}

export interface PolledDataOptions {
  /** 0 disables the interval (manual refresh only). */
  intervalMs?: number;
  /** Semantic dependencies; changing them reloads from scratch. */
  deps?: readonly unknown[];
}

/**
 * Fetch-on-mount + silent interval polling.
 *
 * Unlike `useAsync`, refreshing never flips `loading` (so tables don't flash
 * skeletons every five seconds) and the latest loader closure is always used.
 */
export function usePolledData<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  options: PolledDataOptions = {},
): PolledData<T> {
  const { intervalMs = 5_000, deps = [] } = options;
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  const [data, setDataState] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<number | null>(null);

  const mounted = useRef(true);
  const inFlight = useRef(false);
  const pending = useRef(false);

  const load = useCallback(async (initial: boolean): Promise<void> => {
    if (inFlight.current) {
      pending.current = true;
      return;
    }
    inFlight.current = true;
    if (!initial) setRefreshing(true);
    try {
      const result = await loaderRef.current(new AbortController().signal);
      if (!mounted.current) return;
      setDataState(result);
      setError(null);
      setLastUpdatedAt(Date.now());
    } catch (caught) {
      if (!mounted.current) return;
      if (caught instanceof Error && caught.name === "AbortError") return;
      const apiError = caught instanceof ApiError ? caught : null;
      if (apiError?.kind === "aborted") return;
      setError(caught instanceof Error ? caught : new Error(String(caught)));
    } finally {
      inFlight.current = false;
      if (mounted.current) {
        setLoading(false);
        setRefreshing(false);
      }
      if (pending.current) {
        pending.current = false;
        void load(false);
      }
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void load(true);
    let timer: number | undefined;
    if (intervalMs > 0) {
      timer = window.setInterval(() => {
        if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
        void load(false);
      }, intervalMs);
    }
    const onVisibility = (): void => {
      if (document.visibilityState === "visible") void load(false);
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      mounted.current = false;
      if (timer !== undefined) window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
    // `deps` are the caller's semantic contract (like `useAsync`).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, load, ...deps]);

  const refresh = useCallback(() => load(false), [load]);

  const setData = useCallback((updater: T | ((current: T | null) => T | null)): void => {
    setDataState((current) =>
      typeof updater === "function" ? (updater as (value: T | null) => T | null)(current) : updater,
    );
  }, []);

  return { data, error, loading, refreshing, lastUpdatedAt, refresh, setData };
}

/* -------------------------------------------------------------------------- */
/* Fleet + instance live data                                                  */
/* -------------------------------------------------------------------------- */

type EventStreamValue = ReturnType<typeof useEventStream>;

/**
 * The console always mounts `EventStreamProvider`, but pages are also rendered
 * in isolation by the registry smoke test. Fall back to an inert stream instead
 * of crashing the whole page when the provider is absent.
 */
const EMPTY_EVENT_STREAM: EventStreamValue = {
  status: "disabled",
  events: [],
  lastEvent: null,
  clearEvents: () => undefined,
};

export function useOptionalEventStream(): EventStreamValue {
  try {
    return useEventStream();
  } catch {
    return EMPTY_EVENT_STREAM;
  }
}

/** Fleet list with 5 s polling and optimistic state overlays from the WS feed. */
export function useFleetInstances(intervalMs = 5_000): PolledData<AvdInfo[]> {
  const polled = usePolledData<AvdInfo[]>(() => api.listInstances(), { intervalMs });
  const { lastEvent } = useOptionalEventStream();
  const { refresh, setData } = polled;
  const handledEventId = useRef<string | null>(null);

  useEffect(() => {
    if (!lastEvent || handledEventId.current === lastEvent.id) return;
    handledEventId.current = lastEvent.id;
    const payload = lastEvent.payload as Partial<InstanceStatePayload>;
    if (lastEvent.type === "instance.state" && typeof payload.instance === "string") {
      const state = payload.state as InstanceState | undefined;
      if (typeof state === "string") {
        setData((current) =>
          current
            ? current.map((instance) =>
                instance.name === payload.instance ? { ...instance, state } : instance,
              )
            : current,
        );
        return;
      }
    }
    if (
      lastEvent.type === "instance.created" ||
      lastEvent.type === "instance.deleted" ||
      lastEvent.type === "instance.boot"
    ) {
      void refresh();
    }
  }, [lastEvent, refresh, setData]);

  return polled;
}

/** Single instance with polling + live state overlays (detail page). */
export function useInstanceDetail(name: string, intervalMs = 5_000): PolledData<AvdInfo> {
  const polled = usePolledData<AvdInfo>(() => api.getInstance(name), { intervalMs, deps: [name] });
  const { lastEvent } = useOptionalEventStream();
  const { refresh, setData } = polled;
  const handledEventId = useRef<string | null>(null);

  useEffect(() => {
    if (!lastEvent || handledEventId.current === lastEvent.id) return;
    handledEventId.current = lastEvent.id;
    const payload = lastEvent.payload as Partial<InstanceStatePayload>;
    if (lastEvent.type === "instance.state" && payload.instance === name) {
      const state = payload.state as InstanceState | undefined;
      if (typeof state === "string") setData((current) => (current ? { ...current, state } : current));
    } else if (lastEvent.type === "instance.boot" && payload.instance === name) {
      void refresh();
    }
  }, [lastEvent, name, refresh, setData]);

  return polled;
}

/** True when an event envelope refers to this instance (directly or by target list). */
function eventBelongsToInstance(event: FleetEvent, name: string): boolean {
  const payload = event.payload;
  if (payload.instance === name || payload.avd_name === name || payload.name === name) return true;
  const targets = payload.instances;
  return Array.isArray(targets) && targets.includes(name);
}

export interface InstanceEventsState {
  /** Newest-first, de-duplicated REST history + live stream events. */
  events: FleetEvent[];
  loading: boolean;
  error: Error | null;
  refresh: () => Promise<void>;
}

/** Events for one instance, merged from `GET /events` and the live WS buffer. */
export function useInstanceEvents(name: string, intervalMs = 10_000): InstanceEventsState {
  const polled = usePolledData<FleetEvent[]>(() => api.listEvents(200), { intervalMs });
  const { events: liveEvents } = useOptionalEventStream();
  const { data, loading, error, refresh } = polled;

  const events = useMemo(() => {
    const merged = new Map<string, FleetEvent>();
    for (const event of [...(data ?? []), ...liveEvents]) {
      if (eventBelongsToInstance(event, name)) merged.set(event.id, event);
    }
    return [...merged.values()].sort((left, right) => Date.parse(right.ts) - Date.parse(left.ts));
  }, [data, liveEvents, name]);

  return { events, loading: loading && data === null, error, refresh };
}

/* -------------------------------------------------------------------------- */
/* Deploy helpers                                                              */
/* -------------------------------------------------------------------------- */

/** Instance names accepted by `POST /instances` and used as the bulk prefix. */
export const NAME_PATTERN = /^[a-zA-Z0-9._-]{1,60}$/;
export const DEFAULT_INSTANCE_PREFIX = "avd";
export const MAX_INSTANCE_COUNT = 64;

/** Preview the names a bulk deploy will create (`prefix-01`, `prefix-02`, …). */
export function previewNames(prefix: string, count: number): string[] {
  const safePrefix = prefix.trim() || DEFAULT_INSTANCE_PREFIX;
  const safeCount = Math.max(0, Math.min(Math.round(count) || 0, MAX_INSTANCE_COUNT));
  const width = Math.max(2, String(safeCount).length);
  return Array.from({ length: safeCount }, (_, index) =>
    `${safePrefix}-${String(index + 1).padStart(width, "0")}`,
  );
}
