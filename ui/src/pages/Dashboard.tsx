import { useEffect, useMemo, useState } from "react";

import { ApiError, api } from "@/lib/api";
import {
  Badge,
  Button,
  ButtonLink,
  Card,
  ConfirmDialog,
  EmptyState,
  Icon,
  InlineNotice,
  PageHeader,
  ProgressBar,
  Skeleton,
  StatCard,
  StatusPill,
  Table,
  type Column,
  type IconName,
  useToast,
} from "@/lib/components";
import { useAsync } from "@/lib/hooks";
import type { PageMeta } from "@/lib/page-meta";
import type {
  AvdInfo,
  CapacityInfo,
  EventType,
  FleetEvent,
  OobeMatrix,
  ScriptRun,
  SystemImage,
  SystemInfo,
} from "@/lib/types";
import {
  formatBytes,
  formatDuration,
  formatRelativeTime,
  humanize,
  pluralize,
  truncate,
} from "@/lib/utils";

export const meta: PageMeta = {
  path: "/",
  title: "Dashboard",
  icon: "dashboard",
  nav: 10,
  description: "Fleet health, capacity and recent activity",
};

/** Dashboard auto-refresh interval; the manual Refresh button shares the loader. */
const REFRESH_INTERVAL_MS = 10_000;
const RECENT_EVENT_LIMIT = 6;
const RECENT_RUN_LIMIT = 5;

/* -------------------------------------------------------------------------- */
/* Data loading                                                                */
/* -------------------------------------------------------------------------- */

interface DashboardData {
  instances: AvdInfo[];
  capacity: CapacityInfo;
  system: SystemInfo;
  runs: ScriptRun[];
  events: FleetEvent[];
  images: SystemImage[];
  oobe: OobeMatrix;
}

async function loadDashboard(): Promise<DashboardData> {
  const [instances, capacity, system, runs, events, images, oobe] = await Promise.all([
    api.listInstances(),
    api.getCapacity(),
    api.getSystem(),
    api.listRuns(RECENT_RUN_LIMIT),
    api.listEvents(RECENT_EVENT_LIMIT * 2),
    api.listImages(),
    api.getOobeMatrix(),
  ]);
  return { instances, capacity, system, runs, events, images, oobe };
}

/* -------------------------------------------------------------------------- */
/* Small formatting helpers                                                    */
/* -------------------------------------------------------------------------- */

function readNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function formatMb(mb: number | null | undefined): string {
  if (mb === null || mb === undefined) return "—";
  return formatBytes(mb * 1024 * 1024);
}

function errorText(error: Error | ApiError | null): string {
  if (!error) return "";
  if (error instanceof ApiError) {
    return [error.message, error.hint].filter(Boolean).join(" — ");
  }
  return error.message;
}

const EVENT_ICONS: Partial<Record<EventType, IconName>> = {
  "instance.created": "instance",
  "instance.deleted": "instance",
  "instance.state": "instance",
  "instance.boot": "instance",
  "instance.log": "instance",
  "instance.screen": "camera",
  "proxy.created": "proxies",
  "proxy.updated": "proxies",
  "proxy.checked": "proxies",
  "script.run.started": "scripts",
  "script.run.output": "scripts",
  "script.run.finished": "scripts",
  "install.step": "download",
  "install.finished": "download",
  "oobe.result": "check-circle",
  "doctor.finished": "doctor",
  "fleet.applied": "fleet",
  system: "server",
  error: "alert-circle",
  log: "info",
};

function eventIcon(type: EventType): IconName {
  return EVENT_ICONS[type] ?? "info";
}

/** One-line, redaction-safe summary of an event payload for the activity feed. */
function summarizeEvent(event: FleetEvent): string {
  const payload = event.payload ?? {};
  const text = (key: string): string | null => {
    const value = payload[key];
    return typeof value === "string" && value.trim() ? value : null;
  };
  switch (event.type) {
    case "instance.state": {
      const instance = text("instance") ?? "instance";
      const state = text("state");
      const reason = text("reason");
      return state ? `${instance} → ${humanize(state)}${reason ? ` (${reason})` : ""}` : `${instance} state changed`;
    }
    case "instance.created":
    case "instance.deleted":
    case "instance.boot": {
      const instance = text("instance") ?? "instance";
      return `${humanize(event.type)}: ${instance}`;
    }
    case "proxy.checked": {
      const proxy = text("proxy_id") ?? "proxy";
      const ok = payload["ok"];
      return `${proxy} check ${ok === false ? "failed" : "passed"}`;
    }
    case "script.run.started":
    case "script.run.finished": {
      const run = text("run_id") ?? "run";
      return `${humanize(event.type)}: ${run}`;
    }
    case "doctor.finished": {
      const summary = text("summary");
      return summary ? `Doctor finished: ${summary}` : "Doctor run finished";
    }
    case "fleet.applied":
      return "Fleet configuration applied";
    default: {
      const detail = text("message") ?? text("summary") ?? text("error");
      return detail ? `${humanize(event.type)}: ${truncate(detail, 90)}` : humanize(event.type);
    }
  }
}

const RUN_COLUMNS: Column<ScriptRun>[] = [
  {
    key: "script",
    header: "Script",
    render: (run) => (
      <div>
        <div style={{ color: "var(--text)", fontWeight: 560 }}>{run.script_name ?? run.script_id}</div>
        <div className="muted small">{run.type ? humanize(run.type) : run.id}</div>
      </div>
    ),
  },
  {
    key: "state",
    header: "Status",
    render: (run) => <StatusPill status={run.state} />,
  },
  {
    key: "instances",
    header: "Targets",
    align: "right",
    render: (run) => pluralize(run.instances?.length ?? 0, "instance"),
  },
  {
    key: "started",
    header: "Started",
    align: "right",
    render: (run) => (
      <span title={run.started_at ?? undefined}>{formatRelativeTime(run.started_at)}</span>
    ),
  },
];

/* -------------------------------------------------------------------------- */
/* Page                                                                        */
/* -------------------------------------------------------------------------- */

export default function DashboardPage() {
  const toast = useToast();
  const { data, error, loading, reload } = useAsync(loadDashboard, []);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [confirmStart, setConfirmStart] = useState(false);

  useEffect(() => {
    if (data) setLastUpdated(Date.now());
  }, [data]);

  // Auto-refresh keeps the overview live without asking the operator to reload.
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      reload();
    }, REFRESH_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [reload]);

  const counts = useMemo(() => {
    const instances = data?.instances ?? [];
    const stateOf = (instance: AvdInfo): string => instance.state;
    return {
      total: instances.length,
      running: instances.filter((instance) => stateOf(instance) === "running").length,
      booting: instances.filter(
        (instance) => stateOf(instance) === "booting" || stateOf(instance) === "starting",
      ).length,
      stopped: instances.filter((instance) => stateOf(instance) === "stopped").length,
      errors: instances.filter((instance) => stateOf(instance) === "error").length,
    };
  }, [data]);

  const initialLoading = loading && data === null;
  const recentEvents = useMemo(() => {
    const seen = new Map<string, FleetEvent>();
    for (const event of data?.events ?? []) seen.set(event.id, event);
    return [...seen.values()]
      .sort((left, right) => new Date(right.ts).getTime() - new Date(left.ts).getTime())
      .slice(0, RECENT_EVENT_LIMIT);
  }, [data]);

  const capacity = data?.capacity;
  const ramAvailableMb = capacity?.ram_available_mb ?? null;
  const ramRequiredMb = capacity?.ram_required_mb ?? null;
  const ramTotalMb =
    ramAvailableMb !== null && ramRequiredMb !== null ? ramAvailableMb + ramRequiredMb : null;
  const portsAllocated = readNumber(capacity?.ports?.["allocated"]);
  const portsAvailable = readNumber(capacity?.ports?.["available"]);
  const portTotal =
    portsAllocated !== null && portsAvailable !== null ? portsAllocated + portsAvailable : null;

  const startable = useMemo(
    () =>
      (data?.instances ?? []).filter(
        (instance) => instance.state === "stopped" || instance.state === "defined",
      ),
    [data],
  );

  const handleStartAll = async (): Promise<void> => {
    const targets = startable;
    if (targets.length === 0) return;
    const results = await Promise.allSettled(
      targets.map((instance) => api.startInstance(instance.name)),
    );
    const failed = results.filter((result) => result.status === "rejected").length;
    const started = targets.length - failed;
    if (failed === 0) {
      toast.success(
        `${pluralize(started, "instance")} starting`,
        "Boot progress appears in the fleet as each emulator comes up.",
      );
    } else if (started === 0) {
      toast.error(
        `Could not start ${pluralize(failed, "instance")}`,
        "Open the fleet and the doctor report to inspect the failure.",
      );
    } else {
      toast.warning(
        `${started} started, ${failed} failed`,
        "Some instances did not accept the start request; review their error state.",
      );
    }
    setConfirmStart(false);
    reload();
  };

  /* -- derived presentation values ---------------------------------------- */

  const system = data?.system;
  const images = data?.images ?? [];
  const installedImages = images.filter((image) => image.installed).length;
  const oobeRecords = data?.oobe.records ?? [];
  const verifiedOobe = oobeRecords.filter((record) => record.status === "verified").length;
  const unverifiedOobe = oobeRecords.length - verifiedOobe;
  const acceleratorOk = system?.accelerator_ok ?? system?.hypervisor?.available ?? null;

  return (
    <div className="page">
      <PageHeader
        title="Dashboard"
        eyebrow="Fleet overview"
        description="Daemon health, instance states, capacity and the latest activity on this host."
        meta={
          <>
            {acceleratorOk === null ? null : (
              <StatusPill
                status={acceleratorOk ? "ok" : "fail"}
                icon="cpu"
                label={acceleratorOk ? "Acceleration ready" : "Acceleration blocked"}
              />
            )}
            <Badge tone="neutral" icon="clock">
              Auto-refresh 10 s
            </Badge>
            <span className="muted small" aria-live="polite">
              Updated {lastUpdated ? new Date(lastUpdated).toLocaleTimeString() : "—"}
            </span>
          </>
        }
        actions={
          <>
            <Button
              variant="secondary"
              icon="refresh"
              loading={loading}
              onClick={reload}
            >
              Refresh
            </Button>
            <ButtonLink to="/fleet" variant="secondary" icon="fleet">
              Open fleet
            </ButtonLink>
          </>
        }
      />

      {error ? (
        <InlineNotice
          tone="danger"
          title="Dashboard data could not be loaded"
          action={
            <Button size="sm" variant="secondary" icon="refresh" onClick={reload}>
              Retry
            </Button>
          }
        >
          <p>{errorText(error)}</p>
          {data ? <p>Showing the last data received before the failure.</p> : null}
        </InlineNotice>
      ) : null}

      <div className="stat-grid">
        <StatCard
          label="Total AVDs"
          value={data ? counts.total : "—"}
          hint={`${capacity?.max_instances ?? "—"} instance limit`}
          icon="fleet"
          loading={initialLoading}
        />
        <StatCard
          label="Running AVDs"
          value={data ? counts.running : "—"}
          hint="Boot completed"
          icon="play"
          tone="success"
          loading={initialLoading}
        />
        <StatCard
          label="Booting"
          value={data ? counts.booting : "—"}
          hint="Starting or booting"
          icon="clock"
          tone="warning"
          loading={initialLoading}
        />
        <StatCard
          label="Stopped"
          value={data ? counts.stopped : "—"}
          hint="No emulator process"
          icon="stop"
          loading={initialLoading}
        />
        <StatCard
          label="Errors"
          value={data ? counts.errors : "—"}
          hint={counts.errors > 0 ? "Needs attention" : "No failed instances"}
          icon="alert-triangle"
          tone={counts.errors > 0 ? "danger" : "default"}
          loading={initialLoading}
        />
      </div>

      {data && data.instances.length === 0 ? (
        <EmptyState
          compact
          icon="fleet"
          title="No instances are deployed"
          description="Deploy AVDs from a template to populate the fleet overview, capacity and activity panels."
          action={
            <ButtonLink to="/deploy" variant="primary" icon="deploy">
              Deploy AVDs
            </ButtonLink>
          }
        />
      ) : null}

      <div className="grid-2">
        <Card
          title="Capacity"
          description="RAM, port slots and instance headroom reported by the daemon."
          icon="cpu"
          actions={
            <ButtonLink to="/settings" variant="ghost" size="sm" icon="settings">
              Limits
            </ButtonLink>
          }
        >
          {initialLoading ? (
            <div className="skeleton-card">
              <Skeleton lines={3} />
              <Skeleton width="48%" />
            </div>
          ) : capacity ? (
            <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
              <ProgressBar
                label="Committed RAM"
                value={ramRequiredMb ?? 0}
                max={ramTotalMb && ramTotalMb > 0 ? ramTotalMb : 1}
                showValue
                tone={ramTotalMb && ramRequiredMb && ramRequiredMb / ramTotalMb > 0.85 ? "warning" : "accent"}
              />
              <ProgressBar
                label="Allocated port slots"
                value={portsAllocated ?? 0}
                max={portTotal && portTotal > 0 ? portTotal : 1}
                showValue
                tone={portTotal && portsAllocated && portsAllocated / portTotal > 0.85 ? "warning" : "accent"}
              />
              <dl className="kv-list">
                <div className="kv-list__row">
                  <dt>RAM available</dt>
                  <dd>{formatMb(ramAvailableMb)}</dd>
                </div>
                <div className="kv-list__row">
                  <dt>RAM committed</dt>
                  <dd>{formatMb(ramRequiredMb)}</dd>
                </div>
                <div className="kv-list__row">
                  <dt>Ports free</dt>
                  <dd>{portsAvailable ?? "—"}</dd>
                </div>
                <div className="kv-list__row">
                  <dt>CPU cores</dt>
                  <dd>{capacity.cpu_cores ?? system?.cpu_count ?? "—"}</dd>
                </div>
                <div className="kv-list__row">
                  <dt>Can start more</dt>
                  <dd>{capacity.can_start_more ?? "—"}</dd>
                </div>
              </dl>
              {(capacity.warnings ?? []).length > 0 ? (
                <InlineNotice tone="warning" title="Capacity warnings">
                  {(capacity.warnings ?? []).map((warning) => (
                    <p key={warning}>{warning}</p>
                  ))}
                </InlineNotice>
              ) : null}
            </div>
          ) : (
            <EmptyState
              compact
              icon="cpu"
              title="Capacity unavailable"
              description="The daemon did not return a capacity report. Retry once it is reachable."
            />
          )}
        </Card>

        <Card
          title="Quick actions"
          description="Deploy, diagnose or start the stopped part of the fleet."
          icon="zap"
        >
          <div style={{ display: "grid", gap: "var(--space-2)" }}>
            <ButtonLink to="/deploy" variant="primary" icon="deploy" fullWidth>
              Deploy AVDs
            </ButtonLink>
            <ButtonLink to="/doctor" variant="secondary" icon="doctor" fullWidth>
              Run environment doctor
            </ButtonLink>
            <Button
              variant="secondary"
              icon="play"
              fullWidth
              disabled={startable.length === 0}
              title={
                startable.length === 0
                  ? "No stopped instances to start"
                  : `Start ${pluralize(startable.length, "instance")}`
              }
              onClick={() => setConfirmStart(true)}
            >
              Start stopped instances{startable.length > 0 ? ` (${startable.length})` : ""}
            </Button>
          </div>
          <p className="muted small" style={{ marginTop: "var(--space-3)" }}>
            Starting reuses each instance&apos;s reserved console, adb and gRPC ports plus its
            assigned proxy. The action asks for confirmation first.
          </p>
        </Card>
      </div>

      <div className="grid-2">
        <Card
          title="Recent runs"
          description="Latest script fan-outs from GET /runs."
          icon="runs"
          flush
          actions={
            <ButtonLink to="/runs" variant="ghost" size="sm" icon="arrow-right">
              All runs
            </ButtonLink>
          }
        >
          <Table
            dense
            columns={RUN_COLUMNS}
            rows={data?.runs ?? []}
            rowKey={(run) => run.id}
            loading={initialLoading}
            skeletonRows={3}
            empty={
              <EmptyState
                compact
                icon="scripts"
                title="No script runs yet"
                description="Launch a script from the Scripts page to see run history here."
                action={
                  <ButtonLink to="/scripts" variant="secondary" icon="scripts" size="sm">
                    Go to scripts
                  </ButtonLink>
                }
              />
            }
          />
        </Card>

        <Card
          title="Recent events"
          description="Newest events from GET /events; refreshed with the dashboard."
          icon="clock"
          actions={
            <Badge tone="neutral" icon="wifi">
              10 s refresh
            </Badge>
          }
        >
          {initialLoading ? (
            <div className="skeleton-card">
              <Skeleton lines={4} />
            </div>
          ) : recentEvents.length === 0 ? (
            <EmptyState
              compact
              icon="info"
              title="No events yet"
              description="Instance, proxy, doctor and script activity will stream into this panel."
            />
          ) : (
            <ul className="type-list">
              {recentEvents.map((event) => (
                <li key={event.id} className="type-list__item" style={{ alignItems: "flex-start" }}>
                  <span className="type-list__icon" aria-hidden="true" style={{ marginTop: 2 }}>
                    <Icon name={eventIcon(event.type)} size={16} />
                  </span>
                  <span className="type-list__title" style={{ flex: "1 1 220px", fontWeight: 560 }}>
                    {summarizeEvent(event)}
                  </span>
                  <span className="type-list__hint" title={event.ts}>
                    {event.type} · {formatRelativeTime(event.ts)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <div className="grid-3">
        <Card title="Image readiness" description="Installed system images and their OOBE verdict." icon="images">
          {initialLoading ? (
            <Skeleton lines={3} />
          ) : images.length === 0 ? (
            <EmptyState
              compact
              icon="images"
              title="No images"
              description="No system images are available yet."
              action={
                <ButtonLink to="/images" variant="secondary" icon="images" size="sm">
                  Open images
                </ButtonLink>
              }
            />
          ) : (
            <>
              <p className="small" style={{ marginBottom: "var(--space-2)" }}>
                <strong>{installedImages}</strong> of {images.length} images installed
              </p>
              <ProgressBar
                label="Installed images"
                value={installedImages}
                max={Math.max(images.length, 1)}
                showValue
                tone={installedImages === images.length ? "success" : "accent"}
              />
              <ul className="type-list" style={{ marginTop: "var(--space-3)" }}>
                {images.slice(0, 3).map((image) => (
                  <li key={image.id} className="type-list__item">
                    <span className="type-list__icon" aria-hidden="true">
                      <Icon name="images" size={16} />
                    </span>
                    <span className="type-list__title" style={{ flex: "1 1 160px" }}>
                      {image.display_name ?? image.id}
                    </span>
                    <span
                      className="type-list__hint"
                      style={{ display: "inline-flex", gap: "var(--space-2)", alignItems: "center" }}
                    >
                      <Badge tone={image.installed ? "success" : "neutral"}>
                        {image.installed ? "Installed" : "Remote"}
                      </Badge>
                      <StatusPill status={image.oobe_status ?? "unverified"} />
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </Card>

        <Card title="OOBE coverage" description="First-boot Setup Wizard verification matrix." icon="check-circle">
          {initialLoading ? (
            <Skeleton lines={3} />
          ) : oobeRecords.length === 0 ? (
            <EmptyState
              compact
              icon="check-circle"
              title="No OOBE records"
              description="Run OOBE verification from the Images page to build the matrix."
            />
          ) : (
            <>
              <p className="small" style={{ marginBottom: "var(--space-2)" }}>
                <strong>{verifiedOobe}</strong> of {pluralize(oobeRecords.length, "record")} verified
              </p>
              <ProgressBar
                label="Verified OOBE records"
                value={verifiedOobe}
                max={Math.max(oobeRecords.length, 1)}
                showValue
                tone={unverifiedOobe > 0 ? "warning" : "success"}
              />
              <dl className="kv-list" style={{ marginTop: "var(--space-2)" }}>
                <div className="kv-list__row">
                  <dt>Host ABI</dt>
                  <dd>{data?.oobe.host_abi ?? "—"}</dd>
                </div>
                <div className="kv-list__row">
                  <dt>Unverified</dt>
                  <dd>{unverifiedOobe}</dd>
                </div>
              </dl>
              <div style={{ marginTop: "var(--space-3)" }}>
                <ButtonLink to="/images" variant="secondary" icon="images" size="sm">
                  Manage OOBE
                </ButtonLink>
              </div>
            </>
          )}
        </Card>

        <Card
          title="Host & daemon"
          description="Runtime facts from GET /system."
          icon="server"
          footer={
            <ButtonLink to="/doctor" variant="ghost" size="sm" icon="doctor">
              Troubleshoot acceleration
            </ButtonLink>
          }
        >
          {initialLoading ? (
            <Skeleton lines={5} />
          ) : system ? (
            <dl className="kv-list">
              <div className="kv-list__row">
                <dt>Daemon</dt>
                <dd>
                  {system.version ? `v${system.version}` : "—"}
                  {system.edition ? ` · ${humanize(system.edition)}` : ""}
                </dd>
              </div>
              <div className="kv-list__row">
                <dt>Uptime</dt>
                <dd>{formatDuration(system.uptime_s)}</dd>
              </div>
              <div className="kv-list__row">
                <dt>Operating system</dt>
                <dd>{[system.os_name, system.os_version].filter(Boolean).join(" ") || humanize(system.os) || "—"}</dd>
              </div>
              <div className="kv-list__row">
                <dt>Architecture</dt>
                <dd>
                  {system.arch ?? "—"}
                  {system.machine ? ` (${system.machine})` : ""}
                </dd>
              </div>
              <div className="kv-list__row">
                <dt>Python</dt>
                <dd>{system.python_version ?? "—"}</dd>
              </div>
              <div className="kv-list__row">
                <dt>CPU / RAM</dt>
                <dd>
                  {system.cpu_count ?? "—"} cores · {system.total_ram_gb ?? "—"} GB
                </dd>
              </div>
              <div className="kv-list__row">
                <dt>Acceleration</dt>
                <dd>
                  <StatusPill
                    status={acceleratorOk ? "ok" : acceleratorOk === false ? "fail" : "unknown"}
                    label={
                      system.hypervisor?.available
                        ? humanize(system.hypervisor.kind)
                        : acceleratorOk === false
                          ? "Blocked"
                          : "Unknown"
                    }
                    icon="cpu"
                  />
                </dd>
              </div>
            </dl>
          ) : (
            <EmptyState
              compact
              icon="server"
              title="System information unavailable"
              description="The daemon did not return host information."
            />
          )}
        </Card>
      </div>

      <ConfirmDialog
        open={confirmStart}
        title="Start stopped instances?"
        description={`Start ${pluralize(startable.length, "instance")} with their reserved ports and assigned proxies.`}
        confirmLabel="Start instances"
        tone="primary"
        onCancel={() => setConfirmStart(false)}
        onConfirm={handleStartAll}
      >
        <ul style={{ margin: 0, paddingLeft: "1.1rem", display: "grid", gap: "0.25rem" }}>
          {startable.slice(0, 6).map((instance) => (
            <li key={instance.name}>
              <code>{instance.name}</code>
            </li>
          ))}
          {startable.length > 6 ? (
            <li className="muted">and {startable.length - 6} more…</li>
          ) : null}
        </ul>
      </ConfirmDialog>
    </div>
  );
}
