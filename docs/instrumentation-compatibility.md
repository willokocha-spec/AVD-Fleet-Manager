# Root and instrumentation compatibility

Which emulator images this project has actually rooted or instrumented, which
upstream releases support which Android API levels, and how the four package
formats differ. The companion page is [instrumentation.md](instrumentation.md).

The matrix below mixes two kinds of statement; the footnotes say which kind each
row is:

- **measured here** — a result from this project's own emulator runs, recorded in
  [rooted-images.md](rooted-images.md), [oobe-api25.md](oobe-api25.md) or
  [live-acceptance-2026-11-27.md](live-acceptance-2026-11-27.md);
- **upstream** — documented by Magisk/LSPosed/Google, but not exercised on this
  host.

Cell values:

| Value | Meaning |
|---|---|
| `yes` | demonstrated on this host |
| `no` | impossible or refused for that image class |
| `untested` | not exercised in this project; it may still be supported upstream |

The matrix records what this project knows about a **booted instance**. It is not
a statement that the image ships the component: no stock API 33+ image ships
Magisk, and the `Magisk` column only says whether a working Magisk root was
produced here.

Measured 2026-11-27 on Windows 11 / WHPX, emulator 37.3.10, adb 37.0.3. Live
checks through the daemon that day covered the LSPosed acquisition/import, an
API 25 capability scan, a module import, and a full API 34 Magisk + Zygisk +
LSPosed install/activation (footnote 6). An Xposed APK hook was not exercised.

## Image matrix

| Android API | Image tag | ABI | adb root | writable-system | Magisk | Zygisk | LSPosed | Xposed modules | Evidence |
|---|---|---|---|---|---|---|---|---|---|
| 25 (7.1) | `google_apis` | `x86` | yes | yes | yes ¹ | untested | no ² | no ² | [rooted-images.md](rooted-images.md), [oobe-api25.md](oobe-api25.md) |
| 33 (13) | `google_apis` | `x86_64` | untested | untested | untested | untested | untested | untested | [live-acceptance-2026-11-27.md](live-acceptance-2026-11-27.md) (boot only); [oobe-api25.md](oobe-api25.md) (`su` absent) |
| 34 (14) | `google_apis` | `x86_64` | yes | yes ³ | yes ⁶ | yes ⁶ | yes ⁶ | untested ⁶ | [rooted-images.md](rooted-images.md), [oobe-api25.md](oobe-api25.md), live API 34 run (2026-11-27) |
| 34 (14) | `default` | `x86_64` | untested | untested | untested ⁴ | untested | untested | untested | [rooted-images.md](rooted-images.md) (host note only) |
| 34 (14) | `google_apis_playstore` | `x86_64` | no | no | no | no | no | no | [oobe-api25.md](oobe-api25.md), [rooted-images.md](rooted-images.md), [live-acceptance-2026-11-27.md](live-acceptance-2026-11-27.md) |
| 37.2-beta3 | `google_apis_playstore_ps16k` | `x86_64` | no ⁵ | no ⁵ | no ⁵ | no ⁵ | no ⁵ | no ⁵ | [live-acceptance-2026-11-27.md](live-acceptance-2026-11-27.md) (boot only) |

¹ Magisk **26.1** (`26100`) was produced on API 25 by patching the emulator
`ramdisk.img` with the operator's external rootAVD toolchain, then completing the
Magisk app's **Additional Setup** (on API 34 the `complete_magisk_setup` step
automated it). Without that second step `/data/adb/magisk` is absent, magiskd logs
`/data/adb is not present, abort`, `magisk --install-module` prints `Incomplete
Magisk install`, and no module script runs. API 25 was verified by hand (two cold
boots); API 34 through the service (footnote 6). The ramdisk step still needs the
operator's toolchain via `AVDFLEET_MAGISK_INSTALLER`
([oobe-api25.md](oobe-api25.md)).

² LSPosed 1.9.2 supports **Android 8.1–14** (API 27–34) in its own `module.prop`,
so API 25 (Android 7.1) is below its floor. The pinned acquisition records the
same floor as `min_api: 27`, and `install_lsposed` refuses an API < 27 target
before pushing anything. Xposed modules need a framework, and the only one this
project targets is LSPosed, so the same bound applies. API 25 can still run
Magisk modules; it cannot run LSPosed.

³ On API 34, `/system` becomes writable only after `adb remount` sets up overlayfs
and the AVD is **rebooted**; the remount itself reports "Now reboot your device for
settings to take effect". API 25 needs no reboot. Both were measured here
([rooted-images.md](rooted-images.md)); the reboot requirement is AOSP `fs_mgr`
behaviour ([commit d9b6030](https://android.googlesource.com/platform/system/core/+/d9b60303a005f6a37374fad507658ba7f0c389f6)).

⁴ Stock API 33+ images ship **no Magisk**. On this host the local API 34
`google_apis` and `default` `ramdisk.img` files already contained a Magisk patch
from earlier work (their modification times are later than the rest of the image
directory), which is a host artefact rather than a stock property; the Play Store
image is unpatched ([rooted-images.md](rooted-images.md)). The `google_apis` row
above was then verified with a full install (footnote 6); the `default` variant
was not exercised.

⁵ Inferred from the `google_apis_playstore` tag: Play Store images are user
builds (`ro.build.type=user`, `ro.debuggable=0`), so `adb root` is refused and no
Magisk/LSPosed path is available without a user build. `adb root` was not
attempted on this particular API 37 beta image; the boot itself is recorded in
[live-acceptance-2026-11-27.md](live-acceptance-2026-11-27.md).

⁶ **Verified live on a throwaway API 34 AVD, 2026-11-27.** Magisk 26.1 was
patched in; `complete_magisk_setup` completed with four unprompted taps and
populated `/data/adb/magisk`; Zygisk was enabled; LSPosed 1.9.2 was installed and
activated after the reboot — `/data/adb/modules/zygisk_lsposed` (no
`disable`/`remove`), `/data/adb/lspd/{config,log}`, `pidof lspd` returned a pid,
logcat showed `ZygiskCompanion: welcome to LSPosed!` and `LSPosed Bridge: binder
received`. `lspd` did **not** install the manager; `pm install -r` of
`/data/adb/modules/zygisk_lsposed/manager.apk` installed `org.lsposed.manager`,
whose Overview reported **Activated**. No Xposed APK hook was exercised, so the
Xposed-modules cell stays `untested`.

Notes that apply to the whole matrix:

- API 25 images are `x86` only (`google_apis;x86`); there is no `x86_64` variant
  on this host ([oobe-api25.md](oobe-api25.md)).
- `adb root` does not survive a reboot and is not Magisk root. Both are per-boot
  host capabilities unless a Magisk ramdisk patch is in the image
  ([rooted-images.md](rooted-images.md)).
- `-writable-system` writes go to that AVD's `system.img.qcow2`, are invisible on
  a plain boot, and are lost when the AVD is deleted. Google documents the flag's
  writable copy as session-scoped
  ([emulator command line](https://developer.android.com/studio/run/emulator-commandline));
  measured persistence across `-writable-system` boots is in
  [rooted-images.md](rooted-images.md).
- The matrix is about **stock images on this host plus hand-made Magisk patches**.
  A custom repacked image ([rooted-images.md](rooted-images.md) §"What true
  persistence would require") would move the Magisk row to `yes` for its API level,
  at the cost of no longer being a stock image.

## LSPosed release → supported Android API

From the official release notes and the `module.prop` inside the released ZIP.
LSPosed's README also states "Supported Versions: Android 8.1 ~ 14".

| LSPosed release | Published | Documented Android range (API) | Evidence |
|---|---|---|---|
| 1.9.2 | 2023-10-11 | Android 8.1 – 14 (API 27–34) | `module.prop` in `LSPosed-v1.9.2-7024-zygisk-release.zip`: "Supports Android 8.1 ~ 14. Requires Magisk 24.0+ and Zygisk enabled."; release notes fix Android 14 hooks ([v1.9.2](https://github.com/LSPosed/LSPosed/releases/tag/v1.9.2)); **verified live on API 34** on 2026-11-27 (footnote 6) |
| 1.9.1 | 2023-08-30 | Android 8.1 – 14 (inferred) | notes: Android 8.1 hook fixes, Android 14 fixes, "Upgrade target SDK to 34", "Magisk version requires 24.0+" ([v1.9.1](https://github.com/LSPosed/LSPosed/releases/tag/v1.9.1)) |
| 1.9.0 | 2023-08-29 | Android 8.1 – 14 (inferred) | replaced by 1.9.1 because of a signature error; same changelog ([v1.9.0](https://github.com/LSPosed/LSPosed/releases/tag/v1.9.0)) |
| 1.8.6 | 2023-01-16 | not stated in the notes | previous release line; no version statement ([v1.8.6](https://github.com/LSPosed/LSPosed/releases/tag/v1.8.6)) |
| 1.8.0 | 2022-03-23 | Android 8.1 – 13 (inferred) | notes: "Support Android 13 DP1 and DP2", "Fix parasitic manager in Android 8.1" ([v1.8.0](https://github.com/LSPosed/LSPosed/releases/tag/v1.8.0)) |

Boundaries worth knowing:

- **No release newer than 1.9.2 exists** on GitHub as of 2026-11-27, although the
  repository has commits through 2026-10. Canary/debug builds are GitHub Actions
  artifacts, not versioned releases, and their Android support is not documented.
  No versioned LSPosed release therefore documents support for API 35/36 as of
  this date.
- 1.9.x requires **Magisk 24.0+**; the Zygisk flavour requires **Zygisk enabled**,
  the Riru flavour requires **Riru 26.1.7+** (README and 1.9.1 notes).
- The LSPosed manager is bundled in the module (`manager.apk`) but **`lspd` does
  not install it**: logcat reports `manager is not installed`. Install it with
  `pm install -r /data/adb/modules/zygisk_lsposed/manager.apk`; only then does the
  package `org.lsposed.manager` appear and its Overview report **Activated**.
- The **pinned acquisition** target is 1.9.2 (7024): 2,462,055 bytes, SHA-256
  `0ebc6bcb465d1c4b44b7220ab5f0252e6b4eb7fe43da74650476d2798bb29622`. Acquiring it
  imports the ZIP into the module library with `source: "official-release"`; the
  built-in LSPosed profile then resolves the newest `lsposed_zip` when its step has
  no module bound. Acquisition is not installation.

## Format vs installer

| Format | Library kind | What is inside | Installed with | Activated by | Notes |
|---|---|---|---|---|---|
| Xposed APK | `xposed_apk` | `AndroidManifest.xml` plus legacy `assets/xposed_init` and `xposedmodule`/`xposedminversion`/`xposeddescription` meta-data, or the modern `META-INF/xposed/` entries | `adb install -r -g` | enable in the LSPosed manager, select per-app scope, reboot (the best-effort DB steps `enable_xposed_module`/`set_xposed_scope` can write LSPosed's database instead; the manager is authoritative) | installing is not activation; see [instrumentation.md](instrumentation.md#why-installing-an-xposed-apk-is-not-activation) |
| Magisk ZIP | `magisk_zip` | `module.prop` at the archive root; optional `customize.sh`, `post-fs-data.sh`, `service.sh`, `system/`; a recovery-flashable ZIP also has `META-INF/com/google/android/update-binary` and an `updater-script` containing `#MAGISK` | `magisk --install-module ZIP` (or the Magisk app) | reboot: Magisk extracts to `/data/adb/modules/<id>` and runs module scripts at boot | `disable` marker disables it; `remove` marker removes it at the next boot ([Magisk guides](https://topjohnwu.github.io/Magisk/guides.html)) |
| Zygisk ZIP | `zygisk_zip` | a Magisk ZIP plus `zygisk/<abi>.so` native payloads | `magisk --install-module ZIP` | reboot with Zygisk enabled; module scripts see `ZYGISK_ENABLED=1` | the payload is mounted into every app process; Zygisk must be enabled in Magisk |
| LSPosed ZIP | `lsposed_zip` | a Magisk ZIP whose `module.prop` id is `zygisk_lsposed` (or `riru_lsposed`), containing `manager.apk`, `daemon.apk`, `framework/lspd.dex`, `lib/<abi>/liblspd.so`, and for the Zygisk flavour `zygisk/<abi>.so` | `magisk --install-module ZIP` | reboot; the module's `post-fs-data.sh`/`service.sh` run its `daemon` script, which execs `app_process --nice-name=lspd`, and the manager reports status | Zygisk flavour needs Magisk 24+ with Zygisk; Riru flavour needs Riru 26.1.7+. The pinned acquisition imports this ZIP with `source: "official-release"`; acquisition is not installation. |

The distinction matters in the API: `install_module` dispatches on the library
kind (`xposed_apk` → `adb install`, everything else → `magisk
--install-module`), and `install_lsposed` refuses anything that is not a
`lsposed_zip`. Importing an Xposed APK does not make it a Magisk module, and a
Magisk ZIP is not something `adb install` can install.

## ABI coverage

| Component | ABIs documented/observed | Source |
|---|---|---|
| Magisk | module installers get `ARCH` = `arm`, `arm64`, `x86`, `x64` or `riscv64`; Zygisk payload directories are `armeabi-v7a`, `arm64-v8a`, `x86`, `x86_64`, `riscv64` | [Magisk developer guides](https://topjohnwu.github.io/Magisk/guides.html) |
| LSPosed 1.9.2 | ships `lib/x86`, `lib/x86_64`, `lib/armeabi-v7a`, `lib/arm64-v8a`; its `customize.sh` aborts on any other `ARCH` | inspected `LSPosed-v1.9.2-7024-zygisk-release.zip` |
| AVD images here | API 25 `x86` only; API 33/34 `x86_64` | [oobe-api25.md](oobe-api25.md), [live-acceptance-2026-11-27.md](live-acceptance-2026-11-27.md) |

Both Magisk and LSPosed therefore cover emulator `x86`/`x86_64`, which is why the
instrumentation subsystem is usable on AVDs at all. The host must still match the
image ABI (x86_64 hosts run x86_64 images; API 25 `x86` runs under WHPX/KVM on an
x86_64 host).

## Upstream facts that bound the matrix

- **Magisk** current release is **v31.1** (2026-10-25). It drops Android 6.0 and
  makes **Android 7.0 the minimum**, removes interactive `su` prompts (superuser
  access is granted ahead of time in the app) and removes app hiding. The verified
  baseline in this project is **26.1**; do not assume 31.x `su` behaviour
  ([v31.1 release](https://github.com/topjohnwu/Magisk/releases/tag/v31.1),
  [installation](https://topjohnwu.github.io/Magisk/install.html)).
- **Zygisk is off by default.** There is no `settings` row until it is enabled;
  `zygisk=1` is the persisted enabled flag, and `active` means a running `zygiskd`
  (or a staged `loader*` under `$(magisk --path)/.magisk/zygisk`). Do not test
  activation with `/sbin/.magisk` on API 34.
- **Magisk's `su`** is an applet of the `magisk` binary; `--install-module ZIP`
  and `--sqlite SQL` are documented tools
  ([Magisk tools](https://topjohnwu.github.io/Magisk/tools.html)). The
  `settings` table and its `zygisk` key are **internal schema**, not a documented
  interface; this project writes them anyway, so a Magisk release that renames
  them would break `enable_zygisk`.
- **LSPosed** is capped at Android 14 for its latest release (see above), and its
  installer refuses unknown ABIs.
- **XposedBridge** is archived (last push 2021) and its legacy API is frozen.
  Its `assets/xposed_init` + manifest metadata contract is what "legacy Xposed
  module" means here ([XposedBridge tutorial](https://github.com/rovo89/XposedBridge/wiki/Development-tutorial)).
- **rootAVD** is GPL-3.0 and archived (last push 2023-10-19); it patches a shared
  SDK ramdisk in place, so it must be pointed at a copy
  ([rootAVD](https://github.com/newbit1/rootAVD), [oobe-api25.md](oobe-api25.md)).
- **Play Store images** are user builds; `adb root` answers "adbd cannot run as
  root in production builds" ([rooted-images.md](rooted-images.md)).
- **Snapshots** restore the whole device state, including the system image state;
  a Quick Boot restore can silently undo a Magisk/Zygisk change and bypass a
  patched ramdisk. Instrumentation work should cold boot with `-no-snapshot`
  ([emulator snapshots](https://developer.android.com/studio/run/emulator-snapshots),
  [oobe-api25.md](oobe-api25.md)).

## How to fill an `untested` cell

On an accelerated host:

```bash
# 1. What is this image?
adb -s emulator-5554 shell getprop ro.build.type       # userdebug vs user
adb -s emulator-5554 shell getprop ro.debuggable
adb -s emulator-5554 shell getprop ro.build.version.sdk

# 2. adb root
adb -s emulator-5554 root && adb -s emulator-5554 shell id

# 3. Root-only checks, from a device shell (`adb root` or `su` first)
adb -s emulator-5554 shell

# --- inside the device shell ---
su -c id                                    # expects context=u:r:magisk:s0
su -c 'magisk -V'
su -c 'ls /data/adb/magisk'                 # absent => Additional Setup not completed
su -c "magisk --sqlite \"select value from settings where key='zygisk'\""
ps -A | grep zygiskd                        # Zygisk active (x86_64: zygiskd64)
pidof lspd                                  # LSPosed active
su -c 'ls /data/adb/lspd'
exit
```

To install the LSPosed manager (it is not installed by `lspd`):

```bash
adb -s emulator-5554 shell pm install -r /data/adb/modules/zygisk_lsposed/manager.apk
```

or, through the product:

```bash
avdfleet instrumentation toolchain list
avdfleet instrumentation toolchain acquire magisk --confirm    # required by complete_magisk_setup
avdfleet instrumentation toolchain acquire lsposed --confirm
avdfleet instrumentation scan AVD_NAME --json
```

The pinned acquisition supplies the Magisk APK and the LSPosed ZIP (the ZIP is
imported into the library); installing them on a device is still a separate deploy
step. Live checks through the daemon on 2026-11-27 covered acquisition, an API 25
capability scan, a module import, and a full API 34 Magisk + Zygisk + LSPosed
install/activation.

Record the image package (`system-images;android-34;google_apis;x86_64`), the
emulator and adb versions, whether `-writable-system` was passed, and whether a
snapshot was loaded. Without them a row is not reproducible; that is why the
matrix cites a document with a date rather than a bare "works".

## See also

- [instrumentation.md](instrumentation.md): capability model, library, profiles,
  installer configuration, licences, troubleshooting.
- [compatibility.md](compatibility.md): host OS/emulator version matrix.
- [rooted-images.md](rooted-images.md), [oobe-api25.md](oobe-api25.md),
  [live-acceptance-2026-11-27.md](live-acceptance-2026-11-27.md): the measurements
  cited above.
