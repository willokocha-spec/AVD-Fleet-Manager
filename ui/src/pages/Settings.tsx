import { Component, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { ApiError, api } from "@/lib/api";
import {
  Badge,
  Button,
  ButtonLink,
  Card,
  Checkbox,
  CodeBlock,
  ConfirmDialog,
  Field,
  Icon,
  InlineNotice,
  Input,
  PageHeader,
  Select,
  Skeleton,
  StatusPill,
  useToast,
  type SelectOption,
} from "@/lib/components";
import { useAsync } from "@/lib/hooks";
import { TOKEN_STORAGE_KEY } from "@/lib/config";
import type { PageMeta } from "@/lib/page-meta";
import { useTheme, type ThemePreference } from "@/lib/theme";
import type { ConfigPatch, ConfigView } from "@/lib/types";
import { humanize } from "@/lib/utils";

import { useAuth } from "@/app/AuthProvider";

export const meta: PageMeta = {
  path: "/settings",
  title: "Settings",
  icon: "settings",
  nav: 80,
  description: "Appearance, daemon configuration and access token",
};

/* -------------------------------------------------------------------------- */
/* Form model                                                                  */
/* -------------------------------------------------------------------------- */

const GPU_MODES = ["auto", "host", "swiftshader_indirect", "swiftshader", "angle_indirect", "off"] as const;
const GPU_LABELS: Record<string, string> = {
  auto: "Auto (host decides)",
  host: "Host GPU",
  swiftshader_indirect: "SwiftShader (indirect)",
  swiftshader: "SwiftShader",
  angle_indirect: "ANGLE (indirect)",
  off: "Off (software rendering)",
};
const THEME_VALUES: ThemePreference[] = ["dark", "light", "system"];
const LOG_LEVELS = ["debug", "info", "warning", "error"] as const;

interface Draft {
  headless: boolean;
  gpu_mode: string;
  max_instances: string;
  default_ram_mb: string;
  theme: string;
  log_level: string;
}

interface DraftErrors {
  gpu_mode?: string;
  max_instances?: string;
  default_ram_mb?: string;
  theme?: string;
  log_level?: string;
}

function isThemePreference(value: unknown): value is ThemePreference {
  return typeof value === "string" && (THEME_VALUES as string[]).includes(value);
}

function draftFromConfig(config: ConfigView): Draft {
  return {
    headless: config.headless ?? false,
    gpu_mode: config.gpu_mode ?? "auto",
    max_instances: config.max_instances !== undefined ? String(config.max_instances) : "1",
    default_ram_mb: config.default_ram_mb !== undefined ? String(config.default_ram_mb) : "2048",
    theme: isThemePreference(config.theme) ? config.theme : "dark",
    // ConfigView does not echo log_level; an empty value means "keep current".
    log_level: "",
  };
}

function validate(draft: Draft): DraftErrors {
  const errors: DraftErrors = {};
  const maxInstances = Number(draft.max_instances);
  if (
    draft.max_instances.trim() === "" ||
    !Number.isInteger(maxInstances) ||
    maxInstances < 1 ||
    maxInstances > 64
  ) {
    errors.max_instances = "Enter a whole number between 1 and 64.";
  }
  const ram = Number(draft.default_ram_mb);
  if (draft.default_ram_mb.trim() === "" || !Number.isInteger(ram) || ram < 512) {
    errors.default_ram_mb = "RAM must be at least 512 MB.";
  }
  if (!draft.gpu_mode.trim()) errors.gpu_mode = "Choose a GPU mode.";
  if (!isThemePreference(draft.theme)) errors.theme = "Choose a theme.";
  if (draft.log_level && !(LOG_LEVELS as readonly string[]).includes(draft.log_level)) {
    errors.log_level = "Choose a supported log level.";
  }
  return errors;
}

function buildPatch(draft: Draft, baseline: Draft): ConfigPatch {
  const patch: ConfigPatch = {};
  if (draft.headless !== baseline.headless) patch.headless = draft.headless;
  if (draft.gpu_mode !== baseline.gpu_mode) patch.gpu_mode = draft.gpu_mode;
  if (draft.max_instances !== baseline.max_instances) {
    patch.max_instances = Number(draft.max_instances);
  }
  if (draft.default_ram_mb !== baseline.default_ram_mb) {
    patch.default_ram_mb = Number(draft.default_ram_mb);
  }
  if (draft.theme !== baseline.theme) patch.theme = draft.theme;
  if (draft.log_level) patch.log_level = draft.log_level;
  return patch;
}

function errorText(error: unknown): string {
  if (!error) return "";
  if (error instanceof ApiError) {
    return [error.message, error.hint].filter(Boolean).join(" — ");
  }
  if (error instanceof Error) return error.message;
  return String(error);
}

function tokenFilePath(dataDir: string | undefined): string {
  if (!dataDir) return "<data directory>/token";
  const separator = dataDir.includes("\\") ? "\\" : "/";
  return `${dataDir.replace(/[\\/]+$/, "")}${separator}token`;
}

/* -------------------------------------------------------------------------- */
/* Page                                                                        */
/* -------------------------------------------------------------------------- */

export default function SettingsPage() {
  const toast = useToast();
  const { theme: localTheme, resolved, setTheme } = useTheme();
  const { data: config, error, loading, reload, setData } = useAsync(() => api.getConfig(), []);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [baseline, setBaseline] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [pendingPatch, setPendingPatch] = useState<ConfigPatch | null>(null);
  /** Local theme to restore when the staged theme change is discarded. */
  const restoreThemeRef = useRef<ThemePreference>(localTheme);

  useEffect(() => {
    if (!config) return;
    const next = draftFromConfig(config);
    setDraft(next);
    setBaseline(next);
  }, [config]);

  const errors = useMemo<DraftErrors>(() => (draft ? validate(draft) : {}), [draft]);
  const hasErrors = Object.keys(errors).length > 0;
  const patch = useMemo<ConfigPatch>(
    () => (draft && baseline ? buildPatch(draft, baseline) : {}),
    [baseline, draft],
  );
  const dirty = Object.keys(patch).length > 0;

  const updateDraft = (values: Partial<Draft>): void => {
    setDraft((current) => (current ? { ...current, ...values } : current));
  };

  const applyTheme = (value: string): void => {
    if (isThemePreference(value)) setTheme(value);
    updateDraft({ theme: value });
  };

  const discard = (): void => {
    if (!baseline) return;
    setDraft(baseline);
    setSaveError(null);
    setPendingPatch(null);
    setTheme(restoreThemeRef.current);
  };

  const savePatch = async (values: ConfigPatch): Promise<void> => {
    setSaving(true);
    setSaveError(null);
    try {
      const updated = await api.updateConfig(values);
      setData(updated);
      restoreThemeRef.current = localTheme;
      toast.success("Configuration saved", "The daemon applied the whitelisted settings.");
    } catch (caught) {
      const message = errorText(caught);
      setSaveError(message);
      toast.error("Save failed", message);
    } finally {
      setSaving(false);
    }
  };

  const requestSave = (): void => {
    if (!draft || !baseline || hasErrors) return;
    const values = buildPatch(draft, baseline);
    if (Object.keys(values).length === 0) {
      toast.info("Nothing to save", "Change a setting first.");
      return;
    }
    const currentLimit = Number(baseline.max_instances);
    if (
      values.max_instances !== undefined &&
      Number.isFinite(currentLimit) &&
      values.max_instances < currentLimit
    ) {
      setPendingPatch(values);
      return;
    }
    void savePatch(values);
  };

  const confirmReduction = async (): Promise<void> => {
    if (!pendingPatch) return;
    await savePatch(pendingPatch);
    setPendingPatch(null);
  };

  const cancelReduction = (): void => setPendingPatch(null);

  const initialLoading = loading && config === null;
  const themeOptions: Array<{ value: ThemePreference; label: string; icon: "moon" | "sun" | "monitor" }> = [
    { value: "dark", label: "Dark", icon: "moon" },
    { value: "light", label: "Light", icon: "sun" },
    { value: "system", label: "System", icon: "monitor" },
  ];

  const gpuOptions = useMemo<SelectOption[]>(() => {
    const options: SelectOption[] = GPU_MODES.map((value) => ({
      value,
      label: GPU_LABELS[value] ?? humanize(value),
    }));
    if (draft && !(GPU_MODES as readonly string[]).includes(draft.gpu_mode)) {
      options.push({ value: draft.gpu_mode, label: `${humanize(draft.gpu_mode)} (reported)` });
    }
    return options;
  }, [draft]);

  return (
    <div className="page">
      <PageHeader
        title="Settings"
        eyebrow="Configuration"
        description="Appearance for this browser, daemon configuration from /api/v1/config, and session access."
        meta={
          <>
            {config?.edition ? (
              <Badge tone="neutral" icon="server">
                {humanize(config.edition)} edition
              </Badge>
            ) : null}
            {config?.version ? (
              <Badge tone="neutral" icon="info">
                daemon v{config.version}
              </Badge>
            ) : null}
            {dirty ? <StatusPill status="warning" label="Unsaved changes" /> : null}
          </>
        }
        actions={
          <ButtonLink to="/doctor" variant="secondary" icon="doctor">
            Environment doctor
          </ButtonLink>
        }
      />

      {error ? (
        <InlineNotice
          tone="danger"
          title="Configuration could not be loaded"
          action={
            <Button size="sm" variant="secondary" icon="refresh" onClick={reload}>
              Retry
            </Button>
          }
        >
          <p>{errorText(error)}</p>
        </InlineNotice>
      ) : null}

      <Card
        title="Appearance"
        description={`Theme preference for this browser. Currently rendering in ${resolved} mode.`}
        icon="sun"
      >
        <div className="theme-options" role="radiogroup" aria-label="Theme preference">
          {themeOptions.map((option) => (
            <button
              key={option.value}
              type="button"
              role="radio"
              aria-checked={localTheme === option.value}
              className={`theme-option${localTheme === option.value ? " is-active" : ""}`}
              onClick={() => applyTheme(option.value)}
            >
              <Icon name={option.icon} size={20} aria-hidden="true" />
              <span className="theme-option__label">{option.label}</span>
              <span className="theme-option__hint">
                {option.value === "system"
                  ? "Follow the OS preference"
                  : `Always use ${option.label.toLowerCase()}`}
              </span>
            </button>
          ))}
        </div>
        <p className="muted small" style={{ marginTop: "var(--space-3)" }}>
          Switching a theme applies it immediately and stages the same value as the daemon&apos;s
          default. Choose <strong>Save changes</strong> to persist it for future consoles.
        </p>
      </Card>

      {initialLoading ? (
        <Card title="Daemon configuration" icon="server">
          <div className="skeleton-card">
            <Skeleton lines={4} />
            <Skeleton width="52%" />
          </div>
        </Card>
      ) : null}

      {draft && baseline ? (
        <Card
          title="Daemon configuration"
          description="Whitelisted settings from GET/PATCH /config. Values are validated here and again by the daemon."
          icon="server"
          footer={
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: "var(--space-3)",
                flexWrap: "wrap",
              }}
            >
              <span className="muted small">
                {dirty
                  ? "You have unsaved changes."
                  : "All daemon settings match the running configuration."}
              </span>
              <div style={{ display: "flex", gap: "var(--space-2)" }}>
                <Button
                  variant="ghost"
                  icon="restart"
                  onClick={discard}
                  disabled={!dirty || saving}
                >
                  Discard
                </Button>
                <Button
                  variant="primary"
                  icon="check"
                  onClick={requestSave}
                  disabled={!dirty || hasErrors || saving}
                  loading={saving}
                >
                  Save changes
                </Button>
              </div>
            </div>
          }
        >
          {saveError ? (
            <InlineNotice tone="danger" title="Configuration not saved">
              <p>{saveError}</p>
            </InlineNotice>
          ) : null}
          <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)", marginTop: saveError ? "var(--space-4)" : 0 }}>
            <div className="grid-2">
              <Field
                label="Max instances"
                hint="Concurrent AVD limit on this host (1–64)."
                error={errors.max_instances}
                required
              >
                <Input
                  type="number"
                  min={1}
                  max={64}
                  step={1}
                  value={draft.max_instances}
                  invalid={Boolean(errors.max_instances)}
                  onChange={(event) => updateDraft({ max_instances: event.target.value })}
                />
              </Field>
              <Field
                label="Default RAM per instance (MB)"
                hint="Used when a template does not override the AVD RAM (minimum 512 MB)."
                error={errors.default_ram_mb}
                required
              >
                <Input
                  type="number"
                  min={512}
                  step={256}
                  value={draft.default_ram_mb}
                  invalid={Boolean(errors.default_ram_mb)}
                  onChange={(event) => updateDraft({ default_ram_mb: event.target.value })}
                />
              </Field>
              <Field label="GPU mode" hint="Default emulator GPU backend for new instances." error={errors.gpu_mode}>
                <Select
                  value={draft.gpu_mode}
                  invalid={Boolean(errors.gpu_mode)}
                  options={gpuOptions}
                  onChange={(event) => updateDraft({ gpu_mode: event.target.value })}
                />
              </Field>
              <Field
                label="Daemon theme default"
                hint="Applied to this browser immediately; persisted for future consoles with Save."
                error={errors.theme}
              >
                <Select
                  value={draft.theme}
                  invalid={Boolean(errors.theme)}
                  options={THEME_VALUES.map((value) => ({
                    value,
                    label: humanize(value),
                  }))}
                  onChange={(event) => applyTheme(event.target.value)}
                />
              </Field>
              <Field
                label="Log level"
                hint="The API never echoes the current level; pick a value to stage an update."
                error={errors.log_level}
              >
                <Select
                  value={draft.log_level}
                  placeholder="Keep current"
                  invalid={Boolean(errors.log_level)}
                  options={LOG_LEVELS.map((value) => ({ value, label: humanize(value) }))}
                  onChange={(event) => updateDraft({ log_level: event.target.value })}
                />
              </Field>
            </div>
            <Checkbox
              label="Headless by default"
              hint="Start emulators without a visible window; required for most server hosts."
              checked={draft.headless}
              onChange={(event) => updateDraft({ headless: event.target.checked })}
            />
          </div>
        </Card>
      ) : null}

      <div className="grid-2">
        <Card
          title="Daemon"
          description="Read-only identity and filesystem facts from the configuration view."
          icon="server"
        >
          {config ? (
            <dl className="kv-list">
              <div className="kv-list__row">
                <dt>Edition</dt>
                <dd>{config.edition ? humanize(config.edition) : "—"}</dd>
              </div>
              <div className="kv-list__row">
                <dt>Version</dt>
                <dd>{config.version ? `v${config.version}` : "—"}</dd>
              </div>
              <div className="kv-list__row">
                <dt>API listen</dt>
                <dd>
                  <code>
                    {config.api_host ?? "127.0.0.1"}:{config.api_port ?? "—"}
                  </code>
                </dd>
              </div>
              <div className="kv-list__row">
                <dt>Data directory</dt>
                <dd>
                  <code>{config.data_dir ?? "—"}</code>
                </dd>
              </div>
              <div className="kv-list__row">
                <dt>SDK root</dt>
                <dd>
                  <code>{config.sdk_root ?? "—"}</code>
                </dd>
              </div>
              <div className="kv-list__row">
                <dt>Authentication</dt>
                <dd>
                  <Badge tone={config.auth_required ? "success" : "warning"} dot>
                    {config.auth_required ? "Bearer token required" : "Auth disabled"}
                  </Badge>
                </dd>
              </div>
            </dl>
          ) : (
            <div className="skeleton-card">
              <Skeleton lines={5} />
            </div>
          )}
        </Card>

        <OptionalAuthBoundary>
          <AccessCard dataDir={config?.data_dir} />
        </OptionalAuthBoundary>
      </div>

      <Card
        title="Danger zone"
        description="Uninstall and repair live in the CLI, not in the GUI."
        icon="alert-triangle"
        tone="muted"
      >
        <p className="small" style={{ marginBottom: "var(--space-3)" }}>
          Repair and uninstall rewrite the host toolchain (Android SDK, JDK, Node, Appium and
          virtualisation features), can require elevation and may need a reboot. They are
          intentionally not exposed by the API, so the web console cannot trigger them.
        </p>
        <CodeBlock
          filename="Terminal (CLI)"
          language="shell"
          code={`# Re-check and repair the installed toolchain
avdfleet repair

# Preview what uninstall would remove (no changes)
avdfleet uninstall --dry-run

# Remove the toolchain but keep AVDs and daemon state
avdfleet uninstall --keep-data

# Remove installer-managed files and the data directory
avdfleet uninstall`}
        />
        <p className="muted small" style={{ margin: "var(--space-3) 0" }}>
          The Doctor page can still apply the individual checks the daemon marks as fixable.
        </p>
        <ButtonLink to="/doctor" variant="secondary" icon="doctor">
          Open doctor
        </ButtonLink>
      </Card>

      <ConfirmDialog
        open={pendingPatch !== null}
        title="Reduce max instances?"
        description="This lowers the concurrent AVD limit reported by the daemon."
        confirmLabel="Reduce limit"
        tone="danger"
        onCancel={cancelReduction}
        onConfirm={confirmReduction}
      >
        <p>
          Change the limit from <strong>{baseline?.max_instances}</strong> to{" "}
          <strong>{pendingPatch?.max_instances}</strong>?
        </p>
        <p className="muted small">
          New deployments and starts above the limit will be refused by the daemon. Review the
          capacity panel on the dashboard before lowering it.
        </p>
      </ConfirmDialog>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Access card (requires the auth provider)                                    */
/* -------------------------------------------------------------------------- */

const MASKED_TOKEN = "••••••••••••";

function AccessCard({ dataDir }: { dataDir?: string }) {
  const auth = useAuth();
  const tokenFile = tokenFilePath(dataDir);
  return (
    <Card
      title="Access & token"
      description="The console authenticates with a daemon-issued bearer token."
      icon="key"
    >
      <dl className="kv-list">
        <div className="kv-list__row">
          <dt>Session</dt>
          <dd>
            <StatusPill status="authenticated" label="Bearer token accepted" icon="key" />
          </dd>
        </div>
        <div className="kv-list__row">
          <dt>Stored token</dt>
          <dd>
            <code aria-label="masked token value">{MASKED_TOKEN}</code>{" "}
            <Badge tone="neutral">masked</Badge>
          </dd>
        </div>
        <div className="kv-list__row">
          <dt>Browser storage</dt>
          <dd>
            <code>localStorage["{TOKEN_STORAGE_KEY}"]</code>
          </dd>
        </div>
        <div className="kv-list__row">
          <dt>Token file</dt>
          <dd>
            <code>{tokenFile}</code>
          </dd>
        </div>
        <div className="kv-list__row">
          <dt>Transport</dt>
          <dd>
            <code>Authorization: Bearer</code> · WebSocket <code>?token=</code>
          </dd>
        </div>
      </dl>
      <InlineNotice tone="info" title="The token value is never displayed">
        <p>
          The daemon stores the opaque token at <code>{tokenFile}</code> with owner-only
          permissions, or reads <code>AVDFLEET_TOKEN</code> when that environment variable is set.
          To rotate the token, stop the daemon, remove the file and start it again — then
          reconnect from the connect screen.
        </p>
      </InlineNotice>
      <div style={{ marginTop: "var(--space-3)" }}>
        <Button variant="secondary" icon="logout" onClick={auth.signOut}>
          Sign out
        </Button>
      </div>
    </Card>
  );
}

interface OptionalAuthBoundaryState {
  failed: boolean;
}

/**
 * Isolated page renders (for example the registry smoke test) mount pages
 * without the app's auth provider. The access card is the only part of this
 * page that needs it, so contain that failure instead of failing the page.
 */
class OptionalAuthBoundary extends Component<{ children: ReactNode }, OptionalAuthBoundaryState> {
  state: OptionalAuthBoundaryState = { failed: false };

  static getDerivedStateFromError(): OptionalAuthBoundaryState {
    return { failed: true };
  }

  componentDidCatch(): void {
    /* Rendered outside the authenticated console; the card stays hidden. */
  }

  render(): ReactNode {
    return this.state.failed ? null : this.props.children;
  }
}
