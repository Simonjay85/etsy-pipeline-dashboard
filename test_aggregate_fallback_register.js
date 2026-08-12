'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');

const source = fs.readFileSync('dashboard_static/app.js', 'utf8');

assert.match(source, /api\/local-products\/register/, 'Dashboard JS must call new local registration endpoint');
assert.match(source, /function\s+registerLocalFolders/, 'registerLocalFolders helper must exist');
assert.match(source, /function\s+registerSingleLocalFolder/, 'Single-register wrapper must exist');
assert.match(source, /♻️\s*Đăng\s*ký\s*local/, 'Fallback aggregate card should expose register button text');
assert.match(source, /record\.source\s*===\s*['"]local['"]/,'Fallback should render only for local aggregate rows');
assert.match(source, /record\.source === 'local' \&\& !record\.row && record\.folder/,'Fallback button should require missing catalog row');

console.log('aggregate fallback registration JS assertions passed');
