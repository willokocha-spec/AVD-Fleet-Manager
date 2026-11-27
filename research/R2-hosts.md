# R2 — Host matrix: OS/arch support, virtualisation, headless GPU, capacity

**Author:** research-core (R2) · **Accessed:** 2026-11-26 (Europe/Dublin) · **Status:** read-only
research (no emulator was booted; this host has no hypervisor — see BRIEF §6/PLAN Wave 0).

Scope: Windows 11, macOS (Apple Silicon / Intel), Ubuntu LTS, Debian, Fedora, Rocky/RHEL, Arch;
WHPX / KVM / HVF / AEHD requirements; nested-virtualisation limits; cloud/GitHub-runner constraints;
headless GPU modes; emulator RAM/capacity guidance; Linux package dependencies per distro; macOS
Command Line Tools/Homebrew prerequisites; Windows long-path/Defender/reboot details.

Legend: **VERIFIED** = read on the upstream page named in the row on 2026-11-26 · **INFERRED** =
derived from verified facts (derivation shown) · **UNVERIFIED** = no authoritative source found; do
not present as fact. Sources are listed at the end.

Core upstream pages:
- Emulator system requirements + usage: <https://developer.android.com/studio/run/emulator>
- Emulator acceleration (WHPX/AEHD/KVM/HVF + GPU modes): <https://developer.android.com/studio/run/emulator-acceleration>
- Android Studio system requirements: <https://developer.android.com/studio/install>
- Emulator release notes: <https://developer.android.com/studio/releases/emulator>
- Emulator command-line options: <https://developer.android.com/studio/run/emulator-commandline>
- Emulator troubleshooting: <https://developer.android.com/studio/run/emulator-troubleshooting>

---

## 1. Host support matrix

### 1.1 What Android Studio / the emulator actually support (VERIFIED unless marked)

| Host | Studio support | Emulator support | VM acceleration | Emulator host package | System images |
|---|---|---|---|---|---|
| **Windows 11 x64** | Yes (min "64-bit Microsoft Windows 10", latest Win 11 recommended) | Yes | **WHPX** (recommended); AEHD until **2026-12-31** | `emulator-windows_x64-*.zip` | x86_64 |
| **Windows 10 x64 (1803+)** | Yes (minimum) | Yes (WHPX needs 1803+) | WHPX / AEHD | `emulator-windows_x64-*.zip` | x86_64 |
| **Windows ARM64** | **NOT supported** — "Windows machines with ARM-based CPUs aren't currently supported" | Stable emulator **ships no windows_aarch64 build** (37.3.10 = x64 only; a preview `emulators;latest` aarch64 build exists) | — | — | — |
| **macOS Apple Silicon** | Yes (minimum macOS 12; Apple M1+ recommended) | Yes | **HVF** (built-in, no setup) | `emulator-darwin_aarch64-*.zip` | arm64-v8a |
| **macOS Intel x64** | Yes (min 6th-gen Intel; "phasing out support for Mac with Intel chips") | Yes | HVF | `emulator-darwin_x64-*.zip` | x86_64 |
| **Ubuntu LTS (x64)** | Yes (any 64-bit distro, glibc ≥ 2.31) | Yes | **KVM** | `emulator-linux_x64-*.zip` | x86_64 |
| **Debian (x64)** | Yes (Debian ≥ 11 ships glibc 2.31; Debian 12/13/14 current) | Yes | KVM | same | x86_64 |
| **Fedora (x64)** | Yes (glibc ≥ 2.31 in all current releases) | Yes | KVM | same | x86_64 |
| **Rocky/RHEL 9 (x64)** | **INFERRED** yes — RHEL 9 glibc 2.34 ≥ 2.31 | **INFERRED** yes | KVM (nested restrictions apply) | same | x86_64 |
| **Rocky/RHEL 8 (x64)** | **INFERRED no** — RHEL 8 glibc 2.28 < required 2.31 | **INFERRED** no | KVM | — | — |
| **Arch Linux (x64)** | Yes (rolling glibc; **Studio is not in official Arch repos** — AUR/tarball only) | Yes | KVM | same | x86_64 |
| **Linux ARM64 (any distro)** | **NOT supported** — "Linux machines with ARM-based CPUs aren't currently supported" | No stable linux_aarch64 host package | — | — | — |
| **WSL2 / Docker / any VM** | Can run Studio, but | **Emulator cannot be VM-accelerated** (see §3.4) | None usable | — | — |
| **ChromeOS** | Yes (separate documented path) | Yes (ChromeOS emulator support list) | KVM | — | x86_64 |

Key verified minimums (Android Studio install page and emulator page):
- **Emulator system requirements:** 16 GB RAM, 64-bit Windows 10+ / macOS 12+ / Linux / ChromeOS,
  **16 GB free disk**. Higher API levels/larger devices need more.
- **Studio alone:** 8 GB RAM minimum; **Studio + emulator:** 16 GB minimum, **32 GB recommended**;
  disk 8 GB (Studio) / 16 GB (Studio+emulator), SSD 32 GB+ recommended; GPU: none for Studio,
  4 GB VRAM minimum / 8 GB VRAM recommended for Studio+emulator; screen 1280×800 min, 1920×1080 rec.
- **CPU:** virtualisation support required (Intel VT-x or AMD-V, enabled in BIOS); minimum
  "CPU microarchitecture after 2017" (Intel 8th-gen Core i5 / AMD Zen Ryzen); Intel N-Series/U-Series
  explicitly **not recommended**; recommended latest microarchitecture.
- **Windows 11 is not required by Studio/emulator** — Windows 10 64-bit is the documented minimum.
  (The product's "fresh Windows 11" target is fine, just not the minimum.)
- **Host ABI ↔ image ABI must match** for acceleration: x86_64 hosts need x86/x86_64 images
  (API ≥ 10); ARM64 hosts need arm64-v8a images (API ≥ 21). Mismatched AVDs cannot be VM-accelerated
  (emulator acceleration page, verified). This drives the image catalogue (`host_abi`).

### 1.2 Architecture consequences for the product
- `avdfleet` portable bundles must be **per host OS + arch** (already PLAN Decision 5). Only four
  host targets exist: Windows x64, macOS arm64, macOS x64, Linux x64.
- Windows-on-ARM and Linux-on-ARM must be detected and reported as **unsupported** by `doctor`
  (not merely "no hypervisor").
- macOS Intel is supported but on a deprecation path: treat x64 macOS as supported-but-degraded, and
  don't assume `/opt/homebrew` (Intel uses `/usr/local`).

---

## 2. Acceleration matrix (WHPX / AEHD / KVM / HVF)

### 2.1 Windows — WHPX (recommended, and the only supported path after 2026-12-31)

**VERIFIED** (emulator acceleration page + emulator 37.1.11/37.3.10 release notes):
- Requires **Windows 10 version 1803 or higher** (Studio may require newer); Intel: VT-x + EPT +
  Unrestricted Guest, enabled in BIOS; AMD: SVM/AMD-V, Ryzen recommended; Studio ≥ 3.2 Beta 1;
  emulator ≥ 27.3.8.
- Enable via **Turn Windows features on or off → Windows Hypervisor Platform** → **restart required**.
  No separate driver. Check: `emulator -accel-check` prints e.g. `WHPX(10.0.22631) is installed and usable.`
- Hyper-V / Core Isolation / Memory Integrity coexist with WHPX (they are Hyper-V-based); this is why
  WHPX is the recommended path on modern Windows 11.
- **The installer must checkpoint and resume after this reboot** (BRIEF §3.2 already requires
  reboot resume; `StepStatus.REBOOT_REQUIRED` / `RebootRequiredError` exist in core).

### 2.2 Windows — AEHD (Android Emulator hypervisor driver) — sunsetting

**VERIFIED** (acceleration page note + release notes 37.1.11):
- AEHD is being **sunset on 2026-12-31**; the SDK package is being removed after that date. Until
  then it can be used, installed from SDK Manager ("Android Emulator hypervisor driver") or from
  <https://github.com/google/android-emulator-hypervisor-driver/releases> (`silent_install.bat`,
  admin). Requirements: 64-bit Win10/Win11, Intel/AMD VT; **Hyper-V hypervisor, Core Isolation and
  other Hyper-V-using features must be disabled** — the opposite of WHPX.
- Service names: AEHD ≥ 2.1 = `aehd`; AEHD ≤ 2.0 = `gvm` (`sc query aehd` / `sc query gvm`;
  uninstall `sc stop` + `sc delete`).
- **Installer policy:** default to WHPX; **do not install AEHD automatically**. Offer it only as an
  explicit opt-in for old hardware before 2026-12-31, with a hard heading in docs that it is
  deprecated. Detection for migration: `sc query aehd` running + WHPX absent → prompt/convert.
- HAXM is dead: removed from the emulator as of 36.2.x; uninstall instructions are in the
  acceleration page (service `intelhaxm`, install dir `%SDK%\extras\intel`).

### 2.3 Linux — KVM

**VERIFIED** (acceleration page):
- Intel: VT-x + Intel EM64T + XD bit enabled; AMD: AMD-V. Check `egrep -c '(vmx|svm)' /proc/cpuinfo`
  ≥ 1 and `kvm-ok` (from `cpu-checker`; expected `INFO: /dev/kvm exists / KVM acceleration can be used`).
- Ubuntu install command (verified on the Android page):
  `sudo apt-get install qemu-kvm libvirt-daemon-system libvirt-clients bridge-utils`
  (older Ubuntu variants have `libvirt-bin`/`ubuntu-vm-builder`; current releases use the first form).
- **Permissions are the usual failure mode:** the user running the emulator must be able to open
  `/dev/kvm` (typically the `kvm` group). Arch Wiki KVM (VERIFIED) documents `/dev/kvm` ownership
  and `kvm`-group access; the GitHub Actions workaround (§4) uses a udev rule that makes
  `KERNEL=="kvm", GROUP="kvm", MODE="0666"`.
- Kernel modules `kvm_intel` / `kvm_amd` must load; if `modprobe kvm_intel` fails while `kvm` loads,
  the extension is usually disabled in BIOS/firmware (Arch Wiki, VERIFIED).
- **Nested KVM** (emulator inside a Linux VM) is possible only if the host exposes nested virt:
  `options kvm_intel nested=1` (or `kvm_amd nested=1`) and the hypervisor/cloud must enable it —
  but see §3.4: the Android emulator docs say a VM-accelerated emulator must run directly on the
  host, so this is out of support for us.

### 2.4 macOS — Hypervisor.framework (HVF)

**VERIFIED** (acceleration page):
- Built into macOS (requires macOS 10.10+, but Studio requires macOS 12+). No installation, no
  reboot. HAXM is unavailable since macOS 11 and HAXM support was removed from the emulator.
- Check: `emulator -accel-check` prints `Hypervisor.Framework OS X Version 13.2` (example) or
  `sysctl kern.hv_support` = 1 (**UNVERIFIED** sysctl example, commonly used; any Apple Silicon Mac
  supports HVF).
- Apple Silicon needs **arm64-v8a** system images; **Mac with Intel** needs x86_64 images. The
  emulator ships separate `darwin_aarch64` and `darwin_x64` host packages (R1 §5).
- Emulator 37.3.10 adds **KosmicKrisp Vulkan-on-Metal** on macOS 26+; `-gpu host` can enable
  MoltenVK for accelerated Vulkan.
- Apple Silicon hosts also face macOS-specific quirks fixed recently: emulator keeping macOS awake
  (37.2.12), App Nap/latency scheduling lag (37.3.10), CoreAudio race (37.3.10).

### 2.5 Acceleration support matrix (compressed)

| Feature | Windows x64 | macOS arm64/x64 | Linux x64 |
|---|---|---|---|
| Primary hypervisor | WHPX | HVF (built-in) | KVM |
| Admin/root needed | Yes (enable Windows feature; reboot) | No | Yes (install packages / group membership) |
| Reboot needed | Yes (WHPX first enable) | No | No (re-login for `kvm` group) |
| Alternative | AEHD (deprecated, sunsets 2026-12-31) | none | none |
| Software fallback | **None usable** (`-accel off` requires `ANDROID_I_WANT_MY_TCG=yes`, far too slow — BRIEF §6) | same | same |
| Nested/in-VM | **Not supported** | **Not supported** | **Not supported** |

---

## 3. Nested virtualisation & cloud constraints

### 3.1 Emulator's own restriction (VERIFIED)

The acceleration page states plainly:
> "You can't run a VM-accelerated emulator inside another VM, such as a VM hosted by VirtualBox,
> VMWare, or Docker. You must run a VM-accelerated emulator directly on your host computer."
and
> "Depending on your operating system and hypervisor, you may not run software that uses another
> virtualization technology at the same time … mostly observed on Windows with Hyper-V off."

So: **WSL2, Docker Desktop, Parallels/VMware/VirtualBox guests, cloud VMs without exposed nested
virt, and most CI containers cannot run an accelerated AVD.** This is the primary reason BRIEF §6
declares real-emulator work out of scope on this build host, and it must be surfaced in
`avdfleet doctor` as a distinct "nested/VM detected" finding (the `AcceleratorInfo.nested` field
already exists).

### 3.2 Hyper-V / VBS on Windows

- With Hyper-V or **Core Isolation/Memory Integrity** enabled, *WHPX still works*; **AEHD cannot**
  (verified in acceleration page AEHD requirements). Windows 11 machines commonly have VBS on by
  default → assume WHPX, and detect AEHD-vs-Hyper-V conflicts when the user asks for AEHD.
- Detection: `systeminfo` reports "A hypervisor has been detected"; PowerShell
  `Get-ComputerInfo -Property HyperVRequirementVirtualizationFirmwareEnabled` (the build host reports
  `False`). These are the same signals P1's detector needs for WHPX availability.

### 3.3 GitHub-hosted runners (VERIFIED where cited)

- **Linux x64 runners:** GitHub Actions has hardware-accelerated Android virtualisation since the
  2024-04-02 changelog, including **2-vCPU** runners (previously only ≥4 vCPU). The runner user must
  be added to the `kvm` group and a udev rule applied; the changelog gives the canonical snippet:
  ```yaml
  - name: Enable KVM group perms
    run: |
      echo 'KERNEL=="kvm", GROUP="kvm", MODE="0666", OPTIONS+="static_node=kvm"' \
        | sudo tee /etc/udev/rules.d/99-kvm4all.rules
      sudo udevadm control --reload-rules
      sudo udevadm trigger --name-match=kvm
  ```
  This is the mechanism used by `reactivecircus/android-emulator-runner`. Ubuntu runners are x64 only.
- **Nested virtualisation is not officially supported on any GitHub-hosted runner:** GitHub docs
  (runner docs) say: *"While nested virtualization is technically possible while using runners, it
  is not officially supported. Any use of nested VMs is experimental and done at your own risk, we
  offer no guarantees regarding stability, performance…"*.
- **Windows runners:** Hyper-V-based Azure VMs; no WHPX/AEHD exposure to jobs → **no accelerated
  Android emulator**. **macOS runners:** also VMs; HVF/nested is not an officially supported path →
  treat as **no accelerated AVD**. (This is INFERRED from the nested-virtualisation statement above
  plus the emulator's own no-nested rule; GitHub does not publish per-OS hypervisor support.)
- **Capacity:** standard GitHub-hosted runners have limited RAM (historically 7 GB on private and
  16 GB on public repos — **UNVERIFIED** current values; check the runner spec table before relying
  on numbers). With ~4 GB per accelerated AVD (see §5), a 16 GB runner fits **~2 AVDs at most** and
  a 7 GB runner **0–1**. Therefore BRIEF §6's guidance ("best-effort CI, API ≤ 36 images or at most 2
  instances") is consistent with the documented limits.
- **Practical recommendation:** run real-emulator E2E only on self-hosted/accelerated hosts; on
  GitHub use `ubuntu-latest` x64 with the KVM udev step for a best-effort smoke job
  (≤2 AVDs, API ≤ 36, 2 GB/AVD), and keep everything else fakes-backed.

### 3.4 Cloud hosts, containers, portable edition
- A cloud VM without nested virt (EC2 non-metal, GCE default, most Azure SKUs) = no usable AVD.
  Bare-metal instances (e.g. `.metal` SKUs) or VMs with explicit nested-virt enablement are the
  documented workaround for KVM — **UNVERIFIED** per provider/SKU; verify per cloud.
- The portable edition requires "hypervisor enabled" on the target machine (BRIEF §7.7) — that
  machine must be physical (or a VM with fully exposed nested virt, which is out of support).
- Containers: mounting `/dev/kvm` into a container can work on a bare-metal Linux host (KVM is
  passed through), but this is **not** the emulator's supported "inside another VM" scenario; treat as
  unsupported and detect it (`is_container` in `HostInfo`).

---

## 4. Headless GPU modes

### 4.1 Current `-gpu` modes (VERIFIED, acceleration page, 2026-11-26)

| Mode | Status | Meaning | Fleet recommendation |
|---|---|---|---|
| `auto` | supported (recommended for desktop) | emulator chooses hardware vs software based on AVD, host and quality metrics | **Avoid in fleets** — not deterministic across hosts |
| `host` | supported | host GPU hardware acceleration; can enable MoltenVK on macOS | Only on GPU hosts with a real driver; GPU + VRAM required (§1.1) |
| `software` | supported (new in emulator 36.4.9) | "most suitable software rendering backend for the AVD, requested emulation features, and quality metrics" | **Default for headless fleets/servers** |
| `lavapipe` | supported | Mesa llvmpipe software rendering | Deterministic software Vulkan (Lavapipe is the default software renderer since 36.4.9) |
| `swiftshader` | supported | SwiftShader for GLES + Vulkan | Fallback if software/lavapipe misbehaves |
| `swangle` | supported | SwiftShader software drivers + ANGLE backend | Fallback |
| `mesa` | **deprecated** (25.3.0) | — | never emit |
| `angle` | **deprecated** (27.0.2) | — | never emit |
| `angle_indirect` | **deprecated** (34.2.14) | — | never emit |
| `guest` | **deprecated** (36.4.9) | — | never emit |
| **`swiftshader_indirect`** | **deprecated** (36.4.9) | superseded by `software`/`lavapipe`/`swiftshader` | **remove from production defaults** |
| **`swangle_indirect`** | **deprecated** (36.4.9) | — | never emit |
| `off` | **not listed** on the current page | historically disables GPU emulation | **UNVERIFIED** in current emulator; do not rely on it |

### 4.2 Headless launch flags (VERIFIED, emulator command-line page)
- `-no-window` — headless; combined with `-gpu software` for servers (docs note that launching with
  `-no-window` historically defaulted to the software renderer).
- `-no-audio` — avoid audio device/backend problems on servers (host audio input is squelched by
  default since the "Ok Google" change; `hw.audioInput=no` in `config.ini` for headless AVDs).
- `-no-boot-anim` — faster boot.
- `-no-snapshot-save` / `-no-snapshot` — avoid snapshot writes per instance; `-read-only` for
  multiple instances of the same AVD (see docs; supported since 28.0.16). Useful for many identical
  fleet instances backed by one AVD, though our design uses one AVD per instance.
- `-memory <1536..8192>` — overrides AVD RAM in MB.
- `-port <even>` / `-ports <console>,<adb>` — valid port range **5554–5682**, "allowing for **64**
  concurrent virtual devices"; default console 5554. (This is where the 64-instance target comes
  from, and `Config.port_range_start=5554/end=5680` yields exactly 64 console pairs.)
- `-accel-check` — diagnostic for WHPX/KVM/HVF presence.
- `-grpc <port>` and the `-grpc-use-token`/`-idle-grpc-timeout` flags are described as Studio-only
  window plumbing — the screen/gRPC work (V1/R4) must discover endpoints rather than assume these.

### 4.3 GPU/BRIEF mismatch (important)
`core.models.GpuMode` and `Config.gpu_mode`/`Template.gpu_mode`/`FleetTemplateSpec.gpu_mode` default
to **`swiftshader_indirect`**, and the enum includes `ANGLE_INDIRECT` and `OFF`. As of emulator
36.4.9 both `*_indirect` modes are deprecated and `off` is no longer documented. For headless fleets
the supported, future-proof choice is **`-gpu software`** (or `lavapipe`/`swiftshader` if a specific
backend is desired). This should be changed in core models/config/spec (contract change — root must
record it in PLAN.md → Decisions), with legacy values kept parseable for old AVDs.

### 4.4 Host GPU notes (VERIFIED)
- Studio+emulator minimum GPU is 4 GB VRAM (NVIDIA GeForce 10 series / AMD RX 5000+), recommended
  8 GB VRAM (RTX 20 series / RX 6600+). A headless server can run the emulator without a GPU using
  `-gpu software`, but **software rendering is CPU-bound** — plan CPU headroom accordingly.
- Windows: known fallbacks to software on unsupported GPUs; `-gpu swiftshader` is the documented
  troubleshooting recommendation (troubleshooting page).
- macOS: `-gpu host` may use MoltenVK; KosmicKrisp Vulkan-on-Metal available on macOS 26+ with
  emulator 37.3.10.
- Vulkan: missing/incompatible Vulkan drivers can force software rendering; `-feature -Vulkan`
  disables Vulkan when snapshot/rendering issues occur (troubleshooting page).

---

## 5. Emulator RAM, CPU, disk and capacity guidance

**VERIFIED facts**
- Emulator/Studio system requirement: **16 GB RAM** for Studio + a single emulator; **32 GB
  recommended**. Running **multiple AVDs simultaneously requires approximately 4 GB of memory per
  AVD** ("the exact amount might vary depending on your system") — Android Studio install page.
- "You might need **up to 6 GB of additional storage for each additional AVD** created."
- `-memory` valid range **1536–8192 MB**.
- **From API 37, the emulator enforces a 4 GB minimum AVD RAM** (36.6.11 release notes): lower
  `hw.ramSize` values are automatically overwritten to ≥4 GB. API ≤ 36 can run smaller (typically
  2 GB; the product's 2048 MB default is sane, minimum 1536 MB).
- 64 console/adb port pairs exist (5554–5682), so the *port* ceiling is exactly 64 — consistent with
  BRIEF's 64-instance target.

**Capacity model (INFERRED but arithmetic-only)**
```
max_instances = floor((host_ram_gb - host_reserve_gb) / ram_per_avd_gb)
ram_per_avd_gb = 4  (API >= 37, enforced)
               = 2  (API <= 36, e.g. 2048 MB)
host_reserve_gb ~= 8 (headless Linux) .. 16 (Windows desktop / Studio present)
```
Examples: 
- 16 GB host: 16 − 8 = 8 GB → **2 AVDs at 4 GB** or **4 AVDs at 2 GB** (and Studio would not fit).
- 64 GB host: **14 AVDs** at 4 GB / **28** at 2 GB.
- 128 GB: 30 / 60.
- **64 AVDs at 4 GB needs 256 GB RAM + reserve (≈264–272 GB); 64 at 2 GB needs ≈128–136 GB.**
  So BRIEF §2's "up to 64 concurrent instances on a large host" is only realistic for API ≤ 36 at
  2 GB/AVD on a ~136 GB host, or API 37+ on a ≥272 GB host. Capacity checks in `runtime.capacity()`
  should compute this and expose `capacity`, `ram_per_instance`, `disk_estimate` rather than assume
  64 is achievable. `Template.disk_estimate_mb = max(2048, ram*2+3072)` is in the right spirit; the
  Android page's "up to 6 GB extra per AVD" suggests a larger default for API 37+ (data partition is
  6 GB on Play images by default, per emulator history).
- CPU: emulator docs don't publish a cores/AVD figure; practical guidance is ≥2 vCPU per running x86
  AVD plus host overhead (**INFERRED**; 64 AVDs therefore imply 128+ vCPU, but 64 concurrent AVDs
  will be RAM-bound long before CPU-bound). `Config.default_cpu_cores=2` is fine.
- Windows-specific: the emulator reserves commit charge = guest RAM; exceeding the Windows commit
  limit (physical RAM + pagefile) fails to launch. Fix: free RAM, grow/auto-manage the pagefile
  (troubleshooting page, VERIFIED). A large fleet on Windows needs a large fixed/auto pagefile.
- Software rendering (`-gpu software`) burns host CPU per instance, so the "16 GB RAM → 4 small
  AVDs" figure is optimistic with software rendering; test on the target host.

---

## 6. Linux package dependencies per distro

### 6.1 Verified inputs
- KVM stack (Ubuntu command on the Android acceleration page):
  `qemu-kvm libvirt-daemon-system libvirt-clients bridge-utils`; check with `cpu-checker` (`kvm-ok`).
- Studio 64-bit runtime libraries (Android Studio install page, VERIFIED):
  - Ubuntu/Debian: `sudo apt-get install libc6:i386 libncurses5:i386 libstdc++6:i386 lib32z1 libbz2-1.0:i386`
  - Fedora: `sudo yum install zlib.i686 ncurses-libs.i686 bzip2-libs.i686`
- Emulator troubleshooting page (VERIFIED): on ChromeOS-like/Minimal Linux, **`libnss3`** is a
  missing dependency that blocks AVD launch (`sudo apt install libnss3`).
- Emulator 37.3.10 release notes (VERIFIED): Linux startup no longer fails when `libXdmcp` or
  `libbsd.so.0` is missing — the emulator now ships a hermetic `libxcb-aemu.so`. Older emulators
  needed those system libs.
- Arch: `extra/android-tools 37.0.0.p1` (adb/fastboot), `extra/android-udev 20260922` (USB/udev rules
  for real devices), `extra/scrcpy 5.0.1`, `extra/jdk21-openjdk 21.0.13.1.u2`, `extra/jdk25-openjdk
  25.0.5.1.u2`, `extra/nodejs 26.13.1` (VERIFIED, Arch package index).
- Android's emulator is a Qt/OpenGL application. **A per-distro package list for the emulator's GUI
  libraries is not published** (the SDK zip bundles most of its own libs). The following are
  commonly required and should be verified with `ldd`/a launch smoke test on each distro:
  `libpulse0`, `libgtk-3-0`, `libx11-6`, `libxcomposite1`, `libxcursor1`, `libxi6`, `libxrandr2`,
  `libxrender1`, `libvulkan1`, `libglu1-mesa`, `libasound2` — **UNVERIFIED as the canonical set.**

### 6.2 Recommended install lines (mix of VERIFIED and derived; mark in docs)

| Distro | Command (run as admin) | Status |
|---|---|---|
| Ubuntu LTS / Debian | `apt-get install -y qemu-kvm libvirt-daemon-system libvirt-clients bridge-utils cpu-checker libnss3 libpulse0 libgtk-3-0 libvulkan1 libx11-6 libxcomposite1 libxcursor1 libxi6 libxrandr2 libglu1-mesa` + 32-bit Studio libs above | KVM parts **VERIFIED**; GUI lib list **UNVERIFIED** |
| Fedora | `dnf install -y qemu-kvm libvirt-daemon-kvm libvirt-client libnss3 pulseaudio-libs gtk3 vulkan-loader mesa-libGLU libX11 libXcomposite libXcursor libXi libXrandr` + `zlib.i686 ncurses-libs.i686 bzip2-libs.i686` | Studio i686 part **VERIFIED**; rest **UNVERIFIED** |
| Rocky/RHEL 9 | `dnf install -y qemu-kvm libvirt-daemon-kvm libvirt-client` (EPEL may be needed for some GUI libs) | **UNVERIFIED**; glibc 2.34 OK (**INFERRED**) |
| Arch | `pacman -S --needed qemu-base libvirt android-tools android-udev scrcpy jdk21-openjdk nodejs` (+ GUI libs: `libpulse gtk3 vulkan-icd-loader libxcomposite libxcursor libxi libxrandr glu`) | package names **VERIFIED** for android-tools/android-udev/scrcpy/jdk/nodejs; GUI libs **UNVERIFIED** |

### 6.3 udev rules / groups
- **KVM access:** user must be able to open `/dev/kvm`. Common approaches: add the user to the `kvm`
  group (`usermod -aG kvm <user>`; requires re-login) or a udev rule
  `KERNEL=="kvm", GROUP="kvm", MODE="0666"` (the latter is what GitHub Actions uses — VERIFIED from
  the changelog). This is a **`--fix` candidate for `doctor`** on Linux.
- **Real USB devices:** not needed for AVDs (emulator adb is loopback TCP), but Studio/adb with
  physical devices needs the Android udev rules: install `android-udev` (Arch, VERIFIED) or the
  vendor rules documented at <https://developer.android.com/studio/run/device>. Fleet AVD use does
  not require this; keep it out of critical path.
- **Nested KVM (only when deliberately running inside a Linux VM):** `options kvm_intel nested=1`
  in `/etc/modprobe.d/kvm_intel.conf` (Arch Wiki, VERIFIED) — but see §3.1: not a supported way to
  run the Android emulator.

---

## 7. macOS prerequisites (Command Line Tools, Homebrew, Gatekeeper)

**VERIFIED**
- Studio minimum **macOS 12**; Apple M1+ recommended; Intel supported but being phased out
  (minimum 6th-gen Intel). Emulator page repeats macOS 12+.
- **CLT:** macOS development requires **Xcode Command Line Tools** (`xcode-select --install`);
  Homebrew documents CLT (or Xcode) as required when building formulae from source
  (<https://docs.brew.sh/Installation>). The BRIEF §3.2 requirement "macOS installs Command Line
  Tools before Homebrew" is correct and necessary.
- **Homebrew now requires macOS 15 (Sequoia) or higher** on officially supported hardware; macOS 11–14
  are "unsupported but may work", ≤10.15 will not run Homebrew at all. Default prefix is
  **`/opt/homebrew`** on Apple Silicon (and `/home/linuxbrew/.linuxbrew` on Linux); non-interactive
  install with `NONINTERACTIVE=1`.
  → **Installer consequence:** on macOS 12–14 (which Studio still supports), Homebrew is not a
  supported dependency. The bootstrap must prefer direct downloads (Studio dmg, cmdline-tools zip,
  Node tar.gz, uv standalone) and only *optionally* use brew on macOS 15+.
- **Gatekeeper/quarantine:** after manually unzipping the emulator, Android documents
  `xattr -dr com.apple.quarantine emulator/` to reduce "blocked/checked" prompts (emulator archive
  page, VERIFIED). The portable launcher's Windows `Unblock-File` equivalent on macOS is `xattr
  -d com.apple.quarantine` — apply to downloaded launchers/emulator after first download.
- **HVF:** built-in, nothing to install (§2.4).
- **macOS 26+:** emulator 37.3.10's KosmicKrisp Vulkan-on-Metal driver is available — good news for
  future macOS hosts; older macOS uses MoltenVK/software.

---

## 8. Windows specifics: long paths, Defender, reboot, paths

**VERIFIED**
- **Long paths:** Win32 `MAX_PATH` is **260 characters**; extended-length paths use the `\\?\`
  prefix and allow up to ~32,767 characters. Since Windows 10 1607 many file APIs can exceed MAX_PATH
  **only if opted in** via registry
  `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled = 1` (REG_DWORD; the value is
  cached per process after first call). PowerShell:
  `New-ItemProperty -Path HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force`.
  → Installer should set this (admin) **and** still keep the SDK/AVD/bundle roots short and ASCII;
  AVD `.ini`/`config.ini` contain absolute paths, so deep user directories are a real failure mode for
  a fleet (also relevant to relocation, R7).
- **Non-ASCII paths:** emulator 37.3.10 fixed a Windows hang when the AVD/user directory path
  contains non-ASCII characters (release notes). Still: warn in `doctor` and prefer
  `%LOCALAPPDATA%\avdfleet`-style ASCII roots.
- **Defender/antivirus:** Android's troubleshooting page says security/antivirus software that scans
  every file access slows the emulator and recommends adding the **emulator application to the
  antivirus trusted-exclusion list**. In practice the installer/`doctor --fix` should (with explicit
  consent) add Defender exclusions for: the emulator directory, the SDK root, the AVD home, the
  avdfleet data/cache dirs, and the bundle root (portable). Use
  `Add-MpPreference -ExclusionPath` (Defender) — **UNVERIFIED** command syntax on this host; verify
  before shipping.
- **Reboot:** enabling **Windows Hypervisor Platform** requires a restart (acceleration page). This
  is the single reboot in BRIEF R-INSTALL. The installer must persist state (`InstallManifest`) and
  resume; after reboot, re-check `emulator -accel-check` / WHPX presence before continuing.
- **Commit charge/pagefile:** the emulator reserves guest RAM as commit; Windows fails launches when
  the commit limit (RAM + pagefile) is exceeded. Fleet hosts on Windows need a large pagefile and
  `doctor` should warn when `CommitLimit - Committed` is small relative to planned AVD RAM
  (troubleshooting page, VERIFIED).
- **Hyper-V/VBS:** on Windows 11 with Core Isolation/Memory Integrity enabled, AEHD cannot run —
  WHPX can. `doctor` should detect Hyper-V present and select WHPX accordingly.
- **PowerShell:** `install.ps1` will be executed in a fresh PowerShell; execution policy may block
  scripts — invoke via `-ExecutionPolicy Bypass`, and portable launchers should `Unblock-File`
  downloaded scripts (Mark-of-the-Web). (BRIEF already references the execution-policy doc.)
- **Intel hybrid CPUs:** headless emulator on Windows with Intel hybrid CPUs was power-throttled onto
  E-cores (2–3× slower cold boot); fixed in emulator 37.3.10. Pin ≥37.3.10 for Windows fleet hosts
  with P/E cores.
- **Windows ARM64:** not supported by Studio, and stable emulator has no windows_aarch64 package
  (§1.1). Detect and refuse with a clear message.

---

## 9. What this means for the installer/`doctor` (P1/I4/N1 inputs)

1. **Detect and classify** before installing:
   - `HostOS` × `CpuArch` → only `windows/x64`, `macos/arm64`, `macos/x64`, `linux/x64` are supported;
     refuse `windows/arm64`, `linux/arm64` with an explicit reason.
   - Accelerator: WHPX (Windows, `Windows Hypervisor Platform` feature on/available), AEHD
     (`sc query aehd`), KVM (`/dev/kvm` + writable + `kvm_ok`), HVF (macOS) → `AcceleratorInfo.kind`.
   - Nested/VM: `is_container` / hypervisor-detected-without-usable-HVF/KVM → set `nested=True` and
     make it a **WARN** ("AVDs cannot be accelerated in a VM; real-emulator tests will be skipped"),
     never a hard failure of `doctor` on a CI box.
2. **Acceleration steps per host:** Windows WHPX feature + reboot + resume; Linux KVM packages +
   `kvm` group/udev + verify `/dev/kvm`; macOS nothing (HVF) but install CLT if using brew.
3. **Do not install AEHD by default**; offer migration path until 2026-12-31, remove afterwards.
4. **GPU:** default `-gpu software` for headless; allow `host` where a GPU is detected; never emit
   deprecated `*_indirect` modes. Change core defaults accordingly (§4.3).
5. **Capacity:** compute from actual RAM/commit-limit, 4 GB/AVD (API 37+) or 2 GB/AVD (≤36), 6 GB
   disk/AVD, 8–16 GB host reserve; surface a real maximum rather than 64 unconditionally.
6. **CI/cloud:** treat hardware acceleration as Linux-x64-only and best-effort; skip real-emulator
   tests elsewhere (matches BRIEF §6).

---

## 10. Where BRIEF.md / frozen core appears wrong or under-specified (R2 findings)

| # | BRIEF / core statement | Finding (2026-11-26) | Proposed change |
|---|---|---|---|
| R2-1 | `GpuMode` enum + defaults use `swiftshader_indirect` (and `angle_indirect`); §3.3 says launch with `-gpu <mode>` | Both `*_indirect` modes are **deprecated** (swiftshader_indirect/swangle_indirect/guest since emulator 36.4.9; angle_indirect since 34.2.14). `off` is not documented on the current page. Supported headless mode is **`software`** (plus `lavapipe`, `swiftshader`, `swangle`) | Change `GpuMode` default → `SOFTWARE`; add `SOFTWARE`, `SWANGLE`, `LAVAPIPE`; keep deprecated values parse-only; drop reliance on `off` (or mark legacy). Contract change → PLAN Decisions |
| R2-2 | R-INSTALL §2 lists "fresh **Windows 11**" as the host baseline | Studio/emulator minimum is **Windows 10 64-bit** (emulator WHPX needs 1803+); Windows **ARM64 is not supported** | Keep Win 11 as *recommended*, document Win 10 1803+ as supported, and hard-refuse Windows ARM64 |
| R2-3 | §2 lists "macOS (Apple Silicon)" only | Studio still supports **macOS Intel x64** (min macOS 12, 6th-gen Intel; being phased out); emulator ships `darwin_x64` | Decide: support Intel macOS in v0.1 (it is cheap — same tarball path) or document as unsupported-but-untested. Also note Homebrew requires macOS 15+ → don't depend on brew on 12–14 |
| R2-4 | §3.2 "AEHD → WHPX migration" | AEHD **sunsets 2026-12-31** (≈5 weeks after this report); SDK package will disappear; AEHD cannot coexist with Hyper-V/VBS | Make WHPX the only default on Windows; AEHD opt-in/migration only; schedule removal from installer code path |
| R2-5 | §3.3/§6 assume "capacity up to 64" is a host-capability question | Port range supports exactly 64 pairs (5554–5682), but **RAM is the real ceiling**: docs say **~4 GB per additional AVD**, and API 37+ enforces 4 GB minimum. 64 × 4 GB ≈ 256 GB RAM + reserve | Keep 64 as a hard protocol/port limit; make capacity RAM-derived; document realistic host sizing (≈136 GB for 64 × 2 GB API ≤36; ≈272 GB for API 37+) |
| R2-6 | `Config.default_ram_mb=2048`, `MIN_RAM_PER_INSTANCE_MB=1536` | Fine for API ≤ 36, but **API 37+ forces ≥4 GB**; the emulator overwrites smaller values | Add per-API minimum (`4096` for API ≥ 37) in capacity/AVD config validation and surface a warning when a template targets API 37 with <4096 |
| R2-7 | BRIEF §6 "GitHub calls nested virtualisation unsupported" | Correct: GitHub docs say nested VMs are experimental/unsupported, and the emulator's own docs say accelerated AVDs cannot run inside a VM. KVM on GitHub Linux x64 works only with the group+udev step; 2-vCPU runners now included | Keep BRIEF policy; add the exact udev snippet to Q1 workflow docs and to `doctor` hints; state Windows/macOS GH runners cannot run accelerated AVDs |
| R2-8 | §3.2 "Linux KVM udev rules and group membership" (singular) | There are two different things: **KVM access** (`/dev/kvm`, `kvm` group/udev) and **USB device udev rules** (`android-udev`) — the latter is unnecessary for AVD fleets | Split into two `doctor` checks: `kvm_access` (critical) and `usb_udev_rules` (optional, for physical devices) |
| R2-9 | Host requirement table missing Linux glibc floor | Studio requires **glibc ≥ 2.31** (any 64-bit distro with GNOME/KDE/Unity). RHEL/Rocky **8** (glibc 2.28) therefore cannot run Studio (**INFERRED**); RHEL/Rocky 9 (2.34) can | Make `doctor` check glibc ≥ 2.31 and refuse Rocky/RHEL 8; document Rocky/RHEL 9+ |
| R2-10 | Windows details absent from BRIEF (long paths, Defender, commit charge) | `MAX_PATH`=260 with opt-in `LongPathsEnabled`; AV exclusions recommended by Android docs; commit charge/pagefile limits AVD count; non-ASCII paths bug fixed only in emulator 37.3.10 | Add Windows `doctor --fix` checks: long paths registry, AVD/SDK path ASCII+short, Defender exclusion hint, commit-limit headroom; pin emulator ≥37.3.10 |
| R2-11 | macOS prerequisites only "Command Line Tools before Homebrew" | Correct but insufficient: **Homebrew requires macOS 15+** while Studio supports macOS 12+; `/opt/homebrew` vs `/usr/local`; quarantine `xattr` for manually installed emulator | Bootstrap must not require Homebrew on macOS 12–14; add quarantine-clear step; detect both brew prefixes |

---

## 11. Sources (all accessed 2026-11-26)

Android (first-party)
- <https://developer.android.com/studio/run/emulator> — emulator system requirements (16 GB RAM, Win10+/macOS 12+/Linux/ChromeOS, 16 GB disk)
- <https://developer.android.com/studio/run/emulator-acceleration> — GPU modes + deprecations, WHPX/AEHD/KVM/HVF setup, no-nested-VM restriction, `-accel-check`, AEHD sunset
- <https://developer.android.com/studio/install> — Studio system requirements per OS, RAM/disk/GPU, Windows/Linux ARM unsupported, multi-AVD ~4 GB each + ~6 GB disk each, glibc 2.31, Ubuntu/Fedora 32-bit libs
- <https://developer.android.com/studio/releases/emulator> — 37.3.10/37.2.12/37.1.11/36.6.11/36.5.10/36.4.9 release notes (AEHD sunset, API 37 4 GB min, software GPU modes, libxcb-aemu, fixes)
- <https://developer.android.com/studio/run/emulator-commandline> — `-memory` 1536–8192, `-port`/`-ports` 5554–5682 / 64 devices, `-no-window`, `-no-audio`, `-no-boot-anim`, `-read-only`, `-accel`
- <https://developer.android.com/studio/run/emulator-troubleshooting> — antivirus exclusion advice, Windows commit charge/pagefile, `libnss3`, Vulkan fallback, `-gpu swiftshader`
- <https://developer.android.com/studio/emulator_archive> — `xattr -dr com.apple.quarantine emulator/`, manual install
- <https://developer.android.com/studio/run/device> — USB udev rules for physical devices

Linux
- <https://wiki.archlinux.org/title/KVM> — `/dev/kvm` permissions, `kvm` group, `kvm_intel nested=1`, module/BIOS checks
- <https://archlinux.org/packages/> — `android-tools 37.0.0.p1`, `android-udev 20260922`, `scrcpy 5.0.1`, `jdk21-openjdk`, `jdk25-openjdk`, `nodejs 26.13.1` (verified via package search JSON)
- <https://repology.org/api/v1/project/{scrcpy,uv,nodejs,openjdk}> — Debian/Ubuntu/Fedora/Rocky package/version cross-check
- <https://help.ubuntu.com/community/KVM/Installation> (linked from Android docs; currently returns 404 — recorded as dead link; KVM commands instead taken verbatim from the Android acceleration page)

macOS
- <https://docs.brew.sh/Installation> — Homebrew macOS 15+ requirement, `/opt/homebrew`, Xcode CLT/Xcode prerequisite, `NONINTERACTIVE=1`

Windows
- <https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation> — MAX_PATH 260, `\\?\` 32,767, `LongPathsEnabled` registry, opt-in since Win10 1607

CI/cloud
- <https://github.blog/changelog/2024-04-02-github-actions-hardware-accelerated-android-virtualization-now-available/> — 2-vCPU Linux KVM support, group + udev rule snippet
- <https://docs.github.com/en/actions/concepts/runners/about-github-hosted-runners> — nested virtualisation experimental/unsupported statement
- <https://developer.android.com/studio/run/emulator-acceleration> — "can't run VM-accelerated emulator inside another VM"

BRIEF/core files read (Wave 0, frozen): `BRIEF.md` §1–3, §6–7; `PLAN.md`;
`contracts/fakes-spec.md`; `src/avdfleet/core/{models,config,paths,ports,interfaces,errors,__init__}.py`.

**Not verified / gaps for R2**
- Exact GitHub-hosted runner RAM/vCPU by repo visibility (change frequently).
- Per-distro canonical GUI library list for the emulator (no official per-distro table; verify via
  `ldd`/launch smoke test).
- Rocky/RHEL 9 package names for KVM/QEMU and Studio GUI libs (dnf names inferred).
- `Add-MpPreference` Defender-exclusion syntax (not exercised on the build host).
- glibc versions of each RHEL/Rocky release (used for the RHEL 8 = 2.28 derivation; confirm per release).
- Actual `-gpu off` support in current emulator (documented list omits it).
- Nested-virt enablement per cloud provider/SKU.
