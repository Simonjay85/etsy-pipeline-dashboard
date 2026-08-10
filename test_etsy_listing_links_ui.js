'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');
const helpersStart = appJs.indexOf('function normalizeEtsyStatus');
const helpersEnd = appJs.indexOf('// ── Init', helpersStart);
const needsStart = appJs.indexOf('function productNeedsEtsyLink');
const needsEnd = appJs.indexOf('\nfunction productCard', needsStart);
assert.ok(helpersStart >= 0 && helpersEnd > helpersStart, 'Etsy link helpers missing');
assert.ok(needsStart >= 0 && needsEnd > needsStart, 'productNeedsEtsyLink missing');

const sandbox = { module: { exports: {} }, exports: {} };
vm.runInNewContext(`
  ${appJs.slice(helpersStart, helpersEnd)}
  ${appJs.slice(needsStart, needsEnd)}
  module.exports = { resolveEtsyListingLink, productEtsyLink, productNeedsEtsyLink };
`, sandbox);
const { resolveEtsyListingLink, productEtsyLink, productNeedsEtsyLink } = sandbox.module.exports;

const activeLink = resolveEtsyListingLink({ id: '100', managerStatus: 'active', url: 'https://www.etsy.com/listing/100', editUrl: 'https://www.etsy.com/your/shops/me/listing-editor/edit/100' });
assert.equal(activeLink.url, 'https://www.etsy.com/listing/100');
assert.equal(activeLink.kind, 'public');
assert.equal(activeLink.listingId, '100');
assert.equal(activeLink.stale, false);
assert.equal(
  resolveEtsyListingLink({ id: '104', managerStatus: 'active' }).url,
  'https://www.etsy.com/listing/104',
);
assert.equal(
  resolveEtsyListingLink({ id: '101', managerStatus: 'draft', url: 'https://www.etsy.com/listing/101', editUrl: 'https://www.etsy.com/your/shops/me/listing-editor/edit/101' }).url,
  'https://www.etsy.com/your/shops/me/listing-editor/edit/101',
);
assert.equal(
  resolveEtsyListingLink({ id: '102', managerStatus: 'inactive', url: 'https://www.etsy.com/listing/102' }).url,
  '',
);
assert.equal(
  productEtsyLink({ etsy_listing_id: '103', etsy_manager_status: 'expired', etsy_manage_url: 'https://www.etsy.com/your/shops/me/listing-editor/edit/103' }).kind,
  'manager',
);
assert.equal(
  productEtsyLink({ etsy_listing_id: '999', etsy_manager_status: 'draft', etsy_public_url: 'https://www.etsy.com/listing/999' }).url,
  '',
);
assert.equal(productNeedsEtsyLink({ etsy_url: 'https://www.etsy.com/listing/999', status: '✅ Đã đăng' }), true);
assert.equal(productNeedsEtsyLink({ etsy_listing_id: '101', etsy_manager_status: 'draft', etsy_manage_url: 'https://www.etsy.com/your/shops/me/listing-editor/edit/101', status: '✅ Đã đăng draft' }), false);

console.log('etsy listing link UI tests passed');
