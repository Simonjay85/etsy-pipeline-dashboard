'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');

const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');

const helpersStart = appJs.indexOf('function findAggregateLocalProduct');
const statsStart = appJs.indexOf('function updateEtsyManagerStats');
const loadAggregateCatalogStart = appJs.indexOf('async function loadAggregateCatalog');
const filterEnd = appJs.indexOf('function renderAggregateProducts', helpersStart);
const switcherStart = appJs.indexOf('function updateProductSourceSwitcher');
const filterStart = appJs.indexOf('function filterProducts', switcherStart);
const updateStatsStart = appJs.indexOf('function updateStats(products)');
if (
  helpersStart < 0 ||
  statsStart < 0 ||
  loadAggregateCatalogStart < 0 ||
  filterEnd <= helpersStart ||
  switcherStart < 0 ||
  filterStart < 0 ||
  updateStatsStart < loadAggregateCatalogStart
) {
  throw new Error('Cannot isolate helper functions from dashboard_static/app.js');
}

const getSyncableEtsyListings = (listings = []) => (listings || []).filter(Boolean);

function runInSandbox(code, sandbox = {}) {
  const vm = require('node:vm');
  const defaultSandbox = {
    module: { exports: {} },
    exports: {},
    console,
    CatalogOrdering: require('./dashboard_static/catalog_sort.js'),
    getSyncableEtsyListings,
    statusFilterMatches: () => true,
    cloudStatusFilterMatches: () => true,
    renderAggregateProducts: () => {},
    renderProducts: () => {},
    updateStats: () => {},
    refreshScrollNavState: () => {},
    document: {
      getElementById: () => null,
    },
    ...sandbox,
  };
  const ctx = vm.createContext(defaultSandbox);
  vm.runInContext(code, ctx);
  return { exports: ctx.module.exports, sandbox: defaultSandbox, context: ctx };
}

const helpersCode = `
  const allProducts = globalThis.__testAllProducts;
  const aggregateCatalog = globalThis.__testAggregateCatalog;
  const currentProductSource = globalThis.__testCurrentProductSource;
  const etsyManagerSnapshot = globalThis.__testEtsyManagerSnapshot || null;
  ${appJs.slice(statsStart, helpersStart)}
  ${appJs.slice(helpersStart, filterEnd)}
  module.exports = {
    getAggregateLocalRecords,
    findAggregateLocalProduct,
    aggregateDisplayProduct,
    filterProducts,
    updateProductSourceSwitcher,
    updateEtsyManagerStats,
  };
`;

const loadAggregateCatalogCode = `
  let allProducts = globalThis.__testAllProducts;
  let aggregateCatalog = globalThis.__testAggregateCatalog;
  const currentProductSource = globalThis.__testCurrentProductSource;
  let etsyManagerSnapshot = globalThis.__testEtsyManagerSnapshot || null;
  ${appJs.slice(loadAggregateCatalogStart, filterEnd)}
  module.exports = {
    loadAggregateCatalog,
    __getAggregateCatalog: () => aggregateCatalog,
  };
`;

const { getAggregateLocalRecords } = runInSandbox(helpersCode).exports;

const aggregateCatalogForFilter = {
  records: [
    { source: 'local', folder: 'product-111', exists: true, title: 'Local local', row: 111 },
    { source: 'both', folder: 'product-222', exists: true, title: 'Both both', row: 222 },
    { source: 'LOCAL', folder: 'product-333', exists: true, title: 'Case source', row: 333 },
    { source: 'both', folder: '', exists: true, title: 'Missing folder', row: 444 },
    { source: 'local', folder: 'product-missing', exists: false, title: 'Missing files', row: 555 },
    { source: 'etsy', folder: 'etsy-folder', exists: true, title: 'Etsy-only', listing_id: '12' },
  ],
};

assert.deepEqual(
  getAggregateLocalRecords(aggregateCatalogForFilter).map(record => record.source),
  ['local', 'both', 'LOCAL'],
);
assert.deepEqual(
  getAggregateLocalRecords(aggregateCatalogForFilter).map(record => record.folder),
  ['product-111', 'product-222', 'product-333'],
);

const reconciliationContext = runInSandbox(helpersCode, {
  __testAllProducts: [{ row: 111, folder: 'product-111', status: '✅ Đã đăng draft' }],
  __testAggregateCatalog: aggregateCatalogForFilter,
  __testCurrentProductSource: 'local',
  __testEtsyManagerSnapshot: {},
});
const unmatchedDisplay = reconciliationContext.exports.aggregateDisplayProduct(
  { row: 111, folder: 'product-111', reconciliation_status: 'unmatched_local_listing' },
);
assert.equal(unmatchedDisplay.status, '⏳ Chờ đăng · Chưa khớp snapshot Etsy');

const createElement = (value = '') => ({ value, textContent: value, disabled: false, dataset: {}, style: {}, classList: { toggle(){}, remove(){} } });
const createSwitcherDocument = (aggregateEnabled = true) => {
  const localOption = createElement('');
  const shopOption = createElement('');
  const aggregateOption = createElement('');
  const selector = {
    querySelector: (selector) => {
      if (selector === 'option[value="local"]') return localOption;
      if (selector === 'option[value="shop"]') return shopOption;
      if (selector === 'option[value="aggregate"]') return aggregateEnabled ? aggregateOption : null;
      return null;
    },
  };
  return {
    localOption,
    shopOption,
    aggregateOption,
    selector,
    getElementById: (id) => {
      if (id !== 'product-source-select') return { querySelector: () => null };
      return selector;
    },
  };
};

const switcherDoc = createSwitcherDocument(true);
const documentForSwitcher = {
  getElementById: (id) => {
    if (id !== 'product-source-select') return { querySelector: () => null };
    return switcherDoc.selector;
  },
};

const switcherContext = runInSandbox(helpersCode, {
  __testAllProducts: [{ row: 1, folder: 'product-01', status: '✅ Đã đăng' }],
  __testAggregateCatalog: aggregateCatalogForFilter,
  __testCurrentProductSource: 'local',
  __testEtsyManagerSnapshot: {},
  document: documentForSwitcher,
});

const { updateProductSourceSwitcher } = switcherContext.exports;

updateProductSourceSwitcher();
assert.equal(switcherDoc.localOption.textContent, '📁 Sản phẩm local (3)');

const fallbackSwitcherDoc = createSwitcherDocument(false);
const fallbackContext = runInSandbox(helpersCode, {
  __testAllProducts: [{ row: 1, folder: 'product-01', status: '✅ Đã đăng' }],
  __testAggregateCatalog: null,
  __testCurrentProductSource: 'local',
  __testEtsyManagerSnapshot: {},
  document: {
    getElementById: (id) => {
      if (id !== 'product-source-select') return { querySelector: () => null };
      return fallbackSwitcherDoc.selector;
    },
  },
});
const updateProductSourceSwitcherNoAgg = fallbackContext.exports.updateProductSourceSwitcher;
updateProductSourceSwitcherNoAgg();
assert.equal(fallbackSwitcherDoc.localOption.textContent, '📁 Sản phẩm local (1)');

const aggregateStatsCatalog = {
  records: [
    { source: 'local', folder: 'product-a', exists: true, title: 'A', row: 11 },
    { source: 'both', folder: 'product-b', exists: true, title: 'B', row: 22 },
    { source: 'LOCAL', folder: 'product-c', exists: true, title: 'C', row: 33 },
    { source: 'etsy', folder: 'etsy-only', exists: true, title: 'Only Etsy', listing_id: '12' },
  ],
};

const strip = { innerHTML: '', style: { display: '' } };
const { updateEtsyManagerStats } = runInSandbox(helpersCode, {
  __testAllProducts: [
    { row: 11, folder: 'product-a' },
    { row: 22, folder: 'product-b' },
  ],
  __testAggregateCatalog: aggregateStatsCatalog,
  __testCurrentProductSource: 'aggregate',
  __testEtsyManagerSnapshot: { counts: { active: 1, draft: 0 } },
  document: {
    getElementById: (id) => {
      if (id === 'etsy-sync-strip') return strip;
      return { value: '' };
    },
  },
}).exports;

updateEtsyManagerStats({ counts: { active: 1, draft: 0 } });
assert.equal(strip.style.display, 'flex');
assert.equal(
  strip.innerHTML.includes('Có 3 folder local · 1 đã ghép Etsy · 2 chưa ghép.'),
  true,
);

updateEtsyManagerStats({ counts: { active: 1, draft: 2, inactive: 3, expired: 4, total: 10 } });
assert.equal(strip.innerHTML.includes('Inactive 3'), true);
assert.equal(strip.innerHTML.includes('Expired 4'), true);
assert.equal(strip.innerHTML.includes('Tổng Etsy 10'), true);

const fallbackStrip = { innerHTML: '', style: { display: '' } };
const { updateEtsyManagerStats: updateEtsyManagerStatsFallback } = runInSandbox(helpersCode, {
  __testAllProducts: [{ row: 1, folder: 'local-only' }],
  __testAggregateCatalog: null,
  __testCurrentProductSource: 'local',
  __testEtsyManagerSnapshot: { counts: { active: 1, draft: 0 } },
  document: {
    getElementById: () => fallbackStrip,
  },
}).exports;
updateEtsyManagerStatsFallback({ counts: { active: 1, draft: 0 } });
assert.equal(
  fallbackStrip.innerHTML.includes('Dashboard đang hiển thị 1 dòng local đang quản lý.'),
  true,
);

const rendered = [];
const filterSandbox = runInSandbox(helpersCode, {
  __testAllProducts: [
    { row: 111, folder: 'product-111', status: '✅ Đã đăng', missing_fields: ['title'] },
  ],
  __testAggregateCatalog: aggregateCatalogForFilter,
  __testCurrentProductSource: 'local',
  __testEtsyManagerSnapshot: {},
  document: {
    getElementById: (id) => {
      if (id === 'search') return { value: '' };
      if (id === 'filter-cloud-status') return { value: 'all' };
      if (id === 'filter-status') return { value: '' };
      if (id === 'product-grid') return { innerHTML: '', innerText: '' };
      return { value: '' };
    },
  },
  renderAggregateProducts: (records) => { rendered.push(...records); },
  updateStats: () => {},
});
const { filterProducts } = filterSandbox.exports;
filterProducts();
assert.deepEqual(rendered.map(record => record.folder).sort(), ['product-111', 'product-222', 'product-333']);

const unavailableMessage = '⚠️ Catalog tổng tạm thời không khả dụng. Hãy tải lại hoặc kiểm tra Live Logs.';

const aggregateFailureResponse = {
  ok: false,
  status: 503,
  json: async () => ({ detail: 'catalog unavailable' }),
};

const createAggregateFailureDoc = () => {
  const localOption = createElement('');
  const shopOption = createElement('');
  const aggregateOption = createElement('');
  const selector = {
    querySelector: (selector) => {
      if (selector === 'option[value="local"]') return localOption;
      if (selector === 'option[value="shop"]') return shopOption;
      if (selector === 'option[value="aggregate"]') return aggregateOption;
      return null;
    },
  };
  const syncStrip = { innerHTML: '', style: { display: '' } };
  const grid = { innerHTML: '' };
  const docs = {
    localOption,
    shopOption,
    aggregateOption,
    selector,
    syncStrip,
    grid,
    statTotal: { textContent: '' },
    statPosted: { textContent: '' },
    statPending: { textContent: '' },
    statError: { textContent: '' },
    getElementById: (id) => {
      if (id === 'product-source-select') return selector;
      if (id === 'etsy-sync-strip') return syncStrip;
      if (id === 'product-grid') return grid;
      if (id === 'search') return { value: '' };
      if (id === 'filter-status') return { value: '' };
      if (id === 'filter-cloud-status') return { value: 'all' };
      if (id === 'stat-total') return docs.statTotal;
      if (id === 'stat-posted') return docs.statPosted;
      if (id === 'stat-pending') return docs.statPending;
      if (id === 'stat-error') return docs.statError;
      return { value: '' };
    },
  };
  return docs;
};

(async () => {
  const localRendered = [];
  const localFailureDoc = createAggregateFailureDoc();
  const localSourceContext = runInSandbox(loadAggregateCatalogCode, {
    __testAllProducts: [{ row: 1, folder: 'product-01', status: '✅ Đã đăng', tags: 'tag1' }],
    __testAggregateCatalog: { records: [{ source: 'local', folder: 'product-01', exists: true, title: 'Old catalog' }] },
    __testCurrentProductSource: 'local',
    __testEtsyManagerSnapshot: { counts: { active: 1, draft: 0 }, listings: [] },
    document: {
      ...localFailureDoc,
      getElementById: (id) => {
        if (id === 'renderProductList') return { innerText: '' };
        if (id === 'search') return { value: '' };
        if (id === 'filter-status') return { value: '' };
        if (id === 'filter-cloud-status') return { value: 'all' };
        if (id === 'product-grid') return localFailureDoc.grid;
        if (id === 'product-source-select') return localFailureDoc.selector;
        if (id === 'etsy-sync-strip') return localFailureDoc.syncStrip;
        if (id === 'stat-total') return localFailureDoc.statTotal;
        if (id === 'stat-posted') return localFailureDoc.statPosted;
        if (id === 'stat-pending') return localFailureDoc.statPending;
        if (id === 'stat-error') return localFailureDoc.statError;
        return { value: '' };
      },
    },
    fetch: async () => aggregateFailureResponse,
    renderProducts: (records) => {
      localRendered.push(...records);
    },
  }).exports;
  const { loadAggregateCatalog: loadAggregateCatalogLocal, __getAggregateCatalog: getAggregateCatalogLocal } = localSourceContext;
  await loadAggregateCatalogLocal();
  assert.equal(getAggregateCatalogLocal(), null);
  assert.equal(localFailureDoc.localOption.textContent.includes('(1)'), true);
  assert.equal(localFailureDoc.syncStrip.innerHTML.includes('Dashboard đang hiển thị 1 dòng local đang quản lý.'), true);
  assert.equal(localRendered.length, 1);

  const aggregateSourceDoc = createAggregateFailureDoc();
  const aggregateSourceContext = runInSandbox(loadAggregateCatalogCode, {
    __testAllProducts: [{ row: 1, folder: 'product-01', status: '✅ Đã đăng', tags: 'tag1' }],
    __testAggregateCatalog: { records: [{ source: 'local', folder: 'product-01', exists: true, title: 'Old catalog' }] },
    __testCurrentProductSource: 'aggregate',
    __testEtsyManagerSnapshot: { counts: { active: 1, draft: 0 }, listings: [] },
    document: aggregateSourceDoc,
    fetch: async () => aggregateFailureResponse,
  }).exports;
  await aggregateSourceContext.loadAggregateCatalog();
  assert.equal(aggregateSourceDoc.grid.innerHTML.includes(unavailableMessage), true);
  assert.equal(aggregateSourceDoc.aggregateOption.textContent.includes('(0)'), true);
  assert.equal(aggregateSourceDoc.aggregateOption.disabled, true);
  assert.equal(aggregateSourceDoc.statTotal.textContent, 0);
  assert.equal(aggregateSourceDoc.syncStrip.innerHTML.includes('Dashboard đang hiển thị 1 dòng local đang quản lý.'), true);

  let thrown = false;
  const throwingDoc = createAggregateFailureDoc();
  let throwingContext = null;
  try {
    throwingContext = runInSandbox(loadAggregateCatalogCode, {
      __testAllProducts: [{ row: 1, folder: 'product-01', status: '✅ Đã đăng', tags: 'tag1' }],
      __testAggregateCatalog: { records: [{ source: 'local', folder: 'product-01', exists: true, title: 'Old catalog' }] },
      __testCurrentProductSource: 'aggregate',
      __testEtsyManagerSnapshot: { counts: { active: 1, draft: 0 }, listings: [] },
      document: throwingDoc,
      fetch: async () => aggregateFailureResponse,
    }).exports;
    await throwingContext.loadAggregateCatalog({ throwOnError: true });
  } catch (error) {
    thrown = true;
    assert.equal(error.message.includes('catalog'), true);
    assert.equal(throwingDoc.grid.innerHTML.includes(unavailableMessage), true);
    assert.equal(throwingContext.__getAggregateCatalog(), null);
  }
  assert.equal(thrown, true);

console.log('local source aggregate UI regression tests passed');
})();
