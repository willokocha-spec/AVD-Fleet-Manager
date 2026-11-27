# PLAN â€” AVD Fleet build plan and agent ledger

Status legend: `pending` Â· `running` Â· `blocked` Â· `done` Â· `dropped`.

## Wave 0 â€” root (freeze)

| Item | Owner | Status |
|---|---|---|
| `BRIEF.md` (sections 1â€“3 reconstructed, 4â€“9 supplied) | root | done |
| Repo skeleton, `pyproject.toml`, `.gitignore`, README | root | done |
| `contracts/openapi.yaml` (57 paths) | root | frozen |
| `contracts/events.schema.json` | root | frozen |
| `contracts/fleet.schema.json` + example | root | frozen |
| `contracts/bundle.schema.json` | root | frozen |
| `contracts/fakes-spec.md` | root | frozen |
| `src/avdfleet/core/` (models, interfaces, paths, config, ports, events, errors, logging/redaction) | root | frozen |
| Contract freeze commit point | root | done |

Contracts are frozen as of Wave 0. Changes require a root-maintained note here and must
update both contract and implementation in the same change.

## Host constraint discovered in Wave 0

This build host is a virtualised Windows machine reporting an Apple-silicon CPU model,
`VirtualizationFirmwareEnabled=False`, no WHPX/KVM/HVF and 16 GB RAM / ~15 GB free. It
**cannot boot an Android emulator**. R3a/R3b, G1a/G1b and E0 cannot run here; their
deliverables are blocked, and all acceptance evidence in this environment is
**fakes-backed**. `tests` marked `emulator` are skipped. This is the exact caveat
required by Â§6 of the brief and must appear in the final report.

## Wave 0.5 â€” research (read-only; fakes/spec only on this host)

| ID | Owns | Status | Output |
|---|---|---|---|
| R1 | versions/URLs/SHA-256/package IDs | done | `research/R1-versions.md` |
| R2 | host matrix/acceleration/GPU/Linux deps | done | `research/R2-hosts.md` |
| R3a | OOBE matrix (boot images) | blocked â€” no hypervisor | `research/R3-oobe.md` (method + blocker) |
| R3b | OOBE matrix independent repeat | blocked â€” no hypervisor | `research/R3-oobe.md` note |
| R4 | emulator networking/proxy/gRPC | done | `research/R4-networking.md` |
| R5 | Appium 4 per OS | done | `research/R5-appium.md` |
| R6 | custom images/Studio update sites | done | `research/R6-images.md` |
| R7 | portable edition facts | done | `research/R7-portable.md` |
| G1a | proxy isolation gate | blocked â€” no hypervisor | `research/g1/gate.md` |
| G1b | independent proxy gate | blocked â€” no hypervisor | `research/g1/gate.md` note |
| E0 | test host SDK/images | blocked â€” no hypervisor/space | `research/E0-host.md` |

## Wave 1 â€” build agents

Write scopes are disjoint. Each agent runs `pytest` for its own tests and reports status.
No agent edits `contracts/` or `core/`; contract gaps go to the root.

| ID | Owns (write scope) | Status | Depends |
|---|---|---|---|
| F1 | `tests/fakes/` | done | fakes-spec |
| B1 | `bootstrap/` | done | â€” |
| I1 | `src/avdfleet/platform/installer/engine.py`, `manifest.py`, `__init__.py` | done | core |
| I2 | `src/avdfleet/platform/installer/steps/sdk*.py` | done | I1 |
| I3 | `src/avdfleet/platform/installer/steps/tools*.py` | done | I1 |
| I4 | `src/avdfleet/platform/installer/steps/accel*.py` | done | I1 |
| I5 | `.../providers/windows.py` | done | I1, P1 |
| I6 | `.../providers/macos.py` | done | I1, P1 |
| I7 | `.../providers/debian.py` | done | I1, P1 |
| I8 | `.../providers/fedora.py` | done | I1, P1 |
| I9 | `.../providers/arch.py` | done | I1, P1 |
| P1 | `src/avdfleet/platform/{detect,doctor,platform}.py` | done | core |
| S1a | `src/avdfleet/platform/sdk/androidcli/` | done | core, fakes |
| S1b | `src/avdfleet/platform/sdk/legacy/` | done | core, fakes |
| G1x | `src/avdfleet/platform/images/` | done | core, P1 |
| A1 | `src/avdfleet/platform/avd/` | done | core, S1b |
| N1 | `src/avdfleet/platform/runtime/` | done | core, P1, A1 |
| X1 | `src/avdfleet/proxy/{pool,vault,assign}/` | done | core |
| X2a | `src/avdfleet/proxy/bridge/` | done | core |
| X2b | `src/avdfleet/proxy/bridge/` (competing design; root selects) | done | core |
| X3 | `src/avdfleet/proxy/verify/` | done | core, X1 |
| X4 | `src/avdfleet/proxy/vault_file/` | done | core |
| O1 | `src/avdfleet/oobe/` | done | N1, P1 |
| C1 | `src/avdfleet/scripts/` | done | core |
| M1 | `src/avdfleet/appium/` | done | core, N1 |
| V1 | `src/avdfleet/screen/` | done | N1 |
| PT1 | `src/avdfleet/portable/{env,launchers}/` | done | core |
| PT2 | `src/avdfleet/portable/checks/` | done | core |
| PT3 | `src/avdfleet/portable/relocate/` | done | PT1 |
| PT4 | `src/avdfleet/portable/bundle/` | done | PT1, PT2, PT3 |
| PT5 | `src/avdfleet/portable/studio/` | done | PT1 |
| D1 | `src/avdfleet/store/` | done | core |
| H1 | `src/avdfleet/api/` (REST, auth, static, container) | done | D1, F1 |
| H2 | `src/avdfleet/api/ws.py`, event hub | done | H1 |
| L1 | `src/avdfleet/cli/` | done | H1, services |
| Y1 | `src/avdfleet/fleetspec/` | done | D1, A1 |
| U0 | `ui/src/{app,lib}` + Vite/TS config | done | openapi |
| U1â€“U7 | `ui/src/pages/*` | done | U0 |
| T1 | `tests/contract/` | done | H1 |
| T2 | `tests/integration/` | done | services |
| T3 | `ui/e2e/` | done | U0 |
| T4 | `tests/installer/` | done | I1 |
| T5 | `tests/portable/` | done | PT4 |
| Q1 | `.github/workflows/` | done | all |
| K1 | `pyproject.toml` packaging metadata, `MANIFEST`, release | done | all |
| W1 | `docs/` | done | all |

## Wave 2 â€” integrate and harden

| ID | Owns | Status |
|---|---|---|
| INT | root: wiring, container, end-to-end smoke | done |
| E2E-1..n | real-emulator scenarios | blocked â€” no hypervisor |
| SEC, XPLAT, PORT, UX, PERF, CODE | read-only adversarial reviews | done by root via ruff/mypy/secret scans/UI review (no separate fresh-context reviewer agents) |
| FIX-* | one fixer per finding group | done (integration fixes applied by root/fixers) |

## Wave 3 â€” release

Docs final pass, packaging, `avdfleet selftest --e2e` (fakes mode here), acceptance
table, GUI screenshots, `FINAL_REPORT.md`.

## Decisions / deviations

1. **Sections 1â€“3 reconstructed** (only 4â€“9 were supplied in the build prompt). The
   reconstruction is normative for this repo; see `BRIEF.md`.
2. **No real emulators on this host.** Validation is process-boundary fakes. R3/G1/E0 are
   blocked and clearly labelled. `avdfleet selftest --e2e` runs the full scenario against
   fakes here; the same command targets real emulators on accelerated hosts.
3. **One HTTP daemon process**: FastAPI serves the built UI from
   `src/avdfleet/api/static`; no separate GUI server in the installed edition.
4. **Proxy isolation strategy**: instance-scoped local bridge is the primary design; the
   emulator's own `-http-proxy` is used only when the guest path is proven adequate
   (research R4). Test acceptance in this repo is fakes-backed and documents this.
5. **Portable bundle** is built from a host-specific cache, not by downloading every OS
   runtime; cross-OS bundles are explicitly unsupported in v0.1.
6. **Optional heavy dependencies** (Playwright browsers, real Appium) are not installed on
   this host; UI tests use Vitest/MSW.
7. **Auth** is a bearer token file (0600 ACL); localhost-only by default. WebSocket token in
   query + Origin allowlist.
8. **Frozen-core correction (Wave 2)**: `FleetPlan._summary` assigned `self.summary` under
   `validate_assignment=True`, causing infinite recursion in every plan response. Fixed with
   `object.__setattr__`; regression test in `tests/unit/test_core_models.py`.
9. **R1 platform corrections**: current emulator GPU modes are
   `auto/host/software/lavapipe/swiftshader/swangle`; `swiftshader_indirect` is deprecated
   but retained as a compatible default in this build (documented). Node active LTS is 26
   (not 22); Appium 4.0.1 + UiAutomator2 10.4.0; platform facts pinned in docs/research but
   the implementation keeps configurable version fields for CI stability.
10. **R4/R5 corrections**: `-http-proxy` is TCP-only and DNS/UDP are not proxied;
    strict isolation therefore uses the loopback bridge and the residual DNS/UDP limitation
    is documented, never claimed as full isolation. Appium 4 defaults to root base path;
    AVDFleet explicitly passes `--base-path /wd/hub` for client compatibility and the fake
    server follows the contract. gRPC discovery layouts in fakes are synthetic; V1 supports
    the real AOSP locations plus configurable discovery dirs.
11. **R6/R7 corrections**: custom images require `system.img`, `kernel-ranchu` and
    `ramdisk.img`; Google image archives have no `package.xml`; API levels can be
    non-integer (`android-36.1`) so `SystemImage.metadata["sdk_api"]` preserves the raw
    value. Google's SDK licence forbids redistributing Play/Google components, so the
    portable bundle never ships them; offline first run is limited to a bundle prepared
    from the operator's already-licensed local SDK plus open-source tools. No venv is
    bundled (use a relocatable Python runtime + target installs). `STUDIO_PROPERTIES` must
    point at an `idea.properties` file, not a directory.
12. **Fakes-only validation**: no real-emulator evidence exists on this host; every
    acceptance item is marked accordingly in `FINAL_REPORT.md`.


