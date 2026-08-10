# Checkpoint 2 — Phase A2 local security boundary

## Result

**A2 implementation and focused verification PASS.** The live process has not been restarted, so the deployed/read-back state is still the pre-A2 listener until a safe restart is possible.

## Changed files

- `dashboard_app.py`
  - Removed wildcard CORS configuration.
  - Added centralized Host/Origin checks and per-launch `secrets.token_urlsafe(32)` mutation-token enforcement for all POST/PATCH/DELETE requests.
  - Added loopback-default bind selection with explicit `ETSY_DASHBOARD_ALLOW_LAN` opt-in.
  - Injects the ephemeral token into the same dashboard HTML response without URL or persistent browser storage.
- `dashboard_static/app.js`
  - Added one same-origin mutation fetch wrapper that sends the token header.
- `test_dashboard_security.py`
  - Added focused Host, Origin, token, OPTIONS/CORS, mocked mutation, and secret-redaction coverage.

The existing dirty changes in these files were preserved and the A2 code was layered narrowly. No backup file or A1 test was changed.

## Verification

- Focused independent rerun: `43 passed, 7 subtests passed` in `test_dashboard_security.py`, `test_dashboard_etsy_single_sync.py`, and `test_dashboard_etsy_manager_links.py`.
- `py_compile`: PASS.
- `node --check dashboard_static/app.js`: PASS.
- `git diff --check`: PASS.
- Manual JS `test_dashboard_etsy_manager_link_ui.js`: existing expectation drift remains (`actual unavailable`, `expected fallback`); this is the known pre-existing C2 regression and is not caused by A2.

## Security evidence

- Loopback Host and same-origin read requests pass.
- Hostile Host and hostile Origin return 403.
- Missing/wrong mutation tokens return 403.
- Correct token reaches the mocked mutation handler.
- OPTIONS does not return wildcard `Access-Control-Allow-Origin`.
- The service response and captured request log messages do not contain the token.

## Runtime mutations and preserved work

- No dashboard restart, Etsy mutation, workbook write, Drive upload, scheduler install, stage, commit, push, or mirror write occurred.
- The existing bulk sync process remains active and user-owned; it was not stopped or interrupted.
- All unrelated dirty paths remain preserved.

## Rollback and risks

Rollback is limited to the reviewed A2 hunks and new security test. The current live PID still exposes the old `*:8090` behavior until a later approved safe restart after the active bulk sync is finished. LAN mode remains intentionally opt-in and must be configured with an explicit host allowlist if used.

## Next checkpoint

Proceed to A3 runtime identity/startup refusal and A4 Git hygiene locally. Do not restart until active jobs are empty; after restart, re-prove PID, cwd, loopback bind, title, active shop, runtime health, and scheduler state.
