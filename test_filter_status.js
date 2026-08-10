'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');
const filterStart = appJs.indexOf('function isStatusFilterDisabledForSource');
const filterEnd = appJs.indexOf('function renderAggregateProducts', filterStart);
if (filterStart < 0 || filterEnd < 0 || filterEnd <= filterStart) {
  throw new Error('Cannot isolate helper functions from dashboard_static/app.js');
}

const sandbox = { module: { exports: {} }, exports: {} };
const products = [
  { row: 12, folder: 'product-12', status: '⚠️ Thiếu Title', missing_fields: ['title'], etsy_status: 'draft' },
  { row: 13, folder: 'product-13', status: '⏳ Chờ đăng', missing_fields: [], etsy_status: 'active' },
];
sandbox.__testAllProducts = products;
const helpers = `
  const allProducts = globalThis.__testAllProducts;
  ${appJs.slice(filterStart, filterEnd)}
  module.exports = {
    isStatusFilterDisabledForSource,
    findAggregateLocalProduct,
    statusFilterMatches,
  };
`;
vm.runInNewContext(helpers, sandbox);
const { isStatusFilterDisabledForSource, findAggregateLocalProduct, statusFilterMatches } = sandbox.module.exports;

// Legacy filters
assert.equal(statusFilterMatches({ status: '✅ Đã đăng', missing_fields: ['title'] }, 'posted'), true);
assert.equal(statusFilterMatches({ status: '✅ Đã đăng draft', missing_fields: [] }, 'posted'), false);
assert.equal(statusFilterMatches({ status: 'draft', missing_fields: ['title'] }, 'draft'), true);
assert.equal(statusFilterMatches({ status: 'draft', missing_fields: [] }, 'pending'), false);
assert.equal(statusFilterMatches({ status: '⏳ Chờ đăng', missing_fields: [] }, 'pending'), true);
assert.equal(statusFilterMatches({ source: 'etsy', status: 'active', etsy_status: 'active', missing_fields: [] }, 'posted'), true);
assert.equal(statusFilterMatches({ status: 'active', missing_fields: [] }, 'draft'), false);
assert.equal(statusFilterMatches({ status: '❌ Lỗi', missing_fields: [] }, 'error'), true);
assert.equal(statusFilterMatches({ status: '⚠️ Thiếu Title', missing_fields: ['title'] }, 'missing_title'), true);
assert.equal(statusFilterMatches({ status: '✅ Đã đăng', missing_fields: ['title'] }, 'missing_title'), true);
assert.equal(statusFilterMatches({ status: '✅ Đã đăng', missing_fields: ['description'] }, 'missing_description'), true);
assert.equal(statusFilterMatches({ status: '✅ Đã đăng', missing_fields: ['tags_count'] }, 'missing_tags'), true);
assert.equal(statusFilterMatches({ status: '✅ Đã đăng', missing_fields: ['title', 'description'] }, 'missing_seo'), true);
assert.equal(statusFilterMatches({ status: '✅ Đã đăng', missing_fields: [] }, 'missing_seo'), false);
assert.equal(statusFilterMatches({ source: 'etsy', etsy_status: 'draft', missing_fields: ['tags'] }, 'draft'), true);
assert.equal(statusFilterMatches({ etsy_status: 'active', missing_fields: ['tags'] }, 'missing_tags'), true);
assert.equal(statusFilterMatches({ status: 'active', missing_fields: [] }, 'missing_tags'), false);
assert.equal(statusFilterMatches({ status: '✅ Đã đăng', missing_fields: [] }, 'unknown_filter'), false);

// Etsy-only aggregate records should not be considered missing_* despite local-like status names.
assert.equal(statusFilterMatches({ source: 'etsy', status: 'active', etsy_status: 'active' }, 'posted'), true);
assert.equal(statusFilterMatches({ source: 'etsy', status: 'draft', etsy_status: 'draft' }, 'draft'), true);
assert.equal(statusFilterMatches({ source: 'etsy', status: 'active', etsy_status: 'active', missing_fields: ['title'] }, 'missing_title'), false);

// Filter status enable/disable by source
assert.equal(isStatusFilterDisabledForSource('shop'), true);
assert.equal(isStatusFilterDisabledForSource('local'), false);
assert.equal(isStatusFilterDisabledForSource('aggregate'), false);

assert.deepEqual(findAggregateLocalProduct({ row: 12, folder: 'product-12' }), products[0]);
assert.deepEqual(findAggregateLocalProduct({ row: 999, folder: 'product-13' }), products[1]);
assert.equal(findAggregateLocalProduct({ row: 999, folder: 'missing-folder' }), null);

// Aggregate status filtering uses local product for local/both records when available.
const localRecord = { row: 12, folder: 'product-12', source: 'both', status: 'active', etsy_status: 'active', missing_fields: [] };
const noLocalRecord = { row: 999, folder: 'product-999', source: 'etsy', status: 'active', etsy_status: 'active', missing_fields: [] };
assert.equal(statusFilterMatches(findAggregateLocalProduct(localRecord), 'missing_title'), true);
assert.equal(statusFilterMatches(findAggregateLocalProduct(localRecord), 'draft'), false);
assert.equal(statusFilterMatches(findAggregateLocalProduct(noLocalRecord), 'missing_title'), false);
assert.equal(statusFilterMatches(noLocalRecord, 'posted'), true);

console.log('status filter tests passed');
