/**
 * Overview tab: target AVD selection + capability scan.
 *
 * Deliberately keeps the only AVD picker on the page: the selection drives every
 * deploy tab too. No instrumentation endpoint is called until the operator runs
 * a scan, so the page mounts without touching the instrumentation API.
 */
import { useMemo } from "react";

import {
  Badge,
  Button,
  ButtonLink,
  Card,
  Checkbox,
  EmptyState,
  InlineNotice,
  Skeleton,
} from "@/lib/components";
import type { AvdInfo, DeviceInstrumentation } from "@/lib/types";
import { humanize } from "@/lib/utils";

import {
  DeviceInstrumentationCard,
  apiErrorText,
  styles,
} from "@/pages/_components/instrumentation/shared";

export interface OverviewTabProps {
  instances: AvdInfo[];
  instancesLoading: boolean;
  instancesError: unknown;
  onReloadInstances: () => void;
  selected: string[];
  onSelectionChange: (next: string[]) => void;
  devices: DeviceInstrumentation[];
  scanning: boolean;
  scanError: string | null;
  onScan: () => void;
  onRefreshDevice: (instance: string) => void;
  refreshingInstance: string | null;
}

export function OverviewTab({
  instances,
  instancesLoading,
  instancesError,
  onReloadInstances,
  selected,
  onSelectionChange,
  devices,
  scanning,
  scanError,
  onScan,
  onRefreshDevice,
  refreshingInstance,
}: OverviewTabProps) {
  const runningNames = useMemo(
    () => instances.filter((instance) => instance.state === "running").map((instance) => instance.name),
    [instances],
  );

  const compatible = devices.filter((device) => device.compatible).length;
  const rebootNeeded = devices.filter((device) => device.capabilities.reboot_required).length;
  const blocked = devices.length - compatible;

  const toggle = (name: string, checked: boolean): void => {
    onSelectionChange(
      checked
        ? Array.from(new Set([...selected, name]))
        : selected.filter((candidate) => candidate !== name),
    );
  };

  const selectAll = (): void => {
    onSelectionChange(instances.map((instance) => instance.name));
  };

  return (
    <div style={styles.stack}>
      <Card
        title="Target AVDs"
        icon="fleet"
        description="The instance list comes from GET /instances. This one selection is reused by the Root & Magisk, LSPosed and Deployments tabs."
        actions={
          <div style={styles.rowTight}>
            <Button
              size="sm"
              variant="ghost"
              disabled={runningNames.length === 0}
              title={
                runningNames.length === 0
                  ? "No instance is reported as running"
                  : `Select the ${runningNames.length} running instance(s)`
              }
              onClick={() => onSelectionChange(runningNames)}
            >
              Select running
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={instances.length === 0 || selected.length === instances.length}
              title={
                instances.length === 0
                  ? "No AVDs to select"
                  : selected.length === instances.length
                    ? "Every AVD is already selected"
                    : "Select every AVD"
              }
              onClick={selectAll}
            >
              Select all
            </Button>
          </div>
        }
      >
        <div style={styles.stack}>
          {instancesError ? (
            <InlineNotice
              tone="danger"
              title="Could not load the AVD list"
              action={
                <Button size="sm" variant="secondary" icon="refresh" onClick={onReloadInstances}>
                  Retry
                </Button>
              }
            >
              <p>{apiErrorText(instancesError)}</p>
            </InlineNotice>
          ) : null}

          {instancesLoading && instances.length === 0 ? (
            <div style={styles.scrollBox}>
              <Skeleton lines={4} />
            </div>
          ) : instances.length === 0 ? (
            instancesError ? (
              <EmptyState
                compact
                icon="alert-circle"
                title="AVD list unavailable"
                description="The instance request failed; retry it above. No instance data is being shown."
              />
            ) : (
              <EmptyState
                compact
                icon="fleet"
                title="No AVDs are defined"
                description="Create an instance from the Fleet page before probing root or deploying modules."
                action={
                  <ButtonLink to="/fleet" size="sm" variant="secondary" icon="fleet">
                    Open Fleet
                  </ButtonLink>
                }
              />
            )
          ) : (
            <>
              <div style={styles.scrollBox} role="group" aria-label="AVD selection">
                {instances.map((instance) => {
                  const details = [
                    humanize(instance.state),
                    instance.image_name,
                    instance.ports?.adb ? `adb ${instance.ports.adb}` : null,
                    instance.boot_completed ? "boot completed" : null,
                  ]
                    .filter(Boolean)
                    .join(" · ");
                  return (
                    <Checkbox
                      key={instance.name}
                      label={instance.name}
                      hint={details}
                      checked={selected.includes(instance.name)}
                      onChange={(event) => toggle(instance.name, event.target.checked)}
                    />
                  );
                })}
              </div>
              <div style={styles.rowBetween}>
                <span className="muted small">
                  {selected.length} of {instances.length} selected
                  {instancesLoading ? " · refreshing…" : ""}
                </span>
                <div style={styles.rowTight}>
                  <Button
                    size="sm"
                    variant="ghost"
                    icon="close"
                    disabled={selected.length === 0}
                    title={selected.length === 0 ? "Nothing is selected" : "Clear the selection"}
                    onClick={() => onSelectionChange([])}
                  >
                    Clear
                  </Button>
                  <Button
                    variant="primary"
                    icon="shield"
                    loading={scanning}
                    disabled={selected.length === 0}
                    title={
                      selected.length === 0
                        ? "Select at least one AVD to scan"
                        : `Probe ${selected.length} AVD(s) with adb`
                    }
                    onClick={onScan}
                  >
                    Scan selected
                  </Button>
                </div>
              </div>
            </>
          )}

          {scanError ? (
            <InlineNotice
              tone="danger"
              title="Scan failed"
              action={
                <Button
                  size="sm"
                  variant="secondary"
                  icon="refresh"
                  disabled={selected.length === 0}
                  onClick={onScan}
                >
                  Retry
                </Button>
              }
            >
              <p>{scanError}</p>
            </InlineNotice>
          ) : null}
        </div>
      </Card>

      <InlineNotice tone="info" title="Installed ≠ enabled ≠ active">
        <p>
          <strong>Installed</strong> means the artifact is present on the device, <strong>enabled</strong> means
          the persisted Magisk switch is on, and <strong>active</strong> means the framework has actually applied
          it. Zygisk and LSPosed only become active after a reboot, so a completed install is not proof that the
          framework is running.
        </p>
        <p>
          <strong>ADB root</strong> is host-side root on a userdebug image: it gives the adb shell uid 0 but no
          application-facing <code>su</code>, Zygisk or LSPosed. It is reported separately from Magisk root.
        </p>
      </InlineNotice>

      <section style={styles.stack} aria-label="Scan results">
        <div style={styles.rowBetween}>
          <span style={styles.labels}>Scan results</span>
          {devices.length > 0 ? (
            <div style={styles.rowTight}>
              <Badge tone="success" icon="check-circle">
                {compatible} compatible
              </Badge>
              {blocked > 0 ? (
                <Badge tone="warning" icon="alert-triangle">
                  {blocked} with blockers
                </Badge>
              ) : null}
              {rebootNeeded > 0 ? (
                <Badge tone="warning" icon="restart">
                  {rebootNeeded} need a reboot
                </Badge>
              ) : null}
            </div>
          ) : null}
        </div>

        {devices.length === 0 ? (
          <EmptyState
            compact
            icon="search"
            title="No instrumentation results yet"
            description="Select one or more AVDs above and run Scan to probe root, Magisk, Zygisk and LSPosed state. The probe talks to each device over adb and can take a while."
          />
        ) : (
          <div style={styles.cardGrid}>
            {devices.map((device) => (
              <DeviceInstrumentationCard
                key={device.instance}
                device={device}
                refreshing={refreshingInstance === device.instance}
                onRefresh={() => onRefreshDevice(device.instance)}
              />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
