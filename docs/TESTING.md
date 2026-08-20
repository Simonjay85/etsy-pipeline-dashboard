# Testing and CI

The repository has two intentionally separate local gates:

```sh
python -m pytest -v
python scripts/flake8_baseline.py --all-tracked
npm run test:safe
```

The Python CI job runs on Ubuntu with Python 3.12. It installs
`requirements-dev.txt`, compiles every tracked Python file with `py_compile`,
runs `pytest -v`, and then runs the all-tracked Flake8 baseline gate. The
development requirements include `requirements.txt`; Flake8, the blind-except
plugin, pre-commit, and pytest are not runtime dependencies.

`scripts/flake8_baseline.py --all-tracked` checks every tracked Python path in
the working tree. It compares E722/B902 findings with `.flake8-baseline.json`
and fails on a new source fingerprint. The no-filename invocation used by the
pre-commit hook retains its existing Git-index behavior; it must not be changed
to scan arbitrary working-tree files. The baseline is reviewed data, not a
permission to add broad exceptions. The additions from commit `6604bd5` are
retained as reviewed boundary catches where they remain; do not regenerate the
whole file to make a gate green.

The retained additions are reviewed by scope, with no new broad catches added
in this hardening change:

- `dashboard_app.py`: Vertex/SEO enrichment catches convert optional external
  or asset-inspection failures into bounded user-facing results; cloud upload,
  `sync_to_shop`, poster, and asset-backup catches close transaction/queue
  state and return a failed-closed response after cleanup. The workbook status
  probe is a defensive read-only fallback.
- `etsy_auto_post.py`: locator probing, listing-type/category readback, photo
  and customer-file upload/reconciliation, and translation catches cover
  Playwright's changing DOM boundary. They either continue to the next
  verified selector or raise a contract error; they do not turn an uncertain
  upload into a success.
- `etsy_push_update.py::_open_updater_context`: the `BaseException` catch closes
  an owned Playwright context before re-raising, including cancellation; it is
  not a recovery or publish path.
- `scripts/daisy_folder_renumber.py`: workbook/cloud preflight, remote
  postflight, and apply/rollback catches convert unknown failures to
  `MigrationError`, write the migration journal, and attempt exact rollback.
  The canonical repository check at the destructive CLI `--apply` boundary is
  unchanged; tests use temporary repository roots and never invoke production
  apply.

## Safe JavaScript checks

`scripts/run_safe_js_tests.mjs` runs an explicit, fail-fast allowlist. It never
discovers files with a `test_*.js` glob. Each child is run from the repository
root, its filename is printed, and the final summary is nonzero if a child
fails. The 26 allowlisted scripts are:

```text
test_batch_post_ui.js
test_batch_select_all_ui.js
test_batch_selection.js
test_catalog_scaling_ui.js
test_catalog_sort.js
test_change_status_ui.js
test_cloud_assets_ui.js
test_cross_shop_sync_ui.js
test_dashboard_compact_density_ui.js
test_dashboard_etsy_manager_link_ui.js
test_dashboard_stats_ui.js
test_edit_save.js
test_etsy_bulk_sync_ui.js
test_etsy_link_unverified_ui.js
test_etsy_listing_links_ui.js
test_etsy_single_sync_ui.js
test_filter_status.js
test_image_factory_scan_ui.js
test_image_lightbox.js
test_local_source_aggregate_ui.js
test_modal_accessibility_ui.js
test_mutation_token_refresh.js
test_operation_queue_dashboard.js
test_regen_seo_ui.js
test_runtime_health_ui.js
test_social_ui_badges.js
```

These are static/VM or mocked-fetch checks. They do not create listings, edit,
publish, delete, sync, launch Chrome, use Playwright, call Etsy, or require
credentials or external services. The remaining root JavaScript scripts are
not part of this CI gate. In particular, browser/live/manual scripts and any
script needing a real dashboard session remain manual and must not be added by
replacing the allowlist with a glob. The known `test_dashboard_etsy_manager_link_ui.js`
script is included only as its local assertion-only check; no browser session is
needed.

## Cache-version assertions

The four cache assertions repaired for the current dashboard are deliberately
separate from behavior assertions. They read the versions served by
`dashboard_static/index.html`:

```text
style.css?v=20260814-status-summary-click-01
app.js?v=20260819-cloud-offload-preflight-01
```

If a later frontend change updates a cache key, update only the affected
assertion after checking the current `index.html`; do not remove the behavior
checks around it.

## Hygiene and retained artifacts

`.gitignore` covers local sessions/auth/cookies, logs and PIDs, temporary DBs,
Node modules, test output/cache, and cloud-operation scratch directories. Ignore
rules are not cleanup commands. Existing tracked or explicitly retained
artifacts—including `active_shop.txt`, `shops_config.json`,
`save_draft_failure.png`, the tracked `tmp_test.db`, and the user-provided
`.codex-tmp/` workbook—must not be deleted or rewritten merely because they
resemble runtime hygiene files.

The CI workflow is local/test-only: it does not load Etsy credentials, connect
to Chrome or Playwright, run rclone, mutate cloud storage, or perform Daisy
apply operations.

## Optional external contract checks

`test_etsy_listing_gallery_channel_guard.py` is marked
`external_contract` because it exercises the optional local Codex
`etsy-10-image-maker` skill rather than a repository-owned module. When that
skill is installed under `~/.codex/skills/etsy-10-image-maker/scripts`, the
test runs its full guard and CLI contract checks. On machines without the
skill, including GitHub-hosted Ubuntu runners, pytest skips these tests before
execution; the repository-owned Python tests and CI checks remain required.
