from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openpyxl

from etsy_catalog import _write_product_financials, merge_safe_duplicates
from scripts.daisy_folder_renumber import MigrationError, apply_plan, build_plan
from test_daisy_folder_renumber import DaisyFolderRenumberTests


class FinancialIdentityTests(unittest.TestCase):
    def _write_store(self, shop: Path, products: dict) -> bytes:
        value = {"version": 4, "shop_id": "daisyflowdigital", "settings": {"confirmed": True},
                 "products": products}
        data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        (shop / "product_financials.json").write_bytes(data)
        (shop / "product_financials.json.lock").touch()
        return data

    def _merge_fixture(self, root: Path, products: dict) -> tuple[Path, Path]:
        shop = root / "shops" / "daisyflowdigital"
        shop.mkdir(parents=True)
        for folder in ("product-a", "product-b"):
            (shop / folder / "files").mkdir(parents=True)
            (shop / folder / "files" / "same.pdf").write_bytes(b"same deliverable")
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Listings"
        sheet.cell(4, 2).value = "product-a"
        sheet.cell(5, 2).value = "product-b"
        workbook_path = shop / "Etsy_SEO_Generator.xlsx"
        workbook.save(workbook_path)
        workbook.close()
        self._write_store(shop, products)
        return shop, workbook_path

    @staticmethod
    def _group():
        return {"counts": {"total": 2}, "duplicate_groups": [{
            "group_id": "financial-identity", "match_type": "matching digital asset signature",
            "match_value": "fixture", "asset_names": [], "folders": ["product-a", "product-b"],
            "listing_ids": [], "safe_to_merge": True,
            "records": [{"folder": "product-a", "row": 4}, {"folder": "product-b", "row": 5}],
        }]}

    def test_merge_conflict_stops_before_any_product_or_workbook_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a = {"costs": {"product": {"amount": "100", "currency": "VND"}}}
            b = {"costs": {"product": {"amount": "200", "currency": "VND"}}}
            shop, workbook = self._merge_fixture(root, {"product-a": a, "product-b": b})
            paths = (shop / "product-a" / "files" / "same.pdf", shop / "product-b" / "files" / "same.pdf",
                     workbook, shop / "product_financials.json")
            before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
            with patch("etsy_catalog.build_unified_catalog", return_value=self._group()):
                result = merge_safe_duplicates(root, "daisyflowdigital", workbook, ["financial-identity"])
            self.assertEqual([], result["merged"])
            self.assertIn("financial records differ", result["skipped"][0]["reason"])
            self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})
            self.assertTrue((shop / "product-a").is_dir())
            self.assertTrue((shop / "product-b").is_dir())
            self.assertFalse(list(shop.glob(".quarantine_duplicates_*")))
            self.assertFalse(list(shop.glob("*.backup_aggregate_merge_*.xlsx")))

    def test_merge_promotes_source_record_when_canonical_has_none_and_keeps_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = {"costs": {"product": {"amount": "1250000", "currency": "VND"}}}
            shop, workbook = self._merge_fixture(root, {"product-a": payload})
            with patch("etsy_catalog.build_unified_catalog", return_value=self._group()):
                result = merge_safe_duplicates(root, "daisyflowdigital", workbook, ["financial-identity"])
            self.assertEqual(1, len(result["merged"]))
            saved = json.loads((shop / "product_financials.json").read_text())
            self.assertEqual(payload, saved["products"]["product-b"])
            self.assertEqual(payload, saved["products"]["product-a"])

    def test_merge_failure_after_financial_write_restores_folders_and_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = {"costs": {"product": {"amount": "1250000", "currency": "VND"}}}
            shop, workbook = self._merge_fixture(root, {"product-a": payload})
            workbook_hash = hashlib.sha256(workbook.read_bytes()).hexdigest()
            with patch("etsy_catalog.build_unified_catalog", return_value=self._group()), \
                    patch("openpyxl.workbook.workbook.Workbook.save", side_effect=OSError("injected save failure")):
                result = merge_safe_duplicates(root, "daisyflowdigital", workbook, ["financial-identity"])
            self.assertEqual([], result["merged"])
            self.assertIn("rollback", result["skipped"][0]["reason"])
            self.assertTrue((shop / "product-a").is_dir())
            self.assertTrue((shop / "product-b").is_dir())
            self.assertEqual(workbook_hash, hashlib.sha256(workbook.read_bytes()).hexdigest())
            saved = json.loads((shop / "product_financials.json").read_text())
            self.assertEqual({"product-a": payload}, saved["products"])

    def test_merge_reports_financial_restore_failure_and_verified_recovery_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = {"costs": {"product": {"amount": "1250000", "currency": "VND"}}}
            shop, workbook = self._merge_fixture(root, {"product-a": payload})
            real_write = _write_product_financials
            writes = 0

            def fail_rollback(shop_dir, shop_id, store):
                nonlocal writes
                writes += 1
                if writes == 2:
                    raise OSError("injected finance restore failure")
                return real_write(shop_dir, shop_id, store)

            with patch("etsy_catalog.build_unified_catalog", return_value=self._group()), \
                    patch("etsy_catalog._write_product_financials", side_effect=fail_rollback), \
                    patch("openpyxl.workbook.workbook.Workbook.save", side_effect=OSError("injected save failure")):
                result = merge_safe_duplicates(root, "daisyflowdigital", workbook, ["financial-identity"])
            self.assertEqual([], result["merged"])
            reason = result["skipped"][0]["reason"]
            self.assertIn("rollback tài chính thất bại", reason)
            self.assertIn("injected finance restore failure", reason)
            backup_path = Path(result["skipped"][0]["financial_backup"])
            self.assertTrue(backup_path.is_file())
            backup = json.loads(backup_path.read_text())
            self.assertEqual({"product-a": payload}, backup["products"])
            self.assertTrue((shop / "product-a").is_dir())
            self.assertTrue((shop / "product-b").is_dir())

    def test_renumber_remaps_financial_keys_simultaneously_and_backs_up_source_hash(self):
        fixture_helper = DaisyFolderRenumberTests()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = fixture_helper._fixture(Path(temporary))
            payloads = {
                "product-01": {"label": "one"},
                "product-03": {"label": "three"},
                "product-08": {"label": "eight"},
            }
            original = self._write_store(fixture["shop"], payloads)
            plan = fixture_helper._plan(fixture)
            store_path = str((fixture["shop"] / "product_financials.json").resolve())
            self.assertEqual(hashlib.sha256(original).hexdigest(), plan.source_hashes[store_path])
            backup, counts = apply_plan(plan, fixture["root"] / "backup", remote=fixture["remote"],
                                        runtime_checker=lambda _: None)
            current = json.loads((fixture["shop"] / "product_financials.json").read_text())
            self.assertEqual({"product-01": {"label": "one"}, "product-02": {"label": "three"},
                              "product-03": {"label": "eight"}}, current["products"])
            self.assertEqual(5, current["version"])
            self.assertEqual(hashlib.sha256(original).hexdigest(),
                             hashlib.sha256((backup / "product_financials.json").read_bytes()).hexdigest())
            manifest = json.loads((backup / "mapping_manifest.json").read_text())
            self.assertEqual(plan.source_hashes[store_path], manifest["source_hashes"][store_path])
            self.assertEqual(3, counts["financial_records"])

    def test_renumber_failure_after_store_write_restores_exact_original_store(self):
        fixture_helper = DaisyFolderRenumberTests()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = fixture_helper._fixture(Path(temporary))
            original = self._write_store(fixture["shop"], {
                "product-01": {"label": "one"}, "product-03": {"label": "three"},
                "product-08": {"label": "eight"},
            })
            plan = fixture_helper._plan(fixture)
            from scripts import daisy_folder_renumber
            real_update = daisy_folder_renumber._update_local_files

            def update_then_fail(active_plan):
                real_update(active_plan)
                raise RuntimeError("injected post-write failure")

            with patch("scripts.daisy_folder_renumber._update_local_files", side_effect=update_then_fail):
                with self.assertRaisesRegex(MigrationError, "automatic rollback completed"):
                    apply_plan(plan, fixture["root"] / "backup", remote=fixture["remote"],
                               runtime_checker=lambda _: None)
            self.assertEqual(original, (fixture["shop"] / "product_financials.json").read_bytes())
            self.assertTrue((fixture["shop"] / "product-03").is_dir())
            self.assertTrue((fixture["shop"] / "product-08").is_dir())

    def test_renumber_preflight_fails_closed_on_corrupt_store(self):
        fixture_helper = DaisyFolderRenumberTests()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = fixture_helper._fixture(Path(temporary))
            store = fixture["shop"] / "product_financials.json"
            store.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(MigrationError, "invalid financial store"):
                fixture_helper._plan(fixture)


if __name__ == "__main__":
    unittest.main()
