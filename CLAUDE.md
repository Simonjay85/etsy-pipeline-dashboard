# CLAUDE.md

## Imported Claude Cowork project instructions

- At the start of each conversation, read this file before performing operations.
- This dashboard project manages Etsy + eBay/WordPress tooling and product pipelines.
- Common tasks are already defined by the existing API routes in:
  - `dashboard_app.py` (Etsy pipeline, default port 8090)
  - `ebay_wp_dashboard/dashboard_app.py` (eBay + WordPress, default port 8091)
- Use the existing queue/process model for task safety:
  - Etsy: `push-to-etsy`, `sync-from-etsy`, `post`, `run-all-pending`
  - Stop-all should terminate tracked subprocesses and clear pending queues.

## Canonical workspace (only root)

This repo is the **only** canonical Etsy project root:

| Role | Canonical path |
|------|----------------|
| Project root | `/Users/aaronnguyen/Developer/Etsy` |
| Factory source | `/Users/aaronnguyen/Developer/Etsy/master_products` |
| Shop data | `/Users/aaronnguyen/Developer/Etsy/shops` |
| Backups | `/Users/aaronnguyen/Developer/Etsy/output/backup` |

- Import Factory reads `master_products`. Asset-intake / image-factory output must land under `master_products` in this repo.
- Resolve paths from the repo root (`Path(__file__).resolve().parent` for root-level scripts). Do not hardcode the obsolete tree.
- `/Users/aaronnguyen/Documents/Claude/Projects/Etsy` is obsolete — never use it as source or destination.

**Before claiming completion:** confirm generated `master_products/product-NN` folders exist in this repo and that the dashboard Import Factory scan can see them.

## Operational notes

- `shops_config.json` / `ebay_wp_config.json` and secret files are JSON configs and should be treated defensively.
- Active context is persisted in:
  - `active_shop.txt` for Etsy
  - `ebay_wp_dashboard/active_site.txt` for eBay/WordPress
- Before changing live behavior, prefer queue-key scoping by active shop/site + folder/row to avoid cross-context clashes.

## Testing & validation

- Run tests: `.venv/bin/python -m pytest` (207 tests collected, all passing as of 2026-08-04)
- Config: `pytest.ini` at project root; `conftest.py` excludes non-test scripts.

### Test-to-module map (all tracked pytest modules)

| Test file | Covers |
|-----------|--------|
| `test_dashboard_image_previews.py` | `dashboard_app._renderable_image_url`, image preview/hydration logic |
| `test_dashboard_etsy_single_sync.py` | `dashboard_app` single-sync Etsy scrape session routing + identity gates |
| `test_dashboard_etsy_listing_links.py` | `dashboard_app` status-aware Etsy listing-link enrichment (snapshot fail-closed) |
| `test_dashboard_etsy_manager_links.py` | `dashboard_app.enrich_products_with_etsy_manager` read-only link decoration |
| `test_dashboard_etsy_public_images.py` | `dashboard_app` public Etsy listing image fallback |
| `test_dashboard_latest_snapshot_normalization.py` | `dashboard_app` normalized Etsy snapshot loading |
| `test_etsy_asset_sync_status.py` | `dashboard_app._size_text_to_bytes`, `_etsy_size_matches`, `_extract_asset_sync_status` |
| `test_etsy_draft_delete.py` | `dashboard_app` draft-delete route + `etsy_clean_duplicates` |
| `test_etsy_link_local.py` | `dashboard_app._status_for_linked_etsy_listing`, local Etsy link status logic |
| `test_etsy_local_delete.py` | `dashboard_app` local-delete route + `etsy_catalog` cleanup |
| `test_etsy_selected_post.py` | `dashboard_app` selected-products post flow (translation mocked) |
| `test_image_factory_import.py` | `dashboard_app` image-factory scan/import endpoints |
| `test_etsy_auto_post.py` | `etsy_auto_post` browser session resolution, draft filter |
| `test_etsy_browser_session.py` | `etsy_browser_session` / `etsy_auto_post` per-shop profile resolution |
| `test_etsy_shop_sync_session.py` | `etsy_shop_sync.crawl_etsy_shop` CDP session reuse, temp-page ownership |
| `test_etsy_catalog_ordering.py` | `etsy_catalog.build_unified_catalog`, `load_local_catalog` |
| `test_merge_safe_duplicates.py` | `etsy_catalog.merge_safe_duplicates` digital-hash guard |
| `test_medium_content.py` | `medium_content` article generation + integration seams in `dashboard_app`, `generate_social_posts`, `social_auto_post` |
| `test_social_auto_post.py` | `social_auto_post` Pinterest publish-success detection, title/description normalization |
| `test_social_browser_session.py` | `social_browser_session.resolve_social_session` per-shop defaults + `dashboard_app` capture |
| `test_social_post_store.py` | `social_post_store` record/status persistence, multi-process safety |

### Excluded scripts (not real tests)

`test_seo_fail.py`, `test_tag_pills.py`, `test_tags.py`, `test_translate.py` are manual/interactive scripts excluded via `conftest.py` (`collect_ignore`).

### JavaScript test files (manual scripts — no automated runner)

The `test_*.js` files at the repo root are **manual, standalone scripts** for the dashboard frontend layer (`dashboard_static/`), not automated test suites. Each is a plain Node script using `node:assert` (some drive a live browser via Playwright-style interaction) and must be run individually, e.g. `node test_image_lightbox.js`. There is **no automated JS test runner** configured — no `package.json`, no Jest/Mocha/Playwright-test setup — so these scripts are never collected or executed by any CI/test command in this repo.

| Script | Checks |
|--------|--------|
| `test_batch_post_ui.js` | Batch post/SEO button markup + ordering in `dashboard_static/index.html` / `app.js` |
| `test_batch_selection.js` | Batch selection logic in `dashboard_static/app.js` |
| `test_catalog_sort.js` | Catalog sort behavior in `dashboard_static/app.js` |
| `test_edit_save.js` | Edit/save flow in `dashboard_static/app.js` |
| `test_etsy_link_unverified_ui.js` | Unverified Etsy link UI states |
| `test_image_lightbox.js` | Image lightbox behavior in `dashboard_static/app.js` (via `node:vm`) |
