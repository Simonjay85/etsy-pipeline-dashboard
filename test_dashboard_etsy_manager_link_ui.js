const fs = require('node:fs');
const assert = require('node:assert/strict');
const vm = require('node:vm');

const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');
const start = appJs.indexOf('function resolveEtsyListingLink');
const end = appJs.indexOf('// ── Init', start);
assert.ok(start >= 0 && end > start, 'Unable to isolate Etsy link resolver');

const sandbox = { module: { exports: {} }, exports: {} };
vm.runInNewContext(`function normalizeEtsyStatus(status) { return String(status || '').trim().toLowerCase(); }\n${appJs.slice(start, end)}\nmodule.exports = { resolveEtsyListingLink };`, sandbox);
const { resolveEtsyListingLink } = sandbox.module.exports;

const managerLink = resolveEtsyListingLink({
  etsy_listing_id: '123',
  etsy_manager_status: 'expired',
  etsy_url: 'https://www.etsy.com/listing/123',
  etsy_edit_url: 'https://www.etsy.com/your/shops/me/listing-editor/edit/123',
});
assert.equal(managerLink.url, 'https://www.etsy.com/your/shops/me/listing-editor/edit/123');
assert.equal(managerLink.kind, 'manager');
assert.equal(managerLink.listingId, '123');

assert.equal(
  resolveEtsyListingLink({
    etsy_listing_id: '456',
    etsy_manager_status: 'active',
    etsy_url: 'https://www.etsy.com/listing/456',
    etsy_edit_url: 'https://www.etsy.com/your/shops/me/listing-editor/edit/456',
  }).kind,
  'public',
);

assert.equal(
  resolveEtsyListingLink({
    etsy_listing_id: '789',
    etsy_url: 'https://www.etsy.com/listing/789',
  }).kind,
  'unavailable',
);

assert.equal(
  resolveEtsyListingLink({
    etsy_listing_id: '999',
    etsy_manager_status: 'inactive',
    etsy_url: 'https://www.etsy.com/listing/999',
  }).kind,
  'unavailable',
);

console.log('dashboard Etsy Manager link UI tests passed');
