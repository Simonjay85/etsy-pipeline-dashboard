'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const BatchSelection = require('./dashboard_static/batch_selection.js');
const appSource = fs.readFileSync(
  path.join(__dirname, 'dashboard_static', 'app.js'),
  'utf8',
);

function extractFunction(source, name) {
  const marker = `function ${name}`;
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(`Unable to locate function: ${name}`);
  const next = source.indexOf('\nfunction ', start + marker.length);
  return source.slice(start, next === -1 ? undefined : next);
}

function createFunctionBundle() {
  const names = [
    'isSelectableBatchCheckbox',
    'getSelectableBatchCheckboxes',
    'catalogSelectionKey',
    'restoreCatalogSelections',
    'rememberCatalogSelections',
    'selectedBatchCheckboxes',
    'applyBatchActionVisibility',
    'toggleSelectAll',
    'updateBatchUI',
  ];
  return names.map(name => extractFunction(appSource, name)).join('\n\n');
}

class MockStyle {
  constructor(values = {}) {
    this.display = '';
    Object.assign(this, values);
  }
  setProperty(name, value) {
    this[name] = value;
  }
}

class MockClassList {
  constructor(element) {
    this._element = element;
    this._tokens = new Set(
      String(element.className || '')
        .split(/\s+/)
        .map(token => token.trim())
        .filter(Boolean),
    );
  }
  add(token) {
    this._tokens.add(token);
    this._flush();
  }
  remove(token) {
    this._tokens.delete(token);
    this._flush();
  }
  contains(token) {
    return this._tokens.has(token);
  }
  _flush() {
    this._element.className = [...this._tokens].join(' ');
  }
}

class MockElement {
  constructor({
    tagName = 'div',
    id = '',
    className = '',
    value = '',
    checked = false,
    disabled = false,
    dataset = {},
    hidden = false,
    style = {},
  } = {}) {
    this.tagName = tagName;
    this.id = id;
    this.className = className;
    this.value = value;
    this.checked = checked;
    this.disabled = disabled;
    this.dataset = dataset;
    this.hidden = hidden;
    this.style = new MockStyle(style);
    this.classList = new MockClassList(this);
    this.parentNode = null;
  }
  closest(selector) {
    if (selector === '.product-cb') return this;
    if (selector === '.product-card') return this.closestCard || null;
    return this.parentNode && this.parentNode.closest
      ? this.parentNode.closest(selector)
      : this.parentNode;
  }
}

class MockDocument {
  constructor({
    productCheckboxes = [],
    localBatchActions = [],
    crossShopBatchActions = [],
    shopBatchActions = [],
  } = {}) {
    this._productCheckboxes = productCheckboxes;
    this._localBatchActions = localBatchActions;
    this._crossShopBatchActions = crossShopBatchActions;
    this._shopBatchActions = shopBatchActions;
    this._byId = new Map();
    [...productCheckboxes, ...localBatchActions, ...crossShopBatchActions, ...shopBatchActions].forEach((node) => {
      if (node?.id) this._byId.set(node.id, node);
    });
  }
  getElementById(id) {
    return this._byId.get(id) || null;
  }
  querySelectorAll(selector) {
    if (selector === '.product-cb') return this._productCheckboxes;
    if (selector === '.product-cb:checked') return this._productCheckboxes.filter(cb => cb.checked);
    if (selector === '.local-batch-action') return this._localBatchActions;
    if (selector === '.cross-shop-batch-action') return this._crossShopBatchActions;
    if (selector === '.shop-batch-action') return this._shopBatchActions;
    return [];
  }
  register(node) {
    if (node?.id) this._byId.set(node.id, node);
  }
}

function createCheckbox({
  sourceKind = 'local',
  value = '',
  checked = false,
  disabled = false,
  hidden = false,
  cardHidden = false,
  folder = '',
  listingId = '',
  etsyStatus = 'draft',
}) {
  const className = sourceKind === 'shop' ? 'product-cb shop-product-cb' : 'product-cb';
  const checkbox = new MockElement({
    tagName: 'input',
    className,
    value,
    checked,
    disabled,
    hidden,
    dataset: { folder, listingId, etsyStatus },
  });
  const card = new MockElement({
    tagName: 'div',
    className: 'product-card',
    hidden: cardHidden,
  });
  card.style.display = cardHidden ? 'none' : '';
  checkbox.closestCard = card;
  return checkbox;
}

function createCatalogState({
  includeProducts = true,
  currentProductSource = 'aggregate',
}) {
  const document = new MockDocument({
    productCheckboxes: includeProducts ? [
      createCheckbox({
        sourceKind: 'local',
        value: 'local-visible',
        checked: false,
        folder: 'folder-alpha',
        etsyStatus: '',
      }),
      createCheckbox({
        sourceKind: 'shop',
        value: 'shop-visible',
        checked: false,
        listingId: '10001',
      }),
      createCheckbox({
        sourceKind: 'shop',
        value: 'shop-disabled',
        checked: false,
        disabled: true,
        listingId: '10002',
      }),
      createCheckbox({
        sourceKind: 'local',
        value: 'local-hidden',
        checked: false,
        cardHidden: true,
        folder: 'folder-hidden',
        etsyStatus: '',
      }),
    ] : [],
    localBatchActions: [
      new MockElement({
        id: 'local-batch-btn',
        className: 'local-batch-action',
      }),
    ],
    crossShopBatchActions: [
      new MockElement({
        id: 'cross-shop-batch-btn',
        className: 'cross-shop-batch-action',
      }),
    ],
    shopBatchActions: [
      new MockElement({
        id: 'shop-batch-btn',
        className: 'shop-batch-action',
      }),
    ],
  });

  const selectAll = new MockElement({ id: 'cb-select-all', tagName: 'input' });
  const batchActions = new MockElement({ id: 'batch-actions' });
  const batchCountLabel = new MockElement({ id: 'batch-count-label' });
  const draftDeleteButton = new MockElement({
    id: 'shop-bulk-delete-drafts-btn',
    tagName: 'button',
  });

  batchActions.style.display = 'none';
  batchCountLabel.textContent = '';

  document._documentState = { selectAll, batchActions, batchCountLabel, draftDeleteButton };
  document.register(selectAll);
  document.register(batchActions);
  document.register(batchCountLabel);
  document.register(draftDeleteButton);

  return {
    document,
    selectAll,
    batchActions,
    batchCountLabel,
    localBatchAction: document._localBatchActions[0],
    shopBatchAction: document._shopBatchActions[0],
    crossShopBatchAction: document._crossShopBatchActions[0],
    checkboxes: document._productCheckboxes,
    currentProductSource,
  };
}

const context = {
  currentProductSource: 'aggregate',
  selectedCatalogIds: new Set(),
  BatchSelection,
  document: null,
};
context.window = context;
vm.createContext(context);
vm.runInContext(createFunctionBundle(), context, {
  filename: 'dashboard_static/app.batch-selection.js-harness',
});

let runtime = createCatalogState({ includeProducts: true });
context.currentProductSource = runtime.currentProductSource;
context.document = runtime.document;
context.document._documentState.batchCountLabel.textContent = '';

assert.equal(context.selectedCatalogIds.size, 0);

// 1) toggleSelectAll checked should only affect enabled and visible checkboxes.
runtime.selectAll.checked = true;
context.toggleSelectAll();
assert.equal(runtime.checkboxes[0].checked, true); // local visible
assert.equal(runtime.checkboxes[1].checked, true); // shop visible
assert.equal(runtime.checkboxes[2].checked, false); // shop disabled
assert.equal(runtime.checkboxes[3].checked, false); // local hidden

// 2) batch label + visibility for mixed aggregate selection.
assert.equal(runtime.batchCountLabel.textContent, '2 sản phẩm (1 local, 1 Etsy)');
assert.equal(runtime.batchActions.style.display, 'flex');
assert.equal(runtime.localBatchAction.style.display, '');
assert.equal(runtime.crossShopBatchAction.style.display, '');
assert.equal(runtime.shopBatchAction.style.display, '');

// 3) In shop source, a mapped local checkbox should show cross-shop action but hide local batch actions.
const shopSourceWithMapped = createCatalogState({ includeProducts: true, currentProductSource: 'shop' });
const [shopMappedLocal, shopRemoteCheckbox] = shopSourceWithMapped.document._productCheckboxes;
shopMappedLocal.checked = true;
const preShopSource = context.currentProductSource;
context.currentProductSource = shopSourceWithMapped.currentProductSource;
context.document = shopSourceWithMapped.document;
context.updateBatchUI();
assert.equal(shopSourceWithMapped.document._documentState.batchActions.style.display, 'flex');
assert.equal(shopSourceWithMapped.batchCountLabel.textContent, '1 sản phẩm');
assert.equal(shopSourceWithMapped.localBatchAction.style.display, 'none');
assert.equal(shopSourceWithMapped.crossShopBatchAction.style.display, '');
assert.equal(shopSourceWithMapped.shopBatchAction.style.display, 'none');

// 4) Shop source with both mapped local + Etsy keeps shop actions and cross-shop button.
shopRemoteCheckbox.checked = true;
context.updateBatchUI();
assert.equal(shopSourceWithMapped.document._documentState.batchActions.style.display, 'flex');
assert.equal(shopSourceWithMapped.batchCountLabel.textContent, '2 sản phẩm (1 local, 1 Etsy)');
assert.equal(shopSourceWithMapped.localBatchAction.style.display, 'none');
assert.equal(shopSourceWithMapped.crossShopBatchAction.style.display, '');
assert.equal(shopSourceWithMapped.shopBatchAction.style.display, '');

// 5) Recreate DOM with same keys and restore + update, keep enabled selections restored only.
const recreated = createCatalogState({ includeProducts: true });
const [reLocal, reShop, reDisabled, reHidden] = recreated.document._productCheckboxes;
context.currentProductSource = preShopSource;
const restoredKeys = [
  context.catalogSelectionKey(runtime.checkboxes[0]),
  context.catalogSelectionKey(runtime.checkboxes[1]),
  context.catalogSelectionKey(runtime.checkboxes[2]),
  context.catalogSelectionKey(runtime.checkboxes[3]),
];
context.selectedCatalogIds.clear();
for (const key of restoredKeys.slice(0, 2)) context.selectedCatalogIds.add(key);
context.document = recreated.document;
context.restoreCatalogSelections();
context.updateBatchUI();
assert.equal(reLocal.checked, true);
assert.equal(reShop.checked, true);
assert.equal(reDisabled.checked, false);
assert.equal(reHidden.checked, false);
assert.equal(recreated.document._documentState.batchActions.style.display, 'flex');

// 6) Toggle select-all off clears enabled selections and hides batch actions.
recreated.document._documentState.selectAll.checked = false;
context.toggleSelectAll();
assert.equal(reLocal.checked, false);
assert.equal(reShop.checked, false);
assert.equal(recreated.document._documentState.selectAll.checked, false);
assert.equal(recreated.document._documentState.batchActions.style.display, 'none');

// 7) Empty catalog should clear header/batch UI state.
const emptyRuntime = createCatalogState({ includeProducts: false });
context.currentProductSource = emptyRuntime.currentProductSource;
context.document = emptyRuntime.document;
context.updateBatchUI();
assert.equal(emptyRuntime.document._documentState.selectAll.checked, false);
assert.equal(emptyRuntime.document._documentState.batchActions.style.display, 'none');

console.log('batch select-all UI regression tests passed');
