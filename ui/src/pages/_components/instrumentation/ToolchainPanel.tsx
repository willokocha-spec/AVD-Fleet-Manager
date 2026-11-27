/**
 * Toolchain acquisition panel.
 *
 * Magisk and LSPosed are GPL-3.0; AVD Fleet is MIT and bundles neither. This
 * panel shows the *pinned* upstream releases the daemon knows about and asks it
 * to download one on demand. The API reports presence (`present`) and integrity
 * (`verified`) separately, so the three states are rendered as three distinct
 * badges: a present copy that failed verification never looks usable.
 *
 * Acquisition is not installation: an LSPosed ZIP is imported into the module
 * library (and only then resolvable by the built-in LSPosed profile), while the
 * Magisk APK is a payload for an external ramdisk patcher. Deploying anything is
 * a separate, explicit step on the Profiles/Deployments tabs.
 *
 * Shared by the Root & Magisk and LSPosed & Zygisk tabs — the copy must stay
 * true on both.
 */
import { useState, type CSSProperties } from "react";

import { ApiError, instrumentationApi } from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  IconButton,
  InlineNotice,
  Skeleton,
  useToast,
} from "@/lib/components";
import { useAsync } from "@/lib/hooks";
import type { ArtifactStatus } from "@/lib/types";
import { formatBytes, formatDateTime } from "@/lib/utils";

import { apiErrorText, styles } from "@/pages/_components/instrumentation/shared";

const css = (value: CSSProperties): CSSProperties => value;

const localStyles = {
  artifact: css({
    display: "flex",
    flexDirection: "column",
    gap: "var(--space-2)",
    padding: "var(--space-3)",
    border: "1px solid var(--border)",
    borderRadius: "var(--radius-sm)",
    background: "var(--surface-sunken)",
  }),
  name: css({ fontWeight: 600, color: "var(--text)" }),
  link: css({
    color: "var(--accent)",
    fontFamily: "var(--font-mono)",
    fontSize: "var(--fs-xs)",
    overflowWrap: "anywhere",
  }),
} as const;

/** Exact byte count — the confirm dialog must never round a size away. */
function exactBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "—";
  return `${bytes.toLocaleString("en-US")} bytes`;
}

function kindLabel(kind: ArtifactStatus["kind"]): string {
  return kind === "module_zip" ? "module ZIP" : "APK";
}

function kindPhrase(kind: ArtifactStatus["kind"]): string {
  return kind === "module_zip" ? "Magisk module ZIP" : "APK";
}

function acquisitionEffect(kind: ArtifactStatus["kind"]): string {
  return kind === "module_zip"
    ? "The verified ZIP is imported into the module library so profiles can deploy it. No AVD is touched by this request."
    : "The verified APK is stored as the payload for an external ramdisk patcher (rootAVD or equivalent). The built-in profiles install it and drive Magisk's “Additional Setup” with the complete_magisk_setup step; this acquisition alone does not install it on any AVD.";
}

/**
 * Exactly one of three states. `present && !verified` is its own failure badge —
 * it is never folded into the verified state and carries no success icon.
 */
function ArtifactStateBadge({ artifact }: { artifact: ArtifactStatus }) {
  if (!artifact.present) {
    return (
      <Badge tone="neutral" dot title="No local copy of this pinned release. Acquiring downloads and verifies it.">
        not downloaded
      </Badge>
    );
  }
  if (artifact.verified) {
    return (
      <Badge
        tone="success"
        dot
        title="The local copy's SHA-256 matches the pinned value and its size matches the pinned size."
      >
        downloaded + verified
      </Badge>
    );
  }
  return (
    <Badge
      tone="danger"
      dot
      title="The local copy failed verification: its SHA-256 or size does not match the pinned values. Acquire again to replace it."
    >
      downloaded, FAILED verification
    </Badge>
  );
}

interface AcquisitionFailure {
  title: string;
  message: string;
  toastTitle: string;
  /** The HTTP call failed before the file was stored; nothing was installed. */
  discarded: boolean;
  /** HTTP 409: the API wants explicit confirmation and performed no work. */
  consent: boolean;
}

function describeFailure(artifact: ArtifactStatus, error: unknown): AcquisitionFailure {
  const message = apiErrorText(error);
  const status = error instanceof ApiError ? error.status : 0;
  if (status === 409) {
    return {
      title: `Confirmation required for ${artifact.name}`,
      message,
      toastTitle: "Confirmation required",
      discarded: false,
      consent: true,
    };
  }
  if (status === 422) {
    return {
      title: `${artifact.name} was not acquired — the download was discarded`,
      message,
      toastTitle: "Acquisition failed verification",
      discarded: true,
      consent: false,
    };
  }
  return {
    title: `Could not acquire ${artifact.name}`,
    message,
    toastTitle: "Acquisition failed",
    discarded: false,
    consent: false,
  };
}

export interface ToolchainPanelProps {
  /**
   * Called with the new library module id after a module ZIP is imported, so
   * the page can ask the module library tab to refresh its list.
   */
  onModuleImported?: (moduleId: string) => void;
}

export function ToolchainPanel({ onModuleImported }: ToolchainPanelProps) {
  const toast = useToast();
  const toolchain = useAsync(() => instrumentationApi.getToolchain(), []);
  const [confirmTarget, setConfirmTarget] = useState<ArtifactStatus | null>(null);
  const [acquiringKey, setAcquiringKey] = useState<string | null>(null);
  const [failure, setFailure] = useState<AcquisitionFailure | null>(null);

  const artifacts = toolchain.data?.artifacts ?? [];
  const available = toolchain.data?.available ?? false;

  const acquire = async (): Promise<void> => {
    const target = confirmTarget;
    if (!target) return;
    setAcquiringKey(target.key);
    setFailure(null);
    try {
      // Only this call — which sends {confirm:true} — may produce a success toast.
      const result = await instrumentationApi.acquireToolchain(target.key, true);
      if (result.artifact.verified) {
        toast.success(
          `Acquired ${result.artifact.name}`,
          `${result.artifact.version} · ${exactBytes(result.artifact.pinned_size_bytes)} · SHA-256 matches the pinned hash`,
        );
      } else {
        // Defensive: a resolved call should always return a verified artifact,
        // but if it does not, the UI must not claim success.
        toast.warning(
          `Acquired ${result.artifact.name} but it failed verification`,
          "The daemon stored a file that does not match the pinned hash. Do not deploy it.",
        );
      }
      if (result.module_id) {
        toast.success(
          "Module imported",
          `imported as ${result.module_id} — profiles resolve the newest library module of this kind.`,
        );
        onModuleImported?.(result.module_id);
      }
      // Refresh the artifact list from the daemon rather than guessing the state.
      toolchain.reload();
    } catch (caught) {
      const described = describeFailure(target, caught);
      setFailure(described);
      toast.error(described.toastTitle, described.message);
    } finally {
      setAcquiringKey(null);
      setConfirmTarget(null);
    }
  };

  return (
    <Card
      title="Toolchain"
      icon="download"
      description="Pinned upstream releases for root and framework instrumentation. GET /instrumentation/toolchain reports what this daemon already has; nothing here is bundled with AVD Fleet."
      actions={
        <IconButton
          label="Reload toolchain status"
          icon="refresh"
          size="sm"
          loading={toolchain.loading}
          onClick={toolchain.reload}
        />
      }
      footer={
        <span>
          Magisk and LSPosed are <strong>GPL-3.0</strong>. AVD Fleet is MIT and ships neither: each file is
          downloaded from its official release and verified against the pinned SHA-256 before it is stored.
          Acquisition never installs or deploys anything.
        </span>
      }
    >
      <div style={styles.stack}>
        {failure ? (
          <InlineNotice
            tone={failure.consent ? "warning" : "danger"}
            title={failure.title}
            action={
              <Button size="sm" variant="ghost" icon="close" onClick={() => setFailure(null)}>
                Dismiss
              </Button>
            }
          >
            <p style={{ margin: 0 }}>{failure.message}</p>
            {failure.discarded ? (
              <p style={{ margin: "var(--space-2) 0 0" }}>
                The downloaded file was discarded before it was stored: nothing was installed on an AVD and
                nothing was added to the module library.
              </p>
            ) : null}
            {failure.consent ? (
              <p style={{ margin: "var(--space-2) 0 0" }}>
                The request was not retried automatically. Use Acquire again only if you intend to confirm the
                download.
              </p>
            ) : null}
          </InlineNotice>
        ) : null}

        {toolchain.error ? (
          <InlineNotice
            tone="danger"
            title="Could not load the toolchain status"
            action={
              <Button size="sm" variant="secondary" icon="refresh" onClick={toolchain.reload}>
                Retry
              </Button>
            }
          >
            <p style={{ margin: 0 }}>{apiErrorText(toolchain.error)}</p>
          </InlineNotice>
        ) : toolchain.loading && !toolchain.data ? (
          <div className="skeleton-card">
            <Skeleton lines={4} />
          </div>
        ) : null}

        {toolchain.data && !available ? (
          <InlineNotice tone="warning" title="Toolchain acquisition is not configured on this daemon">
            <p style={{ margin: 0 }}>
              The endpoint reports <code style={styles.code}>available: false</code>, so no pinned release can
              be downloaded here. The daemon needs an acquisition store before this panel can do anything.
            </p>
          </InlineNotice>
        ) : null}

        {available && artifacts.length === 0 ? (
          <EmptyState
            compact
            icon="download"
            title="No pinned artifacts"
            description="The daemon reports acquisition available but returned no pinned releases."
          />
        ) : null}

        {available
          ? artifacts.map((artifact) => (
              <div key={artifact.key} style={localStyles.artifact}>
                <div style={styles.rowBetween}>
                  <div style={styles.stackTight}>
                    <div style={styles.rowTight}>
                      <span style={localStyles.name}>{artifact.name}</span>
                      <Badge tone="neutral" title="Pinned upstream release version">
                        {artifact.version}
                      </Badge>
                      <Badge
                        tone={artifact.kind === "module_zip" ? "accent" : "info"}
                        title={
                          artifact.kind === "module_zip"
                            ? "A Magisk module archive; acquiring imports it into the module library."
                            : "An Android APK; acquiring stores it but does not install it."
                        }
                      >
                        {kindLabel(artifact.kind)}
                      </Badge>
                      <Badge tone="neutral" title="Minimum Android API level required by this release">
                        {`min API ${artifact.min_api}`}
                      </Badge>
                      <ArtifactStateBadge artifact={artifact} />
                    </div>

                    <div style={styles.rowTight}>
                      <span
                        className="muted small"
                        title={`Pinned size: ${exactBytes(artifact.pinned_size_bytes)}`}
                      >
                        {exactBytes(artifact.pinned_size_bytes)} ({formatBytes(artifact.pinned_size_bytes)})
                      </span>
                      <span className="muted small">·</span>
                      <span style={styles.labels}>pinned SHA-256</span>
                      <code style={styles.code} title={artifact.pinned_sha256}>
                        {artifact.pinned_sha256 ? `${artifact.pinned_sha256.slice(0, 12)}…` : "—"}
                      </code>
                    </div>

                    {artifact.present ? (
                      <span className="muted small" title={artifact.path}>
                        On disk: {exactBytes(artifact.size_bytes)} · acquired{" "}
                        {formatDateTime(artifact.acquired_at)}
                      </span>
                    ) : null}

                    {artifact.present && !artifact.verified ? (
                      <span style={styles.warning}>
                        The stored copy&apos;s SHA-256 is{" "}
                        <code style={styles.code} title={artifact.sha256}>
                          {artifact.sha256 ? `${artifact.sha256.slice(0, 12)}…` : "unknown"}
                        </code>
                        , which does not match the pin. Acquire again to replace it.
                      </span>
                    ) : null}

                    <span className="muted small">{acquisitionEffect(artifact.kind)}</span>
                  </div>

                  <Button
                    variant={artifact.present && artifact.verified ? "secondary" : "primary"}
                    size="sm"
                    icon="download"
                    loading={acquiringKey === artifact.key}
                    disabled={acquiringKey !== null && acquiringKey !== artifact.key}
                    title={`Acquire ${artifact.name} ${artifact.version} (${exactBytes(artifact.pinned_size_bytes)})`}
                    onClick={() => setConfirmTarget(artifact)}
                  >
                    {`Acquire ${artifact.name}`}
                  </Button>
                </div>
              </div>
            ))
          : null}

        {available ? (
          <div style={styles.stackTight}>
            <p className="muted small" style={{ margin: 0 }}>
              <strong>LSPosed and the built-in LSPosed profile.</strong> The shipped profile leaves its LSPosed
              step unbound, so the backend resolves the newest <code style={styles.code}>lsposed_zip</code> from
              the module library at deploy time. The built-in profile therefore becomes deployable{" "}
              <strong>only after acquisition</strong> imports an LSPosed ZIP into that library — before that, the
              step fails with “no LSPosed is available in the module library”.
            </p>
            <p className="muted small" style={{ margin: 0 }}>
              Acquisition is not installation. A device still needs Magisk root and Zygisk enabled, a reboot,
              and the profile&apos;s <code style={styles.code}>verify</code> step before LSPosed is reported as
              active.
            </p>
          </div>
        ) : null}
      </div>

      <ConfirmDialog
        open={confirmTarget !== null}
        title={
          confirmTarget
            ? `Acquire ${confirmTarget.name} ${confirmTarget.version}?`
            : "Acquire pinned release?"
        }
        description="Downloads one pinned upstream release from its official source."
        confirmLabel="Confirm acquisition"
        tone="primary"
        onCancel={() => {
          if (acquiringKey === null) setConfirmTarget(null);
        }}
        onConfirm={acquire}
      >
        {confirmTarget ? (
          <div style={styles.stackTight}>
            <p style={{ margin: 0 }}>
              <strong>{confirmTarget.name}</strong> {confirmTarget.version} ·{" "}
              {kindPhrase(confirmTarget.kind)} ·{" "}
              <strong>{exactBytes(confirmTarget.pinned_size_bytes)}</strong> (
              {formatBytes(confirmTarget.pinned_size_bytes)})
            </p>
            <p style={{ margin: 0 }}>
              The daemon downloads the pinned file from{" "}
              <a
                href={confirmTarget.source_url}
                target="_blank"
                rel="noreferrer"
                style={localStyles.link}
              >
                {confirmTarget.source_url}
              </a>
              .
            </p>
            <p style={{ margin: 0 }}>
              {confirmTarget.name} is licensed <strong>GPL-3.0</strong>. AVD Fleet is MIT and does not bundle
              it: the file is fetched from the official release above, only after you confirm.
            </p>
            <p style={{ margin: 0 }}>
              <strong>Acquiring is not installation.</strong> {acquisitionEffect(confirmTarget.kind)}
            </p>
            <p className="muted small" style={{ margin: 0 }}>
              The daemon verifies the download against the pinned SHA-256 and size. On a mismatch it discards
              the file and installs nothing.
            </p>
          </div>
        ) : null}
      </ConfirmDialog>
    </Card>
  );
}
