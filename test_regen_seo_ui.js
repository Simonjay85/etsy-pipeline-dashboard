'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');
const timeoutMatch = appJs.match(/const REGEN_SEO_REQUEST_TIMEOUT_MS = (\d+);/);
assert.ok(timeoutMatch, 'production SEO timeout constant is missing');
const productionTimeoutMs = Number(timeoutMatch[1]);
assert.equal(productionTimeoutMs, 195000, 'frontend SEO timeout must leave margin over the 190s backend budget');
const start = appJs.indexOf('async function regenSEO()');
const end = appJs.indexOf('\n\n// ── Image Modal', start);
assert.ok(start >= 0 && end > start, 'Unable to isolate regenSEO');

const nativeSetTimeout = setTimeout;
const nativeClearTimeout = clearTimeout;

const modalButton = { disabled: false, innerHTML: '🤖 Tạo SEO' };
const headerButton = { disabled: false, innerHTML: 'Header SEO action' };
const elements = {
  'edit-row': { value: '409' },
  'edit-folder': { value: 'product-409' },
  'edit-title': { value: 'Original title' },
  'edit-keywords': { value: 'planner' },
  'edit-extra': { value: '' },
  'edit-description': { value: 'Original description' },
  'edit-tags': { value: 'original tag' },
  'edit-modal': {
    querySelector(selector) {
      assert.equal(selector, '.btn-seo');
      return modalButton;
    },
  },
};
const toasts = [];
let fetchCalls = 0;
const sandbox = {
  module: { exports: {} },
  document: {
    getElementById(id) { return elements[id] || null; },
    querySelector() { return headerButton; },
  },
  async fetch(_, init = {}) {
    fetchCalls += 1;
    const signal = init.signal;
    if (signal?.aborted) {
      const aborted = new Error('This operation was aborted');
      aborted.name = 'AbortError';
      throw aborted;
    }
    return new Promise((_, reject) => {
      if (!signal || !signal.addEventListener) {
        return;
      }
      const onAbort = () => {
        const aborted = new Error('This operation was aborted');
        aborted.name = 'AbortError';
        reject(aborted);
      };
      signal.addEventListener('abort', onAbort, { once: true });
    });
  },
  toast(kind, message) { toasts.push({ kind, message }); },
  updateCount() {},
  loadProducts() { throw new Error('must not reload after failed generation'); },
  allProducts: [],
  parseInt,
  JSON,
  AbortController,
  setTimeout: (handler, timeout, ...rest) => nativeSetTimeout(handler, Math.min(timeout || 0, 1), ...rest),
  clearTimeout: nativeClearTimeout,
};

vm.createContext(sandbox);
vm.runInContext(
  `const REGEN_SEO_REQUEST_TIMEOUT_MS = ${productionTimeoutMs};\nlet modalSeoGeneration = null;\n${appJs.slice(start, end)}\nmodule.exports = { regenSEO, getModalSeoGeneration: () => modalSeoGeneration };`,
  sandbox,
);

(async () => {
  const first = sandbox.module.exports.regenSEO();
  const second = sandbox.module.exports.regenSEO();
  assert.equal(fetchCalls, 1, 'double click must not start a second request');
  assert.equal(modalButton.disabled, true, 'button should be disabled after first click');
  assert.equal(headerButton.disabled, false, 'header SEO button must not be targeted');
  assert.match(toasts[0].message, /product-409/);

  await Promise.all([first, second]);
  assert.equal(modalButton.disabled, false, 'button should be restored after timeout');
  assert.equal(modalButton.innerHTML, '🤖 Tạo SEO', 'button label should restore');
  assert.equal(toasts.at(-1).kind, 'error', 'timeout path should show error toast');
  assert.match(toasts.at(-1).message, /Tạo SEO quá thời gian chờ/);
  assert.equal(
    elements['edit-title'].value,
    'Original title',
    'title field should remain unchanged on timeout',
  );
  assert.equal(
    elements['edit-description'].value,
    'Original description',
    'description field should remain unchanged on timeout',
  );
  assert.equal(
    elements['edit-tags'].value,
    'original tag',
    'tags field should remain unchanged on timeout',
  );
  assert.ok(
    sandbox.module.exports.getModalSeoGeneration() === null,
    'modal lock should clear after timeout',
  );
  console.log('regen SEO modal UI tests passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
