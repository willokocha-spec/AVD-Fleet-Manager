/**
 * Shared presentation helpers for the Root & Instrumentation page.
 *
 * Page-private: this directory is outside the `src/pages/*.tsx` route glob, so
 * nothing here is ever registered as a page. Only the U0 design system plus a
 * small amount of inline layout CSS is used — no new stylesheet.
 *
 * The UI keeps *installed*, *enabled* and *active* apart on purpose: the probe
 * reports them separately, and a file transfer must never be presented as a
 * working framework.
 */
import { useMemo, useState, type CSSProperties, type ReactNode } from "react";

import { ApiError, instrumentationApi } from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  Field,
  IconButton,
  InlineNotice,
  Select,
  StatusPill,
  Table,
  useToast,
  type BadgeTone,
  type Column,
} from "@/lib/components";
import type {
  ComponentState,
  DeploymentRun,
  DeviceInstrumentation,
  InstalledModule,
  InstrumentationProfile,
  ModuleKind,
  ProfileStep,
  RootImplementation,
  StepAction,
  TargetResult,
} from "@/lib/types";
import { formatDateTime, humanize, pluralize } from "@/lib/utils";

const css = (value: CSSProperties): CSSProperties => value;

export const styles = {
  stack: css({ display: "flex", flexDirection: "column", gap: "var(--space-4)" }),
  stackTight: css({ display: "flex", flexDirection: "column", gap: "var(--space-3)" }),
  rowTight: css({ display: "flex", alignItems: "center", gap: "var(--space-2)", flexWrap: "wrap" }),
  rowBetween: css({
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: "var(--space-3)",
    flexWrap: "wrap",
  }),
  toolbar: css({
    display: "flex",
    alignItems: "flex-end",
    gap: "var(--space-3)",
    flexWrap: "wrap",
  }),
  actions: css({
    display: "flex",
    alignItems: "center",
    justifyContent: "flex-end",
    gap: "var(--space-1)",
    flexWrap: "nowrap",
  }),
  fieldGrid: css({
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
    gap: "var(--space-4)",
  }),
  capabilityGrid: css({
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(210px, 1fr))",
    gap: "var(--space-4)",
  }),
  cardGrid: css({
    display: "grid",
    gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))",
    gap: "var(--space-4)",
  }),
  subtext: css({ color: "var(--text-muted)", fontSize: "var(--fs-xs)", lineHeight: 1.5 }),
  warning: css({ color: "var(--warning)", fontSize: "var(--fs-xs)", lineHeight: 1.5 }),
  bulletList: css({ margin: 0, paddingLeft: "1.1rem", display: "grid", gap: "2px" }),
  code: css({
    fontFamily: "var(--font-mono)",
    fontSize: "var(--fs-xs)",
    color: "var(--text-secondary)",
    overflowWrap: "anywhere",
  }),
  scrollBox: css({
    display: "flex",
    flexDirection: "column",
    gap: "var(--space-2)",
    maxHeight: "320px",
    overflowY: "auto",
    padding: "var(--space-3)",
    border: "1px solid var(--border)",
    borderRadius: "var(--radius-sm)",
    background: "var(--surface-sunken)",
  }),
  labels: css({
    fontFamily: "var(--font-mono)",
    fontSize: "var(--fs-2xs)",
    letterSpacing: "0.06em",
    textTransform: "uppercase",
    color: "var(--text-subtle)",
  }),
  /** IconButton has no disabled styling in the design system; dim it explicitly. */
  controlDisabled: css({ opacity: 0.45 }),
} as const;

/** Message shown for a failed API call: `ApiError.message` plus its hint. */
export function apiErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    return [error.message, error.hint].filter(Boolean).join(" — ");
  }
  if (error instanceof Error) return error.message;
  return String(error);
}

/* -------------------------------------------------------------------------- */
/* Vocabulary                                                                  */
/* -------------------------------------------------------------------------- */

export const BUILTIN_ROOT_PROFILE = "prof_builtin_root";
export const BUILTIN_MAGISK_PROFILE = "prof_builtin_magisk_zygisk";
export const BUILTIN_LSPOSED_PROFILE = "prof_builtin_lsposed";
export const BUILTIN_XPOSED_PROFILE = "prof_builtin_xposed";

export const MODULE_KINDS: ModuleKind[] = [
  "xposed_apk",
  "magisk_zip",
  "zygisk_zip",
  "lsposed_zip",
  "unknown",
];

const MODULE_KIND_LABEL: Record<ModuleKind, string> = {
  xposed_apk: "Xposed APK",
  magisk_zip: "Magisk ZIP",
  zygisk_zip: "Zygisk ZIP",
  lsposed_zip: "LSPosed ZIP",
  unknown: "Unknown artifact",
};

const MODULE_KIND_TONE: Record<ModuleKind, BadgeTone> = {
  xposed_apk: "info",
  magisk_zip: "neutral",
  zygisk_zip: "accent",
  lsposed_zip: "success",
  unknown: "warning",
};

export function moduleKindLabel(kind: ModuleKind): string {
  return MODULE_KIND_LABEL[kind] ?? humanize(kind);
}

export function ModuleKindBadge({ kind }: { kind: ModuleKind }) {
  return (
    <Badge
      tone={MODULE_KIND_TONE[kind] ?? "neutral"}
      title={
        kind === "xposed_apk"
          ? "An Android APK. Installing it does not activate it; enable and scope it with the Xposed steps (or in the LSPosed manager)."
          : kind === "unknown"
            ? "The backend could not classify this artifact; module steps will reject it."
            : "A Magisk module archive."
      }
    >
      {moduleKindLabel(kind)}
    </Badge>
  );
}

/**
 * The exact package shape the backend enforces for `params.package` and every
 * entry of `params.apps` (`service._PACKAGE_PATTERN`). Validating client-side
 * mirrors the daemon so an invalid value is blocked before the 422.
 */
export const ANDROID_PACKAGE_PATTERN = /^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$/;

export function isValidPackageName(value: string): boolean {
  return ANDROID_PACKAGE_PATTERN.test(value.trim());
}

/** Split a comma/newline separated package list into unique, trimmed entries. */
export function parsePackageList(value: string): string[] {
  const seen = new Set<string>();
  const packages: string[] = [];
  for (const part of value.split(/[,\n]/)) {
    const candidate = part.trim();
    if (!candidate || seen.has(candidate)) continue;
    seen.add(candidate);
    packages.push(candidate);
  }
  return packages;
}

const STATE_TONE: Record<ComponentState, BadgeTone> = {
  absent: "neutral",
  installed: "info",
  enabled: "warning",
  active: "success",
  unsupported: "danger",
  unknown: "neutral",
};

const STATE_LABEL: Record<ComponentState, string> = {
  absent: "absent",
  installed: "installed",
  enabled: "enabled",
  active: "active",
  unsupported: "unsupported",
  unknown: "unknown",
};

/** One lifecycle state as its own badge — never collapses installed/enabled/active. */
export function StateBadge({
  state,
  label,
  title,
}: {
  state: ComponentState;
  label?: string;
  title?: string;
}) {
  const text = `${label ? `${label}: ` : ""}${STATE_LABEL[state]}`;
  return (
    <Badge tone={STATE_TONE[state]} dot title={title ?? text}>
      {text}
    </Badge>
  );
}

export function RootBadge({ implementation }: { implementation: RootImplementation }) {
  if (implementation === "magisk") {
    return (
      <Badge tone="success" dot title="Magisk root: su is available to applications.">
        Root: Magisk
      </Badge>
    );
  }
  if (implementation === "adb_root") {
    return (
      <Badge
        tone="warning"
        dot
        title="adb root restarts adbd as uid 0 for userdebug images. It is host-side root only — no su manager, no Zygisk, no LSPosed."
      >
        Root: adb root
      </Badge>
    );
  }
  return (
    <Badge tone="neutral" dot title="No root was reported by the probe.">
      Root: none
    </Badge>
  );
}

export function YesNoBadge({
  label,
  value,
  yesTone = "success",
  title,
}: {
  label: string;
  value: boolean;
  yesTone?: BadgeTone;
  title?: string;
}) {
  return (
    <Badge tone={value ? yesTone : "neutral"} dot title={title ?? `${label}: ${value ? "yes" : "no"}`}>
      {`${label}: ${value ? "yes" : "no"}`}
    </Badge>
  );
}

/* -------------------------------------------------------------------------- */
/* Step catalogue                                                              */
/* -------------------------------------------------------------------------- */

export interface StepActionMeta {
  value: StepAction;
  label: string;
  hint: string;
  /** The backend fails the step without a library module / module identifier. */
  needsModule: boolean;
  /** The backend requires this module kind for the step. */
  moduleKind?: ModuleKind;
  /** `params.expect` is meaningful for this action. */
  needsExpect?: boolean;
  /** `params.timeout_s` is meaningful for this action. */
  needsTimeout?: boolean;
  /** The backend always reports this action as unsupported; shown, never offered. */
  unsupported?: boolean;
}

export const STEP_ACTIONS: StepActionMeta[] = [
  {
    value: "verify_root",
    label: "Verify root",
    hint: "Probe the device and check that root is available (Magisk or userdebug adb root).",
    needsModule: false,
  },
  {
    value: "install_magisk",
    label: "Install Magisk",
    hint: "Delegates to a configured external installer. No installer is bundled (Magisk is GPL), so the backend fails this step with a configuration error until one is configured.",
    needsModule: false,
  },
  {
    value: "complete_magisk_setup",
    label: "Complete Magisk setup",
    hint: "Installs the acquired Magisk APK, drives the app's “Additional Setup” dialogs and reboots the AVD; until it completes, magiskd has no /data/adb and module scripts do not run.",
    needsModule: false,
  },
  {
    value: "enable_zygisk",
    label: "Enable Zygisk",
    hint: "Persists the Magisk Zygisk setting. It only becomes active after a reboot; run a verify step afterwards.",
    needsModule: false,
  },
  {
    value: "disable_zygisk",
    label: "Disable Zygisk",
    hint: "Persists the disabled setting; reboot to apply.",
    needsModule: false,
  },
  {
    value: "install_lsposed",
    label: "Install LSPosed",
    hint: "Installs an LSPosed ZIP from the library as a Magisk module. Activation is confirmed by a verify step after a reboot, not by the file transfer.",
    needsModule: true,
    moduleKind: "lsposed_zip",
  },
  {
    value: "install_module",
    label: "Install module",
    hint: "Magisk/Zygisk ZIPs are installed with `magisk --install-module`; Xposed APKs with `adb install`. An APK install alone does not activate the module — enable and scope it with the Xposed steps below (or in the LSPosed manager).",
    needsModule: true,
  },
  {
    value: "enable_module",
    label: "Enable module",
    hint: "Removes the Magisk `disable` marker; reboot to apply.",
    needsModule: true,
  },
  {
    value: "disable_module",
    label: "Disable module",
    hint: "Writes the Magisk `disable` marker; reboot to apply.",
    needsModule: true,
  },
  {
    value: "remove_module",
    label: "Remove module",
    hint: "Queues the module for removal at the next boot.",
    needsModule: true,
  },
  {
    value: "enable_xposed_module",
    label: "Enable Xposed module",
    hint: "Writes LSPosed's enabled flag for an Xposed APK's package (params.package / params.enabled). lspd reloads its configuration only after the AVD reboots.",
    needsModule: false,
  },
  {
    value: "set_xposed_scope",
    label: "Set Xposed scope",
    hint: "Writes the app packages an Xposed module targets into LSPosed's scope table (params.package / params.apps / params.replace). lspd reloads its configuration only after the AVD reboots.",
    needsModule: false,
  },
  {
    value: "enable_adb_root",
    label: "Enable adb root",
    hint: "Restarts adbd as uid 0 on images that permit it (userdebug builds). Host-side root only: it gives no application su and does not survive a reboot; production images reject it.",
    needsModule: false,
  },
  {
    value: "enable_writable_system",
    label: "Enable writable /system",
    hint: "Runs adb root + adb remount. On API 30+ this sets up overlayfs and /system only becomes writable after a reboot; the step reports reboot_required in its data.",
    needsModule: false,
  },
  {
    value: "install_lsposed_manager",
    label: "Install LSPosed manager",
    hint: "Installs /data/adb/modules/zygisk_lsposed/manager.apk with pm install -r. lspd never installs its own manager, so this is what makes the manager reachable.",
    needsModule: false,
  },
  {
    value: "reboot",
    label: "Reboot AVD",
    hint: "Reboots the AVD and waits for sys.boot_completed so later steps see the post-reboot state. It can take minutes; optional params.timeout_s (default 300 seconds).",
    needsModule: false,
    needsTimeout: true,
  },
  {
    value: "remove_lsposed",
    label: "Remove LSPosed",
    hint: "Queues the LSPosed Magisk module for removal with the remove marker; requires a reboot to complete.",
    needsModule: false,
  },
  {
    value: "remove_magisk",
    label: "Remove Magisk (unsupported)",
    hint: "Magisk lives in the image ramdisk, so the backend always returns unsupported: stop the AVD, restore ramdisk.img from ramdisk.img.backup (rootAVD keeps it) and reinstall the image if needed. Shown for completeness; it cannot succeed.",
    needsModule: false,
    unsupported: true,
  },
  {
    value: "verify",
    label: "Verify expectations",
    hint: "Re-probes the device and checks params.expect (root, magisk, zygisk_active, lsposed_active).",
    needsModule: false,
    needsExpect: true,
  },
];

export function stepActionMeta(action: StepAction): StepActionMeta {
  return (
    STEP_ACTIONS.find((meta) => meta.value === action) ?? {
      value: action,
      label: humanize(action),
      hint: "",
      needsModule: false,
    }
  );
}

/**
 * Why the backend will reject this step as written. Derived from the step body
 * and the service's own validation messages, never guessed.
 */
export function stepWarning(step: ProfileStep): string | null {
  if (step.action === "remove_magisk") {
    return "The backend always reports unsupported: removing Magisk means restoring the image ramdisk (stop the AVD, restore ramdisk.img from ramdisk.img.backup, reinstall the image if needed).";
  }
  if (step.module_id || step.module_identifier) return null;
  switch (step.action) {
    case "install_lsposed":
      // A built-in profile leaves this step unbound on purpose: the backend
      // resolves the newest `lsposed_zip` from the module library at deploy
      // time (acquiring LSPosed imports one). Only an empty library fails the
      // step, and this UI cannot know that from the profile alone — the run
      // result reports the backend's "no LSPosed is available" message.
      return null;
    case "install_module":
      return "No module bound — the backend fails this step with “the module step needs a module from the library”.";
    case "enable_module":
    case "disable_module":
    case "remove_module":
      return "No module bound — the backend fails this step with “this step needs a module identifier”.";
    default:
      return null;
  }
}

export function profileWarnings(profile: InstrumentationProfile): string[] {
  return profile.steps
    .map((step, index) => {
      const warning = stepWarning(step);
      return warning ? `Step ${index + 1} (${stepActionMeta(step.action).label}): ${warning}` : null;
    })
    .filter((value): value is string => value !== null);
}

/** Read-only rendering of a profile's steps. */
export function ProfileStepPreview({ steps }: { steps: ProfileStep[] }) {
  if (steps.length === 0) {
    return <p className="muted small" style={{ margin: 0 }}>This profile has no steps.</p>;
  }
  return (
    <ol className="step-list">
      {steps.map((step, index) => {
        const meta = stepActionMeta(step.action);
        const warning = stepWarning(step);
        const bound = step.module_id || step.module_identifier;
        return (
          <li key={step.id || `${step.action}-${index}`}>
            <span className="step-list__index" aria-hidden="true">
              {index + 1}
            </span>
            <div>
              <div className="step-list__title">
                {meta.label}
                {bound ? <span className="muted small"> · {bound}</span> : null}
              </div>
              {meta.hint ? <div className="step-list__hint">{meta.hint}</div> : null}
              {step.action === "verify" && step.params.expect !== undefined ? (
                <div className="step-list__hint">
                  Expect:{" "}
                  <code style={styles.code}>{JSON.stringify(step.params.expect)}</code>
                </div>
              ) : null}
              {warning ? <div style={styles.warning}>{warning}</div> : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/* -------------------------------------------------------------------------- */
/* Device capability card                                                      */
/* -------------------------------------------------------------------------- */

function ModuleStateBadges({ module }: { module: { enabled: boolean; active: boolean } }) {
  return (
    <>
      <YesNoBadge
        label="enabled"
        value={module.enabled}
        yesTone="warning"
        title="The persisted Magisk switch, not runtime activation."
      />
      <YesNoBadge
        label="active"
        value={module.active}
        title="Active means the framework has run the module; a reboot is usually required first."
      />
    </>
  );
}

/**
 * One probed AVD. The same card is used by the Overview, Root & Magisk and
 * LSPosed tabs so the capability detail can never drift between them.
 */
export function DeviceInstrumentationCard({
  device,
  onRefresh,
  refreshing = false,
}: {
  device: DeviceInstrumentation;
  onRefresh?: () => void;
  refreshing?: boolean;
}) {
  const caps = device.capabilities;
  const facts: Array<{ label: string; value: ReactNode }> = [
    { label: "API level", value: device.api_level || "—" },
    { label: "ABI", value: device.abi || "—" },
    { label: "Image tag", value: device.image_tag || "—" },
    { label: "Image id", value: device.image_id || "—" },
    { label: "ADB serial", value: device.serial || "—" },
    { label: "Probed at", value: formatDateTime(device.probed_at) },
  ];

  // The probe can only read LSPosed's database when sqlite3 exists on the
  // device. In that case the note is surfaced in place of an (empty) inventory
  // table; an empty table must never read as "no modules".
  const xposedInventoryNote = caps.notes.find((note) =>
    /xposed module inventory unavailable|could not read the LSPosed database/i.test(note),
  );
  const probeNotes = xposedInventoryNote
    ? caps.notes.filter((note) => note !== xposedInventoryNote)
    : caps.notes;
  const showXposedInventory = caps.lsposed_installed || device.xposed_modules.length > 0;

  const xposedColumns: Column<InstalledModule>[] = [
    {
      key: "package",
      header: "Package",
      render: (module) => <code style={styles.code}>{module.identifier || "—"}</code>,
    },
    {
      key: "enabled",
      header: "Enabled",
      render: (module) => (
        <Badge
          tone={module.enabled ? "warning" : "neutral"}
          dot
          title="The persisted LSPosed enabled flag. LSPosed applies it only after the AVD reboots."
        >
          {module.enabled ? "enabled" : "disabled"}
        </Badge>
      ),
    },
    {
      key: "active",
      header: "Active",
      render: (module) => (
        <Badge
          tone={module.active ? "success" : "neutral"}
          dot
          title="Reported by LSPosed for this boot; it is not proof that the module hooked an app."
        >
          {module.active ? "active" : "inactive"}
        </Badge>
      ),
    },
    {
      key: "scope",
      header: "Scope",
      render: (module) => {
        const scope = module.scope ?? [];
        return (
          <span
            title={
              scope.length > 0
                ? scope.join(", ")
                : "No application packages are in this module's LSPosed scope."
            }
          >
            {pluralize(scope.length, "app")}
          </span>
        );
      },
    },
  ];

  return (
    <Card
      title={
        <span style={styles.rowTight}>
          <span>{device.instance}</span>
          <StatusPill
            status={device.running ? "running" : "stopped"}
            label={device.running ? (device.boot_completed ? "Running · boot completed" : "Running · booting") : "Stopped"}
            pulse={device.running && !device.boot_completed}
          />
        </span>
      }
      icon="instance"
      actions={
        onRefresh ? (
          <IconButton
            label={`Re-probe ${device.instance}`}
            icon="refresh"
            size="sm"
            loading={refreshing}
            onClick={onRefresh}
          />
        ) : null
      }
    >
      <div style={styles.stackTight}>
        <div style={styles.rowTight}>
          <RootBadge implementation={device.root_implementation} />
          <YesNoBadge
            label="ADB root"
            value={caps.adb_root}
            title="adb root is host-side root on userdebug images. It does not provide application root, Zygisk or LSPosed."
          />
          <Badge tone={device.compatible ? "success" : "warning"} dot>
            {device.compatible ? "Compatible" : "Not compatible"}
          </Badge>
          {caps.writable_system ? <Badge tone="info">/system writable</Badge> : null}
          {caps.reboot_required ? <StatusPill status="reboot_required" label="Reboot required" /> : null}
        </div>

        <dl className="kv-list" style={{ margin: 0 }}>
          {facts.map((fact) => (
            <div className="kv-list__row" key={fact.label}>
              <dt>{fact.label}</dt>
              <dd>{fact.value}</dd>
            </div>
          ))}
        </dl>

        <div style={styles.capabilityGrid}>
          <div style={styles.stackTight}>
            <span style={styles.labels}>Magisk</span>
            <div style={styles.rowTight}>
              <YesNoBadge
                label="Installed"
                value={caps.magisk}
                yesTone="info"
                title="Magisk su answered the probe. Installed is not the same as active on every boot."
              />
              {caps.magisk_version ? <Badge tone="neutral">v{caps.magisk_version}</Badge> : null}
              {caps.magisk ? (
                <YesNoBadge
                  label="Environment ready"
                  value={caps.magisk_environment_ready}
                  yesTone="success"
                  title="Module scripts only run once the Magisk 'Additional Setup' has completed."
                />
              ) : null}
            </div>
            {caps.magisk && !caps.magisk_environment_ready ? (
              <span style={styles.warning}>
                Magisk is installed but its environment is incomplete: module scripts do not run until the
                Magisk app&apos;s “Additional Setup” completes.
              </span>
            ) : null}
          </div>

          <div style={styles.stackTight}>
            <span style={styles.labels}>Zygisk</span>
            <div style={styles.rowTight}>
              <YesNoBadge
                label="enabled"
                value={caps.zygisk_enabled}
                yesTone="warning"
                title="The persisted Magisk setting. Enabled alone does not mean Zygisk is running."
              />
              <YesNoBadge
                label="active"
                value={caps.zygisk_active}
                title="Active means the Zygisk payload is mounted; that needs a reboot after enabling."
              />
              {caps.zygisk_enabled && !caps.zygisk_active ? (
                <Badge tone="warning" icon="restart">
                  Pending reboot
                </Badge>
              ) : null}
              {!caps.zygisk_available && caps.magisk ? (
                <Badge tone="neutral" title="The probe could not read the Zygisk setting.">
                  State unknown
                </Badge>
              ) : null}
            </div>
          </div>

          <div style={styles.stackTight}>
            <span style={styles.labels}>LSPosed</span>
            <div style={styles.rowTight}>
              <YesNoBadge
                label="Installed"
                value={caps.lsposed_installed}
                yesTone="info"
                title="An LSPosed ZIP is present under /data/adb/modules."
              />
              {caps.lsposed_version ? <Badge tone="neutral">v{caps.lsposed_version}</Badge> : null}
              <YesNoBadge
                label="active"
                value={caps.lsposed_active}
                title="Active means LSPosed's data directory exists after boot; the module ZIP being installed is not enough."
              />
              {caps.lsposed_installed && !caps.lsposed_active ? (
                <Badge tone="warning" icon="restart">
                  Pending reboot
                </Badge>
              ) : null}
            </div>
          </div>
        </div>

        {showXposedInventory ? (
          <div style={styles.stackTight}>
            <span style={styles.labels}>Xposed modules (LSPosed)</span>
            {device.xposed_modules.length > 0 ? (
              <Table
                dense
                caption={`Xposed modules registered in LSPosed on ${device.instance}`}
                columns={xposedColumns}
                rows={device.xposed_modules}
                rowKey={(module) => module.identifier}
              />
            ) : xposedInventoryNote ? (
              <InlineNotice tone="warning" title="Xposed module inventory unavailable">
                <p style={{ margin: 0 }}>{xposedInventoryNote}</p>
              </InlineNotice>
            ) : (
              <span style={styles.subtext}>
                No Xposed module is registered in LSPosed on this device. An installed APK only appears here
                once LSPosed has recognised it.
              </span>
            )}
          </div>
        ) : null}

        {device.blockers.length > 0 ? (
          <InlineNotice
            tone={device.compatible ? "warning" : "danger"}
            title={pluralize(device.blockers.length, "blocker")}
          >
            <ul style={styles.bulletList}>
              {device.blockers.map((blocker) => (
                <li key={blocker}>{blocker}</li>
              ))}
            </ul>
          </InlineNotice>
        ) : null}

        {probeNotes.length > 0 ? (
          <InlineNotice tone="info" title="Probe notes">
            <ul style={styles.bulletList}>
              {probeNotes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          </InlineNotice>
        ) : null}

        {device.magisk_modules.length > 0 ? (
          <div style={styles.stackTight}>
            <span style={styles.labels}>Magisk modules on the device</span>
            <div style={styles.rowTight}>
              {device.magisk_modules.map((module) => (
                <span key={`${module.identifier}-${module.version}`} style={styles.rowTight}>
                  <Badge tone="neutral" icon="folder">
                    {module.identifier || "(no identifier)"}
                  </Badge>
                  <ModuleKindBadge kind={module.kind} />
                  <ModuleStateBadges module={module} />
                  {module.version ? <span className="muted small">{module.version}</span> : null}
                </span>
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Deployment helpers                                                          */
/* -------------------------------------------------------------------------- */

export function isLiveRun(state: DeploymentRun["state"]): boolean {
  return state === "queued" || state === "running";
}

export function targetCounts(results: TargetResult[]): { ok: number; failed: number; skipped: number } {
  const ok = results.filter((result) => result.ok).length;
  const skipped = results.filter((result) => !result.ok && result.skipped).length;
  return { ok, failed: results.length - ok - skipped, skipped };
}

export function runTargetSummary(run: DeploymentRun): string {
  if (run.results.length === 0) {
    return isLiveRun(run.state) ? "pending" : "no results";
  }
  const { ok, failed, skipped } = targetCounts(run.results);
  const parts = [`${ok} passed`];
  if (failed > 0) parts.push(`${failed} failed`);
  if (skipped > 0) parts.push(`${skipped} skipped`);
  return parts.join(" · ");
}

/* -------------------------------------------------------------------------- */
/* Deploy card used by the Root & Magisk and LSPosed tabs                      */
/* -------------------------------------------------------------------------- */

export interface DeployProfileCardProps {
  profiles: InstrumentationProfile[];
  targets: string[];
  title: string;
  description?: ReactNode;
  footerNote?: ReactNode;
  onGoToOverview?: () => void;
  onDeployed?: (run: DeploymentRun) => void;
}

/**
 * Profile-driven deploy. Every deploy goes through `POST /instrumentation/deployments`;
 * failures surface the API message instead of a success toast.
 */
export function DeployProfileCard({
  profiles,
  targets,
  title,
  description,
  footerNote,
  onGoToOverview,
  onDeployed,
}: DeployProfileCardProps) {
  const toast = useToast();
  const [profileId, setProfileId] = useState("");
  const [deploying, setDeploying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const effectiveId = profileId || profiles[0]?.id || "";
  const selected = useMemo(
    () => profiles.find((profile) => profile.id === effectiveId) ?? profiles[0],
    [effectiveId, profiles],
  );
  const warnings = useMemo(() => (selected ? profileWarnings(selected) : []), [selected]);

  const deploy = async (): Promise<void> => {
    if (!selected || targets.length === 0) return;
    setDeploying(true);
    setError(null);
    try {
      const run = await instrumentationApi.createDeployment({
        profile_id: selected.id,
        targets,
      });
      toast.success(
        "Deployment queued",
        `${selected.name} → ${pluralize(targets.length, "AVD")} · ${run.state}. Follow it in the Deployments tab.`,
      );
      onDeployed?.(run);
    } catch (caught) {
      const message = apiErrorText(caught);
      setError(message);
      toast.error("Deployment could not be started", message);
    } finally {
      setDeploying(false);
    }
  };

  return (
    <Card title={title} icon="deploy" description={description}>
      <div style={styles.stack}>
        {profiles.length === 0 ? (
          <p className="muted small" style={{ margin: 0 }}>
            The expected profile is not available on this daemon.
          </p>
        ) : (
          <>
            {profiles.length > 1 ? (
              <Field label="Profile" hint="Built-in profiles are read-only; the API runs them as shipped.">
                <Select
                  value={effectiveId}
                  options={profiles.map((profile) => ({
                    value: profile.id,
                    label: `${profile.name}${profile.builtin ? " (built-in)" : ""}`,
                  }))}
                  onChange={(event) => setProfileId(event.target.value)}
                />
              </Field>
            ) : selected ? (
              <div style={styles.rowBetween}>
                <span className="muted small">
                  Profile: <strong>{selected.name}</strong>
                  {selected.builtin ? " (built-in)" : ""}
                </span>
              </div>
            ) : null}
            {selected ? <ProfileStepPreview steps={selected.steps} /> : null}
            {warnings.length > 0 ? (
              <InlineNotice tone="warning" title="This profile will fail a step as saved">
                <ul style={styles.bulletList}>
                  {warnings.map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
                <p style={{ margin: "var(--space-2) 0 0" }}>
                  Built-in profiles cannot be edited (the API answers 422). Use <strong>Duplicate</strong> on the
                  Profiles tab to get an editable copy, bind a module from the library on the affected steps, and
                  deploy the copy instead.
                </p>
              </InlineNotice>
            ) : null}
          </>
        )}

        <div style={styles.rowBetween}>
          <span className="muted small">
            {targets.length === 0
              ? "No AVDs selected."
              : `Targets (${targets.length}): ${targets.join(", ")}`}
          </span>
          <div style={styles.rowTight}>
            {targets.length === 0 && onGoToOverview ? (
              <Button size="sm" variant="ghost" onClick={onGoToOverview}>
                Select AVDs
              </Button>
            ) : null}
            <Button
              variant="primary"
              icon="play"
              loading={deploying}
              disabled={!selected || targets.length === 0}
              title={
                targets.length === 0
                  ? "Select at least one AVD in the Overview tab first"
                  : !selected
                    ? "No profile is available to deploy"
                    : `Deploy ${selected.name} to ${pluralize(targets.length, "AVD")}`
              }
              onClick={() => void deploy()}
            >
              Deploy to selected AVDs
            </Button>
          </div>
        </div>

        {error ? (
          <InlineNotice tone="danger" title="Deployment could not be started">
            <p>{error}</p>
          </InlineNotice>
        ) : null}

        {footerNote ? (
          <p className="muted small" style={{ margin: 0 }}>
            {footerNote}
          </p>
        ) : null}
      </div>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Target summary                                                              */
/* -------------------------------------------------------------------------- */

export function TargetSummary({
  selected,
  onGoToOverview,
}: {
  selected: string[];
  onGoToOverview?: () => void;
}) {
  return (
    <div style={styles.rowBetween}>
      <div style={styles.rowTight}>
        <span className="muted small">Selected AVDs:</span>
        {selected.length === 0 ? (
          <span className="muted small">none</span>
        ) : (
          selected.map((name) => (
            <Badge key={name} tone="info" icon="instance">
              {name}
            </Badge>
          ))
        )}
      </div>
      {onGoToOverview ? (
        <Button size="sm" variant="ghost" icon="arrow-left" onClick={onGoToOverview}>
          Change selection
        </Button>
      ) : null}
    </div>
  );
}
