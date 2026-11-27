import type { ReactNode } from "react";
import { BrowserRouter } from "react-router-dom";

import { ToastProvider } from "@/lib/components";
import { ThemeProvider } from "@/lib/theme";

import { AuthProvider } from "./AuthProvider";
import { DaemonStatusProvider } from "./DaemonStatus";
import { EventStreamProvider } from "./EventStream";
import { AppShell } from "./Shell";

/**
 * Application-wide providers in dependency order:
 * theme → toasts → daemon health → auth → live events.
 */
export function AppProviders({ children }: { children: ReactNode }) {
  return (
    <ThemeProvider>
      <ToastProvider>
        <DaemonStatusProvider>
          <AuthProvider>
            <EventStreamProvider>{children}</EventStreamProvider>
          </AuthProvider>
        </DaemonStatusProvider>
      </ToastProvider>
    </ThemeProvider>
  );
}

/** Production entry: browser router + providers + shell. */
export function App() {
  return (
    <BrowserRouter>
      <AppProviders>
        <AppShell />
      </AppProviders>
    </BrowserRouter>
  );
}
