'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function makeClassList() {
  const value = new Set();
  return {
    add: (v) => value.add(v),
    remove: (v) => value.delete(v),
    toggle: (v, force) => {
      if (typeof force === 'boolean') {
        if (force) value.add(v);
        else value.delete(v);
        return force;
      }
      if (value.has(v)) {
        value.delete(v);
        return false;
      }
      value.add(v);
      return true;
    },
    contains: (v) => value.has(v),
  };
}

const html = fs.readFileSync('./dashboard_static/index.html', 'utf8');
const css = fs.readFileSync('./dashboard_static/style.css', 'utf8');
const source = fs.readFileSync('./dashboard_static/app.js', 'utf8');

assert.match(html, /id="stat-card-total"/);
assert.match(html, /id="stat-card-posted"/);
assert.match(html, /id="stat-card-pending"/);
assert.match(html, /id="stat-card-error"/);
assert.match(html, /id="stat-card-other"/);
assert.equal((html.match(/class="[^"]*stat-filter-btn[^"]*"/g) || []).length, 5);
assert.match(html, /<option value="other">📦 Khác<\/option>/);
assert.match(html, /onchange="onStatusFilterChange\(\)"/);
assert.match(html, /onclick="onStatusSummaryClick\('[^']*'\)"/);
assert.match(source, /function classifyProductStatus\(/);
assert.match(source, /function statusFilterMatches\(/);
assert.match(source, /function syncStatusSummaryButtons\(/);

assert.ok(html.includes('aria-pressed="false"'), 'stat cards expose initial aria-pressed');

function createElement(initialValue = '') {
  return {
    value: initialValue,
    disabled: false,
    dataset: {},
    attrs: {},
    classList: makeClassList(),
    setAttribute(name, value) { this.attrs[name] = String(value); },
    getAttribute(name) { return this.attrs[name] ?? null; },
  };
}

const filterStatuses = ['','posted','draft','pending','error','other','missing_title','missing_description','missing_tags','missing_seo'];
const productElements = {
  'search': { value: '' },
  'filter-status': createElement(''),
  'stat-card-total': createElement(),
  'stat-card-posted': createElement(),
  'stat-card-pending': createElement(),
  'stat-card-error': createElement(),
  'stat-card-other': createElement(),
  'product-section': {
    scrollIntoViewCalls: 0,
    scrollIntoView() {
      this.scrollIntoViewCalls += 1;
    },
  },
};
productElements['stat-card-total'].dataset.statusFilter = '';
productElements['stat-card-posted'].dataset.statusFilter = 'posted';
productElements['stat-card-pending'].dataset.statusFilter = 'pending';
productElements['stat-card-error'].dataset.statusFilter = 'error';
productElements['stat-card-other'].dataset.statusFilter = 'other';
productElements['filter-status'].classList = makeClassList();
for (const key of filterStatuses) {
  if (!productElements[key]) productElements[key] = createElement('');
}

const context = {
  console,
  setInterval() {},
  setTimeout() {},
  window: {
    innerHeight: 900,
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    addEventListener() {},
  },
  document: {
    _elements: productElements,
    getElementById(id) { return this._elements[id] || null; },
    addEventListener() {},
    querySelector(selector) { return null; },
    querySelectorAll(selector) {
      if (selector === '.stat-filter-btn') {
        return [
          this._elements['stat-card-total'],
          this._elements['stat-card-posted'],
          this._elements['stat-card-pending'],
          this._elements['stat-card-error'],
          this._elements['stat-card-other'],
        ];
      }
      return [];
    },
  },
  reducedMotionQuery: {
    matches: false,
  },
};

vm.createContext(context);
vm.runInContext(source, context);

assert.equal(context.classifyProductStatus('✅ Đã đăng draft'), 'draft');
assert.equal(context.classifyProductStatus('✅ Đã đăng'), 'posted');
assert.equal(context.classifyProductStatus('⚠️ Chờ đăng'), 'pending');
assert.equal(context.classifyProductStatus('❌ Lỗi ảnh không tải'), 'error');
assert.equal(context.classifyProductStatus('Đang xử lý'), 'other');

assert.equal(context.statusFilterMatches('✅ Đã đăng draft', 'posted', {}), false);
assert.equal(context.statusFilterMatches('⚠️ Chờ đăng', 'other', {}), false);
assert.equal(context.statusFilterMatches('active', 'other', { source: 'etsy' }), false);
assert.equal(context.statusFilterMatches('draft', 'other', { source: 'etsy' }), false);
assert.equal(context.statusFilterMatches('⚠️ Chờ đăng', 'other', { source: 'etsy' }), false);
assert.equal(context.statusFilterMatches('❌ Lỗi', 'other', { source: 'etsy' }), false);
assert.equal(context.statusFilterMatches('inactive', 'other', { source: 'etsy' }), true);
assert.equal(context.statusFilterMatches('expired', 'other', { source: 'etsy' }), true);
assert.equal(context.statusFilterMatches('active', 'other', { source: 'both' }), true);
assert.equal(context.statusFilterMatches('Đang xử lý', 'other', {}), true);
assert.equal(context.statusFilterMatches('active', 'other', {}), true);
assert.equal(context.statusFilterMatches('Chưa có title', 'missing_title', { missing_fields: ['title'] }), true);
assert.equal(context.statusFilterMatches('✅ Đã đăng', 'missing_title', { missing_fields: [] }), false);

productElements['product-section'].scrollIntoViewCalls = 0;
assert.doesNotThrow(() => context.scrollProductSectionIntoView(), 'scrollProductSectionIntoView should not throw when getBoundingClientRect is missing');
assert.equal(productElements['product-section'].scrollIntoViewCalls, 1, 'scroll should still run when geometry is unavailable');

productElements['product-section'] = {
  scrollIntoViewCalls: 0,
  getBoundingClientRect() { return { top: 100, bottom: 120 }; },
  scrollIntoView() {
    this.scrollIntoViewCalls += 1;
  },
};
assert.doesNotThrow(() => context.scrollProductSectionIntoView(), 'scrollProductSectionIntoView should remain safe with in-view bounding rect');
assert.equal(productElements['product-section'].scrollIntoViewCalls, 0, 'in-view element should not scroll');

productElements['filter-status'].value = 'error';
context.syncStatusSummaryButtons();
assert.equal(productElements['stat-card-error'].getAttribute('aria-pressed'), 'true');
assert.equal(productElements['stat-card-other'].getAttribute('aria-pressed'), 'false');
productElements['filter-status'].disabled = true;
context.syncStatusSummaryButtons();
assert.equal(productElements['stat-card-error'].disabled, true);
assert.equal(productElements['stat-card-error'].getAttribute('aria-pressed'), 'false');
assert.equal(productElements['filter-status'].value, 'error');

assert.match(css, /\.stat-card:focus-visible/);
assert.match(css, /\.stat-card\[aria-pressed="true"\]/);
assert.match(css, /\.stat-card:disabled/);

console.log('filter status helper and dashboard controls tests passed');
