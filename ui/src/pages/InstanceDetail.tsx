import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import {
  Badge,
  Button,
  ButtonLink,
  Card,
  ConfirmDialog,
  EmptyState,
  Field,
  IconButton,
  InlineNotice,
  Input,
  Modal,
  PageHeader,
  Skeleton,
  Tabs,
  useToast,
  type TabItem,
} from "@/lib/components";
import { useAsync } from "@/lib/hooks";
import { api } from "@/lib/api";
import type { PageMeta } from "@/lib/page-meta";
import { formatDateTime } from "@/lib/utils";

import {
  InstanceEventsPanel,
  InstanceOverviewPanel,
  InstanceProxyPanel,
  InstanceRunsPanel,
  InstanceSnapshotsPanel,
} from "./_components/fleet/InstanceTabs";
import { ResetOobeDialog, type ResetOobeOptions } from "./_components/fleet/ResetOobeDialog";
import { ScreenPreview } from "./_components/fleet/ScreenPreview";
import {
  InstanceStatePill,
  LiveUptime,
  ProxyBadge,
  canRestart,
  canStart,
  canStop,
  describeError,
  isNotFound,
  isRunning,
  portEntries,
  useInstanceDetail,
  useInstanceEvents,
} from "./_components/fleet/fleet-utils";

export const meta: PageMeta = {
  path: "/fleet/:name",
  title: "Instance",
  icon: "instance",
  nav: false,
  description: "Single AVD detail",
};

type LifecycleAction = "start" | "stop" | "restart";

const LIFECYCLE_PAST: Record<LifecycleAction, string> = {
  start: "Started",
  stop: "Stopped",
  restart: "Restarted",
};

const NAME_PATTERN = /^[a-zA-Z0-9._-]{1,63}$/;

/** Tab shell rendered while the instance request is in flight. */
const LOADING_TABS: TabItem[] = [
  {
    id: "overview",
    label: "Overview",
    icon: "dashboard",
    content: <Skeleton lines={6} />,
  },
  {
    id: "runs",
    label: "Scripts & runs",
    icon: "scripts",
    content: <Skeleton lines={4} />,
  },
  {
    id: "snapshots",
    label: "Snapshots",
    icon: "hard-drive",
    content: <Skeleton lines={4} />,
  },
  {
    id: "events",
    label: "Logs & events",
    icon: "file",
    content: <Skeleton lines={4} />,
  },
  {
    id: "proxy",
    label: "Proxy",
    icon: "proxies",
    content: <Skeleton lines={4} />,
  },
];

function ScreenCardSkeleton() {
  return (
    <div className="grid-2">
      <Card title="Live screen" icon="monitor">
        <Skeleton variant="rect" height={260} />
      </Card>
      <Card title="Runtime" icon="instance">
        <Skeleton lines={6} />
      </Card>
    </div>
  );
}

/**
 * Single-instance operator view: live screen with input, lifecycle actions,
 * OOBE reset, clone/delete, and per-instance runs, snapshots, events and proxy
 * assignment.
 */
export default function InstanceDetailPage() {
  const { name = "" } = useParams<{ name: string }>();
  const navigate = useNavigate();
  const toast = useToast();
  const instanceState = useInstanceDetail(name);
  const instanceEvents = useInstanceEvents(name);
  const proxies = useAsync(() => api.listProxies(true), []);

  const instance = instanceState.data;
  const notFound = isNotFound(instanceState.error);

  const [action, setAction] = useState<LifecycleAction | null>(null);
  const [resetOpen, setResetOpen] = useState(false);
  const [cloneOpen, setCloneOpen] = useState(false);
  const [cloneName, setCloneName] = useState("");
  const [cloneError, setCloneError] = useState<string | null>(null);
  const [cloning, setCloning] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  const runLifecycle = async (kind: LifecycleAction): Promise<void> => {
    if (!instance || action) return;
    setAction(kind);
    try {
      const updated =
        kind === "start"
          ? await api.startInstance(name)
          : kind === "stop"
            ? await api.stopInstance(name)
            : await api.restartInstance(name);
      instanceState.setData(updated);
      toast.success(`${LIFECYCLE_PAST[kind]} ${name}`);
      void instanceState.refresh();
    } catch (error) {
      toast.error(`Could not ${kind} ${name}`, describeError(error));
    } finally {
      setAction(null);
    }
  };

  const confirmReset = async (options: ResetOobeOptions): Promise<void> => {
    const result = await api.resetInstanceOobe(name, options);
    instanceState.setData((current) =>
      current ? { ...current, state: "booting", boot_completed: false } : current,
    );
    toast.success(
      `Reset ${name} to OOBE`,
      [
        result.message,
        `strategy: ${result.strategy ?? options.strategy}`,
        result.seconds !== null && result.seconds !== undefined ? `${result.seconds} s` : null,
        options.verify ? "verification requested" : null,
      ]
        .filter(Boolean)
        .join(" · "),
    );
    void instanceState.refresh();
  };

  const openClone = (): void => {
    setCloneName(`${name}-copy`);
    setCloneError(null);
    setCloneOpen(true);
  };

  const submitClone = async (): Promise<void> => {
    const candidate = cloneName.trim();
    if (!NAME_PATTERN.test(candidate)) {
      setCloneError("Use 1–63 letters, digits, dots, dashes or underscores.");
      return;
    }
    if (candidate === name) {
      setCloneError("The clone needs a different name from the source instance.");
      return;
    }
    setCloning(true);
    setCloneError(null);
    try {
      const clone = await api.cloneInstance(name, { name: candidate });
      toast.success(`Cloned ${name}`, `New instance: ${clone.name}`);
      setCloneOpen(false);
      navigate(`/fleet/${encodeURIComponent(clone.name)}`);
    } catch (error) {
      setCloneError(describeError(error));
    } finally {
      setCloning(false);
    }
  };

  const confirmDelete = async (): Promise<void> => {
    try {
      await api.deleteInstance(name, true);
      toast.success(`Deleted ${name}`, "The AVD files, snapshots and proxy assignment are removed.");
      navigate("/fleet");
    } catch (error) {
      toast.error(`Could not delete ${name}`, describeError(error));
    }
  };

  const ports = instance ? portEntries(instance) : [];
  const proxy = proxies.data?.find((candidate) => candidate.id === instance?.proxy_id) ?? null;

  const tabsItems: TabItem[] = instance
    ? [
        {
          id: "overview",
          label: "Overview",
          icon: "dashboard",
          content: (
            <InstanceOverviewPanel
              instance={instance}
              proxies={proxies.data}
              events={instanceEvents.events}
            />
          ),
        },
        {
          id: "runs",
          label: "Scripts & runs",
          icon: "scripts",
          content: <InstanceRunsPanel name={name} />,
        },
        {
          id: "snapshots",
          label: "Snapshots",
          icon: "hard-drive",
          content: <InstanceSnapshotsPanel name={name} state={instance.state} />,
        },
        {
          id: "events",
          label: "Logs & events",
          icon: "file",
          content: <InstanceEventsPanel name={name} state={instanceEvents} />,
        },
        {
          id: "proxy",
          label: "Proxy",
          icon: "proxies",
          content: (
            <InstanceProxyPanel
              instance={instance}
              proxies={proxies.data}
              proxiesLoading={proxies.loading}
              proxiesError={proxies.error}
              refreshProxies={async () => {
                proxies.reload();
              }}
              onAssignmentChanged={() => void instanceState.refresh()}
            />
          ),
        },
      ]
    : instanceState.loading
      ? LOADING_TABS
      : [];

  return (
    <div className="page">
      <PageHeader
        title={name || "Instance"}
        eyebrow="Instance"
        icon="instance"
        breadcrumbs={[{ label: "Fleet", to: "/fleet" }, { label: name }]}
        description={
          instance
            ? `${instance.image_name ?? instance.image_id ?? "Android"} · ${instance.device_id ?? "emulator"} · template ${
                instance.template_id ?? "none"
              }`
            : "Live screen, ports, proxy assignment, snapshots and configuration for this AVD."
        }
        meta={
          instance ? (
            <>
              <InstanceStatePill instance={instance} />
              {instance.image_name ? <Badge tone="neutral" icon="images">{instance.image_name}</Badge> : null}
              {instance.device_id ? <Badge tone="neutral" icon="instance">{instance.device_id}</Badge> : null}
              <ProxyBadge proxyId={instance.proxy_id} proxies={proxies.data} />
              {instance.adb_serial ? (
                <code style={{ fontSize: "var(--fs-xs)" }}>{instance.adb_serial}</code>
              ) : null}
              {isRunning(instance.state) ? (
                <span className="muted small">
                  up <LiveUptime instance={instance} />
                </span>
              ) : null}
            </>
          ) : undefined
        }
        actions={
          <>
            <ButtonLink to="/fleet" variant="ghost" icon="arrow-left">
              Fleet
            </ButtonLink>
            {instance ? (
              <>
                {canStart(instance.state) ? (
                  <Button
                    variant="primary"
                    icon="play"
                    loading={action === "start"}
                    disabled={action !== null}
                    onClick={() => void runLifecycle("start")}
                  >
                    Start
                  </Button>
                ) : null}
                {canStop(instance.state) ? (
                  <Button
                    variant="secondary"
                    icon="stop"
                    loading={action === "stop"}
                    disabled={action !== null}
                    onClick={() => void runLifecycle("stop")}
                  >
                    Stop
                  </Button>
                ) : null}
                {canRestart(instance.state) ? (
                  <Button
                    variant="secondary"
                    icon="restart"
                    loading={action === "restart"}
                    disabled={action !== null}
                    onClick={() => void runLifecycle("restart")}
                  >
                    Restart
                  </Button>
                ) : null}
                <Button
                  variant="secondary"
                  icon="refresh"
                  disabled={action !== null}
                  title="Restore a fresh first-run state using a snapshot, wipe, package reset or reboot"
                  onClick={() => setResetOpen(true)}
                >
                  Reset to OOBE
                </Button>
                <Button variant="ghost" icon="copy" disabled={action !== null} onClick={openClone}>
                  Clone
                </Button>
                <Button
                  variant="danger"
                  icon="trash"
                  disabled={action !== null}
                  onClick={() => setDeleteOpen(true)}
                >
                  Delete
                </Button>
              </>
            ) : null}
            <IconButton
              label="Refresh instance"
              icon="refresh"
              loading={instanceState.refreshing}
              onClick={() => void instanceState.refresh()}
            />
          </>
        }
      />

      {notFound ? (
        <EmptyState
          icon="instance"
          title="Instance not found"
          description={`No instance named "${name}" exists on this daemon. It may have been deleted, renamed, or deployed by a different daemon.`}
          action={
            <>
              <ButtonLink to="/fleet" variant="primary" icon="arrow-left">
                Back to fleet
              </ButtonLink>
              <Button variant="secondary" icon="refresh" onClick={() => void instanceState.refresh()}>
                Retry
              </Button>
            </>
          }
        />
      ) : null}

      {instanceState.error && !notFound ? (
        <InlineNotice
          tone="danger"
          title="Could not load the instance"
          action={
            <Button
              size="sm"
              variant="secondary"
              icon="refresh"
              onClick={() => void instanceState.refresh()}
            >
              Retry
            </Button>
          }
        >
          <p>{describeError(instanceState.error)}</p>
        </InlineNotice>
      ) : null}

      {instanceState.loading && !instance ? <ScreenCardSkeleton /> : null}

      {!instance && tabsItems.length > 0 ? (
        <Tabs ariaLabel="Instance sections" items={tabsItems} />
      ) : null}

      {instanceState.error && !notFound && instance ? (
        <InlineNotice tone="warning" title="Live refresh failed">
          <p>Showing the last known instance state. {describeError(instanceState.error)}</p>
        </InlineNotice>
      ) : null}

      {instance ? (
        <>
          {instance.error ? (
            <InlineNotice
              tone="danger"
              title="Instance error"
              action={
                canStart(instance.state) ? (
                  <Button
                    size="sm"
                    variant="secondary"
                    icon="play"
                    loading={action === "start"}
                    disabled={action !== null}
                    onClick={() => void runLifecycle("start")}
                  >
                    Try starting again
                  </Button>
                ) : undefined
              }
            >
              <p>{instance.error}</p>
            </InlineNotice>
          ) : null}

          <div className="grid-2" style={{ alignItems: "start" }}>
            <Card
              title="Live screen"
              description="Auto-refreshing screenshot straight from the emulator. Click it to send a tap."
              icon="monitor"
            >
              <ScreenPreview
                name={name}
                state={instance.state}
                screenAvailable={instance.screen_available}
                autoRefreshMs={3_000}
              />
            </Card>

            <Card title="Runtime" description="Live process, ports and boot state for this instance." icon="instance">
              <dl className="kv-list">
                <div className="kv-list__row">
                  <dt>State</dt>
                  <dd>
                    <InstanceStatePill instance={instance} />
                  </dd>
                </div>
                <div className="kv-list__row">
                  <dt>Boot completed</dt>
                  <dd>
                    {instance.boot_completed ? (
                      <Badge tone="success" icon="check-circle">Yes</Badge>
                    ) : (
                      <Badge tone="warning" icon="alert-triangle">No</Badge>
                    )}
                  </dd>
                </div>
                <div className="kv-list__row">
                  <dt>PID</dt>
                  <dd>{instance.pid ?? "—"}</dd>
                </div>
                <div className="kv-list__row">
                  <dt>ADB serial</dt>
                  <dd>
                    <code>{instance.adb_serial ?? "—"}</code>
                  </dd>
                </div>
                {ports.map(([label, value]) => (
                  <div className="kv-list__row" key={label}>
                    <dt>{label} port</dt>
                    <dd>
                      <code>{value}</code>
                    </dd>
                  </div>
                ))}
                {instance.grpc_endpoint ? (
                  <div className="kv-list__row">
                    <dt>gRPC endpoint</dt>
                    <dd>
                      <code>{instance.grpc_endpoint}</code>
                    </dd>
                  </div>
                ) : null}
                <div className="kv-list__row">
                  <dt>Uptime</dt>
                  <dd>
                    <LiveUptime instance={instance} />
                  </dd>
                </div>
                <div className="kv-list__row">
                  <dt>Started</dt>
                  <dd>{formatDateTime(instance.started_at)}</dd>
                </div>
                <div className="kv-list__row">
                  <dt>Created</dt>
                  <dd>{formatDateTime(instance.created_at)}</dd>
                </div>
                <div className="kv-list__row">
                  <dt>Proxy</dt>
                  <dd>{proxy ? proxy.name?.trim() || proxy.id : <ProxyBadge proxyId={instance.proxy_id} proxies={proxies.data} />}</dd>
                </div>
              </dl>
            </Card>
          </div>

          {tabsItems.length > 0 ? (
            <Tabs ariaLabel="Instance sections" items={tabsItems} />
          ) : null}

          <ResetOobeDialog
            open={resetOpen}
            instanceNames={[name]}
            onClose={() => setResetOpen(false)}
            onConfirm={confirmReset}
          />

          <Modal
            open={cloneOpen}
            onClose={() => setCloneOpen(false)}
            title={`Clone ${name}`}
            description="Create a stopped copy of this instance. Ports are allocated when the clone starts."
            size="sm"
            footer={
              <>
                <Button variant="ghost" disabled={cloning} onClick={() => setCloneOpen(false)}>
                  Cancel
                </Button>
                <Button variant="primary" icon="copy" loading={cloning} onClick={() => void submitClone()}>
                  Clone instance
                </Button>
              </>
            }
          >
            <Field
              label="New instance name"
              required
              hint="1–63 letters, digits, dots, dashes or underscores."
              error={cloneError ?? undefined}
            >
              <Input
                value={cloneName}
                autoFocus
                disabled={cloning}
                onChange={(event) => setCloneName(event.target.value)}
              />
            </Field>
          </Modal>

          <ConfirmDialog
            open={deleteOpen}
            title={`Delete ${name}?`}
            description="This stops the instance if needed and permanently deletes its AVD files, snapshots and proxy assignment."
            confirmLabel="Delete instance"
            onConfirm={confirmDelete}
            onCancel={() => setDeleteOpen(false)}
          >
            <p className="small" style={{ margin: 0 }}>
              Instance <code>{name}</code> cannot be recovered after deletion.
            </p>
          </ConfirmDialog>
        </>
      ) : null}
    </div>
  );
}
