# Checkpoint 3 — Phase A3 runtime identity and A4 Git/config hygiene

## Result

**A3/A4 local implementation PASS with live deployment read-back pending.** The live process remains the pre-patch PID `17396` on `*:8090` because the user-owned bulk sync is still active. A1 weekly backup source integrity remains separately blocked by the exact zero-byte recovery payload recorded in Checkpoint 1.

## Changed files

- `runtime_identity.py`
  - Centralized canonical-root resolution and explicit development-only noncanonical override.
  - Added safe source/frontend hashes, PID/start time, bind identity, shop display identity, scheduler state, service readiness, and runtime version payload.
  - Added fail-closed startup guard for noncanonical production paths.
  - Backup evidence is summary-only (`timestamp` + fixed `status`); raw log lines and paths are not returned.
- `dashboard_app.py`
  - Added read-only `/api/runtime-health`.
  - Reused service-status snapshot and canonical startup guard.
  - Integrated sanitized shop config validation into `load_shops()`.
- `dashboard_static/index.html`, `dashboard_static/app.js`, `dashboard_static/style.css`
  - Added compact always-visible runtime indicator with warning/offline states.
  - Added polling/rendering for canonical, source, scheduler, backup, and service warnings.
- `test_runtime_identity.py`, `test_dashboard_runtime_health.py`, `test_runtime_health_ui.js`
  - Added runtime identity, redaction, endpoint shape, and UI warning coverage.
- `.gitignore`
  - Added future runtime config/state, diagnostics/staging, screenshot/failure artifact ignores.
- `shops_config.example.json`
  - Added fake, sanitized setup schema.
- `shops_config_validation.py`, `test_shops_config_validation.py`, `test_dashboard_config_validation.py`
  - Added non-secret local config validation, safe missing/malformed errors, backward-compatible empty metadata/browser-session handling, and dashboard startup integration tests.

## Independent verification

- Combined focused Python suite: **64 passed, 7 subtests passed**, one existing Starlette/httpx deprecation warning.
- Included A2 security, A3 runtime, A4 config, single-sync, manager-link, and asset-status tests.
- `py_compile` for all changed Python modules/tests: PASS.
- `node --check dashboard_static/app.js`: PASS.
- `node test_runtime_health_ui.js`: PASS.
- `git diff --check`: PASS.
- Import smoke: `dashboard_app` imports successfully from canonical checkout; active shop remains `templystudios`; configured shops are `daisyflowdigital`, `shop3`, `templystudios`; default bind selection is `127.0.0.1`.
- Existing JS regression remains `test_dashboard_etsy_manager_link_ui.js` (`actual unavailable`, `expected fallback`); this is the documented C2 behavior drift.

## Security/readiness notes

- Runtime health does not include shop config values, token headers, cookies, browser profile paths, or raw backup log lines.
- Existing canonical config is accepted: empty `social_links`/`shop_info` metadata and missing `shop3.browser_session` remain backward-compatible.
- Existing tracked `shops_config.json`, `active_shop.txt`, screenshots, and other runtime artifacts were not untracked, deleted, or rewritten. That cleanup remains approval-gated.

## Runtime mutations and preserved dirty work

- No dashboard restart, process stop, Etsy mutation, workbook mutation, Drive upload, LaunchAgent install, stage, commit, push, or mirror write occurred.
- PID `17396` still owns port `8090` with cwd `/Users/aaronnguyen/Developer/Etsy` but still binds `*:8090`; `/api/runtime-health` on that live process still returns 404 because it predates A3.
- PID `30056` is still running the user-owned bulk sync monitor; its log showed `bulk=true` and progress around 94/226 at the last pre-checkpoint observation.

## Rollback

Rollback is by reviewed file/hunk only. Do not reset or clean the repository. If a later restart read-back fails, stop before any Etsy work, restore only the affected changed-file hunks, and re-prove the listener/cwd/title/health. No cloud or shop rollback artifact was needed in this checkpoint.

## Pending gates

1. Wait for the existing bulk sync to finish; confirm `/api/services` has no running processes/tasks and the monitor has exited.
2. Resolve or owner-approve the exact zero-byte weekly backup source; rerun daily/weekly dry-runs before scheduler certification.
3. Install/reload only the two canonical backup LaunchAgents after source-integrity dry-runs pass.
4. Restart the canonical dashboard once only after active jobs are empty, then verify new PID/cwd/loopback bind/title/active shop/runtime-health/security smoke.

## Next step

Continue to Phase B local implementation independently while treating runtime restart and scheduler certification as pending gates; do not claim Phase A deployed completion until the live read-back is captured.
