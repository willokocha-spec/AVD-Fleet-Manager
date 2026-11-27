/**
 * Cross-package-manager test runner.
 *
 * `pnpm test -- --run` forwards the separator literally (`vitest -- --run`),
 * which Vitest interprets as a filename filter and then stays in watch mode.
 * This wrapper normalises the invocation so both of these always run the suite
 * exactly once:
 *
 *   pnpm test
 *   pnpm test -- --run
 *   pnpm test -- --reporter=verbose --coverage
 */
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const uiRoot = resolve(here, "..");
const require = createRequire(import.meta.url);

const vitestPkgPath = require.resolve("vitest/package.json");
const vitestPkg = require("vitest/package.json");
const vitestBin = resolve(dirname(vitestPkgPath), vitestPkg.bin.vitest);

const passthrough = process.argv
  .slice(2)
  .filter((arg) => arg !== "--" && arg !== "--run" && arg !== "-r");

const result = spawnSync(process.execPath, [vitestBin, "run", ...passthrough], {
  cwd: uiRoot,
  stdio: "inherit",
  env: { ...process.env, CI: process.env.CI ?? "1" },
});

if (result.error) {
  console.error("[avdfleet-ui] failed to start Vitest:", result.error);
  process.exit(1);
}
process.exit(result.status ?? 1);
