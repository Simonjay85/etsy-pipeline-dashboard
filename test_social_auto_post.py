#!/usr/bin/env python3
from __future__ import annotations

import unittest

import social_auto_post


class _FakeLocator:
    def __init__(
        self,
        *,
        text: str = "",
        visible: bool = False,
        click_forbidden: bool = False,
        attrs: dict[str, str | None] | None = None,
        disabled: bool = False,
    ):
        self.text = text
        self.visible = visible
        self.attrs = attrs or {}
        self.disabled = disabled
        self.clicked = False
        self.filled_values: list[str] = []
        self.click_forbidden = click_forbidden

    @property
    def first(self) -> "_FakeLocator":
        return self

    async def count(self) -> int:
        return 1 if self.visible else 0

    async def is_visible(self) -> bool:
        return self.visible

    async def get_attribute(self, name: str):
        return self.attrs.get(name)

    async def is_disabled(self) -> bool:
        return self.disabled

    async def scroll_into_view_if_needed(self):
        return None

    async def click(self, *_, **__):
        if self.click_forbidden:
            raise RuntimeError("click blocked")
        self.clicked = True

    async def text_content(self):
        return self.text


class _FakePage:
    def __init__(
        self,
        url: str,
        locator_map: dict[str, _FakeLocator],
        *,
        evaluate_result: bool = False,
    ):
        self.url = url
        self._locator_map = locator_map
        self.wait_calls: list[int] = []
        self.evaluate_result = evaluate_result
        self.evaluate_calls: list[tuple[str, str]] = []

    def locator(self, selector: str) -> _FakeLocator:
        return self._locator_map.get(selector, _FakeLocator())

    async def wait_for_timeout(self, ms: int):
        self.wait_calls.append(ms)

    async def evaluate(self, script: str, argument: str):
        self.evaluate_calls.append((script, argument))
        return self.evaluate_result


class TestNormalizePinterestTitle(unittest.IsolatedAsyncioTestCase):
    def test_short_title_unchanged(self):
        title = "12 Reindeer Silhouette SVG Bundle"
        self.assertEqual(
            title,
            social_auto_post._normalize_pinterest_title(title, max_length=100),
        )

    def test_long_title_trims_word_boundary(self):
        title = (
            "12 Reindeer Silhouette SVG Bundle Christmas Wallpaper Decoration Template "
            "and Digital Planner Cute Winter Art Printable"
        )

        normalized = social_auto_post._normalize_pinterest_title(title, max_length=100)

        self.assertLessEqual(len(normalized), 100)
        self.assertTrue(title.startswith(normalized))
        self.assertNotEqual(normalized, "")
        self.assertLess(len(normalized), len(title))
        if len(normalized) < len(title):
            self.assertEqual(title[len(normalized)], " ")

    async def test_disabled_publish_not_clicked_and_shows_validation_message(self):
        page = _FakePage(
            url="https://www.pinterest.com/pin-builder/",
            locator_map={
                '[data-test-id="board-dropdown-save-button"]': _FakeLocator(
                    visible=True,
                    attrs={"aria-disabled": "true", "disabled": None},
                ),
                '[role="alert"]': _FakeLocator(
                    visible=True,
                    text="Ooops! This title is getting long. Try trimming it down.",
                ),
            },
        )

        clicked, msg = await social_auto_post._click_pinterest_publish(page)

        self.assertFalse(clicked)
        self.assertIn("ooops", msg.lower())
        self.assertIn("trimming", msg.lower())
        self.assertFalse(page.locator('[data-test-id="board-dropdown-save-button"]').clicked)

    async def test_validation_prompt_returns_immediately_in_publish_wait(self):
        page = _FakePage(
            url="https://www.pinterest.com/pin-builder/",
            locator_map={
                '[role="alert"]': _FakeLocator(
                    visible=True,
                    text="You must fix the highlighted issues before publishing.",
                )
            },
        )

        published, msg = await social_auto_post._wait_for_pinterest_publish_result(
            page, timeout_ms=600
        )

        self.assertFalse(published)
        self.assertIn("you must fix", msg.lower())

    async def test_real_pin_url_is_recognized_as_publish_success(self):
        page = _FakePage(
            url="https://www.pinterest.com/pin/123456789012345678/",
            locator_map={},
        )

        published, msg = await social_auto_post._wait_for_pinterest_publish_result(
            page, timeout_ms=10, poll_ms=1
        )

        self.assertTrue(published)
        self.assertEqual(msg, "https://www.pinterest.com/pin/123456789012345678/")

    async def test_ca_subdomain_pin_url_is_recognized_as_publish_success(self):
        page = _FakePage(
            url="https://ca.pinterest.com/pin/888475832769175963/?utm_campaign=test",
            locator_map={},
        )

        published, msg = await social_auto_post._wait_for_pinterest_publish_result(
            page, timeout_ms=10, poll_ms=1
        )

        self.assertTrue(published)
        self.assertEqual(msg, "https://ca.pinterest.com/pin/888475832769175963/")

    async def test_lookalike_pinterest_domain_is_rejected(self):
        page = _FakePage(
            url="https://evilpinterest.com/pin/123456789012345678/",
            locator_map={},
        )

        published, msg = await social_auto_post._wait_for_pinterest_publish_result(
            page, timeout_ms=10, poll_ms=1
        )

        self.assertFalse(published)
        self.assertIsNone(msg)

        page = _FakePage(
            url="https://pinterest.com.evil/pin/123456789012345678/",
            locator_map={},
        )

        published, msg = await social_auto_post._wait_for_pinterest_publish_result(
            page, timeout_ms=10, poll_ms=1
        )

        self.assertFalse(published)
        self.assertIsNone(msg)

    async def test_relative_pin_url_on_pin_link_is_recognized_as_publish_success(self):
        page = _FakePage(
            url="https://www.pinterest.com/pin-builder/",
            locator_map={
                'a:has-text("See your Pin")': _FakeLocator(
                    visible=True,
                    attrs={"href": "/pin/888475832769175963"},
                    text="See your Pin",
                )
            },
        )

        published, msg = await social_auto_post._wait_for_pinterest_publish_result(
            page, timeout_ms=10, poll_ms=1
        )

        self.assertTrue(published)
        self.assertEqual(msg, "https://www.pinterest.com/pin/888475832769175963")

    async def test_strict_exact_confirmation_text(self):
        page = _FakePage(
            url="https://www.pinterest.com/pin-builder/",
            locator_map={
                '[role="status"]': _FakeLocator(
                    visible=True,
                    text="You created a Pin!",
                )
            },
        )

        published, msg = await social_auto_post._wait_for_pinterest_publish_result(
            page, timeout_ms=10, poll_ms=1
        )

        self.assertTrue(published)
        self.assertIsNone(msg)

    async def test_related_created_text_is_not_enough(self):
        page = _FakePage(
            url="https://www.pinterest.com/pin-builder/",
            locator_map={
                '[role="status"]': _FakeLocator(
                    visible=True,
                    text="You have successfully created a board, but not a Pin.",
                )
            },
        )

        published, msg = await social_auto_post._wait_for_pinterest_publish_result(
            page, timeout_ms=10, poll_ms=1
        )

        self.assertFalse(published)
        self.assertIsNone(msg)

    async def test_invalid_pin_link_is_not_success(self):
        page = _FakePage(
            url="https://www.pinterest.com/pin-builder/",
            locator_map={
                'a:has-text("See your Pin")': _FakeLocator(
                    visible=True,
                    text="See your Pin",
                    attrs={"href": "/pins/888475832769175963"},
                )
            },
        )

        published, msg = await social_auto_post._wait_for_pinterest_publish_result(
            page, timeout_ms=10, poll_ms=1
        )

        self.assertFalse(published)
        self.assertIsNone(msg)

    async def test_unrelated_success_node_is_not_publish_success(self):
        page = _FakePage(
            url="https://www.pinterest.com/business/hub/",
            locator_map={
                '[data-test-id*="success" i]': _FakeLocator(
                    visible=True,
                    text="Account settings saved successfully.",
                )
            },
        )

        published, msg = await social_auto_post._wait_for_pinterest_publish_result(
            page, timeout_ms=10, poll_ms=1
        )

        self.assertFalse(published)
        self.assertIsNone(msg)

    async def test_native_disabled_publish_is_not_clicked(self):
        publish = _FakeLocator(
            visible=True,
            attrs={"disabled": ""},
            disabled=True,
            click_forbidden=True,
        )
        page = _FakePage(
            url="https://www.pinterest.com/pin-builder/",
            locator_map={
                '[data-test-id="board-dropdown-save-button"]': publish,
            },
        )

        published, msg = await social_auto_post._click_pinterest_publish(page)

        self.assertFalse(published)
        self.assertIn("vô hiệu hóa", msg)
        self.assertFalse(publish.clicked)

    async def test_dom_title_fallback_passes_value_as_evaluate_argument(self):
        title = """Teacher's "Best" Planner; window.injected = true"""
        page = _FakePage(
            url="https://www.pinterest.com/pin-builder/",
            locator_map={},
            evaluate_result=True,
        )

        filled = await social_auto_post._fill_pinterest_title_with_dom_events(
            page, title
        )

        self.assertTrue(filled)
        script, argument = page.evaluate_calls[0]
        self.assertEqual(argument, title)
        self.assertNotIn(title, script)
        self.assertIn('new Event("input"', script)
        self.assertIn('new Event("change"', script)

    async def test_board_selection_never_uses_publish_save_button(self):
        save_button = _FakeLocator(visible=True, click_forbidden=True)
        page = _FakePage(
            url="https://www.pinterest.com/pin-builder/",
            locator_map={
                '[data-test-id="board-dropdown-save-button"]': save_button,
            },
        )

        selected = await social_auto_post._select_default_pinterest_board_if_needed(
            page
        )

        self.assertFalse(selected)
        self.assertFalse(save_button.clicked)

    async def test_board_selection_preserves_already_selected_board(self):
        selected_board = _FakeLocator(
            visible=True,
            text="Christmas SVG",
            click_forbidden=True,
        )
        page = _FakePage(
            url="https://www.pinterest.com/pin-builder/",
            locator_map={
                '[data-test-id="board-dropdown-select-button"]': selected_board,
            },
        )

        selected = await social_auto_post._select_default_pinterest_board_if_needed(
            page
        )

        self.assertFalse(selected)
        self.assertFalse(selected_board.clicked)


class TestPinterestDescriptionNormalization(unittest.TestCase):
    def test_short_description_is_unchanged_within_limit(self):
        title = "12 Reindeer Silhouette SVG Bundle"
        description = (
            "Get a cozy holiday-inspired collection of printable SVG files perfect for "
            "digital crafting and instant download."
        )
        tags = "christmas, svg, reindeer, printable, winter"
        etsy_url = "https://www.etsy.com/shop/Templystudios"

        raw = social_auto_post.make_pinterest_description(title, description, tags, etsy_url)
        normalized, was_truncated = social_auto_post._normalize_pinterest_description(
            title,
            description,
            tags,
            etsy_url,
            max_length=social_auto_post.PINTEREST_DESCRIPTION_MAX_LENGTH,
        )

        self.assertFalse(was_truncated)
        self.assertEqual(raw, normalized)
        self.assertIn("🛒 Shop now → https://www.etsy.com/shop/Templystudios", raw)
        self.assertLessEqual(len(raw), social_auto_post.PINTEREST_DESCRIPTION_MAX_LENGTH)

    def test_row_36_style_long_description_keeps_cta_and_stays_within_limit(self):
        title = "12 Reindeer Silhouette SVG Bundle"
        long_sentence = "word " * 260
        description = (f"{long_sentence}. " * 3).strip()
        tags = "reindeer,svg,reusable,printable,christmas,dl,clipart,png,vector"
        etsy_url = "https://www.etsy.com/shop/Templystudios"

        normalized, was_truncated = social_auto_post._normalize_pinterest_description(
            title,
            description,
            tags,
            etsy_url,
            max_length=800,
        )

        self.assertLessEqual(len(normalized), 800)
        self.assertTrue(was_truncated)
        self.assertTrue(normalized.endswith(etsy_url))
        self.assertIn(title, normalized)
        self.assertIn("Instant Digital Download", normalized)

    def test_word_boundary_trimming_keeps_complete_words(self):
        title = "T"
        description = (
            "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu"
        )
        tags = "tag1,tag2,tag3"
        etsy_url = "https://e.ly/1"

        normalized, was_truncated = social_auto_post._normalize_pinterest_description(
            title,
            description,
            tags,
            etsy_url,
            max_length=120,
        )
        self.assertTrue(was_truncated)

        before_cta = normalized.split("🛒 Shop now →", 1)[0].strip()
        parts = [part for part in before_cta.split("\n\n") if part]
        # parts are title, short description, and keyword line
        self.assertGreaterEqual(len(parts), 2)
        short_desc = parts[1]
        self.assertTrue(short_desc.startswith("alpha beta gamma delta"))

    def test_long_url_and_tiny_budget_preserves_link_tail(self):
        title = "Tiny budget test"
        description = "A tiny description that should be dropped when budget is very low."
        tags = "tiny,budget,link"
        etsy_url = "https://www.etsy.com/shop/Templystudios?" + ("x" * 300)
        expected_suffix = f"🛒 Shop now → {etsy_url}"[-20:]

        normalized, was_truncated = social_auto_post._normalize_pinterest_description(
            title,
            description,
            tags,
            etsy_url,
            max_length=20,
        )

        self.assertTrue(was_truncated)
        self.assertLessEqual(len(normalized), 20)
        self.assertEqual(normalized, expected_suffix)

    def test_png_oriented_title_and_tags_dont_include_printable_pdf(self):
        title = "PNG Cute Reindeer Set"
        description = "Cute reindeer SVG set, ideal for instant downloads and digital projects."
        tags = "png,svg,digital,printable"
        etsy_url = "https://www.etsy.com/shop/Templystudios"

        normalized, was_truncated = social_auto_post._normalize_pinterest_description(
            title,
            description,
            tags,
            etsy_url,
            max_length=800,
        )

        self.assertLessEqual(len(normalized), 800)
        self.assertFalse(was_truncated)
        self.assertNotIn("Printable PDF", normalized)
        self.assertIn("Instant Digital Download", normalized)
        self.assertIn(f"🛒 Shop now → {etsy_url}", normalized)


if __name__ == "__main__":
    unittest.main()
