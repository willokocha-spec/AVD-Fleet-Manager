# Proxy isolation

AVD Fleet gives **every instance exactly one authenticated upstream proxy** and
routes that instance's guest TCP egress through a per-instance loopback bridge.
The design, the security invariants and the honest limitations (DNS and UDP are
**not** proxied) are documented here.

## Design

```
guest app ──TCP──▶ emulator proxy setting ──▶ per-instance loopback bridge  127.0.0.1:<port>
                                                   │  injects upstream credentials
                                                   ▼
                                         assigned authenticated proxy (HTTP or SOCKS5)
```

- The proxy pool stores URLs/credentials. Secrets go to the vault (OS keyring or
  AES-256-GCM file vault); the SQLite database stores only a `secret_ref`.
- Each instance is assigned **one** proxy. The runtime starts a bridge for it on
  a free loopback port from `bridge_port_start` (default 7800).
- The emulator is told to use `http://127.0.0.1:<bridge_port>` — **never** a
  credential-bearing URL. The bridge dials the real upstream and adds
  `Proxy-Authorization` (HTTP) or RFC 1929 username/password auth (SOCKS5).
- Bridge configuration exposes `instance`, `listen_host`, `listen_port`,
  `upstream` (redacted), `mode` (`http`/`socks5`/`direct`) and `active`.
- Direct `-http-proxy` is only a fallback mode where the guest path is proven
  adequate; it puts the upstream URL in the emulator command line and cannot
  authenticate SOCKS5, so strict mode does not use it.

Research R4 drives this decision: `-http-proxy` is TCP-only (no SOCKS5), has no
bypass list, no per-request authentication, no logging and no way to carry the
credentials safely in `ps` output. The local bridge is the primary design.

## Adding proxies

Proxy URLs are `scheme://user:pass@host:port` where scheme is `http`, `https`
or `socks5`; pasting a list one-per-line is the batch form.

```bash
avdfleet proxies add "http://user:secret@proxy.example.com:8080" --name corp-1 --tag corp
avdfleet proxies import proxies.txt --source corp
avdfleet proxies list              # passwords masked, has_credentials shown
avdfleet proxies list --all        # include disabled
avdfleet proxies check <proxy-id>  # health check from the host
avdfleet proxies check-all
avdfleet proxies delete <proxy-id>
```

The CLI and API only ever see the redacted view (`username`, `has_credentials`,
`health`); the password is written straight into the vault and is never printed.

In the GUI: **Proxies → Add/Import**, then assign per instance on the Fleet page
or with **Auto-assign**.

## Assignments

```bash
avdfleet proxies assign avd-01 <proxy-id>       # exactly one proxy per instance
avdfleet proxies unassign avd-01
avdfleet proxies auto-assign --strategy round_robin
avdfleet proxies auto-assign --instances avd-01,avd-02 --strategy healthiest
avdfleet proxies bridges                        # active local bridges
```

Strategies: `round_robin` (default), `random`, `healthiest` (prefers proxies
whose latest health check passed/lowest latency). An instance with no assignment
gets no bridge and therefore no proxied egress; it does not silently fall back
to the host network when strict mode is enabled by the caller.

Starting an instance with `--proxy <id>` creates/uses that assignment for the
run. The instance's proxy can be changed by starting/restarting it with a
different `--proxy` (or by re-assigning and restarting).

## Verifying egress

Two levels of evidence:

```bash
# Host-side reachability/health (does the proxy answer?):
avdfleet proxies verify <proxy-id>

# Guest-side isolation evidence (does the guest's traffic arrive on the assigned proxy?):
avdfleet proxies verify <proxy-id> --guest avd-01 --token-url https://example.com/token
```

The guest verifier issues a request from inside the guest to a URL that contains
a per-instance token and proves the request arrived at the assigned proxy. The
API equivalent is `POST /api/v1/proxies/verify-guest` with
`{"avd_name", "proxy_id", "token_url"}`; the response is a `ProxyTestResult`
with `ok`, `latency_ms`, `egress_ip`, `status_code`, `error`.

## DNS and UDP: explicit limitations

`-http-proxy` (and therefore any HTTP proxy understood by the emulator) is
**TCP-only**. From research R4 and Google's emulator documentation:

- **DNS is not proxied.** The guest sends DNS to the emulator's virtual router
  (`10.0.2.3`), and the emulator resolves it on the host through the host
  resolver. The proxy sees the destination hostname only in the proxied TCP
  `CONNECT`/`Host` header. `-dns-server` changes *which* resolver the emulator
  forwards to, not whether DNS goes through the proxy. Per-instance DNS
  isolation therefore needs a per-instance controlled resolver
  (`-dns-server <host>`), which is host resolver egress, not assigned-proxy
  egress.
- **UDP/QUIC is not proxied.** The Emulator Proxy "doesn't support UDP
  redirection"; the AOSP proxy implementation only accepts stream sockets.
  QUIC/HTTP3 (UDP/443), DNS (UDP/53), DTLS, mDNS and app-specific UDP leave
  through the emulator NAT unproxied. ICMP/IGMP are unsupported by the emulator
  entirely.
- **Traffic that ignores the Android system proxy** is still intercepted at the
  emulator's QEMU/slirp socket layer for TCP — but this applies to the classic
  network path. With the 36.5+ shared-Wi-Fi/netsim stack the guest Wi-Fi path is
  handled by `netsimd`; whether the Emulator Proxy still covers it is not stated
  by Google. The documented way back to the legacy stack is
  `-feature -WiFiPacketStream`, which must be measured per emulator version
  before it is trusted.
- A local bridge cannot fix this: it is not a transparent network tap, and in an
  unrooted Play/AOSP user build the guest cannot be forced to route UDP/DNS
  through a proxy. Strict per-instance isolation at the UDP/DNS layer needs
  host-level controls (firewall/routing/namespaces) outside the portable,
  no-admin product scope.

**Consequence**: AVD Fleet claims (and tests) **TCP token isolation plus
credential redaction**. It does *not* claim full-layer isolation for DNS/UDP,
and the API/CLI do not pretend otherwise. The gate below records the residual
leak surface explicitly.

## G1 proxy-isolation gate

BRIEF §3.4/§7.3 defines G1: five isolated test listeners (HTTP and SOCKS5), each
with its own credentials; five AVDs each assigned one proxy; each guest requests
a URL containing its own token; every token appears **only** on its assigned
listener; no credential in logs/`ps`/API; each instance reaches the Setup Wizard
and resets to OOBE with measured timing. The complete protocol, evidence layout
and pass/fail matrix is in `research/g1/gate.md` §2–§8.

**Status in this repository: BLOCKED — not run.** This build host reports no
usable hypervisor (`VirtualizationFirmwareEnabled=False`, no WHPX/KVM/HVF) and
cannot boot the five emulators; there is also not enough RAM/disk and no
permission to install the SDK. The listener harness itself was self-tested
(five loopback listeners, HTTP CONNECT, SOCKS5 auth, UDP-associate rejection,
token sightings, negative controls) and passes, but that verifies the
tool, not the gate. Proxy/bridge/vault/redaction behaviour is covered by
fakes-backed unit and integration tests only. The gate requires a real
accelerated host and the protocol in `research/g1/gate.md`; the final report must
mark G1 "real emulator" (with host and run directory) or blocked. See
[acceptance.md](acceptance.md).

## Credential and token redaction guarantees

- `ProxyConfig.password` is a Pydantic `SecretStr`; JSON serialisation yields
  `**********`, and API views expose `has_credentials` instead.
- Proxy URLs in logs/errors are masked (`user:***@host`); bearer/basic headers,
  key/value secrets, CLI flags and token-shaped strings are redacted by
  `core.logging.redact`, which is applied to log records, API error bodies and
  persisted run results.
- Subprocess argv never contains proxy passwords. The emulator receives the
  credential-free loopback bridge URL.
- Secrets are never written to the SQLite database or installer manifest.
- Tests scan logs and API responses for known fake secrets.

## Vault and keyring

Configuration: `vault_backend = auto|keyring|file` (env `AVDFLEET_VAULT_BACKEND`).

- `auto` (default): OS keyring when a usable backend exists, otherwise the
  encrypted file vault.
- `keyring`: OS credential store only; fails with an actionable error when the
  `keyring` package/backend is unavailable (headless servers often have none).
- `file`: AES-256-GCM file vault at `<data>/secrets.vault`. Passphrase sources in
  order: explicit argument, `AVDFLEET_VAULT_PASSPHRASE`, then a generated
  0600 key file at `<data>/secrets.key` (zero-config fallback).

On the portable edition use `file` — a keyring entry lives outside the bundle
root and is a documented leftover. Losing the passphrase/key file makes the
stored proxy passwords unrecoverable; re-add the proxies in that case.

## Troubleshooting proxies

| Symptom | Likely cause / fix |
|---|---|
| `check` fails with 407/timeout | Wrong credentials or upstream unreachable from the host; re-import the URL, check `HTTPS_PROXY` interference. |
| Guest request goes direct | No assignment, or the emulator was not launched with the bridge URL; assign then restart. |
| Token seen on several listeners | Assignment swapped or bridge hot-swap misconfigured — a G1 failure; collect evidence. |
| DNS resolves but proxy sees no traffic | Expected for DNS; the proxy only covers TCP. Use a token URL over HTTP(S) for evidence, and document host resolver egress. |
| QUIC/HTTP3 traffic bypasses the proxy | Expected; UDP is not proxied (see above). |
| Keyring prompt / `keyring backend not available` | Switch to `vault_backend = "file"`. |
| 401 from the daemon | Missing/incorrect bearer token (see [api.md](api.md)); not a proxy problem. |

See also [troubleshooting.md](troubleshooting.md) and
[api.md](api.md) for the endpoint list and error envelope.
