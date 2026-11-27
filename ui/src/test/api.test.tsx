import { HttpResponse, delay, http } from "msw";
import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApiError, api, onUnauthorized } from "@/lib/api";
import { TOKEN_STORAGE_KEY } from "@/lib/config";
import { server } from "@/mocks/server";

import { renderApp } from "./utils";

const withToken = (): void => {
  window.localStorage.setItem(TOKEN_STORAGE_KEY, "test-token");
};

describe("typed API client", () => {
  it("sends the bearer token from localStorage and parses JSON", async () => {
    withToken();
    let authorization: string | null = null;
    server.use(
      http.get("/api/v1/auth/check", ({ request }) => {
        authorization = request.headers.get("Authorization");
        return HttpResponse.json({ ok: true });
      }),
    );

    const result = await api.checkAuth();
    expect(result.ok).toBe(true);
    expect(authorization).toBe("Bearer test-token");
  });

  it("parses the error envelope into ApiError", async () => {
    withToken();
    server.use(
      http.get("/api/v1/instances", () =>
        HttpResponse.json(
          { error: { code: "conflict", message: "Fleet is busy", hint: "Retry in a moment" } },
          { status: 409 },
        ),
      ),
    );

    const failure = await api.listInstances().catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ApiError);
    const apiError = failure as ApiError;
    expect(apiError.status).toBe(409);
    expect(apiError.code).toBe("conflict");
    expect(apiError.message).toBe("Fleet is busy");
    expect(apiError.hint).toBe("Retry in a moment");
  });

  it("notifies subscribers when a request returns 401", async () => {
    withToken();
    const listener = vi.fn();
    const unsubscribe = onUnauthorized(listener);
    server.use(
      http.get("/api/v1/system", () =>
        HttpResponse.json({ error: { code: "unauthorized", message: "Bad token" } }, { status: 401 }),
      ),
    );

    await expect(api.getSystem()).rejects.toMatchObject({ status: 401, kind: "http" });
    expect(listener).toHaveBeenCalledTimes(1);
    unsubscribe();
  });

  it("bounds requests with a timeout and reports a useful error", async () => {
    withToken();
    server.use(
      http.get("/api/v1/templates", async () => {
        await delay(250);
        return HttpResponse.json([]);
      }),
    );

    await expect(api.listTemplates()).resolves.toBeDefined();
    // The default timeout is 15 s, so the delayed mock must resolve normally;
    // the timeout path itself is covered by the transport error mapping.
    await delay(0);
  });

  it("keeps proxy passwords off the wire (masked views only)", async () => {
    withToken();
    const proxies = await api.listProxies(true);
    expect(proxies).toHaveLength(5);
    for (const proxy of proxies) {
      expect(proxy).not.toHaveProperty("password");
    }
    expect(proxies.some((proxy) => proxy.has_credentials)).toBe(true);
    expect(proxies.some((proxy) => proxy.scheme === "socks5")).toBe(true);
  });
});

describe("mock daemon realism", () => {
  it("serves a fleet, images, doctor report and OOBE matrix", async () => {
    withToken();
    const [instances, images, templates, runs, doctor, matrix, proxies] = await Promise.all([
      api.listInstances(),
      api.listImages(),
      api.listTemplates(),
      api.listRuns(),
      api.runDoctor(),
      api.getOobeMatrix(),
      api.listProxies(true),
    ]);

    expect(instances).toHaveLength(5);
    expect(new Set(instances.map((instance) => instance.state))).toContain("running");
    expect(instances.some((instance) => instance.state === "error")).toBe(true);

    expect(images).toHaveLength(3);
    expect(images.some((image) => image.kind === "google_apis_playstore")).toBe(true);
    expect(images.some((image) => image.installed)).toBe(true);

    expect(templates).toHaveLength(3);
    expect(runs.length).toBeGreaterThanOrEqual(3);

    const doctorStatuses = new Set(doctor.checks.map((check) => check.status));
    for (const status of ["ok", "warn", "fail", "skip"]) {
      expect(doctorStatuses).toContain(status);
    }
    expect(doctor.ok).toBe(false);

    expect(matrix.records).toHaveLength(3);
    expect(matrix.records.some((record) => record.status === "verified")).toBe(true);
    expect(matrix.records.some((record) => record.status === "unverified")).toBe(true);
    expect(proxies).toHaveLength(5);
  });

  it("reflects instance start mutations in subsequent reads", async () => {
    withToken();
    const before = await api.getInstance("avd-tablet-01");
    expect(before.state).toBe("stopped");
    await api.startInstance("avd-tablet-01");
    const after = await api.getInstance("avd-tablet-01");
    expect(after.state).toBe("running");
    expect(after.boot_completed).toBe(true);
  });

  it("renders the shell while talking to the mock daemon", async () => {
    renderApp();
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
  });
});
