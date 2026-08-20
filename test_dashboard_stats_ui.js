'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');
const indexHtml = fs.readFileSync('dashboard_static/index.html', 'utf8');

function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`Cannot find ${name} in dashboard_static/app.js`);
  const bodyStart = source.indexOf('{', start);
  let depth = 0;
  for (let index = bodyStart; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    if (source[index] === '}') depth -= 1;
    if (depth === 0) return source.slice(start, index + 1);
  }
  throw new Error(`Cannot isolate ${name} from dashboard_static/app.js`);
}

const functionNames = [
  'classifyProductStatus',
  'summarizeProductStatuses',
  'statsTotalLabel',
  'statusFilterMatches',
  'updateStats',
];
const elements = Object.fromEntries([
  'stat-total',
  'stat-posted',
  'stat-pending',
  'stat-error',
  'stat-other',
  'stat-total-label',
].map(id => [id, { textContent: '' }]));
const sandbox = {
  module: { exports: {} },
  exports: {},
  __testSource: 'local',
  document: { getElementById: id => elements[id] },
};
vm.runInNewContext(`
  let currentProductSource = globalThis.__testSource;
  ${functionNames.map(name => extractFunction(appJs, name)).join('\n')}
  module.exports = {
    classifyProductStatus,
    summarizeProductStatuses,
    statsTotalLabel,
    statusFilterMatches,
    updateStats,
    setSource: source => { currentProductSource = source; },
  };
`, sandbox);

const {
  classifyProductStatus,
  summarizeProductStatuses,
  statsTotalLabel,
  statusFilterMatches,
  updateStats,
  setSource,
} = sandbox.module.exports;

const products = [
  ...Array.from({ length: 179 }, () => ({ status: '✅ Đã đăng' })),
  ...Array.from({ length: 8 }, () => ({ status: '⏳ Chờ đăng' })),
  { status: '❌ Lỗi đăng' },
  { status: 'LỖI provider' },
  { status: '⚠ Sync lỗi' },
  { status: '⚠ sync Lỗi' },
  { status: 'English ERROR' },
  { status: '🆕 Mới import · ⚠ Sync lỗi' },
  { status: '⚠ Thiếu SEO' },
  { status: '⚠ Thiếu SEO' },
  { status: '⚠ Thiếu SEO' },
  { status: 'Chờ bổ sung' },
  { status: '' },
];

const summary = summarizeProductStatuses(products);
assert.deepEqual(
  { ...summary },
  { total: 198, posted: 179, pending: 8, error: 6, other: 5 },
);
assert.equal(summary.posted + summary.pending + summary.error + summary.other, summary.total);
assert.equal(classifyProductStatus('Đã đăng · sync lỗi'), 'error');
assert.equal(statusFilterMatches({ status: '⚠ SYNC LỖI' }, 'error'), true);
assert.equal(statusFilterMatches({ status: 'English Error' }, 'error'), true);
assert.equal(statusFilterMatches({ status: '✅ Đã đăng · ⚠ SYNC LỖI' }, 'posted'), false);
assert.equal(statusFilterMatches({ status: '⏳ Chờ đăng · ⚠ sync lỗi' }, 'pending'), false);

assert.equal(statsTotalLabel('local'), 'Folder local hiển thị');
assert.equal(statsTotalLabel('aggregate'), 'Catalog hiển thị');
assert.equal(statsTotalLabel('shop'), 'Listing Etsy hiển thị');

setSource('local');
updateStats(products.slice(0, -1));
assert.equal(elements['stat-total'].textContent, 197);
assert.equal(elements['stat-other'].textContent, 4);
updateStats(products);
assert.equal(elements['stat-total'].textContent, 198);
assert.equal(elements['stat-other'].textContent, 5);
assert.equal(elements['stat-total-label'].textContent, 'Folder local hiển thị');
setSource('aggregate');
updateStats(products);
assert.equal(elements['stat-total-label'].textContent, 'Catalog hiển thị');
setSource('shop');
updateStats(products);
assert.equal(elements['stat-total-label'].textContent, 'Listing Etsy hiển thị');

for (const id of [
  'stat-total-label',
  'stat-posted-label',
  'stat-pending-label',
  'stat-error-label',
  'stat-other',
  'stat-other-label',
]) {
  assert.match(indexHtml, new RegExp(`id=["']${id}["']`), `missing HTML id ${id}`);
}
assert.match(indexHtml, /Khác \/ cần xử lý/);
assert.match(indexHtml, /không phải số Etsy Active/);

console.log('dashboard stats UI tests passed');
