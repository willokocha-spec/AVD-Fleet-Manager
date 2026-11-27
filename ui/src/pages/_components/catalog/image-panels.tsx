/**
 * Image catalogue dialogs and the expandable OOBE matrix panel.
 *
 * Kept out of `src/pages/Images.tsx` only for readability; everything here is
 * private to the Images page and the U3 page contract.
 */
import { useEffect, useMemo, useState } from "react";

import { api } from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  Checkbox,
  EmptyState,
  Field,
  InlineNotice,
  Input,
  Modal,
  ProgressBar,
  Select,
  Spinner,
  StatusPill,
  Table,
  type Column,
} from "@/lib/components";
import type { OobeMatrix, OobeRecord, OobeStatus, SystemImage } from "@/lib/types";
import {
  formatBytes,
  formatDateTime,
  formatDuration,
  formatRelativeTime,
  humanize,
} from "@/lib/utils";

import {
  DetailList,
  Hint,
  Mono,
  SubText,
  errorMessage,
  styles,
} from "./common";

/* -------------------------------------------------------------------------- */
/* Helpers                                                                     */
/* -------------------------------------------------------------------------- */

const ABSOLUTE_PATH_PATTERN = /^([a-zA-Z]:[\\/]|\\\\|\/)/;

function validateHostPath(value: string): string | undefined {
  const trimmed = value.trim();
  if (!trimmed) return "Host directory path is required.";
  if (!ABSOLUTE_PATH_PATTERN.test(trimmed)) {
    return "Enter an absolute path, for example D:\\android\\system-images\\... or /opt/android/system-images/....";
  }
  return undefined;
}

function oobeStatusLabel(status: OobeStatus): string {
  return status === "unsupported" ? "Unsupported" : humanize(status);
}

function oobeTone(status: OobeStatus): "success" | "danger" | "warning" | "neutral" {
  if (status === "verified") return "success";
  if (status === "failed") return "danger";
  if (status === "unverified") return "warning";
  return "neutral";
}

/* -------------------------------------------------------------------------- */
/* OOBE matrix panel                                                           */
/* -------------------------------------------------------------------------- */

export function OobeMatrixPanel({
  matrix,
  loading,
  error,
  onRetry,
  onVerify,
}: {
  matrix: OobeMatrix | null;
  loading: boolean;
  error: Error | null;
  onRetry: () => void;
  onVerify?: (imageId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const records = useMemo(() => matrix?.records ?? [], [matrix]);

  const counts = useMemo(() => {
    const accumulator: Record<OobeStatus, number> = {
      verified: 0,
      failed: 0,
      unverified: 0,
      unsupported: 0,
    };
    for (const record of records) {
      accumulator[record.status] = (accumulator[record.status] ?? 0) + 1;
    }
    return accumulator;
  }, [records]);

  const columns = useMemo<Column<OobeRecord>[]>(
    () => [
      {
        key: "image",
        header: "Image",
        sortable: true,
        sortValue: (record) => record.image_id,
        render: (record) => (
          <div style={styles.entity}>
            <span style={styles.entityTitle}>{record.image_id}</span>
            <span style={styles.subtext}>
              API {record.api_level ?? "—"} · {record.tag ?? "—"} · {record.abi ?? "—"}
            </span>
          </div>
        ),
      },
      {
        key: "status",
        header: "Status",
        sortable: true,
        sortValue: (record) => record.status,
        render: (record) => (
          <StatusPill
            status={record.status}
            tone={oobeTone(record.status)}
            label={oobeStatusLabel(record.status)}
          />
        ),
      },
      {
        key: "activity",
        header: "Activity",
        render: (record) => <Mono>{record.activity || "—"}</Mono>,
      },
      {
        key: "screenshot",
        header: "Screenshot",
        render: (record) => <Mono>{record.screenshot_path || "—"}</Mono>,
      },
      {
        key: "boot",
        header: "Boot",
        align: "right",
        sortable: true,
        sortValue: (record) => record.boot_seconds ?? null,
        render: (record) => formatDuration(record.boot_seconds),
      },
      {
        key: "reset",
        header: "Reset",
        align: "right",
        sortable: true,
        sortValue: (record) => record.reset_seconds ?? null,
        render: (record) => formatDuration(record.reset_seconds),
      },
      {
        key: "checked",
        header: "Checked",
        sortable: true,
        sortValue: (record) => record.checked_at ?? null,
        render: (record) => (
          <span title={formatDateTime(record.checked_at)}>{formatRelativeTime(record.checked_at)}</span>
        ),
      },
      {
        key: "evidence",
        header: "Evidence",
        render: (record) =>
          record.evidence?.length ? (
            <span title={record.evidence.join("\n")}>
              <Badge tone="info">{record.evidence.length} file(s)</Badge>
            </span>
          ) : (
            <span className="muted">—</span>
          ),
      },
      ...(onVerify
        ? [
            {
              key: "verify",
              header: "",
              align: "right" as const,
              render: (record: OobeRecord) => (
                <Button
                  size="sm"
                  variant="ghost"
                  icon="shield"
                  onClick={() => onVerify(record.image_id)}
                >
                  Verify
                </Button>
              ),
            },
          ]
        : []),
    ],
    [onVerify],
  );

  return (
    <Card
      title="OOBE matrix"
      description="First-run setup verification recorded per system image. Evidence is captured from a temporary AVD boot."
      icon="shield"
      actions={
        <Button
          size="sm"
          variant="ghost"
          icon={expanded ? "chevron-up" : "chevron-down"}
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "Hide details" : "Show details"}
        </Button>
      }
    >
      <div style={styles.stack}>
        <div style={styles.rowBetween}>
          <div style={styles.rowTight}>
            {counts.verified > 0 ? (
              <StatusPill status="verified" label={`${counts.verified} verified`} />
            ) : null}
            {counts.failed > 0 ? (
              <StatusPill status="failed" label={`${counts.failed} failed`} />
            ) : null}
            {counts.unverified > 0 ? (
              <StatusPill status="unverified" label={`${counts.unverified} unverified`} />
            ) : null}
            {counts.unsupported > 0 ? (
              <StatusPill status="unsupported" label={`${counts.unsupported} unsupported`} />
            ) : null}
            {records.length === 0 && !loading && !error ? (
              <StatusPill status="unverified" label="No records" icon={false} />
            ) : null}
          </div>
          {matrix ? (
            <SubText title={formatDateTime(matrix.generated_at)}>
              {matrix.host_abi ? `Host ABI ${matrix.host_abi} · ` : ""}
              Generated {formatRelativeTime(matrix.generated_at)}
            </SubText>
          ) : null}
        </div>

        {loading && !matrix ? (
          <div style={styles.row}>
            <Spinner size="sm" label="Loading OOBE matrix" />
            <SubText>Loading OOBE matrix…</SubText>
          </div>
        ) : null}

        {error ? (
          <InlineNotice
            tone="danger"
            title="Could not load the OOBE matrix"
            action={
              <Button size="sm" variant="secondary" icon="refresh" onClick={onRetry}>
                Retry
              </Button>
            }
          >
            <p>{errorMessage(error)}</p>
          </InlineNotice>
        ) : null}

        {expanded && !error ? (
          matrix && records.length > 0 ? (
            <Table
              columns={columns}
              rows={records}
              rowKey={(record) => record.image_id}
              caption="OOBE verification records"
              empty={<EmptyState compact title="No OOBE records" description="No image has been verified yet." icon="shield" />}
              loading={loading && !matrix}
            />
          ) : (
            <EmptyState
              compact
              icon="shield"
              title="No OOBE records yet"
              description="Verify an image from the catalogue to boot a temporary AVD and record the setup-wizard evidence."
            />
          )
        ) : null}

        {!expanded ? (
          <Hint>
            Expand to inspect the setup-wizard activity, screenshot path, boot and reset timings, plus
            the raw evidence files recorded for each image.
          </Hint>
        ) : null}
      </div>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Import custom image dialog                                                  */
/* -------------------------------------------------------------------------- */

export function ImportImageDialog({
  open,
  onClose,
  onImported,
}: {
  open: boolean;
  onClose: () => void;
  onImported: (image: SystemImage) => void;
}) {
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [apiLevel, setApiLevel] = useState("");
  const [abi, setAbi] = useState("");
  const [errors, setErrors] = useState<{ path?: string; name?: string; apiLevel?: string }>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!open) return;
    setPath("");
    setName("");
    setApiLevel("");
    setAbi("");
    setErrors({});
    setFormError(null);
    setSubmitting(false);
  }, [open]);

  const validate = (): { path?: string; name?: string; apiLevel?: string } => {
    const next: { path?: string; name?: string; apiLevel?: string } = {};
    const pathError = validateHostPath(path);
    if (pathError) next.path = pathError;
    if (name.trim().length > 120) next.name = "Keep the display name at 120 characters or fewer.";
    if (apiLevel.trim()) {
      if (!/^\d+$/.test(apiLevel.trim())) {
        next.apiLevel = "API level must be a whole number.";
      } else {
        const parsed = Number(apiLevel.trim());
        if (parsed < 1 || parsed > 99) next.apiLevel = "API level must be between 1 and 99.";
      }
    }
    return next;
  };

  const submit = async (): Promise<void> => {
    const next = validate();
    setErrors(next);
    setFormError(null);
    if (Object.keys(next).length > 0) return;

    setSubmitting(true);
    try {
      const image = await api.importImage({
        path: path.trim(),
        name: name.trim() ? name.trim() : null,
        api_level: apiLevel.trim() ? Number(apiLevel.trim()) : null,
        abi: abi.trim() ? abi.trim() : null,
      });
      onImported(image);
      onClose();
    } catch (caught) {
      setFormError(errorMessage(caught));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Import custom image"
      description="Register a system image that already exists in a directory on the daemon host. Nothing is downloaded."
      size="md"
      dismissible={!submitting}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <Button variant="primary" icon="upload" loading={submitting} onClick={() => void submit()}>
            Import image
          </Button>
        </>
      }
    >
      <div style={styles.stack}>
        {formError ? (
          <InlineNotice tone="danger" title="Import failed">
            <p>{formError}</p>
          </InlineNotice>
        ) : null}

        <Field
          label="Host directory path"
          required
          error={errors.path}
          hint="Absolute path on the machine running the daemon, not inside the browser or a guest."
        >
          <Input
            value={path}
            leadingIcon="folder"
            placeholder={"D:\\android\\system-images\\android-35\\google_apis\\x86_64"}
            invalid={Boolean(errors.path)}
            onChange={(event) => setPath(event.target.value)}
          />
        </Field>

        <div style={styles.fieldGridTwo}>
          <Field label="Display name" error={errors.name} hint="Optional; defaults to the directory name.">
            <Input
              value={name}
              placeholder="Internal build · API 35"
              onChange={(event) => setName(event.target.value)}
            />
          </Field>
          <Field label="API level override" error={errors.apiLevel} hint="Optional; blank detects from metadata.">
            <Input
              value={apiLevel}
              type="number"
              min={1}
              max={99}
              inputMode="numeric"
              placeholder="35"
              invalid={Boolean(errors.apiLevel)}
              onChange={(event) => setApiLevel(event.target.value)}
            />
          </Field>
        </div>

        <Field label="ABI override" hint="Optional; use only when the image metadata does not declare an ABI.">
          <Select value={abi} onChange={(event) => setAbi(event.target.value)}>
            <option value="">Detect automatically</option>
            <option value="x86_64">x86_64</option>
            <option value="x86">x86</option>
            <option value="arm64-v8a">arm64-v8a</option>
            <option value="armeabi-v7a">armeabi-v7a</option>
          </Select>
        </Field>

        <InlineNotice tone="info" title="Custom images start unverified">
          <p>
            The image is registered as <code>custom</code> and <code>unverified</code>. Run{" "}
            <strong>Verify OOBE</strong> after importing to boot it once and record setup-wizard evidence
            before using it for Play-compatible automation.
          </p>
        </InlineNotice>
      </div>
    </Modal>
  );
}

/* -------------------------------------------------------------------------- */
/* Download dry-run + confirmation dialog                                      */
/* -------------------------------------------------------------------------- */

export function DownloadImageDialog({
  open,
  image,
  preview,
  onClose,
  onConfirm,
}: {
  open: boolean;
  image: SystemImage | null;
  preview: SystemImage | null;
  onClose: () => void;
  onConfirm: () => void;
}) {
  if (!image) return null;
  const view = preview ?? image;
  const packageName = view.package || image.package || "—";
  const target = view.local_path ?? image.local_path ?? `SDK system-images/${packageName}`;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Install system image"
      description="Review the dry-run plan, then start the download."
      size="md"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" icon="download" onClick={onConfirm}>
            Install image
          </Button>
        </>
      }
    >
      <div style={styles.stack}>
        <InlineNotice tone="info" title="Dry-run preview">
          <p>
            Nothing has been downloaded yet. Confirming starts a long-running download and install on
            the daemon; the image becomes available to templates once it finishes.
          </p>
        </InlineNotice>

        <DetailList
          items={[
            { label: "Image", value: view.display_name ?? view.id },
            {
              label: "Android",
              value: `API ${view.api_level ?? "—"} · ${view.tag ?? "—"} · ${view.abi ?? "—"}`,
            },
            { label: "Package", value: <Mono>{packageName}</Mono> },
            { label: "Download", value: <Mono title={view.url ?? undefined}>{view.url ?? "—"}</Mono> },
            { label: "Size on disk", value: formatBytes(view.size_bytes) },
            {
              label: "SHA-256",
              value: (
                <Mono title={view.sha256 ?? undefined}>
                  {view.sha256 ? `${view.sha256.slice(0, 16)}…` : "—"}
                </Mono>
              ),
            },
            { label: "Install target", value: <Mono>{target}</Mono> },
          ]}
        />

        {view.installed ? (
          <InlineNotice tone="warning" title="Already reported as installed">
            <p>
              The daemon already lists this image as installed. Confirming re-validates the package and
              repairs missing files.
            </p>
          </InlineNotice>
        ) : null}

        <Hint>
          Downloads can take several minutes. The REST API does not stream byte-level progress, so the
          install runs in the background with an indeterminate progress bar and a completion toast.
        </Hint>
      </div>
    </Modal>
  );
}

/* -------------------------------------------------------------------------- */
/* Verify OOBE dialog                                                          */
/* -------------------------------------------------------------------------- */

type VerifyState = "idle" | "running" | "done" | "error";

export function VerifyOobeDialog({
  open,
  image,
  onClose,
  onVerified,
}: {
  open: boolean;
  image: SystemImage | null;
  onClose: () => void;
  onVerified: (record: OobeRecord) => void;
}) {
  const [state, setState] = useState<VerifyState>("idle");
  const [keepAvd, setKeepAvd] = useState(false);
  const [timeoutS, setTimeoutS] = useState("600");
  const [record, setRecord] = useState<OobeRecord | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setState("idle");
    setKeepAvd(false);
    setTimeoutS("600");
    setRecord(null);
    setError(null);
  }, [open, image?.id]);

  const running = state === "running";
  const installed = image?.installed === true;

  const run = async (): Promise<void> => {
    if (!image) return;
    setState("running");
    setError(null);
    try {
      const result = await api.verifyOobeImage({
        image_id: image.id,
        timeout_s: Number(timeoutS),
        keep_avd: keepAvd,
      });
      setRecord(result);
      setState("done");
      onVerified(result);
    } catch (caught) {
      setError(errorMessage(caught));
      setState("error");
    }
  };

  if (!image) return null;

  const footer =
    state === "running" ? (
      <Button variant="secondary" disabled loading>
        Verifying…
      </Button>
    ) : state === "done" ? (
      <>
        <Button variant="ghost" onClick={() => setState("idle")}>
          Verify again
        </Button>
        <Button variant="primary" onClick={onClose}>
          Close
        </Button>
      </>
    ) : state === "error" ? (
      <>
        <Button variant="ghost" onClick={onClose}>
          Close
        </Button>
        <Button variant="primary" icon="refresh" onClick={() => void run()}>
          Try again
        </Button>
      </>
    ) : (
      <>
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button
          variant="primary"
          icon="shield"
          disabled={!installed}
          onClick={() => void run()}
        >
          Start verification
        </Button>
      </>
    );

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`Verify OOBE · ${image.display_name ?? image.id}`}
      description="Boots a temporary AVD from this image and records first-run setup evidence."
      size="md"
      dismissible={!running}
      footer={footer}
    >
      <div style={styles.stack}>
        {state === "idle" || state === "running" ? (
          <>
            <InlineNotice tone="warning" title="This boots a temporary AVD">
              <p>
                Verification cold-boots a throwaway AVD from this system image, waits for the Android
                setup wizard activity, captures a screenshot and writes the timings into the OOBE
                matrix. It needs the emulator, hypervisor and roughly one image&apos;s worth of RAM, and
                can take several minutes. The temporary AVD is deleted afterwards unless you keep it.
              </p>
            </InlineNotice>

            {!installed ? (
              <InlineNotice tone="danger" title="Image not installed">
                <p>Download and install the image before verifying its first-run experience.</p>
              </InlineNotice>
            ) : null}

            <Field label="Verification timeout" hint="How long the daemon may wait for the setup wizard.">
              <Select
                value={timeoutS}
                disabled={running}
                onChange={(event) => setTimeoutS(event.target.value)}
                options={[
                  { value: "300", label: "5 minutes" },
                  { value: "600", label: "10 minutes" },
                  { value: "1200", label: "20 minutes" },
                  { value: "1800", label: "30 minutes" },
                ]}
              />
            </Field>

            <Checkbox
              label="Keep the temporary AVD"
              hint="Useful for debugging a failed verification; otherwise the AVD is removed automatically."
              checked={keepAvd}
              disabled={running}
              onChange={(event) => setKeepAvd(event.target.checked)}
            />

            {running ? (
              <>
                <ProgressBar
                  indeterminate
                  label="Booting temporary AVD and checking the setup wizard"
                />
                <Hint>
                  The dialog cannot be closed while the daemon is working. If the call is interrupted,
                  the timeout above bounds the server-side operation.
                </Hint>
              </>
            ) : null}
          </>
        ) : null}

        {state === "error" ? (
          <InlineNotice tone="danger" title="Verification failed">
            <p>{error}</p>
          </InlineNotice>
        ) : null}

        {state === "done" && record ? (
          <div style={styles.stack}>
            <InlineNotice
              tone={record.status === "verified" ? "success" : "warning"}
              title={`OOBE ${oobeStatusLabel(record.status)}`}
            >
              <p>
                {record.status === "verified"
                  ? "The setup wizard activity was reached and evidence was written to the matrix."
                  : "The verification finished without a clean setup-wizard result. Inspect the evidence below."}
              </p>
            </InlineNotice>
            <DetailList
              items={[
                { label: "Status", value: <StatusPill status={record.status} tone={oobeTone(record.status)} /> },
                { label: "Activity", value: <Mono>{record.activity || "—"}</Mono> },
                { label: "Screenshot", value: <Mono>{record.screenshot_path || "—"}</Mono> },
                { label: "Boot time", value: formatDuration(record.boot_seconds) },
                { label: "Reset time", value: formatDuration(record.reset_seconds) },
                { label: "Checked", value: formatDateTime(record.checked_at) },
                { label: "Checked by", value: record.checked_by || "—" },
                {
                  label: "Evidence",
                  value: record.evidence?.length ? (
                    <span style={styles.rowTight}>
                      {record.evidence.map((item) => (
                        <Badge key={item} tone="info">
                          {item}
                        </Badge>
                      ))}
                    </span>
                  ) : (
                    "—"
                  ),
                },
                ...(record.notes ? [{ label: "Notes", value: record.notes }] : []),
              ]}
            />
          </div>
        ) : null}
      </div>
    </Modal>
  );
}
