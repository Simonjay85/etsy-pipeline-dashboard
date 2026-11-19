'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('dashboard_static/index.html', 'utf8');
const app = fs.readFileSync('dashboard_static/app.js', 'utf8');
const css = fs.readFileSync('dashboard_static/style.css', 'utf8');

assert.match(html, /<details[^>]+id="service-details"[\s\S]*<summary>[\s\S]*Dịch vụ 0\/4/);
for (const [id, handler, label] of [
  ['btn-create-folder', 'createNewFolder', 'Tạo Folder Mới'],
  ['btn-etsy-login', 'openEtsyPostingLogin', 'Đăng nhập Etsy'],
  ['btn-etsy-sync', 'syncEtsyShop', 'Đồng bộ Etsy Shop'],
  ['btn-import-factory', 'openFactoryImport', 'Import từ Factory'],
  ['btn-run-all-pending', 'runAllPending', 'Chạy mục chờ/lỗi'],
  ['job-center-toggle', 'toggleJobCenter', 'Công việc'],
  ['btn-stop-all', 'stopAll', 'Dừng tất cả'],
]) {
  assert.match(html, new RegExp(`id="${id}"`), `missing everyday action id: ${id}`);
  assert.match(html, new RegExp(`onclick="${handler}\\(`), `missing handler for ${id}`);
  assert.match(html, new RegExp(label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
}

assert.match(html, /<details[^>]+id="header-tools"[\s\S]*<summary>Công cụ khác/);
for (const [id, handler, label] of [
  ['btn-start-watcher', 'startWatcher', 'Bật Watcher'],
  ['btn-open-repair', 'openRepairModal', 'Sửa lỗi Etsy'],
  ['btn-clean-duplicates', 'cleanDuplicates', 'Dọn trùng Drafts'],
  ['btn-auto-post', 'openSocialBulkModal', 'Auto Post Hàng Loạt'],
  ['btn-import-csv', 'openImportCSV', 'Import CSV'],
  ['btn-batch-generate-seo', 'batchGenerateSEO', 'Tạo SEO còn thiếu'],
]) {
  assert.match(html, new RegExp(`id="${id}"`), `missing secondary action id: ${id}`);
  assert.match(html, new RegExp(`onclick="${handler}\\(`));
  assert.match(html, new RegExp(label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
}

for (const id of ['svc-together', 'svc-image-studio', 'svc-mlx', 'svc-watcher']) {
  assert.match(html, new RegExp(`id="${id}"`));
}
assert.match(html, /id="svc-together"[^>]*>[\s\S]*Together AI/);
assert.doesNotMatch(html, /svc-bai|B\.AI/);
assert.match(app, /together_api/);
assert.doesNotMatch(app, /B\.AI/);
assert.match(html, /id="service-summary"[^>]*aria-live="polite"/);
assert.match(html, /style\.css\?v=20260904-new-import-filter-01/);
assert.match(html, /app\.js\?v=20260922-together-provider-01/);
assert.match(html, /edit_save\.js\?v=20260811-edit-save-02-category-20260830/);
assert.match(css, /\.log-panel\s*\{[\s\S]*?width:\s*min\(400px, 30vw\)/);

function extractFunction(name) {
  const start = app.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `missing ${name}`);
  const open = app.indexOf('{', start);
  let depth = 0;
  for (let index = open; index < app.length; index += 1) {
    if (app[index] === '{') depth += 1;
    if (app[index] === '}') depth -= 1;
    if (depth === 0) return app.slice(start, index + 1);
  }
  throw new Error(`unbalanced function ${name}`);
}

const repairBody = extractFunction('openRepairModal');
assert.match(repairBody, /openModal\('repair-modal'\)/);
assert.doesNotMatch(repairBody, /classList\.add\(['"]open/);

const batchStart = app.indexOf('async function batchGenerateSEO()');
const batchEnd = app.indexOf('\n\nasync function startWatcher', batchStart);
assert.ok(batchStart >= 0 && batchEnd > batchStart, 'missing batch SEO function');
const batchBody = app.slice(batchStart, batchEnd);
assert.match(batchBody, /getElementById\('btn-batch-generate-seo'\)/);
assert.doesNotMatch(batchBody, /querySelector\(['"]\.btn-seo/);
assert.match(batchBody, /finally\s*\{[\s\S]*originalButtonHtml/);

const nodes = new Map();
function node(id, online = false) {
  const classes = new Set(online ? ['svc', 'online'] : ['svc', 'unknown']);
  const value = {
    id,
    className: '',
    dataset: {},
    title: '',
    textContent: '',
    classList: { contains: token => classes.has(token) },
  };
  nodes.set(id, value);
  return value;
}
node('service-summary');
for (const id of ['svc-together', 'svc-image-studio', 'svc-mlx', 'svc-watcher']) node(id);
node('btn-stop-all');

const sandbox = {
  document: { getElementById: id => nodes.get(id) || null },
  module: { exports: {} },
};
vm.createContext(sandbox);
vm.runInContext(`
  let runningSet = new Set();
  let operationQueueLastCommands = null;
  let servicesSnapshotValid = false;
  let operationQueueSnapshotValid = false;
  ${extractFunction('serviceSignal')}
  ${extractFunction('updateServiceSummary')}
  ${extractFunction('updateStopButtonState')}
  module.exports = {
    serviceSignal,
    updateServiceSummary,
    updateStopButtonState,
    setState(next) {
      runningSet = next.runningSet;
      operationQueueLastCommands = next.queue;
      servicesSnapshotValid = next.services;
      operationQueueSnapshotValid = next.queueSnapshot;
    },
  };
`, sandbox);

const helpers = sandbox.module.exports;
assert.equal(helpers.serviceSignal({ together_api: true }, 'together_api', ['bai_api', 'vertex_app']), true);
assert.equal(helpers.serviceSignal({ together_api: false, bai_api: true }, 'together_api', ['bai_api', 'vertex_app']), false);
assert.equal(helpers.serviceSignal({ bai_api: true }, 'together_api', ['bai_api', 'vertex_app']), true);
assert.equal(helpers.serviceSignal({ vertex_app: false }, 'together_api', ['bai_api', 'vertex_app']), false);
assert.equal(helpers.serviceSignal({ vertex_app: false }, 'image_studio'), false);
assert.equal(helpers.serviceSignal({}, 'together_api', ['bai_api', 'vertex_app']), null);

helpers.updateServiceSummary();
assert.equal(nodes.get('service-summary').textContent, 'Dịch vụ 0/4');
nodes.get('svc-together').classList.contains = token => token === 'online';
helpers.updateServiceSummary();
assert.equal(nodes.get('service-summary').textContent, 'Dịch vụ 1/4');

helpers.setState({ runningSet: new Set(), queue: null, services: false, queueSnapshot: false });
helpers.updateStopButtonState();
assert.equal(nodes.get('btn-stop-all').disabled, false, 'Stop must fail open without snapshots');

helpers.setState({ runningSet: new Set(), queue: [], services: true, queueSnapshot: true });
helpers.updateStopButtonState();
assert.equal(nodes.get('btn-stop-all').disabled, true, 'Stop can disable after proven idle snapshots');
assert.match(nodes.get('btn-stop-all').title, /Không có tác vụ/);

helpers.setState({ runningSet: new Set(['product-1']), queue: [], services: true, queueSnapshot: true });
helpers.updateStopButtonState();
assert.equal(nodes.get('btn-stop-all').disabled, false);

helpers.setState({ runningSet: new Set(), queue: [{ status: 'queued' }], services: true, queueSnapshot: true });
helpers.updateStopButtonState();
assert.equal(nodes.get('btn-stop-all').disabled, false);

console.log('dashboard header controls and stop/service UI tests passed');
