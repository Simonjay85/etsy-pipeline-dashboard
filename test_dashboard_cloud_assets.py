"""Focused Phase 2B dashboard cloud-asset API tests.

These tests exercise only the dashboard boundary.  The store is mocked so no
Drive/rclone call, Etsy call, production asset mutation, or dashboard restart
is involved.
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import dashboard_app


class _JsonRequest:
    def __init__(self, payload: object):
        self.payload = payload

    async def json(self) -> object:
        return self.payload


class _FakeCloudAssetStore:
    def __init__(self) -> None:
        self.status_targets: list[Path] = []
        self.calls: list[tuple[str, Path]] = []
        self.call_threads: list[str] = []
        self.maintain_calls: list[dict] = []
        self.call_lock = threading.Lock()
        self.active_calls = 0
        self.max_active_calls = 0

        self.on_upload_start = None
        self.on_upload_end = None

    def _on_upload_complete(self) -> None:
        with self.call_lock:
            self.active_calls = max(0, self.active_calls - 1)

    def status(self, target: Path) -> dict:
        self.call_threads.append(threading.current_thread().name)
        self.status_targets.append(target)
        return {
            "ok": True,
            "product": "shops/templystudios/product-01",
            "state": "OFFLOAD_SCHEDULED",
            "current_revision": "rev-001",
            "current_manifest_sha256": "a" * 64,
            "revision": "rev-001",
            "hash": "a" * 64,
            "local_available": True,
            "local_matches": True,
            "counts": {"images": 2, "files": 1, "total_bytes": 1234},
            "bytes": 1234,
            "eligible_after": "2026-08-12T00:00:00Z",
            "cache": {"available": False, "status": "miss"},
            "last_error": None,
        }

    def upload(self, target: Path, revision: str | None = None) -> dict:
        with self.call_lock:
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)
        self.call_threads.append(threading.current_thread().name)
        self.calls.append(("upload", target))
        on_upload_start = self.on_upload_start
        if on_upload_start is not None:
            on_upload_start()
        try:
            return {"ok": True, "product": "shops/templystudios/product-01", "state": "CLOUD_VERIFIED", "revision": revision or "rev-upload"}
        finally:
            on_upload_end = self.on_upload_end
            if on_upload_end is not None:
                on_upload_end()
            self._on_upload_complete()

    def verify(self, target: Path) -> dict:
        self.call_threads.append(threading.current_thread().name)
        self.calls.append(("verify", target))
        with self.call_lock:
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)
        return {"ok": True, "product": "shops/templystudios/product-01", "state": "OFFLOAD_SCHEDULED", "revision": "rev-001"}

    def restore(self, target: Path, force: bool = False) -> dict:
        self.call_threads.append(threading.current_thread().name)
        self.calls.append(("restore", target))
        return {"ok": True, "product": "shops/templystudios/product-01", "state": "READY_LOCAL", "force": force}

    def cancel_offload(self, target: Path) -> dict:
        self.call_threads.append(threading.current_thread().name)
        self.calls.append(("cancel-offload", target))
        return {"ok": True, "product": "shops/templystudios/product-01", "state": "CLOUD_VERIFIED", "cancelled": True}

    def maintain(self, product_roots, *, apply, offload_enabled, allowlist, older_than_days):
        self.call_threads.append(threading.current_thread().name)
        self.maintain_calls.append(
            {
                "product_roots": product_roots,
                "apply": apply,
                "offload_enabled": offload_enabled,
                "allowlist": allowlist,
                "older_than_days": older_than_days,
            }
        )
        return [{"ok": True, "product": "shops/templystudios/product-01", "state": "OFFLOAD_SCHEDULED", "would_offload": False, "applied": False}]


class DashboardCloudAssetApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.shop_product = self.root / "shops" / "templystudios" / "product-01"
        (self.shop_product / "images").mkdir(parents=True)
        (self.shop_product / "files").mkdir()
        (self.shop_product / "images" / "hero.png").write_bytes(b"image")
        (self.shop_product / "files" / "source.zip").write_bytes(b"source")
        self.other_shop_product = self.root / "shops" / "other-shop" / "product-02"
        (self.other_shop_product / "images").mkdir(parents=True)
        (self.other_shop_product / "files").mkdir()
        self.master_product = self.root / "master_products" / "product-03"
        (self.master_product / "images").mkdir(parents=True)
        (self.master_product / "files").mkdir()
        self.store = _FakeCloudAssetStore()
        self.base_patch = patch.object(dashboard_app, "BASE_DIR", self.root)
        self.shop_patch = patch.object(dashboard_app, "_active_shop_id", "templystudios")
        self.shops_patch = patch.object(
            dashboard_app,
            "SHOPS",
            {
                "templystudios": {"id": "templystudios"},
                "other-shop": {"id": "other-shop"},
            },
        )
        self.store_patch = patch.object(dashboard_app, "CLOUD_ASSET_STORE", self.store)
        self.base_patch.start()
        self.shop_patch.start()
        self.shops_patch.start()
        self.store_patch.start()
        dashboard_app._CLOUD_ASSET_UPLOAD_SCHEDULES.clear()
        dashboard_app._CLOUD_ASSET_UPLOAD_QUEUE_BY_SHOP.clear()
        dashboard_app._CLOUD_ASSET_UPLOAD_WORKERS.clear()

    async def asyncTearDown(self) -> None:
        tasks = list(dashboard_app._CLOUD_ASSET_UPLOAD_WORKERS.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        dashboard_app._CLOUD_ASSET_UPLOAD_SCHEDULES.clear()
        dashboard_app._CLOUD_ASSET_UPLOAD_QUEUE_BY_SHOP.clear()
        dashboard_app._CLOUD_ASSET_UPLOAD_WORKERS.clear()

    def tearDown(self) -> None:
        self.store_patch.stop()
        self.shops_patch.stop()
        self.shop_patch.stop()
        self.base_patch.stop()
        self.temporary.cleanup()

    @staticmethod
    def _payload(**overrides: object) -> dict:
        payload = {
            "shop_id": "templystudios",
            "scope": "shop",
            "folder": "product-01",
        }
        payload.update(overrides)
        return payload

    async def test_factory_is_lazy_and_uses_secret_free_config(self) -> None:
        config = SimpleNamespace(
            repo_root=self.root,
            remote="gdrive_dest",
            parent_id="parent-id",
            rclone_bin="/opt/homebrew/bin/rclone",
            cache_root=self.root / "output" / "cloud-cache",
            lock_timeout_seconds=30.0,
            success_ttl_seconds=60,
            failure_ttl_seconds=120,
            offload_age_days=7,
        )
        with patch.object(dashboard_app, "CLOUD_ASSET_STORE", None), patch.object(
            dashboard_app, "load_cloud_asset_config", return_value=config
        ), patch.object(dashboard_app, "CloudAssetStore", return_value=Mock()) as store_class:
            store = dashboard_app.get_cloud_asset_store()

        store_class.assert_called_once_with(
            repo_root=self.root,
            remote="gdrive_dest",
            parent_id="parent-id",
            rclone_bin="/opt/homebrew/bin/rclone",
            cache_root=self.root / "output" / "cloud-cache",
            lock_timeout_seconds=30.0,
            success_ttl_seconds=60,
            failure_ttl_seconds=120,
            offload_age_days=7,
        )
        self.assertIsNotNone(store)

    async def test_status_defaults_to_active_shop_and_returns_local_cache_shape(self) -> None:
        result = await dashboard_app.cloud_assets_status()

        self.assertTrue(result["ok"])
        self.assertEqual(result["shop_id"], "templystudios")
        self.assertEqual(result["scope"], "shop")
        self.assertFalse(result["cloud_network_contacted"])
        self.assertEqual([path for path in self.store.status_targets], [self.shop_product])
        self.assertNotIn(self.other_shop_product, self.store.status_targets)
        self.assertNotIn(self.master_product, self.store.status_targets)

        item = result["items"][0]
        self.assertEqual(item["folder"], "product-01")
        self.assertEqual(item["product_identity"]["key"], "shops/templystudios/product-01")
        self.assertEqual(item["state"], "OFFLOAD_SCHEDULED")
        self.assertEqual(item["revision"], "rev-001")
        self.assertEqual(item["hash"], "a" * 64)
        self.assertTrue(item["local_available"])
        self.assertTrue(item["local_matches"])
        self.assertEqual(item["counts"]["total_bytes"], 1234)
        self.assertEqual(item["eligible_after"], "2026-08-12T00:00:00Z")
        self.assertEqual(item["cache"]["status"], "miss")
        self.assertIsNone(item["last_error"])

    async def test_status_master_scope_is_explicit_and_uses_master_path(self) -> None:
        result = await dashboard_app.cloud_assets_status(scope="master")

        self.assertEqual(result["scope"], "master")
        self.assertEqual(result["items"][0]["product_identity"]["key"], "master_products/product-03")
        self.assertEqual(self.store.status_targets, [self.master_product])

    async def test_status_wrong_shop_is_rejected_before_store_lookup(self) -> None:
        with self.assertRaises(dashboard_app.HTTPException) as raised:
            await dashboard_app.cloud_assets_status(shop_id="other-shop")

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.store.status_targets, [])

    async def test_scope_aliases_are_normalized_and_master_stays_under_master_products(self) -> None:
        self.assertEqual(dashboard_app._normalize_cloud_scope(" SHOP "), "shop")
        self.assertEqual(dashboard_app._normalize_cloud_scope("master"), "master")

        result = await dashboard_app.cloud_assets_upload(
            _JsonRequest(self._payload(scope="master", folder="product-03"))
        )

        self.assertEqual(self.store.calls, [("upload", self.master_product)])
        self.assertEqual(result["product"]["key"], "master_products/product-03")

    async def test_missing_product_is_404_but_master_escape_is_400(self) -> None:
        with self.assertRaises(dashboard_app.HTTPException) as missing:
            await dashboard_app.cloud_assets_upload(
                _JsonRequest(self._payload(folder="product-404"))
            )
        self.assertEqual(missing.exception.status_code, 404)

        with self.assertRaises(dashboard_app.HTTPException) as escaped:
            await dashboard_app.cloud_assets_upload(
                _JsonRequest(self._payload(scope="master", folder="product-03/../product-03"))
            )
        self.assertEqual(escaped.exception.status_code, 400)
        self.assertEqual(self.store.calls, [])

    async def test_every_post_requires_shop_scope_and_folder(self) -> None:
        for field in ("shop_id", "scope", "folder"):
            payload = self._payload()
            payload.pop(field)
            with self.subTest(field=field), self.assertRaises(dashboard_app.HTTPException) as raised:
                await dashboard_app.cloud_assets_verify(_JsonRequest(payload))
            self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(self.store.calls, [])

    async def test_wrong_shop_is_rejected_before_store_lookup(self) -> None:
        with self.assertRaises(dashboard_app.HTTPException) as raised:
            await dashboard_app.cloud_assets_upload(
                _JsonRequest(self._payload(shop_id="other-shop"))
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.store.calls, [])

    async def test_invalid_scope_and_absolute_or_traversal_folder_are_400(self) -> None:
        for payload in (
            self._payload(scope="shops"),
            self._payload(folder="product-01/../product-01"),
            self._payload(folder="/tmp/product-01"),
        ):
            with self.subTest(payload=payload), self.assertRaises(dashboard_app.HTTPException) as raised:
                await dashboard_app.cloud_assets_upload(_JsonRequest(payload))
            self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(self.store.calls, [])

    async def test_upload_verify_restore_route_to_store_in_worker_thread(self) -> None:
        upload = await dashboard_app.cloud_assets_upload(
            _JsonRequest(self._payload(revision="rev-explicit"))
        )
        verify = await dashboard_app.cloud_assets_verify(_JsonRequest(self._payload()))
        restore = await dashboard_app.cloud_assets_restore(
            _JsonRequest(self._payload(force=True))
        )

        self.assertTrue(upload["ok"])
        self.assertTrue(verify["ok"])
        self.assertTrue(restore["ok"])
        self.assertEqual([operation for operation, _ in self.store.calls], ["upload", "verify", "restore"])
        self.assertEqual(upload["product"]["key"], "shops/templystudios/product-01")
        self.assertEqual(upload["result"]["revision"], "rev-explicit")
        self.assertTrue(restore["result"]["force"])
        self.assertTrue(self.store.call_threads)
        self.assertTrue(all(name != threading.main_thread().name for name in self.store.call_threads))

    async def test_same_product_mutations_are_serialized(self) -> None:
        blocking_store = _FakeCloudAssetStore()

        started_event = threading.Event()
        release_event = threading.Event()

        def hold_upload() -> None:
            started_event.set()
            release_event.wait(5)

        blocking_store.on_upload_start = hold_upload

        with patch.object(dashboard_app, "get_cloud_asset_store", return_value=blocking_store):
            upload_one = asyncio.create_task(
                dashboard_app.cloud_assets_upload(_JsonRequest(self._payload()))
            )
            await asyncio.sleep(0)
            upload_two = asyncio.create_task(
                dashboard_app.cloud_assets_upload(_JsonRequest(self._payload()))
            )

            while not started_event.is_set():
                await asyncio.sleep(0)

            self.assertFalse(upload_two.done())
            self.assertEqual(blocking_store.calls, [("upload", self.shop_product)])
            self.assertEqual(blocking_store.max_active_calls, 1)

            release_event.set()
            responses = await asyncio.gather(upload_one, upload_two)

        self.assertEqual(blocking_store.calls, [("upload", self.shop_product), ("upload", self.shop_product)])
        self.assertEqual([response["ok"] for response in responses], [True, True])
        self.assertEqual(blocking_store.max_active_calls, 1)
        self.assertEqual(blocking_store.active_calls, 0)

    async def test_scheduled_upload_waits_for_single_sync_then_runs_upload_and_verify(self) -> None:
        dashboard_app._etsy_single_sync_busy_shops.add("templystudios")
        try:
            with patch.object(dashboard_app, "_CLOUD_ASSET_UPLOAD_IDLE_POLL_SECONDS", 0.01):
                response = await dashboard_app.cloud_assets_schedule_upload_verify(
                    _JsonRequest(self._payload())
                )
                self.assertEqual(response.status_code, 202)
                await asyncio.sleep(0.03)
                self.assertEqual(self.store.calls, [])

                status = await dashboard_app.cloud_assets_status(folder="product-01")
                self.assertEqual(status["items"][0]["state"], "UPLOAD_SCHEDULED")
                self.assertEqual(
                    status["items"][0]["upload_schedule"]["wait_reason"],
                    "đang Sync listing Etsy",
                )

                dashboard_app._etsy_single_sync_busy_shops.discard("templystudios")
                for _ in range(60):
                    if [operation for operation, _ in self.store.calls] == ["upload", "verify"]:
                        break
                    await asyncio.sleep(0.01)

            self.assertEqual(
                [operation for operation, _ in self.store.calls],
                ["upload", "verify"],
            )
            final_status = await dashboard_app.cloud_assets_status(folder="product-01")
            self.assertNotIn("upload_schedule", final_status["items"][0])
        finally:
            dashboard_app._etsy_single_sync_busy_shops.discard("templystudios")

    async def test_routes_use_mocked_get_cloud_asset_store(self) -> None:
        with patch.object(dashboard_app, "get_cloud_asset_store", return_value=self.store) as getter:
            result = await dashboard_app.cloud_assets_verify(_JsonRequest(self._payload()))

        getter.assert_called_once_with()
        self.assertTrue(result["ok"])

    async def test_maintain_defaults_to_dry_run_and_policy_disabled(self) -> None:
        result = await dashboard_app.cloud_assets_maintain(
            _JsonRequest(self._payload())
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["apply"])
        self.assertFalse(result["policy_enabled"])
        self.assertEqual(result["allowlist"], [])
        self.assertEqual(
            self.store.maintain_calls[0],
            {
                "product_roots": [self.shop_product],
                "apply": False,
                "offload_enabled": False,
                "allowlist": (),
                "older_than_days": None,
            },
        )

    async def test_maintain_applies_only_with_all_explicit_gates(self) -> None:
        product_key = "shops/templystudios/product-01"
        result = await dashboard_app.cloud_assets_maintain(
            _JsonRequest(
                self._payload(
                    apply=True,
                    dry_run=False,
                    policy_enabled=True,
                    allowlist=[product_key],
                )
            )
        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["dry_run"])
        self.assertTrue(result["apply"])
        self.assertTrue(result["policy_enabled"])
        self.assertEqual(result["allowlist"], [product_key])
        self.assertEqual(
            self.store.maintain_calls[0],
            {
                "product_roots": [self.shop_product],
                "apply": True,
                "offload_enabled": True,
                "allowlist": [product_key],
                "older_than_days": None,
            },
        )

    async def test_maintain_apply_requires_policy_and_exact_allowlist(self) -> None:
        with self.assertRaises(dashboard_app.HTTPException) as missing_policy:
            await dashboard_app.cloud_assets_maintain(
                _JsonRequest(self._payload(apply=True, dry_run=False))
            )
        self.assertEqual(missing_policy.exception.status_code, 409)

        with self.assertRaises(dashboard_app.HTTPException) as missing_allowlist:
            await dashboard_app.cloud_assets_maintain(
                _JsonRequest(
                    self._payload(
                        apply=True,
                        dry_run=False,
                        policy_enabled=True,
                        allowlist=["shops/templystudios/product-999"],
                    )
                )
            )
        self.assertEqual(missing_allowlist.exception.status_code, 409)
        self.assertEqual(self.store.maintain_calls, [])

    async def test_cancel_offload_routes_only_policy_action_and_never_delete(self) -> None:
        result = await dashboard_app.cloud_assets_cancel_offload(
            _JsonRequest(self._payload())
        )

        self.assertTrue(result["ok"])
        self.assertEqual(self.store.calls, [("cancel-offload", self.shop_product)])
        self.assertEqual(self.store.maintain_calls, [])
        self.assertFalse(hasattr(self.store, "delete"))


if __name__ == "__main__":
    unittest.main()
