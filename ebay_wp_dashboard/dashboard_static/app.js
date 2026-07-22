let allProducts = [];
let allPosts = [];
let currentSites = {};
let activeTab = 'products';
let selectedRows = new Set();

function toast(type, msg) {
  const c = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  c.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

function switchTab(tab) {
  activeTab = tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.getElementById(`tab-${tab}`).classList.add('active');
  document.getElementById('header-actions-products').classList.toggle('hidden', tab !== 'products');
  document.getElementById('header-actions-posts').classList.toggle('hidden', tab !== 'posts');
  if (tab === 'posts') loadPosts();
  if (tab === 'settings') loadSettingsForm();
}

async function initSiteSwitcher() {
  const sel = document.getElementById('site-switcher');
  try {
    const res = await fetch('/api/sites');
    const data = await res.json();
    currentSites = data.sites.reduce((a, s) => ({ ...a, [s.id]: s }), {});
    sel.innerHTML = '';
    data.sites.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.id;
      opt.textContent = `${s.emoji || '🏬'} ${s.name}`;
      sel.appendChild(opt);
    });
    sel.value = data.active;
    sel.onchange = async () => {
      await fetch('/api/set-site', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ site_id: sel.value }),
      });
      location.reload();
    };
  } catch (e) {
    toast('error', 'Không tải được sites: ' + e.message);
  }
}

async function loadProducts() {
  try {
    const res = await fetch('/api/products');
    const data = await res.json();
    allProducts = data.products;
    updateProductStats(data.stats);
    renderProducts();
  } catch (e) {
    toast('error', 'Lỗi tải products: ' + e.message);
  }
}

function updateProductStats(s) {
  document.getElementById('stat-total').textContent = s.total;
  document.getElementById('stat-ebay').textContent = s.ebay_active;
  document.getElementById('stat-woo').textContent = s.woo_published;
  document.getElementById('stat-pending').textContent = s.pending;
  document.getElementById('stat-errors').textContent = s.errors;
}

function statusBadge(label, status) {
  const cls = ['active', 'draft', 'published', 'publish'].includes(status) ? status
    : status === 'pending' ? 'pending' : status === 'error' ? 'error' : 'draft';
  return `<span class="badge ${cls}">${label}: ${status}</span>`;
}

function renderProducts() {
  const grid = document.getElementById('product-grid');
  const q = (document.getElementById('search-products').value || '').toLowerCase();
  const pf = document.getElementById('filter-platform').value;
  let items = allProducts.filter(p => {
    const hay = `${p.title} ${p.folder} ${p.tags}`.toLowerCase();
    if (q && !hay.includes(q)) return false;
    if (pf === 'ebay_pending' && p.ebay_status !== 'pending') return false;
    if (pf === 'woo_pending' && p.woo_status !== 'pending') return false;
    if (pf === 'errors' && p.ebay_status !== 'error' && p.woo_status !== 'error') return false;
    return true;
  });
  grid.innerHTML = items.map(p => `
    <div class="product-card ${selectedRows.has(p.row) ? 'selected' : ''}" data-row="${p.row}">
      <div class="card-thumb-wrap">
        <input type="checkbox" class="card-check" ${selectedRows.has(p.row) ? 'checked' : ''}
          onclick="event.stopPropagation(); toggleSelect(${p.row})">
        ${p.thumb
          ? `<img class="card-thumb" src="${p.thumb}" alt="" loading="lazy">`
          : `<div class="card-thumb-placeholder">📦</div>`}
      </div>
      <div class="card-body">
        <div class="card-title">${esc(p.title)}</div>
        <div class="card-folder">${esc(p.folder)} · $${p.price}</div>
        <div class="card-badges">
          ${statusBadge('eBay', p.ebay_status)}
          ${statusBadge('Woo', p.woo_status)}
        </div>
        <div class="card-actions">
          <button class="btn btn-sm btn-ghost" onclick="openProductModal(${p.row})">✏️</button>
          <button class="btn btn-sm btn-warning" onclick="postEbay(${p.row})">eBay</button>
          <button class="btn btn-sm btn-success" onclick="postWoo(${p.row})">Woo</button>
        </div>
      </div>
    </div>
  `).join('');
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

function filterProducts() { renderProducts(); }

function toggleSelect(row) {
  if (selectedRows.has(row)) selectedRows.delete(row);
  else selectedRows.add(row);
  renderProducts();
}

function toggleSelectAll() {
  const cb = document.getElementById('cb-select-all');
  if (cb.checked) allProducts.forEach(p => selectedRows.add(p.row));
  else selectedRows.clear();
  renderProducts();
}

async function openProductModal(row) {
  const res = await fetch(`/api/products/${row}`);
  const p = await res.json();
  document.getElementById('edit-row').value = row;
  document.getElementById('edit-title').value = p.title || '';
  document.getElementById('edit-description').value = p.description || '';
  document.getElementById('edit-price').value = p.price || '';
  document.getElementById('edit-sku').value = p.sku || '';
  document.getElementById('edit-tags').value = p.tags || '';
  document.getElementById('edit-ebay-status').textContent = `eBay: ${p.ebay_status}`;
  document.getElementById('edit-ebay-status').className = `badge ${p.ebay_status}`;
  document.getElementById('edit-woo-status').textContent = `Woo: ${p.woo_status}`;
  document.getElementById('edit-woo-status').className = `badge ${p.woo_status}`;
  document.getElementById('product-modal').classList.remove('hidden');
}

function closeProductModal() {
  document.getElementById('product-modal').classList.add('hidden');
}

async function saveProduct() {
  const row = document.getElementById('edit-row').value;
  await fetch(`/api/products/${row}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      title: document.getElementById('edit-title').value,
      description: document.getElementById('edit-description').value,
      price: parseFloat(document.getElementById('edit-price').value) || 4.99,
      sku: document.getElementById('edit-sku').value,
      tags: document.getElementById('edit-tags').value,
    }),
  });
  toast('success', 'Đã lưu product');
  closeProductModal();
  loadProducts();
}

async function postEbay(row) {
  const res = await fetch(`/api/products/${row}/post-ebay`, { method: 'POST' });
  const data = await res.json();
  toast(data.ok ? 'success' : 'error', data.message || data.error || 'Queued eBay post');
  loadProducts();
}
async function postWoo(row) {
  const res = await fetch(`/api/products/${row}/post-woo`, { method: 'POST' });
  const data = await res.json();
  toast(data.ok ? 'success' : 'error', data.message || data.error || 'Queued Woo post');
  loadProducts();
}
function postEbayFromModal() { postEbay(document.getElementById('edit-row').value); }
function postWooFromModal() { postWoo(document.getElementById('edit-row').value); }
async function pushEbayFromModal() {
  const row = document.getElementById('edit-row').value;
  await fetch(`/api/products/${row}/push-ebay`, { method: 'POST' });
  toast('success', 'Pushing eBay update...');
}

async function syncEbay() {
  const res = await fetch('/api/ebay/sync', { method: 'POST' });
  const data = await res.json();
  toast(data.ok ? 'success' : 'error', data.message || data.error || 'Sync started');
}
async function syncWoo() {
  const res = await fetch('/api/woo/sync', { method: 'POST' });
  const data = await res.json();
  toast(data.ok ? 'success' : 'error', `Synced ${data.matched || 0} products`);
  loadProducts();
}
async function runAllPendingEbay() {
  const res = await fetch('/api/run-all-pending-ebay', { method: 'POST' });
  const data = await res.json();
  toast('success', `Queued ${data.queued} eBay posts`);
}
async function runAllPendingWoo() {
  const res = await fetch('/api/run-all-pending-woo', { method: 'POST' });
  const data = await res.json();
  toast('success', `Queued ${data.queued} Woo posts`);
}
async function stopAll() {
  await fetch('/api/stop-all', { method: 'POST' });
  toast('success', 'Stopped all');
}
async function importFromEtsy() {
  const site = document.getElementById('site-switcher').value;
  const etsyShop = prompt('Etsy shop ID to import from (e.g. daisyflowdigital):', site);
  if (!etsyShop) return;
  const res = await fetch('/api/import-from-etsy', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ etsy_shop: etsyShop }),
  });
  const data = await res.json();
  toast(data.ok ? 'success' : 'error', `Imported ${data.imported || 0}, skipped ${data.skipped || 0}`);
  loadProducts();
}
async function batchDeleteProducts() {
  if (!selectedRows.size) return toast('error', 'Chọn sản phẩm trước');
  if (!confirm(`Xóa ${selectedRows.size} sản phẩm khỏi Excel?`)) return;
  await fetch('/api/batch-delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rows: [...selectedRows] }),
  });
  selectedRows.clear();
  loadProducts();
}

// ── Posts ─────────────────────────────────────────────────────────────────────
async function loadPosts() {
  const res = await fetch('/api/posts');
  const data = await res.json();
  allPosts = data.posts;
  document.getElementById('post-stat-total').textContent = data.stats.total;
  document.getElementById('post-stat-published').textContent = data.stats.published;
  document.getElementById('post-stat-draft').textContent = data.stats.draft;
  renderPosts();
}

function renderPosts() {
  const q = (document.getElementById('search-posts').value || '').toLowerCase();
  const tbody = document.getElementById('posts-tbody');
  const items = allPosts.filter(p => !q || `${p.title} ${p.slug}`.toLowerCase().includes(q));
  tbody.innerHTML = items.map(p => `
    <tr>
      <td>${esc(p.title)}</td>
      <td><span class="badge ${p.wp_status}">${p.wp_status}</span></td>
      <td>${p.wp_url ? `<a href="${esc(p.wp_url)}" target="_blank" style="color:var(--accent2)">Link</a>` : '–'}</td>
      <td>
        <button class="btn btn-sm btn-ghost" onclick="openPostModal(${p.row})">✏️</button>
        <button class="btn btn-sm btn-success" onclick="publishPostRow(${p.row})">🚀</button>
      </td>
    </tr>
  `).join('');
}

function filterPosts() { renderPosts(); }

async function createPost() {
  const title = prompt('Post title:');
  if (!title) return;
  await fetch('/api/posts', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, wp_status: 'draft' }),
  });
  loadPosts();
}

async function openPostModal(row) {
  const res = await fetch(`/api/posts/${row}`);
  const p = await res.json();
  document.getElementById('post-edit-row').value = row;
  document.getElementById('post-edit-title').value = p.title || '';
  document.getElementById('post-edit-slug').value = p.slug || '';
  document.getElementById('post-edit-excerpt').value = p.excerpt || '';
  document.getElementById('post-edit-content').value = p.content || '';
  document.getElementById('post-edit-image').value = p.featured_image || '';
  document.getElementById('post-modal').classList.remove('hidden');
}

function closePostModal() {
  document.getElementById('post-modal').classList.add('hidden');
}

async function savePost() {
  const row = document.getElementById('post-edit-row').value;
  await fetch(`/api/posts/${row}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      title: document.getElementById('post-edit-title').value,
      slug: document.getElementById('post-edit-slug').value,
      excerpt: document.getElementById('post-edit-excerpt').value,
      content: document.getElementById('post-edit-content').value,
      featured_image: document.getElementById('post-edit-image').value,
    }),
  });
  toast('success', 'Đã lưu post');
  closePostModal();
  loadPosts();
}

async function publishPost() {
  const row = document.getElementById('post-edit-row').value;
  await savePost();
  await fetch(`/api/posts/${row}/publish`, { method: 'POST' });
  toast('success', 'Publishing to WordPress...');
}
async function savePostDraft() {
  const row = document.getElementById('post-edit-row').value;
  await savePost();
  await fetch(`/api/posts/${row}/save-draft`, { method: 'POST' });
  toast('success', 'Saving draft to WP...');
}
async function publishPostRow(row) {
  await fetch(`/api/posts/${row}/publish`, { method: 'POST' });
  toast('success', 'Publishing...');
}
async function syncPostsFromWp() {
  const res = await fetch('/api/posts/sync-from-wp', { method: 'POST' });
  const data = await res.json();
  toast(data.ok ? 'success' : 'error', `Imported ${data.imported || 0} posts`);
  loadPosts();
}

// ── Settings ──────────────────────────────────────────────────────────────────
function openSettings() { switchTab('settings'); }

async function loadSettingsForm() {
  const sel = document.getElementById('site-switcher');
  const site = currentSites[sel.value] || {};
  document.getElementById('set-name').value = site.name || '';
  document.getElementById('set-wp-url').value = site.wordpress_url || '';
  document.getElementById('set-wp-user').value = site.wp_username || '';
  document.getElementById('set-ebay-url').value = site.ebay_seller_url || '';
  document.getElementById('set-debug-port').value = site.debug_port || 9230;
}

async function saveSettings() {
  const siteId = document.getElementById('site-switcher').value;
  await fetch('/api/sites/update', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      id: siteId,
      name: document.getElementById('set-name').value,
      wordpress_url: document.getElementById('set-wp-url').value,
      wp_username: document.getElementById('set-wp-user').value,
      ebay_seller_url: document.getElementById('set-ebay-url').value,
      debug_port: parseInt(document.getElementById('set-debug-port').value) || 9230,
    }),
  });
  const pass = document.getElementById('set-wp-pass').value;
  const wcKey = document.getElementById('set-wc-key').value;
  const wcSecret = document.getElementById('set-wc-secret').value;
  if (pass || wcKey || wcSecret) {
    await fetch('/api/secrets/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        site_id: siteId,
        wp_app_password: pass,
        wc_consumer_key: wcKey,
        wc_consumer_secret: wcSecret,
      }),
    });
  }
  toast('success', 'Settings saved');
}

async function testWp() {
  const res = await fetch('/api/test-connection/wp', { method: 'POST' });
  const data = await res.json();
  document.getElementById('settings-result').textContent = data.ok
    ? `✅ WP connected: ${data.name}` : `❌ ${data.error}`;
}
async function testWoo() {
  const res = await fetch('/api/test-connection/woo', { method: 'POST' });
  const data = await res.json();
  document.getElementById('settings-result').textContent = data.ok
    ? '✅ WooCommerce connected' : `❌ ${data.error}`;
}

// ── Logs ──────────────────────────────────────────────────────────────────────
function connectLogs() {
  const body = document.getElementById('log-body');
  const es = new EventSource('/api/logs');
  es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.msg) {
        body.textContent += `[${data.ts}] ${data.msg}\n`;
        body.scrollTop = body.scrollHeight;
      }
    } catch (_) {}
  };
}

function toggleLogPanel() {
  const panel = document.getElementById('log-panel');
  const btn = document.getElementById('btn-toggle-log');
  const hidden = panel.style.display === 'none';
  panel.style.display = hidden ? 'flex' : 'none';
  btn.textContent = hidden ? '📋 Logs' : '📋 Show Logs';
}

async function pollServices() {
  try {
    const res = await fetch('/api/services');
    const data = await res.json();
    document.getElementById('svc-ebay').className = `svc ${data.ebay_browser ? 'online' : 'offline'}`;
    document.getElementById('svc-wp').className = `svc ${data.wordpress_url ? 'online' : 'offline'}`;
    if (activeTab === 'products') loadProducts();
  } catch (_) {}
}

window.addEventListener('DOMContentLoaded', () => {
  initSiteSwitcher().then(() => {
    loadProducts();
    loadSettingsForm();
  });
  connectLogs();
  pollServices();
  setInterval(pollServices, 15000);
});

document.querySelectorAll('.modal-overlay').forEach(overlay => {
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) overlay.classList.add('hidden');
  });
});
