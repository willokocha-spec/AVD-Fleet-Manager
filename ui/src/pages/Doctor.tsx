import { useMemo, useState } from "react";

import { ApiError, api } from "@/lib/api";
import {
  Badge,
  Button,
  ButtonLink,
  Card,
  Checkbox,
  CodeBlock,
  ConfirmDialog,
  EmptyState,
  IconButton,
  InlineNotice,
  PageHeader,
  Skeleton,
  StatCard,
  StatusPill,
  Table,
  type Column,
  useToast,
} from "@/lib/components";
import { useAsync } from "@/lib/hooks";
import type { PageMeta } from "@/lib/page-meta";
import type { CheckStatus, DoctorCheck, DoctorReport, HostOS, SystemInfo } from "@/lib/types";
import { copyText, formatDateTime, formatDuration, humanize, pluralize } from "@/lib/utils";

export const meta: PageMeta = {
  path: "/doctor",
  title: "Doctor",
  icon: "doctor",
  nav: false,
  description: "Host environment health checks and fixes",
};

/* -------------------------------------------------------------------------- */
/* Data loading                                                                */
/* -------------------------------------------------------------------------- */

interface DoctorData {
  report: DoctorReport;
  system: SystemInfo;
}

async function loadDoctor(slow: boolean): Promise<DoctorData> {
  const [report, system] = await Promise.all([api.runDoctor(slow), api.getSystem()]);
  return { report, system };
}

/* -------------------------------------------------------------------------- */
/* Helpers                                                                     */
/* -------------------------------------------------------------------------- */

const STATUS_ORDER: Record<CheckStatus, number> = { fail: 0, warn: 1, skip: 2, ok: 3 };

function errorText(error: unknown): string {
  if (!error) return "";
  if (error instanceof ApiError) {
    return [error.message, error.hint].filter(Boolean).join(" — ");
  }
  if (error instanceof Error) return error.message;
  return String(error);
}

/**
 * Platform-specific remediation for hardware acceleration. The doctor already
 * reports a hint; these are the concrete steps an operator can act on.
 */
function accelerationHints(os: HostOS | undefined): string[] {
  switch (os) {
    case "windows":
      return [
        "Enable Intel VT-x / AMD-V (SVM) in the BIOS/UEFI firmware.",
        "Turn on the Windows Hypervisor Platform Windows feature (or Hyper-V), then reboot the host.",
        "On AMD hosts without Hyper-V, install the Android Emulator Hypervisor Driver (AEHD) — the installer can do this.",
        "If this Windows machine is itself a VM, enable nested virtualisation in the outer hypervisor.",
      ];
    case "linux":
      return [
        "Load KVM (`sudo modprobe kvm_intel` or `kvm_amd`) and install the qemu-kvm packages.",
        "Make sure /dev/kvm exists and your user is in the `kvm` group (`sudo usermod -aG kvm $USER`).",
        "In a VM, expose nested virtualisation so the guest CPU advertises vmx/svm.",
      ];
    case "macos":
      return [
        "HVF needs a Mac with hardware virtualisation support; verify with `sysctl kern.hv_support`.",
        "Nested virtualisation is not supported in most macOS VMs — AVDs need a bare-metal host.",
      ];
    default:
      return [
        "Enable CPU virtualisation in the host firmware and install the platform hypervisor support.",
        "Re-run the doctor after rebooting to confirm the accelerator probe succeeds.",
      ];
  }
}

/* -------------------------------------------------------------------------- */
/* Page                                                                        */
/* -------------------------------------------------------------------------- */

export default function DoctorPage() {
  const toast = useToast();
  const [slow, setSlow] = useState(false);
  const [fixTarget, setFixTarget] = useState<DoctorCheck | null>(null);
  const { data, error, loading, reload } = useAsync(() => loadDoctor(slow), [slow]);

  const initialLoading = loading && data === null;
  const report = data?.report;
  const system = data?.system;

  const counts = useMemo(() => {
    const checks = report?.checks ?? [];
    const count = (status: CheckStatus): number =>
      checks.filter((check) => check.status === status).length;
    return {
      total: checks.length,
      ok: count("ok"),
      warn: count("warn"),
      fail: count("fail"),
      skip: count("skip"),
      fixable: checks.filter((check) => check.fixable).length,
    };
  }, [report]);

  const orderedChecks = useMemo(
    () =>
      [...(report?.checks ?? [])].sort((left, right) => {
        const byStatus = STATUS_ORDER[left.status] - STATUS_ORDER[right.status];
        return byStatus !== 0 ? byStatus : left.title.localeCompare(right.title);
      }),
    [report],
  );

  const host = system ?? report?.host;
  const hypervisor = host?.hypervisor;
  const accelCheck = report?.checks.find((check) =>
    /accel|hypervis/i.test(`${check.id} ${check.title}`),
  );
  const accelerationBlocked =
    accelCheck?.status === "fail" ||
    system?.accelerator_ok === false ||
    host?.acceleration_available === false ||
    hypervisor?.available === false;
  const hostOs: HostOS | undefined = system?.os ?? report?.host?.os;

  const copyCommand = async (check: DoctorCheck): Promise<void> => {
    if (!check.fix_command) return;
    const copied = await copyText(check.fix_command);
    if (copied) {
      toast.success("Fix command copied", check.fix_command);
    } else {
      toast.error("Copy failed", "Select the command and copy it manually.");
    }
  };

  const applyFix = async (): Promise<void> => {
    if (!fixTarget) return;
    const check = fixTarget;
    try {
      const result = await api.fixDoctorCheck(check.id);
      if (result.ok) {
        toast.success(result.message ?? `Applied fix for ${check.title}.`);
      } else {
        toast.warning(
          "Fix not applied",
          result.message ?? `${check.title} cannot be fixed automatically.`,
        );
      }
    } catch (caught) {
      toast.error("Fix failed", errorText(caught));
    } finally {
      setFixTarget(null);
      reload();
    }
  };

  const columns: Column<DoctorCheck>[] = [
    {
      key: "status",
      header: "Status",
      width: "120px",
      render: (check) => <StatusPill status={check.status} />,
    },
    {
      key: "check",
      header: "Check",
      width: "220px",
      render: (check) => (
        <div>
          <div style={{ color: "var(--text)", fontWeight: 560 }}>{check.title}</div>
          <div className="muted small">
            <code>{check.id}</code>
          </div>
        </div>
      ),
    },
    {
      key: "detail",
      header: "Finding & remediation",
      render: (check) => (
        <div style={{ display: "grid", gap: "3px", maxWidth: "64ch" }}>
          <span>{check.detail || "No detail reported."}</span>
          {check.hint ? (
            <span className="muted small" style={{ display: "inline-flex", gap: "6px" }}>
              <span aria-hidden="true">↳</span>
              {check.hint}
            </span>
          ) : null}
          {check.fix_command ? (
            <code style={{ fontSize: "var(--fs-xs)", justifySelf: "start" }}>{check.fix_command}</code>
          ) : null}
        </div>
      ),
    },
    {
      key: "action",
      header: "Action",
      align: "right",
      width: "150px",
      render: (check) => {
        if (check.fixable) {
          return (
            <Button
              size="sm"
              variant="secondary"
              icon="zap"
              aria-label={`Fix ${check.title}`}
              onClick={() => setFixTarget(check)}
            >
              Fix
            </Button>
          );
        }
        if (check.fix_command) {
          return (
            <IconButton
              size="sm"
              label={`Copy fix command for ${check.title}`}
              icon="copy"
              variant="secondary"
              onClick={() => void copyCommand(check)}
            />
          );
        }
        return (
          <span className="muted small">
            {check.status === "fail" || check.status === "warn" ? "Manual" : "—"}
          </span>
        );
      },
    },
  ];

  const reportOk = report?.ok ?? false;
  const checkedAt = report?.checked_at;

  return (
    <div className="page">
      <PageHeader
        title="Doctor"
        eyebrow="Environment"
        description="Inspect the host, Android toolchain, acceleration and services. Fast checks run by default; enable slow checks for the deeper probes."
        meta={
          <>
            {report ? (
              <StatusPill
                status={reportOk ? "ok" : "fail"}
                label={reportOk ? "All checks passed" : "Action required"}
                pulse={!reportOk}
              />
            ) : null}
            {checkedAt ? <Badge tone="neutral" icon="clock">Checked {formatDateTime(checkedAt)}</Badge> : null}
            <Badge tone={slow ? "accent" : "neutral"} icon={slow ? "zap" : "check"}>
              {slow ? "Slow checks" : "Fast checks"}
            </Badge>
          </>
        }
        actions={
          <>
            <Button
              variant="secondary"
              icon="refresh"
              loading={loading}
              onClick={reload}
            >
              Re-run checks
            </Button>
            <ButtonLink to="/settings" variant="secondary" icon="settings">
              Configuration
            </ButtonLink>
          </>
        }
      />

      {error ? (
        <InlineNotice
          tone="danger"
          title="Doctor report could not be loaded"
          action={
            <Button size="sm" variant="secondary" icon="refresh" onClick={reload}>
              Retry
            </Button>
          }
        >
          <p>{errorText(error)}</p>
          {data ? <p>Showing the last report received before the failure.</p> : null}
        </InlineNotice>
      ) : null}

      <div className="stat-grid">
        <StatCard
          label="Passed"
          value={report ? counts.ok : "—"}
          hint={`${counts.total} checks total`}
          icon="check-circle"
          tone="success"
          loading={initialLoading}
        />
        <StatCard
          label="Warnings"
          value={report ? counts.warn : "—"}
          hint="Non-blocking issues"
          icon="alert-triangle"
          tone="warning"
          loading={initialLoading}
        />
        <StatCard
          label="Failures"
          value={report ? counts.fail : "—"}
          hint="Blocking environment issues"
          icon="alert-circle"
          tone={counts.fail > 0 ? "danger" : "default"}
          loading={initialLoading}
        />
        <StatCard
          label="Skipped"
          value={report ? counts.skip : "—"}
          hint="Checks that need another capability"
          icon="info"
          loading={initialLoading}
        />
      </div>

      {initialLoading ? (
        <>
          <Card title="Acceleration" icon="cpu">
            <div className="skeleton-card">
              <Skeleton lines={3} />
            </div>
          </Card>
          <Card title="Checks" icon="doctor">
            <div className="skeleton-card">
              <Skeleton lines={5} />
            </div>
          </Card>
        </>
      ) : error && !data ? null : (
        <>
          <div className="grid-2">
            <Card
              title="Hardware acceleration"
              description="Verdict from the host probe and the acceleration doctor check."
              icon="cpu"
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "var(--space-2)",
                  flexWrap: "wrap",
                  marginBottom: "var(--space-3)",
                }}
              >
                <StatusPill
                  status={accelerationBlocked ? "fail" : "ok"}
                  label={accelerationBlocked ? "Blocked" : "Available"}
                  pulse={!accelerationBlocked}
                />
                <span className="muted small">
                  {hypervisor?.detail ??
                    accelCheck?.detail ??
                    "No accelerator detail was reported by the host probe."}
                </span>
              </div>

              {accelerationBlocked ? (
                <InlineNotice
                  tone="danger"
                  title="Hardware acceleration is unavailable on this host"
                >
                  <p>
                    AVDs can fall back to software emulation, but hardware-accelerated boots
                    (WHPX / AEHD / KVM / HVF) are not available. Expect very slow boots and
                    unreliable graphics for real emulator runs.
                  </p>
                  {accelCheck?.detail ? <p>{accelCheck.detail}</p> : null}
                  <ul style={{ margin: "var(--space-2) 0 0", paddingLeft: "1.1rem", display: "grid", gap: "4px" }}>
                    {accelerationHints(hostOs).map((hint) => (
                      <li key={hint}>{hint}</li>
                    ))}
                  </ul>
                  {accelCheck?.hint ? <p>{accelCheck.hint}</p> : null}
                  {hypervisor?.requires_reboot ? (
                    <p>A reboot is required before the accelerator probe can succeed.</p>
                  ) : null}
                  {hypervisor?.nested === false ? (
                    <p>
                      Nested virtualisation appears to be disabled; enable it in the outer
                      hypervisor when this host is a VM.
                    </p>
                  ) : null}
                </InlineNotice>
              ) : (
                <InlineNotice tone="success" title="Hardware acceleration is available">
                  <p>
                    {hypervisor?.kind && hypervisor.kind !== "none"
                      ? `${humanize(hypervisor.kind)} is detected and available.`
                      : "The host probe reports a usable accelerator."}
                  </p>
                </InlineNotice>
              )}

              {accelCheck?.fix_command ? (
                <div style={{ marginTop: "var(--space-3)" }}>
                  <CodeBlock
                    code={accelCheck.fix_command}
                    language="shell"
                    filename="Suggested command"
                  />
                </div>
              ) : null}

              <p className="muted small" style={{ marginTop: "var(--space-3)" }}>
                Blocked acceleration does not prevent the GUI, doctor, AVD management or fleet
                automation from working; it only limits real emulator boots on this host.
              </p>
            </Card>

            <Card
              title="Host & runtime"
              description="Operating system, architecture, Python and daemon details from GET /system."
              icon="server"
            >
              {system ? (
                <>
                  <dl className="kv-list">
                    <div className="kv-list__row">
                      <dt>Operating system</dt>
                      <dd>
                        {[system.os_name, system.os_version].filter(Boolean).join(" ") ||
                          humanize(system.os) ||
                          "—"}
                      </dd>
                    </div>
                    <div className="kv-list__row">
                      <dt>Architecture</dt>
                      <dd>
                        {system.arch ?? "—"}
                        {system.machine ? ` (${system.machine})` : ""}
                      </dd>
                    </div>
                    <div className="kv-list__row">
                      <dt>Accelerator</dt>
                      <dd>
                        <StatusPill
                          status={accelerationBlocked ? "fail" : "ok"}
                          label={
                            hypervisor?.available
                              ? humanize(hypervisor.kind)
                              : accelerationBlocked
                                ? "Unavailable"
                                : "Unknown"
                          }
                          icon="cpu"
                        />
                      </dd>
                    </div>
                    <div className="kv-list__row">
                      <dt>Python</dt>
                      <dd>{system.python_version ?? "—"}</dd>
                    </div>
                    <div className="kv-list__row">
                      <dt>Daemon</dt>
                      <dd>
                        {system.version ? `v${system.version}` : "—"}
                        {system.edition ? ` · ${humanize(system.edition)}` : ""}
                      </dd>
                    </div>
                    <div className="kv-list__row">
                      <dt>Uptime</dt>
                      <dd>{formatDuration(system.uptime_s)}</dd>
                    </div>
                    <div className="kv-list__row">
                      <dt>CPU / RAM</dt>
                      <dd>
                        {system.cpu_count ?? "—"} cores · {system.total_ram_gb ?? "—"} GB
                      </dd>
                    </div>
                    <div className="kv-list__row">
                      <dt>Free disk</dt>
                      <dd>{system.free_disk_gb !== undefined ? `${system.free_disk_gb} GB` : "—"}</dd>
                    </div>
                    <div className="kv-list__row">
                      <dt>Environment</dt>
                      <dd>
                        {[
                          system.is_wsl ? "WSL" : null,
                          system.is_container ? "Container" : null,
                          system.is_admin ? "Administrator" : "Standard user",
                        ]
                          .filter(Boolean)
                          .join(" · ")}
                      </dd>
                    </div>
                    <div className="kv-list__row">
                      <dt>SDK root</dt>
                      <dd>
                        <code>{system.sdk_root ?? "—"}</code>
                      </dd>
                    </div>
                    <div className="kv-list__row">
                      <dt>Data directory</dt>
                      <dd>
                        <code>{system.data_dir ?? "—"}</code>
                      </dd>
                    </div>
                  </dl>
                  <p className="muted small" style={{ marginTop: "var(--space-3)" }}>
                    Host details are informational; the doctor checks above are the source of
                    truth for readiness.
                  </p>
                </>
              ) : (
                <EmptyState
                  compact
                  icon="server"
                  title="System information unavailable"
                  description="The daemon did not return host information for this report."
                />
              )}
            </Card>
          </div>

          <Card
            title="Checks"
            description={
              report?.summary
                ? report.summary
                : `${pluralize(counts.total, "check")} reported${counts.fixable > 0 ? ` · ${counts.fixable} automatically fixable` : ""}`
            }
            icon="doctor"
            flush
            actions={
              <Checkbox
                label="Run slow checks"
                hint="Adds the deeper, slower probes."
                checked={slow}
                onChange={(event) => setSlow(event.target.checked)}
              />
            }
            footer={
              <span>
                Report values are redacted by the daemon: bearer tokens and proxy passwords are
                never returned by the API and are never rendered here.
              </span>
            }
          >
            <Table
              dense
              columns={columns}
              rows={orderedChecks}
              rowKey={(check) => check.id}
              loading={initialLoading}
              skeletonRows={4}
              empty={
                <EmptyState
                  compact
                  icon="doctor"
                  title="No checks reported"
                  description="The daemon returned an empty doctor report."
                  action={
                    <Button size="sm" variant="secondary" icon="refresh" onClick={reload}>
                      Re-run checks
                    </Button>
                  }
                />
              }
            />
          </Card>
        </>
      )}

      <ConfirmDialog
        open={fixTarget !== null}
        title={fixTarget ? `Apply fix: ${fixTarget.title}` : "Apply fix"}
        description="The daemon runs the documented repair for this check against the host."
        confirmLabel="Apply fix"
        tone="primary"
        onCancel={() => setFixTarget(null)}
        onConfirm={applyFix}
      >
        {fixTarget?.fix_command ? (
          <CodeBlock
            code={fixTarget.fix_command}
            language="shell"
            filename="Command"
            maxHeight={160}
          />
        ) : (
          <p className="muted small">
            The daemon runs its own repair logic for <code>{fixTarget?.id}</code>; no shell command
            is needed.
          </p>
        )}
      </ConfirmDialog>
    </div>
  );
}
