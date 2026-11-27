# API 25 `google_apis` and the OOBE gate

Everything on this page was measured on a Windows 11 / WHPX host on 2026-11-27
with `system-images;android-25;google_apis;x86` (Android 7.1). It explains why
that image is the only stock system image on this host that can show a real
out-of-box experience, why the wizard normally refuses to run, and what is
required to make OOBE repeatable.

## Why API 25 matters here

| Property | API 25 `google_apis;x86` | API 33/34 `google_apis`, Play Store |
|---|---|---|
| Google Setup Wizard package (`com.google.android.setupwizard`) | **present** | **absent** (`pm list packages -u` shows only `googlesdksetup` / `partnersetup`) |
| Real OOBE ever observed | **yes** - `com.google.android.setupwizard/.SetupWizardActivity` | never |
| `adb root` | **works** (userdebug: `ro.debuggable=1`) | works on `google_apis`, not on Play Store images |
| `su` binary | present at `/system/xbin/su` | not present |
| Cold boot measured | **45.5 s / 55.8 s / 66.2 s** | 97-147 s |
| `system.img` size | ~2.5 GB | ~2.9-8 GB |
| ABI | x86 (32-bit) | x86_64 |

So API 25 is the smallest, fastest, most permissive image on the host, and the
only one that can display Google's first-run wizard. That makes it the right
target for OOBE work and for rooted-image experiments; it is not a substitute
for modern API coverage.

## What the wizard is gated on

Launching the wizard manually after clearing provisioning state still yields the
launcher. Logcat states the reason exactly:

```
I SetupWizard.SetupWizardActivity: DEVICE_PROVISIONED=false USER_SETUP_COMPLETE=false
I SetupWizard.SetupWizardActivity: Early exit: provisioningMode=EMULATOR
```

The gate is the read-only property `ro.setupwizard.mode=EMULATOR`, set on
emulator images. While it reads `EMULATOR`, `SetupWizardActivity` (and every
other activity in the package, because they share the base activity) finishes
immediately.

## What does NOT work (all measured)

| Attempt | Result |
|---|---|
| `settings put global device_provisioned 0` + `put secure user_setup_complete 0` | flags do flip to 0, but the wizard still early-exits |
| `pm clear` / `am force-stop` / `am start -n .../.SetupWizardActivity` | process starts, logs the early exit, launcher stays on top |
| Launching wizard sub-activities (`.network.WifiActivity`, `.user.FlowChoiceActivity`, `.user.NameActivity`) | same early exit - the gate is in the shared base activity |
| `pm enable` of the package/component as `shell` | `SecurityException: Shell cannot change component state` |
| `pm enable` as root | succeeds (component state becomes `enabled`, resolves again) but the wizard still early-exits |
| `setprop ro.setupwizard.mode OPTIONAL` (root) | silently ignored - `ro.*` is immutable after boot |
| Emulator `-prop ro.setupwizard.mode=OPTIONAL` | **rejected by the emulator**: `WARNING | unexpected '-prop' value (...), only 'qemu.*' properties are supported` |
| `-wipe-data`, deleting `userdata-qemu.img`/`cache.img`, snapshot removal | restores the image's bundled (already provisioned) userdata; launcher |
| `reset-oobe --strategy wipe` / `--strategy package` | run to completion (64 s / 8.7 s) but report `ok: false` with the launcher as top activity - an accurate report of an image limitation |

The property is injected by the emulator itself, not by any file in the image:
`/system/build.prop`, `/default.prop`, `/vendor/build.prop` and the init scripts
contain no `setupwizard` entry, and the only hits on the filesystem are in the
compiled `SetupWizard.odex`.

## What can unlock repeatable OOBE

Only something that can rewrite a read-only property at runtime, or an image that
ships a different value. Three routes, in order of practicality:

1. **Magisk `resetprop` from a boot-time module (runtime, reversible) - PROVEN.**
   Magisk's property service can override `ro.*`, and a module's
   `post-fs-data.sh` runs before SystemServer, which is early enough. A post-boot
   `resetprop` is too late: the wizard's enablement is decided at boot.
2. **System image repack (build time, persistent).** Unpack the image's
   `system.img`/ramdisk, bake the value in, repack, and register the result as a
   custom image (`avdfleet images import`). Always OOBE-capable, no runtime hooks,
   at the cost of a rebuild and a non-stock image.
3. **Patch the SetupWizard app (surgical).** Decompile `SetupWizard.apk`, remove
   the `provisioningMode == EMULATOR` early-exit branch, re-sign and install.
   Smallest change, but it modifies Google's app and must be redone per update -
   only worth it for a fixed lab image.

## The working recipe (proven 2026-11-27)

Repeatable OOBE on `system-images;android-25;google_apis;x86` **works**, on two
consecutive cold boots:

1. **Root the image** with rootAVD (the `farm` toolkit): copy rootAVD to a private
   directory, stage Magisk 26.1 as `Magisk.zip`, and run
   `rootAVD.bat system-images\android-25\google_apis\x86\ramdisk.img` with
   `ANDROID_HOME`/`ANDROID_SERIAL` set. It repacks the *shared* SDK ramdisk in
   place - back it up first and restore it afterwards; production must patch a
   copy and register it with `avdfleet images import`.
2. **Complete Magisk's "Additional Setup".** This is the step that silently
   breaks everything: the ramdisk patch alone leaves `/data/adb/magisk` empty, so
   magiskd logs `Magisk environment incomplete, abort` and **no module script ever
   runs**. Launch `com.topjohnwu.magisk/.ui.MainActivity` and accept the
   "proceed and reboot?" dialog (automatable with `am start` + `input tap`).
3. **Install a module** at `/data/adb/modules/oobe-arm/` with `module.prop` and an
   executable (0755, LF-only) `post-fs-data.sh` containing
   `magisk resetprop ro.setupwizard.mode OPTIONAL`.
4. **Clear provisioning** immediately before each cold boot and re-verify:
   `settings put global device_provisioned 0`,
   `settings put secure user_setup_complete 0`,
   `pm enable com.google.android.setupwizard` (package and activity component),
   then `sync` before shutting down. The flags self-reset to 1/1 after any boot
   that early-exits, so this must be the last step before boot.
5. **Cold boot with snapshots disabled** (`-no-snapshot`). A snapshot restore
   bypasses the patched ramdisk and silently loses root.

Observed on both runs:

```
getprop ro.setupwizard.mode   -> OPTIONAL
top activity                  -> com.google.android.setupwizard/.user.WelcomeActivity
/data/adb/oobe-arm.log        -> before=EMULATOR / resetprop_rc=0 / after=OPTIONAL
logcat                        -> the "Early exit: provisioningMode=EMULATOR" line is gone
```

Screenshots (1080x2400, ~105 KB) show the genuine wizard: blue **Welcome**,
"ENGLISH (UNITED STATES)", yellow **LET'S GO**, Emergency and Vision Settings. The
first run is byte-identical to a manual capture, so the screen renders
deterministically.

### Two traps worth remembering

- Magisk 26.1's SQLite table is
  `policies(uid, policy, until, logging, notification)` - there is **no `process`
  column**, so an INSERT naming one fails *silently*; and `1` means deny while
  `2` means allow. `INSERT OR REPLACE INTO policies
  (uid,policy,until,logging,notification) VALUES (2000,2,0,1,1);` grants the shell
  uid root.
- `adb root` works on this userdebug image and avoids the `su` chicken-and-egg:
  seed files and policy as uid 0, then `adb unroot`; `su -c id` then reports
  `uid=0(root) ... context=u:r:magisk:s0` and `magisk -V` reports `26100`.

## Status

- **Repeatable OOBE: PROVEN** on `system-images;android-25;google_apis;x86` -
  two consecutive cold boots, wizard in the foreground, screenshots captured.
- The recipe is invasive and image-specific: it mutates the shared SDK ramdisk
  unless you patch a copy, and it deliberately bypasses a boot-security control.
  It belongs behind an explicit, opt-in root step - not in the default boot path.
- Everything else on this page (the gate, the failed userspace attempts, the
  emulator `-prop` restriction) stands as measured.


## Practical notes

- API 25 images are x86 only (`google_apis;x86`, no `x86_64`), so they run on
  any x86_64 host with WHPX but exercise 32-bit ABI behaviour.
- Play Store images are `ro.build.type=user` / `ro.debuggable=0`, so `adb root` is
  refused and they cannot be rooted this way; rooting work targets the `google_apis`
  and `default` images, which are userdebug.
- A real rooted-image workflow must cope with the emulator's read-only system
  partition: `-writable-system` plus `adb root` + `adb remount` gives a writable
  `/system` for the life of that boot, and the changes live in the AVD's qcow2
  overlay, not in the shared system image.
- Every attempt above was time-boxed on one AVD at a time; boots were
  45-66 s, so a full reset cycle is cheap once the property gate is solved.

