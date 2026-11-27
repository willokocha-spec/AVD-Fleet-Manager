# Troubleshooting

Start with `avdfleet doctor` (add `--slow` for tool-version, port and clock
checks) and `avdfleet status`. Logs are under `<data>/Logs` on Windows and
`<state>/logs` on Linux/macOS (see the quickstarts for exact paths). Never paste
proxy credentials or SDK licence files into bug reports.

## Virtualisation and acceleration

| Symptom | Cause | Fix |
|---|---|---|
| `doctor` `hypervisor` fail: "no usable hypervisor" | VT-x/AMD-V disabled, hypervisor not enabled, or this machine is a VM without nested acceleration | Enable virtualisation in firmware, enable WHPX (Windows) / KVM (Linux); HVF is automatic on macOS. Reboot. |
| Emulator exits immediately / `-accel-check` says no acceleration | Same as above; or Hyper-V/VBS holds the CPU | Windows: use **WHPX** (supported even with Hyper-V VBS). Linux: ensure `/dev/kvm`; macOS: check HVF. |
| Windows WHPX not offered | Windows 10 < 1803 or feature disabled | `Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -All`, reboot; Windows 10 x64 1803+ required. |
| AEHD step fails / driver conflict | AEHD is sunsetting and cannot coexist with Hyper-V/VBS | Use WHPX. Only migrate old AEHD installs (`--only accel.aehd`) and remove AEHD afterwards. |
| Nested VM: "acceleration unavailable" | The emulator cannot be accelerated inside most VMs/cloud runners; GitHub calls nested virtualisation unsupported | Run on bare metal with WHPX/KVM/HVF, or accept fakes-backed testing only. CLI-only install: `AVDFLEET_ALLOW_UNSUPPORTED_HOST=1` (unsupported). |
| macOS: emulator hangs, no HVF error | Wrong image ABI, or quarantine on the emulator binary | Apple Silicon needs `arm64-v8a` images; clear quarantine (`xattr -dr com.apple.quarantine`). |
| WSL / Docker / ARM host | No stable accelerated emulator target | Refused by design; use a supported host or the portable edition on one. |

## KVM and Linux permissions

| Symptom | Cause | Fix |
|---|---|---|
| `/dev/kvm` missing | KVM module/firmware not enabled, or a nested VM | Enable VT-x/AMD-V; load `kvm_intel`/`kvm_amd`; nested virt only if your provider exposes it. |
| `Permission denied` opening `/dev/kvm` | User not in `kvm` group / udev mode too restrictive | `sudo usermod -aG kvm "$USER"` then log out/in or reboot. `avdfleet doctor --fix kvm_group` prints the command (add `--dry-run` to preview). |
| KVM works only after reboot | Group/udev changes apply on next session | Reboot or log out and back in; `newgrp kvm` only affects the current shell. |
| `libnss3`/GUI library missing | Minimal Linux install | Install the distro packages listed in [quickstart-linux.md](quickstart-linux.md) (`libnss3`, `libpulse0`, `libgtk-3-0`, `libvulkan1`, X11 libs). |
| Rocky/RHEL 8 refused | glibc 2.28 < 2.31 required by Studio/emulator tooling | Use Rocky/RHEL 9+. |

## Filesystem and locations

| Symptom | Cause | Fix |
|---|---|---|
| Portable refuses to build/start | FAT32 destination | FAT32's 4 GiB file limit cannot hold AVD data/images; use NTFS/APFS/ext4/XFS/Btrfs. `portable checks` reports it. |
| Portable warns about exFAT | No POSIX permissions/owner semantics | Use NTFS/APFS/ext4; expect fragile snapshots on exFAT. |
| `noexec` mount detected | `/tmp`/USB mounted noexec | Mount with `exec`, or place the data/bundle elsewhere; portable preflight refuses to run. |
| Writes appear outside the bundle root | Documented leftovers (Studio/Gradle/temp/adb keys) | Expected but bounded — see [portable.md](portable.md#leftovers-writes-outside-the-bundle-root); `portable verify` fails only on undocumented writes. |
| Spaces/Unicode in SDK or data path | Legacy tooling path parsing | Prefer ASCII paths without spaces for `--sdk-root`/data; Windows long/Unicode path bugs are mostly fixed in emulator ≥ 37.3.10. |
| Names too long / `MAX_PATH` | Windows 260-char default | Enable long paths (`HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled=1`) and reboot; keep SDK/AVD roots short. `doctor` warns. |
| Disk full / out of space | Images (2–8 GB each), AVD data (~6 GB+/AVD), snapshots | Free space or move data; `doctor` checks free space; `avd delete`/`instances delete` reclaim. |
| OneDrive/Dropbox synced folders | Sync locks/rewrites during emulator I/O | Keep SDK, AVDs and bundles out of synced folders. |

## Installer, licences and reboot

| Symptom | Cause | Fix |
|---|---|---|
| Install stalls on licences | `--accept-android-sdk-licenses` missing | Pass the flag (bootstrap does); re-run. `doctor` reports `licenses` fail. |
| `install` exits `3010` (Windows) / `6` (Unix) | Reboot required (WHPX/KVM) | Reboot, then re-run the same command: resumes from the manifest. Bootstrap handles this. |
| Reboot message repeats | Virtualisation still not enabled, or stale checkpoint | Verify WHPX/KVM first; only for a genuinely stale checkpoint set `AVDFLEET_IGNORE_REBOOT_MARKER=1`. |
| Second install re-downloads everything | Looking at a different data dir/`--sdk-root` | Check `avdfleet status` paths; the manifest lives in the data dir. |
| `uv` installation fails (Linux) | Network, PEP 668, missing curl/wget | Use the official uv installer; ensure `HTTPS_PROXY` is set for corporate networks; check `UV_INSTALLER_SHA256` format. |
| `avdfleet: command not found` after install | `~/.local/bin`/`%USERPROFILE%\.local\bin` not on PATH | Add it to PATH and open a new shell. |
| Unsigned script blocked (Windows) | Execution policy / Mark-of-the-Web | `powershell -ExecutionPolicy Bypass -File …` and `Unblock-File -LiteralPath .\bootstrap\install.ps1`. |
| macOS blocks the script/bundle | Gatekeeper quarantine | `xattr -dr com.apple.quarantine <path>` or Open Anyway in System Settings. |
| Python 3.14 host rejected | Project supports 3.11–3.13 | Install Python 3.12 (uv can manage it) or let the bootstrap do it. |

## Daemon, auth and GUI

| Symptom | Cause | Fix |
|---|---|---|
| `401 Missing or invalid bearer token` | Token not sent/wrong file | Read `<data>/token` (owner-only) or set `AVDFLEET_TOKEN`; use `Authorization: Bearer`. See [api.md](api.md). |
| GUI shows "connect" screen forever | Token not entered, or daemon on another port | Enter the token from the data dir; check the daemon URL/port; `curl /health`. |
| `avdfleet serve` fails to bind | Port in use | `--port 9000` (or config `api_port`); portable launchers auto-pick a free port. |
| Blank page at `/` | UI not built/copied into `api/static` | `pnpm build:daemon` from `ui/`, or reinstall the wheel (it force-includes the static UI). Dev mode: `pnpm dev`. |
| CORS/Origin errors | GUI served from an origin not in `allow_origins` | Use the bundled GUI on `127.0.0.1:8765` (or `localhost:5173` for `pnpm dev`); edit `allow_origins` in `config.toml`. |
| WebSocket closes immediately | Missing/invalid token or disallowed Origin | Pass `?token=` and an allowed Origin; close codes `4401`/`4403` are documented in [api.md](api.md). |

## AVDs, ports and adb

| Symptom | Cause | Fix |
|---|---|---|
| Boot timeout (`sys.boot_completed` never 1) | Slow host/software GPU, bad image, insufficient RAM | Increase `boot_timeout_s`; use `-gpu software`; check emulator log; ensure API 37+ AVD has ≥ 4096 MB. |
| Capacity error when starting | RAM/ports exhausted (`max_instances`, port range 5554–5680) | Free instances, reduce `default_ram_mb`, or move to a host with more RAM. |
| Instances missing from `adb devices` beyond the 16th | adb scans only odd ports 5555–5585 by default | Set `ADB_LOCAL_TRANSPORT_MAX_PORT` (e.g. 5682) **before** the adb server starts; AVD Fleet addresses by serial. |
| `more than one device/emulator` | No `-s` selector | AVD Fleet always passes `-s emulator-<console>`; user scripts should use `$AVDFLEET_SERIAL`/ANDROID_SERIAL. |
| Port already in use | Stale emulator/adb/bridge | Stop the instance (`--force`), check `avdfleet instances list`, `proxies bridges`; restart. |
| Snapshot restore fails / behaves oddly | Snapshot invalid after image/config/emulator change or relocation | Reset with `--strategy wipe`; golden snapshots are invalidated automatically; avoid software GPU for snapshot-heavy flows if unstable. |

## Proxies, DNS and UDP

| Symptom | Cause | Fix |
|---|---|---|
| Proxy health check fails (407/timeout) | Wrong credentials or unreachable upstream | Re-import the URL; test from the host; check `HTTPS_PROXY` interference. |
| Guest traffic goes direct | No assignment / stale bridge | Assign a proxy and restart; `avdfleet proxies bridges` should list one. |
| DNS resolution works but the proxy sees nothing | **Expected** — `-http-proxy` does not carry DNS | Not a bug: DNS goes via the emulator's host resolver. Use `-dns-server` + host controls for DNS policy. |
| QUIC/UDP traffic bypasses the proxy | **Expected** — the emulator proxy is TCP-only | Documented limitation; UDP/QUIC/DNS cannot be isolated inside an unrooted guest. See [proxy.md](proxy.md#dns-and-udp-explicit-limitations). |
| Token appears on the wrong listener | Assignment leak (G1 failure) | Stop the fleet, collect evidence per `research/g1/gate.md`, fix assignment/bridge before retrying. |
| Keyring errors / prompts on a server | No OS keyring backend | Set `vault_backend = "file"` and provide `AVDFLEET_VAULT_PASSPHRASE`. |

## Appium and screen

| Symptom | Cause | Fix |
|---|---|---|
| Appium start fails: port busy | Stale server | `avdfleet appium stop`; check port 4723; start with `--port`. |
| Session creation fails: port already forwarded | Stale adb forward from an interrupted run | `avdfleet appium cleanup <avd>`, then retry with unique ports; never `adb kill-server` mid-fleet. |
| Appium not found / driver missing | Node/Appium not installed | `avdfleet install --only tools.appium` (or `appium driver install uiautomator2`); verify with `appium --version` and `appium driver doctor uiautomator2`. |
| Node version rejected by Appium 4 | Node < 22.22.2 / 24.15 | Install a supported Node (26 LTS recommended); `doctor` checks it. |
| Screenshot black/blank | GPU/software rendering or wrong ABI | Use `-gpu software`; verify the AVD is booted (`wait-boot`); check the gRPC fallback diagnostics. |
| Screen stream closes with 4503 | gRPC unavailable and adb screencap unavailable | Ensure the instance is running and adb works; the API falls back to screencopy, GUI shows the last frame. |
| Input not delivered | Guest not focused / gRPC+adb both failing | Check instance state; use `adb shell input` manually to isolate. |

## Logs and diagnostic commands

```bash
avdfleet doctor --slow --json
avdfleet status
avdfleet instances list --json
avdfleet instances get avd-01 --json
avdfleet proxies check-all --json
avdfleet portable checks --json
journalctl --user -u avdfleet 2>/dev/null   # if run as a service on Linux
```

Emulator/adb logs live in the per-instance data directory and the data `Logs`
folder; install logs are printed by `avdfleet install` and captured in the
installer manifest. When reporting a problem include: OS/arch, `avdfleet
--version`, `doctor --json`, the redacted command and the relevant log excerpt —
and never real credentials or licence text.
