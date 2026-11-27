# Acceptance evidence - honest status

This page records, item by item, what `BRIEF.md` section 7 requires, what was actually
verified, and with what evidence. **Nothing on this page is a real-emulator result.**
The build host has no usable hypervisor; every emulator-facing item is blocked or
fakes-backed. Evidence was captured on 2026-11-26 after the final integration pass.

## Build-host facts (why real emulator evidence is impossible here)

Read-only inspection (also in `research/R3-oobe.md`, `research/g1/gate.md`,
`research/E0-host.md`):

- Windows 11 Pro in a VMware VM; CPU model reported as "Apple silicon"; 4 vCPUs.
- `VirtualizationFirmwareEnabled=False`, no WHPX/KVM/HVF, Windows optional features
  `HypervisorPlatform`, `Hyper-V` and `VirtualMachinePlatform` all disabled.
- 16 GiB RAM (~2-10 GiB free depending on parallel agents); `C:` ~13.5 GiB free.
- No Android SDK, adb, emulator, JDK, Node, Appium or uv installed on the host; the
  test suite uses the executable fakes in `tests/fakes`.
- `@pytest.mark.emulator` tests are skipped. There is no supported software emulator
  fallback (BRIEF section 6).

## Section 7 item by item

Legend: **REAL** = real emulator/host evidence; **FAKES** = process-boundary fakes only;
**BLOCKED** = cannot run here; **NOT RUN** = never executed.

| # | Requirement | Status | Evidence | Missing |
|---|---|---|---|---|
| 1 | Fresh Windows 11 / macOS Apple Silicon / Ubuntu / Fedora / Rocky / Arch hosts: one bootstrap command installs unattended; Windows reboot+resume; second run no-op; doctor green | **NOT RUN on real OSes; FAKES installer** | `bootstrap/test_bootstrap.py` (static safety/flags/syntax); installer engine/steps/provider tests; fakes-backed selftest doctor (24 checks) | No real package install, reboot/resume, second-run no-op on real hosts |
| 2 | GUI-only: download a verified Play image, deploy 5 AVDs from one template, import 5 authenticated proxies, assign one each and boot all | **NOT RUN end-to-end; FAKES GUI/API** | Vitest+MSW 68/68; API integration with real container+fakes; `tests/integration/test_full_scenario_fakes.py` (boot/OOBE/bridge/script/screenshot); GUI screenshots under `docs/screenshots/` (MSW mock data) | No 5 real AVDs, no real image download, no real boot |
| 3 | Proxy isolation G1: five listeners, per-listener credentials/tokens, tokens only on assigned listener, no credential in logs/ps/API, OOBE screenshot, reset-to-OOBE, golden reset time | **BLOCKED** | Full protocol in `research/g1/gate.md`; harness `research/g1/listeners.py` self-test (61/61 harness checks, not the gate); bridge/vault/redaction unit tests; selftest guest-egress check (fakes) | No accelerated host, no real guest requests or credentials evidence |
| 4 | adb-shell + host-Python + Appium scripts across 5 instances in parallel, live output, combined results, cancel and timeout | **FAKES PARTIAL** | `tests/unit/test_scripts.py`, `test_script_executor.py`, `test_appium.py`; selftest fakes: adb/host-Python/Appium/cancel/timeout checks pass on **one** booted fake AVD; API/CLI routes | No real 5-instance fan-out or live Appium session |
| 5 | Custom image imported from a directory boots an AVD | **BLOCKED end-to-end; FAKES import** | `tests/unit/test_images.py` (layout/validation/staging/registration); research R6 import algorithm | No real custom image boot |
| 6 | `avdfleet apply -f fleet.yaml` rebuilds the fleet on a clean host | **FAKES PARTIAL** | `tests/integration/test_fleetspec.py` (41 tests); selftest fleet plan/dry-run; CLI/API fleet tests | Not run on a clean real host; no real apply/boot |
| 7 | Portable: `bundle --offline`, fresh machine, GUI offline, boot to OOBE, proxy script, relocation/drive letter, filesystem diff, FAT32 refused, noexec detected | **FAKES/PARTIAL** | `tests/portable/` (169 passed, 1 skipped symlink); bundle manifest schema; launcher/relocation/checks tests; Windows ADS unblock executed for real | No real bundle from licensed SDK cache, no fresh-machine launch, no real removable-media relocation or filesystem diff. Google licence forbids shipping Play components in the bundle. |
| 8 | Lint, type checks, unit/contract/integration/portable/UI tests green on Linux/macOS/Windows; real-emulator E2E where allowed | **PARTIAL PASS** | On this Windows host: ruff clean, mypy clean (182 files), Python suite green, UI Vitest 68/68, typecheck/lint/build green; CI workflows supplied but not executed; `tests/e2e/test_real_emulator.py` skips cleanly without WHPX/KVM/HVF; real emulator BLOCKED | No Linux/macOS execution, no CI run, no real-emulator E2E |
| 9 | Docs complete | **DELIVERED** | `docs/` (quickstarts, installer, portable, proxy, OOBE, scripting, fleetspec, API, CLI, compatibility, testing, troubleshooting, acceptance), README, FINAL_REPORT, 12 GUI screenshots | Real-host screenshots and validation remain for an accelerated machine |

## Final verification snapshot (integrated tree, 2026-11-26)

| Check | Command | Result |
|---|---|---|
| Ruff | `python -m ruff check src tests bootstrap` | **PASS** (all checks passed) |
| Mypy | `python -m mypy` | **PASS** - no issues in 182 source files |
| Python suite | `python -m pytest tests/unit tests/api tests/contract tests/integration tests/portable tests/cli tests/fakes bootstrap/test_bootstrap.py tests/e2e --timeout=180 -q` | **PASS** - **1342 passed, 5 skipped, 0 failed** in 167 s (skips include the real-emulator test) |
| CLI suite | `python -m pytest tests/cli -q` | **PASS** - 111 passed (includes one-time browser sign-in tests) |
| Fakes suite | `python -m pytest tests/fakes -q` | **PASS** - 68 tests (included in the 1332) |
| UI tests | `pnpm --dir ui test -- --run` | **PASS** - 11 files, 68 tests |
| UI typecheck/lint/build | `pnpm --dir ui typecheck`, `lint`, `build` | **PASS** (tsc, eslint max-warnings 0, Vite 335 modules) |
| UI screenshots | `python scripts/screenshots.py --url http://127.0.0.1:5199` | **PASS** - 12 pages in `docs/screenshots/` (MSW mock data) |
| Wheel | `python scripts/build_release.py --no-sdist` | **PASS** - wheel includes `api/static/index.html`, schemas, catalogue, seed matrix |
| Bootstrap | `python -m pytest bootstrap/test_bootstrap.py -q` | **PASS** - 37 passed, 1 skipped (no bash available) |
| Docs links | stdlib link-existence script | **PASS** - all relative links resolve |
| Selftest (fakes) | `python -m avdfleet selftest --e2e --fakes --fast --count 1 --json` | **PASS (fakes)** - 17 checks: 15 pass, 0 fail, 2 skip (`custom_image` not supplied, portable N/A); JSON `ok: true`, `exit_code: 0`, evidence labelled `fakes` |
| Selftest (real) | `python -m avdfleet selftest --e2e` | **BLOCKED** - no hypervisor; emulator checks report SKIP, exit 0 unless `--require-acceleration` |
| CI | GitHub Actions workflows | **NOT EXECUTED** - no git/GitHub remote on this host |

### Selftest fakes detail

The fakes-mode selftest boots a fake AVD through the **real** RuntimeManager, detects the
fake Setup Wizard via the real OOBE manager, assigns a proxy through the real vault/pool
and verifies guest egress through a loopback listener, runs adb-shell / host-Python /
Appium scripts through the real script service, exercises cancel and timeout, and prints a
JSON report with `"evidence": "fakes"`. It passed 15/17 checks with two intentional skips.
This validates wiring and CLI behaviour; it is **not** emulator evidence.

### GUI screenshots

Twelve screenshots were captured with Playwright + Microsoft Edge against the Vite dev
server with MSW mock data (labelled mock data, not real daemon evidence):

`docs/screenshots/dashboard.png`, `fleet.png`, `instance-detail.png`, `deploy.png`,
`images.png`, `templates.png`, `proxies.png`, `scripts.png`, `runs.png`, `doctor.png`,
`settings.png`, `portable.png`.

## What a real accelerated host must still run

1. `avdfleet selftest --e2e` on Windows WHPX, macOS HVF or Linux KVM.
2. The G1 protocol in `research/g1/gate.md`, two independent runs (G1a/G1b).
3. R3a/R3b OOBE matrix boots with screenshots and reset timings.
4. The portable fresh-machine run and filesystem diff from `research/R7-portable.md`.
5. Installer container matrix (Ubuntu/Debian/Fedora/Rocky/Arch) and CI on all three OSes.

No result on this page should be upgraded to "real emulator" without that evidence.
