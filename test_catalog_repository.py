#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp
from unittest import TestCase
from unittest.mock import patch

import openpyxl

import catalog_repository as repository


class TestCatalogRepository(TestCase):
    def setUp(self) -> None:
        self.temp_root = Path(mkdtemp())
        self.shop_id = "daisyflowdigital"
        self.workbook = (
            self.temp_root / "shops" / self.shop_id / repository._DEFAULT_WORKBOOK
        )
        self._seed_workbook(self.workbook)

    def tearDown(self) -> None:
        rmtree(self.temp_root, ignore_errors=True)

    def _seed_workbook(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        book = openpyxl.Workbook()
        try:
            sheet = book.active
            sheet.title = "Listings"
            sheet["A1"] = "original"
            book.save(path)
        finally:
            book.close()

    def _read_cell(self, path: Path, cell: str = "A1") -> str:
        book = openpyxl.load_workbook(path)
        try:
            return book["Listings"][cell].value
        finally:
            book.close()

    def test_successful_write_updates_workbook(self) -> None:
        before_version = repository._catalog_version(self.workbook)
        before_hash = repository._catalog_hash(self.workbook)

        receipt = repository.apply_catalog_update(
            self.temp_root,
            self.shop_id,
            "set-title-cell",
            lambda book: book["Listings"].__setitem__("A1", "updated"),
            expected_version=before_version,
            expected_hash=before_hash,
        )

        self.assertTrue(receipt["success"])
        self.assertEqual(self.shop_id, receipt["shop_id"])
        self.assertEqual("set-title-cell", receipt["operation"])
        self.assertEqual(str(self.workbook.resolve()), receipt["workbook_path"])
        self.assertEqual(self._read_cell(self.workbook), "updated")
        self.assertNotEqual(before_version, receipt["after_version"])
        self.assertNotEqual(before_hash, receipt["after_hash"])
        self.assertIsNone(receipt["error"])
        self.assertGreaterEqual(receipt["duration_ms"], 0)

    def test_stale_hash_rejection_prevents_write_and_preserves_original(self) -> None:
        original_cell = self._read_cell(self.workbook)
        original_hash = repository._catalog_hash(self.workbook)

        with self.assertRaises(repository.CatalogWriteConflict):
            repository.apply_catalog_update(
                self.temp_root,
                self.shop_id,
                "set-title-cell",
                lambda book: book["Listings"].__setitem__("A1", "changed"),
                expected_hash="stale-hash",
            )

        self.assertEqual(original_cell, self._read_cell(self.workbook))
        self.assertEqual(original_hash, repository._catalog_hash(self.workbook))
        backups = list(self.workbook.parent.glob(".Etsy_SEO_Generator.catalog_backup_*.xlsx"))
        self.assertEqual([], backups)

    def test_temp_validation_failure_rolls_back_and_keeps_original(self) -> None:
        original_cell = self._read_cell(self.workbook)
        original_hash = repository._catalog_hash(self.workbook)

        with patch.object(repository, "_validate_workbook") as validate_workbook:
            validate_workbook.side_effect = RuntimeError("simulated validation failure")
            with self.assertRaises(repository.CatalogWriteError):
                repository.apply_catalog_update(
                    self.temp_root,
                    self.shop_id,
                    "simulate-bad-save",
                    lambda book: book["Listings"].__setitem__("A1", "changed"),
                )

        self.assertEqual(original_cell, self._read_cell(self.workbook))
        self.assertEqual(original_hash, repository._catalog_hash(self.workbook))
        backups = list(self.workbook.parent.glob(".Etsy_SEO_Generator.catalog_backup_*.xlsx"))
        self.assertEqual(1, len(backups))
        self.assertTrue(backups[0].is_file())
        self.assertEqual(original_hash, repository._catalog_hash(backups[0]))

    def test_lock_path_is_per_shop_identity(self) -> None:
        lock_one = repository.shop_lock_path(self.temp_root, self.shop_id)
        same_shop = repository.shop_lock_path(self.temp_root, self.shop_id)
        other_shop = repository.shop_lock_path(self.temp_root, "other-shop")

        self.assertEqual(lock_one, same_shop)
        self.assertNotEqual(lock_one, other_shop)
        self.assertTrue(lock_one.name.endswith(".catalog_workbook.lock"))
        self.assertIn(self.shop_id, str(lock_one))

    def test_receipt_contains_safe_fields(self) -> None:
        receipt = repository.apply_catalog_update(
            self.temp_root,
            self.shop_id,
            "safe-receipt",
            lambda book: book["Listings"].__setitem__("A1", "verified"),
        )

        expected_fields = {
            "shop_id",
            "operation",
            "workbook_path",
            "backup_path",
            "lock_path",
            "started_at_utc",
            "finished_at_utc",
            "duration_ms",
            "before_version",
            "after_version",
            "before_hash",
            "after_hash",
            "success",
            "error",
        }
        self.assertEqual(expected_fields, set(receipt.keys()))
        self.assertFalse(receipt["success"] is False)
        self.assertIsNone(receipt["error"])
        self.assertIn("catalog_workbook", receipt["lock_path"])
