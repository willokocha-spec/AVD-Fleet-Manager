# Compatibility

This page records the exact host, component and version support matrix, and the
platform facts that changed from the original brief during research (see
`research/R1-versions.md` … `research/R7-portable.md` and `PLAN.md` → Decisions).

## Host matrix

| Host | Emulator | Acceleration | AVD image ABI | Status |
|---|---|---|---|---|
| Windows 11 x64 | Yes | **WHPX** | `x86_64` | Supported (recommended) |
| Windows 10 x64 (1803+) | Yes | WHPX / legacy AEHD | `x86_64` | Supported (minimum) |
| Windows ARM64 | No stable `windows_aarch64` emulator build | — | — | **Refused** (bootstrap exit `3`) |
| macOS 13+ Apple Silicon (M1+) | Yes | **HVF** (built in) | `arm64-v8a` | Supported (recommended) |
| macOS 12+ Intel x64 | Yes (`darwin_x64`) | HVF | `x86_64` | Supported but on upstream deprecation path |
| Ubuntu LTS x64 / Debian 11+ | Yes | **KVM** (`/dev/kvm`) | `x86_64` | Supported |
| Fedora x64 | Yes | KVM | `x86_64` | Supported |
| Rocky/RHEL **9+** x64 | Yes | KVM | `x86_64` | Supported (`glibc ≥ 2.31`) |
| Rocky/RHEL **8** x64 | No (glibc 2.28 < 2.31) | — | — | **Refused** |
| Arch Linux x64 | Yes | KVM | `x86_64` | Supported (Studio is AUR/tarball; AVD Fleet uses the SDK tarballs) |
| Linux ARM64 / Windows-on-ARM | No stable emulator host package | — | — | **Refused** |
| WSL2, Docker, most VMs | Emulator cannot be VM-accelerated | none usable | — | **Refused** (bootstrap exit `3`); CLI-only install possible with `AVDFLEET_ALLOW_UNSUPPORTED_HOST=1` but unsupported |

Minimums from Google's documentation: emulator 16 GB RAM and 16 GB free disk;
Studio + emulator 16 GB RAM minimum, 32 GB recommended; CPU virtualisation
enabled in firmware; CPU microarchitecture after 2017 (Intel 8th-gen i5 / AMD
Zen recommended). AVD Fleet recommends 32 GB+ for fleets; running N AVDs needs
roughly 4 GB per AVD (Google: "approximately 4 GB of memory per AVD").

## Virtualisation

| Host | Mechanism | Notes |
|---|---|---|
| Windows | **WHPX** (Windows Hypervisor Platform) | Enable `HypervisorPlatform`, reboot once. Needs VT-x+EPT+Unrestricted Guest or AMD-V. |
| Windows legacy | AEHD | **Sunsetting 2026-12-31**; default only for migration of existing installs. Cannot coexist with Hyper-V/VBS. |
| Linux | KVM | `/dev/kvm`; `kvm` group or udev rule; logout/reboot for group changes. |
| macOS | Hypervisor.framework (HVF) | Built in; no setup. |
| Nested VM / cloud | Not supported for accelerated AVDs | GitHub-hosted runners: x86 Linux KVM works only after a udev-group step and is called unsupported by GitHub; Windows/macOS runners cannot run accelerated AVDs. |

`-accel off` is not a usable fallback: the emulator's software path requires
`ANDROID_I_WANT_MY_TCG=yes` and is far too slow to be a product mode.

## Images, ABI and capacity

- Host ABI must match the image ABI: Apple Silicon → `arm64-v8a`; x86_64 hosts →
  `x86_64` (or `x86` where available). Cross-ISA AVDs are not accelerated, so
  the catalogue filters by host ABI.
- Supported API range in configuration: **21–36** (`SUPPORTED_API_RANGE`), with
  newer images representable; the SDK channel is `stable` by default.
- **API levels are not always integers.** Google publishes
  `system-images;android-36.1;…`, `android-37.0`, `android-CANARY`, etc. The
  model keeps the major integer (`api_level`) for UI/filtering and preserves the
  raw API string in `SystemImage.metadata["sdk_api"]` — parsers never
  `int()` the directory segment.
- From **API 37 the emulator enforces a 4 GB minimum** AVD RAM (lower values
  are overwritten to ≥ 4096). API ≤ 36 runs at 2 GB.
- The emulator has exactly **64 console/adb port pairs** (5554–5682, even
  console ports); `Config.port_range_start/end` (defaults 5554/5680) therefore
  yields 64. That is the hard protocol limit, not a RAM guarantee: 64 × 4 GB
  needs ≈ 256 GB+ RAM (≈136 GB for 64 × 2 GB API ≤ 36).
- adb discovers only the **first 16 emulators** (odd ports 5555–5585) unless
  `ADB_LOCAL_TRANSPORT_MAX_PORT` is raised before the adb server starts. AVD
  Fleet addresses instances by serial (`emulator-<console>`) and provisions the
  variable for large fleets.

## Component versions

Pinned upstream versions verified 2026-11-26 in `research/R1-versions.md`; the
implementation keeps these configurable (`config.toml` / `AVDFLEET_*`) so CI and
operators can pin. Where the exact version is supplied by `sdkmanager`/`npm`,
the installer records the installed version in the install manifest.

| Component | Current upstream (2026-11-26) | Implementation note |
|---|---|---|
| Android Studio | Rabbit 2 / 2026.2.2 Patch 3 (build 2026.2.2.11) | Configured, not required (`studio_required = false`) |
| Android cmdline-tools | download zip rev 22.0 (build 15859902); `cmdline-tools;latest` in SDK repo rev 23.0 (build 16111833) | Installed via `sdkmanager` |
| Android platform-tools | 37.0.3 | Via `sdkmanager` |
| Android emulator | 37.3.10 stable (build 16970787) | Via `sdkmanager`; pin ≥ 37.3.10 for Windows non-ASCII path and gRPC cleanup fixes |
| JDK | Temurin/Microsoft OpenJDK 21.0.13.1 (default), 25.0.5.1 current LTS | `jdk_version = "21"` default; 25 opt-in |
| Node.js | 26.13.1 Active LTS (22 is maintenance; 22.22.2+ is the Appium 4 minimum) | `node_version = "22"` default for compatibility; Appium 4 accepts ^22.22.2, ^24.15.0 or >=26 |
| Appium | 4.0.1 | `appium_version = "4"` |
| UiAutomator2 driver | 10.4.0 | `uiautomator2_version = "latest"` (pin for reproducibility) |
| Appium Inspector | 2026.11.6 | Optional GUI tool; not required for a green `doctor` |
| scrcpy | 5.0.1 | Optional; `scrcpy_version = "latest"` |
| uv | 0.13.10 | Installed by bootstrap |

### Brief facts corrected by research

These are recorded exactly as the final report must state them:

1. **GPU mode defaults.** `swiftshader_indirect` (the original default) is
   deprecated as of emulator 36.4.9; current supported modes are
   `auto`, `host`, `software`, `lavapipe`, `swiftshader`, `swangle`. The
   implementation keeps `swiftshader_indirect` as a compatible default/fallback
   for existing AVDs and maps `auto`/`host` to it when headless; new templates
   should prefer `software` (or `lavapipe`). `angle_indirect`/`off` are legacy.
2. **Node LTS.** Node 26.x is the current Active LTS, not 22; Node 22 is
   maintenance and is only the Appium 4 floor. The default stays 22 for
   compatibility and is configurable.
3. **Appium version.** Current is Appium **4.0.1** with UiAutomator2 **10.4.0**
   (the brief said only "Appium 4"). Appium 4 is ESM-only: JS scripts must be
   ESM, and old Appium clients using removed endpoints will break.
4. **`-http-proxy` is TCP-only.** DNS and UDP/QUIC are not proxied; SOCKS5 is not
   supported by the flag. Strict isolation uses the loopback bridge; the residual
   DNS/UDP limitation is documented in [proxy.md](proxy.md), never claimed as
   full isolation.
5. **AEHD sunset.** AEHD sunsets 2026-12-31; WHPX is the only supported Windows
   hypervisor path after that.
6. **Play/Google licence.** Google's SDK licence forbids redistributing
   Play/Google components, Studio or JBR, so the portable bundle cannot ship
   them; offline first run is limited to the operator's licensed local cache.
7. **`STUDIO_PROPERTIES`.** It must point at an *`idea.properties` file*, not a
   directory.
8. **API levels can be non-integer** (`android-36.1`, `android-37.0`); the raw
   string is preserved in metadata.
9. **Windows minimum is Windows 10 x64 (1803+)**, not only Windows 11; Windows
   ARM64 is unsupported.
10. **macOS Intel is still supported** (macOS 12+, 6th-gen Intel+) but on a
    deprecation path; Homebrew requires macOS 15+, so the bootstrap does not
    depend on Homebrew.
11. **Custom images** require `system.img`, `kernel-ranchu` (or `kernel-qemu`)
    and `ramdisk.img`; Google image archives contain no `package.xml` — the SDK
    manager writes it on install.
12. **gRPC discovery** is `…/avd/running/pid_<pid>.ini` (not an `emu-grpc`
    directory); the repository's fakes use a synthetic layout only.

## GPU modes

| Mode | Status | Fleet guidance |
|---|---|---|
| `software` | Supported (new in 36.4.9) | **Recommended for headless fleets/servers** |
| `lavapipe` | Supported | Deterministic software Vulkan |
| `swiftshader` | Supported | Fallback |
| `swangle` | Supported | Fallback |
| `auto` | Supported | Emulator chooses; not deterministic across hosts |
| `host` | Supported | Only with a real GPU/driver; macOS may use MoltenVK |
| `swiftshader_indirect` | **Deprecated** (36.4.9), kept as compatible default/fallback | Remove from new templates |
| `angle_indirect`, `mesa`, `angle`, `guest` | Deprecated | Never emit |
| `off` | No longer documented | Do not rely on it |

## Editions and packaging

- Installed edition: Python 3.11–3.13 (`requires-python = ">=3.11,<3.14"`),
  `uv tool install avdfleet`, one HTTP daemon serving the built UI. One Python
  package, one version (`0.1.0`).
- Portable edition: one bundle per host OS/arch; no venv shipped; no Google
  components redistributed; prepared-cache build in v0.1 (no download step by
  the builder).
- Cross-OS and cross-arch bundles: unsupported.
- Release artifacts: wheel + sdist from hatchling; the wheel force-includes the
  built UI at `avdfleet/api/static`. Build the UI (`pnpm build:daemon`) before
  packaging so the daemon serves the GUI.
- Supported Python on the build host: `>=3.11,<3.14`. Python 3.14 is not
  supported yet (the code targets 3.11 syntax and typed APIs).

## Known compatibility gaps in this checkout

- `tests/e2e/`, `tests/installer/` and `ui/e2e/` are **empty placeholder
  directories** in this checkout (BRIEF Wave 1 T3/T4): no real-emulator E2E, no
  installer-container tests and no Playwright suite were delivered. The CI
  installer job is best-effort and skips when `tests/installer` has no tests.
- Real-emulator E2E is impossible on the build host and is marked best-effort in
  CI; see [testing.md](testing.md) and [acceptance.md](acceptance.md).
