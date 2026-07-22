'use strict';

const assert = require('node:assert/strict');
const {
  filterRenderableCatalogRecords,
  sortCatalogRecords,
} = require('./dashboard_static/catalog_sort.js');

const records = [
  { record_id: 'etsy:10', source: 'etsy', folder: '', listing_id: '10', title: 'Remote ten' },
  { record_id: 'local:469', source: 'local', folder: 'product-469', listing_id: '' },
  { record_id: 'etsy:2', source: 'etsy', folder: '', listing_id: '2', title: 'Remote two' },
  { record_id: 'both:900', source: 'both', folder: 'product-47', listing_id: '900' },
];
const originalOrder = records.map(record => record.record_id);
const sorted = sortCatalogRecords(records);

assert.deepEqual(
  sorted.map(record => record.record_id),
  ['both:900', 'local:469', 'etsy:2', 'etsy:10'],
);
assert.deepEqual(records.map(record => record.record_id), originalOrder);

const renderCandidates = [
  { record_id: 'local:ghost', source: 'local', folder: 'product-246', exists: false },
  { record_id: 'both:ghost', source: 'both', folder: 'product-247', exists: false },
  { record_id: 'local:physical', source: 'local', folder: 'product-12', exists: true },
  { record_id: 'local:legacy', source: 'local', folder: 'product-13' },
  { record_id: 'etsy:remote', source: 'etsy', folder: '', listing_id: '123', exists: false },
];
const renderable = filterRenderableCatalogRecords(renderCandidates);

assert.deepEqual(
  renderable.map(record => record.record_id),
  ['local:physical', 'local:legacy', 'etsy:remote'],
);
assert.equal(renderCandidates.length, 5);

console.log('catalog ordering tests passed');
