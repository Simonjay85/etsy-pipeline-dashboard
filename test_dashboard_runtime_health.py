#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import dashboard_app


def _runtime_identity_stub(**kwargs):
    return {
        "generated_at": "2026-08-08T00:00:00+00:00",
        "canonical_root": "/canonical/root",
        "current_root": str(kwargs.get("runtime_root")),
        "canonical_match": True,
        "source": {"commit": "abc123", "dirty": False, "file_hashes": {}},
        "frontend_assets": {
            "index_hash": "h-index",
            "style_hash": "h-style",
            "app_hash": "h-app",
            "identity_stale": False,
        },
        "process": {
            "pid": 777,
            "start_time_iso": "2026-08-08T00:00:00+00:00",
            "listen": {"host": kwargs.get("listen_host"), "port": kwargs.get("listen_port")},
        },
        "active_shop": {"id": kwargs.get("active_shop_id"), "name": kwargs.get("active_shop_name")},
        "backup_scheduler": {
            "loaded": {"daily": False, "weekly": False},
            "plists": {
                "daily": {"exists": False, "path": "/x/daily.plist"},
                "weekly": {"exists": False, "path": "/x/weekly.plist"},
            },
            "configured": True,
            "loaded_ok": False,
            "status_evidence": {
                "last_success": {"timestamp": "2026-08-08T00:00:01", "status": "success"},
                "last_failure": None,
            },
        },
        "python": {"python_version": "3.13"},
        "services": kwargs.get("service_readiness", {}),
        "service_readiness": {"ok": True, "checks": kwargs.get("service_readiness", {})},
        "health_summary": {"source_stale": False, "backup_scheduler_unloaded": True, "backup_last_failure": False},
    }


class DashboardRuntimeHealthTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(dashboard_app.app, base_url="http://127.0.0.1:8090")

    def test_runtime_health_endpoint_returns_expected_shape_and_safe_active_shop(self):
        service_checks = {
            "together_api": True,
            "image_studio": True,
            "mlx_ai": True,
            "watcher": False,
            "running": ["__ETSY_SYNC__"],
            "running_tasks": [],
        }

        with patch.object(
            dashboard_app, "_service_status_snapshot", new=AsyncMock(return_value=service_checks)
        ), patch.object(
            dashboard_app.runtime_identity,
            "runtime_health_payload",
            side_effect=_runtime_identity_stub,
        ), patch.object(
            dashboard_app,
            "SHOPS",
            {"demo": {"name": "Demo Shop", "token": "should-not-leak", "cookies": "secret-cookie"}},
        ), patch.object(dashboard_app, "_active_shop_id", "demo"):
            response = self.client.get("/api/runtime-health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertIn("generated_at", payload)
        self.assertEqual(payload["active_shop"], {"id": "demo", "name": "Demo Shop"})
        self.assertEqual(payload["canonical_match"], True)
        self.assertEqual(payload["service_readiness"]["checks"], service_checks)
        self.assertNotIn("token", str(payload["active_shop"]))
        self.assertNotIn("should-not-leak", response.text)
        self.assertNotIn("secret-cookie", response.text)
        status_evidence = payload["backup_scheduler"]["status_evidence"]
        self.assertNotIn("line", status_evidence.get("last_success") or {})
        self.assertNotIn("line", status_evidence.get("last_failure") or {})
        self.assertNotIn("backup-folder", response.text)
        self.assertIn("health_summary", payload)
        self.assertIn("canonical_root", payload)


class DashboardOptionalServiceProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_together_probe_requires_key_and_checks_models_without_exposing_key(self):
        captured = {}

        class Response:
            status_code = 200

            def json(self):
                return [
                    {"id": "other/model", "object": "model"},
                    {"id": "deepseek-ai/DeepSeek-V4.1-Flash", "object": "model"},
                ]

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
        with patch.dict("os.environ", {
                "TOGETHER_API_KEY": "test-secret",
                "TOGETHER_API_BASE_URL": "https://together.test/v1",
                "TOGETHER_SEO_MODEL": "deepseek-ai/DeepSeek-V4.1-Flash",
            }), \
                patch.object(dashboard_app, "_get_httpx", return_value=fake_httpx):
            self.assertFalse(await dashboard_app._check_together_service_async())
            probe_task = dashboard_app._together_readiness_probe_task
            self.assertIsNotNone(probe_task)
            self.assertFalse(await dashboard_app._check_together_service_async())
            await probe_task
            self.assertTrue(await dashboard_app._check_together_service_async())

        self.assertEqual(captured["url"], "https://together.test/v1/models")
        self.assertEqual(captured["client_kwargs"]["trust_env"], False)
        self.assertEqual(captured["client_kwargs"]["timeout"], {"timeout": 8.0})
        self.assertEqual(
            captured["request_kwargs"]["headers"],
            {"Authorization": "Bearer test-secret"},
        )
        self.assertNotIn("test-secret", str(dashboard_app._check_together_service_async))

        with patch.dict("os.environ", {"TOGETHER_API_KEY": ""}):
            self.assertFalse(await dashboard_app._check_together_service_async())

    async def test_together_validator_rejects_empty_or_malformed_model_shapes(self):
        invalid_payloads = (
            [],
            [{}],
            [{"id": "demo/model"}],
            {"object": "list", "data": []},
            {"object": "list", "data": [{"id": "", "object": "model"}]},
            {"object": "list", "data": [{"id": "demo/model", "object": "error"}]},
            {"error": "upstream failure"},
        )

        class Response:
            status_code = 200

            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        class Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get(self, _url, **_kwargs):
                return Response(self.payload)

        with patch.dict("os.environ", {
                "TOGETHER_API_KEY": "test-secret",
                "TOGETHER_API_BASE_URL": "https://together.test/v1",
                "TOGETHER_SEO_MODEL": "demo/model",
            }), patch.object(dashboard_app, "_get_httpx") as get_httpx:
            for payload in invalid_payloads:
                Client.payload = payload
                fake_httpx = SimpleNamespace(Timeout=lambda **kwargs: kwargs, AsyncClient=Client)
                get_httpx.return_value = fake_httpx
                dashboard_app._invalidate_together_readiness()
                self.assertFalse(await dashboard_app._check_together_service_async())
                probe_task = dashboard_app._together_readiness_probe_task
                self.assertIsNotNone(probe_task)
                await probe_task
                self.assertFalse(await dashboard_app._check_together_service_async())

    async def test_together_probe_cannot_warm_cache_after_runtime_config_changes(self):
        started = asyncio.Event()
        release = asyncio.Event()

        class Response:
            status_code = 200

            def json(self):
                return [{"id": "demo/model", "object": "model"}]

        class Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get(self, _url, **_kwargs):
                started.set()
                await release.wait()
                return Response()

        fake_httpx = SimpleNamespace(Timeout=lambda **kwargs: kwargs, AsyncClient=Client)
        dashboard_app._invalidate_together_readiness()
        with patch.dict("os.environ", {
                "TOGETHER_API_KEY": "old-secret",
                "TOGETHER_API_BASE_URL": "https://old.example.test/v1",
                "TOGETHER_SEO_MODEL": "demo/model",
            }), patch.object(dashboard_app, "_get_httpx", return_value=fake_httpx):
            api_key, base_url, model, identity = dashboard_app._together_runtime_config_snapshot()
            generation = dashboard_app._together_readiness_generation
            probe_task = asyncio.create_task(
                dashboard_app._run_together_readiness_probe(
                    api_key, base_url, model, identity, generation
                )
            )
            await started.wait()
            with patch.dict("os.environ", {
                    "TOGETHER_API_KEY": "new-secret",
                    "TOGETHER_API_BASE_URL": "https://new.example.test/v1",
                    "TOGETHER_SEO_MODEL": "demo/model",
                }):
                release.set()
                self.assertFalse(await probe_task)
                self.assertEqual(dashboard_app._together_readiness_ok_until, 0.0)
                self.assertIsNone(dashboard_app._together_readiness_cache_identity)

    async def test_image_studio_fallback_probes_run_concurrently(self):
        async def delayed_offline(*_args, **_kwargs):
            await asyncio.sleep(0.15)
            return False

        with patch.object(dashboard_app, "_check_http_json_identity", side_effect=delayed_offline):
            started = time.monotonic()
            self.assertFalse(await dashboard_app._check_image_studio_service_async())
            elapsed = time.monotonic() - started

        # Two fallback endpoints used to run serially.  A single bounded probe
        # window keeps optional readiness from holding up the dashboard UI.
        self.assertLess(elapsed, 0.25)

    async def test_snapshot_returns_offline_when_optional_probe_wedges(self):
        async def wedged_probe():
            await asyncio.sleep(10)
            return True

        with patch.object(dashboard_app, "_check_watcher_async", side_effect=wedged_probe), patch.object(
            dashboard_app, "_check_image_studio_service_async", side_effect=wedged_probe
        ), patch.object(dashboard_app, "_check_mlx_service_async", side_effect=wedged_probe):
            started = time.monotonic()
            snapshot = await dashboard_app._service_status_snapshot()
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.8)
        self.assertFalse(snapshot["watcher"])
        self.assertFalse(snapshot["together_api"])
        self.assertFalse(snapshot["image_studio"])
        self.assertFalse(snapshot["mlx_ai"])

    async def test_snapshot_does_not_wait_for_slow_together_probe(self):
        class SlowClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get(self, _url, **_kwargs):
                await asyncio.sleep(1.0)

        fake_httpx = SimpleNamespace(
            Timeout=lambda **kwargs: kwargs,
            AsyncClient=SlowClient,
        )
        with patch.dict("os.environ", {
                "TOGETHER_API_KEY": "slow-probe-secret",
                "TOGETHER_API_BASE_URL": "https://together.test/v1",
                "TOGETHER_SEO_MODEL": "demo/model",
            }), patch.object(dashboard_app, "_get_httpx", return_value=fake_httpx), patch.object(
                dashboard_app, "_check_watcher_async", new=AsyncMock(return_value=False)
            ), patch.object(
                dashboard_app, "_check_image_studio_service_async", new=AsyncMock(return_value=False)
            ), patch.object(
                dashboard_app, "_check_mlx_service_async", new=AsyncMock(return_value=False)
            ):
            dashboard_app._invalidate_together_readiness()
            started = time.monotonic()
            snapshot = await dashboard_app._service_status_snapshot()
            elapsed = time.monotonic() - started
            probe_task = dashboard_app._together_readiness_probe_task
            dashboard_app._invalidate_together_readiness()
            if probe_task is not None:
                await asyncio.gather(probe_task, return_exceptions=True)

        self.assertLess(elapsed, 0.5)
        self.assertFalse(snapshot["together_api"])


if __name__ == "__main__":
    unittest.main()
