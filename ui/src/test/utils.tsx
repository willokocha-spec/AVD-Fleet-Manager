import { render, type RenderResult } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";

import { AppProviders } from "@/app/App";
import { AppShell } from "@/app/Shell";
import { TOKEN_STORAGE_KEY } from "@/lib/config";

export interface RenderAppOptions {
  route?: string;
  token?: string | null;
}

/**
 * Render the full provider stack + shell inside a MemoryRouter.
 * Sets the bearer token first so the auth check authenticates against MSW.
 */
export function renderApp(options: RenderAppOptions = {}): RenderResult {
  const { route = "/", token = "test-token" } = options;
  if (token) window.localStorage.setItem(TOKEN_STORAGE_KEY, token);
  else window.localStorage.removeItem(TOKEN_STORAGE_KEY);

  return render(
    <MemoryRouter initialEntries={[route]}>
      <AppProviders>
        <AppShell />
      </AppProviders>
    </MemoryRouter>,
  );
}

/** Render an arbitrary tree with the app's providers (no router requirements). */
export function renderWithProviders(ui: ReactElement): RenderResult {
  return render(<AppProviders>{ui}</AppProviders>);
}
