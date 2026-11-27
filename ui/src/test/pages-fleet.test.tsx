import { HttpResponse, http } from "msw";
import { configure, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { server } from "@/mocks/server";

import { renderApp } from "./utils";

// The suite shares the workspace with other test/build processes; be generous
// with async UI waits and the per-test budget so a loaded machine cannot make a
// page-load race flaky.
configure({ asyncUtilTimeout: 10_000 });
vi.setConfig({ testTimeout: 120_000 });

/** Row containing the instance-name link, for scoped assertions. */
function fleetRow(name: string): HTMLElement {
  const link = screen.getByRole("link", { name });
  const row = link.closest("tr");
  if (!(row instanceof HTMLElement)) throw new Error(`No fleet row found for ${name}`);
  return row;
}

async function walkToFleetStep(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.click(await screen.findByRole("button", { name: /Android 16 .*Google APIs/i }));
  await user.click(screen.getByRole("button", { name: "Continue" }));
  await user.click(await screen.findByRole("button", { name: /Pixel 7 · Google APIs \(API 36\)/i }));
  await user.click(screen.getByRole("button", { name: "Continue" }));
}

describe("Fleet page", () => {
  it("lists every instance, filters by state and searches by name", async () => {
    const user = userEvent.setup();
    renderApp({ route: "/fleet" });

    expect(await screen.findByRole("heading", { level: 1, name: "Fleet" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "avd-pixel7-01" })).toBeInTheDocument();
    for (const name of [
      "avd-pixel7-02",
      "avd-pixel7-03",
      "avd-tablet-01",
      "avd-play-01",
    ]) {
      expect(screen.getByRole("link", { name })).toBeInTheDocument();
    }

    // Per-instance status pills, proxy badges, ADB serial and ports are rendered.
    const runningRow = fleetRow("avd-pixel7-01");
    expect(within(runningRow).getByText("Running")).toBeInTheDocument();
    expect(within(runningRow).getByText("EU egress 01")).toBeInTheDocument();
    expect(within(runningRow).getByText("emulator-5554")).toBeInTheDocument();
    expect(within(runningRow).getByText(/console \/ adb: 5554 \/ 5555/)).toBeInTheDocument();

    // Filter by state: only the error instance remains.
    await user.click(screen.getByRole("button", { name: /^Error/ }));
    expect(await screen.findByRole("link", { name: "avd-play-01" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "avd-pixel7-01" })).not.toBeInTheDocument();

    // Back to all, then search narrows the list.
    await user.click(screen.getByRole("button", { name: /^All/ }));
    await screen.findByRole("link", { name: "avd-pixel7-01" });
    const search = screen.getByRole("searchbox", { name: "Search instances" });
    await user.type(search, "tablet");
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "avd-pixel7-01" })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("link", { name: "avd-tablet-01" })).toBeInTheDocument();
    await user.clear(search);
    expect(await screen.findByRole("link", { name: "avd-pixel7-01" })).toBeInTheDocument();
  });

  it("stops multiple selected instances through the bulk action bar", async () => {
    const user = userEvent.setup();
    renderApp({ route: "/fleet" });

    await screen.findByRole("link", { name: "avd-pixel7-01" });
    await user.click(screen.getByRole("checkbox", { name: "Select avd-pixel7-01" }));
    await user.click(screen.getByRole("checkbox", { name: "Select avd-pixel7-02" }));

    const bar = screen.getByRole("region", { name: "Bulk actions" });
    expect(within(bar).getByText("2 instances selected")).toBeInTheDocument();
    await user.click(within(bar).getByRole("button", { name: "Stop" }));

    // The daemon mutation is reflected in the table after the silent refresh.
    expect(await screen.findByText(/Stopped 2 instances/)).toBeInTheDocument();
    await waitFor(() => {
      expect(within(fleetRow("avd-pixel7-01")).getByText("Stopped")).toBeInTheDocument();
      expect(within(fleetRow("avd-pixel7-02")).getByText("Stopped")).toBeInTheDocument();
    });

    // Successful bulk operations clear the selection and hide the action bar.
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: "Bulk actions" })).not.toBeInTheDocument(),
    );
  });

  it("confirms before deleting selected instances", async () => {
    const user = userEvent.setup();
    renderApp({ route: "/fleet" });

    await screen.findByRole("link", { name: "avd-tablet-01" });
    await user.click(screen.getByRole("checkbox", { name: "Select avd-tablet-01" }));
    const bar = screen.getByRole("region", { name: "Bulk actions" });
    await user.click(within(bar).getByRole("button", { name: "Delete" }));

    const dialog = await screen.findByRole("dialog", { name: /Delete avd-tablet-01\?/i });
    await user.click(within(dialog).getByRole("button", { name: "Delete instance" }));

    expect(await screen.findByText(/Deleted 1 instance/)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "avd-tablet-01" })).not.toBeInTheDocument(),
    );
  });
});

describe("Instance detail page", () => {
  it("renders the live screen, runtime facts and resets the instance to OOBE", async () => {
    const user = userEvent.setup();
    renderApp({ route: "/fleet/avd-pixel7-01" });

    expect(
      await screen.findByRole("heading", { level: 1, name: "avd-pixel7-01" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Overview" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Snapshots" })).toBeInTheDocument();

    // GET /instances/avd-pixel7-01/screenshot is rendered into the live preview.
    expect(await screen.findByAltText("Live screen of avd-pixel7-01")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pause live screen" })).toBeInTheDocument();
    expect(screen.getAllByText("emulator-5554").length).toBeGreaterThan(0);

    // Reset flow: choose the wipe strategy and confirm.
    await user.click(screen.getByRole("button", { name: "Reset to OOBE" }));
    const dialog = await screen.findByRole("dialog", {
      name: /Reset avd-pixel7-01 to OOBE/i,
    });
    await user.click(within(dialog).getByRole("radio", { name: /Wipe data/i }));
    await user.click(within(dialog).getByRole("button", { name: "Reset instance" }));

    // The toast reports the strategy; the dialog closes and the instance reboots.
    expect(await screen.findByText(/strategy: wipe/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await screen.findByText("Screen starting")).toBeInTheDocument();
  });

  it("shows a clear not-found state for an unknown instance", async () => {
    renderApp({ route: "/fleet/does-not-exist" });

    expect(
      await screen.findByRole("heading", { level: 1, name: "does-not-exist" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("Instance not found")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to fleet" })).toBeInTheDocument();
  });
});

describe("Deploy wizard", () => {
  it("deploys a batch from an OOBE-verified image through the full wizard", async () => {
    const user = userEvent.setup();
    renderApp({ route: "/deploy" });

    expect(await screen.findByRole("heading", { level: 1, name: "Deploy AVDs" })).toBeInTheDocument();

    // Step 1: only installed images are offered; API 36 is OOBE verified.
    const imageOption = await screen.findByRole("button", {
      name: /Android 16 .*Google APIs/i,
    });
    expect(within(imageOption).getByText("OOBE verified")).toBeInTheDocument();
    await user.click(imageOption);
    await user.click(screen.getByRole("button", { name: "Continue" }));

    // Step 2: pick the template that uses this image.
    await user.click(
      await screen.findByRole("button", { name: /Pixel 7 · Google APIs \(API 36\)/i }),
    );
    await user.click(screen.getByRole("button", { name: "Continue" }));

    // Step 3: two instances with the default "avd" prefix.
    const count = await screen.findByRole("spinbutton", { name: /Instance count/i });
    await user.clear(count);
    await user.type(count, "2");
    expect(await screen.findByText("avd-01")).toBeInTheDocument();
    expect(screen.getByText("avd-02")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Continue" }));

    // Step 4: review, then POST /instances/bulk.
    expect(
      await screen.findByRole("heading", { level: 2, name: "Review the deployment" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Deploy 2 instances/i }));

    expect(
      await screen.findByRole("heading", { level: 2, name: "Deployed 2 instances" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "avd-01" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "avd-02" })).toBeInTheDocument();
  });

  it("blocks the review step when the daemon reports no remaining capacity", async () => {
    server.use(
      http.get("/api/v1/capacity", () =>
        HttpResponse.json({
          max_instances: 5,
          deployed: 5,
          running: 2,
          ram_available_mb: 2048,
          ram_required_mb: 8192,
          can_start_more: 0,
          warnings: ["The host is already at its instance limit."],
        }),
      ),
    );

    const user = userEvent.setup();
    renderApp({ route: "/deploy" });
    await screen.findByRole("heading", { level: 1, name: "Deploy AVDs" });
    await walkToFleetStep(user);

    expect(await screen.findByText(/daemon limit is 5/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByText("Cannot continue yet")).toBeInTheDocument();
    expect(screen.getAllByText(/daemon limit is 5/i).length).toBeGreaterThan(1);
    expect(
      screen.queryByRole("heading", { level: 2, name: "Review the deployment" }),
    ).not.toBeInTheDocument();
  });
});
