from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from cloud_asset_store import (
    CloudAssetError,
    CloudAssetStore,
    LocalRemote,
    RemoteStoreError,
    canonical_json_bytes,
    load_local_state,
)


UTC = dt.timezone.utc


def make_product(root: Path) -> Path:
    product_root = root / "shops" / "templystudios" / "product-01"
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
    *,
    success_ttl_seconds: int = 24 * 60 * 60,
    failure_ttl_seconds: int = 7 * 24 * 60 * 60,
) -> tuple[CloudAssetStore, LocalRemote]:
    remote = LocalRemote(remote_root)
    store = CloudAssetStore(
        repo_root=root,
        remote_store=remote,
        cache_root=cache_root,
        lock_timeout_seconds=2,
        success_ttl_seconds=success_ttl_seconds,
        failure_ttl_seconds=failure_ttl_seconds,
    )
    return store, remote


def make_cloud_only(product_root: Path) -> None:
    for directory in (product_root / "images", product_root / "files"):
        for child in directory.iterdir():
            child.unlink()


class CloudAssetHydrationTests(unittest.TestCase):
    def test_local_hit_returns_current_manifest_and_set_input_files_lists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            product = make_product(root)
            store, remote = make_store(root, Path(temporary) / "remote", Path(temporary) / "cache")
            uploaded = store.upload(product, revision="rev-local", now=dt.datetime(2026, 8, 5, tzinfo=UTC))

            result = store.hydrate_product(product, purpose="browser", now=dt.datetime(2026, 8, 5, 1, tzinfo=UTC))

            self.assertEqual(result["mode"], "local")
            self.assertEqual(result["source"], "local")
            self.assertEqual(result["purpose"], "browser")
            self.assertEqual(result["product_root"], str(product))
            self.assertEqual(result["revision"], "rev-local")
            self.assertEqual(result["manifest_hash"], uploaded["manifest_sha256"])
            self.assertEqual(result["manifest_sha256"], uploaded["manifest_sha256"])
            self.assertEqual(result["images"], result["image_paths"])
            self.assertEqual(result["files"], result["file_paths"])
            self.assertTrue(all(Path(path).is_file() for path in result["images"] + result["files"]))
            self.assertFalse(result["cleanup"]["required"])
            self.assertFalse(any(operation == "download" for operation, _ in remote.operations))

    def test_cloud_only_hydrates_persistent_revision_cache_without_installing_product(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            product = make_product(root)
            store, remote = make_store(root, Path(temporary) / "remote", Path(temporary) / "cache")
            store.upload(product, revision="rev-cloud", now=dt.datetime(2026, 8, 5, tzinfo=UTC))
            make_cloud_only(product)

            result = store.hydrate_product(product, purpose="post", now=dt.datetime(2026, 8, 5, 1, tzinfo=UTC))

            self.assertEqual(result["mode"], "cloud")
            self.assertEqual(result["source"], "cloud-cache")
            self.assertFalse(result["cache_hit"])
            self.assertEqual(result["revision"], "rev-cloud")
            self.assertTrue(result["cleanup"]["required"])
            self.assertTrue(result["cache_path"].startswith(str(Path(temporary) / "cache" / "data")))
            self.assertTrue(all(Path(path).is_file() for path in result["images"] + result["files"]))
            self.assertTrue(all(str(Path(path)).startswith(result["cache_path"]) for path in result["images"] + result["files"]))
            self.assertFalse((product / "images" / "01-hero.png").exists())
            self.assertFalse((product / "files" / "source.zip").exists())
            self.assertEqual([operation for operation, _ in remote.operations].count("download"), 1)
            self.assertEqual(load_local_state(product)["state"], "CLOUD_VERIFIED")

    def test_local_manifest_mismatch_fails_closed_without_using_cloud_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            product = make_product(root)
            store, remote = make_store(root, Path(temporary) / "remote", Path(temporary) / "cache")
            store.upload(product, revision="rev-dirty", now=dt.datetime(2026, 8, 5, tzinfo=UTC))
            (product / "files" / "source.zip").write_bytes(b"dirty-local")

            with self.assertRaisesRegex(CloudAssetError, "do not match"):
                store.hydrate_product(product, purpose="browser")

            self.assertEqual((product / "files" / "source.zip").read_bytes(), b"dirty-local")
            self.assertEqual([operation for operation, _ in remote.operations].count("download"), 0)
            self.assertFalse((Path(temporary) / "cache" / "data").exists())

    def test_success_cache_reuse_honors_configured_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            product = make_product(root)
            store, remote = make_store(
                root,
                Path(temporary) / "remote",
                Path(temporary) / "cache",
                success_ttl_seconds=60,
                failure_ttl_seconds=120,
            )
            store.upload(product, revision="rev-reuse", now=dt.datetime(2026, 8, 5, tzinfo=UTC))
            make_cloud_only(product)
            first_time = dt.datetime(2026, 8, 5, 1, tzinfo=UTC)

            first = store.hydrate_product(product, purpose="browser", now=first_time)
            second = store.hydrate_product(product, purpose="post", now=first_time + dt.timedelta(seconds=59))

            self.assertFalse(first["cache_hit"])
            self.assertTrue(second["cache_hit"])
            self.assertEqual(first["cache_path"], second["cache_path"])
            self.assertEqual([operation for operation, _ in remote.operations].count("download"), 1)
            # The cache metadata itself stores the configured 60-second TTL;
            # inspect the public result rather than relying on wall-clock time.
            expires = dt.datetime.fromisoformat(second["cache_expires_at"].replace("Z", "+00:00"))
            self.assertEqual(expires, first_time + dt.timedelta(seconds=60))

    def test_corrupted_remote_retains_failure_metadata_and_does_not_retry_inside_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            product = make_product(root)
            cache_root = Path(temporary) / "cache"
            store, remote = make_store(
                root,
                Path(temporary) / "remote",
                cache_root,
                success_ttl_seconds=60,
                failure_ttl_seconds=120,
            )
            store.upload(product, revision="rev-corrupt", now=dt.datetime(2026, 8, 5, tzinfo=UTC))
            make_cloud_only(product)
            remote_file = (
                remote.root
                / "assets"
                / "v1"
                / "shops"
                / "templystudios"
                / "product-01"
                / "revisions"
                / "rev-corrupt"
                / "files"
                / "source.zip"
            )
            remote_file.write_bytes(b"corrupted-remote")
            first_time = dt.datetime(2026, 8, 5, 1, tzinfo=UTC)

            with self.assertRaises(CloudAssetError):
                store.hydrate_product(product, purpose="browser", now=first_time)

            metadata_files = list((cache_root / "hydration-metadata").glob("*.json"))
            self.assertEqual(len(metadata_files), 1)
            metadata = json.loads(metadata_files[0].read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "failure")
            self.assertEqual(metadata["revision"], "rev-corrupt")
            self.assertEqual(metadata["expires_at"], (first_time + dt.timedelta(seconds=120)).isoformat().replace("+00:00", "Z"))
            self.assertNotIn("access_token", metadata.get("error", ""))
            downloads_after_first = [operation for operation, _ in remote.operations].count("download")

            with self.assertRaisesRegex(RemoteStoreError, "retry is retained"):
                store.hydrate_product(product, purpose="post", now=first_time + dt.timedelta(seconds=30))

            self.assertEqual([operation for operation, _ in remote.operations].count("download"), downloads_after_first)
            self.assertEqual(load_local_state(product)["state"], "ERROR")

    def test_path_safety_fails_closed_for_pointer_traversal_and_cache_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            product = make_product(root)
            remote_root = Path(temporary) / "remote"
            cache_root = Path(temporary) / "cache"
            store, remote = make_store(root, remote_root, cache_root)
            store.upload(product, revision="rev-safe", now=dt.datetime(2026, 8, 5, tzinfo=UTC))
            make_cloud_only(product)
            current_path = remote_root / "assets" / "v1" / "shops" / "templystudios" / "product-01" / "current.json"
            pointer = json.loads(current_path.read_text(encoding="utf-8"))
            pointer["revision"] = "../escape"
            pointer["revision_path"] = "assets/v1/shops/templystudios/product-01/revisions/../escape"
            current_path.write_bytes(canonical_json_bytes(pointer))

            with self.assertRaises(CloudAssetError):
                store.hydrate_product(product, purpose="browser")
            self.assertFalse((cache_root / "data" / "escape").exists())

            # Recreate a valid pointer/state and place a symlink exactly where
            # the deterministic revision cache would be installed.
            for metadata_path in (cache_root / "hydration-metadata").glob("*.json"):
                metadata_path.unlink()
            store, remote = make_store(root, remote_root, cache_root)
            # The malformed-pointer failure is retained only for the current
            # key, so restore the valid pointer before exercising cache safety.
            uploaded_pointer = json.loads(
                (remote_root / "assets" / "v1" / "shops" / "templystudios" / "product-01" / "current.json").read_text(
                    encoding="utf-8"
                )
            )
            uploaded_pointer["revision"] = "rev-safe"
            uploaded_pointer["revision_path"] = "assets/v1/shops/templystudios/product-01/revisions/rev-safe"
            current_path.write_bytes(canonical_json_bytes(uploaded_pointer))
            outside = Path(temporary) / "outside-cache"
            outside.mkdir()
            cache_entry = cache_root / "data" / "shops" / "templystudios" / "product-01" / "rev-safe"
            cache_entry.parent.mkdir(parents=True)
            cache_entry.symlink_to(outside, target_is_directory=True)

            with self.assertRaises(CloudAssetError):
                store.hydrate_product(product, purpose="post")
            self.assertEqual(list(outside.iterdir()), [])
            self.assertTrue(cache_entry.is_symlink())

    def test_cleanup_requires_success_marker_and_never_removes_product_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            product = make_product(root)
            cache_root = Path(temporary) / "cache"
            store, _ = make_store(
                root,
                Path(temporary) / "remote",
                cache_root,
                success_ttl_seconds=60,
                failure_ttl_seconds=120,
            )
            store.upload(product, revision="rev-cleanup", now=dt.datetime(2026, 8, 5, tzinfo=UTC))
            make_cloud_only(product)
            hydrated_at = dt.datetime(2026, 8, 5, 1, tzinfo=UTC)
            result = store.hydrate_product(product, purpose="browser", now=hydrated_at)
            cache_path = Path(result["cache_path"])
            self.assertTrue(cache_path.is_dir())

            # TTL expiration alone cannot remove bytes before the operation
            # reports success.
            self.assertEqual(store.cleanup_cache(now=hydrated_at + dt.timedelta(seconds=61)), [])
            self.assertTrue(cache_path.is_dir())

            marked = store.mark_hydration_cleanup_eligible(result, now=hydrated_at + dt.timedelta(seconds=30))
            self.assertTrue(marked["marked"])
            cleaned = store.cleanup_hydration_cache(now=hydrated_at + dt.timedelta(seconds=61))
            self.assertEqual(len(cleaned), 1)
            self.assertTrue(cleaned[0]["removed_cache"])
            self.assertFalse(cache_path.exists())
            self.assertTrue((product / "images").is_dir())
            self.assertTrue((product / "files").is_dir())
            self.assertFalse((product / "images" / "01-hero.png").exists())
            self.assertFalse((product / "files" / "source.zip").exists())


if __name__ == "__main__":
    unittest.main()
