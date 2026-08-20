#!/usr/bin/env node

import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

// Keep this list explicit. These scripts are static/local-only checks; do not
// replace it with a test_*.js glob because the repository also contains manual
// and browser/live-boundary scripts.
const SAFE_SCRIPTS = Object.freeze([
  'test_batch_post_ui.js',
  'test_batch_select_all_ui.js',
  'test_batch_selection.js',
  'test_catalog_scaling_ui.js',
  'test_catalog_sort.js',
  'test_change_status_ui.js',
  'test_cloud_assets_ui.js',
  'test_cross_shop_sync_ui.js',
  'test_dashboard_compact_density_ui.js',
  'test_dashboard_etsy_manager_link_ui.js',
  'test_dashboard_stats_ui.js',
  'test_edit_save.js',
  'test_etsy_bulk_sync_ui.js',
  'test_etsy_link_unverified_ui.js',
  'test_etsy_listing_links_ui.js',
  'test_etsy_single_sync_ui.js',
  'test_filter_status.js',
  'test_image_factory_scan_ui.js',
  'test_image_lightbox.js',
  'test_local_source_aggregate_ui.js',
  'test_modal_accessibility_ui.js',
  'test_mutation_token_refresh.js',
  'test_operation_queue_dashboard.js',
  'test_regen_seo_ui.js',
  'test_runtime_health_ui.js',
  'test_social_ui_badges.js',
]);

const failures = [];
let passed = 0;

for (const [index, filename] of SAFE_SCRIPTS.entries()) {
  console.log(`\n[${index + 1}/${SAFE_SCRIPTS.length}] ${filename}`);
  const scriptPath = path.join(ROOT, filename);
  if (!existsSync(scriptPath)) {
    failures.push({ filename, reason: 'file not found' });
    console.error(`FAIL ${filename}: file not found`);
    break;
  }

  const result = spawnSync(process.execPath, [filename], {
    cwd: ROOT,
    stdio: 'inherit',
  });
  if (result.error) {
    failures.push({ filename, reason: result.error.message });
    console.error(`FAIL ${filename}: ${result.error.message}`);
    break;
  }
  if (result.status !== 0) {
    const reason = result.signal ? `terminated by ${result.signal}` : `exit ${result.status}`;
    failures.push({ filename, reason });
    console.error(`FAIL ${filename}: ${reason}`);
    break;
  }
  passed += 1;
}

console.log('\nSafe JavaScript test summary');
console.log(`  allowlisted: ${SAFE_SCRIPTS.length}`);
console.log(`  passed:      ${passed}`);
console.log(`  failed:      ${failures.length}`);
console.log(`  skipped:     ${SAFE_SCRIPTS.length - passed - failures.length}`);
if (failures.length > 0) {
  for (const failure of failures) {
    console.log(`  - ${failure.filename}: ${failure.reason}`);
  }
  process.exitCode = 1;
} else {
  console.log('  result:      PASS');
}
