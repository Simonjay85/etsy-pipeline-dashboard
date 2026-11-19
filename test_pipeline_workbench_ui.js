'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');
const indexHtml = fs.readFileSync('dashboard_static/index.html', 'utf8');
const start = appJs.indexOf('function setPipelineWorkbenchExpanded');
const end = appJs.indexOf('// ── Global operation queue', start);
assert.ok(start >= 0 && end > start, 'Unable to isolate pipeline workbench helpers');

const pipelineToggleAttributes = new Map();
const elements = new Map([
  ['pipeline-workbench', { classList: { toggle() {} } }],
  ['pipeline-workbench-content', { hidden: true }],
  ['pipeline-workbench-toggle', { setAttribute(name, value) { pipelineToggleAttributes.set(name, value); } }],
  ['pipeline-workbench-toggle-label', { textContent: '' }],
  ['pipeline-health-note', { textContent: '' }],
  ['pipeline-policy-card', { innerHTML: '' }],
  ['pipeline-research-shop', { value: '', innerHTML: '' }],
  ['pipeline-jobs-body', { innerHTML: '' }],
  ['pipeline-review-list', { innerHTML: '' }],
]);
const tabs = [
  { dataset: { pipelineView: 'research' }, classList: { toggle() {} }, setAttribute() {} },
  { dataset: { pipelineView: 'pipeline' }, classList: { toggle() {} }, setAttribute() {} },
];
const panels = [
  { id: 'pipeline-view-research', classList: { toggle() {} } },
  { id: 'pipeline-view-pipeline', classList: { toggle() {} } },
  { id: 'pipeline-view-draft-review', classList: { toggle() {} } },
];
const sandbox = {
  module: { exports: {} },
  exports: {},
  document: {
    getElementById: (id) => elements.get(id) || null,
    querySelector: (selector) => selector === '.pipeline-workbench-toggle-icon' ? pipelineToggleIcon : null,
    querySelectorAll: (selector) => selector === '[data-pipeline-view]' ? tabs : panels,
  },
  escHtml: (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char])),
};
const pipelineToggleIcon = { textContent: '' };
vm.runInNewContext(`${appJs.slice(start, end)}\nmodule.exports = { setPipelineWorkbenchExpanded, togglePipelineWorkbench, selectPipelineView, renderPipelineHealth, renderPipelineJobs };`, sandbox);
const { setPipelineWorkbenchExpanded, togglePipelineWorkbench, selectPipelineView, renderPipelineHealth, renderPipelineJobs } = sandbox.module.exports;

assert.match(indexHtml, /id="pipeline-workbench-content" hidden/);
assert.match(indexHtml, /onclick="togglePipelineWorkbench\(\)"/);
setPipelineWorkbenchExpanded(false);
assert.equal(elements.get('pipeline-workbench-content').hidden, true);
assert.equal(pipelineToggleAttributes.get('aria-expanded'), 'false');
assert.equal(elements.get('pipeline-workbench-toggle-label').textContent, 'Mở pipeline');
assert.equal(pipelineToggleIcon.textContent, '▸');
togglePipelineWorkbench();
assert.equal(elements.get('pipeline-workbench-content').hidden, false);
assert.equal(pipelineToggleAttributes.get('aria-expanded'), 'true');
assert.equal(elements.get('pipeline-workbench-toggle-label').textContent, 'Ẩn pipeline');
assert.equal(pipelineToggleIcon.textContent, '▾');

renderPipelineHealth({
  active_shop: 'shop3',
  quota: { used: 2, remaining: 3, max_total: 5, max_per_shop: 2 },
  jobs: { active: 4 },
  policy: { max_inflight_jobs: 10 },
  shops: [
    { id: 'shop3', name: 'Pivot & Page' },
    { id: 'BubbleByJulie', name: 'Bubble By Julie' },
  ],
  workers: {
    hermes: { gateway: 'online' },
    chatgpt: { cdp: 'online' },
    image_api: { budget_usd: 0 },
  },
});
assert.match(elements.get('pipeline-health-note').textContent, /Hermes online/);
assert.match(elements.get('pipeline-health-note').textContent, /API \$0\.00/);
assert.match(elements.get('pipeline-policy-card').innerHTML, /2\/5 draft/);
assert.match(elements.get('pipeline-research-shop').innerHTML, /Pivot &amp; Page/);
assert.equal(elements.get('pipeline-research-shop').value, 'shop3');

renderPipelineJobs([
  { candidate_id: 'candidate-1', shop_id: 'templystudios', stage: 'seo-package', status: 'running', updated_at: 0 },
  { candidate_id: 'candidate-2', shop_id: 'daisyflowdigital', stage: 'create-verified-draft', status: 'verified', updated_at: 0 },
  { candidate_id: '<script>', shop_id: 'shop3', stage: 'research', status: 'blocked', error_message: 'unsafe' },
]);
assert.match(elements.get('pipeline-jobs-body').innerHTML, /candidate-1/);
assert.match(elements.get('pipeline-review-list').innerHTML, /candidate-2/);
assert.match(elements.get('pipeline-jobs-body').innerHTML, /&lt;script&gt;/);
assert.doesNotMatch(elements.get('pipeline-jobs-body').innerHTML, /<script>/);

// The approval-gated view is selectable while publish remains absent from the
// workbench's executable controls and the UI advertises ten images for review.
selectPipelineView('draft-review');
assert.match(indexHtml, /data-pipeline-view="draft-review"/);
assert.match(indexHtml, /Draft Review/);
assert.match(appJs, /10 hình/);
assert.match(appJs, /\/api\/pipeline\/health/);
assert.match(appJs, /\/api\/pipeline\/jobs\?limit=100/);

console.log('pipeline workbench UI tests passed');
