import type { ComponentType } from "react";

import type { IconName } from "./components/Icon";

/**
 * ── PAGE CONTRACT (read me before adding a page) ─────────────────────────────
 *
 * Every file matching `src/pages/*.tsx` is auto-registered by
 * `src/routes.ts` via `import.meta.glob("./pages/*.tsx", { eager: true })`.
 * No route table edits are needed — or allowed — for new pages.
 *
 * A page module MUST export:
 *
 *   1. `export const meta: PageMeta` — route metadata:
 *        path        absolute React Router path, e.g. "/fleet" or "/fleet/:name"
 *        title       string used for the <h1>, document title and sidebar
 *        icon        one of the IconName glyphs in @/lib/components
 *        nav         sidebar order (10, 20, 30…) or `false` to hide it
 *        description optional nav tooltip / header subtitle
 *
 *   2. `export default` — the page component (no props; reads route params
 *      from `useParams()` itself).
 *
 * Rules:
 * - Paths must be unique; duplicates fail fast at startup and in tests.
 * - Hidden routes (detail pages, wizards) set `nav: false`.
 * - Use `PageHeader` as the first element: it owns the page's <h1>.
 * - Data access goes through `@/lib/api`; never call `fetch` directly.
 * - Keep page state local; shared providers live in `src/app`.
 */
export interface PageMeta {
  path: string;
  title: string;
  icon: IconName;
  nav: number | false;
  description?: string;
}

export interface PageModule {
  meta: PageMeta;
  default: ComponentType;
}
