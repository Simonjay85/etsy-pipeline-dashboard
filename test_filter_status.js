'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');

function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`Cannot find ${name} in dashboard_static/app.js`);
  const bodyStart = source.indexOf('{', start);
  let depth = 0;
  for (let index = bodyStart; index < source.length; index += 1) {
    const char = source[index];
    if (char === '{') depth += 1;
    if (char === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`Cannot isolate ${name} from dashboard_static/app.js`);
}

function createElementWithClassList() {
  const classes = new Set();
  return {
    value: '',
    disabled: false,
    dataset: {},
    textContent: '',
    __attrs: {},
    classList: {
      add: className => classes.add(className),
      remove: className => classes.delete(className),
      toggle: (className, force) => {
        if (typeof force === 'boolean') {
          if (force) classes.add(className);
          else classes.delete(className);
          return force;
        }
        if (classes.has(className)) {
          classes.delete(className);
          return false;
        }
        classes.add(className);
        return true;
      },
      contains: className => classes.has(className),
      values: () => [...classes],
    },
    setAttribute(name, value) {
      if (name === 'disabled') {
        this.disabled = String(value) === 'true';
        return;
      }
      this.__attrs[name] = String(value);
    },
    getAttribute(name) {
      if (name === 'disabled') return this.disabled ? 'true' : null;
      return this.__attrs[name] ?? null;
    },
  };
}

const helperNames = [
  'isStatusFilterDisabledForSource',
  'findAggregateLocalProduct',
  'classifyProductStatus',
  'summarizeProductStatuses',
  'normalizeEtsyStatus',
  'statusFilterMatches',
  'syncStatusSummaryButtons',
  'scrollProductSectionIntoView',
  'onStatusFilterChange',
  'onStatusSummaryClick',
];

const helperSource = helperNames.map(name => extractFunction(appJs, name)).join('\n');

const products = [
  { row: 12, folder: 'product-12', status: '⚠️ Thiếu Title', missing_fields: ['title'], etsy_status: 'draft' },
  { row: 13, folder: 'product-13', status: '⏳ Chờ đăng', missing_fields: [], etsy_status: 'active' },
  { row: 14, folder: 'product-14', status: '⚠️ Lỗi', missing_fields: ['tags'], etsy_status: 'active' },
];

const statButtonElements = {
  total: createElementWithClassList(),
  posted: createElementWithClassList(),
  pending: createElementWithClassList(),
  error: createElementWithClassList(),
  other: createElementWithClassList(),
};
statButtonElements.total.dataset.statusFilter = '';
statButtonElements.posted.dataset.statusFilter = 'posted';
statButtonElements.pending.dataset.statusFilter = 'pending';
statButtonElements.error.dataset.statusFilter = 'error';
statButtonElements.other.dataset.statusFilter = 'other';

const filterStatus = createElementWithClassList();
filterStatus.value = '';
const testWindow = {
  innerHeight: 500,
  __filterCalls: 0,
  __scrollIntoView: false,
  __scrollIntoViewCount: 0,
  matchMedia: () => ({ matches: true }),
};
const productSection = {
  getBoundingClientRect: () => ({ top: -40, bottom: 40 }),
  scrollIntoView: () => {
    testWindow.__scrollIntoView = true;
    testWindow.__scrollIntoViewCount += 1;
  },
};

const domElements = {
  'filter-status': filterStatus,
  'search': { value: '' },
  'filter-cloud-status': { value: 'all' },
  'product-section': productSection,
  'stat-card-total': statButtonElements.total,
  'stat-card-posted': statButtonElements.posted,
  'stat-card-pending': statButtonElements.pending,
  'stat-card-error': statButtonElements.error,
  'stat-card-other': statButtonElements.other,
};

const sandbox = {
  module: { exports: {} },
  exports: {},
  document: {
    getElementById(id) {
      return domElements[id];
    },
    querySelectorAll(selector) {
      if (selector !== '.stat-filter-btn') return [];
      return Object.values(statButtonElements);
    },
  },
  window: testWindow,
  reducedMotionQuery: { matches: true },
  allProducts: products,
  currentProductSource: 'local',
};

vm.runInNewContext(
  `
  var currentProductSource = globalThis.currentProductSource;
  var __filterCalls = 0;
  function filterProducts() {
    window.__filterCalls = (window.__filterCalls || 0) + 1;
  }
  ${helperSource}
  module.exports = {
    isStatusFilterDisabledForSource,
    findAggregateLocalProduct,
    classifyProductStatus,
    summarizeProductStatuses,
    statusFilterMatches,
    syncStatusSummaryButtons,
    onStatusFilterChange,
    onStatusSummaryClick,
    scrollProductSectionIntoView,
  };
`,
  sandbox,
);

const {
  isStatusFilterDisabledForSource,
  findAggregateLocalProduct,
  classifyProductStatus,
  summarizeProductStatuses,
  statusFilterMatches,
  syncStatusSummaryButtons,
  onStatusFilterChange,
  onStatusSummaryClick,
  scrollProductSectionIntoView,
} = sandbox.module.exports;

// Existing status mapping behavior
assert.equal(statusFilterMatches({ status: '✅ Đã đăng', missing_fields: ['title'] }, 'posted'), true);
assert.equal(statusFilterMatches({ status: '✅ Đã đăng draft', missing_fields: [] }, 'posted'), false);
assert.equal(statusFilterMatches({ status: 'draft', missing_fields: ['title'] }, 'draft'), true);
assert.equal(statusFilterMatches({ status: 'draft', missing_fields: [] }, 'pending'), false);
assert.equal(statusFilterMatches({ status: '⏳ Chờ đăng', missing_fields: [] }, 'pending'), true);
assert.equal(statusFilterMatches({ source: 'etsy', status: 'active', etsy_status: 'active', missing_fields: [] }, 'posted'), true);
assert.equal(statusFilterMatches({ status: 'active', missing_fields: [] }, 'draft'), false);
assert.equal(statusFilterMatches({ status: '❌ Lỗi', missing_fields: [] }, 'error'), true);
assert.equal(statusFilterMatches({ status: '⚠️ Lỗi provider', missing_fields: [] }, 'error'), true);

// New other-filter behavior
assert.equal(statusFilterMatches({ status: 'Chờ bổ sung', missing_fields: [] }, 'other'), true);
assert.equal(statusFilterMatches({ status: '✅ Đã đăng', missing_fields: [] }, 'other'), false);
assert.equal(classifyProductStatus('Chờ bổ sung'), 'other');
[
  { status: 'active', etsy_status: 'active' },
  { status: 'draft', etsy_status: 'draft' },
  { status: '⚠️ Chờ đăng', etsy_status: 'pending' },
  { status: '❌ Lỗi', etsy_status: 'error' },
].forEach(record => {
  assert.equal(
    statusFilterMatches({ source: 'etsy', ...record }, 'other'),
    false,
    `Etsy-only ${record.etsy_status} record must not match other`,
  );
});
assert.equal(statusFilterMatches({ source: 'etsy', status: 'inactive', etsy_status: 'inactive' }, 'other'), true);
assert.equal(statusFilterMatches({ source: 'etsy', status: 'expired', etsy_status: 'expired' }, 'other'), true);
assert.equal(statusFilterMatches({ source: 'both', status: 'active', etsy_status: 'active' }, 'other'), true);
assert.equal(statusFilterMatches({ status: 'active', etsy_status: 'active' }, 'other'), true);
assert.deepEqual(
  { ...summarizeProductStatuses([
    { source: 'etsy', status: 'active', etsy_status: 'active' },
    { source: 'etsy', status: 'draft', etsy_status: 'draft' },
    { source: 'etsy', status: 'pending', etsy_status: 'pending' },
    { source: 'etsy', status: 'error', etsy_status: 'error' },
    { source: 'etsy', status: 'inactive', etsy_status: 'inactive' },
  ]) },
  { total: 5, posted: 1, pending: 1, error: 1, other: 1 },
  'aggregate summary buckets should match the quick-filter semantics; drafts use the dropdown filter',
);

assert.equal(statusFilterMatches({ status: '⏳ Chờ đăng · Cũ', is_new_import: false, missing_fields: [] }, 'new_import'), false);
assert.equal(statusFilterMatches({ status: '🆕 Mới import · ⏳ Chờ đăng', missing_fields: [] }, 'new_import'), true);
assert.equal(statusFilterMatches({ status: '⏳ Chờ đăng', missing_fields: [] }, 'new_import'), false);
assert.equal(statusFilterMatches({ source: 'etsy', is_new_import: true, status: '🆕 Mới import · active' }, 'new_import'), false);
assert.equal(statusFilterMatches({ source: 'etsy', is_new_import: false, status: '🆕 Mới import · active' }, 'new_import'), false);
assert.equal(statusFilterMatches({ status: '🆕 Mới import · ⏳ Chờ đăng', missing_fields: [] }, 'pending'), true);
assert.equal(statusFilterMatches({ status: '🆕 Mới import · ✅ Đã đăng', missing_fields: [] }, 'posted'), true);
assert.equal(statusFilterMatches({ status: '⚠️ Thiếu Title', missing_fields: ['title'] }, 'missing_title'), true);
assert.equal(statusFilterMatches({ status: '✅ Đã đăng', missing_fields: ['title'] }, 'missing_title'), true);
assert.equal(statusFilterMatches({ status: '✅ Đã đăng', missing_fields: ['description'] }, 'missing_description'), true);
assert.equal(statusFilterMatches({ status: '✅ Đã đăng', missing_fields: ['tags_count'] }, 'missing_tags'), true);
assert.equal(statusFilterMatches({ status: '✅ Đã đăng', missing_fields: ['title', 'description'] }, 'missing_seo'), true);
assert.equal(statusFilterMatches({ status: '✅ Đã đăng', missing_fields: [] }, 'missing_seo'), false);
assert.equal(statusFilterMatches({ source: 'etsy', etsy_status: 'draft', missing_fields: ['tags'] }, 'draft'), true);
assert.equal(statusFilterMatches({ etsy_status: 'active', missing_fields: ['tags'] }, 'missing_tags'), true);
assert.equal(statusFilterMatches({ status: 'active', missing_fields: [] }, 'missing_tags'), false);
assert.equal(statusFilterMatches({ status: '✅ Đã đăng', missing_fields: [] }, 'unknown_filter'), false);

// Etsy-only aggregate records should not be considered missing_* despite local-like status names.
assert.equal(statusFilterMatches({ source: 'etsy', status: 'active', etsy_status: 'active' }, 'posted'), true);
assert.equal(statusFilterMatches({ source: 'etsy', status: 'draft', etsy_status: 'draft' }, 'draft'), true);
assert.equal(statusFilterMatches({ source: 'etsy', status: 'active', etsy_status: 'active', missing_fields: ['title'] }, 'missing_title'), false);

// Filter status enable/disable by source
assert.equal(isStatusFilterDisabledForSource('shop'), true);
assert.equal(isStatusFilterDisabledForSource('local'), false);
assert.equal(isStatusFilterDisabledForSource('aggregate'), false);

assert.deepEqual(findAggregateLocalProduct({ row: 12, folder: 'product-12' }), products[0]);
assert.deepEqual(findAggregateLocalProduct({ row: 999, folder: 'product-13' }), products[1]);
assert.equal(findAggregateLocalProduct({ row: 999, folder: 'missing-folder' }), null);

// Quick-summary card active-state and click wiring
syncStatusSummaryButtons();
assert.equal(statButtonElements.total.getAttribute('aria-pressed'), 'true');
assert.equal(statButtonElements.error.getAttribute('aria-pressed'), 'false');

filterStatus.value = 'error';
onStatusFilterChange();
assert.equal(testWindow.__filterCalls, 1);
assert.equal(filterStatus.value, 'error');
assert.equal(statButtonElements.error.getAttribute('aria-pressed'), 'true');
assert.equal(statButtonElements.total.getAttribute('aria-pressed'), 'false');

testWindow.__filterCalls = 0;
onStatusSummaryClick('pending');
assert.equal(filterStatus.value, 'pending');
assert.equal(testWindow.__filterCalls, 1);
assert.equal(statButtonElements.pending.getAttribute('aria-pressed'), 'true');
assert.equal(statButtonElements.other.getAttribute('aria-pressed'), 'false');

// Source=shop disables status summary buttons except total and blocks applying unavailable filter state
testWindow.__filterCalls = 0;
vm.runInNewContext('currentProductSource = "shop";', sandbox);
filterStatus.value = 'posted';
syncStatusSummaryButtons();
assert.equal(statButtonElements.posted.disabled, true);
assert.equal(statButtonElements.other.disabled, true);
assert.equal(statButtonElements.total.disabled, false);
assert.equal(statButtonElements.posted.getAttribute('aria-pressed'), 'false');
assert.equal(statButtonElements.total.getAttribute('aria-pressed'), 'false');

onStatusSummaryClick('error');
assert.equal(testWindow.__filterCalls, 0, 'source=shop should not apply status summary filters');

const fallbackSection = {
  scrollIntoView: () => {
    testWindow.__scrollIntoViewCount += 1;
  },
};
domElements['product-section'] = fallbackSection;
testWindow.__scrollIntoViewCount = 0;
assert.doesNotThrow(() => scrollProductSectionIntoView(), 'scrollProductSectionIntoView should handle missing getBoundingClientRect');
assert.equal(testWindow.__scrollIntoViewCount, 1, 'safe scroll should run when geometry is unavailable');

domElements['product-section'] = {
  getBoundingClientRect: () => ({ top: 20, bottom: 40 }),
  scrollIntoView: () => {
    testWindow.__scrollIntoViewCount += 1;
  },
};
testWindow.__scrollIntoViewCount = 0;
assert.doesNotThrow(() => scrollProductSectionIntoView(), 'scrollProductSectionIntoView should be safe with in-view geometry');
assert.equal(testWindow.__scrollIntoViewCount, 0, 'in-view geometry should not scroll');

vm.runInNewContext('document = undefined;', sandbox);
assert.doesNotThrow(() => syncStatusSummaryButtons(), 'status buttons should be safe without a DOM');
assert.doesNotThrow(() => scrollProductSectionIntoView(), 'scroll helper should be safe without a DOM');

console.log('status filter tests passed');
