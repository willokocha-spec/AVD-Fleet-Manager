# Quick start — macOS (Apple Silicon)

This page takes a clean Apple Silicon Mac to a running AVD Fleet daemon and web
GUI using the **installed edition**. For the no-admin USB edition see
[portable.md](portable.md).

> Apple Silicon accelerates **`arm64-v8a`** system images only. Intel Macs
> (`x86_64`) are still supported by the emulator but are on a deprecation path;
> on Intel hosts use `x86_64` images. AVD Fleet detects the native architecture
> and filters the image catalogue accordingly.

## 1. Prerequisites

| Item | Requirement |
|---|---|
| OS | macOS 12+ (Studio minimum); macOS 13+ recommended. Homebrew requires macOS 15+, so AVD Fleet does not depend on Homebrew. |
| CPU / hypervisor | Apple M1 or newer; **Hypervisor.framework (HVF)** is built in and needs no setup |
| RAM | 16 GB minimum (32 GB+ recommended; ~4 GB per running AVD, API 37+ enforces 4 GB) |
| Disk | ~15 GB free for the toolchain, plus system images (2–8 GB each) and AVD data |
| Xcode | **Xcode Command Line Tools** (`xcode-select --install`) before any compiler/Homebrew work |
| Network | Outbound HTTPS to `pypi.org`, `files.pythonhosted.org`, `astral.sh`, `dl.google.com` |
| Account | Administrator rights for system-level steps |

Install the Command Line Tools first (a GUI prompt appears; accept it):

```bash
xcode-select --install
xcode-select -p          # should print a path after installation
```

## 2. Install

```bash
cd /path/to/Android        # repository checkout
# the package is not on PyPI yet: build a wheel and point the bootstrap at it
uv build --wheel
export AVDFLEET_PACKAGE_SPEC="$PWD/dist/avdfleet-0.1.0-py3-none-any.whl"
chmod +x bootstrap/install.sh
./bootstrap/install.sh --accept-android-sdk-licenses --yes
```

The installer asks for `sudo` only for system-level steps. It finds or installs
`uv` over HTTPS, installs the `avdfleet` tool, runs `avdfleet install`, then
`avdfleet doctor`. Homebrew is not required.

Useful options: `--no-appium`, `--sdk-root /Users/you/Android/Sdk`,
`--dry-run`, `--verbose`, `--version`. Unknown flags are rejected with exit
code `2`. The full flag table is in [installer.md](installer.md).

### Reboot and resume

macOS normally needs no reboot. If the installer ever reports a pending reboot
(exit code `6` from the bootstrap), reboot and re-run the same command; the
installer resumes from
`~/Library/Application Support/avdfleet/install-manifest.json`.

### Gatekeeper and quarantine

If you downloaded the repository as a ZIP or copied it from another Mac, macOS
may set the quarantine attribute on the scripts:

```bash
xattr -dr com.apple.quarantine ./bootstrap
xattr -dr com.apple.quarantine /Applications/AVD-Fleet-Portable   # portable bundle
```

You can also approve the app/launcher in **System Settings → Privacy &
Security → Open Anyway**. A signed/notarised installer is a release task; the
repository scripts are unsigned.

## 3. Verify

```bash
avdfleet doctor
avdfleet doctor --slow
avdfleet status
```

No HVF setup is required: the emulator uses the framework automatically. The
`hypervisor` check should report `hvf` and `available: true`.

## 4. Run the GUI

```bash
avdfleet serve
open http://127.0.0.1:8765/
```

`serve` opens the GUI in your browser automatically; `--no-open --port 9000`
avoid that/change the port. The bearer token is created on first start at
`~/Library/Application Support/avdfleet/token` with owner-only permissions
(`AVDFLEET_TOKEN` overrides it).

## 5. First fleet

In the GUI: **Images → Templates → Fleet → Proxies → Scripts** (the same order
as the CLI below). Only `arm64-v8a` images are shown on Apple Silicon.

```bash
avdfleet images list --abi arm64-v8a
avdfleet images download "system-images;android-35;google_apis_playstore;arm64-v8a"
avdfleet avd templates create --name pixel-api35 --image "system-images;android-35;google_apis_playstore;arm64-v8a"
avdfleet avd deploy <template-id> --count 5 --prefix avd
avdfleet instances start avd-01
avdfleet instances wait-boot avd-01
```

See [cli.md](cli.md) for every command, [api.md](api.md) for the REST/WS API and
[proxy.md](proxy.md) for per-instance proxy isolation.

## 6. Acceptance self-test

```bash
avdfleet selftest --e2e
```

Runs the BRIEF §7 sequence on the real toolchain and prints the report
(`--json` for machines). `--require-acceleration` turns a missing HVF into exit
`3`; `--custom-image <dir>` exercises custom-image import. On a host without a
hypervisor the real-emulator items are reported as `SKIP`; see
[acceptance.md](acceptance.md).

## 7. Data locations

| Purpose | Default path |
|---|---|
| Data (database, token, manifest, AVDs, SDK) | `~/Library/Application Support/avdfleet` |
| Configuration (`config.toml`) | `~/Library/Application Support/avdfleet` |
| Cache and downloads | `~/Library/Caches/avdfleet` |
| Logs | `~/Library/Logs/avdfleet` |

Override with `AVDFLEET_DATA_DIR`, `AVDFLEET_CONFIG_DIR`,
`AVDFLEET_CACHE_DIR`, `AVDFLEET_LOG_DIR`, `AVDFLEET_SDK_ROOT`,
`AVDFLEET_AVD_HOME`. A config file can also be placed at
`./avdfleet.toml` or given with `--config`.

## 8. Uninstall or repair

```bash
avdfleet repair --yes
avdfleet uninstall --dry-run
avdfleet uninstall --yes                 # remove installer-managed files
avdfleet uninstall --yes --keep-data     # keep AVDs, database, logs
uv tool uninstall avdfleet               # remove the CLI
```

## Next steps

- [installer.md](installer.md) — steps, flags, manifest, exit codes
- [troubleshooting.md](troubleshooting.md) — HVF, quarantine, license and path symptoms
- [compatibility.md](compatibility.md) — exact host/version support matrix
