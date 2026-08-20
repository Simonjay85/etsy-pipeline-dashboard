const fs = require('node:fs');
const assert = require('node:assert/strict');
const vm = require('node:vm');

const indexHtml = fs.readFileSync('dashboard_static/index.html', 'utf8');
const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');

assert.match(indexHtml, /id="sync-copy-files"[^>]*checked/);
assert.match(indexHtml, /Bỏ tick = SEO-only: chỉ copy title, tags, description/);
const openModalStart = appJs.indexOf('function openSyncModal()');
const openModalEnd = appJs.indexOf('\nasync function doSync()', openModalStart);
assert.ok(openModalStart >= 0 && openModalEnd > openModalStart, 'Unable to isolate sync modal function');
assert.match(appJs.slice(openModalStart, openModalEnd), /sync-copy-files.*checked = true/);

// Extract and execute the production functions themselves. This keeps the UI
// test small without evaluating the whole browser bundle and its boot-time I/O.
function extractFunction(source, signature) {
  const start = source.indexOf(signature);
  assert.ok(start >= 0, `Unable to find ${signature}`);
  const open = source.indexOf('{', start);
  assert.ok(open >= 0, `Unable to find body for ${signature}`);
  let depth = 0;
  let quote = null;
  let escaped = false;
  let lineComment = false;
  let blockComment = false;
  for (let index = open; index < source.length; index += 1) {
    const character = source[index];
    const next = source[index + 1];
    if (lineComment) {
      if (character === '\n') lineComment = false;
      continue;
    }
    if (blockComment) {
      if (character === '*' && next === '/') {
        blockComment = false;
        index += 1;
      }
      continue;
    }
    if (quote) {
      if (escaped) escaped = false;
      else if (character === '\\') escaped = true;
      else if (character === quote) quote = null;
      continue;
    }
    if (character === '/' && next === '/') {
      lineComment = true;
      index += 1;
      continue;
    }
    if (character === '/' && next === '*') {
      blockComment = true;
      index += 1;
      continue;
    }
    if (character === '"' || character === "'" || character === '`') {
      quote = character;
      continue;
    }
    if (character === '{') depth += 1;
    if (character === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`Unclosed function ${signature}`);
}

const productionFunctions = [
  extractFunction(appJs, 'function syncModeLabel'),
  extractFunction(appJs, 'function syncAssetCountsLabel'),
  extractFunction(appJs, 'function syncSkippedCount'),
  extractFunction(appJs, 'async function doSync'),
  extractFunction(appJs, 'async function submitSyncConflict'),
];

function makeHarness(responses) {
  const elements = {
    'sync-target-shop': {value: 'target-shop'},
    'sync-copy-files': {checked: true},
    'sync-btn': {disabled: false, innerHTML: ''},
    'sync-result': {style: {}, innerHTML: '', textContent: ''},
  };
  const toasts = [];
  const context = {
    console,
    Promise,
    setTimeout,
    clearTimeout,
    document: {getElementById: id => elements[id]},
    currentShopsData: {'target-shop': {name: 'Target Shop'}},
    selectedBatchCheckboxes: () => [],
    updateBatchUI: () => {},
    toast: (kind, message) => toasts.push({kind, message}),
    fetch: async () => ({json: async () => responses.shift()}),
  };
  vm.createContext(context);
  vm.runInContext(`let _syncRows = [4]; let _syncConflictPayload = null;\n${productionFunctions.join('\n')}`, context);
  return {context, elements, toasts};
}

async function runBehavioralChecks() {
  const ordinary = makeHarness([
    {ok: true, target: 'target-shop', synced: 1, skipped: 0, mode: 'seo+assets', asset_counts: {images: 10, files: 1}},
  ]);
  await ordinary.context.doSync();
  assert.match(ordinary.elements['sync-result'].innerHTML, /SEO \+ ảnh\/file/);
  assert.match(ordinary.elements['sync-result'].innerHTML, /10 ảnh, 1 file/);
  assert.equal(ordinary.toasts[0].kind, 'success');

  const conflictMerge = makeHarness([
    {ok: true, has_conflicts: true, conflicts: [{src_folder: 'product-01', dst_folder: 'product-01', dst_row: 4}]},
    {ok: true, target: 'target-shop', synced: 1, skipped: 0, mode: 'seo+assets', asset_counts: {images: 10, files: 1}},
  ]);
  await conflictMerge.context.doSync();
  await conflictMerge.context.submitSyncConflict('merge');
  assert.match(conflictMerge.elements['sync-result'].innerHTML, /ghi đè/);
  assert.match(conflictMerge.elements['sync-result'].innerHTML, /SEO \+ ảnh\/file/);
  assert.match(conflictMerge.elements['sync-result'].innerHTML, /10 ảnh, 1 file/);
  assert.equal(conflictMerge.toasts.at(-1).kind, 'success');

  const ordinaryZero = makeHarness([
    {ok: true, target: 'target-shop', synced: 0, skipped: 1, mode: 'seo-only', asset_counts: {images: 0, files: 0}},
  ]);
  ordinaryZero.elements['sync-copy-files'].checked = false;
  await ordinaryZero.context.doSync();
  assert.match(ordinaryZero.elements['sync-result'].innerHTML, /Không có sản phẩm nào được sync/);
  assert.match(ordinaryZero.elements['sync-result'].innerHTML, /SEO-only/);
  assert.match(ordinaryZero.elements['sync-result'].innerHTML, /0 ảnh, 0 file/);
  assert.equal(ordinaryZero.toasts[0].kind, 'warning');

  const conflictSkipZero = makeHarness([
    {ok: true, has_conflicts: true, conflicts: [{src_folder: 'product-01', dst_folder: 'product-01', dst_row: 4}]},
    {ok: true, target: 'target-shop', synced: 0, skipped: 1, mode: 'seo-only', asset_counts: {images: 0, files: 0}},
  ]);
  conflictSkipZero.elements['sync-copy-files'].checked = false;
  await conflictSkipZero.context.doSync();
  await conflictSkipZero.context.submitSyncConflict('skip');
  assert.match(conflictSkipZero.elements['sync-result'].innerHTML, /Không có sản phẩm nào được sync/);
  assert.match(conflictSkipZero.elements['sync-result'].innerHTML, /SEO-only/);
  assert.match(conflictSkipZero.elements['sync-result'].innerHTML, /0 ảnh, 0 file/);
  assert.equal(conflictSkipZero.toasts.at(-1).kind, 'warning');
}

runBehavioralChecks().then(
  () => console.log('cross-shop sync UI tests passed'),
  error => {
    console.error(error);
    process.exitCode = 1;
  },
);
