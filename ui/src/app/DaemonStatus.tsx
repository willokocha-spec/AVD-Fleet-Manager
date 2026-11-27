import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { ApiError, api } from "@/lib/api";
import type { HealthResponse } from "@/lib/types";

export type DaemonState = "checking" | "online" | "degraded" | "offline";

interface DaemonStatusContextValue {
  state: DaemonState;
  health: HealthResponse | null;
  /** Connection error message when offline. */
  message: string | null;
  lastCheckedAt: number | null;
  refresh: () => Promise<void>;
}

const DaemonStatusContext = createContext<DaemonStatusContextValue | null>(null);

const POLL_INTERVAL_MS = 15_000;

/**
 * Polls the daemon's unauthenticated `/health` endpoint and exposes a single
 * source of truth for the header status chip and the auth screen.
 */
export function DaemonStatusProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<DaemonState>("checking");
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [lastCheckedAt, setLastCheckedAt] = useState<number | null>(null);
  const mounted = useRef(true);
  const inFlight = useRef(false);

  const refresh = useCallback(async (): Promise<void> => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const result = await api.health();
      if (!mounted.current) return;
      setHealth(result);
      setState(result.status === "degraded" ? "degraded" : "online");
      setMessage(null);
    } catch (error) {
      if (!mounted.current) return;
      setState("offline");
      setMessage(
        error instanceof ApiError
          ? error.message
          : error instanceof Error
            ? error.message
            : "Daemon unreachable",
      );
    } finally {
      inFlight.current = false;
      if (mounted.current) setLastCheckedAt(Date.now());
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void refresh();
    const timer = window.setInterval(() => void refresh(), POLL_INTERVAL_MS);
    const onFocus = (): void => void refresh();
    const onVisibility = (): void => {
      if (document.visibilityState === "visible") void refresh();
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      mounted.current = false;
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [refresh]);

  const value = useMemo<DaemonStatusContextValue>(
    () => ({ state, health, message, lastCheckedAt, refresh }),
    [health, lastCheckedAt, message, refresh, state],
  );

  return <DaemonStatusContext.Provider value={value}>{children}</DaemonStatusContext.Provider>;
}

export function useDaemonStatus(): DaemonStatusContextValue {
  const context = useContext(DaemonStatusContext);
  if (!context) throw new Error("useDaemonStatus must be used inside <DaemonStatusProvider>");
  return context;
}
