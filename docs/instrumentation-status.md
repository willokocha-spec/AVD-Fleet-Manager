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
| 22 | QA review (16 defects from an independent reviewer) | done | all 16 fixed; `tests/qa` is green and stays as the regression suite |
| 23 | Contract typed end to end | done | all instrumentation responses now have named schemas; 0 free-form instrumentation paths; 72 paths / 91 operations |
| 24 | APK import on Windows (leaked mkstemp fd) | done | `_badging` closes the descriptor before unlink; verified by importing an Xposed-marked APK |
| 25 | `scripts/build_release.py` hardcoded toolchain path | done | node/pnpm discovered from PATH or AVDFLEET_NODE/AVDFLEET_PNPM |
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
| `pytest tests/unit tests/api tests/cli tests/contract tests/qa` | **1212 passed, 3 skipped** (191 s) |
| `pytest tests/integration tests/portable` | **220 passed** (21 s) |
| `ruff check src tests bootstrap` | clean |
| `mypy` | clean (195 files) |
| `tsc --noEmit` (ui) | clean |
| `vitest run` (ui) | **82 passed** (12 files) |
| `scripts/check_docs_links.py` | 255 links in 31 files, all resolve |
| emulator e2e (`tests/e2e`) | not run in this pass; it boots real AVDs and was skipped to avoid colliding with the live verification |

## Follow-up fix list (post-`b1e8ce0`)

| # | Item | State |
| --- | --- | --- |
| 1 | Detached bridge discovery (stable digest, scan fallback, orphan cleanup) | done |
| 2 | Bearer token in server logs (single-use WS ticket, logger redaction) | done |
| 3 | Emulator log permissions (`0600` in a `0700` directory) | done (POSIX; tested in CI) |
| 4 | Auto-profile on every start path + GUI control | done |
| 5 | Auto-profile once per boot, atomic (`instrumentation_auto_runs`, schema v4) | done |
| 6 | Auto-profile docs contradiction | done |
| 7 | https-to-http redirect refused before the request; no query strings in errors | done |
| 8 | Redirects to loopback/link-local/private/reserved addresses refused | done |
| 9 | Wildcard bind accepts the loopback browser origin | done |
| 10 | Guest-side bridge isolation proof | not run (needs a quiet emulator host; documented gap) |
| 11 | 17+ instances under one adb server | not run (host capacity; documented gap) |
| 12 | Lint gap (`scripts/` in ruff + CI) | done |

## Resume instructions

1. `uv run pytest tests/unit tests/api tests/contract -q` — must stay green.
2. `uv run ruff check src tests && uv run mypy` — must stay clean.
3. `cd ui && pnpm typecheck && pnpm lint && pnpm build && pnpm test`.
4. Next unfinished task is the lowest-numbered row that is not `done`.

## Portable edition check

- **Build:** `python scripts/build_release.py --no-sdist` produced only `dist/avdfleet-0.1.0-py3-none-any.whl` (919,759 B); the script has no portable-bundle step (`scripts/build_release.py:100-111`), despite README:215 and `docs/portable.md:75-88`. `--build-ui` dies at `scripts/build_release.py:24-25` (hardcoded `C:\Users\lab\...` pnpm).
- **Portable build:** `avdfleet bundle --offline -o <tmp>` produced a folder (300,636,387 B manifest total, 7,335 entries, 288.1 MiB); the project has no zip step, so no zipped portable artifact is reproducible.
- **Daemon:** bundle Python `-m avdfleet serve --portable --port 8791` with a temp data dir: `/health` OK; 5 built-in profiles; both pinned toolchain entries (magisk 26.1, lsposed 1.9.2, `present=false`).
- **Modules:** crafted `module.prop` + `zygisk/` ZIP imported as `mod_6e52157ee4e6b571` (`zygisk_zip`, SHA-256 match), stored under `<data>/instrumentation/modules/<sha>/`, listed, then DELETE 204 removed row and file.
- **aapt2 / APK (genuine Windows limitation):** `find_aapt2` resolves `<bundle>/sdk/build-tools/36.0.0/aapt2.exe` and direct badging reads `com.topjohnwu.magisk` 26.1/26100, but an Xposed-marked APK through the daemon returns **HTTP 500**: `src/avdfleet/instrumentation/library.py:231` drops the open `mkstemp()` fd, so `:245` unlink raises `PermissionError [WinError 32]`; no metadata is stored. Not a portable-path defect, but it breaks the Windows portable build.
- **Other limits:** without an SDK (docs say it is not shipped) the same APK imports as `xposed_apk` with empty package/version and no `badging` (`library.py:94-114`, `:227-228`); a marker-less ordinary APK imports as `unknown` with empty package/version before `_badging` runs (`:203-208`) — design, not aapt2.
- **Cleanup:** both daemons stopped; 8791/8792 free; temp data dirs and leaked aapt temp files deleted; 8765/emulators untouched.

## Xposed write verification

The two LSPosed database **write** steps were exercised on a throwaway API-34
`google_apis;x86_64` AVD (`xposed-probe`, 4096 MiB) with a purpose-built Xposed
APK (`com.avdfleet.probe`, built in `%TEMP%` with build-tools 36.0.0 + a debug
keystore, imported as `mod_957a949008704781`).

| Check | Result |
| --- | --- |
| `install_module` of the bound APK | **verified** — `ok=True`, `state=installed`; `lspd` registered `mid=2 / com.avdfleet.probe / enabled=0` |
| `enable_xposed_module` (`enabled=true`) | **verified** — `ok=True`, `state=enabled`; `modules.enabled` read back as `1` through `sqlite3` on the device |
| `set_xposed_scope` (`apps=[com.android.chrome]`, `replace=true`) | **verified** — `ok=True`, `state=enabled`; `scope` row `2|com.android.chrome|0` |
| `service.scan()` after the writes | **verified** — root `magisk`, LSPosed `v1.9.2 (7024)` active; `xposed_modules` lists `com.avdfleet.probe` with `enabled=true`, `scope=["com.android.chrome"]` |
| unregistered package (`com.example.missing`) | **verified** — `ok=False`, `state=unsupported` |
| undotted name (`lspd`) | **verified** — `ok=False`, validation message `invalid Android package: 'lspd'` |

Defects observed while verifying (not fixed here; `src/**` untouched by this pass):

- `service.py:490-494` (`_execute_target`): with `continue_on_error=true`, a run
  whose **every** step failed still reports `result.ok=True` and the run state
  `succeeded` (seen with `irun_79e7ec4faaa9`). `result.ok` should be
  `all(step.ok ...)` and the run should be `failed`/`partial`. Failing test:
  a two-step profile with `continue_on_error` on both and two failing steps must
  not report `succeeded`.
- `service.py:648-675` vs `:1026` (`complete_magisk_setup`): the post-setup wait
  never calls `Prober.forget_root()`, so an app-initiated reboot leaves a stale
  adbd-root cache and `ls /data/adb/magisk` then runs through a non-root shell.
  Observed: the step failed ("did not complete unattended") while
  `/data/adb/magisk` was in fact populated and `magisk -V` returned 26100; a
  fresh process reported "the Magisk environment is already complete".
- `library.py:220-230`: an APK whose only Xposed marker is the manifest
  `xposedmodule` meta-data imports as `unknown` (the module docstring says
  manifest meta-data counts, the code only scans archive entries). LSPosed does
  register such a module, so `install_module` would refuse it. Failing test:
  import a meta-data-only APK and expect `ModuleKind.XPOSED_APK`.
- `capabilities.py:432` (`probe`): Magisk is only detected when `su -c id`
  succeeds; on a device where shell `su` is denied the scan reports
  `root=adb_root`, `magisk=False` and no `xposed_modules`, even though `lspd`
  runs. Running `adb root` first makes the same call report `magisk`. Consider
  probing through `root_shell(..., magisk=True)` (which has the adbd-root
  fallback) instead of a bare `su -c id`.

## Host note (doctor adb-transport check)

`tests/unit/test_doctor.py::test_all_green_with_complete_fake_toolchain` fails when the
long-lived adb server was started without `ADB_LOCAL_TRANSPORT_MAX_PORT`; the check
inspects the running server's environment, so it is host state, not a code defect.
Restarting the server with the limit makes it pass:

```powershell
$env:ADB_LOCAL_TRANSPORT_MAX_PORT = "5681"; adb kill-server; adb start-server
```

AVD Fleet sets this for every adb it starts; only a server started by something else
(or by an older build) trips the check.
