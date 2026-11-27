import { Button, IconButton, ProgressBar } from "@/lib/components";
import { pluralize } from "@/lib/utils";

export type BulkActionId = "start" | "stop" | "restart" | "reset" | "delete";

const BUSY_LABEL: Record<BulkActionId, string> = {
  start: "Starting",
  stop: "Stopping",
  restart: "Restarting",
  reset: "Resetting to OOBE",
  delete: "Deleting",
};

export interface BulkActionBarProps {
  names: readonly string[];
  busy: BulkActionId | null;
  onStart: () => void;
  onStop: () => void;
  onRestart: () => void;
  onReset: () => void;
  onDelete: () => void;
  onClear: () => void;
}

/**
 * Contextual toolbar shown once one or more instances are selected.
 * Operations are batched by the page; this component is presentation only.
 */
export function BulkActionBar({
  names,
  busy,
  onStart,
  onStop,
  onRestart,
  onReset,
  onDelete,
  onClear,
}: BulkActionBarProps) {
  const count = names.length;
  if (count === 0) return null;
  const label = pluralize(count, "instance");

  return (
    <section
      className="ds-card"
      role="region"
      aria-label="Bulk actions"
      style={{ padding: "var(--space-3) var(--space-4)" }}
    >
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          gap: "var(--space-2)",
        }}
      >
        <p style={{ margin: 0, fontWeight: 600 }} aria-live="polite">
          {label} selected
        </p>
        <span aria-hidden="true" className="muted">
          ·
        </span>
        <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-2)" }}>
          <Button size="sm" icon="play" loading={busy === "start"} disabled={busy !== null && busy !== "start"} onClick={onStart}>
            Start
          </Button>
          <Button size="sm" icon="stop" loading={busy === "stop"} disabled={busy !== null && busy !== "stop"} onClick={onStop}>
            Stop
          </Button>
          <Button size="sm" icon="restart" loading={busy === "restart"} disabled={busy !== null && busy !== "restart"} onClick={onRestart}>
            Restart
          </Button>
          <Button size="sm" icon="refresh" loading={busy === "reset"} disabled={busy !== null && busy !== "reset"} onClick={onReset}>
            Reset to OOBE
          </Button>
          <Button
            size="sm"
            variant="danger"
            icon="trash"
            loading={busy === "delete"}
            disabled={busy !== null && busy !== "delete"}
            onClick={onDelete}
          >
            Delete
          </Button>
          <IconButton
            label="Clear selection"
            icon="close"
            size="sm"
            disabled={busy !== null}
            onClick={onClear}
          />
        </div>
      </div>
      {busy ? (
        <div style={{ marginTop: "var(--space-2)" }}>
          <ProgressBar indeterminate label={`${BUSY_LABEL[busy]} ${label}…`} />
        </div>
      ) : null}
    </section>
  );
}
