# R5 — Appium 4 on Windows / macOS / Linux (research)

Status: research only. Nothing was installed and no real Appium session was created on this
host (no hypervisor; no emulator boots). Everything below is from official documentation and
the published npm package metadata, accessed **2026-11-26**. Labels:

- **[VERIFIED]** — stated explicitly in the cited official docs or npm registry metadata.
- **[PARTIAL]** — documented for one variant/version, not exhaustively.
- **[INFERRED]** — reasoned from docs; confirm on a real host.
- **[UNVERIFIED]** — no primary source found; do not build acceptance criteria on it.

Current versions at access date: **Appium 4.0.1** (npm `appium`), official Android driver
**appium-uiautomator2-driver 10.4.0** (peer dependency `appium: ^4.0.0-beta.8`), Appium
Inspector 2026.11.x (desktop + plugin). R1 owns the full version/URL table; this file owns
behaviour, ports, capabilities and security.

## 1. One-paragraph answer

Appium 4 is a Node.js (ESM-only) server installed globally with `npm install -g appium`
(Node `^22.22.2 || ^24.15.0 || >=26.0.0`, npm `>=10`), listening by default on
`0.0.0.0:4723` with base path `/`. Android automation needs the separately installed
`uiautomator2` driver (`appium driver install uiautomator2`, npm package
`appium-uiautomator2-driver`) plus Android SDK/platform-tools and a JDK. Parallel sessions
must pin **unique `udid`/`avd` + `systemPort` + `mjpegServerPort` + `chromedriverPort`**;
the driver creates adb forwards for those ports and refuses to replace existing ones, so
avdfleet must allocate them centrally (core `PortAllocator`) and clean stale forwards after
unknown failures. Appium Inspector is either a standalone Electron app or an Appium plugin
(`appium plugin install inspector`, served at `/inspector`); either way the Appium server has
no authentication of its own, so bind loopback and never expose it.

---

## 2. Versions, package IDs, install methods

### 2.1 Verified package/version metadata [VERIFIED]

| Item | Value |
|---|---|
| Appium server npm package | `appium` — latest **4.0.1** (registry `appium/latest`) |
| Node requirement | `^22.22.2 || ^24.15.0 || >=26.0.0` (LTS recommended) — same `engines` in npm metadata |
| npm requirement | `>=10` |
| Module format | ESM-only (`"type": "module"`; Appium 4 removed the CommonJS build) |
| UiAutomator2 driver npm package | `appium-uiautomator2-driver` — latest **10.4.0**, peer `appium ^4.0.0-beta.8` |
| Driver identity | short name `uiautomator2`, `automationName: UiAutomator2`, `platformNames: ["Android"]` |
| Appium home dir | `APPIUM_HOME` env var, default `~/.appium` (extension install root) |
| Server URL default | `http://127.0.0.1:4723/` (log line from quickstart); Inspector default target `http://127.0.0.1:4723` |

### 2.2 Install methods [VERIFIED]

- **Supported:** `npm install -g appium` (only npm; "Other package managers are not currently
  supported"). Upgrade is the same command on top of an existing install; Appium 4 stays
  installable the Appium-3 way (`npm install -g appium`).
- Appium is **not** shipped as MSI/deb/rpm/brew formula by the Appium team; the installer
  (I3) must install Node/npm first, then `npm install -g appium@<pinned>`, then
  `appium driver install uiautomator2[@<pinned>]`.
- The driver install uses npm under the hood; **stop the Appium server before installing or
  updating drivers/plugins** (docs tell you to).
- Suggested deterministic installation for avdfleet (pin both):
  `npm install -g appium@4.0.1` +
  `appium driver install uiautomator2@10.4.0` (short-name + version modifier is the
  documented install-spec form), then `appium driver doctor uiautomator2` (expect
  "0 required fixes needed" or only optional fixes).
- Verification commands: `appium --version`, `appium driver list --installed` (or `--json`),
  `appium driver doctor uiautomator2`, `appium --show-build-info`, `appium --show-config`,
  `appium --show-debug-info` (last three are base-command info options that do not launch the
  server). The Appium `/status` endpoint returns `{ready, build.version}`.
- `appium setup` batch-installs the mobile drivers (UiAutomator2, XCUITest only on macOS,
  Espresso) — avoid in a minimal fleet install; install exactly uiautomator2.
- Driver packaging pins bundled dependencies (npm metadata shows `appium-adb`,
  `appium-android-driver`, `appium-uiautomator2-server`, `appium-uiautomator2-server`,
  `io.appium.settings`, etc.); a driver version bump therefore also bumps the on-device server
  APKs — record `appium --version` + driver version per run.
- **Android prerequisites for the driver** (from UiAutomator2 quickstart): Android SDK
  Platform + Platform-Tools, `ANDROID_HOME` pointing at the SDK, a Java JDK with `JAVA_HOME`
  set (JDK 9+ for recent API levels, JDK 8 otherwise per the doc), and a device visible in
  `adb devices` (emulator booted first; Appium will not boot one unless `appium:avd` is used).
- **Portable edition** (INFERRED, no upstream doc for portable npm): global npm installs are
  not relocatable, so bundle Node inside the bundle root, set `npm_config_prefix`/
  `PATH` to the bundle, set **`APPIUM_HOME` inside the bundle**, and pre-install
  `appium` + `uiautomator2` there so the bundle works offline, survives moves and writes
  nothing to `%APPDATA%`/`~/.appium`. Verify with `appium driver list --installed --json` after
  relocation.

### 2.3 Appium 4 breaking changes relevant to the installer/product [VERIFIED]

From the official "Migrating to Appium 3 to 4" guide:

- Node 20 dropped; Node 22.22+/24.15+/26+ required.
- ESM-only — any JS tooling that `require()`s Appium packages must switch to `import()`.
- JSONWP/MJSONWP internals removed (most remaining legacy endpoints remain temporarily, but
  drivers relying on JSONWP are incompatible — Windows driver was replaced by **WinCore**;
  irrelevant for Android but relevant to the "Appium 4" install step and any optional
  Windows-desktop automation).
- Default log level is `info`; CLI output streams split.
- `--allow-cors` was renamed (CORS is now an **insecure feature**: Inspector docs launch with
  `--allow-insecure='*:cors'`).
- SPDY/HTTP2 server support removed; Selenium Grid 3 support removed.
- Post-install script removed; unused server arguments removed; event-timings capability
  removed; some `/session/:id` behavioural fixes. Appium 4 is stricter W3C.
- Practical impact for avdfleet: scripts must use a **W3C Appium client** whose version
  supports Appium 4 (e.g. current `Appium-Python-Client`); older Appium clients that relied
  on `GET /session/:id` (removed) will break. The exact compatible Python client version is
  outside this research (**[UNVERIFIED]** — R1 should pin it with the Appium version).

---

## 3. Server defaults, flags and environment

### 3.1 `appium server` defaults [VERIFIED from CLI reference]

| Argument | Default | Note for avdfleet |
|---|---|---|
| `--address`, `-a` | `0.0.0.0` | **Bind `127.0.0.1` explicitly** for the fleet; 0.0.0.0 has no auth and is a remote-control surface |
| `--port`, `-p` | `4723` | one port per server process (M1 allocates) |
| `--base-path`, `-pa` | `""` (root) | `/wd/hub` is **not** the default (historical); pick one explicitly and use it in clients |
| `--log-level` | `info` | use `--log` file + `--log-format json` for machine parsing if needed |
| `--allow-insecure` | `[]` | per-driver insecure features, prefix `automationName:feature` (e.g. `uiautomator2:adb_shell`) |
| `--deny-insecure` | `[]` | overrides allow/relaxed |
| `--relaxed-security` | `false` | enables **all** insecure features — never in the product |
| `--session-override` | `false` | clobbers existing sessions on new-session request; useful only for single-session mode |
| `--use-drivers` / `--use-plugins` | all installed / none | plugins must be activated explicitly (`--use-plugins=inspector`) |
| `--keep-alive-timeout` | `600` s | |
| `--request-timeout` | `3600` s | |
| `--shutdown-timeout` | `5000` ms | |
| `--default-capabilities`, `-dc` | none | server-side defaults, overridden by client `alwaysMatch`/`firstMatch` |
| `--ssl-cert-path` + `--ssl-key-path` | none | TLS for the Appium API (loopback fleets don't need it) |
| `--app-url-rules` | none | restrict remote `appium:app` URL downloads (see §6) |
| `--config` | none | JSON config file; CLI overrides |

Command form: `appium` or `appium server`; all options can also come from a config file or
(partially) env vars.

### 3.2 Environment variables [VERIFIED]

`APPIUM_HOME` (extension install dir; default `~/.appium`), `APPIUM_TMP_DIR` (same as
`--tmp`), `APPIUM_APPS_CACHE_MAX_AGE` (default 24h), `APPIUM_APPS_CACHE_MAX_ITEMS`
(1024), `APPIUM_APPS_CACHE_IGNORE_URL_QUERY`, `APPIUM_OMIT_PEER_DEPS`,
`APPIUM_RELOAD_EXTENSIONS`, plus plugin-specific (`APPIUM_STORAGE_*`).
Note: there are **no `APPIUM_PORT`/`APPIUM_BASE_PATH` env vars** in the documented list —
always pass `--port`/`--base-path` on the command line (or a config file).

### 3.3 Example fleet-safe launch [INFERRED, composed of verified flags]

```
appium server --address 127.0.0.1 --port <4723+i> --base-path / \
  --log-level info --log <state>/appium-<i>.log \
  --use-drivers=uiautomator2
```

One server process can serve **many sessions in parallel** (docs: multiple sessions on one
server "uses less resources and ensures better control"), but one **device only in one
session at a time**. avdfleet may choose **one Appium server per instance** (simplest
port/state isolation, 64 processes) or **one shared server** (needs unique per-session
system/MJPEG/chromedriver ports; sessions are isolated by the driver anyway). M1 should keep
this configurable; the API model (`AppiumStatus`) suggests a single managed server is the
expected default.

---

## 4. UiAutomator2 driver: capabilities that matter

All names below are `appium:`-prefixed in W3C `alwaysMatch`; defaults are the driver's
documented defaults. Sources: UiAutomator2 capabilities reference + parallel guide + MJPEG
guide (accessed 2026-11-26). `[VERIFIED]` for the whole table unless noted.

### 4.1 Standard / identity

| Capability | Default | Behaviour / fleet note |
|---|---|---|
| `platformName` | — | set `"Android"`; driver is lenient if `automationName` is present |
| `appium:automationName` | — | must be `UiAutomator2` |
| `appium:deviceName` | — | **not used for device selection** (informational); do not rely on it for routing |
| `appium:udid` | — | adb serial, e.g. `emulator-5554`; **"Always set this capability when running parallel tests."** |
| `appium:avd` | — | emulator AVD name; if not running, the **driver will launch it**; use only when avdfleet wants Appium to own launch (normally N1 launches, then set `udid`) |
| `appium:avdArgs` / `avdEnv` / `isHeadless` / `avdLaunchTimeout` (60000) / `avdReadyTimeout` (60000) / `allowDelayAdb` (`true`, maps `-delay-adb`) / `networkSpeed` (maps `-netspeed`) | — | only applied if the driver launches the AVD; **avdfleet should not let Appium boot emulators** except in a fallback mode |

### 4.2 Ports and ADB

| Capability | Default | Behaviour / fleet note |
|---|---|---|
| `appium:systemPort` | `8200`; if omitted first free in `[8200, 8299]` | **host** port forwarded to the on-device UiAutomator2 server (device port configurable via `serverPort` setting). Unique per session; explicit value must differ from `mjpegServerPort`; **session creation fails if the requested port is already forwarded** |
| `appium:mjpegServerPort` | not set | host port forwarded to the device MJPEG server (device default **7810**); if omitted **no MJPEG forward is created**; unique per session; session fails if already forwarded |
| `appium:mjpegScreenshotUrl` | not set | when set, screenshots come from the MJPEG stream |
| `appium:chromedriverPort` | random free port | unique per session when in webview/hybrid mode; `chromedriverPorts` accepts `[5600, 5610, [5650, 5660]]` ranges |
| `appium:webviewDevtoolsPort` | `10900` (fallback range 10900–11000) | unique when webviews are used |
| `appium:chromedriverExecutable` | — | absolute host path to a ChromeDriver binary; takes priority over `chromedriverExecutableDir`; **must pass checksum verification** unless `chromedriver_allow_any_executable` insecure feature is enabled (env-specific pinning for hybrid apps) |
| `appium:chromedriverExecutableDir` | `<driver>/node_modules/appium-chromedriver/chromedriver/<os>` | directory of binaries; downloaded binaries live here too |
| `appium:adbPort` | `5037` | `-P` of adb; use only if running per-instance adb servers |
| `appium:remoteAdbHost` | `localhost` | `-H` of adb |
| `appium:adbExecTimeout` | `20000` | per-adb-command timeout |
| `appium:suppressKillServer` | `false` | prevents driver from ever killing the adb server (important on shared multi-instance hosts) |
| `appium:allowOfflineDevices` | `false` | |
| `appium:adbListenAllNetwork` | `false` | insecure feature `adb_listen_all_network` required; never enable in product |
| `appium:buildToolsVersion` | latest installed | pin if multiple build-tools present |

### 4.3 Session/app state behaviour

| Capability | Default | Behaviour / fleet note |
|---|---|---|
| `appium:noReset` | not set | do **not** stop/clear app data before session; mutually exclusive with `fullReset` |
| `appium:fullReset` | `false` | reinstall app before + uninstall after session (destroys state) |
| `appium:autoGrantPermissions` | not set | grants all runtime permissions at session start; for `targetSdk < 23` / API ≤ 22 needs reinstall (`fullReset`) |
| `appium:autoLaunch` | `true` | launch app on session start |
| `appium:forceAppLaunch` | — | relaunch even with `noReset` |
| `appium:dontStopAppOnReset` | — | don't kill app on session start/delete |
| `appium:shouldTerminateApp` | — | stop app on session delete even with `noReset` |
| `appium:appWaitPackage` / `appWaitActivity` / `appWaitDuration` (20000) | app-derived / 20000 | startup waits |
| `appium:androidInstallTimeout` | `90000` | |
| `appium:uiautomator2ServerInstallTimeout` | `20000` | on-device server APK install timeout; raise for slow hosts |
| `appium:uiautomator2ServerLaunchTimeout` | `30000` | |
| `appium:uiautomator2ServerReadTimeout` | `240000` | |
| `appium:skipServerInstallation` | `false` | skip on-device server install if preinstalled (fast path for golden snapshots) |
| `appium:skipDeviceInitialization` | `false` | skip device init checks (settings app install, permissions…) |
| `appium:skipLogcatCapture` | `false` | skip logcat collection (throughput) |
| `appium:disableWindowAnimation` | `true` | good for flake reduction; restored after session |
| `appium:mockLocationApp` | `io.appium.settings` | |
| `appium:userProfile`, `otherApps`, `uninstallOtherPackages`, `allowTestPackages`, `enforceAppInstall`, `remoteAppsCacheLimit` (10) | — | app-management edge cases |

Driver-installed device packages to know for reset/cleanup: **`io.appium.settings`**,
**`io.appium.uiautomator2.server`**, **`io.appium.uiautomator2.server.test`**. `fullReset`
does not uninstall these; OOBE/wipe handling should expect them to disappear on `-wipe-data`
and need reinstall on the next session (the driver installs them by default; keep
`skipServerInstallation=false` unless the image/golden snapshot preinstalls them).

### 4.4 Capability-building recommendation (M1 `build_capabilities`)

Per `AvdInfo`:
`{"platformName": "Android", "appium:automationName": "UiAutomator2",
  "appium:deviceName": <template/device name>, "appium:udid": <adb serial>,
  "appium:systemPort": <ports.system_port>, "appium:mjpegServerPort": <ports.mjpeg>,
  "appium:noReset": true, "appium:autoGrantPermissions": true,
  "appium:newCommandTimeout": <run timeout>,
  "appium:uiautomator2ServerInstallTimeout": <larger for cold images>,
  ["appium:chromedriverExecutable": "<bundle path>" when hybrid/webview is requested]}`
Add `"appium:avd"` only in the fallback mode where Appium is allowed to launch the AVD, and
then also pass `avdArgs`/`isHeadless` deterministically (or assert the AVD is already running
and use `udid` instead).

---

## 5. Parallel-session port plan

### 5.1 Documented rules [VERIFIED]

- Parallelism has two supported shapes: **multiple Appium servers with one session each**, or
  **one server with multiple sessions**; "a single device under test should only be used in
  one session at a time".
- Must be unique among all parallel sessions: **`udid` (real device) or `avd` (emulator)**,
  **`systemPort`**, **`chromedriverPort`** (if hybrid/webview), **`mjpegServerPort`** (if
  MJPEG streaming). The parallel guide lists exactly these.
- If parallelism is **not** needed, the docs recommend `--session-override` so a new session
  deletes existing ones (single-session managed server mode).
- Collisions are hard failures: "Session creation fails if the requested port is already
  forwarded", and an explicit `systemPort` must differ from an explicit `mjpegServerPort`
  (and an explicit `mjpegServerPort` from `systemPort`).

### 5.2 Suggested 64-instance allocation (INFERRED; wire into `core/ports.py`)

| Resource | Suggested block | Notes |
|---|---|---|
| Appium server port | `4723 + i` (i=0..63 → 4723–4786) | only if one server per instance; shared-server mode uses one port |
| `appium:systemPort` | `8200 + i` (8200–8263) | inside driver fallback range 8200–8299; must be unique + free on host |
| `appium:mjpegServerPort` | e.g. `9300 + i` (9300–9363) | any free host port; **must differ from systemPort**; device side stays 7810 |
| `appium:chromedriverPort` | e.g. `9515 + i` (9515–9578) for hybrid tests; otherwise leave random | only relevant to hybrid/webview sessions |
| `appium:webviewDevtoolsPort` | `10900 + i` (10900–10963) for hybrid tests | driver fallback range is 10900–11000 |
| adb server | shared 5037 (`appium:adbPort`) | one adb server for the fleet; do not let Appium kill it (`suppressKillServer`) |

Keep explicit values deterministic (do not rely on the driver's "first free in range" or on
random chromedriver ports): the driver fails if the port is already forwarded, and M1 needs
predictable numbers for cleanup and for the instance `ports` API model. Reconcile these blocks
with `core/ports.py` (currently uses `appium_start+301/601` for system/MJPEG; the invariant to
keep is uniqueness + non-overlap + differs-from-each-other, not the exact numbers).

### 5.3 UiAutomator2 forward mechanics [VERIFIED]

- The driver forwards `systemPort` to the on-device UiAutomator2 server (device `serverPort`
  setting, default is the driver's server port) using adb.
- MJPEG (docs): the driver reserves the host port with
  **`adb forward --no-rebind tcp:<port> tcp:7810`**, i.e. it refuses to rebind. The device
  MJPEG server starts on every session at device port **7810** (setting `mjpegServerPort` on
  device, 1024–65535); device/server settings: framerate 10, scaling 50%, quality 50,
  bilinear off.
- Because `--no-rebind` is used, **any stale forward blocks the next session** with the same
  requested port.
- If `appium:mjpegServerPort` is omitted, no MJPEG host forward exists — for avdfleet's screen
  features use the emulator gRPC path (R4/V1) or set the MJPEG port deliberately.

---

## 6. Server security notes (must reach the product)

[VERIFIED from the Appium Server Security guide + CLI reference]

- Appium's security model assumes: your own server, local or protected internal network, not
  shared with untrusted parties, **not exposed to the wider internet**. There is **no
  authentication layer** on the Appium HTTP API; loopback binding is the control.
- **All insecure features are disabled by default**; only the server host can enable them;
  client sessions cannot. Features are named per driver/plugin and must be prefixed with the
  driver's `automationName` (or `*` wildcard), e.g. `--allow-insecure=uiautomator2:adb_shell`,
  `*:cors`. `--relaxed-security` enables everything (do not use).
- `--app-url-rules` restricts remote `appium:app` URL downloads (`allow`/`deny` host/CIDR
  lists, `httpsOnly`, `allowCredentials` default true, `maxRedirects` default 21). If the host
  downloads through `HTTP_PROXY`/`HTTPS_PROXY`, IP/subnet rules cannot be enforced for
  proxied downloads — relevant because the fleet host may itself use a corporate proxy.
- Avoid `--allow-insecure='*:cors'` unless the Inspector plugin is actually in use; scope it
  to the plugin/session and keep the server loopback-only.
- The server logs sessions/capabilities — treat `appium:app` URLs with credentials as secrets;
  redact like proxy credentials (R-PROXY).
- Appium 4 rejects state-changing requests from web pages unless CORS is enabled, including
  the Inspector plugin's own page (see §7). This is a deliberate protection; enabling CORS for
  the Inspector effectively allows a browser page to drive the server, so only enable it on a
  loopback-bound server.

---

## 7. Appium Inspector: setup and security

### 7.1 Two distribution formats [VERIFIED]

1. **Standalone desktop app** (Electron):
   - Windows 10+, macOS 13+, Ubuntu 18.04+/Debian 10+/openSUSE 15.5+/Fedora 39+; ~500 MB free.
   - Downloads from GitHub releases (`appium/appium-inspector`): Windows `.exe`/`.zip`
     (x64/arm64; `.exe` supports update checking), macOS `.dmg`/`.zip`, Linux `.AppImage`/`.tar.gz`
     (`chmod a+x` needed); `winget install AppiumDevelopers.AppiumInspector` is
     community-supported (not maintained by the Appium team).
   - Inspector app versions and server versions are decoupled: for Appium 4 target servers use
     Inspector **>= 2026.11.5**; it opens against `http://127.0.0.1:4723` by default and can
     connect to remote/cloud servers (Sauce Labs, BrowserStack, etc. integrations built in).
2. **Appium server plugin** (`appium-inspector-plugin`, short name **`inspector`**):
   - Requires Appium + Node `^20.19.0 || ^22.12.0 || >=24.0.0` (Appium 4 itself needs Node
     22.22+, so the Appium-4 requirement dominates), a 2022+ browser, ~9 MB.
   - Install: `appium plugin install inspector`;
     launch: `appium --use-plugins=inspector --allow-insecure='*:cors'`
     (Appium 4 needs the CORS insecure feature or the plugin UI cannot create sessions);
     open `http://127.0.0.1:4723/inspector`.
   - Compatibility: **Appium 4 needs plugin version >= 2026.11.6**; Appium 3 >= 2025.3.1;
     Appium 2 last compatible 2025.7.3 (installed via `--source=npm appium-inspector-plugin@2025.7.3`).
   - The plugin always serves `/inspector` regardless of the server base path; host/port must
     match the Appium server.
   - Recommended driver versions for the latest Inspector: UiAutomator2 **>= 6.6.0** (we ship
     10.4.x, fine).

### 7.2 Security notes for the Inspector [VERIFIED + INFERRED]

- It is a **GUI/client**, not a sandbox: it sends arbitrary capabilities/commands to whatever
  Appium server it connects to. Treat an Inspector download as code execution on the operator
  workstation (desktop app) or as a browser page attached to the Appium server (plugin).
- Plugin adds a web UI to the server on `/inspector`; combined with
  `--allow-insecure='*:cors'` any page the browser visits on that origin can create sessions.
  Therefore: bind `127.0.0.1`, keep the browser session local, do not leave the plugin enabled
  in unattended production, and never expose the port. avdfleet should offer Inspector setup
  as an optional operator tool (installer step flag / portable optional component), disabled
  by default in fleet mode.
- The formerly hosted web app `https://inspector.appiumpro.com` is **no longer associated**
  with the Appium team — do not point operators at it. **[VERIFIED README note]**
- Desktop app connects to remote servers via URL + capabilities; cloud integrations mean it
  may send app/capability data to third parties — document that operators should not paste
  credentials into it.

---

## 8. Session cleanup and adb-forward behaviour (M1-critical)

### 8.1 What the driver does [VERIFIED / PARTIAL]

- A session is a W3C session on the Appium server; deletion is `DELETE /session/<id>` (the
  normal client `.quit()`), after which the driver tears down its UiAutomator2 server
  connection and its adb forwards, and may stop the app depending on `noReset`/
  `dontStopAppOnReset`/`shouldTerminateApp`.
- **Forwards are the fragile part.** The MJPEG guide's "Recovering a Stale Forward" states:
  "The driver refuses to replace an existing forward. If a session ends without releasing its
  forward, the next session explicitly requesting that `appium:systemPort` or
  `appium:mjpegServerPort` will fail to start." It further notes that "a timeout or dropped
  connection while creating either forward may leave a mapping on the ADB server. The driver
  leaves an unconfirmed mapping alone because ADB does not identify the client that owns it."
- Recovery procedure (from the same doc): inspect forwards with `adb forward --list` against
  the same adb server; after confirming the mapping is not in use, run
  `adb forward --remove tcp:<port>` for that device (select device by serial if the adb server
  is remote). Exact `--list` line format is not documented here (**[PARTIAL]**); parse
  defensively (match serial + local port) or keep an authoritative local ledger.
- `--session-override` (single-session mode) deletes existing sessions when a new one starts
  — a safety net only when the fleet intentionally runs one session per server.
- `fullReset` uninstalls the app under test after the session; `noReset`-style sessions leave
  state, exactly what OOBE reset flows must account for.
- **[INFERRED]** The driver may also kill the adb server in some flows unless
  `appium:suppressKillServer=true`; for a shared 64-instance adb server always set
  `suppressKillServer: true` so one Appium session cannot take down the whole fleet's adb.

### 8.2 `AppiumManager.cleanup_forwards` requirements (from `core/interfaces.py`)

Implement as: for each instance with a known adb serial and allocated
`system_port`/`mjpeg`/`chromedriver` ports, enumerate forwards (`adb -s <serial> forward
--list`), remove only the entries whose local port is in that instance's ledger
(`adb -s <serial> forward --remove tcp:<port>`), then verify with a second `--list`.
Never use `adb kill-server`/`--remove-all` for routine cleanup on a shared host (global
state), and never remove a port another instance owns. Keep an in-process ledger of exact
forwards created per session (M1 already owns the port allocation) so cleanup does not depend
on fragile parsing after a crash. Track server PID as well so orphaned Appium processes can be
reaped; on Windows, process-tree kill semantics differ from POSIX (I3/P1 territory, but M1 must
not assume SIGTERM on Windows). **[INFERRED engineering requirement]**

### 8.3 Appium server lifecycle for M1

- Start: `appium server --address 127.0.0.1 --port <p> [--base-path <bp>] --log <file>
  --use-drivers=uiautomator2`; wait for `/status` `ready:true` with a bounded timeout; capture
  `build.version`.
- Status detail: use `/status` and/or `appium --show-build-info`; `GET /sessions` (Appium
  endpoint) or `/appium/sessions` variants can list live sessions — the repo fakes contract
  uses `GET /wd/hub/sessions`; real Appium exposes the Appium session list under the base
  path (`/sessions` when base path empty) while W3C has no list endpoint. Verify exact path
  against the pinned Appium 4 when wiring M1. **[PARTIAL/UNVERIFIED exact path]**
- Stop: prefer deleting active sessions, then terminate the server process; `--shutdown-timeout`
  governs connection draining (5000 ms default). Ensure no orphan Node process holds the port
  (`EADDRINUSE` on restart); the fake already fails start if the port is busy.
- Config: no auth on the API; protect with loopback + host firewall; never log proxy
  credentials via Appium capability/log paths.

---

## 9. Per-OS install notes (Windows/macOS/Linux)

[VERIFIED unless labelled]

| OS | Notes |
|---|---|
| All | Appium server = `npm install -g appium`; Node 22.22+/24.15+/26+ and npm >=10; driver via `appium driver install uiautomator2` (needs network; stop server first); Android SDK + platform-tools + JDK (`ANDROID_HOME`, `JAVA_HOME`); emulator visible in `adb devices`. |
| Windows | Node installers/`winget` are the usual route (installer I3's choice, R1 pins versions); npm global installs are per-user (`%APPDATA%\npm`) — ensure the `appium` shim is on PATH for the service account; long paths and AVD/emulator paths containing non-ASCII have known emulator bugs (37.3.10 release note) — keep the fleet root ASCII; `suppressKillServer` is important because adb server is a shared Windows process. |
| macOS | Apple Silicon: Node arm64 install, Rosetta not needed for Appium/Node; Appium home in `~/.appium`; UiAutomator2 driver works on any host OS (the emulator ABI must match the host: arm64-v8a images on Apple Silicon). |
| Linux | Node from distro/NodeSource or bundled runtime; `~/.appium`; ensure no `noexec` mount for the Appium install dir (portable edition check); headless host is fine for the server. |

Portable-specific (INFERRED, see §2.2): bundled Node must satisfy Appium 4's engines; the
driver install must be done at bundle-build time into the bundle's `APPIUM_HOME`; relocation
must not depend on absolute npm prefix paths (re-generate `appium` shim or use a wrapper that
sets `APPIUM_HOME` and executes the bundled `node .../appium/build/lib/main.js`).

---

## 10. Implications for avdfleet

1. **I3 installer**: install Node >= 22.22.2 (LTS), verify `node -v`/`npm -v >= 10`,
   `npm install -g appium@<pin>`, then `appium driver install uiautomator2@<pin>`; verify with
   `appium --version` + `appium driver doctor uiautomator2`; record both versions in the
   install manifest (driver updates change on-device APKs/snapshots expectations). Abort with a
   clear error if Node is outside Appium 4's ranges (do **not** silently install Appium 3/2 for
   Appium 4-only capability syntax).
2. **M1 ports**: allocate Appium server port, `systemPort`, `mjpegServerPort`, and (hybrid)
   `chromedriverPort`/`webviewDevtoolsPort` centrally from `core/ports.py`; never rely on the
   driver's random/free-port fallback; guarantee systemPort != mjpegServerPort and uniqueness
   across the fleet; expose them in `InstancePorts` (already modeled).
3. **M1 capabilities**: build from `AvdInfo` with `udid` always set, `automationName:
   UiAutomator2`, explicit `systemPort`/`mjpegServerPort`, `noReset` default true for fleet
   runs, `autoGrantPermissions` true, `suppressKillServer` true on shared adb, and raise the
   server install/launch timeouts for cold images. Use `appium:avd` only in the explicit
   "Appium launches the AVD" fallback.
4. **M1 cleanup**: implement `cleanup_forwards` with a per-instance forward ledger, targeted
   `adb -s ... forward --remove tcp:<port>`, verification pass, and stale-forward recovery
   (documented procedure); never `adb kill-server` on a live fleet; terminate Appium server
   trees and free its port on Windows and POSIX; stop the server before driver updates.
5. **Server defaults**: bind `--address 127.0.0.1`, choose one base path explicitly (real
   Appium 4 default is `/`; the repo fakes contract defaults the fake to `/wd/hub` — M1 must
   pass the same `--base-path` it advertises and clients must use it consistently), pass
   `--use-drivers=uiautomator2`, keep insecure features off except plugin CORS when the
   Inspector is intentionally enabled.
6. **Inspector**: installer step should offer either the standalone app (per-OS download,
   ~500 MB) or the plugin (`appium plugin install inspector`, Appium 4 needs plugin
   >= 2026.11.6, served at `/inspector` with `--allow-insecure='*:cors'`). For fleet mode keep
   it optional/disabled; document that Appium has no auth and the Inspector+CORS combination
   is a local-operator tool only. Portable bundle should pick the plugin (~9 MB) over the
   desktop app to save space, and must not enable it by default.
7. **Scripts (C1)**: Appium Python/JS script types must use Appium-4-compatible W3C clients and
   the server's base path/port; per-run session lifecycle must be quit in a finally block so
   forwards release; timeouts/cancel must kill the session (`DELETE`) not just the client
   process, then M1 cleanup runs. Record Appium/driver versions with each run result for
   reproducibility.
8. **No real-session evidence here**: this host cannot boot an emulator and has no Appium
   installed; parity is fakes-backed (`contracts/fakes-spec.md` §4.6 fixes fake appium
   `--version` 4.0.0, fake driver `uiautomator2@3.0.0`, `/wd/hub` base path, and a fake
   `GET /status`). Wave 1/2 should re-check the fake contract against the real versions above
   when a real host is available, but contracts are frozen this wave — list the mismatches in
   `PLAN.md` decisions rather than editing fakes unilaterally.

---

## 11. Sources (all accessed 2026-11-26)

Appium project documentation:
- System requirements: https://appium.io/docs/en/latest/quickstart/requirements/
- Install Appium: https://appium.io/docs/en/latest/quickstart/install/
- UiAutomator2 driver quickstart (install/doctor): https://appium.io/docs/en/latest/quickstart/uiauto2-driver/
- `appium server` options/defaults: https://appium.io/docs/en/latest/reference/cli/server/
- `appium driver/plugin` CLI (install/list/doctor/update): https://appium.io/docs/en/latest/reference/cli/extensions/
- Environment variables: https://appium.io/docs/en/latest/reference/cli/env-vars/
- Migrating to Appium 4 (Node/ESM/JSONWP/CORS/log/endpoint changes): https://appium.io/docs/en/latest/guides/migrating-3-to-4/
- Appium server security (`--allow-insecure`/`--relaxed-security`/`--app-url-rules`): https://appium.io/docs/en/latest/guides/security/
- UiAutomator2 capabilities reference: https://appium.github.io/appium-uiautomator2-driver/latest/reference/capabilities/
  (raw source used: https://raw.githubusercontent.com/appium/appium-uiautomator2-driver/master/docs/reference/capabilities.md)
- UiAutomator2 parallel sessions (unique udid/avd/systemPort/chromedriverPort/mjpegServerPort):
  https://raw.githubusercontent.com/appium/appium-uiautomator2-driver/master/docs/guides/parallel-tests.md
- UiAutomator2 MJPEG stream + stale-forward recovery + `adb forward --no-rebind tcp:<p> tcp:7810`:
  https://raw.githubusercontent.com/appium/appium-uiautomator2-driver/master/docs/guides/mjpeg.md
- Appium Inspector getting started (formats, OS requirements, plugin install/launch/compat):
  https://appium.github.io/appium-inspector/latest/getting-started/
- Appium Inspector README (desktop releases, plugin, web-app note):
  https://raw.githubusercontent.com/appium/appium-inspector/main/README.md
- Appium Inspector plugin README: https://raw.githubusercontent.com/appium/appium-inspector/main/plugins/README.md

npm registry metadata (exact versions/engines):
- https://registry.npmjs.org/appium/latest → 4.0.1, `engines: {npm: ">=10", node: "^22.22.2 || ^24.15.0 || >=26.0.0"}`, `"type": "module"`
- https://registry.npmjs.org/appium-uiautomator2-driver/latest → 10.4.0, `peerDependencies: {appium: "^4.0.0-beta.8"}`, driver metadata (name/automationName/platforms)

Repo contracts (read-only context): `contracts/fakes-spec.md` §4.6 (fake `appium`; note
`--version` 4.0.0, `/wd/hub` base path, driver `uiautomator2@3.0.0` differ from real 4.0.1 /
10.4.0 / default `/`), `contracts/openapi.yaml` (`AppiumStatus`, `InstancePorts`),
`src/avdfleet/core/interfaces.py` (`AppiumManager.build_capabilities`, `cleanup_forwards`),
`src/avdfleet/core/ports.py`, `BRIEF.md` §3.7.
