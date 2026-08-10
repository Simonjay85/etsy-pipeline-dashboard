const fs = require('node:fs');
const assert = require('node:assert/strict');
const vm = require('node:vm');

const indexHtml = fs.readFileSync('dashboard_static/index.html', 'utf8');
assert.ok(indexHtml.includes('id="runtime-health"'), 'Missing runtime health widget root');
assert.ok(indexHtml.includes('id="runtime-health-meta"'), 'Missing runtime health metadata label');

const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');
const start = appJs.indexOf('function runtimeHealthWarnings');
const end = appJs.indexOf('function setSvc', start);
assert.ok(start >= 0 && end > start, 'Failed to locate runtime health helpers in app.js');
const runtimeHealthSource = appJs.slice(start, end);

class RuntimeHealthNode {
  constructor() {
    this.parentNode = null;
    this._classes = new Set();
    this.children = [];
    this.textContent = '';
    this.dataset = {};
  }

  get className() {
    return Array.from(this._classes).join(' ');
  }

  set className(value) {
    this._classes = new Set(String(value || '').trim().split(/\s+/).filter(Boolean));
  }

  get classList() {
    return {
      add: (value) => this._classes.add(value),
      remove: (value) => this._classes.delete(value),
    };
  }

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
  }

  querySelector(selector) {
    if (selector === '.runtime-health-issues') {
      return this.children.find((node) => node.className.includes('runtime-health-issues')) || null;
    }
    return null;
  }

  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter((item) => item !== this);
    this.parentNode = null;
  }
}

function createDocument() {
  const root = new RuntimeHealthNode();
  root.id = 'runtime-health';
  const title = new RuntimeHealthNode();
  title.className = 'runtime-health-text';
  const meta = new RuntimeHealthNode();
  meta.id = 'runtime-health-meta';
  meta.className = 'runtime-health-meta';

  const nodes = {
    'runtime-health': root,
    'runtime-health-meta': meta,
    runtimeHealthText: title,
  };

  return {
    getElementById(id) {
      return nodes[id] || null;
    },
    querySelector(selector) {
      if (selector === '#runtime-health .runtime-health-text') {
        return title;
      }
      return null;
    },
    createElement(tagName) {
      return new RuntimeHealthNode();
    },
  };
}

const document = createDocument();
const sandbox = {
  document,
  console,
};
vm.createContext(sandbox);

vm.runInContext(runtimeHealthSource, sandbox);

assert.equal(typeof sandbox.runtimeHealthWarnings, 'function');
assert.equal(typeof sandbox.renderRuntimeHealth, 'function');
assert.equal(typeof sandbox.normalizeRuntimeHealthStatus, 'function');

  const mismatchPayload = {
    canonical_match: false,
    source: { dirty: true },
    backup_scheduler: {
      loaded: { daily: false, weekly: false },
    status_evidence: {
      last_failure: { timestamp: '2026-01-01T00:00:00', status: 'failure' },
      last_success: null,
    },
  },
    active_shop: { id: 'demo', name: 'Demo Shop' },
    canonical_root: '/canonical/one',
    current_root: '/tmp/noncanonical',
    service_readiness: {
      ok: false,
      checks: { vertex_app: true, mlx_ai: false, watcher: true },
      optional: { ok: false, checks: { vertex_app: true, mlx_ai: false, watcher: true } },
    },
  };

const warnings = sandbox.runtimeHealthWarnings(mismatchPayload);
assert.equal(warnings.length >= 2, true);
assert.equal(warnings.some((item) => item.kind === 'canonical'), true);
  assert.equal(warnings.some((item) => item.kind === 'scheduler'), true);
  assert.equal(warnings.some((item) => item.kind === 'mlx'), true);
  assert.equal(warnings.some((item) => item.kind === 'backup'), true);

sandbox.renderRuntimeHealth(mismatchPayload);
const widget = document.getElementById('runtime-health');
const meta = document.getElementById('runtime-health-meta');
assert.ok(widget.className.includes('warning'));
assert.ok(/noncanonical/.test(meta.textContent));
assert.ok(document.getElementById('runtime-health').querySelector('.runtime-health-issues'));

  const olderFailurePayload = {
    canonical_match: true,
    source: { dirty: false },
    backup_scheduler: {
      loaded: { daily: true, weekly: true },
      status_evidence: {
        last_failure: { timestamp: '2026-01-02T00:00:00', status: 'failure' },
        last_success: { timestamp: '2026-01-03T00:00:00', status: 'success' },
      },
    },
    active_shop: { id: 'demo', name: 'Demo Shop' },
    canonical_root: '/tmp/canonical',
    current_root: '/tmp/canonical',
    service_readiness: {
      ok: true,
      core: { ok: true, checks: { dashboard_endpoint: true } },
      optional: { ok: true, checks: { vertex_app: true, mlx_ai: true, watcher: true } },
    },
    health_summary: { source_stale: false },
  };

  const olderFailureWarnings = sandbox.runtimeHealthWarnings(olderFailurePayload);
  assert.equal(olderFailureWarnings.some((item) => item.kind === 'backup'), false);

  const newerFailurePayload = {
    canonical_match: true,
    source: { dirty: false },
    backup_scheduler: {
      loaded: { daily: true, weekly: true },
      status_evidence: {
        last_failure: { timestamp: '2026-01-05T00:00:00', status: 'failure' },
        last_success: { timestamp: '2026-01-03T00:00:00', status: 'success' },
      },
    },
    active_shop: { id: 'demo', name: 'Demo Shop' },
    canonical_root: '/tmp/canonical',
    current_root: '/tmp/canonical',
    service_readiness: {
      ok: true,
      core: { ok: true, checks: { dashboard_endpoint: true } },
      optional: { ok: true, checks: { vertex_app: true, mlx_ai: true, watcher: true } },
    },
    health_summary: { source_stale: false },
  };

  const newerFailureWarnings = sandbox.runtimeHealthWarnings(newerFailurePayload);
  assert.equal(newerFailureWarnings.some((item) => item.kind === 'backup'), true);

  const healthyPayload = {
    canonical_match: true,
    source: { dirty: false },
    backup_scheduler: {
      loaded: { daily: true, weekly: true },
    status_evidence: { last_failure: null, last_success: { timestamp: '2026-01-01T00:00:00', status: 'success' } },
  },
    active_shop: { id: 'demo', name: 'Demo Shop' },
    canonical_root: '/tmp/canonical',
    current_root: '/tmp/canonical',
    service_readiness: {
      ok: true,
      core: { ok: true, checks: { dashboard_endpoint: true } },
      optional: { ok: true, checks: { vertex_app: true, mlx_ai: true, watcher: true } },
    },
    health_summary: { source_stale: false, backup_last_failure: false },
  };

  sandbox.renderRuntimeHealth(healthyPayload);
  assert.equal(widget.className.includes('warning'), false);

  const optionalOfflinePayload = {
    canonical_match: true,
    source: { dirty: false },
    backup_scheduler: {
      loaded: { daily: true, weekly: true },
      status_evidence: { last_failure: null, last_success: { timestamp: '2026-01-01T00:00:00', status: 'success' } },
    },
    active_shop: { id: 'demo', name: 'Demo Shop' },
    canonical_root: '/tmp/canonical',
    current_root: '/tmp/canonical',
    service_readiness: {
      ok: true,
      core: { ok: true, checks: { dashboard_endpoint: true } },
      optional: { ok: false, checks: { vertex_app: false, mlx_ai: true, watcher: true } },
    },
    health_summary: { source_stale: false, backup_last_failure: false },
  };

  sandbox.renderRuntimeHealth(optionalOfflinePayload);
  assert.ok(widget.className.includes('warning'));
  assert.ok(/ready/.test(meta.textContent));
  assert.equal(widget.querySelector('.runtime-health-issues').textContent.includes('Vertex service offline'), true);

  const normalized = sandbox.normalizeRuntimeHealthStatus(mismatchPayload);
  assert.equal(normalized.ok, false);
  assert.ok(normalized.warnings.length >= 1);

console.log('runtime health UI tests passed');
