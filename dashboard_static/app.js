// ── State ──────────────────────────────────────────────────────────────────────
let allProducts = [];
let runningSet  = new Set();
let etsySingleSyncInFlight = false;
let etsyBulkSyncInFlight = false;
let etsyBulkUpdateSelection = null;
let etsyManagerSnapshot = null;
let aggregateCatalog = null;
let currentProductSource = 'local';
let cloudAssetStatusByFolder = new Map();
let cloudAssetStatusAvailable = false;
let cloudAssetStatusError = '';
let cloudAssetStatusRequestId = 0;
let cloudAssetStatusPromise = null;
let cloudAssetStatusPromiseShop = '';
let cloudAssetMutationInFlight = new Map();
let scrollNav = null;
let preferredScrollTarget = null;
let imageModalImages = [];
let lightboxState = { images: [], index: 0, caption: '', opener: null };
let jobCenterOpen = false;
let jobCenterTimer = null;
const catalogViewState = { items: [], visibleCount: 0, pageSize: 40 };
const selectedCatalogIds = new Set();
const reducedMotionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
const SYNCABLE_ETSY_STATUSES = new Set(['active', 'draft']);
const CLOUD_ASSET_STATUS_META = Object.freeze({
  LOCAL_ONLY: { category: 'local', label: '📁 Local only' },
  READY_LOCAL: { category: 'local', label: '📁 Local ready' },
  CLOUD_VERIFIED: { category: 'local', label: '☁️ Cloud verified' },
  UPLOAD_SCHEDULED: { category: 'scheduled', label: '🗓️ Upload scheduled' },
  OFFLOAD_SCHEDULED: { category: 'scheduled', label: '⏱ Offload scheduled' },
  CLOUD_ONLY: { category: 'cloud-only', label: '☁️ Cloud-only' },
  ERROR: { category: 'error', label: '❌ Cloud error' },
  DIRTY_LOCAL: { category: 'error', label: '⚠️ Local changed' },
  UPLOADING: { category: 'local', label: '⏫ Uploading…' },
  RESTORING: { category: 'cloud-only', label: '⏬ Restoring…' },
  RESTORE_VERIFIED: { category: 'local', label: '✅ Restore verified' },
});

const DASHBOARD_MUTATION_TOKEN_HEADER = 'X-Dashboard-Mutation-Token';
const DASHBOARD_MUTATION_TOKEN = (() => {
  const tokenMeta = typeof document !== 'undefined' && typeof document.querySelector === 'function'
    ? document.querySelector('meta[name="etsy-dashboard-mutation-token"]')
    : null;
  return tokenMeta?.content ? String(tokenMeta.content).trim() : '';
})();
const _nativeFetch = typeof window !== 'undefined' && typeof window.fetch === 'function'
  ? window.fetch.bind(window)
  : () => Promise.reject(new Error('fetch is unavailable in this DOM sandbox'));

function _isMutationMethod(method = 'GET') {
  return new Set(['POST', 'PATCH', 'DELETE']).has(String(method || '').toUpperCase());
}

function _isSameOriginRequest(targetUrl) {
  if (!targetUrl) return false;
  try {
    const parsed = new URL(targetUrl, window.location.href);
    return parsed.origin === window.location.origin;
  } catch {
    return false;
  }
}

window.fetch = async function dashboardFetch(input, init = {}) {
  const options = { ...init };
  const method = String(options.method || 'GET').toUpperCase();
  const requestUrl = typeof input === 'string' || input instanceof URL ? String(input) : input?.url;

  if (_isMutationMethod(method) && _isSameOriginRequest(requestUrl)) {
    const token = DASHBOARD_MUTATION_TOKEN;
    if (token) {
      const headers = new Headers(options.headers || {});
      if (!headers.has(DASHBOARD_MUTATION_TOKEN_HEADER)) {
        headers.set(DASHBOARD_MUTATION_TOKEN_HEADER, token);
      }
      options.headers = headers;
    }
  }

  return _nativeFetch(input, options);
};

function normalizeEtsyStatus(status) {
  const value = String(status || '').trim().toLowerCase();
  if (value.includes('draft')) return 'draft';
  if (value.includes('expired')) return 'expired';
  if (value.includes('inactive')) return 'inactive';
  if (value.includes('active') || value.includes('đã đăng')) return 'active';
  return value;
}

function isSyncableEtsyStatus(status) {
  return SYNCABLE_ETSY_STATUSES.has(normalizeEtsyStatus(status));
}

function getSyncableEtsyListings(listings) {
  return (listings || []).filter(item => isSyncableEtsyStatus(item?.managerStatus || item?.status));
}

function resolveEtsyListingLink(listing) {
  const rawId = String(listing?.etsy_listing_id || listing?.listing_id || listing?.id || '').trim();
  if (!/^\d+$/.test(rawId)) return { url: '', kind: 'unavailable', listingId: '', stale: Boolean(listing?.etsy_snapshot_stale || listing?.snapshot_stale) };

  const status = normalizeEtsyStatus(listing?.etsy_manager_status || listing?.managerStatus || listing?.status);
  const publicUrl = String(listing?.etsy_public_url || listing?.publicUrl || listing?.url || listing?.etsy_url || '').trim();
  const managerUrl = String(listing?.etsy_edit_url || listing?.etsy_manage_url || listing?.managerUrl || listing?.manageUrl || listing?.editUrl || '').trim();
  const publicMatch = new RegExp(`^https://(?:www\\.)?etsy\\.com/listing/${rawId}(?:[/?#].*)?$`, 'i');
  const managerMatch = new RegExp(`^https://(?:www\\.)?etsy\\.com/(?:your/shops/me/)?listing-editor/edit/${rawId}(?:[/?#].*)?$`, 'i');

  if (status === 'active') {
    const verifiedPublicUrl = publicMatch.test(publicUrl)
      ? publicUrl
      : `https://www.etsy.com/listing/${rawId}`;
    return { url: verifiedPublicUrl, kind: 'public', listingId: rawId, stale: Boolean(listing?.etsy_snapshot_stale || listing?.snapshot_stale) };
  }
  if (new Set(['draft', 'inactive', 'expired']).has(status) && managerMatch.test(managerUrl)) {
    return { url: managerUrl, kind: 'manager', listingId: rawId, stale: Boolean(listing?.etsy_snapshot_stale || listing?.snapshot_stale) };
  }
  return { url: '', kind: 'unavailable', listingId: rawId, stale: Boolean(listing?.etsy_snapshot_stale || listing?.snapshot_stale) };
}

function productEtsyLink(product) {
  return resolveEtsyListingLink({
    etsy_listing_id: product?.etsy_listing_id,
    etsy_manager_status: product?.etsy_manager_status,
    etsy_public_url: product?.etsy_public_url,
    etsy_edit_url: product?.etsy_edit_url,
    etsy_manage_url: product?.etsy_manage_url,
    etsy_url: product?.etsy_url,
    status: product?.status,
    etsy_link_type: product?.etsy_link_type,
    etsy_snapshot_stale: product?.etsy_snapshot_stale,
  });
}

// ── Init ───────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  initShopSwitcher();
  loadProducts();
  connectLogs();
  pollServices();
  pollRuntimeHealth();
  setInterval(pollServices, 8000);
  setInterval(pollRuntimeHealth, 15000);
  setInterval(() => {
    const hasPendingCloudUpload = [...cloudAssetStatusByFolder.values()].some((status) => {
      const state = String(status?.state || '').toUpperCase();
      return state === 'UPLOAD_SCHEDULED' || state === 'UPLOADING';
    });
    if (!hasPendingCloudUpload) return;
    void loadCloudAssetStatus({ force: true })
      .then(() => filterProducts())
      .catch(() => {});
  }, 1500);
  createToastContainer();
  initModalOverlays();
  initScrollNavigation();
  initImageLightbox();
  loadJobCenter();
  jobCenterTimer = setInterval(loadJobCenter, 12000);
});

// ── Scroll Navigation (product list / page fallback) ───────────────────────────
function initScrollNavigation() {
  scrollNav = {
    wrapper: document.getElementById('scroll-nav-controls'),
    topBtn: document.getElementById('scroll-top-btn'),
    bottomBtn: document.getElementById('scroll-bottom-btn'),
    productSection: document.getElementById('product-section'),
  };

  if (!scrollNav.wrapper || !scrollNav.topBtn || !scrollNav.bottomBtn) return;

  window.addEventListener('scroll', refreshScrollNavState, { passive: true });
  if (scrollNav.productSection) {
    scrollNav.productSection.addEventListener('scroll', refreshScrollNavState, { passive: true });
  }
  window.addEventListener('resize', refreshScrollNavState);
  if (reducedMotionQuery?.addEventListener) {
    reducedMotionQuery.addEventListener('change', refreshScrollNavState);
  }

  scrollNav.topBtn.addEventListener('click', scrollToListTop);
  scrollNav.bottomBtn.addEventListener('click', scrollToListBottom);

  refreshScrollNavState();
}

function getScrollTarget() {
  const productSection = scrollNav?.productSection;
  const prefersProductSection = window.innerWidth > 1200;
  if (prefersProductSection && productSection && productSection.scrollHeight > productSection.clientHeight + 1) {
    return productSection;
  }
  return document.scrollingElement || document.documentElement || document.body;
}

function getRootMaxScroll() {
  const root = document.scrollingElement || document.documentElement || document.body;
  const docHeight = Math.max(root.scrollHeight, document.body.scrollHeight);
  const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
  return Math.max(0, docHeight - viewportHeight);
}

function getScrollState(target) {
  if (!target) {
    return { hasOverflow: false, atTop: true, atBottom: true };
  }

  if (target === scrollNav?.productSection) {
    const maxScroll = Math.max(0, target.scrollHeight - target.clientHeight);
    return {
      hasOverflow: maxScroll > 1,
      atTop: target.scrollTop <= 1,
      atBottom: target.scrollTop >= maxScroll - 1,
    };
  }

  const maxScroll = getRootMaxScroll();
  const current = window.scrollY || (document.documentElement && document.documentElement.scrollTop) || 0;
  return {
    hasOverflow: maxScroll > 1,
    atTop: current <= 1,
    atBottom: current >= maxScroll - 1,
  };
}

function refreshScrollNavState() {
  if (!scrollNav?.wrapper || !scrollNav.topBtn || !scrollNav.bottomBtn) return;

  preferredScrollTarget = getScrollTarget();
  const { hasOverflow, atTop, atBottom } = getScrollState(preferredScrollTarget);

  scrollNav.wrapper.classList.toggle('is-visible', hasOverflow);
  scrollNav.topBtn.disabled = !hasOverflow || atTop;
  scrollNav.bottomBtn.disabled = !hasOverflow || atBottom;
}

function getScrollBehavior() {
  return reducedMotionQuery?.matches ? 'auto' : 'smooth';
}

function scrollToListTop() {
  const target = preferredScrollTarget || getScrollTarget();
  const behavior = getScrollBehavior();

  if (!target) return;
  if (target === scrollNav?.productSection) {
    target.scrollTo({ top: 0, behavior });
  } else {
    window.scrollTo({ top: 0, behavior });
  }
}

function scrollToListBottom() {
  const target = preferredScrollTarget || getScrollTarget();
  const behavior = getScrollBehavior();

  if (!target) return;
  if (target === scrollNav?.productSection) {
    target.scrollTo({ top: target.scrollHeight, behavior });
  } else {
    window.scrollTo({ top: getRootMaxScroll(), behavior });
  }
}

let currentShopsData = {};

// ── Shop Switcher ──────────────────────────────────────────────────────────────
async function initShopSwitcher() {
  const shopSwitcher = document.getElementById('shop-switcher');
  try {
    const res = await fetch('/api/shops');
    const data = await res.json();
    currentShopsData = data.shops.reduce((acc, s) => ({...acc, [s.id]: s}), {});
    
    shopSwitcher.innerHTML = '';
    data.shops.forEach(shop => {
      const opt = document.createElement('option');
      opt.value = shop.id;
      opt.textContent = `${shop.emoji || '🏬'} ${shop.name}`;
      shopSwitcher.appendChild(opt);
    });
    shopSwitcher.value = data.active;
    void loadCloudAssetStatus();
    
    shopSwitcher.addEventListener('change', async (e) => {
      try {
        await fetch('/api/set-shop', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ shop_id: e.target.value })
        });
        window.location.reload(); // Reload to reset state for new shop
      } catch (err) {
        toast('error', `Lỗi chuyển shop: ${err.message}`);
      }
    });
  } catch (err) {
    console.error('Failed to load shops:', err);
    shopSwitcher.innerHTML = '<option>Lỗi tải shop</option>';
  }
}

async function openEtsyPostingLogin() {
  const shopId = document.getElementById('shop-switcher')?.value?.trim() || '';
  const button = document.getElementById('btn-etsy-login');
  if (!shopId) {
    toast('error', 'Chưa xác định shop đang hoạt động');
    return;
  }
  if (!button) return;

  const originalLabel = button.innerHTML;
  button.disabled = true;
  button.innerHTML = '<span class="spinner"></span> Đang mở profile Etsy...';
  try {
    const response = await fetch('/api/etsy/session/open', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ shop_id: shopId })
    });
    const data = await response.json();
    if (!response.ok || !data.ok) {
      throw new Error(data.error || 'Không mở được profile Etsy');
    }
    if (data.session?.shop_id !== shopId) {
      throw new Error(`Session trả về sai shop: ${data.session?.shop_id || 'không xác định'}`);
    }
    toast(
      'success',
      `🔐 Đã mở đúng profile Etsy cho ${data.session.shop_name}. Đăng nhập tại Shop Manager rồi giữ cửa sổ mở khi nhấn Post.`
    );
  } catch (error) {
    toast('error', `Không mở được session Etsy cho Post: ${error.message}`);
  } finally {
    button.disabled = false;
    button.innerHTML = originalLabel;
  }
}

// ── Shop Settings ──────────────────────────────────────────────────────────────
function openShopSettings() {
  const activeId = document.getElementById('shop-switcher').value;
  const shop = currentShopsData[activeId];
  if (!shop) return toast('error', 'Không tìm thấy cấu hình shop');
  
  document.getElementById('set-shop-id').value = shop.id;
  document.getElementById('set-name').value = shop.name || '';
  document.getElementById('set-emoji').value = shop.emoji || '';
  document.getElementById('set-etsy-link').value = shop.etsy_link || '';
  document.getElementById('set-social-links').value = shop.social_links || '';
  document.getElementById('set-shop-info').value = shop.shop_info || '';
  
  openModal('settings-modal');
}

async function saveShopSettings() {
  const payload = {
    id: document.getElementById('set-shop-id').value,
    name: document.getElementById('set-name').value.trim(),
    emoji: document.getElementById('set-emoji').value.trim(),
    etsy_link: document.getElementById('set-etsy-link').value.trim(),
    social_links: document.getElementById('set-social-links').value.trim(),
    shop_info: document.getElementById('set-shop-info').value.trim(),
  };
  
  try {
    const res = await fetch('/api/shops/update', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (data.ok) {
      toast('success', '✅ Đã lưu cấu hình shop!');
      closeModal('settings-modal');
      // Refresh the switcher to show updated name/emoji
      initShopSwitcher();
    } else {
      toast('error', data.error || 'Lỗi lưu cấu hình');
    }
  } catch (err) {
    toast('error', `Lỗi: ${err.message}`);
  }
}

// ── Import CSV ─────────────────────────────────────────────────────────────────
let _csvFile = null;

function openImportCSV() {
  _csvFile = null;
  document.getElementById('csv-file-name').textContent = '';
  document.getElementById('csv-import-result').style.display = 'none';
  document.getElementById('csv-file-input').value = '';
  
  // Populate shop options
  const sel = document.getElementById('csv-target-shop');
  sel.innerHTML = '';
  Object.values(currentShopsData).forEach(shop => {
    const opt = document.createElement('option');
    opt.value = shop.id;
    opt.textContent = `${shop.emoji || '🏬'} ${shop.name}`;
    sel.appendChild(opt);
  });
  // Default to active shop
  sel.value = document.getElementById('shop-switcher').value;
  
  openModal('import-csv-modal');
}

function handleCSVDrop(event) {
  event.preventDefault();
  document.getElementById('csv-drop-zone').classList.remove('dragover');
  const f = event.dataTransfer.files[0];
  if (f && f.name.endsWith('.csv')) {
    _csvFile = f;
    document.getElementById('csv-file-name').textContent = `✅ ${f.name}`;
  } else {
    toast('error', 'Chỉ chấp nhận file .csv');
  }
}

function onCSVFileSelected() {
  const f = document.getElementById('csv-file-input').files[0];
  if (f) {
    _csvFile = f;
    document.getElementById('csv-file-name').textContent = `✅ ${f.name}`;
  }
}

async function doImportCSV() {
  if (!_csvFile) return toast('error', 'Chưa chọn file CSV');
  const targetShop = document.getElementById('csv-target-shop').value;
  
  const btn = document.getElementById('csv-import-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Đang import...';
  
  const form = new FormData();
  form.append('file', _csvFile);
  
  const result = document.getElementById('csv-import-result');
  try {
    const res = await fetch(`/api/import-csv?target_shop=${targetShop}`, {
      method: 'POST', body: form
    });
    const data = await res.json();
    if (data.ok) {
      result.style.display = 'block';
      result.style.background = 'rgba(52,211,153,0.15)';
      result.style.color = 'var(--green)';
      result.innerHTML = `✅ Đã tạo thành công thư mục (số tự động tăng).<br><span style="font-size:0.8rem;color:var(--text3)">Vui lòng Restart Watcher để xem sản phẩm mới.</span>`;
      setTimeout(() => { closeModal('csv-modal'); loadProducts(); }, 2000);
    } else {
      throw new Error(data.error);
    }
  } catch (e) {
    result.style.display = 'block';
    result.style.background = 'rgba(248,113,113,0.15)';
    result.style.color = 'var(--red)';
    result.innerHTML = `❌ Lỗi: ${e.message}`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = '📥 Start Import';
  }
}

function cleanDuplicates() {
  if (!confirm("Bạn có chắc chắn muốn dọn sạch các bản nháp trùng lặp tiêu đề trên Etsy?\n\nHành động này sẽ mở Chrome tự động tìm và xoá các bản nháp trùng lặp.")) {
    return;
  }
  
  const btn = document.querySelector('button[onclick="cleanDuplicates()"]');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Đang dọn trùng...';
  }
  
  const es = new EventSource('/api/clean-duplicates');
  
  es.onmessage = function(e) {
    try {
      const data = JSON.parse(e.data);
      if (data.type === 'finished') {
        es.close();
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = '🧹 Dọn trùng Drafts';
        }
        alert("✅ Đã hoàn thành tiến trình dọn dẹp bản nháp trùng lặp trên Etsy!");
      }
    } catch(err) {
      console.log("[Clean Duplicates Log]:", e.data);
    }
  };
  
  es.onerror = function() {
    es.close();
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '🧹 Dọn trùng Drafts';
    }
  };
}

// ── Repair Modal & Scanning ──────────────────────────────────────────────────
function openRepairModal() {
  document.getElementById('repair-listing-id').value = '';
  document.getElementById('repair-result').style.display = 'none';
  document.getElementById('scan-result').style.display = 'none';
  document.getElementById('scan-errors-list').innerHTML = '';
  document.getElementById('scan-log').innerHTML = '';
  document.getElementById('repair-modal').classList.add('open');
}

function startScan() {
  const btn = document.getElementById('scan-btn');
  const resDiv = document.getElementById('scan-result');
  const logDiv = document.getElementById('scan-log');
  const listDiv = document.getElementById('scan-errors-list');
  
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Đang quét...';
  resDiv.style.display = 'block';
  logDiv.innerHTML = 'Đang mở Chrome để quét...';
  listDiv.innerHTML = '';
  
  const evtSource = new EventSource('/api/scan-listings');
  
  evtSource.onmessage = function(e) {
    try {
      const data = JSON.parse(e.data);
      if (data.type === 'finished') {
        evtSource.close();
        btn.disabled = false;
        btn.innerHTML = '🔍 Quét lại';
        logDiv.innerHTML = '✅ Quét hoàn tất!';
        return;
      }
      
      if (data.type === 'info') {
        logDiv.innerHTML = data.msg;
      } else if (data.type === 'error' || data.type === 'fatal') {
        logDiv.innerHTML = `<span style="color:var(--red)">${data.msg || data.error}</span>`;
        if (data.type === 'fatal') {
            evtSource.close();
            btn.disabled = false;
            btn.innerHTML = '🔍 Quét lại';
        }
      } else if (data.type === 'found') {
        // Render error item
        const item = document.createElement('div');
        item.style.padding = '6px 10px';
        item.style.background = 'rgba(248,113,113,0.1)';
        item.style.border = '1px solid rgba(248,113,113,0.2)';
        item.style.borderRadius = '4px';
        item.style.display = 'flex';
        item.style.justifyContent = 'space-between';
        item.style.alignItems = 'center';
        
        let errs = data.errors.map(err => `<span style="display:inline-block; margin-right:6px; padding:2px 6px; background:var(--bg2); border-radius:4px; font-size:10px; color:var(--red);">${err}</span>`).join('');
        
        item.innerHTML = `
          <div>
            <div style="font-size:12px; font-weight:600; color:var(--text1)">${data.id} <span style="font-weight:400; color:var(--text3); margin-left:6px">${data.title}</span></div>
            <div style="margin-top:4px;">${errs}</div>
          </div>
          <button class="btn-icon-sm" onclick="selectListingToRepair('${data.id}')" title="Chọn để sửa" style="color:var(--accent2); background:rgba(124,90,246,0.1); border-radius:4px; padding:4px 8px;">Chọn</button>
        `;
        listDiv.appendChild(item);
      }
    } catch(err) {
      console.log("SSE error:", err);
    }
  };
  
  evtSource.onerror = function() {
    evtSource.close();
    btn.disabled = false;
    btn.innerHTML = '🔍 Quét shop';
    logDiv.innerHTML = '<span style="color:var(--red)">Mất kết nối với server.</span>';
  };
}

function selectListingToRepair(id) {
  document.getElementById('repair-listing-id').value = id;
}

async function doRepair() {
  let val = document.getElementById('repair-listing-id').value.trim();
  if (!val) { toast('error', 'Vui lòng nhập Listing ID hoặc URL'); return; }
  
  // Extract ID if URL is passed
  let m = val.match(/listing\/(\d+)/) || val.match(/edit\/(\d+)/);
  if (m) val = m[1];
  const listingId = val;

  const fixTabs = document.getElementById('repair-tabs').checked;
  const fixDesc = document.getElementById('repair-desc').checked;
  const fixTags = document.getElementById('repair-tags').checked;

  const btn = document.getElementById('repair-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Đang sửa lỗi...';
  
  const result = document.getElementById('repair-result');
  result.style.display = 'none';
  
  toast('info', `🤖 Đang chạy script sửa lỗi cho ${listingId}...`);

  try {
    const res = await fetch('/api/repair-listing', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ listing_id: listingId, fix_tabs: fixTabs, fix_desc: fixDesc, fix_tags: fixTags })
    });
    const data = await res.json();
    result.style.display = 'block';
    
    if (data.ok) {
      result.style.background = 'rgba(52,211,153,0.15)';
      result.style.color = 'var(--green)';
      result.innerHTML = `✅ Sửa lỗi hoàn tất.<br><pre style="margin-top:8px;font-size:10px;white-space:pre-wrap;">${data.output}</pre>`;
      toast('success', 'Sửa lỗi thành công!');
    } else {
      throw new Error(data.error || 'Unknown error');
    }
  } catch (e) {
    result.style.display = 'block';
    result.style.background = 'rgba(248,113,113,0.15)';
    result.style.color = 'var(--red)';
    result.innerHTML = `❌ Lỗi: ${e.message}`;
    toast('error', `Lỗi chạy repair script: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '🛠 Bắt đầu sửa';
  }
}

// ── Sync to Shop ──────────────────────────────────────────────────────────────
let _syncRows = [];
let _syncConflictPayload = null;

function openSyncModal() {
  const checkboxes = selectedBatchCheckboxes('local');
  if (!checkboxes.length) return toast('error', 'Chưa chọn sản phẩm local nào');
  
  _syncRows = [...checkboxes].map(cb => parseInt(cb.value));
  const folders = [...checkboxes].map(cb => cb.dataset.folder);
  
  document.getElementById('sync-product-list').innerHTML =
    `<strong>${_syncRows.length} sản phẩm sẽ được sync:</strong><br>` +
    folders.map(f => `• ${f}`).join('<br>');
  
  document.getElementById('sync-result').style.display = 'none';
  document.getElementById('sync-copy-files').checked = false;
  _syncConflictPayload = null;
  
  // Populate target shop options (exclude current)
  const active = document.getElementById('shop-switcher').value;
  const sel = document.getElementById('sync-target-shop');
  sel.innerHTML = '';
  Object.values(currentShopsData).forEach(shop => {
    if (shop.id === active) return;
    const opt = document.createElement('option');
    opt.value = shop.id;
    opt.textContent = `${shop.emoji || '🏬'} ${shop.name}`;
    sel.appendChild(opt);
  });
  
  if (!sel.options.length) return toast('error', 'Không có shop đích nào khác');
  openModal('sync-modal');
}

async function doSync() {
  const targetShop = document.getElementById('sync-target-shop').value;
  const copyFiles  = document.getElementById('sync-copy-files').checked;
  
  const btn = document.getElementById('sync-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Đang sync...';
  
  const result = document.getElementById('sync-result');
  try {
    const res = await fetch('/api/products/sync-to-shop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_shop: targetShop, rows: _syncRows, copy_files: copyFiles })
    });
    const data = await res.json();
    if (data.ok) {
      if (data.has_conflicts) {
        _syncConflictPayload = {
          target_shop: targetShop,
          rows: _syncRows,
          copy_files: copyFiles
        };
        
        result.style.display = 'block';
        result.style.background = 'rgba(251,146,60,0.06)';
        result.style.border = '1px solid var(--orange)';
        result.style.color = 'var(--text)';
        
        const conflictItemsHtml = data.conflicts.map(c => {
          return `
            <div style="padding: 6px 8px; border-bottom: 1px solid rgba(255,255,255,0.04); display: flex; flex-direction: column; gap: 3px;">
              <div style="display: flex; justify-content: space-between; font-size: 0.8rem; font-weight: 500;">
                <span style="color: var(--orange); font-weight: 600;">⚠️ Trùng: ${c.src_folder}</span>
                <span style="color: var(--text3);">↳ Shop đích: ${c.dst_folder} (Dòng ${c.dst_row})</span>
              </div>
              <div style="font-size: 0.75rem; color: var(--text2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding-left: 10px;">
                <strong>Nguồn:</strong> ${c.src_title || 'Không có tiêu đề'}
              </div>
              <div style="font-size: 0.75rem; color: var(--text3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding-left: 10px;">
                <strong>Đích:</strong> ${c.dst_title || 'Không có tiêu đề'}
              </div>
            </div>
          `;
        }).join('');
        
        const shopName = currentShopsData[targetShop]?.name || targetShop;
        result.innerHTML = `
          <div style="margin-bottom: 10px; font-weight: 600; color: var(--orange); font-size: 0.88rem; display: flex; align-items: center; gap: 6px;">
            <span>⚠️ Phát hiện trùng lặp tại shop "${shopName}"</span>
          </div>
          <p style="font-size: 0.8rem; color: var(--text2); margin: 0 0 10px 0; line-height: 1.4;">
            Có <strong>${data.conflicts.length}</strong> sản phẩm trùng tên hoặc từ khoá tại shop đích. Anh muốn xử lý thế nào?
          </p>
          <div style="max-height: 150px; overflow-y: auto; background: rgba(0,0,0,0.25); border-radius: 6px; border: 1px solid rgba(255,255,255,0.05); margin-bottom: 12px; display: flex; flex-direction: column;">
            ${conflictItemsHtml}
          </div>
          <div style="display: flex; gap: 8px; justify-content: flex-end; align-items: center;">
            <button class="btn btn-ghost btn-sm" onclick="submitSyncConflict('cancel')">Huỷ</button>
            <button class="btn btn-warning btn-sm" onclick="submitSyncConflict('skip')">Bỏ qua trùng</button>
            <button class="btn btn-primary btn-sm" onclick="submitSyncConflict('merge')" style="background: var(--orange); border-color: var(--orange); color: #000; font-weight: 600;">Ghi đè (Merge)</button>
          </div>
        `;
        toast('warning', `⚠️ Phát hiện ${data.conflicts.length} sản phẩm trùng!`);
      } else {
        result.style.display = 'block';
        result.style.background = 'rgba(52,211,153,0.1)';
        result.style.border = '1px solid var(--green)';
        result.style.color = 'var(--green)';
        const shopName = currentShopsData[data.target]?.name || data.target;
        result.innerHTML = `✅ Đã sync <strong>${data.synced}</strong> sản phẩm sang <strong>${shopName}</strong>!`;
        toast('success', `✅ Sync ${data.synced} sản phẩm sang ${shopName} thành công!`);
        selectedBatchCheckboxes('local').forEach(cb => cb.checked = false);
        updateBatchUI();
      }
    } else {
      result.style.display = 'block';
      result.style.background = 'rgba(239,68,68,0.1)';
      result.style.border = '1px solid var(--red)';
      result.style.color = 'var(--red)';
      result.textContent = `❌ ${data.error || 'Sync thất bại'}`;
    }
  } catch(e) {
    toast('error', `Lỗi: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '🔄 Sync';
  }
}

async function submitSyncConflict(resolution) {
  if (resolution === 'cancel') {
    document.getElementById('sync-result').style.display = 'none';
    _syncConflictPayload = null;
    return;
  }
  
  if (!_syncConflictPayload) {
    return toast('error', 'Không tìm thấy thông tin sync');
  }
  
  const btn = document.getElementById('sync-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Đang sync...';
  
  const result = document.getElementById('sync-result');
  try {
    const res = await fetch('/api/products/sync-to-shop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        target_shop: _syncConflictPayload.target_shop,
        rows: _syncConflictPayload.rows,
        copy_files: _syncConflictPayload.copy_files,
        conflict_resolution: resolution
      })
    });
    const data = await res.json();
    if (data.ok) {
      result.style.display = 'block';
      result.style.background = 'rgba(52,211,153,0.1)';
      result.style.border = '1px solid var(--green)';
      result.style.color = 'var(--green)';
      const shopName = currentShopsData[data.target]?.name || data.target;
      const actionText = resolution === 'merge' ? 'ghi đè' : 'bỏ qua trùng';
      result.innerHTML = `✅ Đã sync xong! (Chế độ: <strong>${actionText}</strong>) - Đã cập nhật/tạo <strong>${data.synced}</strong> sản phẩm sang <strong>${shopName}</strong>!`;
      toast('success', `✅ Sync sang ${shopName} thành công!`);
      selectedBatchCheckboxes('local').forEach(cb => cb.checked = false);
      updateBatchUI();
      _syncConflictPayload = null;
    } else {
      result.style.display = 'block';
      result.style.background = 'rgba(239,68,68,0.1)';
      result.style.border = '1px solid var(--red)';
      result.style.color = 'var(--red)';
      result.textContent = `❌ ${data.error || 'Sync thất bại'}`;
    }
  } catch(e) {
    toast('error', `Lỗi: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '🔄 Sync';
  }
}

// ── Products ───────────────────────────────────────────────────────────────────
function getActiveShopId() {
  return document.getElementById('shop-switcher')?.value?.trim() || '';
}

function cloudAssetStatusUrl(shopId, folder = '') {
  const query = `shop_id=${encodeURIComponent(shopId)}&scope=shop`;
  return folder
    ? `/api/cloud-assets/status?${query}&folder=${encodeURIComponent(folder)}`
    : `/api/cloud-assets/status?${query}`;
}

function cloudAssetStatusItems(data) {
  if (Array.isArray(data?.items)) return data.items;
  if (Array.isArray(data?.products)) return data.products;
  return [];
}

function cloudAssetStatusItemFolder(item) {
  const explicitFolder = item?.folder || item?.product_name || item?.product_identity?.product;
  if (explicitFolder) return String(explicitFolder).trim();
  const productKey = item?.product_key || item?.product_identity?.key || item?.product;
  return String(productKey || '').split('/').filter(Boolean).pop() || '';
}

function applyCloudAssetStatusItems(data, replace = false) {
  const next = replace ? new Map() : new Map(cloudAssetStatusByFolder);
  cloudAssetStatusItems(data).forEach(item => {
    const folder = cloudAssetStatusItemFolder(item);
    if (folder) next.set(folder, { ...item, folder });
  });
  cloudAssetStatusByFolder = next;
  return next;
}

function cloudAssetStatusForFolder(folder) {
  const key = String(folder || '').trim();
  return key ? cloudAssetStatusByFolder.get(key) || null : null;
}

function cloudAssetStatusCategory(status) {
  if (!status) return 'local';
  const state = String(status.state || '').trim().toUpperCase();
  const meta = CLOUD_ASSET_STATUS_META[state];
  if (meta) return meta.category;
  if (state === 'CLOUD_ONLY' || (status.cloud_available && !status.local_available)) return 'cloud-only';
  if (state === 'ERROR' || state === 'DIRTY_LOCAL' || status.ok === false) return 'error';
  return 'local';
}

function cloudAssetStatusLabel(status) {
  if (!status) return cloudAssetStatusError ? '☁️ Cloud unavailable' : '☁️ Chưa kiểm tra cloud';
  const state = String(status.state || '').trim().toUpperCase();
  return CLOUD_ASSET_STATUS_META[state]?.label || `☁️ ${state || 'Cloud status'}`;
}

function cloudStatusFilterMatches(record, filter) {
  if (!filter || filter === 'all') return true;
  const folder = String(record?.folder || '').trim();
  if (!folder) return false;
  return cloudAssetStatusCategory(cloudAssetStatusForFolder(folder)) === filter;
}

function formatCloudAssetBytes(value) {
  if (value === null || value === undefined || value === '') return '';
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return '';
  if (bytes < 1000) return `${bytes} B`;
  if (bytes < 1000 ** 2) return `${(bytes / 1000).toFixed(1)} KB`;
  if (bytes < 1000 ** 3) return `${(bytes / 1000 ** 2).toFixed(2)} MB`;
  return `${(bytes / 1000 ** 3).toFixed(2)} GB`;
}

function formatCloudAssetDate(value) {
  if (!value) return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleDateString('vi-VN');
}

function cloudAssetDetailsHtml(status) {
  if (!status) return '';
  const details = [];
  const eligibleAfter = status.eligible_after || status.offload_after || status.offload_date;
  if (eligibleAfter) details.push(`offload ${escHtml(formatCloudAssetDate(eligibleAfter))}`);
  const uploadSchedule = status.upload_schedule;
  if (uploadSchedule?.status === 'queued') {
    details.push(escHtml(uploadSchedule.wait_reason ? `chờ ${uploadSchedule.wait_reason}` : 'chờ Etsy rảnh'));
  } else if (uploadSchedule?.status === 'running') {
    details.push('đang upload & verify');
  }
  const suppliedBytes = status.reclaimable_bytes ?? status.bytes ?? status.counts?.total_bytes;
  const bytes = formatCloudAssetBytes(suppliedBytes);
  if (bytes) details.push(`♻ ${escHtml(bytes)}`);
  const error = status.last_error || status.error || status.local_error || status.remote_error;
  if (error) details.push(escHtml(String(error)));
  return details.length ? `<span class="cloud-asset-details">${details.join(' · ')}</span>` : '';
}

function cloudAssetActionButton(folder, operation, label, title) {
  const safeFolder = escJs(folder);
  const handlers = {
    status: `cloudAssetRefreshStatus('${safeFolder}', this)`,
    upload: `cloudAssetUploadAndVerify('${safeFolder}', this)`,
    restore: `cloudAssetRestore('${safeFolder}', this)`,
    cancel: `cloudAssetCancelOffload('${safeFolder}', this)`,
  };
  return `<button type="button" class="btn btn-ghost btn-sm cloud-asset-action" data-cloud-operation="${operation}" onclick="${handlers[operation]}" title="${title}">${label}</button>`;
}

function cloudAssetActionsHtml(folder, status) {
  const state = String(status?.state || '').trim().toUpperCase();
  const actions = [cloudAssetActionButton(folder, 'status', '↻', 'Làm mới trạng thái cloud')];
  if (state === 'CLOUD_ONLY' || (state === 'ERROR' && status?.cloud_available && !status?.local_available)) {
    actions.push(cloudAssetActionButton(folder, 'restore', '☁↓', 'Restore asset từ cloud về local'));
  } else if (state === 'OFFLOAD_SCHEDULED') {
    actions.push(cloudAssetActionButton(folder, 'cancel', '✕ lịch', 'Huỷ lịch offload'));
  } else if (state === 'UPLOAD_SCHEDULED' || state === 'UPLOADING') {
    // Upload has already been explicitly queued.  Keep the card read-only
    // until the server-side, Etsy-idle worker completes it.
  } else {
    actions.push(cloudAssetActionButton(folder, 'upload', '☁↑', 'Xếp lịch Upload & verify khi Etsy rảnh'));
  }
  return `<div class="cloud-asset-actions" aria-label="Cloud asset actions">${actions.join('')}</div>`;
}

function renderCloudAssetUi(folder) {
  const safeFolder = String(folder || '').trim();
  if (!safeFolder) return '';
  const status = cloudAssetStatusForFolder(safeFolder);
  const category = cloudAssetStatusCategory(status);
  const badgeLabel = cloudAssetStatusLabel(status);
  return `<div class="cloud-asset-ui" data-cloud-folder="${escHtml(safeFolder)}">
    <div class="cloud-asset-status-line"><span class="cloud-asset-badge cloud-status-${category}" title="Trạng thái asset cloud của ${escHtml(safeFolder)}">${escHtml(badgeLabel)}</span>${cloudAssetDetailsHtml(status)}</div>
    ${cloudAssetActionsHtml(safeFolder, status)}
  </div>`;
}

async function loadCloudAssetStatus({ force = false } = {}) {
  const shopId = getActiveShopId();
  if (!shopId) return null;
  if (!force && cloudAssetStatusPromise && cloudAssetStatusPromiseShop === shopId) {
    return cloudAssetStatusPromise;
  }

  const requestId = ++cloudAssetStatusRequestId;
  cloudAssetStatusPromiseShop = shopId;
  const request = (async () => {
    try {
      const res = await fetch(cloudAssetStatusUrl(shopId));
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.error || `Cloud status HTTP ${res.status}`);
      if (requestId !== cloudAssetStatusRequestId) return data;
      applyCloudAssetStatusItems(data, true);
      cloudAssetStatusAvailable = true;
      cloudAssetStatusError = '';
      if (currentProductSource !== 'shop') filterProducts();
      return data;
    } catch (error) {
      if (requestId === cloudAssetStatusRequestId) {
        cloudAssetStatusAvailable = false;
        cloudAssetStatusError = error.message || 'cloud status unavailable';
        console.warn('[Cloud asset status]', error);
      }
      return null;
    }
  })();
  cloudAssetStatusPromise = request;
  try {
    return await request;
  } finally {
    if (cloudAssetStatusPromise === request) cloudAssetStatusPromise = null;
  }
}

function cloudAssetRequestPayload(folder) {
  const shopId = getActiveShopId();
  const safeFolder = String(folder || '').trim();
  if (!shopId) throw new Error('Không đọc được shop hiện tại');
  if (!safeFolder) throw new Error('Thiếu folder cloud asset');
  return { shop_id: shopId, scope: 'shop', folder: safeFolder };
}

async function postCloudAssetMutation(endpoint, payload) {
  const normalizedPayload = {
    operation: endpoint,
    shop_id: String(payload?.shop_id || '').trim(),
    scope: String(payload?.scope || '').trim(),
    folder: String(payload?.folder || '').trim(),
  };
  if (!normalizedPayload.shop_id || !normalizedPayload.scope || !normalizedPayload.folder) {
    throw new Error('Thiếu thông tin cloud mutation');
  }
  const requestKey = `${normalizedPayload.operation}|${normalizedPayload.shop_id}|${normalizedPayload.scope}|${normalizedPayload.folder}`;
  const inFlight = cloudAssetMutationInFlight.get(requestKey);
  if (inFlight) {
    return inFlight;
  }

  const promise = (async () => {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      throw new Error(data.detail || data.error || `Cloud operation HTTP ${res.status}`);
    }
    return data;
  })();

  cloudAssetMutationInFlight.set(requestKey, promise);
  try {
    return await promise;
  } finally {
    const current = cloudAssetMutationInFlight.get(requestKey);
    if (current === promise) {
      cloudAssetMutationInFlight.delete(requestKey);
    }
  }
}

function cloudAssetButtonBusy(button, label) {
  if (!button) return '';
  const original = button.innerHTML;
  button.disabled = true;
  button.innerHTML = `<span class="spinner"></span> ${label}`;
  return original;
}

function cloudAssetButtonRestore(button, original) {
  if (!button) return;
  button.disabled = false;
  button.innerHTML = original;
}

function _cloudAssetMutationKey(operation, payload) {
  const safeShop = String(payload?.shop_id || '').trim();
  const safeScope = String(payload?.scope || 'shop').trim();
  const safeFolder = String(payload?.folder || '').trim();
  return `${operation}|${safeShop}|${safeScope}|${safeFolder}`;
}

async function withCloudAssetMutation(operation, button, payload, action) {
  const key = _cloudAssetMutationKey(operation, payload);
  const inFlight = cloudAssetMutationInFlight.get(key);
  if (inFlight) {
    return inFlight;
  }
  const original = cloudAssetButtonBusy(button, '...');
  const promise = (async () => {
    try {
      return await action();
    } finally {
      cloudAssetButtonRestore(button, original);
    }
  })();
  cloudAssetMutationInFlight.set(key, promise);
  try {
    return await promise;
  } finally {
    const current = cloudAssetMutationInFlight.get(key);
    if (current === promise) {
      cloudAssetMutationInFlight.delete(key);
    }
  }
}

async function cloudAssetRefreshStatus(folder, button) {
  const original = cloudAssetButtonBusy(button, '...');
  try {
    const payload = cloudAssetRequestPayload(folder);
    const res = await fetch(cloudAssetStatusUrl(payload.shop_id, payload.folder));
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || data.error || `Cloud status HTTP ${res.status}`);
    applyCloudAssetStatusItems(data, false);
    cloudAssetStatusAvailable = true;
    cloudAssetStatusError = '';
    filterProducts();
    toast('success', `☁️ Đã làm mới trạng thái ${payload.folder}`);
  } catch (error) {
    toast('error', `❌ Cloud status ${folder}: ${error.message}`);
  } finally {
    cloudAssetButtonRestore(button, original);
  }
}

async function cloudAssetUploadAndVerify(folder, button) {
  try {
    const payload = cloudAssetRequestPayload(folder);
    await withCloudAssetMutation(
      'upload-and-verify',
      button,
      payload,
      async () => {
        const data = await postCloudAssetMutation('/api/cloud-assets/schedule-upload-verify', payload);
        await loadCloudAssetStatus({ force: true });
        if (data?.created === false) {
          toast('info', `🗓️ ${payload.folder} đã có lịch Upload & verify`);
        } else if (data?.schedule?.wait_reason) {
          toast('info', `🗓️ Đã xếp lịch ${payload.folder}; ${data.schedule.wait_reason}`);
        } else {
          toast('success', `🗓️ Đã xếp lịch Upload & verify: ${payload.folder}`);
        }
      },
    );
  } catch (error) {
    toast('error', `❌ Không xếp được lịch cloud ${folder}: ${error.message}`);
  } finally {
    // `withCloudAssetMutation` handles button restore on both primary
    // owner and deduplicated waits.
  }
}

async function cloudAssetRestore(folder, button) {
  try {
    const payload = cloudAssetRequestPayload(folder);
    await withCloudAssetMutation(
      'restore',
      button,
      payload,
      async () => {
        toast('info', `☁️ Đang restore ${payload.folder}...`);
        await postCloudAssetMutation('/api/cloud-assets/restore', payload);
        await loadCloudAssetStatus({ force: true });
        toast('success', `✅ Đã restore ${payload.folder} về local`);
      },
    );
  } catch (error) {
    toast('error', `❌ Cloud restore ${folder}: ${error.message}`);
  } finally {
    // `withCloudAssetMutation` handles button restore on both primary
    // owner and deduplicated waits.
  }
}

async function cloudAssetCancelOffload(folder, button) {
  try {
    const payload = cloudAssetRequestPayload(folder);
    await withCloudAssetMutation(
      'cancel-offload',
      button,
      payload,
      async () => {
        toast('info', `☁️ Đang huỷ lịch offload ${payload.folder}...`);
        await postCloudAssetMutation('/api/cloud-assets/cancel-offload', payload);
        await loadCloudAssetStatus({ force: true });
        toast('success', `✅ Đã huỷ lịch offload ${payload.folder}`);
      },
    );
  } catch (error) {
    toast('error', `❌ Huỷ lịch offload ${folder}: ${error.message}`);
  } finally {
    // `withCloudAssetMutation` handles button restore on both primary
    // owner and deduplicated waits.
  }
}

async function loadProducts(options = {}) {
  const { throwOnError = false } = options;
  try {
    const res  = await fetch('/api/products');
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || data.error || `Không tải được sản phẩm (HTTP ${res.status})`);
    allProducts = data.products || [];
    etsyManagerSnapshot = data.etsy_manager || null;
    updateProductSourceSwitcher();
    setProductSource(currentProductSource, true);
    updateStats(allProducts);
    updateEtsyManagerStats(etsyManagerSnapshot);
    void loadCloudAssetStatus();
    await loadAggregateCatalog({ throwOnError });
    refreshScrollNavState();
    return allProducts;
  } catch (e) {
    document.getElementById('product-grid').innerHTML =
      `<div class="loading-state" style="color:var(--red)">❌ Không kết nối được backend: ${e.message}</div>`;
    refreshScrollNavState();
    if (throwOnError) throw e;
    return null;
  }
}

function formatJobDuration(job) {
  const start = Number(job?.started_at || job?.created_at || 0);
  const end = Number(job?.finished_at || Date.now() / 1000);
  if (!start || end < start) return '—';
  const seconds = Math.max(0, Math.round(end - start));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function jobStatusLabel(status) {
  return {
    queued: 'Queued', running: 'Running', succeeded: 'Done',
    failed: 'Failed', cancelled: 'Cancelled',
  }[String(status || '').toLowerCase()] || String(status || 'Unknown');
}

function toggleJobCenter(force) {
  const drawer = document.getElementById('job-center-drawer');
  const toggle = document.getElementById('job-center-toggle');
  if (!drawer) return;
  jobCenterOpen = typeof force === 'boolean' ? force : !jobCenterOpen;
  drawer.classList.toggle('open', jobCenterOpen);
  drawer.setAttribute('aria-hidden', jobCenterOpen ? 'false' : 'true');
  if (toggle) toggle.setAttribute('aria-expanded', jobCenterOpen ? 'true' : 'false');
  if (jobCenterOpen) loadJobCenter();
}

function renderJobCenter(jobs) {
  const list = document.getElementById('job-center-list');
  const summary = document.getElementById('job-center-summary');
  const count = document.getElementById('job-center-count');
  if (!list) return;
  const active = jobs.filter(job => ['queued', 'running'].includes(String(job.status || '').toLowerCase()));
  if (count) count.textContent = String(active.length);
  if (summary) summary.textContent = `${active.length} active · ${jobs.length} recent`;
  if (!jobs.length) {
    list.innerHTML = '<div class="loading-state">Chưa có job durable nào cho shop này.</div>';
    return;
  }
  list.innerHTML = jobs.map(job => {
    const status = String(job.status || '').toLowerCase();
    const terminal = !['queued', 'running'].includes(status);
    const message = escHtml(String(job.last_message || ''));
    const fields = Array.isArray(job.fields) && job.fields.length ? ` · ${escHtml(job.fields.join(', '))}` : '';
    return `<article class="job-center-item job-status-${escHtml(status)}">
      <div class="job-center-item-head"><strong>${escHtml(jobStatusLabel(status))}</strong><time>${escHtml(formatJobDuration(job))}</time></div>
      <div class="job-center-item-title">${escHtml(job.folder || 'product')} · ${escHtml(job.operation || 'operation')}</div>
      <div class="job-center-item-meta">${escHtml(job.shop_id || '')} · row ${escHtml(job.row ?? '—')} · Etsy ${escHtml(job.listing_id || '—')}${fields}</div>
      <div class="job-center-item-message">${message || '—'}</div>
      <div class="job-center-item-actions">
        ${terminal && ['failed', 'cancelled'].includes(status) ? `<button class="btn btn-ghost btn-sm" type="button" onclick="retryJobCenterJob('${escJs(job.job_id)}')">↻ Retry</button>` : ''}
        ${!terminal ? `<button class="btn btn-danger btn-sm" type="button" onclick="cancelJobCenterJob('${escJs(job.job_id)}')">Cancel</button>` : ''}
      </div>
    </article>`;
  }).join('');
}

async function loadJobCenter() {
  try {
    const response = await fetch('/api/etsy/jobs?limit=50');
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
    renderJobCenter(Array.isArray(data.jobs) ? data.jobs : []);
  } catch (error) {
    const summary = document.getElementById('job-center-summary');
    if (summary) summary.textContent = `Job Center unavailable: ${error.message}`;
  }
}

async function cancelJobCenterJob(jobId) {
  try {
    const response = await fetch(`/api/etsy/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) throw new Error(data.detail || data.error || 'Cancel failed');
    toast('warning', `Đã huỷ job ${jobId}`);
    await loadJobCenter();
  } catch (error) {
    toast('error', `Không thể huỷ job: ${error.message}`);
  }
}

async function retryJobCenterJob(jobId) {
  if (!confirm(`Retry job ${jobId}? Dashboard sẽ kiểm tra lại asset trước khi chạy.`)) return;
  try {
    const response = await fetch(`/api/etsy/jobs/${encodeURIComponent(jobId)}/retry`, { method: 'POST' });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) throw new Error(data.detail || data.error || 'Retry failed');
    toast('info', `Đã xếp lại job ${data.job_id || jobId}`);
    await loadJobCenter();
  } catch (error) {
    toast('error', `Không thể retry job: ${error.message}`);
  }
}

async function loadAggregateCatalog(options = {}) {
  const { throwOnError = false } = options;
  try {
    const res = await fetch('/api/aggregate-products');
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || data.error || 'Không tải được catalog tổng');
    aggregateCatalog = data;
    updateAggregateCatalogStats(data);
    updateEtsyManagerStats(etsyManagerSnapshot);
    updateProductSourceSwitcher();
    if (['local', 'aggregate'].includes(currentProductSource)) filterProducts();
    refreshScrollNavState();
    return aggregateCatalog;
  } catch (e) {
    aggregateCatalog = null;
    console.warn('[Aggregate catalog]', e);
    const grid = document.getElementById('product-grid');
    updateProductSourceSwitcher();
    updateEtsyManagerStats(etsyManagerSnapshot);
    if (currentProductSource === 'local') {
      filterProducts();
    } else if (currentProductSource === 'aggregate') {
      if (grid) {
        grid.innerHTML = '<div class="loading-state">⚠️ Catalog tổng tạm thời không khả dụng. Hãy tải lại hoặc kiểm tra Live Logs.</div>';
      }
      updateStats([]);
    }
    refreshScrollNavState();
    if (throwOnError) throw e;
  }
}

function updateStats(products) {
  const posted  = products.filter(p => String(p.status || '').includes('Đã đăng')).length;
  const pending = products.filter(p => String(p.status || '').includes('Chờ đăng')).length;
  const errors  = products.filter(p => {
    const status = String(p.status || '');
    return status.includes('Lỗi') || status.includes('❌');
  }).length;
  document.getElementById('stat-total').textContent   = products.length;
  document.getElementById('stat-posted').textContent  = posted;
  document.getElementById('stat-pending').textContent = pending;
  document.getElementById('stat-error').textContent   = errors;
}

function updateEtsyManagerStats(snapshot) {
  const el = document.getElementById('etsy-sync-strip');
  if (!el) return;
  const counts = snapshot && snapshot.counts;
  if (!counts) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  el.style.display = 'flex';
  const activeCount = counts.active || 0;
  const draftCount = counts.draft || 0;
  const inactiveCount = counts.inactive || 0;
  const expiredCount = counts.expired || 0;
  const snapshotTotal = Number.isFinite(Number(counts.total))
    ? Number(counts.total)
    : activeCount + draftCount + inactiveCount + expiredCount;
  const aggregateLocalRecords = aggregateCatalog ? getAggregateLocalRecords() : [];
  const aggregateLocalFolderCount = aggregateLocalRecords.length;
  // A workbook row only means that a local folder has been registered. It does
  // not mean it has an Etsy listing. Use the catalog's exact ID reconciliation
  // instead, so a local-only folder is never presented as "already synced".
  const catalogCounts = aggregateCatalog?.counts || {};
  const mappedLocalCount = Number.isFinite(Number(catalogCounts.mapped_total))
    ? Number(catalogCounts.mapped_total)
    : aggregateLocalRecords.filter(record => String(record?.source || '').toLowerCase() === 'both').length;
  const unmappedLocalCount = Math.max(0, aggregateLocalFolderCount - mappedLocalCount);
  const localNote = aggregateCatalog
    ? `Có ${aggregateLocalFolderCount} folder local · ${mappedLocalCount} đã ghép Etsy · ${unmappedLocalCount} chưa ghép.`
    : `Dashboard đang hiển thị ${allProducts.length} dòng local đang quản lý.`;
  el.innerHTML = `
    <span>🧭 Etsy shop snapshot:</span>
    <span class="etsy-sync-pill active">Active ${activeCount}</span>
    <span class="etsy-sync-pill draft">Draft ${draftCount}</span>
    ${inactiveCount ? `<span class="etsy-sync-pill inactive">Inactive ${inactiveCount}</span>` : ''}
    ${expiredCount ? `<span class="etsy-sync-pill inactive">Expired ${expiredCount}</span>` : ''}
    <span class="etsy-sync-pill">Tổng Etsy ${snapshotTotal}</span>
    <span style="color:var(--text3)">${localNote}</span>
  `;
}

function updateAggregateCatalogStats(catalog) {
  const el = document.getElementById('catalog-strip');
  if (!el || !catalog?.counts) return;
  const c = catalog.counts;
  el.style.display = 'flex';
  el.innerHTML = `
    <span>🧮 Catalog tổng:</span>
    <span class="etsy-sync-pill">Tổng ${c.unified_total || 0}</span>
    <span class="etsy-sync-pill">Etsy ${c.etsy_total || 0}</span>
    <span class="etsy-sync-pill">Folder local ${c.local_total || 0}</span>
    <span class="etsy-sync-pill active">Đã ghép Etsy ${c.mapped_total || 0}</span>
    <span class="etsy-sync-pill ${Number(c.local_only_total || 0) ? 'draft' : 'active'}">Chưa ghép ${c.local_only_total || 0}</span>
    <span class="etsy-sync-pill draft">Nhóm trùng ${c.duplicate_groups || 0}</span>
    <span class="etsy-sync-pill inactive">Dồn an toàn ${c.safe_merge_groups || 0}</span>
  `;
}

function updateProductSourceSwitcher() {
  const selector = document.getElementById('product-source-select');
  const shopListings = getSyncableEtsyListings(etsyManagerSnapshot?.listings || []);
  if (!selector) return;
  const localOption = selector.querySelector('option[value="local"]');
  const shopOption = selector.querySelector('option[value="shop"]');
  const aggregateOption = selector.querySelector('option[value="aggregate"]');
  const localCount = aggregateCatalog
    ? getAggregateLocalRecords(aggregateCatalog).length
    : allProducts.length;
  if (localOption) localOption.textContent = `📁 Sản phẩm local (${localCount})`;
  if (shopOption) {
    shopOption.textContent = `🛍 Sản phẩm trên shop (${shopListings.length})`;
    shopOption.disabled = shopListings.length === 0;
  }
  if (aggregateOption) {
    aggregateOption.textContent = `🧮 Tổng Etsy + local (${aggregateCatalog?.counts?.unified_total || 0})`;
    aggregateOption.disabled = !aggregateCatalog;
  }
}

function setProductSource(source, skipRender = false) {
  const shopListings = getSyncableEtsyListings(etsyManagerSnapshot?.listings || []);
  if (source === 'shop' && !shopListings.length) {
    toast('info', 'Chưa có dữ liệu Etsy Shop. Hãy đồng bộ shop trước.');
    return;
  }
  currentProductSource = source;
  const sourceSelector = document.getElementById('product-source-select');
  if (sourceSelector) sourceSelector.value = source;
  const selectAll = document.getElementById('cb-select-all');
  const statusFilter = document.getElementById('filter-status');
  const cloudStatusFilter = document.getElementById('filter-cloud-status');
  if (selectAll) { selectAll.checked = false; selectAll.disabled = !['local', 'shop'].includes(source); }
  if (statusFilter) statusFilter.disabled = isStatusFilterDisabledForSource(source);
  if (cloudStatusFilter) cloudStatusFilter.disabled = source === 'shop';
  applyBatchActionVisibility(BatchSelection.getBatchActionState(source, []));
  const batchActions = document.getElementById('batch-actions');
  batchActions?.classList.remove('is-visible');
  batchActions?.style.setProperty('display', 'none', 'important');
  if (!skipRender || allProducts.length || shopListings.length) filterProducts();
}

function isStatusFilterDisabledForSource(source) {
  return source === 'shop';
}

function findAggregateLocalProduct(record) {
  if (!record) return null;
  const recordRow = record.row == null ? null : Number(record.row);
  if (Number.isFinite(recordRow)) {
    const byRow = allProducts.find(product => Number(product.row) === recordRow);
    if (byRow) return byRow;
  }

  const folder = String(record.folder || '').trim();
  if (!folder) return null;
  return allProducts.find(product => String(product.folder || '').trim() === folder) || null;
}

function aggregateDisplayProduct(record, localProduct = findAggregateLocalProduct(record)) {
  if (!localProduct) return record;
  if (String(record?.reconciliation_status || '') === 'unmatched_local_listing') {
    return {
      ...localProduct,
      status: '⏳ Chờ đăng · Chưa khớp snapshot Etsy',
      reconciliation_status: record.reconciliation_status,
      reconciliation_note: record.reconciliation_note,
    };
  }
  return localProduct;
}

function getAggregateLocalRecords(catalog = aggregateCatalog) {
  const renderableRecords = CatalogOrdering.filterRenderableCatalogRecords(catalog?.records || []);
  return renderableRecords.filter(record => {
    const source = String(record?.source || '').trim().toLowerCase();
    return (source === 'local' || source === 'both') && Boolean(String(record?.folder || '').trim());
  });
}

function statusFilterMatches(record, statusFilter) {
  if (!statusFilter) return true;

  const status = String(record?.status || '');
  const etsyStatus = String(record?.etsy_status || record?.status || '').trim();
  const statusLower = status.toLowerCase();
  const etsyStatusLower = etsyStatus.toLowerCase();
  const source = String(record?.source || '').toLowerCase();
  const isEtsyOnly = source === 'etsy';

  if (statusFilter === 'posted') {
    return (status.includes('Đã đăng') && !status.includes('draft'))
      || statusLower.includes('posted')
      || (isEtsyOnly && etsyStatusLower === 'active');
  }

  if (statusFilter === 'draft') {
    return statusLower.includes('draft') || (isEtsyOnly && etsyStatusLower === 'draft');
  }

  if (statusFilter === 'pending') {
    return status.includes('Chờ đăng') || (isEtsyOnly && etsyStatusLower === 'pending');
  }

  if (statusFilter === 'error') {
    return status.includes('Lỗi') || status.includes('❌')
      || statusLower.includes('error')
      || (isEtsyOnly && etsyStatusLower === 'error');
  }

  const missingFields = Array.isArray(record?.missing_fields) ? record.missing_fields : [];
  if (source === 'etsy') {
    return false;
  }
  if (statusFilter === 'missing_title') {
    return missingFields.includes('title');
  }
  if (statusFilter === 'missing_description') {
    return missingFields.includes('description');
  }
  if (statusFilter === 'missing_tags') {
    return missingFields.includes('tags') || missingFields.includes('tags_count');
  }
  if (statusFilter === 'missing_seo') {
    return missingFields.length > 0;
  }
  return false;
}

function filterProducts() {
  const q   = document.getElementById('search').value.toLowerCase();
  const cloudFilter = document.getElementById('filter-cloud-status')?.value || 'all';
  if (currentProductSource === 'shop') {
    const listings = getSyncableEtsyListings(etsyManagerSnapshot?.listings || []).filter(p => {
      const title = String(p.title || '').toLowerCase();
      const id = String(p.id || '');
      return !q || title.includes(q) || id.includes(q);
    });
    renderShopProducts(listings);
    return;
  }
  if (['aggregate', 'local'].includes(currentProductSource) && aggregateCatalog) {
    const renderableRecords = currentProductSource === 'local'
      ? getAggregateLocalRecords(aggregateCatalog)
      : CatalogOrdering.filterRenderableCatalogRecords(aggregateCatalog?.records || []);
    const filteredRecords = renderableRecords.filter(p => {
      const localProduct = findAggregateLocalProduct(p);
      const haystack = [p.title, p.etsy_title, p.folder, p.sku, p.listing_id, p.source_label].join(' ').toLowerCase();
      const st = document.getElementById('filter-status').value;
      const filterTarget = aggregateDisplayProduct(p, localProduct);
      const cloudTarget = { ...filterTarget, folder: p.folder || filterTarget.folder };
      return (!q || haystack.includes(q))
        && statusFilterMatches(filterTarget, st)
        && cloudStatusFilterMatches(cloudTarget, cloudFilter);
    });
    const records = CatalogOrdering.sortCatalogRecords(filteredRecords);
    renderAggregateProducts(records);
    updateStats(records.map(p => {
      const localProduct = findAggregateLocalProduct(p);
      const displayProduct = aggregateDisplayProduct(p, localProduct);
      const status = String(localProduct
        ? (displayProduct.status || displayProduct.etsy_status || '')
        : (p.etsy_status || p.status || '')
      );
      return { status };
    }));
    return;
  }
  const st  = document.getElementById('filter-status').value;
  const filtered = allProducts.filter(p => {
    const matchQ  = !q || p.title.toLowerCase().includes(q) || p.folder.toLowerCase().includes(q) || p.tags.toLowerCase().includes(q);
    return matchQ && statusFilterMatches(p, st) && cloudStatusFilterMatches(p, cloudFilter);
  });
  if (typeof catalogViewState !== 'undefined') catalogViewState.visibleCount = 0;
  renderProducts(filtered);
}

function renderAggregateProducts(records) {
  const grid = document.getElementById('product-grid');
  if (!records.length) {
    grid.innerHTML = '<div class="loading-state">Không có record nào trong catalog tổng</div>';
    refreshScrollNavState();
    return;
  }
  grid.innerHTML = records.map(record => {
    // The aggregate catalog is an index, not a second product model.  Whenever
    // a record has a local folder, render the real local product card so all
    // existing file, image, SEO, Etsy and status actions remain available.
    const localProduct = findAggregateLocalProduct(record);
    if (localProduct) return productCard(aggregateDisplayProduct(record, localProduct));

    // Etsy-only records have no local files yet.  Keep the Etsy/map actions and
    // clearly disable actions that require a local folder.
    if (record.source === 'etsy' && record.listing_id) {
      const snapshotListing = (etsyManagerSnapshot?.listings || []).find(
        listing => String(listing.id || listing.listing_id || '') === String(record.listing_id)
      );
      return remoteEtsyProductCard({
        ...(snapshotListing || {}),
        id: record.listing_id,
        listing_id: record.listing_id,
        title: record.etsy_title || record.title,
        url: snapshotListing?.url || record.etsy_url,
        managerStatus: record.etsy_status || record.status,
      });
    }

    // Defensive fallback for a stale catalog row whose local workbook row no
    // longer exists.  This should be rare, but it must not reuse the 5-column
    // actionable-card grid because it only contains two blocks.
    const title = escHtml(record.etsy_title || record.title || 'Chưa có tiêu đề');
    const source = escHtml(record.source_label || record.source || '');
    const folder = record.folder ? escHtml(record.folder) : 'Chưa có folder local';
    const listingLink = resolveEtsyListingLink({
      id: record.listing_id,
      managerStatus: record.etsy_status || record.status,
      url: record.etsy_url,
    });
    const listing = listingLink.url
      ? `<a class="product-etsy-link" href="${escHtml(listingLink.url)}" target="_blank" rel="noopener">${listingLink.kind === 'manager' ? '🛠 Etsy manager' : '🔗 Etsy'} ${escHtml(record.listing_id)}</a>`
      : record.listing_id
        ? `<span class="product-etsy-link missing" title="Không có link Etsy đã xác minh từ snapshot mới nhất">⚠️ Etsy link unavailable · ${escHtml(record.listing_id)}</span>`
        : '<span class="product-etsy-link" style="color:var(--text3)">🔗 Chưa map Etsy</span>';
    const duplicate = (aggregateCatalog?.duplicate_groups || []).find(g => g.folders.includes(record.folder));
    const duplicateHtml = duplicate ? `<div style="margin-top:8px;color:${duplicate.safe_to_merge ? 'var(--orange)' : 'var(--text3)'};font-size:11px;">⚠️ ${escHtml(duplicate.match_type)}${duplicate.safe_to_merge ? ' · đủ điều kiện dồn' : ' · chỉ review'}</div>` : '';
    const registerLocalButton = record.source === 'local' && !record.row && record.folder
      ? `<button class="btn btn-warning btn-sm product-primary-action" data-action-role="primary-next" data-action-scope="local" onclick="registerSingleLocalFolder('${escJs(record.folder)}', this)" title="Bước tiếp theo: đăng ký folder này vào catalog">♻️ Đăng ký local</button>`
      : '';
    return `<div class="product-card aggregate-summary-card">
      <div class="gallery-wrap"><div class="shop-thumb">${record.source === 'both' ? '🔗' : record.source === 'etsy' ? '🛍' : '📁'}</div></div>
      <div class="product-info">
        <div class="product-folder"><span>${folder}</span><span style="font-size:10px;color:var(--text3)">${source}</span></div>
        <div class="product-title" title="${title}">${title}</div>
        ${listing}
        <div class="product-meta"><span>🖼 ${record.image_count || 0} ảnh</span><span>📎 ${record.file_count || 0} file</span><span>${escHtml(record.etsy_status || record.status || '')}</span></div>
        ${renderCloudAssetUi(record.folder)}
        ${duplicateHtml}
        <div class="product-actions" style="margin-top:10px;">
          ${registerLocalButton}
        </div>
      </div>
    </div>`;
  }).join('');
  refreshScrollNavState();
}

async function scanAndMergeDuplicates() {
  const catalog = aggregateCatalog || (await (async () => {
    await loadAggregateCatalog();
    return aggregateCatalog;
  })());
  if (!catalog) return toast('error', 'Chưa tải được catalog tổng');
  const safeGroups = (catalog.duplicate_groups || []).filter(group => group.safe_to_merge);
  if (!safeGroups.length) {
    return toast('info', 'Không có nhóm folder trùng an toàn để dồn. Các nhóm còn lại cần review thủ công.');
  }
  const preview = safeGroups.slice(0, 12).map(group => `• ${group.folders.join(' ← ')} (${group.match_type})`).join('\n');
  const confirmed = confirm(`Sẽ dồn ${safeGroups.length} nhóm folder trùng có cùng hash file số.\n\n${preview}${safeGroups.length > 12 ? '\n…' : ''}\n\nFolder cũ sẽ được giữ trong quarantine để rollback. Tiếp tục?`);
  if (!confirmed) return;
  const button = document.querySelector('button[onclick="scanAndMergeDuplicates()"]');
  if (button) { button.disabled = true; button.innerHTML = '<span class="spinner"></span> Đang dồn...'; }
  try {
    const res = await fetch('/api/aggregate-products/merge-duplicates', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({group_ids: safeGroups.map(group => group.group_id)})
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.detail || data.error || 'Dồn folder thất bại');
    const moved = (data.merged || []).reduce((sum, item) => sum + (item.moved_folders || []).length, 0);
    toast('success', `✅ Đã dồn ${moved} folder vào ${data.merged?.length || 0} folder chuẩn.`);
    await loadProducts();
    currentProductSource = 'aggregate';
    setProductSource('aggregate');
  } catch (e) {
    toast('error', `❌ ${e.message}`);
  } finally {
    if (button) { button.disabled = false; button.innerHTML = '🔎 Quét & dồn folder trùng'; }
  }
}

function renderShopProducts(listings) {
  const grid = document.getElementById('product-grid');
  if (!listings.length) {
    grid.innerHTML = '<div class="loading-state">Không có sản phẩm khớp trên Etsy Shop</div>';
    refreshScrollNavState();
    return;
  }
  grid.innerHTML = listings.map(listing => {
    const status = String(listing.managerStatus || 'unknown');
    // A remote listing becomes fully actionable only after an exact Etsy URL
    // match with a local workbook row.  Reuse the real local card so every
    // existing button keeps its own API handler and folder context.
    if (listing.localProduct) {
      return productCard({
        ...listing.localProduct,
        status: `🛍 Etsy ${status}`,
      });
    }
    return remoteEtsyProductCard(listing);
  }).join('');
  refreshScrollNavState();
}

function remoteEtsyProductCard(listing) {
    const status = String(listing.managerStatus || 'unknown');
    const syncableStatus = isSyncableEtsyStatus(status);
    const title = escHtml(String(listing.title || 'Chưa có tiêu đề'));
    const rawId = String(listing.id || '');
    const id = escHtml(rawId);
    const safeListingId = /^\d+$/.test(rawId) ? rawId : '';
    const etsyLink = resolveEtsyListingLink(listing);
    const safeUrl = escHtml(etsyLink.url);
    const actionLabel = etsyLink.kind === 'manager' ? '🛠 Mở listing editor' : '🔗 Mở Etsy';
    const action = etsyLink.url
      ? `<a class="btn btn-etsy btn-sm" href="${safeUrl}" target="_blank" rel="noopener">${actionLabel}</a>`
      : '<button class="btn btn-etsy btn-sm" disabled title="Không có link Etsy đã xác minh từ snapshot mới nhất">🔒 Link unavailable</button>';
    const disabledFileActions = '<button class="btn btn-ghost btn-sm" disabled title="Cần mapping với folder local">📁</button>'
      + '<button class="btn btn-ghost btn-sm" disabled title="Cần mapping với folder local">🖼</button>'
      + '<button class="btn btn-ghost btn-sm" disabled title="Cần mapping với folder local">📷</button>'
      + '<button class="btn btn-ghost btn-sm" disabled title="Cần mapping với folder local">✏️</button>';
    const disabledMarketingActions = '<button class="btn btn-img btn-sm" disabled title="Cần mapping với folder local">🎨 Gen</button>'
      + '<button class="btn btn-success btn-sm" disabled title="Cần mapping với folder local">🔄 Regen</button>'
      + '<button class="btn btn-ghost btn-sm" disabled title="Cần mapping với folder local">📢 Share</button>';
    const checkboxDisabled = !safeListingId || !syncableStatus;
    const mapAction = safeListingId && syncableStatus
      ? `<button class="btn btn-warning btn-sm product-primary-action" data-action-role="primary-next" data-action-scope="local" onclick="mapEtsyListingToLocal('${safeListingId}')" title="Bước tiếp theo: ghép listing này với folder local">🔗 Ghép local</button>`
      : `<button class="btn btn-warning btn-sm product-primary-action" data-action-role="primary-next" data-action-scope="local" disabled title="${safeListingId ? 'Chỉ cho phép ghép listing Active/Draft' : 'Listing không có ID hợp lệ'}">🔗 Ghép local</button>`;
    const deleteAction = status.toLowerCase() === 'draft' && safeListingId
      ? `<button class="btn btn-danger btn-sm live-etsy-write" data-action-scope="live-etsy" data-risk="write" onclick="deleteSingleEtsyDraft('${safeListingId}')" title="Xoá vĩnh viễn Etsy draft này">🗑</button>`
      : '<button class="btn btn-danger btn-sm" disabled title="Chỉ có thể xoá listing ở trạng thái draft">🗑</button>';
    const disabledEtsyActions = '<button class="btn btn-sync btn-sm live-etsy-read" data-action-scope="live-etsy" data-risk="read" disabled title="Ghép folder local trước khi Sync">🔄 Sync</button>'
      + '<button class="btn btn-primary btn-sm live-etsy-write" data-action-scope="live-etsy" data-risk="write" disabled title="Cần mapping với folder local">🚀 Post</button>'
      + deleteAction;
    return `<div class="product-card shop-product-card" id="shop-listing-card-${safeListingId}">
      <div style="padding: 0 10px; display: flex; align-items: center;"><input type="checkbox" class="product-cb shop-product-cb" value="${safeListingId}" data-listing-id="${safeListingId}" data-etsy-status="${escHtml(status.toLowerCase())}" data-etsy-syncable="${syncableStatus ? '1' : '0'}" onclick="updateBatchUI(event)" ${checkboxDisabled ? 'disabled' : ''} title="${checkboxDisabled ? 'Không cho phép chọn listing này (không thuộc phạm vi Active/Draft)' : 'Chọn listing Etsy'}"></div>
      <div class="gallery-wrap"><div class="shop-thumb">🛍</div></div>
      <div class="product-info">
        <div class="product-folder">Etsy ${id}</div>
        <div class="product-title" title="${title}">${title}</div>
        ${etsyLink.url ? `<a class="product-etsy-link" href="${safeUrl}" target="_blank" rel="noopener">${etsyLink.kind === 'manager' ? '🛠 Listing editor' : '🔗 Listing Etsy'} ${id}</a>` : `<span class="product-etsy-link missing" title="Không có link Etsy đã xác minh từ snapshot mới nhất">⚠️ Listing ${id} · link unavailable</span>`}
        <div class="product-meta"><span>🛍 Trên shop</span><span>📌 ${escHtml(status)}</span>${etsyLink.stale ? '<span style="color:var(--orange)">⚠ Snapshot stale</span>' : ''}<span style="color:var(--orange)">Chưa ghép folder local</span></div>
      </div>
      <div class="status-wrap"><div class="status-badge status-${escHtml(status)}">${escHtml(status)}</div></div>
      <div class="product-actions">
        <div class="action-group action-group-local" data-action-scope="local" aria-label="Điều khiển local"><span class="action-group-label">Local</span>${disabledFileActions}${mapAction}</div>
        <div class="action-group action-group-content" data-action-scope="content" aria-label="Điều khiển nội dung"><span class="action-group-label">Content</span>${disabledMarketingActions}</div>
        <div class="action-group action-group-live-etsy" data-action-scope="live-etsy" aria-label="Điều khiển Etsy live"><span class="action-group-label">Etsy live</span>${action}${disabledEtsyActions}</div>
      </div>
    </div>`;
}

async function bulkCreateLocalFromEtsy() {
  const checkboxes = [...document.querySelectorAll('.shop-product-cb:checked')].filter(cb =>
    cb.dataset.etsySyncable === '1' && /^\d+$/.test(String(cb.dataset.listingId || ''))
  );
  if (!checkboxes.length) return toast('warning', 'Chưa chọn listing Etsy nào đang ở trạng thái Active/Draft');
  if (!confirm(`Tạo ${checkboxes.length} product local mới và sync thông tin từ Etsy?\n\nMỗi listing sẽ tạo một folder product-* và một dòng trong Excel.`)) return;

  const button = document.getElementById('shop-bulk-create-btn');
  const originalText = button?.textContent || '➕ Tạo product mới + Sync';
  if (button) { button.disabled = true; button.innerHTML = '<span class="spinner"></span> Đang sync 0/' + checkboxes.length; }
  let created = 0;
  let fullySynced = 0;
  let incomplete = 0;
  let failed = 0;
  const errors = [];

  for (const [index, checkbox] of checkboxes.entries()) {
    const listingId = checkbox.dataset.listingId;
    const card = checkbox.closest('.product-card');
    const listing = (etsyManagerSnapshot?.listings || []).find(item => String(item.id || '') === listingId);
    if (card) card.classList.add('running');
    if (button) button.innerHTML = `<span class="spinner"></span> Đang sync ${index + 1}/${checkboxes.length}`;
    try {
      const res = await fetch('/api/etsy/create-local-listing', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({listing_id: listingId}),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.detail || data.error || 'Không tạo được product');
      created += 1;
      const assetsComplete = data?.sync_status?.assets_complete === true;
      if (assetsComplete) fullySynced += 1;
      else incomplete += 1;
      const imageStatus = data?.assets?.images_found || 0;
      const imageDownloaded = data?.assets?.images_downloaded || 0;
      const fileStatus = data?.assets?.files_found || 0;
      const fileDownloaded = data?.assets?.files_downloaded || 0;
      toast(assetsComplete ? 'success' : 'warning', assetsComplete
        ? `✅ Etsy ${listingId} → ${data.folder} đã tạo và sync đủ`
        : `⚠️ Etsy ${listingId} → ${data.folder} chưa sync đủ (` +
          `${imageDownloaded}/${imageStatus} ảnh, ${fileDownloaded}/${fileStatus} file). ` +
          `${data.sync_status?.image_warning || data.sync_error || 'hãy sync lại trong local'}`);
    } catch (error) {
      failed += 1;
      errors.push(`Etsy ${listingId}: ${error.message}`);
      toast('error', `❌ Etsy ${listingId}: ${error.message}`);
    } finally {
      if (card) card.classList.remove('running');
    }
  }

  document.querySelectorAll('.shop-product-cb:checked').forEach(cb => { cb.checked = false; });
  document.getElementById('cb-select-all').checked = false;
  updateBatchUI();
  await loadProducts();
  setProductSource('shop');
  if (failed || incomplete) {
    toast('warning', `Đã xử lý ${created}/${checkboxes.length}: sync đủ ${fullySynced}, chưa đủ asset ${incomplete}, lỗi ${failed}. ${errors[0] || ''}`);
  } else {
    toast('success', `✅ Đã tạo và sync đủ ${fullySynced} product mới từ Etsy`);
  }
  if (button) { button.disabled = false; button.textContent = originalText; }
}

async function registerLocalFolders(folders, button) {
  const targetFolders = [...new Set(
    (folders || [])
      .map(folder => String(folder || '').trim())
      .filter(folder => folder)
  )];

  if (!targetFolders.length) {
    toast('warning', 'Không có folder nào để đăng ký');
    return;
  }

  const activeShop = document.getElementById('shop-switcher')?.value?.trim() || '';
  if (!activeShop) {
    toast('error', 'Không đọc được shop hiện tại');
    return;
  }

  const registerButton = button;
  const originalText = registerButton?.innerHTML || '♻️ Đăng ký local';
  if (registerButton) {
    registerButton.disabled = true;
    registerButton.innerHTML = '<span class="spinner"></span> Đang đăng ký...';
  }

  try {
    const res = await fetch('/api/local-products/register', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({shop: activeShop, folders: targetFolders}),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data?.detail || data?.error || 'Không thể đăng ký local folder');

    await loadProducts({ throwOnError: true });
    toast('success', `✅ Đã đăng ký ${data.rows?.length || 0}/${targetFolders.length} folder vào catalog.`);
  } catch (error) {
    toast('error', `❌ ${error.message}`);
  } finally {
    if (registerButton) {
      registerButton.disabled = false;
      registerButton.innerHTML = originalText;
    }
  }
}

async function registerSingleLocalFolder(folder, button) {
  return registerLocalFolders([folder], button);
}

function selectedEtsyDraftIds() {
  return BatchSelection.selectedDraftListingIds(document.querySelectorAll('.shop-product-cb:checked'));
}

async function bulkDeleteSelectedEtsyDrafts(explicitIds = null) {
  const selectedShop = selectedBatchCheckboxes('shop');
  const listingIds = explicitIds || selectedEtsyDraftIds();
  if (!listingIds.length) return toast('warning', 'Chưa chọn Etsy draft nào để xoá');
  if (!explicitIds && listingIds.length !== selectedShop.length) {
    return toast('error', 'Lựa chọn có listing không phải draft. Chỉ Etsy draft mới được xoá.');
  }
  const uniqueIds = [...new Set(listingIds)];
  const message = `Xoá vĩnh viễn ${uniqueIds.length} Etsy draft đã chọn?\n\nListing IDs: ${uniqueIds.join(', ')}\n\nThao tác này không thể hoàn tác.`;
  if (!confirm(message)) return;
  const button = document.getElementById('shop-bulk-delete-drafts-btn');
  if (button) button.disabled = true;
  try {
    const response = await fetch('/api/etsy/delete-drafts', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({listing_ids: uniqueIds}),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.detail || data.error || 'Không xoá được Etsy draft');
    toast('success', `✅ Đã xoá ${data.deleted} Etsy draft: ${data.deleted_listing_ids.join(', ')}`);
    await loadProducts({throwOnError: true});
    setProductSource('shop');
  } catch (error) {
    toast('error', `❌ ${error.message}`);
  } finally {
    if (button) button.disabled = false;
  }
}

function deleteSingleEtsyDraft(listingId) {
  return bulkDeleteSelectedEtsyDrafts([String(listingId)]);
}

async function mapEtsyListingToLocal(listingId) {
  const safeListingId = String(listingId || '').trim();
  const listing = (etsyManagerSnapshot?.listings || []).find(item => String(item.id || '') === safeListingId);
  if (!isSyncableEtsyStatus(listing?.managerStatus || listing?.status)) {
    const listingStatus = (listing?.managerStatus || listing?.status || 'không hợp lệ').toString();
    toast('warning', `Chỉ ghép listing tại trạng thái Active/Draft. Hiện tại: ${listingStatus}`);
    return;
  }
  document.getElementById('map-listing-id').value = safeListingId;
  document.getElementById('map-listing-label').innerHTML = listing
    ? `<strong>Etsy ${escHtml(String(listingId))}</strong><span>${escHtml(listing.title || 'Chưa có tiêu đề')}</span><small>Trạng thái: ${escHtml(listing.managerStatus || 'unknown')}</small>`
    : `<strong>Etsy ${escHtml(String(listingId))}</strong>`;
  document.getElementById('map-local-folder').value = '';
  document.getElementById('map-local-folder-options').innerHTML = allProducts
    .map(product => `<option value="${escHtml(product.folder)}">${escHtml(product.title || '')}</option>`)
    .join('');
  document.getElementById('map-scan-status').textContent = '⏳ Đang quét tiêu đề và dữ liệu local...';
  document.getElementById('map-local-suggestions').innerHTML = '';
  openModal('map-local-modal');
  document.getElementById('map-local-folder').focus();
  try {
    const res = await fetch(`/api/etsy/match-suggestions/${encodeURIComponent(listingId)}?limit=5`);
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.detail || data.error || 'Không quét được sản phẩm local');
    const remote = data.listing || {};
    const remoteUrl = String(remote.url || '');
    document.getElementById('map-listing-label').innerHTML = `
      <strong>Etsy ${escHtml(String(remote.id || listingId))}</strong>
      <span>${escHtml(remote.title || 'Chưa có tiêu đề')}</span>
      <small>Trạng thái: ${escHtml(remote.status || 'unknown')}${remoteUrl ? ` · <a href="${escHtml(remoteUrl)}" target="_blank" rel="noopener">Mở Etsy</a>` : ''}</small>`;
    const suggestions = data.suggestions || [];
    document.getElementById('map-local-suggestions').innerHTML = suggestions.length
      ? suggestions.map((candidate, index) => {
          const percent = Math.round(Number(candidate.score || 0) * 100);
          const confidenceText = candidate.confidence === 'high' ? 'Khớp cao' : candidate.confidence === 'medium' ? 'Cần xem lại' : 'Khớp thấp';
          const thumb = candidate.thumb
            ? `<img src="${escHtml(candidate.thumb)}" alt="${escHtml(candidate.folder)}">`
            : '<div class="map-suggestion-placeholder">📦</div>';
          return `<button type="button" class="map-suggestion-card confidence-${escHtml(candidate.confidence)}" data-folder="${escHtml(candidate.folder)}" onclick="chooseEtsyMapSuggestion(this.dataset.folder)">
            ${thumb}
            <span class="map-suggestion-copy">
              <strong>${index === 0 ? '⭐ ' : ''}${escHtml(candidate.folder)}</strong>
              <span>${escHtml(candidate.title || '[Cần SEO]')}</span>
              <small>${confidenceText} · ${percent}% · 🖼 ${candidate.image_count || 0} · 📎 ${candidate.pdf_count || 0} file · $${Number(candidate.price || 0).toFixed(2)}</small>
            </span>
          </button>`;
        }).join('')
      : '<div class="hint">Không tìm thấy ứng viên local.</div>';
    if (data.auto_fill_folder) {
      document.getElementById('map-local-folder').value = data.auto_fill_folder;
      document.getElementById('map-scan-status').textContent = `✅ Đã quét ${data.scanned_local_total} sản phẩm. Đã điền sẵn ứng viên độ khớp cao; anh kiểm tra trước khi ghép.`;
    } else {
      document.getElementById('map-scan-status').textContent = `🔎 Đã quét ${data.scanned_local_total} sản phẩm. Chưa có ứng viên đủ tin cậy để tự điền; hãy chọn một gợi ý bên dưới.`;
    }
  } catch (e) {
    document.getElementById('map-scan-status').textContent = `❌ ${e.message}`;
  }
}

function chooseEtsyMapSuggestion(folder) {
  document.getElementById('map-local-folder').value = folder;
  document.querySelectorAll('.map-suggestion-card').forEach(card => {
    card.classList.toggle('selected', card.dataset.folder === folder);
  });
}

function extractEtsyListingIdInput(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  if (/^\d+$/.test(raw)) return raw;
  const match = raw.match(/\/listing\/(\d+)/) || raw.match(/listing-editor\/edit\/(\d+)/) || raw.match(/listing_id=(\d+)/);
  return match ? match[1] : '';
}

async function openLinkEtsyFromLocal(row) {
  const product = allProducts.find(item => Number(item.row) === Number(row));
  if (!product) return toast('error', 'Không tìm thấy sản phẩm local');
  if (String(product.etsy_url || '').trim()) {
    return toast('info', `${product.folder} đã có link Etsy`);
  }

  document.getElementById('link-local-row').value = String(product.row);
  document.getElementById('link-local-folder').value = product.folder;
  document.getElementById('link-local-label').innerHTML = `
    <strong>${escHtml(product.folder)}</strong>
    <span>${escHtml(product.title || '[Cần SEO]')}</span>
    <small>${escHtml(product.status || '')}</small>`;
  document.getElementById('link-etsy-input').value = '';
  document.getElementById('link-etsy-suggestions').innerHTML = '';
  document.getElementById('link-etsy-scan-status').textContent = '⏳ Đang gợi ý listing Etsy chưa ghép...';
  openModal('link-etsy-modal');
  document.getElementById('link-etsy-input').focus();

  try {
    const res = await fetch(`/api/etsy/link-suggestions-for-folder/${encodeURIComponent(product.folder)}?limit=5`);
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.detail || data.error || 'Không lấy được gợi ý Etsy');
    const suggestions = data.suggestions || [];
    document.getElementById('link-etsy-suggestions').innerHTML = suggestions.length
      ? suggestions.map((candidate, index) => {
          const percent = Math.round(Number(candidate.score || 0) * 100);
          const confidenceText = candidate.confidence === 'high' ? 'Khớp cao' : candidate.confidence === 'medium' ? 'Cần xem lại' : 'Khớp thấp';
          return `<button type="button" class="map-suggestion-card confidence-${escHtml(candidate.confidence)}" data-listing-id="${escHtml(candidate.id)}" onclick="chooseLinkEtsySuggestion(this.dataset.listingId)">
            <div class="map-suggestion-placeholder">🛍</div>
            <span class="map-suggestion-copy">
              <strong>${index === 0 ? '⭐ ' : ''}Etsy ${escHtml(candidate.id)}</strong>
              <span>${escHtml(candidate.title || 'Chưa có tiêu đề')}</span>
              <small>${confidenceText} · ${percent}% · ${escHtml(candidate.status || 'unknown')}</small>
            </span>
          </button>`;
        }).join('')
      : '<div class="hint">Không có listing Active/Draft chưa ghép khớp tiêu đề. Có thể dán Listing ID hoặc URL bên dưới.</div>';
    if (data.auto_fill_listing_id) {
      document.getElementById('link-etsy-input').value = data.auto_fill_listing_id;
      document.getElementById('link-etsy-scan-status').textContent =
        `✅ Đã quét ${data.scanned_etsy_total} listing chưa ghép. Đã điền sẵn ứng viên độ khớp cao; anh kiểm tra trước khi ghép.`;
    } else if (!(data.snapshot_total > 0)) {
      document.getElementById('link-etsy-scan-status').textContent =
        '⚠️ Chưa có bản đồng bộ Etsy Shop. Bấm “Đồng bộ Etsy Shop” hoặc dán Listing ID/URL thủ công.';
    } else {
      document.getElementById('link-etsy-scan-status').textContent =
        `🔎 Đã quét ${data.scanned_etsy_total} listing chưa ghép. Chưa có ứng viên đủ tin cậy; hãy chọn gợi ý hoặc dán ID/URL.`;
    }
  } catch (e) {
    document.getElementById('link-etsy-scan-status').textContent = `❌ ${e.message}`;
  }
}

function chooseLinkEtsySuggestion(listingId) {
  document.getElementById('link-etsy-input').value = String(listingId || '');
  document.querySelectorAll('#link-etsy-suggestions .map-suggestion-card').forEach(card => {
    card.classList.toggle('selected', card.dataset.listingId === String(listingId || ''));
  });
}

async function submitLinkEtsyFromLocal() {
  const folder = document.getElementById('link-local-folder').value.trim();
  const row = Number(document.getElementById('link-local-row').value);
  const rawInput = document.getElementById('link-etsy-input').value.trim();
  const listingId = extractEtsyListingIdInput(rawInput);
  if (!folder) return toast('warning', 'Thiếu folder local');
  if (!listingId) return toast('warning', 'Nhập Listing ID hoặc URL Etsy hợp lệ');

  const submit = document.getElementById('link-etsy-submit');
  const originalText = submit.textContent;
  submit.disabled = true;
  submit.textContent = 'Đang ghép...';
  toast('info', `🔗 Đang ghép ${folder} với Etsy ${listingId}...`);
  try {
    const payload = {
      folder,
      listing_id: listingId,
      allow_manual: true,
    };
    if (/etsy\.com/i.test(rawInput)) payload.etsy_url = rawInput;
    const res = await fetch('/api/etsy/map-listing', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.detail || data.error || 'Không ghép được link Etsy');

    // The mapping endpoint persists the URL, but the rendered card gates Sync/Update
    // on `etsy_listing_id`. Reload canonical product data so the card receives the
    // derived listing ID plus its current Etsy-manager metadata immediately.
    await loadProducts({ throwOnError: true });
    closeModal('link-etsy-modal');
    toast('success', `✅ Đã ghép ${folder} → Etsy ${listingId}. Sync/Update đã sẵn sàng.`);
    updateStats(allProducts);
  } catch (e) {
    toast('error', `❌ ${e.message}`);
  } finally {
    submit.disabled = false;
    submit.textContent = originalText;
  }
}

async function submitEtsyListingMap() {
  const listingId = document.getElementById('map-listing-id').value.trim();
  const folder = document.getElementById('map-local-folder').value.trim();
  if (!folder) {
    toast('warning', 'Nhập folder local, ví dụ product-123');
    return;
  }
  const listing = (etsyManagerSnapshot?.listings || []).find(item => String(item.id || '') === listingId);
  if (!isSyncableEtsyStatus(listing?.managerStatus || listing?.status)) {
    const listingStatus = (listing?.managerStatus || listing?.status || 'không hợp lệ').toString();
    toast('warning', `Không thể ghép listing. Chỉ cho phép Active/Draft. Hiện tại: ${listingStatus}`);
    return;
  }
  const submit = document.getElementById('map-local-submit');
  submit.disabled = true;
  toast('info', `🔗 Đang ghép Etsy ${listingId} với ${folder}...`);
  try {
    const res = await fetch('/api/etsy/map-listing', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({listing_id: listingId, folder}),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.detail || data.error || 'Không ghép được listing');
    toast('success', `✅ Đã ghép Etsy ${listingId} → ${data.folder}. Nút Sync đã sẵn sàng.`);
    closeModal('map-local-modal');
    await loadProducts();
    setProductSource('shop');
  } catch (e) {
    toast('error', `❌ ${e.message}`);
  } finally {
    submit.disabled = false;
  }
}

async function createNewLocalFromEtsy() {
  const listingId = document.getElementById('map-listing-id').value.trim();
  if (!listingId) return;
  const listing = (etsyManagerSnapshot?.listings || []).find(item => String(item.id || '') === listingId);
  if (!isSyncableEtsyStatus(listing?.managerStatus || listing?.status)) {
    const listingStatus = (listing?.managerStatus || listing?.status || 'không hợp lệ').toString();
    toast('warning', `Không thể tạo folder local cho listing này vì trạng thái không phải Active/Draft. Hiện tại: ${listingStatus}`);
    return;
  }
  const button = document.getElementById('map-create-new-submit');
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = '⏳ Đang tạo và sync...';
  toast('info', `📁 Đang tạo product mới cho Etsy ${listingId}...`);
  try {
    const res = await fetch('/api/etsy/create-local-listing', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({listing_id: listingId}),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.detail || data.error || 'Không tạo được product mới');
    const assetsComplete = data?.sync_status?.assets_complete === true;
    const imageStatus = data?.assets?.images_found || 0;
    const imageDownloaded = data?.assets?.images_downloaded || 0;
    const fileStatus = data?.assets?.files_found || 0;
    const fileDownloaded = data?.assets?.files_downloaded || 0;
    if (assetsComplete) {
      toast('success', `✅ Đã tạo ${data.folder} và sync đủ thông tin Etsy về.`);
    } else {
      toast('warning', `⚠️ Đã tạo ${data.folder}, chưa sync đủ thông tin: ` +
        `${imageDownloaded}/${imageStatus} ảnh, ${fileDownloaded}/${fileStatus} file. ` +
        `${data.sync_error || data.sync_status?.image_warning || 'hãy bấm Sync lại'}`);
    }
    closeModal('map-local-modal');
    await loadProducts();
    setProductSource('shop');
  } catch (e) {
    toast('error', `❌ ${e.message}`);
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

function renderProducts(products) {
  const grid = document.getElementById('product-grid');
  if (!products.length) {
    grid.innerHTML = '<div class="loading-state">Không có sản phẩm nào</div>';
    renderCatalogPagination(0);
    refreshScrollNavState();
    return;
  }
  catalogViewState.items = products;
  catalogViewState.visibleCount = Math.min(
    catalogViewState.visibleCount > 0 ? catalogViewState.visibleCount : catalogViewState.pageSize,
    products.length,
  );
  grid.innerHTML = products.slice(0, catalogViewState.visibleCount).map(p => productCard(p)).join('');
  restoreCatalogSelections();
  renderCatalogPagination(products.length);
  updateBatchUI();
  refreshScrollNavState();
}

function catalogSelectionKey(element) {
  if (!element) return '';
  const identity = element.dataset.folder || element.dataset.listingId || element.value || '';
  return `${currentProductSource}:${identity}`;
}

function restoreCatalogSelections() {
  document.querySelectorAll('.product-cb').forEach(checkbox => {
    checkbox.checked = selectedCatalogIds.has(catalogSelectionKey(checkbox));
  });
}

function rememberCatalogSelections() {
  document.querySelectorAll('.product-cb').forEach(checkbox => {
    const key = catalogSelectionKey(checkbox);
    if (!key) return;
    if (checkbox.checked) selectedCatalogIds.add(key);
    else selectedCatalogIds.delete(key);
  });
}

function renderCatalogPagination(total) {
  const pagination = document.getElementById('catalog-pagination');
  if (!pagination) return;
  const visible = Math.min(catalogViewState.visibleCount || 0, total);
  if (!total || visible >= total) {
    pagination.hidden = true;
    pagination.innerHTML = '';
    return;
  }
  pagination.hidden = false;
  pagination.innerHTML = `<span>Hiển thị ${visible}/${total} kết quả phù hợp · chỉ card đang hiển thị mới tạo DOM</span>
    <button class="btn btn-ghost btn-sm" type="button" onclick="loadMoreCatalog()">Tải thêm ${Math.min(catalogViewState.pageSize, total - visible)}</button>`;
}

function loadMoreCatalog() {
  if (!catalogViewState.items.length) return;
  catalogViewState.visibleCount = Math.min(
    catalogViewState.items.length,
    (catalogViewState.visibleCount || catalogViewState.pageSize) + catalogViewState.pageSize,
  );
  renderProducts(catalogViewState.items);
}

function productNeedsEtsyLink(p) {
  const etsyLink = productEtsyLink(p);
  if (etsyLink.url) return false;
  const hasMapping = Boolean(String(p?.etsy_listing_id || p?.etsy_url || '').trim());
  if (hasMapping) return true;
  const status = String(p?.status || '');
  return status.includes('URL chưa xác minh')
    || (status.includes('Đã đăng') && status.toLowerCase().includes('draft'));
}

const SOCIAL_CHANNEL_META = {
  instagram: {icon: '📸', label: 'Instagram'},
  pinterest: {icon: '📌', label: 'Pinterest'},
  facebook: {icon: '👥', label: 'Facebook'},
  twitter: {icon: '𝕏', label: 'X'},
  medium: {icon: '✍️', label: 'Medium'},
  reddit: {icon: '👽', label: 'Reddit'}
};

function socialStatusDateLabel(timestamp) {
  if (!timestamp) return 'không rõ thời gian';
  const parsed = new Date(timestamp);
  if (Number.isNaN(parsed.getTime())) return 'không rõ thời gian';
  return parsed.toLocaleString('vi-VN');
}

function safeExternalSocialUrl(rawUrl) {
  const value = String(rawUrl || '').trim();
  if (!value) return '';
  const looksLikeHttp = /^https?:\/\//i.test(value);
  if (!looksLikeHttp) return '';
  try {
    if (typeof URL === 'undefined') {
      return value;
    }
    const parsed = new URL(value);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return '';
    return parsed.href;
  } catch {
    return '';
  }
}

function normalizeSocialStatusSummary(statuses) {
  return Object.entries(statuses || {})
    .filter(([platform, record]) => SOCIAL_CHANNEL_META[platform])
    .map(([platform, record]) => ({
      platform,
      record: record || {},
      meta: SOCIAL_CHANNEL_META[platform],
    }))
    .sort((a, b) => a.meta.label.localeCompare(b.meta.label));
}

function renderSocialTabsStatus() {
  const summary = _socialStatuses || {};
  Object.keys(SOCIAL_CHANNEL_META).forEach(platform => {
    const btn = document.getElementById(`tab-${platform}`);
    if (!btn) return;
    const meta = SOCIAL_CHANNEL_META[platform];
    const record = summary[platform];
    const hasPosted = record && record.status === 'posted';
    const suffix = hasPosted ? ' ✅' : '';
    btn.textContent = `${meta.icon} ${meta.label}${suffix}`;
    btn.classList.toggle('is-posted', Boolean(hasPosted));
  });
}

function renderSocialChannelBadges(statuses) {
  const entries = Object.entries(statuses || {})
    .filter(([platform, record]) => SOCIAL_CHANNEL_META[platform] && record?.status === 'posted');
  if (!entries.length) return '';
  return `<div class="product-social-statuses" aria-label="Các kênh social đã đăng">${
    entries.map(([platform, record]) => {
      const meta = SOCIAL_CHANNEL_META[platform];
      const title = `${meta.label} · ${record.posted_at || 'đã đăng'}`;
      const content = `${meta.icon} ${meta.label}`;
      const safeUrl = safeExternalSocialUrl(record.url);
      return safeUrl
        ? `<a class="social-channel-badge" href="${escHtml(safeUrl)}" target="_blank" rel="noopener" title="${escHtml(title)}">${content}</a>`
        : `<span class="social-channel-badge" title="${escHtml(title)}">${content}</span>`;
    }).join('')
  }</div>`;
}

function productCard(p) {
  const isRunning = runningSet.has(p.folder);
  const etsyLink = productEtsyLink(p);
  const etsyUrl = etsyLink.url;
  const etsyId = etsyLink.listingId;
  const needsEtsyLink = productNeedsEtsyLink(p);
  const urlUnverified = needsEtsyLink || (Boolean(p.etsy_listing_id || p.etsy_url) && !etsyUrl);

  let statusClass = isRunning ? 'running'
    : (p.needs_seo || p.status.includes('⚠') || urlUnverified) ? 'warning'
    : p.status.includes('Đã đăng') ? 'posted'
    : (p.status.includes('Lỗi') || p.status.includes('❌')) ? 'error'
    : 'pending';

  let badgeLabel = p.status;
  let errorReason = '';
  if (p.status.includes('❌') || p.status.includes('Lỗi')) {
    badgeLabel = '❌ Lỗi';
    if (p.status.includes('❌ Lỗi:')) {
      errorReason = p.status.split('❌ Lỗi:')[1].trim();
    } else if (p.status.includes('❌ Lỗi')) {
      errorReason = p.status.split('❌ Lỗi')[1].replace(/^[:\s]*/, '').trim();
    } else if (p.status.includes('❌')) {
      errorReason = p.status.split('❌')[1].trim();
    } else if (p.status.includes('Lỗi:')) {
      errorReason = p.status.split('Lỗi:')[1].trim();
    } else {
      errorReason = p.status.replace(/^❌\s*/, '').replace(/^Lỗi[:\s]*/, '').trim();
    }
  } else if (urlUnverified) {
    badgeLabel = String(p.status || '').toLowerCase().includes('draft')
      ? '⚠ Draft · chưa có link'
      : '⚠ Etsy link chưa xác minh';
    statusClass = isRunning ? 'running' : 'warning';
  } else if (p.status.includes('Đã đăng')) {
    if (p.status.includes('draft')) {
      badgeLabel = '✅ Đã đăng draft';
    } else {
      badgeLabel = '✅ Đã đăng';
    }
  } else if (p.status.includes('Chờ đăng')) {
    badgeLabel = '⏳ Chờ đăng';
  }
  const statusLabel = isRunning ? '⚡ Đang chạy...' : badgeLabel;
  const cardClass   = isRunning ? 'product-card running' : 'product-card';
  const primaryAction = p.needs_seo ? 'seo'
    : (!etsyId || String(p.status || '').includes('Chờ đăng')) ? 'post'
    : 'update';

  const galleryHtml = productImageGallery(p.image_previews || p.all_images, p.folder);

  const etsyLinkHtml = etsyUrl
    ? `<a class="product-etsy-link" href="${escHtml(etsyUrl)}" target="_blank" rel="noopener" title="${etsyLink.kind === 'manager' ? `Mở Shop Manager cho Etsy ${etsyId} · trạng thái ${p.etsy_manager_status}` : etsyLink.kind === 'fallback' ? `Mở link Etsy từ Excel ${etsyId} · chưa có snapshot Manager khớp` : `Mở listing Etsy ${etsyId}`}">${etsyLink.kind === 'manager' ? '🛠 Shop Manager' : '🔗 Etsy'} ${etsyId}${etsyLink.stale ? ' · snapshot stale' : ''}</a>`
    : `<span class="product-etsy-link missing" title="Không có link Etsy đã xác minh từ snapshot mới nhất">⚠️ ${etsyId ? `Etsy ${etsyId} · link unavailable` : `Chưa có link Etsy${needsEtsyLink ? ' · cần ghép' : ''}`}</span>`;
  const etsyButtonHtml = etsyUrl
    ? `<button class="btn btn-etsy btn-sm live-etsy-read" data-action-scope="live-etsy" data-risk="read" onclick="openEtsyListing('${escJs(etsyUrl)}')" title="${etsyLink.kind === 'manager' ? `Mở Shop Manager cho Etsy ${etsyId}` : etsyLink.kind === 'fallback' ? `Mở link Etsy từ Excel ${etsyId}` : 'Mở listing Etsy trực tiếp'}">${etsyLink.kind === 'manager' ? '🛠 Manager' : '🔗 Etsy'}</button>`
    : etsyId
      ? `<button class="btn btn-ghost btn-sm live-etsy-read" data-action-scope="live-etsy" data-risk="read" disabled title="Không có link Etsy đã xác minh từ snapshot mới nhất">🔒 Unavailable</button>`
      : `<button class="btn btn-warning btn-sm" onclick="openLinkEtsyFromLocal(${p.row})" title="Ghép listing ID/URL Etsy cho sản phẩm này">🔗 Ghép link</button>`;

  let seoBadges = '';
  if (p.missing_fields && p.missing_fields.length > 0) {
    seoBadges = '<div class="seo-badges" style="display:flex; gap:6px; margin-top:8px; flex-wrap:wrap;">';
    if (p.missing_fields.includes('title')) {
      seoBadges += '<span style="background:rgba(239,68,68,0.1); border:1px solid rgba(239,68,68,0.3); color:#ef4444; font-size:10px; padding:2px 6px; border-radius:4px; font-weight:600; display:inline-flex; align-items:center; gap:3px;">⚠️ Thiếu Title</span>';
    }
    if (p.missing_fields.includes('description')) {
      seoBadges += '<span style="background:rgba(245,158,11,0.1); border:1px solid rgba(245,158,11,0.3); color:#f59e0b; font-size:10px; padding:2px 6px; border-radius:4px; font-weight:600; display:inline-flex; align-items:center; gap:3px;">⚠️ Thiếu Mô tả</span>';
    }
    if (p.missing_fields.includes('tags')) {
      seoBadges += '<span style="background:rgba(59,130,246,0.1); border:1px solid rgba(59,130,246,0.3); color:#3b82f6; font-size:10px; padding:2px 6px; border-radius:4px; font-weight:600; display:inline-flex; align-items:center; gap:3px;">⚠️ Thiếu Tags</span>';
    } else if (p.missing_fields.includes('tags_count')) {
      seoBadges += '<span style="background:rgba(16,185,129,0.1); border:1px solid rgba(16,185,129,0.3); color:#10b981; font-size:10px; padding:2px 6px; border-radius:4px; font-weight:600; display:inline-flex; align-items:center; gap:3px;">⚠️ Thiếu Tags (<13)</span>';
    }
    seoBadges += '</div>';
  }

  return `
  <div class="${cardClass}" id="card-${p.row}">
    <div style="padding: 0 10px; display: flex; align-items: center;">
      <input type="checkbox" class="product-cb" value="${p.row}" data-folder="${p.folder}" onclick="updateBatchUI(event)">
    </div>
    <div class="gallery-wrap">${galleryHtml}</div>
      <div class="product-info">
      <div class="product-folder" style="display:flex; justify-content:space-between; align-items:center;">
        <span>${p.folder}</span>
        <span style="font-size:10px; background:rgba(255,255,255,0.06); border:1px solid var(--border); padding:1px 6px; border-radius:10px; color:var(--text3); font-family:monospace; font-weight:normal;" title="Mã SKU của sản phẩm này">${p.sku || ""}</span>
      </div>
      <div class="product-title" title="${escHtml(p.title)}">${escHtml(p.title)}</div>
      ${etsyLinkHtml}
      <div class="product-meta">
        <span>🖼 ${p.image_count} ảnh</span>
        <span>📎 ${p.pdf_count} file</span>
        <span>💲 $${Number(p.price).toFixed(2)}</span>
        ${p.has_planner ? '<span style="color:var(--accent2)">🗂 Planner ✓</span>' : ''}
      </div>
      ${renderCloudAssetUi(p.folder)}
      ${seoBadges}
      ${renderSocialChannelBadges(p.social_statuses)}
    </div>
    <div class="status-wrap" id="status-wrap-${p.row}">
      <div class="status-badge status-${statusClass}" onclick="toggleStatusMenu(${p.row})" title="Click để thay đổi trạng thái" style="cursor:pointer">
        ${statusLabel} <span style="opacity:0.6;font-size:10px">▾</span>
      </div>
      ${errorReason ? `
        <div class="error-reason" style="font-size:11px; color:var(--red); margin-top:6px; line-height:1.3; max-width:180px; word-break:break-word; text-align:center; font-weight:500;" title="${escHtml(errorReason)}">
          ⚠️ ${escHtml(errorReason)}
        </div>
      ` : ''}
      <div class="status-menu" id="smenu-${p.row}" style="display:none">
        <div class="smenu-item" onclick="changeStatus(${p.row},'${p.folder}','✅ Đã đăng')">✅ Đã đăng</div>
        <div class="smenu-item" onclick="changeStatus(${p.row},'${p.folder}','✅ Đã đăng draft')">✅ Đã đăng draft</div>
        <div class="smenu-item pending" onclick="changeStatus(${p.row},'${p.folder}','⏳ Chờ đăng')">⏳ Chờ đăng</div>
        <div class="smenu-item error" onclick="changeStatus(${p.row},'${p.folder}','❌ Lỗi')">❌ Lỗi</div>
      </div>
    </div>
    <div class="product-actions">
      <!-- Nhóm 1: Thư mục & Edit -->
      <div class="action-group action-group-local" data-action-scope="local" aria-label="Điều khiển local-only">
        <span class="action-group-label">Local</span>
        <button class="btn btn-ghost btn-sm" onclick="openFolder(${p.row}, 'files')" title="Mở folder files/">📁</button>
        <button class="btn btn-ghost btn-sm" onclick="openFolder(${p.row}, 'images')" title="Mở folder images/">🖼</button>
        <button class="btn btn-ghost btn-sm" onclick="openImageModal(${p.row}, '${p.folder}')" title="Quản lý ảnh">📷</button>
        <button class="btn btn-ghost btn-sm" onclick="openEditModal(${p.row})" title="Chỉnh sửa Excel">✏️</button>
      </div>

      <!-- Nhóm 2: AI & Marketing -->
      <div class="action-group action-group-content" data-action-scope="content" aria-label="Điều khiển nội dung">
        <span class="action-group-label">Content</span>
        ${p.needs_seo ? `<button class="btn btn-warning btn-sm product-primary-action" data-action-role="primary-next" data-action-scope="local" onclick="quickSEO(${p.row},'${p.folder}')" title="Bước tiếp theo: tạo nhanh SEO">🤖 SEO</button>` : ''}
        <button class="btn btn-img btn-sm" onclick="openGenModal(${p.row}, '${p.folder}')" title="Generate 10 listing images với AI">🎨 Gen</button>
        <button class="btn btn-success btn-sm" onclick="regenImages(${p.row}, '${p.folder}')" ${isRunning ? 'disabled' : ''} title="Tạo lại ảnh listing">🔄 Regen</button>
        <button class="btn btn-ghost btn-sm" onclick="openSocialModal(${p.row}, '${p.folder}')" title="Quản lý & Chia sẻ Social">📢 Share</button>
      </div>

      <!-- Nhóm 3: Etsy Actions -->
      <div class="action-group action-group-live-etsy" data-action-scope="live-etsy" aria-label="Điều khiển Etsy live">
        <span class="action-group-label">Etsy live</span>
        ${etsyButtonHtml}
        ${etsyId ? `<button class="btn btn-sync btn-sm live-etsy-read" data-action-scope="live-etsy" data-risk="read" onclick="syncListingFromEtsy(${p.row}, '${p.folder}')" ${isRunning ? 'disabled' : ''} title="Đồng bộ thông tin từ Etsy về Dashboard">🔄 Sync</button>` : ''}
        ${etsyId ? `<button class="btn btn-update btn-sm live-etsy-write ${primaryAction === 'update' ? 'product-primary-action' : ''}" data-action-role="${primaryAction === 'update' ? 'primary-next' : 'live-write'}" data-action-scope="live-etsy" data-risk="write" onclick="openEtsyUpdateModal(${p.row})" ${isRunning ? 'disabled' : ''} title="${primaryAction === 'update' ? 'Bước tiếp theo: review và cập nhật dữ liệu Local lên listing Etsy' : 'Cập nhật dữ liệu Local lên listing Etsy'}">⬆ Update</button>` : ''}
        <button class="btn btn-primary btn-sm live-etsy-write ${primaryAction === 'post' ? 'product-primary-action' : ''}" data-action-role="${primaryAction === 'post' ? 'primary-next' : 'live-write'}" data-action-scope="live-etsy" data-risk="write" onclick="postProduct(${p.row}, '${p.folder}')" ${isRunning ? 'disabled' : ''} title="${primaryAction === 'post' ? 'Bước tiếp theo: đăng sản phẩm lên Etsy' : 'Đăng lên Etsy'}">🚀 Post</button>
        <button class="btn btn-danger btn-sm live-etsy-write" data-action-scope="live-etsy" data-risk="write" onclick="deleteProduct(${p.row}, '${p.folder}')" title="Xoá sản phẩm local và/hoặc listing Etsy theo quy trình hiện tại">🗑</button>
      </div>
    </div>
  </div>`;
}

function productImageGallery(images, folder) {
  const imageItems = normalizeImageGalleryItems(images);

  if (!imageItems.length) return '<div class="thumb-placeholder">📦</div>';

  const safeFolder = escHtml(folder || 'sản phẩm');
  const controls = imageItems.length > 3 ? `
    <button class="gallery-nav gallery-nav-prev" type="button" onclick="slideProductGallery(this, -1)" aria-label="Xem ảnh trước" disabled>‹</button>
    <button class="gallery-nav gallery-nav-next" type="button" onclick="slideProductGallery(this, 1)" aria-label="Xem ảnh tiếp theo">›</button>` : '';
  const firstRenderableImageIndex = imageItems.findIndex((item) => !item.isHydrationRequired);
  const thumbnails = imageItems.map((item, index) => {
    const position = index + 1;
    const safeFullUrl = escHtml(item.fullUrl || item.url);
    if (item.isHydrationRequired) {
      return `<button class="product-image-placeholder" type="button" data-full-url="${safeFullUrl}" aria-label="Ảnh ${position} trên ${imageItems.length} của ${safeFolder} (dataless, cần tải trước)" data-folder="${safeFolder}" data-image-index="${index}" role="button" tabindex="0" onclick="openProductLightbox(this)" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();openProductLightbox(this)}"><span class="placeholder-icon" aria-hidden="true">☁️</span><span class="placeholder-text">Cần tải</span></button>`;
    }

    const safeUrl = escHtml(item.url);
    const isFirstRenderable = index === firstRenderableImageIndex;
    return `<img src="${safeUrl}" alt="Ảnh ${position} trên ${imageItems.length} của ${safeFolder}" decoding="async" loading="${isFirstRenderable ? 'eager' : 'lazy'}"${isFirstRenderable ? ' fetchpriority="high"' : ''} data-full-url="${safeFullUrl}" data-folder="${safeFolder}" data-image-index="${index}" role="button" tabindex="0" onclick="openProductLightbox(this)" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();openProductLightbox(this)}">`;
  }).join('');

  return `<div class="product-gallery" role="region" aria-label="Thư viện ảnh của ${safeFolder}">
    <div class="product-gallery-track" onscroll="syncProductGalleryControls(this.closest('.product-gallery'))">${thumbnails}</div>
    ${controls}
  </div>`;
}

function normalizeImageGalleryItems(images) {
  const list = Array.isArray(images) ? images : [];
  const normalized = list
    .map((item) => {
      if (typeof item === 'string') {
        const url = item.trim();
        return {
          url,
          name: '',
          fullUrl: url,
          isHydrationRequired: false,
          isSafeImage: /^https?:\/\//i.test(url) || url.startsWith('/'),
        };
      }
      if (!item || typeof item !== 'object') return null;
      const url = String(item.url || '').trim();
      const fullUrl = String(item.full_url || item.url || '').trim();
      return {
        url,
        name: String(item.name || '').trim(),
        fullUrl,
        isHydrationRequired: item.availability === 'hydration_required',
        isSafeImage: /^https?:\/\//i.test(url) || url.startsWith('/'),
      };
    })
    .filter((item) => item?.isSafeImage && item.url)
    .filter(Boolean);

  const seen = new Set();
  return normalized.filter((item) => {
    if (seen.has(item.url)) return false;
    seen.add(item.url);
    return true;
  });
}

function slideProductGallery(button, direction) {
  const gallery = button.closest('.product-gallery');
  const track = gallery?.querySelector('.product-gallery-track');
  if (!track) return;
  track.scrollBy({ left: direction * 90, behavior: 'smooth' });
  window.setTimeout(() => syncProductGalleryControls(gallery), 250);
}

function syncProductGalleryControls(gallery) {
  const track = gallery?.querySelector('.product-gallery-track');
  if (!track) return;
  const previous = gallery.querySelector('.gallery-nav-prev');
  const next = gallery.querySelector('.gallery-nav-next');
  const maxScroll = Math.max(0, track.scrollWidth - track.clientWidth);
  if (previous) previous.disabled = track.scrollLeft <= 1;
  if (next) next.disabled = track.scrollLeft >= maxScroll - 1;
}

function openEtsyListing(url) {
  if (!url) return toast('warning', 'Sản phẩm này chưa có link Etsy');
  window.open(url, '_blank', 'noopener');
}


async function syncListingFromEtsy(row, folder) {
  if (etsySingleSyncInFlight) {
    toast('warning', '⚠️ Đang có một lượt Sync Etsy. Hãy chờ hoàn tất.');
    return;
  }
  const syncButtons = Array.from(document.querySelectorAll('.btn-sync')).filter(button => button && !button.disabled);
  const buttonsToRestore = syncButtons.map(button => ({ button }));
  buttonsToRestore.forEach(({button}) => { button.disabled = true; });

  toast('info', `🗓️ Đang thêm Sync ${folder} vào hàng chờ...`);
  const card = document.getElementById(`card-${row}`);
  const originalOpacity = card ? card.style.opacity : '1';
  if (card) card.style.opacity = '0.5';
  etsySingleSyncInFlight = true;
  try {
    const product = allProducts.find(item => Number(item.row) === Number(row));
    const listingId = String(product?.etsy_url || '').match(/\/listing\/(\d+)/)?.[1] || '';
    const activeShop = document.getElementById('shop-switcher')?.value?.trim() || '';
    if (!product || String(product.folder || '') !== String(folder || '') || !listingId || !activeShop) {
      throw new Error('Mapping row/folder/listing/shop không còn hợp lệ. Hãy tải lại dashboard.');
    }
    const res = await fetch(`/api/products/${row}/sync-from-etsy`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({shop: activeShop, folder, listing_id: listingId}),
    });
    const data = await res.json();
    if (data.ok && data.queued) {
      const position = Number(data.command?.position || 0);
      toast('success', `🗓️ Đã thêm Sync ${folder}${position ? ` vào vị trí #${position}` : ''}. Theo dõi Live Logs để biết khi chạy xong.`);
    } else if (data.ok) {
      const assets = data.assets || {};
      const imageText = `${assets.images_downloaded || 0}/${assets.images_found || 0} ảnh`;
      const fileText = `${assets.files_downloaded || 0}/${assets.files_found || 0} file`;
      if (data.sync_status?.assets_complete) {
        toast('success', `✅ Đồng bộ ${folder}: ${imageText}, ${fileText}.`);
      } else {
        const warnings = [];
        if (assets.image_warning) warnings.push(assets.image_warning);
        if (assets.file_warning) warnings.push(assets.file_warning);
        toast('warning', `⚠️ Đồng bộ chưa đủ ${folder}: ${imageText}, ${fileText}. ${warnings.join(' ') || ''}`);
      }
      await loadProducts();
    } else if (data.code === 'etsy_sync_busy') {
      toast('warning', `${data.error || `Shop ${activeShop} đang có một lượt Sync Etsy khác. Hãy chờ hoàn tất.`}`);
    } else {
      toast('error', `❌ Thất bại: ${data.error || 'Lỗi không xác định'}`);
    }
  } catch (e) {
    toast('error', `❌ Lỗi: ${e.message}`);
  } finally {
    buttonsToRestore.forEach(({button}) => { button.disabled = false; });
    if (card) card.style.opacity = originalOpacity;
    etsySingleSyncInFlight = false;
  }
}


// ── Actions ────────────────────────────────────────────────────────────────────
async function postProduct(row, folder) {
  if (!confirm(`Re-post "${folder}" lên Etsy?\n(Sẽ reset status → Chờ đăng rồi chạy Chrome tự điền)`)) return;
  runningSet.add(folder);
  refreshCard(row, folder);
  try {
    const res  = await fetch(`/api/products/${row}/post`, { method: 'POST' });
    const data = await res.json();
    if (!data.ok) { toast('error', data.error || 'Lỗi kết nối'); runningSet.delete(folder); }
    else toast('info', `🗓️ ${data.created === false ? 'Post đã có trong hàng chờ' : 'Đã thêm Post vào hàng chờ'}: ${folder}`);
  } catch (e) {
    toast('error', `Lỗi: ${e.message}`);
    runningSet.delete(folder);
  }
  refreshCard(row, folder);
}

let etsyUpdateTarget = null;

function resetEtsyUpdateFieldRows() {
  const fields = [
    ['title', 'Title'], ['description', 'Mô tả'], ['tags', 'Tags'],
    ['price', 'Giá'], ['qty', 'Số lượng'], ['images', 'Ảnh listing'],
    ['files', 'File tải xuống'],
  ];
  document.querySelector('.etsy-update-fields').innerHTML = fields.map(([value, label]) =>
    `<label><input type="checkbox" class="etsy-update-field" value="${value}" ${['images', 'files'].includes(value) ? '' : 'checked'}> <span>${label}</span></label>`
  ).join('');
}

function formatComparisonBytes(value) {
  if (value === null || value === undefined) return 'chưa đọc được dung lượng';
  const bytes = Number(value) || 0;
  if (bytes < 1000) return `${bytes} B`;
  if (bytes < 1000 ** 2) return `${(bytes / 1000).toFixed(1)} KB`;
  if (bytes < 1000 ** 3) return `${(bytes / 1000 ** 2).toFixed(2)} MB`;
  return `${(bytes / 1000 ** 3).toFixed(2)} GB`;
}

function comparisonPreview(value, maxLength = 105) {
  const text = String(value || '').replace(/\s+/g, ' ').trim();
  if (!text) return '— Trống —';
  return text.length > maxLength ? `${text.slice(0, maxLength)}…` : text;
}

function comparisonAssetItems(items) {
  if (!Array.isArray(items) || !items.length) return '';
  const visible = items.slice(0, 3).map(item =>
    `${escHtml(item.name || 'file')} (${formatComparisonBytes(item.size_bytes)})`
  );
  if (items.length > 3) visible.push(`+${items.length - 3} file khác`);
  return `<small>${visible.join('<br>')}</small>`;
}

function comparisonCell(field, data, source) {
  const value = data || {};
  if (field === 'title') return `<strong>${value.chars || 0} ký tự</strong><span>${escHtml(comparisonPreview(value.value))}</span>`;
  if (field === 'description') return `<strong>${value.chars || 0} ký tự</strong><span>${escHtml(comparisonPreview(value.value, 135))}</span>`;
  if (field === 'tags') return `<strong>${value.count || 0} tags</strong><span>${escHtml(comparisonPreview(value.value))}</span>`;
  if (field === 'price') return `<strong>$${Number(value.value || 0).toFixed(2)}</strong>`;
  if (field === 'qty') return `<strong>${Number(value.value || 0)} sản phẩm</strong>`;
  if (field === 'images') {
    const size = source === 'local' ? ` · ${formatComparisonBytes(value.total_bytes)}` : '';
    const limitNote = source === 'local' && Number(value.count || 0) > 10 ? ' · Etsy sẽ dùng 10 ảnh đầu' : '';
    return `<strong>${value.count || 0} ảnh${size}${limitNote}</strong>${source === 'local' ? comparisonAssetItems(value.items) : ''}`;
  }
  if (field === 'files') {
    const known = value.total_bytes === null && value.known_size_count
      ? ` · đọc được ${value.known_size_count}/${value.count || 0} dung lượng`
      : '';
    return `<strong>${value.count || 0} file · ${formatComparisonBytes(value.total_bytes)}${known}</strong>${comparisonAssetItems(value.items)}`;
  }
  return `<span>${escHtml(comparisonPreview(value.value))}</span>`;
}

function comparisonValuesMatch(field, localValue, etsyValue) {
  if (field === 'images') return Number(localValue?.count || 0) === Number(etsyValue?.count || 0);
  if (field === 'files') {
    const countMatches = Number(localValue?.count || 0) === Number(etsyValue?.count || 0);
    const sizesKnown = etsyValue?.total_bytes !== null && etsyValue?.total_bytes !== undefined;
    const localBytes = Number(localValue?.total_bytes || 0);
    const etsyBytes = Number(etsyValue?.total_bytes || 0);
    const tolerance = Math.max(50_000, etsyBytes * 0.01);
    return countMatches && (!sizesKnown || Math.abs(localBytes - etsyBytes) <= tolerance);
  }
  return String(localValue?.value ?? '').trim() === String(etsyValue?.value ?? '').trim();
}

function renderEtsyUpdateComparison(data) {
  const definitions = [
    ['title', 'Title'], ['description', 'Mô tả'], ['tags', 'Tags'],
    ['price', 'Giá'], ['qty', 'Số lượng'], ['images', 'Ảnh listing'],
    ['files', 'File tải xuống'],
  ];
  const container = document.querySelector('.etsy-update-fields');
  container.innerHTML = definitions.map(([field, label]) => {
    const localValue = data.local?.[field] || {};
    const etsyValue = data.etsy?.[field] || {};
    const same = comparisonValuesMatch(field, localValue, etsyValue);
    const checked = same ? '' : 'checked';
    return `<label class="etsy-compare-row ${same ? 'is-same' : 'is-different'}">
      <input type="checkbox" class="etsy-update-field" value="${field}" ${checked}>
      <div class="etsy-compare-content">
        <div class="etsy-compare-heading"><strong>${label}</strong><span>${same ? '✓ Đang khớp' : '≠ Khác nhau'}</span></div>
        <div class="etsy-compare-columns">
          <div><small>LOCAL</small>${comparisonCell(field, localValue, 'local')}</div>
          <div><small>ETSY</small>${comparisonCell(field, etsyValue, 'etsy')}</div>
        </div>
      </div>
    </label>`;
  }).join('');
}

async function loadEtsyUpdateComparison(row) {
  const status = document.getElementById('etsy-update-compare-status');
  const submitButton = document.getElementById('etsy-update-submit');
  status.className = 'etsy-update-compare-status is-loading';
  status.innerHTML = '<span class="spinner"></span> Đang mở Etsy và đọc dữ liệu để so sánh...';
  submitButton.disabled = true;
  try {
    const response = await fetch(`/api/products/${row}/etsy-comparison`);
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.detail || data.error || 'Không đọc được dữ liệu Etsy');
    if (!etsyUpdateTarget || Number(etsyUpdateTarget.row) !== Number(row)) return;
    renderEtsyUpdateComparison(data);
    status.className = 'etsy-update-compare-status is-ready';
    status.textContent = `✓ Đã so sánh Local với Etsy ${data.listing_id}. Chọn đúng dòng anh muốn cập nhật.`;
    submitButton.disabled = false;
  } catch (error) {
    status.className = 'etsy-update-compare-status is-error';
    status.innerHTML = `❌ ${escHtml(error.message)} <button class="btn btn-ghost btn-sm" onclick="loadEtsyUpdateComparison(${Number(row)})">Thử lại</button>`;
  }
}

function openEtsyUpdateModal(row) {
  const product = allProducts.find(item => Number(item.row) === Number(row));
  if (!product) return toast('error', 'Không tìm thấy sản phẩm local');

  const etsyUrl = String(product.etsy_url || '').trim();
  const listingMatch = etsyUrl.match(/\/listing\/(\d+)/);
  if (!listingMatch) return toast('warning', 'Sản phẩm chưa có Etsy listing ID hợp lệ');

  etsyUpdateTarget = {
    row: product.row,
    folder: product.folder,
    listingId: listingMatch[1],
    shop: document.getElementById('shop-switcher')?.value?.trim() || '',
  };
  document.getElementById('etsy-update-product').textContent = product.folder;
  document.getElementById('etsy-update-listing').textContent = `Etsy ${listingMatch[1]}`;
  resetEtsyUpdateFieldRows();
  const runStatus = document.getElementById('etsy-update-run-status');
  runStatus.className = 'etsy-update-run-status';
  runStatus.textContent = '';
  const submitButton = document.getElementById('etsy-update-submit');
  submitButton.disabled = true;
  submitButton.innerHTML = '⬆ Cập nhật lên Etsy';
  const closeButton = document.getElementById('etsy-update-cancel');
  if (closeButton) closeButton.textContent = 'Huỷ';
  openModal('etsy-update-modal');
  loadEtsyUpdateComparison(product.row);
}

async function pollEtsyUpdateJob(jobId, target, options = {}) {
  const statusBox = document.getElementById('etsy-update-run-status');
  const intervalMs = Number(options.intervalMs ?? 1500);
  const maxAttempts = Number(options.maxAttempts ?? 800);
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, intervalMs));
    try {
      const response = await fetch(`/api/etsy/update-status?job_id=${encodeURIComponent(jobId)}`);
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.detail || data.error || 'Không đọc được trạng thái');
      const logTail = (data.logs || []).slice(-3).map(line => escHtml(line)).join('<br>');
      statusBox.className = `etsy-update-run-status is-${data.status}`;
      statusBox.innerHTML = `<strong>${data.status === 'success' ? '✅ Hoàn tất' : data.status === 'error' ? '❌ Cập nhật lỗi' : '⏳ Đang cập nhật'}</strong><div>${logTail || escHtml(data.last_message || '')}</div>`;
      if (data.status === 'success') {
        runningSet.delete(target.folder);
        const button = document.getElementById('etsy-update-submit');
        button.disabled = true;
        button.innerHTML = '✅ Đã cập nhật';
        const closeButton = document.getElementById('etsy-update-cancel');
        if (closeButton) closeButton.textContent = 'Đóng';
        toast('success', `✅ Đã cập nhật ${target.folder} lên Etsy ${target.listingId}`);
        await loadProducts();
        return;
      }
      if (data.status === 'error') {
        runningSet.delete(target.folder);
        const button = document.getElementById('etsy-update-submit');
        button.disabled = false;
        button.innerHTML = '↻ Thử cập nhật lại';
        toast('error', `❌ Cập nhật Etsy thất bại: ${data.last_message || 'Xem log chi tiết'}`);
        return;
      }
      if (data.status === 'cancelled') {
        runningSet.delete(target.folder);
        const button = document.getElementById('etsy-update-submit');
        button.disabled = false;
        button.innerHTML = '↻ Thử cập nhật lại';
        toast('warning', `⚠️ Cập nhật Etsy đã bị huỷ: ${target.folder}`);
        return;
      }
    } catch (error) {
      runningSet.delete(target.folder);
      statusBox.className = 'etsy-update-run-status is-error';
      statusBox.textContent = `❌ ${error.message}`;
      const button = document.getElementById('etsy-update-submit');
      button.disabled = false;
      button.innerHTML = '↻ Thử cập nhật lại';
      return;
    }
  }
  runningSet.delete(target.folder);
  statusBox.className = 'etsy-update-run-status is-error';
  statusBox.textContent = '❌ Hết thời gian chờ trạng thái cập nhật Etsy';
  const button = document.getElementById('etsy-update-submit');
  button.disabled = false;
  button.innerHTML = '↻ Thử cập nhật lại';
}

async function submitEtsyUpdate() {
  if (!etsyUpdateTarget) return toast('error', 'Chưa chọn sản phẩm cần cập nhật');
  const fields = [...document.querySelectorAll('.etsy-update-field:checked')]
    .map(input => input.value);
  if (!fields.length) return toast('warning', 'Anh chọn ít nhất một nội dung cần cập nhật nhé');

  const fieldLabels = fields.map(field => ({
    title: 'Title', description: 'Mô tả', tags: 'Tags', price: 'Giá',
    qty: 'Số lượng', images: 'Ảnh', files: 'File số',
  })[field] || field);
  if (!confirm(
    `Cập nhật ${etsyUpdateTarget.folder} lên Etsy ${etsyUpdateTarget.listingId}?\n\n` +
    `Nội dung: ${fieldLabels.join(', ')}\n\nDữ liệu trên Etsy sẽ được thay bằng dữ liệu Local đã chọn.`
  )) return;

  const target = {...etsyUpdateTarget};
  const button = document.getElementById('etsy-update-submit');
  const statusBox = document.getElementById('etsy-update-run-status');
  button.disabled = true;
  button.innerHTML = '<span class="spinner"></span> Đang khởi động...';
  statusBox.className = 'etsy-update-run-status is-starting';
  statusBox.textContent = '⏳ Đang khởi động Chrome đúng shop...';
  runningSet.add(target.folder);

  try {
    const response = await fetch(`/api/products/${target.row}/push-to-etsy`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        fields,
        shop: target.shop,
        folder: target.folder,
        listing_id: target.listingId,
      }),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.detail || data.error || 'Không thể bắt đầu cập nhật');
    toast('info', `⬆ Đang cập nhật ${target.folder} lên Etsy ${data.listing_id}`);
    button.innerHTML = '<span class="spinner"></span> Đang cập nhật...';
    pollEtsyUpdateJob(data.job_id, target);
  } catch (error) {
    runningSet.delete(target.folder);
    button.disabled = false;
    button.innerHTML = '⬆ Cập nhật lên Etsy';
    toast('error', `❌ ${error.message}`);
  }
  refreshCard(target.row, target.folder);
}

function selectedLocalEtsyMappings() {
  const shop = document.getElementById('shop-switcher')?.value?.trim() || '';
  if (!shop) throw new Error('Không đọc được shop hiện tại');
  const selected = selectedBatchCheckboxes('local');
  if (!selected.length) throw new Error('Chưa chọn sản phẩm local nào');

  const seenRows = new Set();
  const seenFolders = new Set();
  const items = [];
  const rejected = [];
  selected.forEach((checkbox) => {
    const row = Number.parseInt(checkbox.value, 10);
    const folder = String(checkbox.dataset.folder || '').trim();
    const product = allProducts.find(item => Number(item.row) === row);
    const listingId = String(product?.etsy_url || '').match(/\/listing\/(\d+)/)?.[1] || '';
    if (!Number.isInteger(row) || row < 4 || !/^product-\d+$/.test(folder)) {
      rejected.push({row, folder: folder || `row-${checkbox.value}`, error: 'Checkbox local không hợp lệ (row/folder)'});
      return;
    }
    if (!product || String(product.folder || '').trim() !== folder) {
      rejected.push({row, folder, error: `Row ${row} không còn khớp folder ${folder}`});
      return;
    }
    if (!listingId) {
      rejected.push({row, folder, error: 'Chưa ghép Etsy listing ID hợp lệ'});
      return;
    }
    if (seenRows.has(row) || seenFolders.has(folder)) {
      rejected.push({row, folder, listingId, error: `Lựa chọn bị lặp: row ${row} / ${folder}`});
      return;
    }
    seenRows.add(row);
    seenFolders.add(folder);
    items.push({row, folder, listingId});
  });
  if (!items.length) {
    throw new Error(`Không có sản phẩm đã ghép Etsy hợp lệ. ${batchFailureSummary(rejected)}`);
  }
  return {shop, items, rejected, selectedCount: selected.length};
}

function disableEtsyBatchActionButtons() {
  const buttons = Array.from(document.querySelectorAll('.local-batch-action, .btn-sync, .btn-update'));
  const enabled = [...new Set(buttons)].filter(button => !button.disabled);
  enabled.forEach(button => { button.disabled = true; });
  return () => enabled.forEach(button => { button.disabled = false; });
}

function restoreFailedBatchSelection(items) {
  const failedKeys = new Set(items.map(item => `${item.row}\u0000${item.folder}`));
  document.querySelectorAll('.product-cb').forEach((checkbox) => {
    const key = `${Number.parseInt(checkbox.value, 10)}\u0000${String(checkbox.dataset.folder || '').trim()}`;
    checkbox.checked = failedKeys.has(key);
  });
  const selectAll = document.getElementById('cb-select-all');
  if (selectAll) selectAll.checked = false;
  updateBatchUI();
}

function batchFailureSummary(failures) {
  const buckets = { mapping: [], request: [], notAttempted: [], queued: [], other: [] };
  failures.forEach(item => {
    const kind = item?.issueKind || 'request';
    if (!Object.prototype.hasOwnProperty.call(buckets, kind)) buckets.other.push(item);
    else buckets[kind].push(item);
  });

  const labels = [];
  if (buckets.mapping.length) labels.push(`${buckets.mapping.length} mapping`);
  if (buckets.request.length) labels.push(`${buckets.request.length} request`);
  if (buckets.notAttempted.length) labels.push(`${buckets.notAttempted.length} chưa thử`);
  if (buckets.queued.length) labels.push(`${buckets.queued.length} queued`);
  if (buckets.other.length) labels.push(`${buckets.other.length} other`);

  const issueSummary = labels.length ? `Theo nhóm: ${labels.join(' · ')}` : '';

  const examples = [];
  const allSorted = [
    ...buckets.mapping.slice(0, 1),
    ...buckets.request.slice(0, 1),
    ...buckets.notAttempted.slice(0, 1),
    ...buckets.queued.slice(0, 1),
    ...buckets.other.slice(0, 1),
  ]
    .filter(Boolean);
  examples.push(...allSorted.map(item => `${item.folder}: ${item.error}`));

  const extra = examples.length ? ` · ${examples.join(' · ')}` : '';
  return `${issueSummary}${extra}`;
}

function getEtsyBulkSyncProgressElements() {
  return {
    panel: document.getElementById('etsy-bulk-sync-panel'),
    meta: document.getElementById('etsy-bulk-sync-meta'),
    progressBar: document.getElementById('etsy-bulk-sync-progress-bar'),
    progressText: document.getElementById('etsy-bulk-sync-progress-text'),
    currentItem: document.getElementById('etsy-bulk-sync-current'),
    summary: document.getElementById('etsy-bulk-sync-summary'),
  };
}

function formatEtsyBulkSyncProgressLine({
  processed,
  selectedCount,
  success,
  requestFailed,
  notAttempted,
  queued,
  skippedMapping,
}) {
  const pieces = [
    `${processed}/${selectedCount} đã xử lý`,
    `${success} thành công`,
  ];
  if (queued) pieces.push(`${queued} đã xếp hàng`);
  if (notAttempted) pieces.push(`${notAttempted} chưa thử`);
  pieces.push(`${requestFailed} lỗi request`);
  pieces.push(`${skippedMapping} mapping`);
  return pieces.join(' · ');
}

function updateEtsyBulkSyncProgress({
  totalValid = 0,
  processed = 0,
  success = 0,
  requestFailed = 0,
  notAttempted = 0,
  queued = 0,
  skippedMapping = 0,
  currentFolder = '',
  currentListingId = '',
  statusText = '',
  summary = '',
  panelState = '',
  backendQueued = false,
  inFlight = false,
}) {
  const refs = getEtsyBulkSyncProgressElements();
  const {panel, meta, progressBar, progressText, currentItem, summary: summaryNode} = refs;
  if (!panel) return;

  const clampedTotal = Number.isFinite(totalValid) && totalValid > 0 ? totalValid : 0;
  const clampedProcessed = Math.max(0, processed);
  const pct = clampedTotal
    ? Math.max(0, Math.min(100, Math.round((Math.min(clampedProcessed, clampedTotal) / clampedTotal) * 100)))
    : 0;

  panel.classList.remove('is-running', 'is-success', 'is-error');
  panel.classList.remove('hidden');
  if (panelState) panel.classList.add(panelState);
  panel.setAttribute('aria-busy', inFlight ? 'true' : 'false');

  if (meta) {
    meta.textContent = formatEtsyBulkSyncProgressLine({
      processed: clampedProcessed,
      selectedCount: clampedTotal,
      success,
      notAttempted,
      requestFailed,
      queued,
      skippedMapping,
    });
  }

  if (progressBar) {
    progressBar.style.width = `${pct}%`;
    progressBar.setAttribute('aria-valuenow', String(pct));
    progressBar.setAttribute('aria-valuetext', `${pct}%`);
  }

  if (progressText) {
    progressText.textContent = statusText || 'Đang chờ cập nhật...';
  }

  if (currentItem) {
    if (!inFlight && !currentFolder && !currentListingId) {
      currentItem.textContent = backendQueued
        ? 'Backend queue đang xử lý các listing đã xếp hàng'
        : 'Không còn listing đang xử lý';
    } else if (currentFolder || currentListingId) {
      const folderText = currentFolder || 'chưa chọn folder';
      const listingText = currentListingId || 'chưa chọn listing';
      currentItem.textContent = `Đang xử lý: ${folderText} (${listingText})`;
    } else {
      currentItem.textContent = 'Đang chờ xử lý...';
    }
  }

  if (summaryNode) summaryNode.textContent = summary;
}

async function batchSyncFromEtsy() {
  if (etsyBulkSyncInFlight || etsySingleSyncInFlight) {
    toast('warning', '⚠️ Đang có một lượt đồng bộ Etsy. Hãy chờ hoàn tất.');
    return;
  }

  let selection;
  try {
    selection = selectedLocalEtsyMappings();
  } catch (error) {
    toast('error', `❌ ${error.message}`);
    return;
  }
  if (!confirm(
    `Kéo ${selection.items.length} listing hợp lệ từ Etsy về đúng folder Local của shop ${selection.shop}?\n` +
    `${selection.rejected.length ? `Bỏ qua ${selection.rejected.length}/${selection.selectedCount} sản phẩm chưa ghép hoặc mapping lỗi.\n` : ''}\n` +
    'Dashboard sẽ kiểm tra mapping và xử lý tuần tự bằng một Chrome.'
  )) return;

  etsyBulkSyncInFlight = true;
  etsySingleSyncInFlight = true;
  const restoreButtons = disableEtsyBatchActionButtons();
  const button = document.getElementById('local-batch-pull-btn');
  const originalText = button?.innerHTML || '⬇ Etsy → Local';
  const selectedCount = selection.selectedCount;
  const failures = selection.rejected.map(item => ({...item, issueKind: 'mapping'}));
  const failed = new Set();
  const skipped = new Set(selection.rejected.map(item => `${item.row}\u0000${item.folder}`));
  const queued = new Set();
  let requestFailedCount = 0;
  let queuedCount = 0;
  let notAttemptedCount = 0;
  let skippedMappingCount = selection.rejected.length;
  let isAborted = false;
  let abortReason = '';
  let abortErrorLabel = '';
  let completed = 0;
  let processed = selection.rejected.length;
  let activeIndex = 0;
  const markPendingAsSkipped = (items, errorMessage, issueKind = 'request', options = {}) => {
    const {countAsProcessed = false} = options;
    if (!items.length) return;
    items.forEach(pendingItem => {
      const pendingKey = `${pendingItem.row}\u0000${pendingItem.folder}`;
      failures.push({
        ...pendingItem,
        issueKind,
        error: errorMessage,
      });
      skipped.add(pendingKey);
      failed.delete(pendingKey);
      if (issueKind === 'request') {
        requestFailedCount += 1;
      } else if (issueKind === 'notAttempted') {
        notAttemptedCount += 1;
      } else if (issueKind === 'mapping') {
        skippedMappingCount += 1;
      } else if (issueKind === 'queued') {
        queuedCount += 1;
        queued.add(pendingKey);
      }
    });
    if (countAsProcessed) {
      processed += items.length;
    }
  };
  const hasQueuedItems = () => queuedCount > 0;
  updateEtsyBulkSyncProgress({
    totalValid: selectedCount,
    processed,
    success: completed,
    requestFailed: requestFailedCount,
    notAttempted: notAttemptedCount,
    queued: queuedCount,
    skippedMapping: skippedMappingCount,
    statusText: `Bắt đầu đồng bộ Etsy → Local cho ${selectedCount} listing`,
    summary: `Shop: ${selection.shop}`,
    panelState: 'is-running',
    inFlight: true,
  });

  try {
    for (let index = 0; index < selection.items.length; index += 1) {
      activeIndex = index;
      const item = selection.items[index];
      const currentShop = document.getElementById('shop-switcher')?.value?.trim() || '';
      const itemKey = `${item.row}\u0000${item.folder}`;
      if (currentShop !== selection.shop) {
        const pending = selection.items.slice(index);
        markPendingAsSkipped(
          pending,
          `Shop đã đổi từ ${selection.shop} sang ${currentShop || '[trống]'}`,
          'notAttempted',
          {countAsProcessed: false},
        );
        updateEtsyBulkSyncProgress({
          totalValid: selectedCount,
          processed,
          success: completed,
          requestFailed: requestFailedCount,
          notAttempted: notAttemptedCount,
          queued: queuedCount,
          skippedMapping: skippedMappingCount,
          statusText: `Tạm dừng do đổi shop: ${selection.shop} → ${currentShop || '[trống]'}`,
          summary: `Shop: ${selection.shop}`,
          panelState: 'is-error',
          inFlight: false,
        });
        break;
      }
      updateEtsyBulkSyncProgress({
        totalValid: selectedCount,
        processed,
        success: completed,
        requestFailed: requestFailedCount,
        notAttempted: notAttemptedCount,
        queued: queuedCount,
        skippedMapping: skippedMappingCount,
        currentFolder: item.folder,
        currentListingId: item.listingId,
        statusText: `Đang xử lý: ${item.folder} (listing ${item.listingId})`,
        summary: `Shop: ${selection.shop}`,
        panelState: 'is-running',
        inFlight: true,
      });
      if (button) button.innerHTML = `<span class="spinner"></span> ${index + 1}/${selection.items.length} ${escHtml(item.folder)}`;
      toast('info', `⬇ Etsy → Local ${index + 1}/${selection.items.length}: ${item.folder}`);
      let response;
      let data;
      try {
        response = await fetch(`/api/products/${item.row}/sync-from-etsy`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            shop: selection.shop,
            folder: item.folder,
            listing_id: item.listingId,
          }),
        });
      } catch (error) {
        isAborted = true;
        abortReason = `Không tiếp tục vì lỗi kết nối khi gọi API đồng bộ: ${error.message || 'Fetch bị hủy'}`;
        abortErrorLabel = '⚠️ Etsy → Local gặp lỗi mạng trước khi gọi API. Hãy kiểm tra kết nối rồi thử lại.';
        markPendingAsSkipped(selection.items.slice(index), abortReason, 'notAttempted', {countAsProcessed: false});
        break;
      }

      try {
        data = await response.json();
      } catch (error) {
        isAborted = true;
        if (response.status === 403) {
          abortReason = `Phản hồi bảo mật không đọc được (HTTP 403, index ${index + 1}/${selection.items.length}).`;
          abortErrorLabel = '⚠️ Etsy → Local bị chặn bởi middleware bảo mật (HTTP 403). Hãy reload dashboard, kiểm tra host/origin/token rồi thử lại.';
        } else {
          abortReason = `Không tiếp tục vì phản hồi không đọc được từ API đồng bộ: ${error.message || 'JSON parse failed'}`;
          abortErrorLabel = `❌ Phản hồi không hợp lệ tại ${item.folder} (${error.message || 'json failed'})`;
        }
        markPendingAsSkipped(selection.items.slice(index), abortReason, 'notAttempted', {countAsProcessed: false});
        break;
      }

      if (response.status === 403) {
        isAborted = true;
        const securityDetail = data?.detail || data?.error || 'Security middleware blocked this request';
        abortReason = `Vui lòng reload dashboard, kiểm tra phiên làm việc/host-orig/token rồi thử lại (${securityDetail}).`;
        abortErrorLabel = '⚠️ Etsy → Local bị chặn bởi middleware bảo mật (HTTP 403).';
        markPendingAsSkipped(selection.items.slice(index), securityDetail, 'notAttempted', {countAsProcessed: false});
        break;
      }

      if (response.status === 409 && data?.code === 'etsy_sync_busy') {
        isAborted = true;
        abortReason = 'etsy_sync_busy';
        abortErrorLabel = '⚠️ Etsy đang bận, đồng bộ tuần tự Etsy → Local đã bị khóa';
        markPendingAsSkipped(selection.items.slice(index), `etsy_sync_busy tại ${item.folder}`, 'notAttempted', {countAsProcessed: false});
        break;
      }

      const isQueued = response.status === 202 || data?.queued === true;
      if (isQueued) {
        processed += 1;
        queued.add(itemKey);
        skipped.add(itemKey);
        queuedCount += 1;
        activeIndex = index + 1;
        failures.push({
          ...item,
          issueKind: 'queued',
          error: `Đã xếp hàng (HTTP ${response.status})`,
        });
        updateEtsyBulkSyncProgress({
          totalValid: selectedCount,
          processed,
          success: completed,
          requestFailed: requestFailedCount,
          notAttempted: notAttemptedCount,
          queued: queuedCount,
          skippedMapping: skippedMappingCount,
          currentFolder: item.folder,
          currentListingId: item.listingId,
          statusText: `🟢 Đã xếp hàng: ${item.folder} (listing ${item.listingId})`,
          summary: `Shop: ${selection.shop}`,
          panelState: 'is-running',
          inFlight: true,
        });
        continue;
      }

      if (!response.ok || !data.ok || data.sync_ok === false) {
        failed.add(itemKey);
        skipped.delete(itemKey);
        requestFailedCount += 1;
        processed += 1;
        activeIndex = index + 1;
        const failReason = data.error || data.detail || 'Sync không hoàn tất';
        failures.push({...item, issueKind: 'request', error: failReason});
        updateEtsyBulkSyncProgress({
          totalValid: selectedCount,
          processed,
          success: completed,
          requestFailed: requestFailedCount,
          notAttempted: notAttemptedCount,
          queued: queuedCount,
          skippedMapping: skippedMappingCount,
          currentFolder: item.folder,
          currentListingId: item.listingId,
          statusText: `❌ Lỗi request: ${item.folder} (${failReason})`,
          summary: `Shop: ${selection.shop}`,
          panelState: 'is-running',
          inFlight: true,
        });
        continue;
      }

      completed += 1;
      skipped.delete(itemKey);
      processed += 1;
      activeIndex = index + 1;
      updateEtsyBulkSyncProgress({
        totalValid: selectedCount,
        processed,
        success: completed,
        requestFailed: requestFailedCount,
        notAttempted: notAttemptedCount,
        queued: queuedCount,
        skippedMapping: skippedMappingCount,
        currentFolder: item.folder,
        currentListingId: item.listingId,
        statusText: `✅ Hoàn tất: ${item.folder} (listing ${item.listingId})`,
        summary: `Shop: ${selection.shop}`,
        panelState: 'is-running',
        inFlight: true,
      });
    }

    if (completed > 0 && !hasQueuedItems()) {
      await loadProducts({throwOnError: true});
    }
    restoreFailedBatchSelection(failures);
    if (failures.length) {
      const finalMessage = isAborted
        ? `${abortErrorLabel || '❌ Etsy → Local: bị dừng giữa chừng.'} ${abortReason ? ` (${abortReason}).` : ''} ${batchFailureSummary(failures)}`
        : `⚠️ Etsy → Local: ${completed} thành công, ${requestFailedCount} lỗi request, ${notAttemptedCount} chưa thử, ${queuedCount} đã xếp hàng, ${skippedMappingCount} bỏ qua mapping. ${batchFailureSummary(failures)}`;
      updateEtsyBulkSyncProgress({
        totalValid: selectedCount,
        processed,
        success: completed,
        requestFailed: requestFailedCount,
        notAttempted: notAttemptedCount,
        queued: queuedCount,
        skippedMapping: skippedMappingCount,
        statusText: finalMessage,
        summary: batchFailureSummary(failures),
        panelState: isAborted ? 'is-error' : (hasQueuedItems() ? 'is-running' : 'is-error'),
        backendQueued: hasQueuedItems(),
        inFlight: false,
      });
      toast('warning', finalMessage);
    } else {
      const successMessage = `✅ Đã sync tuần tự ${completed}/${selectedCount} sản phẩm từ Etsy về Local`;
      updateEtsyBulkSyncProgress({
        totalValid: selectedCount,
        processed,
        success: completed,
        requestFailed: requestFailedCount,
        notAttempted: notAttemptedCount,
        queued: queuedCount,
        skippedMapping: skippedMappingCount,
        statusText: successMessage,
        summary: `Shop: ${selection.shop}`,
        panelState: 'is-success',
        inFlight: false,
      });
      toast('success', successMessage);
    }
  } catch (error) {
    markPendingAsSkipped(
      selection.items.slice(activeIndex),
      `Không tiếp tục do lỗi batch: ${error.message}`,
      'notAttempted',
      {countAsProcessed: false},
    );
    const finalMessage = `❌ Sync hàng loạt Etsy → Local lỗi: ${error.message}`;
    updateEtsyBulkSyncProgress({
      totalValid: selectedCount,
      processed,
      success: completed,
      requestFailed: requestFailedCount,
      notAttempted: notAttemptedCount,
      queued: queuedCount,
      skippedMapping: skippedMappingCount,
      statusText: finalMessage,
      summary: batchFailureSummary(failures),
      panelState: 'is-error',
      backendQueued: hasQueuedItems(),
      inFlight: false,
    });
    toast('error', finalMessage);
  } finally {
    etsyBulkSyncInFlight = false;
    etsySingleSyncInFlight = false;
    restoreButtons();
    if (button) button.innerHTML = originalText;
  }
}

function resetBulkEtsyUpdateFields() {
  const defaults = new Set(['title', 'description', 'tags', 'price', 'qty']);
  document.querySelectorAll('.etsy-bulk-update-field').forEach(input => {
    input.checked = defaults.has(input.value);
    input.disabled = false;
  });
  const confirmation = document.getElementById('etsy-bulk-update-live-confirmation');
  if (confirmation) confirmation.checked = false;
}

const ETSY_UPDATE_FIELD_LABELS = Object.freeze({
  title: 'Title', description: 'Mô tả', tags: 'Tags', price: 'Giá',
  qty: 'Số lượng', images: 'Ảnh', files: 'File số',
});

function updateBulkEtsyReview() {
  const selection = etsyBulkUpdateSelection;
  const shop = selection?.shop || document.getElementById('shop-switcher')?.value?.trim() || '—';
  const items = selection?.items || [];
  const rejected = selection?.rejected || [];
  const fields = Array.from(document.querySelectorAll('.etsy-bulk-update-field:checked'))
    .map(input => ETSY_UPDATE_FIELD_LABELS[input.value] || input.value);
  const shopReview = document.getElementById('etsy-bulk-update-shop-review');
  const direction = document.getElementById('etsy-bulk-update-direction');
  const fieldsSummary = document.getElementById('etsy-bulk-update-fields-summary');
  const products = document.getElementById('etsy-bulk-update-products');
  const rejectedBox = document.getElementById('etsy-bulk-update-rejected');
  const confirmation = document.getElementById('etsy-bulk-update-live-confirmation');
  const submitButton = document.getElementById('etsy-bulk-update-submit');
  if (shopReview) shopReview.textContent = shop;
  if (direction) direction.textContent = 'Local → Etsy';
  if (fieldsSummary) fieldsSummary.textContent = fields.length ? fields.join(', ') : 'Chưa chọn field';
  if (products) {
    products.innerHTML = items.length
      ? items.map(item => `<li><code>${escHtml(item.folder)}</code> → Etsy <strong>${escHtml(item.listingId)}</strong></li>`).join('')
      : '<li>Chưa có sản phẩm hợp lệ</li>';
  }
  if (rejectedBox) {
    rejectedBox.textContent = rejected.length
      ? `Bỏ qua ${rejected.length} lựa chọn không hợp lệ: ${rejected.map(item => `${item.folder} (${item.error})`).join(' · ')}`
      : '';
  }
  const reviewReady = Boolean(items.length && fields.length && confirmation?.checked);
  if (submitButton && !etsyBulkSyncInFlight) submitButton.disabled = !reviewReady;
  return reviewReady;
}

function openBulkEtsyUpdateModal() {
  if (etsyBulkSyncInFlight || etsySingleSyncInFlight) {
    toast('warning', '⚠️ Đang có một lượt đồng bộ Etsy. Hãy chờ hoàn tất.');
    return;
  }
  try {
    etsyBulkUpdateSelection = selectedLocalEtsyMappings();
  } catch (error) {
    toast('error', `❌ ${error.message}`);
    return;
  }
  document.getElementById('etsy-bulk-update-count').textContent = `${etsyBulkUpdateSelection.items.length} sản phẩm`;
  if (etsyBulkUpdateSelection.rejected.length) {
    document.getElementById('etsy-bulk-update-count').textContent =
      `${etsyBulkUpdateSelection.items.length} hợp lệ · ${etsyBulkUpdateSelection.rejected.length} bỏ qua`;
  }
  document.getElementById('etsy-bulk-update-shop').textContent = etsyBulkUpdateSelection.shop;
  const statusBox = document.getElementById('etsy-bulk-update-run-status');
  statusBox.className = 'etsy-update-run-status';
  statusBox.textContent = '';
  document.getElementById('etsy-bulk-update-submit').disabled = false;
  document.getElementById('etsy-bulk-update-submit').innerHTML = '⬆ Ghi LIVE lên Etsy';
  document.getElementById('etsy-bulk-update-cancel').disabled = false;
  document.getElementById('etsy-bulk-update-close-x').disabled = false;
  resetBulkEtsyUpdateFields();
  updateBulkEtsyReview();
  openModal('etsy-bulk-update-modal');
}

async function waitForEtsyUpdateJob(jobId, onProgress, options = {}) {
  const intervalMs = Number(options.intervalMs ?? 1500);
  const maxAttempts = Number(options.maxAttempts ?? 800);
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const response = await fetch(`/api/etsy/update-status?job_id=${encodeURIComponent(jobId)}`);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      throw new Error(data.detail || data.error || `Không đọc được trạng thái job ${jobId}`);
    }
    if (onProgress) onProgress(data);
    if (data.status === 'success') return data;
    if (data.status === 'error' || data.status === 'cancelled') {
      throw new Error(data.last_message || `Job kết thúc với trạng thái ${data.status}`);
    }
    if (attempt + 1 < maxAttempts) await sleep(intervalMs);
  }
  throw new Error('Hết thời gian chờ cập nhật Etsy hoàn tất');
}

async function submitBulkEtsyUpdate() {
  if (etsyBulkSyncInFlight) {
    toast('warning', '⚠️ Batch Etsy đang chạy. Hãy chờ hoàn tất.');
    return;
  }
  if (!etsyBulkUpdateSelection?.items?.length) {
    toast('error', 'Chưa có danh sách cập nhật hợp lệ');
    return;
  }
  const selection = {
    shop: etsyBulkUpdateSelection.shop,
    items: etsyBulkUpdateSelection.items.map(item => ({...item})),
    rejected: (etsyBulkUpdateSelection.rejected || []).map(item => ({...item})),
    selectedCount: etsyBulkUpdateSelection.selectedCount,
  };
  const currentShop = document.getElementById('shop-switcher')?.value?.trim() || '';
  if (currentShop !== selection.shop) {
    toast('error', `❌ Shop đã đổi từ ${selection.shop} sang ${currentShop || '[trống]'}. Hãy chọn lại sản phẩm.`);
    return;
  }
  const fields = Array.from(document.querySelectorAll('.etsy-bulk-update-field:checked'))
    .map(input => input.value);
  if (!fields.length) {
    toast('warning', 'Anh chọn ít nhất một nội dung cần cập nhật nhé');
    return;
  }
  const liveConfirmation = document.getElementById('etsy-bulk-update-live-confirmation');
  if (liveConfirmation && !liveConfirmation.checked) {
    toast('warning', 'Hãy review danh sách rồi xác nhận rõ thao tác ghi LIVE trước khi tiếp tục.');
    updateBulkEtsyReview();
    return;
  }
  const labels = ETSY_UPDATE_FIELD_LABELS;
  if (!confirm(
    `GHI LIVE ${selection.items.length} sản phẩm Local hợp lệ lên shop Etsy ${selection.shop}?\n` +
    `${selection.rejected.length ? `Bỏ qua ${selection.rejected.length}/${selection.selectedCount} sản phẩm chưa ghép hoặc mapping lỗi.\n` : ''}\n` +
    `Nội dung: ${fields.map(field => labels[field] || field).join(', ')}\n\n` +
    'Dữ liệu trên từng listing Etsy sẽ được thay bằng dữ liệu Local. Các listing chạy TUẦN TỰ, không chạy đồng thời.'
  )) return;

  etsyBulkSyncInFlight = true;
  const restoreButtons = disableEtsyBatchActionButtons();
  const submitButton = document.getElementById('etsy-bulk-update-submit');
  const cancelButton = document.getElementById('etsy-bulk-update-cancel');
  const closeButton = document.getElementById('etsy-bulk-update-close-x');
  const statusBox = document.getElementById('etsy-bulk-update-run-status');
  const failures = selection.rejected.map(item => ({...item}));
  const successes = [];
  submitButton.disabled = true;
  cancelButton.disabled = true;
  closeButton.disabled = true;
  document.querySelectorAll('.etsy-bulk-update-field').forEach(input => { input.disabled = true; });

  try {
    for (let index = 0; index < selection.items.length; index += 1) {
      const item = selection.items[index];
      const latestShop = document.getElementById('shop-switcher')?.value?.trim() || '';
      if (latestShop !== selection.shop) {
        selection.items.slice(index).forEach(pendingItem => failures.push({
          ...pendingItem,
          error: `Shop đã đổi từ ${selection.shop} sang ${latestShop || '[trống]'}`,
        }));
        break;
      }
      submitButton.innerHTML = `<span class="spinner"></span> ${index + 1}/${selection.items.length} ${escHtml(item.folder)}`;
      statusBox.className = 'etsy-update-run-status is-running';
      statusBox.textContent = `⏳ ${index + 1}/${selection.items.length}: đang khởi động ${item.folder} → Etsy ${item.listingId}`;
      try {
        const response = await fetch(`/api/products/${item.row}/push-to-etsy`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            fields,
            shop: selection.shop,
            folder: item.folder,
            listing_id: item.listingId,
          }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.ok) {
          throw new Error(data.detail || data.error || 'Không thể bắt đầu cập nhật');
        }
        await waitForEtsyUpdateJob(data.job_id, job => {
          statusBox.className = 'etsy-update-run-status is-running';
          statusBox.textContent = `⏳ ${index + 1}/${selection.items.length}: ${item.folder} — ${job.last_message || job.status}`;
        });
        successes.push(item);
      } catch (error) {
        failures.push({...item, error: error.message});
      }
    }

    restoreFailedBatchSelection(failures);
    if (failures.length) {
      statusBox.className = 'etsy-update-run-status is-error';
      statusBox.textContent = `⚠️ Hoàn tất một phần: ${successes.length} thành công, ${failures.length} lỗi/bỏ qua. ${batchFailureSummary(failures)}`;
      toast('warning', `⚠️ Local → Etsy: ${successes.length} thành công, ${failures.length} lỗi/bỏ qua`);
      submitButton.innerHTML = '↻ Thử lại sản phẩm lỗi';
    } else {
      statusBox.className = 'etsy-update-run-status is-success';
      statusBox.textContent = `✅ Đã cập nhật tuần tự ${successes.length}/${selection.items.length} listing Etsy.`;
      toast('success', `✅ Đã cập nhật ${successes.length} sản phẩm Local lên Etsy ${selection.shop}`);
      submitButton.innerHTML = '✅ Hoàn tất';
    }
  } finally {
    etsyBulkSyncInFlight = false;
    restoreButtons();
    cancelButton.disabled = false;
    cancelButton.textContent = 'Đóng';
    closeButton.disabled = false;
    document.querySelectorAll('.etsy-bulk-update-field').forEach(input => { input.disabled = false; });
    const retryableFailures = failures.filter(item => item.listingId);
    submitButton.disabled = retryableFailures.length === 0;
    etsyBulkUpdateSelection = retryableFailures.length
      ? {
          shop: selection.shop,
          items: retryableFailures.map(({error, ...item}) => item),
          rejected: [],
          selectedCount: retryableFailures.length,
        }
      : null;
    if (retryableFailures.length) {
      document.getElementById('etsy-bulk-update-count').textContent = `${retryableFailures.length} sản phẩm lỗi có thể thử lại`;
    }
  }
}

async function openFolder(row, type) {
  try {
    await fetch(`/api/products/${row}/open-folder`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type }),
    });
    toast('info', `📂 Đã mở folder ${type}/ trong Finder`);
  } catch (e) {
    toast('error', `Lỗi: ${e.message}`);
  }
}

async function deleteProduct(row, folder) {
  const activeShop = document.getElementById('shop-switcher')?.value?.trim() || '';
  if (!activeShop) {
    toast('error', 'Thiếu thông tin shop hiện tại để xoá Local');
    return;
  }
  if (!confirm(`Xoá "${folder}" khỏi dashboard?\n\n• Dữ liệu trong Excel sẽ bị xoá\n• Thư mục trên ổ cứng sẽ được chuyển sang thư mục thu gom để có thể khôi phục\n\nTiếp tục?`)) return;
  try {
    const response = await fetch(`/api/products/${row}`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ shop: activeShop, folder }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      throw new Error(data?.error || data?.detail || `HTTP ${response.status}`);
    }
    try {
      await loadProducts({ throwOnError: true });
      toast('success', `🗑 Đã xoá ${folder} khỏi dashboard`);
    } catch (refreshError) {
      toast('warning', `✅ ${folder} đã bị xoá khỏi dashboard, nhưng làm mới danh sách gặp lỗi: ${refreshError.message}`);
    }
  } catch (e) {
    toast('error', `Lỗi xoá sản phẩm: ${e.message}`);
  }
}

async function batchDelete() {
  const selected = selectedBatchCheckboxes('local');
  const activeShop = document.getElementById('shop-switcher')?.value?.trim() || '';
  if (!activeShop) {
    toast('error', 'Thiếu thông tin shop hiện tại để xoá hàng loạt');
    return;
  }
  const items = selected
    .map((cb) => ({
      row: parseInt(cb.value, 10),
      folder: String(cb.dataset.folder || '').trim(),
    }))
    .filter((item) => Number.isInteger(item.row) && item.row > 0);

  if (selected.length === 0 || items.length === 0) {
    toast('error', 'Chưa chọn sản phẩm nào để xoá!');
    return;
  }
  
  if (!confirm(`Xoá ${items.length} sản phẩm khỏi dashboard?\n\n• Dữ liệu trong Excel sẽ bị xoá\n• Các thư mục local sẽ được chuyển sang vùng thu gom phục hồi\n\nTiếp tục?`)) return;
  const button = document.getElementById('local-batch-delete-btn');
  const oldButtonText = button?.innerHTML;
  if (button) {
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span> Đang xoá...';
  }
  
  try {
    const res = await fetch('/api/batch-delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ shop: activeShop, items }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data?.error || data?.detail || `HTTP ${res.status}`);
    }
    if (data.ok) {
      const deletedCount = data.deleted ?? items.length;
      try {
        await loadProducts({ throwOnError: true });
        toast('success', `✅ Đã xoá ${deletedCount} sản phẩm thành công!`);
      } catch (refreshError) {
        toast('warning', `✅ Đã xoá ${deletedCount} sản phẩm, nhưng làm mới danh sách gặp lỗi: ${refreshError.message}`);
      } finally {
        document.getElementById('cb-select-all').checked = false;
        selectedBatchCheckboxes('local').forEach((cb) => (cb.checked = false));
        updateBatchUI();
      }
    } else {
      throw new Error(data.error || 'Lỗi không xác định');
    }
  } catch (e) {
    toast('error', `Lỗi xoá hàng loạt: ${e.message}`);
  } finally {
    if (button) {
      button.disabled = false;
      button.innerHTML = oldButtonText || '🗑 Xoá Local đã chọn';
    }
  }
}

async function batchPostSelected() {
  const activeShop = document.getElementById('shop-switcher')?.value?.trim() || '';
  if (!activeShop) {
    toast('error', 'Thiếu thông tin shop hiện tại để đăng hàng loạt');
    return;
  }

  const selected = selectedBatchCheckboxes('local');
  const items = selected
    .map((cb) => ({
      row: parseInt(cb.value, 10),
      folder: String(cb.dataset.folder || '').trim(),
    }))
    .filter((item) =>
      Number.isInteger(item.row) && item.row >= 4
      && /^product-\d+$/.test(item.folder)
    );

  if (selected.length === 0 || items.length === 0) {
    toast('error', 'Chưa chọn sản phẩm local nào để đăng');
    return;
  }
  if (selected.length !== items.length) {
    toast('error', 'Có checkbox local chưa hợp lệ (row/folder). Vui lòng kiểm tra lại.');
    return;
  }

  if (!confirm(`Đăng ${items.length} sản phẩm đã chọn?

✅ 1 Chrome, xử lý tuần tự`)) return;

  const button = document.getElementById('local-batch-post-btn');
  const oldButtonText = button?.innerHTML;
  if (button) {
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span> Đang đăng...';
  }

  try {
    const res = await fetch('/api/run-selected-products', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ shop: activeShop, items }),
    });
    const data = await res.json().catch(() => ({}));

    if (!res.ok || !data?.ok) {
      throw new Error(data?.error || data?.detail || `HTTP ${res.status}`);
    }

    selectedBatchCheckboxes('local').forEach((cb) => (cb.checked = false));
    document.getElementById('cb-select-all').checked = false;
    updateBatchUI();
    await loadProducts({ throwOnError: true });

    const queued = data?.queued ?? items.length;
    const skipped = Number(data?.skipped || 0);
    const rejected = Array.isArray(data?.rejected) ? data.rejected : [];
    toast('success', `✅ Đã xếp hàng ${queued} sản phẩm vào Live Queue`);
    if (skipped > 0) {
      const names = rejected
        .map((item) => item?.folder || `row ${item?.row || '?'}`)
        .filter(Boolean)
        .slice(0, 3)
        .join(', ');
      const more = skipped > 3 ? ` +${skipped - 3} khác` : '';
      toast(
        'error',
        `⏭ Bỏ qua ${skipped} sản phẩm (đã gắn lỗi dưới thẻ): ${names}${more}`,
      );
    } else {
      toast('info', '🚀 Đang theo dõi Live Logs để xem tiến trình đăng');
    }
    if (queued > 0 && skipped > 0) {
      toast('info', '🚀 Tiếp tục đăng các sản phẩm còn lại — xem Live Logs');
    }
  } catch (e) {
    toast('error', `Lỗi đăng hàng loạt: ${e.message}`);
    try { await loadProducts({ throwOnError: false }); } catch (_) { /* ignore */ }
  } finally {
    if (button) {
      button.disabled = false;
      button.innerHTML = oldButtonText || '🚀 Đăng hàng loạt';
    }
  }
}

function toggleStatusMenu(row) {
  // Close all other open menus first
  document.querySelectorAll('.status-menu').forEach(m => {
    if (m.id !== `smenu-${row}`) m.style.display = 'none';
  });
  const menu = document.getElementById(`smenu-${row}`);
  if (menu) menu.style.display = menu.style.display === 'none' ? 'block' : 'none';
}

async function changeStatus(row, folder, newStatus) {
  // Close menu
  const menu = document.getElementById(`smenu-${row}`);
  if (menu) menu.style.display = 'none';

  try {
    await fetch(`/api/products/${row}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: newStatus }),
    });
    // Update local state
    const p = allProducts.find(x => x.row === row);
    if (p) p.status = newStatus;
    toast('success', `✅ ${folder}: ${newStatus}`);
    // Refresh just this card
    if (p) {
      const card = document.getElementById(`card-${row}`);
      if (card) card.outerHTML = productCard(p);
    }
    updateStats(allProducts);
  } catch (e) {
    toast('error', `Lỗi: ${e.message}`);
  }
}

// Close status menu when clicking outside
document.addEventListener('click', (e) => {
  if (!e.target.closest('.status-wrap')) {
    document.querySelectorAll('.status-menu').forEach(m => m.style.display = 'none');
  }
});



async function regenImages(row, folder) {
  if (!confirm(`Tạo lại 10 ảnh listing cho "${folder}"?\n(Dùng file planner gốc trong files/)`)) return;
  runningSet.add(folder);
  refreshCard(row, folder);
  try {
    const res  = await fetch(`/api/products/${row}/regenerate`, { method: 'POST' });
    const data = await res.json();
    if (!data.ok) { toast('error', data.error || 'Không có file planner'); runningSet.delete(folder); }
    else toast('info', `🔄 Đang regenerate ảnh ${folder}...`);
  } catch (e) {
    toast('error', `Lỗi: ${e.message}`);
    runningSet.delete(folder);
  }
  refreshCard(row, folder);
}

async function runAllPending() {
  const pending = allProducts.filter(p =>
    p.status.includes('Chờ đăng') || p.status.includes('Lỗi') || p.status.includes('❌')
  );
  if (!pending.length) return toast('info', 'Không có sản phẩm nào đang chờ đăng');
  if (!confirm(`Chạy ${pending.length} sản phẩm đang chờ đăng?\n\n✅ 1 Chrome duy nhất, xử lý tuần tự từng sản phẩm.`)) return;

  try {
    const res = await fetch('/api/run-all-pending', { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      toast('info', `🚀 Batch poster đang chạy — theo dõi Live Logs`);
    } else {
      toast('error', data.error || 'Lỗi khởi động batch');
    }
  } catch(e) {
    toast('error', `Lỗi: ${e.message}`);
  }
}

async function stopAll() {
  if (!confirm('Dừng tất cả poster đang chạy?')) return;
  try {
    const res = await fetch('/api/stop-all', { method: 'POST' });
    const data = await res.json();
    toast('warning', `🛑 Đã dừng: ${(data.stopped || []).join(', ') || 'không có gì đang chạy'}`);
  } catch(e) {
    toast('error', `Lỗi: ${e.message}`);
  }
}

async function syncEtsyShop() {
  const btn = document.getElementById('btn-etsy-sync');
  if (!confirm('Đồng bộ Etsy Shop Manager với dashboard?\n\nChrome sẽ mở Etsy, quét Active/Draft rồi cập nhật link + trạng thái vào Excel.')) return;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Đang sync...';
  try {
    const res = await fetch('/api/etsy/sync', { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      const position = Number(data.command?.position || 0);
      toast('info', `🗓️ Đã thêm Sync Etsy Shop vào hàng chờ${position ? ` (vị trí #${position})` : ''}. Xem Live Logs bên phải.`);
    } else {
      toast('error', data.error || 'Không chạy được đồng bộ Etsy');
      btn.disabled = false;
      btn.innerHTML = '🔄 Đồng bộ Etsy Shop';
    }
  } catch(e) {
    toast('error', `Lỗi: ${e.message}`);
    btn.disabled = false;
    btn.innerHTML = '🔄 Đồng bộ Etsy Shop';
  }
}



async function batchGenerateSEO() {
  if (!confirm('Tự động generate SEO (title, tags, description) cho tất cả sản phẩm có file nhưng chưa có title?\n\nVertex AI / Gemini sẽ xử lý từng sản phẩm và lưu thẳng vào Excel.')) return;
  const btn = document.querySelector('.btn-seo');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Đang chạy...';
  try {
    const res  = await fetch('/api/batch-seo', { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      toast('info', `🤖 Đang generate SEO cho ${data.count} sản phẩm: ${(data.folders || []).join(', ')}`);
    } else {
      toast('info', data.message || 'Không có folder nào cần generate');
      btn.disabled = false;
      btn.innerHTML = '🤖 Generate Missing SEO';
    }
  } catch (e) {
    toast('error', `Lỗi: ${e.message}`);
    btn.disabled = false;
    btn.innerHTML = '🤖 Generate Missing SEO';
  }
}

async function startWatcher() {
  const res  = await fetch('/api/services/watcher/start', { method: 'POST' });
  const data = await res.json();
  toast('success', `✅ Watcher started PID ${data.pid}`);
  pollServices();
}

async function quickSEO(row, folder) {
  toast('info', `🤖 Đang generate SEO cho ${folder}...`);
  const card = document.getElementById(`card-${row}`);
  if (card) card.style.opacity = '0.5';
  try {
    const res  = await fetch(`/api/products/${row}/regen-seo`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: folder, keywords: '' }),
    });
    const data = await res.json();
    if (data.ok) {
      toast('success', `✅ ${folder}: SEO đã generate — kiểm tra thẻ ✏️`);
      await loadProducts();   // reload to remove needs_seo flag & show real title
    } else {
      toast('error', data.error || 'Vertex AI không phản hồi');
    }
  } catch (e) {
    toast('error', `Lỗi: ${e.message}`);
  } finally {
    if (card) card.style.opacity = '1';
  }
}


// ── Edit Modal ─────────────────────────────────────────────────────────────────
function openEditModal(row) {
  const p = allProducts.find(x => x.row === row);
  if (!p) return;
  document.getElementById('edit-row').value   = row;
  document.getElementById('edit-folder').value = p.folder;
  document.getElementById('edit-title').value       = p.needs_seo ? "" : p.title;
  document.getElementById('edit-tags').value        = p.tags;
  document.getElementById('edit-keywords').value    = p.keywords;
  document.getElementById('edit-extra').value       = p.extra || ""; // Khôi phục lại gợi ý AI từ memory
  document.getElementById('edit-description').value = p.description;
  document.getElementById('edit-price').value       = p.price;
  document.getElementById('edit-section').value     = p.section;
  document.getElementById('edit-etsy-url').value    = p.etsy_url || ""; // Khôi phục Etsy URL
  document.getElementById('edit-sku').value         = p.sku || "";
  document.getElementById('edit-qty').value         = p.qty || 999;
  updateCount('edit-title', 'title-count', 140);
  document.getElementById('modal-title').textContent = `✏️ ${p.folder}`;
  openModal('edit-modal');
}

async function saveEdit() {
  const saveButton = document.getElementById('edit-save-btn');
  if (!saveButton || saveButton.disabled) return;

  const row = ProductEditSave.parseProductRow(document.getElementById('edit-row').value);
  const productBeforeSave = row ? allProducts.find(product => product.row === row) : null;
  if (!row || !productBeforeSave) {
    toast('error', '❌ Không xác định được dòng sản phẩm cần lưu. Hãy đóng và mở lại cửa sổ chỉnh sửa.');
    return;
  }

  let titleVal = document.getElementById('edit-title').value.trim();
  if (titleVal.startsWith('[Cần SEO]')) {
    titleVal = '';
  }
  const priceValue = Number(document.getElementById('edit-price').value);
  const qtyInput = document.getElementById('edit-qty').value.trim();
  const qtyValue = qtyInput === '' ? 999 : Number(qtyInput);
  if (!Number.isFinite(priceValue) || priceValue < 0 || !Number.isInteger(qtyValue) || qtyValue < 0) {
    toast('error', '❌ Giá hoặc số lượng không hợp lệ. Vui lòng kiểm tra lại trước khi lưu.');
    return;
  }
  const payload = {
    title:       titleVal,
    tags:        document.getElementById('edit-tags').value.trim(),
    keywords:    document.getElementById('edit-keywords').value.trim(),
    description: document.getElementById('edit-description').value.trim(),
    price:       priceValue,
    section:     document.getElementById('edit-section').value.trim(),
    extra:       document.getElementById('edit-extra').value.trim(), // Save extra to payload
    etsy_url:    document.getElementById('edit-etsy-url').value.trim(), // Save Etsy URL
    sku:         document.getElementById('edit-sku').value.trim(), // Save SKU
    qty:         qtyValue,
  };

  const originalButtonHtml = saveButton.innerHTML;
  saveButton.disabled = true;
  saveButton.innerHTML = '<span class="spinner"></span> Đang lưu...';
  try {
    const response = await fetch(`/api/products/${row}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    let data;
    try {
      data = await response.json();
    } catch (_) {
      throw new Error(`Server trả về dữ liệu không hợp lệ (HTTP ${response.status})`);
    }
    if (!response.ok || !data.ok) {
      throw new Error(data.detail || data.error || data.message || `Server không lưu được (HTTP ${response.status})`);
    }

    await loadProducts({ throwOnError: true });
    const savedProduct = allProducts.find(product => product.row === row);
    const mismatches = ProductEditSave.findSavedFieldMismatches(payload, savedProduct);
    if (mismatches.length) {
      const first = mismatches[0];
      const fieldLabel = first.field === 'etsy_url' ? 'Etsy URL' : first.field;
      throw new Error(`Đã gửi lệnh nhưng kiểm tra lại thấy ${fieldLabel} chưa được cập nhật`);
    }

    markModalClean('edit-modal');
    closeModal('edit-modal');
    const urlMessage = payload.etsy_url ? ' và Etsy URL' : '';
    toast('success', `✅ Đã lưu ${productBeforeSave.folder}${urlMessage}, kiểm tra lại thành công`);
  } catch (e) {
    toast('error', `❌ Không lưu được ${productBeforeSave.folder}: ${e.message}`);
  } finally {
    saveButton.disabled = false;
    saveButton.innerHTML = originalButtonHtml;
  }
}

// ── SEO Auto-generate ──────────────────────────────────────────────────────────
// ── Regen single field ─────────────────────────────────────────────────────────
async function regenField(field) {
  const row      = parseInt(document.getElementById('edit-row').value);
  const folder   = document.getElementById('edit-folder').value;
  const title    = document.getElementById('edit-title').value.trim();
  const keywords = document.getElementById('edit-keywords').value.trim();
  const extra    = document.getElementById('edit-extra').value.trim();

  const labelMap = { title: 'Title', tags: 'Tags', description: 'Mô tả' };
  const btn = document.querySelector(`.btn-regen-field[onclick="regenField('${field}')"]`);
  const origHTML = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>'; }
  toast('info', `🤖 Đang generate ${labelMap[field]} cho ${folder}...`);

  try {
    const res = await fetch(`/api/products/${row}/regen-seo`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, keywords, extra, field }),
    });
    const data = await res.json();
    if (data.ok && data.seo) {
      const seo = data.seo;
      if (field === 'title' && seo.title) {
        document.getElementById('edit-title').value = seo.title;
        updateCount('edit-title', 'title-count', 140);
        toast('success', `✅ Title mới đã fill — nhấn 💾 Lưu để ghi`);
      }
      if (field === 'tags' && seo.tags) {
        document.getElementById('edit-tags').value = seo.tags;
        toast('success', `✅ Tags mới đã fill — nhấn 💾 Lưu để ghi`);
      }
      if (field === 'description' && seo.description) {
        document.getElementById('edit-description').value = seo.description;
        toast('success', `✅ Mô tả mới đã fill — nhấn 💾 Lưu để ghi`);
      }
      // Sync memory
      const p = allProducts.find(x => x.row === row);
      if (p) {
        if (field === 'title') p.title = seo.title;
        if (field === 'tags')  p.tags  = seo.tags;
        if (field === 'description') p.description = seo.description;
      }
    } else {
      toast('error', data.error || 'Vertex AI không phản hồi');
    }
  } catch (e) {
    toast('error', `Lỗi: ${e.message}`);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = origHTML; }
  }
}

async function regenSEO() {
  const row      = parseInt(document.getElementById('edit-row').value);
  const folder   = document.getElementById('edit-folder').value;
  const title    = document.getElementById('edit-title').value.trim();
  const keywords = document.getElementById('edit-keywords').value.trim();
  const extra    = document.getElementById('edit-extra').value.trim();

  const btn = document.querySelector('.btn-seo');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Đang tạo SEO...';
  toast('info', `🤖 Đang generate SEO cho ${folder}...`);

  try {
    const res = await fetch(`/api/products/${row}/regen-seo`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, keywords, extra }),
    });
    const data = await res.json();

    if (data.ok && data.seo) {
      const seo = data.seo;
      // Smart trim title to ≤140 chars at pipe boundary
      let newTitle = seo.title || seo.etsy_title || title;
      if (newTitle.length > 140) {
        const parts = newTitle.split(' | ');
        let trimmed = parts[0];
        for (const part of parts.slice(1)) {
          const candidate = trimmed + ' | ' + part;
          if (candidate.length <= 140) trimmed = candidate;
          else break;
        }
        newTitle = trimmed;
      }
      const tagSource = seo.tags || seo.etsy_tags || '';
      const tagsArr = Array.isArray(tagSource) ? tagSource : tagSource.split(',');
      const newTags = tagsArr.slice(0, 13).join(', ');
      const newDesc = seo.description || '';

      // Fill form fields
      document.getElementById('edit-title').value       = newTitle;
      document.getElementById('edit-description').value = newDesc;
      document.getElementById('edit-tags').value         = newTags;
      updateCount('edit-title', 'title-count', 140);

      // ✅ Sync vào allProducts memory để mở lại modal vẫn thấy data mới
      const p = allProducts.find(x => x.row === row);
      if (p) {
        p.title       = newTitle;
        p.tags        = newTags;
        p.description = newDesc;
        p.keywords    = keywords;
        p.extra       = extra;
        p.needs_seo   = false;
        p.status      = p.status === '⚠ Cần generate SEO' ? '⏳ Chờ đăng' : p.status;
      }

      toast('success', `✅ SEO mới đã fill vào form — nhấn 💾 Lưu để ghi vào Excel`);
      // Reload card UI in background
      loadProducts();
    } else {
      toast('error', data.error || 'Vertex AI không phản hồi');
    }
  } catch (e) {
    toast('error', `Lỗi: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '🤖 Tạo SEO';
  }
}


// ── Image Modal ────────────────────────────────────────────────────────────────
async function openImageModal(row, folder) {
  document.getElementById('img-row').value = row;
  document.getElementById('img-modal-title').textContent = `🖼 ${folder} — Quản lý ảnh`;
  await loadImageGrid(row, folder);
  openModal('image-modal');
}

async function loadImageGrid(row, folder) {
  const grid = document.getElementById('img-grid');
  grid.innerHTML = '<div style="color:var(--text3);grid-column:1/-1">⏳ Đang tải...</div>';
  try {
    const res  = await fetch(`/api/products/${row}/images`);
    const data = await res.json();
    const imgs = data.images || [];
    const normalizedImages = normalizeImageGalleryItems(imgs);
    imageModalImages = normalizedImages;
    if (!normalizedImages.length) {
      grid.innerHTML = '<div style="color:var(--text3);grid-column:1/-1">Chưa có ảnh nào</div>';
      return;
    }
    grid.innerHTML = normalizedImages.map((img, i) => {
        const displayName = escHtml(img.name || `Ảnh ${i + 1}`);
      if (img.isHydrationRequired) {
        return `
      <div class="img-card">
        <button class="img-card-placeholder" type="button" data-image-index="${i}" role="button" tabindex="0" onclick="openImageModalLightbox(this)" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();openImageModalLightbox(this)}">
          <span class="placeholder-icon" aria-hidden="true">☁️</span>
          <span class="placeholder-text">Cần tải</span>
        </button>
        <div class="img-card-overlay">
          <div class="img-card-name">${displayName}</div>
          <button class="btn btn-danger btn-sm" data-row="${row}" data-folder="${escHtml(folder)}" data-filename="${escHtml(img.name)}" onclick="deleteImageFromButton(this)">🗑 Xoá</button>
        </div>
      </div>`;
      }

      return `
      <div class="img-card">
        <img src="${escHtml(img.url)}?t=${Date.now()}" alt="${displayName}" loading="lazy" data-image-index="${i}" role="button" tabindex="0" onclick="openImageModalLightbox(this)" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();openImageModalLightbox(this)}">
        <div class="img-card-overlay">
          <div class="img-card-name">${displayName}</div>
          <button class="btn btn-danger btn-sm" data-row="${row}" data-folder="${escHtml(folder)}" data-filename="${escHtml(img.name)}" onclick="deleteImageFromButton(this)">🗑 Xoá</button>
        </div>
      </div>`;
    }).join('');
  } catch (e) {
    grid.innerHTML = `<div style="color:var(--red);grid-column:1/-1">❌ ${e.message}</div>`;
  }
}

function initImageLightbox() {
  const lightbox = document.getElementById('image-lightbox');
  if (!lightbox) return;
  lightbox.querySelectorAll('[data-lightbox-close]').forEach(button => button.addEventListener('click', closeImageLightbox));
  document.getElementById('lightbox-prev')?.addEventListener('click', () => moveImageLightbox(-1));
  document.getElementById('lightbox-next')?.addEventListener('click', () => moveImageLightbox(1));
  document.addEventListener('keydown', event => {
    if (!lightbox.classList.contains('open')) return;
    if (event.key === 'Escape') closeImageLightbox();
    else if (event.key === 'ArrowLeft') moveImageLightbox(-1);
    else if (event.key === 'ArrowRight') moveImageLightbox(1);
  });
}

function safeLightboxImages(images) {
  return Array.isArray(images)
    ? images
      .map((image) => {
        if (typeof image === 'string') return image.trim();
        if (!image || typeof image !== 'object') return '';
        return String(image.full_url || image.url || '').trim();
      })
      .filter((url) => typeof url === 'string' && url && (url.startsWith('/') || /^https?:\/\//i.test(url)))
    : [];
}

function openProductLightbox(thumbnail) {
  const folder = thumbnail?.dataset?.folder || '';
  const product = allProducts.find(item => String(item.folder || '') === folder);
  const images = normalizeImageGalleryItems(product?.image_previews || product?.all_images || []);
  openImageLightbox(images, Number(thumbnail?.dataset?.imageIndex || 0), folder, thumbnail);
}

function openImageModalLightbox(thumbnail) {
  const index = Number(thumbnail?.dataset?.imageIndex || 0);
  openImageLightbox(imageModalImages, index, imageModalImages[index]?.name || '', thumbnail);
}

function openImageLightbox(images, index = 0, caption = '', opener = null) {
  const safeImages = safeLightboxImages(images);
  if (!safeImages.length) return;
  lightboxState = { images: safeImages, index: Math.max(0, Math.min(index, safeImages.length - 1)), caption, opener };
  const lightbox = document.getElementById('image-lightbox');
  lightbox.classList.add('open');
  lightbox.setAttribute('aria-hidden', 'false');
  document.body.classList.add('lightbox-open');
  renderImageLightbox();
  lightbox.querySelector('.image-lightbox-close')?.focus();
}

function renderImageLightbox() {
  const { images, index, caption } = lightboxState;
  const image = document.getElementById('lightbox-image');
  if (!image || !images.length) return;
  image.src = images[index];
  image.alt = `${caption || 'Ảnh sản phẩm'} — ${index + 1}/${images.length}`;
  document.getElementById('lightbox-caption').textContent = caption || 'Ảnh sản phẩm';
  document.getElementById('lightbox-counter').textContent = `${index + 1} / ${images.length}`;
  document.getElementById('lightbox-prev').disabled = images.length < 2;
  document.getElementById('lightbox-next').disabled = images.length < 2;
}

function moveImageLightbox(direction) {
  if (lightboxState.images.length < 2) return;
  lightboxState.index = (lightboxState.index + direction + lightboxState.images.length) % lightboxState.images.length;
  renderImageLightbox();
}

function closeImageLightbox() {
  const lightbox = document.getElementById('image-lightbox');
  if (!lightbox?.classList.contains('open')) return;
  lightbox.classList.remove('open');
  lightbox.setAttribute('aria-hidden', 'true');
  document.body.classList.remove('lightbox-open');
  document.getElementById('lightbox-image').removeAttribute('src');
  lightboxState.opener?.focus?.();
}

async function handleImageDrop(event) {
  event.preventDefault();
  document.getElementById('drop-zone').classList.remove('dragover');
  await uploadImages(event.dataTransfer.files);
}

async function handleImageSelect() {
  await uploadImages(document.getElementById('img-file-input').files);
}

async function uploadImages(files) {
  if (!files.length) return;
  const row    = parseInt(document.getElementById('img-row').value);
  const folder = allProducts.find(p => p.row === row)?.folder || '';
  const form   = new FormData();
  for (const f of files) form.append('files', f);
  toast('info', `⏳ Uploading ${files.length} ảnh...`);
  try {
    const res  = await fetch(`/api/products/${row}/images`, { method: 'POST', body: form });
    const data = await res.json();
    toast('success', `✅ Upload ${data.saved?.length} ảnh thành công`);
    await loadImageGrid(row, folder);
    loadProducts();
  } catch (e) {
    toast('error', `Lỗi upload: ${e.message}`);
  }
}

async function deleteImage(row, folder, filename, btn) {
  if (!confirm(`Xoá ảnh ${filename}?`)) return;
  btn.disabled = true;
  await fetch(`/api/products/${row}/images/${filename}`, { method: 'DELETE' });
  await loadImageGrid(row, folder);
  loadProducts();
  toast('success', `🗑 Đã xoá ${filename}`);
}

function deleteImageFromButton(button) {
  return deleteImage(Number(button.dataset.row), button.dataset.folder || '', button.dataset.filename || '', button);
}

// ── Services ───────────────────────────────────────────────────────────────────
function runtimeHealthWarnings(payload = {}) {
  const source = payload?.source || {};
  const backup = payload?.backup_scheduler || {};
  const loaded = backup.loaded || {};
  const evidence = backup.status_evidence || {};
  const canonicalMatch = payload?.canonical_match;
  const readiness = payload?.service_readiness || {};
  const core = readiness?.core || {};
  const optional = readiness?.optional || {};
  const checks = [];

  if (canonicalMatch === false) {
    checks.push({ kind: 'canonical', label: 'Non-canonical checkout' });
  }

  if (source?.dirty || payload?.health_summary?.source_stale) {
    checks.push({ kind: 'source', label: 'Source dirty/unstable' });
  }

  if (!loaded?.daily || !loaded?.weekly || backup?.loaded_ok === false) {
    checks.push({ kind: 'scheduler', label: 'Backup scheduler not fully loaded' });
  }

  const summary = payload?.health_summary || {};
  const summaryHasFailure = summary?.backup_last_failure === true;
  const summaryHasNoFailure = summary?.backup_last_failure === false;
  let backupFailure = summaryHasFailure;
  if (!summaryHasFailure && !summaryHasNoFailure) {
    const lastFailure = evidence?.last_failure || {};
    const lastSuccess = evidence?.last_success || {};
    if (lastFailure && lastFailure.timestamp) {
      const failureTs = Date.parse(String(lastFailure.timestamp));
      if (Number.isNaN(failureTs)) {
        backupFailure = true;
      } else if (!(lastSuccess && lastSuccess.timestamp)) {
        backupFailure = true;
      } else {
        const successTs = Date.parse(String(lastSuccess.timestamp));
        backupFailure = Number.isNaN(successTs) || failureTs > successTs;
      }
    }
  }

  if (backupFailure) {
    checks.push({ kind: 'backup', label: 'Backup failure evidence present' });
  }

  const optionalChecks = optional?.checks || readiness?.checks || {};
  if (optionalChecks.vertex_app === false) {
    checks.push({ kind: 'vertex', label: 'Vertex service offline' });
  }
  if (optionalChecks.mlx_ai === false) {
    checks.push({ kind: 'mlx', label: 'MLX service offline' });
  }
  if (optionalChecks.watcher === false) {
    checks.push({ kind: 'watcher', label: 'Watcher not running' });
  }

  return checks;
}

function renderRuntimeHealth(payload = {}) {
  const widget = document.getElementById('runtime-health');
  const text = document.getElementById('runtime-health-meta');
  if (!widget) return;

  const warnings = runtimeHealthWarnings(payload);
  const source = payload?.source || {};
  const currentRoot = String(payload?.current_root || 'unknown').trim();
  const canonicalRoot = String(payload?.canonical_root || 'unknown').trim();
  const activeShop = payload?.active_shop || {};
  const title = document.querySelector('#runtime-health .runtime-health-text');

  const readiness = payload?.service_readiness || {};
  const coreReadiness = readiness?.core;
  const hasWarning = warnings.length > 0;
  const hasMismatch = coreReadiness?.ok === false || (
    coreReadiness?.ok === undefined && readiness?.ok === false
  );
  const coreHealthy = coreReadiness?.ok !== undefined
    ? coreReadiness.ok
    : readiness?.ok !== false;
  widget.classList.remove('warning', 'offline');
  if (hasMismatch || hasWarning) {
    widget.classList.add('warning');
  }

  const canonicalTag = payload?.canonical_match ? '✅' : '⚠️';
  const shopLabel = activeShop?.name ? ` · ${activeShop.name}` : '';

  if (title) {
    title.textContent = `${canonicalTag} Runtime Health`;
  }
  if (text) {
    text.textContent = `${shopLabel ? shopLabel : ''} ${coreHealthy ? 'ready' : 'service degraded'} (${currentRoot === canonicalRoot ? 'canonical' : 'noncanonical'})`;
  }

  widget.dataset.currentRoot = currentRoot;
  widget.dataset.canonicalRoot = canonicalRoot;

  const existingList = widget.querySelector('.runtime-health-issues');
  if (existingList) {
    existingList.remove();
  }
  if (!warnings.length) return;

  const list = document.createElement('span');
  list.className = 'runtime-health-meta runtime-health-issues';
  list.textContent = warnings.map((item) => item.label).join(' • ');
  widget.appendChild(list);
}

function normalizeRuntimeHealthStatus(payload = {}) {
  if (!payload || typeof payload !== 'object') {
    return { ok: false, state: 'offline', warnings: [] };
  }

  const readiness = payload?.service_readiness || {};
  const coreReadiness = readiness?.core;
  const serviceOk = coreReadiness?.ok !== undefined ? coreReadiness?.ok : readiness?.ok;
  const warnings = runtimeHealthWarnings(payload);
  if (serviceOk === false) {
    warnings.push({ kind: 'service', label: 'Core service readiness failed' });
  }

  return {
    ok: serviceOk !== false,
    warnings,
  };
}

async function pollServices() {
  try {
    const res  = await fetch('/api/services');
    const data = await res.json();
    setSvc('svc-vertex',  data.vertex_app);
    setSvc('svc-mlx',     data.mlx_ai);
    setSvc('svc-watcher', data.watcher);

    const prev = new Set(runningSet);
    runningSet = new Set(data.running || []);
    const syncBtn = document.getElementById('btn-etsy-sync');
    if (syncBtn) {
      const syncing = runningSet.has('__ETSY_SYNC__');
      syncBtn.disabled = syncing;
      syncBtn.innerHTML = syncing ? '<span class="spinner"></span> Đang sync...' : '🔄 Đồng bộ Etsy Shop';
    }
    // If any process finished, reload products
    if ([...prev].some(f => !runningSet.has(f))) {
      await loadProducts();
    }
  } catch (e) {
    setSvc('svc-vertex', false);
    setSvc('svc-mlx', false);
    setSvc('svc-watcher', false);
  }
}

async function pollRuntimeHealth() {
  try {
    const response = await fetch('/api/runtime-health');
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload?.detail || `Runtime health HTTP ${response.status}`);
    }
    renderRuntimeHealth(payload);
  } catch (error) {
    const widget = document.getElementById('runtime-health');
    if (widget) {
      widget.className = 'runtime-health offline';
      const title = document.querySelector('#runtime-health .runtime-health-text');
      const text = document.getElementById('runtime-health-meta');
      if (title) {
        title.textContent = '⚠ Runtime Health';
      }
      if (text) {
        text.textContent = `unavailable: ${error?.message || 'network error'}`;
      }
    }
  }
}

function setSvc(id, online) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = `svc ${online ? 'online' : 'offline'}`;
}

// ── SSE Logs ───────────────────────────────────────────────────────────────────
function connectLogs() {
  const es = new EventSource('/api/logs');
  es.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data);
      if (d.ping) return;
      appendLog(d.ts, d.msg);
      // If a poster/regen finished, reload products after 2s
      if (d.msg && (d.msg.includes('✅ Xong') || d.msg.includes('✅ Ảnh mới xong') || d.msg.includes('✅ Hoàn thành') || d.msg.includes('Đồng bộ xong'))) {
        setTimeout(loadProducts, 2000);
      }
    } catch (_) {}
  };
  es.onerror = () => {
    appendLog('--:--', '[SYSTEM] ⚠ Log stream bị ngắt — đang kết nối lại...');
    setTimeout(connectLogs, 3000);
    es.close();
  };
}

function appendLog(ts, msg) {
  const body = document.getElementById('log-body');
  const div  = document.createElement('div');
  div.className = 'log-line ' + logClass(msg);
  div.innerHTML  = `<span class="ts">${ts}</span>${escHtml(msg)}`;
  body.appendChild(div);
  body.scrollTop = body.scrollHeight;
  // Keep max 300 lines
  while (body.children.length > 300) body.removeChild(body.firstChild);
}

function logClass(msg) {
  if (!msg) return 'info';
  if (msg.includes('✅') || msg.includes('Xong') || msg.includes('hoàn thành')) return 'success';
  if (msg.includes('❌') || msg.includes('Lỗi') || msg.includes('error')) return 'error';
  if (msg.includes('⚠') || msg.includes('Warning')) return 'warn';
  if (msg.includes('[REGEN]')) return 'regen';
  if (msg.includes('[SEO]')) return 'seo';
  return 'info';
}

function clearLogs() { document.getElementById('log-body').innerHTML = ''; }

// ── Accessible modal manager ──────────────────────────────────────────────────
// Keep the existing global openModal/closeModal API, but centralise focus,
// keyboard handling, ARIA wiring, and opt-in dirty-form checks in one place.
const modalManager = {
  states: new Map(),
  initialized: false,

  init() {
    document.querySelectorAll('.modal-overlay').forEach(overlay => {
      const dialog = overlay.querySelector('.modal');
      if (!dialog) return;
      dialog.setAttribute('role', 'dialog');
      dialog.setAttribute('aria-modal', 'true');
      dialog.tabIndex = -1;

      const title = dialog.querySelector('[data-modal-title], .modal-header h2');
      if (title) {
        if (!title.id) title.id = `${overlay.id}-title`;
        dialog.setAttribute('aria-labelledby', title.id);
      } else if (!dialog.getAttribute('aria-label')) {
        dialog.setAttribute('aria-label', overlay.id || 'Dialog');
      }
      dialog.querySelectorAll('.btn-close, [data-modal-close]').forEach(button => {
        button.type = 'button';
        if (!button.getAttribute('aria-label')) {
          button.setAttribute('aria-label', `Đóng ${title?.textContent?.trim() || 'cửa sổ'}`);
        }
      });
      overlay.setAttribute('aria-hidden', overlay.classList.contains('open') ? 'false' : 'true');
    });
    document.querySelectorAll('#etsy-bulk-update-live-confirmation, .etsy-bulk-update-field').forEach(control => {
      if (control.dataset.bulkReviewBound === 'true') return;
      control.dataset.bulkReviewBound = 'true';
      control.addEventListener('change', updateBulkEtsyReview);
    });
    if (!this.initialized) {
      document.addEventListener('keydown', event => this.handleKeydown(event));
      this.initialized = true;
    }
  },

  focusable(dialog) {
    return Array.from(dialog.querySelectorAll(
      'a[href], area[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )).filter(element => !element.hidden && element.getAttribute('aria-hidden') !== 'true');
  },

  snapshot(dialog) {
    return Array.from(dialog.querySelectorAll('input, textarea, select')).map(control => ({
      control,
      value: control.type === 'checkbox' || control.type === 'radio' ? control.checked : control.value,
    }));
  },

  hasDirtyCheck(overlay, dialog) {
    return overlay.dataset.unsavedCheck === 'true'
      || dialog.dataset.unsavedCheck === 'true'
      || Boolean(dialog.querySelector('[data-modal-dirty-check]'));
  },

  isDirty(id, overlay, dialog) {
    if (!this.hasDirtyCheck(overlay, dialog)) return false;
    const state = this.states.get(id);
    if (!state?.baseline) return false;
    return state.baseline.some(item => {
      if (!item.control || item.control.isConnected === false) return false;
      const current = item.control.type === 'checkbox' || item.control.type === 'radio'
        ? item.control.checked
        : item.control.value;
      return current !== item.value;
    });
  },

  open(id) {
    this.init();
    const overlay = document.getElementById(id);
    const dialog = overlay?.querySelector('.modal');
    if (!overlay || !dialog) return false;
    const previousFocus = document.activeElement;
    this.states.set(id, {
      opener: previousFocus && typeof previousFocus.focus === 'function' ? previousFocus : null,
      baseline: this.snapshot(dialog),
    });
    overlay.classList.add('open');
    overlay.setAttribute('aria-hidden', 'false');
    const first = dialog.querySelector('[autofocus]') || this.focusable(dialog)[0] || dialog;
    if (typeof first.focus === 'function') first.focus();
    return true;
  },

  close(id, options = {}) {
    const overlay = document.getElementById(id);
    const dialog = overlay?.querySelector('.modal');
    if (!overlay || !dialog) return false;
    if (!options.force && this.isDirty(id, overlay, dialog)) {
      const message = 'Có thay đổi chưa lưu trong cửa sổ này. Đóng sẽ bỏ các thay đổi chưa lưu. Bạn có chắc muốn đóng?';
      if (typeof window.confirm === 'function' && !window.confirm(message)) return false;
    }
    overlay.classList.remove('open');
    overlay.setAttribute('aria-hidden', 'true');
    const state = this.states.get(id);
    this.states.delete(id);
    if (state?.opener && state.opener.isConnected !== false && typeof state.opener.focus === 'function') {
      state.opener.focus();
    }
    return true;
  },

  markClean(id) {
    const overlay = document.getElementById(id);
    const dialog = overlay?.querySelector('.modal');
    const state = this.states.get(id);
    if (dialog && state) state.baseline = this.snapshot(dialog);
  },

  handleKeydown(event) {
    const overlay = document.querySelector('.modal-overlay.open');
    const dialog = overlay?.querySelector('.modal');
    if (!overlay || !dialog) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      closeModal(overlay.id);
      return;
    }
    if (event.key !== 'Tab') return;
    const focusable = this.focusable(dialog);
    if (!focusable.length) {
      event.preventDefault();
      dialog.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) {
      event.preventDefault();
      first.focus();
    }
  },
};

function initModalOverlays() {
  modalManager.init();
  document.querySelectorAll('.modal-overlay').forEach(overlay => {
    if (overlay.dataset.modalOverlayInitialized === 'true') return;
    overlay.dataset.modalOverlayInitialized = 'true';
    overlay.addEventListener('click', event => {
      if (event.target === overlay) closeModal(overlay.id);
    });
  });
}

function openModal(id, options = {}) {
  return modalManager.open(id, options);
}

function closeModal(id, options = {}) {
  return modalManager.close(id, options);
}

function markModalClean(id) {
  modalManager.markClean(id);
}

// ── Social Bulk Poster Modal & Controls ───────────────────────────────────────
function openSocialBulkModal() {
  let minRow = 4;
  let maxRow = 10;
  if (allProducts && allProducts.length > 0) {
    const rows = allProducts.map(p => p.row);
    minRow = Math.min(...rows);
    maxRow = Math.max(...rows);
  }
  
  document.getElementById('bulk-social-start').value = minRow;
  document.getElementById('bulk-social-end').value = maxRow;
  document.getElementById('bulk-social-delay').value = 180;
  
  openModal('social-bulk-modal');
}

async function startBulkSocialPost() {
  const platform = document.getElementById('bulk-social-platform').value;
  const start = parseInt(document.getElementById('bulk-social-start').value) || 4;
  const end = parseInt(document.getElementById('bulk-social-end').value) || 10;
  const delay = parseInt(document.getElementById('bulk-social-delay').value) || 180;
  
  if (start > end) {
    alert("Dòng bắt đầu không được lớn hơn dòng kết thúc!");
    return;
  }
  
  const btn = document.querySelector('#social-bulk-modal .btn-primary');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Đang chạy...';
  
  try {
    const res = await fetch('/api/social/bulk-post', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ platform, start, end, delay })
    });
    const data = await res.json();
    if (data.ok) {
      toast('success', `🚀 Đã kích hoạt đăng bài hàng loạt lên ${platform.toUpperCase()}! Theo dõi tiến trình trong bảng log.`);
      closeModal('social-bulk-modal');
    } else {
      alert("❌ Lỗi: " + data.error);
    }
  } catch(err) {
    alert("❌ Lỗi kết nối server: " + err);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '🚀 Bắt đầu đăng';
  }
}

// ── Toast ──────────────────────────────────────────────────────────────────────
function createToastContainer() {
  const d = document.createElement('div');
  d.className = 'toast-container';
  d.id = 'toast-container';
  document.body.appendChild(d);
}

function toast(type, msg) {
  const container = document.getElementById('toast-container');
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.textContent = msg;
  container.appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

// ── Helpers ────────────────────────────────────────────────────────────────────
function escHtml(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function escJs(str) {
  return String(str || '')
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/\n/g, '\\n')
    .replace(/\r/g, '');
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function updateCount(inputId, countId, max) {
  const len = document.getElementById(inputId).value.length;
  document.getElementById(countId).textContent = `${len}/${max}`;
}

function refreshCard(row, folder) {
  const p = allProducts.find(x => x.row === row);
  if (!p) return;
  const card = document.getElementById(`card-${row}`);
  if (card) card.outerHTML = productCard(p);
}

// ── BATCH ACTIONS ──

function selectedBatchCheckboxes(kind) {
  return BatchSelection.selectedByKind(document.querySelectorAll('.product-cb:checked'), kind);
}

function applyBatchActionVisibility(state) {
  document.querySelectorAll('.local-batch-action').forEach(button => {
    button.style.display = state.showLocalActions ? '' : 'none';
  });
  document.querySelectorAll('.shop-batch-action').forEach(button => {
    button.style.display = state.showShopActions ? '' : 'none';
  });
}

function toggleSelectAll() {
  const isChecked = document.getElementById('cb-select-all').checked;
  const checkboxes = document.querySelectorAll('.product-cb');
  checkboxes.forEach(cb => {
    // Only check visible ones (filtering might hide some)
    const card = cb.closest('.product-card');
    if (card && card.style.display !== 'none') {
      cb.checked = isChecked;
    }
  });
  updateBatchUI();
}

function updateBatchUI(e) {
  rememberCatalogSelections();
  // If an individual checkbox is clicked, uncheck "Select All" if not all are checked
  if (e) {
    const allCb = document.querySelectorAll('.product-cb');
    const checkedCb = document.querySelectorAll('.product-cb:checked');
    document.getElementById('cb-select-all').checked = (allCb.length > 0 && allCb.length === checkedCb.length);
  }

  const checkedCheckboxes = document.querySelectorAll('.product-cb:checked');
  const actionState = BatchSelection.getBatchActionState(currentProductSource, checkedCheckboxes);
  const checkedCount = actionState.total;
  const batchActions = document.getElementById('batch-actions');
  const countLabel = document.getElementById('batch-count-label');
  
  if (checkedCount > 0) {
    batchActions.classList.add('is-visible');
    batchActions.style.setProperty('display', 'flex', 'important');
    countLabel.textContent = actionState.mode === 'mixed'
      ? `${checkedCount} sản phẩm (${actionState.localCount} local, ${actionState.shopCount} Etsy)`
      : `${checkedCount} sản phẩm`;
  } else {
    batchActions.classList.remove('is-visible');
    batchActions.style.setProperty('display', 'none', 'important');
  }
  applyBatchActionVisibility(actionState);
  const draftDeleteButton = document.getElementById('shop-bulk-delete-drafts-btn');
  if (draftDeleteButton) {
    const selectedShop = selectedBatchCheckboxes('shop');
    const draftIds = BatchSelection.selectedDraftListingIds(selectedShop);
    draftDeleteButton.disabled = selectedShop.length === 0 || draftIds.length !== selectedShop.length;
    draftDeleteButton.title = draftDeleteButton.disabled
      ? 'Chỉ có thể xoá khi mọi listing Etsy đã chọn đều ở trạng thái draft'
      : `Xoá ${draftIds.length} Etsy draft đã chọn`;
  }
}

async function batchRegenSEO() {
  const checkboxes = selectedBatchCheckboxes('local');
  if (checkboxes.length === 0) return;
  
  if (!confirm(`Bạn có chắc chắn muốn TẠO LẠI SEO cho ${checkboxes.length} sản phẩm đã chọn?\n\n(Quá trình này sẽ chạy lần lượt từng sản phẩm và ghi đè nội dung cũ)`)) {
    return;
  }
  
  const btn = document.querySelector('#batch-actions button');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Đang chạy...';
  
  for (const cb of checkboxes) {
    const row = parseInt(cb.value);
    const folder = cb.dataset.folder;
    
    // Highlight card
    const card = document.getElementById(`card-${row}`);
    if (card) card.classList.add('running');
    
    toast('info', `🤖 Đang batch SEO cho ${folder}...`);
    try {
      const p = allProducts.find(x => x.row === row);
      const res = await fetch(`/api/products/${row}/regen-seo`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ 
          title: p ? p.title : "", 
          keywords: p ? p.keywords : "", 
          extra: p ? p.extra : "" 
        }),
      });
      const data = await res.json();
      
      if (data.ok && data.seo) {
        toast('success', `✅ Đã tạo xong SEO cho ${folder}`);
        // Optionally update in memory
        if (p) {
          p.title = data.seo.title || p.title;
          p.tags = data.seo.tags || p.tags;
          p.description = data.seo.description || p.description;
          p.needs_seo = false;
        }
      } else {
        toast('error', `❌ Lỗi SEO ${folder}`);
      }
    } catch (e) {
      toast('error', `❌ Lỗi mạng khi gọi SEO ${folder}`);
    }
    
    if (card) card.classList.remove('running');
    cb.checked = false; // Uncheck after done
    updateBatchUI(); // Update UI count
  }
  
  btn.disabled = false;
  btn.innerHTML = '🤖 Tạo SEO Hàng Loạt';
  toast('success', '🎉 Đã hoàn tất Batch SEO!');
  loadProducts(); // Reload to refresh grid
}


// ══════════════════════════════════════════════════════════════════════════════
// ── IMAGE GENERATOR MODAL ────────────────────────────────────────────────────
// ══════════════════════════════════════════════════════════════════════════════

const GEN_STYLES = [
  { icon: '🤍', name: 'Minimalist', desc: 'Clean, airy, premium editorial', val: 'Style: Minimalist. Vibe: Clean, airy, premium editorial. Colors: white, cream, warm grey, black.' },
  { icon: '🌸', name: 'Kawaii',     desc: 'Cute pastel, playful stickers',  val: 'Style: Kawaii. Vibe: Cute pastel, playful. Colors: soft pink, baby blue, pastel yellow, white.' },
  { icon: '🌿', name: 'Boho',       desc: 'Earthy warm, botanical accents', val: 'Style: Boho. Vibe: Earthy warm, botanical. Colors: terracotta, sage green, warm brown, cream.' },
  { icon: '✨', name: 'Dark Luxe',  desc: 'Moody charcoal with gold foil',  val: 'Style: Dark Luxe. Vibe: Moody charcoal with gold. Colors: dark charcoal, black, gold, cream.' },
  { icon: '🌻', name: 'Cottagecore',desc: 'Watercolor florals, linen feel', val: 'Style: Cottagecore. Vibe: Watercolor florals, linen. Colors: sage green, soft peach, cream.' },
  { icon: '💼', name: 'Corporate',  desc: 'Crisp navy, clean professional', val: 'Style: Corporate. Vibe: Crisp, clean professional. Colors: white, light blue, navy, grey.' },
  { icon: '📷', name: 'Retro',      desc: 'Vintage muted, nostalgic warm',  val: 'Style: Retro. Vibe: Vintage muted, nostalgic. Colors: muted orange, tan, brown, warm cream.' },
  { icon: '💿', name: 'Y2K',        desc: 'Chrome silver, holographic pop', val: 'Style: Y2K. Vibe: Chrome silver, holographic. Colors: hot pink, cyan, silver, white.' },
  { icon: '🌱', name: 'Botanical',  desc: 'Deep emerald, tropical lush',    val: 'Style: Botanical. Vibe: Deep emerald, tropical. Colors: deep green, mint, soft white.' },
  { icon: '❄️', name: 'Nordic',     desc: 'Scandinavian ultra-clean',       val: 'Style: Nordic. Vibe: Scandinavian ultra-clean. Colors: icy blue, soft grey, white, birch.' },
];

let _genSelectedStyle = GEN_STYLES[0].val;
let _genUploadedFile  = null;
let _genCurrentRow    = null;
let _genCurrentFolder = null;
let _genRunning       = false;

function _buildGenStyleGrid() {
  const grid = document.getElementById('gen-style-grid');
  if (!grid || grid.children.length > 0) return;
  grid.innerHTML = GEN_STYLES.map((s, i) => `
    <div class="gen-style-card ${i === 0 ? 'active' : ''}" onclick="selectGenStyle(${i})" data-idx="${i}">
      <span class="gen-style-icon">${s.icon}</span>
      <span class="gen-style-name">${s.name}</span>
    </div>`).join('');
}

function selectGenStyle(idx) {
  _genSelectedStyle = GEN_STYLES[idx].val;
  document.querySelectorAll('#gen-style-grid .gen-style-card').forEach((el, i) => {
    el.classList.toggle('active', i === idx);
  });
}

async function openGenModal(row, folder) {
  _genCurrentRow    = row;
  _genCurrentFolder = folder;
  _genUploadedFile  = null;
  _genRunning       = false;

  // Reset UI
  document.getElementById('gen-row').value    = row;
  document.getElementById('gen-folder').value = folder;
  document.getElementById('gen-modal-title').textContent = `🎨 ${folder} — Generate Listing Images`;
  document.getElementById('gen-upload-zone').style.display  = 'block';
  document.getElementById('gen-preview-wrap').style.display = 'none';
  document.getElementById('gen-analyze-status').style.display = 'none';
  document.getElementById('gen-auto-badge').style.display   = 'none';
  document.getElementById('gen-settings-card').style.display = 'none';
  document.getElementById('gen-style-card').style.display   = 'none';
  document.getElementById('gen-btn').style.display          = 'none';
  document.getElementById('gen-progress-section').style.display = 'none';
  document.getElementById('gen-gallery-section').style.display  = 'none';
  document.getElementById('gen-idle-placeholder').style.display = 'flex';
  document.getElementById('gen-pdf-card').style.display        = 'none';
  document.getElementById('gen-pdf-progress').style.display    = 'none';
  document.getElementById('gen-pdf-bar').style.width           = '0%';
  document.getElementById('gen-gallery').innerHTML = '';
  document.getElementById('gen-progress-grid').innerHTML   = '';
  document.getElementById('gen-file-input').value = '';

  _buildGenStyleGrid();

  // Check vertex status
  const svEl = document.getElementById('gen-vertex-status');
  try {
    const svc = await fetch('/api/services').then(r => r.json());
    const ok = svc.mlx_ai || svc.vertex_app; // vertex image studio runs on mlx_ai port
    svEl.textContent = ok ? '● Vertex Online' : '● Vertex Offline';
    svEl.className = `svc ${ok ? 'online' : 'offline'}`;
  } catch { svEl.textContent = '● Checking...'; }

  openModal('gen-modal');

  // Auto-load planner if product has one
  try {
    const info = await fetch(`/api/products/${row}/planner-info`).then(r => r.json());
    if (info.planner_url) {
      const isPdf = info.name?.toLowerCase().endsWith('.pdf');

      if (isPdf) {
        // Show PDF convert card with page count
        try {
          const r2 = await fetch(`/api/products/${row}/pdf-page-count`).then(r => r.json());
          document.getElementById('gen-pdf-pages').textContent = `${r2.pages || '?'} trang`;
        } catch { document.getElementById('gen-pdf-pages').textContent = 'tất cả trang'; }
        document.getElementById('gen-pdf-name').textContent = info.name;
        document.getElementById('gen-pdf-card').style.display = 'block';
        document.getElementById('gen-idle-placeholder').style.display = 'none';
      }

      document.getElementById('gen-analyze-msg').textContent = isPdf ? 'Đang convert PDF → PNG...' : 'Đang tải planner...';
      document.getElementById('gen-analyze-status').style.display = 'block';
      document.getElementById('gen-idle-placeholder').style.display = 'none';
      const pngBlob = await fetch(`/api/products/${row}/planner-png`).then(r => r.blob());
      const fname = (info.name || 'planner').replace(/\.pdf$/i, '.png');
      const file = new File([pngBlob], fname, { type: 'image/png' });
      document.getElementById('gen-auto-badge').style.display = 'inline-block';
      document.getElementById('gen-analyze-status').style.display = 'none';
      await _analyzeFile(file, true);
    }
  } catch { /* no planner file */ }
}

// File input change
document.addEventListener('DOMContentLoaded', () => {
  const fi = document.getElementById('gen-file-input');
  if (fi) fi.addEventListener('change', () => {
    if (fi.files[0]) _analyzeFile(fi.files[0], false);
  });
  // Drag-drop on gen-upload-zone
  const zone = document.getElementById('gen-upload-zone');
  if (zone) {
    zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
    zone.addEventListener('drop', e => {
      e.preventDefault(); zone.classList.remove('dragover');
      if (e.dataTransfer.files[0]) _analyzeFile(e.dataTransfer.files[0], false);
    });
  }
});

async function _analyzeFile(file, isAutoLoad) {
  // If PDF was uploaded manually, convert to PNG first via backend
  if (file.type === 'application/pdf' || file.name?.toLowerCase().endsWith('.pdf')) {
    document.getElementById('gen-analyze-status').style.display = 'block';
    document.getElementById('gen-analyze-msg').textContent = 'Đang convert PDF trang 1 → PNG...';
    document.getElementById('gen-idle-placeholder').style.display = 'none';
    try {
      const form = new FormData();
      form.append('file', file, file.name);
      const pngBlob = await fetch('/api/imagegen/pdf-to-png', { method: 'POST', body: form }).then(r => r.blob());
      file = new File([pngBlob], file.name.replace(/\.pdf$/i, '.png'), { type: 'image/png' });
      toast('info', '📄 PDF đã convert sang PNG');
    } catch (e) {
      toast('error', `PDF convert lỗi: ${e.message}`);
      document.getElementById('gen-analyze-status').style.display = 'none';
      return;
    }
  }

  _genUploadedFile = file;

  // Show preview
  document.getElementById('gen-upload-zone').style.display  = 'none';
  document.getElementById('gen-preview-wrap').style.display = 'block';
  const previewImg = document.getElementById('gen-preview-img');
  previewImg.src = URL.createObjectURL(file);
  document.getElementById('gen-preview-name').textContent = file.name;

  // Show analyze spinner
  document.getElementById('gen-analyze-status').style.display = 'block';
  document.getElementById('gen-idle-placeholder').style.display = 'none';

  try {
    const form = new FormData();
    form.append('file', file, file.name);
    const res  = await fetch('/api/imagegen/analyze', { method: 'POST', body: form });
    const data = await res.json();

    if (data.title) {
      document.getElementById('gen-title').value    = data.title;
      document.getElementById('gen-features').value = data.features || '';
      document.getElementById('gen-emojis').value   = data.suggested_emojis || '✨📝🌟💫🗓️';

      // Auto-select suggested style
      if (data.suggested_style) {
        const idx = GEN_STYLES.findIndex(s => s.name.toLowerCase() === data.suggested_style.toLowerCase());
        if (idx >= 0) selectGenStyle(idx);
      }
    }

    document.getElementById('gen-settings-card').style.display = 'block';
    document.getElementById('gen-style-card').style.display    = 'block';
    document.getElementById('gen-btn').style.display           = 'block';
    toast('success', `✅ Detected: "${data.title || 'Planner'}"`);
  } catch (e) {
    toast('error', `Analyze failed: ${e.message}`);
    document.getElementById('gen-settings-card').style.display = 'block';
    document.getElementById('gen-style-card').style.display    = 'block';
    document.getElementById('gen-btn').style.display           = 'block';
  } finally {
    document.getElementById('gen-analyze-status').style.display = 'none';
  }
}

function resetGenUpload() {
  _genUploadedFile = null;
  document.getElementById('gen-upload-zone').style.display  = 'block';
  document.getElementById('gen-preview-wrap').style.display = 'none';
  document.getElementById('gen-auto-badge').style.display   = 'none';
  document.getElementById('gen-settings-card').style.display = 'none';
  document.getElementById('gen-style-card').style.display   = 'none';
  document.getElementById('gen-btn').style.display          = 'none';
  document.getElementById('gen-idle-placeholder').style.display = 'flex';
  document.getElementById('gen-file-input').value = '';
}

async function startGenerate() {
  if (_genRunning) return;
  if (!_genUploadedFile) { toast('error', 'Chưa upload planner image'); return; }

  _genRunning = true;
  const btn = document.getElementById('gen-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Generating...';

  const title    = document.getElementById('gen-title').value.trim()    || 'Digital Planner';
  const features = document.getElementById('gen-features').value.trim() || '';
  const emojis   = document.getElementById('gen-emojis').value.trim()   || '✨📝🌟💫🗓️';
  const showCanva = document.getElementById('gen-canva-toggle').checked;

  // Show progress section
  document.getElementById('gen-progress-section').style.display = 'block';
  document.getElementById('gen-gallery-section').style.display  = 'none';
  document.getElementById('gen-idle-placeholder').style.display = 'none';
  document.getElementById('gen-progress-bar').style.width = '0%';
  document.getElementById('gen-progress-text').textContent = 'Starting...';

  // Build 10 slot indicators
  const progGrid = document.getElementById('gen-progress-grid');
  progGrid.innerHTML = Array.from({length:10}, (_, i) =>
    `<div class="gen-prog-slot" id="gslot-${i+1}" title="Image ${i+1}">
      <span class="gen-slot-num">${i+1}</span>
      <span class="gen-slot-icon">⏳</span>
    </div>`).join('');

  const payload = { title, features, style: _genSelectedStyle, emojis, show_canva: showCanva, is_bundle: false };

  try {
    const es = new EventSource('/api/imagegen/generate?' + new URLSearchParams({
      _body: JSON.stringify(payload)
    }));

    // Use fetch + ReadableStream for POST SSE
    const response = await fetch('/api/imagegen/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    const genImages = [];

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith('data:')) continue;
        try {
          const d = JSON.parse(line.slice(5).trim());
          if (d.s === 'complete') {
            _onGenComplete(genImages);
            break;
          }
          if (d.s === 'generating') {
            const pct = Math.round(((d.i - 1) / d.n) * 100);
            document.getElementById('gen-progress-bar').style.width = pct + '%';
            document.getElementById('gen-progress-text').textContent = `[${d.i}/${d.n}] ${d.label}...`;
            const slot = document.getElementById(`gslot-${d.i}`);
            if (slot) slot.querySelector('.gen-slot-icon').textContent = '⏳';
          }
          if (d.s === 'done' && d.file) {
            genImages.push(d.file);
            const pct = Math.round((d.i / d.n) * 100);
            document.getElementById('gen-progress-bar').style.width = pct + '%';
            const slot = document.getElementById(`gslot-${d.i}`);
            if (slot) {
              slot.querySelector('.gen-slot-icon').textContent = '✅';
              slot.style.borderColor = 'var(--green)';
            }
            _addGenGalleryItem(d.file, d.label, d.i);
          }
          if (d.s === 'error') {
            const slot = document.getElementById(`gslot-${d.i}`);
            if (slot) slot.querySelector('.gen-slot-icon').textContent = '❌';
          }
        } catch { /* skip malformed */ }
      }
    }

    _onGenComplete(genImages);
  } catch (e) {
    toast('error', `Generate failed: ${e.message}`);
    broadcast && broadcast(`[IMG-GEN] ❌ ${e.message}`);
  } finally {
    _genRunning = false;
    btn.disabled = false;
    btn.innerHTML = '<span class="btn-icon">⚡</span> Generate 10 Listing Images';
  }
}

function _addGenGalleryItem(filename, label, idx) {
  const gallery = document.getElementById('gen-gallery');
  document.getElementById('gen-gallery-section').style.display = 'block';
  const url = `/api/imagegen/output/${filename}?t=${Date.now()}`;
  const div = document.createElement('div');
  div.className = 'gen-img-card';
  div.innerHTML = `
    <img src="${url}" alt="${label}" loading="lazy" onclick="window.open('${url}','_blank')">
    <div class="gen-img-label">${idx}. ${label}</div>`;
  gallery.appendChild(div);
}

function _onGenComplete(images) {
  document.getElementById('gen-progress-bar').style.width = '100%';
  document.getElementById('gen-progress-title').textContent = `✓ Done — ${images.length} images generated`;
  document.getElementById('gen-progress-text').textContent = `Đang lưu vào ${_genCurrentFolder}/images/...`;
  toast('success', `✅ Generated ${images.length} listing images! Đang lưu vào ${_genCurrentFolder}...`);
  // Auto-import immediately into the product folder — no extra click needed
  importGenImages(true);
}

async function importGenImages(auto = false) {
  if (!_genCurrentRow) return;
  const btn = document.getElementById('gen-import-btn');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Saving...'; }
  try {
    const res  = await fetch(`/api/products/${_genCurrentRow}/import-gen-images`, { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      toast('success', `📥 ${data.copied.length} ảnh đã lưu vào ${data.folder}/images/`);
      document.getElementById('gen-progress-text').textContent = `✅ ${data.copied.length} ảnh đã lưu vào ${data.folder}/images/`;
      if (auto) {
        setTimeout(() => { closeModal('gen-modal'); loadProducts(); }, 2500);
      } else {
        closeModal('gen-modal'); loadProducts();
      }
    } else {
      toast('error', 'Import failed');
    }
  } catch (e) {
    toast('error', `Import error: ${e.message}`);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '📥 Import to Product'; }
  }
}

async function convertPdfToImages() {
  if (!_genCurrentRow) return;
  const btn = document.getElementById('gen-pdf-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Converting...';
  document.getElementById('gen-pdf-progress').style.display = 'block';
  document.getElementById('gen-pdf-status').textContent = 'Đang convert PDF...';
  document.getElementById('gen-idle-placeholder').style.display = 'none';

  try {
    // Stream progress via SSE log, trigger conversion
    const res  = await fetch(`/api/products/${_genCurrentRow}/convert-pdf`, { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      document.getElementById('gen-pdf-bar').style.width = '100%';
      document.getElementById('gen-pdf-status').textContent = `✅ ${data.count} ảnh đã lưu vào ${data.folder}/images/`;

      // Show preview in right panel
      document.getElementById('gen-gallery-section').style.display = 'block';
      const gallery = document.getElementById('gen-gallery');
      gallery.innerHTML = '';
      data.saved.forEach((fname, i) => {
        const url = `/files/${data.folder}/images/${fname}?t=${Date.now()}`;
        const div = document.createElement('div');
        div.className = 'gen-img-card';
        div.innerHTML = `
          <img src="${url}" loading="lazy" onclick="window.open('${url}','_blank')">
          <div class="gen-img-label">Page ${i+1}</div>`;
        gallery.appendChild(div);
      });

      toast('success', `📄 ${data.count} trang PDF → ${data.folder}/images/`);
      setTimeout(() => { closeModal('gen-modal'); loadProducts(); }, 3000);
    } else {
      toast('error', 'Convert PDF thất bại');
    }
  } catch (e) {
    toast('error', `Lỗi: ${e.message}`);
    document.getElementById('gen-pdf-status').textContent = `❌ ${e.message}`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = '📄 Convert PDF → Listing Images';
  }
}

// ── Social Post Modal ─────────────────────────────────────────────────────────
let _socialActiveRow = null;
let _socialPosts = {};
let _socialStatuses = {};
let _socialActivePlatform = 'instagram';
let _socialSession = null;

function renderSocialSession(session) {
  _socialSession = session || null;
  const shopEl = document.getElementById('social-session-shop');
  const detailsEl = document.getElementById('social-session-details');
  const authEl = document.getElementById('social-session-auth-note');
  if (!shopEl || !detailsEl || !authEl) return;
  if (!session) {
    shopEl.textContent = 'Không đọc được session social';
    detailsEl.textContent = '';
    authEl.textContent = '';
    return;
  }
  shopEl.textContent = `Session riêng: ${session.shop_name} (${session.shop_id})`;
  detailsEl.textContent = `Chrome profile: ${session.profile_dir} · Debug port: ${session.debug_port} · ${session.browser_ready ? 'Chrome đang sẵn sàng' : 'Chrome chưa mở'}`;
  authEl.textContent = 'Trạng thái đăng nhập: chưa xác nhận — mở từng nền tảng để đăng nhập một lần.';
}

async function loadSocialSession() {
  try {
    const response = await fetch('/api/social/session');
    const data = await response.json();
    renderSocialSession(data.ok ? data.session : null);
  } catch (error) {
    renderSocialSession(null);
  }
}

async function openSocialLoginBrowser() {
  const platform = _socialActivePlatform;
  const button = document.getElementById('btn-open-social-login');
  if (!platform || !button) return;
  button.disabled = true;
  button.innerHTML = '<span class="spinner"></span> Đang mở...';
  try {
    const response = await fetch('/api/social/session/open', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ platform })
    });
    const data = await response.json();
    if (!data.ok) throw new Error(data.error || 'Không mở được Chrome social');
    renderSocialSession(data.session);
    toast('info', `🔐 Đã mở ${platform.toUpperCase()} trong session riêng của ${data.session.shop_name}. Hãy đăng nhập nếu cần.`);
  } catch (error) {
    toast('error', `Không mở được session social: ${error.message}`);
  } finally {
    button.disabled = false;
    button.innerHTML = '🔐 Mở / đăng nhập nền tảng này';
  }
}

async function openSocialModal(row, folder) {
  _socialActiveRow = row;
  document.getElementById('social-row').value = row;
  document.getElementById('social-folder').value = folder;
  document.getElementById('social-modal-title').textContent = `Quản lý & Chia sẻ Social — ${folder}`;
  
  // Clear preview and active states
  document.getElementById('social-etsy-url').value = '';
  document.getElementById('social-caption-text').value = '';
  _socialStatuses = {};
  loadSocialSession();
  
  toast('info', '🤖 Đang lấy thông tin bài đăng social...');
  try {
    const res = await fetch(`/api/products/${row}/social-posts`);
    const data = await res.json();
    if (data.ok) {
      _socialPosts = data.posts || {};
      _socialStatuses = data.social_statuses || {};
      document.getElementById('social-etsy-url').value = data.etsy_url || '';
      renderSocialTabsStatus();
      renderSocialPostsSummary();
      
      // Select first platform by default (Instagram)
      switchSocialTab('instagram');
      openModal('social-modal');
    } else {
      toast('warning', data.error || 'Lỗi lấy bài viết social');
    }
  } catch (err) {
    toast('error', `Lỗi kết nối: ${err.message}`);
  }
}

async function saveSocialUrl() {
  if (!_socialActiveRow) return;
  const url = document.getElementById('social-etsy-url').value.trim();
  try {
    const res = await fetch(`/api/products/${_socialActiveRow}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ etsy_url: url })
    });
    const data = await res.json();
  if (data.ok) {
      // Cập nhật local state
      const p = allProducts.find(x => x.row === _socialActiveRow);
      if (p) p.etsy_url = url;
      toast('success', '💾 Đã lưu Etsy URL thành công!');
      
      // Tải lại bài đăng social để cập nhật url mới trong caption
      const resPosts = await fetch(`/api/products/${_socialActiveRow}/social-posts`);
      const postsData = await resPosts.json();
      if (postsData.ok) {
        _socialPosts = postsData.posts || {};
        // Reload active tab
        const activeTabBtn = document.querySelector('.social-tab-btn.active');
        if (activeTabBtn) {
          const platform = activeTabBtn.id.replace('tab-', '');
          switchSocialTab(platform);
        }
      }
      loadProducts();
      renderSocialTabsStatus();
      renderSocialPostsSummary();
    } else {
      toast('error', 'Lỗi lưu Etsy URL');
    }
  } catch (err) {
    toast('error', `Lỗi: ${err.message}`);
  }
}

function switchSocialTab(platform) {
  _socialActivePlatform = platform;
  
  // Toggle active class on tab buttons
  document.querySelectorAll('.social-tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.id === `tab-${platform}`);
  });
  
  // Set tab title
  const titles = {
    instagram: '📸 Instagram Post Caption',
    pinterest: '📌 Pinterest Pin Description',
    facebook: '👥 Facebook Post Caption',
    twitter: '𝕏 Twitter / X Post',
    medium: '✍️ Medium Research Article Markdown'
  };
  document.getElementById('social-tab-title').textContent = titles[platform] || 'Caption';

  const tipEl = document.getElementById('social-share-tips');
  if (tipEl && platform === 'medium') {
    tipEl.textContent = '💡 Tip: Đây là research note/how-to theo ngữ cảnh sản phẩm — kiểm tra lại các gợi ý thực tế trước khi đăng lên Medium.';
  } else if (tipEl) {
    tipEl.textContent = '💡 Tip: Đăng tải ảnh listing từ thư mục sản phẩm và dán phần caption này vào.';
  }
  
  // Show caption text
  const caption = _socialPosts[platform] || '';
  document.getElementById('social-caption-text').value = caption;
  renderActiveSocialStatus();
  renderSocialPostsSummary();
  
  // Every visible platform is handled by the exact shop-specific browser.
  const autoBtn = document.getElementById('btn-auto-post');
  if (autoBtn) {
    autoBtn.disabled = false;
    autoBtn.innerHTML = '⚡ Auto Post';
    autoBtn.style.background = 'linear-gradient(135deg, #10b981, #059669)';
    autoBtn.style.borderColor = '#059669';
    autoBtn.style.color = '#fff';
  }
}

function renderSocialPostsSummary() {
  const summaryEl = document.getElementById('social-post-summary');
  if (!summaryEl) return;
  const normalizedStatuses = normalizeSocialStatusSummary(_socialStatuses);
  const rows = normalizedStatuses
    .filter(({ record }) => record.status === 'posted')
    .map(({ platform, record, meta }) => {
      const postedAt = socialStatusDateLabel(record.posted_at);
      const safeUrl = safeExternalSocialUrl(record.url);
      const href = safeUrl
        ? `<a href="${escHtml(safeUrl)}" target="_blank" rel="noopener">Mở bài đăng</a>`
        : '';
      const suffix = href ? ` · ${href}` : '';
      return `<div class="social-post-summary-item">${meta.icon} ${meta.label}: ${postedAt}${suffix}</div>`;
    });

  const posted = normalizedStatuses.some(({ record }) => record.status === 'posted');
  summaryEl.className = `social-post-summary ${posted ? 'has-posted' : 'is-pending'}`;
  if (!rows.length) {
    summaryEl.innerHTML = '<div class="social-post-summary-empty-item">Chưa có kênh nào có lịch sử đăng.</div>';
    return;
  }
  summaryEl.innerHTML = rows.join('');
}

function copySocialCaption() {
  const text = document.getElementById('social-caption-text').value;
  if (!text) {
    toast('warning', 'Không có nội dung để copy!');
    return;
  }
  navigator.clipboard.writeText(text).then(() => {
    toast('success', '📋 Đã copy caption vào bộ nhớ tạm!');
  }).catch(err => {
    toast('error', `Không thể copy: ${err}`);
  });
}

function renderActiveSocialStatus() {
  const statusEl = document.getElementById('social-post-status');
  const record = _socialStatuses?.[_socialActivePlatform];
  if (!statusEl) return;
  if (!record || record.status !== 'posted') {
    statusEl.className = 'social-post-status is-pending';
    statusEl.textContent = 'Chưa có lịch sử đăng trên kênh này';
    return;
  }
  const timestamp = record.posted_at
    ? socialStatusDateLabel(record.posted_at)
    : 'không rõ thời gian';
  const safeUrl = safeExternalSocialUrl(record.url);
  statusEl.className = 'social-post-status is-posted';
  statusEl.innerHTML = safeUrl
    ? `✅ Đã đăng ${timestamp} · <a href="${escHtml(safeUrl)}" target="_blank" rel="noopener">Mở bài đăng ↗</a>`
    : `✅ Đã đăng ${timestamp}`;
}

async function waitForSocialPostStatus(row, platform, previousPostedAt) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 3000));
    if (_socialActiveRow !== row) return;
    try {
      const response = await fetch(`/api/products/${row}/social-posts`);
      const data = await response.json();
      const nextRecord = data?.social_statuses?.[platform];
      if (data.ok && nextRecord?.status === 'posted'
          && (nextRecord.posted_at !== previousPostedAt
            || nextRecord.url !== (_socialStatuses?.[platform]?.url || ''))) {
        _socialStatuses = data.social_statuses || {};
        renderActiveSocialStatus();
        renderSocialTabsStatus();
        renderSocialPostsSummary();
        await loadProducts();
        const button = document.getElementById('btn-auto-post');
        if (button && _socialActivePlatform === platform) {
          button.disabled = false;
          button.innerHTML = '✅ Đã đăng · Post lại';
        }
        toast('success', `✅ Đã lưu trạng thái ${platform.toUpperCase()} cho sản phẩm.`);
        return;
      }
    } catch (_) {
      // Live Logs remains the primary error surface while the worker is running.
    }
  }
}

async function autoPostSocial() {
  const row = _socialActiveRow;
  const platform = _socialActivePlatform;
  if (!row || !platform) return;
  
  const btn = document.getElementById('btn-auto-post');
  if (!btn) return;
  
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Đang đăng...';
  const previousPostedAt = _socialStatuses?.[platform]?.posted_at || '';
  
  toast('info', `⏳ Đang khởi động tiến trình đăng tự động lên ${platform.toUpperCase()}...`);
  
  try {
    const res = await fetch(`/api/products/${row}/post-social`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ platform })
    });
    const data = await res.json();
    if (data.ok) {
      toast('success', `🚀 Đang đăng bài lên ${platform.toUpperCase()}! Hãy theo dõi quá trình trong tab Live Logs.`);
      btn.innerHTML = '⚡ Đang chạy...';
      waitForSocialPostStatus(row, platform, previousPostedAt);
      
      // Auto scroll Live Logs container to show real-time progress
      const logBody = document.getElementById('log-body');
      if (logBody) {
        logBody.scrollTop = logBody.scrollHeight;
      }
      
      // Re-enable button after 8 seconds to allow retry or checking other pages
      setTimeout(() => {
        if (btn.innerHTML === '⚡ Đang chạy...') {
          btn.disabled = false;
          btn.innerHTML = '⚡ Auto Post';
        }
      }, 8000);
    } else {
      toast('error', `❌ Lỗi: ${data.error || 'Đăng thất bại'}`);
      btn.disabled = false;
      btn.innerHTML = '⚡ Auto Post';
    }
  } catch (e) {
    toast('error', `❌ Lỗi kết nối: ${e.message}`);
    btn.disabled = false;
    btn.innerHTML = '⚡ Auto Post';
  }
}

async function autoPostSocialAll() {
  const row = _socialActiveRow;
  if (!row) return;

  const btn = document.getElementById('btn-post-all');
  if (!btn) return;

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Đang đăng...';

  toast('info', '⏳ Đang khởi động tiến trình đăng tự động lên TẤT CẢ nền tảng...');

  try {
    const res = await fetch(`/api/products/${row}/post-social-all`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    const data = await res.json();
    if (data.ok) {
      toast('success', `🚀 Đang đăng bài lên TẤT CẢ nền tảng! Hãy theo dõi quá trình trong tab Live Logs.`);
      btn.innerHTML = '⚡ Đang chạy...';

      const logBody = document.getElementById('log-body');
      if (logBody) {
        logBody.scrollTop = logBody.scrollHeight;
      }

      setTimeout(() => {
        if (btn.innerHTML === '⚡ Đang chạy...') {
          btn.disabled = false;
          btn.innerHTML = '🚀 Post 1 lần tất cả';
        }
      }, 15000);
    } else {
      toast('error', `❌ Lỗi: ${data.error || 'Đăng thất bại'}`);
      btn.disabled = false;
      btn.innerHTML = '🚀 Post 1 lần tất cả';
    }
  } catch (e) {
    toast('error', `❌ Lỗi kết nối: ${e.message}`);
    btn.disabled = false;
    btn.innerHTML = '🚀 Post 1 lần tất cả';
  }
}

async function createNewFolder() {
  toast('info', '📁 Đang tạo folder sản phẩm mới...');
  try {
    const res = await fetch('/api/products', { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      toast('success', `✅ Đã tạo thành công: ${data.folder} (hàng ${data.row})`);
      await loadProducts(); // reload UI to display the new folder card
    } else {
      toast('error', `Lỗi: ${data.message || 'Không thể tạo folder'}`);
    }
  } catch (e) {
    toast('error', `Lỗi kết nối server: ${e.message}`);
  }
}


// ── Image Factory Import ──────────────────────────────────────────────────────

let _factoryFolders = [];
let _factorySelected = new Set();
let _factoryFilter = 'all';
let _factoryShopId = null;

async function openFactoryImport() {
  _factorySelected.clear();
  _factoryShopId = null;
  setFactoryFilter('all', false);
  document.getElementById('factory-folder-grid').style.display = 'none';
  document.getElementById('factory-empty').style.display = 'none';
  document.getElementById('factory-result').style.display = 'none';
  document.getElementById('factory-scope-note').style.display = 'none';
  document.getElementById('factory-loading').style.display = 'block';
  document.getElementById('factory-import-btn').disabled = true;
  document.getElementById('factory-select-label').textContent = 'Chọn các folder muốn import';
  openModal('factory-modal');
  await scanFactory();
}

async function scanFactory(options = {}) {
  document.getElementById('factory-loading').style.display = 'block';
  document.getElementById('factory-folder-grid').style.display = 'none';
  document.getElementById('factory-empty').style.display = 'none';
  if (!options.preserveResult) document.getElementById('factory-result').style.display = 'none';
  _factorySelected.clear();
  _factoryShopId = null;
  updateFactoryImportBtn();

  try {
    const res  = await fetch('/api/image-factory/scan');
    const data = await res.json();
    document.getElementById('factory-loading').style.display = 'none';

    if (!data.ok || !data.shop_id) {
      document.getElementById('factory-empty').style.display = 'block';
      document.getElementById('factory-empty').innerHTML =
        `<div style="font-size:2rem;margin-bottom:10px;">⚠️</div><div>${escHtml(data.error || 'Không thể xác định active shop cho Image Factory.')}</div>`;
      return;
    }

    _factoryShopId = data.shop_id;
    const sourceName = data.source_label || data.shop_name || data.shop_id || 'Active shop';
    document.getElementById('factory-path-label').textContent = `📂 Nguồn import (${sourceName}): ${data.factory_path || ''}`;
    _factoryFolders = data.folders || [];
    const scanSummary = data.scan_summary || {};
    const eligibleCount = Number.isFinite(Number(scanSummary.eligible_intake_folders))
      ? Number(scanSummary.eligible_intake_folders)
      : _factoryFolders.length;
    const excludedProductCount = Number.isFinite(Number(scanSummary.excluded_catalog_product_folders))
      ? Number(scanSummary.excluded_catalog_product_folders)
      : 0;
    const scopeNote = document.getElementById('factory-scope-note');
    scopeNote.textContent = `Chỉ hiển thị ${eligibleCount} folder intake có đủ ảnh gallery và ZIP nguồn. ${excludedProductCount} folder catalog product-* được loại để tránh import trùng.`;
    scopeNote.style.display = 'block';
    updateFactoryFilterCounts();

    if (_factoryFolders.length === 0) {
      document.getElementById('factory-empty').style.display = 'block';
      return;
    }

    renderFactoryFolders();
    document.getElementById('factory-folder-grid').style.display = 'grid';
  } catch (e) {
    _factoryShopId = null;
    document.getElementById('factory-loading').style.display = 'none';
    document.getElementById('factory-empty').style.display = 'block';
    document.getElementById('factory-empty').innerHTML =
      `<div style="font-size:2rem;margin-bottom:10px;">❌</div><div>Lỗi kết nối: ${escHtml(e.message)}</div>`;
  }
}

function renderFactoryFolders() {
  const grid = document.getElementById('factory-folder-grid');
  grid.innerHTML = '';
  grid.style.display = 'grid';
  grid.style.gridTemplateColumns = 'repeat(auto-fill, minmax(220px, 1fr))';
  grid.style.gap = '14px';
  grid.style.maxHeight = 'min(62vh, 640px)';
  grid.style.overflowY = 'auto';
  grid.style.paddingRight = '4px';
  const visibleFolders = _factoryFolders.filter(folder =>
    _factoryFilter === 'all' ||
    (_factoryFilter === 'imported' && folder.already_imported) ||
    (_factoryFilter === 'pending' && !folder.already_imported)
  );
  visibleFolders.forEach(folder => {
    const card = document.createElement('div');
    card.className = 'factory-card' + (folder.already_imported ? ' already' : '');
    card.dataset.name = folder.name;
    card.title = folder.name;
    card.style.cssText = [
      'background: var(--bg3)',
      'border: 2px solid var(--border)',
      'border-radius: 10px',
      'overflow: hidden',
      'cursor: pointer',
      'position: relative',
      'min-width: 0',
      'min-height: 250px',
      'display: flex',
      'flex-direction: column'
    ].join(';');
    if (!folder.already_imported) {
      card.onclick = () => toggleFactoryCard(folder.name, card);
    } else {
      card.style.opacity = '0.55';
      card.style.cursor = 'not-allowed';
    }
    const safeName = escHtml(folder.name);
    const safeKeyword = escHtml(folder.keyword_guess || folder.name);
    const safeFirstFileName = escHtml((folder.file_names && folder.file_names[0]) || '');
    const thumbHtml = folder.thumb
      ? `<div class="factory-card-thumb" style="width:100%;height:150px;background:var(--bg2);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:center;flex-shrink:0;"><img src="${folder.thumb}" alt="${safeName}" loading="lazy" style="width:100%;height:100%;object-fit:cover;display:block;" onerror="this.parentElement.innerHTML='<span style=&quot;font-size:2rem;color:var(--text3);&quot;>📁</span>';"></div>`
      : `<div class="factory-card-thumb is-missing" style="width:100%;height:150px;background:var(--bg2);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:2rem;color:var(--text3);flex-shrink:0;">📁</div>`;
    const alreadyBadge = folder.already_imported ? `<div class="factory-card-already-badge" style="position:absolute;top:6px;left:6px;background:rgba(52,211,153,0.9);color:#000;font-size:9px;font-weight:700;padding:2px 6px;border-radius:10px;z-index:2;">✓ Đã import${folder.imported_folder ? ' → ' + escHtml(folder.imported_folder) : ''}</div>` : `<div class="factory-card-pending-badge">Chưa import</div>`;
    const firstFile = folder.file_names && folder.file_names.length > 0
      ? `<div class="factory-card-file" style="font-size:10px;color:var(--text3);margin-top:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${safeFirstFileName}">${safeFirstFileName}${folder.file_names.length > 1 ? ' +' + (folder.file_names.length - 1) : ''}</div>`
      : '';
    card.innerHTML = `${thumbHtml}${alreadyBadge}<div class="factory-card-check" style="position:absolute;top:6px;right:6px;width:22px;height:22px;border-radius:50%;background:#22d3ee;color:#000;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;opacity:0;transition:opacity .15s;">✓</div>
      <div class="factory-card-body" style="padding:10px 12px 11px;min-height:84px;">
        <div class="factory-card-name" style="font-size:13px;font-weight:700;color:var(--text1);line-height:1.25;margin-bottom:5px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;">${safeKeyword}</div>
        <div class="factory-card-keyword" style="color:var(--accent2);font-size:11px;line-height:1.25;margin-bottom:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">Nguồn: ${safeName}</div>
        <div class="factory-card-meta" style="display:flex;gap:10px;flex-wrap:wrap;font-size:11px;color:var(--text2);"><span>🖼 ${folder.image_count} ảnh</span><span>📄 ${folder.file_count} file</span></div>
        ${firstFile}
      </div>`;
    grid.appendChild(card);
  });
  const empty = document.getElementById('factory-empty');
  if (visibleFolders.length === 0 && _factoryFolders.length > 0) {
    empty.innerHTML = '<div style="font-size:2rem;margin-bottom:10px;">📭</div><div>Không có sản phẩm ở bộ lọc này.</div>';
    empty.style.display = 'block';
    grid.style.display = 'none';
  } else {
    empty.style.display = 'none';
  }
}

function updateFactoryFilterCounts() {
  const imported = _factoryFolders.filter(folder => folder.already_imported).length;
  document.getElementById('factory-count-all').textContent = _factoryFolders.length;
  document.getElementById('factory-count-pending').textContent = _factoryFolders.length - imported;
  document.getElementById('factory-count-imported').textContent = imported;
}

function setFactoryFilter(filter, shouldRender = true) {
  _factoryFilter = filter;
  document.querySelectorAll('.factory-filter').forEach(button => {
    button.classList.toggle('active', button.dataset.filter === filter);
  });
  if (shouldRender) renderFactoryFolders();
}

function toggleFactoryCard(name, card) {
  const check = card.querySelector('.factory-card-check');
  if (_factorySelected.has(name)) {
    _factorySelected.delete(name);
    card.classList.remove('selected');
    card.style.borderColor = 'var(--border)';
    card.style.boxShadow = 'none';
    if (check) check.style.opacity = '0';
  } else {
    _factorySelected.add(name);
    card.classList.add('selected');
    card.style.borderColor = '#22d3ee';
    card.style.boxShadow = '0 0 12px rgba(34,211,238,0.25)';
    if (check) check.style.opacity = '1';
  }
  updateFactoryImportBtn();
}

function updateFactoryImportBtn() {
  const btn = document.getElementById('factory-import-btn');
  const label = document.getElementById('factory-select-label');
  const count = _factorySelected.size;
  btn.disabled = count === 0;
  btn.textContent = count > 0 ? ('🖼 Import ' + count + ' folder đã chọn') : '🖼 Import đã chọn';
  label.textContent = count > 0 ? ('Đã chọn ' + count + ' folder') : 'Chọn các folder muốn import';
}

async function doFactoryImport() {
  if (_factorySelected.size === 0) return;
  if (!_factoryShopId) {
    toast('error', '❌ Hãy quét lại nguồn Image Factory của active shop.');
    return;
  }
  const autoSeo = document.getElementById('factory-auto-seo').checked;
  const folders = Array.from(_factorySelected);
  const btn     = document.getElementById('factory-import-btn');
  const resultEl = document.getElementById('factory-result');

  btn.disabled = true;
  btn.textContent = '⏳ Đang import...';
  resultEl.style.display = 'none';

  try {
    const res  = await fetch('/api/image-factory/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ shop_id: _factoryShopId, folders, auto_seo: autoSeo }),
    });
    const data = await res.json();

    if (data.ok) {
      const rows = (data.results || []).map(r => {
        const isSuccess = Boolean(r.ok);
        const isSkipped = Boolean(r.already_imported);
        const icon = isSuccess ? '✅' : isSkipped ? '⏭️' : '❌';
        const color = isSuccess ? 'var(--green)' : isSkipped ? 'var(--yellow)' : 'var(--red)';
        const details = isSuccess
          ? `🖼 ${Number(r.images_copied || 0)} · 📄 ${Number(r.files_copied || 0)}`
          : escHtml(r.error || (isSkipped ? 'Đã import trước đó' : 'Import thất bại'));
        return `<div style="display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--border);">
          <span style="color:${color};">${icon}</span>
          <span style="font-weight:600;color:var(--accent2);">${escHtml(r.folder || r.source || '')}</span>
          <span style="color:var(--text3);font-size:11px;">${r.source ? `← ${escHtml(r.source)}` : ''}</span>
          <span style="margin-left:auto;font-size:11px;color:${color};">${details}</span>
        </div>`;
      }).join('');
      const imported = Number(data.imported || 0);
      const skipped = Number(data.skipped || 0);
      const failed = Number(data.failed || 0);
      const summary = imported > 0
        ? `✅ Import thành công ${imported} sản phẩm${skipped || failed ? ` · bỏ qua ${skipped} · lỗi ${failed}` : ''}`
        : `⚠️ Không có sản phẩm mới được import${skipped ? ` · ${skipped} sản phẩm đã có` : ''}${failed ? ` · ${failed} lỗi` : ''}`;
      const summaryColor = imported > 0 ? 'var(--green)' : failed > 0 ? 'var(--red)' : 'var(--yellow)';
      const seoNote = autoSeo && imported > 0
        ? `<div style="margin-top:10px;padding:8px 12px;background:rgba(251,191,36,0.08);border-radius:6px;font-size:11px;color:var(--yellow);">🤖 Đang gen SEO trong nền cho ${imported} sản phẩm... Xem Live Logs.</div>`
        : '';
      resultEl.style.background = imported > 0 ? 'rgba(52,211,153,0.06)' : 'rgba(251,191,36,0.06)';
      resultEl.style.border = imported > 0 ? '1px solid rgba(52,211,153,0.2)' : '1px solid rgba(251,191,36,0.2)';
      resultEl.innerHTML = `<div style="font-weight:600;color:${summaryColor};margin-bottom:10px;">${summary}</div>${rows}${seoNote}`;
      resultEl.style.display = 'block';
      if (imported > 0) {
        toast(failed > 0 ? 'warning' : 'success', summary);
        await loadProducts();
        setProductSource('local');
        _factorySelected.clear();
        updateFactoryImportBtn();
        await scanFactory({ preserveResult: true });
      } else {
        toast(failed > 0 ? 'error' : 'info', summary);
        btn.disabled = false;
        updateFactoryImportBtn();
      }
    } else {
      resultEl.style.background = 'rgba(248,113,113,0.06)';
      resultEl.style.border = '1px solid rgba(248,113,113,0.2)';
      resultEl.innerHTML = `<span style="color:var(--red);">❌ Lỗi: ${escHtml(data.error || 'Import thất bại')}</span>`;
      resultEl.style.display = 'block';
      btn.disabled = false;
      updateFactoryImportBtn();
    }
  } catch (e) {
    resultEl.style.background = 'rgba(248,113,113,0.06)';
    resultEl.style.border = '1px solid rgba(248,113,113,0.2)';
    resultEl.innerHTML = `<span style="color:var(--red);">❌ Lỗi kết nối: ${escHtml(e.message)}</span>`;
    resultEl.style.display = 'block';
    btn.disabled = false;
    updateFactoryImportBtn();
  }
}
