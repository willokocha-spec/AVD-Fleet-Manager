import { useEffect, useMemo, useState } from "react";

import {
  Badge,
  Button,
  ButtonLink,
  Card,
  Checkbox,
  ConfirmDialog,
  EmptyState,
  Field,
  IconButton,
  InlineNotice,
  Input,
  Modal,
  Select,
  StatusPill,
  Table,
  useToast,
} from "@/lib/components";
import { api } from "@/lib/api";
import { useAsync } from "@/lib/hooks";
import type {
  AvdInfo,
  FleetEvent,
  ProxyView,
  ScriptRun,
  Snapshot,
} from "@/lib/types";
import {
  formatBytes,
  formatDateTime,
  formatRelativeTime,
  formatTime,
  humanize,
  truncate,
} from "@/lib/utils";

import {
  ProxyBadge,
  describeError,
  isRunning,
  portEntries,
  usePolledData,
  type InstanceEventsState,
} from "./fleet-utils";

/* -------------------------------------------------------------------------- */
/* Shared helpers                                                              */
/* -------------------------------------------------------------------------- */

function eventDetail(event: FleetEvent): string {
  const payload = event.payload;
  for (const key of ["message", "reason", "activity", "line", "error", "summary", "status", "state"]) {
    const value = payload[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return "";
}

function runsForInstance(runs: readonly ScriptRun[], name: string): ScriptRun[] {
  return runs.filter((run) => (run.instances ?? []).includes(name));
}

function snapshotNameError(value: string): string | null {
  if (!value.trim()) return "A snapshot name is required.";
  if (!/^[a-zA-Z0-9._-]{1,60}$/.test(value.trim())) {
    return "Use letters, digits, dots, dashes and underscores (max 60).";
  }
  return null;
}

/* -------------------------------------------------------------------------- */
/* Overview                                                                    */
/* -------------------------------------------------------------------------- */

export function InstanceOverviewPanel({
  instance,
  proxies,
  events,
}: {
  instance: AvdInfo;
  proxies: readonly ProxyView[] | null;
  events: readonly FleetEvent[];
}) {
  const configEntries = Object.entries(instance.config ?? {});
  const ports = portEntries(instance);
  const proxy = proxies?.find((candidate) => candidate.id === instance.proxy_id) ?? null;
  const recentEvents = events.slice(0, 5);

  return (
    <div className="grid-2">
      <Card
        title="Configuration"
        description="Per-instance emulator overrides. Unset values come from the template."
        icon="settings"
      >
        {configEntries.length === 0 ? (
          <EmptyState
            compact
            icon="settings"
            title="No instance overrides"
            description={
              instance.template_id
                ? `This instance runs with the defaults from template ${instance.template_id}.`
                : "This instance has no template and uses the emulator defaults."
            }
          />
        ) : (
          <dl className="kv-list">
            {configEntries.map(([key, value]) => (
              <div className="kv-list__row" key={key}>
                <dt>
                  <code style={{ fontSize: "var(--fs-xs)" }}>{key}</code>
                </dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        )}
        <div style={{ marginTop: "var(--space-3)" }}>
          <ButtonLink to="/templates" size="sm" variant="secondary" icon="templates">
            Templates
          </ButtonLink>
        </div>
      </Card>

      <Card title="Ports & endpoints" description="Host ports allocated for console, ADB and gRPC." icon="server">
        {ports.length === 0 ? (
          <EmptyState compact icon="server" title="No ports allocated" description="Ports are assigned when the instance is started." />
        ) : (
          <dl className="kv-list">
            {ports.map(([label, value]) => (
              <div className="kv-list__row" key={label}>
                <dt>{label}</dt>
                <dd>
                  <code>{value}</code>
                </dd>
              </div>
            ))}
            <div className="kv-list__row">
              <dt>ADB serial</dt>
              <dd>
                <code>{instance.adb_serial ?? "—"}</code>
              </dd>
            </div>
            <div className="kv-list__row">
              <dt>gRPC endpoint</dt>
              <dd>
                <code>{instance.grpc_endpoint ?? "—"}</code>
              </dd>
            </div>
          </dl>
        )}
      </Card>

      <Card title="Proxy & egress" description="The proxy that isolates this instance's guest traffic." icon="proxies">
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
          <ProxyBadge proxyId={instance.proxy_id} proxies={proxies} />
          {proxy ? (
            <>
              <dl className="kv-list">
                <div className="kv-list__row">
                  <dt>Endpoint</dt>
                  <dd>
                    <code>
                      {proxy.scheme ?? "http"}://{proxy.host}:{proxy.port}
                    </code>
                  </dd>
                </div>
                <div className="kv-list__row">
                  <dt>Health</dt>
                  <dd>
                    <StatusPill status={proxy.health?.status ?? "unknown"} />
                  </dd>
                </div>
                <div className="kv-list__row">
                  <dt>Egress IP</dt>
                  <dd>{proxy.health?.egress_ip ?? "—"}</dd>
                </div>
              </dl>
              <ButtonLink to="/proxies" size="sm" variant="secondary" icon="external-link">
                Manage proxies
              </ButtonLink>
            </>
          ) : (
            <p className="muted small" style={{ margin: 0 }}>
              {instance.proxy_id
                ? `Proxy ${instance.proxy_id} is assigned but is not returned by the daemon.`
                : "No proxy assigned — guest traffic leaves directly from the host."}
            </p>
          )}
        </div>
      </Card>

      <Card title="Activity" description="Latest daemon events mentioning this instance." icon="runs">
        {recentEvents.length === 0 ? (
          <EmptyState
            compact
            icon="runs"
            title="No recorded activity"
            description="State changes, script output and OOBE results for this instance appear here."
          />
        ) : (
          <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
            {recentEvents.map((event) => (
              <li
                key={event.id}
                style={{ display: "flex", gap: "var(--space-3)", alignItems: "baseline", flexWrap: "wrap" }}
              >
                <span className="muted small" style={{ fontVariantNumeric: "tabular-nums" }}>
                  {formatTime(event.ts)}
                </span>
                <Badge tone="neutral">{humanize(event.type)}</Badge>
                <span className="small" style={{ color: "var(--text-secondary)", overflowWrap: "anywhere" }}>
                  {truncate(eventDetail(event) || event.type, 120)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Scripts & runs                                                              */
/* -------------------------------------------------------------------------- */

export function InstanceRunsPanel({ name }: { name: string }) {
  const toast = useToast();
  const runs = usePolledData<ScriptRun[]>(() => api.listRuns(50), { intervalMs: 0 });
  const scripts = useAsync(() => api.listScripts(), []);
  const [scriptId, setScriptId] = useState("");
  const [dryRun, setDryRun] = useState(false);
  const [starting, setStarting] = useState(false);
  const [cancelling, setCancelling] = useState<string | null>(null);

  const instanceRuns = useMemo(() => runsForInstance(runs.data ?? [], name), [runs.data, name]);
  const scriptOptions = useMemo(
    () =>
      (scripts.data ?? []).map((script) => ({
        value: script.id,
        label: `${script.name} · ${humanize(script.type)}`,
      })),
    [scripts.data],
  );

  const startRun = async (): Promise<void> => {
    if (!scriptId) return;
    setStarting(true);
    try {
      const run = await api.startScriptRun({
        script_id: scriptId,
        instances: [name],
        dry_run: dryRun,
      });
      toast.success(
        `Started "${run.script_name ?? scriptId}"`,
        dryRun ? "Dry run queued — no commands are executed." : `Target: ${name}`,
      );
      await runs.refresh();
    } catch (error) {
      toast.error("Could not start the script", describeError(error));
    } finally {
      setStarting(false);
    }
  };

  const cancelRun = async (run: ScriptRun): Promise<void> => {
    setCancelling(run.id);
    try {
      await api.cancelRun(run.id);
      toast.success(`Cancelled run ${run.id}`);
      await runs.refresh();
    } catch (error) {
      toast.error(`Could not cancel run ${run.id}`, describeError(error));
    } finally {
      setCancelling(null);
    }
  };

  return (
    <Card
      title="Scripts & runs"
      description="Runs that targeted this instance, and one-click scripts to execute against it."
      icon="scripts"
      actions={
        <>
          <ButtonLink to="/runs" size="sm" variant="secondary" icon="runs">
            All runs
          </ButtonLink>
          <IconButton
            label="Refresh runs"
            icon="refresh"
            size="sm"
            loading={runs.refreshing}
            onClick={() => void runs.refresh()}
          />
        </>
      }
    >
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "var(--space-2)",
          alignItems: "flex-end",
          marginBottom: "var(--space-4)",
        }}
      >
        <div style={{ minWidth: 260, flex: "1 1 260px" }}>
          <Field label="Run a script on this instance" hint="The script receives this instance's ADB serial as $SERIAL.">
            <Select
              aria-label="Script to run"
              value={scriptId}
              placeholder="Choose a script…"
              options={scriptOptions}
              disabled={starting || scripts.loading}
              onChange={(event) => setScriptId(event.target.value)}
            />
          </Field>
        </div>
        <Checkbox
          label="Dry run"
          checked={dryRun}
          disabled={starting}
          onChange={(event) => setDryRun(event.target.checked)}
        />
        <Button
          variant="primary"
          icon="play"
          size="md"
          loading={starting}
          disabled={!scriptId || scripts.loading}
          onClick={() => void startRun()}
        >
          Run script
        </Button>
      </div>

      {scripts.error ? (
        <InlineNotice tone="warning" title="Could not load scripts">
          <p>{describeError(scripts.error)}</p>
        </InlineNotice>
      ) : null}

      <Table<ScriptRun>
        caption="Script runs targeting this instance"
        rows={instanceRuns}
        rowKey={(run) => run.id}
        loading={runs.loading}
        skeletonRows={3}
        empty={
          <EmptyState
            compact
            icon="runs"
            title="No runs yet"
            description="Start a script above, or open the Runs page to see fleet-wide executions."
          />
        }
        columns={[
          {
            key: "id",
            header: "Run",
            width: "150px",
            render: (run) => <code style={{ fontSize: "var(--fs-xs)" }}>{run.id}</code>,
          },
          {
            key: "script",
            header: "Script",
            render: (run) => run.script_name ?? run.script_id,
          },
          {
            key: "state",
            header: "State",
            width: "120px",
            render: (run) => (
              <StatusPill status={run.state} pulse={run.state === "running" || run.state === "queued"} />
            ),
          },
          {
            key: "result",
            header: "This instance",
            width: "150px",
            render: (run) => {
              const result = run.results?.find((candidate) => candidate.instance === name);
              if (!result) return <span className="muted">—</span>;
              return (
                <StatusPill
                  status={result.state}
                  title={result.error || result.stderr || undefined}
                  pulse={result.state === "running" || result.state === "queued"}
                />
              );
            },
          },
          {
            key: "started",
            header: "Started",
            width: "150px",
            render: (run) => formatRelativeTime(run.started_at),
          },
          {
            key: "actions",
            header: "",
            width: "110px",
            align: "right",
            render: (run) =>
              run.state === "running" || run.state === "queued" ? (
                <Button
                  size="sm"
                  variant="ghost"
                  icon="stop"
                  loading={cancelling === run.id}
                  onClick={() => void cancelRun(run)}
                >
                  Cancel
                </Button>
              ) : null,
          },
        ]}
      />
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Snapshots                                                                   */
/* -------------------------------------------------------------------------- */

type SnapshotConfirm = { kind: "load" | "delete"; snapshot: Snapshot };

export function InstanceSnapshotsPanel({ name, state }: { name: string; state: AvdInfo["state"] }) {
  const toast = useToast();
  const snapshots = usePolledData<Snapshot[]>(() => api.listSnapshots(name), { intervalMs: 0, deps: [name] });
  const [saveOpen, setSaveOpen] = useState(false);
  const [snapshotName, setSnapshotName] = useState("");
  const [golden, setGolden] = useState(false);
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirm, setConfirm] = useState<SnapshotConfirm | null>(null);

  const openSave = (): void => {
    setSnapshotName(`snapshot-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}`);
    setGolden(false);
    setFieldError(null);
    setSaveOpen(true);
  };

  const saveSnapshot = async (): Promise<void> => {
    const error = snapshotNameError(snapshotName);
    if (error) {
      setFieldError(error);
      return;
    }
    setSaving(true);
    setFieldError(null);
    try {
      const created = await api.createSnapshot(name, { name: snapshotName.trim(), golden });
      toast.success(`Saved snapshot ${created.name}`, golden ? "Marked as golden." : undefined);
      setSaveOpen(false);
      await snapshots.refresh();
    } catch (caught) {
      setFieldError(describeError(caught));
    } finally {
      setSaving(false);
    }
  };

  const resolveConfirm = async (): Promise<void> => {
    const target = confirm;
    if (!target) return;
    try {
      if (target.kind === "load") {
        await api.loadSnapshot(name, target.snapshot.name);
        toast.success(`Loaded snapshot ${target.snapshot.name}`, "The instance state was restored.");
      } else {
        await api.deleteSnapshot(name, target.snapshot.name);
        toast.success(`Deleted snapshot ${target.snapshot.name}`);
      }
      setConfirm(null);
      await snapshots.refresh();
    } catch (caught) {
      toast.error(
        target.kind === "load"
          ? `Could not load snapshot ${target.snapshot.name}`
          : `Could not delete snapshot ${target.snapshot.name}`,
        describeError(caught),
      );
    }
  };

  return (
    <Card
      title="Snapshots"
      description="Save or restore emulator snapshots. Golden snapshots make reset-to-OOBE fast."
      icon="hard-drive"
      actions={
        <>
          <Button size="sm" variant="primary" icon="plus" onClick={openSave}>
            Save snapshot
          </Button>
          <IconButton
            label="Refresh snapshots"
            icon="refresh"
            size="sm"
            loading={snapshots.refreshing}
            onClick={() => void snapshots.refresh()}
          />
        </>
      }
    >
      {snapshots.error ? (
        <InlineNotice tone="danger" title="Could not load snapshots">
          <p>{describeError(snapshots.error)}</p>
        </InlineNotice>
      ) : null}
      {!isRunning(state) ? (
        <InlineNotice tone="info">
          <p>Snapshots are most useful while the instance is running — save one after a clean first boot.</p>
        </InlineNotice>
      ) : null}

      <Table<Snapshot>
        caption={`Snapshots of ${name}`}
        rows={snapshots.data ?? []}
        rowKey={(snapshot) => snapshot.name}
        loading={snapshots.loading}
        skeletonRows={3}
        empty={
          <EmptyState
            compact
            icon="hard-drive"
            title="No snapshots"
            description="Create a golden snapshot after the first successful boot to make OOBE resets instant."
            action={
              <Button size="sm" variant="primary" icon="plus" onClick={openSave}>
                Save the first snapshot
              </Button>
            }
          />
        }
        columns={[
          {
            key: "name",
            header: "Name",
            render: (snapshot) => (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                <code>{snapshot.name}</code>
                {snapshot.is_golden ? <Badge tone="warning" icon="shield">Golden</Badge> : null}
              </span>
            ),
          },
          {
            key: "created",
            header: "Created",
            width: "170px",
            render: (snapshot) => formatDateTime(snapshot.created_at),
          },
          {
            key: "size",
            header: "Size",
            width: "110px",
            align: "right",
            render: (snapshot) => formatBytes(snapshot.size_bytes),
          },
          {
            key: "actions",
            header: "",
            width: "190px",
            align: "right",
            render: (snapshot) => (
              <span style={{ display: "inline-flex", gap: 6 }}>
                <Button
                  size="sm"
                  variant="secondary"
                  icon="restart"
                  onClick={() => setConfirm({ kind: "load", snapshot })}
                >
                  Load
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  icon="trash"
                  onClick={() => setConfirm({ kind: "delete", snapshot })}
                >
                  Delete
                </Button>
              </span>
            ),
          },
        ]}
      />

      <Modal
        open={saveOpen}
        onClose={() => setSaveOpen(false)}
        title={`Save snapshot of ${name}`}
        description="Snapshots capture the current guest state. A golden snapshot is preferred for OOBE resets."
        size="sm"
        footer={
          <>
            <Button variant="ghost" disabled={saving} onClick={() => setSaveOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              icon="hard-drive"
              loading={saving}
              onClick={() => void saveSnapshot()}
            >
              Save snapshot
            </Button>
          </>
        }
      >
        <Field
          label="Snapshot name"
          required
          hint="Letters, digits, dots, dashes and underscores."
          error={fieldError ?? undefined}
        >
          <Input
            value={snapshotName}
            autoFocus
            disabled={saving}
            onChange={(event) => setSnapshotName(event.target.value)}
          />
        </Field>
        <Checkbox
          label="Mark as golden"
          hint="Golden snapshots are used as the preferred target for reset-to-OOBE."
          checked={golden}
          disabled={saving}
          onChange={(event) => setGolden(event.target.checked)}
        />
      </Modal>

      <ConfirmDialog
        open={confirm !== null}
        title={
          confirm
            ? confirm.kind === "load"
              ? `Load snapshot ${confirm.snapshot.name}?`
              : `Delete snapshot ${confirm.snapshot.name}?`
            : "Confirm snapshot action"
        }
        description={
          confirm?.kind === "load"
            ? "The instance is restored to this snapshot. Unsaved guest state is lost."
            : confirm?.kind === "delete"
              ? "The snapshot is deleted permanently and cannot be recovered."
              : undefined
        }
        confirmLabel={confirm?.kind === "load" ? "Load snapshot" : "Delete snapshot"}
        tone={confirm?.kind === "delete" ? "danger" : "primary"}
        onConfirm={resolveConfirm}
        onCancel={() => setConfirm(null)}
      />
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Events / logs                                                               */
/* -------------------------------------------------------------------------- */

export function InstanceEventsPanel({
  name,
  state,
}: {
  name: string;
  state: InstanceEventsState;
}) {
  return (
    <Card
      title="Logs & events"
      description={`Daemon events mentioning ${name}, including live messages from the event stream.`}
      icon="file"
      actions={
        <IconButton
          label="Refresh events"
          icon="refresh"
          size="sm"
          loading={state.events.length === 0 && state.loading}
          onClick={() => void state.refresh()}
        />
      }
    >
      {state.error ? (
        <InlineNotice tone="warning" title="Could not refresh events">
          <p>{describeError(state.error)}</p>
        </InlineNotice>
      ) : null}
      <Table<FleetEvent>
        caption={`Events for ${name}`}
        rows={state.events}
        rowKey={(event) => event.id}
        loading={state.loading}
        skeletonRows={4}
        empty={
          <EmptyState
            compact
            icon="file"
            title="No events captured"
            description="Boot messages, state transitions and script output for this instance will show up here."
          />
        }
        columns={[
          {
            key: "ts",
            header: "Time",
            width: "130px",
            render: (event) => (
              <span title={formatDateTime(event.ts)}>{formatTime(event.ts)}</span>
            ),
          },
          {
            key: "type",
            header: "Event",
            width: "190px",
            render: (event) => <Badge tone="neutral">{humanize(event.type)}</Badge>,
          },
          {
            key: "detail",
            header: "Detail",
            render: (event) => (
              <span style={{ overflowWrap: "anywhere" }}>{eventDetail(event) || "—"}</span>
            ),
          },
          {
            key: "source",
            header: "Source",
            width: "110px",
            render: (event) => <span className="muted">{event.source ?? "—"}</span>,
          },
        ]}
      />
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Proxy assignment                                                            */
/* -------------------------------------------------------------------------- */

export function InstanceProxyPanel({
  instance,
  proxies,
  proxiesLoading,
  proxiesError,
  refreshProxies,
  onAssignmentChanged,
}: {
  instance: AvdInfo;
  proxies: readonly ProxyView[] | null;
  proxiesLoading: boolean;
  proxiesError: Error | null;
  refreshProxies: () => Promise<void>;
  onAssignmentChanged: () => void;
}) {
  const toast = useToast();
  const [selectedProxyId, setSelectedProxyId] = useState(instance.proxy_id ?? "");
  const [assigning, setAssigning] = useState(false);
  const [verifying, setVerifying] = useState(false);

  useEffect(() => {
    setSelectedProxyId(instance.proxy_id ?? "");
  }, [instance.proxy_id]);

  const currentProxy = proxies?.find((proxy) => proxy.id === instance.proxy_id) ?? null;
  const selectedProxy = proxies?.find((proxy) => proxy.id === selectedProxyId) ?? null;
  const proxyOptions = useMemo(
    () =>
      (proxies ?? []).map((proxy) => ({
        value: proxy.id,
        label: `${proxy.name?.trim() || proxy.host} · ${proxy.scheme ?? "http"}://${proxy.host}:${proxy.port}${
          proxy.enabled === false ? " (disabled)" : ""
        }`,
        disabled: proxy.enabled === false,
      })),
    [proxies],
  );

  const assign = async (): Promise<void> => {
    if (!selectedProxyId) return;
    setAssigning(true);
    try {
      await api.assignProxy({ avd_name: instance.name, proxy_id: selectedProxyId });
      toast.success("Proxy assigned", `${selectedProxy?.name ?? selectedProxyId} → ${instance.name}`);
      onAssignmentChanged();
      await refreshProxies();
    } catch (error) {
      toast.error("Could not assign the proxy", describeError(error));
    } finally {
      setAssigning(false);
    }
  };

  const unassign = async (): Promise<void> => {
    setAssigning(true);
    try {
      await api.unassignProxy(instance.name);
      toast.success("Proxy unassigned", `${instance.name} now routes traffic directly.`);
      onAssignmentChanged();
      await refreshProxies();
    } catch (error) {
      toast.error("Could not unassign the proxy", describeError(error));
    } finally {
      setAssigning(false);
    }
  };

  const verifyEgress = async (): Promise<void> => {
    if (!currentProxy) return;
    setVerifying(true);
    try {
      const result = await api.verifyGuestEgress({
        avd_name: instance.name,
        proxy_id: currentProxy.id,
      });
      if (result.ok) {
        toast.success(
          "Guest egress verified",
          `${result.egress_ip ?? "unknown egress IP"} · ${result.latency_ms ?? "?"} ms`,
        );
      } else {
        toast.error("Guest egress check failed", result.error || "The guest could not reach the check endpoint.");
      }
    } catch (error) {
      toast.error("Could not verify guest egress", describeError(error));
    } finally {
      setVerifying(false);
    }
  };

  return (
    <div className="grid-2">
      <Card title="Current assignment" description="The proxy assigned to this instance, if any." icon="proxies">
        {currentProxy ? (
          <>
            <dl className="kv-list">
              <div className="kv-list__row">
                <dt>Proxy</dt>
                <dd>{currentProxy.name?.trim() || currentProxy.id}</dd>
              </div>
              <div className="kv-list__row">
                <dt>Endpoint</dt>
                <dd>
                  <code>
                    {currentProxy.scheme ?? "http"}://{currentProxy.host}:{currentProxy.port}
                  </code>
                </dd>
              </div>
              <div className="kv-list__row">
                <dt>Credentials</dt>
                <dd>{currentProxy.has_credentials ? "Stored in the vault" : "None"}</dd>
              </div>
              <div className="kv-list__row">
                <dt>Health</dt>
                <dd>
                  <StatusPill status={currentProxy.health?.status ?? "unknown"} />
                </dd>
              </div>
              <div className="kv-list__row">
                <dt>Egress IP</dt>
                <dd>{currentProxy.health?.egress_ip ?? "—"}</dd>
              </div>
              <div className="kv-list__row">
                <dt>Last checked</dt>
                <dd>{formatRelativeTime(currentProxy.health?.checked_at)}</dd>
              </div>
            </dl>
            <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap", marginTop: "var(--space-3)" }}>
              <Button
                size="sm"
                variant="secondary"
                icon="wifi"
                loading={verifying}
                disabled={!isRunning(instance.state)}
                title={
                  isRunning(instance.state)
                    ? "Verify traffic leaves through this proxy"
                    : "The instance must be running to verify guest egress"
                }
                onClick={() => void verifyEgress()}
              >
                Verify guest egress
              </Button>
              <Button
                size="sm"
                variant="danger"
                icon="close"
                loading={assigning}
                onClick={() => void unassign()}
              >
                Unassign proxy
              </Button>
            </div>
          </>
        ) : (
          <EmptyState
            compact
            icon="proxies"
            title="No proxy assigned"
            description="Guest traffic leaves directly from the host. Assign a proxy to isolate this instance's egress."
          />
        )}
      </Card>

      <Card
        title="Assign a proxy"
        description="Pick an existing proxy managed by this daemon. Health is checked from the Proxies page."
        icon="link"
        actions={
          <ButtonLink to="/proxies" size="sm" variant="secondary" icon="external-link">
            Manage proxies
          </ButtonLink>
        }
      >
        {proxiesError ? (
          <InlineNotice tone="danger" title="Could not load proxies">
            <p>{describeError(proxiesError)}</p>
          </InlineNotice>
        ) : null}
        <Field label="Proxy" hint="Disabled proxies cannot be assigned.">
          <Select
            aria-label="Proxy to assign"
            value={selectedProxyId}
            placeholder={proxiesLoading ? "Loading proxies…" : "Choose a proxy…"}
            options={proxyOptions}
            disabled={proxiesLoading || assigning}
            onChange={(event) => setSelectedProxyId(event.target.value)}
          />
        </Field>
        {selectedProxy ? (
          <p className="muted small" style={{ marginTop: "var(--space-2)" }}>
            {selectedProxy.name?.trim() || selectedProxy.id} · health{" "}
            {selectedProxy.health?.status ?? "unknown"}
            {selectedProxy.enabled === false ? " · disabled" : ""}
          </p>
        ) : null}
        {selectedProxy?.enabled === false ? (
          <InlineNotice tone="warning">
            <p>This proxy is disabled. Enable it on the Proxies page before assigning it.</p>
          </InlineNotice>
        ) : null}
        <div style={{ display: "flex", gap: "var(--space-2)", marginTop: "var(--space-3)" }}>
          <Button
            variant="primary"
            icon="link"
            loading={assigning}
            disabled={
              !selectedProxyId ||
              selectedProxyId === instance.proxy_id ||
              selectedProxy?.enabled === false ||
              proxiesLoading
            }
            onClick={() => void assign()}
          >
            Assign proxy
          </Button>
          <Button
            variant="ghost"
            icon="refresh"
            loading={proxiesLoading}
            onClick={() => void refreshProxies()}
          >
            Refresh proxies
          </Button>
        </div>
      </Card>
    </div>
  );
}
