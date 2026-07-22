'use strict';

const assert = require('node:assert/strict');
const {
  checkboxKind,
  selectedByKind,
  summarizeSelection,
  getBatchActionState,
  selectedDraftListingIds,
} = require('./dashboard_static/batch_selection.js');

const local = { checked: true, className: 'product-cb', value: '85' };
const localWithExplicitClass = { checked: true, className: 'product-cb local-product-cb', value: '86' };
const shop = { checked: true, className: 'product-cb shop-product-cb', value: '4528326700' };
const uncheckedShop = { checked: false, className: 'product-cb shop-product-cb', value: '9' };

assert.equal(checkboxKind(local), 'local');
assert.equal(checkboxKind(shop), 'shop');
assert.deepEqual(summarizeSelection([local, localWithExplicitClass, uncheckedShop]), {
  total: 2, localCount: 2, shopCount: 0, mode: 'local',
});
assert.deepEqual(summarizeSelection([shop]), {
  total: 1, localCount: 0, shopCount: 1, mode: 'shop',
});
assert.deepEqual(summarizeSelection([local, shop]), {
  total: 2, localCount: 1, shopCount: 1, mode: 'mixed',
});
assert.deepEqual(selectedByKind([local, shop], 'local'), [local]);
assert.deepEqual(selectedByKind([local, shop], 'shop'), [shop]);

assert.deepEqual(getBatchActionState('local', [local, shop]), {
  total: 2, localCount: 1, shopCount: 1, mode: 'mixed',
  showLocalActions: true, showShopActions: false,
});
assert.deepEqual(getBatchActionState('shop', [local, shop]), {
  total: 2, localCount: 1, shopCount: 1, mode: 'mixed',
  showLocalActions: false, showShopActions: true,
});
assert.deepEqual(getBatchActionState('aggregate', [local]), {
  total: 1, localCount: 1, shopCount: 0, mode: 'local',
  showLocalActions: true, showShopActions: false,
});
assert.deepEqual(getBatchActionState('aggregate', [shop]), {
  total: 1, localCount: 0, shopCount: 1, mode: 'shop',
  showLocalActions: false, showShopActions: true,
});
assert.deepEqual(getBatchActionState('aggregate', [local, shop]), {
  total: 2, localCount: 1, shopCount: 1, mode: 'mixed',
  showLocalActions: true, showShopActions: true,
});
assert.deepEqual(getBatchActionState('aggregate', []), {
  total: 0, localCount: 0, shopCount: 0, mode: 'none',
  showLocalActions: false, showShopActions: false,
});

const draft = { checked: true, className: 'product-cb shop-product-cb', value: '123', dataset: {listingId: '123', etsyStatus: 'draft'} };
const active = { checked: true, className: 'product-cb shop-product-cb', value: '456', dataset: {listingId: '456', etsyStatus: 'active'} };
const unsafe = { checked: true, className: 'product-cb shop-product-cb', value: 'x', dataset: {listingId: 'x', etsyStatus: 'draft'} };
assert.deepEqual(selectedDraftListingIds([draft, active, unsafe]), ['123']);

console.log('batch selection tests passed');
