# Live acceptance - 2026-11-27 (Windows 11 + WHPX)

First real-emulator acceptance pass for AVD Fleet. A hardware-accelerated
Windows 11 host booted stock system images through `avdfleet`, recorded OOBE
evidence and ran the section 7 self-test end to end with `--fast --count 1`.
This page is the raw evidence log; the item-by-item status is in
[acceptance.md](acceptance.md).

Everything below is measured on this host unless a row says otherwise. Nothing
here is fakes-backed.

## Host facts

| Fact | Value |
|---|---|
| OS | Windows 11 Pro, build 26200 (25H2), x86_64 |
| CPU / RAM | 8 logical CPUs, 63.8 GiB RAM |
| Hypervisor | WHPX; `emulator -accel-check` reports `WHPX(10.0.26200) is installed and usable` |
| Android SDK | `C:\Users\Admin\AppData\Local\Android\Sdk` |
| JDK | 25 (Android Studio JBR) |
| Node | 24.19.0 |
| Appium | 3.6.0 |
| adb | 1.0.41 |
| emulator | 37.3.10.0 |

All AVD boots recorded here were **cold boot**, **headless**, with
**`swiftshader_indirect`** rendering and **2048 MiB** RAM.

## Commands run

```powershell
# hypervisor proof
emulator -accel-check

# per-image OOBE verification (boots a temporary cold-boot AVD; API 25 was
# run five times - see the reproducibility caveat below)
avdfleet oobe verify "system-images;android-34;google_apis;x86_64" --timeout 900
avdfleet oobe verify "system-images;android-34;google_apis_playstore;x86_64" --timeout 900
avdfleet oobe verify "system-images;android-33;google_apis;x86_64" --timeout 900
avdfleet oobe verify "system-images;android-25;google_apis;x86" --timeout 900

# matrix of checked-in + local records
avdfleet oobe matrix

# full real-emulator acceptance sequence (183 s, JSON report)
avdfleet selftest --e2e --fast --count 1 --json
```

Guest probes used to inspect the Play Store image by hand:

```powershell
adb shell pm list packages -u
adb shell getprop ro.setupwizard.mode
adb shell dumpsys activity activities
```

## Boot timings

Time from emulator launch to `sys.boot_completed` in the guest:

| Image | Boot | Context |
|---|---|---|
| `system-images;android-34;google_apis;x86_64` | **146.7 s** | first cold boot through the CLI |
| `system-images;android-34;google_apis;x86_64` | **97-134 s** | later `oobe verify` runs |
| `system-images;android-34;google_apis_playstore;x86_64` | **127.7 s** | first boot |
| `system-images;android-25;google_apis;x86` | **45.5 s** | OOBE verification run |
| `system-images;android-37.2-beta3;google_apis_playstore_ps16k;x86_64` | **128.7 s** | `boot` check inside the selftest run |

## OOBE matrix

Results recorded by `avdfleet oobe verify`, the selftest detection and one hand
inspection in this session:

| Image | Result | Observed | Boot |
|---|---|---|---|
| `system-images;android-25;google_apis;x86` | **verified once** (17:04Z run) | `com.google.android.setupwizard/.SetupWizardActivity` | 45.5 s |
| `system-images;android-33;google_apis;x86_64` | **unsupported** | `com.android.settings/.FallbackHome` | 124.1 s |
| `system-images;android-34;google_apis;x86_64` | **unsupported** | `com.android.settings/.FallbackHome` and `com.google.android.googlesdksetup/.DefaultActivity` | 97-134 s |
| `system-images;android-34;google_apis_playstore;x86_64` | **no Setup Wizard** | first boot lands on `com.google.android.apps.nexuslauncher` | 127.7 s |
| `system-images;android-37.2-beta3;google_apis_playstore_ps16k;x86_64` | **no top activity** at selftest detection | screen still off directly after `sys.boot_completed` | 128.7 s |

On the Play Store image the guest shows no wizard package at all: `pm list
packages -u` lists only `com.google.android.googlesdksetup` and
`com.google.android.partnersetup`, `getprop ro.setupwizard.mode` returns
`DISABLED`, and the first boot goes to `com.google.android.apps.nexuslauncher`.
The local matrix has no `oobe verify` record for that image, so its matrix row
stays `unverified`; the facts above come from the hand inspection and the boot.

### Key finding

Modern emulator system images (API 33 and 34, and the API 37 beta) ship **no
Google Setup Wizard** at all and bake `ro.setupwizard.mode=DISABLED` into the
image. A wiped AVD parks on `FallbackHome` - a blank/black HOME placeholder -
and then self-provisions to the launcher. There is no visual OOBE to detect,
screenshot or reset on those images; the real wizard still exists on the older
API 25 `google_apis` image, which boots straight into setup.

The four first-boot states the detector now recognises are
`com.android.settings/.FallbackHome`,
`com.google.android.googlesdksetup/.DefaultActivity`,
`com.google.android.sdksetup/.DefaultActivity` and a real
`com.google.android.setupwizard/.SetupWizardActivity`; only the last is OOBE.
Classifying the emulator SDK-setup stubs as `provisioning` (and therefore
`unsupported`) is a code change made after these measurements, so a rerun of
the API 25 case would now report `unsupported` rather than `failed`; it does not
change the fact that the wizard was not seen.

### Reproducibility caveat (API 25)

Verified once on the 17:04Z run
(`com.google.android.setupwizard/.SetupWizardActivity`, 45.5 s boot).
Repeatability was **not** established: every later run (17:06Z, 17:16Z, 17:21Z
and 17:27Z), including the final one with `wipe_data=True` plus `-no-snapshot`,
landed on the Nexus launcher or an emulator SDK-setup stub. The generic matrix
keeps one record per image and currently reads `failed` (49.8 s,
`com.google.android.sdksetup/.DefaultActivity`).

The practical conclusion is that the API 25 Setup Wizard appears only on the
genuinely first boot of a freshly installed image; an image that has already
been booted on this host cannot be assumed to re-enter OOBE. Treat that as a
documented limitation, not a passing capability. No reset-to-OOBE timing was
recorded (`reset_seconds` is `null` for every record).

## Selftest, check by check

`avdfleet selftest --e2e --fast --count 1 --json` ran in **~183 s** and wrote
`"evidence": "real-emulator"`, `"ok": false`, `"exit_code": 1`. The selftest
picked `system-images;android-37.2-beta3;google_apis_playstore_ps16k;x86_64` as
its image, so it booted an image with no Setup Wizard.

| Check | Result | Measured detail |
|---|---|---|
| `acceleration` | **pass** | WHPX(10.0.26200) installed and usable |
| `doctor` | **pass** | 25 checks |
| `sdk` | **pass** | SDK at `C:\Users\Admin\AppData\Local\Android\Sdk` |
| `portable` | skip | installed edition (portable bundle not built) |
| `image` | **pass** | API 37.2 beta3 Play Store image selected from the installed SDK |
| `template` | **pass** | template created from that image |
| `instances` | **pass** | `selftest-01` created |
| `boot` | **pass** | `sys.boot_completed` in **128.7 s** |
| `oobe` | **fail** | "no OOBE activity detected (status: failed)"; `image_id` = the API 37.2 beta3 Play Store image, `activity: ""`, `screenshot: ""` |
| `proxies` | skip | no proxies configured |
| `scripts.adb` | **pass** | adb-shell script ran on `selftest-01` |
| `scripts.host_python` | **pass** | host Python script ran on `selftest-01` |
| `scripts.appium` | skip | Appium server not started |
| `scripts.cancel` | **pass** | running script cancelled |
| `scripts.timeout` | **pass** | timeout enforced |
| `custom_image` | skip | no custom image supplied |
| `fleet` | **pass** | fleet apply dry run (3 changes) |

That is **17 checks: 12 pass, 1 fail, 4 skip** - recorded as-is; the tally was
not retro-fixed after the check was repaired.

### Notes on the `oobe` failure

- The check was defective at the time: it treated *any* foreground activity as a
  pass candidate and only knew `verified`/`failed`, so an image with no Setup
  Wizard could never be handled gracefully. It now passes only on `verified`,
  **skips** on `unsupported` (no Setup Wizard in the image) and fails otherwise.
  A rerun on the same beta image would therefore report `skip` - but that is a
  code change made after the measurement, not new evidence about the image.
- A rerun intended to show a clean tally aborted early on the `instances` check
  with "an AVD named 'selftest-01' already exists" - an orphaned AVD directory
  whose emulator process was still alive and holding the files open. That defect
  was fixed afterwards (cleanup scans both AVD homes, stops running selftest
  emulators through the runtime *and* kills stray `selftest-*` qemu processes
  from the OS process table, and reports cleanup failures in `notes`).

### Final green run (after the fixes)

Re-running `avdfleet selftest --e2e --fast --count 1 --json` after the fixes
produced a **fully green real-emulator run**:

```
ok: true        exit_code: 0       evidence: real-emulator
total 17        pass 12            fail 0          skip 5
```

- It selected the stable `system-images;android-34;default;x86_64` image; the
  first run had picked the API 37.2 beta Play Store image, and canary/beta
  images are now excluded from acceptance whenever a stable image is installed.
- `boot` passed on a real AVD; `scripts.adb`, `scripts.host_python`,
  `scripts.cancel`, `scripts.timeout` and the `fleet` dry run all passed.
- `oobe` reported **skip**: "the image ships no Setup Wizard, so there is no OOBE
  to detect" - the honest outcome for a modern image, not a failure.
- Remaining skips are environmental: portable (installed edition), proxies (none
  configured), `scripts.appium` (Appium server not started) and `custom_image`
  (not supplied).

The 183 s run above is kept as the historical first run; the green run is the
acceptance result.

## Evidence on disk

All paths are under `%LOCALAPPDATA%\avdfleet` (on this host
`C:\Users\Admin\AppData\Local\avdfleet`). No images are embedded in this page.

- `screenshots\oobe-<image>-<utc-timestamp>.png` - one screenshot per
  `oobe verify`, named from the image id and the run timestamp.
  `screenshots\live\` holds the same PNGs deduplicated under short content
  hashes. Examples:
  - `oobe-avdfleet-oobe-system-images-android-25-google_ap-system-images-android-25-google_apis-x86-20261127T170416729207.png`
    - the verified API 25 Setup Wizard screen;
  - `oobe-...-android-33-...-20261127T170707957390.png` - `FallbackHome`;
  - `oobe-...-android-34-...-20261127T170255191734.png` -
    `googlesdksetup/.DefaultActivity`;
  - `oobe-...-android-25-...-20261127T171638624137.png` - the launcher from a
    later API 25 run.
- `artifacts\oobe\*-detection.txt` - the detection evidence behind each record:
  a header (`avd`, `image_id`, `serial`, `detected_at`, `top_activity`,
  `activity_source`, `classification`, `api_level`, `abi`, `release`,
  `boot_completed`) followed by `dumpsys activity activities` and
  `dumpsys window`. Key files:
  - `system-images-android-25-google_apis-x86-20261127T170416732481-detection.txt`
    - `classification: setup_wizard`;
  - `system-images-android-33-google_apis-x86_64-20261127T170707963743-detection.txt`
    - `classification: provisioning`;
  - `system-images-android-34-google_apis-x86_64-20261127T170255195039-detection.txt`
    - `classification: provisioning`;
  - `system-images-android-37.2-beta3-google_apis_pla-20261127T171112221565-detection.txt`
    - `avd: selftest-01`, no top activity.
- `Logs\*-emulator.log` - emulator stdout/stderr for every boot, e.g.
  `Logs\avdfleet-oobe-system-images-android-25-google_ap-e820f5-emulator.log`
  and `Logs\selftest-01-emulator.log`.
- `oobe_matrix.json` - the local matrix that `avdfleet oobe matrix` merges with
  the checked-in seed records.
- `artifacts\script-runs\<run-id>\selftest-01\` - script sources left behind by
  the selftest script checks.

## What this does not prove

- Proxy isolation (G1): no proxies were configured, so `proxies` skipped; there
  is no guest-egress or credential evidence. Unverified.
- Appium on a real session: the server was not running, so `scripts.appium`
  skipped. Unverified.
- Custom-image boot: no custom image was supplied, so `custom_image` skipped.
  Unverified.
- The portable edition on a fresh machine: this host runs the installed
  edition. Unverified.
- Installer containers on other distros, and macOS HVF / Linux KVM hosts: not
  run.
- Five-instance parallel fan-out and the full `--e2e` sequence without
  `--fast`/`--count 1`: not run.
- GUI flows: the captured GUI screenshots in `docs/screenshots/` are still MSW
  mock data, not real-host evidence.
- OOBE on Play Store images: these images have no Setup Wizard, so there is
  nothing to verify there; that is a finding, not a pass.
- Repeatable OOBE on API 25: the wizard appeared only on the genuinely first
  boot of the installed image; every later run (17:06Z, 17:16Z, 17:21Z and
  17:27Z, the last with `wipe_data=True` plus `-no-snapshot`) did not restore
  it.

## How to reproduce

On a Windows 11 host with virtualization enabled and the SDK installed:

```powershell
# 0. hypervisor must be usable
emulator -accel-check

# 1. install the images used here
avdfleet images download "system-images;android-34;google_apis;x86_64"
avdfleet images download "system-images;android-34;google_apis_playstore;x86_64"
avdfleet images download "system-images;android-33;google_apis;x86_64"
avdfleet images download "system-images;android-25;google_apis;x86"

# 2. host checks, then per-image OOBE verification
avdfleet doctor
avdfleet oobe verify "system-images;android-34;google_apis;x86_64" --timeout 900 --json
avdfleet oobe verify "system-images;android-34;google_apis_playstore;x86_64" --timeout 900 --json
avdfleet oobe verify "system-images;android-33;google_apis;x86_64" --timeout 900 --json
avdfleet oobe verify "system-images;android-25;google_apis;x86" --timeout 900 --json

# 3. matrix and the acceptance sequence
avdfleet oobe matrix
avdfleet selftest --e2e --fast --count 1 --json
```

Expect the same order of magnitude for boot times (roughly 45-150 s for a cold
headless boot on this class of host), not identical seconds: emulator version,
image, disk cache and host load all move the number. The API 25 `verified`
result is not yet reproducible - rerun the matrix after each attempt and keep
the generated screenshots and detection files.
