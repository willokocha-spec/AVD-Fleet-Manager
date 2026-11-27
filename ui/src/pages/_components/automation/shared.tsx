/**
 * Page-private helpers for the Automation pages (U5 Scripts/Runs + U7 Portable).
 *
 * Everything in `src/pages/_components/automation/` is owned by the automation
 * pages. These modules are deliberately NOT matched by the page registry glob
 * (`src/pages/*.tsx`), so they never become routes.
 */

import { useEffect } from "react";

import { ApiError } from "@/lib/api";
import type { CheckStatus, RunState, ScriptRun, ScriptType } from "@/lib/types";
import { formatDuration, humanize, pluralize } from "@/lib/utils";

/* -------------------------------------------------------------------------- */
/* Constants                                                                   */
/* -------------------------------------------------------------------------- */

/** Older stream lines are dropped past this cap to keep the DOM bounded. */
export const MAX_OUTPUT_LINES = 5_000;
export const RUN_POLL_INTERVAL_MS = 3_000;
export const RUN_LIST_POLL_INTERVAL_MS = 5_000;
export const MIN_CONCURRENCY = 1;
export const MAX_CONCURRENCY = 64;

/* -------------------------------------------------------------------------- */
/* Script types                                                               */
/* -------------------------------------------------------------------------- */

export interface ScriptTypeMeta {
  value: ScriptType;
  label: string;
  /** One-line explanation shown in the type selector / library cards. */
  hint: string;
  /** What to put in the content editor for this type. */
  contentHint: string;
}

export const SCRIPT_TYPES: readonly ScriptTypeMeta[] = [
  {
    value: "adb_shell",
    label: "adb shell",
    hint: "Shell command executed per instance through adb.",
    contentHint:
      "adb shell fragment run on every instance — e.g. `pm list packages`. `$SERIAL`/`$AVD_NAME` are expanded by the runner.",
  },
  {
    value: "host_python",
    label: "Host Python",
    hint: "Python executed on the host with instance metadata injected.",
    contentHint:
      "Python source executed on the host. Instance metadata is provided as AVDFLEET_* environment variables.",
  },
  {
    value: "appium_python",
    label: "Appium (Python)",
    hint: "UiAutomator2 Appium session driven from Python.",
    contentHint:
      "Appium Python client script. The executor allocates the server/system ports and exports AVDFLEET_* connection details.",
  },
  {
    value: "appium_js",
    label: "Appium (JS)",
    hint: "WebdriverIO Appium session driven from Node.js.",
    contentHint:
      "WebdriverIO/Appium script run with Node.js. Connection details are provided as AVDFLEET_* environment variables.",
  },
  {
    value: "inline_shell",
    label: "Inline shell",
    hint: "Host shell fragment (PowerShell / bash, depending on the host).",
    contentHint:
      "Host shell fragment. Prefer adb_shell for device work; use this for host-side file/OS commands.",
  },
] as const;

export function scriptTypeMeta(type: ScriptType | undefined): ScriptTypeMeta {
  return SCRIPT_TYPES.find((entry) => entry.value === type) ?? SCRIPT_TYPES[0]!;
}

/* -------------------------------------------------------------------------- */
/* Run state helpers                                                          */
/* -------------------------------------------------------------------------- */

const TERMINAL_STATES: ReadonlySet<RunState> = new Set<RunState>([
  "succeeded",
  "failed",
  "cancelled",
  "timed_out",
]);

export function isTerminalRunState(state: RunState | undefined): boolean {
  return state !== undefined && TERMINAL_STATES.has(state);
}

export function isActiveRunState(state: RunState | undefined): boolean {
  return state === "queued" || state === "running";
}

export interface RunCounts {
  total: number;
  succeeded: number;
  /** Failed family (`failed`, `timed_out`, `cancelled`) — matches the daemon model. */
  failed: number;
  active: number;
  finished: number;
}

export function countRunResults(run: Pick<ScriptRun, "results">): RunCounts {
  const results = run.results ?? [];
  let succeeded = 0;
  let failed = 0;
  let active = 0;
  for (const result of results) {
    if (result.state === "succeeded") succeeded += 1;
    else if (result.state === "failed" || result.state === "timed_out" || result.state === "cancelled") {
      failed += 1;
    } else {
      active += 1;
    }
  }
  return { total: results.length, succeeded, failed, active, finished: succeeded + failed };
}

/** Wall-clock duration of a run in seconds (ticking while it is active). */
export function runDurationSeconds(
  run: Pick<ScriptRun, "state" | "started_at" | "finished_at">,
  now = Date.now(),
): number | null {
  if (!run.started_at) return null;
  const started = Date.parse(run.started_at);
  if (Number.isNaN(started)) return null;
  const finishedAt = run.finished_at ? Date.parse(run.finished_at) : Number.NaN;
  const end = !Number.isNaN(finishedAt)
    ? finishedAt
    : isTerminalRunState(run.state)
      ? started
      : now;
  return Math.max(0, (end - started) / 1000);
}

export function formatRunDuration(run: Pick<ScriptRun, "state" | "started_at" | "finished_at">): string {
  return formatDuration(runDurationSeconds(run));
}

/** Human label for a result state ("Timed out" reads better than "Timed Out"). */
export function runStateLabel(state: RunState | null | undefined): string {
  switch (state) {
    case "timed_out":
      return "Timed out";
    case "queued":
      return "Queued";
    default:
      return humanize(state) || "Unknown";
  }
}

export function checkStatusLabel(status: CheckStatus): string {
  switch (status) {
    case "ok":
      return "Pass";
    case "skip":
      return "Skip";
    default:
      return humanize(status);
  }
}

/* -------------------------------------------------------------------------- */
/* Errors                                                                      */
/* -------------------------------------------------------------------------- */

export function apiErrorStatus(error: unknown): number | null {
  if (error instanceof ApiError) return error.status || null;
  return null;
}

/** True for "this daemon build does not expose the endpoint" style responses. */
export function isEndpointUnavailable(error: unknown): boolean {
  const status = apiErrorStatus(error);
  return status === 404 || status === 501 || status === 503;
}

export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    return error.hint ? `${error.message} — ${error.hint}` : error.message;
  }
  if (error instanceof Error) return error.message;
  return String(error);
}

/* -------------------------------------------------------------------------- */
/* Polling                                                                     */
/* -------------------------------------------------------------------------- */

/**
 * `setInterval` wrapper used for the REST polling fallback. `load` must be
 * referentially stable (e.g. `useAsync(...).reload`) or the interval restarts.
 */
export function usePolling(load: () => void, intervalMs: number, enabled: boolean): void {
  useEffect(() => {
    if (!enabled) return;
    const timer = window.setInterval(load, intervalMs);
    return () => window.clearInterval(timer);
  }, [enabled, intervalMs, load]);
}

/* -------------------------------------------------------------------------- */
/* Script library helpers                                                      */
/* -------------------------------------------------------------------------- */

export function scriptTargetsLabel(targets: string[] | null | undefined): string {
  const count = targets?.length ?? 0;
  return count === 0 ? "Any instance" : pluralize(count, "default target");
}

/** Stable, unique script id derived from the human name. */
export function slugifyScriptId(name: string, existingIds: Iterable<string>): string {
  const slug =
    name
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 40) || "script";
  const base = `scr-${slug}`;
  const existing = new Set(existingIds);
  if (!existing.has(base)) return base;
  let counter = 2;
  while (existing.has(`${base}-${counter}`)) counter += 1;
  return `${base}-${counter}`;
}

/** Parse a comma/newline separated tag string into a clean unique list. */
export function parseTags(value: string): string[] {
  return Array.from(
    new Set(
      value
        .split(/[,\n]/)
        .map((tag) => tag.trim())
        .filter(Boolean),
    ),
  );
}

/* -------------------------------------------------------------------------- */
/* Run export helpers                                                         */
/* -------------------------------------------------------------------------- */

export interface RunExportSummary {
  total: number;
  succeeded: number;
  failed: number;
}

function exportSummary(run: ScriptRun): RunExportSummary {
  const counts = countRunResults(run);
  return { total: counts.total, succeeded: counts.succeeded, failed: counts.failed };
}

/** Human-readable combined summary + per-instance output for download/copy. */
export function buildRunTextExport(run: ScriptRun): string {
  const counts = exportSummary(run);
  const lines: string[] = [
    "AVD Fleet script run",
    "====================",
    `Run:        ${run.id}`,
    `Script:     ${run.script_name || run.script_id} (${run.script_id})`,
    `Type:       ${run.type ?? "unknown"}`,
    `State:      ${run.state}`,
    `Dry run:    ${run.dry_run ? "yes" : "no"}`,
    `Requested:  ${run.requested_by || "operator"}`,
    `Started:    ${run.started_at ?? "—"}`,
    `Finished:   ${run.finished_at ?? "—"}`,
    `Duration:   ${formatRunDuration(run)}`,
    `Instances:  ${counts.total} (${counts.succeeded} succeeded, ${counts.failed} failed)`,
  ];
  if (run.error) lines.push(`Error:      ${run.error}`);
  lines.push("");

  for (const result of run.results ?? []) {
    lines.push(`--- ${result.instance} [${result.state}] ---`);
    if (result.exit_code !== null && result.exit_code !== undefined) {
      lines.push(`exit code: ${result.exit_code}`);
    }
    if (result.duration_s !== null && result.duration_s !== undefined) {
      lines.push(`duration: ${formatDuration(result.duration_s)}`);
    }
    if (result.error) lines.push(`error: ${result.error}`);
    if (result.stdout) {
      lines.push("stdout:");
      lines.push(result.stdout.replace(/\s+$/, ""));
    }
    if (result.stderr) {
      lines.push("stderr:");
      lines.push(result.stderr.replace(/\s+$/, ""));
    }
    lines.push("");
  }

  return `${lines.join("\n").replace(/\n+$/, "")}\n`;
}

/** Machine-readable combined results (excludes bulky raw request payloads). */
export function buildRunJsonExport(run: ScriptRun): string {
  const summary = exportSummary(run);
  return `${JSON.stringify(
    {
      id: run.id,
      script_id: run.script_id,
      script_name: run.script_name ?? "",
      type: run.type ?? null,
      state: run.state,
      dry_run: Boolean(run.dry_run),
      requested_by: run.requested_by ?? "",
      started_at: run.started_at ?? null,
      finished_at: run.finished_at ?? null,
      duration_s: runDurationSeconds(run),
      summary,
      error: run.error ?? "",
      results: (run.results ?? []).map((result) => ({
        instance: result.instance,
        state: result.state,
        exit_code: result.exit_code ?? null,
        duration_s: result.duration_s ?? null,
        started_at: result.started_at ?? null,
        finished_at: result.finished_at ?? null,
        error: result.error ?? "",
        stdout: result.stdout ?? "",
        stderr: result.stderr ?? "",
      })),
    },
    null,
    2,
  )}\n`;
}

export function safeFilename(value: string, fallback = "avdfleet"): string {
  const cleaned = value
    .trim()
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
  return cleaned || fallback;
}

/** Trigger a client-side text download. Returns false when the browser blocks it. */
export function downloadTextFile(
  filename: string,
  content: string,
  mime = "text/plain;charset=utf-8",
): boolean {
  if (typeof document === "undefined" || typeof URL === "undefined") return false;
  try {
    const blob = new Blob([content], { type: mime });
    if (typeof URL.createObjectURL !== "function") return false;
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
    return true;
  } catch {
    return false;
  }
}
