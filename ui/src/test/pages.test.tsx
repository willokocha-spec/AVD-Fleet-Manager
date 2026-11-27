import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderApp } from "./utils";

/**
 * Smoke test for the page contract: a new page exports `meta` + a default
 * component, is discovered by `import.meta.glob`, becomes routable, updates the
 * document title and shows its header. Kept intentionally small; deeper page
 * behaviour belongs to the owning page agent.
 */
describe("page contract smoke test", () => {
  it("renders Images at /images with its metadata applied", async () => {
    renderApp({ route: "/images" });
    expect(await screen.findByRole("heading", { level: 1, name: "Images" })).toBeInTheDocument();
    await waitFor(() => expect(document.title).toBe("Images · AVD Fleet"));
    expect(screen.getByText("System images")).toBeInTheDocument();
  });

  it("renders a detail page from its route param", async () => {
    renderApp({ route: "/fleet/avd-pixel7-01" });
    expect(
      await screen.findByRole("heading", { level: 1, name: "avd-pixel7-01" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: /breadcrumb/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Overview" })).toBeInTheDocument();
  });
});
