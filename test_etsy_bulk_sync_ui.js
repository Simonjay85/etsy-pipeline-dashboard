const fs = require('node:fs');
const assert = require('node:assert/strict');
const vm = require('node:vm');

const indexHtml = fs.readFileSync('dashboard_static/index.html', 'utf8');
const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');
assert.match(indexHtml, /id="local-batch-pull-btn"[^>]*onclick="batchSyncFromEtsy\(\)"/);
assert.match(indexHtml, /id="local-batch-update-btn"[^>]*onclick="openBulkEtsyUpdateModal\(\)"/);
assert.match(indexHtml, /class="etsy-bulk-update-field" value="title" checked/);
assert.match(indexHtml, /class="etsy-bulk-update-field" value="images">/);
assert.match(indexHtml, /Đây là thao tác ghi LIVE lên các listing Etsy hiện tại/);
assert.match(indexHtml, /style\.css\?v=20260809-bulk-sync-ui-safety-01/);
assert.match(indexHtml, /app\.js\?v=20260809-bulk-sync-ui-safety-01/);

const start = appJs.indexOf('function selectedLocalEtsyMappings()');
const end = appJs.indexOf('\nasync function openFolder(', start);
assert.ok(start >= 0 && end > start, 'Unable to isolate bulk Etsy sync functions');

function element(extra = {}) {
  const attrs = Object.create(null);
  const classSet = new Set(String(extra.className || '').split(/\s+/).filter(Boolean));
  const node = {
    disabled: false,
    innerHTML: '',
    textContent: '',
    className: extra.className || '',
    checked: false,
    value: '',
    dataset: {},
    style: {},
    _attributes: attrs,
    setAttribute(name, value) {
      attrs[String(name)] = String(value);
    },
    getAttribute(name) {
      return attrs[String(name)];
    },
    ...extra,
  };
  node.classList = {
    add(...tokens) {
      tokens.forEach((token) => {
        if (token) classSet.add(token);
      });
      node.className = Array.from(classSet).join(' ');
    },
    remove(...tokens) {
      tokens.forEach((token) => {
        classSet.delete(token);
      });
      node.className = Array.from(classSet).join(' ');
    },
    contains(token) {
      return classSet.has(token);
    },
  };
  return node;
}

function createHarness({resolveSync, loadProducts} = {}) {
  const checkboxes = [
    element({checked: true, value: '121', dataset: {folder: 'product-121'}}),
    element({checked: true, value: '122', dataset: {folder: 'product-122'}}),
    element({checked: true, value: '276', dataset: {folder: 'product-276'}}),
  ];
  const bulkFields = [
    element({checked: true, value: 'title'}),
    element({checked: true, value: 'description'}),
    element({checked: true, value: 'tags'}),
    element({checked: true, value: 'price'}),
    element({checked: true, value: 'qty'}),
    element({checked: false, value: 'images'}),
    element({checked: false, value: 'files'}),
  ];
  const toasts = [];
  const confirms = [];
  const fetchBodies = [];
  const elements = {
    'shop-switcher': element({value: 'templystudios'}),
    'local-batch-pull-btn': element({innerHTML: '⬇ Etsy → Local'}),
    'etsy-bulk-update-count': element(),
    'etsy-bulk-update-shop': element(),
    'etsy-bulk-update-run-status': element(),
    'etsy-bulk-update-submit': element({innerHTML: '⬆ Ghi LIVE lên Etsy'}),
    'etsy-bulk-update-cancel': element(),
    'etsy-bulk-update-close-x': element(),
    'etsy-bulk-sync-panel': element({className: 'hidden'}),
    'etsy-bulk-sync-meta': element(),
    'etsy-bulk-sync-progress-bar': element({style: {width: '0%'}}),
    'etsy-bulk-sync-progress-text': element(),
    'etsy-bulk-sync-current': element(),
    'etsy-bulk-sync-summary': element(),
    'cb-select-all': element(),
  };
  const actionButtons = [
    elements['local-batch-pull-btn'],
    element({disabled: true, innerHTML: 'pre-disabled'}),
    elements['etsy-bulk-update-submit'],
  ];

  let phase = 'pull';
  let activeRequests = 0;
  let maxActiveRequests = 0;
  let activeUpdater = null;
  const jobPolls = new Map();

  const defaultSync = ({body}) => {
    if (body.folder === 'product-122') {
      return {ok: false, status: 500, json: async () => ({ok: false, error: 'asset failed'})};
    }
    return {ok: true, status: 200, json: async () => ({ok: true, sync_ok: true})};
  };

  const resolver = resolveSync || defaultSync;

  const sandbox = {
    module: {exports: {}},
    exports: {},
    allProducts: [
      {row: 121, folder: 'product-121', etsy_url: 'https://www.etsy.com/listing/4529147696/a'},
      {row: 122, folder: 'product-122', etsy_url: 'https://www.etsy.com/listing/4529136743/b'},
      {row: 276, folder: 'product-276', etsy_url: ''},
    ],
    document: {
      getElementById(id) { return elements[id] || null; },
      querySelectorAll(selector) {
        if (selector === '.product-cb:checked') return checkboxes.filter(item => item.checked);
        if (selector === '.product-cb') return checkboxes;
        if (selector === '.local-batch-action, .btn-sync, .btn-update') return actionButtons;
        if (selector === '.etsy-bulk-update-field') return bulkFields;
        if (selector === '.etsy-bulk-update-field:checked') return bulkFields.filter(item => item.checked);
        throw new Error(`Unexpected selector: ${selector}`);
      },
    },
    selectedBatchCheckboxes(kind) {
      assert.equal(kind, 'local');
      return checkboxes.filter(item => item.checked);
    },
    updateBatchUI() {},
    loadProducts: loadProducts || (async () => sandbox.allProducts),
    openModal(id) { assert.equal(id, 'etsy-bulk-update-modal'); },
    escHtml(value) { return String(value); },
    sleep: async () => {},
    toast(level, message) { toasts.push({level, message}); },
    confirm(message) { confirms.push(message); return true; },
    async fetch(url, options = {}) {
      if (url.includes('/sync-from-etsy')) {
        assert.equal(phase, 'pull');
        activeRequests += 1;
        maxActiveRequests = Math.max(maxActiveRequests, activeRequests);
        let body;
        try {
          body = JSON.parse(options.body);
          fetchBodies.push({url, body});
          const result = await resolver({
            url,
            body,
            options,
            callIndex: fetchBodies.length,
          });
          return result;
        } finally {
          activeRequests -= 1;
        }
      }
      if (url.includes('/push-to-etsy')) {
        assert.equal(phase, 'push');
        assert.equal(activeUpdater, null, 'Next updater started before previous job reached a terminal status');
        const body = JSON.parse(options.body);
        fetchBodies.push({url, body});
        const jobId = body.folder === 'product-121' ? 'job-121' : 'job-122';
        activeUpdater = jobId;
        jobPolls.set(jobId, 0);
        return {ok: true, status: 200, json: async () => ({ok: true, job_id: jobId})};
      }
      if (url.includes('/api/etsy/update-status')) {
        const jobId = decodeURIComponent(url.split('job_id=')[1]);
        assert.equal(activeUpdater, jobId);
        if (jobId === 'job-timeout') {
          return {ok: true, status: 200, json: async () => ({ok: true, status: 'running', last_message: 'still running'})};
        }
        const polls = (jobPolls.get(jobId) || 0) + 1;
        jobPolls.set(jobId, polls);
        if (polls === 1) {
          return {ok: true, status: 200, json: async () => ({ok: true, status: 'running', last_message: 'running'})};
        }
        activeUpdater = null;
        if (jobId === 'job-122') {
          return {ok: true, status: 200, json: async () => ({ok: true, status: 'error', last_message: 'save failed'})};
        }
        return {ok: true, status: 200, json: async () => ({ok: true, status: 'success', last_message: 'done'})};
      }
      throw new Error(`Unexpected fetch: ${url}`);
    },
    console,
    Set,
    Promise,
    JSON,
    Number,
    String,
    Array,
    encodeURIComponent,
  };

  vm.runInNewContext(
    `let etsyBulkSyncInFlight = false;\nlet etsySingleSyncInFlight = false;\nlet etsyBulkUpdateSelection = null;\n${appJs.slice(start, end)}\n` +
    'module.exports = {batchSyncFromEtsy, openBulkEtsyUpdateModal, submitBulkEtsyUpdate, waitForEtsyUpdateJob};',
    sandbox,
  );

  return {
    module: sandbox.module.exports,
    checkboxes,
    bulkFields,
    elements,
    actionButtons,
    toasts,
    confirms,
    fetchBodies,
    getState() {
      return {
        maxActiveRequests,
        getPanelAriaBusy() {
          return elements['etsy-bulk-sync-panel'].getAttribute('aria-busy');
        },
      };
    },
    jobPolls,
    setPhase(next) {
      phase = next;
    },
    setActiveUpdater(next) {
      activeUpdater = next;
    },
  };
}

(async () => {
  // Mixed result + sequentiality + live-push flow (existing coverage preserved)
  const baseline = createHarness();
  await baseline.module.batchSyncFromEtsy();
  assert.equal(baseline.getState().maxActiveRequests, 1, 'Etsy -> Local requests must be sequential');
  assert.deepEqual(baseline.fetchBodies.slice(0, 2), [
    {
      url: '/api/products/121/sync-from-etsy',
      body: {shop: 'templystudios', folder: 'product-121', listing_id: '4529147696'},
    },
    {
      url: '/api/products/122/sync-from-etsy',
      body: {shop: 'templystudios', folder: 'product-122', listing_id: '4529136743'},
    },
  ]);
  assert.match(baseline.confirms[0], /2 listing hợp lệ.*shop templystudios/);
  assert.match(baseline.confirms[0], /Bỏ qua 1\/3 sản phẩm chưa ghép/);
  assert.equal(baseline.checkboxes[0].checked, false, 'Successful pull should be cleared after refresh');
  assert.equal(baseline.checkboxes[1].checked, true, 'Failed pull should remain selected');
  assert.equal(baseline.checkboxes[2].checked, true, 'Unmapped Temply product should be skipped and remain selected');
  assert.equal(baseline.elements['etsy-bulk-sync-panel'].className.includes('hidden'), false, 'Progress panel must stay visible for final batch summary');
  assert.equal(baseline.elements['etsy-bulk-sync-panel'].className.includes('is-error'), true, 'Progress panel should expose error summary state');
  assert.equal(baseline.elements['etsy-bulk-sync-meta'].textContent, '3/3 đã xử lý · 1 thành công · 1 lỗi request · 1 mapping');
  assert.equal(baseline.elements['etsy-bulk-sync-progress-bar'].style.width, '100%');
  assert.equal(baseline.elements['etsy-bulk-sync-progress-bar'].getAttribute('aria-valuenow'), '100');
  assert.match(baseline.elements['etsy-bulk-sync-progress-text'].textContent, /^⚠️ Etsy → Local: 1 thành công, 1 lỗi request, 0 chưa thử, 0 đã xếp hàng, 1 bỏ qua mapping\./);
  assert.match(baseline.elements['etsy-bulk-sync-summary'].textContent, /Theo nhóm: .*1 request.*1 mapping|Theo nhóm: .*1 mapping.*1 request/);
  assert.equal(baseline.elements['etsy-bulk-sync-current'].textContent, 'Không còn listing đang xử lý');
  assert.ok(baseline.toasts.some(item => item.message.includes('1 thành công, 1 lỗi request, 0 chưa thử, 0 đã xếp hàng, 1 bỏ qua mapping')));
  assert.equal(baseline.actionButtons[1].disabled, true, 'A pre-disabled action must stay disabled');

  baseline.checkboxes.forEach(item => { item.checked = true; });
  baseline.setPhase('push');
  baseline.module.openBulkEtsyUpdateModal();
  assert.equal(baseline.elements['etsy-bulk-update-shop'].textContent, 'templystudios');
  assert.equal(baseline.elements['etsy-bulk-update-count'].textContent, '2 hợp lệ · 1 bỏ qua');
  assert.equal(baseline.bulkFields.find(item => item.value === 'title').checked, true);
  assert.equal(baseline.bulkFields.find(item => item.value === 'images').checked, false);
  await baseline.module.submitBulkEtsyUpdate();

  assert.match(baseline.confirms[1], /GHI LIVE 2 sản phẩm Local hợp lệ lên shop Etsy templystudios/);
  assert.match(baseline.confirms[1], /Bỏ qua 1\/3 sản phẩm chưa ghép/);
  assert.match(baseline.confirms[1], /TUẦN TỰ, không chạy đồng thời/);
  const pushCalls = baseline.fetchBodies.slice(2);
  assert.deepEqual(pushCalls.map(call => call.body), [
    {
      fields: ['title', 'description', 'tags', 'price', 'qty'],
      shop: 'templystudios', folder: 'product-121', listing_id: '4529147696',
    },
    {
      fields: ['title', 'description', 'tags', 'price', 'qty'],
      shop: 'templystudios', folder: 'product-122', listing_id: '4529136743',
    },
  ]);
  assert.equal(baseline.jobPolls.get('job-121'), 2);
  assert.equal(baseline.jobPolls.get('job-122'), 2);
  assert.equal(baseline.checkboxes[0].checked, false);
  assert.equal(baseline.checkboxes[1].checked, true, 'Failed live update should remain selected');
  assert.equal(baseline.checkboxes[2].checked, true, 'Unmapped product must never be pushed and remains selected');
  assert.match(baseline.elements['etsy-bulk-update-run-status'].textContent, /1 thành công, 2 lỗi\/bỏ qua/);

  baseline.setActiveUpdater('job-timeout');
  await assert.rejects(
    baseline.module.waitForEtsyUpdateJob('job-timeout', null, {intervalMs: 0, maxAttempts: 2}),
    /Hết thời gian chờ cập nhật Etsy hoàn tất/,
  );

  baseline.setActiveUpdater(null);

  // Queued acceptance (202/queued) must not count as completed success and must surface backend queue state.
  const queuedSyncHarness = createHarness({
    resolveSync({body}) {
      if (body.folder === 'product-121') {
        return {ok: true, status: 202, json: async () => ({ok: true, queued: true})};
      }
      return {ok: true, status: 200, json: async () => ({ok: true, sync_ok: true})};
    },
  });
  await queuedSyncHarness.module.batchSyncFromEtsy();
  assert.match(queuedSyncHarness.elements['etsy-bulk-sync-progress-text'].textContent, /đã xếp hàng|backend queue|Backend queue/);
  assert.match(queuedSyncHarness.elements['etsy-bulk-sync-meta'].textContent, /^3\/3 đã xử lý · 1 thành công · 1 đã xếp hàng · 0 lỗi request · 1 mapping$/);
  assert.equal(queuedSyncHarness.elements['etsy-bulk-sync-current'].textContent, 'Backend queue đang xử lý các listing đã xếp hàng');
  assert.equal(queuedSyncHarness.elements['etsy-bulk-sync-panel'].className.includes('is-running'), true, 'Queued run should remain in running-state');

  // 409 busy case: stop immediately, no second request, keep current + pending selected
  const busySyncHarness = createHarness({
    resolveSync({body}) {
      if (body.folder === 'product-121') {
        return {ok: false, status: 409, json: async () => ({ok: false, code: 'etsy_sync_busy'})};
      }
      return {ok: true, status: 200, json: async () => ({ok: true, sync_ok: true})};
    },
  });
  await busySyncHarness.module.batchSyncFromEtsy();
  assert.equal(busySyncHarness.fetchBodies.length, 1, 'Busy response should stop before issuing second request');
  assert.equal(busySyncHarness.getState().maxActiveRequests, 1, 'Busy stop still keeps sequential call behavior');
  assert.equal(busySyncHarness.getState().getPanelAriaBusy(), 'false', 'Busy block should stop and clear aria-busy');
  assert.equal(busySyncHarness.checkboxes[0].checked, true, 'Current busy item must remain selected');
  assert.equal(busySyncHarness.checkboxes[1].checked, true, 'Pending item must remain selected');
  assert.equal(busySyncHarness.checkboxes[2].checked, true, 'Rejected listing stays selected');
  assert.match(busySyncHarness.elements['etsy-bulk-sync-summary'].textContent, /etsy_sync_busy|đang bận|bị khóa/);
  assert.equal(busySyncHarness.elements['etsy-bulk-sync-current'].textContent, 'Không còn listing đang xử lý');
  assert.match(busySyncHarness.elements['etsy-bulk-sync-meta'].textContent, /^1\/3 đã xử lý · 0 thành công · 2 chưa thử · 0? lỗi request · 1 mapping( · 0? đã xếp hàng)?$/);

  // 403 security middleware: fail-fast after first response, keep one-fetch only, actionable reload hint.
  const securitySyncHarness = createHarness({
    resolveSync({body}) {
      if (body.folder === 'product-121') {
        return {
          ok: false,
          status: 403,
          json: async () => ({ok: false, detail: 'invalid token', error: 'forbidden'}),
        };
      }
      return {ok: true, status: 200, json: async () => ({ok: true, sync_ok: true})};
    },
  });
  await securitySyncHarness.module.batchSyncFromEtsy();
  assert.equal(securitySyncHarness.fetchBodies.length, 1, 'Security 403 must stop immediately before issuing remaining requests');
  assert.equal(securitySyncHarness.elements['etsy-bulk-sync-meta'].textContent.includes('/3'), true, 'Progress should keep 3 as denominator');
  assert.equal(securitySyncHarness.elements['etsy-bulk-sync-current'].textContent, 'Không còn listing đang xử lý');
  assert.match(securitySyncHarness.elements['etsy-bulk-sync-progress-text'].textContent, /reload dashboard|host[- ]or|token|security/i);
  assert.match(securitySyncHarness.elements['etsy-bulk-sync-summary'].textContent, /Theo nhóm: .*1 mapping.*2 chưa thử|Theo nhóm: .*2 chưa thử.*1 mapping/);
  assert.match(securitySyncHarness.elements['etsy-bulk-sync-summary'].textContent, /2 chưa thử/);
  assert.match(securitySyncHarness.elements['etsy-bulk-sync-summary'].textContent, /invalid token/);
  assert.equal(securitySyncHarness.checkboxes[0].checked, true, 'Current item must remain selected after security fail-fast');
  assert.equal(securitySyncHarness.checkboxes[1].checked, true, 'Pending item must remain selected after security fail-fast');
  assert.equal(securitySyncHarness.checkboxes[2].checked, true, 'Unmapped listing should remain selected');
  assert.ok(securitySyncHarness.toasts.some(item => /reload|reload dashboard|host|token/i.test(item.message)), 'Security fail should expose reload/actionable text');

  // Rejected fetch case: unknown/unreadable request outcomes stop immediately and do not advance counters
  const rejectedFetchHarness = createHarness({
    resolveSync({body}) {
      if (body.folder === 'product-121') {
        throw new Error('network is down');
      }
      return {ok: true, status: 200, json: async () => ({ok: true, sync_ok: true})};
    },
  });
  await rejectedFetchHarness.module.batchSyncFromEtsy();
  assert.equal(rejectedFetchHarness.fetchBodies.length, 1, 'Fetch rejection should stop immediately without second request');
  assert.equal(rejectedFetchHarness.getState().getPanelAriaBusy(), 'false', 'Unknown error stop should clear aria-busy');
  assert.match(rejectedFetchHarness.elements['etsy-bulk-sync-meta'].textContent, /^1\/3 đã xử lý · 0 thành công · 2 chưa thử · 0? lỗi request · 1 mapping( · 0? đã xếp hàng)?$/);
  assert.equal(rejectedFetchHarness.checkboxes[0].checked, true, 'Current item should remain selected after unknown interruption');
  assert.equal(rejectedFetchHarness.checkboxes[1].checked, true, 'Pending item should remain selected after unknown interruption');
  assert.equal(rejectedFetchHarness.checkboxes[2].checked, true, 'Rejected mapping should remain selected');
  assert.equal(rejectedFetchHarness.elements['etsy-bulk-sync-current'].textContent, 'Không còn listing đang xử lý');

  // Unreadable JSON is also ambiguous: stop without counting the current item.
  const unreadableResponseHarness = createHarness({
    resolveSync({body}) {
      if (body.folder === 'product-121') {
        return {
          ok: true,
          status: 200,
          json: async () => { throw new Error('invalid json'); },
        };
      }
      return {ok: true, status: 200, json: async () => ({ok: true, sync_ok: true})};
    },
  });
  await unreadableResponseHarness.module.batchSyncFromEtsy();
  assert.equal(unreadableResponseHarness.fetchBodies.length, 1, 'Unreadable JSON should stop before the next request');
  assert.equal(unreadableResponseHarness.getState().getPanelAriaBusy(), 'false', 'Unreadable JSON should clear aria-busy');
  assert.match(unreadableResponseHarness.elements['etsy-bulk-sync-meta'].textContent, /^1\/3 đã xử lý · 0 thành công · 2 chưa thử · 0 lỗi request · 1 mapping$/);
  assert.equal(unreadableResponseHarness.checkboxes[0].checked, true, 'Current item should remain selected after unreadable JSON');
  assert.equal(unreadableResponseHarness.checkboxes[1].checked, true, 'Pending item should remain selected after unreadable JSON');
  assert.equal(unreadableResponseHarness.checkboxes[2].checked, true, 'Rejected mapping should remain selected after unreadable JSON');

  // Regression: if loadProducts throws after full successful loop, do not reclassify already-processed items.
  const loadProductsThrowAfterSuccessHarness = createHarness({
    loadProducts: async () => { throw new Error('loadProducts failed after success'); },
    resolveSync({body}) {
      return {ok: true, status: 200, json: async () => ({ok: true, sync_ok: true})};
    },
  });
  await loadProductsThrowAfterSuccessHarness.module.batchSyncFromEtsy();
  assert.equal(loadProductsThrowAfterSuccessHarness.fetchBodies.length, 2, 'Both valid items should be attempted and completed before refresh failure');
  assert.equal(loadProductsThrowAfterSuccessHarness.checkboxes[0].checked, true, 'First success selection should stay as-is when loadProducts fails after loop');
  assert.equal(loadProductsThrowAfterSuccessHarness.checkboxes[1].checked, true, 'Second success selection should stay as-is when loadProducts fails after loop');
  assert.equal(loadProductsThrowAfterSuccessHarness.checkboxes[2].checked, true, 'Unmapped item should remain selected as preflight rejected');
  assert.match(loadProductsThrowAfterSuccessHarness.elements['etsy-bulk-sync-meta'].textContent, /^3\/3 đã xử lý · 2 thành công · 0 lỗi request( · 0 chưa thử)? · 1 mapping/);
  assert.equal(loadProductsThrowAfterSuccessHarness.elements['etsy-bulk-sync-current'].textContent, 'Không còn listing đang xử lý');
  const lastToast = loadProductsThrowAfterSuccessHarness.toasts[loadProductsThrowAfterSuccessHarness.toasts.length - 1];
  assert.ok(lastToast && /loadProducts failed after success/i.test(lastToast.message), 'LoadProducts exception should bubble as batch failure');

  console.log('etsy bulk bidirectional sync UI tests passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
