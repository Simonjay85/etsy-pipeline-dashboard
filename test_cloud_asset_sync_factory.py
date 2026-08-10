from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import etsy_shop_sync


class FakeCandidateStore:
    def __init__(self):
        self.calls = []

    def record_local_candidate(self, product_root: Path) -> dict:
        self.calls.append(Path(product_root))
        return {"ok": True, "marked": True, "state": "DIRTY_LOCAL"}


class CloudAssetSyncFactoryTests(unittest.TestCase):
    def test_candidate_helper_marks_only_a_clearly_resolved_product_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            product_root = base / "shops" / "templystudios" / "product-07"
            (product_root / "images").mkdir(parents=True)
            (product_root / "files").mkdir()
            store = FakeCandidateStore()

            with patch.object(etsy_shop_sync, "BASE_DIR", base):
                result = etsy_shop_sync.record_local_sync_candidate(
                    "templystudios", "product-07", store
                )

            self.assertTrue(result["marked"])
            self.assertEqual([product_root], store.calls)

    def test_candidate_helper_does_not_guess_for_invalid_or_unresolved_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            store = FakeCandidateStore()
            with patch.object(etsy_shop_sync, "BASE_DIR", base):
                invalid = etsy_shop_sync.record_local_sync_candidate(
                    "templystudios", "../master_products/product-07", store
                )
                missing = etsy_shop_sync.record_local_sync_candidate(
                    "templystudios", "product-07", store
                )

            self.assertFalse(invalid["marked"])
            self.assertFalse(missing["marked"])
            self.assertEqual([], store.calls)


if __name__ == "__main__":
    unittest.main()
