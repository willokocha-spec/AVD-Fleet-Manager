import { useMemo, useState } from "react";

import { api } from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  IconButton,
  InlineNotice,
  PageHeader,
  ProgressBar,
  SearchBox,
  StatusPill,
  Table,
  useToast,
  type Column,
} from "@/lib/components";
import { useAsync } from "@/lib/hooks";
import type { PageMeta } from "@/lib/page-meta";
import type { ProxyTestResult, ProxyView } from "@/lib/types";
import { formatDateTime, formatRelativeTime } from "@/lib/utils";

import {
  EntityCell,
  Mono,
  SubText,
  TagList,
  errorMessage,
  styles,
} from "@/pages/_components/catalog/common";
import {
  AddProxyDialog,
  EditProxyDialog,
  ImportProxiesDialog,
} from "@/pages/_components/catalog/proxy-dialogs";
import { ProxyAssignmentPanel } from "@/pages/_components/catalog/proxy-panels";
import {
  proxyEndpoint,
  proxyHostPort,
  redactProxySecrets,
} from "@/pages/_components/catalog/proxy-utils";

export const meta: PageMeta = {
  path: "/proxies",
  title: "Proxies",
  icon: "proxies",
  nav: 50,
  description: "Authenticated HTTP/HTTPS and SOCKS5 proxies",
};

function healthLabel(status: string | undefined): string {
  if (status === "ok") return "Healthy";
  if (status === "failed") return "Failed";
  return "Unknown";
}

function healthTone(status: string | undefined): "success" | "danger" | "neutral" {
  if (status === "ok") return "success";
  if (status === "failed") return "danger";
  return "neutral";
}

export default function ProxiesPage() {
  const toast = useToast();
  const proxies = useAsync(() => api.listProxies(true), []);
  const assignments = useAsync(() => api.listAssignments(), []);
  const instances = useAsync(() => api.listInstances(), []);
  const bridges = useAsync(() => api.listBridges(), []);

  const [query, setQuery] = useState("");
  const [addOpen, setAddOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<ProxyView | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ProxyView | null>(null);
  const [checkingId, setCheckingId] = useState<string | null>(null);
  const [checkingAll, setCheckingAll] = useState(false);
  const [lastCheck, setLastCheck] = useState<ProxyTestResult[] | null>(null);

  const allProxies = useMemo(() => proxies.data ?? [], [proxies.data]);
  const allAssignments = useMemo(() => assignments.data ?? [], [assignments.data]);
  const allInstances = useMemo(() => instances.data ?? [], [instances.data]);
  const allBridges = useMemo(() => bridges.data ?? [], [bridges.data]);

  const enabledCount = allProxies.filter((proxy) => proxy.enabled !== false).length;
  const healthyCount = allProxies.filter((proxy) => proxy.health?.status === "ok").length;
  const failedCount = allProxies.filter((proxy) => proxy.health?.status === "failed").length;

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return allProxies;
    return allProxies.filter((proxy) => {
      const haystack = [
        proxy.name,
        proxy.id,
        proxy.host,
        proxy.username,
        proxy.scheme,
        ...(proxy.tags ?? []),
      ]
        .filter((value): value is string => typeof value === "string")
        .join(" ")
        .toLowerCase();
      return haystack.includes(needle);
    });
  }, [allProxies, query]);

  const reloadAll = (): void => {
    proxies.reload();
    assignments.reload();
    instances.reload();
    bridges.reload();
  };

  const applyCheck = (result: ProxyTestResult): void => {
    if (result.ok) {
      toast.success(
        "Proxy reachable",
        `${result.latency_ms ?? "—"} ms${result.egress_ip ? ` · egress ${result.egress_ip}` : ""}`,
      );
    } else {
      toast.warning("Proxy check failed", redactProxySecrets(result.error || "No response from the upstream."));
    }
  };

  const checkOne = async (proxy: ProxyView): Promise<void> => {
    setCheckingId(proxy.id);
    try {
      const result = await api.checkProxy(proxy.id);
      applyCheck(result);
      proxies.reload();
    } catch (caught) {
      toast.error(`Could not check ${proxy.name ?? proxy.id}`, errorMessage(caught));
    } finally {
      setCheckingId(null);
    }
  };

  const checkAll = async (): Promise<void> => {
    setCheckingAll(true);
    try {
      const results = await api.checkAllProxies();
      setLastCheck(results);
      const ok = results.filter((result) => result.ok).length;
      const failed = results.length - ok;
      if (failed === 0) {
        toast.success(
          `Checked ${results.length} ${results.length === 1 ? "proxy" : "proxies"}`,
          `${ok} healthy.`,
        );
      } else {
        toast.warning(
          `Checked ${results.length} ${results.length === 1 ? "proxy" : "proxies"}`,
          `${ok} healthy · ${failed} failed. Inspect the health column for details.`,
        );
      }
      proxies.reload();
    } catch (caught) {
      toast.error("Could not check the proxy pool", errorMessage(caught));
    } finally {
      setCheckingAll(false);
    }
  };

  const handleCreated = (proxy: ProxyView): void => {
    toast.success("Proxy added", `${proxyEndpoint(proxy)} is ready to assign.`);
    proxies.reload();
  };

  const handleImported = (result: { added: ProxyView[]; skipped?: number; errors?: string[] }): void => {
    const errors = result.errors?.length ?? 0;
    if (result.added.length > 0) {
      toast.success(
        `Imported ${result.added.length} ${result.added.length === 1 ? "proxy" : "proxies"}`,
        `${result.skipped ?? 0} skipped · ${errors} errors.`,
      );
    } else {
      toast.warning("No proxies imported", `${errors} invalid ${errors === 1 ? "line" : "lines"}.`);
    }
    proxies.reload();
  };

  const handleSaved = (proxy: ProxyView): void => {
    toast.success("Proxy updated", proxy.name ?? proxy.id);
    proxies.reload();
  };

  const assignedToDelete = deleteTarget
    ? allAssignments.filter((assignment) => assignment.proxy_id === deleteTarget.id).length
    : 0;

  const handleDelete = async (): Promise<void> => {
    if (!deleteTarget) return;
    try {
      await api.deleteProxy(deleteTarget.id);
      toast.success("Proxy deleted", deleteTarget.name ?? deleteTarget.id);
      setDeleteTarget(null);
      reloadAll();
    } catch (caught) {
      toast.error("Could not delete the proxy", errorMessage(caught));
    }
  };

  const columns: Column<ProxyView>[] = [
    {
      key: "name",
      header: "Name",
      sortable: true,
      sortValue: (proxy) => proxy.name ?? proxy.id,
      render: (proxy) => <EntityCell title={proxy.name ?? "Unnamed proxy"} subtitle={<Mono>{proxy.id}</Mono>} />,
    },
    {
      key: "scheme",
      header: "Scheme",
      sortable: true,
      sortValue: (proxy) => proxy.scheme ?? "http",
      render: (proxy) => <Badge tone="info">{(proxy.scheme ?? "http").toUpperCase()}</Badge>,
    },
    {
      key: "endpoint",
      header: "Host : port",
      sortable: true,
      sortValue: (proxy) => proxyHostPort(proxy),
      render: (proxy) => <Mono title={proxyEndpoint(proxy)}>{proxyHostPort(proxy)}</Mono>,
    },
    {
      key: "username",
      header: "Username",
      sortable: true,
      sortValue: (proxy) => proxy.username ?? "",
      render: (proxy) => (
        <div style={styles.stackTight}>
          {proxy.username ? <Mono>{proxy.username}</Mono> : <span className="muted">—</span>}
          <Badge tone={proxy.has_credentials ? "success" : "neutral"} icon={proxy.has_credentials ? "key" : undefined}>
            {proxy.has_credentials ? "Password in vault" : "No password"}
          </Badge>
        </div>
      ),
    },
    {
      key: "tags",
      header: "Tags",
      sortValue: (proxy) => (proxy.tags ?? []).join(","),
      render: (proxy) => <TagList tags={proxy.tags} />,
    },
    {
      key: "enabled",
      header: "Enabled",
      sortable: true,
      sortValue: (proxy) => (proxy.enabled === false ? 0 : 1),
      render: (proxy) => (
        <Badge tone={proxy.enabled === false ? "neutral" : "success"} dot>
          {proxy.enabled === false ? "Disabled" : "Enabled"}
        </Badge>
      ),
    },
    {
      key: "health",
      header: "Health",
      sortable: true,
      sortValue: (proxy) => (proxy.health?.status === "ok" ? 0 : proxy.health?.status === "unknown" ? 1 : 2),
      render: (proxy) => {
        const health = proxy.health;
        const status = health?.status ?? "unknown";
        const detail = [
          health?.message ? `Message: ${health.message}` : "",
          health?.egress_ip ? `Egress IP: ${health.egress_ip}` : "",
          health?.checked_at ? `Checked: ${formatDateTime(health.checked_at)}` : "",
        ]
          .filter(Boolean)
          .join("\n");
        return (
          <div style={styles.stackTight}>
            <div style={styles.rowTight}>
              <StatusPill status={status} tone={healthTone(status)} label={healthLabel(status)} />
              <SubText>{health?.latency_ms !== null && health?.latency_ms !== undefined ? `${health.latency_ms} ms` : "—"}</SubText>
            </div>
            <SubText title={detail || undefined}>
              {health?.checked_at ? `Checked ${formatRelativeTime(health.checked_at)}` : "Never checked"}
            </SubText>
          </div>
        );
      },
    },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (proxy) => (
        <div style={styles.actions}>
          <IconButton
            label={`Check ${proxy.name ?? proxy.id}`}
            icon="refresh"
            size="sm"
            loading={checkingId === proxy.id}
            disabled={checkingAll}
            onClick={() => void checkOne(proxy)}
          />
          <IconButton
            label={`Edit ${proxy.name ?? proxy.id}`}
            icon="edit"
            size="sm"
            onClick={() => setEditTarget(proxy)}
          />
          <IconButton
            label={`Delete ${proxy.name ?? proxy.id}`}
            icon="trash"
            size="sm"
            variant="danger"
            onClick={() => setDeleteTarget(proxy)}
          />
        </div>
      ),
    },
  ];

  const emptyState = allProxies.length === 0 && !proxies.loading ? (
    <EmptyState
      icon="shield"
      title="No proxies configured"
      description="Add one proxy URL or bulk-import a list. Credentials are stored write-only in the encrypted vault and never returned by the API."
      action={
        <div style={styles.rowTight}>
          <Button variant="primary" icon="plus" onClick={() => setAddOpen(true)}>
            Add proxy
          </Button>
          <Button variant="secondary" icon="upload" onClick={() => setImportOpen(true)}>
            Bulk import
          </Button>
        </div>
      }
    />
  ) : (
    <EmptyState
      compact
      icon="search"
      title="No proxies match your search"
      description={`Nothing matches “${query}”. Try a host, username or tag.`}
      action={
        <Button size="sm" variant="secondary" icon="close" onClick={() => setQuery("")}>
          Clear search
        </Button>
      }
    />
  );

  return (
    <div className="page">
      <PageHeader
        title="Proxies"
        eyebrow="Network isolation"
        description="Import authenticated HTTP/HTTPS and SOCKS5 proxies, health-check them and assign exactly one to each instance."
        meta={
          <div style={styles.rowTight}>
            <Badge tone="info" icon="proxies">
              {allProxies.length} proxies
            </Badge>
            <Badge tone="success" icon="check-circle">
              {enabledCount} enabled
            </Badge>
            {healthyCount > 0 ? (
              <StatusPill status="ok" label={`${healthyCount} healthy`} />
            ) : null}
            {failedCount > 0 ? (
              <StatusPill status="failed" label={`${failedCount} failed`} />
            ) : null}
            {lastCheck ? (
              <SubText>
                Last pool check: {lastCheck.filter((result) => result.ok).length}/
                {lastCheck.length} healthy
              </SubText>
            ) : null}
          </div>
        }
        actions={
          <>
            <Button variant="secondary" icon="refresh" loading={checkingAll} onClick={() => void checkAll()}>
              Check all
            </Button>
            <Button variant="secondary" icon="upload" onClick={() => setImportOpen(true)}>
              Bulk import
            </Button>
            <Button variant="primary" icon="plus" onClick={() => setAddOpen(true)}>
              Add proxy
            </Button>
          </>
        }
      />

      <InlineNotice tone="info" title="Bridge & egress isolation">
        <p>
          Each instance gets a per-instance loopback bridge (<Mono>127.0.0.1</Mono>) that forwards TCP
          CONNECT traffic to exactly one upstream proxy. Bridges are bound to loopback only and mirror
          the upstream scheme; guest traffic never leaks past the assigned proxy.
        </p>
        <p>
          <strong>Limitations:</strong> only HTTP, HTTPS and SOCKS5 upstreams are supported, and the
          bridge carries TCP only — UDP/QUIC traffic (used by some video, voice and DNS paths) does not
          traverse the proxy. Host-side checks are a smoke test; use{" "}
          <strong>Verify egress</strong> from inside the guest for proof.
        </p>
        {allBridges.length > 0 ? (
          <details>
            <summary className="small" style={{ cursor: "pointer" }}>
              Active loopback bridges ({allBridges.length})
            </summary>
            <ul style={{ margin: "var(--space-2) 0 0", paddingLeft: "var(--space-4)" }} className="small">
              {allBridges.map((bridge) => (
                <li key={`${bridge.instance}-${bridge.listen_port}`}>
                  <Mono>{bridge.instance}</Mono> → <Mono>{`${bridge.listen_host ?? "127.0.0.1"}:${bridge.listen_port}`}</Mono>
                  {bridge.mode ? ` (${bridge.mode})` : ""}
                  {bridge.upstream ? (
                    <>
                      {" "}
                      → <Mono>{redactProxySecrets(bridge.upstream)}</Mono>
                    </>
                  ) : null}
                </li>
              ))}
            </ul>
          </details>
        ) : null}
      </InlineNotice>

      {proxies.error ? (
        <InlineNotice
          tone="danger"
          title="Could not load the proxy pool"
          action={
            <Button size="sm" variant="secondary" icon="refresh" onClick={proxies.reload}>
              Retry
            </Button>
          }
        >
          <p>{errorMessage(proxies.error)}</p>
        </InlineNotice>
      ) : null}

      <Card
        title="Proxy pool"
        description="Passwords are write-only: the API returns only whether credentials are stored. Use the edit dialog to change labels, tags or enablement."
        icon="proxies"
      >
        <div style={styles.stack}>
          {checkingAll ? (
            <InlineNotice tone="info" title="Checking the proxy pool">
              <p>Sending a request through each proxy and recording latency and egress IP.</p>
              <ProgressBar indeterminate label="Checking proxies" />
            </InlineNotice>
          ) : null}

          <div style={styles.toolbar}>
            <div style={{ flex: "1 1 260px", maxWidth: "420px" }}>
              <SearchBox
                value={query}
                onChange={setQuery}
                placeholder="Search name, host, username or tag…"
                label="Search proxies"
              />
            </div>
            <div style={styles.toolbarSpacer} />
            <SubText>
              {filtered.length} of {allProxies.length} proxies
              {proxies.loading ? " · refreshing…" : ""}
            </SubText>
          </div>

          <Table
            columns={columns}
            rows={filtered}
            rowKey={(proxy) => proxy.id}
            loading={proxies.loading && allProxies.length === 0}
            skeletonRows={4}
            caption="Proxy pool"
            empty={emptyState}
          />
        </div>
      </Card>

      <ProxyAssignmentPanel
        instances={allInstances}
        proxies={allProxies}
        assignments={allAssignments}
        loading={instances.loading || assignments.loading || proxies.loading}
        error={instances.error ?? assignments.error ?? proxies.error}
        onReload={() => {
          assignments.reload();
          instances.reload();
        }}
      />

      <AddProxyDialog open={addOpen} onClose={() => setAddOpen(false)} onCreated={handleCreated} />
      <ImportProxiesDialog
        open={importOpen}
        onClose={() => setImportOpen(false)}
        onImported={handleImported}
      />
      <EditProxyDialog
        open={editTarget !== null}
        proxy={editTarget}
        onClose={() => setEditTarget(null)}
        onSaved={handleSaved}
      />
      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete proxy?"
        description={deleteTarget ? `${deleteTarget.name ?? deleteTarget.id} (${proxyEndpoint(deleteTarget)})` : undefined}
        confirmLabel="Delete proxy"
        onCancel={() => setDeleteTarget(null)}
        onConfirm={handleDelete}
      >
        {assignedToDelete > 0 ? (
          <InlineNotice tone="warning" title="This proxy is assigned">
            <p>
              {assignedToDelete} {assignedToDelete === 1 ? "instance is" : "instances are"} using this
              proxy. Deleting it removes the assignment and those instances lose their egress route — their
              traffic may fall back to direct internet access. Re-assign them first.
            </p>
          </InlineNotice>
        ) : (
          <p>
            This removes the proxy and its stored credentials from the vault. No instance is currently
            assigned to it.
          </p>
        )}
      </ConfirmDialog>
    </div>
  );
}
