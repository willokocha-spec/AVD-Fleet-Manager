# FINAL REPORT — AVD Fleet v0.1.0

**Build date:** 2026-11-26 · **Host:** Windows 11 VM (no hardware virtualisation)
· **Edition:** installed + portable (one engine)

> **Evidence caveat, read first.** This build host has **no usable hypervisor**
> (WHPX/KVM/HVF all unavailable) and no admin permission to install the Android SDK.
> No real emulator was booted. Every emulator-facing acceptance item below is marked
> **BLOCKED** or **FAKES**; nothing is claimed as real-emulator evidence. A real
> accelerated host must run `avdfleet selftest --e2e`, the G1 protocol and the OOBE
> matrix before this can be called a release. The complete item-by-item table is in
> [docs/acceptance.md](docs/acceptance.md).

> **Prompt recovery.** The first message supplied sections 4–9 only, so Wave 0
> reconstructed the product brief and recorded that fact. The operator then supplied the
> missing first part (sections 0–3.1); it is preserved in
> [BRIEF_ORIGINAL.md](BRIEF_ORIGINAL.md). Four explicit gaps found by comparing the
> supplied decisions against the build were closed in the final pass: `avdfleet gui`,
> top-level `avdfleet plan`/`apply`, `install --images/--avd-home/--proxies/
> --download-proxy`, and explicit `serve --allow-remote` with optional TLS. The UI-stack
> and generated-client deviations are recorded in [DECISIONS.md](DECISIONS.md) D-004 and
> section 5 below.

## 1. What was built

AVD Fleet (`avdfleet`) is an operator product for running fleets of Android Virtual
Devices on one accelerated host. The repository implements both editions around one
engine:

**Installed edition**
- Python 3.12 package (`src/avdfleet/`) with a Typer CLI, a FastAPI REST + WebSocket
  daemon and a React + TypeScript + Vite GUI served by the same loopback process.
- Thin bootstraps (`bootstrap/install.sh`, `bootstrap/install.ps1`) that install `uv`,
  install the package with `uv tool install`, hand over to the idempotent installer
  (ordered check/apply/verify steps, dry-run, resume after reboot, manifest,
  uninstall/repair) and per-OS providers (Windows/macOS/Debian/Fedora/Arch).
- Subsystems: `core` (models, Protocols, paths, config, port allocator, event bus,
  redaction), `platform` (installer, two SDK tooling backends, images, AVDs, runtime),
  `proxy` (parser, keyring/file vault, assignments, loopback HTTP/SOCKS5 bridge, guest
  egress verifier), `oobe` (detection, matrix, four reset strategies, golden snapshot),
  `scripts` (adb-shell / host-Python / Appium fan-out with live output, cancel, timeout),
  `appium`, `screen` (gRPC-aware capture with adb fallback, input injection, scrcpy
  builder), `portable` (environment, launchers, checks, relocation, bundle, Studio
  config), `store` (SQLite WAL + migrations), `fleetspec` (fleet.yaml plan/apply), `api`
  and `cli`.
- Frozen contracts in `contracts/`: OpenAPI 3.1 (57 paths / 72 operations), event,
  fleet and bundle JSON schemas, and the executable-fakes specification.

**Portable edition**
- `avdfleet bundle [--offline]` assembles a per-host folder from a prepared component
  cache: pinned Python runtime, SDK tools, UI build, default config, launchers, config
  and AVD homes, checksum manifest, offline cache. It never downloads at build time.
- Launchers for Windows (`AVD Fleet.cmd`), macOS (`AVD Fleet.command`) and Linux
  (`avdfleet.sh`) derive the root, isolate all state inside it, unblock Mark-of-the-Web
  on Windows, warn about macOS quarantine, run preflight checks, start
  `avdfleet serve --portable`, wait for `/health` and open the GUI.
- Relocation rewrites AVD `.ini`/`config.ini` absolute paths, repairs execute bits,
  invalidates stale snapshots, updates the manifest and verifies checksums. Preflight
  refuses FAT32, detects noexec, checks free space/write speed/quarantine and reports the
  hypervisor state. Documented leftovers remain per OS.

**GUI**
- 12 pages (Dashboard, Fleet, Instance detail, Deploy, Images, Templates, Proxies,
  Scripts, Runs, Settings, Doctor, Portable) auto-registered from `ui/src/pages/*.tsx`,
  a design system, typed API client, WebSocket event stream, MSW mocks, auth screen,
  dark/light/system themes, responsive layout. `avdfleet gui` starts the daemon if
  needed and opens a one-time sign-in link (single-use code, URL fragment, exchanged
  over loopback) so the browser is authenticated without copy/paste; `--no-auto-auth`
  restores the manual Connect screen. Screenshots:
  [dashboard](docs/screenshots/dashboard.png),
  [fleet](docs/screenshots/fleet.png),
  [instance detail](docs/screenshots/instance-detail.png),
  [images](docs/screenshots/images.png),
  [proxies](docs/screenshots/proxies.png),
  [scripts](docs/screenshots/scripts.png),
  [doctor](docs/screenshots/doctor.png),
  [portable](docs/screenshots/portable.png) and the rest of
  [docs/screenshots/](docs/screenshots/). These were captured with Playwright + Edge
  against **MSW mock data**, not a real daemon.

**Quality assets**
- Python tests: core/unit, API, contract, fakes-backed integration, portable, CLI.
- UI tests: Vitest + Testing Library + MSW.
- CI workflows for Linux/macOS/Windows, UI build, installer containers (best-effort) and
  a non-gating KVM E2E job; release workflow builds UI + wheel/sdist + checksums.
  **CI is green on `main`** (run 45953572457): Python 3.12/ruff/mypy/pytest green on
  Ubuntu, macOS ARM64 and Windows; UI typecheck/lint/test/build green; installer
  containers and docs links green. The KVM job is non-gating and self-reported green
  because its emulator test is best-effort.

## 2. Acceptance results (§7) — real vs fakes

| # | Requirement | Result | Evidence |
|---|---|---|---|
| 1 | Fresh-Windows/macOS/Linux one-command unattended install, Windows reboot+resume, second run no-op, doctor green | **NOT RUN on real hosts; FAKES installer** | `bootstrap/test_bootstrap.py`; installer engine/steps/provider tests; fakes selftest doctor 24/24 | 
| 2 | GUI-only: verified Play image, 5 AVDs from one template, 5 authenticated proxies assigned, boot all | **NOT RUN end-to-end; FAKES GUI/API** | UI 68/68 Vitest+MSW; API + real-container integration; full fakes scenario boots/OOBE/bridge/scripts; screenshots (mock data) |
| 3 | G1 proxy isolation: 5 listeners, tokens only on assigned listener, no credential leaks, OOBE screenshot, reset-to-OOBE, golden-snapshot timing | **BLOCKED** | protocol `research/g1/gate.md`; harness self-test 61/61 (tool only); bridge/vault/redaction tests; selftest guest-egress (fakes) |
| 4 | adb-shell + host-Python + Appium scripts across 5 instances in parallel, live output, cancel, timeout | **FAKES PARTIAL** (one booted fake AVD; no five-instance run) | real script service in selftest fakes; executor tests cover concurrency/cancel/timeout |
| 5 | Custom image imported from a directory boots an AVD | **BLOCKED end-to-end; import FAKES** | `tests/unit/test_images.py`; R6 import algorithm; selftest `--custom-image` not supplied |
| 6 | `avdfleet apply -f fleet.yaml` rebuilds the fleet on a clean host | **FAKES PARTIAL** (plan/dry-run; no real clean-host apply) | 41 fleetspec integration tests; selftest fleet plan/dry-run; CLI/API fleet tests |
| 7 | Portable: bundle --offline, fresh-machine offline GUI, boot to OOBE, script through proxy, relocation, filesystem diff, FAT32/noexec | **FAKES/PARTIAL** | 169 portable tests (1 symlink skip); real NTFS ADS unblock test; no real fresh-machine/removable-media run. Google licence forbids shipping Play components in the bundle. |
| 8 | Lint, type checks, unit/contract/integration/portable/UI green on Linux/macOS/Windows; real-E2E where allowed | **PASS (static/unit), real E2E BLOCKED** | GitHub Actions run 45953572457: ruff, mypy and pytest green on Ubuntu, macOS ARM64 and Windows; UI 68/68 + typecheck/lint/build green; docs/installer jobs green. Real-emulator E2E still BLOCKED on this host. |
| 9 | Docs complete | **DELIVERED** | `docs/` set, README, this report, 12 screenshots, contracts, research/ |

**Exact fakes self-test result:** `avdfleet selftest --e2e --fakes --fast --count 1
--json` → `"ok": true`, `"exit_code": 0`, `"evidence": "fakes"`, summary **17 checks:
15 pass, 0 fail, 2 skip** (`custom_image` not supplied; portable checks are
installed-edition N/A). It boots a fake AVD through the real runtime, detects the fake
Setup Wizard, assigns a proxied bridge, verifies guest egress against a loopback
listener, runs adb/host-Python/Appium scripts and exercises cancel/timeout. This is
wiring evidence only, not emulator evidence.

## 3. G1 outcome

**G1 is BLOCKED — not run and not passed.** The host has no WHPX/KVM/HVF, 16 GiB RAM and
no permission to install Android tooling. The full five-listener topology, per-listener
credentials, token-request protocol, negative controls, credential-leak checks
(logs/ps/API/DB/vault/network), OOBE/screenshot requirements and reset-timing protocol are
in [research/g1/gate.md](research/g1/gate.md) and
[research/g1/listeners.py](research/g1/listeners.py). The harness self-test passes (61
checks) but that verifies the tool, not the gate. G1a and G1b must run independently on
an accelerated host.

Residual proxy limitation (R4): the emulator's `-http-proxy` is TCP-only, and DNS/UDP/QUIC
are not proxied. Strict mode uses the loopback bridge; even a passing G1 cannot claim
full UDP/DNS isolation. This is stated in [docs/proxy.md](docs/proxy.md).

## 4. Platform facts that proved wrong (and what changed)

Full detail: [docs/compatibility.md](docs/compatibility.md),
[research/R1](research/R1-versions.md)–[R7](research/R7-portable.md).

1. **GPU modes:** `swiftshader_indirect` and other `*_indirect` modes are deprecated
   (emulator 36.4.9+). Supported modes are `auto/host/software/lavapipe/swiftshader/
   swangle`; `software` is recommended headless. Code keeps the compatible fallback and
   supports configurable modes; docs recommend `software`.
2. **Node LTS:** Node 26 is Active LTS (Node 22 is maintenance). Appium 4 accepts
   `^22.22.2 || ^24.15.0 || >=26`; the default stays 22 for compatibility.
3. **Appium:** current is Appium 4.0.1 + UiAutomator2 10.4.0 (brief said only “Appium 4”);
   Appium 4 is ESM-only, so JS scripts must be ESM.
4. **`-http-proxy` is TCP-only:** no UDP redirection, no SOCKS5, DNS bypass. The design
   uses the loopback bridge and documents the limitation.
5. **AEHD sunsets 2026-12-31:** WHPX is the Windows path; AEHD remains migration-only.
6. **Google SDK licence forbids redistributing Play/Google components**, so the portable
   bundle cannot ship them; offline first run is limited to the operator's licensed local
   cache plus OSS components.
7. **`STUDIO_PROPERTIES` is a file path** to `idea.properties`, not a directory.
8. **API levels can be non-integer** (`android-36.1`, `android-37.0`, canary tags); the
   raw SDK API string is preserved in `SystemImage.metadata["sdk_api"]`.
9. **Windows minimum is Windows 10 x64 (1803+)**, not only Windows 11; Windows ARM64 has
   no stable emulator build and is refused. Linux arm64 is likewise refused.
10. **macOS Intel** remains supported (macOS 12+), while current Homebrew needs macOS 15+;
    the installer does not depend on Homebrew for SDK components.
11. **Custom images** need `system.img`, `kernel-ranchu` and `ramdisk.img`; current Google
    archives contain no `package.xml`. The importer validates those files.
12. **gRPC discovery** is `<tmp>/avd/running/pid_<pid>.ini` in current AOSP clients; the
    old `emu-grpc` directory used by the fakes is test-only. adb discovers only the first
    16 emulator ports unless `ADB_LOCAL_TRANSPORT_MAX_PORT` is raised; V1/ADR handling
    supports configurable discovery dirs and the screen path falls back to adb screencap.
13. **Android Studio/CLI:** Google marks `sdkmanager`, `avdmanager` and the emulator
    command as deprecated in favour of the preview Android CLI. Both toolchains are
    implemented behind one interface; the legacy emulator binary is kept for flags the
    CLI lacks (`-http-proxy`, `-grpc`, `-sysdir`, `-ports`, `-read-only`).

## 5. Deviations from the supplied prompt

The operator supplied the missing first part of the prompt after Wave 0; it is preserved
in [BRIEF_ORIGINAL.md](BRIEF_ORIGINAL.md). Known deviations:

- **UI stack:** React + TypeScript + Vite + hand-written design system, **not Mantine**;
  the typed client is hand-written rather than generated from OpenAPI. This is the
  largest conformance gap (fixed decision §2). It is recorded as release debt; the UI is
  tested and accessible but would need a Mantine migration for strict conformance.
- **`avdfleet gui`, top-level `avdfleet apply`/`plan` and install flags** required by the
  supplied §2/§3.1 were added in the final pass; see the CLI help and
  [docs/cli.md](docs/cli.md).
- **Portable builder does not download.** It consumes a prepared cache; there is no
  one-command `--prepare` download. Documented in `docs/portable.md`.
- **Git branches/worktrees** were impossible because `git` is not installed; disjoint
  write scopes + task ledgers replaced worktrees.
- **Playwright E2E** has no tests (only Vitest + MSW); screenshots were captured with
  Playwright against MSW mocks. A skipped real-emulator test lives at
  `tests/e2e/test_real_emulator.py`; it runs `avdfleet selftest --e2e` on an accelerated
  host. Real UI walkthrough on a live daemon is still required.
- **CI is green** on Ubuntu, macOS ARM64 and Windows (run 45953572457); the portable,
  fake-tooling and line-ending issues found only on those runners were fixed. Real
  emulator E2E remains unvalidated.
- **Python 3.14** is the host default and unsupported; all runs used bundled 3.12.14.

### Post-review hardening (security/ops pass)

A second security review identified five issues; all were fixed with tests and are
documented in [docs/security.md](docs/security.md):

- **Per-instance proxy isolation.** The runtime bridge now receives a random per-instance
  listener password and passes it to the emulator through `http_proxy`, never through
  `-http-proxy`/argv. A guest or local process cannot reuse another instance's bridge.
- **Child environments.** `core/childenv.py` strips `AVDFLEET_TOKEN`,
  `AVDFLEET_VAULT_PASSPHRASE` and reserved `AVDFLEET_SECRET_*` names at every spawn
  boundary (scripts, Appium, emulator, SDK tools, installer, screen/OOBE helpers).
- **Remote bind guard.** `serve` refuses a non-loopback bind when authentication is
  disabled, and the warning now reflects the effective auth state.
- **Remote Host/Origin allowlists.** Concrete bind addresses and the actual port are
  added to Host/Origin validation automatically; wildcard binds require an explicit
  `--trusted-host`; `--trusted-origin` covers reverse proxies. This also fixes the
  WebSocket origin on custom loopback ports.
- **ADB >16 instances.** `ADB_LOCAL_TRANSPORT_MAX_PORT` is set from the configured port
  range at CLI/container startup and in every child environment. `doctor --slow` now
  checks an already-running adb server and reports the required `adb kill-server` restart.

Real-emulator confirmation of the bridge's `http_proxy` credential handling and a
17-instance adb run remain hardware-gated and are called out in
[docs/acceptance.md](docs/acceptance.md).

## 6. Known limitations

- No real-emulator validation on this host: G1, R3 OOBE matrix, real Appium sessions,
  gRPC screen streaming, custom-image boot, fleet boot storms and Windows reboot/resume
  are unvalidated.
- DNS/UDP proxy bypass is a documented residual limitation (TCP isolation only).
- Portable: no download/prepare command; no real fresh-machine or removable-media test;
  Google components cannot be redistributed; Studio is only partially portable.
- Appium Inspector is optional and GUI-only. `screen` uses adb screencap unless a gRPC
  transport/stub is injected; the gRPC protobuf stubs are not vendored.
- UI screenshots use mock data; the real daemon UI walkthrough is still required.
- Playwright/browser E2E and installer container tests are not part of the executed suite.
- Cross-OS test execution (Linux/macOS) was not possible; only this Windows host ran.

## 7. How to install and run

### Installed edition

```bash
# macOS / Linux (from the repository checkout)
chmod +x bootstrap/install.sh
./bootstrap/install.sh --accept-android-sdk-licenses --yes
```

```powershell
# Windows (elevated PowerShell recommended)
powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap\install.ps1 `
  -AcceptAndroidSdkLicenses -Yes
```

Reboot if WHPX requires it and re-run the same command (resume is automatic). Then:

```bash
avdfleet doctor
avdfleet serve                 # GUI + REST + WS at http://127.0.0.1:8765/
avdfleet gui                   # start the daemon if needed and open the browser
avdfleet selftest --e2e        # acceptance self-test on an accelerated host
```

Per-OS prerequisites and troubleshooting:
[docs/quickstart-windows.md](docs/quickstart-windows.md),
[docs/quickstart-macos.md](docs/quickstart-macos.md),
[docs/quickstart-linux.md](docs/quickstart-linux.md),
[docs/troubleshooting.md](docs/troubleshooting.md).

### Portable edition

```bash
# Prepare python/, sdk/, ui/ (and optional components) in the offline cache once,
# then build the bundle:
avdfleet bundle --offline -o AVD-Fleet-Portable

# On the target machine (virtualisation enabled, standard account):
#   Windows: double-click "AVD Fleet.cmd"
#   macOS:   open "AVD Fleet.command"
#   Linux:   ./avdfleet.sh
# After moving the folder / changing drive letter:
avdfleet portable relocate ./AVD-Fleet-Portable
avdfleet portable verify ./AVD-Fleet-Portable
```

See [docs/portable.md](docs/portable.md) for cache layout, preflight checks, relocation,
leftovers and the Google-licence constraint.

**Prebuilt portable bundles produced on this host** (under `dist/`, not committed to git):

- `AVD-Fleet-Portable-Windows-x64.zip` — Windows 10/11 x64, ~23 MB
- `AVD-Fleet-Portable-Windows-arm64.zip` — Windows 11 ARM64, ~21 MB (app only; the
  Android emulator has no Windows ARM64 build)
- Each contains a pinned Python 3.12 runtime, application code, built GUI, launcher,
  config/AVD/data homes and a checksum manifest. Extract and double-click
  `AVD Fleet.cmd`; no Python or pip install is needed. State stays inside the folder,
  relocation is automatic, and the launcher opens the GUI with a one-time sign-in link.
- **The Android SDK is not bundled** (Google licence and size). On an x64 host run the
  installer with `--sdk-root <bundle>/sdk`, or copy a licensed SDK in. System images are
  never redistributed.
- Verified locally: ARM64 bundle extracted to a path with spaces, launched via `.cmd`,
  `/health` 200, GUI 200, authenticated API 200, `avdfleet portable verify` passed 1864
  checksums after relocation. The x64 bundle's embedded x64 Python/app served `/health`
  200 and the GUI under emulation; its launcher's arch check correctly refuses to run it
  on this ARM64 host.

## 8. Verification evidence

| Check | Command | Result |
|---|---|---|
| Lint | `python -m ruff check src tests bootstrap` | **PASS** |
| Types | `python -m mypy` | **PASS** — no issues in 182 source files |
| Python suite | `python -m pytest tests/unit tests/api tests/contract tests/integration tests/portable tests/cli tests/fakes bootstrap/test_bootstrap.py tests/e2e --timeout=180 -q` | **PASS** — 1361 passed, 5 skipped, 0 failed (some skips include the real-emulator test) |
| CLI | `python -m pytest tests/cli -q` | **PASS** — 116 passed |
| Fakes | `python -m pytest tests/fakes -q` | **PASS** — 68 tests (included above) |
| UI | `pnpm --dir ui test -- --run` | **PASS** — 11 files / 68 tests |
| UI gates | `pnpm --dir ui typecheck`, `lint`, `build` | **PASS** |
| Browser auto-login | headless Edge against a live daemon: one-time link → localStorage token → dashboard | **PASS** — redirected to `/`, dashboard rendered, code replay returned 401 |
| Screenshots | `python scripts/screenshots.py` | **PASS** — 12 pages |
| Wheel/sdist | `python scripts/build_release.py --no-sdist`; `python -m build --sdist` | **PASS** — 199-entry wheel (0.8 MiB) with UI + schemas; sdist with docs/screenshots |
| Portable bundles | `PortableBundleBuilder` on prepared x64/ARM64 trees | **PASS** — x64 and ARM64 ZIPs built and verified; ARM64 launcher, GUI, authenticated API and relocation verified end-to-end |
| Source archive | `dist/avdfleet-0.1.0-source.zip` | **PASS** — 398-file GitHub-ready tree, no secrets/runtime state |
| Bootstrap | `python -m pytest bootstrap/test_bootstrap.py -q` | **PASS** — 37 passed, 1 skipped (no bash) |
| Selftest | `avdfleet selftest --e2e --fakes --fast --count 1 --json` | **PASS (fakes)** — 15 pass, 0 fail, 2 skip |
| CI | [GitHub Actions run 45953572457](https://github.com/willokocha-spec/AVD-Fleet-Manager/actions/runs/45953572457) | **PASS** — Python on Ubuntu/macOS/Windows, UI, installer containers, docs links, best-effort KVM |

## 9. Links

- Repository: <https://github.com/willokocha-spec/AVD-Fleet-Manager> (public, `main`;
  published 2026-11-27 from this tree)
- Working brief: [BRIEF.md](BRIEF.md) · supplied prompt part 1:
  [BRIEF_ORIGINAL.md](BRIEF_ORIGINAL.md) · decisions: [DECISIONS.md](DECISIONS.md) ·
  plan/agent ledger: [PLAN.md](PLAN.md)
- Docs index: [README.md](README.md) · [docs/architecture.md](docs/architecture.md) ·
  [docs/security.md](docs/security.md) · [docs/api.md](docs/api.md) ·
  [docs/cli.md](docs/cli.md) · [docs/testing.md](docs/testing.md)
- Acceptance: [docs/acceptance.md](docs/acceptance.md)
- Research: [research/](research/) — R1–R7, G1 protocol, R3 OOBE protocol, E0 runbook
- Contracts: [contracts/openapi.yaml](contracts/openapi.yaml),
  [contracts/events.schema.json](contracts/events.schema.json),
  [contracts/fleet.schema.json](contracts/fleet.schema.json),
  [contracts/bundle.schema.json](contracts/bundle.schema.json),
  [contracts/fakes-spec.md](contracts/fakes-spec.md)
- Screenshots: [docs/screenshots/](docs/screenshots/)

## 10. Evidence caveat

There is **no real emulator evidence in this repository.** The build host is a VM with no
usable hypervisor, so the emulator cannot start. G1 is blocked, not passed. The OOBE
matrix seed is deliberately all-`unverified` because no image was booted on real hardware;
the fakes can produce a verified-looking record only inside `tests/fakes`. DNS and UDP are
not covered by the proxy design and are not claimed to be. Tests marked `emulator` are
skipped here. On an accelerated host, run `avdfleet selftest --e2e`, the G1 protocol and
the R3 OOBE matrix, then update [docs/acceptance.md](docs/acceptance.md) and this report
with the real results.
