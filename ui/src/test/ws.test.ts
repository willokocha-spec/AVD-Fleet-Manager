import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createEventSocket, fetchEventSocketTicket, type WebSocketLike } from "@/lib/ws";

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

/** Let the async ticket request and the socket factory settle. */
async function flushAsync(): Promise<void> {
  for (let index = 0; index < 10; index += 1) {
    await Promise.resolve();
  }
}

describe("event WebSocket client", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("fetches a single-use ticket and never puts the token in the socket URL", async () => {
    const sockets: FakeSocket[] = [];
    const events: unknown[] = [];
    const statuses: string[] = [];
    const tickets = vi.fn(async () => "ticket-1");

    createEventSocket({
      token: "secret-token",
      fetchTicket: tickets,
      onEvent: (event) => events.push(event),
      onStatus: (status) => statuses.push(status),
      WebSocketImpl: (url) => {
        const socket = new FakeSocket(url);
        sockets.push(socket);
        return socket;
      },
    });

    expect(statuses).toContain("connecting");
    // The socket waits for the ticket: the token is never placed in a URL.
    expect(sockets).toHaveLength(0);

    await flushAsync();
    expect(tickets).toHaveBeenCalledTimes(1);
    expect(tickets).toHaveBeenCalledWith("secret-token");
    expect(sockets).toHaveLength(1);

    const url = new URL(sockets[0]!.url);
    expect(url.pathname).toBe("/ws/events");
    expect(url.searchParams.get("ticket")).toBe("ticket-1");
    expect(url.searchParams.has("token")).toBe(false);
    expect(sockets[0]!.url).not.toContain("secret-token");

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

  it("ignores malformed frames", async () => {
    const events: unknown[] = [];
    let socket: FakeSocket | null = null;
    createEventSocket({
      token: "t",
      fetchTicket: async () => "ticket",
      onEvent: (event) => events.push(event),
      WebSocketImpl: (url) => {
        socket = new FakeSocket(url);
        socket.open();
        return socket;
      },
    });
    await flushAsync();
    socket!.message("not json");
    socket!.message(JSON.stringify({ id: "x" }));
    expect(events).toHaveLength(0);
  });

  it("fetches a fresh ticket on every reconnect and stops after close()", async () => {
    const sockets: FakeSocket[] = [];
    const statuses: string[] = [];
    let issued = 0;
    const tickets = vi.fn(async () => {
      issued += 1;
      return `ticket-${issued}`;
    });
    const handle = createEventSocket({
      token: "t",
      fetchTicket: tickets,
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

    await flushAsync();
    expect(sockets).toHaveLength(1);
    expect(new URL(sockets[0]!.url).searchParams.get("ticket")).toBe("ticket-1");

    sockets[0]!.drop();
    expect(statuses).toContain("reconnecting");
    expect(sockets).toHaveLength(1);

    vi.advanceTimersByTime(99);
    await flushAsync();
    expect(sockets).toHaveLength(1);
    vi.advanceTimersByTime(1);
    await flushAsync();
    expect(sockets).toHaveLength(2);
    // Each connect burns one ticket, so a reconnect must request a new one.
    expect(new URL(sockets[1]!.url).searchParams.get("ticket")).toBe("ticket-2");

    sockets[1]!.drop();
    vi.advanceTimersByTime(199);
    await flushAsync();
    expect(sockets).toHaveLength(2);
    vi.advanceTimersByTime(1);
    await flushAsync();
    expect(sockets).toHaveLength(3);
    expect(new URL(sockets[2]!.url).searchParams.get("ticket")).toBe("ticket-3");

    handle.close();
    expect(handle.status()).toBe("closed");
    vi.advanceTimersByTime(60_000);
    await flushAsync();
    expect(sockets).toHaveLength(3);
  });

  it("retries when the ticket request fails", async () => {
    const sockets: FakeSocket[] = [];
    const statuses: string[] = [];
    let calls = 0;
    const tickets = vi.fn(async () => {
      calls += 1;
      if (calls === 1) throw new Error("daemon unreachable");
      return "ticket-retry";
    });

    createEventSocket({
      token: "t",
      fetchTicket: tickets,
      onEvent: () => undefined,
      onStatus: (status) => statuses.push(status),
      WebSocketImpl: (url) => {
        const socket = new FakeSocket(url);
        sockets.push(socket);
        return socket;
      },
      minDelayMs: 100,
      jitter: 0,
      random: () => 0,
    });

    await flushAsync();
    expect(sockets).toHaveLength(0);
    expect(statuses).toContain("reconnecting");

    vi.advanceTimersByTime(100);
    await flushAsync();
    expect(sockets).toHaveLength(1);
    expect(new URL(sockets[0]!.url).searchParams.get("ticket")).toBe("ticket-retry");
  });

  it("does not open a socket when closed while the ticket request is in flight", async () => {
    const sockets: FakeSocket[] = [];
    let release: ((ticket: string) => void) | null = null;
    const handle = createEventSocket({
      token: "t",
      fetchTicket: () =>
        new Promise<string>((resolve) => {
          release = resolve;
        }),
      onEvent: () => undefined,
      WebSocketImpl: (url) => {
        const socket = new FakeSocket(url);
        sockets.push(socket);
        return socket;
      },
    });

    handle.close();
    (release as ((ticket: string) => void) | null)?.("late-ticket");
    await flushAsync();
    expect(sockets).toHaveLength(0);
  });

  it("requests the ticket from the authenticated endpoint", async () => {
    const fetchMock = vi.fn<typeof fetch>(
      async () =>
        new Response(JSON.stringify({ ticket: "abc", expires_in: 30 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchEventSocketTicket("secret-token")).resolves.toBe("abc");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(String(url)).toContain("/api/v1/auth/ws-ticket");
    expect(init?.method).toBe("POST");
    expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer secret-token");
  });
});
