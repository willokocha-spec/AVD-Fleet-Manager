# E0 — Test-host provisioning: Android SDK + target system images

> **STATUS: BLOCKED — NOT RUN ON THIS HOST.**
>
> No Android SDK, command-line tools, emulator, JDK, system images or AVDs have been
> installed or downloaded on this build host, and per this build's instructions none
> may be. This document is (a) the runbook that E0 will execute on an accelerated test
> host and (b) the recorded current state of this host that makes E0 impossible here.
> See section 5 for the inspected facts and section 7 for the blockage chain.

## 1. Purpose and linkage

BRIEF.md §5 defines E0 as: *install the SDK and download target images in the
background* on the accelerated host, so that R3 (OOBE matrix), G1 (proxy isolation) and
the E2E scenarios can run. BRIEF.md §2 (R-INSTALL) and §3.2 define the canonical
one-command installer; E0 is the research-time host prep, not a replacement for the
installer.

E0 is a prerequisite for:

- R3 — OOBE matrix needs `emulator`, `platform-tools`, `avdmanager`, `sdkmanager`, the
  candidate system images and `build-tools` (probe APK), `research/R3-oobe.md` §3.
- G1 — proxy isolation needs 5 bootable AVDs from a verified image and a real guest,
  `research/g1/gate.md`.
- E2E — the §7 scenarios and `avdfleet selftest --e2e` on the accelerated host.

E0 output is a **provisioned host** plus a machine-readable manifest of exactly what was
installed/downloaded (versions, package ids, sizes, SHA-256 where available, timestamps,
command log) that R3/G1 cite.

## 2. Target host specification (must be true before E0 starts)

| Requirement | Minimum / recommendation | Notes |
|---|---|---|
| Hardware virtualisation exposed | WHPX (Windows), KVM (Linux) or HVF (Apple Silicon); `emulator -accel-check` exit 0 | On cloud/VM hosts this means **nested virtualisation** is exposed; if not, E0 must stop, not install-and-hope |
| OS/arch | Windows 11 or Server with WHPX/AEHD; Ubuntu LTS / Fedora / Rocky / Arch with KVM; macOS Apple Silicon | ARM host uses `arm64-v8a` images; Intel/AMD host uses `x86_64` |
| Admin rights | Yes (SDK install, KVM udev/group, WHPX enable + reboot) | Licences accepted only via the explicit installer flag (BRIEF.md §2/§3.2) |
| RAM | 16 GiB absolute minimum; **≥32 GiB recommended** for 5 concurrent AVDs; API 37+ AVDs need ≥4 GiB each (BRIEF.md §7) | E0 records actual RAM; R3/G1 schedule concurrency accordingly |
| Free disk | **≥60 GiB** for a lean single-image + 5-AVD run; **≥120 GiB** to hold the full candidate image matrix plus AVD data/snapshots | Measure and record; formula in §3.4. Current build host has ~13.5 GiB free — a blocker |
| Network | Stable egress to the Android SDK repository (`dl.google.com`); wired preferred; metered connections avoided | Downloads are multi-GB and can take a long time; E0 runs them in the background with logging |
| Egress accounting | Record bytes downloaded and time taken | Useful for acceptance reports and CI runner planning |
| Time zone/clock | UTC timestamps in all evidence | ISO-8601 with milliseconds |
| No existing conflict | No other `sdkmanager`/emulator install running; a single process owns the SDK root | Concurrent SDK writes are unsupported |

## 3. What E0 would install and download

All versions/URLs/SHA-256 are being pinned by R1 (`research/R1-versions.md`); E0 uses
that list when it exists, otherwise the latest stable from the SDK channel, and records
the **exact** resolved versions. Nothing below was downloaded on this host.

### 3.1 Toolchain

| Component | How | Approx. size (measure; do not hardcode) | Purpose |
|---|---|---|---|
| uv + uv-managed Python (3.12) | product bootstrap (`install.sh`/`install.ps1`) or `uv python install 3.12` | tens of MB | runs `avdfleet` |
| JDK/JBR 21 | product installer / platform package manager | ~200-300 MB | `sdkmanager`, `avdmanager`, `build-tools`, probe APK |
| Android command-line tools (`cmdline-tools;latest`) | SDK zip from developer.android.com | ~150 MB | `sdkmanager`, `avdmanager` |
| `platform-tools` | `sdkmanager --install` | ~10-20 MB | `adb` (required by emulator/runtime) |
| `emulator` | `sdkmanager --install` | ~1-2 GB installed | the AVD runtime (needs acceleration) |
| `build-tools;35.0.0` (or R1-pinned version) | `sdkmanager --install` | ~100-200 MB | build the G1 probe APK; aapt/zipalign |
| `platforms;android-36` (or pinned API) | `sdkmanager --install` | ~100-200 MB | compile target for probe APK / tooling |
| Node 22 + Appium 4 + UiAutomator2 driver + Appium Inspector + scrcpy | product installer | hundreds of MB | R5/G1 automation evidence, screen |
| Test artefacts | build or download | small | G1 probe APK (`com.avdfleet.g1probe`), optional test TLS PEM |

The canonical route once Wave 1 installer exists:

```bash
# macOS / Linux (accelerated host, admin/sudo)
./bootstrap/install.sh --accept-android-sdk-licenses --yes
# Windows (elevated PowerShell)
powershell -ExecutionPolicy Bypass -File .\bootstrap\install.ps1 -AcceptAndroidSdkLicenses -Yes
```

The research-time route for E0 may call `sdkmanager` directly (as below) while the
installer is still being built. Either way the same manifest is recorded. E0 must not
silently accept licences: the explicit flag/`sdkmanager --licenses` interaction is part
of the evidence.

### 3.2 Target system images

The candidate set is defined in `research/R3-oobe.md` §3 (API 34/35/36/37, tags
`google_apis_playstore`, `google_apis`, `aosp_atd`, `google_atd`, `default`, optional
`ps16k`, host ABI only). E0 downloads at least:

- the **primary verified-candidate images** for the host ABI (Play Store and Google APIs
  for API 34/35/36, API 37 if published), plus
- the ATD/AOSP candidates needed to complete the R3 matrix, subject to disk budget.

Discovery on the host (source of truth; package list changes):

```bash
sdkmanager --list --channel=0 | grep -E 'system-images;android-(34|35|36|37);'
# Windows: sdkmanager.bat --list | Select-String 'system-images;android-(34|35|36|37)'
```

Do not hardcode availability: record the resolved package ids, channel and versions in
the manifest. Each `system-images;android-<api>;<tag>;<abi>` package installs under
`<sdk_root>/system-images/android-<api>/<tag>/<abi>/` with a `package.xml` and
`system.img`; E0 records their sizes and SHA-256 (the zip hash is not retained by
`sdkmanager`).

### 3.3 AVD-adjacent storage (for R3/G1, provisioned but not part of E0's download)

- 5 AVDs × `disk.dataPartition.size=6G` (sparse; logical 30 GB, actual usage much
  lower but can grow), plus AVD home under `Paths.avd_home` (default
  `<data_dir>/avd`);
- golden snapshots (one per verified image) can be ~1-3 GB each depending on userdata;
- screenshots/logcat artifacts are small (MBs per image).

### 3.4 Disk budget formula (record measured numbers, then compare)

```
budget ≈ SDK tools (~5-8 GB)
       + Σ system images (n_images × measured installed size, typically ~1.5-3.5 GB each)
       + 5 × 6 GB AVD userdata (sparse; reserve logical space)
       + snapshots (n_verified × measured)
       + 15% headroom
```

On this build host the free space (~13.5 GiB) does not even cover the SDK tools plus one
image set plus AVD data, which is a second independent reason E0 cannot run here.

## 4. Runbook on the accelerated host

Execute top-to-bottom; every phase appends to `research/e0/<run-id>/timeline.jsonl`
(command, start/end UTC, exit code, log path). On any failed precondition, stop and
record the failure — do not continue installing.

### Phase 0 — Preflight (read-only)

```bash
emulator -accel-check            # after SDK install; before that use OS checks (R3 §2)
# host facts
uname -a; nproc; free -g; df -h /
# Windows: Get-CimInstance Win32_Processor / Win32_ComputerSystem / Win32_LogicalDisk
```

If acceleration is unavailable: mark E0 **blocked (no acceleration)** and stop. E0
must not install a fleet that cannot boot.

### Phase 1 — Install the SDK toolchain

Preferred: product bootstrap with the explicit licence flag (canonical, checkpointed,
resumable, writes the install manifest). Research-time alternative:

```bash
export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$HOME/android-sdk}"
export ANDROID_HOME="$ANDROID_SDK_ROOT"
mkdir -p "$ANDROID_SDK_ROOT/cmdline-tools"
# unpack the R1-pinned commandlinetools zip into cmdline-tools/latest
export PATH="$ANDROID_SDK_ROOT/cmdline-tools/latest/bin:$ANDROID_SDK_ROOT/platform-tools:$ANDROID_SDK_ROOT/emulator:$PATH"
sdkmanager --version
sdkmanager --install "platform-tools" "emulator" "build-tools;<pinned>" "platforms;<pinned>"
sdkmanager --licenses         # explicit acceptance step, logged
```

On Windows the product default SDK root is `<data_dir>\sdk` (usually
`%LOCALAPPDATA%\avdfleet\sdk`); the portable edition uses `<bundle>\sdk`. Record the
actual root in the manifest and export the same env vars for the session.

### Phase 2 — Verify the toolchain can boot (smoke AVD, one image)

```bash
PKG="system-images;android-36;google_apis_playstore;x86_64"   # host ABI
sdkmanager --install "$PKG"
avdmanager create avd --force --name e0-smoke --package "$PKG" --device pixel_7
emulator -avd e0-smoke -no-window -no-audio -no-boot-anim \
  -gpu swiftshader_indirect -no-snapshot -wipe-data -port 5554 &
adb wait-for-device
until [ "$(adb -s emulator-5554 shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; do sleep 1; done
adb -s emulator-5554 shell dumpsys activity activities | head -50 > e0-smoke-activity.txt
adb -s emulator-5554 exec-out screencap -p > e0-smoke.png
adb -s emulator-5554 emu kill
```

Record boot duration and screenshot. **Do not proceed to mass downloads if the smoke
boot fails** — fix acceleration/SDK first.

### Phase 3 — Background download of the target image set

One `sdkmanager` process at a time; log everything; retry transient failures; monitor
disk before each image. Example POSIX:

```bash
RUN="research/e0/$(date -u +%Y%m%dT%H%M%SZ)-$(hostname)"
mkdir -p "$RUN"
IMAGES=( "system-images;android-34;google_apis_playstore;x86_64"
         "system-images;android-35;google_apis_playstore;x86_64"
         "system-images;android-36;google_apis_playstore;x86_64"
         # plus google_apis / ATD / AOSP candidates per R3 §3 and disk budget
       )
for pkg in "${IMAGES[@]}"; do
  avail=$(df -Pk "$ANDROID_SDK_ROOT" | awk 'NR==2{print $4}')
  [ "$avail" -gt 20971520 ] || { echo "STOP: <20 GiB free" >> "$RUN/downloads.log"; break; }
  for attempt in 1 2 3; do
    echo "=== $(date -u +%FT%TZ) $pkg attempt $attempt" >> "$RUN/downloads.log"
    sdkmanager --verbose --install "$pkg" >> "$RUN/downloads.log" 2>&1 && break
    sleep $((attempt * 30))
  done
  sdkmanager --list_installed >> "$RUN/sdk-installed.txt" 2>&1
done
```

Windows equivalent: `Start-Process -PassThru -RedirectStandardOutput "$RUN\downloads.log"`
around the same `sdkmanager.bat` loop; use `Get-CimInstance Win32_LogicalDisk` for free
space. Record bytes downloaded/time via the log timestamps and measured directory sizes.

If the product's `avdfleet images download` path is available (Wave 1 G1x), prefer it:
it records catalogue ids, URLs, expected SHA-256 and resume state. Either path must
produce the same manifest fields.

### Phase 4 — Assemble manifest and hand off

For every installed package: id, version, installed path, size, `system.img` and
`package.xml` sha256, channel, timestamp. Also record: accelerator check output,
emulator/adb/sdkmanager versions, SDK root, AVD home, free disk before/after, smoke
AVD result, failures and retries. Write `research/e0/<run-id>/E0-manifest.json`, update
`research/E0-host.md` (this file) status and the "Current state" facts, and announce the
host ready to R3/G1.

## 5. Current state on this host — 2026-11-26 (not run, read-only inspection)

Nothing was installed or downloaded. Observed facts:

| Fact | Value |
|---|---|
| OS | Windows 11 Pro, build `10.0.26300`, `OSArchitecture=ARM 64-bit` |
| Platform | `VMware, Inc.` model `VMware20,1` (the build host is itself a VM) |
| CPU | reported as `Apple silicon`, 4 vCPUs (1 core each) |
| Virtualisation firmware | `VirtualizationFirmwareEnabled=False`, `SecondLevelAddressTranslationExtensions=False`, `VMMonitorModeExtensions=False` |
| Hypervisor exposed | `Win32_ComputerSystem.HypervisorPresent=True` (outer VMware hypervisor only; no nested acceleration usable by the Android emulator) |
| Windows features | `HypervisorPlatform` (WHPX) `InstallState=2` (disabled), `Microsoft-Hyper-V-All` `InstallState=2`, `VirtualMachinePlatform` `InstallState=2`; `systeminfo` reports "A hypervisor has been detected. Features required for Hyper-V will not be displayed." |
| RAM | 16 GiB physical (`17,179,869,184` bytes); `17,174,372,352` bytes visible; ~6.1 GB free at inspection |
| Disk | `C:` 67,588,059,136 bytes total; 14,490,906,624 bytes free (~13.5 GiB) at inspection — far below the E0 budget |
| Android tooling | none: `ANDROID_HOME`, `ANDROID_SDK_ROOT`, `JAVA_HOME` unset; no `adb`, `emulator`, `sdkmanager`, `avdmanager`, `java`, `node`, `appium`, `uv` on PATH; no `~/.android`; no `C:\Android`; no `%LOCALAPPDATA%\Android\Sdk` |
| Python | 3.14.10 x64 present (project requires `>=3.11,<3.14`; only relevant to product tests, not to the stdlib G1 listener harness) |
| Other tools | no `git`, no `openssl` on PATH |
| Network | single VMware vmxnet3 adapter; default routes via a VPN interface and Ethernet; reachability/throughput to `dl.google.com` not evaluated |

Consequences:

- Preflight fails at **hardware acceleration**: no WHPX/KVM/HVF. E0 must not install an
  unusable SDK fleet.
- Disk and RAM are also below the recommended budget for the matrix + 5 AVDs.
- This build's instructions explicitly forbid installing the Android SDK, downloading
  images and starting an emulator, so E0 was not attempted here.
- The R3/G1 listener harness (`research/g1/listeners.py`) runs here because it is
  stdlib-only and needs no Android SDK; that does not satisfy E0, R3 or G1.

## 6. Evidence requirements for E0 (accelerated host)

```
research/
  E0-host.md                                  # update status/facts after the run
  e0/
    <YYYYMMDDTHHMMSSZ>-<host>/
      preflight.json                          # acceleration + host capability proof
      accel-check.txt                         # emulator -accel-check output
      host.json                               # OS/CPU/RAM/disk/arch
      versions.json                           # emulator/adb/sdkmanager/avdmanager/JDK/Node
      downloads.log                           # every sdkmanager invocation, timestamps, errors/retries
      sdk-installed.txt                       # sdkmanager --list_installed snapshots
      packages.json                           # per package: id, version, path, size, sha256(artefacts)
      disk.json                               # free space before/after per phase
      smoke-boot/
        boot-timings.json, activity.txt, screenshot.png, emulator.log
      E0-manifest.json                        # consolidated machine-readable manifest
      timeline.jsonl                          # command journal
      checksums.sha256
```

Acceptance for E0: every installed package has a recorded version and size; every
downloaded system image has recorded `system.img`/`package.xml` SHA-256 and installed
path; accelerator proof and smoke-boot proof are present; no licence was accepted
implicitly; the manifest is consistent with `sdkmanager --list_installed`; failures and
retries are documented rather than hidden. E0 passes only when an AVD from the chosen
image actually boots to the first screen (smoke evidence), not merely when downloads
finish.

## 7. Blockers, risks and dependencies

Blockers on this host:

1. **No usable hardware virtualisation.** WHPX/KVM/HVF absent (see §5); the emulator
   cannot start; BRIEF.md §6 says there is no supported software fallback. This blocks
   E0's smoke-boot acceptance even if downloads were allowed.
2. **Policy.** This build host must not install the SDK, download images or start an
   emulator (explicit build instruction, consistent with BRIEF.md §6).
3. **Disk/RAM budget.** ~13.5 GiB free and 16 GiB RAM cannot hold the SDK + candidate
   images + 5 AVDs + snapshots even on an accelerated host of the same size.

Risks for the accelerated run:

- **Licence acceptance**: only with the explicit `--accept-android-sdk-licenses` flag
  (BRIEF.md §2); record it. Never auto-accept silently.
- **Download duration/instability**: multi-GB images can take hours; run in the
  background with retries and disk guards; do not start parallel `sdkmanager`
  processes.
- **Disk exhaustion** during downloads/AVD creation; monitor free space and gate each
  image.
- **WHPX enable needs a reboot**; the installer must checkpoint/resume (BRIEF.md §3.2).
- **ABI mismatch**: download only host-ABI images; record ABI.
- **API 37+** may be preview-only, unstable and RAM-hungry; prefer API ≤36 for CI-like
  hosts with ≤16 GiB and 32 GiB+ for API 37+ five-ADV fleets.
- **Silent stale SDK state**: a reused host may already have a partial SDK; E0 records
  pre-existing packages and either reconciles via the installer or starts from a known
  root.
- **E0 cascades**: without E0, R3/G1 are blocked; the final report must mark all
  real-emulator items accordingly (BRIEF.md §6/§9).

## 8. Sources

- BRIEF.md §2 (R-INSTALL), §3.2 (installer), §5 (E0), §6 (no acceleration here), §7
  (acceptance items 2, 3, 8).
- PLAN.md "Host constraint discovered in Wave 0".
- `research/R3-oobe.md` §2/§3 (candidate images, preflight), `research/g1/gate.md` §3
  (guest protocol), `research/R1-versions.md` (pinned versions/URLs/SHA-256, when
  available).
- `src/avdfleet/core/paths.py` (SDK root, AVD home, data/log paths).
- `sdkmanager`: <https://developer.android.com/tools/sdkmanager>
- `avdmanager`: <https://developer.android.com/tools/avdmanager>
- Emulator acceleration: <https://developer.android.com/studio/run/emulator-acceleration>
- Android Studio / command-line tools download page:
  <https://developer.android.com/studio>
- Emulator command line: <https://developer.android.com/studio/run/emulator-commandline>
