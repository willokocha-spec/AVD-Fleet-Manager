# Testing and contributing

The test strategy is **no real Android tooling required**: every layer runs
against executable **process-boundary fakes**, so the suite is deterministic and
works on Windows, macOS and Linux without an SDK, emulator, JDK, Node or
hypervisor. Tests that need real acceleration are marked `emulator` and skipped
automatically.

## Test layers

| Layer | Location | What it covers | Acceleration |
|---|---|---|---|
| unit | `tests/unit/` | core models/config/paths/ports/logging, SDK backends, installer engine/steps/providers, images, AVD config, runtime, proxies/bridge/vault/verify, OOBE, scripts, appium, screen, store, portable | no |
| fakes contract | `tests/fakes/` | fake tool contract from `contracts/fakes-spec.md`, state/event logs, fixtures | no |
| API | `tests/api/` | REST routes, auth/token ACL, error envelopes, WebSocket events/screen, static UI | no |
| contract | `tests/contract/` | OpenAPI path/schema conformance and checked-in JSON contracts | no |
| integration | `tests/integration/` | fleet scenarios end-to-end against fakes: container wiring, fleetspec plan/apply, full §7-shaped scenario | no |
| portable | `tests/portable/` | bundle build/verify, preflight checks, launchers, relocation, Studio config | no |
| CLI | `tests/cli/` | root command tree, installer commands, service commands, selftest | no |
| bootstrap | `bootstrap/test_bootstrap.py` | static checks of `install.sh`/`install.ps1` (flags, safety, PowerShell 5.1 syntax) | no |
| UI | `ui/src/test/` | Vitest + Testing Library + MSW: app/nav/404, page registry, typed API, auth, components, WebSocket | no |
| E2E | `tests/e2e/`, `ui/e2e/` | real emulator acceptance / Playwright — **placeholders in this checkout** | yes |

The fakes are defined by the frozen [contracts/fakes-spec.md](../contracts/fakes-spec.md):
`fake_cli.py` implements `adb`, `emulator`, `sdkmanager`, `avdmanager`, `android`,
`appium`, `node`, `npm`, `uv`, `java`, `scrcpy`, `python`; `tests/fakes/runtime.py`
builds OS-appropriate shims and records every invocation in `events.jsonl`.
Tests pass `AVDFLEET_FAKE_*` environment variables and never touch the real SDK
or `~/.android`.

## Running the suite

```bash
python -m pip install -e ".[dev]"    # or: uv tool install / pip install -e .
pytest                                # everything except emulator tests (skipped)
pytest -m "not emulator"             # explicit
pytest tests/unit tests/api tests/contract tests/portable tests/cli      # fast layers
pytest tests/integration              # fleet scenarios against fakes
pytest -m emulator                    # real emulator only (needs WHPX/KVM/HVF)
pytest --cov=avdfleet --cov-report=term-missing
python -m pytest bootstrap/test_bootstrap.py -q
```

Requires Python **3.11–3.13** (`requires-python = ">=3.11,<3.14"`). The
`pytest` configuration lives in `pyproject.toml` (`pythonpath = ["src", "."]`,
`asyncio_mode = "auto"`, `timeout = 120`); markers are declared there:
`emulator`, `integration`, `contract`, `portable`, `ui`, `slow`.

Emulator tests are skipped when `WHPX`/`KVM`/`HVF` is unavailable — the
`platform.PlatformDetector` honours `AVDFLEET_FAKE_ACCEL` so they can also be
simulated, but no test may claim they are real-emulator evidence.

### UI tests

```bash
cd ui
pnpm install
pnpm typecheck
pnpm lint
pnpm test            # Vitest run once (node scripts/test.mjs)
pnpm build           # tsc --noEmit + vite build -> ui/dist
pnpm build:daemon    # build + copy to src/avdfleet/api/static (for packaging)
```

The UI is developed against the frozen OpenAPI contract with an in-browser MSW
mock daemon; `VITE_USE_MOCKS=false` plus `pnpm dev` proxies to a real
`avdfleet serve` at `127.0.0.1:8765` (see [ui/README.md](../ui/README.md)).
Playwright against MSW/daemon+fakes belongs to `ui/e2e/` and is not part of
`pnpm test`; that directory is a placeholder in this checkout.

### Bootstrap tests

`python -m pytest bootstrap/test_bootstrap.py -q` inspects the scripts (flags,
safety invariants, shebang/line endings, PowerShell 5.1 compatibility, README
coverage). It never installs software. If `bash`/PowerShell exist it also runs
`bash -n`/the PowerShell parser; otherwise those checks are skipped.

### Contract tests

`tests/contract/test_openapi_static.py` asserts that the 57 paths and component
schemas in `contracts/openapi.yaml` stay consistent with the checked-in contract
shape, and the API tests exercise the routes. `contracts/fleet.schema.json`,
`events.schema.json` and `bundle.schema.json` are validated wherever they are
used. Contract changes require a root-maintained note in `PLAN.md` and updating
the implementation in the same change.

## Acceptance self-test

The machine-checkable acceptance command is:

```bash
avdfleet selftest --e2e --json        # real-emulator host
avdfleet selftest --fakes --fast --json   # this repo's build host / CI
```

`--fakes` labels the report `evidence: fakes`; it can never be mistaken for real
emulator evidence. Host checks always run; on a host without a hypervisor the
real-emulator items are reported as `SKIP` and the command exits `0` unless
`--require-acceleration` is passed (then `3`). The report contains per-item
`status`, `detail`, `duration_s` and data (instance names, image ids, run ids).
See [acceptance.md](acceptance.md) for the item-by-item status in this checkout.

## CI

`.github/workflows/ci.yml` runs on GitHub Actions:

| Job | Matrix | What it does |
|---|---|---|
| `python` | ubuntu-latest, macos-latest, windows-latest | `ruff check src tests bootstrap`, `mypy`, `pytest -m "not emulator"` (includes contract/API/fakes); `bootstrap/test_bootstrap.py` |
| `ui` | ubuntu-latest | `pnpm install --frozen-lockfile`, `pnpm typecheck`, `pnpm lint`, `pnpm test`, `pnpm build` |
| `installer-containers` | debian/ubuntu/fedora/rocky/arch images | best-effort installer test in containers; **skips cleanly if Docker is unavailable** |
| `kvm-e2e` | ubuntu-latest | best-effort accelerated emulator E2E with `continue-on-error: true` — GitHub calls nested virtualisation unsupported, so this must never gate merges |

There is also a `docs` link/existence check (Python stdlib only) and
`nightly-emulator.yml` (scheduled best-effort KVM run, same caveat). Real
emulator tests are never reported as passing by CI; they are best-effort jobs.

## On this build host

This checkout's build host reports **no usable hypervisor** (no WHPX/KVM/HVF,
16 GiB RAM, ~13.5 GiB free) and cannot install the SDK or boot an emulator.
Everything here is fakes-backed. The current evidence status, including the last
test-run numbers and the full-suite teardown timeout that was observed while
these tests were still being written, is recorded honestly in
[acceptance.md](acceptance.md). Do not cite this repository's CI as proof of
real-emulator behaviour.

## Contributing

- Keep `core/` free of imports from other avdfleet packages; services take
  dependencies through Protocols/constructors (no global singletons except
  logging).
- Never put secrets in argv, logs, error bodies or persisted run records; use
  `core.logging.redact` at every boundary and add tests for the new sink.
- Add tests at the right layer; tests must not require real tools. Mark anything
  needing acceleration `@pytest.mark.emulator`.
- Run `ruff check src tests bootstrap`, `mypy`, `pytest` and (for UI changes)
  `pnpm typecheck && pnpm lint && pnpm test` before sending a change.
- Contract or `core/` changes are root-owned: record them in `PLAN.md` →
  Decisions and update contract + implementation together.
