import { Component, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { useEventStream } from "@/app/EventStream";
import { Badge, Button, Card, IconButton, StatusPill, type BadgeTone } from "@/lib/components";
import type { FleetEvent, ScriptRun } from "@/lib/types";
import type { SocketStatus } from "@/lib/ws";
import { copyText, formatDuration } from "@/lib/utils";

import {
  MAX_OUTPUT_LINES,
  RUN_POLL_INTERVAL_MS,
  downloadTextFile,
  isActiveRunState,
  runStateLabel,
  safeFilename,
} from "./shared";

/* -------------------------------------------------------------------------- */
/* Types                                                                       */
/* -------------------------------------------------------------------------- */

type LiveStatus = SocketStatus | "disabled" | "unavailable";
type LineStream = "stdout" | "stderr" | "system";

interface StreamLine {
  key: string;
  instance: string;
  stream: LineStream;
  text: string;
  at?: string;
}

interface StreamView {
  status: LiveStatus;
  lastEvent: FleetEvent | null;
}

/* -------------------------------------------------------------------------- */
/* Helpers                                                                     */
/* -------------------------------------------------------------------------- */

const INSTANCE_COLORS = [
  "var(--accent)",
  "var(--violet)",
  "var(--success)",
  "var(--warning)",
  "var(--info)",
] as const;

function instanceColor(instance: string): string {
  let hash = 0;
  for (const char of instance) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return INSTANCE_COLORS[hash % INSTANCE_COLORS.length]!;
}

/** Extract one line from a `script.run.output` event, or null when it is for another run. */
export function extractOutputLine(
  event: FleetEvent,
  runId: string,
): Omit<StreamLine, "key"> | null {
  if (event.type !== "script.run.output") return null;
  const payload = event.payload ?? {};
  if (payload.run_id !== runId) return null;
  const instance = typeof payload.instance === "string" ? payload.instance : "unknown";
  const stream: LineStream = payload.stream === "stderr" ? "stderr" : "stdout";
  const text = typeof payload.line === "string" ? payload.line : "";
  return { instance, stream, text, at: event.ts };
}

/** Build the captured stdout/stderr transcript from per-instance run results. */
export function capturedOutputLines(run: ScriptRun): StreamLine[] {
  const lines: StreamLine[] = [];
  for (const result of run.results ?? []) {
    const stdout = (result.stdout ?? "").replace(/\s+$/, "");
    const stderr = (result.stderr ?? "").replace(/\s+$/, "");
    if (!stdout && !stderr) continue;
    const detail = [
      runStateLabel(result.state),
      result.exit_code !== null && result.exit_code !== undefined ? `exit ${result.exit_code}` : null,
      result.duration_s !== null && result.duration_s !== undefined
        ? formatDuration(result.duration_s)
        : null,
    ]
      .filter(Boolean)
      .join(" · ");
    lines.push({
      key: `captured-${lines.length}`,
      instance: result.instance,
      stream: "system",
      text: `— ${result.instance} · ${detail} —`,
    });
    for (const text of stdout.split(/\r?\n/)) {
      lines.push({ key: `captured-${lines.length}`, instance: result.instance, stream: "stdout", text });
    }
    for (const text of stderr.split(/\r?\n/)) {
      lines.push({ key: `captured-${lines.length}`, instance: result.instance, stream: "stderr", text });
    }
  }
  return lines.length > MAX_OUTPUT_LINES ? lines.slice(-MAX_OUTPUT_LINES) : lines;
}

interface LiveStatusInfo {
  status: string;
  tone: BadgeTone;
  label: string;
  hint: string;
  pulse: boolean;
}

function liveStatusInfo(status: LiveStatus): LiveStatusInfo {
  switch (status) {
    case "open":
      return {
        status: "open",
        tone: "success",
        label: "Live",
        hint: "Streaming script.run.output from /ws/events while the run is active.",
        pulse: true,
      };
    case "connecting":
      return {
        status: "connecting",
        tone: "warning",
        label: "Connecting",
        hint: "Opening /ws/events — REST polling covers the gap.",
        pulse: true,
      };
    case "reconnecting":
      return {
        status: "reconnecting",
        tone: "warning",
        label: "Reconnecting",
        hint: "The event stream dropped; reconnecting with backoff while REST polling continues.",
        pulse: true,
      };
    case "closed":
      return {
        status: "closed",
        tone: "danger",
        label: "Events offline",
        hint: "The event socket is closed; output falls back to REST polling.",
        pulse: false,
      };
    case "idle":
      return {
        status: "idle",
        tone: "neutral",
        label: "Events idle",
        hint: "The event stream is not connected; output falls back to REST polling.",
        pulse: false,
      };
    case "disabled":
      return {
        status: "disabled",
        tone: "neutral",
        label: "Live paused",
        hint: "Live events are disabled in this environment; output falls back to REST polling.",
        pulse: false,
      };
    default:
      return {
        status: "unavailable",
        tone: "warning",
        label: "Unavailable",
        hint: "No live event provider in this context; showing captured output and REST polling only.",
        pulse: false,
      };
  }
}

/* -------------------------------------------------------------------------- */
/* Optional event-stream provider bridge                                       */
/* -------------------------------------------------------------------------- */

interface BoundaryProps {
  fallback: ReactNode;
  children: ReactNode;
}

interface BoundaryState {
  failed: boolean;
}

/**
 * The console shell owns the single `/ws/events` socket. When the live panel
 * is rendered outside that shell (isolated page tests, embedded previews) the
 * provider hook throws; this narrow boundary degrades to captured output and
 * polling instead of taking the whole Runs page down.
 */
class LiveEventsBoundary extends Component<BoundaryProps, BoundaryState> {
  state: BoundaryState = { failed: false };

  static getDerivedStateFromError(): BoundaryState {
    return { failed: true };
  }

  componentDidCatch(): void {
    /* Expected in provider-less render contexts; the fallback is intentional. */
  }

  render(): ReactNode {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

function LiveRunOutput({ run }: { run: ScriptRun }) {
  const { status, lastEvent } = useEventStream();
  return <RunOutputPanel run={run} stream={{ status, lastEvent }} />;
}

/**
 * Live per-run output over `/ws/events` (`script.run.output` filtered by run
 * id) with stream/instance colouring, auto-scroll pause, clear, copy and
 * download. Falls back to per-instance stdout/stderr from the REST run record.
 */
export function LiveRunOutputSection({ run }: { run: ScriptRun }) {
  return (
    <LiveEventsBoundary fallback={<RunOutputPanel run={run} stream={null} />}>
      <LiveRunOutput run={run} />
    </LiveEventsBoundary>
  );
}

/* -------------------------------------------------------------------------- */
/* Output panel                                                                */
/* -------------------------------------------------------------------------- */

interface RunOutputPanelProps {
  run: ScriptRun;
  /** `null` when the event provider is unavailable (fallback path). */
  stream: StreamView | null;
}

function RunOutputPanel({ run, stream }: RunOutputPanelProps) {
  const [lines, setLines] = useState<StreamLine[]>([]);
  const [autoScroll, setAutoScroll] = useState(true);
  const [cleared, setCleared] = useState(false);
  const [copied, setCopied] = useState(false);
  const nextId = useRef(0);
  const bodyRef = useRef<HTMLDivElement>(null);
  const copyTimer = useRef<number | null>(null);
  // Events delivered before this panel mounted are already part of the
  // captured transcript; don't replay the latest one a second time.
  const lastSeenEventId = useRef<string | null>(stream?.lastEvent?.id ?? null);

  const captured = useMemo(() => capturedOutputLines(run), [run]);

  useEffect(() => {
    setLines([]);
    setAutoScroll(true);
    setCleared(false);
    setCopied(false);
    nextId.current = 0;
  }, [run.id]);

  // Seed the transcript from the REST record (opening a finished run, or the
  // provider-less/polling fallback). Live lines keep it up to date afterwards.
  useEffect(() => {
    if (cleared || captured.length === 0) return;
    setLines((current) => (current.length === 0 ? captured : current));
  }, [captured, cleared]);

  const lastEvent = stream?.lastEvent ?? null;
  useEffect(() => {
    if (!lastEvent) return;
    if (lastSeenEventId.current === lastEvent.id) return;
    lastSeenEventId.current = lastEvent.id;
    const parsed = extractOutputLine(lastEvent, run.id);
    if (!parsed) return;
    nextId.current += 1;
    const key = `live-${nextId.current}`;
    setLines((current) => {
      const next = [...current, { ...parsed, key }];
      return next.length > MAX_OUTPUT_LINES ? next.slice(-MAX_OUTPUT_LINES) : next;
    });
    setCleared(false);
  }, [lastEvent, run.id]);

  useEffect(
    () => () => {
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
    },
    [],
  );

  useEffect(() => {
    if (!autoScroll) return;
    const body = bodyRef.current;
    if (body) body.scrollTop = body.scrollHeight;
  }, [autoScroll, lines]);

  const statusInfo = liveStatusInfo(stream?.status ?? "unavailable");
  const active = isActiveRunState(run.state);
  const capped = lines.length >= MAX_OUTPUT_LINES;
  const plain = useMemo(
    () =>
      lines
        .map((line) => {
          if (line.stream === "system") return line.text;
          return `${line.instance} › ${line.text}`;
        })
        .join("\n"),
    [lines],
  );

  const handleCopy = async (): Promise<void> => {
    const ok = await copyText(plain);
    if (!ok) return;
    setCopied(true);
    if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
    copyTimer.current = window.setTimeout(() => setCopied(false), 1_600);
  };

  const handleDownload = (): void => {
    downloadTextFile(`${safeFilename(run.id)}-output.txt`, `${plain}\n`);
  };

  const handleClear = (): void => {
    setLines([]);
    setCleared(true);
  };

  const emptyLabel = cleared
    ? "Output cleared. New live lines will appear here as they arrive."
    : active
      ? "Waiting for the first line on /ws/events. Output is also captured by the daemon for each instance."
      : "No stdout/stderr was captured for this run.";

  return (
    <Card
      icon="file"
      title="Run output"
      description={
        active
          ? statusInfo.hint
          : "Run finished — the captured stdout/stderr is shown below. Instance cards hold the authoritative per-instance output."
      }
      actions={
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "var(--space-2)",
            flexWrap: "wrap",
            justifyContent: "flex-end",
          }}
        >
          <StatusPill
            status={active ? statusInfo.status : "captured"}
            tone={active ? statusInfo.tone : "neutral"}
            label={active ? statusInfo.label : "Captured"}
            pulse={active && statusInfo.pulse}
            title={`/ws/events · script.run.output · REST polling every ${RUN_POLL_INTERVAL_MS / 1000} s`}
          />
          <Badge tone="neutral">{lines.length} line{lines.length === 1 ? "" : "s"}</Badge>
          {capped ? (
            <Badge tone="warning" title={`Older lines are dropped past ${MAX_OUTPUT_LINES} lines.`}>
              last {MAX_OUTPUT_LINES.toLocaleString()}
            </Badge>
          ) : null}
          <Button
            size="sm"
            variant="ghost"
            icon={autoScroll ? "eye" : "eye-off"}
            onClick={() => setAutoScroll((value) => !value)}
          >
            {autoScroll ? "Pause scroll" : "Resume scroll"}
          </Button>
          <Button size="sm" variant="ghost" icon={copied ? "check" : "copy"} onClick={() => void handleCopy()}>
            {copied ? "Copied" : "Copy"}
          </Button>
          <IconButton label="Download run output" icon="download" size="sm" onClick={handleDownload} />
          <IconButton
            label="Clear run output"
            icon="trash"
            size="sm"
            onClick={handleClear}
            disabled={lines.length === 0}
          />
        </div>
      }
    >
      <p className="muted small" style={{ marginBottom: "var(--space-2)" }}>
        <code>/ws/events</code> · <code>script.run.output</code> · instance colouring, stdout/stderr
        highlighting, capped at {MAX_OUTPUT_LINES.toLocaleString()} lines
      </p>
      <div className="ds-logs">
        <div
          ref={bodyRef}
          className="ds-logs__body"
          style={{ maxHeight: 380 }}
          role="log"
          aria-label={`Run ${run.id} output`}
          aria-live="polite"
          tabIndex={0}
        >
          {lines.length === 0 ? (
            <p className="ds-logs__empty">{emptyLabel}</p>
          ) : (
            lines.map((line) => (
              <div key={line.key} className={`ds-logs__line ds-logs__line--${line.stream}`}>
                <span className="ds-logs__text" title={line.at}>
                  {line.stream === "system" ? null : (
                    <>
                      <span style={{ color: instanceColor(line.instance), fontWeight: 600 }}>
                        {line.instance}
                      </span>
                      <span style={{ color: "var(--text-subtle)" }}> › </span>
                    </>
                  )}
                  {line.text || "\u00a0"}
                </span>
              </div>
            ))
          )}
        </div>
      </div>
    </Card>
  );
}
