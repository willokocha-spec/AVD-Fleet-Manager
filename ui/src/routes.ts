import type { ComponentType } from "react";
import { matchPath } from "react-router-dom";

import type { PageMeta, PageModule } from "./lib/page-meta";

/**
 * Page auto-registration.
 *
 * Every `src/pages/*.tsx` module that exports `meta` (see lib/page-meta.ts) and
 * a default component becomes a route and — when `meta.nav` is a number — a
 * sidebar entry. Adding a page requires no changes to this file or to the app
 * shell. Invalid or duplicated metadata fails fast with a descriptive error.
 */
const modules = import.meta.glob<PageModule>("./pages/*.tsx", { eager: true });

export interface RegisteredPage {
  meta: PageMeta;
  Page: ComponentType;
  /** Module key, useful in error messages and tests. */
  modulePath: string;
}

function validateAndCollect(): RegisteredPage[] {
  const collected: RegisteredPage[] = [];
  const seen = new Map<string, string>();

  for (const [modulePath, module] of Object.entries(modules)) {
    const meta = (module as Partial<PageModule>).meta;
    const Page = (module as Partial<PageModule>).default;
    if (!meta || typeof meta !== "object") {
      throw new Error(`Page module "${modulePath}" must export \`meta\` (see src/lib/page-meta.ts).`);
    }
    if (typeof Page !== "function") {
      throw new Error(`Page module "${modulePath}" must export a default React component.`);
    }
    if (typeof meta.path !== "string" || !meta.path.startsWith("/")) {
      throw new Error(`Page module "${modulePath}" has an invalid meta.path: ${String(meta.path)}`);
    }
    if (typeof meta.title !== "string" || meta.title.trim() === "") {
      throw new Error(`Page module "${modulePath}" has an empty meta.title.`);
    }
    if (typeof meta.icon !== "string") {
      throw new Error(`Page module "${modulePath}" must set meta.icon.`);
    }
    const existing = seen.get(meta.path);
    if (existing) {
      throw new Error(`Duplicate page path "${meta.path}" in "${existing}" and "${modulePath}".`);
    }
    seen.set(meta.path, modulePath);
    collected.push({ meta, Page, modulePath });
  }

  return collected.sort((left, right) => left.meta.path.localeCompare(right.meta.path));
}

export const pages: RegisteredPage[] = validateAndCollect();

/** Sidebar entries, ordered by `meta.nav`. */
export const navPages: RegisteredPage[] = pages
  .filter((page) => page.meta.nav !== false)
  .sort((left, right) => {
    const leftOrder = typeof left.meta.nav === "number" ? left.meta.nav : Number.MAX_SAFE_INTEGER;
    const rightOrder = typeof right.meta.nav === "number" ? right.meta.nav : Number.MAX_SAFE_INTEGER;
    if (leftOrder !== rightOrder) return leftOrder - rightOrder;
    return left.meta.title.localeCompare(right.meta.title);
  });

/** Resolve the page registered for a URL path (supports `:params`). */
export function findPage(pathname: string): RegisteredPage | undefined {
  return pages.find((page) => matchPath({ path: page.meta.path, end: true }, pathname) !== null);
}
