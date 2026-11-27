# Installer

AVD Fleet has two layers: the thin **bootstrap** scripts
([bootstrap/install.sh](../bootstrap/install.sh),
[bootstrap/install.ps1](../bootstrap/install.ps1)) install `uv` and the
`avdfleet` Python tool, then hand over to the **installer engine**
(`avdfleet install`), which owns every host change. This page documents the
engine and the flags both layers accept.

```
bootstrap -> uv -> uv tool install avdfleet -> avdfleet install -> avdfleet doctor
```

## What gets installed

The engine runs an ordered, idempotent step list. Each step is independently
checkable, dry-runnable and records its result in the install manifest.

| Step id | Phase | Installs / configures |
|---|---|---|
| `tools.jdk` | tools | A JDK for the SDK tools (Temurin/Microsoft OpenJDK; default JDK 21) |
| `tools.node` | tools | Node.js for Appium (config default 22.x; Appium 4 needs Node ≥ 22.22.2, 24.15+ or 26+) |
| `tools.appium` | tools | Appium 4 server, the UiAutomator2 driver, optional Appium Inspector |
| `tools.scrcpy` | tools | scrcpy for local display (optional) |
| `sdk.cmdline_tools` | sdk | Android command-line tools (`cmdline-tools;latest`) |
| `sdk.licenses` | sdk | Android SDK licences — **only** with explicit acceptance |
| `sdk.packages` | sdk | platform-tools, emulator, platforms, build-tools and the required system-image family |
| `sdk.env` | sdk | `ANDROID_HOME`/`ANDROID_SDK_ROOT`/`JAVA_HOME`/`PATH` for the current user |
| `studio.config` | config | Android Studio configuration and `idea.properties` (optional) |
| `accel.detect` | acceleration | WHPX / KVM / HVF detection, nested-virtualisation reporting |
| `accel.aehd` | acceleration | AEHD → WHPX migration for existing installs (opt-in; AEHD is sunsetting) |
| `accel.whpx` | acceleration | Enable Windows Hypervisor Platform (may require a reboot) |
| `accel.kvm` | acceleration | `/dev/kvm` access: group/udev guidance, reboot hint |

Steps have explicit dependencies; `--only` resolves the dependency closure and
skips unrelated steps. `--no-appium`, `--no-studio` and `--no-scrcpy` remove
those steps (and steps that depend on them fail fast if selected explicitly).

Component versions are resolved by the installer and recorded in the install
manifest; the implementation keeps configurable version fields in
`config.toml` so operators and CI can pin them. The version policy and the
current upstream versions are documented in [compatibility.md](compatibility.md)
and `research/R1-versions.md`.

## Commands

### `avdfleet install`

```bash
avdfleet install --accept-android-sdk-licenses --yes
avdfleet install --dry-run
avdfleet install --repair
avdfleet install --resume
avdfleet install --only tools.jdk --only sdk
avdfleet install --sdk-root /opt/android-sdk --no-appium --no-studio
avdfleet install --platform-provider debian
```

| Flag | Meaning |
|---|---|
| `--accept-android-sdk-licenses` | Accept the Android SDK licences unattended. Required for non-interactive installs; never implied by `--yes`. |
| `-y`, `--yes` | Assume "yes" for installer confirmations. |
| `--dry-run` | Plan every step and change nothing (no commands run, no manifest written). |
| `--repair` | Re-check and repair an existing installation (same as `avdfleet repair`). |
| `--resume` | Resume after the reboot requested by the previous run. |
| `--no-appium` | Skip Node/Appium/Inspector. |
| `--no-studio` | Skip Android Studio configuration. |
| `--no-scrcpy` | Skip scrcpy. |
| `--sdk-root PATH` | Install/repair the SDK at an absolute path; exports `ANDROID_SDK_ROOT`/`ANDROID_HOME` for the run. |
| `--avd-home PATH` | Shared AVD home directory (`ANDROID_AVD_HOME`); created before use. |
| `--images LIST` | System-image package ids to download after the main install (repeatable/comma-separated). |
| `--proxies FILE` | Proxy URL list (one per line) used to seed the encrypted proxy pool after install. |
| `--download-proxy URL` | Install-time HTTP(S) proxy for SDK/npm/pip/brew downloads; used for this run only, password masked in output and never persisted. |
| `--only ID` | Only these step ids/phases/prefixes (repeatable/comma-separated). `tools`, `sdk`, `config`, `acceleration`, `core` select a phase. |
| `--platform-provider NAME` | Force a platform provider: `windows`, `macos`, `debian`, `fedora`, `arch`. |
| `--json` | Machine-readable output. |

### `avdfleet repair`

`avdfleet repair [--yes|--dry-run|--accept-android-sdk-licenses|--sdk-root]` is
a convenience wrapper for `avdfleet install --repair`: it re-checks every step,
repairs missing/broken components and leaves satisfied steps alone. It is safe to
run repeatedly.

### `avdfleet status`

Prints host facts, the SDK/data locations and the persisted install manifest
summary (steps, statuses, timestamps, reboot pending). `--json` for machines.

### `avdfleet uninstall`

```bash
avdfleet uninstall --dry-run
avdfleet uninstall --yes
avdfleet uninstall --yes --keep-data
```

Removes installer-managed files and environment changes. `--keep-data` keeps
the data directory (AVDs, database, logs, snapshots). It only deletes known
AVD Fleet directories, never a computed path it has not resolved and checked.

## Dry run, plan and idempotency

`--dry-run` prints the ordered plan (step ids, titles, phases, whether the step
is already satisfied, whether a reboot may be required) and executes nothing.
Every step's `check` is run first; an install on a satisfied host is a fast
no-op (`avdfleet install` re-checks and exits without reinstalling). The engine
never reboots the host and never downloads anything outside its own steps.

## Manifest, checkpoints and reboot/resume

The install manifest is the engine's checkpoint. It lives at
`<data>/install-manifest.json` (Windows
`%LOCALAPPDATA%\avdfleet\install-manifest.json`) and is written atomically after
every step. It records the host, SDK root, avdfleet version, step results,
statuses and timestamps. Secrets are never written to it; captured output is
redacted.

When a step requires a reboot (typically enabling WHPX), the engine:

1. records the step as `reboot_required`;
2. exits with **code `3010`** (`avdfleet install`/`repair` only — other commands
   use `6` for a reboot requirement);
3. prints the exact resume command.

After the reboot run the same bootstrap command again (it re-invokes
`avdfleet install`), or run `avdfleet install --resume` with the same flags. The
engine re-runs the rebooted step's check, sees it satisfied and continues; it
does not repeat completed steps. A stale `reboot_required` entry is handled by
`--resume`; if a checkpoint is wrong, `AVDFLEET_IGNORE_REBOOT_MARKER=1` is a
diagnostic escape hatch (do not use it to skip a genuinely needed reboot).

Bootstrap exit codes: `0` success, `1` unexpected, `2` usage, `3` unsupported
host, `4` prerequisite/network/checksum failure, `5` uv/tool installation
failure, `6` reboot required (`install.sh`; Windows returns `3010`), `N`
propagated from `avdfleet install`/`doctor`.

## Licences

Android SDK licences are accepted only when `--accept-android-sdk-licenses` is
passed. The bootstrap passes it through; there is no default `yes |` pipe. Google
Play/Google APIs image terms are surfaced as part of the SDK licence review; AVD
Fleet does not accept third-party terms on your behalf, and Google's SDK licence
does not permit redistributing Google components — relevant to the portable
edition (see [portable.md](portable.md)).

## Environment and configuration

The engine reads the normal configuration precedence: defaults < `config.toml` <
`AVDFLEET_*` environment variables < explicit flags. Relevant variables include
`AVDFLEET_DATA_DIR`, `AVDFLEET_CONFIG_DIR`, `AVDFLEET_SDK_ROOT`,
`AVDFLEET_AVD_HOME`, `AVDFLEET_PACKAGE_SPEC`, `AVDFLEET_VERSION`,
`AVDFLEET_REINSTALL`, `UV_VERSION`, `UV_INSTALLER_URL`, `UV_INSTALLER_SHA256`,
`AVDFLEET_MIN_FREE_GB` and `AVDFLEET_ALLOW_UNSUPPORTED_HOST`. The complete list
and the bootstrap-only variables are in [bootstrap/README.md](../bootstrap/README.md).

## Acceleration behaviour per host

| Host | Engine action |
|---|---|
| Windows | Detect WHPX; enable `HypervisorPlatform` (reboot), migrate AEHD → WHPX if an old AEHD installation is found. AEHD is sunsetting (2026-12-31), so WHPX is the default path. |
| macOS | Detect HVF (built in; no setup). Intel Macs use HVF too. |
| Linux | Check `/dev/kvm`, group membership and udev rules; print the exact `usermod`/udev fix and require a logout/reboot so it takes effect. |
| Any nested VM / cloud host | Report nested virtualisation and its limits; the Android emulator cannot be accelerated inside most VMs. Bootstrap refuses WSL/ARM64 up front. |

`avdfleet doctor` verifies the same facts after installation; the symptom table
in [troubleshooting.md](troubleshooting.md) covers each failure mode.

## Security notes

- Bootstrap downloads are HTTPS-only; the uv installer is downloaded to a
  private temp directory, optionally SHA-256 verified and then executed. No
  `curl | sh` pipeline without checksum verification where upstream publishes
  one.
- Package-manager commands use argv arrays where possible; elevation prints the
  exact command instead of silently re-launching.
- Custom image import validates paths, rejects symlink escapes and never
  overwrites without `--force` (see [testing.md](testing.md) and the import
  design in `research/R6-images.md`).
- `avdfleet uninstall` never recursively deletes computed paths without
  resolving and checking the target.
