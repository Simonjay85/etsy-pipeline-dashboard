const fs = require('node:fs');
const assert = require('node:assert/strict');

const indexHtml = fs.readFileSync('dashboard_static/index.html', 'utf8');
const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');

assert.ok(indexHtml.includes('id="local-batch-post-btn"'), 'Missing local batch post button id');
assert.ok(indexHtml.includes('class="btn btn-primary btn-sm local-batch-action"'), 'Missing batch post button style class');
const seoPos = indexHtml.indexOf('batchRegenSEO()');
const postPos = indexHtml.indexOf('batchPostSelected()');
assert.ok(seoPos >= 0 && postPos > seoPos, 'Batch post button should be placed after batch SEO button');

assert.ok(appJs.includes("async function batchPostSelected()"), 'batchPostSelected function should exist');
assert.ok(appJs.includes("selectedBatchCheckboxes('local')"), 'batchPostSelected must use local checkbox filter');
assert.ok(appJs.includes("'/api/run-selected-products'"), 'batchPostSelected must call new selected products endpoint');

const batchStart = appJs.indexOf('async function batchPostSelected()');
const batchEnd = appJs.indexOf('\nfunction', batchStart + 1);
const batchBody = batchStart >= 0 ? appJs.slice(batchStart, batchEnd) : '';
assert.ok(batchBody, 'Unable to isolate batchPostSelected function body');
assert.ok(!batchBody.includes("selectedBatchCheckboxes('shop')"), 'batchPostSelected should not include shop-only checkboxes');
assert.ok(batchBody.includes('1 Chrome, xử lý tuần tự'), 'batchPostSelected should show sequential Chrome confirmation text');
assert.ok(batchBody.includes('data?.skipped'), 'batchPostSelected should surface skipped count from API');
assert.ok(batchBody.includes('data?.rejected'), 'batchPostSelected should surface rejected items from API');
assert.ok(batchBody.includes('Bỏ qua'), 'batchPostSelected should toast skipped products instead of aborting silently');

console.log('batch post UI tests passed');
