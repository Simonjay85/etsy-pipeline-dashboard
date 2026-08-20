import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call
import pytest

import dashboard_app
import etsy_auto_post
import etsy_browser_session
import social_browser_session


def test_etsy_session_uses_configured_poster_profile_not_social_profile(tmp_path):
    poster_profile = tmp_path / "etsy-poster"
    shops = {
        "daisyflowdigital": {
            "browser_session": str(poster_profile),
        }
    }

    etsy = etsy_browser_session.resolve_etsy_session(
        tmp_path, shops, "daisyflowdigital"
    )
    social = social_browser_session.resolve_social_session(
        tmp_path, shops, "daisyflowdigital"
    )

    assert etsy.profile_dir == poster_profile.resolve()
    assert etsy.profile_dir != social.profile_dir
    assert etsy.debug_port != social.debug_port


def test_open_login_fails_closed_when_profile_is_locked(tmp_path, monkeypatch):
    profile = tmp_path / "etsy-profile"
    profile.mkdir()
    (profile / "SingletonLock").touch()
    session = etsy_browser_session.EtsyBrowserSession(
        "daisyflowdigital", profile, 42123
    )
    monkeypatch.setattr(
        etsy_browser_session, "is_session_ready", lambda candidate: False
    )

    try:
        etsy_browser_session.open_etsy_login_browser(session, wait_seconds=0)
    except etsy_browser_session.EtsyProfileLockedError as exc:
        assert "SingletonLock" in str(exc)
    else:
        raise AssertionError("Locked profile must be rejected")


def test_open_login_reuses_only_verified_exact_session(tmp_path, monkeypatch):
    session = etsy_browser_session.EtsyBrowserSession(
        "daisyflowdigital", tmp_path / "etsy-profile", 42123
    )
    open_tab = Mock(return_value=True)
    popen = Mock()
    monkeypatch.setattr(
        etsy_browser_session, "is_session_ready", lambda candidate: True
    )
    monkeypatch.setattr(etsy_browser_session, "open_cdp_tab", open_tab)
    monkeypatch.setattr(etsy_browser_session.subprocess, "Popen", popen)

    assert etsy_browser_session.open_etsy_login_browser(session)
    open_tab.assert_called_once_with(
        session, etsy_browser_session.SHOP_MANAGER_URL
    )
    popen.assert_not_called()


def test_open_endpoint_rejects_shop_mismatch_before_open(monkeypatch):
    opened = Mock()

    class Request:
        async def json(self):
            return {"shop_id": "templystudios"}

    monkeypatch.setattr(dashboard_app, "_active_shop_id", "daisyflowdigital")
    monkeypatch.setattr(dashboard_app, "open_etsy_login_browser", opened)

    response = asyncio.run(dashboard_app.open_etsy_session(Request()))

    assert response.status_code == 409
    opened.assert_not_called()


def test_open_endpoint_fails_if_active_shop_changes_during_request(monkeypatch):
    opened = Mock()

    class SwitchingRequest:
        async def json(self):
            dashboard_app._active_shop_id = "templystudios"
            return {"shop_id": "daisyflowdigital"}

    monkeypatch.setattr(dashboard_app, "_active_shop_id", "daisyflowdigital")
    monkeypatch.setattr(dashboard_app, "open_etsy_login_browser", opened)

    response = asyncio.run(dashboard_app.open_etsy_session(SwitchingRequest()))

    assert response.status_code == 409
    opened.assert_not_called()


def test_poster_reuses_verified_login_context_without_closing_it(
    tmp_path, monkeypatch
):
    profile = tmp_path / "etsy-profile"
    shops = {
        "daisyflowdigital": {
            "browser_session": str(profile),
            "etsy_login_debug_port": 43997,
        }
    }
    page = Mock()
    context = Mock()
    context.new_page = AsyncMock(return_value=page)
    browser = Mock(contexts=[context])
    chromium = Mock()
    chromium.connect_over_cdp = AsyncMock(return_value=browser)
    chromium.launch_persistent_context = AsyncMock()
    pw = Mock(chromium=chromium)

    monkeypatch.setattr(etsy_auto_post, "SHOPS", shops)
    monkeypatch.setattr(
        etsy_auto_post, "is_etsy_session_ready", lambda session: True
    )

    result_context, result_page, owns_context = asyncio.run(
        etsy_auto_post._open_poster_context(
            pw, "daisyflowdigital", profile
        )
    )

    assert result_context is context
    assert result_page is page
    assert owns_context is False
    chromium.connect_over_cdp.assert_awaited_once_with(
        "http://127.0.0.1:43997", timeout=10000
    )
    chromium.launch_persistent_context.assert_not_awaited()


def test_poster_reconnect_retry_transient_timeout_then_success_same_session(
    tmp_path, monkeypatch
):
    profile = tmp_path / "etsy-profile"
    shops = {
        "daisyflowdigital": {
            "browser_session": str(profile),
            "etsy_login_debug_port": 43997,
        }
    }
    page = Mock()
    context = Mock()
    context.new_page = AsyncMock(return_value=page)
    browser = Mock(contexts=[context])
    sleep_calls = []

    async def _record_sleep(seconds):
        sleep_calls.append(seconds)

    connect = AsyncMock(
        side_effect=[etsy_auto_post.PlaywrightTimeoutError("timed out"), browser]
    )
    launch = AsyncMock()
    chromium = Mock(
        connect_over_cdp=connect,
        launch_persistent_context=launch,
    )
    pw = Mock(chromium=chromium)

    monkeypatch.setattr(etsy_auto_post, "SHOPS", shops)
    readiness_calls = []
    def _session_ready(session):
        readiness_calls.append(session)
        return True

    monkeypatch.setattr(etsy_auto_post, "is_etsy_session_ready", _session_ready)
    monkeypatch.setattr(etsy_auto_post.asyncio, "sleep", _record_sleep)

    result_context, result_page, owns_context = asyncio.run(
        etsy_auto_post._open_poster_context(
            pw, "daisyflowdigital", profile
        )
    )

    assert result_context is context
    assert result_page is page
    assert owns_context is False
    connect.assert_has_awaits(
        [
            call("http://127.0.0.1:43997", timeout=10000),
            call("http://127.0.0.1:43997", timeout=10000),
        ],
        any_order=False,
    )
    assert connect.await_count == 2
    assert len(readiness_calls) == 2
    assert sleep_calls == [0.25]
    launch.assert_not_awaited()


def test_poster_reconnect_retry_exhausted_raises_shop_and_port_error(
    tmp_path, monkeypatch
):
    profile = tmp_path / "etsy-profile"
    shops = {
        "daisyflowdigital": {
            "browser_session": str(profile),
            "etsy_login_debug_port": 43997,
        }
    }
    context = Mock()
    timeout_error = etsy_auto_post.PlaywrightTimeoutError("timed out")
    connect = AsyncMock(
        side_effect=[
            timeout_error,
            timeout_error,
            timeout_error,
        ]
    )
    sleep_calls = []
    launch = AsyncMock()
    chromium = Mock(
        connect_over_cdp=connect,
        launch_persistent_context=launch,
    )
    pw = Mock(chromium=chromium)

    monkeypatch.setattr(etsy_auto_post, "SHOPS", shops)
    readiness_calls = []

    def _ready(session):
        readiness_calls.append(session)
        return True

    monkeypatch.setattr(etsy_auto_post, "is_etsy_session_ready", _ready)

    async def _record_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(etsy_auto_post.asyncio, "sleep", _record_sleep)

    # Reuse the same timeout object per mock side-effect so we can assert cause chaining.
    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(etsy_auto_post._open_poster_context(pw, "daisyflowdigital", profile))

    message = str(exc_info.value)

    assert "CDP đang bận hoặc không phản hồi" in message
    assert "daisyflowdigital" in message
    assert str(profile) in message
    assert "43997" in message
    assert "mở lại phiên đăng nhập đúng profile" in message
    connect.assert_has_awaits(
        [
            call("http://127.0.0.1:43997", timeout=10000),
            call("http://127.0.0.1:43997", timeout=10000),
            call("http://127.0.0.1:43997", timeout=10000),
        ],
        any_order=False,
    )
    assert connect.await_count == 3
    assert exc_info.value.__cause__ is timeout_error
    assert sleep_calls == [0.25, 0.50]
    assert len(readiness_calls) == 3
    launch.assert_not_awaited()


def test_poster_reconnect_stops_if_session_no_longer_ready_after_timeout(
    tmp_path, monkeypatch
):
    profile = tmp_path / "etsy-profile"
    shops = {
        "daisyflowdigital": {
            "browser_session": str(profile),
            "etsy_login_debug_port": 43997,
        }
    }
    page = Mock()
    context = Mock()
    context.new_page = AsyncMock(return_value=page)
    browser = Mock(contexts=[context])
    connect = AsyncMock(side_effect=[etsy_auto_post.PlaywrightTimeoutError("timed out")])
    launch = AsyncMock()
    chromium = Mock(
        connect_over_cdp=connect,
        launch_persistent_context=launch,
    )
    pw = Mock(chromium=chromium)

    readiness = iter([True, False])
    def _ready(session):
        return next(readiness)

    monkeypatch.setattr(etsy_auto_post, "SHOPS", shops)
    monkeypatch.setattr(etsy_auto_post, "is_etsy_session_ready", _ready)
    sleep_calls = []

    async def _record_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(etsy_auto_post.asyncio, "sleep", _record_sleep)

    try:
        asyncio.run(etsy_auto_post._open_poster_context(pw, "daisyflowdigital", profile))
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected readiness disappearance to raise RuntimeError")

    assert "CDP đang bận hoặc không phản hồi" in message
    assert "43997" in message
    assert "profile" in message
    assert connect.await_count == 1
    assert sleep_calls == []
    launch.assert_not_awaited()


def test_poster_connect_non_timeout_error_no_retry_or_sleep(
    tmp_path, monkeypatch
):
    profile = tmp_path / "etsy-profile"
    shops = {
        "daisyflowdigital": {
            "browser_session": str(profile),
            "etsy_login_debug_port": 43997,
        }
    }
    connect = AsyncMock(side_effect=RuntimeError("non-timeout boom"))
    launch = AsyncMock()
    chromium = Mock(
        connect_over_cdp=connect,
        launch_persistent_context=launch,
    )
    pw = Mock(chromium=chromium)

    monkeypatch.setattr(etsy_auto_post, "SHOPS", shops)
    monkeypatch.setattr(
        etsy_auto_post, "is_etsy_session_ready",
        lambda session: True
    )
    sleep_calls = []

    async def _record_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(etsy_auto_post.asyncio, "sleep", _record_sleep)

    with pytest.raises(RuntimeError, match="non-timeout boom"):
        asyncio.run(etsy_auto_post._open_poster_context(pw, "daisyflowdigital", profile))
    assert connect.await_count == 1
    assert sleep_calls == []
    launch.assert_not_awaited()


def test_poster_reopen_guard_blocks_mismatched_profile_dir(
    tmp_path, monkeypatch
):
    profile = tmp_path / "etsy-profile"
    shops = {
        "daisyflowdigital": {
            "browser_session": str(profile),
            "etsy_login_debug_port": 43997,
        }
    }
    other_profile = tmp_path / "etsy-other-profile"
    launch = AsyncMock()
    chromium = Mock(launch_persistent_context=launch)
    pw = Mock(chromium=chromium)

    monkeypatch.setattr(etsy_auto_post, "SHOPS", shops)
    monkeypatch.setattr(
        etsy_auto_post, "is_etsy_session_ready",
        lambda session: True,
    )

    with pytest.raises(RuntimeError, match="không khớp cấu hình poster"):
        asyncio.run(etsy_auto_post._open_poster_context(
            pw, "daisyflowdigital", other_profile
        ))
