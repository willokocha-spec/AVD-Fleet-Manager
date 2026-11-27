# R3 — OOBE matrix: candidate images, boot evidence and reset timing

> **STATUS: BLOCKED — NOT EXECUTED ON THIS HOST.**
>
> This document is the executable method that will be run on an accelerated host. It
> contains **zero real-emulator evidence**. The build host cannot boot an Android
> emulator (no usable hardware virtualisation; `VirtualizationFirmwareEnabled=False`,
> no WHPX/KVM/HVF) and this build was explicitly forbidden to install the Android SDK,
> download images or start an emulator. See section 9 for the exact host facts.
>
> The only matrix-like data in this checkout is the *fake* seed in
> `contracts/fakes-spec.md` §6 (API 35/36 Play images marked `verified`, API 35
> `google_apis` `unverified`, one API 33 `failed`). That data exists to test the
> `oobe/` code path; it is **not** OOBE evidence and must never be cited as an R3
> result. No `oobe_matrix.json` has been produced by this gate.
>
> R3a and R3b are independent runs of this method (BRIEF.md §5); G1 and the final
> report must cite both run directories, not one agent's recollection.

## 1. Scope and acceptance linkage

BRIEF.md §3.5 requires, for every candidate system image supported by the host ABI:
boot a fresh AVD, record whether the first-boot Setup Wizard appears, the activity
name, a screenshot, boot duration, image id/checksum and timestamp; mark unknown
images `unverified`; define a per-image reset-to-OOBE strategy; and measure
golden-snapshot reset time.

BRIEF.md §7 items 2, 3 and 8 depend on this result:

- item 2 needs an image that `oobe_matrix.json` marks `verified` before deploying 5 AVDs;
- item 3 needs each of the 5 AVDs to reach the first Setup Wizard (activity + screenshot)
  and needs reset-to-OOBE plus measured golden-snapshot reset time;
- item 8's real-emulator E2E is only meaningful once this matrix exists.

The matrix is also the source for `avdfleet oobe verify` expectations and for the
`oobe_status` field read by the images catalogue (`OobeStatus`: `verified`, `failed`,
`unverified`, `unsupported`).

## 2. Host prerequisites and preflight (accelerated host only)

Run preflight and record it to `meta/preflight.txt`. Any failed check means **STOP**:
do not install, do not boot, do not fabricate matrix records.

| Check | Windows | Linux | macOS |
|---|---|---|---|
| Hardware acceleration | `emulator -accel-check`; WHPX enabled (optional feature `HypervisorPlatform`), reboot after enabling; AEHD alternative only if WHPX impossible | `emulator -accel-check`; `/dev/kvm` present and `r-x` for the user; `kvm-ok` where available; `grep -cE 'vmx|svm' /proc/cpuinfo` > 0 | `emulator -accel-check`; `sysctl kern.hv_support` = 1 |
| Firmware virtualisation | `Get-CimInstance Win32_Processor` → `VirtualizationFirmwareEnabled=True`, `VMMonitorModeExtensions=True` | BIOS/UEFI VT-x/AMD-V on; `dmesg | grep -i kvm` shows KVM initialised | Apple Silicon: HVF is built in; no extra firmware toggle |
| Nested-virtualisation note | cloud/VM guests must expose nested virt; if not, record `nested=unsupported` and stop | same | same |
| RAM | API ≤36: ≥2 GB per concurrent AVD plus ~4 GB host; API 37+: BRIEF.md §7 uses ≥4 GB per AVD and suggests a 32 GB+ host. Measure per image and record actual | same | same |
| Disk | free space ≥ the documented budget (see `research/E0-host.md` §3); measure before downloads | same | same |
| CPU | x86_64 or arm64 host ABI; images are chosen to match the host ABI (`arm64-v8a` on Apple Silicon, `x86_64` on Intel/AMD) | same | same |

Record `emulator -version`, `adb version`, `sdkmanager --version`, `avdmanager` version,
OS build, CPU/ABI, free RAM and free disk in `meta/versions.json` / `meta/host.json`.
The preflight result is itself evidence: `meta/preflight.txt` plus the command log.

## 3. Candidate image matrix (API/ABI/tag)

Do not hardcode availability: on the accelerated host, enumerate what the SDK
channel actually publishes, then run every candidate for the host ABI. The table
below is the candidate set at the time of writing; package availability and
latest preview API vary by channel and date.

Discovery command (POSIX; adapt on Windows):

```bash
sdkmanager --list --channel=0 \
  | grep -E 'system-images;android-(34|35|36|37);' \
  | grep -E ';(x86_64|arm64-v8a)[[:space:]]*$' \
  | sort -u
```

Filter rule: API ∈ {34, 35, 36, 37 (if published)}, ABI = host ABI, tag ∈
{`google_apis_playstore`, `google_apis`, `aosp_atd`, `google_atd`, `default`,
`google_apis_playstore_ps16k`}. The primary matrix is the Play/Google APIs set;
ATD/AOSP variants are included because the brief asks about every candidate image,
not only the ones expected to show the wizard.

| API | ABI | Tag | Package id (template) | Expected first boot (hypothesis — matrix records reality) |
|---|---|---|---|---|
| 34 | x86_64 / arm64-v8a | `google_apis_playstore` | `system-images;android-34;google_apis_playstore;<abi>` | Setup Wizard expected (primary candidates) |
| 34 | x86_64 / arm64-v8a | `google_apis` | `system-images;android-34;google_apis;<abi>` | Setup Wizard likely; verify |
| 35 | x86_64 / arm64-v8a | `google_apis_playstore` | `system-images;android-35;google_apis_playstore;<abi>` | Setup Wizard expected (primary) |
| 35 | x86_64 / arm64-v8a | `google_apis` | `system-images;android-35;google_apis;<abi>` | Setup Wizard likely; verify |
| 35 | x86_64 / arm64-v8a | `aosp_atd`, `google_atd` | `system-images;android-35;<tag>;<abi>` | ATD is an automated-test image: no/few wizard screens expected → likely `failed` |
| 36 | x86_64 / arm64-v8a | `google_apis_playstore` | `system-images;android-36;google_apis_playstore;<abi>` | Setup Wizard expected (primary; CI-friendly API level) |
| 36 | x86_64 / arm64-v8a | `google_apis` | `system-images;android-36;google_apis;<abi>` | Setup Wizard likely; verify |
| 36 | x86_64 / arm64-v8a | `aosp_atd`, `google_atd` | `system-images;android-36;<tag>;<abi>` | likely no wizard |
| 37 (if published) | host ABI only | `google_apis_playstore`, `google_apis` | `system-images;android-37;<tag>;<abi>` | Newest API; treat as preview/experimental; ≥4 GB RAM per AVD per BRIEF §7; record channel (`--channel=3` preview vs stable) |
| any API | host ABI | `default` (AOSP) | `system-images;android-<api>;default;<abi>` | AOSP usually has no Google Setup Wizard → record `failed`/`unverified` with activity proof |
| 35/36 optional | host ABI | `google_apis_playstore_ps16k` | `system-images;android-<api>;google_apis_playstore_ps16k;<abi>` | 16 KB page-size images; include only if published and the emulator build supports them |

Rules:

- One matrix record per installed image, keyed by the SDK package id (use
  `system-images;android-<api>;<tag>;<abi>` as `image_id` until the catalogue ships a
  different stable id; the evidence must include the package id verbatim so the
  mapping to `SystemImage.id` can be resolved later).
- Download only the images for the host ABI. Cross-ABI boots (`arm64-v8a` image on
  x86_64 host or vice versa) are out of scope; record `unsupported` if attempted.
- Never complete the wizard and never sign into a Google account (BRIEF.md §8).
- If an image is not published/available, record `unsupported` (or omit and list it
  in the run README as unavailable) — do not invent observed data.
- Play Store images require accepting the SDK licence with the explicit flags; the
  same image set is used by the product, so record the licence acceptance step.

## 4. Run protocol per image

All timestamps are UTC ISO-8601 with milliseconds (`2026-11-26T18:00:00.123Z`).
Record a JSON-lines command/timestamp journal (`timeline.jsonl`) with every action and
the exact command line so the run can be replayed.

### 4.1 Catalogue and checksum capture

```bash
# installed package metadata + version
sdkmanager --list_installed --verbose > meta/sdk-installed.txt
# per image: installed directory, package.xml, system.img checksum
AVD_SYS="$ANDROID_SDK_ROOT/system-images/android-36/google_apis_playstore/x86_64"
sha256sum "$AVD_SYS/package.xml" "$AVD_SYS/system.img" > images/<image_id>/sha256.txt
```

Record: package id, SDK channel, installed path, `system.img` sha256, `package.xml`
sha256, downloaded package version, emulator version, host ABI. `sdkmanager` does not
necessarily retain the downloaded zip hash, so hash the installed artefacts and the
package metadata.

### 4.2 Fresh AVD creation

```bash
avdmanager create avd --force \
  --name "r3a-<api>-<tag>-<abi>" \
  --package "system-images;android-<api>;<tag>;<abi>" \
  --device "pixel_7"
```

Then set deterministic settings in `<avd_home>/<name>.avd/config.ini` (copy the file to
evidence):

```ini
hw.ramSize=2048          # 4096 for API 37+ / Play images if the image needs it
hw.cpu.ncore=2
disk.dataPartition.size=6G
hw.gpu.enabled=yes
hw.gpu.mode=swiftshader_indirect
fastboot.forceColdBoot=yes
```

`hw.gpu.mode` is a headless-GPU choice; if the host reports no usable GPU, use
`swiftshader_indirect` and record the choice. For a guaranteed first boot:
`-no-snapshot -wipe-data` on the first launch.

### 4.3 Cold boot and boot timing

Start the emulator with a unique console port and capture stdout/stderr separately:

```bash
# t0 = timestamp immediately before spawning the emulator process
emulator -avd "r3a-<api>-<tag>-<abi>" \
  -no-window -no-audio -no-boot-anim \
  -gpu swiftshader_indirect \
  -no-snapshot -wipe-data \
  -port 5554 \
  > images/<image_id>/run-01/emulator.stdout.log \
  2> images/<image_id>/run-01/emulator.stderr.log &
```

Poll (1 s interval, per-image timeout e.g. 300 s) and record each transition:

| Marker | Command | Meaning |
|---|---|---|
| `t_adb` | `adb -s emulator-5554 wait-for-device` then `adb -s emulator-5554 get-state` = `device` | adb transport up |
| `t_boot` | `adb -s emulator-5554 shell getprop sys.boot_completed` = `1` | boot completed |
| `t_pm` | `adb -s emulator-5554 shell pm path android` non-empty | package manager ready |
| `t_home` | `adb -s emulator-5554 shell cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME` | launcher resolvable |
| `t_wizard` | wizard activity focused/resumed (see 4.4) plus `user_setup_complete` = `0` | OOBE visible |

Also record `adb -s emulator-5554 shell uptime`, `getprop ro.build.fingerprint`,
`ro.build.version.sdk`, `ro.build.version.release`, `ro.product.cpu.abi`,
`ro.kernel.qemu`, `adb devices -l`, and the console port mapping.

`boot_seconds` in the matrix is `t_wizard - t0` for the first successful run (the
brief asks for boot duration; also store the intermediate markers so the definition is
unambiguous). Keep one cold-boot sample per image for the matrix and at least two more
for the reset timing baseline, or state clearly that the reset baseline is the same
cold-boot measurement.

### 4.4 OOBE detection and evidence

Run all of the following while the device is up; do not tap through the wizard.

```bash
SER="emulator-5554"
AVD_EV=images/<image_id>/run-01
adb -s $SER shell dumpsys activity activities > "$AVD_EV/dumpsys-activity.txt"
adb -s $SER shell dumpsys window windows   > "$AVD_EV/dumpsys-window.txt"
adb -s $SER shell dumpsys package com.google.android.setupwizard > "$AVD_EV/dumpsys-setupwizard.txt" 2>&1 || true
adb -s $SER shell settings get secure user_setup_complete > "$AVD_EV/user_setup_complete.txt"
adb -s $SER shell settings get global device_provisioned > "$AVD_EV/device_provisioned.txt"
adb -s $SER shell pm path com.google.android.setupwizard > "$AVD_EV/setupwizard-path.txt" 2>&1 || true
adb -s $SER exec-out screencap -p > "$AVD_EV/screenshot-01.png"
adb -s $SER shell uiautomator dump /sdcard/r3-window.xml >/dev/null 2>&1 || true
adb -s $SER pull /sdcard/r3-window.xml "$AVD_EV/uiautomator.xml" >/dev/null 2>&1 || true
adb -s $SER logcat -d -b all > "$AVD_EV/logcat.txt" 2>&1 || true
```

Take a second screenshot 5 s later (`screenshot-02.png`) so a transient splash/black
screen is not mistaken for the wizard. Validation rules:

- `dumpsys activity activities` must contain a resumed/top-resumed component for
  `com.google.android.setupwizard` (or `com.android.setupwizard` on non-Google
  images) — record the exact component string.
- `dumpsys window` should show the same component as `mCurrentFocus`/`mFocusedApp`.
- `user_setup_complete` = `0` and `device_provisioned` = `0` strengthen first-boot
  evidence; if either is `1`, the wizard must be reached by a legitimate reset, not by
  claiming a fresh boot.
- `uiautomator.xml` must contain a sane hierarchy for the foreground package (a
  wizard page or at least the setup-wizard package); note that wizard text is
  localised, so do not match English strings only.
- PNG validation: file starts with `89 50 4E 47 0D 0A 1A 0A`, has plausible width and
  height in the IHDR (record them), and is not a blank single-colour frame. Record
  PNG sha256 and byte size.
- Capture `adb shell wm size`, `wm density`, and the device profile in evidence.

Classification for the matrix record follows section 5. If the wizard activity is not
observed within the timeout but boot completed, capture the launcher/home screen
evidence and classify `failed` with the observed activity and reason.

### 4.5 Cross-check with the product path (after Wave 1 lands)

The raw protocol above is canonical for R3. Once `oobe/` and the CLI exist, repeat the
same image check with the product path (`avdfleet oobe verify ...` / the GUI
"verify OOBE" action) and attach its JSON record plus the raw evidence. Product and raw
records must agree on `image_id`, activity and status; disagreement is a bug, not a
matrix result.

### 4.6 Golden snapshot, reset strategy and reset timing

The requirement is a *per-image* reset strategy and a measured golden-snapshot reset
time. For every image that reaches the wizard:

1. **Create the golden snapshot before completing the wizard.** Keep the wizard on
   screen; then save a snapshot and record the save duration and files:
   ```bash
   adb -s emulator-5554 emu avd snapshot save avdfleet-golden
   adb -s emulator-5554 emu avd snapshot list > images/<image_id>/snapshot-list.txt
   ```
   Record snapshot directory/file sizes and confirm the snapshot is listed. If the
   emulator console requires auth, use the emulator's console auth token (the
   `avdfleet` OOBE service should encapsulate this; the raw console is acceptable for
   R3 evidence).
2. **Snapshot reset trial (3 trials).** Stop the emulator (`adb -s ... emu kill`, wait
   until the process exits), record `t0`, then launch:
   ```bash
   emulator -avd "<avd>" -no-window -no-audio -no-boot-anim \
     -gpu swiftshader_indirect -snapshot avdfleet-golden -no-snapshot-save -port 5554
   ```
   Measure `t_wizard` again (activity + `user_setup_complete=0` + fresh screenshot).
   `reset_seconds = t_wizard - t0`; record all trials, report median/min/max.
3. **Wipe trial (3 trials).** Stop, then launch with `-wipe-data -no-snapshot`
   (no snapshot); measure the same markers. Wipe is deterministic but slower; it is the
   fallback if the snapshot cannot be restored on some images.
4. **Package/state cleanup fallback.** Only when snapshot and wipe are both unusable:
   ```bash
   adb -s emulator-5554 shell pm clear com.google.android.setupwizard
   adb -s emulator-5554 shell settings put secure user_setup_complete 0
   adb -s emulator-5554 shell settings put global device_provisioned 0
   adb -s emulator-5554 reboot
   ```
   Record why the fallback was needed and re-verify the wizard with 4.4. This is
   invasive; never prefer it over snapshot/wipe.
5. **Strategy decision order** for the matrix record: `snapshot` if all 3 trials reach
   OOBE within timeout; else `wipe`; else `package`; else `failed` with evidence.
   Record the chosen strategy and why (`OobeResetStrategy` enum: `snapshot`, `wipe`,
   `package`, `reboot`).
6. Report golden-snapshot reset time in the matrix record (`reset_seconds`) and in the
   run README; state that it is a start-to-wizard measurement, not a console load time,
   and note host load conditions.

### 4.7 Teardown

```bash
adb -s emulator-5554 emu kill
# wait for process exit, then:
avdmanager delete avd --name "r3a-<api>-<tag>-<abi>"
# optionally keep the downloaded image for the next R3b/G1 run; record that decision
```

Check that no emulator/adb-server processes and no console ports (5554/5555, grpc)
remain; record `adb devices` after teardown.

## 5. Classification rules

| Status | Rule |
|---|---|
| `verified` | Boot completed; Setup Wizard component observed as the foreground activity in `dumpsys activity` **and** `dumpsys window`; `user_setup_complete=0`; screenshot(s) captured and valid; repeatable on a second run (R3a and R3b both see it, or same operator boots twice). |
| `failed` | Boot completed within timeout but no Setup Wizard observed (e.g., ATD/AOSP home screen); or wizard component appears but the screen is invalid/blank and UI hierarchy disagrees. Record observed activity + screenshots + reason. |
| `unverified` | Not run, run timed out, device offline, checks missing, or evidence incomplete. This is the default for any image without both a valid record and complete evidence. |
| `unsupported` | Image not available for the host ABI on the used SDK channel, or the host cannot run that ABI/API (e.g., insufficient RAM for API 37+). |

A first-boot run only becomes `verified` when R3a and R3b agree on image checksum,
activity and status. Disagreements go to the root for review; do not overwrite one
run's evidence with the other's.

## 6. Evidence directory layout and record schema

Planned locations (written only on the accelerated host; **nothing was created here**):

```
research/
  R3-oobe.md                                  # this method + final summary
  oobe/
    r3a-<YYYYMMDDTHHMMSSZ>-<host>/            # R3a independent run
      meta/host.json                          # OS, CPU, RAM, disk, ABI, hypervisor proof
      meta/versions.json                      # emulator/adb/sdkmanager/avdmanager versions
      meta/preflight.txt                      # accel-check + hypervisor commands/output
      meta/sdk-installed.txt                  # sdkmanager --list_installed --verbose
      meta/licences.txt                       # acceptance flags/commands used
      images/<image_id>/
        sha256.txt                            # system.img + package.xml sha256
        avd-config.ini                        # copy of config.ini used
        run-01/
          emulator.stdout.log, emulator.stderr.log
          timeline.jsonl                      # every command + timestamp + result
          boot-timings.json                   # t0,t_adb,t_boot,t_pm,t_home,t_wizard
          dumpsys-activity.txt, dumpsys-window.txt, dumpsys-setupwizard.txt
          user_setup_complete.txt, device_provisioned.txt, setupwizard-path.txt
          uiautomator.xml, screenshot-01.png, screenshot-02.png, logcat.txt
        reset/
          snapshot-list.txt, snapshot-save.json
          snapshot-<01..03>/...               # same evidence set per reset trial
          wipe-<01..03>/...
          package-<01..03>/...                # only if the fallback was used
          reset-timings.json                  # trials + median/min/max + strategy
      matrix/oobe_matrix.json                 # candidate output for review
      README.md                               # operator, host, UTC window, verdict
      checksums.sha256                        # sha256 of every evidence file
    r3b-<...>/                                # independent repeat, same layout
```

Record fields map to `OobeRecord` (`src/avdfleet/core/models.py`):

| `OobeRecord` field | R3 evidence source |
|---|---|
| `image_id` | SDK package id (`system-images;android-<api>;<tag>;<abi>`) |
| `api_level`, `tag`, `abi` | package id + installed metadata |
| `status` | classification in section 5 |
| `activity` | exact resumed/top-resumed component from dumpsys |
| `first_run_component` | wizard component / launcher component observed |
| `screenshot_path` | relative path under the run dir (plus sha256) |
| `boot_seconds` | `t_wizard - t0` (cold, first boot) |
| `reset_seconds` | median of snapshot reset trials |
| `checked_at` | UTC ISO-8601 |
| `checked_by` | operator/agent id (`R3a` / `R3b`) |
| `emulator_version` | `emulator -version` |
| `notes` | image channel/API level caveats, failure reason, timeout |
| `evidence` | list of relative evidence paths + sha256 |

`OobeMatrix` wrapper: `version`, `generated_at`, `host_abi`, `records[]`. Merge both
independent runs only after agreement; publish the checked-in `oobe_matrix.json` from
the reviewed merged result (location decided by the root; the data dir copy is
`Paths.oobe_matrix_path`). Never publish a matrix record without its evidence files and
checksums.

## 7. Commands quick reference

POSIX shell (Linux/macOS), one image, fresh boot:

```bash
SDK="${ANDROID_SDK_ROOT:?set ANDROID_SDK_ROOT}"
export PATH="$SDK/cmdline-tools/latest/bin:$SDK/platform-tools:$SDK/emulator:$PATH"
PKG="system-images;android-36;google_apis_playstore;x86_64"
NAME="r3a-36-play-x86_64"
OUT="research/oobe/r3a-$(date -u +%Y%m%dT%H%M%SZ)-$(hostname)/images/$PKG"
mkdir -p "$OUT/run-01" "$OUT/reset"
sdkmanager --install "$PKG"
sdkmanager --licenses
avdmanager create avd --force --name "$NAME" --package "$PKG" --device pixel_7
t0=$(python3 -c 'import time; print(time.time())')
emulator -avd "$NAME" -no-window -no-audio -no-boot-anim \
  -gpu swiftshader_indirect -no-snapshot -wipe-data -port 5554 \
  > "$OUT/run-01/emulator.stdout.log" 2> "$OUT/run-01/emulator.stderr.log" &
adb -s emulator-5554 wait-for-device
until [ "$(adb -s emulator-5554 shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; do sleep 1; done
adb -s emulator-5554 shell dumpsys activity activities > "$OUT/run-01/dumpsys-activity.txt"
adb -s emulator-5554 exec-out screencap -p > "$OUT/run-01/screenshot-01.png"
t1=$(python3 -c 'import time; print(time.time())')
printf '{"t0":%s,"t_wizard":%s,"boot_seconds":%s}\n' "$t0" "$t1" "$(python3 -c "print($t1-$t0)")" \
  > "$OUT/run-01/boot-timings.json"
```

PowerShell/Windows equivalents use the same commands with `$env:ANDROID_SDK_ROOT`,
`$t0 = [DateTime]::UtcNow`, `Start-Process -PassThru -RedirectStandardOutput ...` and
`Select-String` instead of `grep`; keep the journal machine-readable
(`timeline.jsonl`) rather than relying on console scrollback.

## 8. Definition of done for R3

- `research/oobe/r3a-*` and `r3b-*` both contain a complete run per candidate image
  for the host ABI, with `meta/`, per-image evidence and `checksums.sha256`.
- Every record has a status; `verified` only where the wizard was observed twice and
  both independent runs agree.
- For every `verified` image: chosen reset strategy is recorded, snapshot reset time is
  measured over 3 trials (median/min/max), and a post-reset wizard activity +
  screenshot + `user_setup_complete=0` were captured.
- The merged matrix is reviewed and checked in; this document's status header is
  updated to **EXECUTED** with the run window, host, and per-image table, or the run
  remains explicitly `blocked` with the failing preflight evidence.
- The final report marks these results **real emulator** (and names the host) or
  leaves them fakes/blocked. No exceptions.

## 9. Why this is blocked on this build host (evidence)

Host facts inspected 2026-11-26 (read-only; nothing installed or downloaded):

- OS: Windows 11 Pro, build `10.0.26300`, `OSArchitecture=ARM 64-bit`; platform
  `VMware, Inc. / VMware20,1` (the machine is itself a VM).
- CPU: model reported as `Apple silicon`, 4 vCPUs (1 core each);
  `VirtualizationFirmwareEnabled=False`,
  `SecondLevelAddressTranslationExtensions=False`,
  `VMMonitorModeExtensions=False`. `Win32_ComputerSystem.HypervisorPresent=True` only
  because an outer VMware hypervisor exists; no nested acceleration is exposed.
- Windows optional features: `HypervisorPlatform` (WHPX) `InstallState=2` (disabled),
  `Microsoft-Hyper-V-All` `InstallState=2`, `VirtualMachinePlatform` `InstallState=2`;
  `systeminfo` reports "A hypervisor has been detected. Features required for Hyper-V
  will not be displayed." There is no usable WHPX for the emulator.
- RAM: 16 GiB physical (`17179869184` bytes), visible `17174372352` bytes, ~6.1 GB
  free at inspection.
- Disk: `C:` 67,588,059,136 bytes total, 14,490,906,624 bytes free (~13.5 GiB) at
  inspection — below the budget for the SDK plus a multi-API image matrix.
- Tooling: `ANDROID_HOME`/`ANDROID_SDK_ROOT`/`JAVA_HOME` unset; no `adb`, `emulator`,
  `sdkmanager`, `avdmanager`, `java`, `node`, `appium` or `uv` on PATH; no
  `C:\Android`, no `%LOCALAPPDATA%\Android\Sdk`, no `%USERPROFILE%\.android`. Python is
  3.14.10 (the project requires `<3.14`; this only matters for product tests, not for
  the stdlib listener harness). No `git`/`openssl` on PATH.
- BRIEF.md §6: the emulator requires WHPX, KVM or HVF; there is no supported software
  fallback (`-accel off` is unsupported and far too slow). BRIEF.md §6 also forbids
  claiming real-emulator evidence on this host.
- Build policy for this agent explicitly forbids installing the Android SDK,
  downloading images and starting an emulator, so even a partial matrix (one image)
  is not permitted here.

Consequence: R3 is **BLOCKED**. The deliverable is this executable method plus the
evidence requirements above. Real OOBE evidence must be produced by running this
protocol on an accelerated host (and/or by `avdfleet selftest --e2e` on that host once
the product path exists); the final report must say so explicitly.

## 10. Risks and limitations

- **API 37 / preview availability** changes by SDK channel; package ids in section 3
  are candidates, not guarantees. The run must discover the actual set with
  `sdkmanager --list` and record the channel.
- **Snapshot reliability** varies by image/guest RAM and emulator build; a snapshot
  may save but restore to a non-wizard state. Always verify post-reset OOBE; do not
  assume a snapshot is valid because `snapshot list` shows it.
- **Network-dependent first boot**: Setup Wizard may wait on connectivity; classify
  with evidence (activity/screenshot), do not complete or skip the wizard.
- **ATD/AOSP images** exist for automation and may have no wizard at all; they are
  legitimate `failed` matrix rows, not defects.
- **Headless GPU modes** can produce a black screenshot on some hosts; use a second
  screenshot and check UI hierarchy. Record `hw.gpu.mode` with every record.
- **Resource pressure**: API 37+ AVDs need ≥4 GB each (BRIEF.md §7); a 16 GB host
  cannot run the matrix concurrently — serialize and record actual RAM.
- **Screenshot/evidence integrity**: store sha256 for every file; a matrix record
  without its evidence files is treated as `unverified`.
- **Play images**: do not sign in to Google accounts or bypass Play Integrity — out of
  scope per BRIEF.md §8.

## 11. Sources

All checked in BRIEF.md "Key sources" on 2026-11-26; re-verify command syntax against
the installed emulator version during the run.

- Emulator command line: <https://developer.android.com/studio/run/emulator-commandline>
- Emulator acceleration: <https://developer.android.com/studio/run/emulator-acceleration>
- Emulator snapshots: <https://developer.android.com/studio/run/emulator-snapshots>
- Emulator console (snapshot/console auth commands): <https://developer.android.com/studio/run/emulator-console>
- Emulator networking: <https://developer.android.com/studio/run/emulator-networking>
- `sdkmanager`: <https://developer.android.com/tools/sdkmanager>
- `avdmanager`: <https://developer.android.com/tools/avdmanager>
- `adb`: <https://developer.android.com/tools/adb>
- `dumpsys`: <https://developer.android.com/tools/dumpsys>
- UIAutomator / `uiautomator dump`: <https://developer.android.com/training/testing/other-components/ui-automator>
- Snapshot/AVD concepts: <https://developer.android.com/studio/run/managing-avds>
