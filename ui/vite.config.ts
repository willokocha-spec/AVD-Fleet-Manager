import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/**
 * Vite configuration for the AVD Fleet GUI.
 *
 * - `@` aliases `src/`.
 * - In dev, requests the MSW mocks do not handle fall through to the real daemon
 *   on 127.0.0.1:8765 (HTTP + WebSocket) via the Vite proxy.
 * - `pnpm build` emits `dist/` which the daemon serves (see ui/README.md).
 */
export default defineConfig({
  plugins: [react()],
  base: "/",
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: "http://127.0.0.1:8765", changeOrigin: true },
      "/health": { target: "http://127.0.0.1:8765", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8765", ws: true, changeOrigin: true },
    },
  },
  preview: {
    port: 4173,
    strictPort: true,
  },
  build: {
    outDir: "dist",
    target: "es2022",
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
    restoreMocks: true,
    clearMocks: true,
    // jsdom + MSW request interception is comparatively slow on cold workers.
    testTimeout: 30_000,
    hookTimeout: 30_000,
    environmentOptions: {
      jsdom: { url: "http://127.0.0.1:5173/" },
    },
  },
});
