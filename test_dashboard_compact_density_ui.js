'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = __dirname;
const html = fs.readFileSync(path.join(root, 'dashboard_static/index.html'), 'utf8');
const app = fs.readFileSync(path.join(root, 'dashboard_static/app.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'dashboard_static/style.css'), 'utf8');

function balancedBlock(source, start) {
  assert.ok(start >= 0, 'Expected CSS block was not found');
  const open = source.indexOf('{', start);
  assert.ok(open >= 0, 'Expected opening brace was not found');
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    if (source[index] === '}') depth -= 1;
    if (depth === 0) return source.slice(start, index + 1);
  }
  throw new Error('Unbalanced CSS block');
}

const compactMarker = css.indexOf('/* ── Compact desktop density ──');
const desktopStart = css.indexOf('@media (min-width: 1201px)', compactMarker);
const desktop = balancedBlock(css, desktopStart);

assert.match(html, /style\.css\?v=20260813-compact-density-01/);
assert.match(desktop, /\.topbar\s*\{[\s\S]*?display:\s*grid/);
assert.match(desktop, /grid-template-areas:\s*"brand runtime services"\s*"actions actions actions"/);
assert.match(desktop, /\.topbar \.brand > div\s*\{[^}]*display:\s*flex[^}]*align-items:\s*center/);
assert.match(desktop, /\.topbar \.brand-meta\s*\{[^}]*margin-top:\s*0/);
assert.match(desktop, /\.topbar \.runtime-health\s*\{[\s\S]*?overflow-x:\s*auto/);
assert.match(desktop, /\.topbar \.header-actions\s*\{[\s\S]*?flex-wrap:\s*nowrap[\s\S]*?overflow-x:\s*auto/);
assert.match(desktop, /\.operation-queue-dashboard\s*\{[\s\S]*?display:\s*grid[\s\S]*?grid-template-columns:\s*max-content minmax\(0, 1fr\)/);
assert.match(desktop, /\.operation-queue-list\s*\{[^}]*min-width:\s*0/);
assert.match(desktop, /\.stat-card\s*\{[^}]*min-height:\s*40px/);
assert.match(desktop, /\.stat-card\s*\{[^}]*display:\s*flex/);
assert.match(desktop, /\.stat-num\s*\{[^}]*font-size:\s*20px/);
assert.match(desktop, /\.etsy-sync-strip\s*\{[\s\S]*?flex-wrap:\s*nowrap[\s\S]*?overflow-x:\s*auto/);
assert.match(desktop, /\.toolbar\s*\{[\s\S]*?flex-wrap:\s*nowrap[\s\S]*?overflow-x:\s*auto/);
assert.doesNotMatch(desktop, /etsy-bulk-sync-panel/);

const wideStart = css.indexOf('@container product-list (min-width: 1320px)', compactMarker);
const wide = balancedBlock(css, wideStart);
assert.match(wide, /\.catalog-product-card\s*\{[\s\S]*?grid-template-columns:\s*28px 214px minmax\(240px, 1fr\) minmax\(760px, 1\.55fr\)/);
assert.match(wide, /\.product-action-panel\s*\{[\s\S]*?display:\s*grid[\s\S]*?grid-template-columns:[^;]*minmax\([^;]*minmax\([^;]*minmax\([^;]*minmax\(/);
assert.match(wide, /\.product-action-header\s*\{[\s\S]*?flex-direction:\s*column/);

const mediumStart = css.indexOf('@container product-list (min-width: 960px) and (max-width: 1319px)', compactMarker);
const medium = balancedBlock(css, mediumStart);
assert.match(medium, /grid-template-columns:\s*28px 68px minmax\(220px, 1fr\) minmax\(575px, 1\.25fr\)/);
assert.match(medium, /\.gallery-wrap\s*\{[^}]*width:\s*68px/);
assert.match(medium, /\.product-action-panel\s*\{[\s\S]*?display:\s*grid[\s\S]*?grid-template-columns:[^;]*minmax\([^;]*minmax\([^;]*minmax\([^;]*minmax\(/);
assert.match(medium, /grid-template-columns:\s*minmax\(112px, \.68fr\) minmax\(96px, \.58fr\) minmax\(130px, \.78fr\) minmax\(205px, 1\.3fr\)/);
assert.match(medium, /\.product-action-panel\s*\{[^}]*grid-column:\s*4[^}]*grid-row:\s*1/);
assert.match(medium, /\.product-action-header\s*\{[^}]*grid-column:\s*auto[^}]*grid-row:\s*auto/);
assert.doesNotMatch(medium, /\.product-action-panel\s*\{[^}]*grid-row:\s*2/);

const narrowStart = css.indexOf('@container product-list (min-width: 761px) and (max-width: 959px)', compactMarker);
const narrow = balancedBlock(css, narrowStart);
assert.match(narrow, /\.product-action-panel\s*\{[\s\S]*?display:\s*grid[\s\S]*?grid-template-columns:[^;]*minmax\([^;]*minmax\([^;]*minmax\([^;]*minmax\(/);
assert.match(narrow, /grid-column:\s*2 \/ -1/);
assert.match(narrow, /grid-row:\s*2/);
assert.match(narrow, /\.product-action-header\s*\{[^}]*grid-column:\s*auto[^}]*grid-row:\s*auto/);

const finalMobileStart = css.lastIndexOf('@media (max-width: 700px)');
const finalMobile = balancedBlock(css, finalMobileStart);
assert.ok(finalMobileStart > narrowStart, 'Final mobile catalog guard must remain after desktop container overrides');
assert.match(finalMobile, /\.catalog-product-card\s*\{[^}]*grid-template-columns:\s*30px minmax\(0, 1fr\)/);

// Compact density must preserve every primary dashboard region and action group.
for (const id of [
  'runtime-health',
  'operation-queue-dashboard',
  'stats-bar',
  'etsy-sync-strip',
  'catalog-strip',
  'etsy-bulk-sync-panel',
  'product-source-switcher',
  'product-grid',
]) {
  assert.match(html, new RegExp(`id="${id}"`), `Missing critical dashboard region: ${id}`);
}

assert.match(app, /product-actions product-action-panel/);
assert.match(app, /product-action-header/);
assert.match(app, /action-group action-group-local/);
assert.match(app, /action-group action-group-content/);
assert.match(app, /action-group action-group-live-etsy/);
assert.match(app, /status-menu/);
assert.match(app, /renderCloudAssetUi\(p\.folder\)/);

console.log('dashboard compact density UI tests passed');
