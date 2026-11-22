/* Per-product finance workspace. Calculations are always returned by the server. */
(() => {
  'use strict';
  const esc = value => typeof escHtml === 'function' ? escHtml(value) : String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const CURRENCIES = ['VND', 'USD', 'EUR', 'GBP', 'AUD', 'CAD', 'JPY'];
  const money = value => value == null || value === '' ? '—' : Number(value).toLocaleString('vi-VN', {maximumFractionDigits: 2});
  const state = { shop: '', folder: '', request: 0, editorGeneration: 0, summaryRequest: 0, financials: null, baseline: '', version: null, calculation: null, settings: null, settingsVersion: null, settingsBaseline: '', loading: false, error: '', savingFinancials: false, timer: null, summaries: {}, infoBaseline: null, activeTab: 'product', lastPrice: '', savedPrice: '', suggestedPriceApplied: false };
  const q = id => document.getElementById(id);
  const clone = value => JSON.parse(JSON.stringify(value));
  const stable = value => JSON.stringify(value, (key, item) => item && typeof item === 'object' && !Array.isArray(item)
    ? Object.keys(item).sort().reduce((sorted, name) => { sorted[name] = item[name]; return sorted; }, {}) : item);
  const api = (shop, suffix) => `/api/shops/${encodeURIComponent(shop)}${suffix}`;
  const productApi = (shop, folder) => api(shop, `/products/${encodeURIComponent(folder)}/financials`);
  const amount = (v, currency = 'VND') => ({ amount: v === '' || v == null ? null : String(v), currency });
  const defaultScenario = () => ({ id: `shipping-${Date.now()}-${Math.random().toString(36).slice(2,6)}`, name: 'Mỹ · tiêu chuẩn', country: 'US', freight: amount(null), freight_discount_percent: '0', insurance: amount('0'), buyer_shipping: amount('0'), free_shipping: true, quote_date: '', notes: '', buyer_tax: amount('0','USD'), offsite_percent: 0 });
  const OPTIONAL_ZERO_COSTS = ['packaging', 'labor', 'other', 'etsy_ads'];
  const zeroUsdCost = () => amount('0', 'USD');
  const blankCost = value => value == null || (typeof value === 'object' && (value.amount == null || String(value.amount).trim() === ''));
  const defaultFinancials = () => ({ listing_type: 'physical', costs: { cogs: amount(null), packaging: zeroUsdCost(), labor: zeroUsdCost(), other: zeroUsdCost(), etsy_ads: zeroUsdCost() }, discount_percent: '0', target_margin_percent: null, representative_id: null, scenarios: [defaultScenario()] });
  const defaultSettings = () => ({ listing_currency: 'USD', payout_currency: 'USD', bank_country: '', fx_vnd_per_unit: {VND:'1'}, fx_date: '', processing_percent: '', processing_fixed: amount(null), transaction_percent: '6.5', regulatory_percent: '', conversion_percent: '', listing_fee: amount('0.20', 'USD'), fee_vat_percent: '', tax_mode: 'unknown', confirmed: false, source: { label: 'Tham chiếu Etsy; cần đối chiếu Payment Account', checked_date: '2026-10-01' } });
  const normalizeSettings = value => { const defaults=defaultSettings(), input=value&&typeof value==='object'?value:{}, rates={...defaults.fx_vnd_per_unit,...(input.fx_vnd_per_unit||{})};Object.keys(rates).forEach(currency=>{if(rates[currency]==null||rates[currency]==='')delete rates[currency];});if(!rates.VND)rates.VND='1';return {...defaults,...input,processing_fixed:{...defaults.processing_fixed,...(input.processing_fixed||{})},listing_fee:{...defaults.listing_fee,...(input.listing_fee||{})},source:{...defaults.source,...(input.source||{})},fx_vnd_per_unit:rates}; };
  const normalizeFinancials = value => { const defaults=defaultFinancials(),input=value&&typeof value==='object'?value:{},costs={...defaults.costs,...(input.costs||{})};OPTIONAL_ZERO_COSTS.forEach(name=>{if(blankCost(costs[name]))costs[name]=zeroUsdCost();});return {...defaults,...input,costs,scenarios:Array.isArray(input.scenarios)&&input.scenarios.length?input.scenarios:defaults.scenarios}; };
  const currencyOptions = selected => CURRENCIES.map(c => `<option value="${c}" ${c===selected?'selected':''}>${c}</option>`).join('');
  const currencyField = (id, selected) => `<select data-fin="${id}" aria-label="Tiền tệ">${currencyOptions(selected || 'VND')}</select>`;
  const amountField = (id, value, label, preferredCurrency='VND') => `<label class="fin-field"><span>${label}</span><div class="fin-amount"><input data-fin="${id}" type="number" min="0" step="any" value="${esc(value?.amount ?? '')}" placeholder="Chưa nhập" aria-label="${label}">${currencyField(`${id}.currency`, value?.currency || preferredCurrency)}</div></label>`;
  function editorShell() {
    const modal = q('edit-modal'); if (!modal || q('finance-tab-panel')) return;
    const body = modal.querySelector('.modal-body-edit');
    body.insertAdjacentHTML('afterbegin', `<nav class="fin-tabs" role="tablist" aria-label="Chỉnh sửa sản phẩm"><button type="button" role="tab" id="fin-tab-product" aria-selected="true" onclick="switchFinanceTab('product')">Thông tin sản phẩm</button><button type="button" role="tab" id="fin-tab-finance" aria-selected="false" onclick="switchFinanceTab('finance')">Chi phí &amp; lợi nhuận</button></nav>`);
    body.querySelector('.fin-tabs').insertAdjacentHTML('afterend', `<div id="fin-price-save-guidance" class="fin-section fin-note" role="status" aria-live="polite" hidden></div>`);
    body.insertAdjacentHTML('beforeend', `<section id="finance-tab-panel" class="fin-panel" role="tabpanel" aria-labelledby="fin-tab-finance" hidden><div id="finance-content"><div class="fin-loading">Mở sản phẩm để tải chi phí…</div></div></section>`);
    q('edit-modal').dataset.unsavedCheck = 'false';
    body.addEventListener('input', financeInput);
    body.addEventListener('change', financeInput);
  }
  function switchFinanceTab(tab) {
    state.activeTab = tab;
    const finance = tab === 'finance';
    q('finance-tab-panel').hidden = !finance;
    q('edit-modal').querySelector('.edit-modal-grid').hidden = finance;
    q('edit-modal').querySelector('.modal').classList.toggle('finance-active', finance);
    const footerSave=q('finance-footer-save');if(footerSave)footerSave.hidden=!finance;
    q('fin-tab-product').setAttribute('aria-selected', String(!finance));
    q('fin-tab-finance').setAttribute('aria-selected', String(finance));
    if (finance && state.shop && state.folder && !state.financials) void loadFinance(state.shop, state.folder);
  }
  function section(title, content) { return `<section class="fin-section"><h3>${title}</h3>${content}</section>`; }
  function settingsMarkup() {
    const s = state.settings || defaultSettings();
    return `<details class="fin-settings"><summary>Cấu hình phí và tỷ giá của shop</summary><p class="fin-note">Thiết lập theo Payment Account. Các ô để trống vẫn được xem là chưa xác nhận và sẽ không tự tính thành 0.</p>
      <div class="fin-grid">
        <label class="fin-field"><span>Tiền tệ niêm yết</span><select data-setting="listing_currency">${currencyOptions(s.listing_currency)}</select></label>
        <label class="fin-field"><span>Tiền nhận về</span><select data-setting="payout_currency">${currencyOptions(s.payout_currency)}</select></label>
        <label class="fin-field"><span>Quốc gia tài khoản ngân hàng (ISO)</span><input data-setting="bank_country" maxlength="2" value="${esc(s.bank_country)}" placeholder="VN"></label>
        ${CURRENCIES.map(currency=>`<label class="fin-field"><span>Tỷ giá VND cho 1 ${currency}</span><input data-setting-fx="${currency}" type="number" min="0" step="any" value="${esc(s.fx_vnd_per_unit?.[currency] ?? '')}" ${currency==='VND'?'readonly title="Tỷ giá đồng nhất"':''} placeholder="Nhập thủ công"></label>`).join('')}
        <label class="fin-field"><span>Ngày tỷ giá</span><input data-setting="fx_date" type="date" value="${esc(s.fx_date)}"></label>
        <label class="fin-field"><span>Transaction fee (%)</span><input data-setting="transaction_percent" type="number" step="any" value="${esc(s.transaction_percent)}"></label>
        <label class="fin-field"><span>Processing fee (%)</span><input data-setting="processing_percent" type="number" step="any" value="${esc(s.processing_percent)}"></label>
        ${amountFieldSetting('processing_fixed', s.processing_fixed, 'Processing cố định', 'VND')}
        <label class="fin-field"><span>Regulatory fee (%)</span><input data-setting="regulatory_percent" type="number" step="any" value="${esc(s.regulatory_percent)}"></label>
        <label class="fin-field"><span>Phí đổi tiền (%)</span><input data-setting="conversion_percent" type="number" step="any" value="${esc(s.conversion_percent)}"></label>
        ${amountFieldSetting('listing_fee', s.listing_fee, 'Listing fee / đơn vị', 'USD')}
        <label class="fin-field"><span>VAT trên phí sàn (%)</span><input data-setting="fee_vat_percent" type="number" step="any" value="${esc(s.fee_vat_percent)}"></label>
        <label class="fin-field"><span>Chế độ thuế</span><select data-setting="tax_mode"><option value="unknown" ${s.tax_mode==='unknown'?'selected':''}>Chưa xác nhận</option><option value="vn_individual" ${s.tax_mode==='vn_individual'?'selected':''}>Việt Nam · cá nhân</option><option value="vn_business" ${s.tax_mode==='vn_business'?'selected':''}>Việt Nam · doanh nghiệp</option><option value="none" ${s.tax_mode==='none'?'selected':''}>Không áp dụng</option></select></label>
        <label class="fin-check"><input data-setting="confirmed" type="checkbox" ${s.confirmed?'checked':''}> Tôi đã đối chiếu và xác nhận cấu hình phí/thuế</label>
      </div><p class="fin-note">Preset Việt Nam: processing 4,5%, regulatory 1,24%, processing cố định tham chiếu 11.550 VND (Help Center ghi 11.500 VND). Nhập theo Payment Account trước khi xác nhận. Nguồn: Etsy Payments Policy và Help Center.</p>
      <div class="fin-actions"><button type="button" class="btn btn-ghost btn-sm" onclick="setVietnamFinancePreset()" ${state.loading||state.error?'disabled':''}>Nạp preset VN (chưa xác nhận)</button><button type="button" class="btn btn-primary btn-sm" id="finance-settings-save" onclick="saveFinanceSettings()" ${state.loading||state.error?'disabled':''}>Lưu cấu hình shop</button></div><div id="finance-settings-status" class="fin-status" role="status"></div></details>`;
  }
  function amountFieldSetting(id, value, label, currency) { return `<label class="fin-field"><span>${label}</span><div class="fin-amount"><input data-setting="${id}.amount" type="number" min="0" step="any" value="${esc(value?.amount ?? '')}" placeholder="Chưa nhập">${currencyField(`setting.${id}.currency`, value?.currency || currency)}</div></label>`; }
  function priceSimulationNote(){const current=String(q('edit-price')?.value??'');return current===String(state.savedPrice??'')?'Giá hiện tại đã lưu trong thông tin sản phẩm.':'Đang mô phỏng giá chưa lưu vào thông tin sản phẩm.';}
  function readSettingsForm() {
    const settings = clone(state.settings || defaultSettings());
    document.querySelectorAll('#finance-tab-panel [data-setting]').forEach(el => {
      const key = el.dataset.setting;
      if (key.includes('.')) { const [parent, child] = key.split('.'); settings[parent] ||= {}; settings[parent][child] = el.value === '' ? null : el.value; }
      else if (el.type === 'checkbox') settings[key] = el.checked;
      else settings[key] = el.value;
    });
    document.querySelectorAll('#finance-tab-panel [data-fin^="setting."]').forEach(el => {
      const [, path, child] = el.dataset.fin.match(/setting\.(.+)\.([^.]*)/) || [];
      if (path && child === 'currency') settings[path].currency = el.value;
    });
    document.querySelectorAll('#finance-tab-panel [data-setting-fx]').forEach(el => { const c=el.dataset.settingFx; settings.fx_vnd_per_unit ||= {}; if(el.value==='')delete settings.fx_vnd_per_unit[c];else settings.fx_vnd_per_unit[c]=el.value; });
    return settings;
  }
  function readFinancialsForm() {
    const f = clone(state.financials || defaultFinancials());
    document.querySelectorAll('#finance-tab-panel [data-fin]').forEach(el => {
      const key = el.dataset.fin;
      if (key.startsWith('setting.')) return;
      if (key.startsWith('costs.')) { const [, name, field] = key.match(/costs\.([^.]+)(?:\.(.+))?/) || []; if (name) { f.costs[name] ||= amount(null); if (field === 'currency') f.costs[name].currency = el.value; else f.costs[name].amount = el.value === '' ? null : el.value; } return; }
      const m = key.match(/scenarios\.(\d+)\.([^.]+)(?:\.(.+))?/); if (!m) return;
      const scenario = f.scenarios[Number(m[1])]; if (!scenario) return;
      const [, idx, prop, sub] = m;
      if (prop === 'freight' || prop === 'insurance' || prop === 'buyer_shipping' || prop === 'buyer_tax') { scenario[prop] ||= amount(null); if (sub === 'currency') scenario[prop].currency = el.value; else scenario[prop].amount = el.value === '' ? null : el.value; }
      else if (prop === 'free_shipping') scenario[prop] = el.checked;
      else scenario[prop] = el.value;
    });
    f.listing_type = q('fin-listing-type')?.value || f.listing_type;
    f.discount_percent = q('fin-sale-discount')?.value || '0';
    f.target_margin_percent = q('fin-target-margin')?.value === '' ? null : q('fin-target-margin')?.value;
    return f;
  }
  function formMarkup() {
    const f = state.financials || defaultFinancials();
    return `${state.error ? `<div class="fin-error" role="alert">${esc(state.error)}</div>` : ''}
      <div class="fin-head"><div><strong>Lợi nhuận dự kiến</strong><span class="fin-muted"> · mỗi sản phẩm trong một đơn</span></div><button type="button" class="btn btn-primary btn-sm" id="finance-save" onclick="saveProductFinancials()" ${state.loading||state.error?'disabled':''}>Lưu chi phí</button></div>
      <div class="fin-status" id="finance-status" role="status">${state.loading?'Đang tải dữ liệu…':''}</div>
      ${settingsMarkup()}
      ${section('Sản phẩm', `<div class="fin-grid"><label class="fin-field"><span>Loại hàng</span><select id="fin-listing-type"><option value="physical" ${f.listing_type==='physical'?'selected':''}>Hàng vật lý</option><option value="digital" ${f.listing_type==='digital'?'selected':''}>Digital</option></select></label><label class="fin-field"><span>Giảm giá bán (%)</span><input id="fin-sale-discount" type="number" min="0" max="100" step="any" value="${esc(f.discount_percent ?? 0)}"></label><div class="fin-field"><span>Giá niêm yết</span><strong id="fin-price-display">${esc(q('edit-price')?.value || '—')} ${esc((state.settings||{}).listing_currency||'USD')}</strong><small id="fin-price-note">${priceSimulationNote()}</small><small>Thuế người mua không tính vào doanh thu; có thể dùng làm cơ sở phí processing.</small></div></div>`)}
      ${section('Chi phí chung cho mỗi sản phẩm', `<div class="fin-grid">${amountField('costs.cogs',f.costs?.cogs,'Giá vốn (cần xác nhận)')}${amountField('costs.packaging',f.costs?.packaging,'Đóng gói')}${amountField('costs.labor',f.costs?.labor,'Công xử lý')}${amountField('costs.other',f.costs?.other,'Chi phí khác')}${amountField('costs.etsy_ads',f.costs?.etsy_ads,'Etsy Ads phân bổ')}</div>`)}
      ${section('Phương án vận chuyển', `<div class="fin-scenarios" id="fin-scenarios">${(f.scenarios||[]).map((s,i)=>scenarioMarkup(s,i,f.representative_id)).join('')}</div><button class="btn btn-ghost btn-sm" type="button" onclick="addFinanceScenario()">＋ Thêm phương án</button>`)}
      ${section('Mục tiêu định giá', `<div class="fin-grid"><label class="fin-field"><span>Biên lợi nhuận mục tiêu (%)</span><input id="fin-target-margin" type="number" min="0" max="99.99" step="any" value="${esc(f.target_margin_percent ?? '')}" placeholder="Để trống nếu chưa đặt mục tiêu"></label><div id="fin-target-result" class="fin-target-result">Nhập mục tiêu để xem giá cần đạt.</div></div>`)}
      <div id="fin-results" class="fin-results"><div class="fin-loading">${state.loading?'Đang tính…':'Nhập chi phí bắt buộc để xem kết quả.'}</div></div>
      <div class="fin-footer"><button type="button" class="btn btn-ghost btn-sm" onclick="calculateProductFinancials()">Tính lại</button><span id="fin-fx-note" class="fin-muted"></span></div>`;
  }
  function scenarioMarkup(s,i,representative) {
    return `<article class="fin-scenario" data-scenario-index="${i}"><div class="fin-scenario-head"><label class="fin-field"><span>Tên phương án</span><input data-fin="scenarios.${i}.name" value="${esc(s.name||'')}" placeholder="Mỹ · tiêu chuẩn"></label><label class="fin-field"><span>Nước đến (ISO)</span><input data-fin="scenarios.${i}.country" maxlength="2" value="${esc(s.country||'')}" placeholder="US"></label><label class="fin-radio"><input data-fin="scenarios.${i}.representative" type="radio" name="fin-representative" ${s.id===representative?'checked':''} onchange="setFinanceRepresentative(${i})"> Đại diện</label></div>
      <div class="fin-grid">${amountField(`scenarios.${i}.freight`,s.freight,'Cước người bán trả')}
      <label class="fin-field"><span>Giảm cước (%)</span><input data-fin="scenarios.${i}.freight_discount_percent" type="number" min="0" max="100" step="any" value="${esc(s.freight_discount_percent??0)}"></label>
      ${amountField(`scenarios.${i}.insurance`,s.insurance,'Bảo hiểm / phụ phí')}${amountField(`scenarios.${i}.buyer_shipping`,s.buyer_shipping,'Ship thu khách')}
      <label class="fin-field"><span>Offsite Ads</span><select data-fin="scenarios.${i}.offsite_percent"><option value="0" ${Number(s.offsite_percent)===0?'selected':''}>Đơn thường</option><option value="12" ${Number(s.offsite_percent)===12?'selected':''}>12%</option><option value="15" ${Number(s.offsite_percent)===15?'selected':''}>15%</option></select></label>
      <label class="fin-field"><span>Ngày báo giá vận chuyển</span><input data-fin="scenarios.${i}.quote_date" type="date" value="${esc(s.quote_date||'')}"></label>
      ${amountField(`scenarios.${i}.buyer_tax`,s.buyer_tax,'Thuế người mua dự kiến (phí processing tính cả khoản này)')}</div>
      <label class="fin-check"><input data-fin="scenarios.${i}.free_shipping" type="checkbox" ${s.free_shipping?'checked':''}> Free shipping (ship thu khách = 0)</label>
      <label class="fin-field"><span>Ghi chú</span><textarea data-fin="scenarios.${i}.notes" rows="2">${esc(s.notes||'')}</textarea></label>
      <div class="fin-actions"><button type="button" class="btn btn-ghost btn-sm" onclick="copyFinanceScenario(${i})">Sao chép</button><button type="button" class="btn btn-danger btn-sm" onclick="deleteFinanceScenario(${i})">Xóa phương án</button></div></article>`;
  }
  async function loadFinance(shop,folder) {
    const token=++state.request; state.shop=shop; state.folder=folder; state.loading=true; state.error=''; paint();
    try {
      const [fr,sr]=await Promise.all([fetch(productApi(shop,folder)),fetch(api(shop,'/financial-settings'))]);
      const [fd,sd]=await Promise.all([fr.json(),sr.json()]);
      if(token!==state.request||shop!==getActiveShopId()) return;
      if(!fr.ok||!fd.ok) throw new Error(fd.detail||fd.error||`Không đọc được tài chính (${fr.status})`);
      if(!sr.ok||!sd.ok) throw new Error(sd.detail||sd.error||`Không đọc được cấu hình phí (${sr.status})`);
      state.financials=normalizeFinancials(fd.financials); state.version=fd.version; state.settings=normalizeSettings(sd.settings); state.settingsVersion=sd.version; state.calculation=fd.calculation||null;state.savedPrice=fd.price??'';
      state.baseline=stable(state.financials);state.settingsBaseline=stable(state.settings);state.loading=false;state.error='';paint(); scheduleCalculate();
    } catch(e) { if(token!==state.request)return;state.loading=false;state.error=e.message;paint(); }
  }
  function syncFinanceSaveButtons(){[q('finance-save'),q('finance-footer-save')].filter(Boolean).forEach(button=>{button.disabled=state.loading||Boolean(state.error)||state.savingFinancials;});}
  function paint() { const host=q('finance-content');if(host)host.innerHTML=formMarkup();syncFinanceSaveButtons();const footer=q('finance-footer-save');if(footer)footer.hidden=state.activeTab!=='finance'; }
  function financeInput(event) {
    if (!event.target.closest('#finance-tab-panel')) return;
    if (event.target.matches('[data-setting-fx]')) { const s=readSettingsForm(); state.settings=s; }
    const current=readFinancialsForm(); state.financials=current;
    if (event.target.matches('[data-fin="scenarios.0.free_shipping"]') && event.target.checked) { current.scenarios[0].buyer_shipping=amount('0',current.scenarios[0].buyer_shipping?.currency||'VND'); }
    if (event.target.matches('[data-fin^="scenarios."][data-fin$=".free_shipping"]') && event.target.checked) { const i=Number(event.target.dataset.fin.match(/scenarios\.(\d+)/)[1]); current.scenarios[i].buyer_shipping=amount('0',current.scenarios[i].buyer_shipping?.currency||'VND'); const ship=document.querySelector(`[data-fin="scenarios.${i}.buyer_shipping"]`);if(ship)ship.value='0'; }
    if(event.target.dataset.setting==='listing_currency') { q('fin-price-display').textContent=`${q('edit-price')?.value||'—'} ${event.target.value}`; }
    scheduleCalculate(); setFinanceDirtyStatus();
  }
  function setFinanceDirtyStatus() {
    const status=q('finance-status'); if(!status)return;
    const dirty=stable(readFinancialsForm())!==state.baseline||stable(readSettingsForm())!==state.settingsBaseline;
    status.textContent=dirty?'Có thay đổi chưa lưu. Lưu chi phí và cấu hình shop bằng các nút riêng.':'';
  }
  function scheduleCalculate() { clearTimeout(state.timer);state.timer=setTimeout(calculateProductFinancials,350); }
  async function calculateProductFinancials() {
    if(!state.shop||!state.folder||!state.financials)return;
    const request=++state.request,shop=state.shop,folder=state.folder,financials=readFinancialsForm(),settings=readSettingsForm();
    state.financials=financials; const host=q('fin-results'); if(host)host.innerHTML='<div class="fin-loading">Đang tính…</div>';
    try {
      const r=await fetch(api(shop,`/products/${encodeURIComponent(folder)}/financials/calculate`),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({financials,price:q('edit-price')?.value||null,settings})});
      if(request!==state.request||shop!==getActiveShopId()||folder!==state.folder)return;
      const data=await r.json();if(!r.ok||!data.ok)throw new Error(data.detail||data.error||`Không tính được (${r.status})`);
      state.calculation=data.calculation;renderCalculation();
    } catch(e) {if(request===state.request&&q('fin-results'))q('fin-results').innerHTML=`<div class="fin-error">${esc(e.message)}</div>`;}
  }
  function renderCalculation() {
    const c=state.calculation,host=q('fin-results');if(!host)return;
    if(!c||!Array.isArray(c.scenarios)){host.innerHTML='<div class="fin-loading">Chưa có kết quả tính toán.</div>';return;}
    if(!c.scenarios.length){host.innerHTML=`<div class="fin-incomplete-note">${(c.missing||[]).length?`Chưa đủ dữ liệu: ${esc(c.missing.join(', '))}`:'Chưa có phương án vận chuyển.'}</div>`;return;}
    const rows=c.scenarios.map(s=>`<tr class="${s.status==='incomplete'?'fin-incomplete':s.status==='provisional'?'fin-provisional':''}"><th>${esc(s.name||s.id)}${Number(s.offsite_percent)?` · Offsite ${esc(s.offsite_percent)}%`:''}${s.id===state.financials?.representative_id?' · Đại diện':''}</th><td>${money(s.revenue)} ${esc(c.currency||'')}</td><td>${money(s.etsy_fees)} ${esc(c.currency||'')}</td><td>${money(s.operating_costs)} ${esc(c.currency||'')}</td><td>${money(s.proceeds)} ${esc(c.currency||'')}</td><td class="${Number(s.profit)>=0?'fin-positive':'fin-negative'}">${s.status==='incomplete'?'Thiếu dữ liệu':`${money(s.profit)} ${esc(c.currency||'')}${s.status==='provisional'?'<small class="fin-provisional-label">Ước tính · cấu hình chưa xác nhận</small>':''}<small>${s.profit_usd==null?'':` · ${money(s.profit_usd)} USD / ${money(s.profit_vnd)} VND`}</small>`}</td><td>${s.margin_percent==null?'—':`${money(s.margin_percent)}%`}</td><td>${money(s.break_even_price)}</td><td>${s.status==='incomplete'?esc((s.missing||[]).join(', ')):money(s.target_price)}</td></tr>`).join('');
    const feeLabels={transaction:'Transaction',processing:'Processing theo %',processing_fixed:'Processing cố định',regulatory:'Regulatory',listing:'Listing phân bổ',conversion:'Đổi tiền',offsite:'Offsite Ads',fee_vat:'VAT trên phí'};
    const details=c.scenarios.map(s=>`<details class="fin-breakdown"><summary>${esc(s.name||s.id)} · Phân tích phí</summary><div class="fin-breakdown-grid">${Object.entries(s.fees||{}).map(([key,value])=>`<span>${esc(feeLabels[key]||key)}</span><strong>${money(value)} ${esc(c.currency||'')}</strong>`).join('')}<span>Thuế/khấu trừ người bán</span><strong>${money(s.taxes)} ${esc(c.currency||'')}</strong><span>Thuế người mua (cơ sở processing)</span><strong>${esc((state.financials?.scenarios||[]).find(row=>row.id===s.id)?.buyer_tax?.amount??'—')} ${esc((state.financials?.scenarios||[]).find(row=>row.id===s.id)?.buyer_tax?.currency||'')}</strong></div><h4>So sánh Offsite Ads</h4><div class="fin-breakdown-grid">${(s.offsite_comparisons||[]).map(row=>`<span>${esc(row.offsite_percent)}% Offsite</span><strong>${money(row.profit)} ${esc(c.currency||'')}</strong>`).join('')}</div>${s.missing?.length?`<p class="fin-missing">Thiếu: ${esc(s.missing.join(', '))}</p>`:''}</details>`).join('');
    host.innerHTML=`<div class="fin-table-wrap"><table class="fin-table"><thead><tr><th>Phương án</th><th>Doanh thu</th><th>Phí Etsy</th><th>Chi phí hàng / vận chuyển</th><th>Còn lại sau phí</th><th>Lợi nhuận dự kiến</th><th>Biên</th><th>Hòa vốn</th><th>Giá mục tiêu</th></tr></thead><tbody>${rows}</tbody></table></div>${(c.scenarios||[]).map(s=>s.status==='provisional'?`<p class="fin-provisional-note">⚠ ${esc(s.warning||'Ước tính tạm thời; cấu hình phí/thuế shop chưa được xác nhận.')}</p>`:s.status==='incomplete'&&s.missing?.length?`<p class="fin-missing">${esc(s.name)}: thiếu ${esc(s.missing.join(', '))}</p>`:'').join('')}${details}`;
    q('fin-fx-note').textContent=`Tiền niêm yết ${c.currency||'—'} · Tỷ giá ngày ${c.fx_date||state.settings?.fx_date||'chưa nhập'}`;
    const rep=c.scenarios.find(s=>s.id===state.financials?.representative_id)||c.scenarios[0];
    q('fin-target-result').innerHTML=rep?.target_price!=null?`Giá đề xuất: <strong>${money(rep.target_price)} ${esc(c.currency||'')}</strong> <button type="button" class="btn btn-primary btn-sm" onclick="applyFinanceSuggestedPrice('${esc(rep.target_price)}')">Dùng giá này</button>`:'Nhập biên mục tiêu và đủ dữ liệu để tính giá đề xuất.';
  }
  async function saveProductFinancials() {
    if(!state.shop||!state.folder)return;
    if(state.savingFinancials)return;
    const shop=state.shop,folder=state.folder,generation=state.editorGeneration,button=q('finance-save'); if(button)button.disabled=true;
    if(shop!==getActiveShopId()){if(button)button.disabled=false;if(typeof toast==='function')toast('warning','Shop đã đổi. Đóng cửa sổ và mở lại sản phẩm trong shop đang chọn trước khi lưu.');return;}
    state.savingFinancials=true;syncFinanceSaveButtons();
    const financials=readFinancialsForm();state.financials=financials;
    try {const editorFolder=folder,editorShop=shop;const r=await fetch(productApi(shop,folder),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_version:state.version,financials})});if(generation!==state.editorGeneration||editorShop!==getActiveShopId()||editorFolder!==state.folder)return;const d=await r.json();if(generation!==state.editorGeneration||editorShop!==getActiveShopId()||editorFolder!==state.folder)return;if(!r.ok||!d.ok)throw new Error(d.detail||d.error||`Lưu thất bại (${r.status})`);
      const verify=await fetch(productApi(shop,folder));const v=await verify.json();if(!verify.ok||!v.ok||stable(v.financials)!==stable(financials))throw new Error('Lệnh lưu đã gửi nhưng đọc lại chưa khớp; chưa xác nhận hoàn tất.');
      if(generation!==state.editorGeneration||editorShop!==getActiveShopId()||editorFolder!==state.folder)return;state.version=v.version;state.settingsVersion=v.version;state.financials=v.financials;state.baseline=stable(v.financials);state.calculation=null;setFinanceDirtyStatus();if(q('fin-results'))q('fin-results').innerHTML='<div class="fin-loading">Đang cập nhật theo giá hiện tại…</div>';scheduleCalculate();await loadFinancialSummaries(shop);if(typeof toast==='function')toast('success','Đã lưu và đọc lại chi phí sản phẩm.');
    }catch(e){if(typeof toast==='function')toast('error',`Không lưu được chi phí: ${e.message}`);else alert(e.message);}finally{state.savingFinancials=false;syncFinanceSaveButtons();}
  }
  async function saveFinanceSettings() {
    const shop=state.shop,folder=state.folder,generation=state.editorGeneration,settings=readSettingsForm(),button=q('finance-settings-save');if(!shop)return;if(button)button.disabled=true;
    if(shop!==getActiveShopId()){if(button)button.disabled=false;const el=q('finance-settings-status');if(el)el.textContent='Shop đã đổi. Mở lại sản phẩm trong shop đang chọn trước khi lưu cấu hình.';return;}
    try{const r=await fetch(api(shop,'/financial-settings'),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_version:state.settingsVersion,settings})});if(generation!==state.editorGeneration||shop!==getActiveShopId()||folder!==state.folder)return;const d=await r.json();if(generation!==state.editorGeneration||shop!==getActiveShopId()||folder!==state.folder)return;if(!r.ok||!d.ok)throw new Error(d.detail||d.error||`Lưu thất bại (${r.status})`);const vr=await fetch(api(shop,'/financial-settings'));if(generation!==state.editorGeneration||shop!==getActiveShopId()||folder!==state.folder)return;const v=await vr.json();if(generation!==state.editorGeneration||shop!==getActiveShopId()||folder!==state.folder)return;if(!vr.ok||!v.ok||stable(v.settings)!==stable(settings))throw new Error('Lưu xong nhưng đọc lại cấu hình chưa khớp.');state.settings=v.settings;state.settingsVersion=v.version;state.version=v.version;state.settingsBaseline=stable(v.settings);state.calculation=null;paint();scheduleCalculate();await loadFinancialSummaries(shop);if(typeof toast==='function')toast('success','Đã lưu và xác nhận cấu hình phí của shop.');}catch(e){const el=q('finance-settings-status');if(el&&generation===state.editorGeneration&&shop===getActiveShopId()&&folder===state.folder)el.textContent=`Không lưu được: ${e.message}`;}finally{if(button&&generation===state.editorGeneration&&shop===getActiveShopId()&&folder===state.folder)button.disabled=false;}
  }
  function setVietnamFinancePreset(){const s=normalizeSettings(readSettingsForm());s.bank_country='VN';s.processing_percent='4.5';s.processing_fixed=amount('11550','VND');s.regulatory_percent='1.24';s.transaction_percent='6.5';s.listing_fee=amount('0.20','USD');s.fx_vnd_per_unit.VND='1';s.tax_mode='unknown';s.confirmed=false;s.source={label:'Etsy Payments Policy / Help Center; đối chiếu chênh lệch processing 11.500–11.550 VND',checked_date:'2026-10-01'};state.settings=s;paint();setFinanceDirtyStatus();scheduleCalculate();}
  function addFinanceScenario(){const f=readFinancialsForm();f.scenarios.push(defaultScenario());state.financials=f;paint();scheduleCalculate();setFinanceDirtyStatus();}
  function copyFinanceScenario(i){const f=readFinancialsForm(),copy=clone(f.scenarios[i]);copy.id=`shipping-${Date.now()}-${Math.random().toString(36).slice(2,6)}`;copy.name=`${copy.name||'Phương án'} · bản sao`;f.scenarios.splice(i+1,0,copy);state.financials=f;paint();scheduleCalculate();setFinanceDirtyStatus();}
  function deleteFinanceScenario(i){const f=readFinancialsForm();if(f.scenarios.length<=1)return;const [gone]=f.scenarios.splice(i,1);if(f.representative_id===gone.id)f.representative_id=f.scenarios[0].id;state.financials=f;paint();scheduleCalculate();setFinanceDirtyStatus();}
  function setFinanceRepresentative(i){const f=readFinancialsForm();f.representative_id=f.scenarios[i]?.id||null;state.financials=f;scheduleCalculate();setFinanceDirtyStatus();}
  function renderSuggestedPriceGuidance(){const guide=q('fin-price-save-guidance');if(!guide)return;guide.hidden=!state.suggestedPriceApplied;if(!state.suggestedPriceApplied){guide.innerHTML='';return;}guide.innerHTML=`<strong>Giá đề xuất chưa được lưu vào dashboard.</strong> Bấm <button type="button" class="btn btn-primary btn-sm" onclick="switchFinanceTab('product')">Mở tab Thông tin sản phẩm</button>, rồi bấm <strong>💾 Lưu</strong> ở cuối cửa sổ để cập nhật giá vào dashboard. Đây chỉ là lưu cục bộ; muốn đổi giá trên listing Etsy, sau đó cần dùng riêng nút <strong>Update</strong> của sản phẩm.`;}
  function applyFinanceSuggestedPrice(value){const input=q('edit-price');if(!input)return;input.value=String(value);input.dispatchEvent(new Event('input',{bubbles:true}));if(typeof updateModalDirtyState==='function')updateModalDirtyState('edit-modal');state.suggestedPriceApplied=true;renderSuggestedPriceGuidance();const priceDisplay=q('fin-price-display');if(priceDisplay)priceDisplay.textContent=`${value} ${state.settings?.listing_currency||'USD'}`;const priceNote=q('fin-price-note');if(priceNote)priceNote.textContent=priceSimulationNote();scheduleCalculate();}
  async function loadFinancialSummaries(shop=getActiveShopId()) {
    const token=++state.summaryRequest;if(!shop)return;
    try{const r=await fetch(api(shop,'/financial-summaries'));const d=await r.json();if(token!==state.summaryRequest||shop!==getActiveShopId()||!r.ok||!d.ok)return;state.summaries=d.summaries||{};if(typeof filterProducts==='function')filterProducts();}catch(_e){/* Finance badges remain unavailable until next refresh. */}
  }
  function summaryFor(folder){return state.summaries?.[folder]||null;}
  function badgeHtml(folder){const item=summaryFor(folder),calc=item?.calculation||item,s=calc?.scenario||(calc?.scenarios||[]).find(x=>x.id===item?.representative_id)||(calc?.scenarios||[])[0];if(!s||s.status==='incomplete')return `<div class="fin-product-badge incomplete">Lợi nhuận dự kiến · chưa đủ dữ liệu${(item?.missing||s?.missing)?.length?` · ${esc((item?.missing||s?.missing).join(', '))}`:''}</div>`;const scenarioName=s.name||s.country||'Phương án đại diện';const fxDate=item?.fx_date||calc?.fx_date||'chưa nhập',provisional=s.status==='provisional';return `<div class="fin-product-badge ${Number(s.profit)>=0?'positive':'negative'}${provisional?' provisional':''}" data-fin-profit="${Number(s.profit)||0}" data-fin-status="${Number(s.profit)>=0?'profit':'loss'}" title="${esc(s.warning||scenarioName)} · Tỷ giá ngày ${esc(fxDate)}">${provisional?'Ước tính chưa xác nhận':'Lợi nhuận dự kiến'} · ${esc(scenarioName)}: ${money(s.profit_usd)} USD / ${money(s.profit_vnd)} VND · ${money(s.margin_percent)}%</div>`;}
  function installListHooks(){
    if(typeof productCard==='function'&&!productCard.__financeWrapped){const original=productCard;const wrapped=function(p){const html=original(p);if(!p?.folder)return html;return html.replace(/(<div class="product-meta">[\s\S]*?<\/div>)/,match=>`${match}${badgeHtml(p.folder)}`);};wrapped.__financeWrapped=true;productCard=wrapped;}
    if(typeof filterProducts==='function'&&!filterProducts.__financeWrapped){const original=filterProducts;const wrapped=function(...args){original(...args);applyFinanceFilterSort();};wrapped.__financeWrapped=true;filterProducts=wrapped;}
    if(typeof loadProducts==='function'&&!loadProducts.__financeWrapped){const original=loadProducts;const wrapped=async function(...args){const result=await original(...args);const shop=getActiveShopId();if(result&&shop)void loadFinancialSummaries(shop);return result;};wrapped.__financeWrapped=true;loadProducts=wrapped;}
    if(typeof openEditModal==='function'&&!openEditModal.__financeWrapped){const original=openEditModal;const wrapped=function(row){const p=allProducts.find(x=>x.row===row);const result=original(row);if(!p)return result;editorShell();state.editorGeneration++;state.request++;state.shop=getActiveShopId();state.folder=p.folder;state.financials=null;state.settings=null;state.calculation=null;state.version=null;state.baseline='';state.settingsBaseline='';state.loading=true;state.error='';state.lastPrice=q('edit-price')?.value||'';state.savedPrice=q('edit-price')?.value||'';state.suggestedPriceApplied=false;state.activeTab='product';state.infoBaseline=infoSnapshot();renderSuggestedPriceGuidance();switchFinanceTab('product');paint();void loadFinance(state.shop,state.folder);return result;};wrapped.__financeWrapped=true;openEditModal=wrapped;}
    if(typeof markModalClean==='function'&&!markModalClean.__financeWrapped){const original=markModalClean;const wrapped=function(id){original(id);if(id==='edit-modal')state.infoBaseline=infoSnapshot();};wrapped.__financeWrapped=true;markModalClean=wrapped;}
    if(typeof closeModal==='function'&&!closeModal.__financeWrapped){const original=closeModal;const wrapped=function(id,options={}){if(id==='edit-modal'&&!options.force){const infoDirty=stable(infoSnapshot())!==stable(state.infoBaseline);const financeDirty=state.financials&&stable(readFinancialsForm())!==state.baseline;const settingsDirty=state.settings&&stable(readSettingsForm())!==state.settingsBaseline;if((infoDirty||financeDirty||settingsDirty)&&!window.confirm('Có thay đổi chưa lưu trong cửa sổ này. Đóng sẽ bỏ các thay đổi chưa lưu. Bạn có chắc muốn đóng?'))return false;}const closed=original(id,{...options,force:id==='edit-modal'?true:options.force});if(id==='edit-modal'&&closed){state.editorGeneration++;state.request++;}return closed;};wrapped.__financeWrapped=true;closeModal=wrapped;}
  }
  function infoSnapshot(){return ['edit-title','edit-tags','edit-keywords','edit-category','edit-extra','edit-description','edit-price','edit-section','edit-etsy-url','edit-sku','edit-qty'].map(id=>q(id)?.value??'');}
  function applyFinanceFilterSort(){const grid=q('product-grid');if(!grid)return;const filter=q('filter-profit-status')?.value||'',sort=q('sort-profit')?.value||'';const cards=Array.from(grid.querySelectorAll('.product-card'));cards.forEach(card=>{const badge=card.querySelector('.fin-product-badge');const status=badge?.dataset.finStatus||(badge?.classList.contains('incomplete')?'incomplete':'incomplete');card.hidden=!!filter&&status!==filter;});if(sort){cards.sort((a,b)=>{const av=Number(a.querySelector('[data-fin-profit]')?.dataset.finProfit),bv=Number(b.querySelector('[data-fin-profit]')?.dataset.finProfit);const aa=Number.isFinite(av)?av:(sort==='profit-desc'?-Infinity:Infinity),bb=Number.isFinite(bv)?bv:(sort==='profit-desc'?-Infinity:Infinity);return sort==='profit-desc'?bb-aa:aa-bb;}).forEach(card=>grid.appendChild(card));}const visible=cards.filter(card=>!card.hidden);if(typeof renderCatalogSummary==='function'&&cards.length)renderCatalogSummary(visible.length);if(currentProductSource==='local'&&typeof updateStats==='function'){const folders=new Set(visible.map(card=>card.querySelector('.product-cb')?.dataset.folder).filter(Boolean));updateStats(allProducts.filter(product=>folders.has(product.folder)));}if(typeof updateBatchUI==='function'&&q('batch-actions'))updateBatchUI();}
  window.switchFinanceTab=switchFinanceTab;window.saveProductFinancials=saveProductFinancials;window.saveFinanceSettings=saveFinanceSettings;window.calculateProductFinancials=calculateProductFinancials;window.setVietnamFinancePreset=setVietnamFinancePreset;window.addFinanceScenario=addFinanceScenario;window.copyFinanceScenario=copyFinanceScenario;window.deleteFinanceScenario=deleteFinanceScenario;window.setFinanceRepresentative=setFinanceRepresentative;window.applyFinanceSuggestedPrice=applyFinanceSuggestedPrice;window.loadFinancialSummaries=loadFinancialSummaries;
  if(typeof window.__FINANCIALS_TEST__==='function')window.__FINANCIALS_TEST__({state,readSettingsForm,readFinancialsForm,settingsMarkup,priceSimulationNote,normalizeFinancials,applyFinanceSuggestedPrice,switchFinanceTab});
  installListHooks();document.addEventListener('DOMContentLoaded',()=>{editorShell();const select=q('shop-switcher');select?.addEventListener('change',()=>{state.request++;state.summaryRequest++;state.summaries={};});q('edit-price')?.addEventListener('input',()=>{if(state.activeTab==='finance'){const display=q('fin-price-display');if(display)display.textContent=`${q('edit-price').value||'—'} ${state.settings?.listing_currency||'USD'}`;const note=q('fin-price-note');if(note)note.textContent=priceSimulationNote();scheduleCalculate();}});});
})();
