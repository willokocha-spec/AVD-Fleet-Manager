# Scripting, Appium and screen

AVD Fleet runs automation across many instances in parallel, with live output,
per-instance results, cancellation and timeouts. It also drives Appium sessions
and streams/displays emulator screens.

## Script types

| Type | Runs where | Typical content |
|---|---|---|
| `adb_shell` | Inside the guest via `adb -s <serial> shell …` | Shell commands (inline or a remote script path) |
| `inline_shell` | Guest shell, body supplied inline | Short ad-hoc commands |
| `host_python` | On the host, in a temporary run directory | Python that uses `$AVDFLEET_SERIAL`/environment to target the instance |
| `appium_python` | Host Python with an Appium/W3C session created for the target | Python + `Appium-Python-Client` (Appium 4 compatible) |
| `appium_js` | Host Node/Appium client | **ESM** JavaScript (`.mjs` / `"type": "module"`); Appium 4 removed CommonJS |

A script definition has `id`, `name`, `type`, `content` (or `path`), optional
`description`, `timeout_s`, per-run `env`, default `targets`, `builtin` flag and
`tags`.

Built-in library entries (read-only, always available): `builtin.getprop-summary`,
`builtin.list-packages`, `builtin.oobe-activity`, `builtin.screenshot-pull`,
`builtin.ping-proxy-token`.

## Creating and running scripts

```bash
avdfleet scripts list
avdfleet scripts create --name health --type adb_shell --content "getprop sys.boot_completed"
avdfleet scripts create --name ui --type appium_python --file ./ui_check.py
avdfleet scripts create --name job --type appium_js --file ./job.mjs

# Run across instances (synchronous, streams output, prints the combined result):
avdfleet scripts run health --instances avd-01,avd-02 --instances avd-03
avdfleet scripts run health --concurrency 10 --timeout 120
avdfleet scripts run health --detach          # background; returns the run id
avdfleet scripts run health --dry-run         # validate targets, run nothing

avdfleet scripts runs list
avdfleet scripts runs status <run-id>
avdfleet scripts runs cancel <run-id>
avdfleet scripts delete <script-id>
```

`--instances` is repeatable/comma-separated; omitting it targets every deployed
instance (the script's default targets are used when defined). `--concurrency`
defaults to 5 and is capped at 64. `--timeout` overrides the per-instance
timeout.

### Fan-out, results, cancel and timeout

The executor:

- resolves targets to adb serials, plans one command set per target and runs
  them with a bounded worker pool (`concurrency`);
- streams stdout/stderr per instance as it arrives, persists runs in SQLite and
  publishes `script.run.started` / `script.run.output` / `script.run.finished`
  events on `/ws/events` (the GUI Log viewer consumes them);
- aggregates per-instance `{state, exit_code, stdout, stderr, started_at,
  finished_at, duration_s, error}` plus the overall run state
  (`queued|running|succeeded|failed|cancelled|timed_out`);
- supports **cancel** (`avdfleet scripts runs cancel`, API
  `POST /api/v1/runs/{run_id}/cancel`, or the GUI) which terminates running
  per-instance processes and marks unfinished targets `cancelled`;
- enforces a **per-instance timeout** (`timed_out` state, process killed) and a
  run-level timeout when supplied.

Run output and persisted errors are redacted before storage/streaming: proxy
credentials and tokens never appear in run records. Lifecycle hooks
(`before_run`/`after_run`) are available to embedded users of the script
service; script definitions can be used to model the deploy/boot/reset steps
from the brief. (The brief's dedicated deploy/boot/reset hook bus is not a
separate feature in v0.1.)

### API

```http
POST /api/v1/scripts            # create/update a definition
GET  /api/v1/scripts
POST /api/v1/scripts/runs       # {"script_id", "instances", "concurrency", "timeout_s", "env", "dry_run"} -> 202
GET  /api/v1/runs?limit=50
GET  /api/v1/runs/{run_id}
POST /api/v1/runs/{run_id}/cancel
```

See [api.md](api.md) for the full endpoint list and auth.

## Appium

AVD Fleet manages one Appium 4 server (default loopback `127.0.0.1:4723`) and
builds UiAutomator2 capabilities with centrally allocated ports, then cleans up
adb forwards after sessions.

```bash
avdfleet appium status
avdfleet appium start --port 4723
avdfleet appium stop
avdfleet appium capabilities avd-01      # JSON UiAutomator2 capabilities
avdfleet appium cleanup avd-01           # remove forwards/session state
```

Server behaviour:

- launched as `appium --address 127.0.0.1 --port <p> --base-path /wd/hub
  --log-level info`; AVDFleet passes `--base-path /wd/hub` explicitly for client
  compatibility (Appium 4's own default is `/`).
- `APPIUM_HOME` is redirected under the avdfleet data directory so extensions
  do not pollute `~/.appium`; the portable edition sets it inside the bundle.
- no Appium authentication exists — the server is loopback-only by design.
  Insecure features stay off (`--allow-insecure` is not used in fleet mode;
  Appium Inspector + CORS is an explicit, optional operator tool).

Capabilities include `platformName: Android`,
`appium:automationName: UiAutomator2`, `appium:deviceName`,
`appium:udid: emulator-<console>`, unique `appium:systemPort` and
`appium:mjpegServerPort`, `noReset`, `autoGrantPermissions` and generous
server-install/launch timeouts. `appium:avd` is only used when Appium is allowed
to launch the AVD (normally AVD Fleet starts it and passes `udid`).

Parallel sessions need unique `udid`, `systemPort`, `mjpegServerPort` and (for
webviews) `chromedriverPort`; AVD Fleet allocates these centrally from
`core.ports` and guarantees `systemPort != mjpegServerPort`. Session creation
fails if a requested port is already forwarded, so `avdfleet appium cleanup`
(or a fresh session with unique ports) is the recovery path after an interrupted
run. Do not run `adb kill-server` while a fleet is live.

Appium 4 facts (research R5): Node `^22.22.2 || ^24.15.0 || >=26`, npm ≥ 10,
ESM-only, driver `appium-uiautomator2-driver` (10.4.x current), package
`appium` 4.0.1 current. The installer records the server and driver versions in
the install manifest; run results should be interpreted together with those
versions.

## Screen and gRPC

`avdfleet screen capture <instance> [-o out.png]` captures one PNG;
`tap`, `swipe`, `text` and `key` inject input. The GUI Fleet/Instance pages
show a live screen preview over WebSocket.

Fallback chain:

1. **Emulator gRPC** (`EmulatorController`/screen services) when an endpoint and
   transport are usable — the client discovers the emulator's advertisement file
   (`.../avd/running/pid_<pid>.ini` under the temp/Android user/runtime dirs),
   reads `grpc.port`/`grpc.address` and uses `authorization: Bearer <grpc.token>`
   when present. Discovery dirs are configurable; the repository's fakes write a
   synthetic `<fake_home>/grpc/<console>.ini` layout that is *not* the real one.
2. **`adb exec-out screencap -p`** (then `adb shell screencap -p`) when gRPC is
   unavailable.
3. A cached last frame for display when capture fails; `scrcpy` is available as
   a local-display fallback but is not required for the GUI.

Input is injected through gRPC when available, otherwise via `adb shell input`.
gRPC stays loopback-only; its bearer token is treated as a secret and never
logged or returned. When gRPC is unavailable the product degrades cleanly to the
adb path (no hard failure for screenshot/input).

Streaming over WebSocket:

- `/ws/instances/{name}/screen` — raw PNG/JPEG frames by default;
  `?encoding=base64` returns `{"type":"frame","seq":N,"mime":"image/png","data":...}`.
  A `hello` frame comes first; errors are `{"type":"error",...}` before close
  code `4503` (screen unavailable) or `1011` (internal).
- `/ws/events` carries run output, state changes and other events (see
  [api.md](api.md)).

## Scripting through a proxy

A script run inherits the instance's proxy isolation: the emulator is already
configured to use its assigned bridge, so guest HTTP(S) traffic issued by an
adb-shell/host-Python script is monitored by the assigned proxy at the TCP
layer. Verification uses a tokenized URL:

```bash
avdfleet scripts run builtin.ping-proxy-token --instances avd-01 --env TOKEN_URL=https://example.com/token
avdfleet proxies verify <proxy-id> --guest avd-01 --token-url https://example.com/token
```

DNS/UDP are **not** proxied — see [proxy.md](proxy.md#dns-and-udp-explicit-limitations).
