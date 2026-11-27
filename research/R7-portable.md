# R7 — Portable edition: verified facts, what cannot be portable, projected leftovers

Status: Wave 0.5 research · Owner: R7 research agent · Accessed 2026-11-26 (Europe/Dublin)
Applies to BRIEF.md §3.10 (R-PORTABLE), §7 item 7, PLAN.md decision 5, and Wave-1 agents
PT1 (`portable/env`, `launchers`), PT2 (`portable/checks`), PT3 (`portable/relocate`),
PT4 (`portable/bundle`), PT5 (`portable/studio`), T5 (`tests/portable`).

Labels: **VERIFIED** = checked in a primary source at the URL in §10; **PARTIAL** =
corroborated/inferred but not fully evidenced; **UNVERIFIED** = cannot be confirmed here,
must be re-tested on a real host. This host is a virtualised Windows box with no
hypervisor and no Android SDK (PLAN.md "Host constraint"), so **no boot, mount or
portable-run evidence was produced** — every empirical acceptance item stays open.

---

## 1. §3.10 claim-by-claim verification

| # | §3.10 claim | Status | Evidence / correction |
|---|---|---|---|
| 1 | Bundle contains a pinned Python runtime, Android cmdline-tools/platform-tools/emulator, Appium + driver, UI build, default config, launchers, checksums and an offline cache | **VERIFIED (mechanical)** | All components are downloadable per-OS archives with known package ids (R1); layout and packaging are ours. **Correction/caveat:** Google system images may **not** be redistributed in a bundle — treat the offline cache as *user-provided* only, or download-on-first-run. Appium driver installs and chromedriver caches are pulled from npm/network (see #2). |
| 2 | `--offline` means no network needed on first run | **PARTIAL** | Achievable for: interpreter start, daemon start, GUI load, `emulator` boot to OOBE **if** the system image is already inside the bundle (and redistribution allows it), `adb`, scripts that only touch the guest. **Not** achievable for: Play Store sign-in / Play app updates / any web content (obviously), Appium sessions that trigger chromedriver acquisition, `sdkmanager` operations, Studio plugin/licence flows, emulator update checks (harmless but may log). The bundle must pre-seed Appium's extension/cache dirs and disable update checks. |
| 3 | Launchers construct the environment from the bundle root, unblock files on Windows first run, detect root changes | **VERIFIED mechanism** | Env construction is fully under our control. Windows: `Unblock-File` removes the `Zone.Identifier` ADS (VERIFIED), and is a no-op on unblocked files; PowerShell 3.0+; works only on filesystem drives; can be applied with `Get-ChildItem -Recurse -File | Unblock-File`. Root-change detection: store `root` + a marker hash in the bundle manifest and compare at launch (design). |
| 4 | Preflight checks: filesystem type (refuse FAT32, warn exFAT), noexec mount detection, quarantine/Mark-of-the-Web, free space, write speed, hypervisor availability | **PARTIAL (VERIFIED inputs)** | FAT32 max file size is **4 GiB** (VERIFIED Microsoft table) — an AVD data partition (typically 6–8 GB) and large userdata images cannot fit; refuse FAT32 as BRIEF says. exFAT supports 2^64−1-byte files but tracks no file owner/permissions (VERIFIED table) and its Linux driver exposes uid/gid/umask mount options only (VERIFIED) → warn and degrade. `noexec` = "Do not permit direct execution of any binaries on the mounted filesystem" (VERIFIED mount(8)). MOTW/Zone.Identifier + quarantine mechanisms VERIFIED; exact macOS xattr name PARTIAL (§7). Hypervisor availability must be *detected*, never provided (see §8). |
| 5 | Relocation: rewrite AVD `.ini` paths, sysdir handling, rebuild venv if needed, invalidate stale snapshots, verify | **PARTIAL** | AVD `.ini` holds an absolute `path=` into the avd folder; `avdmanager move avd -n <name> -p <path>` exists specifically to rewrite it (VERIFIED docs) — our relocator must do the equivalent for every AVD, or avoid absolute paths entirely. `image.sysdir.1` is normally relative to the SDK root and survives relocation (PARTIAL: emulator resolves images under `<sdk>/system-images`, AVD config key not publicly documented). "Rebuild venv if needed": **design decision — do not ship a venv at all** (Python venvs are "inherently non-portable", VERIFIED; see §2). Snapshots contain absolute/run-specific state → invalidate on relocation (PARTIAL: no doc statement; conservative behaviour is to delete/ignore snapshots after a root change). |
| 6 | Studio portable config via `STUDIO_PROPERTIES`; document leftovers; `portable verify` compares a filesystem snapshot and fails on undocumented writes | **PARTIAL** | `STUDIO_PROPERTIES` "sets the name and location of the `idea.properties` file" (VERIFIED). It is a **file path**, not a directory. Inside it, `idea.system.path`, `idea.plugins.path` (and `idea.config.path`, `idea.log.path`) can redirect IDE dirs (VERIFIED for system/plugins examples in Android docs; config/log are IntelliJ platform properties — PARTIAL). Studio still writes outside: JVM crash logs, `.android`/adb keys, Gradle caches, project dirs, licence/first-run state — the leftovers list (§9) is therefore non-empty and must be documented, exactly as BRIEF requires. `portable verify` design: hash-manifest + snapshot diff of a documented watchlist (user profile dirs/registry/temp), not a full disk diff. |
| 7 | "emulator's `~/.android` only when unavoidable" | **PARTIAL** | We can force nearly everything under the bundle root: `ANDROID_USER_HOME`, `ANDROID_EMULATOR_HOME`, `ANDROID_AVD_HOME`, `ANDROID_HOME`/`ANDROID_SDK_ROOT` (all VERIFIED, §3). Caveat: "Some older tools, such as Android Studio 4.3 and earlier, do not read `ANDROID_USER_HOME`" (VERIFIED docs) and legacy tools use `ANDROID_SDK_HOME`; also `adb` keys and console/GUI integrations may still touch `~/.android` if any code path forgets the env. Project leftovers: `~/.android` **only if unavoidable**, list it as documented. |
| 8 | Filesystem diff shows no writes outside root except documented leftovers | **PARTIAL (test design)** | Achievable only with (a) full env redirection, (b) `HOME`/`USERPROFILE` **not** redirected (don't break the OS), (c) Studio/other-app leftovers documented, (d) registry writes enumerated on Windows. Cannot be proven on this host; T5 must assert against the documented watchlist. |

---

## 2. Python runtime: uv layout, relocation, embedding

### 2.1 uv storage layout (VERIFIED, uv docs)

<https://docs.astral.sh/uv/reference/storage/> (accessed 2026-11-26). uv resolves, in order
of environment variable then default:

| Data | Env override | Unix default | Windows default |
|---|---|---|---|
| cache | `UV_CACHE_DIR` (documented under cache) | `$XDG_CACHE_HOME/uv` → `$HOME/.cache/uv` | `%LOCALAPPDATA%\uv\cache` |
| persistent data (managed Pythons, tools) | `UV_PYTHON_INSTALL_DIR`, `UV_TOOL_DIR` | `$XDG_DATA_HOME/uv` → `$HOME/.local/share/uv` | `%APPDATA%\uv\data` |
| executables (python shims, tool shims) | `UV_PYTHON_BIN_DIR`, `UV_TOOL_BIN_DIR`, `UV_INSTALL_DIR` | `$XDG_BIN_HOME` → `$XDG_DATA_HOME/../bin` → `$HOME/.local/bin` | `%XDG_BIN_HOME%`/`%XDG_DATA_HOME%\..\bin`/`%USERPROFILE%\.local\bin` |
| config | (standard uv config dirs) | `$XDG_CONFIG_HOME/uv` → `$HOME/.config/uv` | `%APPDATA%\uv` |
| temporary | `TMPDIR`/`TMP`/`TEMP` | `/tmp` | `%TMP%`/`%TEMP%`/`%USERPROFILE%` |

Two operational requirements called out by uv docs: for performance the **cache must be on
the same filesystem as virtual environments**, and changing the Python install directory
"will not be automatically reflected in existing virtual environments; they will keep
referring to the old location, and will need to be updated manually (e.g. by re-creating
them)."

**Portable instruction:** set `UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR`, `UV_TOOL_DIR`,
`UV_TOOL_BIN_DIR`, and `TMPDIR`/`TMP`/`TEMP` under the bundle root for the lifetime of every
launcher-spawned process; never let `uv` write to `~/.local/share/uv` or `%APPDATA%\uv`.

### 2.2 uv-managed Python = python-build-standalone, with relocation caveats

- uv's managed CPythons are Astral `python-build-standalone` builds, "self-contained,
  highly-portable, and performant" (VERIFIED uv Python-versions doc).
- python-build-standalone records **build-time absolute paths** in
  `lib/python*/_sysconfigdata_*.py`, `lib/python*/config-*/Makefile` and its `PYTHON.json`
  metadata; the project's quirks page notes these "are fixed up to point to the actual
  location on your system where the distribution was installed, so this quirk generally
  does not affect uv users" (VERIFIED) — i.e. **uv relocates at install time**, and a
  directory moved *after* installation has stale paths until uv reinstalls. Also on
  Windows the project ships no `Scripts/pip.exe` (VERIFIED quirks page) — use
  `python -m pip`/`uv pip`.
- uv uses a **minor-version directory** (`cpython-3.12-<os>-<arch>-none`) that is a
  **symlink on Unix or a junction on Windows** to a specific patch version (VERIFIED uv
  docs); `windows junction` targets are absolute — a bundle moved to another drive breaks
  the junction until recreated. **PARTIAL** for the breakage claim, VERIFIED for the
  junction/symlink mechanism.
- On Windows, `uv python install` registers Pythons in the **Windows registry** per PEP 514
  (VERIFIED uv docs) — that is a write outside the bundle root; avoid it (do not run
  `uv python install` at runtime; ship the runtime directory and call it directly), or
  unregister on uninstall.
- Linux builds are glibc-based; musl/static-only systems are a support question — treat as
  **UNVERIFIED** (uv platform policy owns this; note for R1/R2).

### 2.3 Virtual environments are not portable (VERIFIED, Python docs)

Python's own `venv` docs: launcher scripts in a virtual environment "have their shebang
lines contain the absolute paths to their environment's interpreters. Because of this,
environments are inherently non-portable, in the general case."
<https://docs.python.org/3/library/venv.html>

**Design ruling for avdfleet portable:** do **not** ship a venv. Ship either

1. **Primary (all OSes):** a self-contained Python distribution directory under
   `<root>/runtime/python/` (python-build-standalone, as shipped by uv's cache) plus the
   application's dependencies installed with
   `uv pip install --target <root>/runtime/app-site-packages` (a plain directory, no
   shebangs, no bin shims), and a launcher that runs
   `<root>/runtime/python/python -m avdfleet ...` with the site-packages directory added to
   `sys.path` (a `sitecustomize.py`/`usercustomize.py` under the runtime, or `PYTHONPATH`).
   Recreate/refresh on relocation only if a hash check fails; no venv to rebuild.
2. **Windows-only alternative:** the official **embeddable package** is explicitly
   "(almost) fully isolated from the user's system, including environment variables, system
   registry settings, and installed packages", stdlib is zipped, pip is not included, and a
   default `._pth` file restricts `sys.path` (VERIFIED docs.python.org Windows page §4.2).
   Good for a zero-install Windows launcher, but it is a different runtime layout from the
   uv one — choose one per bundle; don't mix.
3. **Embedding (`Py_Initialize`)**: possible per docs.python.org "Embedding Python in
   Another Application", but unnecessary: avdfleet's portable launcher only needs
   subprocesses (`python -m avdfleet serve`) plus the daemon. **PARTIAL** (page exists;
   not needed for design).

If any third-party package insists on console scripts (e.g. `uvicorn`), generate shims at
first run *inside the bundle* (`<root>/bin/`) rather than installing wrapper scripts with
absolute shebangs.

---

## 3. Android tooling: path portability and env vars

### 3.1 Environment variables (VERIFIED, developer.android.com/tools/variables)

Exact documented semantics:

- `ANDROID_HOME` — SDK installation directory. `ANDROID_SDK_ROOT` — "also points to the SDK
  installation directory, is deprecated. If you continue to use it, Android Studio and the
  Android Gradle plugin will check that the old and new variables are consistent."
  → set **both** to the same `<root>/sdk` for maximum compatibility.
- `ANDROID_USER_HOME` — "Sets the path to the user preferences directory for tools that are
  part of the Android SDK. Defaults to `$HOME/.android/`. Some older tools, such as Android
  Studio 4.3 and earlier, do not read `ANDROID_USER_HOME`. To override the user preferences
  location for those older tools, set `ANDROID_SDK_HOME` to the parent directory you would
  like the `.android` directory to be created under." → set `ANDROID_USER_HOME=<root>/home/android`
  **and** `ANDROID_SDK_HOME=<root>/home` (whose `.android` child = same place).
- `ANDROID_EMULATOR_HOME` — "Sets the path to the user-specific emulator configuration
  directory. Defaults to `$ANDROID_USER_HOME`." → set to `<root>/home/android`.
- `ANDROID_AVD_HOME` — "Sets the path to the directory that contains all AVD-specific files…
  The default location is `$ANDROID_EMULATOR_HOME/avd/`." → set to `<root>/avd` (large
  images; the bundle root is usually the USB disk, so this is the correct place).
- AVD lookup order (documented): `$ANDROID_AVD_HOME`, `$ANDROID_USER_HOME/avd/`,
  `$HOME/.android/avd/` — so one missing variable can silently fall back to the real
  `~/.android` (leftover risk + wrong AVD set).
- `STUDIO_VM_OPTIONS` — location of `studio.vmoptions`; `STUDIO_PROPERTIES` — location of
  `idea.properties`; `STUDIO_JDK`, `STUDIO_GRADLE_JDK` — JDK selection (VERIFIED).
- `ANDROID_EMULATOR_USE_SYSTEM_LIBS` — Linux only, use system `libstdc++.so`; not for
  portable (never set it).
- Emulator also reads `ANDROID_LOG_TAGS`, `ANDROID_VERBOSE`, `ANDROID_SERIAL`
  (VERIFIED on the same page and emulator docs).

**Un-documented / legacy (UNVERIFIED, do not rely on):** `ANDROID_PREFS_ROOT` (a legacy
emulator alias for the `.android` prefs root — not present in the current official
environment-variables page) and `EMULATOR_HOME` (the documented variable is
`ANDROID_EMULATOR_HOME`; `EMULATOR_HOME` is not documented). Set `ANDROID_PREFS_ROOT` to the
same `<root>/home/android` **defensively** (harmless), but never depend on it; mark both in
`docs/portable.md` as compatibility aliases.

### 3.2 SDK root / cmdline-tools / emulator relocatability

- `sdkmanager`, `avdmanager` take `--sdk_root=<path>`; emulator finds `system-images/`,
  `platform-tools/`, `emulator/` relative to `ANDROID_SDK_ROOT`/`ANDROID_HOME` and errors
  with the documented message when it cannot (R6 §1.4). **VERIFIED**.
- `cmdline-tools` must live at `<sdk>/cmdline-tools/<version>/` with its
  `source.properties`; the launcher scripts (`sdkmanager`, `avdmanager`) locate the SDK via
  the parent path/env. Moving the whole SDK tree and re-pointing env vars is the supported
  pattern (`--sdk_root` exists precisely for multiple SDK roots) — **PARTIAL** (no single
  Google doc says "SDK root is relocatable"; every observed mechanism is path-relative or
  env-driven).
- Java: `sdkmanager`/`avdmanager` are Java tools and need a JDK/JRE. A portable bundle must
  ship one (e.g. a redistributable Temurin JRE) and set `JAVA_HOME`, or rely on Studio's
  JBR if Studio is present. **PARTIAL** for the exact minimum Java version (R1 owns
  versions/licences; BRIEF §3.2 installs JDK/JBR).
- `platform-tools` (`adb`) and `emulator` are per-OS zips with the same relative layout; no
  install step, so bundling + env is enough. **PARTIAL** (layout verified by R1 download
  ids; relocation itself is path-relative).

### 3.3 AVD `.ini` / `config.ini` absolute paths (the real relocation hazard)

- `<ANDROID_AVD_HOME>/<name>.ini` contains an **absolute** `path=` to `<name>.avd`
  (plus `path.rel`, `target=android-<api>`, `avd.ini.encoding=UTF-8`); `avdmanager move avd
  -n <name> -p <path>` exists to move an AVD and rewrite that file (VERIFIED docs).
  → on drive-letter change every `.ini` `path=` must be rewritten; the `avdmanager move`
  command is the documented way to do it (or rewrite the key ourselves, then verify with
  `emulator -list-avds`/`avdmanager list avd`).
- `<name>.avd/config.ini` holds `image.sysdir.1` (normally a **relative** path such as
  `system-images/android-35/google_apis/x86_64/`), `AvdId`, `tag.id`, `abi.type`,
  `hw.device.name`, RAM/disk/GPU keys and possibly absolute `skin.path`. **PARTIAL** (the
  file format is generated by avdmanager/Studio; no public doc page enumerates keys).
- The `.avd/` directory also contains generated/runtime files that may embed absolute
  paths or host state: `hardware-qemu.ini` (regenerated each launch), `userdata-qemu.img`,
  `cache.img`, `snapshots/`, `*.lock`. Conservative relocation: delete
  `hardware-qemu.ini`, `*.lock` and invalidate `snapshots/` (a snapshot is host/emulator
  state and may reference absolute image paths); keep user data only if the user opts in
  (**PARTIAL**; BRIEF explicitly requires "invalidate stale snapshots").
- Custom images imported by us should use relative `image.sysdir.1` under the bundle SDK so
  relocation needs no `config.ini` edit (R6 §5.5); images outside the SDK force an absolute
  path → mark the AVD as relocation-unsafe and rewrite on relocate.
- The emulator's gRPC discovery files and console token live outside the bundle by default
  (per-user temp/home); their exact paths are R4 territory (gRPC discovery), and for
  portable they must be redirected or added to the leftovers list. `ANDROID_EMULATOR_HOME`/
  `ANDROID_USER_HOME` cover most emulator home writes (VERIFIED defaults).

### 3.4 Studio portable configuration

- `STUDIO_PROPERTIES` "Sets the location of the `idea.properties` file" (VERIFIED,
  developer.android.com/tools/variables) — point it at `<root>/studio/idea.properties`.
- `STUDIO_VM_OPTIONS` sets the location of `studio.vmoptions` (VERIFIED) — point it at
  `<root>/studio/studio.vmoptions` to redirect JVM/heap/crash-related options.
- Inside `idea.properties`, documented Android examples redirect `idea.system.path` and
  `idea.plugins.path` (VERIFIED, Android Studio "Customize your IDE properties" page:
  `# idea.plugins.path=${idea.config.path}/plugins` and
  `idea.system.path=c:/workfolder/studio/caches/trunk-system`); the IntelliJ platform also
  supports `idea.config.path` and `idea.log.path` (**PARTIAL** for Android Studio's
  acceptance — same property file, but not shown in Android docs).
- What this cannot redirect: JVM crash logs (`java_error_in_studio*.log`), first-run
  licence/wizard state, downloaded plugin binaries (unless plugins path redirected),
  `.android`/adb keys (unless env vars set), Gradle home and project caches, recently-used
  project lists. Android's own docs list default Windows locations we must document as
  leftovers: Gradle cache `%USERPROFILE%\.gradle`, projects
  `%USERPROFILE%\AndroidStudioProjects`, SDK `%USERPROFILE%\AppData\Local\Android\SDK`,
  Android Studio system files `%LOCALAPPDATA%\Google\<product><version>`
  (VERIFIED <https://developer.android.com/studio/intro/studio-config> Windows section).
  Config dirs on macOS/Linux are the JetBrains defaults
  (`~/Library/Application Support/Google/AndroidStudio<ver>`,
  `~/Library/Caches/…`, `~/Library/Logs/…`; `~/.config/Google/…`, `~/.cache/Google/…`,
  `~/.local/share/Google/…`) — **PARTIAL** (JetBrains/Android KB pages not fully fetched).
- **Conclusion:** Studio portable config is "best effort": you can keep the *SDK* and most
  IDE state in the bundle, but Studio itself is not a portable application. Document and
  test the leftovers list instead of promising zero writes.

---

## 4. Windows: Mark-of-the-Web and execution policy

(All VERIFIED from Microsoft Learn, accessed 2026-11-26.)

- **MOTW/Zone.Identifier**: `Unblock-File` "removes the `Zone.Identifier` alternate data
  stream, which has a value of `3` to indicate that it was downloaded from the internet";
  "performs the same operation as the Unblock button on the Properties dialog box in File
  Explorer"; "works only in file system drives"; no-op (no error) on unblocked files;
  Windows PowerShell 3.0+; available on Windows and (PowerShell 7) macOS.
  <https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/unblock-file>
- **Execution policy** (`about_Execution_Policies`): default for Windows clients is
  `Restricted` (no scripts); `RemoteSigned` is the documented default for Windows
  computers/servers in the policy description and "runs scripts that are downloaded from
  the internet and not signed, if the scripts are unblocked, such as by using the
  Unblock-File cmdlet"; `Bypass` blocks nothing; enforcement only on Windows; scopes
  `MachinePolicy`/`UserPolicy` (Group Policy, highest) > `Process` (registered as
  `$Env:PSExecutionPolicyPreference`, session-only) > `CurrentUser` > `LocalMachine`;
  effective policy is by precedence, and Group Policy overrides all. A `-ExecutionPolicy`
  parameter on `powershell.exe`/`pwsh.exe` applies to that process only.
  <https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_execution_policies>
- **Important nuance (VERIFIED in that page):** "Other methods of downloading files may not
  mark the files as coming from the Internet Zone. Some examples include: `curl.exe`,
  `Invoke-RestMethod`, `Invoke-WebRequest`." So a bundle installed with our own downloader
  may have no MOTW at all, while a bundle delivered as a browser-downloaded ZIP and
  extracted may have it on extracted files (**PARTIAL**: per-file propagation of
  Zone.Identifier from ZIP extraction is not stated by Microsoft docs; test on Windows).
- **Launcher design:** `.cmd`/`.bat` are **not** governed by PowerShell execution policy
  (policy applies to script files interpreted by PowerShell). Keep the first-run entry
  point a `.cmd`; if a helper `.ps1` is used, invoke
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<root>\bin\first-run.ps1"` and
  still `Unblock-File` the bundle (belt and braces). Unblocking a large tree is slow
  (one ADS delete per file) → limit to executables/scripts (`*.exe`, `*.dll`, `*.ps1`,
  `*.cmd`, `*.bat`, `*.cmd`, `*.jar`) or do it once and record a marker file.

## 5. Filesystems: FAT32 / exFAT / noexec (portable preflight)

**VERIFIED** (Microsoft "File System Functionality Comparison",
<https://learn.microsoft.com/en-us/windows/win32/fileio/filesystem-functionality-comparison>):

| Limit | NTFS | exFAT | FAT32 |
|---|---|---|---|
| Maximum file size | 2^64−1 bytes | 2^64−1 bytes | **4 GiB** |
| Tracks file owner | Yes | No | No |
| Metadata-only journaling | Yes | No | No |

- **FAT32 → refuse** (BRIEF requirement): a 4 GiB per-file cap breaks AVD data images
  (`userdata-qemu.img` for a 6–8 GB data partition) and any large system image; also no
  journal → corruption after yank. Detect by filesystem name (`GetVolumeInformationW` on
  Windows, `statvfs`/`getmntent` on POSIX, `diskutil info` on macOS) and refuse with a
  clear message.
- **exFAT → warn** (BRIEF says warn): files >4 GiB fine, but no owner/permission model
  (Windows table), Linux exFAT driver is mount-option-driven only (`uid=`, `gid=`,
  `umask=`, `dmask=`, `fmask=` — VERIFIED docs.kernel.org/filesystems/exfat), and it
  cannot store Unix executable bits/symlinks natively → a Python runtime whose `bin/`
  layout uses symlinks (uv/python-build-standalone Unix layouts do; VERIFIED uv docs use
  symlinks for minor-version dirs) will not survive an exFAT copy faithfully. Warn that
  reinstallation/first-run repair may be needed.
- **NTFS** is the recommended Windows removable FS; **APFS/HFS+** on macOS; **ext4/XFS/
  btrfs** on Linux. Cross-OS bundles are unsupported (PLAN decision 5).
- **noexec**: `mount` option `noexec` = "Do not permit direct execution of any binaries on
  the mounted filesystem" (VERIFIED mount(8) <https://man7.org/linux/man-pages/man8/mount.8.html>).
  Detection (PT2): parse `/proc/self/mountinfo`, find the longest mount point prefix of the
  bundle root, check for `noexec` (and `nosuid`/`nodev` as warnings), **plus** a runtime
  probe: copy a tiny executable (e.g. the bundled `python` headerless launcher) to the root
  and `subprocess.run` it; treat `EACCES/EPERM` with `exec` semantics as noexec. Also check
  `readonly` mounts. macOS: check `mount` output for `noexec`; Windows: filesystem exec is
  governed by ACL/AV, not mount flags.
- **Space/size**: preflight requires free space ≥ bundle size × 1.2 + headroom for AVD data
  (config default 6–8 GB per AVD; capacity model in `core/config.py`); write-speed test:
  write/fsync/read a 64–256 MiB temp file inside the bundle root and report MB/s; abort or
  warn below a threshold (e.g. <10 MB/s write → 64 AVDs unusable).
- **Sparse files**: emulator `userdata-qemu.img` and `system.img` may be sparse/partially
  allocated; copying with plain copy can inflate to full size and fail on FAT32 first. Use
  FSCTL_SET_SPARSE on Windows, `SEEK_HOLE`/`--sparse=always` equivalents on POSIX, and
  report allocated vs logical size in preflight.

## 6. macOS quarantine / Gatekeeper

**VERIFIED** (Apple Platform Security, "Gatekeeper and runtime protection in macOS",
published 2024-12-19, and Apple Support "Safely open apps on your Mac",
<https://support.apple.com/en-us/102445>):

- Gatekeeper verifies, for software **downloaded from outside the App Store**, that it is
  from an identified developer, notarized by Apple, and unaltered; it "requests user
  approval before opening downloaded software for the first time" and "tracks the
  provenance of files written by downloaded software".
- Unsigned/unnotarized software triggers a warning; the user can override via
  System Settings → Privacy & Security → **Open Anyway** (after a first open attempt).
  A Mac with "App Store only" policy will not open non-App-Store software at all.
- Our CLI/launcher role: the bundle's Python/emulator binaries come from third parties
  (python-build-standalone, Google emulator) and are typically signed/notarized by their
  producers, but the **bundle as assembled** (zip or folder downloaded by the user) will
  carry quarantine on the downloaded archive; extracted files may inherit quarantine
  (**PARTIAL** — Apple's docs describe the approval flow, not ZIP propagation).
- Practical mitigation for the launcher:
  `xattr -dr com.apple.quarantine "<bundle-root>"` (documented BSD `xattr`; **PARTIAL** for
  the exact attribute name in Apple's current docs — this is long-standing Gatekeeper
  behavior, test on macOS 15/26). The launcher can detect the attribute with
  `xattr -p com.apple.quarantine <file>` and print a one-line instruction rather than
  silently stripping it (silently stripping is a security-smell; BRIEF only requires the
  launcher to "unblock files on Windows first run", not macOS).
- **Cannot be portable:** notarization identity/team of *our* bundle; Gatekeeper policy;
  macOS 15+ "Open Anyway" flow is user-interactive. Document as a first-run step in
  `docs/portable.md`.

## 7. Keyring availability on portable machines

**VERIFIED** (keyring docs <https://keyring.readthedocs.io/en/latest/>):

- Recommended backends: **macOS Keychain**, **Freedesktop Secret Service**, **Windows
  Credential Locker**. They are auto-chosen, and "credentials stored in these are machine +
  user scoped" by construction (each OS service), never inside the bundle.
- Linux Secret Service needs a running D-Bus session; keyring docs document use on headless
  machines only via a GNOME Keyring daemon and "run your application in the same D-Bus
  session as the daemon"; the KWallet backend needs `dbus-python` from the system. Error
  when no backend is viable:
  `RuntimeError: No recommended backend was available. Install the keyrings.alt package…`.
- `keyrings.alt` provides "alternate, possibly-insecure backends"; "PNG/plaintext" style
  file backends write secrets **inside the user profile**, not the bundle (and would still
  be per-machine). `PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring` forces the Null
  backend.
- **Portable implication:** a USB bundle opened on a *different machine* cannot rely on the
  keyring having the old credentials — they live in that machine's credential store. So:
  the vault must fall back to the BRIEF's **passphrase-derived encrypted file vault inside
  the bundle root** (`proxy/vault_file`, X4) and the product must surface "credentials are
  per-machine unless the file vault is used". Never attempt keyring provisioning on a
  portable machine; never store proxy passwords in `config.toml`.

## 8. What cannot be portable (explicit)

1. **Hardware virtualisation.** WHPX (Windows feature + reboot + BIOS VT-x/AMD-V), AEHD
   (kernel driver, admin), KVM (Linux module + `/dev/kvm` permissions via group/udev),
   HVF (macOS built-in but Apple-Silicon-only for arm64 images). Emulator cannot use a
   software fallback in practice (BRIEF §6). Portable edition can *detect* and *diagnose*;
   it cannot install/reboot/authorise. Android-Silicon hosts cannot boot x86/x86_64 images;
   x86 hosts cannot boot arm64 images (emulator has no cross-ISA runtime for AVDs).
2. **Linux udev rules / group membership** for USB device access and KVM: root-owned files
   under `/etc/udev/rules.d` and group changes. Not needed for emulator-over-TCP, needed
   for physical-device flows.
3. **Windows kernel/driver changes** (AEHD, WHPX) and any admin-elevated install: portable
   edition must run from a standard account with no elevation.
4. **Google Play images/licence/account.** Play Store system images require licence
   acceptance and are Google-signed; redistribution in a bundle is not permitted, Play
   sign-in needs network + account (BRIEF §8 out of scope). Only *stock downloads* or
   *user-supplied* images are portable.
5. **Android Studio as an application.** Installer/uninstaller registry keys, IDE updates,
   licence wizard, crash logs, machine-scoped caches — only its `idea.properties` can be
   redirected; the rest is documented leftover, never zero.
6. **macOS Gatekeeper/notarization** of the assembled bundle (user interaction required).
7. **Filesystem guarantees.** FAT32 refusal is a hard limitation; exFAT loses POSIX
   semantics; noexec mounts cannot be worked around without remount privileges.
8. **Cross-OS bundles** (per PLAN decision 5): runtime + emulator + JDK are per-OS.
9. **Registry writes on Windows** unavoidable in some paths (uv PEP 514 registration if
   `uv python install` is used; Studio uninstall entries; possibly emulator GPU
   capability caching — **UNVERIFIED**). Keep them enumerated in the leftovers list and
   `portable verify`.
10. **`~/.android`, `$HOME`-relative caches**: reducible to near-zero with env vars, but
    legacy tools (Studio ≤4.3 reading `ANDROID_SDK_HOME`, adb keys, Java prefs
    `~/.java/.userPrefs`) can still write outside — document, don't promise zero.

## 9. Projected leftovers list per OS (for `portable verify` + docs)

`verify` design: record a **snapshot** of the documented watchlist (file paths + mtimes/
hashes; registry keys on Windows) immediately after bundle creation, then re-snapshot after
`avdfleet selftest --portable` and fail if a *new* path appears that is not in this
documented list (allow-list with glob patterns; ignore OS churn like browser caches).

### Windows 11

| Leftover | Why / when | Status |
|---|---|---|
| `%USERPROFILE%\.android\` (adbkey, adb_usb.ini, avd/, cache/, breakpad/) | any tool with unset/ignored `ANDROID_USER_HOME`; adb keys; crashpad | PARTIAL (docs: older tools ignore ANDROID_USER_HOME) |
| `%LOCALAPPDATA%\Google\<product><version>\` | Android Studio system files (caches/indexes/logs) | **VERIFIED** (studio-config docs) |
| `%APPDATA%\Google\<product><version>\` | Android Studio config/options/plugins | PARTIAL (JetBrains default; Android docs show only LOCALAPPDATA path) |
| `%USERPROFILE%\.gradle\` | Gradle cache if Studio/Gradle used; can redirect with `GRADLE_USER_HOME` | **VERIFIED** (studio-config lists default path + GRADLE_USER_HOME override) |
| `%USERPROFILE%\AndroidStudioProjects\` | default project dir | **VERIFIED** (studio-config) |
| `%TEMP%\android-*` / emulator gRPC discovery, lock files, crash dumps | emulator/adb temp files | PARTIAL (R4 owns gRPC paths; redirect `TMP`/`TEMP` for child processes) |
| `HKCU\Software\Python\Astral\CPython<ver>` | uv PEP 514 registration **if** `uv python install` runs | **VERIFIED** (uv docs) |
| `HKCU\...\Android Studio*` / uninstall entries | Studio installer only; we don't install Studio | UNVERIFIED |
| `%LOCALAPPDATA%\uv\cache`, `%APPDATA%\uv\data`, `%APPDATA%\uv` config | uv defaults outside root | **VERIFIED** (uv storage docs) — redirect via `UV_*` |
| Defender/AV logs, `Zone.Identifier` ADS on bundle files | unblock removes ADS; Windows may log scans | PARTIAL |
| `%USERPROFILE%\.java\.userPrefs\` | AWT/Studio preferences store | UNVERIFIED (not created in our test; standard Java behaviour) |

### macOS

| Leftover | Why / when | Status |
|---|---|---|
| `~/.android/` | emulator/adb prefs if env not honoured | PARTIAL |
| `~/Library/Application Support/Google/AndroidStudio<ver>/` | Studio config/options | PARTIAL (JetBrains default) |
| `~/Library/Caches/Google/AndroidStudio<ver>/` | Studio caches | PARTIAL |
| `~/Library/Logs/Google/AndroidStudio<ver>/` | Studio logs | PARTIAL |
| `~/Library/Preferences/*AndroidStudio*.plist` | Studio prefs | UNVERIFIED |
| `~/.cache/uv/`, `~/.local/share/uv/` | uv defaults on macOS follow XDG | **VERIFIED** (uv storage docs) — redirect |
| quarantine xattr (`com.apple.quarantine`) on downloaded/extracted bundle files | Gatekeeper provenance | PARTIAL (mechanism VERIFIED, attribute name PARTIAL) |
| `/var/folders/.../T/` temp files | per-user temp; redirect `TMPDIR` | PARTIAL |
| `~/Library/Application Support/Google/AndroidStudio*/` plugin downloads | plugins | PARTIAL |

### Linux

| Leftover | Why / when | Status |
|---|---|---|
| `~/.android/` | emulator/adb prefs if env not honoured | PARTIAL |
| `~/.config/Google/AndroidStudio<ver>/`, `~/.local/share/Google/AndroidStudio<ver>/`, `~/.cache/Google/AndroidStudio<ver>/`, `~/.local/share/Google/AndroidStudio<ver>/log/` | Studio config/system/log | PARTIAL (JetBrains default) |
| `~/.gradle/` | Gradle cache; redirect `GRADLE_USER_HOME` | PARTIAL |
| `~/.cache/uv/`, `~/.local/share/uv/`, `~/.local/bin/` (uv/python shims) | uv defaults / uv creates shims in `$XDG_BIN_HOME` | **VERIFIED** (uv storage docs) — redirect |
| `/tmp/android-<user>/` (gRPC discovery, locks, surfaceflinger sockets?) | emulator runtime temp files | PARTIAL (R4) |
| Secret Service keyring item(s) for proxy vault (if keyring used) | keyring stores in the desktop keyring, not files | **VERIFIED** (keyring docs) — use file vault for portability |
| `~/.java/.userPrefs/` | Java/AWT prefs (sdkmanager/avdmanager/Studio) | UNVERIFIED |
| X11/Wayland runtime sockets | OS, not our write | n/a |

Also always document: **the bundle root itself is written** (data/, cache/, logs/, avd/,
sdk/ updates, `.portable-state/` marker), which is expected; and **absolute paths recorded
inside the bundle** (AVD `.ini` `path=`, `hardware-qemu.ini`, snapshots) make relocate
mandatory after a drive change.

## 10. Commands / implementation notes for PT1–PT5

Launcher environment (per-OS, all absolute under `<root>`; set before any child process):

```text
ANDROID_HOME=<root>/sdk
ANDROID_SDK_ROOT=<root>/sdk
ANDROID_USER_HOME=<root>/home/android
ANDROID_SDK_HOME=<root>/home
ANDROID_EMULATOR_HOME=<root>/home/android
ANDROID_AVD_HOME=<root>/avd
ANDROID_PREFS_ROOT=<root>/home/android      # defensive legacy alias (UNVERIFIED)
JAVA_HOME=<root>/runtime/jre
UV_CACHE_DIR=<root>/cache/uv
UV_PYTHON_INSTALL_DIR=<root>/runtime/uv-python
UV_TOOL_DIR=<root>/runtime/uv-tools
UV_TOOL_BIN_DIR=<root>/bin
TMPDIR=<root>/tmp            # POSIX
TMP=<root>\tmp ; TEMP=<root>\tmp   # Windows
STUDIO_PROPERTIES=<root>/studio/idea.properties
STUDIO_VM_OPTIONS=<root>/studio/studio.vmoptions
GRADLE_USER_HOME=<root>/cache/gradle
PYTHONNOUSERSITE=1
```

Windows first-run (in `AVD Fleet.cmd` → powershell helper):

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<root>\bin\first-run.ps1"
# first-run.ps1 core:
Get-ChildItem -LiteralPath "<root>" -Recurse -File -Include *.exe,*.dll,*.ps1,*.cmd,*.bat,*.jar |
  Unblock-File -ErrorAction SilentlyContinue
```

Root-change detection / relocate trigger: compare `manifest.json.root` (recorded at build)
with the actual resolved root; on change run PT3: rewrite each `<avd>.ini` `path=` (or use
`avdmanager move avd -n <name> -p <newdir>`), delete `hardware-qemu.ini`/`*.lock`, mark
snapshots invalid, re-run `emulator -list-avds` + `avdmanager list avd` verification, and
update the manifest `root`. Also re-check junctions/symlinks inside a uv-installed Python
(`cpython-3.x-<os>-<arch>-none` → patch dir) after a drive/letter change.

macOS first run:

```text
xattr -p com.apple.quarantine "<root>/bin/avdfleet.sh" 2>/dev/null && \
  echo "If macOS blocks the launcher: xattr -dr com.apple.quarantine \"<root>\" (or approve via System Settings > Privacy & Security > Open Anyway)"
```

Linux preflight probes: `findmnt -no FSTYPE,OPTIONS -T <root>`; refuse `vfat`, warn
`exfat`/`msdos`, fail on `noexec`; `df -B1`; write-speed test; `/dev/kvm` existence +
access (`R_OK|W_OK`), `os.access`; `nproc`; `MemAvailable`.

## 11. Hand-offs / open items

- **PT1**: implement the env block above, `AVD Fleet.cmd`/`avdfleet.sh`/`.command`,
  Windows unblock (fast allow-list), root-change detection; never redirect
  `HOME`/`USERPROFILE`.
- **PT2**: FAT32 refusal; exFAT warn; noexec probe (mountinfo + exec probe); free space
  (bundle + AVD data estimate); write-speed; quarantine/MOTW detection (report, don't
  silently fix macOS); hypervisor detection delegates to P1/R2.
- **PT3**: AVD `.ini` rewrite (prefer documented `avdmanager move avd`), delete
  `hardware-qemu.ini`/locks, invalidate snapshots, fix uv junction/symlink dirs, re-verify.
- **PT4**: bundle manifest records absolute root + sha256 of every file (contracts
  `bundle.schema.json` already supports `root`, `entries`), plus `tool_versions`;
  `--offline` must pre-seed Appium extension dirs (APPIUM_HOME under root) and never call
  sdkmanager at runtime; record that Google images cannot be redistributed.
- **PT5**: `STUDIO_PROPERTIES` = **file** path; ship `idea.properties` with
  `idea.config.path`/`idea.system.path`/`idea.plugins.path`/`idea.log.path` under
  `<root>/studio/`; document the Studio leftovers in §9 (do not claim zero writes).
- **T5**: assertions for FAT32 refusal, exFAT warning, noexec detection, relocate
  (drive-letter simulation: move bundle to a new path and rewrite AVD ini), leftover diff
  against the allow-list, offline first run with an already-cached image.
- **Contract gap**: `contracts/bundle.schema.json` has no field for "documented leftovers"
  or a root/hash history; `PortableStatus.leftovers` (OpenAPI) exists — T5/PT4 should
  drive the leftovers list from the same checked-in allow-list used by `portable verify`.
  Flag to root for v0.2.

## 12. Sources (all accessed 2026-11-26)

uv:

- Storage layout/env overrides: <https://docs.astral.sh/uv/reference/storage/>
- Python versions (managed = python-build-standalone; self-contained; storage; PEP 514
  registry registration; minor-version symlink/junction):
  <https://docs.astral.sh/uv/concepts/python-versions/>
- python-build-standalone quirks (build-time absolute paths fixed up by uv; no
  `Scripts/pip.exe` on Windows): <https://gregoryszorc.com/docs/python-build-standalone/main/quirks.html>

Python:

- `venv` non-portability ("inherently non-portable"): <https://docs.python.org/3/library/venv.html>
- Windows embeddable package (`._pth`, isolated, no pip; isolated mode, search paths):
  <https://docs.python.org/3/using/windows.html#the-embeddable-package>
- Embedding Python in another application (reference; not required by our launcher):
  <https://docs.python.org/3/extending/embedding.html>

Android:

- Environment variables (`ANDROID_HOME`, deprecated `ANDROID_SDK_ROOT`,
  `ANDROID_USER_HOME`, legacy `ANDROID_SDK_HOME`, `ANDROID_EMULATOR_HOME`,
  `ANDROID_AVD_HOME`, `STUDIO_VM_OPTIONS`, `STUDIO_PROPERTIES`, `STUDIO_JDK`,
  `ANDROID_EMULATOR_USE_SYSTEM_LIBS`): <https://developer.android.com/tools/variables>
- `avdmanager` (`move avd`, absolute `path`, `-k` package ids):
  <https://developer.android.com/studio/command-line/avdmanager>
- `sdkmanager` (`--sdk_root`, package channels):
  <https://developer.android.com/studio/command-line/sdkmanager>
- Android Studio config/customise (`STUDIO_VM_OPTIONS`, `STUDIO_PROPERTIES`,
  `idea.properties`, `idea.system.path`/`idea.plugins.path`, Windows default dirs:
  `%USERPROFILE%\.gradle`, `%USERPROFILE%\AndroidStudioProjects`,
  `%USERPROFILE%\AppData\Local\Android\SDK`,
  `%LOCALAPPDATA%\Google\<product><version>`):
  <https://developer.android.com/studio/intro/studio-config>
- Emulator environment variables and `-help-environment` pointer:
  <https://developer.android.com/studio/run/emulator-commandline>

Windows:

- `Unblock-File` (Zone.Identifier ADS, value 3, no-op if unblocked, filesystem drives):
  <https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/unblock-file>
- Execution policies (RemoteSigned/unblock relation; scopes and precedence; Group Policy;
  curl/IRM/IWR do not add MOTW):
  <https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_execution_policies>
- Filesystem comparison (FAT32 max file 4 GiB; exFAT 2^64−1; file-owner tracking):
  <https://learn.microsoft.com/en-us/windows/win32/fileio/filesystem-functionality-comparison>

macOS:

- Gatekeeper and runtime protection (notarization, first-open approval, provenance):
  <https://support.apple.com/guide/security/gatekeeper-and-runtime-protection-sec5599b66df/web>
- Safely open apps on your Mac (Open Anyway flow, App Store-only policy):
  <https://support.apple.com/en-us/102445>
- `com.apple.quarantine` xattr name/removal: standard `xattr(1)` behaviour — **PARTIAL**
  (Apple pages fetched above do not name the attribute; verify on macOS 15/26).

Linux filesystems:

- `mount(8)` (`noexec`, `nosuid`, `nodev`; remount/bind semantics):
  <https://man7.org/linux/man-pages/man8/mount.8.html>
- exFAT kernel driver mount options (`uid`, `gid`, `umask`, `dmask`, `fmask`):
  <https://docs.kernel.org/filesystems/exfat.html>

Keyring:

- `keyring` backends (macOS Keychain, Windows Credential Locker, Freedesktop Secret
  Service; headless D-Bus requirement; `keyrings.alt`; null backend):
  <https://keyring.readthedocs.io/en/latest/>

Cross-references (owned elsewhere): R1 (versions/checksums of Python/Node/JDK/tools),
R2 (hypervisor/WHPX/KVM/HVF setup, GPU, Linux deps), R4 (emulator gRPC discovery files and
temp paths), R6 (custom images + `image.sysdir.1` semantics). BRIEF.md §3.10, §6, §7 item 7;
PLAN.md host constraint and decision 5 (cross-OS bundles unsupported).
