import { useEffect, useMemo, useState } from "react";

import {
  Button,
  ButtonLink,
  ConfirmDialog,
  EmptyState,
  IconButton,
  InlineNotice,
  Modal,
  PageHeader,
  SearchBox,
  useToast,
} from "@/lib/components";
import { useAsync, useDebouncedValue } from "@/lib/hooks";
import { api } from "@/lib/api";
import type { PageMeta } from "@/lib/page-meta";
import type { AvdInfo, InstanceState } from "@/lib/types";
import { pluralize } from "@/lib/utils";

import { BulkActionBar, type BulkActionId } from "./_components/fleet/BulkActionBar";
import {
  FleetGrid,
  FleetTable,
  FleetTableSkeleton,
} from "./_components/fleet/FleetTable";
import { ResetOobeDialog, type ResetOobeOptions } from "./_components/fleet/ResetOobeDialog";
import { ScreenPreview } from "./_components/fleet/ScreenPreview";
import {
  FLEET_FILTERS,
  countInstances,
  describeError,
  matchesFilter,
  runBulk,
  searchInstances,
  useFleetInstances,
  useOptionalEventStream,
  type FleetStateFilter,
} from "./_components/fleet/fleet-utils";

export const meta: PageMeta = {
  path: "/fleet",
  title: "Fleet",
  icon: "fleet",
  nav: 20,
  description: "Deploy, start, stop and inspect every AVD",
};

type SelectionAction = "start" | "stop" | "restart";

const SELECTION_ACTIONS: Record<
  SelectionAction,
  {
    eligible: (state: InstanceState) => boolean;
    past: string;
    run: (name: string) => Promise<unknown>;
  }
> = {
  start: {
    eligible: (state) => state === "defined" || state === "stopped" || state === "error" || state === "unknown",
    past: "Started",
    run: (name) => api.startInstance(name),
  },
  stop: {
    eligible: (state) => state === "running" || state === "booting" || state === "starting",
    past: "Stopped",
    run: (name) => api.stopInstance(name),
  },
  restart: {
    eligible: (state) => state === "running" || state === "booting" || state === "starting",
    past: "Restarted",
    run: (name) => api.restartInstance(name),
  },
};

/**
 * Fleet overview: live instance list with state filters, search, multi-select
 * bulk lifecycle actions, per-row live screenshot and reset-to-OOBE.
 *
 * Data refreshes every 5 s and is overlaid with `instance.state` events from the
 * event WebSocket, so state changes appear without waiting for the next poll.
 */
export default function FleetPage() {
  const toast = useToast();
  const fleet = useFleetInstances(5_000);
  const { status: eventStatus } = useOptionalEventStream();
  const proxies = useAsync(() => api.listProxies(true), []);

  const [filter, setFilter] = useState<FleetStateFilter>("all");
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebouncedValue(search, 200);
  const [selected, setSelected] = useState<ReadonlySet<string>>(() => new Set<string>());
  const [dense, setDense] = useState(false);
  const [view, setView] = useState<"list" | "grid">("list");
  const [busy, setBusy] = useState<BulkActionId | null>(null);
  const [screenshotName, setScreenshotName] = useState<string | null>(null);
  const [resetOpen, setResetOpen] = useState(false);
  const [resetTargets, setResetTargets] = useState<string[]>([]);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteTargets, setDeleteTargets] = useState<string[]>([]);

  const instances = useMemo(() => fleet.data ?? [], [fleet.data]);
  const visible = useMemo(() => {
    const filtered = instances.filter((instance) => matchesFilter(instance.state, filter));
    return searchInstances(filtered, debouncedSearch);
  }, [instances, filter, debouncedSearch]);
  const selectedInstances = useMemo(
    () => instances.filter((instance) => selected.has(instance.name)),
    [instances, selected],
  );
  const selectedNames = useMemo(
    () => selectedInstances.map((instance) => instance.name),
    [selectedInstances],
  );
  const screenshotInstance = screenshotName
    ? (instances.find((instance) => instance.name === screenshotName) ?? null)
    : null;

  // Drop selected names that no longer exist (e.g. deleted elsewhere).
  useEffect(() => {
    setSelected((current) => {
      if (current.size === 0) return current;
      const names = new Set(instances.map((instance) => instance.name));
      let changed = false;
      const next = new Set<string>();
      for (const name of current) {
        if (names.has(name)) next.add(name);
        else changed = true;
      }
      return changed ? next : current;
    });
  }, [instances]);

  const toggleSelection = (name: string): void => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const toggleAllVisible = (): void => {
    setSelected((current) => {
      const allSelected = visible.length > 0 && visible.every((instance) => current.has(instance.name));
      const next = new Set(current);
      for (const instance of visible) {
        if (allSelected) next.delete(instance.name);
        else next.add(instance.name);
      }
      return next;
    });
  };

  const clearFilters = (): void => {
    setFilter("all");
    setSearch("");
  };

  const runSelectionAction = async (action: SelectionAction): Promise<void> => {
    if (busy) return;
    const definition = SELECTION_ACTIONS[action];
    const eligible = selectedInstances
      .filter((instance) => definition.eligible(instance.state))
      .map((instance) => instance.name);
    if (eligible.length === 0) {
      toast.info(`Nothing to ${action}`, "None of the selected instances is eligible in its current state.");
      return;
    }
    const skipped = selectedNames.length - eligible.length;
    setBusy(action);
    try {
      const result = await runBulk(eligible, definition.run);
      await fleet.refresh();
      setSelected(new Set(result.failed.map((failure) => failure.name)));
      const failureDetail = result.failed
        .map((failure) => `${failure.name}: ${failure.message}`)
        .join(" · ");
      const skippedNote = skipped > 0 ? `${pluralize(skipped, "instance")} skipped (state).` : undefined;
      if (result.failed.length === 0) {
        toast.success(`${definition.past} ${pluralize(result.succeeded.length, "instance")}`, skippedNote);
      } else if (result.succeeded.length === 0) {
        toast.error(
          `Could not ${action} the selected instances`,
          failureDetail || skippedNote,
        );
      } else {
        toast.warning(
          `${definition.past} ${result.succeeded.length} of ${pluralize(eligible.length, "instance")}`,
          failureDetail,
        );
      }
    } catch (error) {
      toast.error(`Could not ${action} instances`, describeError(error));
    } finally {
      setBusy(null);
    }
  };

  const openReset = (): void => {
    setResetTargets(selectedNames);
    setResetOpen(true);
  };

  const confirmReset = async (options: ResetOobeOptions): Promise<void> => {
    setBusy("reset");
    try {
      const result = await runBulk(resetTargets, (name) => api.resetInstanceOobe(name, options));
      await fleet.refresh();
      setSelected(new Set(result.failed.map((failure) => failure.name)));
      if (result.succeeded.length > 0) {
        toast.success(
          `Reset ${pluralize(result.succeeded.length, "instance")} to OOBE`,
          `Strategy: ${options.strategy}${options.verify ? " · verification requested" : ""}`,
        );
      }
      if (result.failed.length > 0) {
        // Keep the dialog open showing only the instances that still need a reset.
        setResetTargets(result.failed.map((failure) => failure.name));
        throw new Error(
          result.failed.map((failure) => `${failure.name}: ${failure.message}`).join(" · "),
        );
      }
    } finally {
      setBusy(null);
    }
  };

  const openDelete = (): void => {
    setDeleteTargets(selectedNames);
    setDeleteOpen(true);
  };

  const confirmDelete = async (): Promise<void> => {
    if (busy) return;
    setBusy("delete");
    const names = deleteTargets;
    try {
      const result = await runBulk(names, (name) => api.deleteInstance(name, true));
      await fleet.refresh();
      setSelected(new Set(result.failed.map((failure) => failure.name)));
      if (result.failed.length === 0) {
        toast.success(`Deleted ${pluralize(result.succeeded.length, "instance")}`, names.join(", "));
        setDeleteOpen(false);
      } else if (result.succeeded.length === 0) {
        toast.error(
          "Could not delete the selected instances",
          result.failed.map((failure) => `${failure.name}: ${failure.message}`).join(" · "),
        );
      } else {
        toast.warning(
          `Deleted ${result.succeeded.length} of ${pluralize(names.length, "instance")}`,
          result.failed.map((failure) => `${failure.name}: ${failure.message}`).join(" · "),
        );
        setDeleteOpen(false);
      }
    } catch (error) {
      toast.error("Could not delete instances", describeError(error));
    } finally {
      setBusy(null);
    }
  };

  const lastSynced = fleet.lastUpdatedAt
    ? `Last synced ${new Date(fleet.lastUpdatedAt).toLocaleTimeString()} · polling every 5 s`
    : "Loading fleet…";
  const liveLabel =
    eventStatus === "open"
      ? "live events connected"
      : eventStatus === "connecting" || eventStatus === "reconnecting"
        ? "events connecting"
        : "events offline";

  return (
    <div className="page">
      <PageHeader
        title="Fleet"
        eyebrow="Instances"
        icon="fleet"
        description="Every Android Virtual Device on this host, its live state, proxy assignment and unique ports."
        meta={
          <span className="muted small">
            {lastSynced} · {liveLabel}
          </span>
        }
        actions={
          <>
            <ButtonLink to="/deploy" variant="primary" icon="deploy">
              Bulk deploy
            </ButtonLink>
            <ButtonLink to="/templates" variant="secondary" icon="templates">
              Templates
            </ButtonLink>
            <IconButton
              label="Refresh fleet now"
              icon="refresh"
              loading={fleet.refreshing}
              onClick={() => void fleet.refresh()}
            />
          </>
        }
      />

      {fleet.loading && !fleet.data ? <FleetTableSkeleton rows={5} /> : null}

      {fleet.error && !fleet.data ? (
        <InlineNotice
          tone="danger"
          title="Could not load the fleet"
          action={
            <Button size="sm" variant="secondary" icon="refresh" onClick={() => void fleet.refresh()}>
              Retry
            </Button>
          }
        >
          <p>{describeError(fleet.error)}</p>
        </InlineNotice>
      ) : null}

      {fleet.data ? (
        instances.length === 0 ? (
          <EmptyState
            icon="fleet"
            title="No instances are deployed"
            description="Deploy one or more AVDs from a template. Each instance gets unique console/adb ports and an optional isolated proxy."
            action={
              <ButtonLink to="/deploy" variant="primary" icon="deploy">
                Deploy your first fleet
              </ButtonLink>
            }
          />
        ) : (
          <>
            {fleet.error ? (
              <InlineNotice
                tone="warning"
                title="Live refresh failed"
                action={
                  <Button size="sm" variant="secondary" icon="refresh" onClick={() => void fleet.refresh()}>
                    Retry
                  </Button>
                }
              >
                <p>Showing the last fleet snapshot. {describeError(fleet.error)}</p>
              </InlineNotice>
            ) : null}

            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: "var(--space-3)",
                alignItems: "center",
                justifyContent: "space-between",
              }}
            >
              <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-2)", alignItems: "center" }}>
                <SearchBox
                  value={search}
                  onChange={setSearch}
                  label="Search instances"
                  placeholder="Search name, image, serial or proxy…"
                />
                <div role="group" aria-label="Filter by state" style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {FLEET_FILTERS.map((option) => {
                    const active = filter === option.id;
                    const count = countInstances(instances, option.id);
                    return (
                      <Button
                        key={option.id}
                        size="sm"
                        variant={active ? "secondary" : "ghost"}
                        aria-pressed={active}
                        title={option.description}
                        onClick={() => setFilter(option.id)}
                      >
                        {option.label}{" "}
                        <span className="muted" style={{ fontVariantNumeric: "tabular-nums" }}>
                          {count}
                        </span>
                      </Button>
                    );
                  })}
                </div>
              </div>
              <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center", flexWrap: "wrap" }}>
                <Button
                  size="sm"
                  variant={view === "list" ? "secondary" : "ghost"}
                  icon="menu"
                  aria-pressed={view === "list"}
                  onClick={() => setView("list")}
                >
                  List
                </Button>
                <Button
                  size="sm"
                  variant={view === "grid" ? "secondary" : "ghost"}
                  icon="dashboard"
                  aria-pressed={view === "grid"}
                  onClick={() => setView("grid")}
                >
                  Grid
                </Button>
                <Button
                  size="sm"
                  variant={dense ? "secondary" : "ghost"}
                  aria-pressed={dense}
                  title="Toggle compact row padding"
                  onClick={() => setDense((value) => !value)}
                >
                  Compact rows
                </Button>
              </div>
            </div>

            {selectedNames.length > 0 ? (
              <BulkActionBar
                names={selectedNames}
                busy={busy}
                onStart={() => void runSelectionAction("start")}
                onStop={() => void runSelectionAction("stop")}
                onRestart={() => void runSelectionAction("restart")}
                onReset={openReset}
                onDelete={openDelete}
                onClear={() => setSelected(new Set())}
              />
            ) : null}

            <p className="muted small" style={{ margin: 0 }} aria-live="polite">
              Showing {visible.length} of {pluralize(instances.length, "instance")} ·{" "}
              {pluralize(selectedNames.length, "instance")} selected
            </p>

            {view === "list" ? (
              <FleetTable
                instances={visible}
                proxies={proxies.data}
                selected={selected}
                dense={dense}
                onToggle={toggleSelection}
                onToggleAll={toggleAllVisible}
                onScreenshot={(instance: AvdInfo) => setScreenshotName(instance.name)}
                empty={
                  <EmptyState
                    compact
                    icon="search"
                    title="No instances match"
                    description="Adjust the state filter or search query to see more instances."
                    action={
                      <Button size="sm" variant="secondary" onClick={clearFilters}>
                        Clear filters
                      </Button>
                    }
                  />
                }
              />
            ) : (
              <FleetGrid
                instances={visible}
                proxies={proxies.data}
                selected={selected}
                dense={dense}
                onToggle={toggleSelection}
                onToggleAll={toggleAllVisible}
                onScreenshot={(instance: AvdInfo) => setScreenshotName(instance.name)}
                empty={
                  <EmptyState
                    compact
                    icon="search"
                    title="No instances match"
                    description="Adjust the state filter or search query to see more instances."
                    action={
                      <Button size="sm" variant="secondary" onClick={clearFilters}>
                        Clear filters
                      </Button>
                    }
                  />
                }
              />
            )}
          </>
        )
      ) : null}

      <Modal
        open={screenshotInstance !== null}
        onClose={() => setScreenshotName(null)}
        title={screenshotInstance ? `Screen · ${screenshotInstance.name}` : "Screen"}
        description="Live screenshot of the running instance. Click the image to send a tap."
        size="lg"
      >
        {screenshotInstance ? (
          <ScreenPreview
            name={screenshotInstance.name}
            state={screenshotInstance.state}
            screenAvailable={screenshotInstance.screen_available}
            autoRefreshMs={5_000}
          />
        ) : null}
      </Modal>

      <ResetOobeDialog
        open={resetOpen}
        instanceNames={resetTargets}
        onClose={() => setResetOpen(false)}
        onConfirm={confirmReset}
      />

      <ConfirmDialog
        open={deleteOpen}
        title={
          deleteTargets.length === 1 ? `Delete ${deleteTargets[0]}?` : `Delete ${deleteTargets.length} instances?`
        }
        description="The instances are stopped if needed. Their AVD files, snapshots and proxy assignments are removed permanently."
        confirmLabel={
          deleteTargets.length === 1 ? "Delete instance" : `Delete ${deleteTargets.length} instances`
        }
        onConfirm={confirmDelete}
        onCancel={() => setDeleteOpen(false)}
      >
        <p className="small" style={{ margin: 0, overflowWrap: "anywhere" }}>
          {deleteTargets.slice(0, 8).join(", ")}
          {deleteTargets.length > 8 ? ` +${deleteTargets.length - 8} more` : ""}
        </p>
      </ConfirmDialog>
    </div>
  );
}
