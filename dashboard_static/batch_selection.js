(function attachBatchSelection(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BatchSelection = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function createBatchSelection() {
  function isEnabledCheckbox(checkbox) {
    return checkbox?.disabled !== true;
  }

  function hasClass(checkbox, className) {
    if (checkbox?.classList?.contains) return checkbox.classList.contains(className);
    return String(checkbox?.className || '').split(/\s+/).includes(className);
  }

  function checkboxKind(checkbox) {
    return hasClass(checkbox, 'shop-product-cb') ? 'shop' : 'local';
  }

  function selectedByKind(checkboxes, kind) {
    return Array.from(checkboxes || []).filter(checkbox =>
      isEnabledCheckbox(checkbox) && checkbox?.checked !== false && checkboxKind(checkbox) === kind
    );
  }

  function summarizeSelection(checkboxes) {
    const selected = Array.from(checkboxes || []).filter(checkbox =>
      isEnabledCheckbox(checkbox) && checkbox?.checked !== false
    );
    const localCount = selectedByKind(selected, 'local').length;
    const shopCount = selectedByKind(selected, 'shop').length;
    const mode = localCount && shopCount ? 'mixed' : localCount ? 'local' : shopCount ? 'shop' : 'none';
    return { total: localCount + shopCount, localCount, shopCount, mode };
  }

  function getBatchActionState(source, checkboxes) {
    const summary = summarizeSelection(checkboxes);
    return {
      ...summary,
      showLocalActions: (source === 'local' || source === 'aggregate') && summary.localCount > 0,
      showShopActions: (source === 'shop' || source === 'aggregate') && summary.shopCount > 0,
      showCrossShopAction: summary.localCount > 0,
    };
  }

  function selectedDraftListingIds(checkboxes) {
    return selectedByKind(checkboxes, 'shop')
      .filter(checkbox => String(checkbox?.dataset?.etsyStatus || '').toLowerCase() === 'draft')
      .map(checkbox => String(checkbox?.dataset?.listingId || checkbox?.value || ''))
      .filter(listingId => /^\d+$/.test(listingId));
  }

  return { isEnabledCheckbox, checkboxKind, selectedByKind, summarizeSelection, getBatchActionState, selectedDraftListingIds };
}));
