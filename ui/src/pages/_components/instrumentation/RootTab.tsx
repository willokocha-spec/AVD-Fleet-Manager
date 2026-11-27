/**
 * Root & Magisk tab.
 *
 * Shows the probed root/Magisk detail for the selected AVDs and deploys one of
 * the built-in root profiles. The external-installer requirement is explained
 * in place: the backend refuses `install_magisk` with a configuration error when
 * no installer is configured, and this page never pretends otherwise.
 */
import { useMemo } from "react";

import { instrumentationApi } from "@/lib/api";
import {
  Button,
  Card,
  EmptyState,
  InlineNotice,
  Skeleton,
} from "@/lib/components";
import { useAsync } from "@/lib/hooks";
import type { DeviceInstrumentation } from "@/lib/types";

import {
  BUILTIN_MAGISK_PROFILE,
  BUILTIN_ROOT_PROFILE,
  DeployProfileCard,
  DeviceInstrumentationCard,
  TargetSummary,
  apiErrorText,
  styles,
} from "@/pages/_components/instrumentation/shared";
import { ToolchainPanel } from "@/pages/_components/instrumentation/ToolchainPanel";

export interface RootTabProps {
  devices: DeviceInstrumentation[];
  selected: string[];
  onGoToOverview: () => void;
  onModuleImported?: (moduleId: string) => void;
}

export function RootTab({ devices, selected, onGoToOverview, onModuleImported }: RootTabProps) {
  const profiles = useAsync(() => instrumentationApi.listProfiles(), []);
  const allProfiles = useMemo(() => profiles.data?.profiles ?? [], [profiles.data]);

  const rootProfiles = useMemo(() => {
    const byId = allProfiles.filter(
      (profile) => profile.id === BUILTIN_ROOT_PROFILE || profile.id === BUILTIN_MAGISK_PROFILE,
    );
    const matches =
      byId.length > 0
        ? byId
        : // Fallback for a daemon whose built-in ids differ: match the shipped names.
          allProfiles.filter(
            (profile) =>
              profile.builtin && (profile.name === "Root" || profile.name === "Magisk + Zygisk"),
          );
    const order = (profileId: string): number => {
      if (profileId === BUILTIN_ROOT_PROFILE) return 0;
      if (profileId === BUILTIN_MAGISK_PROFILE) return 1;
      return profileId === "Root" ? 0 : profileId === "Magisk + Zygisk" ? 1 : 2;
    };
    return [...matches].sort(
      (left, right) =>
        order(left.id) - order(right.id) || left.name.localeCompare(right.name),
    );
  }, [allProfiles]);

  return (
    <div style={styles.stack}>
      <InlineNotice tone="warning" title="The install_magisk step needs a configured external installer">
        <p>
          Magisk is GPL-licensed and is <strong>not bundled</strong> with AVD Fleet. The Toolchain panel below
          can fetch the pinned Magisk APK from the official release and verify its SHA-256, but the APK is only
          the payload for the patcher: the backend still delegates the install to the installer configured on the
          daemon (for example rootAVD with a user-supplied Magisk APK). When none is configured the step fails
          with the configuration error
          <em> “no Magisk installer is configured”</em> and the run records that failure — the UI will never
          report a successful Magisk install that did not happen.
        </p>
        <p>
          The shipped profiles then run <code>complete_magisk_setup</code>, which installs the acquired APK and
          completes the Magisk app&apos;s “Additional Setup” (rebooting the AVD). Until that finishes, magiskd has
          no <code>/data/adb</code> and no module script runs.
        </p>
        <p>
          Zygisk can only be enabled once Magisk root exists, and it only becomes <strong>active</strong> after a
          reboot. The shipped “Magisk + Zygisk” profile now runs <code>install_magisk</code> →{" "}
          <code>complete_magisk_setup</code> → <code>enable_zygisk</code> → <code>reboot</code> →{" "}
          <code>verify</code>, so the verify step re-probes the device after the reboot instead of trusting the
          file transfer. The built-in Root profile is <code>enable_adb_root</code> → <code>verify_root</code>{" "}
          (host-side root only; production images reject it).
        </p>
      </InlineNotice>

      <ToolchainPanel onModuleImported={onModuleImported} />

      <TargetSummary selected={selected} onGoToOverview={onGoToOverview} />

      {devices.length === 0 ? (
        <EmptyState
          compact
          icon="search"
          title="No capability results yet"
          description="Run Scan on the Overview tab to probe root and Magisk state for the selected AVDs."
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
        <Card title="Deploy a root profile" icon="deploy">
          <div className="skeleton-card">
            <Skeleton lines={4} />
          </div>
        </Card>
      ) : (
        <DeployProfileCard
          profiles={rootProfiles}
          targets={selected}
          title="Deploy a root profile"
          description="Runs the profile on every selected AVD through POST /instrumentation/deployments. Built-in profiles are read-only; duplicate one to bind modules."
          footerNote="Deployments run in the background on the daemon; follow the run in the Deployments tab for per-target step results and logs."
          onGoToOverview={onGoToOverview}
        />
      )}
    </div>
  );
}
