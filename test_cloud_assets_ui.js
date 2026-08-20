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
assert.match(indexHtml, /id="local-batch-cloud-btn"[^>]*onclick="batchCloudUploadAndOffload\(\)"/);
assert.match(indexHtml, /app\.js\?v=[^"]*cloud-offload-preflight/);
assert.match(appJs, /async function batchCloudUploadAndOffload\(\)/);
assert.match(appJs, /cloudBatchSkipReason/);
for (const endpoint of ['schedule-upload-verify', 'schedule-upload-verify-offload', 'restore', 'cancel-offload']) {
  assert.match(appJs, new RegExp(`/api/cloud-assets/${endpoint}`), `Missing cloud endpoint: ${endpoint}`);
}
assert.match(appJs, /void loadCloudAssetStatus\(\)/, 'Cloud status must not block product loading');

const actionStart = appJs.indexOf('async function cloudAssetRefreshStatus');
const actionEnd = appJs.indexOf('async function loadProducts', actionStart);
assert.ok(actionStart >= 0 && actionEnd > actionStart, 'Unable to isolate cloud action functions');
const cloudActionSource = appJs.slice(actionStart, actionEnd);
assert.doesNotMatch(cloudActionSource, /\/api\/etsy\//, 'Cloud actions must not call Etsy endpoints');
assert.doesNotMatch(cloudActionSource, /\/api\/products\/[^'"`]*\/post|push-to-etsy|run-selected-products/, 'Cloud actions must not call Etsy post endpoints');

const productGridNode = { id: 'product-grid', _innerHTML: '', _innerHTMLWrites: 0 };
Object.defineProperty(productGridNode, 'innerHTML', {
  get() {
    return this._innerHTML;
  },
  set(value) {
    this._innerHTML = String(value);
    this._innerHTMLWrites += 1;
  },
});

const context = {
  console,
  setInterval() {},
  setTimeout,
  _productGrid: productGridNode,
  _cloudUiNodes: new Map(),
  _cloudUiInnerWrites: 0,
  _checkboxNodes: [],
  window: {
    matchMedia: () => ({ addEventListener() {} }),
    addEventListener() {},
    confirm: () => true,
  },
  document: {
    _cloudFilterEl: { value: 'all' },
    getElementById(id) {
      if (id === 'product-grid') return context._productGrid;
      if (id === 'shop-switcher') return { value: 'templystudios' };
      if (id === 'filter-cloud-status') return this._cloudFilterEl;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === '.product-cb') return [...context._checkboxNodes];
      if (selector === '.product-cb:checked') return [...context._checkboxNodes].filter((checkbox) => checkbox.checked);
      const match = String(selector || '').match(/^\.cloud-asset-ui\[data-cloud-folder="([^"]+)"\]$/);
      if (match) {
        const folder = match[1];
        const safeFolder = folder.replace(/\\"/g, '"');
        const matchNode = context._cloudUiNodes.get(safeFolder);
        return matchNode ? [matchNode] : [];
      }
      return [];
    },
    addEventListener() {},
  },
  toast() {},
  fetch: async () => ({ ok: false, status: 500, async json() { return { ok: false, error: 'not mocked' }; } }),
};

const setCloudUiNode = (folder, { key = '' } = {}) => {
  const safeFolder = String(folder || '').trim();
  if (!safeFolder) return null;
  const node = {
    _attrs: {
      'data-cloud-folder': safeFolder,
      'data-cloud-ui-key': String(key),
    },
    _innerHTML: '',
    _innerHTMLWrites: 0,
    getAttribute(name) {
      return this._attrs[name] || null;
    },
    setAttribute(name, value) {
      this._attrs[name] = String(value);
      if (name === 'data-cloud-ui-key') {
        context._cloudUiInnerWrites += 1;
      }
    },
  };
  Object.defineProperty(node, 'innerHTML', {
    get() {
      return this._innerHTML;
    },
    set(value) {
      this._innerHTML = String(value);
      this._innerHTMLWrites += 1;
      context._cloudUiInnerWrites += 1;
    },
  });
  context._cloudUiNodes.set(safeFolder, node);
  return node;
};

const setCheckboxNode = (folder, checked = false) => {
  const node = {
    value: String(folder),
    checked: Boolean(checked),
    dataset: { folder: String(folder) },
  };
  context._checkboxNodes.push(node);
  return node;
};
const setCloudStatus = (folder, status) => {
  vm.runInContext(`cloudAssetStatusByFolder.set(${JSON.stringify(String(folder))}, ${JSON.stringify(status)});`, context);
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
setCloudStatus('product-09', {
  state: 'LOCAL_ONLY',
  local_assets_complete: false,
  local_error: 'missing usable files assets',
});
assert.match(
  context.cloudBatchSkipReason('product-09'),
  /local thiếu image\/file usable/,
  'Batch cloud action must report incomplete local asset groups from cloud status',
);
setCloudStatus('product-10', {
  state: 'ERROR',
  local_available: false,
  cloud_available: false,
});
assert.match(
  context.cloudBatchSkipReason('product-10'),
  /local thiếu image\/file usable/,
  'Batch cloud action must skip a known empty local product',
);
setCloudStatus('product-11', {
  state: 'CLEANUP_PENDING',
  local_available: false,
  cloud_available: true,
  local_assets_complete: false,
});
assert.equal(
  context.cloudBatchSkipReason('product-11'),
  '',
  'Batch cloud action must preserve an idempotent cleanup-pending retry',
);
vm.runInContext(`cloudAssetStatusByFolder.set('product-05', {
  state: 'UPLOAD_SCHEDULED',
  upload_schedule: { status: 'queued', wait_reason: 'đang Sync listing Etsy', delete_local: true },
});`, context);
const uploadQueuedCardCloudUi = context.renderCloudAssetUi('product-05');
assert.match(uploadQueuedCardCloudUi, /🗓️ Upload \+ xoá local scheduled/);
assert.match(uploadQueuedCardCloudUi, /chờ đang Sync listing Etsy/);
assert.match(uploadQueuedCardCloudUi, /sẽ xoá local sau verify/);
assert.match(uploadQueuedCardCloudUi, /Đã vào hàng chờ/);
assert.match(uploadQueuedCardCloudUi, /1\. Chờ lượt/);
assert.match(uploadQueuedCardCloudUi, /Local vẫn còn nguyên/);
vm.runInContext(`cloudAssetStatusByFolder.set('product-06', {
  state: 'UPLOADING',
  upload_schedule: { status: 'running', delete_local: true },
});`, context);
const uploadRunningCardCloudUi = context.renderCloudAssetUi('product-06');
assert.match(uploadRunningCardCloudUi, /Đang upload &amp; verify manifest\/hash/);
assert.match(uploadRunningCardCloudUi, /Xoá local sau verify/);
assert.match(uploadRunningCardCloudUi, /Cloud-only/);
const scheduledCardCloudUiKey = scheduledCardCloudUi.match(/data-cloud-ui-key="([^"]+)"/)?.[1];
assert.ok(scheduledCardCloudUiKey, 'Cloud UI should include a stable render key attribute');
const gridElement = context.document.getElementById('product-grid');
const product03UiNode = setCloudUiNode('product-03');
const product04UiNode = setCloudUiNode('product-04');
const product05UiNode = setCloudUiNode('product-05');
const product08UiNode = setCloudUiNode('product-08');
product03UiNode?.setAttribute('data-cloud-ui-key', context.cloudAssetUiRenderKey(context.cloudAssetStatusForFolder('product-03')));
product04UiNode?.setAttribute('data-cloud-ui-key', context.cloudAssetUiRenderKey());
product05UiNode?.setAttribute('data-cloud-ui-key', context.cloudAssetUiRenderKey(context.cloudAssetStatusForFolder('product-05')));
product08UiNode?.setAttribute('data-cloud-ui-key', context.cloudAssetUiRenderKey(context.cloudAssetStatusForFolder('product-08')));
(async () => {
  const statusCalls = [];
  let releaseStatus;
  const statusGate = new Promise((resolve) => {
    releaseStatus = resolve;
  });
  let filterCalls = 0;
  context.filterProducts = () => {
    filterCalls += 1;
  };
  context.fetch = async (url) => {
    statusCalls.push(url);
    await statusGate;
    return {
      ok: true,
      status: 200,
      async json() {
        return { ok: true, items: [] };
      },
    };
  };
  const firstStatus = context.loadCloudAssetStatus({ force: true });
  await new Promise((resolve) => setTimeout(resolve, 0));
  const secondStatus = context.loadCloudAssetStatus({ force: true });
  assert.equal(
    statusCalls.filter((url) => url.includes('/api/cloud-assets/status')).length,
    1,
    'Forced whole-shop refreshes must share an in-flight request',
  );
  releaseStatus();
  await Promise.all([firstStatus, secondStatus]);

  const preWholeShopWriteCount = gridElement._innerHTMLWrites;
  const product03BeforeWholeShopKey = product03UiNode?.getAttribute('data-cloud-ui-key') || '';
  const product04BeforeWholeShopKey = product04UiNode?.getAttribute('data-cloud-ui-key') || '';
  context.fetch = async (url) => {
    assert.equal(url.includes('/api/cloud-assets/status'), true, 'Expected cloud status request for whole-shop UI refresh');
    return {
      ok: true,
      status: 200,
      async json() {
        return {
          ok: true,
          items: [
            { folder: 'product-03', state: 'CLOUD_ONLY', local_available: false, cloud_available: true },
            { folder: 'product-04', state: 'UPLOADING', upload_schedule: { status: 'running', delete_local: true } },
          ],
        };
      },
    };
  };
  await context.loadCloudAssetStatus({ force: true });
  assert.notEqual(product03UiNode?.getAttribute('data-cloud-ui-key'), product03BeforeWholeShopKey, 'Whole-shop status update should update in-place cloud widget for product-03');
  assert.notEqual(product04UiNode?.getAttribute('data-cloud-ui-key'), product04BeforeWholeShopKey, 'Whole-shop status update should update in-place cloud widget for product-04');
  assert.equal(gridElement._innerHTMLWrites, preWholeShopWriteCount, 'Whole-shop status update should not rewrite #product-grid');

  const nonAllFilterPreviousStatus = { state: 'READY_LOCAL', local_available: true, cloud_available: true };
  const nonAllBeforeKey = context.cloudAssetUiRenderKey(nonAllFilterPreviousStatus);
  const nonAllNode = setCloudUiNode('product-07', { key: nonAllBeforeKey });
  setCloudStatus('product-07', nonAllFilterPreviousStatus);
  context.document._cloudFilterEl.value = 'cloud-only';
  setCloudStatus('product-07', { state: 'CLOUD_ONLY', local_available: false, cloud_available: true });
  const nonAllCategoryBefore = nonAllNode?.getAttribute('data-cloud-ui-key');
  context.handleScopedCloudAssetStatusUpdate('product-07', nonAllFilterPreviousStatus);
  assert.notEqual(nonAllNode?.getAttribute('data-cloud-ui-key'), nonAllCategoryBefore, 'Category changes should update only cloud widget when filter != all');
  assert.equal(filterCalls, 0, 'Non-all filter category changes should not invoke filterProducts');
  context.document._cloudFilterEl.value = 'all';

  setCheckboxNode('product-05', true);
  setCheckboxNode('product-06', false);
  const expectedTerminalSelectionKey = context.catalogSelectionKey({
    dataset: { folder: 'product-05' },
    value: 'product-05',
  });
  setCloudStatus('product-05', { state: 'UPLOADING', upload_schedule: { status: 'running', delete_local: true } });
  setCloudStatus('product-04', { state: 'UPLOADING', upload_schedule: { status: 'running', delete_local: false } });
  const completionCalls = [];
  const terminalRefreshCalls = [];
  context.loadProducts = async () => {
    terminalRefreshCalls.push('loadProducts');
    context._checkboxNodes = [];
    setCheckboxNode('product-05', false);
    setCheckboxNode('product-06', false);
    return [];
  };
  const completionResponsesByFolder = {
    'product-05': [
      { state: 'UPLOADING', upload_schedule: { status: 'running', delete_local: true } },
      { state: 'CLOUD_ONLY', local_available: false, cloud_available: true },
    ],
    'product-04': [
      { state: 'UPLOADING', upload_schedule: { status: 'running', delete_local: false } },
      { state: 'READY_LOCAL', local_available: true, cloud_available: true },
    ],
  };
  const terminalPollIndex = { 'product-05': 0, 'product-04': 0 };
  context.fetch = async (url) => {
    completionCalls.push(url);
    const safeUrl = String(url);
    const folder = String(safeUrl.includes('folder=')
      ? safeUrl.split('folder=').pop().split('&')[0]
      : '')
      .trim();
    const responsesForFolder = completionResponsesByFolder[folder] || [];
    const responseIdx = terminalPollIndex[folder] || 0;
    terminalPollIndex[folder] = responseIdx + 1;
    const status = responsesForFolder[Math.min(responseIdx, responsesForFolder.length - 1)] || {};
    return {
      ok: true,
      status: 200,
      async json() { return { ok: true, items: [{ folder, ...status }] }; },
    };
  };
  await context.pollActiveCloudAssetStatuses();
  assert.equal(context.cloudAssetStatusForFolder('product-05').state, 'UPLOADING');
  await context.pollActiveCloudAssetStatuses();
  assert.equal(context.cloudAssetStatusForFolder('product-05').state, 'CLOUD_ONLY');
  await context.pollActiveCloudAssetStatuses();
  assert.equal(completionCalls.length, 4, 'A terminal cloud snapshot should stop scheduled polling');
  assert.equal(terminalRefreshCalls.length, 1, 'Terminal transition across multiple folders should coalesce to one catalog refresh');
  assert.equal(
    vm.runInContext(`selectedCatalogIds.has(${JSON.stringify(expectedTerminalSelectionKey)})`, context),
    true,
    'Terminal catalog refresh should preserve selected checkboxes through full refresh',
  );
  const refreshedCheckbox = context._checkboxNodes.find((checkbox) => checkbox.dataset.folder === 'product-05');
  assert.equal(refreshedCheckbox?.checked, true, 'Refreshed card checkbox should remain checked for preserved selection');
  assert.equal(filterCalls, 0, 'Unchanged scoped polls should not invoke full filter rerender');

  const repeatActiveCalls = [];
  context.fetch = async (url) => {
    repeatActiveCalls.push(url);
    return {
      ok: true,
      status: 200,
      async json() {
        return { ok: true, items: [{ folder: 'product-07', state: 'UPLOADING', upload_schedule: { status: 'running', delete_local: true } }] };
      },
    };
  };
  vm.runInContext(`cloudAssetStatusByFolder.set('product-07', { state: 'UPLOADING', upload_schedule: { status: 'running', delete_local: true } });`, context);
  await context.pollActiveCloudAssetStatuses();
  await context.pollActiveCloudAssetStatuses();
  assert.equal(repeatActiveCalls.length, 2, 'Repeated active polls should re-fetch active scoped statuses');
  assert.equal(filterCalls, 0, 'Unchanged scoped polls with same UI key must not invoke full filter rerender');

  context.loadCloudAssetStatus = async () => ({});
  const calls = [];
  const payloads = [];
  let releaseUpload;
  const uploadGate = new Promise((resolve) => {
    releaseUpload = resolve;
  });

  context.fetch = async (url, init = {}) => {
    calls.push(url);
    if (String(url).includes('/api/cloud-assets/schedule-upload-verify-offload')) {
      payloads.push(JSON.parse(init.body));
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
  const first = context.cloudAssetUploadAndOffload('product-01', firstButton);
  await new Promise((resolve) => setTimeout(resolve, 0));
  const second = context.cloudAssetUploadAndOffload('product-01', secondButton);

  assert.equal(calls.filter((u) => u.includes('/api/cloud-assets/schedule-upload-verify-offload')).length, 1);
  assert.deepEqual(payloads[0], {
    shop_id: 'templystudios',
    scope: 'shop',
    folder: 'product-01',
    delete_local: true,
    confirmed_product_key: 'shops/templystudios/product-01',
  });
  assert.equal(firstButton.disabled, true, 'Primary request should disable its button while in progress');
  assert.equal(secondButton.disabled, false, 'Deduped request should not enable a second button lock');

  releaseUpload();
  await Promise.all([first, second]);
  assert.equal(calls.filter((u) => u.includes('/api/cloud-assets/schedule-upload-verify-offload')).length, 1);
  assert.equal(calls.filter((u) => u.includes('/api/cloud-assets/verify')).length, 0);
  assert.equal(firstButton.disabled, false);
  assert.equal(secondButton.disabled, false);

  context.window.confirm = () => false;
  await context.cloudAssetUploadAndOffload('product-02', { disabled: false, innerHTML: 'upload' });
  assert.equal(calls.length, 1, 'Cancelling confirmation must not send a destructive request');

  context.window.confirm = undefined;
  await context.cloudAssetUploadAndOffload('product-03', { disabled: false, innerHTML: 'upload' });
  assert.equal(calls.length, 1, 'Unavailable confirmation must fail closed without a request');
})().then(() => {
  console.log('cloud assets UI tests passed');
}).catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
