import { act, configure, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { FleetEvent } from "@/lib/types";

import { server } from "../mocks/server";
import { renderApp } from "./utils";

// The full suite runs several page suites in parallel; full-shell renders plus
// MSW round-trips can exceed the 1 s Testing Library default under load.
configure({ asyncUtilTimeout: 10_000 });

/**
 * Controllable live-event provider.
 *
 * `EventStreamProvider` intentionally never opens sockets under Vitest, so the
 * Runs tests replace the module with an in-memory store they can push
 * `script.run.output` envelopes into. The production provider is exercised by
 * `ws.test.ts` and the app shell tests.
 */
const live = vi.hoisted(() => {
  interface Snapshot {
    status: string;
    events: FleetEvent[];
    lastEvent: FleetEvent | null;
  }
  let snapshot: Snapshot = { status: "open", events: [], lastEvent: null };
  const listeners = new Set<() => void>();
  return {
    get: (): Snapshot => snapshot,
    push(event: FleetEvent): void {
      snapshot = {
        ...snapshot,
        events: [...snapshot.events, event].slice(-200),
        lastEvent: event,
      };
      for (const listener of listeners) listener();
    },
    reset(): void {
      snapshot = { status: "open", events: [], lastEvent: null };
    },
    subscribe(listener: () => void): () => void {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
});

vi.mock("@/app/EventStream", async () => {
  const React = await import("react");
  type Child = import("react").ReactNode;

  function useEventStream() {
    const snapshot = React.useSyncExternalStore(live.subscribe, live.get, live.get);
    return {
      status: snapshot.status,
      events: snapshot.events,
      lastEvent: snapshot.lastEvent,
      clearEvents: () => undefined,
    };
  }

  function EventStreamProvider({ children }: { children?: Child }) {
    return React.createElement(React.Fragment, null, children);
  }

  return { EventStreamProvider, useEventStream };
});

function outputEvent(
  seq: number,
  runId: string,
  instance: string,
  stream: "stdout" | "stderr",
  line: string,
): FleetEvent {
  return {
    id: `evt-live-${seq}`,
    type: "script.run.output",
    ts: new Date().toISOString(),
    payload: { run_id: runId, instance, stream, line },
  };
}

afterEach(() => {
  live.reset();
});

describe("Scripts page", () => {
  it("creates a script in the library and starts a fan-out run", async () => {
    const user = userEvent.setup({ delay: null });
    renderApp({ route: "/scripts" });

    expect(await screen.findByRole("heading", { level: 1, name: "Scripts" })).toBeInTheDocument();
    // Fixture built-ins are rendered and marked read-only.
    expect(await screen.findByRole("heading", { name: "Boot readiness check" })).toBeInTheDocument();
    expect(screen.getAllByText("Built-in").length).toBeGreaterThan(0);

    await user.click(screen.getByRole("button", { name: "New script" }));
    const editor = await screen.findByRole("dialog");
    await user.type(
      within(editor).getByPlaceholderText("e.g. Check Play Store version"),
      "Inventory ping",
    );
    await user.type(within(editor).getByLabelText("Content"), "pm list packages | wc -l");
    await user.selectOptions(within(editor).getByLabelText("Type"), "adb_shell");
    await user.click(within(editor).getByRole("button", { name: "Create script" }));

    // The mock daemon persists the definition and the library reloads it.
    expect(await screen.findByText("Script created")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Inventory ping" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Run Inventory ping" }));
    const runDialog = await screen.findByRole("dialog");
    // Only the two running fixture instances are offered as targets.
    expect(
      await within(runDialog).findByText("2 of 2 running instances selected"),
    ).toBeInTheDocument();
    await user.click(within(runDialog).getByRole("button", { name: "Start run" }));

    // Starting navigates to /runs?run=<id>, where the detail view takes over.
    expect(await screen.findByRole("heading", { level: 1, name: "Runs" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Inventory ping" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Cancel run" })).toBeInTheDocument();
  }, 120_000);
});

describe("Runs page", () => {
  it("appends live script.run.output lines, pauses/clears, and cancels the run", async () => {
    const user = userEvent.setup({ delay: null });
    renderApp({ route: "/runs?run=run-0003" });

    expect(await screen.findByRole("heading", { level: 1, name: "Runs" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Boot readiness check" })).toBeInTheDocument();
    expect(await screen.findByText("Live")).toBeInTheDocument();

    const log = await screen.findByRole("log", { name: /run run-0003 output/i });

    // stdout/stderr envelopes are filtered by run id and rendered per stream.
    // Push one envelope per act so the provider's `lastEvent` semantics match
    // the real socket (one React update per WebSocket message).
    act(() => {
      live.push(outputEvent(1, "run-0003", "avd-pixel7-03", "stdout", "package manager ready"));
    });
    await waitFor(() => {
      expect(log.textContent).toContain("package manager ready");
      expect(
        Array.from(log.querySelectorAll(".ds-logs__line--stdout")).some((line) =>
          line.textContent?.includes("package manager ready"),
        ),
      ).toBe(true);
    });

    act(() => {
      live.push(outputEvent(2, "run-0003", "avd-pixel7-03", "stderr", "error: activity not found"));
    });
    await waitFor(() => {
      expect(log.textContent).toContain("error: activity not found");
      expect(
        Array.from(log.querySelectorAll(".ds-logs__line--stderr")).some((line) =>
          line.textContent?.includes("error: activity not found"),
        ),
      ).toBe(true);
    });

    // Another run's output must not leak into this view.
    act(() => {
      live.push(outputEvent(3, "run-0099", "avd-play-01", "stdout", "unrelated output"));
    });
    await waitFor(() => {
      expect(log.textContent).not.toContain("unrelated output");
    });

    await user.click(screen.getByRole("button", { name: "Pause scroll" }));
    expect(screen.getByRole("button", { name: "Resume scroll" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Resume scroll" }));

    await user.click(screen.getByRole("button", { name: "Clear run output" }));
    expect(
      screen.getByRole("log", { name: /run run-0003 output/i }).textContent,
    ).toContain("Output cleared");

    // New lines still arrive after a clear.
    act(() => {
      live.push(outputEvent(4, "run-0003", "avd-pixel7-01", "stdout", "late line"));
    });
    await waitFor(() => {
      expect(
        screen.getByRole("log", { name: /run run-0003 output/i }).textContent,
      ).toContain("late line");
    });

    // Cancel requires confirmation; the mock daemon returns the cancelled run.
    await user.click(screen.getByRole("button", { name: "Cancel run" }));
    const confirmation = await screen.findByRole("dialog");
    expect(
      within(confirmation).getByText(/stop scheduling instances/i),
    ).toBeInTheDocument();
    await user.click(within(confirmation).getByRole("button", { name: "Cancel the run" }));

    expect(await screen.findByText("Run cancelled")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getAllByText("Cancelled").length).toBeGreaterThan(0);
      expect(screen.queryByRole("button", { name: "Cancel run" })).not.toBeInTheDocument();
    });
  }, 60_000);

  it("renders per-instance results and exports from a terminal run", async () => {
    renderApp({ route: "/runs?run=run-0001" });

    expect(await screen.findByRole("heading", { name: "Open Settings app" })).toBeInTheDocument();
    expect((await screen.findAllByText("avd-play-01")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("1 ok").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Copy JSON" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download text" })).toBeInTheDocument();
    // Terminal runs do not offer cancellation.
    expect(screen.queryByRole("button", { name: "Cancel run" })).not.toBeInTheDocument();
  }, 60_000);
});

describe("Portable page", () => {
  it("renders portable status, preflight checks and leftovers", async () => {
    renderApp({ route: "/portable" });

    expect(await screen.findByRole("heading", { level: 1, name: "Portable" })).toBeInTheDocument();
    expect(await screen.findByText("Filesystem type")).toBeInTheDocument();
    expect(screen.getByText("Hypervisor availability")).toBeInTheDocument();
    expect(screen.getByText("Mark-of-the-Web")).toBeInTheDocument();

    // Status labels map ok/warn/fail/skip to pass/warn/fail/skip.
    expect(screen.getAllByText("Pass").length).toBeGreaterThanOrEqual(3);
    expect(screen.getAllByText("Fail").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Warn").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Skip")).toBeInTheDocument();

    // Edition comparison + the installed root from /portable/status.
    expect(screen.getByRole("heading", { name: "Installed edition" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Portable edition" })).toBeInTheDocument();
    expect(screen.getByText(/AppData\\Local\\avdfleet/)).toBeInTheDocument();

    // Documented-leftovers empty state and CLI build/verify instructions.
    expect(screen.getByText("No leftovers reported")).toBeInTheDocument();
    expect(screen.getByText(/avdfleet bundle --offline/)).toBeInTheDocument();
    expect(screen.getAllByText(/avdfleet portable verify/).length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: /Microsoft filesystem comparison/ })).toBeInTheDocument();
  }, 60_000);

  it("degrades gracefully when the portable endpoints are unavailable (503)", async () => {
    server.use(
      http.get("*/api/v1/portable/status", () =>
        HttpResponse.json(
          { error: { code: "service_unavailable", message: "portable services are not available" } },
          { status: 503 },
        ),
      ),
      http.get("*/api/v1/portable/checks", () =>
        HttpResponse.json(
          { error: { code: "service_unavailable", message: "portable services are not available" } },
          { status: 503 },
        ),
      ),
    );

    renderApp({ route: "/portable" });

    expect(await screen.findByText(/Portable API unavailable on this daemon/)).toBeInTheDocument();
    expect(screen.getByText(/HTTP 503/)).toBeInTheDocument();

    // Static check guides remain visible with an explicit "not checked" state.
    expect(await screen.findByText("Filesystem type")).toBeInTheDocument();
    expect(screen.getAllByText("Not checked").length).toBeGreaterThanOrEqual(6);
    expect(screen.getByText(/noexec mount/i)).toBeInTheDocument();

    // The edition still falls back to /system so the page stays informative.
    expect(await screen.findByText("Installed")).toBeInTheDocument();

    // CLI workflow is still offered.
    expect(screen.getByText(/avdfleet bundle --offline/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Re-run checks" }),
    ).toBeInTheDocument();
  }, 60_000);
});
