'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');
const indexHtml = fs.readFileSync('dashboard_static/index.html', 'utf8');

assert.match(indexHtml, /app\.js\?v=20260922-together-provider-01/);
assert.match(appJs, /void initializeDashboard\(\);/);
assert.doesNotMatch(appJs.slice(appJs.indexOf("window.addEventListener('DOMContentLoaded'"), appJs.indexOf('// ── Global operation queue')), /loadProducts\(\)/);
assert.ok(appJs.indexOf('validateProductsShopIdentity(data)') < appJs.indexOf('allProducts = data.products || []'));

const contextStart = appJs.indexOf('let currentShopsData = {}');
const contextEnd = appJs.indexOf('async function openEtsyPostingLogin', contextStart);
assert.ok(contextStart >= 0 && contextEnd > contextStart, 'Unable to isolate shop context implementation');

class Node {
  constructor(id = '') {
    this.id = id;
    this.value = '';
    this.innerHTML = '';
    this.dataset = {};
    this.children = [];
    this.listeners = {};
    this.disabled = false;
    this.title = '';
  }
  appendChild(child) { this.children.push(child); }
  addEventListener(type, handler) { this.listeners[type] = handler; }
}

function makeHarness(fetchResponses) {
  const selector = new Node('shop-switcher');
  const controls = [new Node('btn-etsy-sync'), new Node('btn-run-all-pending')];
  let reloads = 0;
  const toasts = [];
  let fetchIndex = 0;
  const document = {
    getElementById(id) {
      if (id === 'shop-switcher') return selector;
      return controls.find((node) => node.id === id) || null;
    },
    createElement() { return new Node(); },
    querySelectorAll() { return controls; },
  };
  const sandbox = {
    module: { exports: {} },
    document,
    window: { location: { reload() { reloads += 1; } } },
    console,
    setTimeout: (fn) => { fn(); return 1; },
    fetch: async () => fetchResponses[fetchIndex++],
    toast: (type, msg) => toasts.push({ type, msg }),
    loadCloudAssetStatus: () => {},
  };
  vm.createContext(sandbox);
  vm.runInContext(`${appJs.slice(contextStart, contextEnd)}\nmodule.exports = { initShopSwitcher, handleShopContextDrift, shopContext, currentShopContextId };`, sandbox);
  return { sandbox, selector, controls, toasts, get reloads() { return reloads; } };
}

async function run() {
  const success = makeHarness([
    { ok: true, status: 200, async json() { return { shops: [{ id: 'shop-a', name: 'A' }, { id: 'shop-b', name: 'B' }], active: 'shop-a' }; } },
    { ok: true, status: 200, async json() { return { ok: true, active: 'shop-b', shop: { id: 'shop-b', name: 'B' } }; } },
  ]);
  const api = success.sandbox.module.exports;
  assert.equal(await api.initShopSwitcher(), true);
  assert.equal(success.selector.value, 'shop-a');
  success.selector.value = 'shop-b';
  await success.selector.listeners.change({ target: success.selector });
  assert.equal(success.reloads, 1);

  const failure = makeHarness([
    { ok: true, status: 200, async json() { return { shops: [{ id: 'shop-a', name: 'A' }, { id: 'shop-b', name: 'B' }], active: 'shop-a' }; } },
    { ok: false, status: 400, async json() { return { ok: false, error: 'invalid shop' }; } },
  ]);
  const failureApi = failure.sandbox.module.exports;
  await failureApi.initShopSwitcher();
  failure.selector.value = 'shop-b';
  await failure.selector.listeners.change({ target: failure.selector });
  assert.equal(failure.selector.value, 'shop-a');
  assert.equal(failure.reloads, 0);

  const drift = makeHarness([]);
  const driftApi = drift.sandbox.module.exports;
  driftApi.handleShopContextDrift({ actualId: 'shop-b', source: 'products' });
  driftApi.handleShopContextDrift({ actualId: 'shop-b', source: 'runtime-health' });
  assert.equal(drift.reloads, 1);
  assert.equal(drift.controls.every((control) => control.disabled), true);
  assert.equal(drift.toasts.length, 2);
}

run().catch((error) => { throw error; });
console.log('shop context guard UI tests passed');
