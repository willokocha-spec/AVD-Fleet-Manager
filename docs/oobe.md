# OOBE (Out-of-Box Experience)

AVD Fleet records, verifies and resets the Android first-boot **Setup Wizard**
state for system images, and measures how long a reset to OOBE takes.

## What "OOBE" means here

A stock Android system image boots to a Setup Wizard on first boot. After setup
completes (or after the user data partition is reused), the wizard no longer
appears. The product treats an AVD as being **at OOBE** when the Setup Wizard
activity is in the foreground and the setup-complete settings are still unset.

Detection uses the guest's own state, not a screenshot heuristic:

- `dumpsys activity activities` — resumed/top-resumed component;
- `dumpsys window` — current focus component;
- `settings get secure user_setup_complete` — `0` means not complete;
- `settings get global device_provisioned` — `0` means not provisioned;
- a screenshot for the evidence record.

The canonical wizard component is
`com.google.android.setupwizard/.SetupWizardActivity`; images without a wizard
(e.g. some AOSP/ATD builds) are recorded as `failed` with the observed
launcher/activity, not silently marked verified.

## The OOBE matrix

`oobe_matrix.json` is the checked-in result of booting each candidate image and
recording evidence (BRIEF §3.5). A seed matrix ships with the product for the
candidate API/tag/ABI combinations; local verification results and the local
data-directory copy take precedence where present.

```bash
avdfleet oobe matrix                 # checked-in + local records
avdfleet oobe matrix --json
avdfleet oobe verify "system-images;android-35;google_apis_playstore;x86_64"
avdfleet oobe verify <image-id> --timeout 900 --keep-avd
```

`oobe verify` boots a **temporary AVD** from the image, waits for
`sys.boot_completed`, detects the wizard, screenshots it, records boot seconds
and deletes the temporary AVD (unless `--keep-avd`). It never accepts an image
that is not installed (download it first with `avdfleet images download`).

A record has status, exact activity component, screenshot path, boot seconds,
checked-at timestamp, checker id, emulator version and notes. The API mirrors
this at `GET /api/v1/oobe/matrix`, `POST /api/v1/oobe/verify` and
`GET /api/v1/oobe/records/{image_id}`.

### Statuses and evidence rules

| Status | Meaning |
|---|---|
| `verified` | Boot completed; the Setup Wizard was observed in `dumpsys activity` **and** `dumpsys window`; `user_setup_complete=0`; screenshot captured and valid; repeatable. |
| `failed` | Boot completed but no wizard was observed (e.g. launcher/ATD home), or the activity appeared but the screenshot/UI evidence is invalid. |
| `unverified` | Not run, timed out, device offline, or evidence incomplete. **Default for unknown images.** |
| `unsupported` | Image not available for the host ABI/API, or the host cannot run it (e.g. RAM/API 37 constraints). |

Evidence is enforced in code: a record claiming `verified` without both an
activity and a screenshot is downgraded to `unverified` on read, with a warning.
Images never booted on a real host therefore stay `unverified` — the product
never fabricates OOBE evidence.

## Reset to OOBE

Four strategies are implemented; the CLI/API let you choose explicitly, or the
manager picks the default (snapshot when a golden snapshot exists, otherwise
wipe):

| Strategy | What it does | Use when |
|---|---|---|
| `snapshot` | Restores the golden `avdfleet-golden` snapshot saved while the wizard was visible. Fastest. | Preferred; requires the golden snapshot. |
| `wipe` | Stops the instance and relaunches the emulator with `-wipe-data`. | No golden snapshot; full data reset. |
| `package` | From inside the guest: `pm clear com.google.android.setupwizard`, reset `user_setup_complete`/`device_provisioned`, force-stop and restart the wizard. | Fast fallback without stopping the emulator. |
| `reboot` | Restarts the instance; used when the guest is already sitting on the first-boot screen. | The guest is already at OOBE. |

```bash
avdfleet instances reset-oobe avd-01 --strategy snapshot --verify
avdfleet oobe reset avd-01 --strategy wipe --no-verify
```

* `--verify` (default on) re-detects the wizard after the reset and records the
  activity/screenshot; `--no-verify` only performs the reset.
* Every strategy measures wall-clock duration and returns
  `{avd_name, strategy, ok, seconds, activity, screenshot_path, message}`; a
  failed reset is reported as `ok: false` without raising.
* The API equivalent is `POST /api/v1/instances/{name}/reset-oobe` with
  `{"strategy": "snapshot|wipe|package|reboot", "verify": true}`.

### Golden snapshot

`ensure_golden_snapshot` confirms the wizard is visible on the running instance,
saves the snapshot as `avdfleet-golden` (marked golden) and records a marker file
(`oobe_golden_snapshots.json`) with image/emulator identity. Golden snapshots
are invalidated when the emulator version, image or relevant AVD config changes,
and on portable relocation — restore can then safely fall back to `wipe`.

Saving the golden snapshot requires the guest to be at the wizard; save it
before completing setup. From the CLI:

```bash
avdfleet instances snapshots save avd-01 avdfleet-golden --golden
avdfleet instances snapshots load avd-01 avdfleet-golden
```

### Measuring reset time

Every reset result carries `seconds` measured from the start of the reset
operation to the verified wizard state. The acceptance protocol requires the
median/min/max of three snapshot trials per image (plus `-wipe-data` fallback
timing) to be recorded under `research/oobe/`. `avdfleet selftest --e2e` records
the OOBE and reset timings it observes on a real-emulator host.

## Evidence requirements (R3 protocol)

The full protocol (candidate matrix, probes, checksums, evidence layout and
classification rules) is in `research/R3-oobe.md`. Key requirements to call an
image verified:

1. Host evidence: OS/CPU/RAM/disk, hypervisor proof (`emulator -accel-check`),
   emulator/adb/sdkmanager versions, SDK channel, licence acceptance method.
2. Image evidence: installed package metadata and SHA-256 of `system.img`.
3. Per-run evidence: emulator logs, boot timings, `dumpsys activity`,
   `dumpsys window`, `user_setup_complete`, `device_provisioned` screenshot(s),
   uiautomator XML, logcat.
4. Reset evidence: snapshot save/list, three reset trials with timings, wipe
   fallback and package fallback where used.
5. Two independent runs (R3a/R3b) must agree on checksum, activity and status
   before a checked-in matrix entry becomes `verified`.

## Current status on this build host

Real OOBE evidence now exists for a WHPX Windows 11 host. The original build
host had no usable hypervisor (`VirtualizationFirmwareEnabled=False`, no
WHPX/KVM/HVF), so it produced no measurements and every matrix entry there
stayed seed-only.

Measured on 2026-11-27 on the accelerated host (see
[live-acceptance-2026-11-27.md](live-acceptance-2026-11-27.md)):

- `system-images;android-25;google_apis;x86` booted the real
  `com.google.android.setupwizard/.SetupWizardActivity` once (45.5 s boot).
  Repeat verification - including with `-wipe-data` and `-no-snapshot` - did
  not reproduce it, so treat API 25 OOBE as single-shot on a freshly installed
  image.
- `system-images;android-33;google_apis;x86_64` and
  `system-images;android-34;google_apis;x86_64` ship **no Setup Wizard**: a
  pristine AVD parks on `com.android.settings/.FallbackHome` (or an emulator
  `sdksetup` stub) and is reported `unsupported` after probing the guest's
  package list.
- The API 34 Play Store image lists no `setupwizard` package at all
  (`pm list packages -u`), so it cannot show OOBE either.

The first-boot states the detector recognises are `FallbackHome`, the
`googlesdksetup`/`sdksetup` stubs, and a real Setup Wizard; only the last is
OOBE. Reset timings on real AVDs are still not measured - run the R3 protocol or
`avdfleet selftest --e2e` on an accelerated host.
