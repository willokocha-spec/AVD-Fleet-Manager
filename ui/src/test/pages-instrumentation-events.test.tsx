import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import { ToastProvider } from "@/lib/components";
import type { DeploymentRun, EventType, FleetEvent } from "@/lib/types";

import { server } from "@/mocks/server";
import { DeploymentsTab } from "@/pages/_components/instrumentation/DeploymentsTab";

const API = "/api/v1";

/**
 * The Deployments tab imports `useEventStream` from the app provider. This test
 * swaps that module for a controllable hook so an `instrumentation.run` event can
 * be delivered without opening a socket (tests never open one).
 */
const eventState = vi.hoisted(() => ({ lastEvent: null as FleetEvent | null }));

vi.mock("@/app/EventStream", () => ({
  useEventStream: () => ({
    status: "open",
    events: eventState.lastEvent ? [eventState.lastEvent] : [],
    lastEvent: eventState.lastEvent,
    clearEvents: () => undefined,
  }),
}));

function json(data: unknown, status = 200): Response {
  return HttpResponse.json(data as Record<string, unknown>, { status });
}

function instrumentationEvent(
  type: EventType,
  payload: Record<string, unknown>,
  id = "evt-1",
): FleetEvent {
  return {
    id,
    type,
    ts: "2026-11-27T20:00:06Z",
    payload,
    source: "instrumentation",
    seq: 1,
  };
}

function runningRun(): DeploymentRun {
  return {
    id: "irun_live",
    profile_id: "prof_builtin_lsposed",
    label: "Nightly run",
    state: "running",
    targets: ["avd-pixel7-01"],
    results: [],
    created_at: "2026-11-27T20:00:00Z",
    started_at: "2026-11-27T20:00:01Z",
    finished_at: null,
    error: "",
  };
}

function completedRun(): DeploymentRun {
  return {
    ...runningRun(),
    state: "succeeded",
    finished_at: "2026-11-27T20:00:05Z",
    results: [
      {
        instance: "avd-pixel7-01",
        ok: true,
        skipped: false,
        error: "",
        steps: [
          {
            step_id: "step-verify_root",
            action: "verify_root",
            ok: true,
            state: "active",
            message: "root available via magisk",
            data: {},
          },
        ],
      },
    ],
  };
}

function tree() {
  return (
    <ToastProvider>
      <DeploymentsTab selected={[]} onGoToOverview={() => undefined} />
    </ToastProvider>
  );
}

describe("Deployments live progress", () => {
  it("refreshes the visible run on a matching event without waiting for the poll", async () => {
    const user = userEvent.setup();
    eventState.lastEvent = null;
    let detailCalls = 0;
    server.use(
      http.get(`${API}/instrumentation/profiles`, () => json({ profiles: [] })),
      http.get(`${API}/instrumentation/deployments`, () => json({ runs: [runningRun()] })),
      http.get(`${API}/instrumentation/deployments/:run_id/logs`, () => json({ logs: [] })),
      http.get(`${API}/instrumentation/deployments/:run_id`, () => {
        detailCalls += 1;
        return json(detailCalls === 1 ? runningRun() : completedRun());
      }),
    );

    const view = render(tree());
    await user.click(await screen.findByRole("button", { name: /Open run Nightly run/ }));
    expect(await screen.findByText("Polling every 2 s")).toBeInTheDocument();
    expect(detailCalls).toBe(1);

    // An event for a different run must not trigger a refresh.
    eventState.lastEvent = instrumentationEvent(
      "instrumentation.run",
      { action: "started", run_id: "irun_other", targets: ["avd-other"] },
      "evt-other",
    );
    view.rerender(tree());
    await new Promise((resolve) => setTimeout(resolve, 200));
    expect(detailCalls).toBe(1);

    // A matching `finished` event refreshes immediately (well inside the 2 s poll).
    eventState.lastEvent = instrumentationEvent(
      "instrumentation.run",
      {
        action: "finished",
        run_id: "irun_live",
        targets: ["avd-pixel7-01"],
        state: "succeeded",
        failed: [],
      },
      "evt-live",
    );
    view.rerender(tree());
    await waitFor(() => expect(detailCalls).toBeGreaterThan(1), { timeout: 1_500 });
    expect(await screen.findByText("succeeded")).toBeInTheDocument();
  });
});
