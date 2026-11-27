import { useState } from "react";

import { api } from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  ConfirmDialog,
  EmptyState,
  InlineNotice,
  ProgressBar,
  Skeleton,
  StatCard,
  StatusPill,
  Table,
  type Column,
  useToast,
  statusTone,
} from "@/lib/components";
import { useAsync } from "@/lib/hooks";
import type { InstanceResult } from "@/lib/types";
import { copyText, formatDuration, formatRelativeTime } from "@/lib/utils";

import { LiveRunOutputSection } from "./RunLiveOutput";
import {
  RUN_POLL_INTERVAL_MS,
  apiErrorStatus,
  buildRunJsonExport,
  buildRunTextExport,
  countRunResults,
  describeError,
  downloadTextFile,
  formatRunDuration,
  isActiveRunState,
  runStateLabel,
  safeFilename,
  usePolling,
} from "./shared";

export interface RunDetailProps {
  runId: string;
  /** Return to the run list (clears the `?run=` query). */
  onClose: () => void;
  /** Notify the list so counts/state refresh after a mutation. */
  onChanged: () => void;
}

/**
 * U5 — run detail: state/timeout/cancel summary, per-instance results, live
 * `/ws/events` output, JSON/text export and REST polling while the run is
 * active (the fallback when the event socket is closed).
 */
export function RunDetail({ runId, onClose, onChanged }: RunDetailProps) {
  const toast = useToast();
  const state = useAsync(() => api.getRun(runId), [runId]);
  const run = state.data && state.data.id === runId ? state.data : null;
  const active = run ? isActiveRunState(run.state) : false;
  usePolling(state.reload, RUN_POLL_INTERVAL_MS, active);

  const [confirming, setConfirming] = useState(false);

  const counts = run ? countRunResults(run) : { total: 0, succeeded: 0, failed: 0, active: 0, finished: 0 };
  const results = run?.results ?? [];

  const handleCancel = async (): Promise<void> => {
    if (!run) return;
    try {
      const updated = await api.cancelRun(run.id);
      state.setData(updated);
      setConfirming(false);
      onChanged();
      toast.success("Run cancelled", updated.script_name ?? updated.id);
    } catch (error) {
      toast.error("Could not cancel the run", describeError(error));
    }
  };

  const copyExport = async (kind: "json" | "text"): Promise<void> => {
    if (!run) return;
    const payload = kind === "json" ? buildRunJsonExport(run) : buildRunTextExport(run);
    const copied = await copyText(payload);
    if (copied) toast.success(`Run results copied as ${kind.toUpperCase()}`);
    else toast.error("Could not copy the run results");
  };

  const downloadExport = (kind: "json" | "text"): void => {
    if (!run) return;
    const payload = kind === "json" ? buildRunJsonExport(run) : buildRunTextExport(run);
    const extension = kind === "json" ? "json" : "txt";
    const mime = kind === "json" ? "application/json;charset=utf-8" : "text/plain;charset=utf-8";
    const started = downloadTextFile(`${safeFilename(run.id)}-results.${extension}`, payload, mime);
    if (started) toast.success(`Downloading ${kind.toUpperCase()} results`);
    else toast.error("The browser blocked the download");
  };

  if (!run && state.error) {
    if (apiErrorStatus(state.error) === 404) {
      return (
        <Card title="Run not found" icon="runs">
          <EmptyState
            icon="search"
            title={`No run with id "${runId}"`}
            description="It may have been pruned from the run history, or the id is mistyped."
            action={
              <Button variant="secondary" icon="arrow-left" onClick={onClose}>
                Back to run history
              </Button>
            }
          />
        </Card>
      );
    }
    return (
      <InlineNotice
        tone="danger"
        title="Could not load the run"
        action={
          <>
            <Button size="sm" variant="secondary" icon="refresh" onClick={state.reload}>
              Retry
            </Button>
            <Button size="sm" variant="ghost" icon="arrow-left" onClick={onClose}>
              All runs
            </Button>
          </>
        }
      >
        <p>{describeError(state.error)}</p>
      </InlineNotice>
    );
  }

  if (!run) {
    return (
      <Card title="Run" icon="runs">
        <div className="skeleton-card">
          <Skeleton width="38%" height={22} />
          <Skeleton lines={3} />
          <Skeleton width="64%" height={14} />
        </div>
      </Card>
    );
  }

  const stateTone = statusTone(run.state);
  const statTone =
    stateTone === "success" || stateTone === "warning" || stateTone === "danger" ? stateTone : "default";
  const requestEntries = run.request ? Object.entries(run.request) : [];
  const finishedHint = run.finished_at
    ? `finished ${formatRelativeTime(run.finished_at)}`
    : active
      ? "in progress"
      : "not finished";

  const columns: Column<InstanceResult>[] = [
    {
      key: "instance",
      header: "Instance",
      sortValue: (result) => result.instance,
      sortable: true,
      render: (result) => (
        <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)" }}>{result.instance}</span>
      ),
    },
    {
      key: "state",
      header: "State",
      sortValue: (result) => result.state,
      sortable: true,
      render: (result) => (
        <StatusPill status={result.state} label={runStateLabel(result.state)} pulse={result.state === "running"} />
      ),
    },
    {
      key: "exit_code",
      header: "Exit",
      align: "right",
      sortValue: (result) => result.exit_code ?? -1,
      sortable: true,
      render: (result) =>
        result.exit_code === null || result.exit_code === undefined ? "—" : String(result.exit_code),
    },
    {
      key: "duration",
      header: "Duration",
      align: "right",
      sortValue: (result) => result.duration_s ?? -1,
      sortable: true,
      render: (result) => formatDuration(result.duration_s),
    },
    {
      key: "started",
      header: "Started",
      render: (result) => (
        <span title={result.started_at ?? undefined}>{formatRelativeTime(result.started_at)}</span>
      ),
    },
    {
      key: "output",
      header: "Output",
      render: (result) => <ResultOutputCell result={result} />,
    },
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-5)" }}>
      <Card
        icon="runs"
        title={run.script_name || run.script_id}
        description={
          <span>
            Run <code>{run.id}</code> · requested by {run.requested_by || "operator"}
            {run.dry_run ? " · dry run" : ""}
          </span>
        }
        actions={
          <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center", flexWrap: "wrap" }}>
            <Button size="sm" variant="ghost" icon="arrow-left" onClick={onClose}>
              All runs
            </Button>
            <Button size="sm" variant="ghost" icon="copy" onClick={() => void copyExport("text")}>
              Copy summary
            </Button>
            {active ? (
              <Button size="sm" variant="danger" icon="stop" onClick={() => setConfirming(true)}>
                Cancel run
              </Button>
            ) : null}
          </div>
        }
      >
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
          <div className="stat-grid">
            <StatCard
              label="State"
              icon="runs"
              tone={statTone}
              value={<StatusPill status={run.state} label={runStateLabel(run.state)} pulse={active} />}
              hint={finishedHint}
            />
            <StatCard label="Duration" icon="clock" value={formatRunDuration(run)} hint={finishedHint} />
            <StatCard
              label="Instances"
              icon="fleet"
              value={counts.total}
              hint={`${counts.active} still active`}
            />
            <StatCard
              label="Succeeded"
              icon="check-circle"
              tone={counts.succeeded > 0 ? "success" : "default"}
              value={counts.succeeded}
              hint={`of ${counts.total}`}
            />
            <StatCard
              label="Failed"
              icon="alert-circle"
              tone={counts.failed > 0 ? "danger" : "default"}
              value={counts.failed}
              hint="includes cancelled / timed out"
            />
            <StatCard
              label="Trigger"
              icon="play"
              value={run.requested_by || "operator"}
              hint={run.dry_run ? "dry run — no commands executed" : "live execution"}
            />
          </div>

          {active && counts.total > 0 ? (
            <ProgressBar
              value={counts.finished}
              max={counts.total}
              label={`${counts.finished} of ${counts.total} instances finished — polling every ${
                RUN_POLL_INTERVAL_MS / 1000
              } s`}
              showValue
            />
          ) : null}

          {run.error ? (
            <InlineNotice
              tone={run.state === "failed" || run.state === "timed_out" ? "danger" : "warning"}
              title="Run error"
            >
              <p>{run.error}</p>
            </InlineNotice>
          ) : null}

          {run.dry_run ? (
            <InlineNotice tone="info" title="Dry run">
              <p>
                No commands were executed. Each result below lists the exact command line the runner
                would have executed for that instance.
              </p>
            </InlineNotice>
          ) : null}
        </div>
      </Card>

      <Card
        icon="instance"
        title={`Instance results (${counts.total})`}
        description="Per-instance state, exit code, duration and captured output. Rows are sortable."
      >
        {results.length === 0 ? (
          <EmptyState
            compact
            icon="instance"
            title="No instance results yet"
            description={
              active
                ? "The executor is still resolving targets; results appear as each instance finishes."
                : "This run has no per-instance results."
            }
          />
        ) : (
          <Table
            columns={columns}
            rows={results}
            rowKey={(result) => result.instance}
            caption={`Results for run ${run.id}`}
            dense
          />
        )}
      </Card>

      <LiveRunOutputSection run={run} />

      {requestEntries.length > 0 ? (
        <Card
          icon="settings"
          title="Request"
          description="Redacted run request persisted with the run (targets, concurrency, timeout, env and planned commands in dry-run mode)."
        >
          <CodeBlock
            code={JSON.stringify(run.request, null, 2)}
            language="json"
            filename={`${run.id}.request.json`}
            maxHeight={260}
          />
        </Card>
      ) : null}

      <Card
        icon="download"
        title="Export results"
        description="Combined summary plus every instance's captured stdout/stderr, ready for a ticket or artifact."
      >
        <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
          <Button size="sm" icon="copy" onClick={() => void copyExport("json")}>
            Copy JSON
          </Button>
          <Button size="sm" icon="copy" onClick={() => void copyExport("text")}>
            Copy text
          </Button>
          <Button size="sm" icon="download" onClick={() => downloadExport("json")}>
            Download JSON
          </Button>
          <Button size="sm" icon="download" onClick={() => downloadExport("text")}>
            Download text
          </Button>
        </div>
        <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap", marginTop: "var(--space-3)" }}>
          <Badge tone="neutral">{counts.succeeded} succeeded</Badge>
          <Badge tone={counts.failed > 0 ? "danger" : "neutral"}>{counts.failed} failed</Badge>
          <Badge tone="neutral">{formatRunDuration(run)} total</Badge>
          {run.dry_run ? <Badge tone="info">dry run</Badge> : null}
        </div>
      </Card>

      <ConfirmDialog
        open={confirming}
        title="Cancel this run?"
        description={`Run ${run.id} will stop scheduling instances. Instances that are already executing are cancelled and completed results are kept.`}
        confirmLabel="Cancel the run"
        tone="danger"
        onCancel={() => setConfirming(false)}
        onConfirm={handleCancel}
      >
        {counts.active > 0 ? (
          <p className="muted small">{counts.active} instance result(s) are still queued or running.</p>
        ) : null}
      </ConfirmDialog>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Result output cell                                                          */
/* -------------------------------------------------------------------------- */

function ResultOutputCell({ result }: { result: InstanceResult }) {
  const stdout = (result.stdout ?? "").replace(/\s+$/, "");
  const stderr = (result.stderr ?? "").replace(/\s+$/, "");
  const hasOutput = Boolean(stdout || stderr);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
      {result.error ? (
        <span style={{ color: "var(--danger)", fontSize: "var(--fs-xs)" }}>{result.error}</span>
      ) : null}
      {hasOutput ? (
        <details>
          <summary className="small" style={{ cursor: "pointer", color: "var(--text-muted)" }}>
            {stdout ? `stdout (${stdout.split(/\r?\n/).length})` : ""}
            {stdout && stderr ? " · " : ""}
            {stderr ? `stderr (${stderr.split(/\r?\n/).length})` : ""}
          </summary>
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: "var(--space-2)",
              marginTop: "var(--space-2)",
              minWidth: 280,
            }}
          >
            {stdout ? (
              <CodeBlock code={stdout} filename={`${result.instance}.stdout`} maxHeight={160} />
            ) : null}
            {stderr ? (
              <CodeBlock code={stderr} filename={`${result.instance}.stderr`} maxHeight={160} />
            ) : null}
          </div>
        </details>
      ) : !result.error ? (
        <span className="muted small">—</span>
      ) : null}
    </div>
  );
}
