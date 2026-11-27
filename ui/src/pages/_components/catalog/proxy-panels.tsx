/**
 * Per-instance proxy assignment panel for the Proxies page.
 * Page-private: it composes the table, assignment selects, round-robin action
 * and the guest-egress verification dialog.
 */
import { useMemo, useState } from "react";

import { api } from "@/lib/api";
import {
  Button,
  Card,
  EmptyState,
  IconButton,
  InlineNotice,
  Select,
  StatusPill,
  Table,
  useToast,
  type Column,
} from "@/lib/components";
import type { AvdInfo, ProxyAssignment, ProxyTestResult, ProxyView } from "@/lib/types";
import { formatDateTime, formatRelativeTime } from "@/lib/utils";

import { EntityCell, SubText, errorMessage, styles } from "./common";
import { VerifyGuestDialog } from "./proxy-dialogs";
import { proxyHostPort } from "./proxy-utils";

export function ProxyAssignmentPanel({
  instances,
  proxies,
  assignments,
  loading,
  error,
  onReload,
}: {
  instances: AvdInfo[];
  proxies: ProxyView[];
  assignments: ProxyAssignment[];
  loading: boolean;
  error: Error | null;
  onReload: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [verifyTarget, setVerifyTarget] = useState<AvdInfo | null>(null);

  const assignmentByInstance = useMemo(() => {
    const map = new Map<string, ProxyAssignment>();
    for (const assignment of assignments) map.set(assignment.avd_name, assignment);
    return map;
  }, [assignments]);

  const autoAssign = async (): Promise<void> => {
    setBusy("auto");
    setMutationError(null);
    try {
      const created = await api.autoAssignProxies({
        instances: instances.map((instance) => instance.name),
        strategy: "round_robin",
      });
      if (created.length > 0) {
        toast.success(
          `Round-robin assigned ${created.length} ${created.length === 1 ? "instance" : "instances"}`,
          "Assignments replace any previous proxy for the selected instances.",
        );
      } else {
        toast.warning("No instances were auto-assigned", "Check that enabled proxies and instances exist.");
      }
      onReload();
    } catch (caught) {
      setMutationError(errorMessage(caught));
    } finally {
      setBusy(null);
    }
  };

  const assign = async (instance: AvdInfo, proxyId: string): Promise<void> => {
    const key = `assign:${instance.name}`;
    setBusy(key);
    setMutationError(null);
    try {
      if (!proxyId) {
        await api.unassignProxy(instance.name);
        toast.info(`Proxy unassigned from ${instance.name}`);
      } else {
        await api.assignProxy({ avd_name: instance.name, proxy_id: proxyId });
        toast.success(`Proxy assigned to ${instance.name}`, "The loopback bridge is re-pointed at the new upstream.");
      }
      onReload();
    } catch (caught) {
      setMutationError(errorMessage(caught));
    } finally {
      setBusy(null);
    }
  };

  const onEgressVerified = (result: ProxyTestResult): void => {
    if (result.ok) {
      toast.success(
        "Guest egress verified",
        result.egress_ip ? `Traffic left the guest via ${result.egress_ip}.` : undefined,
      );
    } else {
      toast.warning("Guest egress check failed", result.error || "Inspect the verification details.");
    }
  };

  const columns: Column<AvdInfo>[] = [
    {
      key: "instance",
      header: "Instance",
      sortable: true,
      sortValue: (instance) => instance.name,
      render: (instance) => <EntityCell title={instance.name} subtitle={instance.device_id ?? undefined} />,
    },
    {
      key: "state",
      header: "State",
      render: (instance) => <StatusPill status={instance.state} />,
    },
    {
      key: "proxy",
      header: "Assigned proxy",
      render: (instance) => {
        const assigned = assignmentByInstance.get(instance.name);
        const current = assigned?.proxy_id ?? instance.proxy_id ?? "";
        return (
          <div style={styles.stackTight}>
            <Select
              value={current}
              aria-label={`Proxy for ${instance.name}`}
              disabled={busy !== null}
              options={[
                { value: "", label: "Unassigned (direct traffic)" },
                ...proxies.map((proxy) => ({
                  value: proxy.id,
                  label: `${proxy.name ?? proxy.id} · ${proxyHostPort(proxy)}${
                    proxy.enabled === false ? " (disabled)" : ""
                  }`,
                })),
              ]}
              onChange={(event) => void assign(instance, event.target.value)}
            />
            {assigned?.assigned_at ? (
              <SubText title={formatDateTime(assigned.assigned_at)}>
                Assigned {formatRelativeTime(assigned.assigned_at)}
              </SubText>
            ) : (
              <SubText>No assignment record</SubText>
            )}
          </div>
        );
      },
    },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (instance) => {
        const assigned = assignmentByInstance.get(instance.name);
        const current = assigned?.proxy_id ?? instance.proxy_id ?? "";
        return (
          <div style={styles.actions}>
            <IconButton
              label={`Verify egress for ${instance.name}`}
              icon="wifi"
              size="sm"
              disabled={!current || busy !== null}
              onClick={() => setVerifyTarget(instance)}
            />
            <IconButton
              label={`Unassign proxy from ${instance.name}`}
              icon="minus"
              size="sm"
              disabled={!current || busy !== null}
              onClick={() => void assign(instance, "")}
            />
          </div>
        );
      },
    },
  ];

  const defaultProxyId = verifyTarget
    ? assignmentByInstance.get(verifyTarget.name)?.proxy_id ?? verifyTarget.proxy_id ?? ""
    : "";

  return (
    <>
      <Card
        title="Per-instance proxy assignments"
        description="Each instance is pinned to exactly one proxy through a loopback bridge. Re-assigning replaces the previous route."
        icon="link"
        actions={
          <div style={styles.rowTight}>
            <IconButton label="Reload assignments" icon="refresh" size="sm" onClick={onReload} />
            <Button
              size="sm"
              variant="secondary"
              icon="zap"
              loading={busy === "auto"}
              disabled={busy !== null || instances.length === 0 || proxies.length === 0}
              onClick={() => void autoAssign()}
            >
              Auto-assign round robin
            </Button>
          </div>
        }
      >
        <div style={styles.stack}>
          {mutationError ? (
            <InlineNotice tone="danger" title="Assignment update failed">
              <p>{mutationError}</p>
            </InlineNotice>
          ) : null}

          {error ? (
            <InlineNotice
              tone="danger"
              title="Could not load assignments"
              action={
                <Button size="sm" variant="secondary" icon="refresh" onClick={onReload}>
                  Retry
                </Button>
              }
            >
              <p>{errorMessage(error)}</p>
            </InlineNotice>
          ) : null}

          {!error && proxies.length === 0 ? (
            <EmptyState
              compact
              icon="shield"
              title="No proxies to assign"
              description="Add or bulk-import a proxy first, then assign one to each instance."
            />
          ) : !error && instances.length === 0 && !loading ? (
            <EmptyState
              compact
              icon="fleet"
              title="No instances to assign"
              description="Deploy AVDs from a template and come back to pin each one to an isolated proxy."
            />
          ) : (
            <Table
              columns={columns}
              rows={instances}
              rowKey={(instance) => instance.name}
              loading={loading}
              skeletonRows={4}
              caption="Per-instance proxy assignments"
              empty={
                <EmptyState
                  compact
                  icon="link"
                  title="No instances"
                  description="Deployment creates the instances that can be assigned a proxy."
                />
              }
            />
          )}

          <p style={styles.footerNote}>
            {proxies.length} proxies in the pool · {assignments.length} active assignments ·{" "}
            {instances.length} instances. Round robin walks the enabled proxies in order and replaces
            existing assignments.
          </p>
        </div>
      </Card>

      <VerifyGuestDialog
        open={verifyTarget !== null}
        instance={verifyTarget}
        proxies={proxies}
        defaultProxyId={defaultProxyId}
        onClose={() => setVerifyTarget(null)}
        onVerified={onEgressVerified}
      />
    </>
  );
}
