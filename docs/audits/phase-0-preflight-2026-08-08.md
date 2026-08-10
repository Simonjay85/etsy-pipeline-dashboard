# Phase 0 preflight — 2026-08-08

## Scope and authority

- Execution plan: `/Users/aaronnguyen/.codex/visualizations/2026/08/08/019fdf3d-9095-7e50-96e9-ab10f2ec97fa/etsy-project-audit/LUNA-XHIGH-EXECUTION-PLAN.md`.
- Audit source: `/Users/aaronnguyen/.codex/visualizations/2026/08/08/019fdf3d-9095-7e50-96e9-ab10f2ec97fa/etsy-project-audit/PROJECT-AUDIT-2026-08-08.md`.
- Canonical checkout: `/Users/aaronnguyen/Developer/Etsy`.
- Obsolete mirror preserved and not modified: `/Users/aaronnguyen/Documents/Claude/Projects/Etsy`.
- No Etsy listing mutation, active-shop switch, real Drive upload, credential rotation, Git history rewrite, commit, push, checkout deletion, or cleanup was performed during preflight.

## Runtime owner evidence

Captured before implementation:

- Port `8090` listener PID: `17396`.
- Command: `Python /Users/aaronnguyen/Developer/Etsy/dashboard_app.py`.
- Process cwd: `/Users/aaronnguyen/Developer/Etsy`.
- Listener before the safety patch: `TCP *:8090 (LISTEN)`; this is an in-scope Phase A defect.
- `GET http://127.0.0.1:8090/`: HTTP success and title `<title>Etsy Pipeline Dashboard</title>`.
- `GET /api/services`: HTTP 200, `running: []`, `running_tasks: []` at capture time.
- Active shop file: `templystudios`.
- Existing health endpoint candidates `/api/health`, `/api/runtime-health`, and `/api/backup-health` returned 404; `/api/services` is the current safe service read-back route.
- Source hashes at capture: `dashboard_app.py` `23f724542eaf559bfa52b67793359a2096f3ed6c7b3a256c33f5cc95400a96f7`; `dashboard_static/app.js` `b42cbb44e599d1c251879c332192776c27002845147e7d4d4162f1b8327840dd`; `dashboard_static/index.html` `19e79b08b0dd9962fe0a82723264865efa58a961f05a7de42406998609f617bf`; `dashboard_static/style.css` `8b3b4c37558d3c822363294b5d5b645993ae7b37103abd7e56fd06531791d2c2`.

The owner proof satisfies the canonical-source gate. The dashboard was not restarted because a separate Python automation process (`.codex-tmp/activate_single_sync_fix.py`, PID `30056`) and two Etsy Chrome CDP contexts were still present. No restart is safe until those contexts/jobs are independently confirmed idle or finished.

## Scheduler and backup baseline

- `com.user.etsy-backup.daily.plist` and `com.user.etsy-backup.weekly.plist` pass `plutil -lint` and point to the canonical `backup_etsy_to_drive.py`.
- The two LaunchAgent files are absent from `~/Library/LaunchAgents` and the labels are absent from `launchctl`.
- The existing backup script already contains SHA-256 manifest creation, zero-byte/dataless checks, `Etsy Automated Backups` scoping, dry-run support, and retention arguments. Dedicated regression tests are not yet present.
- No real Drive upload was attempted. A later real upload/read-back remains approval-gated.

## Dirty-work inventory and preservation decision

At preflight the canonical checkout was on branch `main`, HEAD `354e080`, with:

- 25 modified tracked paths;
- 49 untracked paths;
- 43 ignored paths reported by `git status --ignored`.

Tracked dirty work includes `dashboard_app.py`, `dashboard_static/app.js`, `dashboard_static/index.html`, `dashboard_static/style.css`, Etsy/browser automation modules, backup plists, catalog/source-map files, tests, and a screenshot. Untracked work includes cloud-asset modules/config examples, social/Medium modules, gallery-contract examples and tests, source-review renders, scripts, and a cloud-maintenance plist. These paths remain user-owned dirty work and are not reverted, staged, deleted, or overwritten wholesale.

The interrupted Phase A candidates named by the plan are:

- `dashboard_app.py`: large dirty diff (`+1898/-327`) containing reusable identity-aware Etsy sync, cloud/social/Medium additions, logging, and other prior workflow work, but no centralized Host/Origin/token boundary and no runtime-health endpoint. It is classified **preserve; selectively review and layer**. No broad adoption or reversal is authorized from the diff alone.
- `test_dashboard_etsy_single_sync.py`: large dirty diff (`+515/-4`) containing useful single-sync retry, busy-guard, stale-mapping, and status-write tests. It is classified **preserve; selectively reuse**. It is not evidence that Phase A security/runtime gates are implemented.
- Other modified/untracked paths: **pre-existing or ownership-unknown; preserve** unless a later narrow patch explicitly owns the path and its diff is reviewed.

## Baseline verification

- Default `./.venv/bin/python -m pytest`: collection failure in `node_modules/pnpm/.../test_gyp.py` due to duplicate module names.
- Root `test_*.py` discovery: collection failure from import-time browser scripts `test_tag_pills.py` and `test_tags.py` attempting CDP `localhost:9222`.
- Explicit Python suite excluding those four manual scripts: **318 passed, 5 failed, 39 subtests passed**. The five failures are the known gallery-contract fixture/strict-guard drift; the strict rights/provenance guard was not weakened.
- Manual root JavaScript scripts: all observed scripts passed except `test_dashboard_etsy_manager_link_ui.js`, whose expected value is stale (`fallback`) versus current behavior (`unavailable`). The loop continued after that failure, so the shell aggregate exit code is not a reliable JS gate; the individual failure is recorded.
- Disk free at capture: approximately `10 GiB` (98% used). Large image generation or broad rendering must stay narrow.

## Phase A intended file allowlist

The planned implementation may touch these paths only through reviewed narrow patches, plus checkpoint evidence under `docs/audits/`:

1. Backup: `backup_etsy_to_drive.py`, `com.user.etsy-backup.daily.plist`, `com.user.etsy-backup.weekly.plist`, and new backup-focused tests.
2. Security/runtime: `dashboard_app.py`, `dashboard_static/app.js`, `dashboard_static/index.html`, `dashboard_static/style.css`, `pytest.ini`, `conftest.py`, `.gitignore`, and a sanitized `shops_config.example.json`.
3. If a new small module is needed to avoid widening the dashboard diff: only a clearly named runtime/security helper under the repository root or `dashboard/`, with a matching focused test.
4. Documentation: this report and later checkpoint reports under `docs/audits/`.

Existing dirty changes in these paths must be reviewed in place; no checkout or mirror synchronization is allowed.

## Approval gates and next step

Pending gates that do not block local implementation: real Google Drive upload/read-back, live Etsy mutation, untracking/history cleanup, secret rotation, checkout deletion/archive, commit, push, and PR.

Phase 0 exit evidence is complete for canonical ownership, dirty-work inventory, baseline tests, scheduler status, active context, and the Phase A allowlist. Next is Checkpoint 1 / A1: add backup dry-run verification/tests, validate the existing plists, install/reload only the two local LaunchAgents after validation, and run daily/weekly dry runs without uploading to Drive.

## Rollback

No source/runtime mutation from implementation has occurred yet. For later patches, rollback is by reverting only the reviewed changed-file hunks or moving the generated checkpoint/report aside; do not use `git reset`, `git clean`, or broad checkout restoration. LaunchAgents can be unloaded individually using their exact labels if a later local scheduler change fails validation.
