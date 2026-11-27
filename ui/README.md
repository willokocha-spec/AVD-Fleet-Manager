# AVD Fleet GUI (`ui/`)

React + TypeScript single-page operator console for the `avdfleet` daemon.
Served by the installed daemon at `http://127.0.0.1:8765/`; developed against the
frozen OpenAPI contract with an in-browser MSW mock daemon.

This directory is the **U0 foundation**: shell, routing, theme, typed API/WS
clients, design system, mocks and the page contract. Page implementations are
owned by U1–U7 and live in `src/pages/`.

## Stack

| Concern      | Choice |
| ------------ | ------ |
| Runtime      | React 18 + TypeScript 5 (strict), Vite 5 |
| Routing      | React Router 6 (`BrowserRouter`) |
| Styling      | Hand-written CSS design system, CSS variables, dark-first + light + system palettes |
| API          | Typed fetch client (`src/lib/api.ts`) against `/api/v1` (frozen OpenAPI 3.1) |
| Realtime     | `src/lib/ws.ts` — `/ws/events?token=…`, typed events, backoff+jitter, dev simulator |
| Mocks        | MSW 2 — browser service worker for dev, node server for Vitest |
| Tests        | Vitest + Testing Library + jsdom + MSW, 40 tests |
| Lint/format  | ESLint 9 flat config + typescript-eslint |

## Commands

Run with the repository's bundled Node/pnpm from this directory:

```powershell
node <path-to-node.exe> <path-to-pnpm.mjs> install
pnpm dev            # http://127.0.0.1:5173, MSW mocks on
pnpm build          # tsc --noEmit + vite build -> ui/dist
pnpm build:daemon   # vite build + copy dist -> ../src/avdfleet/api/static
pnpm typecheck      # tsc --noEmit
pnpm lint           # eslint . --max-warnings=0
pnpm test -- --run  # run the suite once (the wrapper also accepts plain `pnpm test`)
pnpm preview        # serve the built app
```

`scripts/test.mjs` normalises argument forwarding: pnpm 11 passes the `--`
separator through to Vitest, which would otherwise treat `--run` as a filename
filter and stay in watch mode. Both `pnpm test` and `pnpm test -- --run` always
run once; `pnpm test:watch` keeps Vitest's watch mode.

`mockServiceWorker.js` is generated once with `pnpm mocks:init` and committed.

### Environment flags (`.env.example`)

| Variable            | Default     | Meaning |
| ------------------- | ----------- | ------- |
| `VITE_USE_MOCKS`    | dev: `true` | MSW mocks intercept `/api/v1`; set `false` to talk to a real daemon |
| `VITE_API_BASE_URL` | `/api/v1`   | REST base URL (same origin by default) |
| `VITE_WS_URL`       | `/ws/events`| Event WebSocket URL |

With mocks **off**, `vite dev` proxies `/api`, `/health` and `/ws` to
`http://127.0.0.1:8765`, so a locally running `avdfleet serve` works without CORS.
With mocks **on** (dev default), requests the mock handlers do not own pass
through to the same proxy (`onUnhandledRequest: "bypass"`).

## Page contract (U1–U7: read this)

Every `src/pages/*.tsx` file is auto-registered — never edit the route table.

```tsx
import { PageHeader } from "@/lib/components";
import type { PageMeta } from "@/lib/page-meta";

export const meta: PageMeta = {
  path: "/fleet",          // absolute; may contain ":name" params
  title: "Fleet",          // <h1>, document title, sidebar label
  icon: "fleet",           // IconName from @/lib/components
  nav: 20,                 // sidebar order; false = hidden route
  description: "…",        // optional nav tooltip
};

export default function FleetPage() {
  return (
    <div className="page">
      <PageHeader title="Fleet" description="…" actions={/* … */} />
      {/* content */}
    </div>
  );
}
```

Rules enforced by `src/routes.ts` (fail fast at startup and in tests):

- `meta` and a default component are required; `path` must start with `/`.
- Paths must be unique. Hidden routes (`/fleet/:name`, `/deploy`, `/doctor`) use `nav: false`.
- `src/pages/*.tsx` is the **only** glob; put tests in `src/test/` (never in `src/pages/`).
- The registry provides `pages`, `navPages` and `findPage(pathname)` from `@/routes`.
- `PageBoundary` sets `document.title` (`"<title> · AVD Fleet"`), catches render
  errors per page, and restores the rest of the console.
- Data access: `api` from `@/lib/api`; state helpers in `@/lib/hooks` (`useAsync`).
  Never call `fetch` directly.
- Use the design system from `@/lib/components` (barrel import). It provides:
  `PageHeader`, `Button`, `ButtonLink`, `IconButton`, `Card`, `StatCard`,
  `Badge`, `StatusPill`, `Table`, `EmptyState`, `Spinner`, `Skeleton`,
  `ProgressBar`, `InlineNotice`, `Modal`, `ConfirmDialog`, `Field`, `Input`,
  `Select`, `Textarea`, `Checkbox`, `SearchBox`, `Tabs`, `ToastProvider`/`useToast`,
  `CodeBlock`, `LogViewer`, `Icon`.
- Page states: render meaningful loading (`Skeleton`/`Spinner`), empty
  (`EmptyState`) and error (`InlineNotice tone="danger"`) presentations. The stub
  files currently shipped in `src/pages/` show the expected quality bar.

## API client shape

`src/lib/api.ts` exports:

```ts
api.listInstances(): Promise<AvdInfo[]>
api.startInstance(name, { proxy_id }): Promise<AvdInfo>
api.runDoctor(slow?): Promise<DoctorReport>
// … one method per operation in contracts/openapi.yaml, grouped by tag
```

- Bearer token: `localStorage["avdfleet.token"]`, sent as `Authorization: Bearer`.
- Errors: non-2xx envelope → `ApiError { status, code, message, hint, details, kind }`.
  `kind` is `http | network | timeout | aborted | parse`.
- `401` responses notify `onUnauthorized()` — `AuthProvider` clears the session and
  returns to the connect screen; `useAuth()` exposes `status`, `login`, `signOut`.
- Every request has a timeout (default 15 s; per-endpoint overrides for long
  operations) and accepts an optional `AbortSignal`.
- Datetimes are ISO-8601 strings; types mirror `src/avdfleet/core/models.py`.

`src/lib/ws.ts`:

```ts
const socket = createEventSocket({ token, onEvent, onStatus });
// onStatus: idle | connecting | open | reconnecting | closed
socket.close();
```

Reconnect uses exponential backoff + jitter (1 s → 30 s). `EventStreamProvider`
owns the single socket, keeps the last 200 events and exposes `useEventStream()`.
In MSW dev mode a simulated stream is used; in tests sockets are never opened
(`MODE === "test"`).

## MSW mocks

`src/mocks/handlers.ts` implements **every** endpoint in `contracts/openapi.yaml`
with realistic, stateful data:

- 5 AVDs (running, running, booting, stopped, error) with ports/proxies.
- 3 images: API 36 Google APIs, API 35 **Google Play**, API 34 AOSP (not installed).
- 5 proxies with masked credentials (`has_credentials`, never a password), health
  states ok/failed/unknown, one disabled.
- 3 templates, 3 scripts, 3 runs (succeeded/running/failed), snapshots per AVD.
- Doctor report with `ok`/`warn`/`fail`/`skip`, host report, capacity, devices,
  OOBE matrix (verified + unverified), portable checks, events.
- Auth guard: any non-empty bearer token is accepted (`dev-token` is the dev
  shortcut); mutating endpoints are reflected in later reads.
- `resetMockDb()` restores fixtures between tests.

## Build → daemon

`pnpm build` writes `ui/dist/` (gitignored). The installed daemon serves it from
`src/avdfleet/api/static`; the wheel force-includes that directory
(`pyproject.toml`). Run `pnpm build:daemon` to build and copy in one step. The API
must mount the static directory at `/` **after** `/api/v1`, `/health` and `/ws`
routes, with an SPA fallback to `index.html` for client-side routes.

## Test coverage

`src/test/` contains: app render/nav/404, page auto-registration + every-page
smoke, typed API success/error/401/timeout, mock realism/mutations, auth screen
login/sign-out, status pill/button/field/modal/tabs/progress/toast components,
and WebSocket connect/parse/backoff tests. Playwright E2E belongs to T3 in
`ui/e2e/` (not part of `pnpm test`).

## Known gaps for page agents (U1–U7) and T3

- Stub pages in `src/pages/` intentionally render empty/loading states only;
  wire them to `api` per the table below.
- No screenshot/screen-stream viewer component yet (screen API exists:
  `api.getScreenshot`, `api.sendInput`; WS screen stream is a T3/U2 concern).
- U1 Dashboard/Doctor; U2 Fleet/InstanceDetail; U3 Images/Templates/Deploy;
  U4 Proxies; U5 Scripts/Runs; U6 Settings/auth; U7 Portable.
- Toast helpers exist but pages must call them after mutations.
- `ui/e2e/` is reserved for T3; `pnpm test` ignores it.
- No i18n layer; copy is English.
