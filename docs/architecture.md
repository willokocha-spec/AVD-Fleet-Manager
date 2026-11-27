# Architecture

AVD Fleet is a single Python package with a React front end and two distribution modes.

```
                         ┌──────────────────────────── GUI (React/Vite) ───────────────────────────┐
                         │ Dashboard · Fleet · Images · Templates · Proxies · Scripts · Settings   │
                         └───────────────────────────────┬─────────────────────────────────────────┘
                                           REST /api/v1 │ WebSocket /ws/*
┌──────────────────────────────┐                       ▼
│ CLI (Typer)                  │         ┌──────────────────────────────┐
│ avdfleet install/doctor/...  │────────▶│ FastAPI daemon (api/)        │
└──────────────┬───────────────┘         │ auth · DI · static UI · WS   │
               │                         └──────────────┬───────────────┘
               │                                        │ ServiceContainer
               ▼                                        ▼
┌─────────────────────────────────────────────── services ───────────────────────────────────────┐
│ installer │ images │ avd │ runtime │ proxy pool/vault/assign/bridge/verify │ oobe │ scripts      │
│ appium    │ screen │ fleetspec │ portable/bundle/checks/relocate/studio                        │
└───────────────┬───────────────────────────────┬──────────────────────────────┬───────────────┘
                │                               │                              │
        ┌───────▼────────┐              ┌───────▼───────┐              ┌───────▼─────────┐
        │ store (SQLite) │              │ core (models, │              │ host tools      │
        │ WAL+migrations │              │ Protocols,    │              │ adb, emulator,  │
        └────────────────┘              │ config,ports) │              │ sdkmanager, ... │
                                        └───────────────┘              └─────────────────┘
```

## Layering rules

1. `core/` imports **nothing** from other avdfleet packages. It owns Pydantic models,
   Protocols, error codes, configuration, paths, port allocation, the event bus and
   logging/redaction.
2. `platform/`, `proxy/`, `oobe/`, `scripts/`, `appium/`, `screen/`, `portable/`, `store/`
   may import `core` and each other only through Protocols or narrow constructor
   dependencies. No service imports FastAPI.
3. `api/` and `cli/` are thin adapters. They never contain business logic.
4. `ui/` talks only to `/api/v1` and `/ws/*`; every page is mocked in MSW so UI work is
   independent of backend availability.
5. Adapters (SDK toolchains, host providers, vaults) are selected at the container edge.
   Tests inject fakes; services never import test code.

## Packages

| Path | Responsibility |
|---|---|
| `core/models.py` | Domain schema shared by every layer and mirrored in contracts |
| `core/interfaces.py` | One Protocol per subsystem; the integration seam |
| `core/paths.py` | Installed vs portable filesystem layout, no writes outside portable root |
| `core/config.py` | TOML + `AVDFLEET_*` env configuration, capacity defaults |
| `core/ports.py` | Emulator console/adb pair and side-channel allocation |
| `core/events.py` | In-process pub/sub feeding `/ws/events` |
| `core/logging.py` | Structured logging and the single redaction choke point |
| `platform/installer/` | Ordered idempotent steps, providers per OS, resume manifest |
| `platform/sdk/` | Legacy `sdkmanager/avdmanager` and new Android CLI behind one interface |
| `platform/images/` | Stock catalogue, custom import, OOBE matrix |
| `platform/avd/` | Templates, AVD CRUD, bulk deploy, `config.ini`, YAML import/export |
| `platform/runtime/` | Launch, boot detection, state machine, supervision, snapshots |
| `proxy/pool/` | Parse/validate proxies, encrypted secret storage, health checks |
| `proxy/assign/` | Per-AVD assignment strategies |
| `proxy/bridge/` | Loopback HTTP/SOCKS5 bridge with upstream auth and hot swap |
| `proxy/verify/` | Host-side and guest-side egress evidence |
| `oobe/` | Setup Wizard detection, reset strategies, golden snapshot timing |
| `scripts/` | adb-shell / host-Python / Appium fan-out executor, streams, cancel |
| `appium/` | Appium 4 server lifecycle, parallel-session port plan, capabilities |
| `screen/` | Emulator gRPC screen stream, screencap fallback, input, scrcpy |
| `portable/` | Environment, launchers, checks, relocation, bundle, Studio config |
| `store/` | SQLite schema, migrations, repositories (no plaintext proxy passwords) |
| `api/` | Auth, DI, routes, WS hub, static SPA, OpenAPI contract |
| `cli/` | Typer mirror of every capability and `selftest --e2e` |
| `fleetspec/` | `fleet.yaml` parsing, plan/diff, apply reconciler |

## Data and filesystem

Installed mode (defaults; all overridable):

| Purpose | Windows | macOS | Linux |
|---|---|---|---|
| data | `%LOCALAPPDATA%\avdfleet` | `~/Library/Application Support/avdfleet` | `$XDG_DATA_HOME/avdfleet` |
| config | `%APPDATA%\avdfleet` | `~/Library/Application Support/avdfleet` | `$XDG_CONFIG_HOME/avdfleet` |
| cache | `%LOCALAPPDATA%\avdfleet\Cache` | `~/Library/Caches/avdfleet` | `$XDG_CACHE_HOME/avdfleet` |
| logs | `%LOCALAPPDATA%\avdfleet\Logs` | `~/Library/Logs/avdfleet` | `$XDG_STATE_HOME/avdfleet/logs` |

The SQLite database (`avdfleet.db`) never stores proxy passwords: rows carry a
`secret_ref` and the secret lives in the OS keyring or the encrypted file vault.
Portable mode prefixes everything with the bundle root (`data/`, `config/`, `cache/`,
`logs/`, `sdk/`, `avd/`) and records the root in `avdfleet-portable.json`.

## Instance lifecycle

```
defined ──start──▶ starting ──emulator pid──▶ booting ──sys.boot_completed=1──▶ running
   ▲                  │                         │                                  │
   │                  └────────error────────────┴──────────error───────────────────┤
   └──────────────────────────── stop ◀── stopping ◀── stop/restart ◀───────────────┘
```

Boot detection polls `adb -s emulator-<port> shell getprop sys.boot_completed` and the
package manager readiness (`pm path android`), with a configurable timeout. Supervisors
watch the emulator process and the adb device; on unexpected exit the state becomes
`error` and the event bus publishes an `instance.state` event.

## Proxy isolation model

The strict mode used by acceptance tests:

```
guest app ──HTTP(S)──▶ emulator proxy setting ──▶ loopback bridge (127.0.0.1:auto)
                                                    │  injects upstream credentials
                                                    ▼
                                          assigned authenticated proxy (HTTP or SOCKS5)
```

- The assigned upstream is stored only in the vault; the bridge holds it in memory.
- Each instance gets a distinct bridge port; bridge configs never expose the upstream
  userinfo.
- The verifier issues a tokenized request from inside the guest and proves that the token
  arrived at the assigned listener and nowhere else. G1 requires evidence from five
  listeners, each with distinct credentials and tokens.
- Credentials are stripped from logs, API responses, error messages and stored run
  records by `core.logging.redact`; process command lines never receive proxy passwords.

Research R4 documents which guest traffic the emulator's own proxy covers. Where
`-http-proxy` does not cover a path (notably UDP/DNS bypass), strict mode uses the bridge
and/or explicitly documents the residual limitation rather than claiming coverage.

## Testing strategy

| Layer | What runs | Acceleration needed |
|---|---|---|
| unit | core, parsers, services, installer steps, vaults | no |
| fakes-based integration | API, fleet scenarios, scripts, portable | no |
| contract | OpenAPI path/schema conformance, JSON schemas | no |
| UI | Vitest + MSW; Playwright against MSW/daemon+fakes | no |
| portable | bundle/verify/relocation/checks | no |
| `@pytest.mark.emulator` | real AVD boot, OOBE, proxy G1, Appium | WHPX/KVM/HVF |

On hosts without hardware virtualisation the emulator tests are skipped and the report
marks those acceptance items as **fakes**. This repository's build host is one of those.
