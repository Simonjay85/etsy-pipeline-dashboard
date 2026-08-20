from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import etsy_push_update
from etsy_browser_session import EtsyBrowserSession, PROFILE_LOCK_NAMES


class EtsyPushUpdateSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.profile = self.root / "daisy-profile"
        self.profile.mkdir()
        self.session = EtsyBrowserSession(
            "daisyflowdigital",
            self.profile,
            43997,
        )
        self.page = SimpleNamespace(close=AsyncMock())
        self.context = SimpleNamespace(
            pages=[],
            new_page=AsyncMock(return_value=self.page),
            close=AsyncMock(),
        )
        self.browser = SimpleNamespace(contexts=[self.context], close=AsyncMock())
        self.chromium = SimpleNamespace(
            connect_over_cdp=AsyncMock(return_value=self.browser),
            launch_persistent_context=AsyncMock(return_value=self.context),
        )
        self.playwright = SimpleNamespace(chromium=self.chromium)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    async def test_ready_exact_cdp_is_reused_without_launch_or_lock_unlink(self) -> None:
        lock_contents = {}
        for lock_name in PROFILE_LOCK_NAMES:
            lock_path = self.profile / lock_name
            lock_path.write_text(f"live-{lock_name}", encoding="utf-8")
            lock_contents[lock_name] = lock_path.read_text(encoding="utf-8")

        with patch.object(
            etsy_push_update,
            "load_shops_config",
            return_value={"daisyflowdigital": {}},
        ), patch.object(
            etsy_push_update,
            "resolve_etsy_session",
            return_value=self.session,
        ) as resolve_session, patch.object(
            etsy_push_update,
            "is_etsy_session_ready",
            return_value=True,
        ) as session_ready:
            context, page, owns_context = await etsy_push_update._open_updater_context(
                self.playwright,
                "daisyflowdigital",
                self.profile,
            )

        resolve_session.assert_called_once_with(
            etsy_push_update.BASE_DIR,
            {"daisyflowdigital": {}},
            "daisyflowdigital",
        )
        session_ready.assert_called_once_with(self.session)
        self.chromium.connect_over_cdp.assert_awaited_once_with(
            "http://127.0.0.1:43997",
            timeout=5000,
        )
        self.chromium.launch_persistent_context.assert_not_awaited()
        self.assertIs(context, self.context)
        self.assertIs(page, self.page)
        self.assertFalse(owns_context)
        for lock_name, expected_content in lock_contents.items():
            lock_path = self.profile / lock_name
            self.assertTrue(lock_path.exists())
            self.assertEqual(expected_content, lock_path.read_text(encoding="utf-8"))

    async def test_profile_mismatch_fails_closed_before_cdp_or_launch(self) -> None:
        wrong_profile = self.root / "wrong-profile"
        wrong_profile.mkdir()
        with patch.object(
            etsy_push_update,
            "load_shops_config",
            return_value={"daisyflowdigital": {}},
        ), patch.object(
            etsy_push_update,
            "resolve_etsy_session",
            return_value=self.session,
        ), patch.object(
            etsy_push_update,
            "is_etsy_session_ready",
            Mock(return_value=True),
        ) as session_ready:
            with self.assertRaisesRegex(RuntimeError, "không khớp cấu hình updater"):
                await etsy_push_update._open_updater_context(
                    self.playwright,
                    "daisyflowdigital",
                    wrong_profile,
                )

        session_ready.assert_not_called()
        self.chromium.connect_over_cdp.assert_not_awaited()
        self.chromium.launch_persistent_context.assert_not_awaited()

    async def test_unverified_locked_profile_fails_without_unlink_or_launch(self) -> None:
        lock_path = self.profile / "SingletonLock"
        lock_path.write_text("live-owner", encoding="utf-8")
        with patch.object(
            etsy_push_update,
            "load_shops_config",
            return_value={"daisyflowdigital": {}},
        ), patch.object(
            etsy_push_update,
            "resolve_etsy_session",
            return_value=self.session,
        ), patch.object(
            etsy_push_update,
            "is_etsy_session_ready",
            return_value=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "SingletonLock"):
                await etsy_push_update._open_updater_context(
                    self.playwright,
                    "daisyflowdigital",
                    self.profile,
                )

        self.assertEqual("live-owner", lock_path.read_text(encoding="utf-8"))
        self.chromium.connect_over_cdp.assert_not_awaited()
        self.chromium.launch_persistent_context.assert_not_awaited()

    async def test_unlocked_unready_profile_launches_owned_context(self) -> None:
        with patch.object(
            etsy_push_update,
            "load_shops_config",
            return_value={"daisyflowdigital": {}},
        ), patch.object(
            etsy_push_update,
            "resolve_etsy_session",
            return_value=self.session,
        ), patch.object(
            etsy_push_update,
            "is_etsy_session_ready",
            return_value=False,
        ), patch.object(
            etsy_push_update,
            "CHROME_PATH",
            self.root / "missing-chrome",
        ):
            context, page, owns_context = await etsy_push_update._open_updater_context(
                self.playwright,
                "daisyflowdigital",
                self.profile,
            )

        self.chromium.connect_over_cdp.assert_not_awaited()
        self.chromium.launch_persistent_context.assert_awaited_once()
        launch_kwargs = self.chromium.launch_persistent_context.await_args.kwargs
        self.assertEqual(str(self.profile.resolve()), launch_kwargs["user_data_dir"])
        self.assertIs(context, self.context)
        self.assertIs(page, self.page)
        self.assertTrue(owns_context)

    async def test_cleanup_does_not_close_user_owned_cdp_context(self) -> None:
        await etsy_push_update._close_updater_context(
            self.context,
            self.page,
            owns_context=False,
        )

        self.page.close.assert_awaited_once()
        self.context.close.assert_not_awaited()
        self.browser.close.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
