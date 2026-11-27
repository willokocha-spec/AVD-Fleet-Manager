# R1 — Versions, download URLs, SHA-256s and package IDs

**Author:** research-core (R1) · **Accessed:** 2026-11-26 (Europe/Dublin) · **Status:** read-only research
**Scope:** Android Studio, Android command-line tools, Android CLI, platform-tools, emulator, JDK
(Temurin / Microsoft / Android Studio JBR), Node.js LTS, Appium 4 + UiAutomator2 driver, Appium
Inspector, scrcpy, uv. Per-host package IDs (sdkmanager / winget / brew / apt / dnf / pacman) and
license implications.

## Legend — how to read this document

| Tag | Meaning |
|---|---|
| **VERIFIED** | Value read directly on the upstream page/API named in the row on 2026-11-26. |
| **VERIFIED-2ND** | Value read from a first-party downstream manifest (winget-pkgs YAML generated from the official installer; Homebrew JSON; PyPI/npm registry metadata). Upstream URL and digest match the manifest, but the publisher does not publish this digest itself. |
| **UNVERIFIED** | No published value found. **Do not invent one.** The installer/downloader must fail closed or compute-and-pin at build time. |
| **INFERRED** | Conclusion derived from two verified facts (derivation shown). |
| **STALE** | Package-manager copy exists but lags the upstream release (shown with delta). |

All hashes are lowercase hex unless noted. "Size" is the published byte size where available.
`repository2-3.xml` = the sdkmanager package index at
<https://dl.google.com/android/repository/repository2-3.xml> (**VERIFIED**, fetched 2026-11-26;
435,058 bytes; it is the authoritative source for sdkmanager package IDs, revisions and SHA-1s).

> **SHA-256 vs SHA-1 policy for this project.** Google publishes **SHA-256 on the
> developer.android.com download pages** for Android Studio and command-line tools, and publishes
> **only SHA-1** inside `repository2-3.xml` for sdkmanager packages (platform-tools, emulator,
> cmdline-tools, system images). Where only SHA-1 exists this document says so; the product must
> not present a SHA-1 as SHA-256. For emulator/platform-tools on macOS/Linux there is no official
> SHA-256: either pin the sdkmanager package by revision (integrity then relies on
> `https` + Google's index) or compute the digest on first acquisition and record it in the install
> manifest (see §13 recommendations).

---

## Summary table (one row per component)

| Component | Current stable (2026-11-26) | Official publisher channel | SHA-256 published? |
|---|---|---|---|
| Android Studio | **Rabbit 2 \| 2026.2.2 Patch 3** (build `2026.2.2.11`) | dl.google.com / edgedl.me.gvt1.com | **Yes** (all 6 packages) |
| Android command-line tools | download page zip build **15859902** (rev **22.0**); SDK repo `cmdline-tools;latest` = rev **23.0** build **16111833** | dl.google.com | **Yes** for 15859902; only SHA-1 for 16111833 |
| Android CLI (new) | **1.1.16919734** (2026-11-13) | dl.google.com/android/cli | Windows only (via winget manifest); others **UNVERIFIED** |
| platform-tools | **37.0.3** (Nov 2026) | dl.google.com + sdkmanager | Windows only (winget); macOS/Linux SHA-1 only |
| emulator | **37.3.10 stable** (2026-11-19, build `16970787`) | sdkmanager / dl.google.com | **No** (SHA-1 in repo XML only) |
| JDK — Temurin | **25.0.5.1+2 LTS** (current LTS); **21.0.13.1+2 LTS** (previous LTS) | adoptium.net / api.adoptium.net | **Yes** (API + `.sha256.txt` per binary) |
| JDK — Microsoft Build of OpenJDK | **25.0.5.1 LTS**; **21.0.13.1 LTS** | learn.microsoft.com/java/openjdk | **Yes** (`.sha256sum.txt` per package) |
| JDK — Android Studio JBR | bundled with Studio (JBR recommended) | inside Studio distribution | n/a — exact build **UNVERIFIED** |
| Node.js LTS | **26.13.1** ("Lithium", Active LTS since 2026-10-28) | nodejs.org/dist | **Yes** (`SHASUMS256.txt`) |
| Appium 4 | **4.0.1** (2026-11-23) | npm `appium` | **No SHA-256** (npm SHA-512 integrity only) |
| UiAutomator2 driver | **10.4.0** (2026-11-25) | npm `appium-uiautomator2-driver` | **No SHA-256** (npm SHA-512 integrity only) |
| Appium Inspector | **2026.11.6** (2026-11-13) | GitHub releases / winget | **No SHA-256** (Electron SHA-512 in `latest.yml`) |
| scrcpy | **5.0.1** (2026-10-08) | GitHub releases | **Yes** (`SHA256SUMS.txt` + `.asc`) |
| uv | **0.13.10** (2026-11-24) | GitHub releases / PyPI / astral.sh | **Yes** (`sha256.sum`, per-asset `.sha256`, PyPI digests) |

---

## 1. Android Studio

**Current stable: Android Studio Rabbit 2 | 2026.2.2 Patch 3** — **VERIFIED** on
<https://developer.android.com/studio> (also `/studio/releases`), 2026-11-26. Build id in download
paths is `2026.2.2.11`; the license shown is the Android SDK License Agreement dated **April 28,
2026**. Minimum supported Studio for Cloud services is now "Panda 2" (i.e. 2025.2.x), per the same
page.

### Downloads and published SHA-256 (VERIFIED)

| Host | File | Size | SHA-256 | URL |
|---|---|---|---|---|
| Windows x64 (installer) | `android-studio-rabbit2-patch3-windows.exe` | 1.5 GB | `0e790c951d19e34e1cf4dbbeb7441c18e445bf594be08c9176b150229134c7fd` | <https://edgedl.me.gvt1.com/android/studio/install/2026.2.2.11/android-studio-rabbit2-patch3-windows.exe> |
| Windows x64 (zip, no installer) | `android-studio-rabbit2-patch3-windows.zip` | 1.5 GB | `56a077b4a48ae5029fb952962e68aa0139756fc23d83e74053bbe8595bdc9ab7` | <https://edgedl.me.gvt1.com/android/studio/ide-zips/2026.2.2.11/android-studio-rabbit2-patch3-windows.zip> |
| macOS Intel (dmg) | `android-studio-rabbit2-patch3-mac.dmg` | 1.5 GB | `49128aeb9b122a8a3ebbb2bb10ce5072146f3df8769a7f1a02f56ef9b1d24da0` | <https://edgedl.me.gvt1.com/android/studio/install/2026.2.2.11/android-studio-rabbit2-patch3-mac.dmg> |
| macOS Apple Silicon (dmg) | `android-studio-rabbit2-patch3-mac_arm.dmg` | 1.5 GB | `6ecbca33a56c5af0916ddaf2e8cfc9aac9fd28fdae30ec7c5f5ad01944b73956` | <https://edgedl.me.gvt1.com/android/studio/install/2026.2.2.11/android-studio-rabbit2-patch3-mac_arm.dmg> |
| Linux x64 (tar.gz) | `android-studio-rabbit2-patch3-linux.tar.gz` | 1.6 GB | `1c7ccf6dbb0ac6376e3b15e536096f1878621b4b5586d81e22510cd835f1438d` | <https://edgedl.me.gvt1.com/android/studio/ide-zips/2026.2.2.11/android-studio-rabbit2-patch3-linux.tar.gz> |
| ChromeOS (deb) | `android-studio-rabbit2-patch3-cros.deb` | 1.3 GB | `338397ac609747ac1f1ec4b19f61ced02bf2309a83047239495c6c9ab8e0219c` | <https://edgedl.me.gvt1.com/android/studio/install/2026.2.2.11/android-studio-rabbit2-patch3-cros.deb> |

Notes:
- The `edgedl.me.gvt1.com` host is Google's CDN front for `dl.google.com/android/studio/...`; the
  version-pinned path is stable and is the correct thing to pin. The general landing page is
  <https://developer.android.com/studio>; its download table publishes the hashes above.
- There is **no sdkmanager package ID** for Studio (Studio is not an SDK package). Package manager
  IDs: **winget `Google.AndroidStudio` = 2026.2.2.10** (**STALE**, one patch behind the .11 release;
  also `Google.AndroidStudio.Beta` `2026.2.3.5`, `Google.AndroidStudio.Canary` `2026.3.1.3`) — VERIFIED
  by local `winget search` on this host; **brew cask `android-studio` =
  `2026.2.2.11,rabbit2-patch3`** — VERIFIED at
  <https://formulae.brew.sh/api/cask/android-studio.json>.
- **Flathub:** app id **`com.google.AndroidStudio` = 2026.2.2.11** (**VERIFIED** from
  <https://flathub.org/api/v2/appstream/com.google.AndroidStudio>, releases
  `2026.2.2.11, 2026.2.2.10, 2026.2.2.9, 2026.2.2.8`, current). Flathub tracks the current stable
  exactly. Caveat: a Flatpak is sandboxed, so the emulator inside it needs `/dev/kvm`/device access;
  whether this app's Flathub permissions expose it was **not inspected → UNVERIFIED**. Do not make
  Flatpak the primary install path for a fleet host.
- **UNVERIFIED:** no apt/dnf/pacman package. Studio is not packaged by Debian/Ubuntu/Fedora/Rocky
  and is **not** in Arch official repos (Arch package search for `android` returns no
  `android-studio`, 2026-11-26); Arch users use the AUR (not checked here) or the tarball.

### License / SDK-license implications
- Studio download is gated by the **Android Software Development Kit License Agreement**
  (currently dated 2026-04-28). §3.4 restricts copying/redistribution/modification. **Studio bytes
  must not be redistributed in the portable bundle.** `InstallOptions.skip_studio` and
  `Config.studio_required=False` are correct defaults; the installer should install Studio only on
  explicit opt-in and never from a bundle cache.

---

## 2. Android command-line tools ("cmdline-tools")

There are **two different "current" builds**; this matters for pinning and for checksum coverage.

### 2a. developer.android.com download page (zip, SHA-256 published)

**Build 15859902 = cmdline-tools revision 22.0** — **VERIFIED** on
<https://developer.android.com/studio> ("Android Command Line Tools" table + per-platform
download dialogs), cross-checked against `repository2-3.xml` (`cmdline-tools;22.0`).

| Host | File | Size | SHA-256 | URL |
|---|---|---|---|---|
| Windows x64 | `commandlinetools-win-15859902_latest.zip` | 155.7 MB | `90ae805d20434428bffcb699c290860f19bb5f66a67e6b330067e3de801fb04a` | <https://dl.google.com/android/repository/commandlinetools-win-15859902_latest.zip> |
| macOS Intel | `commandlinetools-mac_x86_64-15859902_latest.zip` | 156.3 MB | `c5a6378ab5cf7e0d5701921405115befff13e9ff7417fb588389338f8bd050f3` | <https://dl.google.com/android/repository/commandlinetools-mac_x86_64-15859902_latest.zip> |
| macOS Apple Silicon | `commandlinetools-mac_arm64-15859902_latest.zip` | 156.1 MB | `835b62a26162b229b441d1f6d4680383815a270809eb33522c0d480fa5002c4e` | <https://dl.google.com/android/repository/commandlinetools-mac_arm64-15859902_latest.zip> |
| Linux x64 | `commandlinetools-linux-15859902_latest.zip` | 181.8 MB | `4e4c464f145a7512b57d088ac6c278c03c9eea610886b35a5e0804e74eedf583` | <https://dl.google.com/android/repository/commandlinetools-linux-15859902_latest.zip> |

### 2b. sdkmanager index (`repository2-3.xml`) — `cmdline-tools;latest` = revision **23.0**, build **16111833**

**VERIFIED** in `repository2-3.xml` (remotePackage `cmdline-tools;latest`, channel-0/stable).
Only **SHA-1** is published there. Per-archive: Linux `commandlinetools-linux-16111833_latest.zip`
size 181,052,239 sha1 `e025545c62a8e64c7559119566a569fb1dec5f60`; macOS x64
`commandlinetools-mac_x86_64-16111833_latest.zip` 155,582,504 sha1 `112cf9618794a997ff273537d55bee02c22abffe`;
macOS arm64 `commandlinetools-mac_arm64-16111833_latest.zip` 155,384,151 sha1 `ad03dc49bfacfd52c110b14104ea548b8a07e830`;
Windows `commandlinetools-win-16111833_latest.zip` 154,957,218 sha1 `57d04f2d75eb8e8fffc5000a987e5de4b5a63e9d`.

**INFERRED discrepancy:** the Studio download page (2a) is one revision behind the sdkmanager index
(2b): 22.0/15859902 vs 23.0/16111833. Both are channel-0. The download page is the only place with
SHA-256, so an installer that wants a published SHA-256 should pin **22.0 / 15859902** and note it is
one revision behind; an installer that wants newest should use sdkmanager `cmdline-tools;23.0`
(SHA-1 only from Google, or pin the SHA-1 from the index). Either way, **pin an explicit revision —
never `latest`.**

- **sdkmanager package IDs:** `cmdline-tools;latest` (mutable alias → 23.0 today),
  `cmdline-tools;23.0` (immutable), `cmdline-tools;22.0` (immutable, the one with published SHA-256).
- **winget:** no first-party Google cmdline-tools package verified. **UNVERIFIED** — do not assume one.
- **brew:** cask **`android-commandlinetools` = 15859902** — **VERIFIED** at
  <https://formulae.brew.sh/api/cask/android-commandlinetools.json> (matches 2a; **STALE** vs 2b).
- **apt/dnf/pacman:** no official Google package for cmdline-tools. Debian/Ubuntu side has historical
  `android-sdk`-family packages (not checked on this host; **UNVERIFIED**) — do not depend on distro
  copies for a pinned toolchain.
- **JDK dependency:** `sdkmanager`/`avdmanager` are JVM tools, so a JDK is required on
  `JAVA_HOME`/`PATH` before cmdline-tools can run. The exact minimum JDK version is **not stated**
  on the sdkmanager/cmdline-tools pages checked → **UNVERIFIED**; install JDK 21 LTS (Temurin) and
  pin it (see §6).
- **License:** Android SDK License (the `sdkmanager --licenses` prompt accepts `android-sdk-license`;
  preview channels also require `android-sdk-preview-license`). Redistribution prohibited (§3.4).

---

## 3. Android CLI (new `android` tooling)

**Current: 1.1.16919734, released 2026-11-13** — **VERIFIED** from the official Android CLI
download page (<https://developer.android.com/tools/agents/android-cli/download>) and from the
winget-pkgs manifest `manifests/g/Google/AndroidCLI/1.1.16919734/` (**VERIFIED-2ND** for the hash).

This is a **new first-party CLI** (announced in preview, docs at
<https://developer.android.com/tools/agents/android-cli>) that wraps SDK/AVD/emulator/device
management plus build/run/install/profile commands: `android --version`, `android update`,
`android init`, `android info`, `android sdk list|install|update|remove`, `android avd ...`,
`android emulator ...`, `android install`, `android run`, `android screen`, `android input`,
`android layout`, `android docs`, `android skills`, `android studio`, etc. It can also manage
commands like `android install` (platform-tools 37.0.3 deprecates `adb install --fastdeploy` in
favour of it). Config file: `~/.androidrc` (`%USERPROFILE%\.androidrc` on Windows); global options
`--sdk`, `-v`, `-V`, `-h`; **usage metrics are collected** and can be disabled with `--no-metrics`.

Official install channels (**VERIFIED** on the download page):

| Host | Method | Command / artifact |
|---|---|---|
| Linux x86_64 | curl \| bash (local) | `curl -fsSL https://dl.google.com/android/cli/latest/linux_x86_64/install.sh \| bash` |
| Linux x86_64 | curl \| bash (global) | `.../install_root.sh \| bash` |
| Linux x86_64 | apt repo | `deb [arch=amd64 signed-by=...] http://dl.google.com/android/cli/latest/debian/ stable main` → `apt-get install android-cli` |
| Linux x86_64 | direct binary | <https://dl.google.com/android/cli/latest/linux_x86_64/android> |
| macOS arm64 / x64 | curl \| bash | `.../darwin_arm64/install.sh` / `.../darwin_x86_64/install.sh` (also `install_root.sh`) |
| macOS | Homebrew tap | `brew tap android/tap && brew install android-cli` |
| macOS | direct | `https://dl.google.com/android/cli/latest/darwin_arm64/android`, `.../darwin_x86_64/android` |
| Windows x64 | winget | `winget install --id Google.AndroidCLI` |
| Windows x64 | curl \| cmd | `curl.exe -fsSL https://dl.google.com/android/cli/latest/windows_x86_64/install.cmd -o "%TEMP%\i.cmd" && ...` |
| Windows x64 | direct | <https://dl.google.com/android/cli/latest/windows_x86_64/android.exe> |

**Pinned, checksummed URL + SHA-256 (VERIFIED-2ND, winget manifest generated from the official
binary):**

```
PackageIdentifier: Google.AndroidCLI
PackageVersion:    1.1.16919734
ReleaseDate:       2026-11-13
InstallerType:     portable          # command alias: android
URL:     https://dl.google.com/android/cli/1.1.16919734/windows_x86_64/android.exe
SHA256:  A236AA879F63EE7F2E4267047B2FCDC2FA09CD56024803E3844DF67FF1789636
```

- **SHA-256 for macOS/Linux binaries: UNVERIFIED.** No checksum file is published next to the
  per-OS binaries, and the installer script below does **not** verify anything.
- **Do not use `curl | bash` for this tool.** The published `install.sh` (fetched 2026-11-26,
  <https://dl.google.com/android/cli/latest/linux_x86_64/install.sh>) downloads
  `.../cli/latest/<os_arch>/android` (an unpinned "latest" URL), writes it to `~/.local/bin/android`,
  edits shell profiles, and runs it with `ANDROID_CLI_FRESH_INSTALL=1` — **no checksum verification**.
  This directly conflicts with BRIEF §3.2 ("Never `curl | sh` unpinned pipelines without checksum
  verification where upstream publishes one"). Upstream publishes a SHA-256 only through the winget
  manifest for a pinned Windows build, so: install from a **version-pinned direct URL** and verify the
  winget SHA-256 where available; otherwise record a computed digest in the install manifest.
- **License: UNVERIFIED.** No license text is presented on the download page/installer. It is
  first-party Google tooling that collects usage metrics; treat it as proprietary/Google-licensed and
  **do not redistribute it in the portable bundle** until the license is confirmed. It is also
  *optional*: the SDK/AVD operations it covers are covered by cmdline-tools + emulator, so the
  `sdk/androidcli` backend (S1a) can degrade to `sdk/legacy` (S1b) when Android CLI is absent.

---

## 4. platform-tools

**Current: 37.0.3 (November 2026)** — **VERIFIED** on
<https://developer.android.com/tools/releases/platform-tools> (release notes) and in
`repository2-3.xml` (`platform-tools`, revision 37.0.3, channel-0/stable). Release notes mention:
`adb install --fastdeploy` deprecated in favour of `android install`; `adb pair --qr`; new
`libadbusb` USB backend on Linux/macOS (legacy via `ADB_USB_LEGACY=1`); mDNS backend now
`libadbmdns` only (openscreen removed in 37.0.1); macOS fixes for Apple Silicon M6/macOS 27.

### Pinned URLs + published digests

| Host | Pinned URL (SHA-1 in repo XML) | Size | SHA-1 (VERIFIED) | SHA-256 |
|---|---|---|---|---|
| Linux | <https://dl.google.com/android/repository/platform-tools_r37.0.3-linux.zip> | 9,571,024 | `e1579b288a7ae4e9bc28dedec6a154d78b3c04ed` | **UNVERIFIED** |
| macOS (universal) | <https://dl.google.com/android/repository/platform-tools_r37.0.3-darwin.zip> | 16,597,626 | `d48920e2650321710b4d15d3acd9e277d59cd55b` | **UNVERIFIED** |
| Windows | <https://dl.google.com/android/repository/platform-tools_r37.0.3-win.zip> | 8,188,601 | `7fea85bc18d0480bc81d3603952d666f49496e5a` | `89D108360A3BF2924D3BEA837239EDACCC22FD6FF5BEFD3787C4348A9FE4F09D` (**VERIFIED-2ND**, winget `Google.PlatformTools` 37.0.3, ReleaseDate 2026-11-19) |

Stable "latest" aliases (content changes over time — **do not pin by hash**):
<https://dl.google.com/android/repository/platform-tools-latest-linux.zip>,
`...-latest-darwin.zip`, `...-latest-windows.zip`.
Note the macOS zip is a single universal binary (has been since platform-tools 32.0.0) — it serves
both Apple Silicon and Intel.

- **sdkmanager ID:** `platform-tools`.
- **winget:** `Google.PlatformTools` = **37.0.3** (**VERIFIED**, local `winget search`, 2026-11-26).
- **brew:** cask `android-platform-tools` = **37.0.1** — **STALE** vs 37.0.3 (VERIFIED at
  <https://formulae.brew.sh/api/cask/android-platform-tools.json>).
- **apt (Debian/Ubuntu):** distro `adb`/`fastboot` come from distro packages (`android-tools-adb`,
  `android-tools-fastboot`, or `adb` in newer suites) — versions lag and are **UNVERIFIED** here;
  Arch ships `android-tools 37.0.0.p1` (**VERIFIED**, <https://archlinux.org/packages/extra/x86_64/android-tools/>).
- **JDK:** none needed (`adb`/`fastboot` are native binaries).
- **License:** Android SDK License; redistribution prohibited — same caveat as §2 for bundles.

---

## 5. emulator

**Current stable: 37.3.10 (November 19, 2026), package build `16970787`** — **VERIFIED** on
<https://developer.android.com/studio/releases/emulator> ("37.3.10 Stable (Nov 19, 2026)") and in
`repository2-3.xml` (remotePackage `emulator`, revision 37.3.10, channel-0/stable).

Release highlights that matter for this product (**VERIFIED** from the same page):
- **KosmicKrisp Vulkan-on-Metal** graphics driver added for macOS 26+.
- SD card support on ARM64 virtual devices.
- Fixes: Windows emulator hang when AVD/user path contains **non-ASCII characters**; Quick Boot
  snapshot crash loading 37.2 snapshots after 37.3 upgrade; Windows headless power-throttling onto
  E-cores on Intel hybrid CPUs (2–3× slower cold boot); time-zone detection on non-English Windows 11;
  netsim init deadlock; concurrent-startup race on `%TMP%\avd\running\pid_*.ini` discovery files;
  `-snapshot` name ignored on exit; Linux startup failure when `libXdmcp`/`libbsd.so.0` missing (now
  bundles hermetic `libxcb-aemu.so`).
- **37.1.11 (Jul 30, 2026): AEHD will sunset on December 31, 2026**; Studio now helps migrate
  Windows users to WHPX.
- **36.6.11 (Jun 2, 2026): from API 37 the emulator enforces a 4 GB minimum AVD RAM** (config is
  overwritten to ≥4 GB).
- 36.5.10 (Apr 2, 2026): new emulator **networking stack** (multi-device discovery, no manual adb
  port forwarding) — relevant to R4.
- 36.4.9 (Feb 10, 2026): `-gpu software` mode introduced; **Lavapipe is the default software
  renderer**; `swiftshader_indirect`, `swangle_indirect`, `guest` marked deprecated.
- Canary preview: next-generation emulator on upstream QEMU 11+ starting Rabbit 3 Canary 1 (API 37.0+
  AVDs), legacy QEMU 2 fork for existing AVDs.

### Pinned URLs + digests (SHA-1 only — VERIFIED from repo XML)

Stable `emulator` 37.3.10 (`channel-0`):

| Host/arch | URL | Size | SHA-1 |
|---|---|---|---|
| Linux x64 | `emulator-linux_x64-16970787.zip` | 350,068,887 | `5f7314368fc6c05c937f6991bb4106214dc389e7` |
| macOS x64 (Intel) | `emulator-darwin_x64-16970787.zip` | 487,638,528 | `8845fc2dd60325425e3013dd0ed2c50e0beb68c0` |
| macOS arm64 (Apple Silicon) | `emulator-darwin_aarch64-16970787.zip` | 420,629,851 | `3ec739595d44092e835f006d806dd380bc8d6dd7` |
| Windows x64 | `emulator-windows_x64-16970787.zip` | 456,613,348 | `7599293267e61ce67423f87848ff8219da09258b` |

All four are under `https://dl.google.com/android/repository/`. **SHA-256: UNVERIFIED** — Google
publishes no SHA-256 for emulator archives. (The "Emulator download archive" page is an iframe/JS
widget with no static checksum table; its manual-install instructions were verified:
<https://developer.android.com/studio/emulator_archive>.)

Also present in `repository2-3.xml` (**VERIFIED**):
- `emulators;latest` = 37.1.10 build `16969890`, **channel-2** (preview/dev), includes
  `emulator_windows_aarch64-16969890.zip` (Windows ARM64 emulator exists only in preview channels —
  see R2: Studio does not support Windows-on-ARM).
- `emulator` = 37.4.2 build `16982698`, **channel-2** (preview) — newer than stable; do not ship.

- **sdkmanager IDs:** `emulator` (stable, resolves to 37.3.10 today), `emulators;latest` (preview),
  `emulators;<build>` for pinning an exact preview build. Use `--channel=0` for stable.
- **winget:** no Google emulator package verified (**UNVERIFIED**); emulator is installed via
  sdkmanager/Studio only.
- **License:** Android SDK License; the emulator package contains third-party notices
  (<https://cs.android.com/android/platform/superproject/+/master:prebuilts/android-emulator/NOTICE>).
  Redistribution prohibited; portable bundle must download it on first run.

---

## 6. JDK

Three viable sources. **Recommendation: pin Eclipse Temurin 21 LTS** for `JAVA_HOME` (needed by
`sdkmanager`/`avdmanager`/Studio/Gradle), because 21 is the long-supported LTS baseline for Android
tooling; offer 25 LTS as an option and never rely on the JBR being present when Studio is skipped.

### 6a. Eclipse Temurin (Adoptium) — **VERIFIED**

Latest LTS releases as of 2026-11-26 (**VERIFIED** via <https://api.adoptium.net/v3/info/available_releases>:
`available_lts_releases=[8,11,17,21,25]`, `most_recent_lts=25`):

| Line | Latest GA | Windows x64 zip SHA-256 | Notes |
|---|---|---|---|
| **25 (current LTS)** | `25.0.5.1+2` (`jdk-25.0.5.1+2`) | `849d380ab0a64d9098e7d3a2de28beb73841c3b255c8284f019ccd8748c5eb5b` | 141,295,462 B |
| **21 (previous LTS)** | `21.0.13.1+2` (`jdk-21.0.13.1+2`) | `bbebea1469996b276683c9299c227b294f85c847039c82633218186a4ad4fbf2` | 205,447,663 B |

Verified package URL examples (from the Adoptium API; the API also exposes `checksum_link` /
`.sha256.txt` for every OS/arch):
- <https://github.com/adoptium/temurin25-binaries/releases/download/jdk-25.0.5.1%2B2/OpenJDK25U-jdk_x64_windows_hotspot_25.0.5.1_2.zip>
- <https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.13.1%2B2/OpenJDK21U-jdk_x64_windows_hotspot_21.0.13.1_2.zip>
- Machine-readable per-platform assets+checksums: `https://api.adoptium.net/v3/assets/latest/{21|25}/hotspot?architecture={x64|aarch64}&image_type=jdk&os={windows|mac|linux}&vendor=eclipse`
- Landing pages: <https://adoptium.net/temurin/releases/> (Adoptium news confirms
  "Eclipse Temurin 17.0.21, 21.0.13, 25.0.5 and 27.0.1 Available" — 27.0.1 is a **non-LTS** feature
  release; do not target it for the fleet).
- Package IDs: winget **`EclipseAdoptium.Temurin.21.JDK`** = `21.0.13.102` (**VERIFIED** via local
  `winget search`; `EclipseAdoptium.Temurin.25.JDK` exists as a winget package directory —
  **VERIFIED-2ND** pattern, version `25.0.5.1` expected but not separately queried); brew cask
  `temurin@21` / `temurin@25` — **UNVERIFIED** (not queried); apt: `openjdk-21-jdk`/`openjdk-25-jdk`
  (Debian 13 ships both, **VERIFIED** via Repology: `openjdk-21 21.0.13.1+2`, `openjdk-25 25.0.5.1+2`);
  Arch: `jdk21-openjdk 21.0.13.1.u2`, `jdk25-openjdk 25.0.5.1.u2` (**VERIFIED**); Fedora:
  `java-21-openjdk`/`java-25-openjdk` (**INFERRED** naming; not queried).
- **License: GPLv2 + Classpath Exception (OpenJDK).** Redistributable; safe to bundle if the
  project chooses a standalone JDK.

### 6b. Microsoft Build of OpenJDK — **VERIFIED**

<https://learn.microsoft.com/en-us/java/openjdk/download> (accessed 2026-11-26) lists:
**OpenJDK 25.0.5.1 LTS** and **OpenJDK 21.0.13.1 LTS**, with `<package>.sha256sum.txt` and `.sig`
links per artifact. Package filenames (verified):
`microsoft-jdk-25.0.5.1-windows-x64.exe|.msi|.zip`, `-windows-aarch64.exe|.msi|.zip`,
`-macos-x64.pkg|.tar.gz`, `-macos-aarch64.pkg|.tar.gz`, `-linux-x64.tar.gz`,
`-linux-aarch64.tar.gz` (same pattern for 21.0.13.1).
Package IDs: winget `Microsoft.OpenJDK.21` / `Microsoft.OpenJDK.25` (**VERIFIED-2ND** winget-pkgs
directories exist). License: GPLv2+CE. **SHA-256: published** as `.sha256sum.txt` per artifact
(the concrete values were not transcribed here — fetch the `.sha256sum.txt` next to the chosen
artifact and pin it).

### 6c. Android Studio JBR (JetBrains Runtime) — bundled, **UNVERIFIED version**

<https://developer.android.com/build/jdks> (VERIFIED): JBR is the enhanced JDK **distributed with
Android Studio**; Studio startup searches `STUDIO_JDK` → `studio.jdk/` → `jbr/` (recommended) →
`JDK_HOME` → `JAVA_HOME` → `java` on PATH; JBR is recommended to run Studio and is the default
Gradle JDK for new projects (`GRADLE_LOCAL_JAVA_HOME`). The **exact JBR build bundled in
Studio 2026.2.2.11 is not published on developer.android.com** (checked Studio/install and
`build/jdks` pages) → **UNVERIFIED**. Do not hard-code a JBR version; read it at runtime from
`<studio>/jbr/release` (`JAVA_VERSION`) if Studio is present, and never require JBR when Studio is
skipped. No redistribution of JBR separate from Studio.

**JDK implications for the installer:** `sdkmanager`/`avdmanager` require a JVM; `emulator`/`adb`
do not. Install a pinned Temurin JDK **before** cmdline-tools steps and set `JAVA_HOME`; set
`STUDIO_JDK` never (per Android docs, leave JBR in charge of Studio). JDK 21 is the conservative pin;
support 25 as an opt-in.

---

## 7. Node.js LTS

**Current Active LTS: Node.js 26.13.1 ("Lithium")** — **VERIFIED** from
<https://nodejs.org/dist/index.json> and <https://raw.githubusercontent.com/nodejs/Release/main/schedule.json>
(2026-11-26). Schedule: v26 released 2026-05-05, LTS start **2026-10-28**, maintenance 2027-10-20,
EOL 2029-04-30. Other lines: **v24.22.3 "Krypton"** (maintenance since 2026-10-20, EOL 2028-04-30);
**v22.23.5 "Jod"** (maintenance since 2025-10-21, EOL 2027-04-30); v20 EOL 2026-04-30; v27 is alpha
(2027-04-22). Note Appium 4 requires Node **≥ 22.22** (see §8) — v22/v24/v26 all satisfy it, but
v26 is the only Active LTS.

### Downloads + SHA-256 (VERIFIED, `https://nodejs.org/dist/v26.13.1/SHASUMS256.txt`)

| Artifact | SHA-256 |
|---|---|
| `node-v26.13.1-win-x64.zip` | `e2edda75bfb089654705cf65a13b58933bfd5f57148e8ebda916f58a2474f1f7` |
| `node-v26.13.1-win-arm64.zip` | `181ebe431b90787113a9e4fc80d38ce86d37888a353a0386de44d2872e48cb97` |
| `node-v26.13.1-darwin-arm64.tar.gz` | `aec2514ea1ceb2ba52455e9ec65986f6749a06dd759e5db2a72e6889668c9735` |
| `node-v26.13.1-darwin-x64.tar.gz` | `82bb96078a24ca50b9c9a38f2379e6be6d87171fcd400fc14c8934b3bce96e2f` |
| `node-v26.13.1-linux-x64.tar.xz` | `5c063e46bab0fbc1c17cc445d7ff129cbdcd0471be6ed83b6ab864f8143bc864` |
| `node-v26.13.1-linux-arm64.tar.xz` | `89e1853138db51e8b82d3e98ba9c0d3f2956a28e5e55ea1b7e12d25aae92d5dd` |

Base URL: `https://nodejs.org/dist/v26.13.1/<artifact>`. Checksum file itself:
<https://nodejs.org/dist/v26.13.1/SHASUMS256.txt> (also signed `SHASUMS256.txt.asc`).
Package IDs: winget **`OpenJS.NodeJS` = 26.12.1** (**STALE**, VERIFIED local `winget search`;
`OpenJS.NodeJS.20/18/...` also exist); brew formula **`node` = 26.13.1** (**VERIFIED**,
<https://formulae.brew.sh/api/formula/node.json>); Arch `extra/nodejs 26.13.1` (**VERIFIED**);
apt/dnf: `nodejs` (distro versions lag heavily; **UNVERIFIED** here). License: MIT for the Node core
(bundles third-party deps under their own licences) — redistributable.

---

## 8. Appium 4.x + UiAutomator2 driver

**Appium 4 is out.** **`appium@4.0.1`**, published **2026-11-23**; the "Appium 4" announcement blog is
dated 2026-11-15 — **VERIFIED** from
<https://appium.io/docs/en/latest/blog/2026/11/15/-appium-4/> and
<https://api.github.com/repos/appium/appium/releases/latest> (tag `appium@4.0.1`).

Breaking changes relevant to us (**VERIFIED**, blog + release notes):
- **Minimum Node.js is 22.22** (npm `engines`: `node: ^22.22.2 || ^24.15.0 || >=26.0.0`,
  `npm: >=10`). This is why Node 22 LTS is the *floor*, not the target.
- **All Appium packages are ESM-only.** Any Appium **JS** script we generate (BRIEF §3.6
  `appium_js`) must be ESM (`import`/`export`, `.mjs` or `"type":"module"`); `require()` no longer
  works for Appium packages.
- The Windows driver is replaced by the **WinCore** driver (irrelevant for us, but do not install
  `appium-windows-driver`).
- Some deprecated endpoints removed; **the CORS-enabling server flag was renamed**; new server flag
  controls allowed app URLs in capabilities (e.g. `appium:app`).
- `appium driver install/update` now handles extension server-version-range incompatibilities
  gracefully.

**Registry data (VERIFIED, npm registry):**

| Package | Version (2026-11-26) | Tarball | Integrity (SHA-512, npm) |
|---|---|---|---|
| `appium` | **4.0.1** | <https://registry.npmjs.org/appium/-/appium-4.0.1.tgz> | `sha512-0xVTeYmTm6h37Cq7nqYjpurBpXTE6XgAFBKaVAo/SY3H9qeADF2pCpZNR0ClJjmlmNZjeiQ7nbK9EHC62JqyXw==` |
| `appium-uiautomator2-driver` | **10.4.0** (2026-11-25) | <https://registry.npmjs.org/appium-uiautomator2-driver/-/appium-uiautomator2-driver-10.4.0.tgz> | `sha512-Y8W9JZ4ecSZpLqarlLydhLYCITYfXJ3rnJnUtwbpNSKv9ItXp2sjxgcQyUXJB/3UEh72U63ZHW9KY84JqqwmZQ==` |

- UiAutomator2 driver 10.x declares `peerDependencies: {"appium": "^4.0.0-beta.8"}` (**VERIFIED**) —
  i.e. **uiautomator2 10.x requires Appium 4**; it is not compatible with an Appium 3 server.
- **SHA-256: UNVERIFIED for both** — npm publishes SHA-512 integrity + SHA-1 shasum only. Pin exact
  versions (`appium@4.0.1`, `appium-uiautomator2-driver@10.4.0`) and let npm verify its own
  integrity; if the product needs SHA-256, record the computed digest of the installed tree/tarball
  in the install manifest.
- Package IDs: npm `appium`, `appium-uiautomator2-driver`. No winget/brew/apt packages for the Appium
  server itself (the winget `JSFoundation.Appium` 1.22.3 is the **discontinued Appium Server GUI** —
  do not use). Install via `npm i -g appium@4.0.1` then `appium driver install uiautomator2@10.4.0`.
- License: **Apache-2.0** for appium and the UiAutomator2 driver (VERIFIED via GitHub repo metadata).
- Config implication: `Config.appium_version="4"` is right; `uiautomator2_version="latest"`
  (**BRIEF/config**) must be pinned to `10.4.0` for reproducible fleets.

---

## 9. Appium Inspector

**Current: 2026.11.6, released 2026-11-13** — **VERIFIED** from
<https://api.github.com/repos/appium/appium-inspector/releases/latest>. Electron desktop app +
Appium plugin; **Apache-2.0** (VERIFIED via GitHub repo metadata).

Release highlights (VERIFIED): "no functional changes, only platform compatibility"; the **Windows
desktop app is now signed** and should no longer be blocked by Windows Defender; macOS notarization
fixed since 2026.11.5; **the plugin version is now compatible with both Appium 3 and Appium 4
installations**. Note from the release body: **macOS artifacts are from 2026.11.5** due to a
publishing error (functionally identical).

Artifacts (VERIFIED asset list; under `https://github.com/appium/appium-inspector/releases/download/v2026.11.6/`):
`Appium-Inspector-2026.11.6-win-x64.exe` (145,108,792 B), `-win.exe` (283,664,328 B, universal),
`-win-arm64.exe` (139,214,448 B), `-win-x64.zip`/`-win-arm64.zip`; `-linux-x86_64.AppImage`,
`-linux-arm64.AppImage`, `-linux-x64.tar.gz`, `-linux-arm64.tar.gz`; macOS `-mac-arm64.dmg|.zip`,
`-mac-x64.dmg|.zip` (files named `2026.11.5`).

- **SHA-256: UNVERIFIED.** Electron publishes **SHA-512** per file in `latest.yml`
  (<https://github.com/appium/appium-inspector/releases/download/v2026.11.6/latest.yml>); example —
  `Appium-Inspector-2026.11.6-win-x64.exe` `sha512: jbP6SFO7A5yBWkdXYVWVRQQjWbNIGFBdz6zioTzpJYslp0PDHjsYq/M35C4PsxQW16cZARJjO11Su5Z/i6f2Wg==`
  (`latest.yml` also carries `latest-linux*.yml` / `latest-mac.yml`). If the product requires SHA-256,
  compute and record it once.
- Package IDs: winget **`AppiumDevelopers.AppiumInspector` = 2026.11.6** (**VERIFIED**, local
  `winget search`); brew cask **UNVERIFIED**; apt/dnf/pacman **UNVERIFIED** (Electron app; not
  commonly packaged).
- **Headless caveat:** Inspector is a GUI Electron app. It cannot be used or smoke-tested on a
  headless server; installer step must be optional/skippable and must not be part of `doctor` green
  criteria on servers. (BRIEF §3.2 lists it as part of install — keep it opt-in.)

---

## 10. scrcpy

**Current: 5.0.1, released 2026-10-08** — **VERIFIED** from
<https://api.github.com/repos/Genymobile/scrcpy/releases/latest> (tag `v5.0.1`). **Apache-2.0**
(VERIFIED via GitHub repo metadata). v5.0.1 changes: accept `DRM_FORMAT_RG88` for NV12 chroma plane;
fix H.265 hardware decoding on Windows; use renderer GPU for VA-API decoding; force EGL only if
VA-API enabled.

### Downloads + official SHA-256 (`SHA256SUMS.txt` + `.asc`, VERIFIED)

| Asset | SHA-256 |
|---|---|
| `scrcpy-win64-v5.0.1.zip` | `b12a2c4ee8be317422451fc7dcf8ee20a71b5ea7ef9ad73ddd825a25316227a5` |
| `scrcpy-win32-v5.0.1.zip` | `0cb65ebf1bce892fa4d1502d699af0c7a1a7f24d7febcb9ef72894e3f23fc2ad` |
| `scrcpy-winarm64-v5.0.1.zip` | `16c3fc2068df64946f670e3504b4752073ebae47a416a45b46e79a5ab19c689b` |
| `scrcpy-macos-aarch64-v5.0.1.tar.gz` | `33611e51977a8289e2e124b220f727895181ef2eb9060e0987a4dc2db9cf0d6b` |
| `scrcpy-macos-x86_64-v5.0.1.tar.gz` | `31a5467a9e907f162b093267cbbab1276b15c6e709c2ea8159566a8d165beb50` |
| `scrcpy-linux-x86_64-v5.0.1.tar.gz` | `9f969d30cc574816077edecda65719c1c70b0aee1df1ba5fa00779ac07b8fd6e` |
| `scrcpy-server-v5.0.1` | `764eb6f79811d5211fe9df341120882ba9994c7a61b897d7bf3fb662e53bc536` |

Base: `https://github.com/Genymobile/scrcpy/releases/download/v5.0.1/<asset>`; checksum file
<https://github.com/Genymobile/scrcpy/releases/download/v5.0.1/SHA256SUMS.txt> (+ `.asc`).
Package IDs: winget **`Genymobile.scrcpy` = 5.0.1** (VERIFIED, local `winget search`); brew formula
**`scrcpy` = 5.0.1** (VERIFIED, <https://formulae.brew.sh/api/formula/scrcpy.json>); Arch
**`extra/scrcpy 5.0.1`** (VERIFIED); Debian 14/unstable `4.1`, Ubuntu 26.10 `4.1`, Ubuntu 26.04
`3.3.4` (**STALE**, VERIFIED via Repology) — do **not** rely on apt for the pinned version.
**Flathub: not found** under the plausible IDs checked (`com.genymobile.scrcpy`,
`org.genymobile.scrcpy`, `io.github.genymobile.scrcpy` → 404 on 2026-11-26) → **UNVERIFIED/no
official Flatpak**; do not plan on one.
Note: there is **no linux-arm64 prebuilt** asset for 5.0.1 (x86_64 only); ARM Linux must build from
source. Used only as a fallback display path (BRIEF §3.8).

---

## 11. uv

**Current: 0.13.10, released 2026-11-24** — **VERIFIED** from
<https://api.github.com/repos/astral-sh/uv/releases/latest> (tag `0.13.10`; release notes: fixes
GHSA-vjvj-3m3f-fgmm). **Dual MIT OR Apache-2.0** (GitHub reports Apache-2.0). `requires_python >=3.8`
(VERIFIED via PyPI). Docs: <https://docs.astral.sh/uv/>.

### Downloads + official SHA-256 (`sha256.sum` + per-asset `.sha256`, VERIFIED)

| Asset (GitHub release `0.13.10`) | SHA-256 |
|---|---|
| `uv-x86_64-pc-windows-msvc.zip` | `297b3881fe1adfb922becc2c1613202abb41dc83ea5ff43d472aa087ca1f8683` |
| `uv-aarch64-pc-windows-msvc.zip` | `f223bcd3fa010174ffeeb885cc1d5334bb81388640c65baa5b8b42b47017337e` |
| `uv-aarch64-apple-darwin.tar.gz` | `e419bf07533bdbf48795fc793601c834dd79db57a8c49ce8e25cfe3a8639958d` |
| `uv-x86_64-apple-darwin.tar.gz` | `153e03197588204c04fc4c81cc4bafbfa4280222a385e4ee36a87aeec0ae63b4` |
| `uv-x86_64-unknown-linux-gnu.tar.gz` | `bf65ac1a0af10f92c522d91ac096c7c0a39657647b155577e7836c2cbbb4526c` |
| `uv-aarch64-unknown-linux-gnu.tar.gz` | `14590f644805a6234ffe071154340669e38137f38e60aa9629690119704107d2` |
| `uv-x86_64-unknown-linux-musl.tar.gz` | `5597970380083a340b7983af05a84824b4db1c62b7059c796935b7fd6dc95fd2` |

Base: `https://github.com/astral-sh/uv/releases/download/0.13.10/<asset>`; checksum file
<https://github.com/astral-sh/uv/releases/download/0.13.10/sha256.sum>; GitHub-hosted per-asset
`.sha256` files too. PyPI wheels/sdist also carry SHA-256 (`https://pypi.org/pypi/uv/json`,
`digests.sha256`), e.g. `uv-0.13.10.tar.gz`.
Package IDs (**VERIFIED**): winget **`astral-sh.uv` = 0.13.10** (ReleaseDate 2026-11-24, zip/portable,
per-arch SHA-256 in the manifest, e.g. x64 `297B3881FE1ADFB922BECC2C1613202ABB41DC83EA5FF43D472AA087CA1F8683`);
brew formula **`uv` = 0.13.10**; Arch **`extra/uv 0.13.10`**; Fedora rawhide `uv 0.13.10` (older
Fedora 39–45 lag 0.4–0.13), Debian 14/unstable `uv 0.9.17` (**STALE**). Official installer script
(standalone) and `pip install uv` exist; the standalone script is the usual bootstrap path. Because
the portable edition needs a pinned Python runtime too, pin both uv and the uv-managed CPython
(`uv python install <pin>`) in the bundle manifest.

---

## 12. sdkmanager package IDs that the installer will need (VERIFIED from `repository2-3.xml`)

| Package ID | Revision (2026-11-26) | Channel | Notes |
|---|---|---|---|
| `cmdline-tools;latest` | 23.0 (build 16111833) | channel-0 stable | mutable alias — pin `cmdline-tools;23.0` or `;22.0` |
| `cmdline-tools;22.0` | 22.0 (build 15859902) | channel-0 stable | the revision with published SHA-256 on the web page |
| `platform-tools` | 37.0.3 | channel-0 stable | SHA-1 in index only |
| `emulator` | 37.3.10 (build 16970787) | channel-0 stable | SHA-1 in index only |
| `build-tools;…` / `platforms;android-…` | versions track API levels (API 37.x exists in the index) | channel-0 | owned by the image catalogue (G1x), not R1 |
| `system-images;…` | per API/tag/ABI | channel-0 | catalogue/download is G1x + R3; licences apply |

`sdkmanager --channel=0|1|2|3` = stable/beta/dev/canary. `--sdk_root=<path>` is required for the
portable/relocatable SDK. The fakes contract (`contracts/fakes-spec.md` §4.1) mirrors this.

---

## 13. Licence / SDK-licence implications (read before building the portable bundle)

1. **Android SDK License Agreement** (current text dated **2026-04-28** on developer.android.com; the
   `android-sdk-license` text inside `repository2-3.xml` is the older 2019-01-16 revision that
   sdkmanager still uses). Key clause (§3.4): you may not copy (except backup), modify, adapt,
   **redistribute**, decompile, reverse-engineer or create derivative works of the SDK except as
   third-party licences require. §3.2 limits use to developing apps for compatible Android
   implementations; §14.5 adds US export restrictions.
2. **Consequence for BRIEF §3.10 (portable edition):** bundling Google's cmdline-tools,
   platform-tools, emulator, Google APIs/Play system images, Studio or JBR **inside a distributed
   bundle is not permitted by the SDK licence**. The portable edition must either (a) download those
   components on first run (with checksums, accepting licences with the user's explicit consent), or
   (b) ship only user-supplied/OSS components (uv, Python, Node, Appium, scrcpy, the avdfleet code).
   This is a normative change request to BRIEF §3.10/§7.7 (record in `PLAN.md` → Decisions).
3. **`--accept-android-sdk-licenses`** should map to `sdkmanager --licenses` accepting
   `android-sdk-license` (+ `android-sdk-preview-license` when preview channels are used). It cannot
   accept Google APIs/Play image terms on the user's behalf beyond the SDK licence; the product must
   surface those terms.
4. **OSS components are redistributable:** uv (MIT/Apache-2.0), Node core (MIT), Appium/Inspector/
   UiAutomator2/scrcpy (Apache-2.0), Temurin/Microsoft OpenJDK (GPLv2+CE). Keep their `LICENSE`/
   `NOTICE` files in the bundle (Apache-2.0 requires NOTICE propagation).
5. **Android CLI license UNVERIFIED** — do not bundle it; treat it as an optional, downloaded-at-
   install component with metrics opt-out (`--no-metrics`).
6. **Appium Inspector** is a GUI app: never required for the daemon, never bundled-on-a-server, and
   its presence must not gate `avdfleet doctor`.

---

## 14. Where BRIEF.md / frozen core appears wrong or under-specified (R1 findings)

| # | BRIEF / core statement | Finding (2026-11-26) | Proposed change |
|---|---|---|---|
| R1-1 | §3.2/§5 R1 implicitly assume a JDK/JBR; no Node pin stated | Node **26.13.1 is Active LTS**; Node 22 is **maintenance** (EOL 2027-04-30) and only the Appium 4 floor | Pin Node **26.x** (or 24.x) in `Config.node_version`; keep 22 as absolute minimum only |
| R1-2 | `Config.uiautomator2_version = "latest"` | Unpinned; and 10.x now requires Appium ≥4 (`peerDependencies ^4.0.0-beta.8`) | Pin `10.4.0` (and Appium `4.0.1`) |
| R1-3 | §3.2 install list: "Android command-line tools, platform-tools, emulator" | There is now a **new first-party Android CLI** (`android`, 1.1.16919734) overlapping sdkmanager/avdmanager | Install it only optionally; S1a backend must degrade to legacy; never `curl \| bash` it (unpinned, no checksum) |
| R1-4 | §3.2 "AEHD → WHPX migration" | **AEHD sunsets 2026-12-31**; GTK/WHPX is the only supported Windows hypervisor path after that | Default to WHPX; do not install AEHD unless explicitly requested; remove after 2026-12-31 |
| R1-5 | §3.10 portable bundle contains "Android cmdline-tools/platform-tools/emulator, Appium + driver, UI build …" | **SDK licence forbids redistribution** of the Google components; **Android Studio/JBR** too | Change portable to download-at-first-run for Google components; bundle only OSS; document licence acceptance |
| R1-6 | §3.2 "install … Appium Inspector" as part of unattended install | Inspector is a **GUI Electron app**, newly signed, plugin supports Appium 3 & 4; cannot run headless | Make it opt-in/skippable; exclude from `doctor` green on servers |
| R1-7 | No pinned versions anywhere | Package managers lag upstream: winget Studio `.10` vs `.11`, winget platform-tools 37.0.1 vs 37.0.3, brew cmdline-tools 15859902 vs repo 16111833, winget Node 26.12.1 vs 26.13.1 | Installer must use **official version-pinned URLs + published SHA-256** where available, not `winget`/`brew` "latest" |
| R1-8 | §3.2 "checksum verification where upstream publishes one" | Emulator has **no published SHA-256** (SHA-1 only); Appium/Inspector have no SHA-256 (SHA-512 only); Android CLI publishes SHA-256 only via winget | Add fallback: verify publisher SHA-1/SHA-512 where SHA-256 absent, or record computed digest in install manifest; never claim SHA-256 for these |
| R1-9 | §3.2 mention of cmdline-tools without revision | Two "current" builds: web zip 22.0/15859902 (SHA-256) vs sdkmanager `latest` 23.0/16111833 (SHA-1) | Pin an explicit revision; document the choice; prefer 22.0 if SHA-256 is mandatory, else 23.0 with SHA-1 pin |
| R1-10 | `Config.jdk_version = "21"` | 21 is still an LTS and fine; **25 is the current LTS** (Temurin 25.0.5.1 / MS OpenJDK 25.0.5.1); Studio JBR version unpublished | Keep 21 as default pin (supports all current tooling) and document 25 as opt-in; read JBR from `<studio>/jbr/release`, never hard-code |
| R1-11 | §3.6 `appium_js` script type | **Appium 4 is ESM-only** — CommonJS `require()` of Appium packages no longer works | JS script templates must be ESM (`.mjs` / `"type":"module"`) |
| R1-12 | §3.3 images/catalogue assumes `sha256` field always available | Google's index publishes SHA-1; system-image zips are per-tag/API and the SHA-256 story is catalogue-specific (G1x/R6) | `SystemImage.sha256` should be optional and paired with a `checksum_type`; do not fabricate |

---

## 15. Sources (all accessed 2026-11-26; first-party unless marked)

Android / Google
- <https://developer.android.com/studio> — Android Studio + command-line tools download tables with SHA-256s
- <https://developer.android.com/studio/releases> — Studio stable release notes/download
- <https://developer.android.com/studio/install> — Studio system requirements, platform limits, Linux libs
- <https://developer.android.com/studio/releases/emulator> — emulator release notes (37.3.10, AEHD sunset, API 37 RAM, GPU deprecations)
- <https://developer.android.com/studio/emulator_archive> — emulator archive (manual install; no static checksums)
- <https://developer.android.com/tools/releases/platform-tools> — platform-tools 37.0.3 release notes
- <https://developer.android.com/tools/releases/cmdline-tools> — cmdline-tools channels/revisions
- <https://developer.android.com/tools/sdkmanager> — sdkmanager docs
- <https://developer.android.com/build/jdks> — JBR and JDK selection for Android builds
- <https://developer.android.com/tools/agents/android-cli> and `/download` — Android CLI overview + installers
- <https://dl.google.com/android/repository/repository2-3.xml> — sdkmanager package index (IDs, revisions, SHA-1s)
- <https://dl.google.com/android/cli/latest/linux_x86_64/install.sh> — Android CLI installer script (no verification)

Third-party / package-manager sources
- <https://api.adoptium.net/v3/info/available_releases>, `.../v3/assets/latest/25/hotspot`, `.../v3/assets/latest/21/hotspot` — Temurin versions + SHA-256s
- <https://adoptium.net/de/news?tag=release-notes> — Temurin release announcements
- <https://learn.microsoft.com/en-us/java/openjdk/download> — Microsoft Build of OpenJDK 25.0.5.1 / 21.0.13.1 + `.sha256sum.txt`
- <https://nodejs.org/dist/index.json>, <https://nodejs.org/dist/v26.13.1/SHASUMS256.txt>, <https://raw.githubusercontent.com/nodejs/Release/main/schedule.json>
- <https://appium.io/docs/en/latest/blog/2026/11/15/-appium-4/> — Appium 4 breaking changes
- <https://api.github.com/repos/appium/appium/releases/latest>, `.../appium-uiautomator2-driver/releases/latest`, `.../appium-inspector/releases/latest`
- <https://registry.npmjs.org/appium/latest>, <https://registry.npmjs.org/appium-uiautomator2-driver/latest>
- <https://api.github.com/repos/Genymobile/scrcpy/releases/latest>` + `SHA256SUMS.txt`
- <https://api.github.com/repos/astral-sh/uv/releases/latest>` + `sha256.sum`, <https://pypi.org/pypi/uv/json>, <https://docs.astral.sh/uv/>
- <https://formulae.brew.sh/api/cask/android-studio.json>, `.../cask/android-commandlinetools.json`, `.../cask/android-platform-tools.json`, `.../formula/{scrcpy,uv,node}.json`
- <https://flathub.org/api/v2/appstream/com.google.AndroidStudio> (Flathub Studio 2026.2.2.11); Flathub scrcpy probe returned 404 for the plausible IDs
- winget-pkgs manifests: `manifests/g/Google/AndroidCLI/1.1.16919734/`, `manifests/g/Google/PlatformTools/37.0.3/`, `manifests/a/astral-sh/uv/0.13.10/`; local `winget search` v1.29.380 on this host
- <https://archlinux.org/packages/search/json/?q=...> (arch package versions), <https://repology.org/api/v1/project/{scrcpy,uv,nodejs,openjdk}> (Debian/Ubuntu/Fedora versions)
- JetBrains Android Studio releases list (build ids): <https://plugins.jetbrains.com/docs/intellij/android-studio-releases-list.html>

**Not verified / gaps**
- Android Studio JBR exact build for 2026.2.2.11; emulator/platform-tools SHA-256 on macOS/Linux; Android CLI licence and macOS/Linux checksums; Appium/Inspector/UiAutomator2 SHA-256 (publishers use SHA-512); apt/dnf package IDs for Google SDK components (no official packages); Fedora `java-25-openjdk` name (inferred from Fedora `java-*-openjdk` convention); Temurin 25 winget version string.
