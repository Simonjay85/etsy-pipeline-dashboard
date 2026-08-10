#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import inspect
import tempfile
import types
import unittest
from unittest.mock import patch

import etsy_auto_post


class _FakeStatusRadio:
    def __init__(self, page: "_FakeDraftPage", index: int, value: str, checked: bool = False, disabled: bool = False):
        self._page = page
        self.index = index
        self.value = str(value or "").strip().lower()
        self.checked = bool(checked)
        self.disabled = bool(disabled)

    async def get_attribute(self, name: str):
        if name == "value":
            return self.value
        return None

    async def is_checked(self):
        return bool(self.checked)

    async def is_enabled(self):
        return not self.disabled

    async def check(self, *, force: bool = False, timeout: int | None = None):
        self._page.check_calls.append((self.index, self.value, force, timeout))
        for radio in self._page.radios:
            radio.checked = False
        self.checked = True


class _FakeLocator:
    def __init__(self, page: "_FakeDraftPage", radios: list[_FakeStatusRadio]):
        self._page = page
        self._radios = radios

    async def count(self):
        return len(self._radios)

    def nth(self, index: int):
        if 0 <= index < len(self._radios):
            return _FakeLocator(self._page, [self._radios[index]])
        return _FakeLocator(self._page, [])

    async def is_checked(self):
        return bool(self._radios[0].checked) if self._radios else False

    async def is_enabled(self):
        return not self._radios[0].disabled if self._radios else False

    async def get_attribute(self, name: str):
        return await self._radios[0].get_attribute(name) if self._radios else None

    async def check(self, *, force: bool = False, timeout: int | None = None):
        if not self._radios:
            raise RuntimeError("Locator trống")
        await self._radios[0].check(force=force, timeout=timeout)


class _FakeDraftPage:
    def __init__(
        self,
        radio_specs: list[tuple[str, bool, bool]],
        *,
        cards=None,
        grid_state: dict | None = None,
    ):
        self.url = "https://www.etsy.com/your/shops/me/tools/listings"
        self.radios = [
            _FakeStatusRadio(self, idx, value, checked=checked, disabled=disabled)
            for idx, (value, checked, disabled) in enumerate(radio_specs)
        ]
        self.cards = cards or []
        self.grid_state = grid_state or {"loading": False, "ids": [], "emptyState": True}
        self.goto_calls: list[tuple[str, str | None]] = []
        self.wait_calls: list[int] = []
        self.check_calls: list[tuple[int, str, bool, int | None]] = []

    def locator(self, selector: str):
        if selector == 'input[name="item_status"]':
            return _FakeLocator(self, self.radios)
        if selector == 'input[name="item_status"][value="draft"]':
            return _FakeLocator(self, [r for r in self.radios if r.value == "draft"])
        return _FakeLocator(self, [])

    async def goto(self, url: str, wait_until: str | None = None):
        self.url = url
        self.goto_calls.append((url, wait_until))

    async def wait_for_timeout(self, ms: int):
        self.wait_calls.append(ms)

    async def evaluate(self, script: str):
        if "__etsyDraftGridStateMarker" in script:
            return self.grid_state
        if 'listing-editor/edit/' in script:
            return self.cards
        return {"loading": False, "ids": [], "emptyState": False}

    def set_checked_value(self, value: str):
        target = str(value).strip().lower()
        for radio in self.radios:
            radio.checked = radio.value == target


class TestResolveBrowserSessionDir(unittest.TestCase):
    def test_config_override_with_tilde_expands_to_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            base = Path(tmp) / "base"
            base.mkdir()

            cfg = {
                "daisyflowdigital": {
                    "browser_session": "~/.etsy_browser_session_daisyflowdigital"
                }
            }

            resolved = etsy_auto_post.resolve_browser_session_dir(
                "daisyflowdigital",
                config=cfg,
                base_dir=base,
                home_dir=home,
            )

            self.assertEqual(home / ".etsy_browser_session_daisyflowdigital", resolved)

    def test_temply_falls_back_to_legacy_session_if_no_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            base = Path(tmp) / "base"
            base.mkdir()
            legacy = base / ".browser-session"
            legacy.mkdir()

            resolved = etsy_auto_post.resolve_browser_session_dir(
                "templystudios",
                config={},
                base_dir=base,
                home_dir=home,
            )

            self.assertEqual(legacy, resolved)

    def test_unknown_shop_falls_back_to_home_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            base = Path(tmp) / "base"
            base.mkdir()
            (base / ".browser-session").mkdir()

            resolved = etsy_auto_post.resolve_browser_session_dir(
                "newshop123",
                config={},
                base_dir=base,
                home_dir=home,
            )

            self.assertEqual(home / ".etsy_browser_session_newshop123", resolved)


class TestForceDraftFilter(unittest.IsolatedAsyncioTestCase):
    async def test_no_click_when_draft_already_checked_but_disabled(self):
        page = _FakeDraftPage([
            ("draft", True, True),
            ("active", False, False),
        ])

        await etsy_auto_post._force_draft_filter(page)

        self.assertEqual(0, len(page.check_calls))
        self.assertEqual((etsy_auto_post.DRAFT_LISTINGS_URL, "domcontentloaded"), page.goto_calls[-1])
        self.assertTrue(page.radios[0].checked)

    async def test_multiple_draft_radios_selects_enabled_draft(self):
        page = _FakeDraftPage([
            ("draft", False, True),
            ("draft", False, False),
            ("active", True, False),
        ])

        await etsy_auto_post._force_draft_filter(page)

        self.assertEqual(1, len(page.check_calls))
        clicked_index, clicked_value, clicked_force, clicked_timeout = page.check_calls[0]
        self.assertEqual(1, clicked_index)
        self.assertEqual("draft", clicked_value)
        self.assertTrue(clicked_force)
        self.assertEqual(etsy_auto_post.DRAFT_FILTER_CHECK_TIMEOUT_MS, clicked_timeout)
        self.assertTrue(page.radios[1].checked)
        self.assertFalse(page.radios[2].checked)

    async def test_active_checked_still_gets_forced_to_draft(self):
        page = _FakeDraftPage([
            ("draft", False, False),
            ("active", True, False),
        ])

        await etsy_auto_post._force_draft_filter(page)

        self.assertTrue(page.radios[0].checked)
        self.assertFalse(page.radios[1].checked)
        self.assertEqual(1, len(page.check_calls))

    async def test_collect_drafts_revalidates_filter_after_grid_and_fails_on_drift(self):
        page = _FakeDraftPage([
            ("draft", True, False),
            ("active", False, False),
        ], cards=[{"id": "111", "title": "A", "sku": "A1", "status": "draft"}])

        async def drift_wait_grid(_page):
            page.set_checked_value("active")

        with patch.object(etsy_auto_post, "_wait_for_draft_grid_stable", side_effect=drift_wait_grid):
            with self.assertRaises(RuntimeError) as ex:
                await etsy_auto_post._collect_draft_cards(page)
        self.assertIn("Lọc Draft không còn chính xác sau khi grid ổn định", str(ex.exception))

    def test_force_draft_filter_uses_check_not_default_click_timeout(self):
        source = inspect.getsource(etsy_auto_post._force_draft_filter)
        self.assertNotIn(".click(", source)
        self.assertIn("check(force=True", source)
        self.assertIn("timeout=", source)
        self.assertNotIn("timeout=30000", source)


class TestDraftUrlBaselineVerification(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_check_records_pre_create_draft_ids(self):
        product = {"title": "Brand New Printable", "sku": "NEW-394"}
        cards = [
            {"id": "101", "title": "Existing One", "sku": "OLD-101", "status": "draft"},
            {"id": "202", "title": "Existing Two", "sku": "OLD-202", "status": "draft"},
        ]

        with patch.object(etsy_auto_post, "_collect_draft_cards", return_value=cards):
            is_duplicate = await etsy_auto_post.check_duplicate_draft(object(), product)

        self.assertFalse(is_duplicate)
        self.assertEqual(["101", "202"], product["_draft_ids_before_create"])

    async def test_uses_exactly_one_new_draft_id_when_signature_does_not_match(self):
        product = {
            "title": "Brand New Printable",
            "sku": "NEW-394",
            "_draft_ids_before_create": ["101", "202"],
        }
        async def _noop_timeout(ms): pass
        page = types.SimpleNamespace(url="https://www.etsy.com/your/shops/me/tools/listings", wait_for_timeout=_noop_timeout)
        cards = [
            {"id": "101", "title": "Existing One", "sku": "", "status": "draft"},
            {"id": "202", "title": "Existing Two", "sku": "", "status": "draft"},
            {"id": "303", "title": "", "sku": "", "status": "draft"},
        ]

        with patch.object(etsy_auto_post, "_collect_draft_cards", return_value=cards):
            result = await etsy_auto_post.get_newly_created_listing_url(page, product)

        self.assertEqual("https://www.etsy.com/listing/303", result)

    async def test_rejects_multiple_new_draft_ids(self):
        product = {
            "title": "Brand New Printable",
            "sku": "NEW-394",
            "_draft_ids_before_create": ["101"],
        }
        async def _noop_timeout(ms): pass
        page = types.SimpleNamespace(url="https://www.etsy.com/your/shops/me/tools/listings", wait_for_timeout=_noop_timeout)
        cards = [
            {"id": "101", "title": "Existing One", "sku": "", "status": "draft"},
            {"id": "202", "title": "", "sku": "", "status": "draft"},
            {"id": "303", "title": "", "sku": "", "status": "draft"},
        ]

        with patch.object(etsy_auto_post, "_collect_draft_cards", return_value=cards):
            result = await etsy_auto_post.get_newly_created_listing_url(page, product)

        self.assertEqual(etsy_auto_post.UNVERIFIED_DRAFT_URL_SENTINEL, result)

    async def test_rejects_zero_new_draft_ids(self):
        product = {
            "title": "Brand New Printable",
            "sku": "NEW-394",
            "_draft_ids_before_create": ["101"],
        }
        async def _noop_timeout(ms): pass
        page = types.SimpleNamespace(url="https://www.etsy.com/your/shops/me/tools/listings", wait_for_timeout=_noop_timeout)
        cards = [
            {"id": "101", "title": "Existing One", "sku": "", "status": "draft"},
        ]

        with patch.object(etsy_auto_post, "_collect_draft_cards", return_value=cards):
            result = await etsy_auto_post.get_newly_created_listing_url(page, product)

        self.assertEqual(etsy_auto_post.UNVERIFIED_DRAFT_URL_SENTINEL, result)


class ImageThumbCountTests(unittest.IsolatedAsyncioTestCase):
    def test_media_selectors_prefer_delete_buttons_not_broad_image_labels(self):
        selectors = etsy_auto_post._get_media_thumbnail_selectors()
        joined = ", ".join(selectors)
        self.assertIn('button[data-testid="image-delete-button"]', joined)
        self.assertNotIn("le-aspect-ratio", joined)
        self.assertNotIn("aria-label*='image'", joined)
        self.assertNotIn('aria-label*="image"', joined)
        self.assertNotIn("aria-label*='thumbnail'", joined)
        fallback = ", ".join(etsy_auto_post._get_media_thumbnail_fallback_selectors())
        self.assertIn("le-aspect-ratio", fallback)

    async def test_count_prefers_delete_buttons_over_inflated_square_tiles(self):
        class _CountLocator:
            def __init__(self, n: int | Exception):
                self._n = n

            async def count(self):
                if isinstance(self._n, Exception):
                    raise self._n
                return self._n

        class _Page:
            def locator(self, sel: str):
                # New Etsy UI: 10 delete buttons, but 11 square tiles
                # (10 photos + 1 empty upload slot). Must return 10, not 11.
                if "image-delete-button" in sel:
                    return _CountLocator(10)
                if "le-aspect-ratio" in sel:
                    return _CountLocator(11)
                if "Remove" in sel or "Delete" in sel:
                    return _CountLocator(Exception("missing"))
                return _CountLocator(0)

        self.assertEqual(10, await etsy_auto_post._count_listing_image_thumbs(_Page()))

    async def test_count_falls_back_to_square_tiles_when_no_delete_buttons(self):
        class _CountLocator:
            def __init__(self, n: int):
                self._n = n

            async def count(self):
                return self._n

        class _Page:
            def locator(self, sel: str):
                if "le-aspect-ratio" in sel:
                    return _CountLocator(7)
                return _CountLocator(0)

        self.assertEqual(7, await etsy_auto_post._count_listing_image_thumbs(_Page()))

    async def test_wait_exact_passes_when_delete_count_matches(self):
        class _CountLocator:
            def __init__(self, n: int):
                self._n = n

            async def count(self):
                return self._n

        class _Page:
            def __init__(self):
                self.wait_calls = 0

            def locator(self, sel: str):
                if "image-delete-button" in sel:
                    return _CountLocator(10)
                return _CountLocator(0)

            async def wait_for_timeout(self, _ms: int):
                self.wait_calls += 1

        page = _Page()
        ok = await etsy_auto_post._wait_for_expected_image_count(
            page, expected_count=10, exact=True, timeout_ms=500
        )
        self.assertTrue(ok)
        self.assertEqual(0, page.wait_calls)

    async def test_upload_until_count_batches_and_topups_missing(self):
        upload_calls: list[list[str]] = []
        counts = [0, 5, 6, 10]  # before batch1, before batch2, after batches, after top-up

        async def fake_count(_page):
            return counts.pop(0) if counts else 10

        async def fake_upload(_page, paths):
            upload_calls.append(list(paths))

        async def fake_wait(_page, expected_count, exact=False, timeout_ms=90000, log_progress=False):
            return True

        paths = [f"img-{i}.png" for i in range(1, 11)]
        with patch.object(etsy_auto_post, "_count_listing_image_thumbs", side_effect=fake_count), \
                patch.object(etsy_auto_post, "_upload_listing_photos", side_effect=fake_upload), \
                patch.object(etsy_auto_post, "_wait_for_expected_image_count", side_effect=fake_wait):
            final = await etsy_auto_post._upload_listing_photos_until_count(
                object(),
                paths,
                expected_total=10,
                exact=True,
                batch_size=5,
            )

        self.assertEqual(10, final)
        self.assertEqual(3, len(upload_calls))
        self.assertEqual(5, len(upload_calls[0]))
        self.assertEqual(5, len(upload_calls[1]))
        self.assertEqual(4, len(upload_calls[2]))  # top-up missing 4
        self.assertEqual(paths[-4:], upload_calls[2])


if __name__ == "__main__":
    unittest.main()
