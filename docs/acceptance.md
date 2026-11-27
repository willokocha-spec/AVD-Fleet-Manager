# Acceptance evidence - honest status

This page records, item by item, what `BRIEF.md` section 7 requires, what was
actually verified, and with what evidence. It now covers two hosts:

- **Live host (2026-11-27)** - a real accelerated Windows 11 machine (WHPX).
  Real AVD boots, real `oobe verify` records and a real-emulator
  `selftest --e2e` run were measured here. Host facts, per-image timings, the
  OOBE matrix and the command log are in
  [live-acceptance-2026-11-27.md](live-acceptance-2026-11-27.md).
- **Original build host (2026-11-26)** - a VMware VM with no hypervisor and no
  Android tooling. Everything emulator-facing was fakes-backed there.

Statuses below are upgraded to **REAL** only where the live host measured them.
The proxy isolation gate (G1), a real Appium session, a custom-image boot and a
fresh-machine portable run are still **not verified**.

## Live host facts (2026-11-27)

- Windows 11 Pro, build 26200 (25H2), x86_64; 8 logical CPUs; 63.8 GiB RAM.
- WHPX available and usable: `emulator -accel-check` reports
  `WHPX(10.0.26200) is installed and usable`.
- Android SDK at `C:\Users\Admin\AppData\Local\Android\Sdk`; JDK 25 (Android
  Studio JBR); Node 24.19.0; Appium 3.6.0; adb 1.0.41; emulator 37.3.10.0.
- All AVD boots were cold boot, headless, `swiftshader_indirect`, 2048 MiB.

Measured boots to `sys.boot_completed`:

| Image | Boot |
|---|---|
| `system-images;android-34;google_apis;x86_64` | 146.7 s first CLI boot; 97-134 s in later `oobe verify` runs |
| `system-images;android-34;google_apis_playstore;x86_64` | 127.7 s |
| `system-images;android-25;google_apis;x86` | 45.5 s (OOBE verification run) |

The selftest's own `boot` check measured **128.7 s** on
`system-images;android-37.2-beta3;google_apis_playstore_ps16k;x86_64`.

## OOBE matrix (live host)

| Image | Result | Observed | Boot |
|---|---|---|---|
| `system-images;android-25;google_apis;x86` | verified once (17:04Z run) | `com.google.android.setupwizard/.SetupWizardActivity` | 45.5 s |
| `system-images;android-33;google_apis;x86_64` | unsupported | `com.android.settings/.FallbackHome` | 124.1 s |
| `system-images;android-34;google_apis;x86_64` | unsupported | `FallbackHome` and `com.google.android.googlesdksetup/.DefaultActivity` | 97-134 s |
| `system-images;android-34;google_apis_playstore;x86_64` | no Setup Wizard | first boot lands on `com.google.android.apps.nexuslauncher` | 127.7 s |

**Key finding:** modern emulator images (API 33/34 and the API 37 beta) ship no
Google Setup Wizard and bake `ro.setupwizard.mode=DISABLED`; a wiped AVD parks
on `FallbackHome` (blank/black HOME placeholder) and self-provisions to the
launcher. The API 25 `google_apis` image still has the real wizard.

**Caveat:** the image was verified once on the 17:04Z run. Repeatability was
**not** established: every later run (17:06Z, 17:16Z, 17:21Z and 17:27Z),
including the final one with `wipe_data=True` plus `-no-snapshot`, landed on
the Nexus launcher or an emulator SDK-setup stub. `oobe matrix` keeps one
record per image and currently reads `failed` (49.8 s,
`com.google.android.sdksetup/.DefaultActivity`). The API 25 Setup Wizard appears
only on the genuinely first boot of a freshly installed image, so an image that
has already been booted on this host cannot be assumed to re-enter OOBE - a
documented limitation, not a passing capability. No reset-to-OOBE timing was
recorded. Details and evidence paths:
[live-acceptance-2026-11-27.md](live-acceptance-2026-11-27.md).

## What the original build host could not do

The 2026-11-26 evidence was captured on a Windows 11 Pro VMware VM reported as
"Apple silicon", 4 vCPUs, 16 GiB RAM (~13.5 GiB free on `C:`), with
`VirtualizationFirmwareEnabled=False` and the `HypervisorPlatform`, `Hyper-V`
and `VirtualMachinePlatform` features disabled - no WHPX/KVM/HVF. The host had
no Android SDK, adb, emulator, JDK, Node, Appium or uv, and build policy forbade
installing them (read-only details: [E0-host.md](../research/E0-host.md)).

That host could not boot a single AVD, record an OOBE result, observe guest
proxy egress, start an Appium session or import a custom image. Its results
remain valid for what they are - unit, contract, API, integration, portable, CLI
and UI suites against process-boundary fakes, static bootstrap checks and CI
configuration - but they are not emulator evidence. The old blanket statement
that *nothing* on this page was a real-emulator result described that host only;
the live host above changes that for the items marked REAL below.

## Section 7 item by item

Legend: **REAL** = measured on the live accelerated host; **PARTIAL REAL** =
part measured on the live host, the rest still fakes or not run; **FAKES** =
process-boundary fakes only; **BLOCKED** = cannot be verified with the resources
available; **NOT RUN** = never executed.

| # | Requirement | Status | Evidence | Missing |
|---|---|---|---|---|
| 1 | Fresh Windows 11 / macOS Apple Silicon / Ubuntu / Fedora / Rocky / Arch hosts: one bootstrap command installs unattended; Windows reboot+resume; second run no-op; doctor green | **NOT RUN on fresh hosts; REAL doctor** | Live host: `doctor` 25/25 on real tools inside the selftest; `bootstrap/test_bootstrap.py` static safety/flags/syntax; installer engine/steps/provider tests | No fresh-host bootstrap, reboot/resume or second-run no-op on any real OS |
| 2 | GUI-only: download a verified Play image, deploy 5 AVDs from one template, import 5 authenticated proxies, assign one each and boot all | **NOT RUN end-to-end; PARTIAL REAL (CLI)** | Live host booted a real Play Store AVD (127.7 s) and recorded real `oobe verify` results; Vitest+MSW 68/68; API integration with real container + fakes | No 5 real AVDs, no GUI-driven download/boot, no proxy import/assignment |
| 3 | Proxy isolation G1: five listeners, per-listener credentials/tokens, tokens only on assigned listener, no credential in logs/ps/API, OOBE screenshot, reset-to-OOBE, golden reset time | **BLOCKED** | Full protocol in [research/g1/gate.md](../research/g1/gate.md); harness `research/g1/listeners.py` self-test (61/61 harness checks, not the gate); bridge/vault/redaction unit tests; live selftest `proxies` skipped - "no proxies are configured" | No real proxies, guest egress, credential evidence or reset timing |
| 4 | adb-shell + host-Python + Appium scripts across 5 instances in parallel, live output, combined results, cancel and timeout | **PARTIAL REAL** | Live selftest: `scripts.adb`, `scripts.host_python`, `scripts.cancel`, `scripts.timeout` pass on one real booted AVD; `scripts.appium` skipped (server not started); `tests/unit/test_scripts.py`, `test_script_executor.py`, `test_appium.py` | No real 5-instance fan-out; no live Appium session |
| 5 | Custom image imported from a directory boots an AVD | **BLOCKED** | `tests/unit/test_images.py` (layout/validation/staging/registration); research R6 import algorithm; live selftest `custom_image` skipped (not supplied) | No real custom-image boot |
| 6 | `avdfleet apply -f fleet.yaml` rebuilds the fleet on a clean host | **PARTIAL REAL (dry-run only)** | Live selftest `fleet` dry-run pass; `tests/integration/test_fleetspec.py` (41 tests); CLI/API fleet tests | No real apply + boot on a clean host |
| 7 | Portable: `bundle --offline`, fresh machine, GUI offline, boot to OOBE, proxy script, relocation/drive letter, filesystem diff, FAT32 refused, noexec detected | **FAKES/PARTIAL** | `tests/portable/` (169 passed, 1 skipped symlink); bundle manifest schema; launcher/relocation/checks tests; Windows ADS unblock executed for real; live host runs the installed edition, `portable` skipped | No bundle from a licensed SDK cache, no fresh-machine launch, no real removable-media relocation or filesystem diff. Google licence forbids shipping Play components in the bundle. |
| 8 | Lint, type checks, unit/contract/integration/portable/UI tests green on Linux/macOS/Windows; real-emulator E2E where allowed | **PARTIAL REAL** | CI run 45953572457: ruff, mypy, pytest, UI typecheck/lint/test/build, docs links, installer containers green on Ubuntu, macOS ARM64 and Windows; live `selftest --e2e --fast --count 1` now runs **green** (`ok: true`, `exit_code: 0`, `"evidence": "real-emulator"`): 12 pass / 0 fail / 5 skip with a real AVD boot (~102 s). The first run was ~183 s with one `oobe` failure caused by a defective check, kept in the record | Full 5-instance `--e2e` not run; no macOS HVF or Linux KVM accelerated run |
| 9 | Docs complete | **DELIVERED** | `docs/` (quickstarts, installer, portable, proxy, OOBE, scripting, fleetspec, API, CLI, compatibility, testing, troubleshooting, acceptance, live acceptance), README, FINAL_REPORT, 12 GUI screenshots (mock data) | Real-host GUI screenshots and full validation remain for an accelerated machine |

## Verification snapshots

### Original build host (2026-11-26, static/unit)

Recorded on the unaccelerated host; rerun before quoting these as current
numbers for the live tree.

| Check | Command | Result |
|---|---|---|
| Ruff | `python -m ruff check src tests bootstrap` | **PASS** (all checks passed) |
| Mypy | `python -m mypy` | **PASS** - no issues in 182 source files |
| Python suite | `python -m pytest tests/unit tests/api tests/contract tests/integration tests/portable tests/cli tests/fakes bootstrap/test_bootstrap.py tests/e2e --timeout=180 -q` | **PASS** - 1361 passed, 5 skipped, 0 failed (skips include the real-emulator test) |
| CLI suite | `python -m pytest tests/cli -q` | **PASS** - 116 passed |
| Fakes suite | `python -m pytest tests/fakes -q` | **PASS** - 68 tests |
| UI tests | `pnpm --dir ui test -- --run` | **PASS** - 11 files, 68 tests |
| UI typecheck/lint/build | `pnpm --dir ui typecheck`, `lint`, `build` | **PASS** (tsc, eslint max-warnings 0, Vite 335 modules) |
| UI screenshots | `python scripts/screenshots.py --url http://127.0.0.1:5199` | **PASS** - 12 pages in `docs/screenshots/` (MSW mock data) |
| Wheel | `python scripts/build_release.py --no-sdist` | **PASS** - wheel includes `api/static/index.html`, schemas, catalogue, seed matrix |
| Bootstrap | `python -m pytest bootstrap/test_bootstrap.py -q` | **PASS** - 37 passed, 1 skipped (no bash available) |
| Docs links | stdlib link-existence script | **PASS** - all relative links resolve |
| Selftest (fakes) | `python -m avdfleet selftest --e2e --fakes --fast --count 1 --json` | **PASS (fakes)** - 17 checks: 15 pass, 0 fail, 2 skip; JSON `ok: true`, evidence `fakes` |
| Selftest (real) | `python -m avdfleet selftest --e2e` | **BLOCKED** on that host - no hypervisor; emulator checks reported SKIP |
| CI | GitHub Actions workflows | **PASS** - [run 45953572457](https://github.com/willokocha-spec/AVD-Fleet-Manager/actions/runs/45953572457) |

### Live host (2026-11-27, real emulator)

| Check | Command | Result |
|---|---|---|
| Acceleration | `emulator -accel-check` | **PASS** - `WHPX(10.0.26200) is installed and usable` |
| Doctor | inside `selftest` | **PASS** - 25 checks |
| Boot | `selftest` `boot` check | **PASS** - 128.7 s to `sys.boot_completed` |
| Real-emulator E2E | `avdfleet selftest --e2e --fast --count 1 --json` | **PASS (real)** - first run ~183 s, `ok: false` (12 pass / 4 skip / 1 fail); after the fixes: **`ok: true`, `exit_code: 0`**, 12 pass / 0 fail / 5 skip, ~102 s, evidence `real-emulator` |
| OOBE evidence | `avdfleet oobe verify` + hand inspection | three recorded images plus the hand-inspected Play Store image; API 25 wizard seen once (45.5 s), repeatability not established |
| OOBE matrix | `avdfleet oobe matrix` | local records for the three `oobe verify` images; Play Store row stays `unverified`; no reset timing |

Full check-by-check table, artifact paths and reproduction commands:
[live-acceptance-2026-11-27.md](live-acceptance-2026-11-27.md).

## Selftest detail

**Fakes mode (2026-11-26).** The fakes selftest boots a fake AVD through the
real RuntimeManager, detects the fake Setup Wizard via the real OOBE manager,
assigns a proxy through the real vault/pool and verifies guest egress through a
loopback listener, runs adb-shell / host-Python / Appium scripts through the
real script service, exercises cancel and timeout, and prints a JSON report with
`"evidence": "fakes"`. It passed 15/17 checks with two intentional skips. This
validates wiring and CLI behaviour; it is **not** emulator evidence.

**Real mode (2026-11-27).** The same command without `--fakes` ran against real
tools on the WHPX host. The first run was **12 pass, 4 skip, 1 fail**
(`ok: false`, `exit_code: 1`) in ~183 s: `oobe` failed because that image ships
no Setup Wizard, and the check itself was defective (it treated any foreground
activity as a pass candidate and knew only `verified`/`failed`). Two further
defects surfaced while re-running: the `instances` check could not see an
orphaned `selftest-01.avd` whose emulator process was still alive, and
acceptance could select a canary/beta image.

All of that was fixed, and the re-run is **green**:

```
ok: true   exit_code: 0   evidence: real-emulator
total 17   pass 12        fail 0        skip 5
```

with `boot`, `scripts.adb`, `scripts.host_python`, `scripts.cancel`,
`scripts.timeout` and `fleet` passing on a real booted AVD, and `oobe` correctly
skipping ("the image ships no Setup Wizard"). The remaining skips are
environmental: portable (installed edition), proxies (none configured),
`scripts.appium` (Appium server not started) and `custom_image` (not supplied).

### Second pass: proxies, Appium, root and API 25 (2026-11-27, later)

**Appium - verified live.** `avdfleet appium start` plus `avdfleet scripts run`
against a real API 34 AVD produced a real UiAutomator2 session
(`0daabe6a-cb8d-40dc-9bd3-b1ab23f037c8`, created in 17.4 s, activity
`com.google.android.apps.nexuslauncher/.NexusLauncherActivity`, screen 1080x2400).
Three defects had to be fixed first: every one-shot CLI command killed the shared
server on exit (`appium status` included); Appium scripts ignored the managed
server's real port and always used `127.0.0.1:4723`; and the first-session
UiAutomator2 APK install inherited Appium's 20 s timeout (observed `adbExec timed
out after 20000ms`). Regression tests cover all three. Note the selftest's
`scripts.appium` check only asserts that `APPIUM_URL`/`UDID` are non-empty - it
never creates a session, so it is not proof of a working session; the session
above is.

**Proxies - G1 isolation measured against 12 real authenticated proxies.**
All 12 worked host-side (one probe each, HTTP 200, 606-6269 ms, 12 distinct exit
IPs). A guest booted with an assigned proxy was proven to egress through the
per-instance bridge (`http_proxy=http://<instance>:<token>@127.0.0.1:7800`, zero
`Proxy will be ignored`, proxy handshakes reaching the bridge), and the assigned
proxy's exit IP (212.129.86.177) differed from the host's CGNAT address. The
upstream password appeared **zero** times in logs, process argv (490 command
lines), the REST `/proxies` view, CLI output, SQLite, the vault and bridge state.
Two findings: the in-process bridge used to die when the `instances start` CLI
exited, so the emulator gave up on its proxy and **silently egressed on the host
network** - fixed with a detached per-instance bridge worker and 9 regression
tests; and the emulator logs its own `http_proxy` value, so the per-instance
*loopback* token appears in `Logs/<instance>-emulator.log` (the upstream
credential never does) - documented, not scrubbed. Known gap: `instances stop` can
leave an orphaned bridge worker, visible in `avdfleet proxies bridges`.

**Rooted images - scripted and measured.** [rooted-images.md](rooted-images.md)
and `scripts/emulator-root-probe.ps1`: `adb root` works on userdebug images;
`-writable-system` + `adb root` + `adb remount` gives a writable `/system` (API 25
immediately, API 34 after an overlayfs reboot); the changes live in the AVD's
`system.img.qcow2`, so they vanish on a plain boot and on AVD deletion; SELinux
can be made permissive per boot. Script self-test: API 25 PASS in 75 s, API 34
PASS in 189 s.

**OOBE on API 25.** [oobe-api25.md](oobe-api25.md) documents the gate
(`ro.setupwizard.mode=EMULATOR`, injected by the emulator and unreachable from
userspace), everything that does *not* cross it, and the three routes that can.
The `farm` Magisk toolkit roots API 25 through a ramdisk patch, and
`magisk resetprop ro.setupwizard.mode OPTIONAL` does change the property - but
applying it after boot is too late for the wizard, so an OOBE-capable API 25
needs the value set before SystemServer starts (a Magisk `post-fs-data.sh`) or a
repacked image.


## GUI screenshots

Twelve screenshots were captured with Playwright + Microsoft Edge against the
Vite dev server with MSW mock data (labelled mock data, not real daemon
evidence):

`docs/screenshots/dashboard.png`, `fleet.png`, `instance-detail.png`, `deploy.png`,
`images.png`, `templates.png`, `proxies.png`, `scripts.png`, `runs.png`, `doctor.png`,
`settings.png`, `portable.png`.

## Still not verified

1. The formal G1 gate - two independent runs with a **guest-measured** egress IP
   ([research/g1/gate.md](../research/g1/gate.md)). Ad-hoc evidence now exists:
   12/12 host probes, the guest proven to route through its per-instance bridge,
   zero upstream-credential leaks, and distinct exit IPs, but the API 34 image
   has no HTTP client able to echo an IP from inside the guest, so the exit IP was
   measured host-side through the identical bridge. Also required: a second run,
   and a concurrent two-instance isolation run (only sequential was possible).
2. A custom-image boot (no custom image supplied).
3. The portable edition on a fresh machine (bundle, launch, relocation,
   filesystem diff) - [research/R7-portable.md](../research/R7-portable.md).
4. Installer container matrix on Ubuntu/Debian/Fedora/Rocky/Arch and accelerated
   runs on macOS HVF / Linux KVM.
5. Five-instance parallel fan-out and the full `--e2e` sequence without
   `--fast --count 1`.
6. Repeatable API 25 OOBE as a **product feature**. It has been proven possible
   by hand - see [oobe-api25.md](oobe-api25.md): root the image (rootAVD +
   Magisk), complete Magisk's "Additional Setup" (without it no module runs),
   install a `post-fs-data.sh` module that does
   `magisk resetprop ro.setupwizard.mode OPTIONAL`, clear the provisioning flags
   and cold boot with snapshots disabled. Two consecutive cold boots landed in
   the real wizard, with screenshots. What is *not* done: wiring that into
   `avdfleet` as a supported, opt-in path (it mutates the shared SDK ramdisk
   unless the image is copied, and it deliberately bypasses a boot-security
   control). Userspace reset strategies still cannot cross the gate, which is why
   `reset-oobe` correctly reports `ok: false` on that image (wipe 64 s,
   package 8.7 s).


No result on this page should be upgraded to "real emulator" without a cited
record like the ones in the live page.
