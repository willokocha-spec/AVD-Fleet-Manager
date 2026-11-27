import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import {
  Button,
  Field,
  IconButton,
  InlineNotice,
  Input,
  Modal,
  Select,
  Textarea,
} from "@/lib/components";
import type { ScriptDefinition, ScriptType } from "@/lib/types";

import { SCRIPT_TYPES, describeError, parseTags, scriptTypeMeta, slugifyScriptId } from "./shared";

/* -------------------------------------------------------------------------- */
/* Form model                                                                  */
/* -------------------------------------------------------------------------- */

interface EnvRow {
  uid: number;
  key: string;
  value: string;
}

interface ScriptFormState {
  id: string;
  name: string;
  type: ScriptType;
  content: string;
  path: string;
  description: string;
  timeout: string;
  env: EnvRow[];
  tags: string;
}

interface ScriptFormErrors {
  name?: string;
  id?: string;
  content?: string;
  timeout?: string;
  env?: string;
}

let envRowSequence = 0;

function newEnvRow(key = "", value = ""): EnvRow {
  envRowSequence += 1;
  return { uid: envRowSequence, key, value };
}

function formFromTemplate(template: ScriptDefinition): ScriptFormState {
  const form = formFromScript(template);
  return {
    ...form,
    id: "",
    name: `${template.name} (copy)`,
  };
}

function emptyForm(): ScriptFormState {
  return {
    id: "",
    name: "",
    type: "adb_shell",
    content: "",
    path: "",
    description: "",
    timeout: "120",
    env: [newEnvRow()],
    tags: "",
  };
}

function formFromScript(script: ScriptDefinition): ScriptFormState {
  const envEntries = Object.entries(script.env ?? {});
  return {
    id: script.id,
    name: script.name,
    type: script.type,
    content: script.content ?? "",
    path: script.path ?? "",
    description: script.description ?? "",
    timeout: String(script.timeout_s ?? 120),
    env: envEntries.length > 0 ? envEntries.map(([key, value]) => newEnvRow(key, value)) : [newEnvRow()],
    tags: (script.tags ?? []).join(", "),
  };
}

function envRecord(rows: EnvRow[]): Record<string, string> | false {
  const env: Record<string, string> = {};
  for (const row of rows) {
    const key = row.key.trim();
    const value = row.value.trim();
    if (!key && !value) continue;
    if (!key || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) return false;
    env[key] = value;
  }
  return env;
}

/* -------------------------------------------------------------------------- */
/* Component                                                                   */
/* -------------------------------------------------------------------------- */

export interface ScriptEditorModalProps {
  open: boolean;
  /** `null` creates a new script; a definition edits it. */
  script: ScriptDefinition | null;
  /** Optional definition used to pre-fill a brand-new script (duplication). */
  template?: ScriptDefinition | null;
  /** Existing ids (used to generate a unique one for new scripts). */
  existingIds: string[];
  onClose: () => void;
  onSaved: (script: ScriptDefinition) => void;
}

/** Create/edit dialog for a script definition, including inline content or path. */
export function ScriptEditorModal({
  open,
  script,
  template = null,
  existingIds,
  onClose,
  onSaved,
}: ScriptEditorModalProps) {
  const [form, setForm] = useState<ScriptFormState>(emptyForm);
  const [errors, setErrors] = useState<ScriptFormErrors>({});
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!open) return;
    setForm(script ? formFromScript(script) : template ? formFromTemplate(template) : emptyForm());
    setErrors({});
    setSubmitError(null);
    setSubmitting(false);
  }, [open, script, template]);

  const update = (patch: Partial<ScriptFormState>): void => {
    setForm((current) => ({ ...current, ...patch }));
  };

  const updateEnv = (uid: number, patch: Partial<Pick<EnvRow, "key" | "value">>): void => {
    setForm((current) => ({
      ...current,
      env: current.env.map((row) => (row.uid === uid ? { ...row, ...patch } : row)),
    }));
  };

  const validate = (): { errors: ScriptFormErrors; env: Record<string, string> } => {
    const next: ScriptFormErrors = {};
    if (!form.name.trim()) next.name = "A script name is required.";
    if (!form.content.trim() && !form.path.trim()) {
      next.content = "Provide inline content or a path to a script file.";
    }
    const timeout = Number(form.timeout);
    if (!Number.isInteger(timeout) || timeout < 1 || timeout > 86_400) {
      next.timeout = "Timeout must be a whole number of seconds between 1 and 86400.";
    }
    const env = envRecord(form.env);
    if (env === false) {
      next.env = "Environment variable names must start with a letter or underscore and use only A-Z, 0-9 and _.";
    }
    if (!script && form.id.trim() && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(form.id.trim())) {
      next.id = "Use letters, digits, dots, dashes or underscores (max 64 characters).";
    }
    return { errors: next, env: env === false ? {} : env };
  };

  const handleSubmit = async (): Promise<void> => {
    const { errors: found, env } = validate();
    setErrors(found);
    if (Object.keys(found).length > 0) return;

    const payload: ScriptDefinition = {
      id: script?.id ?? (form.id.trim() || slugifyScriptId(form.name, existingIds)),
      name: form.name.trim(),
      type: form.type,
      description: form.description.trim(),
      timeout_s: Number(form.timeout),
      env,
      tags: parseTags(form.tags),
      path: form.path.trim() ? form.path.trim() : null,
      ...(form.content.trim() ? { content: form.content } : {}),
      ...(script
        ? {
            targets: script.targets ?? [],
            builtin: script.builtin ?? false,
            created_at: script.created_at ?? null,
          }
        : template
          ? { targets: template.targets ?? [] }
          : {}),
    };

    setSubmitting(true);
    setSubmitError(null);
    try {
      const saved = script
        ? await api.updateScript(script.id, payload)
        : await api.createScript(payload);
      onSaved(saved ?? payload);
    } catch (error) {
      setSubmitError(describeError(error));
    } finally {
      setSubmitting(false);
    }
  };

  const typeMeta = scriptTypeMeta(form.type);

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={script ? "Edit script" : "New script"}
      description={
        script
          ? `Update ${script.name}. Runs already in flight keep the definition they started with.`
          : "Define a reusable adb-shell, host-Python or Appium script, then fan it out across running instances."
      }
      size="lg"
      dismissible={!submitting}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <Button
            variant="primary"
            icon={script ? "check" : "plus"}
            loading={submitting}
            onClick={() => void handleSubmit()}
          >
            {script ? "Save changes" : "Create script"}
          </Button>
        </>
      }
    >
      {submitError ? (
        <InlineNotice tone="danger" title="Could not save the script">
          <p>{submitError}</p>
        </InlineNotice>
      ) : null}

      <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-5)" }}>
        <div style={{ display: "grid", gap: "var(--space-4)", gridTemplateColumns: "minmax(0, 1.4fr) minmax(0, 1fr)" }}>
          <Field label="Name" htmlFor="script-editor-name" required error={errors.name}>
            <Input
              id="script-editor-name"
              value={form.name}
              onChange={(event) => update({ name: event.target.value })}
              placeholder="e.g. Check Play Store version"
              autoFocus
            />
          </Field>
          <Field
            label="Script ID"
            htmlFor="script-editor-id"
            hint={script ? "The id is stable and cannot be renamed." : "Auto-generated from the name when left blank."}
            error={errors.id}
          >
            <Input
              id="script-editor-id"
              value={form.id}
              onChange={(event) => update({ id: event.target.value })}
              placeholder="scr-check-play-store"
              disabled={Boolean(script)}
              style={{ fontFamily: "var(--font-mono)" }}
            />
          </Field>
        </div>

        <div style={{ display: "grid", gap: "var(--space-4)", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)" }}>
          <Field label="Type" htmlFor="script-editor-type" hint={typeMeta.hint}>
            <Select
              id="script-editor-type"
              value={form.type}
              onChange={(event) => update({ type: event.target.value as ScriptType })}
              options={SCRIPT_TYPES.map((entry) => ({ value: entry.value, label: entry.label }))}
            />
          </Field>
          <Field
            label="Timeout"
            htmlFor="script-editor-timeout"
            hint="Per instance, in seconds. The run dialog can override it."
            error={errors.timeout}
          >
            <Input
              id="script-editor-timeout"
              type="number"
              min={1}
              max={86_400}
              step={1}
              value={form.timeout}
              onChange={(event) => update({ timeout: event.target.value })}
            />
          </Field>
        </div>

        <Field
          label="Path (alternative to inline content)"
          htmlFor="script-editor-path"
          hint="Relative to the daemon data directory. Provide either inline content below or a path, not both."
        >
          <Input
            id="script-editor-path"
            value={form.path}
            onChange={(event) => update({ path: event.target.value })}
            placeholder="scripts/enumerate_packages.py"
            style={{ fontFamily: "var(--font-mono)" }}
          />
        </Field>

        <Field label="Content" htmlFor="script-editor-content" hint={typeMeta.contentHint} error={errors.content}>
          <Textarea
            id="script-editor-content"
            value={form.content}
            onChange={(event) => update({ content: event.target.value })}
            rows={10}
            spellCheck={false}
            placeholder={"# Inline script content\npm list packages | wc -l"}
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: "var(--fs-xs)",
              lineHeight: 1.6,
              minHeight: 200,
              tabSize: 2,
            }}
          />
        </Field>

        <Field label="Description" htmlFor="script-editor-description">
          <Input
            id="script-editor-description"
            value={form.description}
            onChange={(event) => update({ description: event.target.value })}
            placeholder="What the script verifies and why it exists"
          />
        </Field>

        <fieldset className="ds-field" style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="ds-field__label">Environment variables</legend>
          <p className="ds-field__hint">
            Exported to the script process in addition to the <code>AVDFLEET_*</code> instance metadata
            (serial, instance name, ports and output directory).
          </p>
          <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
            {form.env.map((row, index) => (
              <div key={row.uid} style={{ display: "flex", gap: "var(--space-2)", alignItems: "center" }}>
                <Input
                  aria-label={`Environment variable ${index + 1} name`}
                  value={row.key}
                  onChange={(event) => updateEnv(row.uid, { key: event.target.value })}
                  placeholder="KEY"
                  style={{ flex: "1 1 42%", fontFamily: "var(--font-mono)" }}
                />
                <Input
                  aria-label={`Environment variable ${index + 1} value`}
                  value={row.value}
                  onChange={(event) => updateEnv(row.uid, { value: event.target.value })}
                  placeholder="value"
                  style={{ flex: "1 1 58%", fontFamily: "var(--font-mono)" }}
                />
                <IconButton
                  label={`Remove environment variable ${index + 1}`}
                  icon="trash"
                  size="sm"
                  onClick={() => update({ env: form.env.filter((candidate) => candidate.uid !== row.uid) })}
                />
              </div>
            ))}
          </div>
          {errors.env ? (
            <p className="ds-field__error" role="alert">
              {errors.env}
            </p>
          ) : null}
          <div>
            <Button
              size="sm"
              variant="ghost"
              icon="plus"
              onClick={() => update({ env: [...form.env, newEnvRow()] })}
            >
              Add variable
            </Button>
          </div>
        </fieldset>

        <Field
          label="Tags"
          htmlFor="script-editor-tags"
          hint="Comma separated; used for filtering and fleet-spec references."
        >
          <Input
            id="script-editor-tags"
            value={form.tags}
            onChange={(event) => update({ tags: event.target.value })}
            placeholder="inventory, smoke"
          />
        </Field>

        <p className="muted small">
          Runs are fanned out with a bounded concurrency; stdout/stderr streams live over the event
          socket and is captured per instance.
        </p>
      </div>
    </Modal>
  );
}
