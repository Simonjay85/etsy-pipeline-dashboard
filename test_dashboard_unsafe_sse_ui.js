'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');
const tokenStart = appJs.indexOf('const DASHBOARD_MUTATION_TOKEN_HEADER');
const tokenEnd = appJs.indexOf('function normalizeEtsyStatus', tokenStart);
const streamStart = appJs.indexOf('async function readDashboardSSE');
const streamEnd = appJs.indexOf('function selectListingToRepair', streamStart);
assert.ok(tokenStart >= 0 && tokenEnd > tokenStart, 'Unable to isolate the wrapped fetch implementation');
assert.ok(streamStart >= 0 && streamEnd > streamStart, 'Unable to isolate the streamed action clients');

const tokenSource = appJs.slice(tokenStart, tokenEnd);
const streamSource = appJs.slice(streamStart, streamEnd);
assert.doesNotMatch(streamSource, /new EventSource\(/, 'Action streams must use fetch POST');
const logsStart = appJs.indexOf('function connectLogs()');
const logsEnd = appJs.indexOf('function appendLog', logsStart);
assert.match(appJs.slice(logsStart, logsEnd), /new EventSource\('\/api\/logs'\)/,
  'The read-only logs stream must remain on EventSource');

function makeResponse(text, { splitEveryByte = false } = {}) {
  const bytes = new TextEncoder().encode(text);
  const chunks = splitEveryByte
    ? Array.from(bytes, (_byte, index) => bytes.slice(index, index + 1))
    : [bytes];
  let index = 0;
  const state = { cancelled: false };
  return {
    state,
    response: {
      ok: true,
      status: 200,
      body: {
        getReader() {
          return {
            async read() {
              if (index >= chunks.length) return { done: true, value: undefined };
              return { done: false, value: chunks[index++] };
            },
            async cancel() {
              state.cancelled = true;
            },
            releaseLock() {},
          };
        },
      },
    },
  };
}

function makeHttpErrorResponse(status, detail) {
  return {
    response: {
      ok: false,
      status,
      async text() { return detail; },
    },
  };
}

const nodes = {
  'scan-btn': { disabled: false, innerHTML: '' },
  'scan-result': { style: { display: '' } },
  'scan-log': { innerHTML: '' },
  'scan-errors-list': { innerHTML: '', children: [], appendChild(item) { this.children.push(item); } },
};
const cleanButton = { disabled: false, innerHTML: '' };
const calls = [];
const responses = [];
const alerts = [];
const pagehideListeners = new Set();
const windowObject = {
  location: { href: 'http://localhost:8090/', origin: 'http://localhost:8090' },
  addEventListener(name, callback) {
    if (name === 'pagehide') pagehideListeners.add(callback);
  },
  removeEventListener(name, callback) {
    if (name === 'pagehide') pagehideListeners.delete(callback);
  },
  async fetch(url, options) {
    calls.push({ url, options });
    const next = responses.shift();
    assert.ok(next, `Unexpected fetch: ${url}`);
    return next.response;
  },
};
const context = {
  AbortController,
  Headers,
  Request,
  TextDecoder,
  URL,
  alert(message) { alerts.push(message); },
  confirm() { return true; },
  console,
  document: {
    querySelector(selector) {
      if (selector === 'meta[name="etsy-dashboard-mutation-token"]') return { content: 'test-token' };
      if (selector === 'button[onclick="cleanDuplicates()"]') return cleanButton;
      return null;
    },
    getElementById(id) {
      assert.ok(nodes[id], `Unexpected DOM id: ${id}`);
      return nodes[id];
    },
    createElement() {
      return { style: {}, innerHTML: '' };
    },
  },
  window: windowObject,
};
Object.defineProperty(context, 'fetch', {
  configurable: true,
  get() { return windowObject.fetch; },
});
vm.createContext(context);
vm.runInContext(tokenSource, context);
vm.runInContext(streamSource, context);

function assertPostedWithMutationToken(index, path) {
  const request = calls[index];
  assert.equal(request.url, path);
  assert.equal(request.options.method, 'POST');
  assert.equal(new Headers(request.options.headers).get('x-dashboard-mutation-token'), 'test-token');
}

(async () => {
  const scanResponse = makeResponse([
    ': keepalive\r\n\r\n',
    'data: {"type":"info","msg":"Đã kiểm tra 🧹"}\r\n\r\n',
    'data: {"type":"found","id":"123","title":"Tiêu đề","errors":["Ảnh thiếu"]}\r\n\r\n',
    'data: {"type":"finished"}\r\n\r\n',
  ].join(''), { splitEveryByte: true });
  responses.push(scanResponse);
  await context.startScan();
  assertPostedWithMutationToken(0, '/api/scan-listings');
  assert.equal(nodes['scan-log'].innerHTML, '✅ Quét hoàn tất!');
  assert.equal(nodes['scan-errors-list'].children.length, 1);
  assert.match(nodes['scan-errors-list'].children[0].innerHTML, /Tiêu đề/);
  assert.equal(nodes['scan-btn'].disabled, false);
  assert.equal(nodes['scan-btn'].innerHTML, '🔍 Quét lại');
  assert.equal(scanResponse.state.cancelled, true, 'Finished must cancel the remaining response reader');

  const cleanResponse = makeResponse('data: {"type":"finished"}\r\n\r\n', { splitEveryByte: true });
  responses.push(cleanResponse);
  await context.cleanDuplicates();
  assertPostedWithMutationToken(1, '/api/clean-duplicates');
  assert.equal(alerts.length, 1, 'The clean completion alert should appear only after finished');
  assert.equal(cleanButton.disabled, false);
  assert.equal(cleanResponse.state.cancelled, true, 'Finished must cancel the remaining response reader');

  const fatalResponse = makeResponse('data: {"type":"fatal","error":"mock fatal"}\r\n\r\n');
  responses.push(fatalResponse);
  await context.startScan();
  assertPostedWithMutationToken(2, '/api/scan-listings');
  assert.match(nodes['scan-log'].innerHTML, /mock fatal/);
  assert.equal(nodes['scan-btn'].disabled, false);
  assert.equal(nodes['scan-btn'].innerHTML, '🔍 Quét lại');
  assert.equal(fatalResponse.state.cancelled, true, 'Fatal must cancel the remaining response reader');

  const incompleteClean = makeResponse('data: {"type":"info","msg":"still running"}\r\n\r\n');
  responses.push(incompleteClean);
  await context.cleanDuplicates();
  assertPostedWithMutationToken(3, '/api/clean-duplicates');
  assert.equal(alerts.length, 1, 'EOF before finished must not be reported as successful cleanup');
  assert.equal(cleanButton.disabled, false, 'EOF must restore the clean button');

  responses.push(makeHttpErrorResponse(500, 'mock scan failure'));
  await context.startScan();
  assertPostedWithMutationToken(4, '/api/scan-listings');
  assert.match(nodes['scan-log'].innerHTML, /Mất kết nối với server/);
  assert.equal(nodes['scan-btn'].disabled, false, 'HTTP rejection must restore the scan button');
  assert.equal(nodes['scan-btn'].innerHTML, '🔍 Quét shop');

  responses.push(makeHttpErrorResponse(500, 'mock clean failure'));
  await context.cleanDuplicates();
  assertPostedWithMutationToken(5, '/api/clean-duplicates');
  assert.equal(alerts.length, 1, 'HTTP rejection must not report successful cleanup');
  assert.equal(cleanButton.disabled, false, 'HTTP rejection must restore the clean button');
  assert.equal(cleanButton.innerHTML, '🧹 Dọn trùng Drafts');
  assert.equal(pagehideListeners.size, 0, 'Completed streams must remove their pagehide abort handlers');

  console.log('unsafe SSE UI tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
