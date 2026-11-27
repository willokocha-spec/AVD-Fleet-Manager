import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api } from "@/lib/api";
import {
  Badge,
  Button,
  ButtonLink,
  Card,
  EmptyState,
  IconButton,
  InlineNotice,
  PageHeader,
  SearchBox,
  Select,
  StatusPill,
  Table,
  type Column,
} from "@/lib/components";
import { useAsync } from "@/lib/hooks";
import type { PageMeta } from "@/lib/page-meta";
import type { RunState, ScriptRun } from "@/lib/types";
import { formatRelativeTime } from "@/lib/utils";

import { RunDetail } from "./_components/automation/RunDetail";
import {
  RUN_LIST_POLL_INTERVAL_MS,
  countRunResults,
  describeError,
  formatRunDuration,
  isActiveRunState,
  runDurationSeconds,
  runStateLabel,
  usePolling,
} from "./_components/automation/shared";

export const meta: PageMeta = {
  path: "/runs",
  title: "Runs",
  icon: "runs",
  nav: 70,
  description: "Script run history, live output and cancellation",
};

const STATE_FILTER_OPTIONS: Array<{ value: "all" | RunState; label: string }> = [
  { value: "all", label: "All states" },
  { value: "queued", label: "Queued" },
  { value: "running", label: "Running" },
  { value: "succeeded", label: "Succeeded" },
  { value: "failed", label: "Failed" },
  { value: "cancelled", label: "Cancelled" },
  { value: "timed_out", label: "Timed out" },
];

const RUN_COLUMNS: Array<Column<ScriptRun>> = [
  {
    key: "run",
    header: "Run",
    sortable: true,
    sortValue: (run) => run.started_at ?? "",
    render: (run) => (
      <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        <span style={{ fontWeight: 600 }}>{run.script_name || run.script_id}</span>
        <span className="muted" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-2xs)" }}>
          {run.id}
          {run.dry_run ? " · dry run" : ""}
        </span>
      </div>
    ),
  },
  {
    key: "state",
    header: "State",
    sortable: true,
    sortValue: (run) => run.state,
    render: (run) => (
      <StatusPill
        status={run.state}
        label={runStateLabel(run.state)}
        pulse={isActiveRunState(run.state)}
      />
    ),
  },
  {
    key: "instances",
    header: "Instances",
    align: "right",
    sortable: true,
    sortValue: (run) => run.instances?.length ?? 0,
    render: (run) => (
      <span title={(run.instances ?? []).join(", ")}>{(run.instances ?? []).length}</span>
    ),
  },
  {
    key: "results",
    header: "Results",
    sortable: true,
    sortValue: (run) => countRunResults(run).succeeded,
    render: (run) => {
      const counts = countRunResults(run);
      return (
        <span style={{ display: "inline-flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          <Badge tone="success">{counts.succeeded} ok</Badge>
          {counts.failed > 0 ? <Badge tone="danger">{counts.failed} failed</Badge> : null}
          <span className="muted small">of {counts.total}</span>
        </span>
      );
    },
  },
  {
    key: "duration",
    header: "Duration",
    align: "right",
    sortable: true,
    sortValue: (run) => runDurationSeconds(run) ?? -1,
    render: (run) => formatRunDuration(run),
  },
  {
    key: "trigger",
    header: "Trigger",
    render: (run) => (
      <span style={{ display: "inline-flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
        <span>{run.requested_by || "operator"}</span>
        {run.dry_run ? <Badge tone="info">dry run</Badge> : null}
      </span>
    ),
  },
  {
    key: "started",
    header: "Started",
    sortable: true,
    sortValue: (run) => run.started_at ?? "",
    render: (run) => (
      <span title={run.started_at ?? undefined}>{formatRelativeTime(run.started_at)}</span>
    ),
  },
];

/**
 * U5 — run history + detail.
 *
 * The selected run lives in the `?run=` query string so live runs are
 * deep-linkable (the Scripts page navigates here after starting a run) and the
 * page stays a single route. Detail view, live output and exports are rendered
 * by `RunDetail`.
 */
export default function RunsPage() {
  const runs = useAsync(() => api.listRuns(), []);
  const [searchParams, setSearchParams] = useSearchParams();
  const [query, setQuery] = useState("");
  const [stateFilter, setStateFilter] = useState<"all" | RunState>("all");

  const selectedId = searchParams.get("run");
  const data = useMemo(() => runs.data ?? [], [runs.data]);
  const activeCount = data.filter((run) => isActiveRunState(run.state)).length;
  usePolling(runs.reload, RUN_LIST_POLL_INTERVAL_MS, activeCount > 0);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return data.filter((run) => {
      if (stateFilter !== "all" && run.state !== stateFilter) return false;
      if (!needle) return true;
      return [run.id, run.script_name ?? "", run.script_id, run.requested_by ?? ""]
        .join(" ")
        .toLowerCase()
        .includes(needle);
    });
  }, [data, query, stateFilter]);

  const totalSucceeded = data.filter((run) => run.state === "succeeded").length;
  const totalFailed = data.filter((run) => run.state === "failed" || run.state === "timed_out").length;

  const selectRun = (run: ScriptRun): void => {
    setSearchParams({ run: run.id });
  };

  const clearSelection = (): void => {
    setSearchParams({});
  };

  const clearFilters = (): void => {
    setQuery("");
    setStateFilter("all");
  };

  return (
    <div className="page">
      <PageHeader
        title="Runs"
        eyebrow="Execution history"
        description="Every script fan-out, its per-instance result, exit codes, duration and live output. Runs are persisted and can be cancelled or allowed to time out."
        actions={
          <>
            <ButtonLink to="/scripts" variant="secondary" icon="scripts">
              Script library
            </ButtonLink>
            <IconButton
              label="Refresh run history"
              icon="refresh"
              onClick={runs.reload}
              loading={runs.loading}
            />
          </>
        }
        meta={
          <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
            <Badge tone="neutral" icon="runs">
              {data.length} run{data.length === 1 ? "" : "s"}
            </Badge>
            {activeCount > 0 ? (
              <Badge tone="warning" dot>
                {activeCount} in progress
              </Badge>
            ) : null}
            <Badge tone="success">{totalSucceeded} succeeded</Badge>
            <Badge tone={totalFailed > 0 ? "danger" : "neutral"}>{totalFailed} failed</Badge>
          </div>
        }
      />

      {runs.error ? (
        <InlineNotice
          tone="danger"
          title="Could not load run history"
          action={
            <Button size="sm" variant="secondary" icon="refresh" onClick={runs.reload}>
              Retry
            </Button>
          }
        >
          <p>{describeError(runs.error)}</p>
        </InlineNotice>
      ) : null}

      {selectedId ? (
        <RunDetail runId={selectedId} onClose={clearSelection} onChanged={runs.reload} />
      ) : null}

      <Card
        icon="runs"
        title="Run history"
        description={
          activeCount > 0
            ? `Auto-refreshing every ${RUN_LIST_POLL_INTERVAL_MS / 1000} s while runs are active. Select a run for per-instance detail and live output.`
            : "Select a run for per-instance results, live output, cancellation and JSON/text export."
        }
      >
        {data.length === 0 && !runs.loading ? (
          <EmptyState
            icon="runs"
            title="No runs yet"
            description="Start a run from the script library. Runs are persisted, streamed live over /ws/events and can be cancelled or allowed to time out."
            action={
              <ButtonLink to="/scripts" variant="primary" icon="play">
                Choose a script
              </ButtonLink>
            }
          />
        ) : (
          <>
            <div
              style={{
                display: "flex",
                gap: "var(--space-3)",
                alignItems: "center",
                flexWrap: "wrap",
                marginBottom: "var(--space-4)",
              }}
            >
              <SearchBox
                value={query}
                onChange={setQuery}
                placeholder="Search script, run id or operator…"
                label="Search runs"
                className=""
              />
              <div style={{ minWidth: 160 }}>
                <Select
                  aria-label="Filter runs by state"
                  value={stateFilter}
                  onChange={(event) => setStateFilter(event.target.value as "all" | RunState)}
                  options={STATE_FILTER_OPTIONS}
                />
              </div>
              <span className="muted small" style={{ marginLeft: "auto" }}>
                {filtered.length} of {data.length} shown
              </span>
              {activeCount > 0 ? (
                <Badge tone="warning" dot>
                  live updates on
                </Badge>
              ) : null}
            </div>

            {filtered.length === 0 ? (
              <EmptyState
                compact
                icon="search"
                title="No runs match these filters"
                description="Try a different search term or state."
                action={
                  <Button size="sm" variant="secondary" icon="refresh" onClick={clearFilters}>
                    Clear filters
                  </Button>
                }
              />
            ) : (
              <Table
                columns={RUN_COLUMNS}
                rows={filtered}
                rowKey={(run) => run.id}
                loading={runs.loading && !runs.data}
                skeletonRows={4}
                onRowClick={selectRun}
                rowAriaLabel={(run) => `Open run ${run.id} — ${run.script_name || run.script_id}`}
                selectedKey={selectedId}
                caption="Script run history"
              />
            )}
          </>
        )}
      </Card>
    </div>
  );
}
