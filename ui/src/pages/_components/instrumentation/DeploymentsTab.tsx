/**
 * Deployments tab.
 *
 * Starts runs (`POST /deployments`), lists recent runs, and opens one to show
 * per-target step results plus a polled log. Polling every ~2 s remains the
 * source of truth while the run is `queued`/`running`; an `instrumentation.run`
 * WebSocket event for the visible run refreshes it immediately instead of
 * waiting for the next tick. Polling stops when the run finishes or the tab
 * unmounts.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useEventStream } from "@/app/EventStream";
import { instrumentationApi } from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  ConfirmDialog,
  EmptyState,
  Field,
  IconButton,
  InlineNotice,
  Input,
  LogViewer,
  Select,
  StatusPill,
  Table,
  useToast,
  type Column,
  type LogLine,
} from "@/lib/components";
import { useAsync } from "@/lib/hooks";
import type {
  DeploymentRun,
  InstrumentationProfile,
  LogEntry,
  StepResult,
  TargetResult,
} from "@/lib/types";
import { formatDateTime, formatTime, pluralize } from "@/lib/utils";

import {
  StateBadge,
  apiErrorText,
  isLiveRun,
  runTargetSummary,
  stepActionMeta,
  styles,
  targetCounts,
} from "@/pages/_components/instrumentation/shared";

export interface DeploymentsTabProps {
  selected: string[];
  onGoToOverview: () => void;
}

function stepTone(step: StepResult): "success" | "danger" {
  return step.ok ? "success" : "danger";
}

function TargetResultCard({ result }: { result: TargetResult }) {
  const failed = !result.ok && !result.skipped;
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-3)",
        padding: "var(--space-3)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius-sm)",
        background: "var(--surface-sunken)",
      }}
    >
      <div style={styles.rowBetween}>
        <span style={{ fontWeight: 600, color: "var(--text)" }}>{result.instance}</span>
        <div style={styles.rowTight}>
          {result.ok ? (
            <Badge tone="success" icon="check-circle">
              Passed
            </Badge>
          ) : null}
          {result.skipped ? (
            <Badge tone="neutral" icon="info" title="The target was skipped (for example an instance that is not running) — it did not run the steps and did not fail one.">
              Skipped
            </Badge>
          ) : null}
          {failed ? (
            <Badge tone="danger" icon="alert-circle">
              Failed
            </Badge>
          ) : null}
        </div>
      </div>

      {result.error ? <div style={styles.warning}>{result.error}</div> : null}

      {result.steps.length > 0 ? (
        <ol className="step-list">
          {result.steps.map((step, index) => {
            const meta = stepActionMeta(step.action);
            return (
              <li key={`${step.step_id}-${index}`}>
                <span className="step-list__index" aria-hidden="true">
                  {index + 1}
                </span>
                <div style={{ minWidth: 0 }}>
                  <div style={styles.rowTight}>
                    <span className="step-list__title">{meta.label}</span>
                    <Badge tone={stepTone(step)} dot>
                      {step.ok ? "ok" : "failed"}
                    </Badge>
                    {step.state ? <StateBadge state={step.state} label="state" /> : null}
                  </div>
                  {step.message ? <div className="step-list__hint">{step.message}</div> : null}
                  {Object.keys(step.data).length > 0 ? (
                    <details>
                      <summary className="muted small" style={{ cursor: "pointer" }}>
                        Step data
                      </summary>
                      <div style={{ marginTop: "var(--space-2)" }}>
                        <CodeBlock
                          code={JSON.stringify(step.data, null, 2)}
                          language="json"
                          filename={`${step.action} data`}
                          maxHeight={180}
                        />
                      </div>
                    </details>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ol>
      ) : (
        <span className="muted small">No steps ran for this target.</span>
      )}
    </div>
  );
}

export function DeploymentsTab({ selected, onGoToOverview }: DeploymentsTabProps) {
  const toast = useToast();
  const profiles = useAsync(() => instrumentationApi.listProfiles(), []);
  const runs = useAsync(() => instrumentationApi.listDeployments(50), []);
  const reloadRuns = runs.reload;

  const [profileId, setProfileId] = useState("");
  const [label, setLabel] = useState("");
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  const [activeRun, setActiveRun] = useState<DeploymentRun | null>(null);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [logsLoading, setLogsLoading] = useState(false);
  const [logError, setLogError] = useState<string | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [cancelTarget, setCancelTarget] = useState<DeploymentRun | null>(null);
  const openSeq = useRef(0);
  const busyRef = useRef(false);
  const mountedRef = useRef(true);
  const visibleRunRef = useRef<string | null>(null);
  const { lastEvent } = useEventStream();

  const allProfiles = useMemo(() => profiles.data?.profiles ?? [], [profiles.data]);
  const allRuns = useMemo(() => runs.data?.runs ?? [], [runs.data]);
  const profileById = useMemo(() => {
    const map = new Map<string, InstrumentationProfile>();
    for (const profile of allProfiles) map.set(profile.id, profile);
    return map;
  }, [allProfiles]);

  const chosenProfile = allProfiles.find((profile) => profile.id === profileId);
  const runId = activeRun?.id ?? null;
  const live = activeRun ? isLiveRun(activeRun.state) : false;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Track the run that is actually on screen so a late poll or event response
  // can never overwrite the run the operator switched to.
  useEffect(() => {
    visibleRunRef.current = runId;
  }, [runId]);

  const openRun = async (run: DeploymentRun): Promise<void> => {
    openSeq.current += 1;
    const seq = openSeq.current;
    setActiveRun(run);
    setLogs([]);
    setLogError(null);
    setPollError(null);
    setLogsLoading(true);
    try {
      const [fresh, logResult] = await Promise.all([
        instrumentationApi.getDeployment(run.id),
        instrumentationApi.listDeploymentLogs(run.id),
      ]);
      if (seq !== openSeq.current) return;
      setActiveRun(fresh);
      setLogs(logResult.logs);
    } catch (caught) {
      if (seq !== openSeq.current) return;
      setLogError(apiErrorText(caught));
    } finally {
      if (seq === openSeq.current) setLogsLoading(false);
    }
  };

  const reloadLogs = async (): Promise<void> => {
    if (!runId) return;
    setLogsLoading(true);
    try {
      const result = await instrumentationApi.listDeploymentLogs(runId);
      setLogs(result.logs);
      setLogError(null);
    } catch (caught) {
      setLogError(apiErrorText(caught));
    } finally {
      setLogsLoading(false);
    }
  };

  /**
   * Re-read the visible run and its log. Shared by the 2 s poll and the
   * `instrumentation.run` WebSocket event so an event can refresh immediately.
   */
  const refreshRun = useCallback(
    async (id: string): Promise<void> => {
      if (busyRef.current) return;
      busyRef.current = true;
      try {
        const [next, logResult] = await Promise.all([
          instrumentationApi.getDeployment(id),
          instrumentationApi.listDeploymentLogs(id),
        ]);
        if (!mountedRef.current || visibleRunRef.current !== id) return;
        setActiveRun(next);
        setLogs(logResult.logs);
        setPollError(null);
        if (!isLiveRun(next.state)) reloadRuns();
      } catch (caught) {
        if (mountedRef.current && visibleRunRef.current === id) {
          setPollError(apiErrorText(caught));
        }
      } finally {
        busyRef.current = false;
      }
    },
    [reloadRuns],
  );

  // Polling stays the source of truth: one request every ~2 s while the visible
  // run is in flight. The interval is torn down when the run finishes.
  useEffect(() => {
    if (!runId || !live) return;
    const timer = window.setInterval(() => {
      void refreshRun(runId);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [live, refreshRun, runId]);

  // Fast path: the daemon publishes `instrumentation.run` on the existing event
  // stream. Refresh the visible run immediately instead of waiting for a tick.
  useEffect(() => {
    if (!lastEvent || lastEvent.type !== "instrumentation.run" || !runId) return;
    const eventRunId = lastEvent.payload.run_id;
    if (typeof eventRunId !== "string" || eventRunId !== runId) return;
    void refreshRun(runId);
  }, [lastEvent, refreshRun, runId]);

  const startDeployment = async (): Promise<void> => {
    if (!chosenProfile) return;
    if (selected.length === 0) {
      setStartError("Select at least one AVD in the Overview tab first.");
      return;
    }
    setStarting(true);
    setStartError(null);
    try {
      const run = await instrumentationApi.createDeployment({
        profile_id: chosenProfile.id,
        targets: selected,
        label: label.trim(),
      });
      toast.success(
        "Deployment queued",
        `${chosenProfile.name} → ${pluralize(selected.length, "AVD")} · ${run.state}`,
      );
      setLabel("");
      void openRun(run);
      reloadRuns();
    } catch (caught) {
      const message = apiErrorText(caught);
      setStartError(message);
      toast.error("Deployment could not be started", message);
    } finally {
      setStarting(false);
    }
  };

  const cancelRun = async (): Promise<void> => {
    if (!cancelTarget) return;
    try {
      const updated = await instrumentationApi.cancelDeployment(cancelTarget.id);
      if (updated.state === "cancelled") {
        toast.success("Run cancelled", updated.label || updated.id);
      } else {
        toast.warning(
          "Cancellation requested",
          "The daemon stops the run after the step already in flight; watch the run for its final state.",
        );
      }
      setActiveRun((current) => (current && current.id === updated.id ? updated : current));
      reloadRuns();
    } catch (caught) {
      toast.error("Could not cancel the run", apiErrorText(caught));
    } finally {
      setCancelTarget(null);
    }
  };

  const logLines: LogLine[] = logs.map((entry) => ({
    text: `[${formatTime(entry.at)}]${entry.instance ? ` [${entry.instance}]` : ""} ${entry.level}: ${entry.message}`,
    stream: entry.level === "error" ? "stderr" : entry.level === "warn" ? "system" : "stdout",
  }));

  const columns: Column<DeploymentRun>[] = [
    {
      key: "run",
      header: "Run",
      sortable: true,
      sortValue: (run) => run.created_at,
      width: "270px",
      render: (run) => (
        <div style={styles.stackTight}>
          <span style={{ fontWeight: 600, color: "var(--text)" }}>{run.label || run.id}</span>
          <code style={styles.code}>{run.id}</code>
        </div>
      ),
    },
    {
      key: "profile",
      header: "Profile",
      sortable: true,
      sortValue: (run) => profileById.get(run.profile_id)?.name ?? run.profile_id,
      width: "150px",
      render: (run) => (
        <span>
          {profileById.get(run.profile_id)?.name ?? (
            <span className="muted small" title={run.profile_id}>
              unknown profile
            </span>
          )}
        </span>
      ),
    },
    {
      key: "state",
      header: "State",
      sortable: true,
      sortValue: (run) => run.state,
      width: "130px",
      render: (run) => (
        <StatusPill status={run.state} pulse={isLiveRun(run.state)} label={run.state} />
      ),
    },
    {
      key: "targets",
      header: "Targets",
      width: "90px",
      render: (run) => (
        <span title={run.targets.join(", ")}>{pluralize(run.targets.length, "AVD")}</span>
      ),
    },
    {
      key: "results",
      header: "Results",
      width: "210px",
      render: (run) => {
        if (run.results.length === 0) {
          return (
            <span className="muted small">
              {isLiveRun(run.state) ? "running…" : "no results"}
            </span>
          );
        }
        const { ok, failed, skipped } = targetCounts(run.results);
        return (
          <div style={styles.rowTight}>
            <Badge tone="success">{ok} passed</Badge>
            {failed > 0 ? <Badge tone="danger">{failed} failed</Badge> : null}
            {skipped > 0 ? (
              <Badge tone="neutral" title="Skipped targets did not run the steps.">
                {skipped} skipped
              </Badge>
            ) : null}
          </div>
        );
      },
    },
    {
      key: "created",
      header: "Created",
      width: "150px",
      sortable: true,
      sortValue: (run) => run.created_at,
      render: (run) => <span className="muted small">{formatDateTime(run.created_at)}</span>,
    },
    {
      key: "actions",
      header: "",
      align: "right",
      width: "80px",
      render: (run) => (
        <div style={styles.actions}>
          <IconButton
            label={`Open run ${run.label || run.id}`}
            icon="eye"
            size="sm"
            onClick={() => void openRun(run)}
          />
          <IconButton
            label={`Cancel run ${run.label || run.id}`}
            icon="stop"
            size="sm"
            variant="danger"
            disabled={!isLiveRun(run.state)}
            style={isLiveRun(run.state) ? undefined : styles.controlDisabled}
            title={
              isLiveRun(run.state)
                ? "Ask the daemon to cancel this run"
                : "Only queued or running deployments can be cancelled"
            }
            onClick={() => setCancelTarget(run)}
          />
        </div>
      ),
    },
  ];

  return (
    <div style={styles.stack}>
      <Card
        title="Start a deployment"
        icon="play"
        description="POST /instrumentation/deployments. The daemon runs the profile in the background; this page polls the run while it is queued or running."
      >
        <div style={styles.stack}>
          {profiles.error ? (
            <InlineNotice
              tone="danger"
              title="Could not load instrumentation profiles"
              action={
                <Button size="sm" variant="secondary" icon="refresh" onClick={profiles.reload}>
                  Retry
                </Button>
              }
            >
              <p>{apiErrorText(profiles.error)}</p>
            </InlineNotice>
          ) : null}

          <div style={styles.fieldGrid}>
            <Field
              label="Profile"
              hint="Built-in profiles are read-only. Duplicate one in the Profiles tab to bind modules."
            >
              <Select
                value={profileId}
                placeholder="Choose a profile"
                options={allProfiles.map((profile) => ({
                  value: profile.id,
                  label: `${profile.name}${profile.builtin ? " (built-in)" : ""}`,
                }))}
                onChange={(event) => setProfileId(event.target.value)}
              />
            </Field>
            <Field label="Label" hint="Optional; defaults to “profile → N AVD(s)” on the daemon.">
              <Input
                value={label}
                placeholder="Nightly LSPosed rollout"
                onChange={(event) => setLabel(event.target.value)}
              />
            </Field>
          </div>

          <div style={styles.rowBetween}>
            <div style={styles.rowTight}>
              <span className="muted small">Targets:</span>
              {selected.length === 0 ? (
                <span className="muted small">none selected</span>
              ) : (
                selected.map((name) => (
                  <Badge key={name} tone="info" icon="instance">
                    {name}
                  </Badge>
                ))
              )}
            </div>
            <div style={styles.rowTight}>
              {selected.length === 0 ? (
                <Button size="sm" variant="ghost" icon="arrow-left" onClick={onGoToOverview}>
                  Select AVDs
                </Button>
              ) : null}
              <Button
                variant="primary"
                icon="play"
                loading={starting}
                disabled={!chosenProfile || selected.length === 0}
                title={
                  !chosenProfile
                    ? "Choose a profile first"
                    : selected.length === 0
                      ? "Select at least one AVD in the Overview tab"
                      : `Deploy ${chosenProfile.name} to ${pluralize(selected.length, "AVD")}`
                }
                onClick={() => void startDeployment()}
              >
                Start deployment
              </Button>
            </div>
          </div>

          {startError ? (
            <InlineNotice tone="danger" title="Deployment could not be started">
              <p>{startError}</p>
            </InlineNotice>
          ) : null}

          {chosenProfile ? (
            <p className="muted small" style={{ margin: 0 }}>
              {chosenProfile.steps.length > 0
                ? `Steps: ${chosenProfile.steps
                    .map((step) => stepActionMeta(step.action).label)
                    .join(" → ")}`
                : "This profile has no steps."}
            </p>
          ) : null}
        </div>
      </Card>

      {activeRun ? (
        <Card
          title={`Run · ${activeRun.label || activeRun.id}`}
          icon="runs"
          description={`GET /instrumentation/deployments/${activeRun.id}`}
          actions={
            <div style={styles.rowTight}>
              {live ? (
                <Badge tone="info" icon="clock">
                  Polling every 2 s
                </Badge>
              ) : null}
              <Button
                size="sm"
                variant="danger"
                icon="stop"
                disabled={!live}
                title={
                  live
                    ? "Ask the daemon to cancel this run"
                    : "Only queued or running deployments can be cancelled"
                }
                onClick={() => setCancelTarget(activeRun)}
              >
                Cancel
              </Button>
              <IconButton
                label="Close run details"
                icon="close"
                size="sm"
                onClick={() => setActiveRun(null)}
              />
            </div>
          }
        >
          <div style={styles.stack}>
            <div style={styles.rowTight}>
              <StatusPill status={activeRun.state} pulse={live} label={activeRun.state} />
              <Badge tone="neutral" icon="templates">
                {profileById.get(activeRun.profile_id)?.name ?? activeRun.profile_id}
              </Badge>
              <Badge tone="neutral" icon="clock">
                Created {formatDateTime(activeRun.created_at)}
              </Badge>
              {activeRun.started_at ? (
                <Badge tone="neutral">Started {formatDateTime(activeRun.started_at)}</Badge>
              ) : null}
              {activeRun.finished_at ? (
                <Badge tone="neutral">Finished {formatDateTime(activeRun.finished_at)}</Badge>
              ) : null}
              <Badge tone="info">{pluralize(activeRun.targets.length, "target")}</Badge>
            </div>

            {activeRun.error ? (
              <InlineNotice tone="danger" title="Run error">
                <p>{activeRun.error}</p>
              </InlineNotice>
            ) : null}

            {pollError ? (
              <InlineNotice
                tone="warning"
                title="Live polling failed"
                action={
                  <Button size="sm" variant="secondary" icon="refresh" onClick={reloadLogs}>
                    Refresh logs
                  </Button>
                }
              >
                <p>{pollError}</p>
                <p>The last successful state stays on screen; the run keeps going on the daemon.</p>
              </InlineNotice>
            ) : null}

            <div style={styles.rowBetween}>
              <span style={styles.labels}>Per-target results</span>
              <span className="muted small">{runTargetSummary(activeRun)}</span>
            </div>

            {activeRun.results.length === 0 ? (
              <EmptyState
                compact
                icon="clock"
                title={live ? "Waiting for per-target results" : "No per-target results"}
                description={
                  live
                    ? "The daemon writes results after the run finishes; the log below updates while it runs."
                    : "This run recorded no target results."
                }
              />
            ) : (
              <div style={styles.cardGrid}>
                {activeRun.results.map((result) => (
                  <TargetResultCard key={result.instance} result={result} />
                ))}
              </div>
            )}

            {logError ? (
              <InlineNotice tone="danger" title="Could not load the run log">
                <p>{logError}</p>
              </InlineNotice>
            ) : null}

            <LogViewer
              lines={logLines}
              title={`Deployment log (${logs.length})`}
              emptyLabel={logsLoading ? "Loading the deployment log…" : "No log lines yet."}
              maxHeight={320}
              showLineNumbers
              actions={
                <IconButton
                  label="Reload the deployment log"
                  icon="refresh"
                  size="sm"
                  loading={logsLoading}
                  onClick={() => void reloadLogs()}
                />
              }
            />
          </div>
        </Card>
      ) : null}

      <Card
        title="Recent deployments"
        icon="runs"
        description="GET /instrumentation/deployments?limit=50. Open a run for per-target step results and its log."
        actions={
          <IconButton
            label="Reload deployments"
            icon="refresh"
            size="sm"
            loading={runs.loading}
            onClick={reloadRuns}
          />
        }
      >
        <div style={styles.stack}>
          {runs.error ? (
            <InlineNotice
              tone="danger"
              title="Could not load deployments"
              action={
                <Button size="sm" variant="secondary" icon="refresh" onClick={reloadRuns}>
                  Retry
                </Button>
              }
            >
              <p>{apiErrorText(runs.error)}</p>
            </InlineNotice>
          ) : null}

          <Table
            dense
            columns={columns}
            rows={allRuns}
            rowKey={(run) => run.id}
            loading={runs.loading && allRuns.length === 0}
            skeletonRows={4}
            caption="Recent instrumentation deployments"
            empty={
              runs.error && allRuns.length === 0 ? (
                <EmptyState
                  compact
                  icon="alert-circle"
                  title="Deployments unavailable"
                  description="The deployment request failed; retry it above. No run history is being shown."
                />
              ) : (
                <EmptyState
                  compact
                  icon="runs"
                  title="No deployments yet"
                  description="Pick a profile and at least one AVD above to start the first run."
                />
              )
            }
          />
        </div>
      </Card>

      <ConfirmDialog
        open={cancelTarget !== null}
        title="Cancel this deployment?"
        description={cancelTarget ? cancelTarget.label || cancelTarget.id : undefined}
        confirmLabel="Cancel run"
        onCancel={() => setCancelTarget(null)}
        onConfirm={cancelRun}
      >
        <p>
          Cancellation is cooperative: the daemon stops the run after the step currently in flight. Targets that
          already finished keep their results, and the run ends in the <code>cancelled</code> state.
        </p>
      </ConfirmDialog>
    </div>
  );
}
