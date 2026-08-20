'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {
  parseProductRow,
  findSavedFieldMismatches,
} = require('./dashboard_static/edit_save.js');

assert.equal(parseProductRow('33'), 33);
assert.equal(parseProductRow('0'), null);
assert.equal(parseProductRow('not-a-row'), null);
assert.equal(parseProductRow('2.5'), null);

const expected = {
  title: '',
  tags: 'reading journal, book tracker',
  keywords: 'reading journal',
  description: 'Description',
  price: 1.98,
  section: '',
  extra: '',
  etsy_url: 'https://www.etsy.com/ca/listing/4434249871/2026-digital-planner',
  sku: 'dd_product_30',
  qty: 898,
};
const saved = {
  ...expected,
  title: '[Cần SEO] product-30',
  section: 'Digital Planner',
  price: '1.98',
  qty: '898',
};

assert.deepEqual(findSavedFieldMismatches(expected, saved), []);
assert.deepEqual(
  findSavedFieldMismatches(expected, {
    ...saved,
    title: 'PLR Resell Digital Planner 115250858',
    needs_seo: true,
  }),
  [],
);
assert.deepEqual(
  findSavedFieldMismatches(expected, { ...saved, etsy_url: 'https://www.etsy.com/listing/old' }),
  [{
    field: 'etsy_url',
    expected: expected.etsy_url,
    actual: 'https://www.etsy.com/listing/old',
  }],
);
assert.deepEqual(
  findSavedFieldMismatches(expected, null),
  [{ field: 'row', expected: 'saved product', actual: 'missing' }],
);

const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');

function sourceSlice(startMarker, endMarker) {
  const start = appJs.indexOf(startMarker);
  const end = appJs.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `Unable to isolate ${startMarker}`);
  return appJs.slice(start, end);
}

async function testTimeoutHelperAborts() {
  const sandbox = {
    module: { exports: {} },
    AbortController,
    Error,
    Number,
    Promise,
    String,
    clearTimeout,
    setTimeout,
    fetch(_url, init) {
      return new Promise((resolve, reject) => {
        init.signal.addEventListener('abort', () => {
          const error = new Error('aborted');
          error.name = 'AbortError';
          reject(error);
        });
      });
    },
  };
  vm.runInNewContext(
    `${sourceSlice('class ProductSaveRequestTimeoutError', '\nfunction refreshProductEnrichmentsAfterSave')}\n` +
      'module.exports = { ProductSaveRequestTimeoutError, fetchJsonWithTimeout };',
    sandbox,
  );

  await assert.rejects(
    sandbox.module.exports.fetchJsonWithTimeout(
      '/api/products/259',
      { method: 'PATCH' },
      { timeoutMs: 5, phase: 'save-patch' },
    ),
    error => error.code === 'PRODUCT_SAVE_TIMEOUT'
      && error.phase === 'save-patch'
      && error.timeoutMs === 5,
  );
}

async function testLoadProductsCanSkipWholeShopRefreshes() {
  const calls = { aggregate: 0, cloud: 0, requests: [] };
  const sandbox = {
    module: { exports: {} },
    document: {
      getElementById() { return { innerHTML: '' }; },
    },
    async fetchJsonWithTimeout(url, init, options) {
      calls.requests.push({ url, init, options });
      return {
        response: { ok: true, status: 200 },
        data: { products: [{ row: 259, folder: 'product-259' }], etsy_manager: null },
      };
    },
    async loadAggregateCatalog() { calls.aggregate += 1; },
    async loadCloudAssetStatus() { calls.cloud += 1; },
    updateProductSourceSwitcher() {},
    setProductSource() {},
    updateStats() {},
    updateEtsyManagerStats() {},
    refreshScrollNavState() {},
  };
  vm.runInNewContext(
    'let allProducts = []; let etsyManagerSnapshot = null; let currentProductSource = "local";\n' +
      `${sourceSlice('async function loadProducts', '\nfunction formatJobDuration')}\n` +
      'module.exports = loadProducts;',
    sandbox,
  );

  const localProducts = await sandbox.module.exports({
    throwOnError: true,
    includeAggregateCatalog: false,
    includeCloudStatus: false,
    requestTimeoutMs: 10000,
    requestPhase: 'local-readback',
  });
  assert.equal(localProducts[0].folder, 'product-259');
  assert.equal(calls.aggregate, 0, 'Save read-back must not start the aggregate catalog');
  assert.equal(calls.cloud, 0, 'Save read-back must not start cloud status');
  assert.deepEqual(JSON.parse(JSON.stringify(calls.requests[0].options)), {
    timeoutMs: 10000,
    phase: 'local-readback',
  });

  await sandbox.module.exports();
  assert.equal(calls.aggregate, 1, 'Default initial load must still await aggregate catalog');
  assert.equal(calls.cloud, 1, 'Default initial load must still start cloud status');
}

function createSaveHarness({ patchTimeout = false, readbackTimeout = false } = {}) {
  const product = {
    row: 259,
    folder: 'product-259',
    title: 'Product 259 title',
    tags: 'planner, printable',
    keywords: 'planner',
    description: 'Saved description',
    price: 1.98,
    section: 'Digital Planner',
    extra: '',
    etsy_url: '',
    sku: 'product-259',
    qty: 999,
  };
  const elements = {
    'edit-save-btn': { disabled: false, innerHTML: '💾 Lưu' },
    'edit-row': { value: '259' },
    'edit-title': { value: product.title },
    'edit-tags': { value: product.tags },
    'edit-keywords': { value: product.keywords },
    'edit-description': { value: product.description },
    'edit-price': { value: String(product.price) },
    'edit-section': { value: product.section },
    'edit-extra': { value: product.extra },
    'edit-etsy-url': { value: product.etsy_url },
    'edit-sku': { value: product.sku },
    'edit-qty': { value: String(product.qty) },
  };
  const events = [];
  const toasts = [];
  const readbackOptions = [];
  let patchCalls = 0;
  let aggregateCalls = 0;
  let cloudCalls = 0;
  const never = new Promise(() => {});

  const sandbox = {
    module: { exports: {} },
    AbortController,
    Error,
    JSON,
    Math,
    Number,
    Promise,
    String,
    clearTimeout,
    setTimeout,
    console,
    ProductEditSave: { parseProductRow, findSavedFieldMismatches },
    document: {
      getElementById(id) { return elements[id] || null; },
    },
    async fetch(url, init) {
      patchCalls += 1;
      events.push(`fetch:${init.method}:${url}`);
      if (patchTimeout) {
        const error = new Error('timed out');
        error.code = 'PRODUCT_SAVE_TIMEOUT';
        error.phase = 'save-patch';
        error.timeoutMs = 15000;
        throw error;
      }
      return {
        ok: true,
        status: 200,
        async json() { return { ok: true }; },
      };
    },
    async loadProducts(options) {
      readbackOptions.push(options);
      events.push('local-readback');
      if (readbackTimeout) {
        const error = new Error('timed out');
        error.code = 'PRODUCT_SAVE_TIMEOUT';
        error.phase = 'local-readback';
        error.timeoutMs = 10000;
        throw error;
      }
      return [product];
    },
    loadAggregateCatalog() {
      aggregateCalls += 1;
      events.push('aggregate-background');
      events.push(`aggregate-button-disabled:${elements['edit-save-btn'].disabled}`);
      return never;
    },
    loadCloudAssetStatus() {
      cloudCalls += 1;
      events.push('cloud-background');
      return never;
    },
    getActiveShopId() { return 'templystudios'; },
    markModalClean(id) { events.push(`clean:${id}`); },
    closeModal(id) { events.push(`close:${id}`); },
    toast(level, message) { toasts.push({ level, message }); },
    updateCount() {},
    openModal() {},
  };

  vm.runInNewContext(
    `let allProducts = [${JSON.stringify(product)}]; let cloudAssetStatusError = '';\n` +
      `${sourceSlice('const PRODUCT_SAVE_PATCH_TIMEOUT_MS', '\n// ── SEO Auto-generate')}\n` +
      'module.exports = { saveEdit };',
    sandbox,
  );

  return {
    saveEdit: sandbox.module.exports.saveEdit,
    elements,
    events,
    toasts,
    readbackOptions,
    counts() { return { patchCalls, aggregateCalls, cloudCalls }; },
  };
}

async function testSaveCriticalPathAndButtonRestore() {
  const harness = createSaveHarness();
  await Promise.race([
    harness.saveEdit(),
    new Promise((_, reject) => setTimeout(() => reject(new Error('saveEdit stayed on Đang lưu...')), 100)),
  ]);

  assert.deepEqual(JSON.parse(JSON.stringify(harness.readbackOptions)), [{
    throwOnError: true,
    includeAggregateCatalog: false,
    includeCloudStatus: false,
    requestTimeoutMs: 10000,
    requestPhase: 'local-readback',
  }]);
  assert.equal(harness.elements['edit-save-btn'].disabled, false);
  assert.equal(harness.elements['edit-save-btn'].innerHTML, '💾 Lưu');
  assert.ok(harness.events.includes('close:edit-modal'), 'Verified save must close the modal');
  assert.deepEqual(harness.counts(), { patchCalls: 1, aggregateCalls: 1, cloudCalls: 1 });
  assert.ok(harness.events.indexOf('close:edit-modal') < harness.events.indexOf('aggregate-background'));
  assert.ok(harness.events.includes('aggregate-button-disabled:false'), 'Background refresh must start after button restoration');
  assert.ok(harness.toasts.some(item => item.level === 'success' && item.message.includes('kiểm tra lại thành công')));
}

async function testUncertainTimeoutMessagingAndButtonRestore() {
  const patchTimeout = createSaveHarness({ patchTimeout: true });
  await patchTimeout.saveEdit();
  assert.equal(patchTimeout.elements['edit-save-btn'].disabled, false);
  assert.equal(patchTimeout.elements['edit-save-btn'].innerHTML, '💾 Lưu');
  assert.equal(patchTimeout.counts().patchCalls, 1, 'Uncertain PATCH must not be retried automatically');
  assert.equal(patchTimeout.readbackOptions.length, 0);
  assert.ok(!patchTimeout.events.includes('close:edit-modal'));
  assert.ok(patchTimeout.toasts.some(item => item.level === 'warning'
    && item.message.includes('kết quả chưa chắc chắn')
    && item.message.includes('không tự gửi lại')));

  const readbackTimeout = createSaveHarness({ readbackTimeout: true });
  await readbackTimeout.saveEdit();
  assert.equal(readbackTimeout.elements['edit-save-btn'].disabled, false);
  assert.equal(readbackTimeout.elements['edit-save-btn'].innerHTML, '💾 Lưu');
  assert.equal(readbackTimeout.counts().patchCalls, 1, 'Read-back timeout must not resend PATCH');
  assert.ok(!readbackTimeout.events.includes('close:edit-modal'));
  assert.ok(readbackTimeout.toasts.some(item => item.level === 'warning'
    && item.message.includes('Server đã báo lưu')
    && item.message.includes('không tự gửi PATCH lại')));
}

(async () => {
  await testTimeoutHelperAborts();
  await testLoadProductsCanSkipWholeShopRefreshes();
  await testSaveCriticalPathAndButtonRestore();
  await testUncertainTimeoutMessagingAndButtonRestore();
  console.log('edit save tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
