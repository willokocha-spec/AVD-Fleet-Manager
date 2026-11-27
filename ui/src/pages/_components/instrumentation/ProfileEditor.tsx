/**
 * Create / edit dialog for an instrumentation profile.
 *
 * The step editor mirrors the backend `StepAction` enum exactly and only offers
 * a module binding where the service actually uses one. `params.expect` is
 * editable for `verify` steps only because that is the only key the backend
 * asserts on (`root`, `magisk`, `zygisk_active`, `lsposed_active`).
 */
import { useEffect, useState } from "react";

import { instrumentationApi } from "@/lib/api";
import {
  Button,
  Checkbox,
  Field,
  IconButton,
  InlineNotice,
  Input,
  Modal,
  Select,
  Textarea,
} from "@/lib/components";
import type { InstrumentationProfile, ModuleRecord, ProfileStep, StepAction, StepBody } from "@/lib/types";

import {
  STEP_ACTIONS,
  apiErrorText,
  isValidPackageName,
  moduleKindLabel,
  parsePackageList,
  stepActionMeta,
  styles,
} from "@/pages/_components/instrumentation/shared";

interface StepDraft {
  key: string;
  /** Existing step id; empty for a new step (the backend generates one). */
  id: string;
  action: StepAction;
  moduleId: string;
  moduleIdentifier: string;
  /** Original params, preserved across edits (only the keys edited here change). */
  params: Record<string, unknown>;
  expectText: string;
  /** `params.package` for the Xposed actions; falls back to module_identifier. */
  xposedPackage: string;
  /** `params.enabled` for enable_xposed_module (the backend defaults to true). */
  xposedEnabled: boolean;
  /** `params.apps` for set_xposed_scope, kept as the operator typed it. */
  scopeAppsText: string;
  /** `params.replace` for set_xposed_scope (the backend defaults to true). */
  scopeReplace: boolean;
  /** `params.timeout_s` for reboot, kept as text (empty means the backend default of 300 s). */
  rebootTimeoutText: string;
}

export interface ProfileEditorDialogProps {
  open: boolean;
  mode: "create" | "edit";
  initial: InstrumentationProfile | null;
  modules: ModuleRecord[];
  onClose: () => void;
  onSaved: (profile: InstrumentationProfile, mode: "create" | "edit") => void;
}

function toDraft(step: ProfileStep, key: string): StepDraft {
  const expect = step.params.expect;
  const scope = Array.isArray(step.params.apps)
    ? step.params.apps.filter((value): value is string => typeof value === "string")
    : [];
  return {
    key,
    id: step.id ?? "",
    action: step.action,
    moduleId: step.module_id ?? "",
    moduleIdentifier: step.module_identifier ?? "",
    params: { ...step.params },
    expectText: expect === undefined ? "" : JSON.stringify(expect, null, 2),
    xposedPackage:
      (typeof step.params.package === "string" ? step.params.package.trim() : "") ||
      step.module_identifier ||
      "",
    xposedEnabled: typeof step.params.enabled === "boolean" ? step.params.enabled : true,
    scopeAppsText: scope.join(", "),
    scopeReplace: typeof step.params.replace === "boolean" ? step.params.replace : true,
    rebootTimeoutText:
      typeof step.params.timeout_s === "number"
        ? String(step.params.timeout_s)
        : typeof step.params.timeout_s === "string"
          ? step.params.timeout_s
          : "",
  };
}

function emptyDraft(): StepDraft {
  return {
    key: `new-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    id: "",
    action: "verify_root",
    moduleId: "",
    moduleIdentifier: "",
    params: {},
    expectText: "",
    xposedPackage: "",
    xposedEnabled: true,
    scopeAppsText: "",
    scopeReplace: true,
    rebootTimeoutText: "",
  };
}

function parseExpect(text: string): { value?: Record<string, unknown>; error?: string } {
  try {
    const parsed: unknown = JSON.parse(text);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return { error: "params.expect must be a JSON object, for example {\"magisk\": true}." };
    }
    return { value: parsed as Record<string, unknown> };
  } catch {
    return { error: "params.expect is not valid JSON." };
  }
}

const EXPECT_KEYS = ["root", "magisk", "zygisk_active", "lsposed_active"];

/**
 * The backend validates `params.package` and every `params.apps` entry with
 * `^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$`; block the same shapes before the
 * profile is sent so the operator gets a clear message instead of a 422.
 */
function packageProblem(value: string, role: "module" | "app"): string | null {
  const candidate = value.trim();
  const example = role === "module" ? "com.example.module" : "com.target.app";
  if (!candidate) {
    return role === "module"
      ? `Enter the Xposed module's package name (for example ${example}).`
      : `Add at least one app package (for example ${example}).`;
  }
  if (!isValidPackageName(candidate)) {
    return `“${candidate}” is not a valid Android package name — use a name like ${example}.`;
  }
  return null;
}

function scopeAppsProblem(text: string): string | null {
  const apps = parsePackageList(text);
  if (apps.length === 0) {
    return "Add at least one app package to the scope, separated by commas or newlines.";
  }
  for (const app of apps) {
    const problem = packageProblem(app, "app");
    if (problem) return problem;
  }
  return null;
}

export function ProfileEditorDialog({
  open,
  mode,
  initial,
  modules,
  onClose,
  onSaved,
}: ProfileEditorDialogProps) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [drafts, setDrafts] = useState<StepDraft[]>([]);
  const [nameError, setNameError] = useState<string | undefined>();
  const [stepsError, setStepsError] = useState<string | undefined>();
  const [stepErrors, setStepErrors] = useState<Record<string, string>>({});
  const [packageErrors, setPackageErrors] = useState<Record<string, string>>({});
  const [scopeErrors, setScopeErrors] = useState<Record<string, string>>({});
  const [timeoutErrors, setTimeoutErrors] = useState<Record<string, string>>({});
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) return;
    setName(initial?.name ?? "");
    setDescription(initial?.description ?? "");
    setDrafts(
      initial && initial.steps.length > 0
        ? initial.steps.map((step, index) => toDraft(step, `step-${index}-${step.id || step.action}`))
        : [emptyDraft()],
    );
    setNameError(undefined);
    setStepsError(undefined);
    setStepErrors({});
    setPackageErrors({});
    setScopeErrors({});
    setTimeoutErrors({});
    setSubmitError(null);
    setSaving(false);
  }, [open, initial]);

  const updateDraft = (key: string, patch: Partial<StepDraft>): void => {
    setDrafts((current) =>
      current.map((draft) => (draft.key === key ? { ...draft, ...patch } : draft)),
    );
  };

  const changeAction = (key: string, action: StepAction): void => {
    const meta = stepActionMeta(action);
    setDrafts((current) =>
      current.map((draft) =>
        draft.key === key
          ? {
              ...draft,
              action,
              moduleId: meta.needsModule ? draft.moduleId : "",
              moduleIdentifier: meta.needsModule ? draft.moduleIdentifier : "",
            }
          : draft,
      ),
    );
  };

  const moveStep = (index: number, delta: number): void => {
    setDrafts((current) => {
      const target = index + delta;
      if (target < 0 || target >= current.length) return current;
      const next = [...current];
      const [item] = next.splice(index, 1);
      if (!item) return current;
      next.splice(target, 0, item);
      return next;
    });
  };

  const removeStep = (key: string): void => {
    setDrafts((current) => current.filter((draft) => draft.key !== key));
  };

  const validate = (): boolean => {
    let valid = true;
    if (!name.trim()) {
      setNameError("A profile needs a name.");
      valid = false;
    } else {
      setNameError(undefined);
    }
    if (drafts.length === 0) {
      setStepsError("A profile needs at least one step.");
      valid = false;
    } else {
      setStepsError(undefined);
    }
    const nextStepErrors: Record<string, string> = {};
    const nextPackageErrors: Record<string, string> = {};
    const nextScopeErrors: Record<string, string> = {};
    const nextTimeoutErrors: Record<string, string> = {};
    for (const draft of drafts) {
      const meta = stepActionMeta(draft.action);
      if (meta.needsModule && !draft.moduleId && !draft.moduleIdentifier) {
        nextStepErrors[draft.key] = "Choose a library module for this step.";
        valid = false;
      }
      if (meta.needsExpect && draft.expectText.trim()) {
        const parsed = parseExpect(draft.expectText);
        if (parsed.error) {
          nextStepErrors[draft.key] = parsed.error;
          valid = false;
        }
      }
      if (meta.needsTimeout) {
        const text = draft.rebootTimeoutText.trim();
        if (text && (!/^\d+$/.test(text) || Number(text) <= 0)) {
          nextTimeoutErrors[draft.key] =
            "Boot timeout must be a whole number of seconds greater than zero.";
          valid = false;
        }
      }
      if (draft.action === "enable_xposed_module" || draft.action === "set_xposed_scope") {
        const problem = packageProblem(draft.xposedPackage, "module");
        if (problem) {
          nextPackageErrors[draft.key] = problem;
          valid = false;
        }
      }
      if (draft.action === "set_xposed_scope") {
        const problem = scopeAppsProblem(draft.scopeAppsText);
        if (problem) {
          nextScopeErrors[draft.key] = problem;
          valid = false;
        }
      }
    }
    setStepErrors(nextStepErrors);
    setPackageErrors(nextPackageErrors);
    setScopeErrors(nextScopeErrors);
    setTimeoutErrors(nextTimeoutErrors);
    return valid;
  };

  const buildSteps = (): StepBody[] =>
    drafts.map((draft) => {
      const meta = stepActionMeta(draft.action);
      const params: Record<string, unknown> = { ...draft.params };
      if (meta.needsExpect) {
        const trimmed = draft.expectText.trim();
        const parsed = trimmed ? parseExpect(trimmed) : {};
        if (parsed.value) params.expect = parsed.value;
        else delete params.expect;
      } else {
        delete params.expect;
      }
      if (meta.needsTimeout) {
        const timeout = draft.rebootTimeoutText.trim();
        if (timeout) params.timeout_s = Number(timeout);
        else delete params.timeout_s;
      } else {
        delete params.timeout_s;
      }
      if (draft.action === "enable_xposed_module") {
        params.package = draft.xposedPackage.trim();
        params.enabled = draft.xposedEnabled;
        delete params.apps;
        delete params.replace;
      } else if (draft.action === "set_xposed_scope") {
        params.package = draft.xposedPackage.trim();
        params.apps = parsePackageList(draft.scopeAppsText);
        params.replace = draft.scopeReplace;
        delete params.enabled;
      }
      const body: StepBody = {
        action: draft.action,
        module_id: meta.needsModule ? draft.moduleId : "",
        module_identifier: meta.needsModule ? draft.moduleIdentifier : "",
        params,
      };
      if (draft.id) body.id = draft.id;
      return body;
    });

  const submit = async (): Promise<void> => {
    if (!validate()) return;
    setSaving(true);
    setSubmitError(null);
    try {
      const body = { name: name.trim(), description: description.trim(), steps: buildSteps() };
      const saved =
        mode === "edit" && initial
          ? await instrumentationApi.updateProfile(initial.id, body)
          : await instrumentationApi.createProfile(body);
      onSaved(saved, mode);
    } catch (caught) {
      setSubmitError(apiErrorText(caught));
    } finally {
      setSaving(false);
    }
  };

  const expectWarning = (text: string): string | null => {
    if (!text.trim()) return null;
    const parsed = parseExpect(text);
    if (parsed.error || !parsed.value) return null;
    const unknown = Object.keys(parsed.value).filter((key) => !EXPECT_KEYS.includes(key));
    if (unknown.length === 0) return null;
    return `The backend only asserts root, magisk, zygisk_active and lsposed_active; ignored key(s): ${unknown.join(", ")}.`;
  };

  const readOnly = Boolean(initial?.builtin);

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={mode === "edit" ? "Edit profile" : "New profile"}
      description={
        mode === "edit"
          ? "Saving increments the profile revision. Runs already started keep the steps they were created with."
          : "A profile is an ordered list of instrumentation steps deployed to the selected AVDs."
      }
      size="lg"
      dismissible={!saving}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button
            variant="primary"
            icon="check"
            loading={saving}
            disabled={readOnly}
            title={
              readOnly
                ? "Built-in profiles are read-only — the API rejects edits with HTTP 422. Duplicate it first."
                : undefined
            }
            onClick={() => void submit()}
          >
            {mode === "edit" ? "Save profile" : "Create profile"}
          </Button>
        </>
      }
    >
      <div style={styles.stack}>
        {readOnly ? (
          <InlineNotice tone="warning" title="Built-in profile">
            <p>
              This profile ships with AVD Fleet and cannot be edited (the API answers HTTP 422). Use{" "}
              <strong>Duplicate</strong> in the Profiles tab and edit the copy.
            </p>
          </InlineNotice>
        ) : null}

        {submitError ? (
          <InlineNotice tone="danger" title="Could not save the profile">
            <p>{submitError}</p>
          </InlineNotice>
        ) : null}

        <div style={styles.fieldGrid}>
          <Field label="Name" required error={nameError}>
            <Input
              value={name}
              placeholder="LSPosed on API 35 Play images"
              invalid={Boolean(nameError)}
              onChange={(event) => setName(event.target.value)}
            />
          </Field>
          <Field label="Description" hint="Optional; shown on the profile list.">
            <Input
              value={description}
              placeholder="Magisk → Zygisk → LSPosed → verify"
              onChange={(event) => setDescription(event.target.value)}
            />
          </Field>
        </div>

        <div style={styles.stackTight}>
          <div style={styles.rowBetween}>
            <span style={styles.labels}>Steps ({drafts.length})</span>
            <Button size="sm" variant="secondary" icon="plus" onClick={() => setDrafts((current) => [...current, emptyDraft()])}>
              Add step
            </Button>
          </div>
          {stepsError ? (
            <p className="muted small" style={{ margin: 0, color: "var(--danger)" }} role="alert">
              {stepsError}
            </p>
          ) : null}

          {drafts.map((draft, index) => {
            const meta = stepActionMeta(draft.action);
            const warning = meta.needsExpect ? expectWarning(draft.expectText) : null;
            const eligibleModules = modules.filter(
              (module) =>
                !meta.moduleKind || module.kind === meta.moduleKind || module.id === draft.moduleId,
            );
            const boundModule = modules.find((module) => module.id === draft.moduleId);
            const kindMismatch =
              Boolean(meta.moduleKind) && Boolean(boundModule) && boundModule?.kind !== meta.moduleKind;
            return (
              <div
                key={draft.key}
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
                  <span className="muted small">
                    Step {index + 1}
                    {draft.id ? ` · ${draft.id}` : " · new"}
                  </span>
                  <div style={styles.rowTight}>
                    <IconButton
                      label={`Move step ${index + 1} up`}
                      icon="chevron-up"
                      size="sm"
                      disabled={index === 0}
                      title={index === 0 ? "This is already the first step" : `Move step ${index + 1} up`}
                      style={index === 0 ? styles.controlDisabled : undefined}
                      onClick={() => moveStep(index, -1)}
                    />
                    <IconButton
                      label={`Move step ${index + 1} down`}
                      icon="chevron-down"
                      size="sm"
                      disabled={index === drafts.length - 1}
                      title={
                        index === drafts.length - 1
                          ? "This is already the last step"
                          : `Move step ${index + 1} down`
                      }
                      style={index === drafts.length - 1 ? styles.controlDisabled : undefined}
                      onClick={() => moveStep(index, 1)}
                    />
                    <IconButton
                      label={`Remove step ${index + 1}`}
                      icon="trash"
                      size="sm"
                      variant="danger"
                      onClick={() => removeStep(draft.key)}
                    />
                  </div>
                </div>

                <div style={styles.fieldGrid}>
                  <Field label="Action" hint={meta.hint}>
                    <Select
                      value={draft.action}
                      options={STEP_ACTIONS.map((candidate) => ({
                        value: candidate.value,
                        label: candidate.label,
                        disabled: candidate.unsupported === true,
                      }))}
                      onChange={(event) => changeAction(draft.key, event.target.value as StepAction)}
                    />
                  </Field>

                  {meta.needsModule ? (
                    <Field
                      label="Module from the library"
                      error={stepErrors[draft.key]}
                      hint={
                        meta.moduleKind
                          ? `Requires a ${moduleKindLabel(meta.moduleKind)} record from the library.`
                          : "Any library module the action can install or toggle."
                      }
                    >
                      <Select
                        value={draft.moduleId}
                        placeholder={
                          modules.length === 0
                            ? "The module library is empty"
                            : eligibleModules.length === 0
                              ? `No ${moduleKindLabel(meta.moduleKind ?? "unknown")} in the library`
                              : "Choose a module"
                        }
                        invalid={Boolean(stepErrors[draft.key])}
                        options={eligibleModules.map((module) => ({
                          value: module.id,
                          label: `${module.name || module.package_name || module.id} · ${moduleKindLabel(
                            module.kind,
                          )}`,
                        }))}
                        onChange={(event) =>
                          updateDraft(draft.key, {
                            moduleId: event.target.value,
                            moduleIdentifier: "",
                          })
                        }
                      />
                    </Field>
                  ) : null}

                  {draft.action === "enable_xposed_module" ||
                  draft.action === "set_xposed_scope" ? (
                    <Field
                      label="Xposed module package"
                      error={packageErrors[draft.key]}
                      hint="The package name LSPosed knows this module by, e.g. com.example.module."
                    >
                      <Input
                        value={draft.xposedPackage}
                        placeholder="com.example.module"
                        invalid={Boolean(packageErrors[draft.key])}
                        onChange={(event) =>
                          updateDraft(draft.key, { xposedPackage: event.target.value })
                        }
                      />
                    </Field>
                  ) : null}
                </div>

                {draft.action === "enable_xposed_module" ? (
                  <Checkbox
                    label="Enabled"
                    hint="Writes LSPosed's enabled flag for this package. lspd reloads its configuration only after the AVD reboots."
                    checked={draft.xposedEnabled}
                    onChange={(event) =>
                      updateDraft(draft.key, { xposedEnabled: event.target.checked })
                    }
                  />
                ) : null}

                {draft.action === "set_xposed_scope" ? (
                  <div style={styles.stackTight}>
                    <Field
                      label="Scope app packages"
                      error={scopeErrors[draft.key]}
                      hint="Comma- or newline-separated application packages, e.g. com.target.app, com.other.app."
                    >
                      <Textarea
                        rows={3}
                        value={draft.scopeAppsText}
                        placeholder={"com.target.app\ncom.other.app"}
                        invalid={Boolean(scopeErrors[draft.key])}
                        onChange={(event) =>
                          updateDraft(draft.key, { scopeAppsText: event.target.value })
                        }
                      />
                    </Field>
                    <Checkbox
                      label="Replace the existing scope"
                      hint="On: the listed apps replace the module's current scope. Off: they are added to it. lspd reloads the scope only after the AVD reboots."
                      checked={draft.scopeReplace}
                      onChange={(event) =>
                        updateDraft(draft.key, { scopeReplace: event.target.checked })
                      }
                    />
                    <div style={styles.subtext}>
                      This build has no API that lists a device&apos;s packages, so there is no picker here.
                      Fill the list from <code>adb shell pm list packages</code> (or read the installed apps in
                      the LSPosed manager on the device).
                    </div>
                  </div>
                ) : null}

                {draft.action === "reboot" ? (
                  <Field
                    label="Boot timeout (seconds)"
                    error={timeoutErrors[draft.key]}
                    hint="Optional; defaults to 300 s. The step reboots the AVD and waits for sys.boot_completed, so a slow boot fails the step rather than the run."
                  >
                    <Input
                      inputMode="numeric"
                      value={draft.rebootTimeoutText}
                      placeholder="300"
                      invalid={Boolean(timeoutErrors[draft.key])}
                      onChange={(event) =>
                        updateDraft(draft.key, { rebootTimeoutText: event.target.value })
                      }
                    />
                  </Field>
                ) : null}

                {draft.action === "remove_magisk" ? (
                  <div style={styles.warning}>
                    The backend always reports this step as unsupported: Magisk lives in the image ramdisk.
                    Stop the AVD, restore <code>ramdisk.img</code> from <code>ramdisk.img.backup</code> (rootAVD
                    keeps it) and reinstall the image if needed.
                  </div>
                ) : null}

                {draft.action === "enable_xposed_module" ||
                draft.action === "set_xposed_scope" ? (
                  <div style={styles.subtext}>
                    LSPosed keeps this configuration in its own database and reloads it only after the AVD
                    reboots. Add a <code>verify</code> step after the reboot to check{" "}
                    <code>lsposed_active</code>.
                  </div>
                ) : null}

                {draft.action === "complete_magisk_setup" ? (
                  <div style={styles.subtext}>
                    Requires the pinned Magisk APK from the Toolchain panel (<code>magisk</code>); the step
                    reboots the AVD when it succeeds.
                  </div>
                ) : null}

                {kindMismatch && boundModule && meta.moduleKind ? (
                  <div style={styles.warning}>
                    This action needs a {moduleKindLabel(meta.moduleKind)} record, but the bound module is a{" "}
                    {moduleKindLabel(boundModule.kind)}. The backend will reject the step.
                  </div>
                ) : null}

                {meta.needsModule && draft.moduleIdentifier ? (
                  <p className="muted small" style={{ margin: 0 }}>
                    Bound by module identifier <code style={styles.code}>{draft.moduleIdentifier}</code> from the
                    existing profile. Selecting a library module replaces it.
                  </p>
                ) : null}

                {meta.needsModule && modules.length === 0 ? (
                  <p className="muted small" style={{ margin: 0 }}>
                    The library is empty. Import an APK or ZIP on the Module library tab first; the backend fails
                    this step unless a module is bound.
                  </p>
                ) : null}

                {meta.needsModule && modules.length > 0 && eligibleModules.length === 0 ? (
                  <p className="muted small" style={{ margin: 0 }}>
                    The library has no {moduleKindLabel(meta.moduleKind ?? "unknown")} yet. Import one on the
                    Module library tab, or this step will fail.
                  </p>
                ) : null}

                {meta.needsExpect ? (
                  <Field
                    label="params.expect (JSON)"
                    hint='Optional. Only keys set to true are asserted, e.g. {"magisk": true, "lsposed_active": true}.'
                    error={stepErrors[draft.key]}
                  >
                    <Textarea
                      rows={3}
                      value={draft.expectText}
                      placeholder={'{\n  "magisk": true,\n  "zygisk_active": true\n}'}
                      invalid={Boolean(stepErrors[draft.key])}
                      onChange={(event) => updateDraft(draft.key, { expectText: event.target.value })}
                    />
                  </Field>
                ) : null}

                {warning ? <div style={styles.warning}>{warning}</div> : null}
                {meta.needsExpect ? (
                  <div className="muted small" style={styles.subtext}>
                    Known keys: {EXPECT_KEYS.join(", ")}.
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      </div>
    </Modal>
  );
}
