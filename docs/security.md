# Security model

AVD Fleet is a local operator tool, but it controls processes, filesystem paths and
authenticated proxy credentials. The following invariants are enforced in code and
checked by tests/reviews.

## Threat model

- **Local attacker / other process**: may read world-readable files, inspect process
  command lines and connect to loopback ports.
- **Malicious web page**: may attempt CSRF against the local daemon or cross-origin
  WebSocket connections.
- **Untrusted proxy credentials**: must never leak into logs, API responses, process
  argv, crash traces or persisted run records.
- **Untrusted custom image directory**: must not be able to write outside the SDK root
  or traverse paths.
- **Compromised UI**: served locally; no secrets in localStorage beyond the API token.

Out of scope: a multi-user server deployment, and hostile root/admin on the same
machine.

## API

- Binds `127.0.0.1` by default. Changing `api_host` to a non-loopback address requires an
  explicit configuration edit; docs warn loudly.
- Bearer token generated with `secrets.token_urlsafe(32)`, stored in
  `<data>/token` with owner-only permissions (`0600`; Windows ACL via `icacls` best
  effort). `AVDFLEET_TOKEN` can override for tests/containers.
- Constant-time comparison. Missing/invalid token → 401 with the standard error envelope.
- The token is never written to daemon output: the GUI authenticates its WebSockets with a
  **single-use ticket** (`POST /api/v1/auth/ws-ticket`, burned on first use), and uvicorn's
  `error`/`access` loggers carry a redaction filter so a legacy `?token=` URL cannot reach a
  log record either.
- `GET /health` is unauthenticated but exposes only status/version.
- CORS is an explicit allowlist of GUI origins; `TrustedHostMiddleware` limits Host
  headers. Browsers cannot use the API without the token, which cross-origin JS cannot
  read from the local filesystem.
- WebSocket `/ws/events` and `/ws/instances/{name}/screen` require authentication **and** an
  allowed `Origin`; missing credentials or origin closes with 1008. Browsers present a
  single-use ticket (`?ticket=`), CLI clients send `Authorization: Bearer`; the legacy
  `?token=` query parameter still works but is deprecated and redacted from logs. Screen
  frames are per-instance and never include shell credentials.
- `avdfleet gui` can open a **one-time sign-in link** instead of asking for the token.
- Emulator logs contain the per-instance listener URL (an `http_proxy` with its token), so on
  POSIX they are created `0600` inside a `0700` directory; the data directory ACL covers
  them on Windows.
- Acquiring a pinned artifact from a URL refuses an **https → http** downgrade before the
  insecure request is sent, refuses redirects to loopback, link-local, private and reserved
  addresses, and never prints a URL's query string in an error.
  It requests a short-lived, single-use code over the authenticated loopback API, then
  opens `/auth/bootstrap#code=...`. The code stays in the URL fragment (never sent to the
  server or written to logs), is consumed on first redemption, and is exchanged for the
  token over loopback. The token is written to browser local storage and is never rendered
  or placed in the URL. Pass `--no-auto-auth` to skip this and use the Connect screen.
- Remote binds are fail-closed: `--allow-remote` is required, authentication must remain
  enabled, and `--host 0.0.0.0`/`::` additionally requires an explicit `--trusted-host`.
  A concrete bind host and the actual port are added to the Host/Origin allowlists
  automatically; `--trusted-origin` covers reverse-proxy hostnames. This closes the
  DNS-rebinding gap without accepting every `Host` header.
- Daemon secrets are removed from every child environment (`AVDFLEET_TOKEN`,
  `AVDFLEET_VAULT_PASSPHRASE`, future `AVDFLEET_SECRET_*` names) before scripts, Appium,
  the emulator, SDK tools or installer commands are spawned. User scripts receive only
  their explicit `AVDFLEET_*` run variables, never the daemon's bearer token or vault
  passphrase.
- Each bridged AVD has a random per-instance loopback proxy password, passed to the
  emulator through its `http_proxy` environment (not `argv`, so it does not appear in
  `ps`/command-line logs). Another AVD or local process cannot reuse that listener.
- `ADB_LOCAL_TRANSPORT_MAX_PORT` is raised so adb accepts all 64 emulator ports. adb
  reads it only when its server starts; `avdfleet doctor --slow` fails if an existing
  adb server was started with the default 16-emulator limit and gives the safe restart
  instruction (`adb kill-server`).
- Responses set `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`. Stack traces are never returned.

## Secrets

| Secret | Storage | Exposure |
|---|---|---|
| API token | token file, 0600 | client localStorage only; never returned by API |
| Proxy passwords | OS keyring or AES-GCM file vault | never in DB; masked in API/UI/logs |
| Vault passphrase | env `AVDFLEET_VAULT_PASSPHRASE` or prompt | never persisted |
| Emulator console token | emulator's own token file | read by console client only |
| gRPC screen token | emulator discovery file | passed to gRPC client in memory |

- The database stores only `secret_ref` for proxies.
- `ProxyConfig.password` is a Pydantic `SecretStr`; JSON serialisation yields
  `**********`; API routes return a view with `has_credentials` instead.
- `core.logging.redact()` masks URL userinfo, bearer/basic headers, key/value secrets,
  CLI flags, env assignments and token-shaped strings. It is applied to every log record
  (filter), every API error body, and every persisted run result. Tests scan logs and
  responses for known test secrets.
- Subprocess argv must not contain proxy passwords. The emulator receives a loopback
  bridge URL with a random per-instance listener password through `http_proxy` (never
  `-http-proxy`), so neither the upstream credential nor the listener credential appears
  in the process command line. Direct `-http-proxy` remains only for credential-free
  upstreams; authenticated direct proxies are refused.
- Run output is redacted before persistence/streaming; hooks receive environment
  variables only via explicit script definitions, not the daemon's environment.
  `core.childenv` sanitizes every child-process environment at the spawn boundary.

## Installs and elevated actions

- License acceptance happens only with explicit `--accept-android-sdk-licenses`; the
  installer never pipes `yes` by default.
- Package-manager commands are argv arrays, not shell strings, where possible; on Windows
  quoting is handled by the OS. Commands that require elevation print the exact command
  instead of silently re-launching.
- Downloads are HTTPS; when upstream publishes SHA-256 the installer/bundle verifier
  checks it. Unverified downloads are surfaced as warnings.
- Custom image import validates that paths are under the SDK root after resolution,
  rejects symlink escapes and never overwrites without `--force`.
- `avdfleet uninstall` deletes only known avdfleet directories; `--keep-data` preserves
  state. No recursive deletes on computed paths without resolving and checking the
  target.
- Portable bundle verification rejects path traversal, absolute paths, symlinks and
  modified/missing files. FAT32/noexec are refused before running.

## Web/UI

- React escapes all rendered data; no `dangerouslySetInnerHTML`. Log viewer renders text.
- API token is held in memory/localStorage only; no cookies, so CSRF surface is limited to
  the token and Origin checks.
- MSW mocks contain fake credentials only.

## Reporting a vulnerability

This is a local lab tool shipped without a hosted service. Report issues privately to the
maintainers, attaching the affected version, platform and a redacted reproduction. Do not
include real proxy credentials or SDK license files.
