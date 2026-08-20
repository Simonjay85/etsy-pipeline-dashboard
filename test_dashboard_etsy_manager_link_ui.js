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
  etsy_link_type: 'manager',
});
assert.equal(managerLink.url, 'https://www.etsy.com/your/shops/me/listing-editor/edit/123');
assert.equal(managerLink.kind, 'manager');
assert.equal(managerLink.listingId, '123');
assert.equal(managerLink.stale, false);

const fallbackLink = resolveEtsyListingLink({
  etsy_listing_id: '321',
  etsy_manager_status: '',
  etsy_edit_url: 'https://www.etsy.com/your/shops/me/listing-editor/edit/321',
  etsy_link_type: 'manager_fallback',
  etsy_link_warning_reason: 'listing_not_in_stale_snapshot',
  etsy_snapshot_stale: true,
});
assert.equal(fallbackLink.url, 'https://www.etsy.com/your/shops/me/listing-editor/edit/321');
assert.equal(fallbackLink.kind, 'fallback');
assert.equal(fallbackLink.warningReason, 'listing_not_in_stale_snapshot');
assert.equal(fallbackLink.stale, true);

assert.equal(
  resolveEtsyListingLink({
    etsy_listing_id: '500',
    etsy_manager_status: 'active',
    etsy_link_type: 'public',
    etsy_public_url: 'https://www.etsy.com/ca/listing/500/locale-listing-slug',
  }).url,
  'https://www.etsy.com/ca/listing/500/locale-listing-slug',
);

const freshFallbackProduct = {
  etsy_listing_id: '4554423428',
  etsy_manager_status: null,
  etsy_public_url: null,
  etsy_manage_url: null,
  etsy_edit_url: 'https://www.etsy.com/your/shops/me/listing-editor/edit/4554423428',
  etsy_link_type: 'manager_fallback',
  etsy_link_verified: false,
  etsy_link_warning_reason: null,
  etsy_snapshot_stale: false,
  etsy_url: 'https://www.etsy.com/listing/4554423428',
  status: '✅ Đã đăng draft',
};
const freshFallbackLink = resolveEtsyListingLink(freshFallbackProduct);
assert.equal(freshFallbackLink.url, 'https://www.etsy.com/your/shops/me/listing-editor/edit/4554423428');
assert.equal(freshFallbackLink.kind, 'fallback');
assert.equal(freshFallbackLink.listingId, '4554423428');
assert.equal(freshFallbackLink.warningReason, '');
assert.equal(freshFallbackLink.stale, false);

assert.equal(
  resolveEtsyListingLink({
    etsy_listing_id: '456',
    etsy_manager_status: 'active',
    etsy_link_type: 'public',
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

assert.equal(
  resolveEtsyListingLink({
    etsy_listing_id: '321',
    etsy_manager_status: '',
    etsy_link_type: 'manager_fallback',
    etsy_edit_url: 'https://www.etsy.com/listing/321',
  }).kind,
  'unavailable',
);

assert.equal(
  resolveEtsyListingLink({
    etsy_listing_id: '321',
    etsy_manager_status: '',
    etsy_link_type: 'manager_fallback',
    etsy_edit_url: 'https://www.etsy.com/your/shops/me/listing-editor/edit/322',
  }).kind,
  'unavailable',
);

const productCardStart = appJs.indexOf('function productCard');
const productCardEnd = appJs.indexOf('\nfunction productImageGallery', productCardStart);
const aggregateDisplayStart = appJs.indexOf('function findAggregateLocalProduct');
const aggregateDisplayEnd = appJs.indexOf('\nfunction getAggregateLocalRecords', aggregateDisplayStart);
assert.ok(productCardStart >= 0 && productCardEnd > productCardStart, 'Unable to isolate Etsy product card renderer');
assert.ok(
  aggregateDisplayStart >= 0 && aggregateDisplayEnd > aggregateDisplayStart,
  'Unable to isolate aggregate product display projection',
);
vm.runInNewContext(`
  const allProducts = [];
  function normalizeEtsyStatus(status) { return String(status || '').trim().toLowerCase(); }
  const runningSet = new Set();
  const productNeedsEtsyLink = product => !productEtsyLink(product).url;
  const productImageGallery = () => '';
  const renderCloudAssetUi = () => '';
  const renderSocialChannelBadges = () => '';
  const escHtml = value => String(value ?? '');
  const escJs = value => String(value ?? '');
  ${appJs.slice(start, end)}
  ${appJs.slice(aggregateDisplayStart, aggregateDisplayEnd)}
  ${appJs.slice(productCardStart, productCardEnd)}
  module.exports = { aggregateDisplayProduct, productCard };
`, sandbox);
const aggregateFreshFallbackProduct = sandbox.module.exports.aggregateDisplayProduct({
  row: 10,
  folder: 'product-06',
  reconciliation_status: 'unmatched_local_listing',
  reconciliation_note: 'Listing ID is absent from the latest Etsy Manager snapshot',
}, {
  ...freshFallbackProduct,
  row: 10,
  folder: 'product-06',
  title: 'Fresh draft fallback regression',
  sku: '',
  image_count: 0,
  pdf_count: 0,
  price: 4.99,
  missing_fields: [],
  social_statuses: {},
});
assert.equal(aggregateFreshFallbackProduct.status, '✅ Đã đăng draft');
assert.equal(aggregateFreshFallbackProduct.reconciliation_status, 'unmatched_local_listing');
assert.equal(aggregateFreshFallbackProduct.etsy_link_type, 'manager_fallback');
assert.equal(aggregateFreshFallbackProduct.etsy_link_verified, false);
const freshFallbackCard = sandbox.module.exports.productCard(aggregateFreshFallbackProduct);
assert.match(freshFallbackCard, /href="https:\/\/www\.etsy\.com\/your\/shops\/me\/listing-editor\/edit\/4554423428"/);
assert.match(freshFallbackCard, /🧭 Manager \(snapshot chưa xác minh\) 4554423428/);
assert.match(freshFallbackCard, />🧭 Manager \(snapshot chưa xác minh\)<\/button>/);
assert.match(freshFallbackCard, /⚠ Etsy link chưa xác minh/);
assert.match(freshFallbackCard, /status-badge status-posted/);
assert.doesNotMatch(freshFallbackCard, /link unavailable/i);
assert.doesNotMatch(freshFallbackCard, /🔒 Unavailable/);
assert.doesNotMatch(freshFallbackCard, /⚠ Draft · chưa có link/);
assert.doesNotMatch(freshFallbackCard, /snapshot stale/);

const postedNoLinkAggregateProduct = sandbox.module.exports.aggregateDisplayProduct({
  row: 12,
  folder: 'product-posted-no-link',
  reconciliation_status: 'unmatched_local_listing',
  reconciliation_note: 'Mapping pending',
}, {
  etsy_listing_id: '4554423428',
  etsy_manager_status: null,
  etsy_manage_url: null,
  etsy_edit_url: null,
  etsy_link_type: 'local_unverified',
  etsy_public_url: 'https://www.etsy.com/listing/4554423428',
  etsy_link_verified: false,
  etsy_link_warning_reason: null,
  etsy_snapshot_stale: false,
  etsy_url: 'https://www.etsy.com/listing/4554423428',
  row: 12,
  folder: 'product-posted-no-link',
  title: 'Posted without link regression',
  sku: '',
  image_count: 0,
  pdf_count: 0,
  price: 4.99,
  missing_fields: [],
  social_statuses: {},
  status: '✅ Đã đăng',
});
assert.equal(postedNoLinkAggregateProduct.status, '✅ Đã đăng');
assert.equal(postedNoLinkAggregateProduct.etsy_link_type, 'local_unverified');
assert.equal(postedNoLinkAggregateProduct.etsy_link_verified, false);
assert.equal(postedNoLinkAggregateProduct.reconciliation_status, 'unmatched_local_listing');
const postedNoLinkCard = sandbox.module.exports.productCard(postedNoLinkAggregateProduct);
assert.match(postedNoLinkCard, /status-badge status-posted/);
assert.doesNotMatch(postedNoLinkCard, /status-badge status-pending/);
assert.match(postedNoLinkCard, /⚠ Etsy link chưa xác minh/);
assert.doesNotMatch(postedNoLinkCard, /⚠ Draft · chưa có link/);
assert.match(postedNoLinkCard, /href="https:\/\/www\.etsy\.com\/listing\/4554423428"/);
assert.match(postedNoLinkCard, /🔗 Etsy \(snapshot chưa xác minh\) 4554423428/);
assert.doesNotMatch(postedNoLinkCard, /🔒 Unavailable/);

const unsafeAggregateFallbackProduct = sandbox.module.exports.aggregateDisplayProduct({
  row: 11,
  folder: 'product-unsafe',
  reconciliation_status: 'unmatched_local_listing',
}, {
  ...freshFallbackProduct,
  row: 11,
  folder: 'product-unsafe',
  title: 'Unsafe fallback regression',
  status: '❌ Lỗi: draft creation failed',
  sku: '',
  image_count: 0,
  pdf_count: 0,
  price: 4.99,
  missing_fields: [],
  social_statuses: {},
});
assert.equal(unsafeAggregateFallbackProduct.status, '⏳ Chờ đăng · Chưa khớp snapshot Etsy');
assert.equal(unsafeAggregateFallbackProduct.etsy_listing_id, '4554423428');
assert.equal(unsafeAggregateFallbackProduct.etsy_url, 'https://www.etsy.com/listing/4554423428');
assert.equal(unsafeAggregateFallbackProduct.etsy_link_type, 'unavailable');
assert.equal(unsafeAggregateFallbackProduct.etsy_edit_url, null);
assert.equal(unsafeAggregateFallbackProduct.etsy_manage_url, null);
assert.equal(unsafeAggregateFallbackProduct.etsy_link_verified, false);
const unsafeFallbackCard = sandbox.module.exports.productCard(unsafeAggregateFallbackProduct);
assert.doesNotMatch(unsafeFallbackCard, /listing-editor\/edit\/4554423428/);
assert.doesNotMatch(unsafeFallbackCard, /🧭 Manager/);
assert.doesNotMatch(unsafeFallbackCard, />🧭 Manager<\/button>/);
assert.match(unsafeFallbackCard, /link unavailable/i);
assert.match(unsafeFallbackCard, /🔒 Unavailable/);

console.log('dashboard Etsy Manager link UI tests passed');
