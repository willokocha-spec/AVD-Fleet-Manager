import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "@/app/App";

import "./styles/index.css";

/**
 * Bootstrap: when mocks are enabled (the dev default), start the MSW service
 * worker so the whole console runs without a daemon. Requests the mock handlers
 * do not own pass through to the Vite dev proxy → 127.0.0.1:8765.
 *
 * The condition is written inline (rather than via `mocksEnabled()`) so Vite
 * statically removes the MSW browser chunk from production builds.
 */
const USE_MOCKS =
  import.meta.env.VITE_USE_MOCKS === "true" ||
  (import.meta.env.DEV && import.meta.env.VITE_USE_MOCKS !== "false");

async function bootstrap(): Promise<void> {
  if (USE_MOCKS) {
    const { worker } = await import("./mocks/browser");
    await worker.start({
      onUnhandledRequest: "bypass",
      quiet: true,
      serviceWorker: { url: `${import.meta.env.BASE_URL}mockServiceWorker.js` },
    });
  }

  const container = document.getElementById("root");
  if (!container) throw new Error('Missing #root element in index.html');

  createRoot(container).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

void bootstrap().catch((error: unknown) => {
  console.error("Failed to start the AVD Fleet GUI", error);
  const container = document.getElementById("root");
  if (container) {
    container.innerHTML =
      '<div style="font-family: system-ui, sans-serif; padding: 2.5rem; max-width: 40rem; margin: 0 auto; color: #e8eef7; background: #0b0f16; min-height: 100vh;">' +
      '<h1 style="font-size: 1.25rem;">AVD Fleet failed to start</h1>' +
      '<p style="color: #9aa8bd; line-height: 1.6;">The web UI could not initialise. Open the browser console for the underlying error, then reload.</p>' +
      "</div>";
  }
});
