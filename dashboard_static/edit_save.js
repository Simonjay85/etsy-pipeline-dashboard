(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.ProductEditSave = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const VERIFIED_FIELDS = Object.freeze([
    'title', 'tags', 'keywords', 'description', 'price', 'section',
    'extra', 'etsy_url', 'sku', 'qty',
  ]);

  function parseProductRow(value) {
    const row = Number(value);
    return Number.isInteger(row) && row > 0 ? row : null;
  }

  function normalizedValue(field, value) {
    if (field === 'price' || field === 'qty') {
      const number = Number(value);
      return Number.isFinite(number) ? number : null;
    }

    const text = String(value == null ? '' : value).trim();
    if (field === 'title' && text.startsWith('[Cần SEO]')) return '';
    // The product API presents an empty workbook section as Digital Planner.
    if (field === 'section' && !text) return 'Digital Planner';
    return text;
  }

  function findSavedFieldMismatches(expected, actual) {
    if (!actual) {
      return [{ field: 'row', expected: 'saved product', actual: 'missing' }];
    }

    return VERIFIED_FIELDS.flatMap((field) => {
      const expectedValue = normalizedValue(field, expected[field]);
      const actualValue = normalizedValue(field, actual[field]);
      // Products that still need SEO expose their seed title as the display
      // title, while the workbook title remains intentionally empty.  Treat
      // that API presentation value as an empty title during read-back.
      const intentionallyMissingTitle = field === 'title'
        && expectedValue === ''
        && actual.needs_seo === true;
      return intentionallyMissingTitle || expectedValue === actualValue
        ? []
        : [{ field, expected: expectedValue, actual: actualValue }];
    });
  }

  return Object.freeze({
    VERIFIED_FIELDS,
    parseProductRow,
    findSavedFieldMismatches,
  });
});
