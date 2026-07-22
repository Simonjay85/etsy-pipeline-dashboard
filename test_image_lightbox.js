'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('./dashboard_static/app.js', 'utf8');
const context = {
  console,
  setInterval() {},
  setTimeout() {},
  window: {
    matchMedia: () => ({ addEventListener() {} }),
    addEventListener() {},
  },
  document: { addEventListener() {} },
};
vm.createContext(context);
vm.runInContext(source, context);

assert.deepEqual(
  Array.from(context.safeLightboxImages(['/files/a.png', 'javascript:alert(1)', 'https://example.com/b.jpg', null])),
  ['/files/a.png', 'https://example.com/b.jpg'],
);
assert.deepEqual(
  Array.from(context.safeLightboxImages([
    { url: '/files/local.jpg', full_url: '/files/full.jpg', preview_only: false },
    { url: '/files/dataless-cloud.jpg', full_url: '/files/full-cloud.jpg', preview_only: true, hydration_needed: true },
  ])),
  ['/files/full.jpg', '/files/full-cloud.jpg'],
);

const mixedGallery = context.productImageGallery([
  { url: '/files/a.jpg', full_url: '/files/a.jpg', preview_only: false },
  { url: '/files/b.jpg', full_url: '/files/b-full.jpg', preview_only: true, hydration_needed: true, availability: 'hydration_required' },
], 'product-01');
assert.match(mixedGallery, /product-image-placeholder/);
assert.match(mixedGallery, /data-full-url="\/files\/b-full\.jpg"/);
assert.doesNotMatch(mixedGallery, /<img[^>]+"\/files\/b\.jpg"/);
assert.match(mixedGallery, /fetchpriority="high"/);

const cachedPreviewGallery = context.productImageGallery([
  { url: '/files/product-01/images/.thumbcache/cache.webp', full_url: '/files/product-01/images/full.png', preview_only: true, hydration_needed: true, availability: 'cached_preview' },
], 'product-01');
assert.match(cachedPreviewGallery, /<img[^>]+src="\/files\/product-01\/images\/\.thumbcache\/cache\.webp"/);
assert.doesNotMatch(cachedPreviewGallery, /product-image-placeholder/);

assert.match(source, /event\.key === 'Escape'/);
assert.match(source, /event\.key === 'ArrowLeft'/);
assert.match(source, /event\.key === 'ArrowRight'/);
assert.match(source, /data-image-index=/);
assert.match(source, /role="button" tabindex="0"/);
assert.match(source, /event\.key==='Enter'\|\|event\.key===' '/);
assert.doesNotMatch(source, /openImageLightbox\([^\n]*JSON\.stringify/);

const styles = fs.readFileSync('./dashboard_static/style.css', 'utf8');
assert.match(styles, /\.img-card-overlay[\s\S]*pointer-events:\s*none/);
assert.match(styles, /\.img-card-overlay button\s*\{\s*pointer-events:\s*auto/);

console.log('image lightbox tests passed');
