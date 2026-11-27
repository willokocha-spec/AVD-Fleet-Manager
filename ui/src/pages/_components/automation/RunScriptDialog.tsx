import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "@/lib/api";
import {
  Badge,
  Button,
  ButtonLink,
  Checkbox,
  Field,
  InlineNotice,
  Input,
  Modal,
  Spinner,
} from "@/lib/components";
import type { AvdInfo, ScriptDefinition, ScriptRun } from "@/lib/types";

import { MAX_CONCURRENCY, MIN_CONCURRENCY, describeError } from "./shared";

export interface RunScriptDialogProps {
  open: boolean;
  script: ScriptDefinition;
  instances: AvdInfo[];
  instancesLoading: boolean;
  instancesError: Error | null;
  onRetryInstances: () => void;
  onClose: () => void;
  onStarted: (run: ScriptRun) => void;
}

/**
 * Fan-out launcher: pick running instances, bounded concurrency, an optional
 * timeout override and dry-run planning. Only `running` instances can be
 * targeted — the executor needs an adb serial / Appium endpoint.
 */
export function RunScriptDialog({
  open,
  script,
  instances,
  instancesLoading,
  instancesError,
  onRetryInstances,
  onClose,
  onStarted,
}: RunScriptDialogProps) {
  const running = useMemo(() => instances.filter((instance) => instance.state === "running"), [instances]);
  const runningNames = useMemo(() => new Set(running.map((instance) => instance.name)), [running]);
  const configuredTargets = script.targets ?? [];
  const unselectableTargets = configuredTargets.filter((target) => !runningNames.has(target));

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [concurrency, setConcurrency] = useState("5");
  const [timeoutOverride, setTimeoutOverride] = useState("");
  const [dryRun, setDryRun] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const touched = useRef(false);

  // Prefer the script's configured targets when they are running; otherwise
  // default to every running instance. Never overwrite a manual selection.
  useEffect(() => {
    if (touched.current) return;
    const configured = configuredTargets.filter((target) => runningNames.has(target));
    setSelected(new Set(configured.length > 0 ? configured : running.map((instance) => instance.name)));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- recompute only when instances change
  }, [running, runningNames]);

  const toggle = (name: string): void => {
    touched.current = true;
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const selectAll = (): void => {
    touched.current = true;
    setSelected(new Set(running.map((instance) => instance.name)));
  };

  const selectNone = (): void => {
    touched.current = true;
    setSelected(new Set());
  };

  const selectedInstances = running.filter((instance) => selected.has(instance.name));
  const unbooted = selectedInstances.filter((instance) => instance.boot_completed === false);

  const handleClose = (): void => {
    if (submitting) return;
    onClose();
  };

  const handleStart = async (): Promise<void> => {
    const concurrencyValue = Number(concurrency);
    if (
      !Number.isInteger(concurrencyValue) ||
      concurrencyValue < MIN_CONCURRENCY ||
      concurrencyValue > MAX_CONCURRENCY
    ) {
      setError(`Concurrency must be a whole number between ${MIN_CONCURRENCY} and ${MAX_CONCURRENCY}.`);
      return;
    }
    if (selectedInstances.length === 0) {
      setError("Select at least one running instance to run on.");
      return;
    }
    let timeout: number | null = null;
    if (timeoutOverride.trim()) {
      const parsed = Number(timeoutOverride);
      if (!Number.isInteger(parsed) || parsed < 1) {
        setError("Timeout override must be a whole number of seconds (or left blank).");
        return;
      }
      timeout = parsed;
    }

    setSubmitting(true);
    setError(null);
    try {
      const run = await api.startScriptRun({
        script_id: script.id,
        instances: selectedInstances.map((instance) => instance.name),
        concurrency: concurrencyValue,
        timeout_s: timeout,
        dry_run: dryRun,
      });
      onStarted(run);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={handleClose}
      title={`Run ${script.name}`}
      description={`Fan ${script.type.replace(/_/g, " ")} out across running instances.`}
      size="lg"
      dismissible={!submitting}
      footer={
        <>
          <span className="muted small" style={{ marginRight: "auto" }}>
            {selectedInstances.length} of {running.length} running instance
            {running.length === 1 ? "" : "s"} selected
            {dryRun ? " · dry run" : ""}
          </span>
          <Button variant="ghost" onClick={handleClose} disabled={submitting}>
            Cancel
          </Button>
          <Button
            variant="primary"
            icon={dryRun ? "eye" : "play"}
            loading={submitting}
            disabled={instancesLoading || selectedInstances.length === 0}
            onClick={() => void handleStart()}
          >
            {dryRun ? "Plan dry run" : "Start run"}
          </Button>
        </>
      }
    >
      <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-5)" }}>
        {error ? (
          <InlineNotice tone="danger" title="Run not started">
            <p>{error}</p>
          </InlineNotice>
        ) : null}

        {instancesError ? (
          <InlineNotice
            tone="danger"
            title="Could not load instances"
            action={
              <Button size="sm" variant="secondary" icon="refresh" onClick={onRetryInstances}>
                Retry
              </Button>
            }
          >
            <p>{describeError(instancesError)}</p>
          </InlineNotice>
        ) : null}

        {instancesLoading ? (
          <div style={{ display: "flex", alignItems: "center", gap: "var(--space-3)" }}>
            <Spinner size="sm" label="Loading instances" />
            <span className="muted small">Loading fleet instances…</span>
          </div>
        ) : null}

        {!instancesLoading && !instancesError && running.length === 0 ? (
          <InlineNotice
            tone="warning"
            title="No running instances"
            action={
              <ButtonLink to="/fleet" size="sm" variant="secondary" icon="fleet">
                Open Fleet
              </ButtonLink>
            }
          >
            <p>
              Scripts execute against booted emulators, so only instances in the{" "}
              <code>running</code> state can be selected. Start an instance first, then re-open this
              dialog.
            </p>
          </InlineNotice>
        ) : null}

        {unselectableTargets.length > 0 ? (
          <InlineNotice tone="warning" title="Some configured targets are not running">
            <p>
              {unselectableTargets.join(", ")} {unselectableTargets.length === 1 ? "is" : "are"}{" "}
              configured on this script but cannot be selected until the instance is running.
            </p>
          </InlineNotice>
        ) : null}

        {unbooted.length > 0 ? (
          <InlineNotice tone="warning" title="Selected instance has not finished booting">
            <p>
              {unbooted.map((instance) => instance.name).join(", ")} reports{" "}
              <code>boot_completed=false</code>; adb commands may fail until the boot completes.
            </p>
          </InlineNotice>
        ) : null}

        {running.length > 0 ? (
          <fieldset className="ds-field" style={{ border: 0, padding: 0, margin: 0 }}>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: "var(--space-3)",
                flexWrap: "wrap",
              }}
            >
              <legend className="ds-field__label">Target instances (running only)</legend>
              <div style={{ display: "flex", gap: "var(--space-2)" }}>
                <Button size="sm" variant="ghost" onClick={selectAll}>
                  Select all
                </Button>
                <Button size="sm" variant="ghost" onClick={selectNone}>
                  Select none
                </Button>
              </div>
            </div>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))",
                gap: "var(--space-2)",
                maxHeight: 240,
                overflowY: "auto",
                padding: "var(--space-1)",
              }}
            >
              {running.map((instance) => (
                <Checkbox
                  key={instance.name}
                  label={instance.name}
                  hint={
                    instance.ports?.adb
                      ? `adb :${instance.ports.adb}${instance.boot_completed ? "" : " · booting"}`
                      : instance.image_name
                  }
                  checked={selected.has(instance.name)}
                  onChange={() => toggle(instance.name)}
                />
              ))}
            </div>
          </fieldset>
        ) : null}

        <div style={{ display: "grid", gap: "var(--space-4)", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)" }}>
          <Field
            label="Concurrency"
            htmlFor="run-concurrency"
            hint={`Parallel instances (${MIN_CONCURRENCY}–${MAX_CONCURRENCY}).`}
          >
            <Input
              id="run-concurrency"
              type="number"
              min={MIN_CONCURRENCY}
              max={MAX_CONCURRENCY}
              step={1}
              value={concurrency}
              onChange={(event) => setConcurrency(event.target.value)}
            />
          </Field>
          <Field
            label="Timeout override (s)"
            htmlFor="run-timeout"
            hint={`Leave blank to use the script's ${script.timeout_s ?? 120} s timeout.`}
          >
            <Input
              id="run-timeout"
              type="number"
              min={1}
              step={1}
              placeholder={String(script.timeout_s ?? 120)}
              value={timeoutOverride}
              onChange={(event) => setTimeoutOverride(event.target.value)}
            />
          </Field>
        </div>

        <Checkbox
          label="Dry run"
          hint="Resolve targets and print the exact commands without executing them. Results show the planned command lines."
          checked={dryRun}
          onChange={(event) => setDryRun(event.target.checked)}
        />

        <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
          <Badge tone="info">{script.type.replace(/_/g, " ")}</Badge>
          {configuredTargets.length > 0 ? (
            <Badge tone="neutral">{configuredTargets.length} configured target(s)</Badge>
          ) : (
            <Badge tone="neutral">Any running instance</Badge>
          )}
          <Badge tone="neutral">timeout {script.timeout_s ?? 120}s</Badge>
        </div>
      </div>
    </Modal>
  );
}
