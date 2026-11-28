# Root and instrumentation

AVD Fleet can measure what root and instrumentation a running AVD actually has,
keep a central library of Xposed/Magisk/Zygisk/LSPosed packages, and deploy those
packages to selected instances with per-target results, cancellation and a
persistent log.

It performs **capability detection by measurement**, not by assumption: `adb root`
and Magisk root are different things, and *installed*, *enabled* and *active* are
different states. Most of this page exists to keep those distinctions visible in
the API, CLI and GUI.

The subsystem does **not** ship or embed Magisk, LSPosed or rootAVD. They are
GPL-licensed third-party tools; the operator supplies them, or asks the running
instance to acquire a **pinned official release** with a measured SHA-256 (see
[official-source acquisition](#official-source-acquisition)). Nothing is bundled
in the repository, wheel or portable bundle. See
[licences and redistribution](#licences-and-redistribution).

Per-image support is in [instrumentation-compatibility.md](instrumentation-compatibility.md).
The root/writable-system measurements this page builds on are in
[rooted-images.md](rooted-images.md); the verified Magisk 26.1 recipe for API 25
is in [oobe-api25.md](oobe-api25.md).

## Status of this subsystem

This page documents the change that adds the subsystem. It is deliberately
explicit about which parts were read in the source tree, which were measured on a
real emulator, and which are not verified.

| Surface | State in this change | Evidence |
|---|---|---|
| Capability probe (`DeviceProber`) | implemented; the `adb` command boundary is injectable and the parsers are pure functions | `src/avdfleet/instrumentation/capabilities.py` |
| SQLite storage | implemented as schema migration **v3** (`instrumentation_modules`, `instrumentation_profiles`, `instrumentation_runs`, `instrumentation_logs`) | `src/avdfleet/store/migrations.py` |
| REST API | implemented under `/api/v1/instrumentation` (17 operations, including the two toolchain routes) | `src/avdfleet/api/routes/instrumentation.py` |
| CLI | implemented; `avdfleet instrumentation --help` lists `scan`, `deploy`, `runs`, `logs`, `modules`, `profiles`, `toolchain` | `src/avdfleet/cli/commands/instrumentation.py` |
| GUI page | implemented at `/instrumentation` with tabs Overview, Root & Magisk, LSPosed & Zygisk, Module library, Profiles and Deployments; a **Toolchain** panel in the Root & Magisk and LSPosed & Zygisk tabs performs acquisition | `ui/src/pages/Instrumentation.tsx`, `.../_components/instrumentation/ToolchainPanel.tsx` |
| Official-source acquisition | implemented: pinned Magisk 26.1 APK and LSPosed 1.9.2 ZIP, size/SHA-256 verified, explicit confirmation, provenance manifest | `src/avdfleet/instrumentation/acquire.py` |
| Backend tests | 93 instrumentation tests pass (70 unit test functions + 16 API test functions, some parametrised); the LSPosed DB inventory/scope steps are unit-tested, not live-tested | `tests/unit/test_instrumentation.py`, `tests/api/test_instrumentation_api.py` |
| GUI tests | 14 Vitest/MSW page tests pass (scan states, failed import, duplicate-only built-ins, skipped targets and run log, acquisition consent/failure/import, new step editor) | `ui/src/test/pages-instrumentation.test.tsx` |
| Live checks on this host | API 34: Magisk 26.1, Zygisk and LSPosed 1.9.2 installed and **activated**, manager installed from the module APK, `complete_magisk_setup` driven unattended; LSPosed ZIP acquired/imported; an API 25 capability scan; a module import | [live evidence](#what-was-verified-live) |
| Magisk installation | **not bundled**; ramdisk patching is operator-supplied (rootAVD or equivalent, wrapped by `AVDFLEET_MAGISK_INSTALLER` when automated), then `complete_magisk_setup` | [external toolchain](#external-toolchain-avdfleet_magisk_installer) |
| Magisk "Additional Setup" | implemented as the `complete_magisk_setup` step: installs the acquired pinned APK, drives the dialogs via `uiautomator dump` + `input tap`, waits for the app's self-reboot and verifies `/data/adb/magisk`; **live-verified on API 34 (4 taps)** | `InstrumentationService._step_complete_magisk_setup` |
| LSPosed installation | implemented as a Magisk module install and **live-verified on API 34**: module active after reboot, `pidof lspd` returns a pid; the manager must be installed from `/data/adb/modules/zygisk_lsposed/manager.apk` | `InstrumentationService._step_install_lsposed` |
| Xposed enable + scope | implemented as best-effort writes to LSPosed's SQLite database (`enable_xposed_module`, `set_xposed_scope`), and the probe reports modules + scope; the LSPosed manager remains authoritative; device path measured on API 34, not exercised end-to-end in a deployment | `InstrumentationService._step_enable_xposed_module` |

Nothing in this page claims a full profile deployment end-to-end; the components
above were verified individually. See [honest limitations](#honest-limitations),
which also records what the test suite does and does not cover.

## Where it lives

The subsystem is a normal part of the product: one Python service, three thin
adapters (GUI, REST, CLI) and one SQLite database. The adapters share the service
rather than reimplementing it; the GUI covers the full step set.

### GUI page

The **Root & Instrumentation** page (`/instrumentation`) is the operator surface,
organised as six tabs:

- **Overview**: select AVDs, scan them, and see root implementation,
  Magisk/Zygisk/LSPosed state, blockers and `reboot_required`
- **Root & Magisk**: the per-device root detail and the Magisk-specific fields
  (version, environment ready, adb root vs Magisk root), plus the **Toolchain**
  panel for acquiring the pinned Magisk APK or LSPosed ZIP
- **LSPosed & Zygisk**: framework state, the Magisk module inventory and the same
  **Toolchain** panel
- **Module library**: import an APK/ZIP from disk or an HTTPS URL, list by kind,
  delete, inspect SHA-256 and provenance
- **Profiles**: list the read-only built-ins, duplicate one into an editable
  profile (with a module reference), edit every step action (all 19, including
  `complete_magisk_setup`, `enable_adb_root`, `enable_writable_system`, `reboot`,
  `install_lsposed_manager`, `remove_lsposed`, `enable_xposed_module` and
  `set_xposed_scope`), delete user profiles
- **Deployments**: deploy a profile to selected AVDs, then follow the run:
  per-target, per-step results, run state and log tail; cancel a run

The LSPosed tab and the device card render the `scope` column that `/capabilities`
returns; the older notice claiming scope was not reported has been removed. The
profile editor exposes every step action, including the Xposed database steps, the
root/writable-system actions and `reboot`.

### Lifecycle automation

An AVD can be bound to a profile that runs automatically when it starts:

```bash
curl -X PUT "$API/instrumentation/instances/Device_4/auto-profile" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"profile_id": "prof_builtin_root"}'
```

`GET` the same path returns the binding; an empty `profile_id` clears it. The
binding is stored in the `settings` table (`instrumentation.auto_profiles`) and
survives daemon restarts. Both `POST /instances/{name}/start` and
`POST /instances/{name}/wait-boot` queue the configured profile on a worker
thread after the call succeeds, so a slow deployment never delays the start
response. Automation is best-effort by design: a missing profile, a stopped AVD
or a failing step is logged and never fails the instance start.

To verify components after a reboot rather than install anything, bind a
verify-only profile (for example `Root`, or a custom profile whose single step is
`verify` with `expect`). Note that this does not detect a *snapshot restore*
having reverted the data partition — a restore that removes `/data/adb` shows up
as `magisk_environment_ready: false` on the next scan, which is the signal to
redeploy.

### REST API

All routes are under `/api/v1`, require the daemon bearer token, and return JSON
(the import route also accepts multipart). Errors use the standard envelope from
[api.md](api.md).

| Method + path | Purpose |
|---|---|
| `GET /api/v1/instrumentation/capabilities?targets=NAME[,NAME]` | Probe one or more AVDs; `targets` is repeatable or comma-separated |
| `POST /api/v1/instrumentation/scan` | Same probe with a JSON body: `{"targets": ["avd-01", ...]}` |
| `GET /api/v1/instrumentation/modules?kind=` | List the module library; `kind` is one of `xposed_apk`, `magisk_zip`, `zygisk_zip`, `lsposed_zip`, `unknown` |
| `POST /api/v1/instrumentation/modules/import` | Import a package: multipart form field `file`, or JSON `{"path": "..."}` / `{"url": "https://..."}` |
| `DELETE /api/v1/instrumentation/modules/{module_id}` | Remove a library record and its stored copy (`204`) |
| `GET /api/v1/instrumentation/toolchain` | Pinned upstream artifacts, local presence/integrity, and the provenance manifest; `available` is false if acquisition is not configured |
| `POST /api/v1/instrumentation/toolchain/{key}/acquire` | Download and verify one pinned release (`{"confirm": true}`; `409` without confirmation, `422` on a size/hash mismatch, `404` for an unknown key) |
| `GET /api/v1/instrumentation/profiles` | List profiles (built-ins first) |
| `POST /api/v1/instrumentation/profiles` | Create a profile: `{"name", "description", "steps": [...]}` |
| `PUT /api/v1/instrumentation/profiles/{profile_id}` | Replace a user profile's name, description and steps |
| `DELETE /api/v1/instrumentation/profiles/{profile_id}` | Delete a user profile (`204`); built-ins are refused |
| `POST /api/v1/instrumentation/profiles/{profile_id}/clone` | Copy any profile (including a built-in) into an editable one; optional `{"name"}` |
| `POST /api/v1/instrumentation/deployments` | Start a run: `{"profile_id", "targets", "label", "background"}` |
| `GET /api/v1/instrumentation/deployments?limit=` | Recent runs (default 50, max 500) |
| `GET /api/v1/instrumentation/deployments/{run_id}` | One run with every target and step result |
| `POST /api/v1/instrumentation/deployments/{run_id}/cancel` | Request cancellation |
| `GET /api/v1/instrumentation/deployments/{run_id}/logs?limit=` | Persisted log lines (default 500, max 5000) |

`background` defaults to `true`: the endpoint persists the run as `queued`,
starts it on a daemon thread and returns immediately. Pass `background: false` to
execute synchronously and receive the finished run.

Example:

```bash
TOKEN=$(cat "$HOME/.local/share/avdfleet/token")
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8765/api/v1/instrumentation/capabilities?targets=avd-01,avd-02"
```

Importing by `path` reads a file **on the daemon host** as the daemon user; the
bearer token is therefore equivalent to that user's file access. Treat it like any
other authenticated administrative call ([security.md](security.md)).

### CLI

```text
avdfleet instrumentation scan AVD [AVD ...] [--json]
avdfleet instrumentation modules list [--kind KIND] [--json]
avdfleet instrumentation modules import PATH | --url URL [--json]
avdfleet instrumentation modules delete MODULE_ID
avdfleet instrumentation toolchain list [--json]
avdfleet instrumentation toolchain acquire magisk|lsposed --confirm [--json]
avdfleet instrumentation profiles list [--json]
avdfleet instrumentation profiles delete PROFILE_ID
avdfleet instrumentation deploy PROFILE_ID --target AVD [--target AVD ...] [--label TEXT] [--wait/--no-wait] [--json]
avdfleet instrumentation runs [--limit N] [--json]
avdfleet instrumentation logs RUN_ID [--json]
```

`scan` prints a table of instance, API level, ABI, root, Magisk version, Zygisk
(active/enabled/no), LSPosed (active/installed/no) and `compatible`, then one
warning per blocker. `deploy` waits for the run by default; `--no-wait` returns
the queued run. `toolchain list` shows pinned version, kind, downloaded/verified
state and the pinned hash prefix; `toolchain acquire` refuses to download without
`--confirm`. The CLI is a thin adapter: it calls the same
`InstrumentationService` as the REST API, so each behaviour has one implementation
even where the CLI exposes a smaller surface.

## The capability model

### Two kinds of root

| | `adb root` | Magisk root |
|---|---|---|
| Whose root is it | the **host's** adb connection: adbd restarts as uid 0 | **applications'** root, through a `su` manager and policy |
| How it is obtained | a userdebug/eng build permits `adb root` | a patched boot ramdisk plus the Magisk app's setup |
| Survives a reboot | **no** — adbd restarts as the shell user | yes, the patch travels with the image |
| Gives apps `su` | no | yes |
| Detected by | `ro.build.type=userdebug` (or `ro.debuggable=1`) and a bare `adb shell id` showing `uid=0` (the rooted adbd runs in `u:r:su:s0`) | `su -c id` reporting `uid=0` in the Magisk domain (`context=u:r:magisk:s0`) |
| Enables Zygisk/LSPosed | **no** | yes |

Both are reported separately on every snapshot (`RootImplementation` is `none`,
`adb_root` or `magisk`), because a userdebug image with `adb root` looks "rooted"
to a naive check while having no application-facing root at all. That is exactly
the API 25/34 `google_apis` situation: `adb root` works, but `su` as an
application manager does not exist until Magisk is installed
([rooted-images.md](rooted-images.md)).

`adb root` is also **per boot**: after a reboot `adb shell id` is the shell user
again, so automation must re-run it after every boot. This is why the probe
re-checks instead of caching a previous answer.

### Installed, enabled, active

A file that has been copied is not a working module. The snapshot separates three
states for every component:

| State | Meaning | How this build detects it |
|---|---|---|
| `absent` | not present, or the prerequisite is missing | probe found nothing |
| `installed` | the artifact is on the device | Magisk reports a version; `/data/adb/modules/<id>` exists; LSPosed's module is present |
| `enabled` | it will run on the next boot | no `/data/adb/modules/<id>/disable` marker; Zygisk's persisted setting is `1` |
| `active` | it is running **now** | a reboot that loads it; checked separately (Zygisk: `zygiskd`, LSPosed: `pidof lspd`, Xposed: DB `enabled` only) |

A separate distinction inside Magisk matters: `/data/adb/magisk` existing means
**only** that the app's "Additional Setup" has completed. Magisk root can work
while that directory is absent; module scripts cannot. The snapshot reports both
(`magisk` and `magisk_environment_ready`).
| `unsupported` | this action cannot work here (e.g. a Magisk step on an `adb root`-only device) | step result |
| `unknown` | not measured (device not running) | probe result |

Concrete consequences:

- A Magisk module ZIP pushed with `magisk --install-module` is **installed and
  enabled**, not active. Magisk runs module scripts at boot, so the run reports
  `reboot_required` and the verify step stays unsatisfied until the AVD reboots
  ([Magisk developer guides](https://topjohnwu.github.io/Magisk/guides.html)).
- Zygisk is **off by default** (no `settings` row). The probe reads the persisted
  setting through `magisk --sqlite`; when it is `1`, `active` is proved by a
  running `zygiskd` (`ps -A` shows `zygiskd64` on x86_64) or by a staged
  `loader*` under `$(magisk --path)/.magisk/zygisk`. `/sbin/.magisk` does not
  exist on API 34 — never hard-code it.
- LSPosed is a Magisk module, so "installed" comes from the module list and
  "active" is proved by `pidof lspd` (falling back to a non-empty
  `/data/adb/lspd`). `pm list packages | grep lsposed` is a **false negative**
  for activation and is not used (see
  [verifying LSPosed](#verifying-lsposed-is-running)).
- Magisk's own app is needed for setup but is not proof of it:
  `complete_magisk_setup` installs the pinned APK, and only a populated
  `/data/adb/magisk` (after the app's self-reboot) means module scripts can run.
- For **Magisk modules** the probe currently reports `active` equal to `enabled`:
  there is no reliable per-module marker for "its script ran". Only the
  framework-level Zygisk/LSPosed state is checked after a reboot. Do not read a
  module's `active` flag as proof that its hook is working.
- For **Xposed modules** `enabled` and `active` both come from LSPosed's `modules`
  table (`active` mirrors `enabled`), and `scope` lists the target app packages
  LSPosed has recorded. None of that proves a hook ran; confirm in the manager.

### Per-device fields

`DeviceInstrumentation` is the object returned by a scan (GUI, `GET
/instrumentation/capabilities`, `avdfleet instrumentation scan`).

| Field | Meaning |
|---|---|
| `instance` | AVD name |
| `api_level`, `abi` | from the instance/image where known, otherwise from `ro.build.version.sdk` and `ro.product.cpu.abi` |
| `image_id`, `image_tag` | the image catalogue entry when the container can resolve it |
| `serial` | the adb serial used for every command |
| `running`, `boot_completed` | whether the probe could talk to a booted device |
| `root_implementation` | `none`, `adb_root` or `magisk` — the authoritative root field |
| `capabilities` | `CapabilityFlags` (below) |
| `magisk_modules` | `InstalledModule` records from `/data/adb/modules`: identifier, kind, version, enabled, active, source, scope (empty) and notes |
| `xposed_modules` | Xposed modules **recognised by LSPosed**, read from its SQLite database: identifier (module package), kind `xposed_apk`, `enabled`, `active`, `source: "lsposed"`, `scope` (target app packages) and notes. Empty when LSPosed is absent, Magisk root is missing, or the device has no `sqlite3` (a note explains which) |
| `compatible` | `true` when any root is available (the precondition for every step) |
| `blockers` | human-readable reasons the device cannot be instrumented, e.g. "no root: install Magisk or use a userdebug image with adb root" |
| `probed_at` | UTC timestamp of the measurement |

`CapabilityFlags` carries the measured details:

| Flag | Meaning |
|---|---|
| `adb_root` | `adbd` itself is running as uid 0, measured from a bare `adb shell id`. Magisk root does **not** set this; use `root_implementation` as the authoritative root field |
| `writable_system` | `/system` is already read-write (remounted or overlayfs) |
| `magisk`, `magisk_version`, `magisk_environment_ready` | Magisk is reachable through `su`, its version, and whether `/data/adb/magisk` is populated. That last flag means **only** "Additional Setup completed" — Magisk root works without it; module scripts do not |
| `zygisk_available`, `zygisk_enabled`, `zygisk_active` | Zygisk prerequisites, persisted setting (off by default), and runtime proof (`zygiskd` or a staged loader under `$(magisk --path)`) |
| `lsposed_installed`, `lsposed_version`, `lsposed_active` | LSPosed module presence, version from `module.prop`, and runtime proof (`pidof lspd`, with `/data/adb/lspd` as fallback) |
| `reboot_required` | Magisk is installed and Zygisk's persisted setting differs from its runtime state |
| `notes` | probe explanations, including the "`/data/adb` is not present" / Additional Setup warning |

`root_state` (used by the GUI badge and the verify step) is a compact projection:
Magisk → `active`, `adb root` → `enabled`, no root → `absent` when running and
`unknown` when not.

The probe enumerates `/data/adb/modules` **once per scan** and reuses that
inventory for the LSPosed check, so `magisk_modules` is populated whether or not
LSPosed is installed.

## The module library

The library is central: import an artifact once, deploy it to many AVDs. It stores
the bytes and the provenance, and it never executes or activates anything.

### Package kinds

`ModuleKind` exists because the four formats are **not interchangeable**:

| Kind | What it is | How this build recognises it |
|---|---|---|
| `xposed_apk` | an Android app that contains Xposed entry points | the archive has `AndroidManifest.xml` **and** an Xposed marker: `assets/xposed_init`, `META-INF/xposed/module.prop`, `META-INF/xposed/scope.list`, or any `META-INF/xposed/` entry |
| `magisk_zip` | a Magisk module installer ZIP | `module.prop` at the archive root, and the classification below picks Magisk |
| `zygisk_zip` | a Magisk module with native Zygisk payloads | `module.prop`, and the archive contains `zygisk/` (or, currently, any `system/bin/` entry — see the caveat below) |
| `lsposed_zip` | the LSPosed framework, which **is** a Magisk module | `module.prop` whose `id`/`name` contains `lsposed` (`zygisk_lsposed`, `riru_lsposed`, `lsposed`, `zygisk-lsposed` are accepted) |
| `unknown` | a readable APK with no Xposed markers | reported as `unknown` with a note; it cannot be installed as an Xposed module |

The archive is inspected as inert bytes with `zipfile`; entries are limited to
4096 and the artifact to 256 MiB. `module.prop` is parsed with a forgiving
`key=value` parser (comments and malformed lines are skipped), and APK metadata
(package, version, label, min SDK) comes from `aapt2 dump badging` when the SDK's
`build-tools` provides it. If `aapt2` is unavailable the record is still created
with the data the archive itself exposes.

The upstream formats behind these checks:

- Magisk module layout, `module.prop` fields (`id`, `name`, `version`,
  `versionCode`, `author`, `description`, optional `updateJson`) and the
  `disable`/`remove` status markers are documented in the
  [Magisk developer guides](https://topjohnwu.github.io/Magisk/guides.html).
- The legacy Xposed marker is `assets/xposed_init`, one fully-qualified class name
  per line, plus the `xposedmodule`, `xposedminversion` and `xposeddescription`
  manifest meta-data ([XposedBridge development tutorial](https://github.com/rovo89/XposedBridge/wiki/Development-tutorial)).
- The modern Xposed convention replaces the legacy marker with
  `META-INF/xposed/java_init.list`, `META-INF/xposed/scope.list` and
  `META-INF/xposed/module.prop` ([LSPosed wiki](https://github.com/LSPosed/LSPosed/wiki/Develop-Xposed-Modules-Using-Modern-Xposed-API)).
- LSPosed 1.9.2's own `module.prop` is `id=zygisk_lsposed`, `name=Zygisk -
  LSPosed`, `version=v1.9.2 (7024)`, and it states "Supports Android 8.1 ~ 14.
  Requires Magisk 24.0+ and Zygisk enabled."

### Identity, dedupe and provenance

- The record id is `mod_<first 16 hex of SHA-256>`.
- `sha256` is unique in the database. Importing the same bytes again is a **409
  conflict** that names the existing record, not a second copy.
- Bytes are stored at
  `<data>/instrumentation/modules/<sha256>/<sanitised filename>`. Deleting the
  library record removes that copy; copies already deployed to an AVD are not
  touched by the library.
- `source` is `upload` for a file/multipart import and `url` for an HTTPS import;
  `source_url` records the URL. `created_at`/`updated_at`, size, version and the
  inspection `metadata` (including the parsed `module.prop` and badging) are
  stored with the record, so provenance outlives the operator's memory.
- URL imports must be **HTTPS**; plain HTTP is refused. (The library class can be
  constructed with an explicit insecure-URL opt-in, but the daemon does not enable
  it.) The download is capped at the same 256 MiB.

Import does **not**:

- check a signature, certificate or publisher (SHA-256 binds the bytes, not the
  author);
- execute the archive, load the APK or run its installer;
- activate anything, install anything on a device, or verify that the artifact
  works on a given API level.

Acquisition is stricter than import about integrity, but still not a signature:
it compares the bytes of two **known upstream releases** against a pinned size and
SHA-256 (see [official-source acquisition](#official-source-acquisition)). An
imported file the operator supplies is content-addressed but not authenticated.

### Known classification caveat

The Zygisk classifier currently treats either a `zygisk/` entry **or** any
`system/bin/` entry as a Zygisk payload. A plain Magisk module that ships a binary
under `system/bin/` can therefore be labelled `zygisk_zip`. Treat the kind as a
hint for filtering; `module.prop`'s `id` is authoritative for identity, and the
deployment steps do not depend on the Zygisk label.

## Deployment profiles

A profile is a named, reusable sequence of steps. Five built-ins are seeded
idempotently at startup; they are read-only starting points.

| Profile | Id | Steps |
|---|---|---|
| Root | `prof_builtin_root` | enable adb root, verify root |
| Magisk + Zygisk | `prof_builtin_magisk_zygisk` | install Magisk, complete Magisk setup, enable Zygisk, reboot, verify `magisk` + `zygisk_active` |
| LSPosed | `prof_builtin_lsposed` | install Magisk, complete Magisk setup, enable Zygisk, install LSPosed, reboot, install the LSPosed manager, verify `magisk` + `lsposed_active` |
| Xposed Instrumentation | `prof_builtin_xposed` | install Magisk, complete Magisk setup, enable Zygisk, install LSPosed, reboot, install the LSPosed manager, install a library module, verify `magisk` + `lsposed_active` |
| Custom | `prof_builtin_custom` | verify root (a starting point to edit) |

The seeded built-ins are refreshed on startup when their id/name already exists
(so an upgrade that adds a step, such as `complete_magisk_setup`, updates the
read-only copy); user profiles are never overwritten.

**The built-ins ship without a module binding.** A step that knows which kind it
needs — `install_lsposed` requires an `lsposed_zip` — resolves the **newest**
library artifact of that kind when its `module_id`/`module_identifier` is empty.
So after `toolchain acquire lsposed` (which imports the ZIP) the built-in LSPosed
profile resolves it without an edit; if the library has no artifact of that kind,
the step fails with a hint to acquire or import one.

`install_module` is the exception: it accepts an Xposed APK **or** a Magisk/Zygisk
ZIP, so there is no safe kind to fall back to and the step still requires an
explicit `module_id` (or a `module_identifier` matching a package/record name).
The built-in **Xposed Instrumentation** profile therefore fails at its module step
until you clone it — `POST /profiles/{profile_id}/clone`, or the **Duplicate**
action in the GUI's Profiles tab — and bind a module to the clone. An explicit
binding always wins over the fallback, and a binding of the wrong kind is
rejected.

### Step actions

| Action | What it does | Requires |
|---|---|---|
| `verify_root` | probes the device and succeeds when `root_implementation` is not `none` | a running AVD |
| `install_magisk` | runs the external installer for the instance | `AVDFLEET_MAGISK_INSTALLER` |
| `complete_magisk_setup` | installs the **acquired pinned** Magisk APK, launches the Magisk app, taps its "Additional Setup" dialogs (via `uiautomator dump` + `input tap`), waits up to 300 s for the app's self-reboot, then verifies `/data/adb/magisk` is populated. It reboots the AVD as a side effect | `AVDFLEET_MAGISK_INSTALLER` not required, but the `magisk` artifact must be acquired; Magisk root already running |
| `enable_zygisk` | writes the persisted Zygisk setting to `1` (off by default) | Magisk root |
| `disable_zygisk` | writes the persisted Zygisk setting to `0` | Magisk root |
| `install_lsposed` | refuses API < 27, pushes an `lsposed_zip` module and runs `magisk --install-module` | Magisk root, API ≥ 27; an `lsposed_zip` in the library (newest used when unbound) |
| `install_module` | Xposed APK → `adb install -r -g`; Magisk/Zygisk ZIP → `magisk --install-module` | LSPosed for APKs, Magisk for ZIPs; a **bound** library module |
| `enable_module` | removes the module's `disable` marker | Magisk root, module identifier |
| `disable_module` | creates the module's `disable` marker | Magisk root, module identifier |
| `remove_module` | creates the module's `remove` marker (removed at next boot) | Magisk root, module identifier |
| `enable_xposed_module` | sets LSPosed's `modules.enabled` flag for a module package (`params.package` or `module_identifier`; `params.enabled` defaults to true) | Magisk root, LSPosed, `sqlite3` on the device, a module LSPosed has registered |
| `set_xposed_scope` | replaces (default) or extends a module's per-app scope (`params.package`, `params.apps`, optional `params.replace: false`) | Magisk root, LSPosed, `sqlite3`, a module LSPosed recognises |
| `enable_adb_root` | runs `adb root` and verifies `id` reports `uid=0` | a userdebug image (production images refuse) |
| `enable_writable_system` | `adb root` + `adb remount`, then reports whether `/system` is actually writable; on API 30+ this sets up overlayfs and needs a reboot | a userdebug image |
| `install_lsposed_manager` | installs `/data/adb/modules/zygisk_lsposed/manager.apk` with `pm install -r` | LSPosed installed |
| `reboot` | reboots the AVD and waits for `sys.boot_completed` (optional `params.timeout_s`, default 300) | a running AVD |
| `remove_lsposed` | writes the LSPosed module's `remove` marker; removal completes at the next boot | Magisk root |
| `remove_magisk` | always reports `unsupported`: removing Magisk means restoring the image ramdisk (`ramdisk.img.backup`), which mutates a shared system image and is not automated | n/a |
| `verify` | probes and checks the `expect` map (`magisk`, `zygisk_active`, `lsposed_active`, `root`) | a running AVD |

The two Xposed steps write LSPosed's own database
(`/data/adb/lspd/config/modules_config.db`) with `sqlite3`. That schema is
LSPosed-internal, not a documented API: a future LSPosed release can change it,
and the LSPosed manager remains the supported way to enable modules and set scope.
Package names are validated before they are embedded in SQL
(`^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$`), which is the injection boundary.
Both Xposed steps fail as `unsupported` when LSPosed does not recognise the module
(install the APK and open the manager once so it registers);
`enable_xposed_module` checks that LSPosed has registered the module first and
reports `unsupported` otherwise, so it cannot claim success for an unknown package. Both require a reboot so `lspd`
reloads the configuration, and `active` for an Xposed module only mirrors
`enabled` — it is not proof the hook ran.

Every step that changes the device reports that a **reboot** is required.
`complete_magisk_setup` is the exception to "the deployment never reboots": the
Magisk app reboots the AVD itself as part of its setup, and the step waits (up to
300 s) for `sys.boot_completed` before verifying `/data/adb/magisk`. All other
steps leave rebooting to the operator (`avdfleet instances restart`, or the GUI),
so Zygisk/LSPosed/config changes only take effect on the next boot.

### Running a deployment

1. `POST /deployments` validates the profile and targets, persists a run with
   state `queued`, and (by default) starts it on a daemon thread.
2. Targets run in a bounded pool of worker threads (default 4, clamped to the
   number of targets). No CLI or API switch changes the bound in this change.
3. Each target runs the profile's steps in order. A step failure stops that
   target, unless the step sets `params.continue_on_error: true`. Other targets
   are unaffected: every target is executed inside its own exception boundary, so
   one broken or incompatible AVD cannot sink the run.
4. A target whose instance is not running is recorded as `skipped` with an
   explanation instead of failing the whole run.
5. Cancellation is a flag checked **between steps** (and before the first step).
   It cannot interrupt an in-flight adb call, the external installer, or the
   `complete_magisk_setup` wait (up to 300 s); a run that is cancelled is recorded
   as `cancelled`, with the per-target results collected so far.
6. Run state: `succeeded` when every target succeeded, `partial` when some did,
   `failed` when none did, `cancelled` when cancellation was requested. Per-target
   `ok`/`error`/`skipped` and per-step `ok`/`state`/`message` are stored in the run.
7. Log lines (run-level and per-target) are persisted with level, instance and
   timestamp, and redacted through the same choke point as every other run record.

A run does not:

- reboot except through `complete_magisk_setup`, where the Magisk app reboots the
  AVD and the step waits up to 300 s for boot; every other step only reports
  `reboot_required`, so a later `verify` in the same run confirms only what is
  already active;
- resume after a daemon restart — a run left `queued`/`running` by a restart stays
  in that state; re-deploy it;
- roll back a partially deployed device. Removing a module is an explicit step;
  `remove_module` only marks it for removal at the next boot.

## External toolchain: `AVDFLEET_MAGISK_INSTALLER`

Magisk and rootAVD are GPL-licensed and are not bundled. rootAVD is never
downloaded by AVD Fleet; the Magisk APK can be acquired from its pinned official
release on request (see [official-source acquisition](#official-source-acquisition)).
Installing Magisk is a per-image boot-ramdisk patch, and the safe procedure
depends on the emulator build; the product therefore delegates it to a command the
operator supplies.

Set one environment variable before starting the daemon (or the CLI command):

```bash
# macOS / Linux
export AVDFLEET_MAGISK_INSTALLER='/opt/magisk/install-magisk.sh {instance} {serial}'
```

```powershell
# Windows PowerShell
$env:AVDFLEET_MAGISK_INSTALLER = 'C:\tools\magisk\install-magisk.cmd {instance} {serial}'
```

Semantics, exactly as implemented:

- The template is run through the platform shell (`shell=True`), so quoting is
  your responsibility.
- `{instance}` is replaced with the AVD name and `{serial}` with its adb serial.
- The child environment is the daemon's environment **with the daemon secrets
  removed** (see below), plus `ANDROID_SERIAL` (when a serial is known) and
  `ANDROID_HOME`/`ANDROID_SDK_ROOT` (the configured SDK root).
- The timeout is 1800 s. A non-zero exit is a failed `install_magisk` step whose
  message is the tail of the output; the step never pretends to have installed.
- Nothing runs unless a profile containing `install_magisk` is explicitly
  deployed to that instance.

**Why it is not bundled:** both Magisk and rootAVD are GPLv3; redistributing them
inside an MIT distribution would change the licensing of that distribution (see
below). There is also no single safe installer: the official Magisk flow patches
a boot/init_boot/vendor_boot/recovery image and requires a fastboot flash or a
repacked emulator ramdisk, plus the Magisk app's "Additional Setup" on first launch
([Magisk installation](https://topjohnwu.github.io/Magisk/install.html)). The
procedure verified on this project is rootAVD patching the emulator `ramdisk.img`
with Magisk 26.1 (API 25 by hand; API 34 with `complete_magisk_setup` automating
the app half), and it mutates a shared SDK image unless a copy is patched — which
is another reason it must stay an explicit operator action
([oobe-api25.md](oobe-api25.md)).

The installer runs with your privileges and can modify the AVD's image. A
deployment is authenticated like every other API call, but the command itself is
only as safe as the script you point it at.

The child environment is built through the shared `core.childenv` sanitiser: the
daemon's bearer token (`AVDFLEET_TOKEN`), the vault passphrase
(`AVDFLEET_VAULT_PASSPHRASE`) and any `AVDFLEET_SECRET_*` values are removed after
`ANDROID_SERIAL`/`ANDROID_HOME` are added, so the operator's installer does not
inherit them. It still receives the rest of the daemon environment, and it runs
with your privileges.

### Completing Magisk's Additional Setup

A patched ramdisk gives Magisk root, but not a working module environment. Until
the Magisk app has run its **Additional Setup**, `/data/adb/magisk` is absent,
magiskd logs `/data/adb is not present, abort`, and `magisk --install-module`
prints `Incomplete Magisk install`; no module script runs. The
`complete_magisk_setup` step does this unattended:

- it requires the pinned **`magisk` artifact** to be present and verified
  (`toolchain acquire magisk --confirm`, or the GUI Toolchain panel); otherwise
  the step fails with a hint to acquire it;
- it installs that APK with `adb install -r -g` and launches
  `com.topjohnwu.magisk/.ui.MainActivity`;
- it reads the live UI with `uiautomator dump` and taps the first matching button
  (`ok`, `allow`, `proceed`, `yes`, `reboot`, or an id ending `button1`), up to
  five times, 2.5 s apart — no hard-coded coordinates;
- the Magisk app **reboots the AVD itself**; the step waits up to 300 s for
  `sys.boot_completed` and then checks that `/data/adb/magisk` is populated. If it
  never is, the step fails and asks the operator to open the app and confirm the
  dialog;
- an already-complete environment short-circuits to `active` without a reboot, and
  a device without Magisk root fails as `unsupported`.

Measured on API 34: the flow completed with **four unprompted taps**. That is a
UI-automation guarantee, not a protocol: a changed Magisk UI, a different locale,
or a missing `uiautomator` can break it, which is why the failure path is explicit.
The built-in Magisk + Zygisk, LSPosed and Xposed Instrumentation profiles all run
this step between `install_magisk` and `enable_zygisk`.

## Official-source acquisition

The alternative to a manual download is a **pinned acquisition**: the running
instance fetches a known upstream release asset, verifies its exact size and
SHA-256, stores it under the data directory, records provenance, and — for a
Magisk module ZIP — imports it into the module library. It is available over the
REST API, the CLI and the GUI Toolchain panel.

This is how the licence is kept honest without vendoring: AVD Fleet is MIT, the
tooling is GPL-3.0, and a download performed by the operator's own instance on
the operator's own host is not a redistribution of the tooling. Nothing is
bundled in the repository, wheel or portable bundle.

### Pinned releases

| Key | Version | Kind | Size | SHA-256 | Min API | Notes |
|---|---|---|---|---|---|---|
| `magisk` | Magisk 26.1 (`26100`) | `apk` | 11,411,692 B | `ae1a02b1ab608a51d5bc9b323e0588d06d30d9987ac8da01f4710d76f705dccb` | 21 | The APK is the payload the Magisk app installs during "Additional Setup". Ramdisk patching is still required (rootAVD or equivalent). |
| `lsposed` | LSPosed 1.9.2 (`7024`) | `module_zip` | 2,462,055 B | `0ebc6bcb465d1c4b44b7220ab5f0252e6b4eb7fe43da74650476d2798bb29622` | 27 | Zygisk flavour: requires Magisk 24+ with Zygisk, and Android 8.1+ (API 27). |

`min_api` is reported by `toolchain list` and the API; **acquisition does not
enforce it against a device** — it is metadata for choosing a target. Both hashes
and sizes were measured by downloading the official release assets on 2026-11-27.

### Consent, verification and storage

- `POST /api/v1/instrumentation/toolchain/{key}/acquire` requires
  `{"confirm": true}`. Without it the API returns **409** naming the pinned
  version and its GPL-3.0 licence; the CLI equivalent is
  `--confirm`. An unknown key is **404**.
- The download is capped at `min(512 MiB, pinned size + 1 MiB)` and must match the
  pinned size exactly, then the pinned SHA-256. A mismatch is **422**, the bytes
  are discarded and nothing is recorded or imported.
- A present, verified copy is returned without re-downloading. A present but
  corrupted copy is replaced.
- Files live under
  `<data>/instrumentation/toolchain/<key>/<version>/<filename>` (for example
  `.../toolchain/lsposed/1.9.2/LSPosed-v1.9.2-7024-zygisk-release.zip`). Provenance
  is a single newest-first `<data>/instrumentation/toolchain/manifest.json` with
  `key`, `name`, `version`, `kind`, `sha256`, `size_bytes`, `source_url`,
  `licence` and `acquired_at` per entry.
- After a successful `module_zip` acquisition the ZIP is **imported into the
  module library** with `source: "official-release"` and the release URL as
  `source_url`; the response includes the resulting `module_id`. If those exact
  bytes are already in the library, the existing record is reused. Acquired APKs
  are stored only — they are not library records and not Xposed modules.

Acquisition is **not installation**. It does not patch a ramdisk, complete
Magisk's Additional Setup, enable Zygisk, reboot an AVD, install a module on a
device, or activate an Xposed module. Those remain the separate, explicit steps
described on this page.

### What was verified live

On this host, 2026-11-27, through the running daemon and a throwaway API 34
`google_apis;x86_64` AVD:

- **Magisk 26.1 + Additional Setup**: `complete_magisk_setup` completed with
  **four unprompted taps**; `/data/adb/magisk` was populated afterwards. Before
  setup, `magisk --install-module` failed with `Incomplete Magisk install`; after
  it, module installs exit 0.
- **Zygisk and LSPosed 1.9.2**: the LSPosed module installed
  (`/data/adb/modules/zygisk_lsposed`, no `disable`/`remove`), `/data/adb/lspd/{config,log}`
  existed, `pidof lspd` returned a pid, and logcat showed
  `ZygiskCompanion: welcome to LSPosed!` and `LSPosed Bridge: binder received`.
- **LSPosed manager**: `lspd` did *not* install it (`I LSPosedService: manager is
  not installed`); `pm install -r` of the module's `manager.apk` installed
  `org.lsposed.manager`, whose Overview then reported **Activated**.
- **Acquisition**: the LSPosed 1.9.2 ZIP was acquired through the pinned path
  (2,462,055 bytes, SHA-256 verified, manifest written at
  `2026-11-27T21:22:44Z`) and imported into the library as
  `mod_0ebc6bcb465d1c4b` with `source: "official-release"`.
- **API 25 probe** and a manual **module import** also ran live through the
  daemon.

Acquisition itself still does not install anything: every install/activation
result above came from the separate deployment or manual steps.

## Why installing an Xposed APK is not activation

This is the single most misunderstood part of Xposed, and the subsystem refuses to
paper over it.

Xposed modules are **not apps you launch**. The framework's zygote hook loads
module entry classes into app processes at boot; the APK is only a container for
those classes and its manifest metadata. The legacy contract is:

- `assets/xposed_init`: one fully-qualified entry class per line;
- manifest meta-data `xposedmodule=true`, `xposedminversion`, `xposeddescription`;
- the module must be **enabled** in the Xposed/LSPosed manager and the target app
  must be in its **scope**, then the device must reboot so zygote reloads.

The upstream tutorial is explicit: after installing the APK you "need to enable it
before you can use it", in the installer's Modules tab, "then reboot"
([XposedBridge tutorial](https://github.com/rovo89/XposedBridge/wiki/Development-tutorial)).
LSPosed keeps the same model, with per-app scope and a manager app that is bundled
inside its Magisk module (`manager.apk`), though `lspd` does not install it — see
[verifying LSPosed](#verifying-lsposed-is-running) ([LSPosed README](https://github.com/LSPosed/LSPosed)).

Therefore:

- `adb install` of an Xposed APK only makes the package present. The step records
  state `installed` and returns `activation: requires-lsposed-manager`.
- There are two ways to enable it and set scope. The **supported** one is the
  LSPosed manager UI. The **measured, best-effort** one is the profile steps
  `enable_xposed_module` and `set_xposed_scope`, which write LSPosed's internal
  SQLite database and require a reboot for `lspd` to reload; they only work once
  LSPosed has registered the module, and the schema is not a public API. Both
  steps are available in the GUI profile editor and the API.
- The probe reports Xposed modules and their scope from that database
  (`xposed_modules[].scope`), but `active` mirrors `enabled`; it is not proof the
  hook ran. Confirm the live result in the LSPosed manager.
- A "successful" deployment of the Xposed Instrumentation profile without the DB
  steps means *Magisk, Zygisk, LSPosed and the APK are in place*, not that the
  hook is live. With the DB steps it additionally means "enabled and scoped was
  written"; only a reboot plus the manager can confirm it took effect.

## Verifying LSPosed is running

LSPosed publishes no official `adb`-only health command. The signals below were
measured on API 34 (`google_apis;x86_64`) with LSPosed 1.9.2:

1. **`pidof lspd` returns a pid.** This is the probe's primary activity check (the
   fallback is a non-empty `/data/adb/lspd`). After the reboot the daemon's
   `service.sh` starts `app_process --nice-name=lspd`.
2. **Logcat** shows `LSPosed : ZygiskCompanion: welcome to LSPosed!` and
   `LSPosed Bridge: binder received`. A running daemon is the activation evidence;
   the module install alone is not.
3. **The module directory**: `/data/adb/modules/zygisk_lsposed` exists with no
   `disable`/`remove` marker, and `/data/adb/lspd/{config,log}` is present.
4. **The manager is a separate install.** `lspd` does *not* install it — logcat
   says `I LSPosedService: manager is not installed`. Install
   `/data/adb/modules/zygisk_lsposed/manager.apk` with `pm install -r`; only then
   does `pm list packages` show `org.lsposed.manager`, and its Overview reports
   **Activated**.
5. **`pm list packages | grep lsposed` is a false negative** for activation and is
   never used as the test. The manager's own status is authoritative.
6. **Reading its database**: `/data/adb/lspd/config/modules_config.db` is
   WAL-mode — around 4 KB for the database plus ~99 KB in the `-wal` file. Copy
   `db`, `-wal` and `-shm` together, or query on-device with the image's
   `sqlite3` (API 34 ships `/system/bin/sqlite3` 3.39.2, which is what the
   subsystem uses). A naive copy of the `.db` alone reads as an empty database.

The daemon/database details are on-device conventions, not a stable documented
API. They can change between LSPosed releases; the manager is the authority.

## Licences and redistribution

| Component | Licence | Source |
|---|---|---|
| AVD Fleet | **MIT** | [LICENSE](../LICENSE) |
| Magisk | **GPL-3.0** | [Magisk repository](https://github.com/topjohnwu/Magisk) (GitHub licence metadata: GPL-3.0) |
| rootAVD | **GPL-3.0** | [rootAVD repository](https://github.com/newbit1/rootAVD) (GPL-3.0; the repository is archived) |
| LSPosed | **GPL-3.0** | [LSPosed repository](https://github.com/LSPosed/LSPosed), and its README states GPL-3 |
| XposedBridge (legacy API) | **Apache-2.0** | [NOTICE.txt](https://github.com/rovo89/XposedBridge/blob/art/NOTICE.txt) |
| Legacy XposedInstaller | **unstated upstream** | no licence file at the repository root and GitHub detects none; do not assume a licence without checking the source headers |

Rules this project follows, and what an operator must do:

- **Do not vendor or redistribute Magisk, LSPosed or rootAVD** with AVD Fleet,
  in the repository, a wheel, a portable bundle or a container image.
- **Require user-supplied or explicitly acquired official packages.** The module
  library accepts the operator's own APK/ZIP; the Magisk installer is an operator
  command; the acquisition flow fetches only the pinned official release, only
  after explicit confirmation, and records its licence (GPL-3.0) in the manifest.
- **Keep them external.** AVD Fleet stores and serves the artifact the operator
  imported or acquired, on the operator's host; it does not embed GPL code in its
  own process, link against it, or ship a modified copy.
- **Record provenance and version.** Every library record stores SHA-256, size,
  source, URL and the parsed `module.prop`; keep the upstream release tag with the
  artifact in your own records. The measured baseline here is Magisk **26.1**
  (`26100`); the LSPosed artifact acquired and imported here is **1.9.2** (version
  code `7024`).
- **Preserve licence notices.** If you redistribute a bundle that contains these
  tools, ship their licences and source offer as GPLv3 requires.

Mixing GPL binaries into an MIT distribution **would change its licensing**: the
distributed whole would have to satisfy GPLv3 (source availability, notices,
no extra restrictions), even though AVD Fleet's own code remains MIT. Keeping the
tools user-supplied and outside the distribution is what preserves the MIT-only
claim. Installing GPL software on a device you own is not distribution; putting it
in a bundle you hand to someone else is.

## Honest limitations

### Implemented and tested without an emulator

- **93 instrumentation tests pass** in this checkout: 70 unit test functions plus
  16 API test functions (some parametrised). They cover the v3 migration, module
  repository CRUD and SHA-256 duplicate rejection, profile CRUD, built-in seeding
  and cloning, archive classification for all four formats, `module.prop` and
  `aapt2` parsing, the probe's root/Zygisk/LSPosed/Magisk-environment state machine
  over a scripted `adb` runner (including `zygiskd`, `pidof lspd` and the Xposed
  inventory/scope tables), the two Xposed DB-write steps and their injection
  validation, `complete_magisk_setup`'s acquired-APK/no-op/unsupported paths, REST
  round trips, the 401/404/409/422/503 envelopes, synchronous deployments, skipped
  targets and per-run logs.
- Acquisition is covered with an injected `http_get` (no network): pinned
  baseline, absent/verify status, confirmation refusal, store+verify+manifest,
  hash mismatch and wrong-length rejection with nothing left behind, idempotent
  re-download, replacement of a corrupted file, module-ZIP import, library dedupe
  by SHA-256, the LSPosed fallback resolution, and the 404 for an unknown key.
- The archive inspector and `module.prop`/badging parsers are pure functions over
  bytes and text.
- `DeviceProber` takes an injected command runner, so its logic is driven by
  scripted `adb` output; the production path passes the SDK `adb`.
- The GUI page is covered by its own Vitest/MSW suite
  (`ui/src/test/pages-instrumentation.test.tsx`, 14 tests): separate
  installed/enabled/active states with the reboot requirement, a failed import
  surfacing the API error instead of a success toast, built-in profiles offering
  duplicate rather than edit/delete, skipped targets rendered separately from
  failed ones with the run log, the Toolchain panel's consent dialog,
  discarded-download failure and LSPosed module import, and the profile editor's
  `complete_magisk_setup`/Xposed step fields. It mocks HTTP, so it is not a
  live-emulator test.

What the tests do **not** cover: real `adb`, the network path of acquisition, a
real Magisk installation, LSPosed, Zygisk or any live emulator. Passing tests are
not evidence that a deployment works on a device; the measured evidence below
predates this subsystem (except where it is labelled live) and the unverified list
is honest about that.

### Live-verified in this change

Measured through the running daemon on this host on 2026-11-27 (details in
[what was verified live](#what-was-verified-live)):

- **API 34 full stack**: Magisk 26.1 with a completed Additional Setup (four
  unprompted taps), Zygisk and LSPosed 1.9.2 installed and activated (`pidof lspd`
  plus the `ZygiskCompanion`/`binder received` logcat lines), and the LSPosed
  manager installed from the module's `manager.apk` — its Overview reported
  **Activated**. `magisk --install-module` exits 0 only after setup; before it,
  the CLI prints `Incomplete Magisk install`.
- **LSPosed ZIP acquisition**: pinned 1.9.2 artifact downloaded, size and SHA-256
  verified, `manifest.json` written at `2026-11-27T21:22:44Z`, imported as
  `mod_0ebc6bcb465d1c4b` (`source: "official-release"`). Acquisition did not
  install it.
- **API 25 capability scan**: `root_implementation=adb_root`, `magisk=false`, with
  the "Magisk/Zygisk/LSPosed need Magisk root, not adb root" blocker.
- **Module import**: a crafted Zygisk ZIP imported, classified `zygisk_zip`, stored
  content-addressed.
- **Not live yet**: a complete profile deployment to a device, Xposed APK install
  and the Xposed DB steps end-to-end, enable/disable markers, cancellation, and the
  GUI against a real daemon.

### Verified on a real emulator (by hand, before this subsystem)

These are measurements from [rooted-images.md](rooted-images.md),
[oobe-api25.md](oobe-api25.md) and
[live-acceptance-2026-11-27.md](live-acceptance-2026-11-27.md), not results of the
new service:

- `adb root` works on API 25 `google_apis;x86` and API 34 `google_apis;x86_64`;
  it is refused by Play Store images.
- `-writable-system` + `adb root` + `adb remount` gives a writable `/system`
  immediately on API 25, and on API 34 only after the overlayfs remount and a
  reboot. Writes live in the AVD's `system.img.qcow2` and are invisible on a plain
  boot.
- Magisk 26.1 on API 25 via rootAVD's ramdisk patch, followed by the app's
  Additional Setup, produces a working Magisk (`magisk -V` → `26100`, `su -c id`
  in a Magisk context) and module scripts then run (`resetprop` from
  `post-fs-data.sh` was observed).
- Modern stock images (API 33+) ship no Setup Wizard and no Magisk; Play Store
  images are user builds (`ro.debuggable=0`) and are not rootable this way.

### Not verified

- **A complete profile deployment end-to-end**: the individual components above
  were measured, but no full profile run (install Magisk → setup → Zygisk →
  LSPosed → module → verify) was exercised start-to-finish on a live AVD.
  Enable/disable markers, cancellation and the GUI against a real daemon are also
  unexercised.
- **Xposed APK install + DB steps end-to-end.** The LSPosed database schema, the
  `modules`/`scope` read path and the probe inventory were measured on API 34;
  both DB-write steps are unit-tested against a scripted device (including package
  validation), but they were not run against a real device as a deployment.
- Zygisk on API 25. Magisk 26.1 is installed there, but Zygisk was not enabled or
  checked.
- Magisk **v31.1** behaviour. Upstream v31.1 (2026-10-25) raises the minimum to
  Android 7.0, removes interactive `su` prompts (superuser access is granted ahead
  of time in the app) and removes app hiding
  ([release notes](https://github.com/topjohnwu/Magisk/releases/tag/v31.1)). The
  verified baseline here is 26.1; do not assume `su -c` prompts or policies behave
  the same on 31.x.

### Not implemented

- **Installing the LSPosed manager.** `lspd` does not install it; the operator (or
  a future step) must `pm install -r
  /data/adb/modules/zygisk_lsposed/manager.apk`. The API has no step for this.
- **A supported (upstream) API for Xposed activation/scope.** The DB-write steps
  are best-effort against LSPosed's internal schema; the manager is authoritative.
- **Signature or publisher verification** of imported artifacts.
- **Automatic reboot after Zygisk/LSPosed/module changes.** Only
  `complete_magisk_setup` waits for the Magisk app's own reboot; everything else
  reports `reboot_required`.
- **Run recovery** after a daemon restart.
- **Module updates** (importing a newer version creates a new record; there is no
  update/upgrade relationship).
- **Snapshots.** The subsystem does not disable, save or reason about emulator
  snapshots; a restored snapshot can silently undo a change (see below).

## Troubleshooting

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| `root_implementation` is `none` | user build (Play Store image), or `adb root` was not attempted | `getprop ro.build.type`, `getprop ro.debuggable`, `adb shell id` | use a `google_apis`/`default` userdebug image; Play images cannot be rooted this way |
| Root is `adb_root` but Magisk steps say "requires Magisk root" | `adb root` is host-side only; there is no `su` manager | `adb shell su -c id` | install Magisk with your own toolchain, or use a Magisk-patched image |
| Magisk root works but no module runs; magiskd logs `/data/adb is not present, abort` | the ramdisk patch is in place but "Additional Setup" never completed, so `/data/adb/magisk` is absent | `adb shell su -c "ls /data/adb/magisk"` | acquire the pinned `magisk` artifact and deploy `complete_magisk_setup`, or open the Magisk app and confirm the dialog ([oobe-api25.md](oobe-api25.md)) |
| `magisk --install-module` fails with `Incomplete Magisk install` | same cause: module installs need the completed environment | `/data/adb/magisk` absent | run `complete_magisk_setup` before installing modules |
| `complete_magisk_setup` fails "did not complete unattended" | UI automation missed a dialog (locale/UI change, `uiautomator` unavailable), or the app never rebooted | screenshot/`uiautomator dump` on the device | open `com.topjohnwu.magisk/.ui.MainActivity` and confirm "Requires Additional Setup" manually, then re-run verify |
| `complete_magisk_setup` fails "the pinned Magisk APK has not been acquired" | acquisition has not stored the verified APK | `GET /instrumentation/toolchain` shows `present: false` for `magisk` | `avdfleet instrumentation toolchain acquire magisk --confirm`, or the GUI Toolchain panel |
| Step succeeded but the component is not active | Zygisk/LSPosed/module changes apply on the next boot; only `complete_magisk_setup` waits for the Magisk app's reboot | `reboot_required` on the snapshot | reboot the AVD (instance restart) and re-run `verify` |
| A change disappeared after a reboot | plain boot without `-writable-system`, or the AVD was recreated/deleted | was the AVD booted with `-writable-system`? does `<avd>/system.img.qcow2` still exist? | boot with the flag; keep the AVD; remember `adb root`/`adb remount` are per-boot |
| Everything looks rolled back after starting the AVD | a Quick Boot snapshot was restored; a snapshot restores the device state (and can bypass a patched ramdisk) | boot with `-no-snapshot` and re-check | cold boot; do not use snapshots for instrumentation work ([emulator snapshots](https://developer.android.com/studio/run/emulator-snapshots)) |
| `adb root` says "adbd cannot run as root in production builds" | Play Store (`google_apis_playstore`) image: `ro.build.type=user`, `ro.debuggable=0` | `getprop ro.build.type` | use a userdebug image; there is no supported root path on that image |
| Zygisk is "enabled" but not "active" | the persisted setting is on but the reboot that starts `zygiskd` has not happened | `ps -A \| grep zygiskd`, `ls $(magisk --path)/.magisk/zygisk` | reboot; then re-scan. Zygisk is off by default (no `settings` row) |
| LSPosed is installed but not active | its daemon did not start, or the reboot has not happened | `pidof lspd`, logcat for `ZygiskCompanion`/`binder received` | reboot; check the Magisk log and Zygisk state |
| LSPosed is active but the manager is missing | `lspd` does not install the manager; logcat says `manager is not installed` | `pm list packages \| grep org.lsposed.manager` | `pm install -r /data/adb/modules/zygisk_lsposed/manager.apk`; then its Overview should read Activated |
| An Xposed APK is installed but its hook does nothing | installing is not activating | `xposed_modules` from a scan (enabled? scope includes the target?), or the LSPosed manager | enable it and set scope in the manager, or use the DB steps and reboot; then confirm in the manager |
| `xposed_modules` is empty with a note "no sqlite3 on the device" | LSPosed's inventory is read with `sqlite3`; the image lacks it | note on the snapshot | use an image with `sqlite3` (API 34 ships `/system/bin/sqlite3`), or inspect modules/scope in the LSPosed manager |
| A copied `modules_config.db` reads as empty | the database is WAL-mode; the data is mostly in `-wal` until checkpoint | file sizes (db ≈4 KB, `-wal` ≈99 KB) | copy `db` + `-wal` + `-shm`, or query on-device with `sqlite3` |
| `install_lsposed` fails "requires Android API 27 or newer" | LSPosed does not support API < 27 (API 25 is Android 7.1) | `getprop ro.build.version.sdk` | target API ≥ 27; API 25 cannot run LSPosed |
| `set_xposed_scope` returns `unsupported` for a module | LSPosed has not registered the module package | APK installed? manager opened once? | install the APK and open the LSPosed manager once, then retry |
| `enable_xposed_module` reports success but the module stays disabled | the `UPDATE` matched no row (module unknown to LSPosed); the step does not count rows | `xposed_modules` / manager | register the module first; confirm in the manager after the reboot |
| `set_xposed_scope` fails with "needs a non-empty 'apps' list" | the step has no `params.apps` | profile JSON | add `{"apps": ["com.example.target"]}` (and optionally `"replace": false`) |
| Import fails with "not a ZIP/APK archive" or "neither an APK nor a Magisk module" | wrong file, or a Magisk module without `module.prop` at its root | `unzip -l FILE` | import the real module ZIP/APK; repack with `module.prop` at the root |
| Import fails with 409 "module already imported" | the same bytes are already in the library (SHA-256 unique) | `GET /instrumentation/modules` | reuse the existing record id |
| Deploy reports `skipped` for an AVD | the instance is not running | `avdfleet instances list` | start it, then re-deploy |
| `503` from an instrumentation route | the service is unavailable in this build | `/openapi.json` contains the routes? | run a build with the subsystem; check the daemon log |
| `GET .../toolchain` returns `"available": false` | this build has no acquisition store configured | the Toolchain panel in the GUI shows it too | use a build with the instrumentation container wiring |
| Acquire returns `409 "requires explicit confirmation"` | the request omitted `confirm: true` (CLI: `--confirm`; the GUI asks first) | response hint names the pinned version and GPL-3.0 | resend with `{"confirm": true}` / `--confirm` |
| Acquire returns `422` size or hash mismatch | the upstream asset changed, or the download was corrupted/tampered with | response message names expected vs actual | nothing was stored; re-check the pinned release or import the file manually |
| Acquire returns `404 "no pinned artifact named ..."` | unknown key | keys are `magisk` and `lsposed` (case-insensitive) | `toolchain list` shows the keys |
| LSPosed step fails with "no LSPosed is available in the module library" | the library has no `lsposed_zip` | `GET /instrumentation/modules?kind=lsposed_zip` | `toolchain acquire lsposed --confirm` (or the GUI Toolchain panel), or import the ZIP manually; the built-in LSPosed profile then resolves the newest one |
| Xposed Instrumentation profile fails with "the module step needs a module from the library" | `install_module` accepts several kinds, so it has no safe fallback | — | clone the profile (Profiles tab or `POST .../clone`) and bind `module_id` on its install step |
| Deploy seems stuck after the Magisk app reboots | `complete_magisk_setup` is waiting (up to 300 s) for `sys.boot_completed` and then for `/data/adb/magisk` | `adb shell getprop sys.boot_completed` | wait; if it exceeds 300 s the step fails — open the Magisk app and finish the dialog |
| `su -c id` returns `context=u:r:su:s0`, not `u:r:magisk:s0` | that is the userdebug `su`/`adb root`, not Magisk | context field | Magisk is not installed; use `install_magisk` + `complete_magisk_setup` on a patched ramdisk |
| `install_magisk` fails with "no Magisk installer is configured" | `AVDFLEET_MAGISK_INSTALLER` is unset | `echo $env:AVDFLEET_MAGISK_INSTALLER` | set it before starting the daemon; see [the installer section](#external-toolchain-avdfleet_magisk_installer) |

### Emulator "Detected ADB" modal on every launch

A modal **Detected ADB** ("The ADB binary found at `…\platform-tools\adb.exe` is obsolete
and has serious performance problems…"), with a **Never show this again.** checkbox and an
OK button, blocks the emulator window until it is dismissed. It is a false positive, not an
out-of-date install: emulator 37.3.10 decides "obsolete" from the protocol version it parses
out of `adb version`, and platform-tools **37.0.3** is the newest published release
(`adb version` = 1.0.41 → protocol 41), so there is nothing newer to update to. Confirmed
against a stub `adb` reporting `1.0.20` (protocol 20): the dialog appears naming that path,
and stops appearing once the setting below is set.

The checkbox is a per-user Qt (QSettings) value, **not** a file under `%USERPROFILE%\.android\`:
REG_SZ `showAdbWarning` under `HKEY_CURRENT_USER\Software\Android Open Source Project\Emulator`
(absent = default, enabled). Back the key up, then set it:

```powershell
reg export "HKCU\Software\Android Open Source Project" "$env:TEMP\aosp-emulator.reg" /y
New-ItemProperty -Path 'HKCU:\Software\Android Open Source Project\Emulator' `
  -Name showAdbWarning -Value 'false' -PropertyType String -Force
```

Manual equivalent: tick "Never show this again." and press OK once — that writes the same
value. Extended Controls (…) → Settings → General → "Use detected ADB location" is a
different setting (`set/autoFindAdb`) and does not silence this dialog.

AVD Fleet could pre-seed `showAdbWarning=false` on a host before launching its emulators, so
the operator never meets the dialog.

## Related documentation

- [instrumentation-compatibility.md](instrumentation-compatibility.md): per-image
  matrix, LSPosed release/API table, format-vs-installer table.
- [rooted-images.md](rooted-images.md): `adb root`, `-writable-system`, overlayfs,
  qcow2 persistence and SELinux measurements.
- [oobe-api25.md](oobe-api25.md): the verified Magisk 26.1 + rootAVD recipe and
  the Magisk environment trap.
- [api.md](api.md), [cli.md](cli.md), [security.md](security.md),
  [troubleshooting.md](troubleshooting.md).
- Upstream: [Magisk](https://github.com/topjohnwu/Magisk),
  [Magisk installation](https://topjohnwu.github.io/Magisk/install.html),
  [Magisk developer guides](https://topjohnwu.github.io/Magisk/guides.html),
  [Magisk tools](https://topjohnwu.github.io/Magisk/tools.html),
  [LSPosed](https://github.com/LSPosed/LSPosed),
  [XposedBridge](https://github.com/rovo89/XposedBridge),
  [rootAVD](https://github.com/newbit1/rootAVD),
  [Android emulator](https://developer.android.com/studio/run/emulator),
  [emulator command line](https://developer.android.com/studio/run/emulator-commandline),
  [adb manual](https://android.googlesource.com/platform/packages/modules/adb/+/refs/heads/master/docs/user/adb.1.md).
