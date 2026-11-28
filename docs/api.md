# API

The daemon exposes a REST API under `/api/v1` plus two WebSocket endpoints. The
frozen OpenAPI 3.1 contract is checked in at
[contracts/openapi.yaml](../contracts/openapi.yaml) (73 paths, 92 operations) and
served by a running daemon at `/openapi.json`, with interactive docs at `/docs`
(disable with `AVDFLEET_DOCS=0`).

- Default URL: `http://127.0.0.1:8765`
- API prefix: `/api/v1`
- Auth: `Authorization: Bearer <token>` on every `/api/v1` route; `/health` is
  unauthenticated
- Content type: JSON (`application/json`); screenshots are `image/png`
- Errors: one envelope, documented below, always redacted
- Correlation: every response carries `X-Request-ID`

## Authentication

The bearer token is created on first daemon start at `<data>/token` with
owner-only permissions (`0600`; Windows ACL via `icacls`, best effort).
`AVDFLEET_TOKEN` overrides the file and is never persisted. Read the token on
Linux/macOS with:

```bash
TOKEN=$(cat "$HOME/.local/share/avdfleet/token")          # Linux
TOKEN=$(cat "$HOME/Library/Application Support/avdfleet/token")  # macOS
# Windows PowerShell: $TOKEN = Get-Content "$env:LOCALAPPDATA\avdfleet\token"
```

Then:

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/v1/system
```

- Missing/invalid token → `401` with `WWW-Authenticate: Bearer realm="avdfleet"`
  and the error envelope. A token is never echoed or logged.
- Comparison is constant-time (SHA-256 + `hmac.compare_digest`).
- CORS is an explicit origin allowlist; `TrustedHostMiddleware` limits Host
  headers. The actual bind host, port and loopback aliases are added automatically; for a
  wildcard bind (`0.0.0.0`/`::`) an explicit `--trusted-host` is required, and
  `--trusted-origin` is available for reverse proxies.
- The server binds `127.0.0.1` by default. Changing `api_host` to a non-loopback
  address requires `avdfleet serve --allow-remote`, keeps bearer authentication enabled,
  and is not recommended: any caller who has the token has full control of the fleet.
  Remote serving with `auth_required = false` is refused.

See [security.md](security.md) for the full security model.

## Endpoint groups

### System

| Method + path | Purpose |
|---|---|
| `GET /health` | Liveness (no auth): `status`, `version`, `uptime_s`, `emulator_available`, `database` |
| `GET /api/v1/auth/check` | Validate the bearer token (`ok`) |
| `POST /api/v1/auth/ws-ticket` | Issue a single-use WebSocket ticket (`{"ticket", "expires_in"}`) |
| `GET /api/v1/system` | Host + runtime info (OS, arch, RAM, disk, hypervisor, edition, SDK/data dirs) |
| `GET /api/v1/config` | Effective configuration (no secrets) |
| `PATCH /api/v1/config` | Update a whitelisted subset (`headless`, `gpu_mode`, `max_instances`, `default_ram_mb`, `theme`, `log_level`) |
| `GET /api/v1/doctor?slow=` | Run doctor checks (`DoctorReport`) |
| `POST /api/v1/doctor/fix` | Apply a fix (`{"check_id": …}`) |
| `GET /api/v1/capacity` | RAM/ports/CPU capacity for starting more instances |
| `GET /api/v1/events?limit=` | Recent events (same envelope as the WS stream) |

### Images

| Method + path | Purpose |
|---|---|
| `GET /api/v1/images?include_remote=&installed_only=` | Catalogue + installed images |
| `POST /api/v1/images` | Import a custom image directory (`ImageImportRequest`) → `201` |
| `POST /api/v1/images/refresh` | Refresh the catalogue from the SDK |
| `GET /api/v1/images/{image_id}` | One image |
| `POST /api/v1/images/{image_id}/download` | Download/install (`{"dry_run": false}`) |

### OOBE

| Method + path | Purpose |
|---|---|
| `GET /api/v1/oobe/matrix` | OOBE matrix |
| `POST /api/v1/oobe/verify` | Boot a temporary AVD and verify (`{"image_id", "timeout_s", "keep_avd"}`) |
| `GET /api/v1/oobe/records/{image_id}` | One record |

### Templates and devices

| Method + path | Purpose |
|---|---|
| `GET /api/v1/templates` / `POST /api/v1/templates` | List / create |
| `GET/PUT/DELETE /api/v1/templates/{template_id}` | Read / update / delete |
| `GET /api/v1/devices` | Device profiles |

### Instances

| Method + path | Purpose |
|---|---|
| `GET /api/v1/instances` / `POST /api/v1/instances` | List / create (`InstanceCreateRequest`) |
| `POST /api/v1/instances/bulk` | Bulk deploy (`BulkDeployRequest`) |
| `GET/DELETE /api/v1/instances/{name}` | Read / delete (`?delete_files=`) |
| `POST /api/v1/instances/{name}/start` | Start (`{"proxy_id", "headless", "dry_run"}`) |
| `POST /api/v1/instances/{name}/stop` | Stop (`{"force", "timeout_s"}`) |
| `POST /api/v1/instances/{name}/restart` | Restart |
| `POST /api/v1/instances/{name}/wait-boot` | Wait for boot (`{"timeout_s"}`) |
| `POST /api/v1/instances/{name}/reset-oobe` | Reset to OOBE (`{"strategy", "verify"}`) |
| `GET /api/v1/instances/{name}/screenshot` | PNG screenshot |
| `PUT /api/v1/instances/{name}/config` | Typed `config.ini` edits (`{"values": {...}, "dry_run"}`) |
| `POST /api/v1/instances/{name}/clone` | Clone (`{"name"}`) |
| `GET/POST /api/v1/instances/{name}/snapshots` | List / save (`{"name", "golden"}`) |
| `POST /api/v1/instances/{name}/snapshots/{snapshot}/load` | Restore |
| `DELETE /api/v1/instances/{name}/snapshots/{snapshot}` | Delete |
| `POST /api/v1/instances/{name}/input` | Input (`InputRequest`: tap/swipe/text/key) |

### Proxies and assignments

| Method + path | Purpose |
|---|---|
| `GET /api/v1/proxies?include_disabled=` / `POST /api/v1/proxies` | List (masked) / create (`url` or fields) |
| `POST /api/v1/proxies/import` | Import lines (`{"text", "source"}`) |
| `POST /api/v1/proxies/check-all` | Health-check all |
| `GET/PATCH/DELETE /api/v1/proxies/{proxy_id}` | Read / update / delete |
| `POST /api/v1/proxies/{proxy_id}/check` | Health-check one |
| `POST /api/v1/proxies/verify-guest` | Guest egress evidence (`{"avd_name", "proxy_id", "token_url"}`) |
| `GET /api/v1/assignments` / `POST /api/v1/assignments` | List / assign (`{"avd_name", "proxy_id"}`) |
| `DELETE /api/v1/assignments/{avd_name}` | Unassign |
| `POST /api/v1/assignments/auto` | Auto-assign (`{"instances", "strategy"}`) |
| `GET /api/v1/bridges` | Active local bridges |

### Scripts and runs

| Method + path | Purpose |
|---|---|
| `GET/POST /api/v1/scripts` | List / create definitions |
| `GET/PUT/DELETE /api/v1/scripts/{script_id}` | Read / update / delete |
| `POST /api/v1/scripts/runs` | Start a run → `202` (`ScriptRunRequest`) |
| `GET /api/v1/runs?limit=` | List runs |
| `GET /api/v1/runs/{run_id}` | One run |
| `POST /api/v1/runs/{run_id}/cancel` | Cancel |

### Appium

| Method + path | Purpose |
|---|---|
| `GET /api/v1/appium` | Status (`running`, `pid`, `port`, `version`, `drivers`, `sessions`, `url`) |
| `POST /api/v1/appium/start` | Start (`{"port"}`) |
| `POST /api/v1/appium/stop` | Stop |
| `POST /api/v1/appium/capabilities` | UiAutomator2 capabilities (`{"avd_name"}`) |

### Root and instrumentation

| Method + path | Purpose |
|---|---|
| `GET /api/v1/instrumentation/capabilities?targets=` | Probe one or more AVDs (repeatable/comma-separated) |
| `POST /api/v1/instrumentation/scan` | Probe with `{"targets": [...]}` |
| `GET /api/v1/instrumentation/modules?kind=` | Module library (`kind` = `xposed_apk`, `magisk_zip`, `zygisk_zip`, `lsposed_zip`, `unknown`) |
| `POST /api/v1/instrumentation/modules/import` | Multipart `file`, or JSON `{"path"}` / `{"url"}` |
| `DELETE /api/v1/instrumentation/modules/{module_id}` | Remove a library record and its stored copy |
| `GET /api/v1/instrumentation/toolchain` | Pinned artifacts, local presence/integrity, provenance manifest |
| `POST /api/v1/instrumentation/toolchain/{key}/acquire` | Download/verify one pinned release (`{"confirm": true}`; `409` without it, `422` on a size/hash mismatch, `404` for an unknown key) |
| `GET /api/v1/instrumentation/instances/{name}/auto-profile` | Profile applied automatically when the AVD boots |
| `PUT /api/v1/instrumentation/instances/{name}/auto-profile` | Bind or clear that profile (`{"profile_id": "…"}`; empty clears) |
| `GET /api/v1/instrumentation/profiles` / `POST /api/v1/instrumentation/profiles` | List / create profiles |
| `PUT/DELETE /api/v1/instrumentation/profiles/{profile_id}` | Update / delete a user profile |
| `POST /api/v1/instrumentation/profiles/{profile_id}/clone` | Copy any profile into an editable one |
| `POST /api/v1/instrumentation/deployments` | Deploy (`{"profile_id", "targets", "label", "background"}`) |
| `GET /api/v1/instrumentation/deployments?limit=` | Recent deployments |
| `GET /api/v1/instrumentation/deployments/{run_id}` | One run with per-target results |
| `POST /api/v1/instrumentation/deployments/{run_id}/cancel` | Request cancellation |
| `GET /api/v1/instrumentation/deployments/{run_id}/logs?limit=` | Run log |

See [instrumentation.md](instrumentation.md) for the capability model, pinned
versions and the deployment semantics. Two details are easy to miss:

- A device snapshot's `xposed_modules[]` (read from LSPosed's own database)
  includes `scope` — the application packages the module targets — and is empty
  with an explanatory note when LSPosed, Magisk root or the device's `sqlite3` is
  missing.
- Profile steps accept every `StepAction`, including `complete_magisk_setup`
  (installs the acquired Magisk APK, drives its "Additional Setup" and waits for
  the app's own reboot — up to 300 s), and `enable_xposed_module` /
  `set_xposed_scope`, which write LSPosed's internal database on the device and
  require a reboot. The GUI profile editor offers all three; a profile created
  through this API can use the same params.
- LSPosed activation is checked with `pidof lspd`; the manager is not installed by
  `lspd` and must be installed from
  `/data/adb/modules/zygisk_lsposed/manager.apk` (see
  [instrumentation.md](instrumentation.md#verifying-lsposed-is-running)).

### Fleet, portable and events

| Method + path | Purpose |
|---|---|
| `POST /api/v1/fleet/plan` | Plan a fleet (`FleetRequest`: `spec`/`yaml`/`path`, `prune`, `dry_run`) |
| `POST /api/v1/fleet/apply` | Apply a fleet |
| `GET /api/v1/portable/status` | Portable status (edition, root, checks, leftovers) |
| `GET /api/v1/portable/checks` | Portable preflight checks |
| `GET /api/v1/events` | Recent events |

## Error envelope

Every failure (HTTP and WS error frames where applicable) uses:

```json
{
  "error": {
    "code": "not_found",
    "message": "instance 'avd-99' was not found",
    "hint": "list instances with GET /api/v1/instances",
    "details": {"name": "avd-99"}
  }
}
```

- `code` is a stable machine-readable string (`unauthorized`, `not_found`,
  `conflict`, `invalid_request`, `internal_error`, `service_unavailable`,
  `timeout`, …), `message` human-readable, `hint`/`details` optional.
- Status codes used: 400, 401, 403, 404, 409, 422, 500, 501, 503, 504.
- Bodies are redacted (`core.logging.redact`); stack traces are never returned.
  Unexpected failures return `internal_error` with the request id in
  `X-Request-ID` for log correlation.
- Security headers on all responses: `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
  `X-Permitted-Cross-Domain-Policies: none`; API responses add
  `Cache-Control: no-store`.

## Examples (curl)

```bash
BASE=http://127.0.0.1:8765
AUTH="Authorization: Bearer $TOKEN"

# Health (no auth) and system info
curl -s "$BASE/health"
curl -s -H "$AUTH" "$BASE/api/v1/system" | python -m json.tool

# Doctor and capacity
curl -s -H "$AUTH" "$BASE/api/v1/doctor?slow=true"
curl -s -H "$AUTH" "$BASE/api/v1/capacity"

# Images
curl -s -H "$AUTH" "$BASE/api/v1/images?installed_only=false"
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"dry_run": true}' \
  "$BASE/api/v1/images/system-images%3Bandroid-35%3Bgoogle_apis_playstore%3Bx86_64/download"

# Create a template and bulk-deploy five instances
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"id":"pixel-api35","name":"pixel-api35","image_id":"system-images;android-35;google_apis_playstore;x86_64","ram_mb":4096}' \
  "$BASE/api/v1/templates"
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"template_id":"pixel-api35","count":5,"prefix":"avd"}' \
  "$BASE/api/v1/instances/bulk"

# Proxy import + assignment + guest verification
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"text":"http://user:pass@127.0.0.1:8081"}' \
  "$BASE/api/v1/proxies/import"
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"avd_name":"avd-01","proxy_id":"<proxy-id>"}' \
  "$BASE/api/v1/assignments"
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"avd_name":"avd-01","proxy_id":"<proxy-id>","token_url":"https://example.com/token"}' \
  "$BASE/api/v1/proxies/verify-guest"

# Script fan-out with live results
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"script_id":"builtin.getprop-summary","instances":["avd-01","avd-02"],"concurrency":2}' \
  "$BASE/api/v1/scripts/runs"
curl -s -H "$AUTH" "$BASE/api/v1/runs/<run-id>"

# Fleet plan and apply
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"path":"fleet.yaml","prune":false,"dry_run":true}' "$BASE/api/v1/fleet/plan"
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"path":"fleet.yaml"}' "$BASE/api/v1/fleet/apply"
```

## WebSockets

Both endpoints prefer a **single-use ticket**. `POST /api/v1/auth/ws-ticket`
(bearer-authenticated, like every other `/api/v1` route) returns
`{"ticket": "<opaque>", "expires_in": 30}`; the client passes it as
`?ticket=<ticket>`. Tickets are high-entropy values that are never derived from
the token, are stored only hashed in daemon memory, are burned on the first
handshake (a replayed ticket is rejected with `4401`) and expire after at most
60 s. The ticket response is `Cache-Control: no-store`.

Native/CLI clients may instead send `Authorization: Bearer <token>`. The legacy
`?token=` query parameter is still accepted for older clients but is
**deprecated**: uvicorn logs the WebSocket request line (including the query
string) at INFO on `uvicorn.error` even with `access_log=False`, so a token in a
URL reaches the daemon's output and log file. As defence in depth, `avdfleet
serve` installs a redaction filter on uvicorn's `error`/`access` output that
masks `?token=` and `?ticket=` values.

When a `ticket` parameter is present it is authoritative: an invalid ticket is
never silently upgraded to the token path. A socket also requires an `Origin` in
`config.allow_origins` when an Origin header is present; a request without one is
treated as a non-browser client. A rejected handshake is observed as HTTP 403;
the documented close codes are:

| Code | Meaning |
|---|---|
| `4401` | Missing/invalid credentials — invalid, replayed or expired ticket, or bad token (rejected before `accept()`) |
| `4403` | Origin not allowed |
| `4400` | Malformed request parameters (reserved) |
| `4503` | Screen streaming unavailable (accepted, error frame then close) |
| `1011` | Unexpected internal error while streaming |
| `1000` | Normal close (client disconnect, stream ended, shutdown) |

### `/ws/events`

Every server frame is the `Event` envelope from
[contracts/events.schema.json](../contracts/events.schema.json):

```json
{"id":"evt-…","type":"instance.state","ts":"2026-11-26T12:00:00Z","source":"avdfleet","seq":42,
 "payload":{"instance":"avd-01","state":"running"}}
```

Event types: `instance.created`, `instance.deleted`, `instance.state`,
`instance.boot`, `instance.log`, `instance.screen`, `proxy.created`,
`proxy.updated`, `proxy.checked`, `script.run.started`, `script.run.output`,
`script.run.finished`, `install.step`, `install.finished`, `oobe.result`,
`doctor.finished`, `fleet.applied`, `system`, `error`, `log`.

After accept the server sends a `system` event announcing the connection, then up
to 50 recent history events, then live events. A `system` event with
`payload.ping == true` is sent every 25 s to defeat proxy idle timeouts. Client
text frames are ignored (broadcast channel).

```javascript
// Browsers cannot set WebSocket headers: fetch a fresh single-use ticket first.
const issued = await fetch("/api/v1/auth/ws-ticket", {
  method: "POST",
  headers: { Authorization: `Bearer ${token}` },
});
const { ticket } = await issued.json();
const ws = new WebSocket(`ws://127.0.0.1:8765/ws/events?ticket=${ticket}`);
ws.onmessage = (event) => {
  const evt = JSON.parse(event.data);
  if (evt.type === "script.run.output") console.log(evt.payload.instance, evt.payload.line);
};
```

Every reconnect needs a fresh ticket: a ticket is burned by the handshake that
uses it.

### `/ws/instances/{name}/screen`

Frames are raw PNG/JPEG binary by default; with `?encoding=base64` each JSON
frame is `{"type":"frame","seq":N,"mime":"image/png","data":"<base64>"}`. The
first frame is `{"type":"hello","instance":name,"encoding":...}`; fatal errors
are `{"type":"error","code":...,"message":...}` immediately before the close
code. Frames never include shell credentials. See [scripting.md](scripting.md#screen-and-grpc)
for the gRPC → adb fallback chain.

## Browser auto-login (internal)

`avdfleet gui` avoids a manual token paste without weakening the normal bearer-token
model. These endpoints are intentionally not part of the frozen OpenAPI contract:

- `POST /api/v1/auth/bootstrap-code` (requires bearer auth) returns
  `{"code": "...", "expires_in": 120}`. The code is 256-bit, single-use, short-lived and
  stored only hashed in daemon memory.
- `GET /auth/bootstrap` serves a tiny same-origin page. The code is passed in the URL
  **fragment** (`/auth/bootstrap#code=...`), so it is not sent to the server, logged or
  included in the Referer.
- `POST /api/v1/auth/bootstrap` (no bearer) exchanges a valid code for
  `{"token": "..."}`. The page stores it in `localStorage["avdfleet.token"]`, clears the
  URL fragment, and redirects to `/`. Invalid, expired and already-used codes return 401.
- Auto-login is restricted to loopback daemons; `avdfleet gui` falls back to the Connect
  screen for remote hosts or with `--no-auto-auth`.

## OpenAPI and client generation

- Contract: [contracts/openapi.yaml](../contracts/openapi.yaml)
- Live document: `GET /openapi.json` (when docs are enabled, the default);
  interactive UI at `/docs`; disable both with `AVDFLEET_DOCS=0`.
- The contract is exercised by `tests/contract/test_openapi_static.py` and the
  daemon routes by `tests/api/`; the React client in `ui/src/lib/api.ts` is
  written against the same contract.
