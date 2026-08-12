#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import etsy_auto_post


class TestCategoryInference(unittest.TestCase):
    def setUp(self):
        self.daisy_shop_dir = Path("/Users/aaronnguyen/Documents/Claude/Projects/Etsy/shops/daisyflowdigital")
        self.patchers = [
            patch.object(etsy_auto_post, "SHOP_DIR", self.daisy_shop_dir),
            patch.object(etsy_auto_post, "EXCEL_FILE", self.daisy_shop_dir / "Etsy_SEO_Generator.xlsx"),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()

    def test_product_389_infers_cutting_machine_files(self):
        products, *_ = etsy_auto_post.read_products(product_folder="product-389", shop_id="daisyflowdigital")
        self.assertEqual(1, len(products), "Expected to load exactly one product-389 row")
        self.assertEqual("Cutting Machine Files", etsy_auto_post.resolve_listing_category(products[0]))

    def test_explicit_category_wins_over_inference(self):
        product = {
            "category": "Digital > Planner Templates",
            "title": "Wildflower SVG Bundle | Cricut Cut Files",
            "keywords": "SVG, Cricut",
            "tags": "",
            "description": "Vectors and cut files",
        }
        self.assertEqual("Planner Templates", etsy_auto_post.resolve_listing_category(product))

    def test_resume_category_inference(self):
        product = {
            "category": "",
            "title": "Professional Resume Cover + CV Pack",
            "keywords": "job search,cv",
            "tags": "",
            "description": "",
        }
        self.assertEqual("Résumé Templates", etsy_auto_post.infer_listing_category(product))

    def test_planner_category_inference(self):
        product = {
            "category": "",
            "title": "Weekly Planner & Journal Kit",
            "keywords": "workbook",
            "tags": "",
            "description": "",
        }
        self.assertEqual("Planner Templates", etsy_auto_post.infer_listing_category(product))

    def test_unknown_category_inference_fails_closed(self):
        product = {
            "category": "",
            "title": "Acoustic Guitar Pick Guard Sticker",
            "keywords": "custom", 
            "tags": "gift",
            "description": "A gift item with no matching category tokens.",
        }
        self.assertEqual("", etsy_auto_post.infer_listing_category(product))

    def test_category_option_match_accepts_suffix_variants(self):
        self.assertTrue(
            etsy_auto_post.category_option_matches(
                "Cutting Machine Files",
                "Cutting Machine Files Digital",
            )
        )
        self.assertTrue(
            etsy_auto_post.category_option_matches(
                "Cutting Machine Files",
                "Cutting Machine Files Physical",
            )
        )
        self.assertTrue(
            etsy_auto_post.category_option_matches(
                "Cutting Machine Files",
                "Cutting Machine Files Physical or digital",
            )
        )

    def test_category_option_match_rejects_unrelated_or_wrong_suffix(self):
        self.assertFalse(
            etsy_auto_post.category_option_matches(
                "Cutting Machine Files",
                "Digital",
            )
        )

    def test_media_thumbnail_selector_prefers_listing_image_buttons(self):
        selector = etsy_auto_post._get_media_thumbnail_selector()
        self.assertIn("button.le-aspect-ratio--square", selector)
        self.assertNotIn("ul li img", selector)
        self.assertNotIn("img.wt-image__image", selector)
        self.assertFalse(
            etsy_auto_post.category_option_matches(
                "Cutting Machine Files",
                "Cutting Machine Files for print",
            )
        )


class _FakeOptionLocator:
    def __init__(self, page, index: int):
        self.page = page
        self.index = index

    async def count(self):
        snapshot = self.page._current_snapshot()
        return 1 if self.index < len(snapshot) else 0

    async def is_visible(self):
        return self.index < len(self.page._current_snapshot())

    async def inner_text(self):
        snapshot = self.page._current_snapshot()
        if self.index < len(snapshot):
            return snapshot[self.index]
        return ""

    async def click(self):
        snapshot = self.page._current_snapshot()
        if self.index < len(snapshot):
            await self.page._on_category_option_click(snapshot[self.index])


class _FakeCategoryOptionsLocator:
    def __init__(self, page, snapshots):
        self.page = page
        self.snapshots = snapshots

    async def count(self):
        self.page._advance_snapshot()
        return len(self._snapshot())

    def _snapshot(self):
        idx = self.page.option_poll_index
        if idx < len(self.snapshots):
            return self.snapshots[idx]
        return self.snapshots[-1]

    def _locator_snapshot(self):
        return self._snapshot()

    def nth(self, idx):
        return _FakeOptionLocator(self, idx)

    def _current_snapshot(self):
        return self._locator_snapshot()

    async def _on_category_option_click(self, option_text: str):
        self.page.option_clicked_count += 1
        self.page.selected_option = option_text
        self.page.selected_input_value = etsy_auto_post.normalize_category_text(option_text).split(" digital")[0].split(" physical")[0].strip()


class _FakeCategoryInputLocator:
    def __init__(self, page):
        self.page = page

    @property
    def first(self):
        return self

    async def wait_for(self, *args, **kwargs):
        return None

    async def count(self):
        return 1

    async def is_visible(self):
        return True

    async def click(self):
        return None

    async def fill(self, value: str):
        if value is not None:
            self.page.selected_input_value = etsy_auto_post.normalize_category_text(value)
        return None

    async def input_value(self):
        return self.page.selected_input_value


class _FakeCategoryPage:
    def __init__(self, option_snapshots):
        self.option_snapshots = option_snapshots
        self.option_poll_index = -1
        self.option_clicked_count = 0
        self.selected_option = ""
        self.selected_input_value = ""
        self.input_locator = _FakeCategoryInputLocator(self)
        self.options_locator = _FakeCategoryOptionsLocator(self, option_snapshots)

    def _advance_snapshot(self):
        self.option_poll_index += 1
        if self.option_poll_index >= len(self.option_snapshots):
            self.option_poll_index = len(self.option_snapshots) - 1

    def _current_snapshot(self):
        idx = self.option_poll_index
        if idx < 0:
            return self.option_snapshots[0] if self.option_snapshots else []
        if idx >= len(self.option_snapshots):
            return self.option_snapshots[-1]
        return self.option_snapshots[idx]

    def locator(self, selector: str):
        if "option" in selector and "role" in selector:
            return self.options_locator
        return self.input_locator

    async def wait_for_timeout(self, timeout: int):
        return None

    async def _on_category_option_click(self, option_text: str):
        self.option_clicked_count += 1
        self.selected_option = option_text
        selected = etsy_auto_post.normalize_category_text(option_text)
        for suffix in ["physical or digital", "physical", "digital"]:
            selected = selected.removesuffix(f" {suffix}")
        self.selected_input_value = selected.strip()


class TestFillCategoryDynamicOptions(unittest.IsolatedAsyncioTestCase):
    async def test_fill_category_tab_polls_until_option_appears_and_clicks_match(self):
        page = _FakeCategoryPage(
            [
                [],
                [],
                ["Digital", "Cutting Machine Files Digital"],
            ]
        )
        product = {"category": "Cutting Machine Files", "title": "Live test"}
        await etsy_auto_post.fill_category_tab(page, product)

        self.assertEqual(1, page.option_clicked_count)
        self.assertEqual("cutting machine files", page.selected_input_value)


class _FakeDraftRadio:
    def __init__(self, page, available=True, checked=False, click_sets_checked=True):
        self.page = page
        self._available = available
        self._checked = checked
        self._click_sets_checked = click_sets_checked

        # Keep compatibility with previous assertions used in tests
        self.page.is_draft_checked = checked

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self._available else 0

    async def is_checked(self):
        return self._checked

    async def click(self):
        self.page.radio_clicked = True
        if self._click_sets_checked:
            self._checked = True
            self.page.is_draft_checked = True


class _FakeFileUploadLocator:
    def __init__(self):
        self.set_inputs = []

    @property
    def first(self):
        return self

    async def count(self):
        return 1

    async def set_input_files(self, files, timeout=None):
        self.set_inputs.append(list(files))


class _FakePageTimeoutMixin:
    async def wait_for_timeout(self, timeout: int):
        return None


class _FakeDraftFilterPage:
    def __init__(self, cards, draft_radio_available=True, click_sets_checked=True, state_snapshots=None):
        self.cards = cards
        self.gotos = []
        self.radio_clicked = False
        self.is_draft_checked = False
        self.draft_radio_available = draft_radio_available
        self.draft_radio_click_sets_checked = click_sets_checked
        self.state_snapshots = state_snapshots
        self.state_poll_index = -1
        self.state_poll_count = 0

    def _next_state(self):
        if not self.state_snapshots:
            ids = [str(c.get("id")) for c in self.cards if str(c.get("id", "")).strip()]
            snapshot = {"loading": False, "ids": ids, "emptyState": not bool(ids)}
        else:
            idx = min(self.state_poll_count, len(self.state_snapshots) - 1)
            snapshot = self.state_snapshots[idx]
            self.state_poll_count += 1
        self.state_poll_index += 1
        return snapshot

    def locator(self, selector: str):
        if 'input[name="item_status"][value="draft"]' in selector:
            return _FakeDraftRadio(
                self,
                available=self.draft_radio_available,
                checked=self.is_draft_checked,
                click_sets_checked=self.draft_radio_click_sets_checked,
            )
        raise AssertionError(f"Unexpected locator selector: {selector}")

    async def goto(self, url: str, wait_until=None):
        self.gotos.append(url)

    async def wait_for_timeout(self, timeout: int):
        if timeout > 0:
            await asyncio.sleep(timeout / 1000)

    async def evaluate(self, script: str):
        if "__etsyDraftGridStateMarker" in script:
            return self._next_state()
        return self.cards


class _FakeSimplePage:
    def __init__(self, url):
        self.url = url


class _FakePhotoTabPage(_FakePageTimeoutMixin):
    def __init__(self):
        self.file_input = _FakeFileUploadLocator()

    def locator(self, selector: str):
        if "input[type=\"file\"]" in selector or "accept*\"image\"" in selector:
            return self.file_input
        raise AssertionError(f"Unexpected locator selector in fill_photo_tab: {selector}")


class _FakeSingleFormPage(_FakePageTimeoutMixin):
    def __init__(self):
        self.url = "https://www.etsy.com/your/shops/me/listing-editor/create"
        self.last_input_files = None
        self.gotos = []
        self.single_radio_clicked = False

    def locator(self, selector: str):
        if selector in {
            'input[name="listing_type_options_group"]',
            '#listing-type',
            '#listing-tags-input',
            '#listing-price-input, [data-testid="price-input"]',
            '#listing-quantity-input, input[name="quantity"]',
            '#listing-sku-input, input[name="sku"], [data-testid="sku-input"]',
            'input[name="listing_type_options_group"]',
            'input[type="file"]',
        }:
            return self
        raise AssertionError(f"Unexpected locator selector in single-form flow: {selector}")

    @property
    def first(self):
        return self

    def nth(self, index: int):
        if index == 1:
            return self
        raise AssertionError(f"Unexpected nth({index})")

    async def count(self):
        return 1

    async def is_visible(self):
        return True

    async def element_handle(self):
        return object()

    async def evaluate(self, script: str, *args):
        return None

    async def click(self, *args, **kwargs):
        self.single_radio_clicked = True

    async def goto(self, url: str, wait_until=None):
        self.gotos.append((url, wait_until))
        self.url = url

    async def set_input_files(self, files, timeout=None):
        self.last_input_files = list(files)


class TestDraftUrlMapping(unittest.IsolatedAsyncioTestCase):
    async def test_pick_draft_card_id_rejects_insufficient_fuzzy_margin(self):
        cards = [
            {"id": "111", "title": "digital planner for craft", "status": "draft"},
            {"id": "222", "title": "digital planner for crefts", "status": "draft"},
        ]
        selected = etsy_auto_post._pick_draft_card_id(cards, {"title": "digital planner for crafts"})
        self.assertIsNone(selected)

    async def test_pick_draft_card_id_accepts_strong_fuzzy_margin(self):
        cards = [
            {"id": "111", "title": "planner bundle planner for journaling", "status": "draft"},
            {"id": "222", "title": "planner bundle planning for journaling", "status": "draft"},
        ]
        selected = etsy_auto_post._pick_draft_card_id(cards, {"title": "planner bundle planner for journaling"})
        self.assertEqual("111", selected)

    async def test_pick_draft_card_prefers_draft_over_active_same_title(self):
        cards = [
            {"id": "4434273252", "title": "Product 389", "sku": "prod389", "status": "active"},
            {"id": "4540467541", "title": "Product 389", "sku": "prod389", "status": "draft"},
        ]
        self.assertEqual("4540467541", etsy_auto_post._pick_draft_card_id(cards, {"title": "Product 389", "sku": "prod389"}))

    async def test_get_newly_created_listing_url_prefers_matching_draft_id(self):
        product = {"title": "Product 389", "sku": "prod389"}
        with (
            patch.object(etsy_auto_post, "_collect_draft_cards", autospec=True) as collect,
            patch.object(etsy_auto_post, "_editor_product_signature_matches", autospec=True) as editor_match,
        ):
            collect.return_value = [
                {"id": "4434273252", "title": "Product 389", "sku": "", "status": "active"},
                {"id": "4540467541", "title": "Product 389", "sku": "prod389", "status": "draft"},
            ]
            editor_match.return_value = False
            url = await etsy_auto_post.get_newly_created_listing_url(_FakeSimplePage("https://www.etsy.com/your/shops/me/listing-editor/edit/9999999999"), product)
            self.assertEqual("https://www.etsy.com/listing/4540467541", url)

    async def test_get_newly_created_listing_url_returns_unverified_sentinel_on_ambiguous_match(self):
        product = {"title": "Product 389", "sku": "ambiguous"}
        with (
            patch.object(etsy_auto_post, "_collect_draft_cards", autospec=True) as collect,
            patch.object(etsy_auto_post, "_editor_product_signature_matches", autospec=True) as editor_match,
        ):
            collect.return_value = [
                {"id": "111", "title": "Product 389", "sku": "", "status": "draft"},
                {"id": "222", "title": "Product 389", "sku": "", "status": "draft"},
            ]
            editor_match.return_value = False
            url = await etsy_auto_post.get_newly_created_listing_url(_FakeSimplePage("https://www.etsy.com/your/shops/me/listing-editor/edit/9999999999"), product)
            self.assertEqual(etsy_auto_post.UNVERIFIED_DRAFT_URL_SENTINEL, url)

    async def test_check_duplicate_draft_uses_exact_match_not_first_active_link(self):
        product = {"title": "Product 389", "sku": "prod389"}
        with patch.object(etsy_auto_post, "_collect_draft_cards", autospec=True) as collect:
            collect.return_value = [
                {"id": "4434273252", "title": "Product 389", "sku": "prod389", "status": "active"},
                {"id": "4540467541", "title": "Product 389", "sku": "prod389", "status": "draft"},
            ]
            is_dup = await etsy_auto_post.check_duplicate_draft(_FakeSimplePage("https://www.etsy.com/your/shops/me/tools/listings"), product)
            self.assertTrue(is_dup)

    async def test_collect_draft_cards_forces_draft_filter(self):
        cards = [{"id": "4540467541", "title": "Product 389", "status": "draft"}]
        page = _FakeDraftFilterPage(cards)
        values = await etsy_auto_post._collect_draft_cards(page)
        self.assertTrue(page.gotos and etsy_auto_post.DRAFT_LISTINGS_URL in page.gotos[0])
        self.assertTrue(page.radio_clicked)
        self.assertEqual(cards, values)

    async def test_collect_draft_cards_deduplicates_same_id_and_merges_fields(self):
        cards = [
            {"id": "4540467541", "title": "", "sku": "", "status": "active"},
            {"id": "4540467541", "title": "Product 389", "sku": "prod389", "status": "draft"},
            {"id": "8888888888", "title": "Other", "sku": "", "status": "draft"},
        ]
        page = _FakeDraftFilterPage(cards)
        values = await etsy_auto_post._collect_draft_cards(page)
        values_by_id = {item["id"]: item for item in values}
        self.assertEqual("Product 389", values_by_id["4540467541"]["title"])
        self.assertEqual("prod389", values_by_id["4540467541"]["sku"])
        self.assertEqual("draft", values_by_id["4540467541"]["status"])
        self.assertEqual(2, len(values))

    async def test_collect_draft_cards_prefers_meaningful_title_for_same_listing_id(self):
        cards = [
            {"id": "4540467541", "title": "Edit", "status": "draft"},
            {"id": "4540467541", "title": "Product 389 Planner Set", "status": "draft"},
        ]
        page = _FakeDraftFilterPage(cards)
        values = await etsy_auto_post._collect_draft_cards(page)
        self.assertEqual(1, len(values))
        self.assertEqual("Product 389 Planner Set", values[0]["title"])

    async def test_collect_draft_cards_waits_for_late_drafts_before_snapshot(self):
        cards = [{"id": "4540467541", "title": "Product 389", "sku": "prod389", "status": "draft"}]
        page = _FakeDraftFilterPage(
            cards,
            state_snapshots=[
                {"loading": False, "ids": [], "emptyState": False},
                {"loading": False, "ids": [], "emptyState": False},
                {"loading": False, "ids": ["4540467541"], "emptyState": False},
                {"loading": False, "ids": ["4540467541"], "emptyState": False},
                {"loading": False, "ids": ["4540467541"], "emptyState": False},
                {"loading": False, "ids": ["4540467541"], "emptyState": False},
                {"loading": False, "ids": ["4540467541"], "emptyState": False},
                {"loading": False, "ids": ["4540467541"], "emptyState": False},
            ],
        )
        values = await etsy_auto_post._collect_draft_cards(page)
        self.assertEqual(1, len(values))
        self.assertEqual("4540467541", values[0]["id"])
        self.assertEqual(8, page.state_poll_count)

    async def test_collect_draft_cards_ignores_equal_count_stale_snapshot(self):
        cards = [{"id": "4540467541", "title": "Product 389", "sku": "prod389", "status": "draft"}]
        page = _FakeDraftFilterPage(
            cards,
            state_snapshots=[
                {"loading": False, "ids": ["4434273252"], "emptyState": False},
                {"loading": False, "ids": ["4434273252"], "emptyState": False},
                {"loading": False, "ids": ["4540467541"], "emptyState": False},
                {"loading": False, "ids": ["4540467541"], "emptyState": False},
                {"loading": False, "ids": ["4540467541"], "emptyState": False},
                {"loading": False, "ids": ["4540467541"], "emptyState": False},
                {"loading": False, "ids": ["4540467541"], "emptyState": False},
                {"loading": False, "ids": ["4540467541"], "emptyState": False},
            ],
        )
        values = await etsy_auto_post._collect_draft_cards(page)
        self.assertEqual("4540467541", values[0]["id"])
        self.assertGreater(page.state_poll_count, 2)

    async def test_collect_draft_cards_marks_empty_state_without_anchors(self):
        page = _FakeDraftFilterPage(
            [],
            state_snapshots=[
                {"loading": True, "ids": [], "emptyState": False},
                {"loading": False, "ids": [], "emptyState": False},
                {"loading": False, "ids": [], "emptyState": True},
                {"loading": False, "ids": [], "emptyState": True},
                {"loading": False, "ids": [], "emptyState": True},
            ],
        )
        values = await etsy_auto_post._collect_draft_cards(page)
        self.assertEqual([], values)
        self.assertGreaterEqual(page.state_poll_count, 5)

    async def test_check_duplicate_draft_resolves_duplicate_listing_id_as_duplicate(self):
        product = {"title": "Product 389", "sku": "prod389"}
        page = _FakeDraftFilterPage([
            {"id": "4540467541", "title": "Product 389", "sku": "", "status": "draft"},
            {"id": "4540467541", "title": "Product 389", "sku": "prod389", "status": "draft"},
        ])
        is_dup = await etsy_auto_post.check_duplicate_draft(page, product)
        self.assertTrue(is_dup)


class TestDraftFilterSafety(unittest.IsolatedAsyncioTestCase):
    async def test_force_draft_filter_rejects_missing_draft_radio(self):
        page = _FakeDraftFilterPage(cards=[], draft_radio_available=False)
        with self.assertRaises(RuntimeError) as ctx:
            await etsy_auto_post._force_draft_filter(page)
        self.assertIn("Không tìm thấy bộ lọc Draft", str(ctx.exception))

    async def test_force_draft_filter_rejects_unchecked_draft_radio(self):
        page = _FakeDraftFilterPage(cards=[], click_sets_checked=False)
        with self.assertRaises(RuntimeError) as ctx:
            await etsy_auto_post._force_draft_filter(page)
        self.assertIn("Không xác nhận được filter Draft đã được bật", str(ctx.exception))


class TestDuplicateFailureAndSaveSelector(unittest.IsolatedAsyncioTestCase):
    async def test_check_duplicate_draft_returns_failure_sentinel_on_error(self):
        product = {"title": "Product 389", "sku": "prod389"}
        with patch.object(etsy_auto_post, "_collect_draft_cards", autospec=True) as collect:
            collect.side_effect = RuntimeError("boom")
            status = await etsy_auto_post.check_duplicate_draft(_FakeSimplePage("https://www.etsy.com/your/shops/me/tools/listings"), product)
            self.assertEqual(etsy_auto_post.DRAFT_DUPLICATE_CHECK_FAILED_SENTINEL, status)

    def test_save_button_selector_omits_publish_and_allows_edit_save_changes(self):
        create_selector = etsy_auto_post._get_save_button_selector(explicit_edit=False)
        edit_selector = etsy_auto_post._get_save_button_selector(explicit_edit=True)

        self.assertIn('button:has-text("Save draft")', create_selector)
        self.assertIn('button:has-text("Save as draft")', create_selector)
        self.assertNotIn("Publish", create_selector)
        self.assertNotIn('button:has-text("Save changes")', create_selector)

        self.assertIn('button:has-text("Save changes")', edit_selector)


class TestPhotoUploadVerification(unittest.IsolatedAsyncioTestCase):
    async def test_fill_photo_tab_raises_when_upload_incomplete_create_mode(self):
        page = _FakePhotoTabPage()
        product = {"image_paths": [f"/tmp/img_{idx}.jpg" for idx in range(10)]}

        with (
            patch.object(etsy_auto_post, "click_tab", AsyncMock()),
            patch.object(etsy_auto_post, "_count_listing_image_thumbs", AsyncMock(return_value=0)),
            patch.object(etsy_auto_post, "_wait_for_expected_image_count", AsyncMock(return_value=False)),
        ):
            with self.assertRaises(RuntimeError):
                await etsy_auto_post.fill_photo_tab(page, product, explicit_edit=False)

    async def test_fill_photo_tab_explicit_edit_expected_count_is_not_additive(self):
        page = _FakePhotoTabPage()
        product = {"image_paths": [f"/tmp/img_{idx}.jpg" for idx in range(3)]}

        with (
            patch.object(etsy_auto_post, "click_tab", AsyncMock()),
            patch.object(etsy_auto_post, "_count_listing_image_thumbs", AsyncMock(return_value=4)),
            patch.object(etsy_auto_post, "_wait_for_expected_image_count", AsyncMock(return_value=True)) as wait_mock,
            patch.object(etsy_auto_post, "fill_image_alt_texts", AsyncMock()),
        ):
            await etsy_auto_post.fill_photo_tab(page, product, explicit_edit=True)
            wait_mock.assert_awaited_once_with(page, expected_count=4, exact=False)

    async def test_fill_listing_single_form_raises_when_upload_incomplete(self):
        page = _FakeSingleFormPage()
        product = {
            "folder": "product-389",
            "title": "Product 389",
            "description": "test",
            "price": 4.99,
            "qty": 1,
            "keywords": "",
            "tags": "",
            "image_paths": [f"/tmp/img_{idx}.jpg" for idx in range(10)],
            "pdf_paths": [],
            "row": 389,
            "sku": "prod389",
        }

        with (
            patch.object(etsy_auto_post, "check_duplicate_draft", AsyncMock(return_value=False)),
            patch.object(etsy_auto_post, "detect_form_type", AsyncMock(return_value="single")),
            patch.object(etsy_auto_post, "smart_fill", AsyncMock(return_value=True)),
            patch.object(etsy_auto_post, "fill_translations", AsyncMock()),
            patch.object(etsy_auto_post, "_count_listing_image_thumbs", AsyncMock(return_value=2)),
            patch.object(etsy_auto_post, "_wait_for_expected_image_count", AsyncMock(return_value=False)),
        ):
            with self.assertRaises(RuntimeError):
                await etsy_auto_post.fill_listing(page, product)


if __name__ == "__main__":
    unittest.main()
