# Quick start — Windows 11

This page takes a clean Windows 11 x64 machine with administrator rights to a
running AVD Fleet daemon and web GUI. It uses the **installed edition**; for the
no-admin USB edition see [portable.md](portable.md).

> Architecture note: the Android emulator has no supported Windows ARM64 build.
> AVD Fleet refuses Windows-on-ARM at bootstrap (exit code `3`) instead of
> installing software that cannot boot an AVD. Windows 10 x64 (1803+) is also
> supported; Windows 11 is the recommended target.

## 1. Prerequisites

| Item | Requirement |
|---|---|
| OS / architecture | Windows 11 (recommended) or Windows 10 1803+, **x86_64 only** |
| CPU | Intel VT-x or AMD-V enabled in firmware (WHPX needs VT-x + EPT + Unrestricted Guest, or AMD-V) |
| Hypervisor | **Windows Hypervisor Platform (WHPX)** enabled and rebooted |
| RAM | 16 GB minimum for a small fleet (32 GB+ recommended; budget ~4 GB per running AVD) |
| Disk | ~15 GB free for the toolchain, plus system images (each can be 2–8 GB) and AVD data |
| Shell | PowerShell 5.1 (built in) or PowerShell 7 (`pwsh`) |
| Network | Outbound HTTPS to `pypi.org`, `files.pythonhosted.org`, `astral.sh` and the Android SDK hosts (`dl.google.com`) |
| Account | Administrator rights recommended (WHPX enablement, system-wide PATH and SDK locations) |

The legacy AEHD (Android Emulator hypervisor driver) path is sunsetting
(2026-12-31). AVD Fleet defaults to WHPX; use the AEHD step only for migrations
of existing installations (`avdfleet install --only accel.aehd`).

### Enable WHPX (one time, needs a reboot)

```powershell
# Elevated PowerShell
Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -All
# or: Turn Windows features on or off -> Windows Hypervisor Platform
Restart-Computer
```

Verify before installing (optional):

```powershell
systeminfo | Select-String "Hyper-V"
Get-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform
```

## 2. Install

From the repository checkout, in an elevated PowerShell:

```powershell
# the package is not on PyPI yet: build a wheel and point the bootstrap at it
uv build --wheel
$env:AVDFLEET_PACKAGE_SPEC = "$PWD\dist\avdfleet-0.1.0-py3-none-any.whl"
powershell -ExecutionPolicy Bypass -File .\bootstrap\install.ps1 -AcceptAndroidSdkLicenses -Yes
```

`AVDFLEET_PACKAGE_SPEC` also accepts a git URL or a pinned version; a local path
that does not exist is rejected before anything is installed
(see [bootstrap/README.md](../bootstrap/README.md)).

What happens, in order: the bootstrap validates flags and the host, finds or
installs `uv` (HTTPS only, checksum verified when `UV_INSTALLER_SHA256` is set),
installs the `avdfleet` tool, runs `avdfleet install`, then
`avdfleet doctor`. It never pipes a downloaded script into a shell.

All installer flags are documented in [installer.md](installer.md). Frequently
used bootstrap parameters: `-NoAppium`, `-SdkRoot D:\Android\Sdk`, `-DryRun`,
`-Version`, `-Help`.

### Reboot and resume

Enabling WHPX can require exactly one reboot. When it does, the installer exits
with code `3010` and writes its checkpoint to
`%LOCALAPPDATA%\avdfleet\install-manifest.json`. Reboot, open PowerShell and run
the **same command again** — the installer resumes (`avdfleet install --resume`
is the underlying command) and does not start over. If you prefer to drive the
resume manually:

```powershell
avdfleet install --resume --accept-android-sdk-licenses --yes
```

A second bootstrap run on an already-complete installation is a no-op: it only
re-runs the cheap checks and `avdfleet doctor`.

## 3. Verify

```powershell
avdfleet doctor
avdfleet doctor --slow        # adds version/port/clock checks
avdfleet status               # host facts, SDK paths, installer manifest
```

`avdfleet doctor` exits `0` only when no check has status `fail`. Warnings
(`warn`) are allowed but should be read. `avdfleet doctor --fix <check-id>`
applies safe, non-destructive fixes (`--dry-run` prints them first).

## 4. Run the GUI

```powershell
avdfleet serve
# opens http://127.0.0.1:8765/ in the default browser
```

The daemon serves the REST API, WebSockets and the built React GUI from one
loopback-only process. Useful options: `--port 9000`, `--no-open`. The bearer
token is created on first start at `%LOCALAPPDATA%\avdfleet\token`
(owner-only ACL; `AVDFLEET_TOKEN` overrides it). The login screen accepts that
token.

## 5. First fleet (GUI or CLI)

The GUI workflow is the same as the CLI workflow:

1. **Images** — download a Google Play or Google APIs system image for `x86_64`
   that your host can accelerate.
2. **Templates** — create a template from that image (`pixel_7`, 2048 MB RAM if
   API ≤ 36; API 37+ needs ≥ 4096 MB).
3. **Fleet** — deploy N instances from the template, start them and wait for
   `running`.
4. **Proxies** — add authenticated HTTP/SOCKS5 proxies and assign one per
   instance (see [proxy.md](proxy.md)).
5. **Scripts** — run an adb-shell, host-Python or Appium script across the
   fleet (see [scripting.md](scripting.md)).

The equivalent CLI:

```powershell
avdfleet images list --abi x86_64
avdfleet images download "system-images;android-35;google_apis_playstore;x86_64"
avdfleet avd templates create --name pixel-api35 --image "system-images;android-35;google_apis_playstore;x86_64"
avdfleet avd deploy <template-id> --count 5 --prefix avd
avdfleet instances list
avdfleet instances start avd-01
avdfleet instances wait-boot avd-01
```

The complete command reference is [cli.md](cli.md); the API is
[api.md](api.md); declarative fleets are [fleetspec.md](fleetspec.md).

## 6. Acceptance self-test

```powershell
avdfleet selftest --e2e
```

This runs the BRIEF §7 sequence against your real toolchain and prints a
machine-readable report (`--json`). Add `--require-acceleration` to fail when
WHPX is unavailable, `--count 5`, and `--custom-image <dir>` to also exercise
custom-image import. On this repository's build host (no hypervisor) it reports
the real-emulator items as `SKIP`; see [acceptance.md](acceptance.md).

## 7. Data locations

| Purpose | Default path |
|---|---|
| Data (database, token, installer manifest, AVDs, SDK) | `%LOCALAPPDATA%\avdfleet` |
| Configuration (`config.toml`) | `%APPDATA%\avdfleet` |
| Cache and downloads | `%LOCALAPPDATA%\avdfleet\Cache` |
| Logs | `%LOCALAPPDATA%\avdfleet\Logs` |

These are overridable with `AVDFLEET_DATA_DIR`, `AVDFLEET_CONFIG_DIR`,
`AVDFLEET_CACHE_DIR`, `AVDFLEET_LOG_DIR`, `AVDFLEET_SDK_ROOT` and
`AVDFLEET_AVD_HOME`; see [installer.md](installer.md) and
[compatibility.md](compatibility.md).

## 8. Uninstall or repair

```powershell
avdfleet repair --yes                          # re-check and repair the toolchain
avdfleet uninstall --dry-run                   # show what would be removed
avdfleet uninstall --yes                       # remove installer-managed files
avdfleet uninstall --yes --keep-data           # keep AVDs, database, logs
uv tool uninstall avdfleet                     # remove the CLI itself
```

If Windows blocks the unsigned bootstrap script, see the execution-policy and
Mark-of-the-Web notes in [bootstrap/README.md](../bootstrap/README.md) and the
symptom table in [troubleshooting.md](troubleshooting.md).

## Next steps

- [installer.md](installer.md) — steps, flags, manifest, reboot/resume, exit codes
- [troubleshooting.md](troubleshooting.md) — WHPX, license, path and port symptoms
- [compatibility.md](compatibility.md) — exact host/version support matrix
