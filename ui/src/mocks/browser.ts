import { setupWorker } from "msw/browser";

import { handlers } from "./handlers";

/** Browser Service Worker used by `vite dev` when VITE_USE_MOCKS is enabled. */
export const worker = setupWorker(...handlers);
