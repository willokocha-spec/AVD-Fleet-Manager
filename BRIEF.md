# AVD Fleet — Build Brief

> Status: Wave 0 frozen source of truth.
> **Update:** the operator supplied the missing first part of the build prompt after
> Wave 0; it is preserved verbatim in [`BRIEF_ORIGINAL.md`](BRIEF_ORIGINAL.md)
> (sections 0–3.1). That text takes precedence over the earlier reconstruction below.
> Sections 1–3 below were reconstructed from the supplied build prompt (the pasted text
> began at section 4); where the original wording was unavailable, the requirement below
> is the normative interpretation used by all agents. Sections 4–9 are the supplied text.
> Any deviation must be recorded in `PLAN.md` → Decisions and in `DECISIONS.md`.

## 1. What we are building

**AVD Fleet (`avdfleet`)** is an operator product for running fleets of Android Virtual
Devices on a single accelerated host. It ships in two editions sharing one engine:

1. **Installed edition** — a pip/uv-installed Python daemon with a FastAPI REST + WebSocket
   API, a Typer CLI, and a React web GUI, plus a one-command bootstrap installer for
   Windows, macOS and Linux.
2. **Portable edition** — a self-contained folder (Python runtime, Android command-line
   tools, emulator, Appium, UI assets, state) that runs from a USB/removable disk with no
   admin rights and no system-wide Android tooling. It must survive relocation and
   drive-letter changes.

Core jobs: install and repair the Android toolchain; catalogue/download/import system
images; create/deploy/clone AVDs (up to **64 concurrent instances** on a large host);
isolate each instance's network egress behind its own authenticated proxy (HTTP/HTTPS or
SOCKS5); detect and reset AVDs to the Out-of-Box-Experience (OOBE); run adb-shell, host
Python and Appium scripts across instances with live output and cancellation; and expose all
of that through REST, CLI and GUI.

Explicitly **out of scope**: rooting/modifying Google Play images, bypassing Play
Integrity/SafetyNet, device-identifier/fingerprint spoofing, and automated Google account
creation. The product works with stock or user-supplied images only.

## 2. Product requirements

- **R-INSTALL**: On a fresh Windows 11, macOS (Apple Silicon) or Linux (Ubuntu LTS, Fedora,
  Rocky/RHEL, Arch) host with admin/sudo, one bootstrap command
  `install.sh|install.ps1 --accept-android-sdk-licenses --yes` installs everything
  unattended. Windows may reboot once and resume. A second run is a no-op. `avdfleet doctor`
  is green.
- **R-GUI**: Every acceptance workflow is possible from the GUI alone.
- **R-FLEET**: One template can deploy 5+ AVDs (capacity up to 64), each independently
  configurable; start/stop/restart/reset; snapshots; clone; bulk operations.
- **R-PROXY**: Per-instance proxies with authentication. The guest's egress must use only
  its assigned proxy. no credential may appear in logs, `ps` output or API responses.
- **R-OOBE**: Record which system images reliably boot to the Setup Wizard; verify it,
  reset to OOBE and time the golden-snapshot reset.
- **R-AUTOMATION**: adb-shell scripts, host Python scripts and Appium sessions across many
  instances in parallel, with live output, combined results, cancel and timeout.
- **R-CUSTOM-IMAGE**: Import a custom system image directory and boot an AVD from it.
- **R-FLEETSPEC**: `avdfleet apply -f fleet.yaml` rebuilds a fleet on a clean host.
- **R-PORTABLE**: `avdfleet bundle --offline` produces a self-contained bundle that opens
  the GUI offline on a fresh machine with no Android tooling, boots an AVD to OOBE and runs
  a script through a proxy; survives being moved/changed drive letter; writes nothing
  outside its root except documented leftovers; refuses FAT32 and detects noexec mounts.
- **R-QUALITY**: lint, type checks and unit/contract/integration/portable/UI tests green on
  Linux, macOS and Windows; real-emulator E2E wherever hardware allows.

## 3. Functional specification

### 3.1 Architecture

```
bootstrap -> uv tool install avdfleet -> avdfleet install (installer engine -> providers)
GUI (React/Vite) <-> FastAPI REST + WS <-> services <-> platform/proxy/oobe/... <-> host
CLI (Typer) ------^                       store (SQLite) and core (models/Protocols)
```

- **`core/`** has no imports from other avdfleet packages. Pydantic models, Protocols,
  error model, paths, config, port allocator, event bus, logging/redaction.
- **`platform/`** owns host specifics: installer engine/steps/providers, tooling backends
  (`sdk/androidcli` and `sdk/legacy` behind one interface), images, AVDs, runtime.
- **`proxy/`** owns pool parsing, encrypted vault, assignment strategies, the local bridge
  (HTTP/HTTPS/SOCKS5 upstreams), guest egress verification.
- **`oobe/`**, **`scripts/`**, **`appium/`**, **`screen/`** are self-contained services.
- **`portable/`** builds/relocates/checks bundles and portable Studio config.
- **`store/`** is the SQLite schema/repositories (WAL, forward migrations).
- **`api/`** exposes `/api/v1`; **`cli/`** mirrors every capability; **`fleetspec/`**
  reconciles YAML.
- `ui/src/pages` auto-registers pages with `import.meta.glob`.
- All services take dependencies via constructor/Protocols; no global singletons except the
  logging config. Every destructive operation supports a dry run.

### 3.2 Installer

- Detect OS/arch, host resources, hypervisor capability, existing SDK, package managers.
- Install: uv, Python (uv-managed) if needed, JDK/JBR, Android command-line tools,
  platform-tools, emulator, required SDK packages, licenses (only with explicit flag),
  environment variables, Android Studio config, Node, Appium 4 + UiAutomator2 driver,
  Appium Inspector, scrcpy.
- Acceleration: Windows WHPX (enable + reboot resume) with AEHD → WHPX migration; Linux
  KVM udev rules and group membership; macOS HVF; detect nested virtualisation and report.
- Step engine: ordered steps, idempotent, dry-run, checkpoint/resume after reboot,
  manifest under the data dir, `uninstall`/`repair`.
- Per-platform providers (`windows`, `macos`, `debian`, `fedora`, `arch`) with a common
  interface; macOS installs Command Line Tools before Homebrew.
- Bootstrap scripts are thin: detect shell/OS/arch → install uv → `uv tool install avdfleet`
  → `avdfleet install "$@"`. Never `curl | sh` unpinned pipelines without checksum
  verification where upstream publishes one.

### 3.3 Images and AVDs

- Catalogue stock Google APIs, Google Play and AOSP images per API/ABI/tag, with URLs,
  SHA-256, sizes, OOBE status from `oobe_matrix.json`.
- Download with resume + checksum; import custom image from a directory (validate
  `system.img`, build.prop/API/ABI metadata, register under a stable id).
- AVD templates (device profile, image, RAM/CPU/heap, GPU mode, proxy, scripts, tags).
  Create/edit/delete/clone, bulk deploy, `config.ini` editing with typed validation,
  YAML export/import.
- Runtime: launch with `-no-window -gpu <mode>` when headless, `-http-proxy` or the local
  bridge as required, unique ports; boot detection by `sys.boot_completed` plus package
  manager readiness; state machine; supervision and restart policy; capacity checks (RAM,
  ports, CPU) up to 64 instances; snapshots (save/load/list/delete).

### 3.4 Proxy isolation (gate G1)

- Accept proxy URLs/lines (`http://user:pass@host:port`, `socks5://...`), store secrets in
  an encrypted vault (OS keyring where available, passphrase-derived file vault fallback).
- Each instance gets exactly one assigned proxy; five isolated test listeners (HTTP and
  SOCKS5), each with its own credentials.
- Guest requests a URL containing its own token; each token must appear **only** on its
  assigned listener. The emulator must not leak around the proxy (DNS/UDP included; use the
  local bridge when direct `-http-proxy` coverage is insufficient).
- Redaction: proxy credentials and API tokens are redacted from logs, errors, API
  responses, `ps` command lines where possible, and stored state. G1 passes only when all
  evidence is recorded under `research/g1/`.

### 3.5 OOBE matrix

- For every candidate image (API levels supported by host ABI), boot a fresh AVD and record:
  whether the first-boot Setup Wizard activity appears, activity name, screenshot, boot
  duration, image id/checksum, and timestamp. `oobe_matrix.json` is the checked-in result;
  unknown images are marked `unverified`.
- Per-image reset strategy: snapshot restore (golden snapshot after first boot before OOBE
  completion), `-wipe-data`, and package/state cleanup fallbacks. Measure golden-snapshot
  reset time and report it.
- "Reset to OOBE" in GUI/API/CLI. Verify activity via `dumpsys activity`/uiautomator.

### 3.6 Scripts

Script types: adb shell (inline/remote), host Python, Appium (Python/JS) — plus built-in
library entries. Fan-out executor targets instances with concurrency limits, streams live
output per instance, aggregates results/exit codes, supports cancel and timeout, persists
runs in SQLite, exposes over WS. Hooks: before/after deploy, boot, reset.

### 3.7 Appium

Manage Appium 4 server lifecycle (start/stop/status), allocate unique ports per session
(server port, system port, MJPEG), build UiAutomator2 capabilities (deviceName, udid,
systemPort, noReset, chrome/chromedriver where applicable), clean adb forwards after runs,
support parallel sessions.

### 3.8 Screen

gRPC client for the emulator's screen-sharing service (discover endpoint via
`/tmp/android-*/emu-grpc` or per-instance ini files, auth token), frame streaming over
WebSocket, fallback to periodic `adb exec-out screencap`, input injection (tap/swipe/text/
key), and scrcpy fallback for local display. Must degrade cleanly when gRPC is unavailable.

### 3.9 API and CLI

- REST under `/api/v1`, OpenAPI 3.1 contract checked in at `contracts/openapi.yaml`; bearer
  token auth (loopback-only by default, token file with 0600 ACL), static serving of the
  built UI, consistent error envelope, operation ids. WebSocket `/ws/events` and
  `/ws/instances/{name}/screen` with Origin and token checks.
- CLI (`avdfleet ...`) mirrors the API: install, doctor, images, avd, instances, proxies,
  oobe, scripts, appium, screen, fleet/apply, bundle, serve, selftest, uninstall/repair.
  Machine-readable `--json`; exit codes documented; secrets never printed.

### 3.10 Portable edition

- Bundle contains a pinned Python runtime, Android cmdline-tools/platform-tools/emulator,
  Appium + driver, UI build, default config, launchers, checksums and an offline cache.
  `--offline` means no network needed on first run.
- Launchers (`AVD Fleet.cmd`, `avdfleet.sh`, `.command`) construct the environment from the
  bundle root, unblock files on Windows first run, and detect root changes.
- Preflight checks: filesystem type (refuse FAT32, warn exFAT), noexec mount detection,
  quarantine/Mark-of-the-Web, free space, write speed, hypervisor availability.
- Relocation: rewrite AVD `.ini` paths, sysdir handling, rebuild venv if needed, invalidate
  stale snapshots, verify.
- Studio portable config via `STUDIO_PROPERTIES`; document leftovers (Studio config/cache,
  emulator's `~/.android` only when unavoidable), and provide `portable verify` that
  compares a filesystem snapshot and fails on undocumented writes.

### 3.11 Fleet spec

`fleet.yaml` describes templates, proxies, image requirements, instances and capacity.
`avdfleet fleet plan` prints a diff; `fleet apply` reconciles (create/update/delete with
`--prune`) and is idempotent; `--dry-run` supported. Schema in
`contracts/fleet.schema.json`.

## 4. Repo layout

```
bootstrap/install.sh  bootstrap/install.ps1
contracts/            openapi.yaml, events.schema.json, fleet.schema.json, bundle.schema.json, fakes-spec.md
src/avdfleet/
  core/       models, interfaces (Protocols), paths, events, errors, config, ports, logging+redaction
  platform/  installer/{engine,steps,providers}  sdk/{androidcli,legacy}  images/  avd/  runtime/
  proxy/{pool,vault,vault_file,assign,bridge,verify}  oobe/  scripts/  appium/  screen/
  portable/{env,checks,relocate,bundle,studio,launchers}
  store/  api/  cli/  fleetspec/
ui/                   React app (pages auto-registered via import.meta.glob)
tests/{fakes,unit,integration,e2e,portable}  docs/  research/  .github/workflows/
```

## 5. Waves and agents

Wave 0 (root, short): BRIEF.md, repo skeleton, all of `contracts/` and
`src/avdfleet/core/`, PLAN.md with every agent ID/owned paths/status. Freeze contracts, then
launch Wave 1 as one batch.

Research and gates. R1, R2, R4, R5, R6, R7 read-only; R3, G1, E0 install and boot emulators
on the accelerated host. Results in `research/` with source URLs.

| ID | Topic |
|---|---|
| R1 | Versions, URLs, SHA-256s and package IDs per host: Android Studio, command-line tools, Android CLI, platform-tools, emulator, JDK, Node, Appium 4 + UiAutomator2, Appium Inspector, scrcpy, uv |
| R2 | Host matrix: OS/arch support, WHPX/KVM/HVF setup, nested-virtualisation limits, headless GPU modes, Linux package dependencies per distro |
| R3a, R3b | OOBE matrix (two independent agents, §3.5): boot every candidate image and record proof |
| R4 | Emulator networking and control: `-http-proxy`, new networking stack and how to disable it, DNS/UDP coverage, ports, adb discovery, multi-instance, gRPC discovery files and keys |
| R5 | Appium 4 on each OS, parallel-session ports, Inspector plugin setup |
| R6 | Custom system image layout, registration, Studio update sites, AOSP `emu_img_zip` |
| R7 | Portable edition: verify every §3.10 fact against bundled versions; leftovers list per host |
| G1a, G1b | Proxy-isolation gate (§3.4), two independent agents |
| E0 | Test host: install the SDK and download target images in the background |

Wave 1 — build (parallel):

| ID | Owns | Scope |
|---|---|---|
| F1 | tests/fakes | Fake adb, emulator, sdkmanager, avdmanager, Android CLI executables per fakes-spec, pytest fixtures |
| B1 | bootstrap/ | install.sh + install.ps1 |
| I1 | installer/engine | Step engine, dry run, resume, manifest, uninstall/repair |
| I2 | installer/steps/sdk* | cmdline tools, packages, licences, env vars, Studio config |
| I3 | installer/steps/tools* | JDK/JBR, Node, Appium + drivers, Inspector, scrcpy |
| I4 | installer/steps/accel* | WHPX + reboot resume, AEHD migration, KVM udev, nested virt |
| I5–I9 | installer/providers/* | windows, macos, debian, fedora, arch |
| P1 | platform/ | Host detection + doctor checks with --fix |
| S1a, S1b | sdk/androidcli, sdk/legacy | Two tooling backends behind one interface + emulator console |
| G1x | images/ | Catalogue, downloads, custom import, oobe_matrix loader |
| A1 | avd/ | Templates, CRUD, bulk deploy, clone, config.ini, YAML export |
| N1 | runtime/ | Launch, boot detection, states, supervision, capacity, ports, snapshots |
| X1 | proxy/pool, vault, assign | Parsing, keyring vault, strategies, health checks |
| X2a, X2b | proxy/bridge | Competing local bridges: HTTP/HTTPS/SOCKS5 upstreams, auth, hot-swap, direct |
| X3 | proxy/verify | Egress verification from inside the guest |
| X4 | proxy/vault_file | Passphrase-derived encrypted vault |
| O1 | oobe/ | Detection, reset strategies, golden snapshot, verify-oobe |
| C1 | scripts/ | Types, fan-out executor, results, library, hooks |
| M1 | appium/ | Server lifecycle, ports, forward cleanup, capabilities |
| V1 | screen/ | gRPC client, fallbacks, frames, input, scrcpy |
| PT1 | portable/env, launchers | Environment, launchers, Unblock-File, root-change detection |
| PT2 | portable/checks | Filesystem, noexec, quarantine, space, write speed, hypervisor |
| PT3 | portable/relocate | AVD ini rewrite, sysdir, venv rebuild, snapshot invalidation |
| PT4 | portable/bundle | Bundle builder, offline caches, checksum manifest, verify |
| PT5 | portable/studio | Portable Studio config, leftover check |
| D1 | store/ | SQLite schema, migrations, repositories |
| H1 | api REST | Routes per OpenAPI, auth, token ACLs, static serving, DI |
| H2 | api WS | Event hub, screen stream, Origin/token checks |
| L1 | cli/ | Typer CLI for every capability, incl. bundle |
| Y1 | fleetspec/ | fleet.yaml schema, plan/diff, apply reconciler |
| U0 | ui/src/{app,lib} | Shell, routing, theme, generated client, WS, MSW mocks |
| U1–U7 | ui pages | Dashboard+Doctor; Fleet+Instance; Images+Templates+Deploy; Proxies; Scripts+Runs; Settings+auth; Portable |
| T1 | tests/contract | Schemathesis-like contract tests against openapi.yaml |
| T2 | tests/integration | Fleet scenarios against fakes from interfaces |
| T3 | ui/e2e | Playwright against MSW and daemon+fakes |
| T4 | tests/installer | Installer in Docker for Ubuntu/Debian/Fedora/Rocky/Arch, re-runs |
| T5 | tests/portable | Relocation, drive-letter, leftover diff, FAT32/noexec, offline first run |
| Q1 | .github/workflows | Lint/type/test on Linux/macOS/Windows, installer containers, UI build, best-effort KVM E2E |
| K1 | pyproject, packaging | uv packaging, bundled UI, versioning, release artifacts |
| W1 | docs/ | README, quickstarts, portable, architecture, proxy, OOBE, scripting, troubleshooting |

Wave 2 — integrate and harden: INT merges continuously, runs the suite and sends fixers.
E2E-1..n run §7 scenarios on every accelerated host. Reviewers (fresh context, read-only)
return findings: SEC, XPLAT, PORT, UX, PERF, CODE. Fixers then repeat until gates green.

Wave 3 — release: docs, packaging, full acceptance run, GUI screenshots, final report.

## 6. If there's no hardware virtualisation

The emulator needs WHPX, KVM or HVF. There is no usable software fallback: `-accel off` is
unsupported (needs `ANDROID_I_WANT_MY_TCG=yes`, far too slow). If the build environment lacks
acceleration (most cloud containers): build and test everything against the fakes, mark real
emulator tests `@pytest.mark.emulator`. GitHub-hosted x86 Linux runners expose KVM only after
a udev rule step, and GitHub calls nested virtualisation unsupported. Treat that CI job as
best-effort, with API ≤36 images or at most 2 instances to fit runner RAM. Ship
`avdfleet selftest --e2e` running the §7 scenario and verify-oobe on the user's machine.
Items 2–4 and 8 of §7 pass only through that self-test. The final report must say exactly
what was verified on real emulators and what only against fakes.

**This build host reports no usable hypervisor (`VirtualizationFirmwareEnabled=False`,
no WHPX/KVM/HVF) and only 16 GB RAM.** Real-emulator work is therefore out of scope here;
everything is fakes-backed and marked accordingly. No agent may claim real-emulator evidence.

## 7. Definition of done

1. Fresh Windows 11, macOS Apple Silicon, Ubuntu LTS, Fedora, Rocky/RHEL and Arch hosts with
   admin/sudo: one bootstrap command with `--accept-android-sdk-licenses --yes` installs
   everything unattended. Windows may reboot once and resume. A second run is a no-op and
   `avdfleet doctor` is all green.
2. Using only the GUI: download a Google Play image for the host ABI that
   `oobe_matrix.json` marks verified, deploy 5 AVDs from one template, import 5
   authenticated proxies (HTTP and SOCKS5), assign one to each and boot all. API 37+ needs
   ≥4 GB RAM per AVD; use 32 GB+ host or API ≤36.
3. Proxy isolation proven: G1 passed; 5 local test listeners each with own credentials; each
   guest requests a URL containing its own token and every token appears only on its assigned
   listener; no credential in logs/ps/API; each instance reaches first Setup Wizard with
   activity + screenshot; reset-to-OOBE works; golden-snapshot reset time measured.
4. An adb-shell script, host Python script and Appium script run across all 5 instances in
   parallel, live output and combined results; cancel and timeout work.
5. A custom image imported from a directory boots an AVD.
6. `avdfleet apply -f fleet.yaml` rebuilds the fleet on a clean host.
7. Portable: `avdfleet bundle --offline` → fresh machine, no Android tooling, standard
   account, hypervisor enabled. Launcher opens GUI offline, boots AVD to OOBE, runs script
   through proxy. Moving folder or changing drive letter still works. Filesystem diff shows
   no writes outside root except documented leftovers. FAT32 refused; noexec detected.
8. Lint, type checks, unit/contract/integration/portable/UI tests green on Linux/macOS/
   Windows. Real-emulator E2E green where hardware allows.
9. Docs complete.

## 8. Out of scope

Rooting or modifying Google Play images, bypassing Play Integrity/SafetyNet, spoofing device
identifiers or emulator fingerprints, automated Google account creation. Stock or
user-supplied images only.

## 9. Final report

What was built; acceptance results item by item marked "real emulator" or "fakes"; G1
outcome; every platform fact in the prompt that proved wrong and the change made; known
limitations; how to install/run both editions; links to repo/docs/screenshots.

## Key sources (checked 2026-11-26)

- https://developer.android.com/studio/run/emulator-commandline
- https://developer.android.com/studio/run/emulator-networking-proxy
- https://developer.android.com/studio/run/emulator-acceleration
- https://developer.android.com/studio/releases/emulator
- https://developer.android.com/studio/run/emulator-snapshots
- https://developer.android.com/tools/variables
- https://developer.android.com/tools/sdkmanager
- https://developer.android.com/studio/test/managed-devices
- https://appium.io/docs/en/latest/quickstart/requirements/
- https://appium.github.io/appium-uiautomator2-driver/latest/reference/capabilities/
- https://docs.astral.sh/uv/reference/storage/
- https://learn.microsoft.com/powershell/module/microsoft.powershell.core/about/about_execution_policies
- https://github.blog/changelog/2024-04-02-github-actions-hardware-accelerated-android-virtualization-now-available/
