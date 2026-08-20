'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');
const changeStatusStart = appJs.indexOf('async function changeStatus');
const changeStatusEnd = appJs.indexOf('// Close status menu', changeStatusStart);
assert.ok(
  changeStatusStart >= 0 && changeStatusEnd > changeStatusStart,
  'Unable to isolate changeStatus implementation',
);

const baseProduct = {
  row: 10,
  folder: 'product-10',
  status: '✅ Đã đăng',
  image_count: 0,
  pdf_count: 0,
  price: 4.99,
  missing_fields: [],
  social_statuses: {},
};

function runChangeStatusHarness(fetchResult, options = {}) {
  const productCopy = JSON.parse(JSON.stringify(baseProduct));
  const calls = {
    toasts: [],
    loadProducts: 0,
    refreshCard: 0,
    updateStats: 0,
    menuDisplay: [],
    loadProductsArgs: [],
  };

  const menuState = { style: { display: '' } };
  const loadProductsResult = Object.prototype.hasOwnProperty.call(options, 'loadProductsResult')
    ? options.loadProductsResult
    : [productCopy];
  const context = {
    module: { exports: {} },
    __calls: calls,
    loadProductsShouldThrow: Boolean(options.loadProductsShouldThrow),
    loadProductsResult,
    fetchJsonWithTimeout: async () => {
      if (options.shouldThrowFetch) {
        throw new Error('fetch transport error');
      }
      return fetchResult;
    },
    document: {
      getElementById(id) {
        calls.menuDisplay.push(id);
        if (id === 'smenu-10') return menuState;
        if (id === 'card-10') return {};
        return null;
      },
      querySelectorAll() {
        return [];
      },
    },
  };

  vm.runInNewContext(`
    let allProducts = ${JSON.stringify([productCopy])};
    const __calls = globalThis.__calls;
    function toast(type, msg) {
      __calls.toasts.push({ type, msg });
    }
    function updateStats() {
      __calls.updateStats += 1;
    }
    async function loadProducts() {
      __calls.loadProductsArgs.push(arguments[0] || {});
      __calls.loadProducts += 1;
      if (globalThis.loadProductsShouldThrow) throw new Error('refresh failed');
      if (Array.isArray(globalThis.loadProductsResult) && globalThis.loadProductsResult[0] && allProducts[0]) {
        Object.assign(allProducts[0], globalThis.loadProductsResult[0]);
        return allProducts;
      }
      return globalThis.loadProductsResult;
    }
    function productCard() {
      return '';
    }
    function refreshCard() {
      __calls.refreshCard += 1;
    }
    ${appJs.slice(changeStatusStart, changeStatusEnd)}
    module.exports = { changeStatus, allProducts };
  `, context);

  return { calls, context };
}

async function runChangeStatusScenarios() {
  // Thành công: response OK && data.ok true -> cập nhật status và làm mới card.
  const harnessSuccess = runChangeStatusHarness({
    response: { ok: true, status: 200 },
    data: { ok: true },
  });
  await harnessSuccess.context.module.exports.changeStatus(10, 'product-10', '✅ Đã đăng draft');
  const successProduct = harnessSuccess.context.module.exports.allProducts[0];
  assert.equal(successProduct.status, '✅ Đã đăng draft');
  assert.equal(harnessSuccess.calls.loadProducts, 0);
  assert.equal(harnessSuccess.calls.updateStats, 1);

  // Thất bại nếu response không OK: giữ nguyên trạng thái local và gọi reload readback.
  const harnessResponseError = runChangeStatusHarness({
    response: { ok: false, status: 409 },
    data: { ok: false, error: 'Catalog changed; refresh before retrying' },
  });
  await harnessResponseError.context.module.exports.changeStatus(10, 'product-10', '✅ Đã đăng draft');
  const responseErrorProduct = harnessResponseError.context.module.exports.allProducts[0];
  assert.equal(responseErrorProduct.status, '✅ Đã đăng');
  assert.equal(harnessResponseError.calls.loadProducts, 1);
  assert.equal(harnessResponseError.calls.loadProductsArgs[0]?.throwOnError, true);
  assert.equal(harnessResponseError.calls.toasts.some((item) => item.type === 'error'), true);
  assert.equal(harnessResponseError.calls.updateStats, 0);

  // Thất bại nếu data.ok === false dù HTTP OK: cũng không ghi đè trạng thái local.
  const harnessDataError = runChangeStatusHarness({
    response: { ok: true, status: 200 },
    data: { ok: false, error: 'Catalog update failed safely' },
  });
  await harnessDataError.context.module.exports.changeStatus(10, 'product-10', '✅ Đã đăng draft');
  const dataErrorProduct = harnessDataError.context.module.exports.allProducts[0];
  assert.equal(dataErrorProduct.status, '✅ Đã đăng');
  assert.equal(harnessDataError.calls.loadProducts, 1);
  assert.equal(harnessDataError.calls.loadProductsArgs[0]?.throwOnError, true);
  assert.equal(harnessDataError.calls.toasts.some((item) => item.type === 'error'), true);

  // Thất bại HTTP và read-back trả về mảng hợp lệ: trạng thái phải được đổi theo dữ liệu authoritative.
  const harnessReadbackSuccess = runChangeStatusHarness({
    response: { ok: false, status: 409 },
    data: { ok: false, error: 'Catalog changed; refresh before retrying' },
  }, {
    loadProductsResult: [{ ...baseProduct, row: 10, folder: 'product-10', status: '✅ Đã đăng' }],
  });
  await harnessReadbackSuccess.context.module.exports.changeStatus(10, 'product-10', '✅ Đã đăng draft');
  const readbackSuccessProduct = harnessReadbackSuccess.context.module.exports.allProducts[0];
  assert.equal(readbackSuccessProduct.status, '✅ Đã đăng');
  assert.equal(harnessReadbackSuccess.calls.loadProducts, 1);
  assert.equal(harnessReadbackSuccess.calls.loadProductsArgs[0]?.throwOnError, true);
  assert.equal(harnessReadbackSuccess.calls.refreshCard, 0);
  assert.equal(harnessReadbackSuccess.calls.toasts.some((item) => item.type === 'error'), true);

  // Lỗi transport gọi PATCH: phải fallback read-back thành công.
  const harnessTransportError = runChangeStatusHarness({
    response: { ok: true, status: 200 },
    data: { ok: true },
  }, {
    shouldThrowFetch: true,
    loadProductsResult: [{ ...baseProduct, row: 10, folder: 'product-10', status: '✅ Đã đăng draft' }],
  });
  await harnessTransportError.context.module.exports.changeStatus(10, 'product-10', '✅ Đã đăng draft');
  const transportErrorProduct = harnessTransportError.context.module.exports.allProducts[0];
  assert.equal(transportErrorProduct.status, '✅ Đã đăng draft');
  assert.equal(harnessTransportError.calls.loadProducts, 1);
  assert.equal(harnessTransportError.calls.loadProductsArgs[0]?.throwOnError, true);
  assert.equal(harnessTransportError.calls.refreshCard, 0);
  assert.equal(harnessTransportError.calls.toasts.some((item) => item.type === 'error'), true);

  // Read-back trả về không phải mảng: fallback sang refreshCard.
  const harnessReadbackNonArray = runChangeStatusHarness({
    response: { ok: false, status: 409 },
    data: { ok: false, error: 'Catalog changed; refresh before retrying' },
  }, {
    loadProductsResult: {},
  });
  await harnessReadbackNonArray.context.module.exports.changeStatus(10, 'product-10', '✅ Đã đăng draft');
  const nonArrayProduct = harnessReadbackNonArray.context.module.exports.allProducts[0];
  assert.equal(nonArrayProduct.status, '✅ Đã đăng');
  assert.equal(harnessReadbackNonArray.calls.loadProducts, 1);
  assert.equal(harnessReadbackNonArray.calls.loadProductsArgs[0]?.throwOnError, true);
  assert.equal(harnessReadbackNonArray.calls.refreshCard, 1);
  assert.equal(harnessReadbackNonArray.calls.toasts.some((item) => item.type === 'error'), true);

  // Read-back bị lỗi transport: fallback refreshCard.
  const harnessReadbackTransportFailure = runChangeStatusHarness({
    response: { ok: false, status: 409 },
    data: { ok: false, error: 'Catalog changed; refresh before retrying' },
  }, {
    loadProductsShouldThrow: true,
    loadProductsResult: [{ ...baseProduct, row: 10, folder: 'product-10', status: '✅ Đã đăng' }],
  });
  await harnessReadbackTransportFailure.context.module.exports.changeStatus(10, 'product-10', '✅ Đã đăng draft');
  const readbackTransportFailureProduct = harnessReadbackTransportFailure.context.module.exports.allProducts[0];
  assert.equal(readbackTransportFailureProduct.status, '✅ Đã đăng');
  assert.equal(harnessReadbackTransportFailure.calls.loadProducts, 1);
  assert.equal(harnessReadbackTransportFailure.calls.loadProductsArgs[0]?.throwOnError, true);
  assert.equal(harnessReadbackTransportFailure.calls.refreshCard, 1);
  assert.equal(harnessReadbackTransportFailure.calls.toasts.some((item) => item.type === 'error'), true);
}

runChangeStatusScenarios().catch((error) => {
  throw error;
});

console.log('changeStatus UI tests passed');
