# Rooted and writable emulator images

Whether AVD Fleet can turn an emulator image into a "rooted device" by script, and
what that actually buys you.  The short answer: **yes on `google_apis`/`default`
(userdebug) images, with `adb root` plus `-writable-system` - but the change is
per-AVD and only visible on boots that pass the flag again.**  No bootloader
unlock, fastboot flashing or Magisk is involved.

All numbers below were measured on Windows 11 with emulator 37.3.10, adb 37.0.3
and WHPX acceleration, using the throwaway-AVD script
[`scripts/emulator-root-probe.ps1`](../scripts/emulator-root-probe.ps1).  Every run
created its own `rootprobe-*` AVD, booted it headless and deleted it afterwards;
no pre-existing AVD or shared SDK image was modified.

## Summary of what works

| Capability | API 25 `google_apis;x86` | Mechanism |
|---|---|---|
| `adb root` (root shell) | **yes** | userdebug image: `ro.build.type=userdebug`, `ro.debuggable=1` |
| Writable `/system` | **yes** | `-writable-system` at boot, then `adb root` + `adb remount` (API 30+ also needs one reboot after remount) |
| Survives a *normal* reboot | **no** | the overlay is only attached when `-writable-system` is passed |
| Survives a `-writable-system` reboot | **yes** | the AVD keeps `system.img.qcow2` |
| `getenforce` -> `Permissive` | **yes** | `setenforce 0` at runtime, or `-selinux permissive` from boot |
| SELinux permissive persists | **no** | runtime switch; re-apply (or pass the flag) after every boot |
| Root for third-party apps | **not provided** | `/system/xbin/su` exists but this is not a `su` manager |

## Measured evidence

Image: `system-images;android-25;google_apis;x86` - Android 7.1.1, API 25, x86,
`ro.build.type=userdebug`, `ro.debuggable=1`, `ro.secure=1` (the `ro.secure=1`
value does not block `adb root`; `ro.debuggable` is what matters).

Root shell:

```text
$ adb -s emulator-5570 shell id
uid=2000(shell) gid=2000(shell) ... context=u:r:shell:s0
$ adb -s emulator-5570 root
restarting adbd as root
$ adb -s emulator-5570 shell id
uid=0(root) gid=0(root) groups=0(root),1004(input),... context=u:r:su:s0
```

Writable system (`-writable-system` boot):

```text
$ adb -s emulator-5570 shell mount | grep " /system "
/dev/block/vda on /system type ext4 (ro,seclabel,relatime,data=ordered)
$ adb -s emulator-5570 root && adb -s emulator-5570 remount
remount succeeded
$ adb -s emulator-5570 shell mount | grep " /system "
/dev/block/vda on /system type ext4 (rw,seclabel,relatime,data=ordered)
$ adb -s emulator-5570 shell "echo hello > /system/etc/avdfleet-probe.txt && cat /system/etc/avdfleet-probe.txt"
hello
```

`adb remount` is idempotent (a second call also prints `remount succeeded`), and
writing into `/system/priv-app/<name>/` works the same way, so preinstalling an APK
into the system partition is a scriptable operation.

Persistence, tested with a marker file under `/system/etc`:

| Boot | Flags | Marker visible? |
|---|---|---|
| 1 | none (plain) | written, then gone after shutdown |
| 2 | `-writable-system` | written |
| 3 | none (plain) | **no** |
| 4 | `-writable-system` | **yes** |
| 5 | `-writable-system -selinux permissive` | **yes** |

### API 34 `google_apis;x86_64`: same root, different remount

Measured on the installed `system-images;android-34;google_apis;x86_64` (Android 14,
`ro.build.type=userdebug`, `ro.debuggable=1`):

```text
$ adb -s emulator-5570 root && adb -s emulator-5570 shell id
uid=0(root) gid=0(root) ... context=u:r:su:s0          # root works the same way
$ adb -s emulator-5570 remount
Successfully disabled verity
[libfs_mgr] could not map scratch image
Failed to allocate scratch on /data, fallback to use free space on super
Using overlayfs for /system
Using overlayfs for /vendor
Using overlayfs for /product
Using overlayfs for /system_dlkm
Using overlayfs for /system_ext
Verity disabled; overlayfs enabled.
Now reboot your device for settings to take effect
$ adb -s emulator-5570 shell "echo x > /system/etc/probe.txt"
/system/bin/sh: can't create /system/etc/probe.txt: Read-only file system
```

So on API 30+ the flow is **`adb root` -> `adb remount` -> reboot -> (`adb root` again)
-> writes go through overlayfs**.  The probe script detects the "Now reboot your device"
message (or a read-only write failure), reboots, re-roots, remounts and retries, which
is why a full pass on API 34 takes roughly twice as long as on API 25.

One more thing was found on this host: the API 34 `google_apis` and `default` x86_64
`ramdisk.img` files contain Magisk (the running guest mounts `/system/bin/magisk` and
`/dev/<id>/.magisk/mirror/...`), and their modification times are 1.4 h / 4 days later
than the rest of their image directory - i.e. somebody already applied the
Magisk-ramdisk patch described below.  The `google_apis_playstore` image is unpatched.
That is why an API 34 boot here takes noticeably longer than API 25.

SELinux:

```text
$ adb -s emulator-5570 shell getenforce
Enforcing                      # default after every boot
$ adb -s emulator-5570 shell "setenforce 0"
$ adb -s emulator-5570 shell getenforce
Permissive                     # immediate, per-boot only
# ...after reboot: Enforcing again
$ emulator -avd X -selinux permissive ...
$ adb -s emulator-5570 shell getenforce
Permissive
$ adb -s emulator-5570 shell "tr ' ' '\n' < /proc/cmdline | grep -i selinux"
androidboot.selinux=permissive
```

## How the write path works

`-writable-system` does not edit the SDK's `system.img`.  The emulator logs
`WARNING | System image is writable` and hands QEMU a qcow2 overlay in the **AVD
directory**:

```text
-drive if=none,index=0,id=system,file=C:\Users\<you>\.android\avd\<avd>.avd\system.img.qcow2,overlap-check=none,cache=unsafe
```

so writes are diffed into `<avd>/system.img.qcow2` and the shared
`%LOCALAPPDATA%\Android\Sdk\system-images\...\system.img` stays byte-identical
(verified: unchanged size and mtime, and the emulator holds no write handle on it).
Without the flag the emulator uses a temporary overlay that is discarded on exit -
which is why a plain boot's writes disappear and why the next normal boot shows the
pristine image.

## What true persistence would require

1. **Always boot that AVD with `-writable-system`** (cheapest).  The overlay lives in
   the AVD, so the modification survives shutdowns and cold boots - but only when the
   flag is present, and the AVD's `system.img.qcow2` must not be deleted.  `adb root`
   and `adb remount` still have to be re-run after every boot.
2. **A custom system image.**  Unpack `system.img` (ext4), change it, repack, and point
   an AVD's `image.sysdir.1` at it (or replace the image in a copied system-image
   directory).  This is what makes the change independent of launch flags, but it is a
   build step: no fastboot/bootloader unlock is needed because the emulator boots the
   files you give it.
3. **A patched boot ramdisk (Magisk-style).**  On an emulator that means repacking
   `ramdisk.img`/`kernel-ranchu`, not `fastboot flash boot`.  This is the only variant
   that needs no launch flag and no per-boot `adb root`, because the patch travels with
   the image: it is also the variant already present on this host for the API 34
   `google_apis`/`default` x86_64 images (see above).  Its cost is that it edits a
   *shared* SDK image - every AVD using that image is affected, a package reinstall
   wipes it, and the image is no longer the one Google shipped - which is why the probe
   script here never touches it.

## Limitations to know before relying on this

- `adb root` is **per-boot**: after a reboot `adb shell id` is `uid=2000(shell)` again,
  because adbd restarts as the shell user.  Scripts must run `adb root` after every boot.
- `-writable-system` must be passed on **every** boot of that AVD, and `adb remount`
  re-run; a normal boot of the same AVD silently shows the unmodified system.
- The overlay is **per AVD** (`<avd>/system.img.qcow2`).  Cloning an AVD directory
  copies it; deleting the AVD discards the modification; other AVDs using the same SDK
  image are unaffected.  Whether `-wipe-data` clears it was not tested.
- Snapshot interaction was not tested - every run here used `-no-snapshot`.  Loading a
  saved snapshot restores guest RAM, which may restore an old mount state; keep
  `-no-snapshot` for reproducible behaviour.
- No dm-verity/AVB work was needed on API 25 (no verity boot properties are exposed and
  `adb disable-verity` fails with `Failed to open /fstab.ranchu`).  On newer AVB images
  `adb remount` may report an overlayfs remount and `adb disable-verity` can be required
  first - unverified here.
- Partition coverage on API 25 x86: `/system` (and `/vendor`, which is
  `/system/vendor` there) becomes writable; `/oem` stays `Read-only file system`
  even after `remount`; there is no `/product` (that arrives with API 29+ dynamic
  partitions, untested here).
- Only `google_apis`/`default` images are userdebug.  `google_apis_playstore` images are
  user builds, where `adb root` answers `adbd cannot run as root in production builds`.
  API 34 `google_apis;x86_64` was verified here end to end: `adb root` works, but
  `/system` only becomes writable after the remount-induced reboot described above.
- API 34 already carries a Magisk patch here, so "root" on that image can come from two
  independent layers (`adb root` from userdebug, Magisk from the patched ramdisk).  When
  a test depends on *which* layer granted root, check `magisk -v`/`su -c id`, not just
  `adb shell id`.
- `su` exists at `/system/xbin/su` (setuid root, group `shell`) and `adb shell su 0 id`
  returns uid 0.  That is not the same as granting root to arbitrary apps: there is no
  `su` manager/policy, and app-initiated `su` was not tested.  Appium/UiAutomator already
  run through adb, so `adb root` is normally what automation needs.
- `setenforce 0` (or `-selinux permissive`) weakens isolation.  Do not combine it with
  real credentials or production data, and never bake it into an image used for anything
  but disposable test instances.
- Boot cost is real: 51 s on an idle host, 95-156 s with other emulators running, for a
  cold API 25 x86 boot.

## Running the probe

`scripts/emulator-root-probe.ps1` (Windows PowerShell 5.1+; this host has no `pwsh` 7
and a restrictive execution policy, hence `-ExecutionPolicy Bypass`):

```powershell
# defaults: API 25 google_apis x86, writable system, headless, port 5570+
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\emulator-root-probe.ps1

# permissive SELinux and a caller-supplied root command
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\emulator-root-probe.ps1 `
    -SelinuxPermissive -Command 'id; settings get global http_proxy'

# A/B comparison without the writable-system flag (expects the writable check to FAIL)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\emulator-root-probe.ps1 -NoWritableSystem
```

| Parameter | Default | Meaning |
|---|---|---|
| `-Image` | `system-images;android-25;google_apis;x86` | system image package to probe |
| `-AvdName` | `rootprobe-<6 hex>` | must start with `rootprobe`; existing names are refused |
| `-Command` | none | shell command run as root, output printed, exit code checked |
| `-Port` | `5570` | first console port tried; scans even ports 5570-5584 (adb = port+1) |
| `-MemoryMB` | `2048` | guest RAM |
| `-BootTimeoutSec` | `300` | boot wait budget |
| `-NoWritableSystem` | off | boot without `-writable-system` (writes become ephemeral) |
| `-SelinuxPermissive` | off | add `-selinux permissive` and require `getenforce=Permissive` |
| `-KeepAvd` | off | keep the AVD and logs for inspection |
| `-LogDir` | `%TEMP%\avdfleet-rootprobe` | emulator stdout/stderr logs |

The script prints a `PASS`/`FAIL` line per check (`id`, `getenforce`, `/system`
writable, `adb remount`, caller command) followed by `OVERALL: PASS|FAIL`, and exits
non-zero when a check fails.  It always shuts the emulator down and deletes its own
AVD, and it refuses to touch `CloneForge*`, `Device*`, `selftest-*`, `proxytest*` or
`oobe-reset*`.  Every adb call is scoped with `-s emulator-<port>`; the shared adb
server is never restarted.

Sample (API 25, defaults):

```text
   before: /system=ro(ext4)
   adb remount: remount succeeded
  PASS   adb root               uid=0(root) gid=0(root) ... context=u:r:su:s0
  PASS   selinux                getenforce=Enforcing (...)
  PASS   adb remount            remount succeeded
  PASS   /system writable       /system/etc/avdfleet-probe-4981.txt -> 'avdfleet-writable-fdaacfc3'
   after: /system=rw(ext4)
OVERALL: PASS  (75s, emulator-5570, image system-images;android-25;google_apis;x86)
```
