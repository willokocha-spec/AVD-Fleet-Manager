import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "@/lib/api";
import {
  Badge,
  Button,
  ButtonLink,
  Card,
  CodeBlock,
  ConfirmDialog,
  EmptyState,
  IconButton,
  InlineNotice,
  Modal,
  PageHeader,
  SearchBox,
  Select,
  Skeleton,
  useToast,
} from "@/lib/components";
import { useAsync } from "@/lib/hooks";
import type { PageMeta } from "@/lib/page-meta";
import type { ScriptDefinition, ScriptType } from "@/lib/types";
import { copyText, formatRelativeTime, humanize } from "@/lib/utils";

import { RunScriptDialog } from "./_components/automation/RunScriptDialog";
import { ScriptEditorModal } from "./_components/automation/ScriptEditorModal";
import {
  SCRIPT_TYPES,
  describeError,
  scriptTargetsLabel,
  scriptTypeMeta,
} from "./_components/automation/shared";

export const meta: PageMeta = {
  path: "/scripts",
  title: "Scripts",
  icon: "scripts",
  nav: 60,
  description: "adb, host Python and Appium script library",
};

type OriginFilter = "all" | "builtin" | "custom";

interface EditorState {
  /** Definition being edited; `null` creates a new script. */
  script: ScriptDefinition | null;
  /** Optional template used to pre-fill a new script (builtin duplication). */
  template?: ScriptDefinition | null;
}

/**
 * U5 — script library.
 *
 * Library CRUD, builtin protection, a monospace content editor and the
 * fan-out run launcher. Starting a run navigates to `/runs?run=<id>` where the
 * Runs page takes over (live output, cancellation, exports).
 */
export default function ScriptsPage() {
  const navigate = useNavigate();
  const toast = useToast();

  const scripts = useAsync(() => api.listScripts(), []);
  const instances = useAsync(() => api.listInstances(), []);

  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState<"all" | ScriptType>("all");
  const [originFilter, setOriginFilter] = useState<OriginFilter>("all");

  const [editor, setEditor] = useState<EditorState | null>(null);
  const [runTarget, setRunTarget] = useState<ScriptDefinition | null>(null);
  const [viewTarget, setViewTarget] = useState<ScriptDefinition | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ScriptDefinition | null>(null);
  const [protectedTarget, setProtectedTarget] = useState<ScriptDefinition | null>(null);

  const data = useMemo(() => scripts.data ?? [], [scripts.data]);
  const builtinCount = data.filter((script) => script.builtin).length;
  const customCount = data.length - builtinCount;
  const existingIds = useMemo(() => data.map((script) => script.id), [data]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return data.filter((script) => {
      if (typeFilter !== "all" && script.type !== typeFilter) return false;
      if (originFilter === "builtin" && !script.builtin) return false;
      if (originFilter === "custom" && script.builtin) return false;
      if (!needle) return true;
      const haystack = [script.name, script.id, script.description ?? "", ...(script.tags ?? [])]
        .join(" ")
        .toLowerCase();
      return haystack.includes(needle);
    });
  }, [data, originFilter, query, typeFilter]);

  const handleSaved = (saved: ScriptDefinition): void => {
    const created = editor?.script === null;
    setEditor(null);
    toast.success(created ? "Script created" : "Script updated", saved.name);
    scripts.reload();
  };

  const handleRunStarted = (run: { id: string; script_name?: string }): void => {
    setRunTarget(null);
    toast.success("Run started", run.script_name ?? `Run ${run.id}`);
    navigate(`/runs?run=${encodeURIComponent(run.id)}`);
  };

  const handleDelete = async (): Promise<void> => {
    if (!deleteTarget) return;
    try {
      await api.deleteScript(deleteTarget.id);
      toast.success("Script deleted", deleteTarget.name);
      scripts.setData((current) => (current ?? []).filter((script) => script.id !== deleteTarget.id));
      setDeleteTarget(null);
    } catch (error) {
      toast.error("Could not delete the script", describeError(error));
    }
  };

  const copyId = async (script: ScriptDefinition): Promise<void> => {
    const copied = await copyText(script.id);
    if (copied) toast.success("Script ID copied", script.id);
    else toast.error("Could not copy the script ID");
  };

  const requestDelete = (script: ScriptDefinition): void => {
    if (script.builtin) setProtectedTarget(script);
    else setDeleteTarget(script);
  };

  const openEditor = (script: ScriptDefinition): void => {
    if (script.builtin) setViewTarget(script);
    else setEditor({ script });
  };

  const duplicate = (template: ScriptDefinition): void => {
    setViewTarget(null);
    setProtectedTarget(null);
    setEditor({ script: null, template });
  };

  const clearFilters = (): void => {
    setQuery("");
    setTypeFilter("all");
    setOriginFilter("all");
  };

  return (
    <div className="page">
      <PageHeader
        title="Scripts"
        eyebrow="Automation"
        description="A reusable script library plus the built-in entries shipped with AVD Fleet. Pick a script, choose running instances, then watch the fan-out live on the Runs page."
        actions={
          <>
            <ButtonLink to="/runs" variant="secondary" icon="runs">
              View runs
            </ButtonLink>
            <IconButton
              label="Refresh script library"
              icon="refresh"
              onClick={scripts.reload}
              loading={scripts.loading}
            />
            <Button variant="primary" icon="plus" onClick={() => setEditor({ script: null })}>
              New script
            </Button>
          </>
        }
        meta={
          <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
            <Badge tone="neutral" icon="scripts">
              {data.length} script{data.length === 1 ? "" : "s"}
            </Badge>
            <Badge tone="accent" icon="shield">
              {builtinCount} built-in
            </Badge>
            <Badge tone="info">{customCount} custom</Badge>
          </div>
        }
      />

      {scripts.error ? (
        <InlineNotice
          tone="danger"
          title="Could not load the script library"
          action={
            <Button size="sm" variant="secondary" icon="refresh" onClick={scripts.reload}>
              Retry
            </Button>
          }
        >
          <p>{describeError(scripts.error)}</p>
        </InlineNotice>
      ) : null}

      <Card
        title="Script library"
        description="Built-in entries are re-seeded by the daemon at startup (view-only). Custom scripts can be edited, duplicated and deleted."
        icon="scripts"
      >
        <div
          style={{
            display: "flex",
            gap: "var(--space-3)",
            alignItems: "center",
            flexWrap: "wrap",
            marginBottom: "var(--space-5)",
          }}
        >
          <SearchBox
            value={query}
            onChange={setQuery}
            placeholder="Search name, id, tag or description…"
            label="Search scripts"
            className=""
          />
          <div style={{ minWidth: 170 }}>
            <Select
              aria-label="Filter by script type"
              value={typeFilter}
              onChange={(event) => setTypeFilter(event.target.value as "all" | ScriptType)}
              options={[
                { value: "all", label: "All types" },
                ...SCRIPT_TYPES.map((entry) => ({ value: entry.value, label: entry.label })),
              ]}
            />
          </div>
          <div style={{ minWidth: 150 }}>
            <Select
              aria-label="Filter by origin"
              value={originFilter}
              onChange={(event) => setOriginFilter(event.target.value as OriginFilter)}
              options={[
                { value: "all", label: "All origins" },
                { value: "builtin", label: "Built-in only" },
                { value: "custom", label: "Custom only" },
              ]}
            />
          </div>
          <span className="muted small" style={{ marginLeft: "auto" }}>
            {visible.length} of {data.length} shown
          </span>
        </div>

        {scripts.loading && !scripts.data ? (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))",
              gap: "var(--space-4)",
            }}
          >
            {Array.from({ length: 6 }, (_, index) => (
              <Card key={`script-skeleton-${index}`}>
                <div className="skeleton-card">
                  <Skeleton width="62%" height={20} />
                  <Skeleton lines={2} />
                  <Skeleton width="42%" height={14} />
                </div>
              </Card>
            ))}
          </div>
        ) : null}

        {!scripts.loading && visible.length === 0 ? (
          data.length === 0 ? (
            <EmptyState
              icon="scripts"
              title="No scripts in the library"
              description="Create an adb-shell, host-Python or Appium script, or use a built-in entry, then fan it out across running instances with a bounded concurrency."
              action={
                <Button variant="primary" icon="plus" onClick={() => setEditor({ script: null })}>
                  New script
                </Button>
              }
            />
          ) : (
            <EmptyState
              compact
              icon="search"
              title="No scripts match these filters"
              description="Try a different search term, type or origin."
              action={
                <Button size="sm" variant="secondary" icon="refresh" onClick={clearFilters}>
                  Clear filters
                </Button>
              }
            />
          )
        ) : null}

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))",
            gap: "var(--space-4)",
            alignItems: "stretch",
          }}
        >
          {visible.map((script) => {
            const typeMeta = scriptTypeMeta(script.type);
            return (
              <Card
                key={script.id}
                title={script.name}
                description={
                  <span style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
                    <Badge tone="info">{typeMeta.label}</Badge>
                    {script.builtin ? (
                      <Badge tone="accent" icon="shield" title="Built-in scripts are read-only and re-seeded at startup">
                        Built-in
                      </Badge>
                    ) : (
                      <Badge tone="neutral">Custom</Badge>
                    )}
                    <Badge tone="neutral" icon="clock">
                      {script.timeout_s ?? 120}s timeout
                    </Badge>
                  </span>
                }
                footer={
                  <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center", flexWrap: "wrap" }}>
                    <Button
                      size="sm"
                      variant="primary"
                      icon="play"
                      aria-label={`Run ${script.name}`}
                      onClick={() => setRunTarget(script)}
                    >
                      Run
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      icon={script.builtin ? "eye" : "edit"}
                      aria-label={`${script.builtin ? "View" : "Edit"} ${script.name}`}
                      onClick={() => openEditor(script)}
                    >
                      {script.builtin ? "View" : "Edit"}
                    </Button>
                    <IconButton
                      label={`Copy ID for ${script.name}`}
                      icon="copy"
                      size="sm"
                      onClick={() => void copyId(script)}
                    />
                    <IconButton
                      label={`Delete ${script.name}`}
                      icon={script.builtin ? "shield" : "trash"}
                      size="sm"
                      onClick={() => requestDelete(script)}
                      title={
                        script.builtin
                          ? "Built-in scripts are protected — click for details"
                          : "Delete this script"
                      }
                    />
                  </div>
                }
              >
                <p className="small" style={{ color: "var(--text-muted)", minHeight: "2.6em" }}>
                  {script.description?.trim() || "No description."}
                </p>
                <dl className="kv-list" style={{ marginTop: "var(--space-2)" }}>
                  <div className="kv-list__row">
                    <dt>Targets</dt>
                    <dd>{scriptTargetsLabel(script.targets)}</dd>
                  </div>
                  <div className="kv-list__row">
                    <dt>Updated</dt>
                    <dd title={script.updated_at ?? undefined}>{formatRelativeTime(script.updated_at)}</dd>
                  </div>
                  <div className="kv-list__row">
                    <dt>ID</dt>
                    <dd style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-2xs)" }}>{script.id}</dd>
                  </div>
                </dl>
                {(script.tags ?? []).length > 0 ? (
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: "var(--space-3)" }}>
                    {(script.tags ?? []).map((tag) => (
                      <Badge key={tag} tone="neutral">
                        #{tag}
                      </Badge>
                    ))}
                  </div>
                ) : null}
                {script.env && Object.keys(script.env).length > 0 ? (
                  <p className="muted small" style={{ marginTop: "var(--space-3)" }}>
                    {Object.keys(script.env).length} environment variable
                    {Object.keys(script.env).length === 1 ? "" : "s"}:{" "}
                    <span style={{ fontFamily: "var(--font-mono)" }}>
                      {Object.keys(script.env).join(", ")}
                    </span>
                  </p>
                ) : null}
              </Card>
            );
          })}
        </div>
      </Card>

      <InlineNotice tone="info" title="Supported script types">
        <p>
          adb shell commands run per instance through adb; host-Python and Appium scripts receive
          instance metadata as <code>AVDFLEET_*</code> environment variables. Runs stream stdout/stderr
          over the event WebSocket, are cancellable, enforce a per-instance timeout and are persisted
          in run history.
        </p>
      </InlineNotice>

      <ScriptEditorModal
        open={editor !== null}
        script={editor?.script ?? null}
        template={editor?.template ?? null}
        existingIds={existingIds}
        onClose={() => setEditor(null)}
        onSaved={handleSaved}
      />

      {runTarget ? (
        <RunScriptDialog
          key={runTarget.id}
          open
          script={runTarget}
          instances={instances.data ?? []}
          instancesLoading={instances.loading}
          instancesError={instances.error}
          onRetryInstances={instances.reload}
          onClose={() => setRunTarget(null)}
          onStarted={handleRunStarted}
        />
      ) : null}

      {/* Built-in view: the content is visible but editing is not offered. */}
      <Modal
        open={viewTarget !== null}
        onClose={() => setViewTarget(null)}
        title={viewTarget?.name ?? "Built-in script"}
        description="Built-in library entry — read-only. Duplicate it to create an editable copy."
        size="lg"
        footer={
          <>
            <Button variant="ghost" onClick={() => setViewTarget(null)}>
              Close
            </Button>
            <Button
              variant="primary"
              icon="copy"
              onClick={() => viewTarget && duplicate(viewTarget)}
            >
              Duplicate to customise
            </Button>
          </>
        }
      >
        {viewTarget ? (
          <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
            <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
              <Badge tone="info">{scriptTypeMeta(viewTarget.type).label}</Badge>
              <Badge tone="accent" icon="shield">
                Built-in
              </Badge>
              <Badge tone="neutral" icon="clock">
                {viewTarget.timeout_s ?? 120}s timeout
              </Badge>
              <Badge tone="neutral">{viewTarget.id}</Badge>
            </div>
            {viewTarget.description ? (
              <p className="small" style={{ color: "var(--text-muted)" }}>
                {viewTarget.description}
              </p>
            ) : null}
            <dl className="kv-list">
              <div className="kv-list__row">
                <dt>Default targets</dt>
                <dd>{scriptTargetsLabel(viewTarget.targets)}</dd>
              </div>
              <div className="kv-list__row">
                <dt>Tags</dt>
                <dd>{(viewTarget.tags ?? []).join(", ") || "—"}</dd>
              </div>
              <div className="kv-list__row">
                <dt>Updated</dt>
                <dd>{formatRelativeTime(viewTarget.updated_at)}</dd>
              </div>
            </dl>
            {viewTarget.path ? (
              <div>
                <p className="ds-field__label">Path</p>
                <CodeBlock code={viewTarget.path} language="path" copyable />
              </div>
            ) : null}
            {viewTarget.content ? (
              <div>
                <p className="ds-field__label">Content</p>
                <CodeBlock
                  code={viewTarget.content}
                  language={viewTarget.type.replace(/_/g, "-")}
                  maxHeight={320}
                  copyable
                />
              </div>
            ) : null}
          </div>
        ) : null}
      </Modal>

      {/* Built-in deletion attempt: explain the protection instead of deleting. */}
      <Modal
        open={protectedTarget !== null}
        onClose={() => setProtectedTarget(null)}
        title="Built-in scripts are protected"
        description="Deleting a built-in entry would not stick — the daemon re-seeds its built-in library at startup."
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setProtectedTarget(null)}>
              Keep it
            </Button>
            <Button variant="primary" icon="copy" onClick={() => protectedTarget && duplicate(protectedTarget)}>
              Duplicate to customise
            </Button>
          </>
        }
      >
        <p className="small" style={{ color: "var(--text-muted)" }}>
          <strong style={{ color: "var(--text)" }}>{protectedTarget?.name}</strong> ({protectedTarget?.id})
          is shipped with AVD Fleet. Duplicate it, then edit or delete the copy with a different id.
        </p>
      </Modal>

      {/* Custom script deletion: destructive confirmation. */}
      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete this script?"
        description={
          deleteTarget
            ? `"${deleteTarget.name}" will be removed from the library. Runs that already started keep the definition and results they captured.`
            : undefined
        }
        confirmLabel="Delete script"
        tone="danger"
        onCancel={() => setDeleteTarget(null)}
        onConfirm={handleDelete}
      >
        {deleteTarget ? (
          <p className="muted small" style={{ fontFamily: "var(--font-mono)" }}>
            {deleteTarget.id}
          </p>
        ) : null}
      </ConfirmDialog>

      {/* Keep the type list visible for operators who only need the reference. */}
      <Card title="Script type reference" icon="file">
        <ul className="type-list">
          {SCRIPT_TYPES.map((entry) => (
            <li key={entry.value} className="type-list__item">
              <Badge tone="info">{entry.label}</Badge>
              <code>{entry.value}</code>
              <span className="type-list__hint">{entry.hint}</span>
            </li>
          ))}
        </ul>
        <p className="muted small" style={{ marginTop: "var(--space-3)" }}>
          Built-in scripts: {data.filter((script) => script.builtin).length
            ? data
                .filter((script) => script.builtin)
                .map((script) => humanize(script.name))
                .join(", ")
            : "none loaded."}
        </p>
      </Card>
    </div>
  );
}
