'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');
const indexHtml = fs.readFileSync('dashboard_static/index.html', 'utf8');
const start = appJs.indexOf('const OPERATION_QUEUE_LABELS');
const end = appJs.indexOf('async function pollOperationQueue', start);
assert.ok(start >= 0 && end > start, 'Unable to isolate operation queue dashboard helpers');

const sandbox = { module: { exports: {} }, exports: {} };
vm.runInNewContext(`${appJs.slice(start, end)}\nmodule.exports = { normalizeOperationQueueCommands, operationQueueDuration, operationQueueDisplay };`, sandbox);
const { normalizeOperationQueueCommands, operationQueueDuration, operationQueueDisplay } = sandbox.module.exports;

const normalized = normalizeOperationQueueCommands([
  { command_id: 'queued-later', status: 'queued', enqueued_at: 30, position: 2, shop_id: 'templystudios' },
  { command_id: 'running', status: 'running', enqueued_at: 10, position: 1, shop_id: 'daisyflowdigital' },
  { command_id: 'queued-first', status: 'queued', enqueued_at: 20, position: 2, shop_id: 'daisyflowdigital' },
  { command_id: 'done', status: 'succeeded', enqueued_at: 1 },
]);

assert.deepEqual(Array.from(normalized, command => command.command_id), ['running', 'queued-first', 'queued-later']);
assert.deepEqual(Array.from(normalized, command => command.overallPosition), [1, 2, 3]);
assert.deepEqual(Array.from(normalized, command => command.waitingPosition), [0, 1, 2]);
assert.deepEqual(Array.from(normalized, command => command.shop_id), ['daisyflowdigital', 'daisyflowdigital', 'templystudios']);
assert.equal(operationQueueDuration(8), '8s');
assert.equal(operationQueueDuration(125), '2m 5s');
assert.equal(operationQueueDuration(3720), '1h 2m');

const display = operationQueueDisplay({
  operation: 'etsy-listing-sync',
  target: 'shops/daisyflowdigital/product-42',
  shop_id: 'daisyflowdigital',
  status: 'queued',
  enqueued_at: 100,
}, 225);
assert.equal(display.operation, 'Etsy → Local');
assert.equal(display.target, 'daisyflowdigital/product-42');
assert.equal(display.shop, 'daisyflowdigital');
assert.equal(display.timingLabel, 'Đã chờ');
assert.equal(display.duration, '2m 5s');

// A transient poll failure must reuse the last successful snapshot rather than
// replacing visible cross-shop commands with an empty error state.
assert.match(appJs, /renderOperationQueue\(operationQueueLastCommands \|\| \[\]/);
assert.match(appJs, /Dữ liệu cũ lúc/);
assert.match(appJs, /hasSuccessfulSnapshot: operationQueueLastCommands !== null/);
assert.match(appJs, /if \(error && !hasSuccessfulSnapshot\)/);
assert.match(indexHtml, /Task đang chạy &amp; Queue chung/);

console.log('global operation queue dashboard tests passed');
