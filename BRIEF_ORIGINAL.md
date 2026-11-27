# AVD Fleet — supplied build prompt, part 1 (sections 0–3.1)

> This is the original text supplied by the operator after Wave 0. The earlier
> `BRIEF.md` reconstructed these sections during Wave 0 because only sections 4–9
> were in the first message. `BRIEF.md` remains the normative working brief; where
> the reconstruction differs, this file wins. Sections 3.2–3.11 were not supplied
> and remain reconstructed in `BRIEF.md`, now cross-checked against the text below.

## 0. How to work (read this first)

You coordinate and subagents build. After Wave 0, do no implementation yourself. Keep
your context for planning, unblocking, integration and verification. There is no cap on
subagents. Split the work into as many parallel agents as there are independently
testable units with separate file ownership. If a unit is too big for one agent run,
split it again. Spawn every independent agent in a wave as one batch, in parallel. Never
run independent work one after another. Pipeline: start each agent as soon as its inputs
exist (most need only the frozen contracts). Nested fan-out: if your harness lets
subagents spawn subagents, lead agents (installer, proxy, portable, UI, QA) spawn their
own workers. Otherwise spawn every worker yourself. Use your harness's spawn tool.
Redundancy for high-risk unknowns: run 2 independent agents each on OOBE image
verification, the proxy bridge and the proxy-isolation gate (G1). Keep the result that is
verified. Ownership: each agent writes only to the paths it owns. You own contracts/ and
src/avdfleet/core/. Agents send contract change requests in their reports; you apply
them additively and tell affected agents. Git: one branch or worktree per agent where
possible; commit early; the integrator merges continuously. Verify before claiming: every
agent runs its tests and pastes the command and pass/fail counts into its report. Re-run
them yourself before you accept the work. Blockers: a blocked agent reports straight
away with the exact thing it needs. A failing gate gets parallel fixer agents scoped to
that failure; unrelated work keeps going. Zero touch: install whatever you need
(runtimes, package managers, build tools, Docker, test proxies) without asking. Elevate
only for steps that need it. If the environment can't do something (e.g. no hardware
virtualisation), take the fallback in section 6. Every agent brief includes: context
(BRIEF.md + contracts), scope, owned paths, inputs, deliverables, acceptance commands,
constraints (section 2) and this report format: status (done|partial|blocked) · files
changed · tests (command, pass/fail) · contract change requests · risks · next.

## 1. Product

AVD Fleet (avdfleet) has three parts:

**Zero-touch installer.** One command per OS (install.sh for macOS/Linux, install.ps1 for
Windows) detects OS, distro, architecture and hypervisor, then installs and configures,
safe to re-run: Android Studio (skip with `--no-studio`), a JDK, the Android SDK
command-line tools, platform-tools (adb), the emulator, build-tools, platforms, chosen
system images, hardware acceleration, Node.js, Appium + UiAutomator2 driver (+ Appium
Inspector on desktops), scrcpy, environment variables, and AVD Fleet itself. It finishes
by running `avdfleet doctor`.

**Fleet manager.** A local daemon with a web GUI and a CLI. It must: create, deploy,
boot, monitor and control many AVDs; manage a proxy pool and give each AVD its own proxy
credentials; deploy any Android system image, from the SDK catalogue or a custom source;
bring instances to the Android OOBE (Setup Wizard) on images proven to have one; run
commands and scripts across selected AVDs in parallel.

**Portable edition.** The same app as a self-contained folder or external drive. It runs
without installing anything and without admin rights, keeps all state inside its own
root, still works after being moved or getting a new drive letter, and leaves nothing on
the host beyond the documented exceptions in section 3.10.

Target hosts: Windows 10 (1803+) and 11 on x64; macOS 12+ on Apple Silicon and Intel;
Linux x64 (Ubuntu/Debian, Fedora/RHEL, Arch). The emulator has no official Windows ARM64
or Linux arm64 builds: detect those hosts and fail with a clear message.

## 2. Fixed decisions (don't reopen these)

- **Core, installer logic, daemon and CLI:** Python 3.12 managed by uv. The bootstrap
  scripts install uv, install AVD Fleet with `uv tool install`, then run
  `avdfleet install`. All cross-platform logic is in Python; the bootstraps stay thin.
- `install.sh` must run on macOS's stock bash 3.2 (no `declare -A`, no `mapfile`).
  `install.ps1` uses Windows PowerShell 5.1 syntax and must also run under PowerShell 7
  (newer Windows releases add `pwsh` and make 5.1 a Feature on Demand). Launch it with
  `-NoProfile -ExecutionPolicy Bypass`; an execution policy set by Group Policy overrides
  Bypass, so detect and report that case.
- **API:** FastAPI + uvicorn, REST + WebSocket, bound to `127.0.0.1` by default. Bearer
  token stored in a file readable only by the current user: mode `0600` on Unix, a
  current-user-only ACL on Windows (`os.chmod` can't do this there). Starlette's CORS
  middleware doesn't cover WebSockets, so check the Origin header in the handler. Remote
  binding only with an explicit flag, a warning and optional TLS.
- **GUI:** React + TypeScript + Vite + Mantine, typed client generated from
  `contracts/openapi.yaml`, built assets shipped in the Python package and served by the
  daemon. `avdfleet gui` starts the daemon if needed and opens the browser. A pywebview
  native window is optional (it needs GTK or Qt on Linux).
- **CLI:** Typer, covering every API capability, plus declarative
  `avdfleet apply -f fleet.yaml` (plan, then apply).
- **Paths:** one `Paths` provider in `core/` resolves every location for installed and
  portable modes. No module computes its own paths.
- **Storage and secrets:** SQLite at the Paths location. Secrets go in the OS keyring.
  When keyring raises `NoKeyringError` (headless Linux) or in portable mode, use an
  encrypted vault file with a key derived from a user passphrase (Argon2id or scrypt +
  AES-GCM; `AVDFLEET_VAULT_PASSPHRASE` for headless runs). A key stored beside the file
  only hides secrets: if no passphrase is set, warn and say so.
- **Android tooling:** Google's docs now mark sdkmanager, avdmanager and the emulator
  command as deprecated in favour of the Android CLI (`android sdk …`,
  `android emulator …`, in preview). Put all tooling behind the `sdk/` interface with
  two backends (Android CLI and legacy tools). Keep the emulator binary for flags the CLI
  lacks (`-http-proxy`, `-grpc`, `-sysdir`, `-ports`, `-read-only`).
- Pin and record the emulator version you test against. Behaviour changes between
  releases; for example, a next-generation engine is the default for new API 37+ AVDs in
  Studio Canary.
- **Subprocesses:** argument lists only, never `shell=True` with interpolated input.
  Timeouts everywhere. asyncio.
- **Quality gates:** ruff, pyright/mypy and pytest for Python; eslint, tsc, vitest and
  Playwright for the UI.
- **Versions:** research current stable versions at build time. As of 2026-11-26:
  Appium 4.x (Node `^22.22.2 || ^24.15.0 || >=26.0.0`, npm 10+), emulator 37.3.x,
  scrcpy 5.0.x, Android CLI in preview. Check SHA-256 for every download that publishes
  one (Android Studio and command-line tools do).
- **SDK licences:** `sdkmanager --licenses` has no non-interactive flag. Accept licences
  only when `--accept-android-sdk-licenses` is passed (then pipe yes, or use the Android
  CLI equivalent) or the user consents interactively.

## 3. Requirements

### 3.1 Installer

**Step engine:** each step has check, apply and verify phases, plus dependencies.
Re-running is a no-op; `--dry-run` prints the plan. Resumes after a failure or reboot.
Writes structured logs and an install manifest that `avdfleet uninstall` and
`avdfleet repair` use. Privileges: zero touch needs an admin- or sudo-capable account.

- **Windows:** enabling Windows Hypervisor Platform needs admin and a reboot, so
  schedule a resume after the reboot.
- **Linux:** grant `/dev/kvm` access with a udev rule (kvm group or uaccess) so no
  re-login is needed.
- **macOS:** install the Command Line Tools non-interactively (`softwareupdate`, with
  sudo) before Homebrew. Homebrew needs them, and `xcode-select --install` is
  interactive.

**Providers:**
- Windows: winget (`Google.AndroidStudio`), choco as fallback.
- macOS: Homebrew casks `android-studio`, `android-commandlinetools`,
  `android-platform-tools`.
- Linux: apt/dnf/pacman.
- Use official archives where packages lag. Unzipped command-line tools must live at
  `<sdk>/cmdline-tools/latest/`.

**JDK:** use Android Studio's bundled JBR as `JAVA_HOME` when Studio is installed,
otherwise a JDK 17+ (recent command-line tools fail on older JDKs).

**Acceleration:**
- Windows: WHPX only (Windows 10 1803+). HAXM was removed in emulator 36.2.x, and AEHD
  sunsets on 31 Dec 2026. If AEHD is installed, offer to switch to WHPX. Hyper-V, WSL2
  and Core Isolation don't conflict with WHPX, so don't report them as conflicts.
- Linux: KVM. macOS: Hypervisor.framework (nothing to install).
- An accelerated emulator can't run inside VirtualBox, VMware or Docker VMs without
  nested virtualisation: detect and report this. Doctor runs `emulator -accel-check`.

**Environment variables:** set `ANDROID_HOME`; unset or align the deprecated
`ANDROID_SDK_ROOT`. Defaults: `ANDROID_USER_HOME` is `~/.android`,
`ANDROID_EMULATOR_HOME` defaults to `ANDROID_USER_HOME`, and `ANDROID_AVD_HOME` defaults
to `$ANDROID_EMULATOR_HOME/avd`. The emulator silently ignores an `ANDROID_AVD_HOME` that
doesn't exist, so create directories before setting it.

**Android Studio:** point it at the shared SDK. Default SDK paths:
`%LOCALAPPDATA%\Android\Sdk` (Windows), `~/Library/Android/sdk` (macOS),
`~/Android/Sdk` (Linux, by convention). Skipping the first-run SDK wizard with
`disable.android.first.run=true` in `idea.properties` is undocumented: test it, and if it
fails, document the click-through. AVDs in the shared AVD home are expected to appear in
Device Manager: verify this.

**Host architecture to image ABI:** `arm64-v8a` on Apple Silicon, `x86_64` elsewhere. On
x86_64 hosts, ARM app binaries run only inside API 30+ Google APIs or Play images
(per-process translation, for development use).

**Flags:** `--no-studio`, `--images <list>`, `--sdk-root`, `--avd-home`,
`--proxies <file>` (seeds the proxy pool), `--download-proxy <url>` (install-time proxy
for sdkmanager, npm, pip and brew), `--accept-android-sdk-licenses`, `--yes`.

`avdfleet doctor` checks Java, every SDK component, acceleration, adb, the emulator, Node,
Appium + `appium driver doctor uiautomator2`, scrcpy, environment variables, free ports,
disk and RAM. Disk: the emulator refuses to start with under 5 GB free. RAM: API 37+ AVDs
need at least 4 GB each, and Windows reserves all guest RAM at launch, so check the commit
limit. Each check gives a fix hint and supports `--fix`.

### 3.2 Images and deployment (supplied text begins)

Catalogue from the Android CLI, sdkmanager or the official manifests. The newest
manifests are `repository2-4.xml`, `addons_list-6.xml` and
`sys-img/<tag>/sys-img2-4.xml`. Discover tags from addons_list rather than hard-coding
them. Parallel downloads with progress, and delete.

Custom images: import a zip or directory into
`system-images/android-<api>/<tag>/<abi>/` with its `source.properties`/`package.xml`, or
launch it with `-sysdir <absolute path>`. Studio can add a custom repository under SDK
Update Sites; sdkmanager has no documented custom-site flag. AOSP builds emulator images
with `make emu_img_zip` (Android 13+).

Templates: image, device profile, RAM, cores, disk, GPU mode, timezone, extra
`config.ini` keys and hooks. Bulk-deploy N AVDs with a naming pattern. Clone, rename,
delete and wipe. Export and import templates as YAML.

*(The supplied message ended here; sections 3.3–3.11 are reconstructed in BRIEF.md.)*
