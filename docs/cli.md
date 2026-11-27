# CLI reference

The `avdfleet` CLI mirrors every product capability. It is built with Typer and
has machine-readable `--json` output; secrets (proxy passwords, API tokens) are
never printed.

## Global options

```text
avdfleet [--config PATH] [--json] [--verbose] [--quiet] [--version] COMMAND ...
```

| Option | Meaning |
|---|---|
| `--config PATH` | Explicit `config.toml` (default: `AVDFLEET_CONFIG_FILE`, then `./avdfleet.toml`, then the user config dir). |
| `--json` | Machine-readable output. Every command also accepts `--json` after the command name. |
| `--verbose` | Verbose diagnostics (secrets still redacted). |
| `--quiet` | Only errors and essential result output. |
| `--version` | Print `avdfleet 0.1.0` and exit `0`. |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Generic failure (failed check, partially failed operation) |
| `2` | Usage error (bad arguments, invalid input document) |
| `3` | Environment/blocked (missing tooling, unavailable service) |
| `4` | Not found (image/template/instance/proxy/run…) |
| `5` | Conflict (already running, capacity exceeded, duplicate name) |
| `6` | Reboot required (host must reboot; not during `install`) |
| `3010` | Reboot required during `install`/`repair` (Windows convention; bootstrap reboots and resumes) |

Expected errors print one redacted message plus an optional hint; stack traces
are shown only with `--verbose`. `--json` errors use
`{"error": {...}, "ok": false, "exit_code": N}` and are redacted.

## Commands

### Installer

| Command | Purpose |
|---|---|
| `install` | Install/repair the complete Android toolchain (idempotent, `--dry-run`). Flags: `--accept-android-sdk-licenses`, `-y/--yes`, `--dry-run`, `--repair`, `--resume`, `--no-appium`, `--no-studio`, `--no-scrcpy`, `--sdk-root PATH`, `--avd-home PATH`, `--images LIST`, `--proxies FILE`, `--download-proxy URL`, `--only ID`, `--platform-provider NAME`, `--json`. |
| `repair` | Re-check and repair an existing installation (`install --repair`); `-y`, `--dry-run`, `--accept-android-sdk-licenses`, `--sdk-root`. |
| `uninstall` | Remove installer-managed files and environment changes; `--dry-run`, `--keep-data`, `-y`. |
| `status` | Installer status, host facts, SDK/data locations, manifest summary. |

See [installer.md](installer.md).

### Host checks

| Command | Purpose |
|---|---|
| `doctor` | Run environment checks (`--slow` adds version/port/clock checks); `--fix CHECK_ID` (repeatable) applies a safe fix, `--dry-run` previews it. |

### Daemon

| Command | Purpose |
|---|---|
| `serve` | Run the REST + WebSocket + GUI daemon; `--host`, `--port`, `--no-open`, `--portable`, `--allow-remote`, `--tls-cert FILE`, `--tls-key FILE`, `--trusted-host HOST` (repeatable), `--trusted-origin ORIGIN` (repeatable). |
| `gui` | Start the daemon if needed and open the web GUI; `--host`, `--port`, `--no-browser`, `--no-auto-auth`, `--json`. By default it opens a one-time sign-in link that stores the daemon token in browser local storage, so no copy/paste is needed; the token is never printed or placed in the URL. |

Remote serving is fail-closed:

- `--allow-remote` is required for any non-loopback bind, and the daemon refuses to
  start remotely when `auth_required = false` (use an SSH tunnel or an authenticating
  reverse proxy for unauthenticated lab setups).
- A concrete `--host` address is trusted automatically for Host-header validation and
  added to the WebSocket/CORS origin allowlist for that exact scheme and port.
- `--host 0.0.0.0` / `::` requires at least one explicit `--trusted-host` (otherwise
  every Host header would be accepted). Add `--trusted-origin` when the browser
  reaches the GUI through a reverse proxy or different hostname.
- The loopback aliases and the actual `--port` are added automatically on every bind,
  so a custom port no longer breaks the GUI WebSocket origin check.

### Images and OOBE

| Command | Purpose |
|---|---|
| `images list` | List catalogue/installed images; `-i/--installed`, `--no-remote`, `--api-level`, `--abi`, `--tag`. |
| `images get IMAGE_ID` | Show one image. |
| `images download IMAGE_ID` | Download/install an image through the SDK package manager; `--dry-run`. |
| `images import PATH` | Import a custom image directory; `--name`, `--id`, `--api-level`, `--abi`, `--force`. |
| `images refresh` | Refresh the remote catalogue (`--timeout`). |
| `oobe matrix` | OOBE matrix (checked-in + local). |
| `oobe verify IMAGE_ID` | Boot a temporary AVD and verify the Setup Wizard; `--timeout`, `--keep-avd`. |
| `oobe reset AVD` | Reset an instance to OOBE; `--strategy snapshot\|wipe\|package\|reboot`, `--verify/--no-verify`. |

See [oobe.md](oobe.md).

### AVDs and instances

| Command | Purpose |
|---|---|
| `avd list` | Deployed AVDs with live state. |
| `avd create NAME --template ID` | Create one instance (`--ram` override). |
| `avd delete NAME` | Delete an instance (`--keep-files`). |
| `avd clone SOURCE --name NAME` | Clone an instance. |
| `avd deploy TEMPLATE --count N --prefix P` | Bulk-deploy from one template (capacity checked, 1–64). |
| `avd config NAME --set k=v [--unset k] [--dry-run]` | Typed `config.ini` edits. |
| `avd export [-o FILE]` / `avd import FILE` | YAML fleet round-trip. |
| `avd devices` | Device profiles known to the tooling. |
| `avd templates list\|create\|delete` | Template CRUD (`create` options: `--name`, `--image`, `--id`, `--device`, `--ram`, `--heap`, `--cpu`, `--gpu`, `--headless/--windowed`, `--proxy`, `--description`, `--tag`). |
| `instances list\|get NAME` | Instance state. |
| `instances start NAME` | Start; `--proxy ID`, `--headless/--windowed`, `--dry-run`. |
| `instances stop NAME` | Stop; `--force`, `--timeout`. |
| `instances restart NAME` | Restart (`--proxy`). |
| `instances wait-boot NAME` | Wait for `sys.boot_completed`; `--timeout`. |
| `instances reset-oobe NAME` | Reset to OOBE; `--strategy`, `--verify/--no-verify`. |
| `instances screenshot NAME` | PNG screenshot; `-o/--output`. |
| `instances config NAME` | Typed `config.ini` edits. |
| `instances clone NAME --name NEW` | Clone. |
| `instances delete NAME` | Delete (refuses while running; `--keep-files`). |
| `instances capacity` | RAM/ports/CPU capacity report. |
| `instances snapshots list\|save\|load\|delete` | Snapshot management (`save … --golden`). |

### Proxies

| Command | Purpose |
|---|---|
| `proxies list` | Proxies with masked credentials (`--all` includes disabled). |
| `proxies add URL` | Add one proxy (`--name`, `--tag`); the password goes to the vault. |
| `proxies import FILE` | URLs one per line, duplicates skipped (`--source`). |
| `proxies check ID` | Host health check (`--timeout`). |
| `proxies check-all` | Health-check every proxy. |
| `proxies delete ID` | Delete proxy and vaulted secret. |
| `proxies verify ID` | Verify from the host; `-g/--guest AVD`, `--token-url`, `--timeout`. |
| `proxies assign AVD ID` / `unassign AVD` | One proxy per instance. |
| `proxies auto-assign` | Assign across instances (`--instances`, `--strategy round_robin\|random\|healthiest`). |
| `proxies bridges` | Active local bridges. |

See [proxy.md](proxy.md).

### Scripts

| Command | Purpose |
|---|---|
| `scripts list` | Definitions incl. built-ins. |
| `scripts create` | Create/update; `--name`, `--type`, `--content`, `--file`, `--description`, `--timeout`, `--id`. |
| `scripts delete ID` | Delete a definition. |
| `scripts run ID` | Fan-out run; `--instances`, `--concurrency`, `--timeout`, `--detach`, `--dry-run`. |
| `scripts runs list\|status\|cancel` | Inspect/cancel runs (`--limit`). |

See [scripting.md](scripting.md).

### Appium and screen

| Command | Purpose |
|---|---|
| `appium status` / `start [--port]` / `stop` | Server lifecycle. |
| `appium capabilities AVD` | UiAutomator2 capabilities JSON. |
| `appium cleanup AVD` | Remove adb forwards/session state. |
| `screen capture AVD [-o FILE]` | Capture one PNG (gRPC → adb → cache). |
| `screen tap AVD X Y`, `swipe AVD X1 Y1 X2 Y2 [--duration]`, `text AVD TEXT`, `key AVD KEYCODE` | Input injection. |

### Root and instrumentation

| Command | Purpose |
|---|---|
| `instrumentation scan AVD [AVD ...]` | Probe root implementation, Magisk/Zygisk/LSPosed state, the Magisk module inventory, Xposed modules + scope (when LSPosed and `sqlite3` are present) and blockers; `--json`. |
| `instrumentation modules list [--kind KIND]` | The central module library (Xposed APK / Magisk ZIP / Zygisk ZIP / LSPosed ZIP). |
| `instrumentation modules import PATH` / `--url URL` | Import a package; SHA-256 dedupe returns a conflict for the same bytes. |
| `instrumentation modules delete MODULE_ID` | Remove a library record and its stored copy. |
| `instrumentation toolchain list` | Pinned Magisk 26.1 APK and LSPosed 1.9.2 ZIP: version, kind, downloaded/verified state, provenance manifest; `--json`. |
| `instrumentation toolchain acquire magisk\|lsposed --confirm` | Download, size/SHA-256 verify and record one pinned release; the LSPosed ZIP is imported into the library. Without `--confirm` it refuses (the API returns `409`). |
| `instrumentation profiles list` | Built-in and user profiles. |
| `instrumentation profiles delete PROFILE_ID` | Delete a user profile (built-ins are protected). |
| `instrumentation deploy PROFILE_ID --target AVD [...]` | Deploy to one or more AVDs; `--label`, `--wait/--no-wait`, `--json`. |
| `instrumentation runs [--limit N]` | Recent deployments. |
| `instrumentation logs RUN_ID` | Persisted lines for one deployment. |

There is no CLI profile editor or clone command (the GUI's **Duplicate** action
and `POST /api/v1/instrumentation/profiles/{id}/clone` cover copying). Xposed
enable/scope can be expressed as the profile steps `enable_xposed_module` and
`set_xposed_scope`; the LSPosed manager remains the supported interface. Built-in
profiles now include `complete_magisk_setup`, so `instrumentation deploy` may
install the Magisk APK and wait while the Magisk app reboots the AVD (up to 300 s)
before continuing. See [instrumentation.md](instrumentation.md).

### Fleet, bundle and portable

| Command | Purpose |
|---|---|
| `fleet validate -f FILE` | Validate a `fleet.yaml`. |
| `plan -f FILE [--prune]` | Top-level alias for `fleet plan` (read-only diff). |
| `apply -f FILE [--prune] [--dry-run]` | Top-level alias for `fleet apply` (idempotent reconcile). |
| `fleet plan -f FILE [--prune]` | Read-only diff. |
| `fleet apply -f FILE [--prune] [--dry-run]` | Idempotent reconcile. |
| `bundle` | Build a portable bundle; `--offline`, `-o/--output`, `--dry-run`, `--force`. |
| `portable checks [--root]` | Preflight checks (filesystem, noexec, space…). |
| `portable verify [ROOT]` | Verify bundle checksums. |
| `portable relocate [ROOT] [--dry-run]` | Rewrite paths after a move. |
| `portable status` | Bundle status/manifest/leftovers. |

See [fleetspec.md](fleetspec.md) and [portable.md](portable.md).

### Acceptance self-test

| Command | Purpose |
|---|---|
| `selftest` | Machine-checkable acceptance self-test: host checks always; `--e2e` runs the BRIEF §7 sequence on real tools; `--fakes` uses the development fakes and labels the report `evidence: fakes`; `--fast`, `--keep`, `--require-acceleration`, `--count N`, `--custom-image DIR`, `--json`. |

Exit code is non-zero only for failed checks (or a missing required accelerator
with `--require-acceleration` → `3`); on a host without WHPX/KVM/HVF the
real-emulator items are reported as `SKIP`.

## Examples

```bash
# First install and health
avdfleet install --accept-android-sdk-licenses --yes
avdfleet doctor --json

# Fleet from YAML
export AVDFLEET_PROXY_1_PASSWORD='…'
avdfleet fleet validate -f fleet.yaml
avdfleet fleet apply -f fleet.yaml

# Deploy and start five instances from a template
avdfleet avd templates create --name pixel --image "system-images;android-35;google_apis_playstore;x86_64"
avdfleet avd deploy pixel --count 5
avdfleet instances list --json | head

# Proxy evidence + script fan-out
avdfleet proxies verify proxy-1 --guest avd-01 --token-url https://example.com/token
avdfleet scripts run health --instances avd-01,avd-02 --concurrency 2

# GUI
avdfleet serve --port 8765

# Acceptance evidence (real host) / fakes-backed here
avdfleet selftest --e2e --json
avdfleet selftest --fakes --fast --json
```

## Environment

All `AVDFLEET_*` configuration variables apply (`AVDFLEET_CONFIG_FILE`,
`AVDFLEET_DATA_DIR`, `AVDFLEET_SDK_ROOT`, `AVDFLEET_API_PORT`, …). Extra CLI
variables: `AVDFLEET_CONTAINER=module:factory` (inject a service container for
tests/embedding), `AVDFLEET_TOKEN` (daemon token for API clients),
`AVDFLEET_VAULT_PASSPHRASE`, `AVDFLEET_CLI_DEBUG=1` (show tracebacks),
`AVDFLEET_SELFTEST_FAKES=1`, `AVDFLEET_SELFTEST_PROXIES`,
`AVDFLEET_SELFTEST_TOKEN_URL`. The bootstrap adds its own variables — see
[bootstrap/README.md](../bootstrap/README.md).

The complete configuration list is in the `Config` model
(`src/avdfleet/core/config.py`) and the data locations table is repeated in each
quickstart.
