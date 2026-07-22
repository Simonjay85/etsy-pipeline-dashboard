(function attachCatalogOrdering(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.CatalogOrdering = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function createCatalogOrdering() {
  function compareText(left, right) {
    const a = String(left || '');
    const b = String(right || '');
    const foldedA = a.toLowerCase();
    const foldedB = b.toLowerCase();
    if (foldedA !== foldedB) return foldedA < foldedB ? -1 : 1;
    if (a === b) return 0;
    return a < b ? -1 : 1;
  }

  function compareNatural(left, right) {
    const a = String(left || '').split(/(\d+)/).filter(Boolean);
    const b = String(right || '').split(/(\d+)/).filter(Boolean);
    const length = Math.max(a.length, b.length);
    for (let index = 0; index < length; index += 1) {
      if (a[index] == null) return -1;
      if (b[index] == null) return 1;
      const aIsNumber = /^\d+$/.test(a[index]);
      const bIsNumber = /^\d+$/.test(b[index]);
      if (aIsNumber && bIsNumber) {
        const difference = Number(a[index]) - Number(b[index]);
        if (difference) return difference;
        continue;
      }
      if (aIsNumber !== bIsNumber) return aIsNumber ? -1 : 1;
      const difference = compareText(a[index], b[index]);
      if (difference) return difference;
    }
    return compareText(left, right);
  }

  function compareCatalogRecords(left, right) {
    const leftFolder = String(left?.folder || '').trim();
    const rightFolder = String(right?.folder || '').trim();
    if (Boolean(leftFolder) !== Boolean(rightFolder)) return leftFolder ? -1 : 1;
    if (leftFolder) {
      return compareNatural(leftFolder, rightFolder)
        || compareText(left?.record_id, right?.record_id);
    }
    return compareNatural(left?.listing_id, right?.listing_id)
      || compareText(left?.etsy_title || left?.title, right?.etsy_title || right?.title)
      || compareText(left?.record_id, right?.record_id);
  }

  function sortCatalogRecords(records) {
    return Array.isArray(records) ? [...records].sort(compareCatalogRecords) : [];
  }

  function filterRenderableCatalogRecords(records) {
    return Array.isArray(records)
      ? records.filter(record => !(String(record?.folder || '').trim() && record?.exists === false))
      : [];
  }

  return { compareCatalogRecords, sortCatalogRecords, filterRenderableCatalogRecords };
}));
