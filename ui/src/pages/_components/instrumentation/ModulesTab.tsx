/**
 * Module library tab.
 *
 * One central library for Xposed APKs and Magisk/Zygisk/LSPosed ZIPs. Importing
 * only stores, hashes and classifies an archive — it never installs or activates
 * anything on a device.
 */
import { useMemo, useState } from "react";

import { instrumentationApi } from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  Field,
  IconButton,
  Input,
  InlineNotice,
  Select,
  Table,
  useToast,
  type Column,
} from "@/lib/components";
import { useAsync } from "@/lib/hooks";
import type { ModuleKind, ModuleRecord } from "@/lib/types";
import { formatBytes, formatDateTime, humanize } from "@/lib/utils";

import {
  MODULE_KINDS,
  ModuleKindBadge,
  apiErrorText,
  moduleKindLabel,
  styles,
} from "@/pages/_components/instrumentation/shared";

export interface ModulesTabProps {
  /**
   * Incremented by the page when a toolchain acquisition imports a module ZIP,
   * so a mounted library list reloads from the daemon.
   */
  revision?: number;
}

export function ModulesTab({ revision = 0 }: ModulesTabProps) {
  const toast = useToast();
  const [kindFilter, setKindFilter] = useState<ModuleKind | "">("");
  const [query, setQuery] = useState("");
  const [pathValue, setPathValue] = useState("");
  const [urlValue, setUrlValue] = useState("");
  const [fileBusy, setFileBusy] = useState(false);
  const [pathBusy, setPathBusy] = useState(false);
  const [urlBusy, setUrlBusy] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ModuleRecord | null>(null);

  const modules = useAsync(
    () => instrumentationApi.listModules(kindFilter === "" ? undefined : kindFilter),
    [kindFilter, revision],
  );

  const allModules = useMemo(() => modules.data?.modules ?? [], [modules.data]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return allModules;
    return allModules.filter((record) =>
      [record.name, record.package_name, record.id, record.description, record.version]
        .filter((value): value is string => typeof value === "string")
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
  }, [allModules, query]);

  const applyImported = (record: ModuleRecord): void => {
    setImportError(null);
    toast.success(
      "Module imported",
      `${record.name || record.id} · ${moduleKindLabel(record.kind)} · ${formatBytes(record.size_bytes)}`,
    );
    modules.reload();
  };

  const importFile = async (file: File): Promise<void> => {
    setFileBusy(true);
    setImportError(null);
    try {
      applyImported(await instrumentationApi.importModuleFile(file));
    } catch (caught) {
      const message = apiErrorText(caught);
      setImportError(message);
      toast.error("Import failed", message);
    } finally {
      setFileBusy(false);
    }
  };

  const importPath = async (): Promise<void> => {
    const value = pathValue.trim();
    if (!value) return;
    setPathBusy(true);
    setImportError(null);
    try {
      const record = await instrumentationApi.importModulePath(value);
      setPathValue("");
      applyImported(record);
    } catch (caught) {
      const message = apiErrorText(caught);
      setImportError(message);
      toast.error("Import failed", message);
    } finally {
      setPathBusy(false);
    }
  };

  const importUrl = async (): Promise<void> => {
    const value = urlValue.trim();
    if (!value) return;
    if (!/^https?:\/\//i.test(value)) {
      setImportError("Enter an http(s) URL — the daemon only downloads http(s) sources.");
      return;
    }
    if (/^http:\/\//i.test(value)) {
      setImportError(
        "Plain http:// imports are refused by the backend unless insecure URLs are enabled in its configuration. Use an https URL.",
      );
      return;
    }
    setUrlBusy(true);
    setImportError(null);
    try {
      const record = await instrumentationApi.importModuleUrl(value);
      setUrlValue("");
      applyImported(record);
    } catch (caught) {
      const message = apiErrorText(caught);
      setImportError(message);
      toast.error("Import failed", message);
    } finally {
      setUrlBusy(false);
    }
  };

  const removeModule = async (): Promise<void> => {
    if (!deleteTarget) return;
    try {
      await instrumentationApi.deleteModule(deleteTarget.id);
      toast.success("Module removed", deleteTarget.name || deleteTarget.id);
      setDeleteTarget(null);
      modules.reload();
    } catch (caught) {
      toast.error("Could not remove the module", apiErrorText(caught));
    }
  };

  const columns: Column<ModuleRecord>[] = [
    {
      key: "kind",
      header: "Kind",
      sortable: true,
      sortValue: (record) => record.kind,
      width: "110px",
      render: (record) => <ModuleKindBadge kind={record.kind} />,
    },
    {
      key: "name",
      header: "Module",
      sortable: true,
      sortValue: (record) => record.name || record.package_name || record.id,
      render: (record) => (
        <div style={styles.stackTight}>
          <span style={{ fontWeight: 600, color: "var(--text)" }}>
            {record.name || record.package_name || record.id}
          </span>
          <span className="muted small">
            <code style={styles.code}>{record.package_name || record.id}</code>
          </span>
          {record.description ? (
            <span className="muted small" style={{ maxWidth: "52ch" }}>
              {record.description}
            </span>
          ) : null}
        </div>
      ),
    },
    {
      key: "version",
      header: "Version",
      sortable: true,
      sortValue: (record) => record.version || String(record.version_code),
      width: "100px",
      render: (record) => (
        <div style={styles.stackTight}>
          <span>{record.version || "—"}</span>
          {record.version_code > 0 ? (
            <span className="muted small">code {record.version_code}</span>
          ) : null}
        </div>
      ),
    },
    {
      key: "size",
      header: "Size",
      sortable: true,
      sortValue: (record) => record.size_bytes,
      width: "90px",
      render: (record) => formatBytes(record.size_bytes),
    },
    {
      key: "sha256",
      header: "SHA-256",
      width: "130px",
      render: (record) => (
        <code style={styles.code} title={record.sha256}>
          {record.sha256 ? `${record.sha256.slice(0, 12)}…` : "—"}
        </code>
      ),
    },
    {
      key: "source",
      header: "Source",
      sortable: true,
      sortValue: (record) => record.source,
      width: "200px",
      render: (record) => (
        <div style={styles.stackTight}>
          <Badge tone={record.source === "url" ? "info" : "neutral"}>
            {record.source === "url" ? "URL" : humanize(record.source) || "Upload"}
          </Badge>
          {record.source_url ? (
            <a
              href={record.source_url}
              target="_blank"
              rel="noreferrer"
              style={{
                ...styles.code,
                color: "var(--accent)",
                display: "inline-block",
                maxWidth: "190px",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
              title={record.source_url}
            >
              {record.source_url}
            </a>
          ) : null}
        </div>
      ),
    },
    {
      key: "created",
      header: "Imported",
      sortable: true,
      sortValue: (record) => record.created_at,
      width: "120px",
      render: (record) => <span className="muted small">{formatDateTime(record.created_at)}</span>,
    },
    {
      key: "actions",
      header: "",
      align: "right",
      width: "56px",
      render: (record) => (
        <div style={styles.actions}>
          <IconButton
            label={`Remove ${record.name || record.id} from the library`}
            icon="trash"
            size="sm"
            variant="danger"
            onClick={() => setDeleteTarget(record)}
          />
        </div>
      ),
    },
  ];

  const libraryEmpty =
    modules.data !== null && allModules.length === 0 && kindFilter === "" && query.trim() === "";

  return (
    <div style={styles.stack}>
      <Card
        title="Import a module"
        icon="upload"
        description="Imports are stored, hashed and classified in the central library. Nothing is installed or activated on an AVD by an import."
        footer={
          <span>
            Accepted: <code>.apk</code> (Xposed module) or <code>.zip</code> (Magisk / Zygisk / LSPosed
            module), up to 256 MiB. Duplicate content is rejected with HTTP 409 because the SHA-256 already
            exists in the library.
          </span>
        }
      >
        <div style={styles.stack}>
          {importError ? (
            <InlineNotice tone="danger" title="Import failed">
              <p>{importError}</p>
            </InlineNotice>
          ) : null}

          <div style={styles.fieldGrid}>
            <Field
              label="Upload a file"
              hint={
                <>
                  Multipart upload sent as the <code>file</code> form field.
                </>
              }
            >
              <Input
                type="file"
                accept=".apk,.zip,application/vnd.android.package-archive,application/zip"
                disabled={fileBusy}
                aria-label="Module file"
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  event.target.value = "";
                  if (file) void importFile(file);
                }}
              />
            </Field>

            <div style={styles.stackTight}>
              <Field
                label="Import from a daemon path"
                hint={"Absolute path on the daemon host, e.g. C:\\modules\\lsposed.zip."}
              >
                <Input
                  value={pathValue}
                  placeholder={"C:\\modules\\lsposed.zip"}
                  disabled={pathBusy}
                  onChange={(event) => setPathValue(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      void importPath();
                    }
                  }}
                />
              </Field>
              <Button
                variant="secondary"
                icon="file"
                loading={pathBusy}
                disabled={!pathValue.trim()}
                title={!pathValue.trim() ? "Enter a path on the daemon host" : undefined}
                onClick={() => void importPath()}
              >
                Import path
              </Button>
            </div>

            <div style={styles.stackTight}>
              <Field
                label="Import from a URL"
                hint="https only; the daemon downloads the archive and records its provenance."
              >
                <Input
                  value={urlValue}
                  placeholder="https://example.com/lsposed.zip"
                  disabled={urlBusy}
                  onChange={(event) => setUrlValue(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      void importUrl();
                    }
                  }}
                />
              </Field>
              <Button
                variant="secondary"
                icon="link"
                loading={urlBusy}
                disabled={!urlValue.trim()}
                title={!urlValue.trim() ? "Enter an https URL" : undefined}
                onClick={() => void importUrl()}
              >
                Import URL
              </Button>
            </div>
          </div>

          {fileBusy ? (
            <p className="muted small" style={{ margin: 0 }}>
              Uploading and inspecting the archive…
            </p>
          ) : null}
        </div>
      </Card>

      <Card
        title="Module library"
        icon="folder"
        description="GET /instrumentation/modules. Kind is filtered server-side; search filters the returned rows."
        actions={
          <IconButton
            label="Reload the module library"
            icon="refresh"
            size="sm"
            loading={modules.loading}
            onClick={modules.reload}
          />
        }
      >
        <div style={styles.stack}>
          {modules.error ? (
            <InlineNotice
              tone="danger"
              title="Could not load the module library"
              action={
                <Button size="sm" variant="secondary" icon="refresh" onClick={modules.reload}>
                  Retry
                </Button>
              }
            >
              <p>{apiErrorText(modules.error)}</p>
            </InlineNotice>
          ) : null}

          <div style={styles.toolbar}>
            <div style={{ flex: "0 1 220px" }}>
              <Field label="Kind">
                <Select
                  value={kindFilter}
                  options={[
                    { value: "", label: "All kinds" },
                    ...MODULE_KINDS.map((kind) => ({ value: kind, label: moduleKindLabel(kind) })),
                  ]}
                  onChange={(event) => setKindFilter(event.target.value as ModuleKind | "")}
                />
              </Field>
            </div>
            <div style={{ flex: "1 1 260px", maxWidth: "380px" }}>
              <Field label="Search">
                <Input
                  type="search"
                  value={query}
                  placeholder="Name, package, hash or description…"
                  onChange={(event) => setQuery(event.target.value)}
                />
              </Field>
            </div>
            <div style={styles.rowBetween}>
              <span className="muted small">
                {filtered.length} of {allModules.length} modules
                {modules.loading ? " · refreshing…" : ""}
              </span>
            </div>
          </div>

          <Table
            dense
            columns={columns}
            rows={filtered}
            rowKey={(record) => record.id}
            loading={modules.loading && allModules.length === 0}
            skeletonRows={4}
            caption="Module library"
            empty={
              modules.error && allModules.length === 0 ? (
                <EmptyState
                  compact
                  icon="alert-circle"
                  title="Module library unavailable"
                  description="The library request failed; retry it above. No module data is being shown."
                />
              ) : libraryEmpty ? (
                <EmptyState
                  compact
                  icon="folder"
                  title="No modules in the library"
                  description="Import an Xposed APK or a Magisk/Zygisk/LSPosed ZIP with the controls above. Framework profiles only work once the module they need is in this library."
                />
              ) : (
                <EmptyState
                  compact
                  icon="search"
                  title="No modules match"
                  description={
                    query.trim()
                      ? `Nothing matches “${query}”.`
                      : "No module of this kind is in the library."
                  }
                  action={
                    <Button
                      size="sm"
                      variant="secondary"
                      icon="close"
                      onClick={() => {
                        setQuery("");
                        setKindFilter("");
                      }}
                    >
                      Clear filters
                    </Button>
                  }
                />
              )
            }
          />
        </div>
      </Card>

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Remove module from the library?"
        description={
          deleteTarget
            ? `${deleteTarget.name || deleteTarget.id} (${moduleKindLabel(deleteTarget.kind)})`
            : undefined
        }
        confirmLabel="Remove module"
        onCancel={() => setDeleteTarget(null)}
        onConfirm={removeModule}
      >
        <p>
          This deletes the stored artifact and its database row. Modules already installed on an AVD are not
          touched, and the action cannot be undone — re-import the file to restore it.
        </p>
        {deleteTarget?.sha256 ? (
          <p className="muted small" style={{ marginBottom: 0 }}>
            SHA-256: <code style={styles.code}>{deleteTarget.sha256}</code>
          </p>
        ) : null}
      </ConfirmDialog>
    </div>
  );
}
