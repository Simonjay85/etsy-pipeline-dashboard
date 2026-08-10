from __future__ import annotations

import base64
import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloud_asset_store import (
    AssetValidationError,
    CloudAssetStore,
    HydrationCache,
    LocalRemote,
    discover_product_roots,
    load_local_state,
    resolve_product,
    utc_text,
)
from cloud_asset_store_config import DEFAULT_FAILURE_TTL_SECONDS


UTC = dt.timezone.utc


def make_product(root: Path, shop: str = "templystudios", product: str = "product-01") -> Path:
    product_root = root / "shops" / shop / product
    (product_root / "images").mkdir(parents=True)
    (product_root / "files").mkdir()
    (product_root / "images" / "01-hero.png").write_bytes(b"image-one")
    (product_root / "images" / "02-detail.jpg").write_bytes(b"image-two")
    (product_root / "files" / "source.zip").write_bytes(b"downloadable-source")
    return product_root


def make_store(
    root: Path,
    remote_root: Path,
    cache_root: Path,
    offload_age_days: int = 7,
) -> tuple[CloudAssetStore, LocalRemote]:
    remote = LocalRemote(remote_root)
    store = CloudAssetStore(
        repo_root=root,
        remote_store=remote,
        cache_root=cache_root,
        lock_timeout_seconds=2,
        offload_age_days=offload_age_days,
    )
    return store, remote


class CloudAssetStoreTests(unittest.TestCase):
    def test_upload_creates_small_webp_preview_when_converter_is_available(self) -> None:
        has_pillow = False
        try:
            import PIL  # noqa: F401

            has_pillow = True
        except ImportError:
            pass
        if not has_pillow and not shutil.which("cwebp"):
            self.skipTest("Pillow or cwebp is required for preview conversion")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            product = root / "shops" / "templystudios" / "product-01"
            (product / "images").mkdir(parents=True)
            (product / "files").mkdir()
            (product / "images" / "01-hero.png").write_bytes(
                base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                )
            )
            (product / "files" / "source.zip").write_bytes(b"downloadable-source")
            store, _ = make_store(root, Path(temporary) / "remote", Path(temporary) / "cache")

            result = store.upload(
                product,
                revision="preview-revision",
                now=dt.datetime(2026, 8, 5, tzinfo=UTC),
            )

            preview = product / ".cloud-preview.webp"
            self.assertTrue(preview.is_file())
            self.assertGreater(preview.stat().st_size, 0)
            self.assertTrue(result["preview_created"])
            self.assertEqual(result["counts"]["preview"], 1)

    def test_upload_writes_manifest_and_pointer_only_after_remote_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            remote_root = Path(temporary) / "remote"
            cache_root = Path(temporary) / "cache"
            product = make_product(root)
            store, remote = make_store(root, remote_root, cache_root)
            now = dt.datetime(2026, 8, 4, 3, 0, tzinfo=UTC)

            result = store.upload(product, revision="rev-001", now=now)

            self.assertTrue(result["ok"])
            self.assertEqual(result["state"], "CLOUD_VERIFIED")
            self.assertFalse(result["idempotent"])
            current_path = remote_root / "assets/v1/shops/templystudios/product-01/current.json"
            manifest_path = remote_root / "assets/v1/shops/templystudios/product-01/revisions/rev-001/manifest.json"
            self.assertTrue(current_path.is_file())
            self.assertTrue(manifest_path.is_file())
            pointer = json.loads(current_path.read_text(encoding="utf-8"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(pointer["revision"], "rev-001")
            self.assertEqual(pointer["manifest_sha256"], result["manifest_sha256"])
            self.assertEqual(manifest["counts"]["images"], 2)
            self.assertEqual(manifest["counts"]["files"], 1)
            self.assertEqual(manifest["counts"]["total"], 3)
            self.assertTrue(all(len(item["sha256"]) == 64 for item in manifest["files"]))
            self.assertEqual(
                [operation for operation, _ in remote.operations],
                ["upload", "verify", "write"],
            )
            local_state = load_local_state(product)
            self.assertEqual(local_state["current_revision"], "rev-001")
            self.assertTrue((cache_root / "audit").is_dir())

    def test_revisions_are_immutable_and_same_current_content_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            product = make_product(root)
            store, remote = make_store(root, Path(temporary) / "remote", Path(temporary) / "cache")
            first_time = dt.datetime(2026, 8, 4, tzinfo=UTC)
            first = store.upload(product, revision="rev-001", now=first_time)
            original_remote_file = (
                remote.root / "assets/v1/shops/templystudios/product-01/revisions/rev-001/files/source.zip"
            ).read_bytes()
            (product / "files" / "source.zip").write_bytes(b"new-revision-source")
            second = store.upload(product, revision="rev-002", now=first_time + dt.timedelta(minutes=1))
            repeat = store.upload(product, now=first_time + dt.timedelta(minutes=2))

            self.assertNotEqual(first["manifest_sha256"], second["manifest_sha256"])
            self.assertEqual(original_remote_file, b"downloadable-source")
            self.assertEqual(repeat["revision"], "rev-002")
            self.assertTrue(repeat["idempotent"])
            pointer = json.loads(
                (remote.root / "assets/v1/shops/templystudios/product-01/current.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(pointer["revision"], "rev-002")

    def test_upload_rejects_missing_zero_byte_dataless_symlink_and_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            product = make_product(root)
            store, _ = make_store(root, Path(temporary) / "remote", Path(temporary) / "cache")
            (product / "files" / "Kawaii Planner Source.zip").write_bytes(b"filename-with-spaces")
            (product / "images" / ".DS_Store").write_bytes(b"finder-metadata")

            (product / "files" / "source.zip").write_bytes(b"")
            with self.assertRaises(AssetValidationError):
                store.upload(product, revision="zero-byte")
            (product / "files" / "source.zip").write_bytes(b"downloadable-source")

            outside = Path(temporary) / "outside.bin"
            outside.write_bytes(b"outside")
            (product / "images" / "linked.png").symlink_to(outside)
            with self.assertRaises(AssetValidationError):
                store.upload(product, revision="symlink")
            (product / "images" / "linked.png").unlink()

            (product / "images" / "placeholder.png").write_bytes(b"placeholder")
            with patch("cloud_asset_store._is_dataless", side_effect=lambda path, info=None: Path(path).name == "placeholder.png"):
                with self.assertRaises(AssetValidationError):
                    store.upload(product, revision="dataless")
            (product / "images" / "placeholder.png").unlink()
            with self.assertRaises(AssetValidationError):
                resolve_product(root, "shops/templystudios/../templystudios/product-01")

            for path in (product / "images").iterdir():
                path.unlink()
            (product / "images").rmdir()
            with self.assertRaises(AssetValidationError):
                store.upload(product, revision="missing-images")

    def test_restore_verifies_staged_remote_bytes_before_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            product = make_product(root)
            store, _ = make_store(root, Path(temporary) / "remote", Path(temporary) / "cache")
            now = dt.datetime(2026, 8, 4, tzinfo=UTC)
            store.upload(product, revision="rev-restore", now=now)
            for path in (product / "images").iterdir():
                path.unlink()
            for path in (product / "files").iterdir():
                path.unlink()

            result = store.restore(product, now=now + dt.timedelta(minutes=1))

            self.assertEqual(result["state"], "READY_LOCAL")
            self.assertEqual((product / "images" / "01-hero.png").read_bytes(), b"image-one")
            self.assertEqual((product / "files" / "source.zip").read_bytes(), b"downloadable-source")
            state = load_local_state(product)
            history_states = [entry["to"] for entry in state["history"]]
            self.assertIn("RESTORING", history_states)
            self.assertIn("RESTORE_VERIFIED", history_states)
            self.assertIn("READY_LOCAL", history_states)
            self.assertEqual(
                state["eligible_after"],
                utc_text(now + dt.timedelta(minutes=1, days=7)),
            )

    def test_no_install_verify_records_restore_verification_and_uses_configured_age(self) -> None:
        for age_days in (7, 30):
            with self.subTest(age_days=age_days), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "repo"
                remote_root = Path(temporary) / "remote"
                cache_root = Path(temporary) / "cache"
                product = make_product(root)
                store, remote = make_store(
                    root,
                    remote_root,
                    cache_root,
                    offload_age_days=age_days,
                )
                uploaded_at = dt.datetime(2026, 8, 4, tzinfo=UTC)
                verified_at = uploaded_at + dt.timedelta(hours=1)
                store.upload(product, revision=f"rev-{age_days}", now=uploaded_at)

                result = store.verify(product, now=verified_at)

                state = load_local_state(product)
                self.assertTrue(result["ok"])
                self.assertEqual(state["last_restore_verified_at"], utc_text(verified_at))
                self.assertEqual(
                    state["eligible_after"],
                    utc_text(verified_at + dt.timedelta(days=age_days)),
                )
                self.assertEqual(state["state"], "OFFLOAD_SCHEDULED")
                self.assertTrue((product / "images" / "01-hero.png").is_file())
                self.assertIn("download", [operation for operation, _ in remote.operations])

    def test_no_install_verify_keeps_cloud_only_and_dirty_states(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            product = make_product(root)
            store, _ = make_store(root, Path(temporary) / "remote", Path(temporary) / "cache")
            verified_at = dt.datetime(2026, 8, 4, tzinfo=UTC)
            store.upload(product, revision="rev-state-branches", now=verified_at)

            (product / "files" / "source.zip").write_bytes(b"dirty-local")
            dirty = store.verify(product, now=verified_at + dt.timedelta(minutes=1))
            self.assertFalse(dirty["ok"])
            self.assertEqual(dirty["state"], "DIRTY_LOCAL")

            (product / "files" / "source.zip").write_bytes(b"downloadable-source")
            for directory in (product / "images", product / "files"):
                for child in directory.iterdir():
                    child.unlink()
                directory.rmdir()
            cloud_only = store.verify(product, now=verified_at + dt.timedelta(minutes=2))
            self.assertFalse(cloud_only["ok"])
            self.assertEqual(cloud_only["state"], "CLOUD_ONLY")

    def test_restore_requires_force_for_dirty_local_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            product = make_product(root)
            store, _ = make_store(root, Path(temporary) / "remote", Path(temporary) / "cache")
            store.upload(product, revision="rev-restore", now=dt.datetime(2026, 8, 4, tzinfo=UTC))
            (product / "files" / "source.zip").write_bytes(b"dirty")

            with self.assertRaisesRegex(Exception, "dirty"):
                store.restore(product)
            self.assertEqual((product / "files" / "source.zip").read_bytes(), b"dirty")
            result = store.restore(product, force=True)
            self.assertEqual(result["state"], "READY_LOCAL")
            self.assertEqual((product / "files" / "source.zip").read_bytes(), b"downloadable-source")

    def test_maintenance_is_gated_by_policy_allowlist_age_restore_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            product = make_product(root)
            store, _ = make_store(root, Path(temporary) / "remote", Path(temporary) / "cache")
            uploaded_at = dt.datetime(2026, 7, 20, tzinfo=UTC)
            maintenance_at = dt.datetime(2026, 7, 28, tzinfo=UTC)
            store.upload(product, revision="rev-maintain", now=uploaded_at)
            store.verify(product, now=uploaded_at + dt.timedelta(minutes=1))

            disabled = store.maintain(
                [product],
                apply=True,
                offload_enabled=False,
                allowlist=["shops/templystudios/product-01"],
                now=maintenance_at,
            )
            self.assertFalse(disabled[0]["applied"])
            self.assertTrue((product / "files" / "source.zip").is_file())

            dry_run = store.maintain(
                [product],
                apply=False,
                offload_enabled=True,
                allowlist=["shops/templystudios/product-01"],
                now=maintenance_at,
            )
            self.assertTrue(dry_run[0]["would_offload"])
            self.assertFalse(dry_run[0]["applied"])
            self.assertTrue((product / "images" / "01-hero.png").is_file())

            applied = store.maintain(
                [product],
                apply=True,
                offload_enabled=True,
                allowlist=["shops/templystudios/product-01"],
                now=maintenance_at,
            )
            self.assertTrue(applied[0]["applied"])
            self.assertEqual(applied[0]["state"], "CLOUD_ONLY")
            self.assertTrue((product / "images").is_dir())
            self.assertTrue((product / "files").is_dir())
            self.assertFalse((product / "images" / "01-hero.png").exists())
            self.assertFalse((product / "files" / "source.zip").exists())
            self.assertTrue((product / ".cloud-assets.json").is_file())
            receipts = list((Path(temporary) / "cache" / "audit").glob("*.json"))
            self.assertGreaterEqual(len(receipts), 3)

    def test_maintenance_does_not_delete_when_local_hash_or_remote_reverification_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            product = make_product(root)
            store, remote = make_store(root, Path(temporary) / "remote", Path(temporary) / "cache")
            uploaded_at = dt.datetime(2026, 7, 20, tzinfo=UTC)
            maintenance_at = dt.datetime(2026, 7, 28, tzinfo=UTC)
            store.upload(product, revision="rev-guard", now=uploaded_at)
            store.verify(product, now=uploaded_at + dt.timedelta(minutes=1))
            (product / "files" / "source.zip").write_bytes(b"dirty")
            dirty = store.maintain(
                [product],
                apply=True,
                offload_enabled=True,
                allowlist=["shops/templystudios/product-01"],
                now=maintenance_at,
            )
            self.assertFalse(dirty[0]["applied"])
            self.assertTrue((product / "files" / "source.zip").is_file())

            (product / "files" / "source.zip").write_bytes(b"downloadable-source")
            remote_file = remote.root / "assets/v1/shops/templystudios/product-01/revisions/rev-guard/files/source.zip"
            remote_file.write_bytes(b"tampered-remote")
            blocked = store.maintain(
                [product],
                apply=True,
                offload_enabled=True,
                allowlist=["shops/templystudios/product-01"],
                now=maintenance_at,
            )
            self.assertFalse(blocked[0]["applied"])
            self.assertTrue((product / "files" / "source.zip").is_file())
            self.assertEqual(load_local_state(product)["state"], "ERROR")

    def test_maintenance_requires_prior_restore_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            product = make_product(root)
            store, _ = make_store(root, Path(temporary) / "remote", Path(temporary) / "cache")
            uploaded_at = dt.datetime(2026, 7, 1, tzinfo=UTC)
            store.upload(product, revision="rev-no-restore-check", now=uploaded_at)

            result = store.maintain(
                [product],
                apply=True,
                offload_enabled=True,
                allowlist=["shops/templystudios/product-01"],
                now=uploaded_at + dt.timedelta(days=30),
            )

            self.assertFalse(result[0]["applied"])
            self.assertFalse(result[0]["would_offload"])
            self.assertIn("no successful temporary restore verification", result[0]["reason"])
            state = load_local_state(product)
            self.assertEqual(state["state"], "CLOUD_VERIFIED")
            self.assertIsNone(state.get("last_restore_verified_at"))
            self.assertIsNone(state.get("eligible_after"))
            self.assertTrue((product / "files" / "source.zip").is_file())

    def test_maintenance_uses_configured_age_for_apply(self) -> None:
        for age_days in (7, 30):
            with self.subTest(age_days=age_days), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "repo"
                product = make_product(root)
                store, _ = make_store(
                    root,
                    Path(temporary) / "remote",
                    Path(temporary) / "cache",
                    offload_age_days=age_days,
                )
                verified_at = dt.datetime(2026, 7, 1, tzinfo=UTC)
                store.upload(product, revision=f"rev-age-{age_days}", now=verified_at)
                store.verify(product, now=verified_at)
                allowlist = ["shops/templystudios/product-01"]

                early = store.maintain(
                    [product],
                    apply=True,
                    offload_enabled=True,
                    allowlist=allowlist,
                    now=verified_at + dt.timedelta(days=age_days, minutes=-1),
                )
                self.assertFalse(early[0]["applied"])
                self.assertTrue((product / "files" / "source.zip").is_file())

                applied = store.maintain(
                    [product],
                    apply=True,
                    offload_enabled=True,
                    allowlist=allowlist,
                    now=verified_at + dt.timedelta(days=age_days),
                )
                self.assertTrue(applied[0]["applied"])
                self.assertEqual(applied[0]["state"], "CLOUD_ONLY")

    def test_maintenance_dry_run_does_not_change_state_or_eligibility(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            product = make_product(root)
            store, _ = make_store(root, Path(temporary) / "remote", Path(temporary) / "cache")
            verified_at = dt.datetime(2026, 7, 1, tzinfo=UTC)
            store.upload(product, revision="rev-dry-run", now=verified_at)
            store.verify(product, now=verified_at)
            state_path = product / ".cloud-assets.json"
            before_bytes = state_path.read_bytes()
            before_state = load_local_state(product)
            eligible_at = verified_at + dt.timedelta(days=7)

            result = store.maintain(
                [product],
                apply=False,
                offload_enabled=True,
                allowlist=["shops/templystudios/product-01"],
                now=eligible_at,
            )

            self.assertTrue(result[0]["would_offload"])
            self.assertFalse(result[0]["applied"])
            self.assertEqual(state_path.read_bytes(), before_bytes)
            after_state = load_local_state(product)
            self.assertEqual(after_state, before_state)
            self.assertEqual(after_state["state"], "OFFLOAD_SCHEDULED")
            self.assertEqual(after_state["history"], before_state["history"])
            self.assertTrue((product / "images" / "01-hero.png").is_file())

    def test_idempotent_upload_preserves_restore_verification_age(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            product = make_product(root)
            store, _ = make_store(root, Path(temporary) / "remote", Path(temporary) / "cache")
            uploaded_at = dt.datetime(2026, 7, 1, tzinfo=UTC)
            verified_at = uploaded_at + dt.timedelta(hours=2)
            store.upload(product, revision="rev-idempotent", now=uploaded_at)
            store.verify(product, now=verified_at)
            before = load_local_state(product)

            result = store.upload(product, now=verified_at + dt.timedelta(days=2))

            after = load_local_state(product)
            self.assertTrue(result["idempotent"])
            self.assertEqual(after["last_restore_verified_at"], before["last_restore_verified_at"])
            self.assertEqual(after["eligible_after"], before["eligible_after"])
            self.assertEqual(after["last_restore_verified_revision"], before["last_restore_verified_revision"])

    def test_changed_revision_clears_restore_verification_age(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            product = make_product(root)
            store, _ = make_store(root, Path(temporary) / "remote", Path(temporary) / "cache")
            uploaded_at = dt.datetime(2026, 7, 1, tzinfo=UTC)
            store.upload(product, revision="rev-old", now=uploaded_at)
            store.verify(product, now=uploaded_at + dt.timedelta(hours=1))
            (product / "files" / "source.zip").write_bytes(b"changed-revision-source")

            store.upload(product, revision="rev-new", now=uploaded_at + dt.timedelta(days=1))

            state = load_local_state(product)
            self.assertEqual(state["current_revision"], "rev-new")
            self.assertIsNone(state.get("last_restore_verified_at"))
            self.assertIsNone(state.get("eligible_after"))

    def test_hydration_cache_has_success_and_failure_ttl_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = HydrationCache(Path(temporary), success_ttl_seconds=60, failure_ttl_seconds=10)
            now = dt.datetime(2026, 8, 4, tzinfo=UTC)
            success = cache.store_success("shops/templystudios/product-01/images/01.png", b"bytes", now=now)
            self.assertTrue(success.hit)
            self.assertEqual(cache.lookup(success.key, now=now + dt.timedelta(seconds=59)).status, "success")
            self.assertFalse(cache.lookup(success.key, now=now + dt.timedelta(seconds=61)).hit)
            failure = cache.store_failure("missing", "access_token=do-not-record", now=now)
            self.assertTrue(failure.hit)
            self.assertEqual(cache.lookup("missing", now=now + dt.timedelta(seconds=9)).status, "failure")
            self.assertFalse(cache.lookup("missing", now=now + dt.timedelta(seconds=11)).hit)
            metadata = next((Path(temporary) / "metadata").glob("*.json"))
            self.assertNotIn("do-not-record", metadata.read_text(encoding="utf-8"))

    def test_default_failure_cache_ttl_is_seven_days(self) -> None:
        self.assertEqual(DEFAULT_FAILURE_TTL_SECONDS, 7 * 24 * 60 * 60)

    def test_inventory_is_canonical_and_skips_invalid_parallel_trees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            product = make_product(root)
            (root / "shops" / "templystudios" / "not-a-product").mkdir()
            (root / "other" / "product-99").mkdir(parents=True)
            found = discover_product_roots(root)
            self.assertEqual(
                [identity.key for _, identity in found],
                ["shops/templystudios/not-a-product", "shops/templystudios/product-01"],
            )
            self.assertEqual(resolve_product(root, product)[1].key, "shops/templystudios/product-01")


if __name__ == "__main__":
    unittest.main()
