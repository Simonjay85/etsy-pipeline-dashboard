#!/usr/bin/env python3
"""Focused regression tests for single-sync Etsy scrape session routing and identity gates."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock
from unittest.mock import patch

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import dashboard_app


class _FakeLocator:
    def __init__(self, value: str | None):
        self._value = value

    @property
    def first(self) -> "_FakeLocator":
        return self

    async def count(self) -> int:
        return 1 if self._value is not None else 0

    async def input_value(self) -> str:
        return str(self._value or "")

    async def inner_text(self) -> str:
        return str(self._value or "")


class _FakePage:
    def __init__(
        self,
        *,
        url: str,
        force_goto_url: str | None = None,
        force_goto_urls: list[str] | None = None,
        title: str = "",
        description: str = "",
        tags: list[str] | None = None,
        shop_slugs: list[str] | None = None,
        manager_shop_slugs: list[str] | None = None,
        editor_shop_slugs: list[str] | None = None,
        content_html: str = "<html><body></body></html>",
        wait_for_selector_error: Exception | None = None,
        locator_values: dict[str, str] | None = None,
        section_text: str = "",
        manager_shop_slugs_sequence: list[list[str]] | None = None,
    ):
        self.url = url
        if force_goto_urls is not None:
            self.force_goto_urls = force_goto_urls[:]
        else:
            self.force_goto_urls = None
        self.goto_calls: list[tuple[str, str | None, int | None]] = []
        self.force_goto_url = force_goto_url
        self.wait_for_selector_error = wait_for_selector_error
        self.title = title
        self.description = description
        self.tags = tags or []
        self.shop_slugs = shop_slugs or []
        self.manager_shop_slugs = manager_shop_slugs
        self.manager_shop_slugs_sequence = manager_shop_slugs_sequence
        self.editor_shop_slugs = editor_shop_slugs
        self.section_text = section_text
        self.content_html = content_html
        self.locator_values = locator_values or {}
        self._manager_shop_slug_poll = 0

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self.locator_values.get(selector))

    def _pick_manager_shop_slugs(self) -> list[str]:
        if self.manager_shop_slugs_sequence is not None:
            if not self.manager_shop_slugs_sequence:
                return self.manager_shop_slugs or []
            index = min(self._manager_shop_slug_poll, len(self.manager_shop_slugs_sequence) - 1)
            self._manager_shop_slug_poll += 1
            return self.manager_shop_slugs_sequence[index]
        return self.manager_shop_slugs or []

    async def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None):
        if self.force_goto_urls is not None and self.force_goto_urls:
            self.url = self.force_goto_urls.pop(0)
        elif self.force_goto_url is not None:
            self.url = self.force_goto_url
        else:
            self.url = url
        self.goto_calls.append((url, wait_until, timeout))

    async def wait_for_selector(self, selector: str, timeout: int | None = None):
        if self.wait_for_selector_error is not None:
            raise self.wait_for_selector_error

    async def wait_for_timeout(self, ms: int) -> None:
        pass

    async def content(self) -> str:
        return self.content_html

    async def evaluate(self, script: str) -> Any:
        if "document?.body?.innerText" in script:
            return self.section_text
        if "/shop/" in script:
            if "/your/shops/me/tools/listings" in self.url:
                return self._pick_manager_shop_slugs()
            if "/your/shops/me/listing-editor/edit/" in self.url and self.editor_shop_slugs is not None:
                return self.editor_shop_slugs
            return self.shop_slugs
        if "let tagSection = null;" in script:
            return self.tags
        if "selectedText = await" in script:
            return ""
        if "selectedText" in script:
            return self.section_text
        return ""


class _FakeContext:
    def __init__(self, page: _FakePage):
        self.pages = [page]
        self.close = AsyncMock()
        self.new_page = AsyncMock(return_value=page)


class _FakeBrowser:
    def __init__(self, context: _FakeContext):
        self.contexts = [context]
        self.new_context = AsyncMock(return_value=context)


class _FakePlaywright:
    def __init__(self, browser: _FakeBrowser):
        self.chromium = Mock()
        self.chromium.connect_over_cdp = AsyncMock(return_value=browser)
        self.chromium.launch_persistent_context = AsyncMock()
        self.chromium.launch = AsyncMock()
        self.stop = AsyncMock()


class _FakeLauncher:
    def __init__(self, playwright: _FakePlaywright):
        self._playwright = playwright
        self.start = AsyncMock(return_value=playwright)


class _ExistingListingWorksheet:
    max_row = 4

    def __init__(self, listing_id: str, prior_status: str) -> None:
        self._values = {
            (4, 2): "product-01",
            (4, 14): prior_status,
            (4, 16): f"https://www.etsy.com/listing/{listing_id}",
        }

    def cell(self, row: int, column: int) -> SimpleNamespace:
        return SimpleNamespace(value=self._values.get((row, column)))


class _ExistingListingWorkbook:
    def __init__(self, worksheet: _ExistingListingWorksheet) -> None:
        self.worksheet = worksheet

    def __getitem__(self, name: str) -> _ExistingListingWorksheet:
        if name != "Listings":
            raise KeyError(name)
        return self.worksheet


class _JsonRequest:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    async def json(self) -> dict[str, object]:
        return self.payload


class TestScrapeListingSingleSync(IsolatedAsyncioTestCase):
    def _build_playwright(self, page: _FakePage) -> tuple[_FakeLauncher, _FakePlaywright]:
        context = _FakeContext(page)
        browser = _FakeBrowser(context)
        playwright = _FakePlaywright(browser)
        return _FakeLauncher(playwright), playwright

    async def test_exact_session_not_ready_raises_actionable_error_and_no_persistent_context(self):
        with patch.object(dashboard_app, "_active_shop_id", "templystudios"), \
            patch.object(dashboard_app, "SHOPS", {
                "templystudios": {
                    "name": "Temply",
                    "etsy_link": "https://www.etsy.com/shop/templystudios",
                    "etsy_login_debug_port": 43123,
                }
            }), \
            patch.object(dashboard_app, "is_etsy_session_ready", return_value=False), \
            patch("playwright.async_api.async_playwright") as launch_factory:

            page = _FakePage(url="https://www.etsy.com/your/shops/me/listing-editor/edit/123")
            launcher, playwright = self._build_playwright(page)
            launch_factory.return_value = launcher

            with self.assertRaisesRegex(
                RuntimeError,
                "Phiên Etsy chưa sẵn sàng",
            ):
                await dashboard_app.scrape_listing_details("123", shop_id="templystudios")

            launch_factory.return_value.start.assert_awaited_once()
            launch_factory.return_value._playwright.chromium.connect_over_cdp.assert_not_called()
            launch_factory.return_value._playwright.chromium.launch_persistent_context.assert_not_called()
            playwright.stop.assert_awaited_once()

    async def test_exact_session_ready_uses_resolved_cdp_url_not_default_9222(self):
        with patch.object(dashboard_app, "_active_shop_id", "templystudios"), \
            patch.object(dashboard_app, "SHOPS", {
                "templystudios": {
                    "name": "Temply",
                    "etsy_link": "https://www.etsy.com/shop/templystudios",
                    "etsy_login_debug_port": 43123,
                }
            }), \
            patch.object(dashboard_app, "is_etsy_session_ready", return_value=True), \
            patch("playwright.async_api.async_playwright") as launch_factory:

            page = _FakePage(
                url="https://www.etsy.com/your/shops/me/listing-editor/edit/123",
                title="My Listing",
                description="Hello Etsy",
                locator_values={
                    'textarea[name="title"]': "My Listing",
                    'textarea[name="description"]': "Hello Etsy",
                    '#listing-price-input': "12.5",
                    '#listing-quantity-input': "3",
                    "body": "My Listing",
                },
                tags=["tag-a", "tag-b"],
                manager_shop_slugs=["templystudios"],
                editor_shop_slugs=[],
            )
            launcher, _ = self._build_playwright(page)
            launch_factory.return_value = launcher

            result = await dashboard_app.scrape_listing_details("123", shop_id="templystudios")
            launch_factory.return_value._playwright.chromium.connect_over_cdp.assert_awaited_once_with(
                "http://127.0.0.1:43123",
                timeout=3000,
            )
            launch_factory.return_value._playwright.chromium.launch_persistent_context.assert_not_called()
            self.assertEqual("My Listing", result.get("title"))
            self.assertEqual("Hello Etsy", result.get("description"))
            self.assertNotIn(
                "9222",
                str(launch_factory.return_value._playwright.chromium.connect_over_cdp.await_args.args[0]),
            )

    async def test_sign_in_redirect_classified_before_identity(self):
        with patch.object(dashboard_app, "_active_shop_id", "templystudios"), \
            patch.object(dashboard_app, "SHOPS", {
                "templystudios": {
                    "name": "Temply",
                    "etsy_link": "https://www.etsy.com/shop/templystudios",
                    "etsy_login_debug_port": 43123,
                }
            }), \
            patch.object(dashboard_app, "is_etsy_session_ready", return_value=True), \
            patch("playwright.async_api.async_playwright") as launch_factory:

            page = _FakePage(
                url="https://www.etsy.com/signin",
                force_goto_url="https://www.etsy.com/signin",
                title="",
                description="",
            )
            launcher, _ = self._build_playwright(page)
            launch_factory.return_value = launcher

            with self.assertRaisesRegex(RuntimeError, "Phiên Etsy chưa đăng nhập"):
                await dashboard_app.scrape_listing_details("123", shop_id="templystudios")

    async def test_wrong_editor_listing_or_title_timeout_classified_before_identity(self):
        with patch.object(dashboard_app, "_active_shop_id", "templystudios"), \
            patch.object(dashboard_app, "SHOPS", {
                "templystudios": {
                    "name": "Temply",
                    "etsy_link": "https://www.etsy.com/shop/templystudios",
                    "etsy_login_debug_port": 43123,
                }
            }), \
            patch.object(dashboard_app, "is_etsy_session_ready", return_value=True), \
            patch("playwright.async_api.async_playwright") as launch_factory:

            page = _FakePage(
                url="https://www.etsy.com/your/shops/me/listing-editor/edit/999",
                force_goto_urls=[
                    "https://www.etsy.com/your/shops/me/tools/listings",
                    "https://www.etsy.com/your/shops/me/listing-editor/edit/999",
                ],
                title="Other",
                description="Oops",
                wait_for_selector_error=PlaywrightTimeoutError("not ready"),
                locator_values={
                    'textarea[name="title"]': "Other",
                    'textarea[name="description"]': "Oops",
                    '#listing-price-input': "5",
                    '#listing-quantity-input': "1",
                    "body": "Other editor loaded",
                },
                tags=["tag-a"],
                manager_shop_slugs=["templystudios"],
                editor_shop_slugs=["templystudios"],
            )
            launcher, _ = self._build_playwright(page)
            launch_factory.return_value = launcher

            with self.assertRaisesRegex(RuntimeError, "Editor sai listing"):
                await dashboard_app.scrape_listing_details("123", shop_id="templystudios")

            page.force_goto_urls = [
                "https://www.etsy.com/your/shops/me/tools/listings",
                "https://www.etsy.com/your/shops/me/listing-editor/edit/123",
            ]
            page.wait_for_selector_error = PlaywrightTimeoutError("not ready")
            with self.assertRaisesRegex(RuntimeError, "Không nạp được giao diện Listing Editor"):
                await dashboard_app.scrape_listing_details("123", shop_id="templystudios")

    async def test_shop_manager_preflight_wrong_shop_fails(self):
        with patch.object(dashboard_app, "_active_shop_id", "templystudios"), \
            patch.object(dashboard_app, "SHOPS", {
                "templystudios": {
                    "name": "Temply",
                    "etsy_link": "https://www.etsy.com/shop/templystudios",
                    "etsy_login_debug_port": 43123,
                }
            }), \
            patch.object(dashboard_app, "is_etsy_session_ready", return_value=True), \
            patch("playwright.async_api.async_playwright") as launch_factory:

            manager_wrong_shop_page = _FakePage(
                url="https://www.etsy.com/your/shops/me/listing-editor/edit/123",
                title="A",
                description="B",
                locator_values={
                    'textarea[name="title"]': "A",
                    'textarea[name="description"]': "B",
                    '#listing-price-input': "10",
                    '#listing-quantity-input': "2",
                    "body": "A",
                },
                tags=["tag-a"],
                manager_shop_slugs=["wrongshop"],
                editor_shop_slugs=[],
            )
            launcher_wrong, _ = self._build_playwright(manager_wrong_shop_page)
            launch_factory.return_value = launcher_wrong
            with self.assertRaisesRegex(RuntimeError, "Phiên Etsy sai shop"):
                await dashboard_app.scrape_listing_details("123", shop_id="templystudios")

    async def test_shop_manager_preflight_no_identity_evidence_fails(self):
        with patch.object(dashboard_app, "_active_shop_id", "templystudios"), \
            patch.object(dashboard_app, "SHOPS", {
                "templystudios": {
                    "name": "Temply",
                    "etsy_link": "https://www.etsy.com/shop/templystudios",
                    "etsy_login_debug_port": 43123,
                }
            }), \
            patch.object(dashboard_app, "is_etsy_session_ready", return_value=True), \
            patch("playwright.async_api.async_playwright") as launch_factory:

            manager_empty_shop_page = _FakePage(
                url="https://www.etsy.com/your/shops/me/listing-editor/edit/123",
                title="A",
                description="B",
                locator_values={
                    'textarea[name="title"]': "A",
                    'textarea[name="description"]': "B",
                    '#listing-price-input': "10",
                    '#listing-quantity-input': "2",
                    "body": "A",
                },
                tags=["tag-a"],
                manager_shop_slugs=[],
                editor_shop_slugs=[],
            )
            launcher_no_identity, _ = self._build_playwright(manager_empty_shop_page)
            launch_factory.return_value = launcher_no_identity
            with self.assertRaisesRegex(RuntimeError, "chưa sẵn sàng/chưa xác minh"):
                await dashboard_app.scrape_listing_details("123", shop_id="templystudios")

    async def test_editor_anchorless_passes_only_after_verified_shop_manager_preflight(self):
        with patch.object(dashboard_app, "_active_shop_id", "templystudios"), \
            patch.object(dashboard_app, "SHOPS", {
                "templystudios": {
                    "name": "Temply",
                    "etsy_link": "https://www.etsy.com/shop/templystudios",
                    "etsy_login_debug_port": 43123,
                }
            }), \
            patch.object(dashboard_app, "is_etsy_session_ready", return_value=True), \
            patch("playwright.async_api.async_playwright") as launch_factory:

            missing_editor_anchor_page = _FakePage(
                url="https://www.etsy.com/your/shops/me/listing-editor/edit/123",
                title="My Listing",
                description="Desc",
                locator_values={
                    'textarea[name="title"]': "My Listing",
                    'textarea[name="description"]': "Desc",
                    '#listing-price-input': "10",
                    '#listing-quantity-input': "2",
                    "body": "My Listing",
                },
                tags=["tag-a"],
                manager_shop_slugs=["templystudios"],
                editor_shop_slugs=[],
            )
            launcher_anchorless, _ = self._build_playwright(missing_editor_anchor_page)
            launch_factory.return_value = launcher_anchorless
            details = await dashboard_app.scrape_listing_details("123", shop_id="templystudios")
            self.assertEqual("My Listing", details["title"])
            missing_editor_anchor_page.manager_shop_slugs = []
            launch_factory.return_value = launcher_anchorless
            with self.assertRaisesRegex(RuntimeError, "chưa sẵn sàng/chưa xác minh"):
                await dashboard_app.scrape_listing_details("123", shop_id="templystudios")


    async def test_expected_and_wrong_shop_anchor_in_editor_fails(self):
        with patch.object(dashboard_app, "_active_shop_id", "templystudios"), \
            patch.object(dashboard_app, "SHOPS", {
                "templystudios": {
                    "name": "Temply",
                    "etsy_link": "https://www.etsy.com/shop/templystudios",
                    "etsy_login_debug_port": 43123,
                }
            }), \
            patch.object(dashboard_app, "is_etsy_session_ready", return_value=True), \
            patch("playwright.async_api.async_playwright") as launch_factory:

            mixed_anchor_page = _FakePage(
                url="https://www.etsy.com/your/shops/me/listing-editor/edit/123",
                title="A",
                description="B",
                locator_values={
                    'textarea[name="title"]': "A",
                    'textarea[name="description"]': "B",
                    '#listing-price-input': "10",
                    '#listing-quantity-input': "2",
                    "body": "A",
                },
                tags=["tag-a"],
                manager_shop_slugs=["templystudios"],
                editor_shop_slugs=["templystudios", "other-shop"],
            )
            launcher_mixed, _ = self._build_playwright(mixed_anchor_page)
            launch_factory.return_value = launcher_mixed

            with self.assertRaisesRegex(RuntimeError, "Phiên Etsy sai shop"):
                await dashboard_app.scrape_listing_details("123", shop_id="templystudios")

    async def test_access_gate_classifies_sign_in_and_blocked_text_and_not_script_hidden(self):
        with patch.object(dashboard_app, "_active_shop_id", "templystudios"), \
            patch.object(dashboard_app, "SHOPS", {
                "templystudios": {
                    "name": "Temply",
                    "etsy_link": "https://www.etsy.com/shop/templystudios",
                    "etsy_login_debug_port": 43123,
                }
            }), \
            patch.object(dashboard_app, "is_etsy_session_ready", return_value=True), \
            patch("playwright.async_api.async_playwright") as launch_factory:

            sign_in_page = _FakePage(
                url="https://www.etsy.com/signin",
                force_goto_url="https://www.etsy.com/signin",
                title="",
                description="",
                section_text="Please sign in to continue",
                manager_shop_slugs=[],
                editor_shop_slugs=["templystudios"],
            )
            launcher_signin, _ = self._build_playwright(sign_in_page)
            launch_factory.return_value = launcher_signin
            with self.assertRaisesRegex(RuntimeError, "Phiên Etsy chưa đăng nhập"):
                await dashboard_app.scrape_listing_details("123", shop_id="templystudios")

            blocked_page = _FakePage(
                url="https://www.etsy.com/your/shops/me/listing-editor/edit/123",
                locator_values={"body": "Our system detected unusual activity and access denied"},
                manager_shop_slugs=["templystudios"],
                editor_shop_slugs=[],
                section_text="Our system detected unusual activity and access denied",
            )
            launcher_blocked, _ = self._build_playwright(blocked_page)
            launch_factory.return_value = launcher_blocked
            with self.assertRaisesRegex(RuntimeError, "Etsy đang chặn/đòi xác minh"):
                await dashboard_app.scrape_listing_details("123", shop_id="templystudios")

            html_script_page = _FakePage(
                url="https://www.etsy.com/your/shops/me/listing-editor/edit/123",
                locator_values={
                    'textarea[name="title"]': "A",
                    'textarea[name="description"]': "B",
                    '#listing-price-input': "10",
                    '#listing-quantity-input': "2",
                    "body": "Listing editor is ready to edit",
                },
                tags=["tag-a"],
                section_text="<script>robot login challenge blocked</script>",
                manager_shop_slugs=["templystudios"],
                editor_shop_slugs=[],
            )
            launcher_script_only, _ = self._build_playwright(html_script_page)
            launch_factory.return_value = launcher_script_only
            details = await dashboard_app.scrape_listing_details("123", shop_id="templystudios")
            self.assertEqual("A", details["title"])

    def test_etsy_auth_required_url_checks_path_only_without_query_false_positives(self) -> None:
        self.assertFalse(
            dashboard_app._is_etsy_auth_required_url(
                "https://www.etsy.com/shop/templystudios?return_to=/signin"
            )
        )
        self.assertTrue(
            dashboard_app._is_etsy_auth_required_url(
                "https://www.etsy.com/signin?next=/your/shops/me/tools/listings"
            )
        )

    def test_etsy_access_blocked_url_checks_path_only_without_query_false_positives(self) -> None:
        self.assertFalse(
            dashboard_app._is_etsy_access_blocked_url(
                "https://www.etsy.com/your/shops/me/listing-editor/edit/123?next=/challenge"
            )
        )
        self.assertTrue(dashboard_app._is_etsy_access_blocked_url("https://www.etsy.com/challenge"))

    async def test_shop_manager_preflight_accepts_manager_route_with_query_hash(self):
        with patch.object(dashboard_app, "_active_shop_id", "templystudios"), \
            patch.object(dashboard_app, "SHOPS", {
                "templystudios": {
                    "name": "Temply",
                    "etsy_link": "https://www.etsy.com/shop/templystudios",
                    "etsy_login_debug_port": 43123,
                }
            }), \
            patch.object(dashboard_app, "is_etsy_session_ready", return_value=True), \
            patch("playwright.async_api.async_playwright") as launch_factory:

            page = _FakePage(
                url="https://www.etsy.com/your/shops/me/tools/listings?foo=1#hash",
                force_goto_urls=[
                    "https://www.etsy.com/your/shops/me/tools/listings?foo=1#hash",
                ],
                title="My Listing",
                description="Desc",
                locator_values={
                    'textarea[name="title"]': "My Listing",
                    'textarea[name="description"]': "Desc",
                    '#listing-price-input': "10",
                    '#listing-quantity-input': "2",
                    "body": "My Listing",
                },
                tags=["tag-a"],
                manager_shop_slugs=["templystudios"],
                editor_shop_slugs=[],
            )
            launcher, _ = self._build_playwright(page)
            launch_factory.return_value = launcher
            details = await dashboard_app.scrape_listing_details("123", shop_id="templystudios")
            self.assertEqual("My Listing", details["title"])

    async def test_shop_manager_preflight_rejects_public_shop_redirect(self):
        with patch.object(dashboard_app, "_active_shop_id", "templystudios"), \
            patch.object(dashboard_app, "SHOPS", {
                "templystudios": {
                    "name": "Temply",
                    "etsy_link": "https://www.etsy.com/shop/templystudios",
                    "etsy_login_debug_port": 43123,
                }
            }), \
            patch.object(dashboard_app, "is_etsy_session_ready", return_value=True), \
            patch("playwright.async_api.async_playwright") as launch_factory:

            page = _FakePage(
                url="https://www.etsy.com/shop/templystudios",
                force_goto_url="https://www.etsy.com/shop/templystudios",
                title="Shop",
                description="Shop page",
            )
            launcher, _ = self._build_playwright(page)
            launch_factory.return_value = launcher
            with self.assertRaisesRegex(RuntimeError, "chưa xác minh"):
                await dashboard_app.scrape_listing_details("123", shop_id="templystudios")

    async def test_shop_manager_preflight_waits_for_delayed_manager_anchors(self):
        with patch.object(dashboard_app, "_active_shop_id", "templystudios"), \
            patch.object(dashboard_app, "SHOPS", {
                "templystudios": {
                    "name": "Temply",
                    "etsy_link": "https://www.etsy.com/shop/templystudios",
                    "etsy_login_debug_port": 43123,
                }
            }), \
            patch.object(dashboard_app, "is_etsy_session_ready", return_value=True), \
            patch("playwright.async_api.async_playwright") as launch_factory:

            manager_delayed_page = _FakePage(
                url="https://www.etsy.com/your/shops/me/tools/listings",
                title="My Listing",
                description="Desc",
                locator_values={
                    'textarea[name="title"]': "My Listing",
                    'textarea[name="description"]': "Desc",
                    '#listing-price-input': "10",
                    '#listing-quantity-input': "2",
                    "body": "My Listing",
                },
                tags=["tag-a"],
                manager_shop_slugs_sequence=[[], ["templystudios"]],
                editor_shop_slugs=[],
            )
            launcher, _ = self._build_playwright(manager_delayed_page)
            launch_factory.return_value = launcher
            details = await dashboard_app.scrape_listing_details("123", shop_id="templystudios")
            self.assertEqual("My Listing", details["title"])
