# Root & Instrumentation — work ledger

Living status record for the Root & Instrumentation subsystem. Updated as work
proceeds so a later session can resume without repeating completed work.

Legend: **done** = implemented *and* verified · **active** = in progress ·
**blocked** = external limitation with a reason · **queued** = not started.

---

## Delivery decisions

| Decision | Rationale |
| --- | --- |
| Keep the project **MIT** | Relicensing to GPLv3 is a one-way door and would not remove the hard part of rooting (per-image ramdisk patching + Magisk "Additional Setup"). |
| Do **not** vendor Magisk / LSPosed / rootAVD | All three are GPL-3.0. They stay operator-supplied or acquired from official sources at runtime, with pinned SHA-256 and explicit consent. |
| Acquire, don't bundle | An in-app acquisition flow gives the convenience of bundling without becoming a distributor of a root toolkit. |

## Task ledger

| # | Task | State | Evidence / notes |
| --- | --- | --- | --- |
| 1 | Schema v3, repository, profiles, deployment engine | done | 93 instrumentation tests; migration applies; contract updated |
| 2 | Capability probing (adb root vs Magisk; installed/enabled/active) | done | live API 25 scan: `root=adb_root`, `magisk=false`, blockers; API 34: Magisk root `u:r:magisk:s0`, Zygisk `zygiskd64`, LSPosed `pidof lspd` |
| 3 | Central module library (APK/ZIP import, SHA-256, provenance) | done | live import of a crafted `zygisk_zip`; correct classification + hash |
| 4 | REST API (17 ops) + CLI group | done | OpenAPI contract test passes; CLI help + live scan/import |
| 5 | GUI page (6 tabs, real endpoints) | done | `typecheck`, `lint`, `build` exit 0; 82 Vitest tests (14 for the page) |
| 6 | Docs (feature + compatibility matrix + README/DECISIONS) | done | 253 relative links resolve; `docs/api.md` (71 paths/89 operations, instrumentation group) and `docs/cli.md` section now current |
| 7 | **Official-source acquisition flow** (pinned versions, SHA-256, consent) | done | live: LSPosed 1.9.2 downloaded (2,462,055 B), hash matched, auto-imported `mod_0ebc6bcb465d1c4b`; CLI refuses without `--confirm` (409) |
| 8 | **Secret leak**: installer child env copied `os.environ` | done | now `child_environment(extra=…)`; token/vault passphrase no longer inherited |
| 9 | **`adb_root` semantics** + single-pass module enumeration | done | bare `adb shell id`; inventory enumerated once and shared with the LSPosed check |
| 10 | **Xposed module inventory + scope** (via LSPosed DB) | done | `xposed_modules()` reads `modules`/`scope` tables; `enable_xposed_module` + `set_xposed_scope` steps; package-name validation is the injection boundary |
| 11 | Live LSPosed verification on API 34 | done | full profile succeeded end-to-end; see below | activated end-to-end: `zygisk_lsposed` module, `pidof lspd`, manager installed separately, Overview reports Activated; Zygisk proven by `zygiskd64`; API 25 still refuses LSPosed (< API 27) |
| 12 | `docs/api.md` (says 57 paths) and `docs/cli.md` (no instrumentation) | done | api.md now 71 paths/89 operations with a Root and instrumentation group; cli.md has the full command section incl. toolchain and the setup/reboot note |
| 13 | Revert `ui/public/mockServiceWorker.js` | done | `git checkout` |
| 14 | Contract error declarations | done | 404/409 responses declared on the instrumentation operations (89 total ops); response bodies remain free-form |
| 15 | GUI acquisition UI + Xposed step actions/inventory | done | Toolchain panel in two tabs; profile editor offers `complete_magisk_setup`, `enable_xposed_module`, `set_xposed_scope`; module table renders `scope` |
| 16 | Rebuild the shipped GUI bundle into `src/avdfleet/api/static/` | done | `index-C3UtynXb.js` (581.94 kB) + `index-CKQTONAt.css`; old `index-Cj2XS7za.js` removed; `index.html` references match |
| 17 | **Unattended Magisk setup** (`complete_magisk_setup`) | done | installs the acquired APK, drives the dialog via `uiautomator dump` + `input tap`, waits for the self-reboot, verifies `/data/adb/magisk`; added to the three Magisk profiles |
| 21 | Lifecycle automation (instance -> profile on start) | done | settings-backed mapping, queued on `instances start` / `wait-boot`, persists across restarts; live smoke test passed |
| 19 | Root/writable-system/manager/reboot/remove steps | done | `enable_adb_root`, `enable_writable_system`, `install_lsposed_manager`, `reboot`, `remove_lsposed`, `remove_magisk` (refuses, with guidance) |
| 20 | WebSocket events for deployments | done | `EventType.INSTRUMENTATION_RUN` with `started`/`finished`; publish failures never break a run |
| 18 | Built-in profiles refresh in place | done | an upgrade that adds a step now updates existing installs (built-ins are read-only, so this cannot clobber user edits) |

## Verified live in this pass

- Acquisition: LSPosed 1.9.2 downloaded over HTTPS, 2,462,055 bytes, SHA-256 matched the
  pinned value, stored under the data directory and auto-imported into the library as
  `mod_0ebc6bcb465d1c4b` (`lsposed_zip`, source `official-release`).
- Consent gate: `toolchain acquire lsposed` without `--confirm` returns the 409 message.
- API 25 capability scan: `root=adb_root`, `magisk=false`, explicit blockers.
- **Full LSPosed profile ran end-to-end on a real API-34 AVD**: `complete_magisk_setup`
  (populated `/data/adb/magisk`), `enable_zygisk`, `reboot` (waited for boot),
  `install_lsposed` (v1.9.2), `reboot`, `install_lsposed_manager` → run state
  **succeeded**; the follow-up scan reported root `magisk`, LSPosed `1.9.2 (7024)`
  installed and **active**. A second targeted run produced the same `succeeded`.
- Two real defects were found by that run and fixed: the Magisk gate after a reboot
  (shell `su` is rejected until the app grants it, so the code now falls back to
  adbd root) and a stale adbd-root cache surviving a guest reboot.
- On-device LSPosed query path exercised: `set_xposed_scope` against the real
  `modules_config.db` correctly reported `unsupported` for an unregistered package.
- Magisk root discriminator verified on both API levels: `su -c id` reports
  `context=u:r:magisk:s0` for Magisk versus `u:r:su:s0` for a plain `adb root`.
- Zygisk defaults off; activation is a running `zygiskd64`, not `/sbin/.magisk`
  (absent on API 34). LSPosed activation is `pidof lspd`; `pm list packages` is a
  false negative because lspd does not install its manager.
- API 34 full stack: `complete_magisk_setup` completed with four unprompted taps
  and populated `/data/adb/magisk`; `magisk --install-module` fails with
  `Incomplete Magisk install` before setup and exits 0 after; LSPosed 1.9.2 module
  active after reboot (`pidof lspd`, `ZygiskCompanion`/`binder received` logcat);
  `manager.apk` installed separately with `pm install -r`, Overview reports
  Activated.

## Known limitations (deliberate, documented)

- LSPosed requires API ≥ 27 — it cannot run on the API 25 image.
- Xposed APK install ≠ activation. Enabling/scope can be done in the LSPosed
  manager (authoritative) or with the best-effort `enable_xposed_module` /
  `set_xposed_scope` DB-write steps; `lspd` reloads either only after a reboot.
- Play Store images (`ro.build.type=user`) are never rootable and are refused.
- Magisk's "Additional Setup" must complete before any module script runs; until
  then magiskd logs `/data/adb is not present, abort` and `--install-module` says
  `Incomplete Magisk install`. `/data/adb/magisk` existing means setup completed,
  not that Magisk is installed.
- The LSPosed manager is not installed by `lspd`; install it from the module's
  `manager.apk`.

## Final verification (this session)

| Check | Result |
| --- | --- |
| `pytest tests/unit tests/api tests/cli tests/contract` | **1196 passed, 3 skipped** (201 s) |
| `ruff check src tests bootstrap` | clean |
| `mypy` | clean (195 files) |
| `tsc --noEmit` (ui) | clean |
| `vitest run` (ui) | **82 passed** (12 files) |
| `scripts/check_docs_links.py` | 253 links in 31 files, all resolve |
| emulator e2e (`tests/e2e`) | not run in this pass; it boots real AVDs and was skipped to avoid colliding with the live verification |

## Resume instructions

1. `uv run pytest tests/unit tests/api tests/contract -q` — must stay green.
2. `uv run ruff check src tests && uv run mypy` — must stay clean.
3. `cd ui && pnpm typecheck && pnpm lint && pnpm build && pnpm test`.
4. Next unfinished task is the lowest-numbered row that is not `done`.
