import { useEffect, useMemo, useState } from "react";

import { api, instrumentationApi } from "@/lib/api";
import {
  Badge,
  Button,
  PageHeader,
  StatusPill,
  Tabs,
  useToast,
  type TabItem,
} from "@/lib/components";
import { useAsync } from "@/lib/hooks";
import type { PageMeta } from "@/lib/page-meta";
import type { DeviceInstrumentation } from "@/lib/types";
import { pluralize } from "@/lib/utils";

import { DeploymentsTab } from "@/pages/_components/instrumentation/DeploymentsTab";
import { LsposedTab } from "@/pages/_components/instrumentation/LsposedTab";
import { ModulesTab } from "@/pages/_components/instrumentation/ModulesTab";
import { OverviewTab } from "@/pages/_components/instrumentation/OverviewTab";
import { ProfilesTab } from "@/pages/_components/instrumentation/ProfilesTab";
import { RootTab } from "@/pages/_components/instrumentation/RootTab";
import { apiErrorText } from "@/pages/_components/instrumentation/shared";

export const meta: PageMeta = {
  path: "/instrumentation",
  title: "Root & Instrumentation",
  icon: "zap",
  nav: 25,
  description: "Root, Magisk, Zygisk, LSPosed and module management",
};

/**
 * Root & Instrumentation.
 *
 * Page-level state is deliberately small: the AVD selection and the last scan
 * result are shared by every tab, so switching tabs never loses the probe. Only
 * `GET /instances` runs on mount — the instrumentation endpoints are called from
 * explicit actions, which keeps the page honest about what it has measured.
 */
export default function InstrumentationPage() {
  const toast = useToast();
  const [tab, setTab] = useState("overview");
  const [selected, setSelected] = useState<string[]>([]);
  const [devices, setDevices] = useState<DeviceInstrumentation[]>([]);
  const [scanning, setScanning] = useState(false);
  const [scanError, setScanError] = useState<string | null>(null);
  const [refreshingInstance, setRefreshingInstance] = useState<string | null>(null);
  // Bumped when an acquisition imports a module ZIP so the (possibly unmounted)
  // Module library tab reloads from the daemon on its next render.
  const [libraryRevision, setLibraryRevision] = useState(0);

  const instances = useAsync(() => api.listInstances(), []);
  const allInstances = useMemo(() => instances.data ?? [], [instances.data]);

  // Drop selections for instances that no longer exist (deleted elsewhere).
  useEffect(() => {
    if (!instances.data) return;
    const names = new Set(instances.data.map((instance) => instance.name));
    setSelected((current) => {
      const next = current.filter((name) => names.has(name));
      return next.length === current.length ? current : next;
    });
  }, [instances.data]);

  const rebootNeeded = devices.filter((device) => device.capabilities.reboot_required).length;

  const scan = async (): Promise<void> => {
    if (selected.length === 0) return;
    setScanning(true);
    setScanError(null);
    try {
      const result = await instrumentationApi.scan(selected);
      setDevices(result.devices);
      const compatible = result.devices.filter((device) => device.compatible).length;
      toast.success(
        `Scanned ${pluralize(result.devices.length, "AVD")}`,
        `${compatible} compatible · ${result.devices.length - compatible} with blockers`,
      );
    } catch (caught) {
      const message = apiErrorText(caught);
      setScanError(message);
      toast.error("Scan failed", message);
    } finally {
      setScanning(false);
    }
  };

  const refreshDevice = async (instance: string): Promise<void> => {
    setRefreshingInstance(instance);
    try {
      const result = await instrumentationApi.getCapabilities([instance]);
      const fresh = result.devices[0];
      if (fresh) {
        setDevices((current) =>
          current.map((device) => (device.instance === instance ? fresh : device)),
        );
      }
    } catch (caught) {
      toast.error(`Could not re-probe ${instance}`, apiErrorText(caught));
    } finally {
      setRefreshingInstance(null);
    }
  };

  const items: TabItem[] = [
    {
      id: "overview",
      label: "Overview",
      icon: "dashboard",
      content: (
        <OverviewTab
          instances={allInstances}
          instancesLoading={instances.loading}
          instancesError={instances.error}
          onReloadInstances={instances.reload}
          selected={selected}
          onSelectionChange={setSelected}
          devices={devices}
          scanning={scanning}
          scanError={scanError}
          onScan={() => void scan()}
          onRefreshDevice={(instance) => void refreshDevice(instance)}
          refreshingInstance={refreshingInstance}
        />
      ),
    },
    {
      id: "root",
      label: "Root & Magisk",
      icon: "zap",
      content: (
        <RootTab
          devices={devices}
          selected={selected}
          onGoToOverview={() => setTab("overview")}
          onModuleImported={() => setLibraryRevision((value) => value + 1)}
        />
      ),
    },
    {
      id: "lsposed",
      label: "LSPosed & Zygisk",
      icon: "shield",
      content: (
        <LsposedTab
          devices={devices}
          selected={selected}
          onGoToOverview={() => setTab("overview")}
          onModuleImported={() => setLibraryRevision((value) => value + 1)}
        />
      ),
    },
    {
      id: "modules",
      label: "Module library",
      icon: "folder",
      content: <ModulesTab revision={libraryRevision} />,
    },
    {
      id: "profiles",
      label: "Profiles",
      icon: "templates",
      content: <ProfilesTab />,
    },
    {
      id: "deployments",
      label: "Deployments",
      icon: "runs",
      content: (
        <DeploymentsTab selected={selected} onGoToOverview={() => setTab("overview")} />
      ),
    },
  ];

  return (
    <div className="page">
      <PageHeader
        title="Root & Instrumentation"
        eyebrow="Device preparation"
        description="Probe and configure root, Magisk, Zygisk, LSPosed and Xposed modules across the fleet. Every state shown here is measured on the device — installed, enabled and active are reported separately."
        meta={
          <>
            <Badge tone="info" icon="fleet">
              {pluralize(allInstances.length, "AVD")}
            </Badge>
            <Badge
              tone={selected.length > 0 ? "accent" : "neutral"}
              icon={selected.length > 0 ? "check-circle" : "info"}
            >
              {selected.length} selected
            </Badge>
            {devices.length > 0 ? (
              <Badge tone="neutral" icon="shield">
                {devices.length} probed
              </Badge>
            ) : null}
            {rebootNeeded > 0 ? (
              <StatusPill
                status="reboot_required"
                label={`${rebootNeeded} need${rebootNeeded === 1 ? "s" : ""} a reboot`}
              />
            ) : null}
          </>
        }
        actions={
          <Button
            variant="secondary"
            icon="refresh"
            loading={scanning}
            disabled={selected.length === 0}
            title={
              selected.length === 0
                ? "Select at least one AVD on the Overview tab to scan"
                : `Probe ${pluralize(selected.length, "AVD")} with adb`
            }
            onClick={() => void scan()}
          >
            Scan AVDs
          </Button>
        }
      />

      <Tabs
        items={items}
        value={tab}
        onChange={setTab}
        ariaLabel="Root and instrumentation sections"
      />
    </div>
  );
}
