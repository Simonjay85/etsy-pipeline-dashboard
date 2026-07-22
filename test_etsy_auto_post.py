#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import inspect
import tempfile
import types
import sys
import unittest
from unittest.mock import patch

if "deep_translator" not in sys.modules:
    _fake_google = types.ModuleType("deep_translator")

    class _FakeGoogleTranslator:
        def __init__(self, source: str, target: str):
            self.source = source
            self.target = target

        def translate(self, text: str):
            return str(text)

    _fake_google.GoogleTranslator = _FakeGoogleTranslator
    sys.modules["deep_translator"] = _fake_google

if "google" not in sys.modules:
    _fake_google_pkg = types.ModuleType("google")
    _fake_google_genai = types.ModuleType("google.genai")
    _fake_google_types = types.ModuleType("google.genai.types")
    _fake_google_genai.types = _fake_google_types
    _fake_google_pkg.genai = _fake_google_genai
    sys.modules["google"] = _fake_google_pkg
    sys.modules["google.genai"] = _fake_google_genai
    sys.modules["google.genai.types"] = _fake_google_types

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


if __name__ == "__main__":
    unittest.main()
