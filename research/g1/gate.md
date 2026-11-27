# G1 — Proxy isolation gate: five authenticated listeners, one token each

> **STATUS: BLOCKED — NOT EXECUTED ON THIS HOST.**
>
> This document is the executable method for gate G1 (BRIEF.md §3.4 and §7 item 3) on
> an accelerated test host. There is **no real-emulator G1 evidence** in this
> checkout: the build host cannot boot an Android emulator (no usable hardware
> virtualisation) and this build was forbidden to install the Android SDK, download
> images or start an emulator. See section 7.
>
> G1a and G1b are independent runs of this method (BRIEF.md §5). Both runs must pass;
> one run alone is not the gate. Evidence lives under `research/g1/` (section 5).
>
> **What does run here:** `research/g1/listeners.py` — the loopback-only HTTP+SOCKS5
> listener harness and its verifier — was executed on this host on 2026-11-26
> (`python research/g1/listeners.py --no-banner`, exit code 0, 61/61 self-test checks
> passed, five listeners: 3 HTTP + 2 SOCKS5, optional HTTPS origin exercised). That is
> **harness verification only, not G1 evidence**: no Android guest existed.

## 1. Gate definition and acceptance linkage

BRIEF.md §3.4 and §7 item 3 require:

1. Each instance gets exactly one assigned proxy; the five test listeners are HTTP and
   SOCKS5 listeners, each **with its own credentials**.
2. The guest requests a URL containing **its own token**; each token must appear
   **only** on its assigned listener.
3. The emulator must not leak around the proxy, including DNS and UDP (use the local
   instance bridge when the emulator's direct `-http-proxy` coverage is insufficient).
4. Proxy credentials and API tokens are redacted from logs, errors, API responses,
   `ps` output where possible, and stored state.
5. G1 passes only when all evidence is recorded under `research/g1/`.
6. The same acceptance run also records each instance reaching the first Setup Wizard
   (activity + screenshot) and a reset-to-OOBE with measured golden-snapshot reset
   time — that part reuses the R3 method (`research/R3-oobe.md` §4.4/§4.6) and its
   evidence is referenced here.

## 2. Topology: the exact five-listener design

`research/g1/listeners.py` starts five authenticated listeners in one loopback-only
process, plus one neutral echo origin. Listener kinds are fixed as **3 HTTP + 2
SOCKS5**; both required protocols are covered and HTTP (the emulator's native
`-http-proxy` path and the bridge's most common upstream) gets the larger share.

| Listener | Kind | Assigned AVD | Proxy authentication | Token channel (guest-visible) |
|---|---|---|---|---|
| `L1` | HTTP forward proxy | `avd-01` | HTTP Basic (`Proxy-Authorization: Basic ...`) | GET `http://<token-L1>.g1.test/g1/origin?...` |
| `L2` | HTTP forward proxy | `avd-02` | HTTP Basic | token in absolute-URI host; CONNECT `/` authority |
| `L3` | HTTP forward proxy | `avd-03` | HTTP Basic | token in absolute-URI host; CONNECT authority; relayed HTTP plaintext |
| `L4` | SOCKS5 proxy | `avd-04` | RFC 1929 username/password | token in CONNECT destination hostname (`<token-L4>.g1.test:80`) and/or SOCKS username |
| `L5` | SOCKS5 proxy | `avd-05` | RFC 1929 username/password | token in CONNECT destination hostname and/or SOCKS username |

Harness-generated fixtures (per run, ephemeral, never reused):

- tokens: `g1t-` + 24 random hex characters; each listener owns exactly one;
- credentials: distinct username/password pair per listener (HTTP Basic / RFC 1929);
- token probe URL: `http://<token>.g1.test/g1/origin?probe=<listener>&token=<token>`
  (the reserved `.test` TLD does not resolve, so a guest that bypasses the proxy cannot
  reach the origin);
- `.g1.test` is mapped by the harness to the local echo origin; the origin records the
  `X-Avdfleet-G1-Listener` header that HTTP forward requests carry, and the harness's
  `verify_isolation(..., origin_logs=...)` flags token-bearing origin requests without
  the right attribution as a bypass.

Listener behaviour relevant to the gate:

- HTTP listeners accept absolute-URI forwarding for `http://` and CONNECT tunnelling for
  `https://`; the CONNECT authority and any plaintext inside the tunnel are scanned for
  tokens; chunked request bodies are rejected (501) and logged.
- SOCKS5 listeners implement RFC 1928 CONNECT and RFC 1929 username/password auth;
  BIND and **UDP ASSOCIATE are rejected with code 0x07 and logged** (`socks_rejected`,
  reason `udp_associate_attempt`). UDP ASSOCIATE from a guest is evidence of a leak
  attempt, not support for UDP relay (the product bridge is responsible for DNS/UDP
  handling; see `research/R4-networking.md`).
- Optional TLS: the echo origin also listens on an HTTPS port with a self-signed
  test certificate (embedded in the harness; override with
  `AVDFLEET_G1_TLS_CERT`/`AVDFLEET_G1_TLS_KEY`). It exists to exercise CONNECT
  tunnelling; a guest browser will not trust it, so guest proof uses the plain-HTTP
  origin and TLS results are plumbing evidence for the verifier only.
- Public output and `listener.redacted_events()` replace raw tokens with
  `<token:<listener>>` and passwords with `***`; the raw in-memory event API
  (`harness.listener_logs()`) is for assertions only and is exported as
  `*.raw.SENSITIVE.jsonl` (mode 0600) which must never be committed.

Guest egress path (per PLAN.md Decision 4): **per-instance local bridge is primary**.
The emulator's native `-http-proxy` may be used as an additional variant only for HTTP
listeners when R4 documents adequate DNS/UDP coverage; SOCKS5 listeners are always
reached through the bridge. The bridge must authenticate to the assigned listener
(HTTP Basic for `L1`–`L3`, RFC 1929 for `L4`–`L5`) and must not put credentials on a
command line; credentials live in the encrypted vault and are injected by the bridge
process.

### 2.1 Running the harness in the gate

```bash
# keep the five listeners live, print URLs with credentials (test-only):
python research/g1/listeners.py --serve

# or drive it from the acceptance test:
```

```python
import json
import sys

sys.path.insert(0, "research/g1")
from listeners import ListenerHarness  # noqa: E402

harness = ListenerHarness.standard().start()   # 5 listeners + echo origin
try:
    # per AVD: configure the proxy/bridge upstream with harness.proxy_url(name),
    # then make the guest GET harness.token_url(name).
    # harness.tokens[name], harness.credentials[name] are the raw fixtures;
    # harness.proxy_url(name) and harness.token_url(name) are the values to use.
    report = harness.verify()                  # tokens + logs + credentials + origin
    harness.export_evidence("research/g1/run-<utc>/listeners")
    print(json.dumps(report, indent=2))
    assert report["passed"], report["failures"]
finally:
    harness.stop()
```

`export_evidence()` writes `listeners.json`, `listeners.redacted.jsonl`,
`origin.redacted.jsonl`, `verify-isolation.json`, `checksums.json` and (when
`include_raw=True`) the two `*.raw.SENSITIVE.jsonl` files. The gate evidence keeps the
redacted files; the raw files prove assertions but are access-controlled and excluded
from the repository.

The harness must bind loopback only (`127.0.0.1`; `localhost` is normalised; any other
address is rejected). It prints ephemeral credentials because the requirement says to
show URLs **with credentials** — that banner is itself test-only output and must not be
captured into product logs or shared evidence.

## 3. Guest request protocol

### 3.1 Prerequisites on the accelerated host

- Five AVDs booted from one template and one image that `oobe_matrix.json` marks
  `verified` (R3 evidence); API ≤ 36 for 16 GiB hosts, API 37+ needs ≥4 GiB per AVD
  (BRIEF.md §7).
- The product daemon/CLI/GUI running, with the proxy vault unlocked.
- The harness running on the same host, loopback only.
- Host egress firewall (recommended, see §4.5) allowing only loopback listener/bridge
  ports during the isolation phase.

### 3.2 Configure the proxies and assignments

Import five authenticated proxies and assign one per AVD through the product (GUI is
equivalent and is the acceptance path; the REST form is shown for scripting). For the
script fragments below, assume the daemon is on its default loopback URL and the bearer
token is read from `Paths.token_path` (`<data_dir>/token`, mode 0600):

```bash
export AVDFLEET_URL="http://127.0.0.1:8765"
export AVDFLEET_TOKEN="$(cat "${AVDFLEET_DATA_DIR:-$HOME/.local/share/avdfleet}/token")"
# proxy import text (one per line); POST /api/v1/proxies/import
# http://g1u-xxxx:g1p-yyyy@127.0.0.1:<L1 port>
# http://g1u-xxxx:g1p-yyyy@127.0.0.1:<L2 port>
# http://g1u-xxxx:g1p-yyyy@127.0.0.1:<L3 port>
# socks5://g1u-xxxx:g1p-yyyy@127.0.0.1:<L4 port>
# socks5://g1u-xxxx:g1p-yyyy@127.0.0.1:<L5 port>
curl -sS -X POST "$AVDFLEET_URL/api/v1/proxies/import" \
  -H "Authorization: Bearer $AVDFLEET_TOKEN" -H "Content-Type: application/json" \
  --data '{"text": "<one proxy URL per line>", "source": "g1-gate"}'
# assign: POST /api/v1/assignments {"avd_name": "avd-01", "proxy_id": "<id>"} ...
```

Verify through the API that each AVD has exactly one assignment and that responses
never echo the password (`ProxyView.has_credentials=true`, `url` ends in `:***@`).
Start the five AVDs with their assignments (GUI: Fleet → deploy/boot; CLI equivalent
once available).

### 3.3 Make the guest issue the token request

The canonical probe is a plain-HTTP GET of
`http://<token>.<base-domain>/g1/origin?...` from inside each guest, with the guest's
egress going through its assigned listener. Use the first channel that is available on
the image; record which one was used for every observation.

**Channel A — browser intent (Play Store images with Chrome).**

```bash
SER="emulator-5554"
URL="<harness.token_url for this AVD>"
adb -s "$SER" shell cmd package resolve-activity --brief -a android.intent.action.VIEW -d "$URL"
adb -s "$SER" shell am start -a android.intent.action.VIEW -d "$URL" com.android.chrome
# wait 5-10 s, then capture evidence (screenshot, logcat, listener logs)
```

The browser renders the JSON echo response; the request itself is the evidence. Take a
screenshot (`adb -s ... exec-out screencap -p > g1-request.png`) and `adb logcat -d`
afterwards.

**Channel B — in-guest HTTP client (if the image ships one).**

```bash
adb -s "$SER" shell 'command -v curl || command -v wget || (toybox --help 2>/dev/null | grep -w nc)'
# with the emulator's -http-proxy path, the client needs no explicit proxy flag;
# with the bridge path, point the client at the bridge exactly as configured.
adb -s "$SER" shell "curl -sS -m 20 '$URL'"      # when curl exists
```

Preflight the in-guest client first; many recent Google APIs images do not ship `curl`,
so this channel is opportunistic, never the only plan.

**Channel C — G1 probe APK (deterministic fallback; required for images without a
browser/client).**

Build once on the test host with the SDK's `build-tools` (the accelerated host already
has them for R3/E0):

- package `com.avdfleet.g1probe`, exported activity `ProbeActivity`;
- intent extras: `url` (mandatory), `timeout_ms` (optional);
- performs `HttpURLConnection` GET through the Android default proxy
  (`ProxySelector`/system proxy set by the emulator or bridge configuration), then logs
  `G1_PROBE http=<code> bytes=<n>` (no token/credential in the log line beyond what the
  URL itself contains, and the URL is not logged in full);
- exits/updates its UI; the listener sighting is the primary evidence.

```bash
adb -s "$SER" install -r g1probe.apk
adb -s "$SER" shell am start -n com.avdfleet.g1probe/.ProbeActivity --es url "$URL"
adb -s "$SER" logcat -d -s G1Probe:* > "$AVD_EV/g1probe-logcat.txt"
```

Record the APK sha256 with the evidence. The APK is a test artefact; its source belongs
under the test tree when built, not in `research/`.

Run the token request **3 times per AVD**, 2 s apart, and keep every request's
listener sighting plus the guest-side success evidence. Every token must be observed
on its own listener at least once and must never appear on another listener (see §5).

### 3.4 Negative controls (required — a gate that cannot fail is not a gate)

Run these once for the run, on a spare AVD or by temporarily re-pointing an AVD; record
that the expected failure was observed and then restore the correct configuration:

1. **Wrong credentials.** Send the token probe through each listener using a wrong
   password: HTTP listeners must answer `407 Proxy Authentication Required`; SOCKS5
   listeners must reject auth (RFC 1929 status 0x01). No token sighting may be logged
   for the rejected attempt. (The harness self-test already covers this per listener.)
2. **Cross-assigned control.** Boot `avd-01` against `L2`'s URL/credentials (test-only,
   never in product state) and issue `avd-01`'s token probe. Token L1 must then appear
   on `L2`; `verify_isolation` must return **FAIL** with `token_on_wrong_listener`.
   Restore AVD-01 to L1 and re-run to PASS. This proves the verifier detects
   cross-contamination rather than "everything seen somewhere".
3. **Bypass control.** Temporarily remove the AVD's proxy/bridge so it egresses
   directly; issue the token probe. The `.g1.test` host does not resolve, so the request
   must fail; zero listener sightings; the origin must not receive the token. The gate
   run must FAIL (`missing_own_sighting`/`no_token_sightings`). Restore and re-run.
4. **UDP/DNS/leak control.** From the guest, force a UDP/QUIC or DNS attempt that would
   bypass the proxy (e.g. an app or `settings`/network util that generates DNS
   traffic), and check: SOCKS5 listeners log `udp_associate_attempt` and reject it; the
   host network evidence in §4.5 shows no direct external 53/443/UDP traffic from the
   emulator process beyond the assigned bridge/listener path. Any direct DNS/UDP
   egress is a **G1 failure**, not a warning.

Also verify `harness.verify()` remains PASS for the correct run after the negative
controls are removed, and export the final report.

## 4. Credential- and token-leak checks

Secrets to scan for, per run:

- proxy passwords (HTTP Basic password and SOCKS5 password) for all five listeners;
- the product API bearer token(s) from the token file and any WS token;
- the five G1 canary tokens (test tokens): they are expected **only** on their own
  listener logs; they must not appear in product logs, API responses, product state,
  or product/bridge process command lines. A guest browser may keep its own history;
  product-owned sinks are what G1 judges.

### 4.1 Logs

Product paths (`src/avdfleet/core/paths.py`):

- Windows: `%LOCALAPPDATA%\avdfleet\Logs\`, data `%LOCALAPPDATA%\avdfleet\`
- macOS: `~/Library/Logs/avdfleet/`, data `~/Library/Application Support/avdfleet/`
- Linux: `$XDG_STATE_HOME/avdfleet/logs/` or `~/.local/state/avdfleet/logs/`; data
  `$XDG_DATA_HOME/avdfleet/` or `~/.local/share/avdfleet/`
- overrides: `AVDFLEET_DATA_DIR`, `AVDFLEET_LOG_DIR`, `AVDFLEET_CONFIG_DIR`,
  `AVDFLEET_CACHE_DIR`

Capture and scan, at minimum: the daemon/CLI log files, the API access log, the
emulator stdout/stderr logs, the bridge logs, `adb logcat -d -b all`, captured
`/ws/events` frames, script-run output records, installer/manifest logs, and the
harness's own public (redacted) log export. Never scan only one file.

```bash
# POSIX (repeat per secret; $SECRETS is a file of exact secrets, one per line)
while IFS= read -r s; do
  grep -R --binary-files=text -n -F -- "$s" \
    "$AVDFLEET_DATA_DIR" "$AVDFLEET_LOG_DIR" "$RUN_EV/emulator.stdout.log" \
    "$RUN_EV/emulator.stderr.log" "$RUN_EV/logcat.txt" "$RUN_EV/bridge.log" \
    && echo "LEAK: $s"
done < "$SECRETS"
```

```powershell
# Windows
Get-ChildItem -Recurse -File "$env:LOCALAPPDATA\avdfleet" |
  Select-String -SimpleMatch -Pattern (Get-Content "$env:RUN_EV\secrets.txt")
```

Expected: no raw secrets. Redacted forms are allowed and expected (`user:***@`,
`"password": "***"`, `Authorization: Bearer ***`, `<token:L1>`). Capture the scan
inputs as fingerprints/sha256, not plaintext, in evidence (`leak/scan-*.json`), and
delete plaintext secret lists after the run.

### 4.2 Process listings (`ps`)

```bash
# POSIX host
ps -eo pid,ppid,args > "$RUN_EV/leak/ps-host.txt"
# guest (Android toybox; -o is supported on modern images, fall back to plain -A)
adb -s "$SER" shell ps -A -o PID,NAME,ARGS > "$RUN_EV/leak/ps-guest-$SER.txt"
```

```powershell
# Windows host
Get-CimInstance Win32_Process |
  Select-Object ProcessId,ParentProcessId,Name,CommandLine |
  Format-List | Out-File "$env:RUN_EV\leak\ps-host.txt"
```

Check emulator, bridge, daemon, CLI and any helper processes for raw passwords,
`Basic <base64>` values, bearer tokens and canary tokens. The emulator's
`-http-proxy` value must not contain proxy credentials: point it at an unauthenticated
loopback bridge, and keep authenticated upstream credentials inside the bridge/ vault.
Customer-facing `-http-proxy user:pass@` usage would be visible in host `ps` — record
whether direct mode was used and why, and treat a credential there as a G1 failure.

### 4.3 API responses, CLI output and WebSocket frames

With the daemon running and the gate proxies configured:

```bash
API="$AVDFLEET_URL/api/v1"; H="Authorization: Bearer $AVDFLEET_TOKEN"
for path in /proxies /bridges /instances /instances/avd-01 /assignments /events?limit=200; do
  curl -sS -H "$H" "$API$path" > "$RUN_EV/api/$(echo "$path" | tr '/?=&' '____').json"
done
# errors must not echo secrets either: force a bad proxy and fetch the error envelope
```

Assertions:

- no raw password/token substrings in any response body, header or error string;
- `ProxyView` masks the password (`url` contains `:***@`), `has_credentials=true`,
  `password` field absent/never returned (OpenAPI: "Passwords are never returned");
- `BridgeConfig.upstream` is redacted (scheme/host/port only; no userinfo);
- `/ws/events` frames contain no secrets (capture with a WS client for ≥60 s around the
  gate requests); the WS token itself must not be logged (PLAN.md Decision 7 puts it in
  the query string — verify the access log redacts it);
- CLI `--json` output for `proxies`, `instances`, `bridges` is secret-free, and CLI
  human output prints masked URLs only (exit codes: 0 success, non-zero on error).

### 4.4 Stored state and secrets at rest

- SQLite database `Paths.db_path` (`<data_dir>/avdfleet.db`) plus WAL/SHM: scan all
  files for secrets; proxy password must not be present in plaintext anywhere.
- Vault: `Paths.vault_path` (`secrets.vault`) must be ciphertext; `secrets.key` /
  OS keyring holds the key material; assert a plaintext secret is not in the vault
  bytes and the vault cannot be read without the passphrase/keyring.
- `config.toml`, `install-manifest.json`, state JSON, `oobe_matrix.json`, snapshot
  metadata, screenshots dir: no secrets.
- Token file `Paths.token_path` is mode 0600 (POSIX) / restricted ACL (Windows); do not
  print its contents; scan its bytes only to confirm it is not duplicated elsewhere.

### 4.5 Network evidence (direct-egress / DNS / UDP)

During the isolation window, capture per-emulator-process sockets and host firewall
telemetry:

```powershell
# Windows
# Windows: the emulator launcher spawns qemu-system-<arch>.exe
$emu = Get-CimInstance Win32_Process |
  Where-Object { $_.Name -like 'qemu-system-*' -or $_.Name -eq 'emulator.exe' }
Get-NetTCPConnection -OwningProcess $emu.ProcessId |
  Select-Object LocalAddress,LocalPort,RemoteAddress,RemotePort,State
Get-NetUDPEndpoint -OwningProcess $emu.ProcessId |
  Select-Object LocalAddress,LocalPort
```

```bash
# Linux / macOS
ss -tnp | grep -E 'emulator|qemu'   # or: lsof -nP -i -a -p <emulator-pid>
ss -unp | grep -E 'emulator|qemu'
```

Allowed: loopback connections to the assigned bridge/listener ports; guest-initiated
traffic to the emulator's own host-side endpoints. Any connection/UDP packet to an
external address (80/443/53/QUIC) from the emulator process, or DNS traffic not
through the bridge, is a leak and fails the gate. Prefer a deny-all-except-loopback
host firewall rule for the window so leaks fail closed; capture the rule and its log.

## 5. Evidence directory layout (`research/g1/`)

Written only on the accelerated host; **nothing below was produced on this build host**.

```
research/g1/
  gate.md                                   # this method
  listeners.py                              # harness (runnable here; gate plumbing)
  run-<YYYYMMDDTHHMMSSZ>-<host>-g1a/        # G1a; a second independent g1b dir is required
    README.md                               # operator, UTC window, verdict, image + versions
    meta.json                               # host/preflight, emulator/ADB versions, image package + sha256
    listeners/
      listeners.json                        # harness describe(redact=True): ports/usernames, no passwords/tokens
      listeners.redacted.jsonl              # public per-listener events
      origin.redacted.jsonl                 # public origin events
      verify-isolation.json                 # verify_isolation report (token names only, no raw values)
      checksums.json                        # sha256 for every listener evidence file
    guest/avd-01..05/
      request-urls.txt                      # token redacted (placeholder only)
      probe-channel.txt                     # browser intent | in-guest client | probe APK + why
      g1probe.apk.sha256                    # when channel C was used
      g1-request-*.png                      # guest screenshot after each request
      logcat.txt                            # guest logcat during the probes
      first-wizard-*.png, activity.txt      # item 3 OOBE evidence (or link to R3 run dir)
    leak/
      secrets.sha256                        # hashes of the scan list, never plaintext
      scan-logs.json, scan-ps-host.txt, scan-ps-guest.txt
      api-responses.json, ws-frames.jsonl
      db-scan.json, vault-scan.json, state-scan.json
      net-tcp.json, net-udp.json, firewall-rule.txt
    negatives/
      wrong-creds.json, cross-assigned.json, bypass.json, udp-dns.json
    reset/
      reset-timings.json                    # per-image golden reset timings (copy/link from R3)
    timeline.jsonl                          # gate actions + timestamps
    checksums.sha256                        # sha256 of every evidence file in the run dir
  run-<...>-g1b/                            # independent repeat, same layout
```

Rules:

- Raw tokens/passwords never appear in `research/g1/` (git). The harness's
  `*.raw.SENSITIVE.jsonl` is kept outside the repository (or encrypted), referenced by
  path + sha256 in `meta.json`, and deleted/rotated after review.
- Each claim in `README.md`/`verify-isolation.json` points to a file path + sha256.
- `listeners.raw.SENSITIVE.jsonl` may contain canary tokens but must never contain
  real product credentials; do not put real proxy passwords into the harness at all.

## 6. Pass/fail criteria (G1 verdict)

The gate passes only when **all** of the following hold, in **both** independent runs
(G1a and G1b), with evidence present and checksummed:

| ID | Check | Evidence |
|---|---|---|
| P1 | Five listeners (≥1 HTTP, ≥1 SOCKS5), distinct token and distinct credentials each; five AVDs each assigned exactly one proxy and no assignment swapped | `listeners/listeners.json`, product API assignments, `meta.json` |
| P2 | Every AVD's token observed **only** on its assigned listener over 3 requests; each token seen at least once on its own listener; `verify_isolation().passed=true` both for raw and redacted listener logs | `verify-isolation.json`, `listeners*.jsonl` |
| P3 | Wrong-credential attempts are rejected (HTTP 407 / SOCKS5 auth failure) with zero token sightings | `listeners*.jsonl`, `negatives/wrong-creds.json` |
| P4 | Cross-assigned negative control is detected (`token_on_wrong_listener`) and the restored correct run passes | `negatives/cross-assigned.json` |
| P5 | Direct-bypass negative control produces no listener sighting/no origin token and is detected (`missing_own_sighting`/`no_token_sightings`) | `negatives/bypass.json` |
| P6 | No raw proxy password or product bearer token in any product-owned log, error or WS frame; canary tokens absent from product logs/state (expected only in harness logs) | `leak/scan-logs.json` |
| P7 | No raw secret in host or guest process listings; emulator/bridge command lines carry no credentials | `leak/scan-ps-host.txt`, `leak/scan-ps-guest.txt` |
| P8 | API and CLI responses mask credentials (`:***@`, no password field) and contain no token/secret | `leak/api-responses.json` |
| P9 | Database/vault/state contain no plaintext secrets; vault is encrypted; token file permissions are restricted | `leak/db-scan.json`, `leak/vault-scan.json` |
| P10 | DNS/UDP/direct-egress checks clean (no external 53/443/UDP from the emulator outside the assigned path; UDP ASSOCIATE attempts logged/rejected if any) | `leak/net-tcp.json`, `leak/net-udp.json`, harness `socks_rejected` events |
| P11 | Each of the five instances reached the first Setup Wizard (activity + screenshot) and reset-to-OOBE works with measured golden-snapshot reset time | guest screenshots/activity files; R3/`reset/reset-timings.json` |

Additional verdict rules:

- Any raw secret found in evidence that is committed to the repository is an
  automatic **FAIL** and a redaction incident.
- A run in which the negative controls were not executed is **not passed** (unproven
  detection).
- Divergence between G1a and G1b (different verdict, different token mapping, missing
  evidence) sends the result to the root for review; do not merge to PASS.
- The final report must mark G1 as **real emulator** with the host name and run
  directory, or leave it blocked/fakes-backed. Fakes-backed proxy tests are useful
  plumbing checks but are not G1.

## 7. Why this is blocked on this build host (evidence)

Inspected 2026-11-26 (read-only; nothing installed or downloaded):

- **No usable hypervisor.** Windows 11 Pro build `10.0.26300` on a VMware `VMware20,1`
  VM, CPU model `Apple silicon`, 4 vCPUs; `VirtualizationFirmwareEnabled=False`,
  `SecondLevelAddressTranslationExtensions=False`, `VMMonitorModeExtensions=False`;
  Windows optional features `HypervisorPlatform` (WHPX), `Microsoft-Hyper-V-All` and
  `VirtualMachinePlatform` all `InstallState=2` (disabled). `HypervisorPresent=True`
  reflects only the outer VMware hypervisor; no nested acceleration is exposed. There
  is no KVM (Windows host) and no HVF (not macOS). The Android emulator therefore
  cannot start, so no guest exists to issue the five token probes.
- **Memory and disk are insufficient for the fleet.** 16 GiB physical RAM
  (17,174,372,352 bytes visible, ~6.1 GB free at inspection) and ~13.5 GiB free on
  `C:` — the gate needs 5 AVDs plus the SDK/image set, and BRIEF.md §6/§7 rules out
  software emulation as a fallback.
- **No Android tooling and no permission to install it here.** `ANDROID_HOME`,
  `ANDROID_SDK_ROOT`, `JAVA_HOME` are unset; no `adb`, `emulator`, `sdkmanager`,
  `avdmanager`, `java`, `node`, `appium` or `uv` is on PATH; no `~/.android`, no
  `C:\Android`, no `%LOCALAPPDATA%\Android\Sdk`. This build's instructions explicitly
  forbid installing the SDK, downloading images or starting an emulator.
- **Product is fakes-backed on this host** (PLAN.md "Host constraint discovered in
  Wave 0"). Tests marked `emulator` are skipped; product paths for proxies/bridge/
  vault/redaction are exercised against fakes only.

What did run: the listener harness self-test (five loopback listeners, HTTP CONNECT,
SOCKS5 auth, UDP-associate rejection, TLS origin, token-sighting, `verify_isolation`
positive/negative controls) passes with exit code 0. That verifies the tool, not the
gate. The gate still requires a real emulator host (WHPX/KVM/HVF) and the method in
this document.

## 8. Risks and limitations

- **Direct `-http-proxy` coverage is not enough by itself.** The emulator's proxy path
  is known not to cover all DNS/UDP/QUIC traffic; PLAN.md Decision 4 makes the
  instance bridge primary. The acceptance must record which path was used per AVD and
  pass only when the bridge path covers DNS/UDP; a bare direct-proxy pass is a FAIL
  until proven complete by R4.
- **Guest browser/client availability.** Chrome exists on Play Store images; Google
  APIs/AOSP images may have no browser and often no `curl`. Channel C (probe APK) is
  therefore mandatory as a fallback; build and hash it before the run.
- **Bridge SOCKS5 support.** The bridge must implement RFC 1929 username/password
  upstream authentication and must not expose credentials on a command line; if it
  cannot authenticate, SOCKS5 listeners reject it and the gate fails.
- **Guest-side proxy configuration.** Verify how the guest is pointed at the bridge
  (emulator proxy vs guest settings vs bridge listening on the guest-reachable host
  alias) and record it; a misconfigured guest that falls back to direct egress must
  show up as a bypass, not as a pass.
- **Canary tokens vs production secrets.** G1 canary tokens are test fixtures and are
  expected in harness logs; the no-leak checks target *product-owned* sinks and all
  real proxy/API credentials. Do not store real proxy passwords in the gate harness.
- **Self-signed TLS origin** is only for CONNECT plumbing; a guest browser will show
  a certificate error. Guest proof uses plain HTTP; do not count a TLS handshake
  failure as a request failure if the listener already observed the token in CONNECT.
- **WS token in query string** (PLAN.md Decision 7) risks appearing in access logs;
  scan for it explicitly and treat it as a product credential.
- **Origin attribution is only available for plain forward-proxy requests.** The
  harness adds `X-Avdfleet-G1-Listener` to forwarded HTTP requests so origin logs can
  prove attribution; CONNECT/TLS payloads cannot carry that header, so a token seen
  only inside a tunnel is attributed by the listener's CONNECT-authority/relay
  sighting, not by the echo origin. Use the listener log as primary evidence and cite
  the origin log as supporting evidence only where the header is present.
- **Five AVDs resource pressure**: run the fleet boot/request phase serially if the
  host is memory-bound, but record per-AVD evidence; do not reduce to fewer than five
  listeners/instances for the gate.

## 9. Sources

- BRIEF.md §3.4, §5, §6, §7 item 3; PLAN.md Decision 4 and Wave 0 host constraint.
- `contracts/openapi.yaml`: `/api/v1/proxies`, `/bridges`, `/assignments`,
  `verify-guest`, `ProxyView` (passwords never returned).
- `contracts/fakes-spec.md`: process-boundary fakes used for fakes-backed proxy tests.
- `src/avdfleet/core/paths.py`: data/log/token/vault paths.
- `src/avdfleet/core/logging.py`: redaction choke point, `file_contains_secret` helper.
- `src/avdfleet/core/models.py`: `ProxyConfig.safe_url` (`user:***@`), `BridgeConfig`.
- Emulator proxy/networking: <https://developer.android.com/studio/run/emulator-networking-proxy>
- Emulator networking: <https://developer.android.com/studio/run/emulator-networking>
- adb: <https://developer.android.com/tools/adb>
- SOCKS5: RFC 1928 (<https://www.rfc-editor.org/rfc/rfc1928>), RFC 1929
  (<https://www.rfc-editor.org/rfc/rfc1929>).
- Harness: `research/g1/listeners.py` (this repository, stdlib only).
