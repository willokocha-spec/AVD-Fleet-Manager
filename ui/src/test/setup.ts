import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";

import { resetMockDb } from "@/mocks/db";
import { server } from "@/mocks/server";

// jsdom implements neither scrolling nor matchMedia; the shell uses both.
Object.defineProperty(window, "scrollTo", { value: () => undefined, writable: true });
if (!window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      dispatchEvent: () => false,
    }),
  });
}

// Every test hits the real client against the MSW mock daemon. Unhandled
// requests fail loudly so a missing handler can never silently pass.
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));

afterEach(() => {
  server.resetHandlers();
  resetMockDb();
  cleanup();
  window.localStorage.clear();
  window.sessionStorage.clear();
  document.documentElement.dataset.theme = "dark";
});

afterAll(() => server.close());
