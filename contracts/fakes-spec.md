# Fakes specification (frozen contract for `tests/fakes`)

Every test in this repository must run without an Android SDK, emulator, JDK, Node,
Appium or hardware virtualisation. `tests/fakes` provides executable fakes and pytest
fixtures that behave like the real tools at the process boundary. Services must remain
backend-agnostic: they discover tools through `HostProvider`/`SdkBackend` and execute
processes, never import the fakes.

## 1. Process model

- `tests/fakes/fake_cli.py` is a single Python entry point dispatched on the first
  argument or on `Path(sys.argv[0]).stem`:
  `adb`, `emulator`, `sdkmanager`, `avdmanager`, `android`, `appium`, `node`, `npm`,
  `uv`, `java`, `scrcpy`, `python` (fake only).
- `tests/fakes/runtime.py` creates a temporary `bin/` directory containing launch
  shims for the current OS:
  - Windows: `<tool>.cmd` → `@"<sys.executable>" "<fake_cli.py>" <tool> %*`
  - POSIX: `<tool>` → `#!/bin/sh\nexec "<sys.executable>" "<fake_cli.py>" <tool> "$@"`,
    mode `0o755`.
- The fixture prepends this directory to `PATH` and sets the Android variables below.
  Tests never rely on global tools.
- Executables write all state to `$AVDFLEET_FAKE_HOME/state.json` and append every
  invocation to `$AVDFLEET_FAKE_HOME/events.jsonl`:
  `{"ts": "...", "tool": "adb", "argv": [...], "cwd": "..."}`.
- State access is atomic: read-modify-write under a lock file with a short retry loop
  (Windows `msvcrt.locking` / POSIX `fcntl.flock`; fall back to an exclusive-create
  lock). Parallel script fan-out must not corrupt state.
- Exit codes: 0 success; 1 unexpected tool failure; 2 usage error. Stdout/stderr are
  deterministic (no random ordering); timestamps are not asserted.

## 2. Environment contract

| Variable | Meaning |
|---|---|
| `AVDFLEET_FAKE_HOME` | State directory (required). |
| `AVDFLEET_FAKE_SDK_ROOT` | Fake SDK root (created by fixture). |
| `AVDFLEET_FAKE_AVD_HOME` | Fake AVD home (created by fixture). |
| `AVDFLEET_FAKE_PYTHON` | Absolute Python used by shims. |
| `AVDFLEET_FAKE_BOOT_SECONDS` | Seconds from launch to boot completion (default `0.2`). |
| `AVDFLEET_FAKE_ACCEL` | `whpx`, `kvm`, `hvf`, `aehd` or `none`. |
| `AVDFLEET_FAKE_HOST_OS` | `windows`, `macos`, `linux` (overrides real detection). |
| `AVDFLEET_FAKE_ARCH` | `x86_64` or `arm64`. |
| `AVDFLEET_FAKE_NOW` | Optional fixed epoch seconds for deterministic uptimes. |
| `AVDFLEET_FAKE_FAIL` | Comma-separated tool/commands that must exit 1, e.g. `emulator`, `sdkmanager:install`. |
| `AVDFLEET_FAKE_SCRIPT_MAP` | JSON mapping adb shell commands to `{"stdout","stderr","exit_code"}`. |
| `AVDFLEET_FAKE_APPIUM_PORT` | Default port for fake appium (default `4723`). |
| `AVDFLEET_FAKE_TOKEN_URL` | URL used by guest egress verification (test listener). |
| `ANDROID_SDK_ROOT` / `ANDROID_HOME` | Point at the fake SDK root. |
| `ANDROID_AVD_HOME` | Point at the fake AVD home. |
| `JAVA_HOME` | `<fake sdk>/jdk`. |

## 3. State schema

```json
{
  "schema": 1,
  "sdk_root": "C:/tmp/fake-sdk",
  "avd_home": "C:/tmp/fake-avd",
  "installed": ["cmdline-tools;latest", "platform-tools", "emulator",
                "build-tools;35.0.0", "platforms;android-35",
                "system-images;android-35;google_apis_playstore;x86_64"],
  "licenses_accepted": ["android-sdk-license"],
  "avds": {
    "avd-01": {
      "display_name": "avd-01",
      "package": "system-images;android-35;google_apis_playstore;x86_64",
      "device": "pixel_7",
      "api": 35,
      "abi": "x86_64",
      "state": "running",
      "pid": 1234,
      "console_port": 5554,
      "adb_port": 5555,
      "grpc_port": 8554,
      "boot_completed": true,
      "boot_complete_at": 1764000000.0,
      "started_at": 1764000000.0,
      "proxy": "http://user:***@127.0.0.1:8081",
      "snapshot": "avdfleet-golden",
      "oobe": "setup",
      "activity": "com.google.android.setupwizard/.SetupWizardActivity",
      "config": {"hw.ramSize": "2048", "disk.dataPartition.size": "6G"},
      "forwards": [{"local": "tcp:8200", "remote": "tcp:8200"}]
    }
  },
  "appium": {"running": false, "port": null, "pid": null},
  "inputs": [{"instance": "avd-01", "kind": "tap", "x": 10, "y": 20}],
  "screenshots": {
    "avd-01": {"width": 1080, "height": 1920, "png_base64": "..."}
  },
  "commands": []
}
```

`oobe` is `"setup"` (first-boot wizard visible) or `"complete"`. A VHD booted from a
golden snapshot resets `oobe` to `"setup"`. `-wipe-data` resets `oobe` to `"setup"` and
clears user data.

## 4. Tool contracts

### 4.1 `sdkmanager`

- `--version` → `11.0.0`.
- `--sdk_root=<path>` and `--sdk_root <path>` accepted anywhere.
- `--list` → blocks `Installed packages:` and `Available Packages:` in the canonical
  table format, including at least the packages listed in §6.
- `--list_installed` → installed table only.
- `--install <id>...` (also `"<id>"` quoted) → echoes `Preparing "Install <id>".`,
  `Downloading...`, `Unzipping...`, `Installing...`, then updates `installed`; unknown
  IDs cause `Warning: Failed to find package <id>` and exit 1.
- `--uninstall <id>...` removes from `installed`.
- `--licenses` prints the license review prompt then reads stdin. If it sees a line
  starting with `y` it records `android-sdk-license` (and `android-sdk-preview-license`)
  and exits 0. On EOF without `y` it exits 1. `--licenses --include_obsolete` accepted.
- `--channel=0|1|2|3` accepted; version strings differ (`stable`, `beta`, `dev`, `canary`).
- `--verbose` adds log lines.
- Failures from `AVDFLEET_FAKE_FAIL=sdkmanager:install` emit `Warning: ...` to stderr and
  exit 1.

### 4.2 `avdmanager`

- `list avd`:
  ```
  Available Android Virtual Devices:
      Name: avd-01
      Path: <avd_home>/avd-01.avd
    Target: Google Play (Google Inc.)
    Based on: Android API 35 Tag/ABI: google_apis_playstore/x86_64
  ---------
  ```
  With none: `Available Android Virtual Devices:` and exit 0.
- `list device` → `id: 7 or "pixel_7"` / `Name: Pixel 7` blocks for at least
  `pixel_7`, `pixel_6`, `pixel_tablet`, `Nexus 5X`.
- `list target` → installed platforms.
- `create avd -n NAME -k PACKAGE [--device DEVICE] [--force] [--path PATH] [--sd-card PATH]`
  → requires PACKAGE in `installed` (else `Error: Package path is not valid.` exit 1).
  Creates `<ANDROID_AVD_HOME>/<NAME>.ini` pointing at `<NAME>.avd/` and a `config.ini`
  with `image.sysdir.1`, `tag.id`, `abi.type`, `hw.device.name`, `hw.ramSize`,
  `disk.dataPartition.size`, `PlayStore.enabled`, `fastboot.forceColdBoot=yes`.
  Duplicate without `--force` → exit 1.
- `delete avd -n NAME` removes both files and state; missing → exit 1.
- `move avd -n NAME -p NEWPATH` rewrites the `.ini`.
- `--silent` suppresses progress.

### 4.3 `adb`

- `start-server`/`kill-server`/`version` exit 0. `version` prints
  `Android Debug Bridge version 1.0.41` / `Version 35.0.2-...`.
- `devices [-l]` lists running and booting AVDs as
  `emulator-<console>\tdevice product:sdk_gphone64_x86_64 model:sdk_gphone64_x86_64 device:emu64x transport_id:1`.
- `-s emulator-<console>` selects an AVD by console port; `-e`/`-d` select the first.
- `wait-for-device`: polls for up to `AVDFLEET_FAKE_BOOT_SECONDS + 5`.
- `get-state`: `device` while booting/running; exit 1 otherwise.
- `shell getprop sys.boot_completed` → `1` once `now >= boot_complete_at`, else `0`.
- `shell getprop ro.build.version.sdk` → API; `ro.product.cpu.abi` → ABI;
  `ro.build.version.release` → e.g. `15`; `ro.serialno` → `emulator-<console>`;
  `ro.kernel.qemu` → `1`; unknown property → exit 0 with empty line.
- `shell dumpsys activity activities` includes `mResumedActivity` /
  `topResumedActivity` for the instance's current activity.
- `shell dumpsys window` includes `mCurrentFocus` with the activity.
- `shell am start -n <component>` sets activity and `oobe` accordingly; `am force-stop`,
  `am broadcast` accepted. `shell am start -a android.intent.action.MAIN` sets activity to
  the launcher when `oobe == complete`.
- `shell settings get secure user_setup_complete` → `1`/`0`.
- `shell pm list packages` → seeded packages (`com.android.settings`,
  `com.google.android.apps.nexuslauncher`, `com.android.launcher3`, plus optional
  `com.example.test`); `pm path <pkg>` → `package:/data/app/<pkg>/base.apk`.
- `shell input tap X Y`, `swipe`, `text`, `keyevent` append to `inputs`; exit 0.
- `shell screencap -p` and `exec-out screencap -p` write a valid PNG (default 1080x1920;
  configurable per state) to stdout. Never write log text to stdout on this path.
- `shell echo <text>` → prints `<text>`; `shell sh -c '<cmd>'` / `shell -c '<cmd>'` uses
  `AVDFLEET_FAKE_SCRIPT_MAP` when defined, otherwise echoes `<fake-adb-shell>: <cmd>`,
  honours `AVDFLEET_FAKE_SCRIPT_MAP` exit codes; `shell exit N` → exit N.
- `install [-r] path` → `Success`; `uninstall` → `Success`; `install` failure if
  `AVDFLEET_FAKE_FAIL=adb:install`.
- `forward tcp:L tcp:R` records, prints `L`; `forward --remove-all` clears.
  `reverse` likewise.
- `emu kill` stops the AVD (state `stopped`, pid cleared) and exits 0; `emu avd name`
  prints the AVD name.
- `logcat [-d]`: prints seeded log lines and exits; without `-d` streams a line per
  0.05s until killed.
- Unknown subcommand → exit 0 with a diagnostic on stderr (tests shouldn't depend on it).
- Every command returns within 5 seconds unless intentionally streaming.

### 4.4 `emulator`

- `-version` → `Android emulator version 35.1.4.0 (build_id 12345678)`.
- `-list-avds` → one name per line, exit 0.
- `-help` → usage, exit 0.
- Launch with `-avd NAME -port <even> [other flags]`:
  - missing AVD → `PANIC: Unknown AVD name [NAME]` and exit 1;
  - `-accel off` → accepted only with a warning in fake (real tool rejects; tests for the
    installer use this to assert our guard, not the fake);
  - writes state: `state="booting"`, `pid`, `console_port`, derives adb, records
    `-http-proxy`, `-gpu`, `-no-window`, `-grpc`, snapshot flags; prints
    `INFO: QEMU waiting for connection, starting emulator...` and `boot completed` after
    `AVDFLEET_FAKE_BOOT_SECONDS` (state becomes `running`, `boot_completed=true`,
    `boot_complete_at`). Sleeps until terminated.
  - on SIGTERM/SIGINT updates state to `stopped`, removes pid, exits 0.
  - `-wipe-data` resets `oobe="setup"`; `-snapshot <name>` sets `oobe="setup"` for
    golden snapshots; `-no-snapshot` accepted.
  - `-http-proxy` value is stored verbatim in state and **must not** be echoed to stdout
    (isolation tests assert credentials never appear in logs).
  - opens a minimal console TCP server on the console port: line protocol with `auth
    <token>` (token `<fake_home>/.emulator_console_auth_token`, contents `fake-token`),
    `avd name`, `kill`, `snapshot save <name>`, `snapshot load <name>`, `help`; `KO:`
    prefix on error.
  - writes a gRPC discovery file under `<fake_home>/grpc/<console>.ini` containing
    `grpc.port=<grpc>` and `grpc.token=fake-grpc-token`.
- If the AVD is already running, exit 1 with `ERROR: The emulator is already running`.

### 4.5 `android` (new Android CLI, S1a)

- `--version` → `android 1.0.0 (fake)`.
- `sdk list [--installed]` → same catalogue as sdkmanager.
- `sdk install <id>...` → delegates semantics of sdkmanager install.
- `sdk licenses` → accepts stdin like sdkmanager.
- `avd list` → JSON array of AVD objects when `--json` is passed; table otherwise.
- `avd create --name NAME --package PACKAGE --device DEVICE [--force]`.
- `avd delete --name NAME`.
- `emulator list` → JSON list of running AVDs.
- Unsupported subcommand → exit 2 with usage.

The fake must also be invocable as the real command name `android` when that name
shadows an installed binary (PATH order is the fixture's responsibility).

### 4.6 `appium`

- `--version` → `4.0.0`.
- `appium --port <p> --base-path /wd/hub --log-level info` starts a stdlib HTTP server
  (threaded) and stays alive; records `appium={running:true,port,pid}`.
- Endpoints: `GET /status` and `/wd/hub/status` →
  `{"value":{"ready":true,"build":{"version":"4.0.0"}}}`;
  `POST /wd/hub/session` → `{"value":{"sessionId":"fake-session-1","capabilities":...}}`;
  `DELETE /wd/hub/session/<id>` → `{"value":null}`; `GET /wd/hub/sessions` lists active
  sessions. CORS not required.
- On SIGTERM updates state and exits.
- `appium driver list --installed` → `uiautomator2@3.0.0`; `driver install` records.
- Fail start if port busy.

### 4.7 Utility fakes

- `node --version` → `v22.14.0`, `npm --version` → `10.9.2`, `npx` optional.
- `uv --version` → `uv 0.6.0`; `uv tool install <name>` exits 0 and records.
- `java -version` writes `openjdk version "21.0.6" 2025-01-21` to stderr and exits 0.
- `python --version` → current runtime version (only used when a service resolves
  `python` from PATH).
- `scrcpy --version` → `scrcpy 3.1`.

## 5. Pytest fixtures (`tests/fakes/conftest.py` or package fixtures)

- `fake_tools` (session-scoped factory): builds shims once per session; returns
  `FakeTools(path, state, env, seed, reset, write_script, add_listener)`.
- `fake_env` (function): sets `PATH`, `ANDROID_SDK_ROOT`, `ANDROID_HOME`,
  `ANDROID_AVD_HOME`, `JAVA_HOME`, all `AVDFLEET_FAKE_*`; seeds:
  - installed packages from §6;
  - images installed on disk under `<sdk>/system-images/...`;
  - 3 free-form AVDs `avd-01..03` (one stopped, one running, one booting) only when a
    test asks via `seed_avds()`;
  - 3 proxies? no — proxies are pure data and tests seed the store directly.
- `fake_state` (function): mutable helper with typed accessors:
  `installed()`, `avds()`, `avd(name)`, `set_oobe(name, "setup"|"complete")`,
  `add_avd(name, **kwargs)`, `add_image(...)`, `commands(tool=None)`.
- `fake_sdk` → `SdkPaths` rooted in the fake SDK.
- `fake_installer_context` → an isolated `Config`/`Paths` under `tmp_path` with fake
  tools on PATH.
- Session-scoped fakes must be safe under pytest-xdist if it is ever enabled; tests that
  mutate state call `fake_tools.reset()` (function autouse) to restore seed state.
- Tests asserting no emulator exist check `pytest.mark.emulator` skips. No test may
  require a real `adb` even if one is installed.

## 6. Seeded catalogue

SDK packages:
`cmdline-tools;latest`, `platform-tools`, `emulator`, `build-tools;35.0.0`,
`platforms;android-35`, `platforms;android-36`, `cmdline-tools;11.0`,
`system-images;android-35;google_apis_playstore;x86_64`,
`system-images;android-35;google_apis;x86_64`,
`system-images;android-36;google_apis_playstore;x86_64`,
`system-images;android-36;google_apis;x86_64`,
`system-images;android-35;aosp_atd;x86_64`,
`system-images;android-34;google_apis_playstore;x86_64`,
and the same set for `arm64-v8a` (used by macOS fake host).

Image metadata (API, tag, ABI, kind, Play):
- API 34/35/36 google_apis_playstore x86_64 & arm64-v8a
- API 35 google_apis and aosp_atd x86_64
- API 34 google_apis_playstore x86_64
`oobe_matrix.json` seed marks the Play Store 35/36 images `verified` with
`com.google.android.setupwizard/.SetupWizardActivity`, API 35 google_apis `unverified`,
and one API 33 image `failed` (use it to test warnings).

Device profiles: `pixel_7`, `pixel_6`, `pixel_tablet`, `Nexus 5X`, `pixel_9`.

## 7. Determinism and safety rules

- Fakes never touch the real `~/.android`, `ANDROID_SDK_ROOT`, or any network.
- The only network listener is the fake Appium HTTP server (loopback) and test proxies
  created by tests (loopback), if needed.
- No real credentials in fixtures; use `user1`/`pass1` etc.
- Output must not include the fake state file path in assertions-sensitive lines unless
  documented.
- Every fake logs its argv to `events.jsonl` for assertion on process boundaries (e.g.
  `-http-proxy` presence, capability flags).
- Exit code and stream contract above is normative: Wave 1 services are written against
  it, Wave 2 tests assert it.

## 8. Host detection overrides

`platform.PlatformDetector` (P1) must honour:
`AVDFLEET_FAKE_HOST_OS`, `AVDFLEET_FAKE_ARCH`, `AVDFLEET_FAKE_ACCEL`,
`AVDFLEET_FAKE_SDK_ROOT`, `ANDROID_SDK_ROOT`, `ANDROID_AVD_HOME`.
This lets the unit suite exercise Windows/macOS/Linux code paths on any host.
