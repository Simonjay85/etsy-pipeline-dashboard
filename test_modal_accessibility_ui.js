'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const indexHtml = fs.readFileSync('dashboard_static/index.html', 'utf8');
const appJs = fs.readFileSync('dashboard_static/app.js', 'utf8');
const styleCss = fs.readFileSync('dashboard_static/style.css', 'utf8');

assert.match(indexHtml, /id="etsy-bulk-update-review"/);
assert.match(indexHtml, /id="etsy-bulk-update-products"/);
assert.match(indexHtml, /id="etsy-bulk-update-live-confirmation"/);
assert.match(indexHtml, /Local → Etsy/);
assert.match(indexHtml, /data-unsaved-check="true"/);
assert.match(appJs, /data-action-role="primary-next"/);
assert.match(appJs, /data-action-scope="local"/);
assert.match(appJs, /data-action-scope="live-etsy"/);
assert.match(appJs, /class="[^"]*live-etsy-write/);
assert.match(appJs, /function updateBulkEtsyReview\(\)/);
assert.match(appJs, /fields,\s*shop: target\.shop,\s*folder: target\.folder,\s*listing_id: target\.listingId/);
assert.match(styleCss, /\.product-primary-action/);
assert.match(styleCss, /\.action-group-live-etsy/);
assert.match(styleCss, /\.product-actions \.btn \{ min-width: 44px; min-height: 44px;/);

const modalStart = appJs.indexOf('const modalManager = {');
const modalEnd = appJs.indexOf('// ── Social Bulk Poster Modal', modalStart);
assert.ok(modalStart >= 0 && modalEnd > modalStart, 'Could not isolate modal manager');

class TestNode {
  constructor(tagName, id = '') {
    this.tagName = tagName;
    this.id = id;
    this.parentNode = null;
    this.children = [];
    this.attributes = new Map();
    this.dataset = {};
    this.value = '';
    this.type = tagName === 'input' ? 'text' : undefined;
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.isConnected = true;
    this.textContent = '';
    this.focusCount = 0;
    this.listeners = {};
    this._classes = new Set();
    this.classList = {
      add: value => this._classes.add(value),
      remove: value => this._classes.delete(value),
      contains: value => this._classes.has(value),
    };
  }

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  addEventListener(name, handler) {
    this.listeners[name] = handler;
  }

  focus() {
    this.focusCount += 1;
    document.activeElement = this;
  }

  contains(node) {
    if (node === this) return true;
    return this.children.some(child => child.contains(node));
  }

  querySelector(selector) {
    if (selector === '.modal') return this.children.find(child => child._classes.has('modal')) || null;
    if (selector === '[data-modal-title], .modal-header h2') return this.title || null;
    if (selector === '[autofocus]') return this.autofocus || null;
    return null;
  }

  querySelectorAll(selector) {
    if (selector.includes('.btn-close')) return this.closeButtons || [];
    if (selector.includes('input, textarea, select')) return this.controls || [];
    if (selector.includes('a[href]')) return this.focusables || [];
    return [];
  }
}

const overlay = new TestNode('div', 'edit-modal');
overlay.dataset.unsavedCheck = 'true';
const dialog = new TestNode('div');
dialog._classes.add('modal');
const title = new TestNode('h2');
title.textContent = 'Chỉnh sửa sản phẩm';
const closeButton = new TestNode('button');
const field = new TestNode('input');
field.type = 'text';
field.value = 'initial';
const secondButton = new TestNode('button');
dialog.title = title;
dialog.closeButtons = [closeButton];
dialog.controls = [field];
dialog.focusables = [closeButton, field, secondButton];
dialog.appendChild(title);
dialog.appendChild(closeButton);
dialog.appendChild(field);
dialog.appendChild(secondButton);
overlay.appendChild(dialog);

const document = {
  activeElement: null,
  listeners: {},
  getElementById(id) {
    return id === overlay.id ? overlay : null;
  },
  querySelectorAll(selector) {
    return selector === '.modal-overlay' ? [overlay] : [];
  },
  querySelector(selector) {
    if (selector === '.modal-overlay.open') return overlay.classList.contains('open') ? overlay : null;
    return null;
  },
  addEventListener(name, handler) {
    this.listeners[name] = handler;
  },
};

const sandbox = {
  document,
  window: { confirm: () => true },
  module: { exports: {} },
};
vm.createContext(sandbox);
vm.runInContext(
  `${appJs.slice(modalStart, modalEnd)}\nglobalThis.__modalManager = modalManager; globalThis.__openModal = openModal; globalThis.__closeModal = closeModal;`,
  sandbox,
);

const { __modalManager: manager, __openModal: openModal, __closeModal: closeModal } = sandbox;
manager.init();
assert.equal(dialog.getAttribute('role'), 'dialog');
assert.equal(dialog.getAttribute('aria-modal'), 'true');
assert.equal(dialog.getAttribute('aria-labelledby'), 'edit-modal-title');
assert.equal(closeButton.getAttribute('aria-label'), 'Đóng Chỉnh sửa sản phẩm');

const opener = new TestNode('button');
document.activeElement = opener;
assert.equal(openModal('edit-modal'), true);
assert.equal(overlay.classList.contains('open'), true);
assert.equal(overlay.getAttribute('aria-hidden'), 'false');
assert.equal(document.activeElement, closeButton, 'Modal should move focus to its first focusable control');

field.value = 'changed';
sandbox.window.confirm = () => false;
assert.equal(closeModal('edit-modal'), false, 'Dirty modal close should be cancellable');
assert.equal(overlay.classList.contains('open'), true);

sandbox.window.confirm = () => true;
assert.equal(closeModal('edit-modal'), true);
assert.equal(overlay.classList.contains('open'), false);
assert.equal(overlay.getAttribute('aria-hidden'), 'true');
assert.equal(document.activeElement, opener, 'Focus should return to the opener');

assert.equal(openModal('edit-modal'), true);
secondButton.focus();
const tabEvent = { key: 'Tab', shiftKey: false, preventDefault() { this.prevented = true; } };
document.listeners.keydown(tabEvent);
assert.equal(tabEvent.prevented, true);
assert.equal(document.activeElement, closeButton, 'Tab from the last control should wrap to the first');

const escapeEvent = { key: 'Escape', preventDefault() { this.prevented = true; } };
document.listeners.keydown(escapeEvent);
assert.equal(escapeEvent.prevented, true);
assert.equal(overlay.classList.contains('open'), false);

console.log('modal accessibility and D3 action UI tests passed');
