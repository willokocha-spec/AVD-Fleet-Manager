import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { renderApp } from "./utils";

describe("auth flow", () => {
  it("shows the connect screen when no token is stored", async () => {
    renderApp({ token: null });

    expect(await screen.findByRole("heading", { level: 2, name: /Connect to AVD Fleet/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/bearer token/i)).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: /primary/i })).not.toBeInTheDocument();
    // The daemon health chip is still visible before authentication.
    expect(await screen.findByText("Daemon online")).toBeInTheDocument();
  });

  it("authenticates with a pasted token and opens the console", async () => {
    const user = userEvent.setup();
    renderApp({ token: null });
    await screen.findByRole("heading", { level: 2, name: /Connect to AVD Fleet/i });

    await user.type(screen.getByLabelText(/bearer token/i), "dev-token");
    await user.click(screen.getByRole("button", { name: /connect to daemon/i }));

    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    expect(window.localStorage.getItem("avdfleet.token")).toBe("dev-token");
    expect(screen.getByText("Authenticated")).toBeInTheDocument();
  });

  it("signs out back to the connect screen", async () => {
    const user = userEvent.setup();
    renderApp();
    await screen.findByRole("heading", { level: 1, name: "Dashboard" });

    await user.click(screen.getByRole("button", { name: "Sign out" }));

    expect(await screen.findByRole("heading", { level: 2, name: /Connect to AVD Fleet/i })).toBeInTheDocument();
    expect(window.localStorage.getItem("avdfleet.token")).toBeNull();
  });

  it("validates that a token is required", async () => {
    renderApp({ token: null });
    await screen.findByRole("heading", { level: 2, name: /Connect to AVD Fleet/i });
    expect(screen.getByRole("button", { name: /connect to daemon/i })).toBeDisabled();
  });
});
