import { configure, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { AuthProvider } from "@/app/AuthProvider";
import { ToastProvider } from "@/lib/components";
import { TOKEN_STORAGE_KEY } from "@/lib/config";
import { ThemeProvider } from "@/lib/theme";
import { server } from "@/mocks/server";
import DashboardPage from "@/pages/Dashboard";
import DoctorPage from "@/pages/Doctor";
import SettingsPage from "@/pages/Settings";

// Pages talk to the real typed client and the MSW daemon; allow the pipeline to
// settle on a loaded machine without promoting flaky retries or fake timers.
configure({ asyncUtilTimeout: 5_000 });

/**
 * Render a page with the same providers the console gives it. The Shell adds
 * no page behaviour, so skipping it keeps these tests fast (the shell itself is
 * covered by app.test.tsx).
 */
function renderPage(page: ReactElement, options: { auth?: boolean } = {}): void {
  window.localStorage.setItem(TOKEN_STORAGE_KEY, "test-token");
  const content = options.auth ? <AuthProvider>{page}</AuthProvider> : page;
  render(
    <MemoryRouter>
      <ThemeProvider>
        <ToastProvider>{content}</ToastProvider>
      </ThemeProvider>
    </MemoryRouter>,
  );
}

/* -------------------------------------------------------------------------- */
/* Dashboard                                                                   */
/* -------------------------------------------------------------------------- */

describe("Dashboard page", () => {
  it("renders fleet metrics, capacity and recent activity from the daemon", async () => {
    renderPage(<DashboardPage />);

    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();

    // Fleet overview counts come from GET /instances.
    const runningCard = (await screen.findByText("Running AVDs")).closest(".ds-stat");
    expect(runningCard).not.toBeNull();
    await waitFor(() =>
      expect(within(runningCard as HTMLElement).getByText("2")).toBeInTheDocument(),
    );

    const errorCard = screen.getByText("Errors").closest(".ds-stat");
    expect(errorCard).not.toBeNull();
    await waitFor(() =>
      expect(within(errorCard as HTMLElement).getByText("1")).toBeInTheDocument(),
    );

    // Capacity rows and warnings come from GET /capacity.
    const startMoreRow = (await screen.findByText("Can start more")).closest(".kv-list__row");
    expect(startMoreRow).not.toBeNull();
    expect(within(startMoreRow as HTMLElement).getByText("2")).toBeInTheDocument();
    expect(await screen.findByText(/real emulator boots are unavailable/i)).toBeInTheDocument();

    // Recent script runs come from GET /runs.
    expect(await screen.findByText("Boot readiness check")).toBeInTheDocument();

    // Recent events come from GET /events and are summarised, not dumped raw.
    expect(await screen.findByText("avd-pixel7-03 → Booting (cold boot)")).toBeInTheDocument();

    // Image / OOBE readiness summaries and the auto-refresh affordance.
    expect(await screen.findByText(/of 3 images installed/)).toBeInTheDocument();
    expect(await screen.findByText(/of 3 records verified/)).toBeInTheDocument();
    expect(screen.getByText("Auto-refresh 10 s")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument();
  });

  it("reloads the fleet when the operator refreshes", async () => {
    const user = userEvent.setup();
    renderPage(<DashboardPage />);
    await screen.findByText("Boot readiness check");

    // The fleet has been emptied between refreshes.
    server.use(http.get("/api/v1/instances", () => HttpResponse.json([])));

    await user.click(screen.getByRole("button", { name: "Refresh" }));

    expect(await screen.findByText("No instances are deployed")).toBeInTheDocument();
    const totalCard = screen.getByText("Total AVDs").closest(".ds-stat");
    expect(totalCard).not.toBeNull();
    await waitFor(() =>
      expect(within(totalCard as HTMLElement).getByText("0")).toBeInTheDocument(),
    );
  });

  it("shows a retryable error when the dashboard cannot load", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/v1/instances", () =>
        HttpResponse.json(
          { error: { code: "boom", message: "Daemon exploded while listing instances" } },
          { status: 500 },
        ),
      ),
    );

    renderPage(<DashboardPage />);

    expect(await screen.findByText("Dashboard data could not be loaded")).toBeInTheDocument();
    expect(screen.getByText(/Daemon exploded while listing instances/)).toBeInTheDocument();

    // Recover: drop the override and retry.
    server.resetHandlers();
    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("Boot readiness check")).toBeInTheDocument();
    expect(screen.queryByText("Dashboard data could not be loaded")).not.toBeInTheDocument();
  });
});

/* -------------------------------------------------------------------------- */
/* Doctor                                                                      */
/* -------------------------------------------------------------------------- */

describe("Doctor page", () => {
  it("explains blocked acceleration and applies a fix for a fixable check", async () => {
    const user = userEvent.setup();
    renderPage(<DoctorPage />);

    expect(await screen.findByRole("heading", { level: 1, name: "Doctor" })).toBeInTheDocument();
    expect(
      await screen.findByText("Hardware acceleration is unavailable on this host"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Enable Intel VT-x / AMD-V (SVM) in the BIOS/UEFI firmware."),
    ).toBeInTheDocument();
    expect(screen.getByText("Action required")).toBeInTheDocument();

    // Slow checks are opt-in and re-run the report; the fast report is shown first.
    const slowToggle = screen.getByLabelText("Run slow checks");
    expect(slowToggle).not.toBeChecked();
    await user.click(slowToggle);
    expect(slowToggle).toBeChecked();

    const fixButton = await screen.findByRole("button", { name: "Fix platform-tools (adb)" });
    await user.click(fixButton);

    const dialog = await screen.findByRole("dialog", { name: /Apply fix: platform-tools/i });
    expect(within(dialog).getByText("avdfleet repair --only platform-tools")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Apply fix" }));

    expect(await screen.findByText("Applied fix for platform-tools (adb).")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    // The page re-runs /doctor after the fix and shows the updated finding.
    expect(await screen.findByText("Fixed by the mock doctor.")).toBeInTheDocument();
  });
});

/* -------------------------------------------------------------------------- */
/* Settings                                                                    */
/* -------------------------------------------------------------------------- */

async function openSettings(): Promise<void> {
  renderPage(<SettingsPage />, { auth: true });
  expect(await screen.findByRole("heading", { level: 1, name: "Settings" })).toBeInTheDocument();
  await screen.findByLabelText(/Max instances/);
}

describe("Settings page", () => {
  it("validates instance limits and RAM, then saves a whitelisted setting", async () => {
    const user = userEvent.setup();
    await openSettings();

    const maxInstances = screen.getByLabelText(/Max instances/) as HTMLInputElement;
    const ram = screen.getByLabelText(/Default RAM per instance/) as HTMLInputElement;
    const save = screen.getByRole("button", { name: "Save changes" });

    await user.clear(maxInstances);
    await user.type(maxInstances, "0");
    expect(await screen.findByText("Enter a whole number between 1 and 64.")).toBeInTheDocument();
    expect(save).toBeDisabled();

    await user.clear(maxInstances);
    await user.type(maxInstances, "65");
    expect(await screen.findByText("Enter a whole number between 1 and 64.")).toBeInTheDocument();

    await user.clear(maxInstances);
    await user.type(maxInstances, "64");
    await user.clear(ram);
    await user.type(ram, "100");
    expect(await screen.findByText("RAM must be at least 512 MB.")).toBeInTheDocument();
    expect(save).toBeDisabled();

    // Valid values clear the errors and re-enable saving.
    await user.clear(ram);
    await user.type(ram, "4096");
    expect(await screen.findByText("Unsaved changes")).toBeInTheDocument();
    await waitFor(() => expect(save).toBeEnabled());
    await user.click(save);

    expect(await screen.findByText("Configuration saved")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByLabelText(/Default RAM per instance/)).toHaveValue(4096),
    );
    await waitFor(() => expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument());
  });

  it("asks for confirmation before reducing the max instance limit", async () => {
    const user = userEvent.setup();
    await openSettings();

    const maxInstances = screen.getByLabelText(/Max instances/) as HTMLInputElement;
    await user.clear(maxInstances);
    await user.type(maxInstances, "16");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    const dialog = await screen.findByRole("dialog", { name: "Reduce max instances?" });
    expect(within(dialog).getByText("64")).toBeInTheDocument();
    expect(within(dialog).getByText("16")).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Reduce limit" }));
    expect(await screen.findByText("Configuration saved")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText(/Max instances/)).toHaveValue(16));
  });

  it("surfaces daemon validation errors without losing the draft", async () => {
    const user = userEvent.setup();
    server.use(
      http.patch("/api/v1/config", () =>
        HttpResponse.json(
          {
            error: {
              code: "validation_failed",
              message: "max_instances cannot be reduced below 8",
              hint: "Choose a value of 8 or more.",
            },
          },
          { status: 422 },
        ),
      ),
    );

    await openSettings();
    const maxInstances = screen.getByLabelText(/Max instances/) as HTMLInputElement;
    await user.clear(maxInstances);
    await user.type(maxInstances, "32");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await user.click(
      within(await screen.findByRole("dialog", { name: "Reduce max instances?" })).getByRole(
        "button",
        { name: "Reduce limit" },
      ),
    );

    expect(await screen.findByText("Configuration not saved")).toBeInTheDocument();
    expect(
      (await screen.findAllByText(/max_instances cannot be reduced below 8/)).length,
    ).toBeGreaterThan(0);
    // The draft is preserved and can be retried or discarded.
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
  });

  it("renders without the authenticated console (isolated page smoke)", async () => {
    // The registry smoke test mounts pages without AuthProvider; the page must
    // still render and hide its auth-only card instead of crashing.
    window.localStorage.setItem(TOKEN_STORAGE_KEY, "test-token");
    render(
      <MemoryRouter>
        <ThemeProvider>
          <ToastProvider>
            <SettingsPage />
          </ToastProvider>
        </ThemeProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { level: 1, name: "Settings" })).toBeInTheDocument();
    expect(screen.queryByText("Access & token")).not.toBeInTheDocument();
    expect(screen.getByText("Danger zone")).toBeInTheDocument();
  });

  it("applies appearance changes, keeps the token masked and can sign out", async () => {
    const user = userEvent.setup();
    await openSettings();

    await user.click(await screen.findByRole("radio", { name: /light/i }));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(await screen.findByText("Unsaved changes")).toBeInTheDocument();

    // Discard restores the daemon default theme.
    await user.click(screen.getByRole("button", { name: "Discard" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();

    const accessCard = (await screen.findByText("Access & token")).closest(".ds-card");
    expect(accessCard).not.toBeNull();
    expect(
      within(accessCard as HTMLElement).getByLabelText("masked token value"),
    ).toBeInTheDocument();
    expect(screen.queryByText("test-token")).not.toBeInTheDocument();

    await user.click(within(accessCard as HTMLElement).getByRole("button", { name: "Sign out" }));
    expect(window.localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });
});
