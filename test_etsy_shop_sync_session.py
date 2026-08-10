"""Regression tests for Etsy shop-sync session reuse.

These tests deliberately stay below the browser/network boundary.  They prove
that ``crawl_etsy_shop`` attaches to the already-authenticated per-shop CDP
session and owns only the temporary page it creates for the crawl.
"""

from __future__ import annotations

import json
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, call, patch

import etsy_browser_session
import etsy_shop_sync


class _PlaywrightContextManager:
    """Async context-manager double returned by ``async_playwright()``."""

    def __init__(self, playwright):
        self.enter = AsyncMock(return_value=playwright)
        self.exit = AsyncMock(return_value=None)

    async def __aenter__(self):
        return await self.enter()

    async def __aexit__(self, exc_type, exc_value, traceback):
        return await self.exit(exc_type, exc_value, traceback)


class _SessionRuntime:
    """All mock browser state and temporary profile artifacts for one test."""

    def __init__(self, root: Path):
        self.profile_dir = root / "etsy-poster-profile"
        self.profile_dir.mkdir()
        self.lock_paths = [
            self.profile_dir / lock_name
            for lock_name in etsy_browser_session.PROFILE_LOCK_NAMES
        ]
        for lock_path in self.lock_paths:
            lock_path.write_text("sentinel", encoding="utf-8")

        self.config = {
            "templystudios": {
                "browser_session": str(self.profile_dir),
                "etsy_link": "https://www.etsy.com/shop/Templystudios",
                "etsy_login_debug_port": 41822,
            }
        }
        self.config_path = root / "shops_config.json"
        self.config_path.write_text(
            json.dumps(self.config),
            encoding="utf-8",
        )
        self.session = etsy_browser_session.EtsyBrowserSession(
            "templystudios", self.profile_dir, 41822
        )

        self.page = Mock()
        self.page.url = etsy_shop_sync.SHOP_MANAGER_URL
        self.page.set_default_timeout = Mock()
        self.page.goto = AsyncMock()
        self.page.wait_for_timeout = AsyncMock()
        self.page.close = AsyncMock()

        self.context = Mock()
        self.context.new_page = AsyncMock(return_value=self.page)
        self.context.close = AsyncMock()

        self.browser = Mock()
        self.browser.contexts = [self.context]
        self.browser.close = AsyncMock()

        self.chromium = Mock()
        self.chromium.connect_over_cdp = AsyncMock(return_value=self.browser)
        self.chromium.launch_persistent_context = AsyncMock()
        self.chromium.launch = AsyncMock()

        self.playwright = Mock()
        self.playwright.chromium = self.chromium
        self.playwright_context_manager = _PlaywrightContextManager(self.playwright)
        self.async_playwright_factory = Mock(
            return_value=self.playwright_context_manager
        )

        # ``etsy_shop_sync`` should no longer import or use subprocess.  A
        # create=True patch also catches a regression that reintroduces the
        # attribute and calls pkill without requiring production changes here.
        self.subprocess_guard = Mock()
        self.resolve_session = Mock(return_value=self.session)


@contextmanager
def _patched_runtime(runtime: _SessionRuntime, *, ready: bool):
    """Patch only runtime seams; no real Etsy or Chrome operation is possible."""

    ready_mock = Mock(return_value=ready)
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(etsy_shop_sync, "SHOPS_CONFIG_FILE", runtime.config_path)
        )
        stack.enter_context(
            patch.object(
                etsy_shop_sync,
                "resolve_etsy_session",
                runtime.resolve_session,
            )
        )
        stack.enter_context(
            patch.object(etsy_shop_sync, "is_session_ready", ready_mock)
        )
        stack.enter_context(
            patch.object(
                etsy_shop_sync,
                "async_playwright",
                runtime.async_playwright_factory,
            )
        )
        stack.enter_context(
            patch.object(
                etsy_shop_sync,
                "subprocess",
                runtime.subprocess_guard,
                create=True,
            )
        )
        yield ready_mock


class TestEtsyShopSyncSession(IsolatedAsyncioTestCase):
    def _runtime(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return _SessionRuntime(Path(temp_dir.name))

    def _assert_resolved_exact_temply_session(self, runtime: _SessionRuntime):
        runtime.resolve_session.assert_called_once_with(
            etsy_shop_sync.BASE_DIR,
            runtime.config,
            "templystudios",
        )

    def _assert_no_relaunch_or_profile_mutation(self, runtime: _SessionRuntime):
        self.assertEqual(
            [],
            runtime.subprocess_guard.mock_calls,
            "sync must not pkill Chrome or invoke another subprocess",
        )
        runtime.chromium.launch_persistent_context.assert_not_called()
        runtime.chromium.launch.assert_not_called()
        for lock_path in runtime.lock_paths:
            self.assertTrue(
                lock_path.exists(),
                f"sync must not delete the live profile lock {lock_path.name}",
            )

    def _assert_browser_context_stays_owned_by_user(
        self, runtime: _SessionRuntime
    ):
        runtime.context.close.assert_not_called()
        runtime.browser.close.assert_not_called()

    async def test_happy_path_reuses_exact_ready_cdp_and_closes_only_new_page(self):
        runtime = self._runtime()
        crawl_status = AsyncMock(side_effect=[[], [], [], []])
        verify_shop = AsyncMock(return_value="Templystudios")

        with _patched_runtime(runtime, ready=True) as ready_mock, patch.object(
            etsy_shop_sync, "crawl_status", crawl_status
        ), patch.object(
            etsy_shop_sync, "verify_active_etsy_shop", verify_shop
        ):
            result = await etsy_shop_sync.crawl_etsy_shop("templystudios")

        self._assert_resolved_exact_temply_session(runtime)
        ready_mock.assert_called_once_with(runtime.session)
        runtime.async_playwright_factory.assert_called_once_with()
        runtime.chromium.connect_over_cdp.assert_awaited_once_with(
            "http://127.0.0.1:41822",
            timeout=5000,
        )
        runtime.context.new_page.assert_awaited_once_with()
        runtime.page.goto.assert_awaited_once_with(
            etsy_shop_sync.SHOP_MANAGER_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )
        verify_shop.assert_awaited_once_with(runtime.page, "templystudios")
        self.assertEqual(
            [
                call(runtime.page, "active"),
                call(runtime.page, "draft"),
                call(runtime.page, "inactive"),
                call(runtime.page, "expired"),
            ],
            crawl_status.await_args_list,
        )
        self.assertEqual(
            {
                "shopId": "templystudios",
                "shopSlug": "Templystudios",
                "active": [],
                "draft": [],
                "inactive": [],
                "expired": [],
            },
            {
                key: result[key]
                for key in ("shopId", "shopSlug", "active", "draft", "inactive", "expired")
            },
        )

        # The sync owns only this dedicated tab.  The authenticated context and
        # browser are attached to the user's Chrome and must remain open.
        runtime.page.close.assert_awaited_once_with()
        self._assert_browser_context_stays_owned_by_user(runtime)
        self._assert_no_relaunch_or_profile_mutation(runtime)

    async def test_unavailable_session_fails_closed_before_playwright(self):
        runtime = self._runtime()

        with _patched_runtime(runtime, ready=False) as ready_mock:
            with self.assertRaisesRegex(RuntimeError, "Phiên Etsy chưa sẵn sàng"):
                await etsy_shop_sync.crawl_etsy_shop("templystudios")

        self._assert_resolved_exact_temply_session(runtime)
        ready_mock.assert_called_once_with(runtime.session)
        runtime.async_playwright_factory.assert_not_called()
        runtime.chromium.connect_over_cdp.assert_not_called()
        runtime.context.new_page.assert_not_called()
        runtime.page.close.assert_not_called()
        self._assert_browser_context_stays_owned_by_user(runtime)
        self._assert_no_relaunch_or_profile_mutation(runtime)

    async def test_cdp_connect_failure_fails_closed_without_launching_profile(self):
        runtime = self._runtime()
        runtime.chromium.connect_over_cdp.side_effect = RuntimeError(
            "CDP connection refused"
        )

        with _patched_runtime(runtime, ready=True) as ready_mock:
            with self.assertRaisesRegex(RuntimeError, "CDP connection refused"):
                await etsy_shop_sync.crawl_etsy_shop("templystudios")

        self._assert_resolved_exact_temply_session(runtime)
        ready_mock.assert_called_once_with(runtime.session)
        runtime.chromium.connect_over_cdp.assert_awaited_once_with(
            "http://127.0.0.1:41822",
            timeout=5000,
        )
        runtime.context.new_page.assert_not_called()
        runtime.page.close.assert_not_called()
        self._assert_browser_context_stays_owned_by_user(runtime)
        self._assert_no_relaunch_or_profile_mutation(runtime)

    async def test_missing_browser_context_fails_closed_without_closing_browser(self):
        runtime = self._runtime()
        runtime.browser.contexts = []

        with _patched_runtime(runtime, ready=True) as ready_mock:
            with self.assertRaisesRegex(RuntimeError, "Không thấy browser context"):
                await etsy_shop_sync.crawl_etsy_shop("templystudios")

        self._assert_resolved_exact_temply_session(runtime)
        ready_mock.assert_called_once_with(runtime.session)
        runtime.chromium.connect_over_cdp.assert_awaited_once_with(
            "http://127.0.0.1:41822",
            timeout=5000,
        )
        runtime.context.new_page.assert_not_called()
        runtime.page.close.assert_not_called()
        self._assert_browser_context_stays_owned_by_user(runtime)
        self._assert_no_relaunch_or_profile_mutation(runtime)

    async def test_signin_redirect_fails_closed_and_closes_only_dedicated_page(self):
        runtime = self._runtime()
        runtime.page.url = "https://www.etsy.com/signin?return_to=%2Fyour%2Fshops"
        verify_shop = AsyncMock()
        crawl_status = AsyncMock()

        with _patched_runtime(runtime, ready=True) as ready_mock, patch.object(
            etsy_shop_sync, "verify_active_etsy_shop", verify_shop
        ), patch.object(
            etsy_shop_sync, "crawl_status", crawl_status
        ):
            with self.assertRaisesRegex(RuntimeError, "chưa đăng nhập Etsy"):
                await etsy_shop_sync.crawl_etsy_shop("templystudios")

        self._assert_resolved_exact_temply_session(runtime)
        ready_mock.assert_called_once_with(runtime.session)
        runtime.chromium.connect_over_cdp.assert_awaited_once_with(
            "http://127.0.0.1:41822",
            timeout=5000,
        )
        runtime.context.new_page.assert_awaited_once_with()
        verify_shop.assert_not_awaited()
        crawl_status.assert_not_awaited()
        runtime.page.close.assert_awaited_once_with()
        self._assert_browser_context_stays_owned_by_user(runtime)
        self._assert_no_relaunch_or_profile_mutation(runtime)

    async def test_wrong_shop_fails_closed_and_closes_only_dedicated_page(self):
        runtime = self._runtime()
        verify_shop = AsyncMock(
            side_effect=RuntimeError("Chrome Etsy đang ở shop WrongShop")
        )
        crawl_status = AsyncMock()

        with _patched_runtime(runtime, ready=True) as ready_mock, patch.object(
            etsy_shop_sync, "verify_active_etsy_shop", verify_shop
        ), patch.object(
            etsy_shop_sync, "crawl_status", crawl_status
        ):
            with self.assertRaisesRegex(RuntimeError, "WrongShop"):
                await etsy_shop_sync.crawl_etsy_shop("templystudios")

        self._assert_resolved_exact_temply_session(runtime)
        ready_mock.assert_called_once_with(runtime.session)
        runtime.chromium.connect_over_cdp.assert_awaited_once_with(
            "http://127.0.0.1:41822",
            timeout=5000,
        )
        verify_shop.assert_awaited_once_with(runtime.page, "templystudios")
        crawl_status.assert_not_awaited()
        runtime.page.close.assert_awaited_once_with()
        self._assert_browser_context_stays_owned_by_user(runtime)
        self._assert_no_relaunch_or_profile_mutation(runtime)


if __name__ == "__main__":
    import unittest

    unittest.main()
