const fs = require('node:fs');
const assert = require('node:assert/strict');
const vm = require('node:vm');

const indexHtml = fs.readFileSync('dashboard_static/index.html', 'utf8');
const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');

assert.ok(indexHtml.includes('id="link-etsy-modal"'), 'Missing link-etsy modal');
assert.ok(indexHtml.includes('id="link-etsy-submit"'), 'Missing link-etsy submit button');
assert.ok(indexHtml.includes('submitLinkEtsyFromLocal()'), 'Missing link-etsy submit handler wiring');
assert.ok(appJs.includes('function productNeedsEtsyLink'), 'productNeedsEtsyLink helper missing');
assert.ok(appJs.includes('⚠ Draft · chưa có link'), 'Unverified draft badge label missing');
assert.ok(appJs.includes('openLinkEtsyFromLocal'), 'openLinkEtsyFromLocal missing');
assert.ok(appJs.includes('/api/etsy/link-suggestions-for-folder/'), 'Folder link suggestions API call missing');
assert.ok(appJs.includes('allow_manual: true'), 'Manual link submit must allow map without snapshot');
assert.ok(
  appJs.includes("await loadProducts({ throwOnError: true });"),
  'A newly linked card must reload canonical product data so Sync/Update are rendered immediately',
);

const helperStart = appJs.indexOf('function productNeedsEtsyLink(p)');
const helperEnd = appJs.indexOf('\nfunction productCard(p)', helperStart);
const linkHelperStart = appJs.indexOf('function normalizeEtsyStatus');
const linkHelperEnd = appJs.indexOf('// ── Init', linkHelperStart);
assert.ok(helperStart >= 0 && helperEnd > helperStart, 'Unable to isolate productNeedsEtsyLink');
assert.ok(linkHelperStart >= 0 && linkHelperEnd > linkHelperStart, 'Unable to isolate Etsy link helpers');
const helperSource = appJs.slice(helperStart, helperEnd);
const sandbox = { module: { exports: {} }, exports: {} };
vm.runInNewContext(`${appJs.slice(linkHelperStart, linkHelperEnd)}\n${helperSource}\nmodule.exports = { productNeedsEtsyLink };`, sandbox);
const { productNeedsEtsyLink } = sandbox.module.exports;

assert.equal(
  productNeedsEtsyLink({ status: '✅ Đã đăng draft (URL chưa xác minh)', etsy_url: '' }),
  true,
);
assert.equal(
  productNeedsEtsyLink({ status: '✅ Đã đăng draft', etsy_url: '' }),
  true,
);
assert.equal(
  productNeedsEtsyLink({ status: '✅ Đã đăng draft', etsy_url: 'https://www.etsy.com/listing/1' }),
  true,
);
assert.equal(
  productNeedsEtsyLink({ status: '⏳ Chờ đăng', etsy_url: '' }),
  false,
);

console.log('etsy link unverified UI tests passed');
