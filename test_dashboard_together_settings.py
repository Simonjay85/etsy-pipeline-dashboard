#!/usr/bin/env python3
"""Focused, offline coverage for the dashboard's runtime Together settings."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import dashboard_app


_SAFE_RESPONSE_KEYS = {"ok", "configured", "key_hint", "base_url", "model", "persisted", "storage", "storage_error"}
_TOGETHER_SETTINGS = "/api/together/settings"
_MUTATION_HEADERS = {
    "X-Dashboard-Mutation-Token": dashboard_app._DASHBOARD_MUTATION_TOKEN,
}


class DashboardTogetherSettingsTests(unittest.TestCase):
    def setUp(self):
        # Unit tests must never touch the developer's real Keychain.
        for name in ("load_config", "save_config", "delete_config"):
            patcher = patch.object(dashboard_app.ai_credentials, name, return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("_together_persisted_identity", None), ("_together_storage_error", "")):
            patcher = patch.object(dashboard_app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(dashboard_app.app, base_url="http://127.0.0.1:8090")

    def test_get_returns_exact_safe_shape_and_last_four_only(self):
        secret = "together-super-secret-1234"
        with patch.dict(
            os.environ,
            {
                "TOGETHER_API_KEY": secret,
                "TOGETHER_API_BASE_URL": "https://api.example.test/",
                "TOGETHER_SEO_MODEL": "demo/model",
            },
            clear=False,
        ):
            response = self.client.get(_TOGETHER_SETTINGS)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), _SAFE_RESPONSE_KEYS)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["configured"])
        self.assertEqual(payload["key_hint"], "1234")
        self.assertEqual(payload["base_url"], "https://api.example.test/v1")
        self.assertEqual(payload["model"], "demo/model")
        self.assertNotIn(secret, response.text)
        self.assertNotIn("api_key", payload)

    def test_post_rejects_missing_and_invalid_mutation_tokens(self):
        secret = "existing-runtime-secret"
        body = {
            "api_key": "replacement-secret",
            "base_url": "https://api.example.test/v1",
            "model": "demo/model",
        }
        with patch.dict(os.environ, {"TOGETHER_API_KEY": secret}, clear=False):
            for headers in ({}, {"X-Dashboard-Mutation-Token": "wrong-token"}):
                response = self.client.post(_TOGETHER_SETTINGS, json=body, headers=headers)
                self.assertEqual(response.status_code, 403)
                self.assertNotIn("replacement-secret", response.text)
            self.assertEqual(os.environ.get("TOGETHER_API_KEY"), secret)

    def test_post_updates_runtime_only_and_invalidates_readiness_cache(self):
        secret = "new-runtime-secret-9876"
        with (
            patch.dict(
                os.environ,
                {
                    "TOGETHER_API_KEY": "old-runtime-secret",
                    "TOGETHER_API_BASE_URL": "https://api.together.xyz/v1",
                    "TOGETHER_SEO_MODEL": dashboard_app.DEFAULT_TOGETHER_SEO_MODEL,
                },
                clear=False,
            ),
            patch.object(dashboard_app, "_together_readiness_ok_until", 123.0),
            patch.object(dashboard_app, "save_shops") as save_shops,
        ):
            response = self.client.post(
                _TOGETHER_SETTINGS,
                headers=_MUTATION_HEADERS,
                json={
                    "api_key": secret,
                    "base_url": "https://proxy.example.test/v1/",
                    "model": "proxy/model",
                },
            )

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(set(payload), _SAFE_RESPONSE_KEYS)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["configured"])
            self.assertEqual(payload["key_hint"], "9876")
            self.assertEqual(payload["base_url"], "https://proxy.example.test/v1")
            self.assertEqual(payload["model"], "proxy/model")
            self.assertEqual(os.environ.get("TOGETHER_API_KEY"), secret)
            self.assertEqual(os.environ.get("TOGETHER_API_BASE_URL"), "https://proxy.example.test/v1")
            self.assertEqual(os.environ.get("TOGETHER_SEO_MODEL"), "proxy/model")
            self.assertEqual(dashboard_app._together_readiness_ok_until, 0.0)
            save_shops.assert_not_called()
            self.assertNotIn(secret, response.text)

    def test_blank_key_preserves_and_explicit_clear_removes_runtime_key(self):
        secret = "keep-until-cleared-4567"
        with patch.dict(
            os.environ,
            {
                "TOGETHER_API_KEY": secret,
                "TOGETHER_API_BASE_URL": "https://api.example.test/v1",
                "TOGETHER_SEO_MODEL": "demo/model",
            },
            clear=False,
        ):
            preserved = self.client.post(
                _TOGETHER_SETTINGS,
                headers=_MUTATION_HEADERS,
                json={"api_key": "", "base_url": "", "model": "demo/model"},
            )
            self.assertEqual(preserved.status_code, 200)
            self.assertEqual(os.environ.get("TOGETHER_API_KEY"), secret)
            self.assertTrue(preserved.json()["configured"])

            cleared = self.client.post(
                _TOGETHER_SETTINGS,
                headers=_MUTATION_HEADERS,
                json={
                    "api_key": "",
                    "clear_key": True,
                    "base_url": "https://api.example.test/v1",
                    "model": "demo/model",
                },
            )
            self.assertEqual(cleared.status_code, 200)
            self.assertNotIn("TOGETHER_API_KEY", os.environ)
            self.assertFalse(cleared.json()["configured"])
            self.assertEqual(cleared.json()["key_hint"], "")
            self.assertNotIn(secret, cleared.text)

    def test_base_url_and_model_validation_is_bounded_and_secret_safe(self):
        secret = "unchanged-runtime-secret"
        invalid_bases = (
            "ftp://api.example.test",
            "https://user:password@api.example.test/v1",
            "https://api.example.test/v1?token=secret",
            "https://api.example.test/v1#fragment",
            "https://",
        )
        with patch.dict(
            os.environ,
            {
                "TOGETHER_API_KEY": secret,
                "TOGETHER_API_BASE_URL": "https://api.example.test/v1",
                "TOGETHER_SEO_MODEL": "demo/model",
            },
            clear=False,
        ):
            for base_url in invalid_bases:
                response = self.client.post(
                    _TOGETHER_SETTINGS,
                    headers=_MUTATION_HEADERS,
                    json={"api_key": "attempted-secret", "base_url": base_url, "model": "demo/model"},
                )
                self.assertEqual(response.status_code, 400, base_url)
                self.assertNotIn("attempted-secret", response.text)
                self.assertEqual(os.environ.get("TOGETHER_API_KEY"), secret)

            for model in ("", "x" * (dashboard_app._TOGETHER_MODEL_MAX_LENGTH + 1), "demo\nmodel"):
                response = self.client.post(
                    _TOGETHER_SETTINGS,
                    headers=_MUTATION_HEADERS,
                    json={"api_key": "attempted-secret", "base_url": "https://api.example.test", "model": model},
                )
                self.assertEqual(response.status_code, 400, repr(model))
                self.assertNotIn("attempted-secret", response.text)
                self.assertEqual(os.environ.get("TOGETHER_API_KEY"), secret)

    def test_child_environment_and_shop_save_are_key_safe(self):
        secret = "child-inherited-secret"
        with patch.dict(os.environ, {"TOGETHER_API_KEY": secret}, clear=False):
            child_env = dashboard_app._together_child_env(include_together=True)
            safe_env = dashboard_app._together_child_env()
            self.assertNotIn("TOGETHER_API_KEY", safe_env)
        self.assertEqual(child_env["TOGETHER_API_KEY"], secret)

        root = Path(__file__).resolve().parent
        html = (root / "dashboard_static" / "index.html").read_text(encoding="utf-8")
        app_js = (root / "dashboard_static" / "app.js").read_text(encoding="utf-8")
        self.assertRegex(html, r'<input[^>]+type="password"[^>]+id="set-together-api-key"[^>]+autocomplete="off"')
        self.assertIn("/api/together/settings", app_js)
        self.assertIn("clear_key: true", app_js)
        save_start = app_js.index("async function saveShopSettings()")
        save_end = app_js.index("\n}\n", save_start) + 3
        shop_save_body = app_js[save_start:save_end]
        self.assertNotIn("api_key", shop_save_body)
        self.assertNotIn("set-together-api-key", shop_save_body)


class DashboardTogetherRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_readiness_uses_runtime_base_and_key_without_network(self):
        captured = {}

        class Response:
            status_code = 200

            def json(self):
                return {"object": "list", "data": [{"id": "demo/model", "object": "model"}]}

        class Client:
            def __init__(self, **kwargs):
                captured["client_kwargs"] = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get(self, url, **kwargs):
                captured["url"] = url
                captured["request_kwargs"] = kwargs
                return Response()

        fake_httpx = SimpleNamespace(Timeout=lambda **kwargs: kwargs, AsyncClient=Client)
        with (
            patch.object(dashboard_app, "_together_readiness_ok_until", 0.0),
            patch.dict(
                os.environ,
                {
                    "TOGETHER_API_KEY": "runtime-probe-secret",
                    "TOGETHER_API_BASE_URL": "https://api.example.test/",
                    "TOGETHER_SEO_MODEL": "demo/model",
                },
                clear=False,
            ),
            patch.object(dashboard_app, "_get_httpx", return_value=fake_httpx),
        ):
            self.assertFalse(await dashboard_app._check_together_service_async())
            probe_task = dashboard_app._together_readiness_probe_task
            self.assertIsNotNone(probe_task)
            await probe_task
            self.assertTrue(await dashboard_app._check_together_service_async())

        self.assertEqual(captured["url"], "https://api.example.test/v1/models")
        self.assertEqual(
            captured["request_kwargs"]["headers"],
            {"Authorization": "Bearer runtime-probe-secret"},
        )
        self.assertNotIn("runtime-probe-secret", str(dashboard_app._together_public_config()))


if __name__ == "__main__":
    unittest.main()
