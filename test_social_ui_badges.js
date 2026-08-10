'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');
const metaStart = appJs.indexOf('const SOCIAL_CHANNEL_META = {');
const metaEnd = appJs.indexOf('function productCard', metaStart);
if (metaStart < 0 || metaEnd <= metaStart) {
  throw new Error('Không isolate được helper functions từ dashboard_static/app.js');
}

const sandbox = { module: { exports: {} }, exports: {} };
const helpers = appJs.slice(metaStart, metaEnd);
const customEscHtml = `
function escHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/\"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
`;
const wrapped = `${helpers};
${customEscHtml};
module.exports = {
  renderSocialChannelBadges,
  normalizeSocialStatusSummary,
  safeExternalSocialUrl,
};
`;
vm.runInNewContext(wrapped, sandbox);
const { renderSocialChannelBadges, normalizeSocialStatusSummary, safeExternalSocialUrl } = sandbox.module.exports;

const statuses = {
  pinterest: {
    status: 'posted',
    posted_at: '2026-07-31T00:00:00Z',
    url: 'https://ca.pinterest.com/pin/888475832769175963/',
  },
  instagram: {
    status: 'error',
    posted_at: '2026-07-31T00:00:00Z',
    url: 'https://example.com',
  },
};

const rendered = renderSocialChannelBadges(statuses);
assert.ok(rendered.includes('📌'), 'Pinterest badge visible');
assert.ok(!rendered.includes('📸'), 'Only posted channels should appear');
assert.ok(rendered.includes('href="'), 'Posted badge should expose action link');

const xssUrl = 'https://example.com/?x=" onmouseover="alert(1)"';
const renderedXss = renderSocialChannelBadges({
  pinterest: {
    status: 'posted',
    posted_at: '2026-07-31T00:00:00Z',
    url: xssUrl,
  },
});
assert.equal(renderedXss.includes('<script>'), false);
assert.equal(renderedXss.includes('\" onmouseover='), false);
assert.equal(renderedXss.includes('https://example.com/?x='), true);
assert.equal(renderedXss.includes('&quot;'), true);

const sorted = normalizeSocialStatusSummary({
  twitter: { status: 'posted' },
  instagram: { status: 'posted' },
  pinterest: { status: 'posted' },
});
const sortedPlatforms = sorted.map(item => item.platform);
assert.equal(sortedPlatforms.join(','), 'instagram,pinterest,twitter');

assert.equal(
  safeExternalSocialUrl('https://ca.pinterest.com/pin/888475832769175963/'),
  'https://ca.pinterest.com/pin/888475832769175963/',
  'Should allow https links'
);
assert.equal(
  safeExternalSocialUrl('javascript:alert(1)'),
  '',
  'Should block javascript scheme'
);
assert.equal(
  safeExternalSocialUrl('data:text/html,<script>alert(1)</script>'),
  '',
  'Should block data URLs'
);
assert.equal(
  safeExternalSocialUrl('not a url at all'),
  '',
  'Should reject malformed URL'
);

const renderedInvalid = renderSocialChannelBadges({
  pinterest: {
    status: 'posted',
    posted_at: '2026-07-31T00:00:00Z',
    url: 'javascript:alert(1)',
  },
});
assert.ok(!renderedInvalid.includes('href="'), 'Unsafe url should not render href');
