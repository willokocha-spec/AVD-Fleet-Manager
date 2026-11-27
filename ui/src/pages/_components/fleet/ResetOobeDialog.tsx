import { useCallback, useEffect, useState } from "react";

import { Button, Checkbox, Modal } from "@/lib/components";
import type { OobeResetStrategy } from "@/lib/types";
import { pluralize } from "@/lib/utils";

import { OOBE_STRATEGIES, describeError } from "./fleet-utils";

export interface ResetOobeOptions {
  strategy: OobeResetStrategy;
  verify: boolean;
}

export interface ResetOobeDialogProps {
  open: boolean;
  /** One name for a single instance, several for a bulk reset. */
  instanceNames: readonly string[];
  onClose: () => void;
  /**
   * Perform the reset. Resolve to close the dialog; throw to keep it open and
   * show the daemon error inline.
   */
  onConfirm: (options: ResetOobeOptions) => Promise<void>;
  title?: string;
}

/**
 * Strategy picker for `POST /instances/{name}/reset-oobe`.
 *
 * A single instance is reset from the detail page; a selection is reset in bulk
 * from the Fleet page. `snapshot` is the default because it is both the fastest
 * and the least destructive route back to a known OOBE state.
 */
export function ResetOobeDialog({
  open,
  instanceNames,
  onClose,
  onConfirm,
  title,
}: ResetOobeDialogProps) {
  const [strategy, setStrategy] = useState<OobeResetStrategy>("snapshot");
  const [verify, setVerify] = useState(true);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setStrategy("snapshot");
    setVerify(true);
    setPending(false);
    setError(null);
  }, [open]);

  const count = instanceNames.length;
  const resolvedTitle =
    title ??
    (count === 1
      ? `Reset ${instanceNames[0] ?? "instance"} to OOBE`
      : `Reset ${count} instances to OOBE`);

  const handleConfirm = async (): Promise<void> => {
    setPending(true);
    setError(null);
    try {
      await onConfirm({ strategy, verify });
      onClose();
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setPending(false);
    }
  };

  const handleClose = useCallback(() => {
    if (!pending) onClose();
  }, [onClose, pending]);

  const detail =
    count === 1
      ? instanceNames[0]
      : `${pluralize(count, "instance")}: ${instanceNames.slice(0, 4).join(", ")}${
          count > 4 ? ` +${count - 4} more` : ""
        }`;

  return (
    <Modal
      open={open}
      onClose={handleClose}
      title={resolvedTitle}
      description={`Restore ${count === 1 ? "this instance" : "these instances"} to a fresh first-run (OOBE) state.`}
      size="md"
      dismissible={!pending}
      footer={
        <>
          <Button variant="ghost" onClick={handleClose} disabled={pending}>
            Cancel
          </Button>
          <Button
            variant="primary"
            icon="refresh"
            loading={pending}
            onClick={() => void handleConfirm()}
          >
            {count === 1 ? "Reset instance" : `Reset ${count} instances`}
          </Button>
        </>
      }
    >
      <p className="muted small" style={{ marginTop: 0, overflowWrap: "anywhere" }}>
        {detail}
      </p>

      {error ? (
        <p role="alert" className="ds-field__error" style={{ marginBottom: "var(--space-4)" }}>
          {error}
        </p>
      ) : null}

      <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
        <legend className="ds-field__label" style={{ marginBottom: "var(--space-2)" }}>
          Reset strategy
        </legend>
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
          {OOBE_STRATEGIES.map((option) => {
            const active = strategy === option.id;
            return (
              <label
                key={option.id}
                style={{
                  display: "flex",
                  alignItems: "flex-start",
                  gap: "var(--space-3)",
                  padding: "var(--space-3)",
                  border: `1px solid ${active ? "var(--accent)" : "var(--border)"}`,
                  background: active ? "var(--accent-soft)" : "var(--surface-sunken)",
                  borderRadius: "var(--radius-sm)",
                  cursor: pending ? "progress" : "pointer",
                }}
              >
                <input
                  type="radio"
                  name="oobe-reset-strategy"
                  value={option.id}
                  checked={active}
                  disabled={pending}
                  onChange={() => setStrategy(option.id)}
                  style={{ marginTop: 3, accentColor: "var(--accent)" }}
                />
                <span style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                  <span style={{ fontWeight: 600, color: "var(--text)" }}>{option.label}</span>
                  <span className="muted small">{option.description}</span>
                </span>
              </label>
            );
          })}
        </div>
      </fieldset>

      <div style={{ marginTop: "var(--space-4)" }}>
        <Checkbox
          label="Verify the Setup Wizard after resetting"
          hint="Runs the OOBE verification pass and reports the visible activity."
          checked={verify}
          disabled={pending}
          onChange={(event) => setVerify(event.target.checked)}
        />
      </div>
    </Modal>
  );
}
