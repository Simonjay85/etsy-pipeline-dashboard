'use strict';

const assert = require('node:assert/strict');
const {
  parseProductRow,
  findSavedFieldMismatches,
} = require('./dashboard_static/edit_save.js');

assert.equal(parseProductRow('33'), 33);
assert.equal(parseProductRow('0'), null);
assert.equal(parseProductRow('not-a-row'), null);
assert.equal(parseProductRow('2.5'), null);

const expected = {
  title: '',
  tags: 'reading journal, book tracker',
  keywords: 'reading journal',
  description: 'Description',
  price: 1.98,
  section: '',
  extra: '',
  etsy_url: 'https://www.etsy.com/ca/listing/4434249871/2026-digital-planner',
  sku: 'dd_product_30',
  qty: 898,
};
const saved = {
  ...expected,
  title: '[Cần SEO] product-30',
  section: 'Digital Planner',
  price: '1.98',
  qty: '898',
};

assert.deepEqual(findSavedFieldMismatches(expected, saved), []);
assert.deepEqual(
  findSavedFieldMismatches(expected, { ...saved, etsy_url: 'https://www.etsy.com/listing/old' }),
  [{
    field: 'etsy_url',
    expected: expected.etsy_url,
    actual: 'https://www.etsy.com/listing/old',
  }],
);
assert.deepEqual(
  findSavedFieldMismatches(expected, null),
  [{ field: 'row', expected: 'saved product', actual: 'missing' }],
);

console.log('edit save tests passed');
