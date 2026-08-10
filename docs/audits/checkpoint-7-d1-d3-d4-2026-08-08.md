# Checkpoint 7 — D1/D3/D4 operator safety

Date: 2026-08-08

D1:

- Added a compact, active-shop-scoped `GET /api/etsy/jobs` read model.
- Added cancel-one and retry-one routes with ownership, status, retry-lineage, and asset checks. Emergency Stop All remains a separate endpoint.
- Added the collapsible Job Center drawer with status, queue item, shop, folder, operation, fields, duration, latest safe message, cancel, and retry controls.

D3/D4:

- Product actions are grouped as Local, Content, and Etsy live, with one marked primary next action and explicit high-risk live-write markers.
- Batch Local → Etsy now has an exact-product/active-shop/direction/fields review and explicit live-write acknowledgement before the existing confirmation.
- The shared modal manager provides ARIA dialog state, initial focus, focus trap, Escape, focus restoration, overlay close, and opt-in edit unsaved-change warning.

Evidence:

- Job Center Python tests: list scoping, cancel-one, retry lineage, and terminal rejection pass.
- `test_modal_accessibility_ui.js`, existing Etsy batch/single/link/modal tests, and `node --check`: passed.
