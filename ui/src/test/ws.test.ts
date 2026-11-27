import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createEventSocket, type WebSocketLike } from "@/lib/ws";

class FakeSocket implements WebSocketLike {
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  closed = false;

  constructor(readonly url: string) {}

  close(): void {
    this.closed = true;
  }

  open(): void {
    this.onopen?.(new Event("open"));
  }

  message(data: string): void {
    this.onmessage?.({ data } as MessageEvent);
  }

  drop(): void {
    this.onclose?.({} as CloseEvent);
  }
}

describe("event WebSocket client", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("connects with the token in the query string and dispatches typed events", () => {
    const sockets: FakeSocket[] = [];
    const events: unknown[] = [];
    const statuses: string[] = [];

    createEventSocket({
      token: "secret-token",
      onEvent: (event) => events.push(event),
      onStatus: (status) => statuses.push(status),
      WebSocketImpl: (url) => {
        const socket = new FakeSocket(url);
        sockets.push(socket);
        return socket;
      },
    });

    expect(sockets).toHaveLength(1);
    expect(sockets[0]!.url).toContain("/ws/events");
    expect(sockets[0]!.url).toContain("token=secret-token");
    expect(statuses).toContain("connecting");

    sockets[0]!.open();
    expect(statuses).toContain("open");

    sockets[0]!.message(
      JSON.stringify({
        id: "evt-1",
        type: "instance.state",
        ts: new Date().toISOString(),
        payload: { instance: "avd-1", state: "running" },
      }),
    );
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ id: "evt-1", type: "instance.state" });
  });

  it("ignores malformed frames", () => {
    const events: unknown[] = [];
    let socket: FakeSocket | null = null;
    createEventSocket({
      token: "t",
      onEvent: (event) => events.push(event),
      WebSocketImpl: (url) => {
        socket = new FakeSocket(url);
        socket.open();
        return socket;
      },
    });
    socket!.message("not json");
    socket!.message(JSON.stringify({ id: "x" }));
    expect(events).toHaveLength(0);
  });

  it("reconnects with exponential backoff and stops after close()", () => {
    const sockets: FakeSocket[] = [];
    const statuses: string[] = [];
    const handle = createEventSocket({
      token: "t",
      onEvent: () => undefined,
      onStatus: (status) => statuses.push(status),
      WebSocketImpl: (url) => {
        const socket = new FakeSocket(url);
        sockets.push(socket);
        return socket;
      },
      minDelayMs: 100,
      maxDelayMs: 1_000,
      jitter: 0,
      random: () => 0,
    });

    expect(sockets).toHaveLength(1);
    sockets[0]!.drop();
    expect(statuses).toContain("reconnecting");
    expect(sockets).toHaveLength(1);

    vi.advanceTimersByTime(99);
    expect(sockets).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(sockets).toHaveLength(2);

    sockets[1]!.drop();
    vi.advanceTimersByTime(199);
    expect(sockets).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(sockets).toHaveLength(3);

    handle.close();
    expect(handle.status()).toBe("closed");
    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(3);
  });
});
