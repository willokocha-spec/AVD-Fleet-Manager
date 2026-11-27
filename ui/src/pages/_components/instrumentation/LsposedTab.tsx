/**
 * LSPosed & Zygisk tab.
 *
 * The framework is only usable after a reboot, so this tab separates the module
 * install from framework activation and explains where Xposed module activation
 * and scope actually live (the LSPosed manager).
 */
import { useMemo } from "react";

import { instrumentationApi } from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  InlineNotice,
  Skeleton,
} from "@/lib/components";
import { useAsync } from "@/lib/hooks";
import type { DeviceInstrumentation } from "@/lib/types";

import {
  BUILTIN_LSPOSED_PROFILE,
  DeployProfileCard,
  DeviceInstrumentationCard,
  TargetSummary,
  apiErrorText,
  styles,
} from "@/pages/_components/instrumentation/shared";
import { ToolchainPanel } from "@/pages/_components/instrumentation/ToolchainPanel";

export interface LsposedTabProps {
  devices: DeviceInstrumentation[];
  selected: string[];
  onGoToOverview: () => void;
  onModuleImported?: (moduleId: string) => void;
}

export function LsposedTab({ devices, selected, onGoToOverview, onModuleImported }: LsposedTabProps) {
  const profiles = useAsync(() => instrumentationApi.listProfiles(), []);
  const allProfiles = useMemo(() => profiles.data?.profiles ?? [], [profiles.data]);

  const lsposedProfiles = useMemo(() => {
    const byId = allProfiles.filter((profile) => profile.id === BUILTIN_LSPOSED_PROFILE);
    if (byId.length > 0) return byId;
    return allProfiles.filter((profile) => profile.builtin && profile.name === "LSPosed");
  }, [allProfiles]);

  const counts = useMemo(() => {
    const active = devices.filter((device) => device.capabilities.lsposed_active).length;
    const pending = devices.filter(
      (device) => device.capabilities.lsposed_installed && !device.capabilities.lsposed_active,
    ).length;
    return { active, pending, missing: devices.length - active - pending };
  }, [devices]);

  return (
    <div style={styles.stack}>
      <InlineNotice tone="warning" title="Framework activation is verified after a reboot, not by the file transfer">
        <p>
          The <code>install_lsposed</code> step pushes an LSPosed ZIP to the device and installs it with{" "}
          <code>magisk --install-module</code>. That only makes the module <strong>installed</strong>. The
          shipped LSPosed profile then runs <code>reboot</code> — which is what activates the framework — and{" "}
          <code>install_lsposed_manager</code>, which installs the manager APK that lspd never installs itself.
          The final <code>verify</code> step re-probes <code>lsposed_active</code>; a green install step on its
          own is not proof that the framework is running.
        </p>
      </InlineNotice>

      <InlineNotice tone="info" title="Xposed APK activation lives in the LSPosed manager">
        <p>
          Installing an Xposed APK (<code>install_module</code> on an <code>xposed_apk</code> record) only runs{" "}
          <code>adb install</code>. Enabling the module and choosing its per-app scope happen in the{" "}
          <strong>LSPosed manager</strong> on the device, and the APK install alone does not activate the module.
          The <code>enable_xposed_module</code> / <code>set_xposed_scope</code> steps write LSPosed&apos;s own
          state, but lspd reloads it only after a reboot; the probe reports the module&apos;s enabled flag and
          scope when it can read that database, and the manager remains the authoritative place for both.
        </p>
      </InlineNotice>

      <ToolchainPanel onModuleImported={onModuleImported} />

      <TargetSummary selected={selected} onGoToOverview={onGoToOverview} />

      {devices.length > 0 ? (
        <div style={styles.rowTight}>
          <Badge tone={counts.active > 0 ? "success" : "neutral"} icon="check-circle">
            LSPosed active: {counts.active}
          </Badge>
          <Badge tone={counts.pending > 0 ? "warning" : "neutral"} icon="restart">
            Installed, pending reboot: {counts.pending}
          </Badge>
          <Badge tone="neutral" icon="info">
            Not installed: {counts.missing}
          </Badge>
        </div>
      ) : null}

      {devices.length === 0 ? (
        <EmptyState
          compact
          icon="search"
          title="No framework state yet"
          description="Run Scan on the Overview tab to read Zygisk and LSPosed state from the selected AVDs."
          action={
            <Button size="sm" variant="secondary" icon="shield" onClick={onGoToOverview}>
              Go to Overview
            </Button>
          }
        />
      ) : (
        <div style={styles.cardGrid}>
          {devices.map((device) => (
            <DeviceInstrumentationCard key={device.instance} device={device} />
          ))}
        </div>
      )}

      {profiles.error ? (
        <InlineNotice
          tone="danger"
          title="Could not load instrumentation profiles"
          action={
            <Button size="sm" variant="secondary" icon="refresh" onClick={profiles.reload}>
              Retry
            </Button>
          }
        >
          <p>{apiErrorText(profiles.error)}</p>
        </InlineNotice>
      ) : profiles.loading && !profiles.data ? (
        <Card title="Deploy the LSPosed profile" icon="deploy">
          <div className="skeleton-card">
            <Skeleton lines={4} />
          </div>
        </Card>
      ) : (
        <DeployProfileCard
          profiles={lsposedProfiles}
          targets={selected}
          title="Deploy the LSPosed profile"
          description="Runs the built-in LSPosed profile (Magisk → setup → Zygisk → LSPosed → reboot → manager → verify) on the selected AVDs."
          footerNote="The built-in LSPosed profile has no module bound to its install step: the backend resolves the newest LSPosed ZIP from the module library, which the Toolchain panel above can acquire and import. Until an LSPosed ZIP is in the library that step fails with “no LSPosed is available in the module library”. The Magisk install step still needs the daemon's configured external installer; the next step (complete_magisk_setup) installs the acquired APK and completes Magisk's Additional Setup, rebooting the AVD. After the LSPosed install the profile reboots (which activates the module) and installs the manager APK; activation is only proven by the profile's verify step that follows."
          onGoToOverview={onGoToOverview}
        />
      )}
    </div>
  );
}
