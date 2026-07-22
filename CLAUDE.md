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

## Operational notes

- `shops_config.json` / `ebay_wp_config.json` and secret files are JSON configs and should be treated defensively.
- Active context is persisted in:
  - `active_shop.txt` for Etsy
  - `ebay_wp_dashboard/active_site.txt` for eBay/WordPress
- Before changing live behavior, prefer queue-key scoping by active shop/site + folder/row to avoid cross-context clashes.
