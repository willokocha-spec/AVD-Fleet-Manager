# AVD Fleet bootstrap installers

One-command bootstrap for the **installed edition** of AVD Fleet on Windows, macOS and
Linux. The bootstrap scripts are deliberately thin: they detect the host, install
[`uv`](https://docs.astral.sh/uv/), install the `avdfleet` tool, and then hand over to the
AVD Fleet installer engine (`avdfleet install`), which owns every Android SDK, JDK, Node,
Appium, emulator and acceleration step. Finish with `avdfleet doctor`.

```
bootstrap -> uv -> uv tool install avdfleet -> avdfleet install -> avdfleet doctor
```

This directory also contains the shared static test-suite for the bootstrap scripts. The
tests inspect the scripts; they never execute an installation.

## Quick start

### macOS and Linux

```bash
chmod +x bootstrap/install.sh
./bootstrap/install.sh --accept-android-sdk-licenses --yes
```

`sudo` is required for the system-level steps; the installer asks for it when needed.

### Windows (PowerShell)

From an elevated PowerShell prompt (recommended):

```powershell
powershell -ExecutionPolicy Bypass -File .\bootstrap\install.ps1 -AcceptAndroidSdkLicenses -Yes
```

Windows may reboot once (WHPX/AEHD acceleration). Reboot, then run the same command again:
the installer resumes from its checkpoint (`install-manifest.json`) and does not start over.
For a fully unattended reboot flow, run this bootstrap command again from an
auto-start-at-logon mechanism after the reboot, or simply re-run it manually.

Both commands are idempotent: a second run only repeats the installer engine's cheap
self-checks and `avdfleet doctor`.

## Prerequisites

| Host | Requirements |
|---|---|
| macOS | Apple silicon (recommended) or Intel; macOS 13+; admin rights for system packages; ~15 GB free disk. |
| Linux | x86_64 only; Ubuntu LTS, Debian, Fedora, Rocky/RHEL (or another well-known distro); `sudo` (or root); CPU virtualisation enabled and `/dev/kvm` available; ~15 GB free disk. |
| Windows | Windows 10 build 19041+ or Windows 11, x86_64 only; PowerShell 5.1 or PowerShell 7; administrator rights recommended; hardware virtualisation enabled (WHPX/AEHD); ~15 GB free disk. |

All hosts need outbound HTTPS to PyPI (`pypi.org`, `files.pythonhosted.org`), Astral
(`astral.sh`) and the Android SDK/dl.google.com hosts that `avdfleet install` uses. If your
network requires a proxy, configure `HTTPS_PROXY`/`HTTP_PROXY` first.

Unsupported by design (the Android emulator cannot run there): WSL, Windows on ARM64,
Linux on ARM64 and any non-Linux/non-macOS POSIX host. The bootstrap refuses these with an
explanation and exit code `3` instead of installing a tool that cannot work. Setting
`AVDFLEET_ALLOW_UNSUPPORTED_HOST=1` after configuration can be used to attempt a CLI-only
install; it is not a supported configuration.

## Flags

### `install.sh` (macOS/Linux)

| Flag | Meaning |
|---|---|
| `--accept-android-sdk-licenses` | Accept the Android SDK licenses unattended. Required for a non-interactive install; forwarded to `avdfleet install`. |
| `-y`, `--yes` | Assume "yes" for installer questions; forwarded to `avdfleet install`. |
| `--dry-run` | Print every planned action and change nothing. If `avdfleet` is already installed, its own `avdfleet install --dry-run` plan is shown; that command is documented as writing nothing. |
| `--no-appium` | Skip the Appium server and driver steps; forwarded to `avdfleet install`. |
| `--sdk-root PATH` | Install the Android SDK under `PATH` (must be absolute); forwarded to `avdfleet install` and exported as `ANDROID_SDK_ROOT`/`ANDROID_HOME` for this run. |
| `--verbose` | Verbose bootstrap diagnostics (does not change the `avdfleet` log level). |
| `-h`, `--help` | Show usage and exit `0`. |
| `--version` | Show bootstrap, `uv` and `avdfleet` versions and exit `0`. |

Unknown flags and stray arguments are rejected with exit code `2`.

### `install.ps1` (Windows)

| Parameter | Meaning |
|---|---|
| `-AcceptAndroidSdkLicenses` | Accept the Android SDK licenses unattended; forwarded to `avdfleet install`. |
| `-Yes` | Assume "yes" for installer questions; forwarded to `avdfleet install`. |
| `-DryRun` | Print every planned action and change nothing. |
| `-NoAppium` | Skip Appium; forwarded to `avdfleet install`. |
| `-SdkRoot PATH` | Android SDK directory (absolute); forwarded and exported as `ANDROID_SDK_ROOT`/`ANDROID_HOME`. |
| `-Verbose` | Verbose bootstrap diagnostics. `install.ps1` is a simple script, so this is a normal switch, not a common parameter. |
| `-Version` | Show bootstrap, `uv` and `avdfleet` versions and exit `0`. |
| `-Help` | Show usage and exit `0`. |

Unknown arguments are rejected with exit code `2`. The preferred invocation is
`powershell -ExecutionPolicy Bypass -File .\bootstrap\install.ps1 ...` (or `pwsh` on
PowerShell 7); see [Execution policy](#execution-policy-windows) below.

## Environment overrides

Both scripts honour the same variables, so a documented invocation works on every host:

| Variable | Purpose |
|---|---|
| `AVDFLEET_PACKAGE_SPEC` | Package spec for `uv tool install`. Default `avdfleet`. Supports a local wheel (`/tmp/avdfleet-0.1.0-py3-none-any.whl`), a git URL (`git+https://...`) or a pinned spec (`avdfleet==0.1.0`). A local path that does not exist is rejected before anything is installed. |
| `AVDFLEET_VERSION` | Desired version (e.g. `0.1.1`). Pins the default package spec and makes a reinstall with `--force` happen only when the installed version differs. |
| `AVDFLEET_REINSTALL=1` | Force `uv tool install --force` even when a version is already installed. |
| `AVDFLEET_PYTHON` | Optional interpreter for the tool environment; passed to `uv tool install --python`. |
| `UV_VERSION` | Pin the uv installer URL: `https://astral.sh/uv/<UV_VERSION>/install.{sh,ps1}`. |
| `UV_INSTALLER_URL` | Override the uv installer URL. Must be `https://`; the scripts refuse plain HTTP. |
| `UV_INSTALLER_SHA256` | Expected SHA-256 of the downloaded uv installer (64 hex characters). Recommended for reproducible installs; see [Supply-chain safety](#supply-chain-safety). |
| `AVDFLEET_UV_INSTALLER_CHECKSUM` | Alias for `UV_INSTALLER_SHA256` for automation. |
| `AVDFLEET_DATA_DIR` | Data directory used to locate the installer checkpoint for reboot detection. |
| `AVDFLEET_IGNORE_REBOOT_MARKER=1` | Do not treat an incomplete `install-manifest.json` as a pending reboot (use only if a stale checkpoint is wrongly reported). |
| `AVDFLEET_MIN_FREE_GB` | Free-disk warning threshold in GB (default 10). |
| `AVDFLEET_ALLOW_UNSUPPORTED_HOST=1` | Attempt a CLI-only install on an emulator-unsupported OS/architecture. |

## Supply-chain safety

The bootstrap never pipes a network response into a shell. Specifically:

- `install.sh` downloads the official uv installer to a private temporary directory
  (`mktemp -d`, mode `0700`), optionally verifies its SHA-256, and only then runs it with
  `sh`. Without `UV_INSTALLER_SHA256` it prints the computed digest and a clear warning,
  because Astral's checksums are published for versioned installer URLs.
- `install.ps1` downloads the official uv installer to a temporary file and runs it in a
  child PowerShell process. It prefers `winget` (which verifies package hashes as part of
  its signed package source) when available, then the downloaded installer, then
  `python -m pip install --user uv`. There is no `irm ... | iex` download-and-execute path.
- All downloads are HTTPS-only. On Linux/macOS the scripts use
  `curl --proto '=https' --tlsv1.2` or `wget --https-only --secure-protocol=TLSv1_2`; on
  Windows they refuse any non-`https://` URL before calling `Invoke-WebRequest`.
- Remaining installs (Android SDK tools, system images, Node, npm packages) are performed by
  the AVD Fleet installer engine with its own checksum verification.

To re-run with a verified uv installer:

```bash
# Look up the SHA-256 published by Astral for the pinned installer release, then:
UV_VERSION=0.5.11 UV_INSTALLER_SHA256=<sha256> \
  ./bootstrap/install.sh --accept-android-sdk-licenses --yes
```

The `bootstrap` scripts do not read, store or print secrets. Proxy credentials in
`HTTPS_PROXY` are inherited only by the installer processes, exactly as any other command.

## What the bootstrap does

1. Parse and validate flags; reject unknown options and invalid paths.
2. Detect host OS, distribution and architecture. Refuse emulator-unsupported hosts (WSL,
   and ARM64 machines) with exit code `3` and an explanation.
3. Check for `curl`/`wget`, `sudo` (Linux) and free disk space, and print acceleration hints
   (KVM on Linux, HVF on macOS, WHPX/AEHD on Windows).
4. Find or install `uv` (see [Supply-chain safety](#supply-chain-safety)). `$HOME/.local/bin`
   is prepended to `PATH` for the current process only; shell profiles are not edited.
5. Install the `avdfleet` tool with `uv tool install --force`. The `--force` only runs when
   the tool is missing or a different/explicitly requested version is needed; an
   already-satisfying install is skipped, so a second bootstrap run is a no-op.
6. Run `avdfleet install` with the forwarded flags and stream its output.
7. If the engine reports a reboot (exit code `3010`, or a pending
   `reboot_required` step in `install-manifest.json`), print the exact resume command and
   stop. This is not a failure: re-run the same command after the reboot.
8. Run `avdfleet doctor` and report its exit code. On failure the logs are kept and their
   paths are printed.

The scripts do not create, modify or delete Android SDK content themselves; that is the
installer engine's job (idempotent, checkpointed, dry-runnable).

## Reboot and resume

- **Windows**: enabling WHPX may require one reboot. The engine exits with code `3010` and
  persists its checkpoint. Reboot, open PowerShell, and run exactly the command printed by
  `install.ps1` (it includes your original flags). The engine resumes from
  `%LOCALAPPDATA%\avdfleet\install-manifest.json`.
- **Linux**: KVM group membership and udev rules take effect after a logout/login or a
  reboot. If the engine requests a reboot it prints the same resume instructions.
- **macOS**: normally no reboot is needed.

To resume with your own CLI invocation instead, run `avdfleet install` with the same flags;
the engine detects the checkpoint. `install.sh` exits with code `6` when a reboot is
required (POSIX shells cannot represent `3010`; the Windows script exits with the real
`3010`).

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success: AVD Fleet installed and `avdfleet doctor` passed. |
| `1` | Unexpected bootstrap failure. |
| `2` | Usage error: unknown option, missing value, non-absolute `--sdk-root`/`-SdkRoot`, missing local package spec. |
| `3` | Unsupported OS or architecture (WSL, ARM64, non-Linux/macOS/Windows). |
| `4` | Prerequisite failure: no `curl`/`wget`, no `sudo`, HTTPS/checksum/download failure. |
| `5` | Could not install `uv`, or the `avdfleet` executable was not found afterwards. |
| `6` | Reboot required (`install.sh` only; the Windows script returns `3010`). |
| `N` | Exit code propagated from `avdfleet install` or `avdfleet doctor`. |

## Execution policy (Windows)

`install.ps1` is an unsigned script from this repository. The recommended invocation grants
a one-off bypass:

```powershell
powershell -ExecutionPolicy Bypass -File .\bootstrap\install.ps1 -AcceptAndroidSdkLicenses -Yes
# PowerShell 7:
pwsh -ExecutionPolicy Bypass -File .\bootstrap\install.ps1 -AcceptAndroidSdkLicenses -Yes
```

If you prefer a permanent setting, use `Set-ExecutionPolicy -Scope CurrentUser
-ExecutionPolicy RemoteSigned` and sign the repository's scripts in managed environments.
If the repository zip was downloaded from the internet, Windows may set Mark-of-the-Web;
unblock it once with:

```powershell
Unblock-File -LiteralPath .\bootstrap\install.ps1
```

The script checks for the Mark-of-the-Web (alternate data stream `Zone.Identifier`) and
warns with this command.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `unknown option` / `unknown argument` | Check the spelling against the flags table; run `install.sh --help` or `install.ps1 -Help`. |
| `unsupported operating system`/`architecture` | The Android emulator needs a supported accelerated host. Use an x86_64 Linux/Windows machine, Apple silicon macOS, or the portable edition on a supported machine. WSL and ARM64 are refused by design. |
| `sudo is required` (Linux) | Run from an account with `sudo`, or as root. |
| `no /dev/kvm` or KVM warning | Enable CPU virtualisation (VT-x/AMD-V, and nested virtualisation if this host is a VM). The installer reports details; `avdfleet doctor` verifies. |
| SDK licenses prompt / install stalls | Pass `--accept-android-sdk-licenses` (Linux/macOS) or `-AcceptAndroidSdkLicenses` (Windows); the license step only runs unattended with this flag. |
| `UV_INSTALLER_SHA256 must be ...` / checksum mismatch | Provide the full 64-character hex digest for the exact installer URL. A mismatch means a different file was served (wrong version, proxy or tampering); do not ignore it. |
| `could not install uv` | Install uv manually from <https://docs.astral.sh/uv/getting-started/installation/> and re-run, or fix the network/proxy. On PEP 668 Linux distributions, use the official installer rather than `pip --user`. |
| `avdfleet was installed ... no executable found` | `$HOME/.local/bin` (Unix) or `%USERPROFILE%\.local\bin` (Windows) is not on `PATH`. Add it and re-run. |
| `avdfleet install failed with exit code N` | Read the log path printed above the error and run `avdfleet doctor`. Common causes: no network, no admin/sudo, virtualisation disabled, missing license flag. |
| Reboot message repeats after a reboot | Check that WHPX/AEHD actually enabled (Windows) or that `/dev/kvm` is accessible; inspect `install-manifest.json`. Only if the checkpoint is stale, set `AVDFLEET_IGNORE_REBOOT_MARKER=1`. |
| `avdfleet doctor` fails after a successful install | Run `avdfleet doctor --fix`, then re-run the bootstrap; doctor prints actionable checks (SDK paths, KVM/WHPX, ports, Node, Appium). |
| Execution policy error on Windows | Use the `-ExecutionPolicy Bypass -File` invocation and `Unblock-File`; see [Execution policy](#execution-policy-windows). |
| Corporate proxy | Set `HTTPS_PROXY`/`HTTP_PROXY` (and `NO_PROXY` for localhost) before running. The bootstrap and the installer engine both honour them. |

## Uninstalling the bootstrap layers

```bash
avdfleet uninstall --yes        # remove the Android SDK/toolchain via the installer engine
uv tool uninstall avdfleet      # remove the CLI itself
```

Keep `%LOCALAPPDATA%\avdfleet` / `~/.local/share/avdfleet` if you want to preserve state;
remove it for a completely clean host.

## Developing and testing the bootstrap scripts

```bash
python -m pytest bootstrap/test_bootstrap.py -q
```

The tests are static/content checks (flags, safety invariants, shebang/line endings,
PowerShell 5.1 compatibility, README coverage). They never install software. If `bash` or
PowerShell is available, the suite additionally runs `bash -n` on `install.sh` and invokes
the PowerShell parser on `install.ps1`; otherwise those checks are skipped.

Known limitations of this bootstrap layer:

- Network access to PyPI/Astral and the Android SDK hosts is required; there is no offline
  bootstrap (use the portable edition for offline machines).
- WSL and ARM64 hosts are refused rather than partially configured.
- The scripts do not edit shell profiles; the `PATH` note they print is the only step that
  needs to be made permanent by the operator.
- Real end-to-end install coverage is owned by `tests/installer` (container/VM tests);
  the bootstrap tests here are static by design so they are safe to run anywhere.
