const fs = require('node:fs');
const assert = require('node:assert/strict');
const vm = require('node:vm');

const indexHtml = fs.readFileSync('dashboard_static/index.html', 'utf8');
const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');
const stateLine = 'let etsySingleSyncInFlight = false;';
const functionStart = appJs.indexOf('async function syncListingFromEtsy(row, folder)');
const functionEnd = appJs.indexOf('\n\n// ── Actions', functionStart);
assert.ok(appJs.includes(stateLine), 'Single Sync in-flight state is missing');
assert.ok(
  indexHtml.includes('/static/app.js?v=20260819-cloud-offload-preflight-01'),
  'Dashboard must load the Single Sync guard JavaScript cache version',
);
assert.ok(functionStart >= 0 && functionEnd > functionStart, 'Unable to isolate syncListingFromEtsy');

const enabledButton = { disabled: false };
const alreadyDisabledButton = { disabled: true };
const card = { style: { opacity: '1' } };
const toasts = [];
let fetchCalls = 0;
let resolveFetch;

const sandbox = {
  module: { exports: {} },
  exports: {},
  allProducts: [{
    row: 65,
    folder: 'product-65',
    etsy_url: 'https://www.etsy.com/listing/4528326700/example',
  }],
  document: {
    querySelectorAll(selector) {
      assert.equal(selector, '.btn-sync');
      return [enabledButton, alreadyDisabledButton];
    },
    getElementById(id) {
      if (id === 'shop-switcher') return { value: 'daisyflowdigital' };
      assert.equal(id, 'card-65');
      return card;
    },
  },
  toast(level, message) {
    toasts.push({ level, message });
  },
  fetch(url, options) {
    fetchCalls += 1;
    assert.equal(url, '/api/products/65/sync-from-etsy');
    assert.equal(options.method, 'POST');
    assert.deepEqual(JSON.parse(options.body), {
      shop: 'daisyflowdigital',
      folder: 'product-65',
      listing_id: '4528326700',
    });
    return new Promise(resolve => { resolveFetch = resolve; });
  },
  loadProducts: async () => {
    throw new Error('409 response must not reload products');
  },
};

vm.runInNewContext(
  `${stateLine}\n${appJs.slice(functionStart, functionEnd)}\nmodule.exports = { syncListingFromEtsy };`,
  sandbox,
);

(async () => {
  const first = sandbox.module.exports.syncListingFromEtsy(65, 'product-65');
  assert.equal(fetchCalls, 1, 'First click must start one request');
  assert.equal(enabledButton.disabled, true, 'Enabled Sync buttons must be visibly disabled');
  assert.equal(alreadyDisabledButton.disabled, true, 'Pre-disabled Sync buttons must remain disabled');
  assert.equal(card.style.opacity, '0.5');

  await sandbox.module.exports.syncListingFromEtsy(65, 'product-65');
  assert.equal(fetchCalls, 1, 'Second click in the same tab must not start another request');
  assert.ok(
    toasts.some(item => item.level === 'warning' && item.message.includes('Đang có một lượt Sync Etsy')),
    'Second click must show an actionable warning',
  );

  resolveFetch({
    status: 409,
    json: async () => ({
      ok: false,
      code: 'etsy_sync_busy',
      error: 'Shop daisyflowdigital đang có một lượt Sync Etsy khác. Hãy chờ hoàn tất.',
    }),
  });
  await first;

  assert.ok(
    toasts.some(item => item.level === 'warning' && item.message.includes('daisyflowdigital')),
    'Backend etsy_sync_busy response must be shown as a warning',
  );
  assert.equal(enabledButton.disabled, false, 'Button disabled by this call must be restored');
  assert.equal(alreadyDisabledButton.disabled, true, 'Button disabled before this call must stay disabled');
  assert.equal(card.style.opacity, '1');

  console.log('etsy single sync UI tests passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
