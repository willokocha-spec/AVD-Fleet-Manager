import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { mocksEnabled } from "@/lib/config";
import type { FleetEvent } from "@/lib/types";
import { createEventSocket, createSimulatedEventSocket, type SocketStatus } from "@/lib/ws";

import { useAuth } from "./AuthProvider";

const MAX_EVENTS = 200;

type EventStreamStatus = SocketStatus | "disabled";

interface EventStreamContextValue {
  /** Connection state of `/ws/events` (or the dev simulator). */
  status: EventStreamStatus;
  /** Most recent events, oldest → newest (bounded). */
  events: FleetEvent[];
  lastEvent: FleetEvent | null;
  clearEvents: () => void;
}

const EventStreamContext = createContext<EventStreamContextValue | null>(null);

/**
 * Keeps a bounded live event buffer for pages that want to react to instance,
 * proxy or script activity. Connection lifecycle lives here so pages never
 * open their own socket.
 */
export function EventStreamProvider({ children }: { children: ReactNode }) {
  const { status: authStatus, token } = useAuth();
  const [status, setStatus] = useState<EventStreamStatus>("idle");
  const [events, setEvents] = useState<FleetEvent[]>([]);

  const append = useCallback((event: FleetEvent) => {
    setEvents((current) => [...current, event].slice(-MAX_EVENTS));
  }, []);

  const clearEvents = useCallback(() => setEvents([]), []);

  useEffect(() => {
    if (authStatus !== "authenticated") {
      setStatus("idle");
      return;
    }
    // Tests never open sockets; they assert the REST surface instead.
    if (import.meta.env.MODE === "test") {
      setStatus("disabled");
      return;
    }
    if (mocksEnabled()) {
      const simulated = createSimulatedEventSocket({
        onEvent: append,
        onStatus: (next) => setStatus(next),
      });
      return () => simulated.close();
    }
    const socket = createEventSocket({
      token,
      onEvent: append,
      onStatus: (next) => setStatus(next),
    });
    return () => socket.close();
  }, [append, authStatus, token]);

  const value = useMemo<EventStreamContextValue>(
    () => ({
      status,
      events,
      lastEvent: events.length > 0 ? events[events.length - 1]! : null,
      clearEvents,
    }),
    [clearEvents, events, status],
  );

  return <EventStreamContext.Provider value={value}>{children}</EventStreamContext.Provider>;
}

export function useEventStream(): EventStreamContextValue {
  const context = useContext(EventStreamContext);
  if (!context) throw new Error("useEventStream must be used inside <EventStreamProvider>");
  return context;
}
