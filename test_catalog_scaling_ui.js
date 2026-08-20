'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = __dirname;
const html = fs.readFileSync(path.join(root, 'dashboard_static/index.html'), 'utf8');
const app = fs.readFileSync(path.join(root, 'dashboard_static/app.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'dashboard_static/style.css'), 'utf8');

assert.match(html, /id="catalog-pagination"/);
assert.match(html, /id="job-center-drawer"/);
assert.match(app, /selectedCatalogIds/);
assert.match(app, /grid\.innerHTML = products\.map\(p => productCard\(p\)\)\.join\(''\);/);
assert.match(app, /function renderCatalogSummary\(total\)/);
assert.match(app, /renderCatalogSummary\(records\.length\)/);
assert.match(app, /renderCatalogSummary\(listings\.length\)/);
assert.doesNotMatch(app, /catalogViewState/);
assert.doesNotMatch(app, /renderCatalogPagination/);
assert.doesNotMatch(app, /loadMoreCatalog/);
assert.doesNotMatch(app, /Tải thêm/);
assert.doesNotMatch(app, /chỉ card đang hiển thị mới tạo DOM/);
assert.match(app, /function toggleJobCenter\(/);
assert.match(app, /\/api\/etsy\/jobs\?limit=50/);
assert.match(css, /\.job-center-drawer/);
assert.match(css, /\.catalog-pagination/);

console.log('catalog scaling and job center UI tests passed');
