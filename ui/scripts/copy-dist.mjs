/**
 * Copy the production UI build into the Python package so the daemon serves it.
 *
 *   ui/dist  ->  src/avdfleet/api/static
 *
 * Run via `pnpm build:daemon`. The `copy-dist` step is deliberately explicit:
 * plain `pnpm build` only produces `ui/dist`, so UI-only iteration never touches
 * the Python tree. The wheel force-includes this directory (see pyproject.toml),
 * and the API mounts it at `/` after the REST routes.
 */
import { cp, mkdir, rm, stat } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const uiRoot = resolve(here, "..");
const distDir = resolve(uiRoot, "dist");
const targetDir = resolve(uiRoot, "..", "src", "avdfleet", "api", "static");

try {
  const info = await stat(distDir);
  if (!info.isDirectory()) throw new Error("dist is not a directory");
} catch {
  console.error(`[copy-dist] ${distDir} not found — run "pnpm build" first.`);
  process.exit(1);
}

await rm(targetDir, { recursive: true, force: true });
await mkdir(dirname(targetDir), { recursive: true });
await cp(distDir, targetDir, { recursive: true });

console.log(`[copy-dist] copied ${distDir} -> ${targetDir}`);
