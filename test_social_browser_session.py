import asyncio
from pathlib import Path
from unittest.mock import Mock

import dashboard_app
import social_browser_session


def test_social_session_defaults_are_unique_per_shop(tmp_path):
    shops = {
        "templystudios": {"id": "templystudios"},
        "daisyflowdigital": {"id": "daisyflowdigital"},
    }

    temply = social_browser_session.resolve_social_session(
        tmp_path, shops, "templystudios"
    )
    daisy = social_browser_session.resolve_social_session(
        tmp_path, shops, "daisyflowdigital"
    )

    assert temply.debug_port != daisy.debug_port
    assert temply.profile_dir != daisy.profile_dir
    assert temply.profile_dir == (
        Path.home() / ".etsy_social_browser_session_templystudios"
    ).resolve()


def test_unrelated_shop_does_not_change_existing_default_session(tmp_path):
    original_shops = {
        "templystudios": {"id": "templystudios"},
        "daisyflowdigital": {"id": "daisyflowdigital"},
    }
    expanded_shops = {
        "aaa-new-shop": {"id": "aaa-new-shop"},
        **original_shops,
    }

    before = social_browser_session.resolve_social_session(
        tmp_path, original_shops, "templystudios"
    )
    after = social_browser_session.resolve_social_session(
        tmp_path, expanded_shops, "templystudios"
    )

    assert after.debug_port == before.debug_port
    assert after.profile_dir == before.profile_dir


def test_duplicate_or_invalid_social_config_falls_back_per_shop(tmp_path):
    shops = {
        "a": {
            "social_browser": {
                "debug_port": 19400,
                "profile_dir": ".sessions/shared",
            }
        },
        "b": {
            "social_browser": {
                "debug_port": 19400,
                "profile_dir": ".sessions/shared",
            }
        },
    }

    first = social_browser_session.resolve_social_session(tmp_path, shops, "a")
    second = social_browser_session.resolve_social_session(tmp_path, shops, "b")

    assert first.debug_port == 19400
    assert second.debug_port != first.debug_port
    assert first.profile_dir != second.profile_dir
    assert first.profile_dir == (tmp_path / ".sessions/shared").resolve()


def test_open_social_browser_uses_exact_profile_and_debug_port(tmp_path, monkeypatch):
    chrome = tmp_path / "Google Chrome"
    chrome.write_text("", encoding="utf-8")
    session = social_browser_session.SocialBrowserSession(
        "shop-a", tmp_path / "profile-a", 19420
    )
    popen = Mock()
    monkeypatch.setattr(social_browser_session, "CHROME_PATH", chrome)
    monkeypatch.setattr(social_browser_session.subprocess, "Popen", popen)
    readiness = iter([False, True])
    monkeypatch.setattr(
        social_browser_session,
        "is_session_ready",
        lambda candidate: next(readiness),
    )

    assert social_browser_session.open_social_browser(
        session, "instagram", wait_seconds=0
    )
    command = popen.call_args.args[0]
    assert "--remote-debugging-port=19420" in command
    assert f"--user-data-dir={session.profile_dir}" in command
    assert social_browser_session.SOCIAL_URLS["instagram"] in command


def test_open_social_browser_reuses_ready_exact_session(tmp_path, monkeypatch):
    session = social_browser_session.SocialBrowserSession(
        "shop-a", tmp_path / "profile-a", 19420
    )
    popen = Mock()
    monkeypatch.setattr(
        social_browser_session, "is_session_ready", lambda candidate: True
    )
    open_tab = Mock(return_value=True)
    monkeypatch.setattr(social_browser_session, "open_cdp_tab", open_tab)
    monkeypatch.setattr(social_browser_session.subprocess, "Popen", popen)

    assert social_browser_session.open_social_browser(session, "instagram")
    open_tab.assert_called_once_with(
        session, social_browser_session.SOCIAL_URLS["instagram"]
    )
    popen.assert_not_called()


def test_single_social_request_keeps_shop_captured_before_await(monkeypatch):
    original_shop = dashboard_app._active_shop_id
    recorded = []

    class SwitchingRequest:
        async def json(self):
            dashboard_app._active_shop_id = "daisyflowdigital"
            return {"platform": "facebook"}

    async def fake_runner(*args):
        recorded.append(args)

    monkeypatch.setattr(dashboard_app, "_active_shop_id", "templystudios")
    monkeypatch.setattr(
        dashboard_app,
        "get_product_by_row",
        lambda row: {"row": row, "folder": "product-07"},
    )
    monkeypatch.setattr(dashboard_app, "_run_social_poster", fake_runner)
    dashboard_app._running_processes.clear()

    async def run():
        result = await dashboard_app.post_to_social(7, SwitchingRequest())
        await asyncio.sleep(0)
        return result

    try:
        result = asyncio.run(run())
        assert result["ok"] is True
        assert recorded == [("templystudios", 7, "product-07", "facebook")]
    finally:
        dashboard_app._active_shop_id = original_shop


def test_bulk_social_request_keeps_shop_captured_before_await(monkeypatch):
    original_shop = dashboard_app._active_shop_id
    recorded = []

    class SwitchingRequest:
        async def json(self):
            dashboard_app._active_shop_id = "daisyflowdigital"
            return {
                "platform": "instagram",
                "start": 4,
                "end": 5,
                "delay": 30,
            }

    async def fake_runner(*args):
        recorded.append(args)

    monkeypatch.setattr(dashboard_app, "_active_shop_id", "templystudios")
    monkeypatch.setattr(dashboard_app, "_run_social_bulk_poster", fake_runner)
    dashboard_app._running_processes.clear()

    async def run():
        result = await dashboard_app.bulk_post_social(SwitchingRequest())
        await asyncio.sleep(0)
        return result

    try:
        result = asyncio.run(run())
        assert result["ok"] is True
        assert recorded == [("templystudios", 4, 5, "instagram", 30)]
    finally:
        dashboard_app._active_shop_id = original_shop
