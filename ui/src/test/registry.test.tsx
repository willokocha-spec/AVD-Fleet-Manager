import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { PageBoundary } from "@/app/PageBoundary";
import { ToastProvider } from "@/lib/components";
import { ThemeProvider } from "@/lib/theme";
import { findPage, navPages, pages } from "@/routes";

const EXPECTED_PATHS = [
  "/",
  "/fleet",
  "/fleet/:name",
  "/images",
  "/templates",
  "/deploy",
  "/proxies",
  "/scripts",
  "/runs",
  "/settings",
  "/portable",
  "/doctor",
];

const EXPECTED_NAV = [
  "Dashboard",
  "Fleet",
  "Images",
  "Templates",
  "Proxies",
  "Scripts",
  "Runs",
  "Settings",
  "Portable",
];

describe("page auto-registration", () => {
  it("discovers every page through import.meta.glob", () => {
    expect(pages).toHaveLength(EXPECTED_PATHS.length);
    expect(pages.map((page) => page.meta.path).sort()).toEqual([...EXPECTED_PATHS].sort());
    expect(pages.every((page) => typeof page.meta.title === "string")).toBe(true);
    expect(pages.every((page) => typeof page.Page === "function")).toBe(true);
    expect(new Set(pages.map((page) => page.meta.path)).size).toBe(pages.length);
  });

  it("builds the sidebar from nav metadata in order", () => {
    expect(navPages.map((page) => page.meta.title)).toEqual(EXPECTED_NAV);
    expect(navPages.every((page) => typeof page.meta.nav === "number")).toBe(true);
    // Hidden routes never appear in the sidebar.
    expect(navPages.some((page) => page.meta.path === "/doctor")).toBe(false);
    expect(navPages.some((page) => page.meta.path === "/fleet/:name")).toBe(false);
  });

  it("resolves dynamic routes to their page", () => {
    expect(findPage("/")?.meta.title).toBe("Dashboard");
    expect(findPage("/fleet/avd-pixel7-01")?.meta.title).toBe("Instance");
    expect(findPage("/nope")).toBeUndefined();
  });

  it("renders every registered page without crashing", async () => {
    // Rendered directly (no auth/daemon round-trips) to keep the smoke test
    // fast; routing itself is covered by app.test.tsx.
    for (const { meta, Page } of pages) {
      const route = meta.path.replace(":name", "avd-pixel7-01");
      const { unmount } = render(
        <MemoryRouter initialEntries={[route]}>
          <ThemeProvider>
            <ToastProvider>
              <Routes>
                <Route
                  path={meta.path}
                  element={
                    <PageBoundary meta={meta}>
                      <Page />
                    </PageBoundary>
                  }
                />
              </Routes>
            </ToastProvider>
          </ThemeProvider>
        </MemoryRouter>,
      );
      const heading = await screen.findByRole("heading", { level: 1 });
      if (meta.path.includes(":name")) {
        expect(heading).toHaveTextContent("avd-pixel7-01");
      } else {
        expect(heading).toHaveTextContent(meta.title);
      }
      unmount();
    }
  });
});
