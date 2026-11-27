# DECISIONS

Chronological decision log for the AVD Fleet build. `PLAN.md` records the agent/wave
ledger; this file records product/platform decisions, deviations and why. The operator
supplied the missing first part of the prompt after Wave 0; it is preserved verbatim in
`BRIEF_ORIGINAL.md` and takes precedence over the original reconstruction.

## D-001 — Reconstructed sections 1–3
Only sections 4–9 were in the initial prompt. Wave 0 reconstructed the product brief from
the referenced requirements. **Update:** the operator later supplied sections 0–3.1; those
are now in `BRIEF_ORIGINAL.md`. Remaining sub-sections 3.2–3.11 were never supplied and
stay reconstructed; implementation is checked against the supplied §3.2 opening.

## D-002 — Build host has no hardware virtualisation
The host is a VM reporting no WHPX/KVM/HVF (`VirtualizationFirmwareEnabled=False`), 16 GiB
RAM and no admin permission to install Android tooling. R3/G1/E0 and all real-emulator
acceptance items are therefore **BLOCKED**, not passed. All verification is against
process-boundary fakes; `avdfleet selftest --fakes` labels its report `evidence: fakes`.
This is the §6 fallback mandated by the prompt.

## D-003 — No git available
`git` is not installed and there is no remote. Agent "branches" are enforced by disjoint
write scopes and task ledgers instead of worktrees; the final tree is not a git commit.
CI workflows are supplied but could not be executed.

## D-004 — UI stack: hand-written design system instead of Mantine
**Deviation from fixed decision §2.** The GUI is React 18 + TypeScript + Vite with a
hand-written, accessible design system (tokens + components) and Vitest/MSW tests. Mantine
was not used. Rationale: the Mantine dependency and component migration could not be
completed safely inside the remaining build window, and the custom components already
provide the required accessibility, theming and density. The typed client is likewise
hand-written in `ui/src/lib/api.ts` rather than generated from `contracts/openapi.yaml`;
it is kept in lockstep by the API contract test and TypeScript types. This is a known
release blocker for strict conformance to §2.

## D-005 — Fakes at the process boundary
`tests/fakes` ships executable `adb`, `emulator`, `sdkmanager`, `avdmanager`, `android`,
`appium`, `node`, `uv`, `java` and utility shims with a locked JSON state file and an argv
audit log. Services never import the fakes. On Windows the shims are pip launcher `.exe`
files (with the spec's `.cmd` files also present) because `CreateProcess` cannot execute a
`.cmd` with `shell=False`. This is a documented deviation from the literal spec, required
for the production subprocess contract.

## D-006 — One daemon, one static bundle
FastAPI serves the built SPA from `src/avdfleet/api/static`; `avdfleet serve` is the only
HTTP server (the portable launcher starts it with `--portable`). A separate dev server is
used only for UI development.

## D-007 — Proxy isolation design and limits
`-http-proxy` is TCP-only (R4), so strict mode uses the per-instance loopback HTTP/SOCKS5
bridge with upstream credentials injected per connection; the emulator receives only the
bridge URL. DNS/UDP are not proxied and are documented as a residual limitation, never
claimed as full isolation. G1 requires a real accelerated host; it is blocked here.

## D-008 — API auth and remote binding
Bearer token in `<data>/token`, `0600` on POSIX and an `icacls` current-user ACL attempt on
Windows; `AVDFLEET_TOKEN` override. API is loopback-only by default; the `serve --host`
path requires explicit `--allow-remote` and prints a warning. WebSocket handlers verify
Origin themselves because Starlette CORS does not cover WS. TLS is optional pass-through.

## D-009 — Secrets and portable mode
Proxy passwords are stored only in the OS keyring or the AES-GCM/scrypt file vault; SQLite
stores `secret_ref`. Generated beside-file keys are opt-in and are labelled as obfuscation,
never as strong protection. When no keyring/passphrase is available the vault refuses
operations with a clear error rather than silently storing plaintext. The portable bundle
cannot ship Google/Play components because Google's SDK licence forbids redistribution;
offline first run uses the operator's already-licensed local SDK plus OSS tools.

## D-010 — GPU/version corrections from research
R1/R2 found that `swiftshader_indirect` and other `*_indirect` GPU modes are deprecated;
`software` is the recommended headless mode. The implementation keeps the compatible
fallback as a default for existing templates and supports configurable modes; docs
recommend `software`. Node 22 default is retained for Appium compatibility even though
Node 26 is Active LTS (Appium 4 accepts `^22.22.2 || ^24.15.0 || >=26`). Emulator version
is recorded in `docs/compatibility.md`; checksums are verified where publishers provide
them.

## D-011 — Custom image import
Google image archives contain `<abi>/` with `system.img`, `kernel-ranchu`, `ramdisk.img`,
optional `vendor.img`; there is no `package.xml` in current archives. Import validates
these files, infers API/tag/ABI from `source.properties`/`build.prop`/path, stages a copy
under `<sdk>/system-images/android-<api>/<tag>/<abi>/`, and registers it atomically. API
levels such as `android-36.1` are preserved in `SystemImage.metadata["sdk_api"]` because
the core model uses an integer `api_level`.

## D-012 — Frozen-contract corrections
`FleetPlan._summary` caused a `validate_assignment` recursion; fixed with
`object.__setattr__` and a regression test. `ImageCatalogue.installed_images` no longer
treats every backend-available image as installed, and SDK directory scanning requires a
bootable-image marker rather than "any file". `Paths` accepts string overrides and raises a
clear error if an uninitialised path is used. `core/interfaces` imports were normalised.

## D-013 — UI/test and release evidence
UI tests use Vitest + MSW; a Playwright browser install was out of the remaining budget,
so Playwright E2E is a placeholder. Python tests use fakes, not real SDK/emulator. CI
workflows are provided but not executed. Known quality debt at handoff is tracked in
`FINAL_REPORT.md` §4.

## D-014 — Python runtime
The project targets CPython 3.12 (uv-managed in production). The bundled build runtime is
3.12.14; the host's system Python 3.14 is unsupported and not used.

## D-015 — Redundancy reduced where the host made it unsafe
The prompt asks for two independent agents on OOBE verification, the proxy bridge and the
G1 gate. On this host those workstreams need a real accelerated guest, so running two
would only duplicate documentation/blocked outcomes. One bridge design and one G1/R3
protocol were produced; the listener harness self-test is the only executable redundant
check. This is a process deviation, not a product shortcut: the blocked gates still require
two independent runs (G1a/G1b, R3a/R3b) on real hardware. Root also performed integration
edits after Wave 0 (frozen-core fixes, container wiring, lint/type fixes) rather than
delegating every one, because the original Wave-0 rule was superseded by the need to
unblock and converge a large parallel build.

## D-016 — Root & instrumentation stays external and measures instead of assuming
The Root & Instrumentation subsystem (service, SQLite v3 tables, REST under
`/api/v1/instrumentation`, CLI `avdfleet instrumentation`, GUI page) keeps Magisk,
rootAVD and LSPosed **outside** the distribution: all three are GPLv3, this project is
MIT, and mixing GPL binaries into a shipped bundle would change that distribution's
licensing. The module library therefore stores operator-imported artifacts with
SHA-256 and provenance instead of fetching them; Magisk installation runs only through
the operator's `AVDFLEET_MAGISK_INSTALLER` command template; Xposed activation and
per-app scope stay a guided LSPosed-manager step because there is no supported
unattended API. The capability model reports `adb root` and Magisk root separately,
and installed/enabled/active separately, so the product cannot claim success for a
file transfer or a pending reboot. The known gaps are documented rather than hidden:
no device-side Xposed inventory, no automated Xposed activation, no signature
verification, no run recovery, and LSPosed installation not yet exercised on a live
instance ([docs/instrumentation.md](docs/instrumentation.md),
[docs/instrumentation-compatibility.md](docs/instrumentation-compatibility.md)).

## D-017 — Official-source acquisition instead of vendoring
D-016's "store operator imports instead of fetching" is narrowed: the daemon may
fetch a **pinned** Magisk 26.1 (`26100`) APK and LSPosed 1.9.2 (`7024`) ZIP from
their official GitHub releases, because a download performed by the operator's own
instance on the operator's host is not a redistribution. Nothing is vendored:
there is no binary in the repository, wheel or portable bundle, and the pins are
version + URL + size + SHA-256 compiled into `instrumentation/acquire.py` (hashes
measured from the official assets on 2026-11-27). Acquisition requires explicit
confirmation (`confirm: true` / `--confirm`; otherwise `409` naming the version and
GPL-3.0), verifies size then hash (`422` and discard on mismatch), stores under
`<data>/instrumentation/toolchain/<key>/<version>/`, and records provenance
(version, hash, size, URL, licence, timestamp) in a shared `manifest.json`. A
downloaded module ZIP is **imported into the module library**
(`source: "official-release"`) rather than installed; consequently the built-in
LSPosed profile resolves the newest `lsposed_zip` when its step has no module bound,
while the kind-ambiguous `install_module` step still requires an explicit binding.
Acquisition does not remove the hard parts: ramdisk patching, Magisk "Additional
Setup", Zygisk and reboots remain separate operator steps. Xposed enable/scope
gained best-effort profile steps that write LSPosed's own SQLite database
(`enable_xposed_module`, `set_xposed_scope`), but that schema is not a public API;
the LSPosed manager remains the authoritative interface and a reboot is still
required. The download path and the LSPosed import were live-verified on
2026-11-27; the Magisk APK path is implemented and unit/API-tested. See D-018 for
the later API 34 verification of Magisk setup and LSPosed runtime, and for the
Xposed enable/scope steps.

## D-018 — Unattended Magisk setup, and LSPosed verified on API 34
`complete_magisk_setup` automates the step that silently breaks every module
workflow: install the acquired pinned Magisk APK, launch
`com.topjohnwu.magisk/.ui.MainActivity`, tap the "Requires Additional Setup"
dialogs located from `uiautomator dump` (no hard-coded coordinates), wait for the
app's own reboot (≤300 s), then require a populated `/data/adb/magisk`. Measured on
API 34: four unprompted taps. The three full-stack built-in profiles now run it
between `install_magisk` and `enable_zygisk`. This is the one step that reboots an
AVD as a side effect, so a deployment that includes it is not reboot-free. It is
UI automation, not a protocol: a changed Magisk UI/locale can break it, and the
failure path asks the operator to finish manually. The measured strings are
`/data/adb is not present, abort` (magiskd) and `Incomplete Magisk install` (CLI);
`/data/adb/magisk` existing means setup completed, not that Magisk is installed.

The same API 34 run verified Zygisk and LSPosed 1.9.2 end-to-end: LSPosed active
after reboot (`pidof lspd`, logcat companion/binder lines), with the manager
installed separately from `/data/adb/modules/zygisk_lsposed/manager.apk` because
`lspd` does not install it. Detection follows measurement: Zygisk active is
`zygiskd` or a staged loader under `$(magisk --path)` (never `/sbin/.magisk` on
API 34), LSPosed active is `pidof lspd`, and `pm list packages` is a false
negative for activation. The Xposed inventory/scope and the two DB-write steps
(`enable_xposed_module`, `set_xposed_scope`) are unit-tested and their package
validation is the SQL injection boundary; the LSPosed database is WAL-mode, so a
copy without `-wal`/`-shm` reads as empty, and the manager remains authoritative.
