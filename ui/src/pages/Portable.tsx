import { api } from "@/lib/api";
import {
  Badge,
  Button,
  ButtonLink,
  Card,
  CodeBlock,
  EmptyState,
  Icon,
  InlineNotice,
  PageHeader,
  Skeleton,
  StatCard,
  StatusPill,
} from "@/lib/components";
import { useAsync } from "@/lib/hooks";
import type { PageMeta } from "@/lib/page-meta";
import type { CheckResult, CheckStatus, PortableStatus } from "@/lib/types";
import { humanize } from "@/lib/utils";

import {
  apiErrorStatus,
  checkStatusLabel,
  describeError,
  isEndpointUnavailable,
} from "./_components/automation/shared";

export const meta: PageMeta = {
  path: "/portable",
  title: "Portable",
  icon: "portable",
  nav: 90,
  description: "Portable bundle status and preflight checks",
};

/* -------------------------------------------------------------------------- */
/* Static check knowledge                                                      */
/* -------------------------------------------------------------------------- */

interface CheckGuide {
  keys: string[];
  title: string;
  explanation: string;
  link?: { label: string; href: string };
}

const CHECK_GUIDES: readonly CheckGuide[] = [
  {
    keys: ["filesystem", "file-system", "fs"],
    title: "Filesystem type",
    explanation:
      "FAT32 is refused (4 GiB per-file cap breaks AVD data images); exFAT is warned about because it loses POSIX permissions. NTFS, APFS, ext4, XFS and Btrfs are supported.",
    link: {
      label: "Microsoft filesystem comparison",
      href: "https://learn.microsoft.com/en-us/windows/win32/fileio/filesystem-functionality-comparison",
    },
  },
  {
    keys: ["free-space", "free-disk-space", "disk-space", "space"],
    title: "Free space",
    explanation:
      "The free space on the bundle volume must cover the bundle itself plus room for AVD data (roughly 6–8 GiB of userdata per instance) and snapshots.",
  },
  {
    keys: ["write-speed", "write-performance", "speed"],
    title: "Write speed",
    explanation:
      "A sequential write test catches slow USB sticks: below roughly 10 MB/s the bundle is technically valid but emulator I/O becomes unusable.",
  },
  {
    keys: ["noexec", "noexec-mount", "mount-options"],
    title: "noexec mount",
    explanation:
      "On Linux/macOS the bundle root must not be mounted with noexec, otherwise the bundled Python runtime, adb and emulator binaries cannot be executed. Windows has no mount-flag equivalent.",
    link: { label: "mount(8) noexec", href: "https://man7.org/linux/man-pages/man8/mount.8.html" },
  },
  {
    keys: ["quarantine", "mark-of-the-web", "motw", "zone-identifier"],
    title: "Quarantine / Mark-of-the-Web",
    explanation:
      "Windows adds a Zone.Identifier stream to browser-downloaded files; the Windows launcher removes it with Unblock-File on first run. macOS Gatekeeper quarantine must be approved once via System Settings → Privacy & Security.",
    link: {
      label: "PowerShell Unblock-File",
      href: "https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/unblock-file",
    },
  },
  {
    keys: ["hypervisor", "hypervisor-availability", "acceleration"],
    title: "Hypervisor availability",
    explanation:
      "The bundle can only detect virtualisation (WHPX/AEHD on Windows, KVM on Linux, HVF on macOS); it can never install it. A host without a usable hypervisor cannot boot AVDs.",
    link: {
      label: "Android emulator acceleration",
      href: "https://developer.android.com/studio/run/emulator-acceleration",
    },
  },
];

function normalizedCheckId(id: string): string {
  return id
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-");
}

function guideForCheckId(id: string): CheckGuide | undefined {
  const normalized = normalizedCheckId(id);
  return CHECK_GUIDES.find((guide) => guide.keys.includes(normalized));
}

function checkTitle(check: CheckResult): string {
  return check.title?.trim() || guideForCheckId(check.id)?.title || humanize(check.id);
}

function checkTone(status: CheckStatus | "unknown"): "success" | "warning" | "danger" | "neutral" {
  switch (status) {
    case "ok":
      return "success";
    case "warn":
      return "warning";
    case "fail":
      return "danger";
    default:
      return "neutral";
  }
}

const DOC_REFERENCES = [
  {
    name: "docs/portable.md",
    description:
      "Portable edition guide: build/relocation commands, first-run unblocking, the documented leftovers allowlist and the `portable verify` workflow.",
  },
  {
    name: "research/R7-portable.md",
    description:
      "Verified fact base behind this page: enforceable filesystem limits, noexec/quarantine behaviour, relocation hazards and the projected leftovers list.",
  },
  {
    name: "docs/troubleshooting.md",
    description: "Host diagnostics for hypervisor, USB device access and emulator startup failures.",
  },
];

const EXTERNAL_REFERENCES = [
  {
    label: "Android environment variables (ANDROID_USER_HOME, ANDROID_AVD_HOME, STUDIO_PROPERTIES)",
    href: "https://developer.android.com/tools/variables",
  },
  {
    label: "Android Studio config (STUDIO_PROPERTIES / idea.properties, default leftover paths)",
    href: "https://developer.android.com/studio/intro/studio-config",
  },
  {
    label: "Apple Gatekeeper & notarisation",
    href: "https://support.apple.com/en-us/102445",
  },
  {
    label: "Linux exFAT driver mount options",
    href: "https://docs.kernel.org/filesystems/exfat.html",
  },
];

/* -------------------------------------------------------------------------- */
/* Page                                                                        */
/* -------------------------------------------------------------------------- */

/**
 * U7 — portable edition.
 *
 * Explains installed vs portable, renders `/portable/status` + `/portable/checks`
 * (filesystem, noexec, quarantine, free space, write speed, hypervisor) and
 * degrades to static guidance when the daemon returns 501/503 for those
 * endpoints. Bundle building/verification is CLI-only in the frozen contract.
 */
export default function PortablePage() {
  const status = useAsync(() => api.getPortableStatus(), []);
  const checks = useAsync(() => api.getPortableChecks(), []);
  const system = useAsync(() => api.getSystem(), []);

  const portableStatus: PortableStatus | null = status.data;
  const edition = portableStatus?.edition ?? system.data?.edition ?? "unknown";
  const portable = portableStatus?.portable ?? edition === "portable";
  const root = portableStatus?.root ?? system.data?.data_dir ?? "—";
  const host = portableStatus
    ? `${portableStatus.host_os ?? "unknown"} · ${portableStatus.host_arch ?? "unknown"}`
    : system.data
      ? `${system.data.os ?? "unknown"} · ${system.data.arch ?? "unknown"}`
      : "—";
  const leftovers = portableStatus?.leftovers ?? [];

  const statusUnavailable = isEndpointUnavailable(status.error);
  const checksUnavailable = isEndpointUnavailable(checks.error);
  const checksData = checks.data ?? portableStatus?.checks ?? [];
  const checkError = !checksUnavailable && checks.error ? checks.error : null;

  const okCount = checksData.filter((check) => check.status === "ok").length;
  const warnCount = checksData.filter((check) => check.status === "warn").length;
  const failCount = checksData.filter((check) => check.status === "fail").length;
  const skipCount = checksData.filter((check) => check.status === "skip").length;

  const statusCode = apiErrorStatus(status.error) ?? apiErrorStatus(checks.error);

  const refresh = (): void => {
    status.reload();
    checks.reload();
    system.reload();
  };

  return (
    <div className="page">
      <PageHeader
        title="Portable"
        eyebrow="Removable-disk edition"
        description="Run AVD Fleet from a self-contained bundle on a USB/removable disk with no installed Android tooling. This page reports bundle status, preflight checks, documented leftovers and the CLI workflow for building and verifying a bundle."
        actions={
          <>
            <ButtonLink to="/doctor" variant="secondary" icon="doctor">
              Environment doctor
            </ButtonLink>
            <Button
              variant="secondary"
              icon="refresh"
              loading={status.loading || checks.loading}
              onClick={refresh}
            >
              Re-run checks
            </Button>
          </>
        }
        meta={
          <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
            <Badge tone={portable ? "accent" : "neutral"} icon="portable">
              {humanize(edition)} edition
            </Badge>
            <Badge tone={portable ? "success" : "neutral"} icon="hard-drive">
              {portable ? "portable bundle" : "not a portable root"}
            </Badge>
            {failCount > 0 ? <Badge tone="danger">{failCount} failing check(s)</Badge> : null}
            {warnCount > 0 ? <Badge tone="warning">{warnCount} warning(s)</Badge> : null}
          </div>
        }
      />

      {statusUnavailable || checksUnavailable ? (
        <InlineNotice
          tone="warning"
          title="Portable API unavailable on this daemon"
          action={
            <Button size="sm" variant="secondary" icon="refresh" onClick={refresh}>
              Retry
            </Button>
          }
        >
          <p>
            The daemon answered HTTP {statusCode ?? "501/503"} for{" "}
            <code>/portable/status</code> or <code>/portable/checks</code>. This build may not
            include the portable services. The guidance, documentation and CLI workflow below are
            static and remain valid; check results are marked as <strong>not checked</strong>.
          </p>
        </InlineNotice>
      ) : null}

      {!statusUnavailable && status.error ? (
        <InlineNotice
          tone="danger"
          title="Could not load the portable status"
          action={
            <Button size="sm" variant="secondary" icon="refresh" onClick={status.reload}>
              Retry
            </Button>
          }
        >
          <p>{describeError(status.error)}</p>
        </InlineNotice>
      ) : null}

      {checkError ? (
        <InlineNotice
          tone="danger"
          title="Could not load portable preflight checks"
          action={
            <Button size="sm" variant="secondary" icon="refresh" onClick={checks.reload}>
              Retry
            </Button>
          }
        >
          <p>{describeError(checkError)}</p>
        </InlineNotice>
      ) : null}

      {failCount > 0 ? (
        <InlineNotice tone="danger" title={`${failCount} preflight check(s) failed`}>
          <p>
            Fix the failing checks before relying on this bundle: a FAT32 volume, a noexec mount, a
            missing hypervisor or missing free space will break boots and script runs in ways that are
            hard to diagnose later.
          </p>
        </InlineNotice>
      ) : null}

      {warnCount > 0 && failCount === 0 ? (
        <InlineNotice tone="warning" title={`${warnCount} preflight warning(s)`}>
          <p>
            The bundle can run, but the warned conditions may cause fragile behaviour (slow I/O,
            quarantine prompts, missing acceleration). Review the check details below.
          </p>
        </InlineNotice>
      ) : null}

      <div className="stat-grid">
        <StatCard
          label="Edition"
          icon="portable"
          loading={status.loading && !portableStatus}
          value={humanize(edition)}
          hint="installed (per-user) or portable (removable disk)"
        />
        <StatCard
          label="Bundle root"
          icon="folder"
          loading={status.loading && !portableStatus}
          value={
            <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", overflowWrap: "anywhere" }}>
              {root}
            </span>
          }
          hint={portable ? "portable root detected" : "installed data directory"}
        />
        <StatCard
          label="Relocatable"
          icon="refresh"
          tone={portableStatus && portableStatus.relocatable === false ? "warning" : "default"}
          loading={status.loading && !portableStatus}
          value={
            portableStatus
              ? portableStatus.relocatable === false
                ? "No"
                : "Yes"
              : statusUnavailable
                ? "Unknown"
                : "—"
          }
          hint="survives a drive-letter / mount-point change"
        />
        <StatCard
          label="Leftovers"
          icon="alert-triangle"
          tone={leftovers.length > 0 ? "warning" : "default"}
          loading={status.loading && !portableStatus}
          value={leftovers.length}
          hint="documented writes outside the bundle root"
        />
        <StatCard
          label="Host"
          icon="monitor"
          loading={status.loading && !portableStatus}
          value={host}
          hint="bundles are per-OS and per-architecture"
        />
        <StatCard
          label="Preflight"
          icon="shield"
          tone={failCount > 0 ? "danger" : warnCount > 0 ? "warning" : "default"}
          loading={checks.loading && checksData.length === 0 && !checksUnavailable}
          value={
            checksData.length === 0 && !checksUnavailable
              ? "—"
              : `${okCount} pass · ${failCount} fail`
          }
          hint={
            checksData.length === 0 && !checksUnavailable
              ? "no checks reported yet"
              : `${warnCount} warn · ${skipCount} skip`
          }
        />
      </div>

      <div className="grid-2">
        <Card
          icon="server"
          title="Installed edition"
          tone={portable ? "default" : "accent"}
          description="The regular per-user installation served by the installer/wheel."
        >
          <ul className="type-list">
            <li className="type-list__item">
              <Icon name="check" size={15} />
              <span>Python runtime, CLI and daemon are installed into the user environment.</span>
            </li>
            <li className="type-list__item">
              <Icon name="check" size={15} />
              <span>State lives in the platform data directory (config, database, AVDs, artifacts).</span>
            </li>
            <li className="type-list__item">
              <Icon name="check" size={15} />
              <span>Upgrades and uninstall are managed by the installer / package manager.</span>
            </li>
            <li className="type-list__item">
              <Icon name="alert-triangle" size={15} />
              <span>Not designed to move between drives or machines; paths are host-relative.</span>
            </li>
          </ul>
          <div style={{ marginTop: "var(--space-3)" }}>
            {!portable ? (
              <Badge tone="success" icon="check">
                Current edition
              </Badge>
            ) : (
              <Badge tone="neutral">Not in use</Badge>
            )}
          </div>
        </Card>

        <Card
          icon="portable"
          title="Portable edition"
          tone={portable ? "accent" : "default"}
          description="A self-contained folder on a USB/removable disk, built on the same OS and CPU architecture."
        >
          <ul className="type-list">
            <li className="type-list__item">
              <Icon name="check" size={15} />
              <span>
                Carries its own pinned Python runtime, Android cmdline-tools/platform-tools/emulator,
                Appium + driver, offline UI build and config.
              </span>
            </li>
            <li className="type-list__item">
              <Icon name="check" size={15} />
              <span>
                Launchers rebuild <code>ANDROID_*</code>/<code>UV_*</code>/<code>STUDIO_*</code> and
                temp paths from the bundle root; Windows first run runs <code>Unblock-File</code>.
              </span>
            </li>
            <li className="type-list__item">
              <Icon name="check" size={15} />
              <span>
                <code>--offline</code> bundles need no network on first run when the image/Appium
                caches are pre-seeded.
              </span>
            </li>
            <li className="type-list__item">
              <Icon name="alert-triangle" size={15} />
              <span>
                Cross-OS bundles are unsupported; hypervisor availability, Play images and macOS
                Gatekeeper can never be provided by the bundle.
              </span>
            </li>
          </ul>
          <div style={{ marginTop: "var(--space-3)" }}>
            {portable ? (
              <Badge tone="success" icon="check">
                Current edition
              </Badge>
            ) : (
              <Badge tone="neutral">Installed edition in use</Badge>
            )}
          </div>
        </Card>
      </div>

      <Card
        icon="shield"
        title="Preflight checks"
        description="Run against the bundle root: filesystem, noexec, quarantine, free space, write speed and hypervisor. FAT32 and noexec must be caught before the bundle is used."
        actions={
          <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center", flexWrap: "wrap" }}>
            <Badge tone="success">{okCount} pass</Badge>
            <Badge tone="warning">{warnCount} warn</Badge>
            <Badge tone="danger">{failCount} fail</Badge>
            <Badge tone="neutral">{skipCount} skip</Badge>
          </div>
        }
      >
        {checks.loading && checksData.length === 0 ? (
          <div className="skeleton-card">
            <Skeleton lines={4} />
          </div>
        ) : checksUnavailable ? (
          <>
            <InlineNotice tone="info" title="Check results unavailable">
              <p>
                <code>/api/v1/portable/checks</code> is not available on this daemon. The expected
                checks are listed below with guidance; run <code>avdfleet portable verify</code> or
                the environment doctor to evaluate them.
              </p>
            </InlineNotice>
            <CheckList
              rows={CHECK_GUIDES.map((guide) => ({
                id: guide.title,
                title: guide.title,
                detail: guide.explanation,
                status: "unknown" as const,
                value: null,
                link: guide.link,
              }))}
            />
          </>
        ) : checksData.length === 0 ? (
          <EmptyState
            compact
            icon="shield"
            title="No portable checks reported"
            description="The daemon did not return check results. Run `avdfleet portable verify` on the bundle root and re-run the checks."
            action={
              <Button size="sm" variant="secondary" icon="refresh" onClick={checks.reload}>
                Re-run checks
              </Button>
            }
          />
        ) : (
          <CheckList
            rows={checksData.map((check) => ({
              id: check.id,
              title: checkTitle(check),
              detail: check.detail?.trim() || guideForCheckId(check.id)?.explanation || "",
              status: check.status,
              value: check.value ?? null,
              link: guideForCheckId(check.id)?.link,
            }))}
          />
        )}
      </Card>

      <div className="grid-2">
        <Card
          icon="alert-triangle"
          title="Documented leftovers"
          description="Portable is near-zero-write, not zero-write: a small allowlist outside the bundle root is documented and checked by `avdfleet portable verify`."
        >
          {leftovers.length > 0 ? (
            <ul className="type-list">
              {leftovers.map((path) => (
                <li key={path} className="type-list__item">
                  <Icon name="external-link" size={15} />
                  <code style={{ overflowWrap: "anywhere" }}>{path}</code>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState
              compact
              icon="shield"
              title="No leftovers reported"
              description="Either the daemon has not run the leftover scan yet, or no undocumented writes were detected."
            />
          )}
          <p className="muted small" style={{ marginTop: "var(--space-3)" }}>
            Documented exceptions include Android Studio config/cache/log directories,{" "}
            <code>%USERPROFILE%\.android</code> only when an older tool ignores{" "}
            <code>ANDROID_USER_HOME</code>, Gradle caches and Windows registry entries written by
            installers. These are listed in the allowlist, not treated as zero.
          </p>
        </Card>

        <Card
          icon="refresh"
          title="Moving the bundle"
          description="The launcher detects that the recorded root changed and triggers relocation instead of silently using stale absolute paths."
        >
          <ul className="type-list">
            <li className="type-list__item">
              <Icon name="check" size={15} />
              <span>AVD <code>.ini</code> files are rewritten to the new bundle root.</span>
            </li>
            <li className="type-list__item">
              <Icon name="check" size={15} />
              <span>
                Stale snapshots, lock files and generated emulator state are invalidated; the Python
                runtime is shipped as a plain directory (no venv with absolute shebangs).
              </span>
            </li>
            <li className="type-list__item">
              <Icon name="check" size={15} />
              <span>
                After moving: relaunch once, re-run preflight checks, then <code>portable verify</code>.
              </span>
            </li>
            <li className="type-list__item">
              <Icon name="alert-triangle" size={15} />
              <span>
                Moving between machines invalidates keyring-stored proxy credentials — use the
                passphrase-derived file vault inside the bundle for portability.
              </span>
            </li>
          </ul>
        </Card>
      </div>

      <Card
        icon="file"
        title="Build a portable bundle"
        description="The frozen API exposes portable status and checks only — bundle build and verify run through the CLI on a host with the same OS and CPU architecture as the target machine."
      >
        <ol className="step-list" style={{ marginBottom: "var(--space-4)" }}>
          <li>
            <span className="step-list__index">1</span>
            <div>
              <p className="step-list__title">Build the offline bundle from a prepared cache</p>
              <p className="step-list__hint">
                Build from the installed CLI on a host of the same OS/architecture. The builder refuses
                FAT32 destinations and warns on exFAT/network shares.
              </p>
              <CodeBlock
                code={'avdfleet bundle --offline "<bundle-directory>"'}
                language="powershell"
                filename="PowerShell / bash"
                maxHeight={90}
              />
            </div>
          </li>
          <li>
            <span className="step-list__index">2</span>
            <div>
              <p className="step-list__title">Move it to the removable disk and launch it</p>
              <p className="step-list__hint">
                Use NTFS, APFS, ext4/XFS/Btrfs. The Windows launcher unblocks bundled executables on
                first run; macOS may require once-only approval in Privacy &amp; Security.
              </p>
              <CodeBlock
                code={'"<bundle-directory>\\AVD Fleet.cmd"\n# macOS / Linux\n"<bundle-directory>/AVD Fleet.command"'}
                language="text"
                filename="launch"
                maxHeight={110}
              />
            </div>
          </li>
          <li>
            <span className="step-list__index">3</span>
            <div>
              <p className="step-list__title">Verify the bundle and the leftovers allowlist</p>
              <p className="step-list__hint">
                Verify hashes against the manifest and re-check documented writes. This is CLI-only:
                the daemon intentionally does not expose a verify endpoint.
              </p>
              <CodeBlock
                code={'avdfleet portable verify --root "<bundle-directory>"'}
                language="powershell"
                filename="verify"
                maxHeight={90}
              />
            </div>
          </li>
        </ol>
        <InlineNotice
          tone="info"
          title={checksUnavailable || statusUnavailable ? "Static guidance only" : "No verify endpoint in this build"}
        >
          <p>
            The daemon API in this release serves <code>/portable/status</code> and{" "}
            <code>/portable/checks</code>; build and verify are deliberately CLI operations because
            they touch the filesystem outside the daemon's data directory. Use{" "}
            <strong>Re-run checks</strong> to refresh the read-only report, or{" "}
            <ButtonLink to="/doctor" variant="ghost" size="sm" icon="doctor">
              run the environment doctor
            </ButtonLink>{" "}
            for host diagnostics.
          </p>
        </InlineNotice>
      </Card>

      <div className="grid-2">
        <Card
          icon="file"
          title="Documentation"
          description="Repository docs that ship with the source tree; the daemon does not serve them, so they are listed by path."
        >
          <ul className="type-list">
            {DOC_REFERENCES.map((doc) => (
              <li key={doc.name} className="type-list__item" style={{ alignItems: "flex-start" }}>
                <Icon name="file" size={15} />
                <div>
                  <p style={{ fontWeight: 600, fontFamily: "var(--font-mono)" }}>{doc.name}</p>
                  <p className="muted small">{doc.description}</p>
                </div>
              </li>
            ))}
          </ul>
        </Card>

        <Card
          icon="external-link"
          title="External references"
          description="Primary sources behind the preflight checks and portable environment."
        >
          <ul className="type-list">
            {EXTERNAL_REFERENCES.map((reference) => (
              <li key={reference.href} className="type-list__item">
                <Icon name="external-link" size={15} />
                <a href={reference.href} target="_blank" rel="noreferrer noopener">
                  {reference.label}
                </a>
              </li>
            ))}
          </ul>
          {!portable ? (
            <p className="muted small" style={{ marginTop: "var(--space-3)" }}>
              You are running the <strong>installed edition</strong>. A portable bundle is a separate
              build artefact — this page will light up with portable checks when the daemon is launched
              from a bundle root.
            </p>
          ) : null}
        </Card>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Check list                                                                  */
/* -------------------------------------------------------------------------- */

interface CheckRow {
  id: string;
  title: string;
  detail: string;
  status: CheckStatus | "unknown";
  value: string | null;
  link?: { label: string; href: string };
}

function CheckList({ rows }: { rows: CheckRow[] }) {
  return (
    <ul className="type-list">
      {rows.map((row) => (
        <li key={row.id} className="type-list__item" style={{ alignItems: "flex-start" }}>
          <StatusPill
            status={row.status === "unknown" ? "skip" : row.status}
            tone={checkTone(row.status)}
            label={row.status === "unknown" ? "Not checked" : checkStatusLabel(row.status)}
            title={row.status === "unknown" ? "No check result from this daemon" : undefined}
          />
          <div style={{ flex: "1 1 260px", minWidth: 0 }}>
            <p style={{ fontWeight: 600 }}>{row.title}</p>
            {row.detail ? <p className="muted small">{row.detail}</p> : null}
            {row.link ? (
              <a
                className="small"
                href={row.link.href}
                target="_blank"
                rel="noreferrer noopener"
                style={{ display: "inline-flex", alignItems: "center", gap: 4, marginTop: 4 }}
              >
                {row.link.label}
                <Icon name="external-link" size={12} />
              </a>
            ) : null}
          </div>
          {row.value ? (
            <Badge tone="neutral" title={row.value}>
              {row.value}
            </Badge>
          ) : null}
        </li>
      ))}
    </ul>
  );
}
