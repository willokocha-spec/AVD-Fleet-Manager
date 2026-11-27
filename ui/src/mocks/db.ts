import { createInitialDb, type MockDb } from "./fixtures";

/**
 * Mutable in-memory state for the MSW mock daemon. Reset between tests with
 * `resetMockDb()`; the browser dev session resets on a page reload.
 */
let db: MockDb = createInitialDb();

export function getDb(): MockDb {
  return db;
}

export function resetMockDb(): void {
  db = createInitialDb();
}

export type { MockDb };
