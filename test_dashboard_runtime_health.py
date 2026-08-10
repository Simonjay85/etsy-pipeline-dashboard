#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import time
import unittest
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
            "vertex_app": True,
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
        self.assertNotIn("line", response.text)
        self.assertNotIn("backup-folder", response.text)
        self.assertIn("health_summary", payload)
        self.assertIn("canonical_root", payload)


class DashboardOptionalServiceProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_vertex_fallback_probes_run_concurrently(self):
        async def delayed_offline(*_args, **_kwargs):
            await asyncio.sleep(0.15)
            return False

        with patch.object(dashboard_app, "_check_http_json_identity", side_effect=delayed_offline):
            started = time.monotonic()
            self.assertFalse(await dashboard_app._check_vertex_service_async())
            elapsed = time.monotonic() - started

        # Two fallback endpoints used to run serially.  A single bounded probe
        # window keeps optional readiness from holding up the dashboard UI.
        self.assertLess(elapsed, 0.25)

    async def test_snapshot_returns_offline_when_optional_probe_wedges(self):
        async def wedged_probe():
            await asyncio.sleep(10)
            return True

        with patch.object(dashboard_app, "_check_watcher_async", side_effect=wedged_probe), patch.object(
            dashboard_app, "_check_vertex_service_async", side_effect=wedged_probe
        ), patch.object(dashboard_app, "_check_mlx_service_async", side_effect=wedged_probe):
            started = time.monotonic()
            snapshot = await dashboard_app._service_status_snapshot()
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.8)
        self.assertFalse(snapshot["watcher"])
        self.assertFalse(snapshot["vertex_app"])
        self.assertFalse(snapshot["mlx_ai"])


if __name__ == "__main__":
    unittest.main()
