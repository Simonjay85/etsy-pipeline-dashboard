'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const indexHtml = fs.readFileSync('dashboard_static/index.html', 'utf8');
const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');

const cloudFilter = indexHtml.match(/<select[^>]+id="filter-cloud-status"[\s\S]*?<\/select>/)?.[0] || '';
assert.ok(cloudFilter, 'Missing cloud status selector');
assert.match(cloudFilter, /onchange="filterProducts\(\)"/);
for (const value of ['all', 'local', 'scheduled', 'cloud-only', 'error']) {
  assert.match(cloudFilter, new RegExp(`value="${value}"`), `Missing cloud filter option: ${value}`);
}

assert.match(appJs, /\/api\/cloud-assets\/status/);
assert.match(appJs, /shop_id=\$\{encodeURIComponent\(shopId\)\}&scope=shop/);
assert.match(appJs, /folder=\$\{encodeURIComponent\(folder\)\}/);
assert.match(appJs, /body: JSON\.stringify\(payload\)/);
assert.match(appJs, /return \{ shop_id: shopId, scope: 'shop', folder: safeFolder \}/);
assert.match(appJs, /withCloudAssetMutation\(/);
assert.match(appJs, /cloudAssetMutationInFlight/);
for (const endpoint of ['schedule-upload-verify', 'restore', 'cancel-offload']) {
  assert.match(appJs, new RegExp(`/api/cloud-assets/${endpoint}`), `Missing cloud endpoint: ${endpoint}`);
}
assert.match(appJs, /void loadCloudAssetStatus\(\)/, 'Cloud status must not block product loading');

const actionStart = appJs.indexOf('async function cloudAssetRefreshStatus');
const actionEnd = appJs.indexOf('async function loadProducts', actionStart);
assert.ok(actionStart >= 0 && actionEnd > actionStart, 'Unable to isolate cloud action functions');
const cloudActionSource = appJs.slice(actionStart, actionEnd);
assert.doesNotMatch(cloudActionSource, /\/api\/etsy\//, 'Cloud actions must not call Etsy endpoints');
assert.doesNotMatch(cloudActionSource, /\/api\/products\/[^'"`]*\/post|push-to-etsy|run-selected-products/, 'Cloud actions must not call Etsy post endpoints');

const context = {
  console,
  setInterval() {},
  setTimeout,
  window: {
    matchMedia: () => ({ addEventListener() {} }),
    addEventListener() {},
  },
  document: {
    addEventListener() {},
    getElementById(id) {
      return id === 'shop-switcher' ? { value: 'templystudios' } : null;
    },
  },
  toast() {},
  fetch: async () => ({ ok: false, status: 500, async json() { return { ok: false, error: 'not mocked' }; } }),
};
vm.createContext(context);
vm.runInContext(appJs, context);
context.toast = () => {};

assert.deepEqual(JSON.parse(JSON.stringify(context.cloudAssetRequestPayload(' product-01 '))), {
  shop_id: 'templystudios',
  scope: 'shop',
  folder: 'product-01',
});
assert.equal(
  context.cloudAssetStatusUrl('templystudios'),
  '/api/cloud-assets/status?shop_id=templystudios&scope=shop',
);
assert.equal(
  context.cloudAssetStatusUrl('templystudios', 'product-01'),
  '/api/cloud-assets/status?shop_id=templystudios&scope=shop&folder=product-01',
);

assert.equal(context.cloudAssetStatusCategory({ state: 'LOCAL_ONLY' }), 'local');
assert.equal(context.cloudAssetStatusCategory({ state: 'OFFLOAD_SCHEDULED' }), 'scheduled');
assert.equal(context.cloudAssetStatusCategory({ state: 'CLOUD_ONLY' }), 'cloud-only');
assert.equal(context.cloudAssetStatusCategory({ state: 'ERROR' }), 'error');
assert.equal(context.cloudAssetStatusLabel({ state: 'CLOUD_ONLY' }), '☁️ Cloud-only');
assert.equal(context.cloudAssetStatusLabel({ state: 'OFFLOAD_SCHEDULED' }), '⏱ Offload scheduled');

vm.runInContext(`cloudAssetStatusByFolder.set('product-01', {
  state: 'OFFLOAD_SCHEDULED',
  eligible_after: '2026-08-12T00:00:00Z',
  reclaimable_bytes: 1234,
});
cloudAssetStatusByFolder.set('product-02', { state: 'CLOUD_ONLY', local_available: false, cloud_available: true });
cloudAssetStatusByFolder.set('product-03', { state: 'ERROR', last_error: 'status failed' });`, context);

assert.equal(context.cloudStatusFilterMatches({ folder: 'product-01' }, 'scheduled'), true);
assert.equal(context.cloudStatusFilterMatches({ folder: 'product-01' }, 'cloud-only'), false);
assert.equal(context.cloudStatusFilterMatches({ folder: 'product-02' }, 'cloud-only'), true);
assert.equal(context.cloudStatusFilterMatches({ folder: 'product-03' }, 'error'), true);
assert.equal(context.cloudStatusFilterMatches({ folder: 'product-04' }, 'local'), true);
assert.equal(context.cloudStatusFilterMatches({ folder: 'product-04' }, 'all'), true);

const scheduledCardCloudUi = context.renderCloudAssetUi('product-01');
assert.match(scheduledCardCloudUi, /⏱ Offload scheduled/);
assert.match(scheduledCardCloudUi, /offload/);
assert.match(scheduledCardCloudUi, /1\.2 KB/);
assert.match(scheduledCardCloudUi, /cloudAssetCancelOffload/);

const cloudOnlyCardCloudUi = context.renderCloudAssetUi('product-02');
assert.match(cloudOnlyCardCloudUi, /☁️ Cloud-only/);
assert.match(cloudOnlyCardCloudUi, /cloudAssetRestore/);
vm.runInContext(`cloudAssetStatusByFolder.set('product-05', {
  state: 'UPLOAD_SCHEDULED',
  upload_schedule: { status: 'queued', wait_reason: 'đang Sync listing Etsy' },
});`, context);
const uploadQueuedCardCloudUi = context.renderCloudAssetUi('product-05');
assert.match(uploadQueuedCardCloudUi, /🗓️ Upload scheduled/);
assert.match(uploadQueuedCardCloudUi, /chờ đang Sync listing Etsy/);
context.loadCloudAssetStatus = async () => ({});

(async () => {
  const calls = [];
  let releaseUpload;
  const uploadGate = new Promise((resolve) => {
    releaseUpload = resolve;
  });

  context.fetch = async (url) => {
    calls.push(url);
    if (String(url).includes('/api/cloud-assets/schedule-upload-verify')) {
      await uploadGate;
      return {
        ok: true,
        status: 200,
        async json() {
          return { ok: true };
        },
      };
    }
    return {
      ok: true,
      status: 200,
      async json() {
        return { ok: true };
      },
    };
  };

  const firstButton = { disabled: false, innerHTML: 'upload' };
  const secondButton = { disabled: false, innerHTML: 'upload' };
  const first = context.cloudAssetUploadAndVerify('product-01', firstButton);
  await new Promise((resolve) => setTimeout(resolve, 0));
  const second = context.cloudAssetUploadAndVerify('product-01', secondButton);

  assert.equal(calls.filter((u) => u.includes('/api/cloud-assets/schedule-upload-verify')).length, 1);
  assert.equal(firstButton.disabled, true, 'Primary request should disable its button while in progress');
  assert.equal(secondButton.disabled, false, 'Deduped request should not enable a second button lock');

  releaseUpload();
  await Promise.all([first, second]);
  assert.equal(calls.filter((u) => u.includes('/api/cloud-assets/schedule-upload-verify')).length, 1);
  assert.equal(calls.filter((u) => u.includes('/api/cloud-assets/verify')).length, 0);
  assert.equal(firstButton.disabled, false);
  assert.equal(secondButton.disabled, false);
})().then(() => {
  console.log('cloud assets UI tests passed');
}).catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
