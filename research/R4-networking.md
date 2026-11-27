# R4 — Android Emulator networking and control (research)

Status: research only. No emulator was booted and no SDK was installed: this build host has
no usable hypervisor (`VirtualizationFirmwareEnabled=False`, no WHPX/KVM/HVF), so **every
statement below is derived from documentation or AOSP/emulator source, not from a live
boot**. Labels:

- **[VERIFIED]** — stated explicitly in the cited official documentation or source.
- **[PARTIAL]** — directly evidenced for one layer/version but not the combined behaviour.
- **[INFERRED]** — reasoned from cited code/docs; must be confirmed on a real accelerated host.
- **[UNVERIFIED]** — plausible but no primary source found; do not build acceptance criteria on it.

Accessed: **2026-11-26**. Relevant current versions at that date: emulator stable
**37.3.10** (2026-11-19), the "new networking stack" shipped in **36.5.10** (2026-04-02);
Appium/UiAutomator2 port facts are in `research/R5-appium.md`.

## 0. Direct answers

### 0.1 Does `-http-proxy` cover DNS?

**No. [PARTIAL → effectively VERIFIED]**
`-http-proxy` forwards **TCP** through an HTTP/HTTPS proxy; DNS from the guest is sent to the
emulator's virtual router at `10.0.2.3` (…`.6`) and resolved by the emulator *on the host*
(host resolver / `resolv.conf` / `GetNetworkParams`), not through the proxy. The emulator's
own proxy layer explicitly refuses non-TCP sockets. Consequences:

- the proxy sees `CONNECT host:port` (or rewritten plain-HTTP `Host:`), so the *proxy* still
  sees hostnames for proxied TCP, but the **DNS query itself escapes the assigned upstream
  proxy** (resolved by the emulator host's resolver);
- `-dns-server` only changes *which* DNS servers the emulator forwards to; it does not make
  DNS go through `-http-proxy`;
- a per-instance DNS policy therefore needs a **per-instance DNS server given via
  `-dns-server`** (host-side, controlled/logged) or host-level egress rules — the emulator
  proxy cannot do it.

### 0.2 Does `-http-proxy` cover QUIC/UDP?

**No. [VERIFIED]** Google's own emulator-proxy page states the Emulator Proxy "doesn't
support UDP redirection"; the AOSP proxy implementation only accepts `SOCKET_STREAM`
connections ("the HTTP proxy can only handle TCP connections"). Any UDP egress — QUIC/HTTP3
(`UDP/443`), DNS (`UDP/53`), DTLS, gaming/UDP app traffic, mDNS — goes out through the
emulator's virtual router/NAT **without the proxy**. The emulator also does not support ICMP
(ping) or IGMP at all.

### 0.3 Does `-http-proxy` cover traffic that bypasses the Android system proxy?

**Yes for TCP, at the emulator network layer. [PARTIAL]** The Emulator Proxy is not the
Android System Proxy: it intercepts at the emulator/QEMU socket layer *below* the guest OS,
so guest apps that ignore `http_proxy`/Wi-Fi proxy settings are still tunnelled (all TCP is
sent to the proxy). Caveats:

- **Both proxy mechanisms are mutually exclusive** ("These two proxies are mutually
  exclusive; you can only have one enabled at a time") — you cannot use the Android System
  Proxy for HTTPS interception at the same time as the Emulator Proxy.
- The interception is inside the **classic QEMU/slirp data path**. With the 36.5+ shared
  Wi-Fi/netsim stack the guest Wi-Fi traffic is handled by `netsimd`, not by the QEMU slirp
  socket hooks; whether the Emulator Proxy still applies to that traffic is **not stated by
  Google** and must be measured. **[INFERRED: likely bypassed on 36.5+ Wi-Fi; reverted by
  `-feature -WiFiPacketStream`, which Google documents as the way back to the legacy model.]**
- Traffic to the host loopback alias `10.0.2.2`, SMB/TFTP-style slirp services, and the
  emulator's own control paths were not verified to pass through the proxy. **[UNVERIFIED]**

### 0.4 Is a local HTTP CONNECT/SOCKS5 bridge required for strict per-instance isolation?

**Yes, for this product. [INFERRED from VERIFIED facts]** A local bridge per instance is
required (or at minimum strongly recommended) because:

1. `-http-proxy` accepts up to **HTTP proxy only** (`http://[user:pass@]host:port`). It
   cannot talk to a **SOCKS5** upstream, which R-PROXY requires as an option. A local bridge
   that accepts the emulator's HTTP `CONNECT` and dials either HTTP or SOCKS5 upstream is
   needed. **[VERIFIED for HTTP-only flag format; bridge requirement is INFERRED]**
2. Passing `http://user:password@host:port` to the emulator puts the **credential in the
   emulator process argv** (and source shows it being turned into a `Proxy-authorization:
   Basic` header). R-PROXY forbids credentials in `ps`/logs. With a bridge the emulator only
   ever sees `http://127.0.0.1:<bridge_port>` with no credentials; the bridge holds the
   secret. **[VERIFIED argv format; R-PROXY implication is repo policy]**
3. `-http-proxy` has **no bypass/"no-proxy-for" list**, no per-request auth, no logging, and
   cannot provide the token-canary evidence G1 needs at the listener. **[VERIFIED]**
4. It cannot stop **UDP/QUIC/DNS** egress; a bridge also cannot, from inside an unrooted
   guest. Strict "only the assigned proxy" therefore additionally needs host-level controls
   (per-instance DNS resolver given via `-dns-server`, UDP canary detection, optional host
   firewall/namespace routing) and must be documented as a limitation rather than claimed as
   guaranteed. **[INFERRED]**

Detailed evidence and the recommended fallback stack are in §3–§4 and §12.

---

## 1. Emulator network model (classic vs "new networking stack")

### 1.1 Virtual router / address space [VERIFIED]

Each instance has its own virtual `10.0.2.0/24` router. Pre-allocated addresses:

| Address | Meaning |
|---|---|
| `10.0.2.1` | Router/gateway |
| `10.0.2.2` | Alias for the host loopback `127.0.0.1` |
| `10.0.2.3` | First DNS server; `.4/.5/.6` optional 2nd–4th |
| `10.0.2.15` | Guest address when using Ethernet |
| `10.0.2.16`, `.17`, … | Guest address(es) when using Wi-Fi; from emulator 36.5 multiple Wi-Fi instances get sequential addresses |
| `127.0.0.1` | Guest loopback |

Outbound **TCP and UDP** are NATed by the emulator; ICMP and IGMP are not supported; the
virtual router isolates instances from the host network and from each other (pre-36.5).
"[The emulator] detects only that it is connected through ethernet to a router or firewall."

### 1.2 Emulator 36.5+ "new networking stack" / netsim [VERIFIED]

- Release notes 36.5.10 (2026-04-02): "The Android Emulator now features a new networking
  stack to improve multi-device testing, eliminating the need for manual port forwarding …
  AVDs can now automatically discover and communicate with each other as if they are on the
  same virtual network" (Wi-Fi Direct, NSD). Enabled by default from 36.5.
- Interconnect doc: from 36.5 instances share a virtual Wi-Fi network; you get addresses
  with `ip addr show wlan0`; to revert: **`-feature -WiFiPacketStream`** (exact documented
  spelling).
- Console doc: "Starting with Android Emulator 36.5 and later, Wi-Fi connection traffic is
  handled by netsim by default. The network capture console command only captures legacy
  cellular/WAN traffic routed through the QEMU slirp stack."
- Netsim is configured with `-netsim-args` (`--pcap`, `--logtostderr`, `--hci-port`,
  `--wifi SSID PASSWORD`, `--rssi …`); artifacts go to `$ANDROID_TMP/android/netsimd/`.
  Troubleshooting: "You can only apply command line network settings specified with
  `-netsim-args` when you launch the *first* emulator instance … You can't configure
  different network settings using command line arguments for multiple emulators running
  concurrently." **[VERIFIED — important multi-instance limitation]**

**Implication for a fleet**: per-instance networking flags (`-netsim-args`, and possibly
`-http-proxy` — see the note "When running multiple emulators, this flag should be set on
the first launch" in §3.2) are not guaranteed to be per-instance on 36.5+. Pin the emulator
version and verify per-instance proxy behaviour on the target version before shipping.

### 1.3 Legacy feature flag `VirtioWifi` and how to disable features [VERIFIED syntax]

`-feature` exists and `-feature -<Name>` disables a feature. Google documents this form in
several places, e.g. "launch … with the flag `-feature -Vulkan`" (troubleshooting; release
notes 35.3.11 "Really disable Vulkan API calls when running with `-feature -Vulkan`"), and the
network-revert flag `-feature -WiFiPacketStream` (interconnect doc). AOSP source
(`android/emu/feature/src/android/featurecontrol/FeatureControlImpl.cpp`,
`emu-36-1-release` branch) shows the exact override parser:

- sources, in order: hw config `hw_featureflags`, env **`ANDROID_EMULATOR_FEATURES`**,
  repeated `-feature <list>` cmdline args, plus a **user file
  `<user dir>/advancedFeatures.ini`** (`~/.android/advancedFeatures.ini` on Windows/macOS/
  Linux by default);
- list syntax: comma-separated names, `-` prefix disables (`-VirtioWifi,-Vulkan`), names are
  matched case-sensitively against the compiled feature table;
- defaults come from **`<emulator launcher dir>/lib/advancedFeatures.ini`** (host) combined
  with the **system image's own feature ini** (guest features require host AND guest to
  enable them).

Per-image example (AOSP prebuilts, API 35 `google_apis/x86_64/advancedFeatures.ini`) includes:

```
VirtioWifi = on
Mac80211hwsimUserspaceManaged = on
VirtioInput = on
...
```

So `VirtioWifi` is a **guest/image feature** (virtio Wi-Fi NIC), not the 36.5 packet-stream
stack. Two different knobs:

| Knob | What it controls | Evidence |
|---|---|---|
| `-feature -VirtioWifi` | Disables virtio Wi-Fi for images that support the legacy path (guest falls back to Ethernet/legacy Wi-Fi). Historically the workaround for Wi-Fi/emulator-proxy interaction bugs. | `VirtioWifi = on` in per-image ini; flag syntax verified; **effect on `-http-proxy` coverage NOT verified** |
| `-feature -WiFiPacketStream` | Reverts the 36.5+ shared virtual-Wi-Fi/netsim model to the previous networking model | Official interconnect doc |
| `-feature -Vulkan` | Example of another feature flag | Official troubleshooting doc |

Do **not** assume `-feature -VirtioWifi` is the fix for proxy isolation on 36.5+; the
documented revert flag for the new stack is `-feature -WiFiPacketStream`. Whether raw
Ethernet-only (`-feature -VirtioWifi` or forcing ethernet) makes `-http-proxy` cover all
guest traffic on 36.5+ is **[UNVERIFIED — must be measured; see §11 test plan]**.

### 1.4 `-netdelay`, `-netspeed`, `-dns-server`, `-no-passive-gps`

- `-netdelay <delay>` / `-netspeed <speed>`: emulated latency/bandwidth. Docs: "This setting
  applies to **Ethernet and Cellular**." Console equivalent `network delay|speed` says
  "(Ethernet and Cellular only)". With 36.5+ Wi-Fi traffic handled by netsim these legacy
  throttles do not cover the Wi-Fi path. **Not an isolation control. [VERIFIED]**
- `-dns-server <serverList>`: comma-separated up to four DNS servers. Default: emulator reads
  host resolv.conf (Linux/macOS) or `GetNetworkParams()` (Windows) and aliases them at
  `10.0.2.3`–`.6`; the emulator ignores the host `hosts` file. This is the only emulator-level
  hook for per-instance DNS steering; it is **not** a proxy. **[VERIFIED]**
- `-no-passive-gps`: irrelevant to networking/proxy; not a proxy or DNS control.
  **[VERIFIED-irrelevant]**
- `-tcpdump file`: captures all Ethernet packets on the virtual LAN (not just TCP) — useful
  for leak evidence in G1 if the new stack still exposes the legacy interface. **[VERIFIED
  flag; captures on netsim path UNVERIFIED]**

---

## 2. Android System Proxy vs Emulator Proxy (`-http-proxy`) — the exact model

Google's emulator-networking-proxy page defines two mechanisms that are **mutually
exclusive** (only one enabled at a time):

| | Android System Proxy | Emulator Proxy (`-http-proxy`) |
|---|---|---|
| Layer | Application layer in guest | Network layer below guest |
| Use case | App traffic inspection (Charles etc.) | Firewall bypass, "route all TCP" |
| Config | Android Settings → Wi-Fi → proxy, or instrumentation/UI automation | `-http-proxy`, `http_proxy` env var, Extended Controls → Proxy |
| HTTPS inspect | Yes (with installed CA) | No — TCP tunnel, no decryption |
| Non-HTTP(S) traffic | Mostly no | **"Forwards all TCP traffic"** |
| UDP | — | **Not supported** |
| Bypass list | Apps may ignore it | No "no-proxy-for" list |

### 2.1 Flag semantics [VERIFIED]

- `emulator @MyAvd -http-proxy http://host:port` or
  `-http-proxy http://username:password@host:port`; `http://` prefix may be omitted.
- If the flag is absent, the emulator reads the `http_proxy` environment variable and uses it
  if it matches the proxy format.
- Extended Controls → Settings → Proxy stores the proxy **globally for the emulator**
  (restart required to apply).
- **"When running multiple emulators, this flag should be set on the first launch."**
  (Verbose/ambiguous; treat as a warning that it may not be a reliably per-instance option
  on all versions. **[PARTIAL]**)
- Android Studio passes its own HTTP-proxy settings into the Emulator Proxy on launch; those
  do not affect the Android System Proxy.

### 2.2 What the AOSP implementation does [VERIFIED from source]

Source: `android/emu/proxy/` in `platform/external/qemu` (branch `emu-36-1-release`; same
files exist on `emu-master-dev`).

- `android_http_proxy_set(proxy)` (http_proxy.c) removes or installs one global HTTP proxy
  service; it is the entry point behind the flag/env var and the console `proxy set|clear`
  command.
- `android_http_proxy_setup` parses the URL, does a **1-second TCP reachability pre-check**
  ("Could not connect to proxy … Proxy will be ignored !"), then calls `proxy_http_setup`
  with optional `PROXY_OPTION_AUTH_USERNAME` / `PROXY_OPTION_AUTH_PASSWORD`.
- `proxy_http.c` builds a `Proxy-authorization: Basic <base64(user:pass)>` footer and
  registers one service; per connection it chooses:
  - **port 80 → HTTP rewriter** (absolute-URI request rewriting), anything else →
    **`CONNECT host:port HTTP/1.1`** connector;
  - `http_service_connect` starts with: "the HTTP proxy can only handle TCP connections" —
    if `sock_type != SOCKET_STREAM` it returns `NULL` (no proxy).
  - a direct connection to the proxy host itself is left unproxied.
- `proxy_check_connection` resolves the **proxy hostname locally** and connects; the upstream
  proxy resolves the destination.
- Only **one** HTTP proxy service exists in the manager (`MAX_SERVICES 4`, one used) and
  "try change proxy on the fly" tears the old one down; the manager is process-global.

### 2.3 Consequences

1. **TCP-only.** UDP, QUIC, DNS, ICMP, IGMP all bypass. **[VERIFIED]**
2. **No SOCKS5** via the flag — only HTTP proxy schemes are accepted. **[VERIFIED]**
3. **Credentials live in argv/env** when using `-http-proxy`, and are base64'd into headers;
   use the bridge to keep them off `ps`. **[VERIFIED format / repo-policy requirement]**
4. **HTTPS is tunneled, not inspectable**; no CA injection. **[VERIFIED]**
5. **No exclusion list**; no per-destination policy; all-or-nothing for TCP. **[VERIFIED]**
6. On 36.5+ the new Wi-Fi/netsim stack may carry guest Wi-Fi traffic outside the QEMU slirp
   socket path the proxy hooks. Whether `-http-proxy` still sees that traffic is
   **[UNVERIFIED — highest-priority real-emulator test]**.

---

## 3. DNS and UDP coverage — leak matrix

| Egress type | Guest path | Covered by `-http-proxy`? | Covered by Android System Proxy? | Notes |
|---|---|---|---|---|
| TCP/443 (HTTPS) | guest → router → host TCP → proxy CONNECT | **Yes** (TCP) | Only apps honouring system proxy | cert not inspected by emulator proxy |
| TCP/80 (HTTP) | rewritten absolute-URI to proxy | **Yes** (TCP) | only system-proxy-aware apps | proxy sees `Host:` |
| TCP other ports | proxy CONNECT | **Yes** (TCP) | no | e.g. 8080, 5228 (GCM) |
| DNS UDP/53 (and TCP/53) | guest → `10.0.2.3` → emulator host resolver | **No** for the query; destination hostnames appear in URI/Host of proxied TCP | no | host resolver sees queries; emulator ignores host `hosts` file |
| QUIC / UDP/443, other UDP | guest → router → host UDP NAT | **No** | no | QUIC-capable apps (Chrome, Google apps, media) will leak |
| ICMP / IGMP | unsupported by emulator | n/a | n/a | ping fails by design |
| Host loopback via `10.0.2.2` | guest → host loopback | **[UNVERIFIED]** | no | proxy source explicitly allows direct connection to the proxy host only |
| Emulator control (console/gRPC/adb) | host-local | n/a | n/a | never proxy the control plane |

Detection guidance for G1 (works against fakes and real):

- a per-instance token URL fetched over **HTTP(S)** proves which upstream listener saw TCP
  (the core G1 evidence);
- **UDP canary**: run a UDP listener on a host address the guest can reach (e.g. host's
  LAN IP via a `redir add udp:` mapping or a host-side UDP echo) and have the guest send a
  UDP datagram to it — if it arrives, UDP is not isolated; a QUIC/HTTP3 URL fetch with
  packet capture (`-tcpdump`, netsim `--pcap`) makes the leak visible;
- **DNS canary**: give each instance a distinct `-dns-server` pointing at a per-instance
  logging resolver; if you see the query, DNS escaped the proxy by design (document it);
- assert proxy credentials/tokens never appear in logs, argv, API responses (repo policy
  already covers this).

---

## 4. Why a local bridge is the primary design (and its limits)

PLAN decision 4 already says the instance-scoped local bridge is primary; `-http-proxy` is
only for the case where guest-path coverage is proven adequate. This section records the
technical basis.

**Bridge responsibilities (HTTP CONNECT + SOCKS5 listener per instance):**

1. Listen on `127.0.0.1:<bridge_port>` and be the only proxy the emulator is told about
   (`-http-proxy http://127.0.0.1:<port>`), so **no credential is ever in the emulator
   argv**. The bridge holds the vault secret and adds `Proxy-Authorization` when dialling the
   real upstream. **[recommended per R-PROXY]**
2. Support **SOCKS5 upstreams**, which the emulator flag cannot. The bridge accepts HTTP
   `CONNECT`/rewritten HTTP from the emulator and dials SOCKS5 (RFC 1928/1929 — username/
   password auth) upstream. **[needed for R-PROXY SOCKS5 requirement]**
3. Enforce **per-instance assignment**: a bridge instance only ever dials its one assigned
   proxy; token canary checks at five listeners prove assignment (G1).
4. Provide observability/redaction and hot-swap (`ProxyBridge.swap` in
   `core/interfaces.py`) that the raw flag cannot.
5. Optionally normalise TLS-encrypted SNI is **not** available to the bridge (it only sees
   CONNECT targets) — no HTTPS inspection, consistent with R-PROXY not needing it.

**What the bridge cannot fix:** UDP/QUIC and DNS from the guest. A local bridge is not a
transparent network tap; the guest must be *told* to use it (emulator proxy flag, or guest
Wi-Fi proxy settings — and the latter is exactly what apps may ignore). Therefore strict
"only egress via the assigned proxy" cannot be enforced from inside an unrooted
Google-Play/AOSP user build. Options to record in the product docs/limitations:

- per-instance `-dns-server` → controlled resolver (logs/blocks; still host egress, not the
  assigned upstream proxy);
- `-feature -WiFiPacketStream` (legacy stack) to make the QEMU-level TCP interception apply
  to all guest traffic; UDP still unproxied;
- optional host-level firewall/routing (out of scope for portable/no-admin edition, requires
  OS-specific privileges);
- G1 should therefore assert **TCP token isolation + credential redaction** and separately
  *measure and document* the DNS/UDP leak surface; do not claim full-layer isolation.

---

## 5. Ports and multi-instance layout

### 5.1 Console/adb pair [VERIFIED]

- Each emulator occupies a **pair**: even console port, odd `console+1` adb port
  (e.g. 5554/5555, 5556/5557, …). Serial = `emulator-<console>`.
- `-port <even>` sets the console port; `-ports <console>,<adb>` sets both (recommended only
  for special cases). Valid range documented as **5554–5682 = 64 concurrent devices**
  (commandline doc). The console doc separately says the console listens on **5554–5585**
  and accepts localhost only; adb scans **5555–5585 = first 16 emulators** by default
  (§6). Treat 5554–5682/64 as the emulator limit and 16 as the **adb auto-discovery**
  default limit — they are different limits. **[VERIFIED-but-doc-inconsistent: 5682 vs 5585
  in official pages]**
- Odd `-port` values in 5554–5584 can leave the emulator invisible to `adb devices` if the
  adb server starts after the emulator — always use even console ports. **[VERIFIED]**
- Ports already in use → emulator refuses to start. **[VERIFIED]**

`core/ports.py` already encodes the even/odd pair + side-channel allocation; R4 confirms the
model but flags the 16-vs-64 adb discovery gap (§6.2) and the range-doc discrepancy.

### 5.2 Side channels

| Port | Purpose | Default / notes |
|---|---|---|
| 5037 | adb server smart socket | shared by all instances; `-P`/`ADB_SERVER_SOCKET` alter it; Appium `appium:adbPort` defaults 5037 |
| console | telnet control console | even console port; auth token required (§7) |
| adb | transport | console+1 |
| gRPC | emulator controller/screen | `-grpc <addr>`/`-grpc <port>`; discovery default `grpc.port` **8554**, Studio example uses 8556; `-grpc-use-token`; `-idle-grpc-timeout` (§8) |
| MJPEG | UiAutomator2 screen stream | **not** an emulator port: device-side MJPEG server default **7810**, forwarded by Appium to `mjpegServerPort` (see R5) |
| bridge listen | local proxy bridge | per instance, e.g. 7800+ range in `core/ports.py` |
| UiAutomator2 systemPort | Appium device-server forward | default **8200**, range 8200–8299 (see R5) |
| Appium server | HTTP API | default **4723**, base path `/` (see R5) |

### 5.3 Cap practical sizing [PARTIAL]

- 64 instances require 64 even/odd pairs (5554…5680) — inside the documented 5554–5682 range.
- gRPC defaults `8554` is a *single* port; with many instances each needs a unique gRPC port
  (the emulator allows `-grpc <port>`; the discovery file records the actual port). AOSP
  python client defaults to 8554 only when no `grpc.port`/`grpc.address` is discovered.
  Plan the gRPC block explicitly (e.g. 8554+2·i or a dedicated range) rather than assuming
  default.
- `-netsim-args` only applies from the first launched instance (§1.2): one netsimd config
  per host, not per instance.

---

## 6. adb discovery, selection, transport IDs

### 6.1 Basics [VERIFIED]

- adb server listens on TCP **5037**; all clients share it.
- `adb devices -l` prints serial, state, and description
  (`product:… model:… device:… transport_id:N` — `transport_id` appears in modern adb output;
  the developer page documents serial/state/description, the repo fakes contract pins the
  `transport_id` field. **[PARTIAL: transport_id not spelled out on the developer page]**).
- States: `offline`, `device`, `no device`; **`device` does not imply fully booted** — "the
  device connects to adb while the system is still booting".
- Emulator serial format `emulator-5554` (console port).
- Selection: `-s SERIAL`, `-e` (the sole emulator; error if several TCP/IP devices), `-d`
  (USB device), and `-t ID` (transport id, per the AOSP adb man page). **`-s` overrides
  `$ANDROID_SERIAL`.**
  If multiple devices and no selector → error `adb: more than one device/emulator`.
- `adb emu <console cmd>` is the fire-and-forget console shortcut; with several emulators you
  must pass `adb -s emulator-<port> emu …`.
- `adb wait-for-device` waits for the `device` state only (not boot completion).

### 6.2 The 16-emulator discovery limit [VERIFIED — critical]

Developer adb doc: "It locates emulators by scanning odd-numbered ports in the range **5555 to
5585**, which is the range used by the **first 16** emulators." AOSP `adb.1` man page
confirms the knob: **`$ADB_LOCAL_TRANSPORT_MAX_PORT` — "Max emulator scan port (default 5585,
16 emulators)"**. Instances above the 16th are **not discovered** by a stock adb server.

Practical options for a 64-instance fleet (all need real-emulator confirmation):

1. set `ADB_LOCAL_TRANSPORT_MAX_PORT=5682` (or the highest odd port in use) **before the adb
   server starts** — must be in the environment of the server process, so `adb kill-server`
   + start with the env set; **[VERIFIED env var, effect at 64 UNVERIFIED]**
2. avoid relying on auto-discovery: drive addressing by deterministic serial
   (`emulator-<console>`) and use `-s`/`ANDROID_SERIAL` for every command; still requires the
   server to have discovered the transport via the scan;
3. run one adb server per instance with `-P <port>` (Appium `appium:adbPort`), at the cost
   of N long-lived processes and independent device scans.

Also: starting the adb server *before* the emulator avoids the `-port` corner case where
running emulators are missing from `adb devices` (§5.1). Multi-instance startup ordering
matters for a supervisor.

### 6.3 adb forward bookkeeping [VERIFIED]

- `adb forward [--no-rebind] tcp:<local> tcp:<remote>`; `--no-rebind` fails instead of
  replacing an existing forward. `--list`, `--remove <local>`, `--remove-all`.
- Forward state is owned by the **shared adb server**, not by the emulator; a crashed tool can
  leave stale forwards that collide later (R5's UiAutomator2 `systemPort`/`mjpegServerPort`
  recovery is exactly this). One `adb kill-server` clears all forwards for the whole fleet —
  avoid it as a routine cleanup; remove per-port instead.

---

## 7. Emulator console and auth token

[VERIFIED unless noted]

- Console = telnet-like line protocol on the even console port, **localhost only**, port range
  documented two ways: "5554 to 5585" (console doc) vs 5554–5682 (commandline doc).
- First command must be `auth <token>`; token is the contents of
  **`~/.emulator_console_auth_token`** (`$HOME`; the file is created with a random token the
  first time you connect if missing). Delete the token contents or create an empty file to
  **disable** console authentication entirely.
- Useful console commands: `avd name`, `avd snapshot save|load|list|delete <name>`, `kill`,
  `redir add tcp|udp:<hostPort>:<guestPort>`, `network delay|speed|capture`,
  `geo fix`, `gsm`, `sms`, `power`, `event send`, `proxy set|clear`, `grpc <port>`,
  `qemu monitor`, `debug <tags>`.
- `adb emu` shortcut does not require the user to type the token (adb talks to the emulator
  through the adb transport).
- Security notes for avdfleet:
  - the token file is **per-user, not per-instance** — every emulator run by that user reads
    the same token; it is not an isolation boundary between instances;
  - token must never be logged or returned by the API (it is a local capability, like the
    gRPC token);
  - `proxy set` exists but troubleshooting/console doc notes proxy set operations **fail in
    headless execution environments** ("Proxy set operations fail in headless execution
    environments") — configure proxy via launch flags/bridge, not via console, for headless
    fleets. **[VERIFIED doc warning]**
  - kill via `adb -s <serial> emu kill` is the preferred supervisor stop path (works with
    `-no-window`); console `kill` is the fallback.

---

## 8. gRPC control plane: flags, discovery files, tokens

### 8.1 Launch flags [VERIFIED]

- Docs (console page): "include the `-grpc` flag, specifying the target loopback port",
  e.g. `emulator -grpc localhost:8556 @Pixel_Fold`; requests are gRPC over plaintext HTTP/2
  (`grpcurl -plaintext … android.emulation.control.EmulatorController/...`).
- Android Studio's own launch line (commandline page) uses
  `… -grpc-use-token -idle-grpc-timeout 300`; these are Studio-internal window/control
  options but show the token flag and idle timeout exist.
- The gRPC service serves `EmulatorController` (getStatus, setVmState, sendKey, sendTouch,
  sendSms, …), `SnapshotService`, `Waterfall` (port forward/exec), logcat/audio streaming,
  etc. Screen/screenshot streaming is available through the controller service (V1's screen
  client should use the discovery file + token below).

### 8.2 Discovery files [VERIFIED from AOSP client, `emu-master-dev`]

The in-tree Python client (`android/android-grpc/python/aemu-grpc/src/aemu/discovery/`)
is the reference implementation:

- Discovery glues are **`pid_<pid>.ini`** files inside a directory `…/avd/running/`.
- Directory search order (Windows/macOS/Linux):
  - Windows: `%LOCALAPPDATA%\Temp\avd\running\` (the temp root also appears in release notes
    as `%TMP%\avd\running\pid_*.ini`; emulator temp artifacts are historically under
    `C:\Users\<u>\AppData\Local\Temp\AndroidEmulator\*`);
  - macOS: `~/Library/Caches/TemporaryItems/avd/running/`;
  - Linux: `$XDG_RUNTIME_DIR/avd/running/`, else `/run/user/<uid>/avd/running/`;
  - plus user dirs: `$ANDROID_EMULATOR_HOME/avd/running/`,
    `$ANDROID_SDK_HOME/.android/avd/running/`, `$ANDROID_AVD_HOME/avd/running/`,
    `~/.android/avd/running/`.
- Ini keys (parsed as `key=value` lines):
  - `grpc.port` — port of the gRPC service (client default **8554** if absent);
  - `grpc.address` — full `host:port` (client default `localhost:<grpc.port>`; a static
    deprecated default `localhost:8554` exists in the same code path);
  - `grpc.token` — legacy bearer token (client sends `authorization: Bearer <token>`);
  - `grpc.jwks` + `grpc.jwk_active` — JWT/JWKS paths (newer emulators; client needs
    `tink`), plus optional `emulator.security=jwt|token|nothing` in caller arguments;
  - `port.serial` — used to derive the adb name `emulator-<serial>`;
  - `pid` — process id; discovery liveness = pid alive and not dead/zombie.
- Access-control file: `$ANDROID_SDK_ROOT/emulator/lib/emulator_access.json` (gRPC access
  rules; referenced by the client docs). Non-loopback/JWT setups need entries there.
- Stale files are cleaned by the client (unlink + rmdir); the emulator also overwrites/cleans
  them (release notes 37.3.10: race during `%TMP%\avd\running\pid_*.ini` cleanup fixed).
- **`emu-grpc` directory** (as used by some third-party tooling/old docs): **does not appear
  in the AOSP client** used here; current AOSP discovery is `avd/running/pid_<pid>.ini`.
  Treat `emu-grpc/<console>.ini` as **[UNVERIFIED/legacy]**. Note the repo fakes contract
  (`contracts/fakes-spec.md` §4.4) writes `<fake_home>/grpc/<console>.ini`; that layout is
  **synthetic**, not the real emulator layout — V1 should discover via a configurable
  directory list (real layout here + fake override) instead of hard-coding one path.
- The emulator writes an advertisement file when gRPC/controller services start
  (`EmulatorAdvertisement.cpp` in the emulator log line "Advertising in:
  …/avd/running/pid_<pid>.ini" seen in public logs — **[PARTIAL, corroborating the AOSP
  client]**).

### 8.3 Auth model [VERIFIED/PARTIAL]

- Token file (`grpc.token`) is a **bearer token**: the AOSP client attaches
  `authorization: Bearer <token>` via a metadata interceptor. Treat it as a secret: never
  log/return it; pass it only to the screen/gRPC client.
- `-grpc-use-token` appears in the Studio launch line; the exact default (token on by
  default when `-grpc` is used?) is **[UNVERIFIED]** — always read `grpc.token` from the
  discovery file rather than assuming.
- JWT/JWKS support (`grpc.jwks`, `grpc.jwk_active`, `emulator_access.json`) exists for
  stronger setups; none of it is needed for a loopback-only fleet, and all of it is more
  surface to protect. Keep gRPC bound to loopback.
- Security note: gRPC (like the console) is a local capability channel; binding to
  `0.0.0.0` or exposing the port is a remote-control vulnerability. The default discovery
  contract is loopback (`localhost:<port>`); avdfleet must not widen it.

---

## 9. Snapshots and data reset flags

[VERIFIED from commandline + snapshots docs + release notes]

| Flag | Effect |
|---|---|
| `-no-snapshot` | Disables Quick Boot completely: no load, no save (overrides `-snapshot`) |
| `-no-snapshot-load` | Cold boot; still saves Quick Boot on exit |
| `-no-snapshot-save` | Quick-boot if possible; does **not** save on exit (changes lost) |
| `-no-snapshot-update-time` | Do not jump the guest clock on snapshot restore (time still synced ~15 s) |
| `-snapshot <name>` | Load snapshot `<name>` and save back under the same name on exit; if missing, cold boot + save. Loading a snapshot **overwrites system/data/sdcard contents** with the snapshot state |
| `-snapshot-list` | List snapshot IDs/tags for the AVD, then exit |
| `-wipe-data` | Delete user data, re-copy `userdata.img` → factory state; removes apps/settings; **does not** touch `sdcard.img`; does **not** wipe cache (cache is normally deleted on exit) |
| `-read-only` | Launch an additional read-only instance of the **same AVD**; guest disk changes discarded on exit (release notes 28.0.16). Useful only if wanting N instances of one AVD; avdfleet clones AVDs instead |
| console `avd snapshot save|load|delete <name>` | Same operations from the console; `adb emu avd snapshot …` shortcut |

Snapshot facts that constrain O1/N1:

- One **Quick Boot** snapshot per AVD + any number of **general** snapshots.
- Snapshots are valid only for the exact system image + AVD config + emulator features;
  updating any of them invalidates all snapshots (next start cold-boots). Also invalidated by
  emulator upgrades — release notes 37.3.10 has a concrete Quick Boot snapshot version break
  (`Unsupported snapshot version: 0x2`). **Pin the emulator version per fleet/golden image.**
- "You can't save snapshots while ADB is offline (such as while the AVD is still booting)."
  → golden-snapshot creation must wait for `sys.boot_completed` and ADB `device` state.
- "Snapshots are not reliable when software rendering is enabled" (docs recommend hardware
  GPU for snapshot workflows). This matters on headless/no-GPU hosts using
  `-gpu swiftshader_indirect`; O1 must treat snapshot-reset as best-effort and keep
  `-wipe-data` fallback (BRIEF §3.5 already lists fallbacks).
- Snapshots don't work with API ≤ 15 and ARM images on API 26.
- `-wipe-data` is the OOBE fallback that does not depend on snapshot validity; it is slower
  but always available.
- Loading a snapshot defeats the emulator's automatic snapshot-on-exit semantics; for a
  golden OOBE image prefer: cold boot → wait boot → (before completing setup) save named
  golden snapshot → on reset: kill + relaunch `-snapshot <golden>`; measure kill+load+first
  frame.

---

## 10. Boot-completed detection

[VERIFIED docs/source + standard practice]

- `adb wait-for-device` / `adb get-state` = transport visible; **not** boot complete. Docs:
  the `device` state "does not imply that the Android system is fully booted".
- The canonical readiness flag is the Android system property **`sys.boot_completed`**,
  exposed by `init` as an exported property (`system/core/init/stable_properties.h`) and set
  to `1` by the framework when boot completes (`ActivityManagerService` source:
  `SystemProperties.set("sys.boot_completed", "1")` before broadcasting
  `ACTION_BOOT_COMPLETED`).
- Recommended detection ladder per instance (used by CI tooling and suitable for N1):
  1. poll `adb -s emulator-<console> shell getprop sys.boot_completed` until `1` (timeout
     per template, e.g. 180–300 s cold boot; snapshot boot much faster);
  2. then verify PackageManager readiness, e.g. `adb shell pm path android` succeeds, or
     `adb shell service check package` reports `found`, or `getprop init.svc.bootanim` =
     `stopped` on images that use it;
  3. optional: verify current top activity with `dumpsys activity activities` for OOBE work.
- Use `adb -s`/`ANDROID_SERIAL` for every poll; use `getprop` (single exec) not interactive
  shell; add jitter/backoff and a bounded total timeout; treat `offline`/`unauthorized` as
  retryable, `error`/process-exit as failure.
- gRPC alternative: `EmulatorController/getStatus` returns boot/VM state (see §8), but ADB
  polling is the simplest portable path and also proves package-manager readiness.

---

## 11. Multi-instance behaviour checklist (what actually bites)

1. **Port pairs** must be unique even/odd; 5554–5682 = 64 pairs. [VERIFIED]
2. **adb discovery** of only the first 16 emulators unless
   `ADB_LOCAL_TRANSPORT_MAX_PORT` is raised for the server process. [VERIFIED]
3. **Same AVD launched twice** fails; only `-read-only` allows concurrent instances of one AVD
   (changes discarded). Fleet design clones per instance. [VERIFIED]
4. **MAC-address conflicts**: "If multiple emulators have the same MAC address, only the first
   one launched will have internet access. This can happen if emulators were assigned the
   same ADB port during creation or if they were launched from the same snapshot." → derive
   clones with distinct ports; expect snapshot-restored instances to share MAC; verify
   internet per instance; wipe/create new AVD if broken. [VERIFIED]
5. **netsim settings are host-global from the first instance** (`-netsim-args`). [VERIFIED]
6. **`-http-proxy` "should be set on the first launch"** for multiple emulators — treat as
   caution; verify per-instance proxy assignment (bridge avoids relying on it). [PARTIAL]
7. **Console auth token is per-user**, not per-instance. [VERIFIED docs]
8. **gRPC discovery is per-process `pid_<pid>.ini`**, and ports are per-instance; never assume
   8554 for all. [VERIFIED]
9. **Snapshots are per-AVD** and invalidated by config/image/emulator changes; clones copy the
   AVD dir and will copy snapshot state too — cloning a golden-snapshot AVD is the fast path
   but must be tested per image. [INFERRED/PARTIAL]
10. **Shared adb server**: one `kill-server`/`-P` mistake breaks every instance in the fleet;
    forwards are global. [VERIFIED]
11. **RAM/commit charge** on Windows: the emulator requests full guest RAM at start; 64
    instances is bounded by commit charge, not just RAM (R2 territory). [VERIFIED
    troubleshooting]
12. **Emulator 37.x next-gen engine preview** (upstream QEMU 11+, engine/UI process split)
    lands "for eligible virtual devices" from Studio canary; behavior of proxy/ports/gRPC
    discovery on the new engine is **not covered by this research** — pin to the 36.x/classic
    engine (or verify) before claiming any of the above. **[VERIFIED announcement;
    UNVERIFIED behaviour]**

### Test plan to convert this research into evidence (needs an accelerated host)

1. Boot two API ≤36 `google_apis` x86_64 AVDs with distinct console ports, `-no-window`,
   `-gpu swiftshader_indirect`, `-no-snapshot`, proxy = two local listeners.
2. For each instance, fetch (a) HTTP token URL, (b) HTTPS token URL, (c) a QUIC/HTTP3 URL,
   (d) trigger a DNS lookup via `adb shell ping`/an app, and (e) send a UDP canary datagram.
3. Record which listener/tap sees which token: expect TCP on assigned listener, UDP/DNS
   direct (leak). Repeat with `-feature -WiFiPacketStream`, `-feature -VirtioWifi`, and
   Ethernet-only (`adb shell svc wifi disable` or image without Wi-Fi) to find the
   combination where all guest TCP is proxied.
4. Confirm the emulator argv/`ps` shows no proxy credentials when using the bridge, and the
   bridge sees the CONNECT for both HTTP and SOCKS5 upstreams.
5. Verify `sys.boot_completed` polling timing, snapshot golden reset timing, gRPC discovery
   parsing (pid/port/token/Bearer), and 16→64-instance adb discovery with
   `ADB_LOCAL_TRANSPORT_MAX_PORT=5682`.
6. Record emulator version + feature flags + image revision in the evidence, because 36.5+
   and 37.x can invalidate all of it.

---

## 12. Implications for avdfleet

1. **PLAN decision 4 is technically justified**: keep the **instance-scoped local bridge as
   the primary and default** proxy path. Never pass a credential-bearing URL to
   `-http-proxy`; the emulator must only ever see `http://127.0.0.1:<bridge_port>`. N1/X2a
   should implement/keep it that way even if `-http-proxy` "works".
2. **Advertise `-http-proxy` as partial**: TCP-only, no SOCKS5, no DNS/UDP/QUIC coverage, no
   bypass list, no HTTPS inspection, credentials in argv if used directly. Docs/troubleshooting
   must say so; R-PROXY's "guest egress must use only its assigned proxy" cannot be claimed
   for UDP/DNS without host-level controls — G1 should assert TCP token isolation and record
   the residual leak surface.
3. **Version/feature policy**: pin the emulator version in `oobe_matrix`/fleet metadata; on
   36.5+ evaluate `-feature -WiFiPacketStream` (documented revert) before trusting
   `-http-proxy`; do not ship `-feature -VirtioWifi` as a proxy fix without measurement;
   record the effective flags at launch (`-feature`, `ANDROID_EMULATOR_FEATURES`) for
   forensics. `-netdelay`/`-netspeed` are Ethernet/cellular-only and irrelevant to isolation.
4. **Port plan** (`core/ports.py`): console pair 5554–5682; a dedicated gRPC block; bridge
   ports; Appium server/systemPort/MJPEG blocks (R5). Provision **`ADB_LOCAL_TRANSPORT_MAX_PORT`
   before starting the adb server** for >16 instances (or document the one-server-per-instance
   alternative), and always start adb before the emulators.
5. **Discovery**: `adb devices -l` + serial `emulator-<console>` (`-s`/`ANDROID_SERIAL`), not
   adb auto-listing beyond 16; always wait for `sys.boot_completed` (plus one
   package-manager check) instead of `wait-for-device`. Record `transport_id` robustly (it
   is positional in adb output).
6. **gRPC/screen (V1)**: discover via `<tmp>/avd/running/pid_<pid>.ini` on all three OSes
   (plus `~/.android/...` fallbacks), read `grpc.port`/`grpc.address`/`grpc.token`, use
   `authorization: Bearer`; keep gRPC loopback-only; treat the repo fakes' `<fake_home>/grpc/
   <console>.ini` as a fake-only layout (make the discovery dirs configurable) and update the
   fakes contract only via the root (contracts frozen this wave).
7. **Console**: use `adb -s <serial> emu kill` for supervised stop; use console only for
   snapshot/`avd name` fallbacks; never log the per-user `.emulator_console_auth_token`;
   remember `proxy set` fails headless.
8. **Snapshots/OOBE**: golden snapshot saved only after `sys.boot_completed` + ADB online;
   `-wipe-data` fallback always available; snapshot invalidation on emulator/image/config
   change must invalidate golden snapshots in the store; record emulator version with the
   golden snapshot and expect `swiftshader` to make snapshots unreliable.
9. **No real-emulator claims**: this host cannot boot; all of the above is documentation/
   source evidence. §11 test plan belongs in `avdfleet selftest --e2e` for accelerated hosts,
   and the final report must state this research was not empirically validated here.

---

## 13. Sources (all accessed 2026-11-26)

Official developer.android.com (markdown views used):
- Emulator proxy: https://developer.android.com/studio/run/emulator-networking-proxy (`.md.txt`)
- Network address space: https://developer.android.com/studio/run/emulator-networking-address (`.md.txt`)
- DNS: https://developer.android.com/studio/run/emulator-networking-dns (`.md.txt`)
- Networking overview: https://developer.android.com/studio/run/emulator-networking (`.md.txt`)
- Interconnect instances / 36.5 stack / `-feature -WiFiPacketStream`:
  https://developer.android.com/studio/run/emulator-networking-interconnect (`.md.txt`)
- Advanced (netsim) networking: https://developer.android.com/studio/run/emulator-networking-advanced (`.md.txt`)
- Command-line startup options: https://developer.android.com/studio/run/emulator-commandline (`.md.txt`)
- Console commands: https://developer.android.com/studio/run/emulator-console (`.md.txt`)
- Snapshots: https://developer.android.com/studio/run/emulator-snapshots (`.md.txt`)
- Troubleshooting (MAC conflict, netsim first-instance, `-feature -Vulkan`, DNS):
  https://developer.android.com/studio/run/emulator-troubleshooting (`.md.txt`)
- Emulator release notes (36.5.10 new stack, 37.3.10, 28.0.16 `-read-only`):
  https://developer.android.com/studio/releases/emulator (`.md.txt`)
- adb: https://developer.android.com/tools/adb (`.md.txt`)
- ADB man page (`-t`, `$ANDROID_SERIAL`, `ADB_LOCAL_TRANSPORT_MAX_PORT`):
  https://android.googlesource.com/platform/packages/modules/adb/+/refs/heads/master/docs/user/adb.1.md

AOSP / emulator source (aosp-mirror `platform_external_qemu`, branch `emu-36-1-release`
unless noted):
- Proxy engine API: `android/emu/proxy/include/android/proxy/proxy_common.h`
- Proxy setup/auth/precheck: `android/emu/proxy/src/android/http_proxy.c`,
  `.../proxy/proxy_setup.cpp`, `.../proxy/proxy_http.c`, `.../proxy/proxy_http_connector.c`,
  `.../proxy/proxy_common.c`
- Feature flags (`-feature`, `ANDROID_EMULATOR_FEATURES`, user `advancedFeatures.ini`):
  `android/emu/feature/src/android/featurecontrol/FeatureControlImpl.cpp`
- gRPC discovery + token semantics (`pid_<pid>.ini`, dirs, keys, Bearer):
  `android/android-grpc/python/aemu-grpc/src/aemu/discovery/emulator_discovery.py` and
  `emulator_description.py` (branch `emu-master-dev`); discovery test:
  `android/android-grpc/python/aemu-grpc/tests/test_discover_channel.py`
- Per-image feature ini (`VirtioWifi = on`):
  https://android.googlesource.com/platform/prebuilts/android-emulator-build/system-images/+/refs/heads/main/generic/system-images/android-35/google_apis/x86_64/advancedFeatures.ini
- Slirp address model (10.0.2.0/24, DNS .3): `net/slirp.c` on `emu-36-1-release`
- `sys.boot_completed` set on boot:
  `platform/frameworks/base` `ActivityManagerService.java` (boot-completed broadcast) and
  `system/core/init/stable_properties.h` (exported property):
  https://android.googlesource.com/platform/frameworks/base/+/d3a636a65f5061e8fd26aefe9b9cb09c170f764e/services/core/java/com/android/server/am/ActivityManagerService.java
  https://fuchsia.googlesource.com/third_party/android/platform/system/core/+/58d5906be3a07e684b54d1c49566a177ae5100b4/init/stable_properties.h

Repo contracts (read-only context): `contracts/fakes-spec.md` §4.3–4.4 (fake adb/emulator
ports, gRPC file; note synthetic discovery path), `src/avdfleet/core/ports.py`,
`src/avdfleet/core/interfaces.py` (RuntimeManager/EmulatorConsole/ScreenClient),
`PLAN.md` decision 4.
