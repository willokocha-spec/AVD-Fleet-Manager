# Portable edition

The portable edition is a self-contained folder that runs AVD Fleet from a USB
stick or removable disk with **no admin rights and no system-wide Android
tooling**, survives relocation and drive-letter changes, and keeps its mutable
state inside the bundle root. It ships the same engine and GUI as the installed
edition.

This page documents how bundles are built, launched, relocated and verified, and
what cannot be portable.

## What a bundle contains

```
AVD-Fleet-Portable/
  AVD Fleet.cmd | AVD Fleet.command | avdfleet.sh   host launcher at the root
  launchers/                   second copy of the host launcher (execute-bit repair)
  avdfleet-portable.json       checksum manifest (root, version, host, entries, warnings)
  config/config.toml           generated portable daemon config (absolute paths inside root)
  python/                      pinned Python runtime (portable layout, no venv)
  sdk/                         Android command-line tools, platform-tools, emulator
  node/ jdk/                   optional runtimes (Appium / SDK tools)
  lib/ venv/                   optional Python dependency tree / prepared environment
  ui/                          built web GUI (index.html + assets)
  studio/                      studio.properties + studio.vmoptions (STUDIO_PROPERTIES)
  data/ cache/ logs/ tmp/ avd/ android/ studio/   mutable state (all under root)
  cache/downloads/             optional offline cache for images/tools
```

Required components for a successful build: `python/`, `sdk/`, `ui/`. Optional:
`lib/`, `venv/`, `node/`, `jdk/`, `studio/`. The builder copies a **prepared
component tree** — it never downloads anything itself. In this version the
components must already exist in the offline cache (`<cache_dir>/python`, …) or
in a source tree; see [Building a bundle](#building-a-bundle) below.

`avdfleet-portable.json` follows `contracts/bundle.schema.json`: every file is
listed with its POSIX-style relative path, SHA-256 and size; the root recorded
at build time is updated on relocation.

## Building a bundle

```bash
# 1. Prepare the components once, on this host, while online:
#    a pinned Python runtime, the Android SDK tools and the built UI, copied into
#    the cache directory under components named python/, sdk/, ui/ (optional:
#    node/, jdk/, lib/, venv/, studio/) plus downloads/ for your offline images.
#    In this v0.1 implementation the bundle builder never downloads; see
#    "Known limitations" below.
#
# 2. Build the bundle (writes nothing outside the destination):
avdfleet bundle --offline -o ./AVD-Fleet-Portable --dry-run    # preview only
avdfleet bundle --offline -o ./AVD-Fleet-Portable
```

`avdfleet bundle` flags:

| Flag | Meaning |
|---|---|
| `--offline` | Require the offline cache; no network needed on first run. |
| `-o`, `--output PATH` | Destination directory (default `./avdfleet-portable`). |
| `--dry-run` | Show the manifest preview and change nothing. |
| `--force` | Replace a non-empty destination. The old bundle is moved to a timestamped `.backup-*` sibling, never deleted. |
| `--json` | Machine-readable manifest. |

The build is assembled in a hidden sibling directory and atomically renamed, so
a failure never leaves a half-written bundle. It refuses FAT32 outright (4 GiB
file limit; AVD data partitions are 6–8 GB) and warns on exFAT and network
shares. `--offline` without a populated cache fails with an actionable error
that lists the missing required components.

Exit codes: `0` built, `1` failure, `2` usage, `3` environment (missing
components/cache), `4` not found, `5` conflict (non-empty destination without
`--force`).

### Building a portable bundle

There are no prebuilt archives in this repository, and `scripts/build_release.py`
does **not** produce one — it builds the wheel and optional sdist only. The portable
bundle is built with the `bundle` command, which writes a **folder** (not a zip):

```bash
avdfleet bundle --offline -o C:/tmp/AVD-Fleet-Portable
```

`--offline` requires a prepared cache (a uv-managed Python, an Android SDK
build-tools + platform-tools tree, a built `ui/dist` and the resolved dependency
tree); without it the command prepares those itself. Zip the folder yourself if you
need an archive for distribution, and keep the SDK out of it for licensing reasons —
the bundle resolves `<bundle>/sdk` at runtime, so install one with
`avdfleet install --sdk-root <bundle>/sdk` on an x64 host.

Verified on 2026-11-27: a bundle built with `--offline` measured 300,636,387 bytes
across 7,335 manifest entries (about 288 MiB on disk), served the daemon on a
non-default port, exposed the five built-in profiles and both pinned toolchain
artifacts, and imported and deleted a Magisk module ZIP. See
[instrumentation-status.md](instrumentation-status.md) for the recorded result.

## First run

- **Windows**: double-click **`AVD Fleet.cmd`**. On first run it removes the
  Mark-of-the-Web from bundled executables (`Unblock-File`), then starts
  `avdfleet serve --portable --port <port>`, waits for `/health` and opens the
  GUI. Optional first-run helper: `bin\first-run.ps1`.
- **macOS**: open **`AVD Fleet.command`** (or run it in Terminal). It checks for
  the quarantine attribute and prints the `xattr -dr com.apple.quarantine`
  remedy if Gatekeeper blocks it, then starts the daemon and opens the GUI.
- **Linux**: `./avdfleet.sh` (the bundled copies live under `launchers/` and are
  re-`chmod +x`-ed on launch, in case the archive lost the execute bit).

Launcher flags: `--port <n>` (default 8765; a free port is chosen if busy),
`--help`. Exit codes: `0` clean exit, `2` usage, `3` missing runtime, `4`
preflight refused, `5` server failure, `130` interrupted.

The launcher exports the full portable environment before starting any child
process: `ANDROID_HOME`, `ANDROID_SDK_ROOT`, `ANDROID_USER_HOME`,
`ANDROID_EMULATOR_HOME`, `ANDROID_AVD_HOME`, `JAVA_HOME`, `UV_*`, `TMPDIR`/
`TMP`/`TEMP`, `STUDIO_PROPERTIES`, `STUDIO_VM_OPTIONS`, `GRADLE_USER_HOME`,
`PYTHONNOUSERSITE=1`. `HOME`/`USERPROFILE` are never redirected.

The portable daemon writes `auth_required = true` and creates its bearer token
under `<root>/data/token`; `AVDFLEET_TOKEN` still overrides it. No telemetry and
no update check are enabled.

## Preflight checks

`avdfleet portable checks [--root PATH] [--json]` runs the portability
preflight; the launcher refuses to start if a critical check fails. Checks:

| Check | What it proves | Severity |
|---|---|---|
| `filesystem` | Filesystem type: refuses FAT32, warns exFAT/network shares, accepts NTFS/APFS/ext4/XFS/Btrfs/ZFS | critical on FAT32 |
| `noexec` | Mount options: detects a `noexec` mount (Linux/macOS) via mount table plus an execute probe | critical |
| `quarantine` | Windows Mark-of-the-Web (`Zone.Identifier`) / macOS quarantine attributes | warning |
| `free_space` | Free space threshold for AVD data and images (`FAIL_FREE_GIB`/`MIN_FREE_GIB`) | critical when below the fail threshold |
| `write_speed` | Small write benchmark inside `<root>/tmp`; reports throughput (warn below the threshold) | warning |
| `hypervisor` | WHPX / KVM / HVF availability, nested-virtualisation reporting (detect only — never installs) | warning (cannot be fixed portably) |

`avdfleet portable status` prints the edition, root, marker, manifest summary
and leftovers list. `avdfleet portable verify [ROOT]` hashes every file against
`avdfleet-portable.json` and fails on missing/modified/unlisted files.

## Relocation (move the folder / change drive letter)

The bundle records absolute paths in `config.toml`, AVD `.ini` files and the
manifest. After moving the folder, either launch again (the launcher detects a
root change and re-runs relocation) or run:

```bash
avdfleet portable relocate ./AVD-Fleet-Portable --dry-run
avdfleet portable relocate ./AVD-Fleet-Portable
```

Relocation rewrites recorded roots (config, manifest, AVD `.ini` `path=`
entries), deletes stale `hardware-qemu.ini`/lock files, **invalidates snapshots**
(snapshot state is tied to absolute paths), repairs a prepared virtualenv only
if one exists, re-verifies that the emulator still lists the AVDs, and updates
the manifest root. `--dry-run` reports the rewrites without touching anything.

## Leftovers (writes outside the bundle root)

The bundle cannot promise zero writes outside its root. `portable verify` uses
the documented allow-list; anything else is a failure. Documented leftovers:

| Host | Leftover | Why |
|---|---|---|
| All | `~/.android/` (adb keys, prefs, running discovery) | any tool that ignores `ANDROID_USER_HOME`, adb keys |
| All | OS keyring entry for the proxy vault if the keyring backend is used | keyring stores outside files — use the file vault for portability |
| Windows | `%LOCALAPPDATA%\Google\<Studio product>\`, `%APPDATA%\Google\…`, `%USERPROFILE%\.gradle\`, `%USERPROFILE%\AndroidStudioProjects\`, `%TEMP%\android-*`, `HKCU\Software\Python\Astral\CPython<ver>` (only if `uv python install` runs), installer/uninstall registry entries | Studio/Gradle/temp/uv registration (Studio only if used) |
| macOS | `~/Library/{Application Support,Caches,Logs}/Google/AndroidStudio*`, `~/Library/Preferences/*AndroidStudio*.plist`, `~/.gradle/`, temp dirs | Studio state not covered by `idea.properties`, quarantine attributes |
| Linux | `~/.config`, `~/.local/share`, `~/.cache` under `Google/AndroidStudio*`, `~/.gradle/`, `/tmp/android-*`, `~/.java/.userPrefs/` | Studio/Gradle/temp/Java prefs |
| Windows | bundle files may carry `Zone.Identifier` until unblocked | Mark-of-the-Web |

Android Studio portability is limited by design: `STUDIO_PROPERTIES` points at a
**file** (`studio/studio.properties`, not a directory) that redirects
`idea.config.path`, `idea.system.path`, `idea.plugins.path`, `idea.log.path` and
`idea.caches.path` into the bundle; `studio.vmoptions` sets the same as system
properties. The rest of Studio's state is documented leftover, never zero.

## What cannot be portable (explicit)

1. **Hardware virtualisation.** WHPX (Windows feature + reboot), KVM (kernel
   module + `/dev/kvm` permissions), HVF (macOS built-in). The portable edition
   detects and diagnoses; it cannot install drivers or change firmware. The
   emulator has no usable software fallback.
2. **Google components licence.** Google's Android SDK licence forbids
   redistributing cmdline-tools, platform-tools, emulator, Google APIs/Play system
   images, Studio or JBR. A bundle you hand to someone else may ship only
   OSS components; Google components must be downloaded/accepted on the target
   (or limited to the operator's own licensed local SDK). The bundle never ships
   Play images; offline first run is limited to a bundle prepared from the
   operator's own cache.
3. **Cross-OS bundles.** Python runtime, JDK and emulator are per-OS/arch: one
   bundle per host target (Windows x64, macOS arm64, macOS x64, Linux x64).
   Windows-on-ARM and Linux-on-ARM are unsupported.
4. **Admin-level setup.** udev rules, kernel drivers, Windows optional features
   and any admin-elevated install are out of scope for a standard account.
5. **macOS Gatekeeper/notarisation.** The assembled bundle is unsigned; the
   operator may need to approve it (`xattr -dr com.apple.quarantine`, Open
   Anyway).
6. **Filesystem guarantees.** FAT32 is refused; exFAT loses POSIX semantics;
   `noexec` cannot be worked around without remount privileges.
7. **A shipped Python virtualenv.** Python venvs are inherently non-portable
   (absolute shebangs); the bundle ships a relocatable runtime plus a plain
   `lib/`/target install instead.
8. **`~/.android` and `$HOME`-relative caches** from legacy tools and adb keys —
   reducible to near-zero, documented in the leftovers list rather than claimed
   zero.

## Known limitations in this version

- The bundle builder performs **no network downloads**: it copies a prepared
  source tree or the populated cache. The documented workflow is to prepare
  `python/`, `sdk/`, `ui/` in the cache once (from the operator's licensed local
  SDK and OSS tools), then build `--offline`. A one-command "bundle everything
  from the internet" is not implemented.
- The portable edition is built per host OS/arch and is created on that host
  (`host_os`/`host_arch` in the manifest); cross-building is unsupported.
- GUI screenshots of the portable launcher are pending (see
  [acceptance.md](acceptance.md)).

## Security notes

- Bundle verification rejects path traversal, absolute paths, symlinks, modified
  and missing files; FAT32/noexec are refused before running.
- The daemon stays loopback-only in portable mode; the token lives inside the
  bundle and is created owner-only.
- Proxy credentials live in the file vault (passphrase via
  `AVDFLEET_VAULT_PASSPHRASE` or a generated key file inside `data/`); they are
  never written to the SQLite database or the manifest.
