import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from openpyxl import Workbook, load_workbook

from scripts.temply_folder_renumber import (
    MigrationError,
    apply_plan,
    build_plan,
    make_mapping,
    rollback_folder_renames,
    rollback_backup,
    transform_map_data,
    transform_social_data,
)


class TemplyFolderRenumberTests(unittest.TestCase):
    def test_mapping_is_numeric_and_collision_safe(self):
        rows = ((4, "product-560"), (5, "product-05"), (6, "product-414"))
        mappings = make_mapping(("product-05", "product-414", "product-560"), rows, token="testtoken")
        self.assertEqual([item.new_name for item in mappings], ["product-01", "product-02", "product-03"])
        self.assertEqual([item.temporary_name for item in mappings], [
            ".temply-folder-renumber-testtoken-001",
            ".temply-folder-renumber-testtoken-002",
            ".temply-folder-renumber-testtoken-003",
        ])
        self.assertTrue(all(item.temporary_name.startswith(".") for item in mappings))
        self.assertEqual(mappings[1].excel_rows, (6,))

    def test_map_update_is_scoped_to_temply_current_references(self):
        data = {
            "catalog": {
                "daisyflowdigital": "product-560",
                "templystudios": "product-560",
            },
            "sync:templystudios/product-560": {"daisyflowdigital": "product-999"},
            "sync:templystudios/product-999": {"templystudios": "product-999"},
            "sync:templystudios/product-07": {"daisyflowdigital": "product-07"},
            "historical": "product-560",
        }
        transformed, key_count, value_count = transform_map_data(
            data, {"product-560": "product-01", "product-07": "product-02"}, shop_id="templystudios"
        )
        self.assertEqual(key_count, 0)
        self.assertEqual(value_count, 1)
        self.assertEqual(transformed["catalog"]["daisyflowdigital"], "product-560")
        self.assertEqual(transformed["catalog"]["templystudios"], "product-01")
        self.assertEqual(transformed["sync:templystudios/product-560"]["daisyflowdigital"], "product-999")
        self.assertEqual(transformed["sync:templystudios/product-07"]["daisyflowdigital"], "product-07")
        self.assertIn("sync:templystudios/product-999", transformed)
        self.assertEqual(transformed["historical"], "product-560")

    def test_social_update_changes_product_keys_and_folder_only(self):
        data = {
            "products": {
                "product-414": {"folder": "product-414", "row": 111, "channels": {"pinterest": {"status": "posted"}}},
                "product-999": {"folder": "product-414"},
            },
            "report": "product-414",
        }
        transformed, key_count, folder_count = transform_social_data(
            data, {"product-414": "product-02"}, path=Path("social_posts.json")
        )
        self.assertEqual((key_count, folder_count), (1, 1))
        self.assertIn("product-02", transformed["products"])
        self.assertEqual(transformed["products"]["product-02"]["row"], 111)
        self.assertEqual(transformed["products"]["product-999"]["folder"], "product-414")
        self.assertEqual(transformed["report"], "product-414")

    def test_social_preserves_stale_key_and_ignores_row(self):
        data = {
            "products": {
                "product-414": {"folder": "product-414", "row": 111, "channels": {"pinterest": {"status": "posted"}}},
                "product-07": {"folder": "product-07", "row": 10},
            }
        }
        transformed, key_count, folder_count = transform_social_data(
            data,
            {"product-639": "product-01", "product-07": "product-02"},
            path=Path("social_post_status.json"),
        )
        self.assertEqual((key_count, folder_count), (1, 1))
        self.assertIn("product-414", transformed["products"])
        self.assertNotIn("product-639", transformed["products"])
        self.assertEqual(transformed["products"]["product-02"]["folder"], "product-02")

    def test_build_plan_preserves_historical_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shop = root / "templystudios"
            shop.mkdir()
            excel = shop / "Etsy_SEO_Generator.xlsx"
            self._write_workbook(excel, ["product-02", "product-07"])
            map_file = root / "product_source_map.json"
            map_file.write_text("{}\n", encoding="utf-8")
            manifest = shop / "2027_PRODUCT_MANIFEST.md"
            original_manifest = "historical: product-07\n"
            manifest.write_text(original_manifest, encoding="utf-8")
            (shop / "product-02").mkdir()
            (shop / "product-07").mkdir()
            (shop / "social_post_status.json").write_text(self._social_json(), encoding="utf-8")
            (shop / "social_posts.json").write_text(self._social_json(), encoding="utf-8")
            plan = build_plan(
                shop_dir=shop,
                excel_path=excel,
                map_path=map_file,
                expected_count=None,
                token="manifest-test",
            )
            self.assertEqual(plan.shop_manifest_updates, 0)
            self.assertEqual(plan.updated_shop_manifest, original_manifest)

    def test_rollback_handles_overlapping_old_and_new_names(self):
        with tempfile.TemporaryDirectory() as temp:
            shop = Path(temp) / "templystudios"
            shop.mkdir()
            for name in ("product-02", "product-03"):
                folder = shop / name
                folder.mkdir()
                (folder / "asset.txt").write_text(name, encoding="utf-8")
            mappings = make_mapping(
                ("product-02", "product-03"),
                ((1, "product-02"), (2, "product-03")),
                token="overlap-test",
            )
            plan = SimpleNamespace(shop_dir=shop, mappings=mappings)
            journal_path = shop / "journal.json"
            journal = {
                "status": "completed",
                "phase1_completed": [item.old_name for item in mappings],
                "phase2_completed": [item.old_name for item in mappings],
            }
            for item in mappings:
                (shop / item.old_name).rename(shop / item.temporary_name)
            for item in mappings:
                (shop / item.temporary_name).rename(shop / item.new_name)
            rollback_folder_renames(plan, journal_path, journal)
            self.assertEqual(
                {path.name for path in shop.iterdir() if path.is_dir()},
                {"product-02", "product-03"},
            )
            self.assertFalse(any(path.name.startswith(".temply-folder-renumber-") for path in shop.iterdir()))

    def test_apply_and_exact_rollback_on_fixture(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shop = root / "templystudios"
            shop.mkdir()
            excel = shop / "Etsy_SEO_Generator.xlsx"
            self._write_workbook(excel, ["product-02", "product-03"])
            map_file = root / "product_source_map.json"
            map_file.write_text(
                json.dumps(
                    {
                        "current": {"templystudios": "product-03"},
                        "sync:templystudios/product-03": {"historical": True},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            original_manifest = "historical: product-03\n"
            (shop / "2027_PRODUCT_MANIFEST.md").write_text(original_manifest, encoding="utf-8")
            social = {
                "version": 1,
                "shop_id": "templystudios",
                "products": {
                    "product-03": {"folder": "product-03", "row": 2},
                    "product-414": {"folder": "product-414", "row": 111},
                },
            }
            for name in ("social_post_status.json", "social_posts.json"):
                (shop / name).write_text(json.dumps(social) + "\n", encoding="utf-8")
            for name in ("product-02", "product-03"):
                folder = shop / name
                folder.mkdir()
                (folder / "asset.bin").write_bytes(name.encode("ascii"))

            backup_root = root / "backups"
            plan = build_plan(
                shop_dir=shop,
                excel_path=excel,
                map_path=map_file,
                expected_count=None,
                token="apply-test",
            )
            backup_dir, counts = apply_plan(plan, backup_root)
            self.assertEqual(counts["catalog_folders"], 2)
            self.assertEqual(
                {path.name for path in shop.iterdir() if path.is_dir()},
                {"product-01", "product-02"},
            )
            self.assertEqual(
                json.loads(map_file.read_text(encoding="utf-8"))["current"]["templystudios"],
                "product-02",
            )
            self.assertEqual(
                json.loads((shop / "social_posts.json").read_text(encoding="utf-8"))["products"]["product-414"]["folder"],
                "product-414",
            )
            self.assertEqual((shop / "2027_PRODUCT_MANIFEST.md").read_text(encoding="utf-8"), original_manifest)

            result = rollback_backup(backup_dir)
            self.assertEqual(result["status"], "rolled_back")
            self.assertEqual(
                {path.name for path in shop.iterdir() if path.is_dir()},
                {"product-02", "product-03"},
            )
            self.assertEqual((shop / "2027_PRODUCT_MANIFEST.md").read_text(encoding="utf-8"), original_manifest)

    def test_build_plan_rejects_missing_and_extra_current_folders(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shop = root / "templystudios"
            shop.mkdir()
            excel = shop / "Etsy_SEO_Generator.xlsx"
            self._write_workbook(excel, ["product-02", "product-01"])
            map_file = root / "product_source_map.json"
            map_file.write_text("{}\n", encoding="utf-8")
            (shop / "product-01").mkdir()
            (shop / "product-03").mkdir()
            (shop / "social_post_status.json").write_text(self._social_json(), encoding="utf-8")
            (shop / "social_posts.json").write_text(self._social_json(), encoding="utf-8")
            with self.assertRaises(MigrationError) as raised:
                build_plan(shop_dir=shop, excel_path=excel, map_path=map_file, expected_count=None)
            self.assertIn("one-to-one validation failed", str(raised.exception))

    @staticmethod
    def _write_workbook(path: Path, folders: list[str]) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Listings"
        sheet.append(["title", "other"])
        sheet.append(["STT", "Folder\n(product-01...)"])
        for index, folder in enumerate(folders, start=1):
            sheet.append([index, folder])
        workbook.save(path)

    @staticmethod
    def _social_json() -> str:
        return json.dumps({"version": 1, "shop_id": "templystudios", "products": {}}, indent=2) + "\n"


if __name__ == "__main__":
    unittest.main()
