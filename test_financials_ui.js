const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = __dirname;
const html = fs.readFileSync(path.join(root, 'dashboard_static/index.html'), 'utf8');
const app = fs.readFileSync(path.join(root, 'dashboard_static/app.js'), 'utf8');
const finance = fs.readFileSync(path.join(root, 'dashboard_static/financials.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'dashboard_static/financials.css'), 'utf8');

assert.match(html, /financials\.css\?v=20261002-provisional-01/);
assert.match(html, /financials\.js\?v=20261002-price-save-guidance-01/);
assert.match(html, /filter-profit-status/);
assert.match(html, /sort-profit/);
assert.match(html, /id="finance-footer-save"/);
assert.match(app, /new Set\(\['POST', 'PATCH', 'PUT', 'DELETE'\]\)/);

for (const contract of [
  '/financial-settings',
  '/financial-summaries',
  '/financials/calculate',
  'expected_version',
  'Thông tin sản phẩm',
  'Chi phí &amp; lợi nhuận',
  'buyer_tax',
  'free_shipping',
  'freight_discount_percent',
  'target_margin_percent',
  'representative_id',
  'saveProductFinancials',
  'saveFinanceSettings',
  'loadFinancialSummaries',
  'Không lưu được chi phí',
  'Có thay đổi chưa lưu',
  'shop!==getActiveShopId()',
]) {
  assert.ok(finance.includes(contract), `finance UI should preserve ${contract} contract`);
}

assert.match(finance, /Lợi nhuận dự kiến/);
assert.match(finance, /Offsite Ads/);
assert.match(finance, /processing cố định/i);
assert.match(finance, /4\.5/);
assert.match(finance, /1\.24/);
assert.match(finance, /11550/);
assert.match(finance, /500–11\.550/);
assert.match(finance, /Giá đề xuất chưa được lưu vào dashboard/);
assert.match(finance, /chỉ là lưu cục bộ/);
assert.match(finance, /nút <strong>Update<\/strong> của sản phẩm/);
assert.match(css, /\.fin-product-badge\.positive/);
assert.match(css, /\.fin-product-badge\.negative/);
assert.match(css, /\.fin-product-badge\.incomplete/);
assert.match(css, /\.modal\.finance-active\{width:min\(1400px,96vw\)!important/);

console.log('financial UI integration assertions passed');

function response(data, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => data };
}

async function runBehavioralChecks() {
  const elements = new Map();
  const queryLists = new Map();
  let fetchCalls = 0;
  const document = {
    getElementById(id) { return elements.get(id) || null; },
    querySelectorAll(selector) { return queryLists.get(selector) || []; },
    addEventListener() {},
  };
  const sandbox = {
    document,
    console,
    setTimeout: () => 0,
    clearTimeout() {},
    Event,
    URL,
    fetch: async () => { fetchCalls += 1; return response({ ok: true }); },
    window: { __FINANCIALS_TEST__(api) { sandbox.finance = api; }, confirm: () => true },
    toast() {},
    filterProducts() {},
    getActiveShopId() { return elements.get('shop-switcher')?.value || ''; },
  };
  vm.runInNewContext(finance, sandbox, { filename: 'financials.js' });
  const state = sandbox.finance.state;
  const normalizedCosts = JSON.parse(JSON.stringify(sandbox.finance.normalizeFinancials({
    costs: {
      cogs: { amount: '12', currency: 'VND' },
      packaging: null,
      labor: { amount: '', currency: 'VND' },
      other: { amount: '3', currency: 'EUR' },
      etsy_ads: { amount: '  ', currency: 'GBP' },
    },
  }).costs));
  assert.deepEqual(normalizedCosts, {
    cogs: { amount: '12', currency: 'VND' },
    packaging: { amount: '0', currency: 'USD' },
    labor: { amount: '0', currency: 'USD' },
    other: { amount: '3', currency: 'EUR' },
    etsy_ads: { amount: '0', currency: 'USD' },
  }, 'optional blank costs default to zero USD while explicit costs remain unchanged');
  elements.set('shop-switcher', { value: 'shop-a' });
  elements.set('fin-listing-type', { value: 'physical' });
  elements.set('fin-sale-discount', { value: '0' });
  elements.set('fin-target-margin', { value: '' });
  elements.set('edit-price', { value: '25', dispatchEvent(event) { this.lastEvent = event; } });
  elements.set('fin-results', { innerHTML: '' });
  elements.set('finance-save', { disabled: false });
  elements.set('finance-footer-save', { disabled: false, hidden: true });
  elements.set('finance-settings-save', { disabled: false });
  elements.set('finance-settings-status', { textContent: '' });
  elements.set('finance-content', { innerHTML: '' });
  const infoGrid = { hidden: false };
  const modalPanel = { classList: { toggle() {} } };
  elements.set('edit-modal', { dataset: {}, querySelector(selector) { return selector === '.edit-modal-grid' ? infoGrid : selector === '.modal' ? modalPanel : null; } });
  elements.set('finance-tab-panel', { hidden: false });
  elements.set('fin-tab-product', { setAttribute(name, value) { this[name] = value; } });
  elements.set('fin-tab-finance', { setAttribute(name, value) { this[name] = value; } });
  elements.set('fin-price-display', { textContent: '' });
  elements.set('fin-price-note', { textContent: '' });
  elements.set('fin-price-save-guidance', { hidden: true, innerHTML: '' });
  let dirtyStatusUpdated = false;
  sandbox.updateModalDirtyState = () => { dirtyStatusUpdated = true; };

  state.shop = 'shop-a';
  state.folder = 'product-01';
  state.settings = { listing_currency: 'USD', payout_currency: 'USD', fx_vnd_per_unit: { VND: '1', USD: null }, processing_fixed: { amount: null, currency: 'VND' }, listing_fee: { amount: '0.2', currency: 'USD' } };
  state.activeTab = 'finance';
  state.financials = { listing_type: 'physical', costs: {}, scenarios: [] };
  state.savedPrice = '2000';
  const fetchCallsBeforeSuggestion = fetchCalls;
  sandbox.finance.applyFinanceSuggestedPrice('1406.53');
  assert.equal(elements.get('edit-price').value, '1406.53', 'applying the recommendation should update the unsaved product price field');
  assert.equal(dirtyStatusUpdated, true, 'the suggested product price must be marked dirty');
  assert.equal(elements.get('fin-price-save-guidance').hidden, false, 'applying the recommendation should reveal save guidance');
  assert.match(elements.get('fin-price-save-guidance').innerHTML, /Mở tab Thông tin sản phẩm/);
  assert.match(elements.get('fin-price-save-guidance').innerHTML, /lưu cục bộ/);
  assert.match(elements.get('fin-price-save-guidance').innerHTML, /nút <strong>Update<\/strong>/, 'the guidance must distinguish local save from the separate Etsy Update action');
  assert.equal(fetchCalls, fetchCallsBeforeSuggestion, 'applying a suggested price must not auto-save or contact Etsy');
  sandbox.finance.switchFinanceTab('product');
  assert.equal(elements.get('finance-tab-panel').hidden, true, 'the CTA target should open the product-information tab');
  assert.equal(infoGrid.hidden, false);
  assert.equal(fetchCalls, fetchCallsBeforeSuggestion, 'opening the product-information tab must not save the price');
  queryLists.set('#finance-tab-panel [data-setting]', []);
  queryLists.set('#finance-tab-panel [data-setting-fx]', [
    { dataset: { settingFx: 'VND' }, value: '1' },
    { dataset: { settingFx: 'USD' }, value: '' },
    { dataset: { settingFx: 'EUR' }, value: '' },
  ]);
  const settings = sandbox.finance.readSettingsForm();
  assert.deepEqual(JSON.parse(JSON.stringify(settings.fx_vnd_per_unit)), { VND: '1' }, 'blank FX rates must stay absent from the payload');
  state.settings = { ...state.settings, tax_mode: 'vn_business' };
  assert.match(sandbox.finance.settingsMarkup(), /<option value="vn_business" selected>/, 'repainting settings must preserve the persisted tax mode');
  state.savedPrice = '800';
  assert.match(sandbox.finance.priceSimulationNote(), /mô phỏng giá chưa lưu/, 'a target-price preview must be identified as unsaved');
  elements.get('edit-price').value = '800';
  assert.match(sandbox.finance.priceSimulationNote(), /giá hiện tại đã lưu/i, 'the note should identify the saved workbook price');
  elements.get('edit-price').value = '25';

  state.financials = { listing_type: 'physical', costs: {}, scenarios: [] };
  queryLists.set('#finance-tab-panel [data-fin]', []);
  let resolveCalculation;
  sandbox.fetch = () => new Promise(resolve => { resolveCalculation = resolve; });
  const calculationPromise = sandbox.window.calculateProductFinancials();
  state.folder = 'product-02';
  resolveCalculation(response({ ok: true, calculation: { scenarios: [], missing: [] } }));
  await calculationPromise;
  assert.match(elements.get('fin-results').innerHTML, /Đang tính/, 'a response for the previous folder must not paint into a newly opened folder');

  state.folder = 'product-02';
  state.settingsVersion = 2;
  state.version = 2;
  state.settings = { listing_currency: 'USD', payout_currency: 'USD', bank_country: 'VN', fx_date: '2026-10-01', fx_vnd_per_unit: { VND: '1' }, transaction_percent: '6.5', processing_percent: '4.5', regulatory_percent: '1.24', conversion_percent: '0', fee_vat_percent: '0', processing_fixed: { amount: '11550', currency: 'VND' }, listing_fee: { amount: '0.2', currency: 'USD' }, tax_mode: 'none', confirmed: true, source: {} };
  const calls = [];
  sandbox.fetch = async (url, options = {}) => {
    calls.push({ url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null });
    if (options.method === 'PUT') return response({ ok: true, version: 3 });
    if (url.endsWith('/financial-settings')) return response({ ok: true, version: 3, settings: calls[0].body.settings });
    if (url.endsWith('/financial-summaries')) return response({ ok: true, summaries: {} });
    return response({ ok: true });
  };
  queryLists.set('#finance-tab-panel [data-setting]', []);
  queryLists.set('#finance-tab-panel [data-setting-fx]', [{ dataset: { settingFx: 'VND' }, value: '1' }]);
  await sandbox.window.saveFinanceSettings();
  assert.deepEqual(calls.map(call => call.method), ['PUT', 'GET', 'GET'], 'settings save must PUT, read back, then refresh batch summaries');
  assert.equal(state.version, 3, 'shared version must advance after settings save');

  state.financials = { listing_type: 'physical', costs: {}, scenarios: [] };
  let releaseProductPut;
  let savedFinancials;
  const previousCallCount = calls.length;
  sandbox.fetch = async (url, options = {}) => {
    calls.push({ url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null });
    if (options.method === 'PUT') { savedFinancials=calls.at(-1).body.financials;return new Promise(resolve=>{releaseProductPut=()=>resolve(response({ ok: true, version: 4 }));}); }
    if (url.endsWith('/financial-summaries')) return response({ ok: true, summaries: {} });
    return response({ ok: true, version: 4, financials: savedFinancials, calculation: null });
  };
  const firstSave = sandbox.window.saveProductFinancials();
  await Promise.resolve();
  await sandbox.window.saveProductFinancials();
  assert.equal(calls.slice(previousCallCount).filter(call => call.method === 'PUT').length, 1, 'top and footer save actions must share a duplicate-request guard');
  releaseProductPut();
  await firstSave;
  assert.equal(state.settingsVersion, 4, 'product save must advance the settings version for the shared store');

  state.shop = 'shop-a';
  state.folder = 'product-02';
  state.version = 4;
  state.settingsVersion = 4;
  state.financials = { listing_type: 'physical', costs: { cogs: { amount: '12', currency: 'VND' } }, scenarios: [] };
  queryLists.set('#finance-tab-panel [data-fin]', []);
  sandbox.fetch = async () => response({ ok: false, error: 'simulated failure' }, 500);
  await sandbox.window.saveProductFinancials();
  assert.equal(state.financials.costs.cogs.amount, '12', 'failed saves must retain financial inputs in memory for correction and retry');
}

runBehavioralChecks().then(() => console.log('financial UI behavioral checks passed')).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
