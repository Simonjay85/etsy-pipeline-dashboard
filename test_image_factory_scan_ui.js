'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');
const indexHtml = fs.readFileSync('dashboard_static/index.html', 'utf8');
assert.match(indexHtml, /\/static\/app\.js\?v=20260811-factory-scan-timeout-01/,
  'Image Factory fix must use a fresh app.js cache key');
const start = appJs.indexOf('let _factoryFolders = []');
const end = appJs.indexOf('function renderFactoryFolders', start);
assert.ok(start >= 0 && end > start, 'Unable to isolate Image Factory scan code');
const scanSource = appJs.slice(start, end);

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function response(data) {
  return {
    ok: true,
    async json() {
      return data;
    },
  };
}

const ids = [
  'factory-loading',
  'factory-folder-grid',
  'factory-empty',
  'factory-result',
  'factory-scope-note',
  'factory-import-btn',
  'factory-select-label',
  'factory-path-label',
];
const nodes = Object.fromEntries(ids.map((id) => [id, {
  id,
  style: { display: '' },
  innerHTML: '',
  textContent: '',
  disabled: false,
}]));

let nextTimerId = 1;
const timers = new Map();
const fetchCalls = [];
let fetchImpl;
const context = {
  AbortController,
  console,
  document: {
    getElementById(id) {
      assert.ok(nodes[id], `Unexpected DOM id: ${id}`);
      return nodes[id];
    },
  },
  fetch(url, options) {
    fetchCalls.push({ url, options });
    return fetchImpl(url, options);
  },
  setTimeout(callback, delay) {
    const id = nextTimerId++;
    timers.set(id, { callback, delay });
    return id;
  },
  clearTimeout(id) {
    timers.delete(id);
  },
  escHtml(value) {
    return String(value);
  },
  updateFactoryImportBtn() {},
  updateFactoryFilterCounts() {},
  setFactoryFilter() {},
  openModal() {},
};
vm.createContext(context);
vm.runInContext(scanSource, context);

(async () => {
  const first = deferred();
  const second = deferred();
  const requests = [first, second];
  fetchImpl = () => requests.shift().promise;

  const firstScan = context.scanFactory();
  const secondScan = context.scanFactory();

  assert.equal(fetchCalls.length, 2);
  assert.equal(fetchCalls[0].url, '/api/image-factory/scan');
  assert.equal(fetchCalls[0].options.signal.aborted, true, 'New scan must abort the prior request');
  assert.equal(fetchCalls[1].options.signal.aborted, false);
  assert.equal(nodes['factory-loading'].style.display, 'block');

  first.resolve(response({ ok: true, shop_id: 'stale-shop', folders: [] }));
  await firstScan;
  assert.equal(nodes['factory-loading'].style.display, 'block', 'Stale request must not hide the newer scan spinner');
  assert.equal(vm.runInContext('_factoryShopId', context), null, 'Stale response must not update shop state');

  second.resolve(response({
    ok: true,
    shop_id: 'current-shop',
    source_label: 'Current Shop',
    factory_path: '/factory/current',
    folders: [],
  }));
  await secondScan;
  assert.equal(nodes['factory-loading'].style.display, 'none', 'Latest request must always settle loading');
  assert.equal(vm.runInContext('_factoryShopId', context), 'current-shop');
  assert.match(nodes['factory-path-label'].textContent, /Current Shop/);

  fetchImpl = (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => {
      const error = new Error('aborted');
      error.name = 'AbortError';
      reject(error);
    }, { once: true });
  });

  const timeoutScan = context.scanFactory();
  const pendingTimers = Array.from(timers.values());
  assert.equal(pendingTimers.length, 1);
  assert.equal(pendingTimers[0].delay, 15000, 'Factory scan timeout must remain bounded at 15 seconds');
  pendingTimers[0].callback();
  await timeoutScan;

  assert.equal(nodes['factory-loading'].style.display, 'none', 'Timed-out request must settle loading in finally');
  assert.equal(nodes['factory-empty'].style.display, 'block');
  assert.match(nodes['factory-empty'].innerHTML, /quá thời gian \(15 giây\)/);
  assert.match(nodes['factory-empty'].innerHTML, /Quét lại/);
  assert.equal(timers.size, 0, 'Timeout must be cleared after scan settles');

  console.log('image factory scan UI tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
