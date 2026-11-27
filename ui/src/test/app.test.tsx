import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { renderApp } from "./utils";

describe("application shell", () => {
  it("renders the dashboard with navigation, daemon chip and auth state", async () => {
    renderApp();

    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    await waitFor(() => expect(document.title).toBe("Dashboard · AVD Fleet"));

    const nav = screen.getByRole("navigation", { name: /primary/i });    for (const label of [
      "Dashboard",
      "Fleet",
      "Images",
      "Templates",
      "Proxies",
      "Scripts",
      "Runs",
      "Settings",
      "Portable",
    ]) {
      expect(within(nav).getByRole("link", { name: label })).toBeInTheDocument();
    }

    await waitFor(() => expect(screen.getByText("Daemon online")).toBeInTheDocument());
    expect(screen.getByText("Authenticated")).toBeInTheDocument();
  });

  it("navigates between pages from the sidebar", async () => {
    const user = userEvent.setup();
    renderApp();
    await screen.findByRole("heading", { level: 1, name: "Dashboard" });

    await user.click(screen.getByRole("link", { name: "Fleet" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Fleet" })).toBeInTheDocument();
    await waitFor(() => expect(document.title).toBe("Fleet · AVD Fleet"));

    await user.click(screen.getByRole("link", { name: "Proxies" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Proxies" })).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Settings" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Settings" })).toBeInTheDocument();
  });

  it("renders a friendly not-found page for unknown routes", async () => {
    renderApp({ route: "/does-not-exist" });
    expect(await screen.findByRole("heading", { level: 1, name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /back to dashboard/i })).toBeInTheDocument();
  });
});
