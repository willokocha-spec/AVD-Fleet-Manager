# Quick start — Linux (Ubuntu/Debian, Fedora, Rocky/RHEL, Arch)

This page takes a clean x86_64 Linux host with `sudo` to a running AVD Fleet
daemon and web GUI using the **installed edition**. For the no-admin USB
edition see [portable.md](portable.md).

> Linux ARM64 is **not supported**: the Android emulator has no stable
> `linux_aarch64` host build. WSL2 and containers also cannot run the
> hardware-accelerated emulator; AVD Fleet refuses them at bootstrap (exit
> code `3`) and `doctor` reports the reason.

## 1. Prerequisites

| Item | Requirement |
|---|---|
| Architecture | `x86_64` only |
| Distribution | Ubuntu LTS, Debian 11+, Fedora (current), Rocky/RHEL **9+**, Arch Linux. glibc **≥ 2.31** (Rocky/RHEL 8 has 2.28 → refused) |
| Virtualisation | Intel VT-x or AMD-V enabled in BIOS/firmware; `/dev/kvm` present and accessible |
| RAM | 16 GB minimum (32 GB+ recommended; ~4 GB per running AVD; API 37+ enforces 4 GB) |
| Disk | ~15 GB free for the toolchain, plus images (2–8 GB each) and AVD data |
| Account | `sudo` or root for system-level steps |
| Network | Outbound HTTPS to `pypi.org`, `files.pythonhosted.org`, `astral.sh`, `dl.google.com` |

## 2. Prepare KVM and packages

Check acceleration:

```bash
ls -l /dev/kvm
sudo apt-get install -y cpu-checker && sudo kvm-ok     # Debian/Ubuntu
groups | tr ' ' '\n' | grep -x kvm                     # is your user in kvm?
```

If `/dev/kvm` is missing, enable VT-x/AMD-V (and nested virtualisation if this
host is itself a VM) — the emulator has no usable software fallback. If it
exists but you get "Permission denied", add your user to the `kvm` group and log
out/in (or reboot):

```bash
sudo usermod -aG kvm "$USER"     # log out and back in afterwards
# alternative for CI/containers: a udev rule
echo 'KERNEL=="kvm", GROUP="kvm", MODE="0660"' | sudo tee /etc/udev/rules.d/99-kvm.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Distro package guidance (AVD Fleet installs the SDK tools itself; these are
host libraries the emulator and SDK tools need):

| Distro | Command |
|---|---|
| Ubuntu / Debian | `sudo apt-get install -y qemu-kvm libvirt-daemon-system libvirt-clients bridge-utils cpu-checker libnss3 libpulse0 libgtk-3-0 libvulkan1 libx11-6 libxcomposite1 libxcursor1 libxi6 libxrandr2 libglu1-mesa zlib1g` |
| Fedora | `sudo dnf install -y qemu-kvm libvirt-daemon-kvm libvirt-client libnss3 pulseaudio-libs gtk3 vulkan-loader mesa-libGLU libX11 libXcomposite libXcursor libXi libXrandr zlib` |
| Rocky / RHEL 9 | `sudo dnf install -y qemu-kvm libvirt-daemon-kvm libvirt-client libnss3 pulseaudio-libs gtk3 vulkan-loader mesa-libGLU libX11 libXcomposite libXcursor libXi libXrandr` (EPEL may be needed for some GUI libraries) |
| Arch | `sudo pacman -S --needed qemu-base libvirt android-tools android-udev scrcpy jdk21-openjdk nodejs` |

The KVM package sets are verified from the Android acceleration page; the exact
GUI library set for the emulator is not published by Google and is therefore
"best effort" — run the emulator once and check `ldd` if a library is missing.
AVD Fleet does **not** require the Android udev rules (real USB devices) for
AVD fleets; those are only needed for physical device debugging.

## 3. Install

```bash
cd /path/to/Android        # repository checkout
chmod +x bootstrap/install.sh
./bootstrap/install.sh --accept-android-sdk-licenses --yes
```

The bootstrap finds or installs `uv` (HTTPS only; checksum verified when
`UV_INSTALLER_SHA256` is set), installs the `avdfleet` tool, runs
`avdfleet install`, then `avdfleet doctor`.

Useful options: `--no-appium`, `--sdk-root /opt/android-sdk`, `--dry-run`,
`--verbose`, `--version`. The full table is in [installer.md](installer.md).

### Reboot / logout and resume

KVM group membership and udev rules take effect after a logout/login or reboot.
If a reboot is requested, the bootstrap exits with code `6`, and the installer
checkpoint stays at `~/.local/share/avdfleet/install-manifest.json` (or
`$AVDFLEET_DATA_DIR`). Re-run the same command after the reboot; it resumes
(`avdfleet install --resume` under the hood) instead of starting over.

## 4. Verify

```bash
avdfleet doctor
avdfleet doctor --slow
avdfleet status
```

`avdfleet doctor --fix kvm_group` prints/uses the `usermod` fix; `--dry-run`
shows it without changing anything.

## 5. Run the GUI

```bash
avdfleet serve
# opens http://127.0.0.1:8765/
```

The daemon is loopback-only by default and serves the REST API, WebSockets and
the built GUI from one process. On a headless server, forward the port over SSH
and open the GUI locally:

```bash
ssh -L 8765:127.0.0.1:8765 user@lab-host
```

The bearer token is created on first start at
`~/.local/share/avdfleet/token` with mode `0600` (`AVDFLEET_TOKEN` overrides it).

## 6. First fleet

In the GUI: **Images → Templates → Fleet → Proxies → Scripts**. The equivalent
CLI:

```bash
avdfleet images list --abi x86_64
avdfleet images download "system-images;android-35;google_apis_playstore;x86_64"
avdfleet avd templates create --name pixel-api35 --image "system-images;android-35;google_apis_playstore;x86_64"
avdfleet avd deploy <template-id> --count 5 --prefix avd
avdfleet instances start avd-01
avdfleet instances wait-boot avd-01
```

See [cli.md](cli.md), [api.md](api.md) and [proxy.md](proxy.md).

## 7. Acceptance self-test

```bash
avdfleet selftest --e2e
```

Runs the BRIEF §7 sequence against the real toolchain (`--json` for the report).
`--require-acceleration` turns missing KVM into exit `3`; `--custom-image <dir>`
exercises custom-image import. A host without `/dev/kvm` reports the
real-emulator items as `SKIP`; see [acceptance.md](acceptance.md).

## 8. Data locations

| Purpose | Default path |
|---|---|
| Data (database, token, manifest, AVDs, SDK) | `$XDG_DATA_HOME/avdfleet` (`~/.local/share/avdfleet`) |
| Configuration (`config.toml`) | `$XDG_CONFIG_HOME/avdfleet` (`~/.config/avdfleet`) |
| Cache and downloads | `$XDG_CACHE_HOME/avdfleet` (`~/.cache/avdfleet`) |
| Logs | `$XDG_STATE_HOME/avdfleet/logs` (`~/.local/state/avdfleet/logs`) |

Override with `AVDFLEET_DATA_DIR`, `AVDFLEET_CONFIG_DIR`,
`AVDFLEET_CACHE_DIR`, `AVDFLEET_LOG_DIR`, `AVDFLEET_SDK_ROOT`,
`AVDFLEET_AVD_HOME`. A config file may also be `./avdfleet.toml` or passed with
`--config`.

## 9. Uninstall or repair

```bash
avdfleet repair --yes
avdfleet uninstall --dry-run
avdfleet uninstall --yes                 # remove installer-managed files
avdfleet uninstall --yes --keep-data     # keep AVDs, database, logs
uv tool uninstall avdfleet               # remove the CLI
```

## Next steps

- [installer.md](installer.md) — steps, flags, manifest, exit codes
- [troubleshooting.md](troubleshooting.md) — KVM, glibc, license, display and port symptoms
- [compatibility.md](compatibility.md) — exact host/version support matrix
