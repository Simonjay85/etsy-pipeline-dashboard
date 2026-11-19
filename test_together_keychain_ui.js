'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');
const indexHtml = fs.readFileSync('dashboard_static/index.html', 'utf8');
const togetherStart = appJs.indexOf('const TOGETHER_DEFAULT_BASE_URL');
const togetherEnd = appJs.indexOf('\nfunction openShopSettings()', togetherStart);
assert.ok(togetherStart >= 0 && togetherEnd > togetherStart, 'Unable to isolate Together settings UI');
const togetherSource = appJs.slice(togetherStart, togetherEnd);

assert.match(indexHtml, /Together AI \(lưu bền vững bằng macOS Keychain\)/);
assert.match(indexHtml, /Không lưu vào cấu hình shop, localStorage hoặc trình duyệt/);
assert.match(indexHtml, /id="save-together-config"[^>]*>Lưu vào Keychain/);
assert.match(indexHtml, /id="clear-together-config"[^>]*>Xoá key Keychain \+ runtime/);
assert.doesNotMatch(indexHtml, /cấu hình theo phiên dashboard/);
assert.match(indexHtml, /app\.js\?v=20260922-together-provider-01&settings=20260922-02&pipeline=20260916-auto-hide-01&status-menu=20260922-01&keychain=20260922-together-keychain-01/);
assert.doesNotMatch(appJs, /localStorage/);
assert.match(togetherSource, /persist:\s*true/);
assert.match(togetherSource, /clear_key:\s*true/);

class Node {
  constructor(id) {
    this.id = id;
    this.value = '';
    this.textContent = '';
    this.dataset = {};
    this.disabled = false;
  }
}

function response(payload, ok = true, status = 200) {
  return {
    ok,
    status,
    async json() { return payload; },
  };
}

function createHarness(fetchImpl) {
  const nodes = new Map([
    ['set-together-api-key', new Node('set-together-api-key')],
    ['set-together-base-url', new Node('set-together-base-url')],
    ['set-together-model', new Node('set-together-model')],
    ['set-together-status', new Node('set-together-status')],
    ['save-together-config', new Node('save-together-config')],
    ['clear-together-config', new Node('clear-together-config')],
  ]);
  const confirmations = [];
  const confirmationMessages = [];
  const sandbox = {
    module: { exports: {} },
    document: { getElementById: id => nodes.get(id) || null },
    window: {
      confirm: message => {
        confirmationMessages.push(message);
        return confirmations.shift() ?? true;
      },
    },
    fetch: fetchImpl,
  };
  vm.createContext(sandbox);
  vm.runInContext(`${togetherSource}\nmodule.exports = { loadTogetherConfig, saveTogetherConfig, clearTogetherConfig, renderTogetherConfig };`, sandbox);
  return {
    api: sandbox.module.exports,
    nodes,
    confirmations,
    confirmationMessages,
  };
}

async function testLoadShowsStoredStateAndNeverHydratesSecret() {
  const secret = 'together-secret-must-not-enter-dom';
  const harness = createHarness(async (url, init) => {
    assert.equal(url, '/api/together/settings');
    assert.equal(init.cache, 'no-store');
    assert.deepEqual(Object.keys(init), ['cache']);
    return response({
      ok: true,
      configured: true,
      key_hint: '6789',
      base_url: 'https://proxy.example.test/v1',
      model: 'demo/model',
      persisted: true,
      storage: 'macos_keychain',
      storage_error: '',
    });
  });

  harness.nodes.get('set-together-api-key').value = secret;
  await harness.api.loadTogetherConfig();
  assert.equal(harness.nodes.get('set-together-api-key').value, '');
  assert.equal(harness.nodes.get('set-together-base-url').value, 'https://proxy.example.test/v1');
  assert.equal(harness.nodes.get('set-together-model').value, 'demo/model');
  assert.match(harness.nodes.get('set-together-status').textContent, /Đã lưu trong macOS Keychain/);
  assert.match(harness.nodes.get('set-together-status').textContent, /6789/);
  assert.doesNotMatch(harness.nodes.get('set-together-status').textContent, new RegExp(secret));
}

async function testSaveAlwaysPersistsAndPreservesButtonState() {
  let release;
  const calls = [];
  const harness = createHarness(async (url, init) => {
    calls.push({ url, init });
    assert.equal(harness.nodes.get('save-together-config').disabled, true);
    await new Promise(resolve => { release = resolve; });
    return response({
      ok: true,
      configured: true,
      key_hint: '4321',
      base_url: 'https://api.together.xyz/v1',
      model: 'demo/model',
      persisted: true,
      storage: 'macos_keychain',
      storage_error: '',
    });
  });
  const keyInput = harness.nodes.get('set-together-api-key');
  harness.nodes.get('set-together-base-url').value = 'https://api.together.xyz/v1';
  harness.nodes.get('set-together-model').value = 'demo/model';
  keyInput.value = 'new-secret-4321';

  const pending = harness.api.saveTogetherConfig();
  assert.equal(harness.nodes.get('save-together-config').disabled, true);
  release();
  await pending;

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/together/settings');
  assert.equal(calls[0].init.method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    api_key: 'new-secret-4321',
    base_url: 'https://api.together.xyz/v1',
    model: 'demo/model',
    persist: true,
  });
  assert.equal(harness.nodes.get('save-together-config').disabled, false);
  assert.equal(keyInput.value, '', 'the key field must be cleared after save');
  assert.match(harness.nodes.get('set-together-status').textContent, /Đã lưu trong macOS Keychain/);
}

async function testBlankKeyIsForwardedForRuntimeKeyPersistence() {
  let request;
  const harness = createHarness(async (_url, init) => {
    request = JSON.parse(init.body);
    return response({
      ok: true,
      configured: true,
      key_hint: '2468',
      base_url: 'https://proxy.example.test/v1',
      model: 'proxy/model',
      persisted: true,
      storage: 'macos_keychain',
      storage_error: '',
    });
  });
  harness.nodes.get('set-together-api-key').value = '';
  harness.nodes.get('set-together-base-url').value = 'https://proxy.example.test/v1';
  harness.nodes.get('set-together-model').value = 'proxy/model';
  await harness.api.saveTogetherConfig();
  assert.equal(request.api_key, '');
  assert.equal(request.persist, true);
  assert.equal(request.clear_key, undefined);
}

async function testClearRequiresConfirmationAndDeletesBothCopies() {
  let request;
  let fetchCount = 0;
  const harness = createHarness(async (_url, init) => {
    fetchCount += 1;
    request = JSON.parse(init.body);
    return response({
      ok: true,
      configured: false,
      key_hint: '',
      base_url: 'https://api.together.xyz/v1',
      model: 'demo/model',
      persisted: false,
      storage: 'macos_keychain',
      storage_error: '',
    });
  });
  harness.nodes.get('set-together-base-url').value = 'https://api.together.xyz/v1';
  harness.nodes.get('set-together-model').value = 'demo/model';
  harness.confirmations.push(false);
  await harness.api.clearTogetherConfig();
  assert.equal(fetchCount, 0, 'cancelled clear must not call the route');
  assert.match(harness.confirmationMessages[0], /Keychain/);
  assert.match(harness.confirmationMessages[0], /runtime/);
  assert.match(harness.confirmationMessages[0], /xoá cả hai bản lưu/);

  harness.confirmations.push(true);
  const clearPromise = harness.api.clearTogetherConfig();
  assert.equal(harness.nodes.get('clear-together-config').disabled, true);
  await clearPromise;
  assert.equal(harness.nodes.get('clear-together-config').disabled, false);
  assert.deepEqual(request, {
    api_key: '',
    clear_key: true,
    base_url: 'https://api.together.xyz/v1',
    model: 'demo/model',
    persist: true,
  });
  assert.match(harness.nodes.get('set-together-status').textContent, /Chưa cấu hình API key Together/);
}

async function testStorageErrorAndOnlyCurrentStateAreVisible() {
  const harness = createHarness(async () => response({
    ok: true,
    configured: true,
    key_hint: '9999',
    base_url: 'https://api.together.xyz/v1',
    model: 'demo/model',
    persisted: false,
    storage: 'macos_keychain',
    storage_error: 'Keychain không khả dụng',
  }));
  await harness.api.loadTogetherConfig();
  assert.equal(harness.nodes.get('set-together-status').dataset.state, 'error');
  assert.match(harness.nodes.get('set-together-status').textContent, /Keychain không khả dụng/);

  harness.api.renderTogetherConfig({
    configured: true,
    key_hint: '9999',
    base_url: 'https://api.together.xyz/v1',
    model: 'demo/model',
    persisted: false,
    storage: 'macos_keychain',
    storage_error: '',
  });
  assert.equal(harness.nodes.get('set-together-status').dataset.state, 'warning');
  assert.match(harness.nodes.get('set-together-status').textContent, /Chỉ dùng trong phiên hiện tại/);
}

(async () => {
  await testLoadShowsStoredStateAndNeverHydratesSecret();
  await testSaveAlwaysPersistsAndPreservesButtonState();
  await testBlankKeyIsForwardedForRuntimeKeyPersistence();
  await testClearRequiresConfirmationAndDeletesBothCopies();
  await testStorageErrorAndOnlyCurrentStateAreVisible();
  console.log('Together Keychain UI tests passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
