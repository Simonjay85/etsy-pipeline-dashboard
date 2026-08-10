#!/usr/bin/env python3
"""Focused checks for normalized snapshot loading in dashboard_app."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dashboard_app


class TestDashboardLatestSnapshotNormalization(unittest.TestCase):
    def test_latest_etsy_manager_snapshot_calls_shared_normalizer(self) -> None:
        raw_snapshot = {
            "active": [{"id": "11", "title": "Active one"}],
            "draft": [{"id": "11", "title": "Active duplicate draft"}],
            "inactive": [{"id": "22", "title": "Inactive one"}],
            "expired": [{"id": "33", "title": "Expired one"}, {"id": "22", "title": "Inactive duplicate"}],
        }
        normalized = {
            "raw_counts": {
                "active": 1,
                "draft": 1,
                "inactive": 1,
                "expired": 2,
            },
            "counts": {
                "active": 1,
                "draft": 0,
                "inactive": 1,
                "expired": 1,
            },
            "duplicate_count": 2,
            "listings": [
                {"id": "11", "title": "Active one", "managerStatus": "active"},
                {"id": "22", "title": "Inactive one", "managerStatus": "inactive"},
                {"id": "33", "title": "Expired one", "managerStatus": "expired"},
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            scratch_dir = Path(tmpdir) / "scratch"
            scratch_dir.mkdir()
            (scratch_dir / "etsy_manager_current_shop_20260101_010101.json").write_text(
                json.dumps(raw_snapshot),
                encoding="utf-8",
            )

            original_base_dir = dashboard_app.BASE_DIR
            original_shop_id = dashboard_app._active_shop_id
            dashboard_app.BASE_DIR = Path(tmpdir)
            dashboard_app._active_shop_id = "shop"
            try:
                with mock.patch.object(
                    dashboard_app,
                    "normalize_etsy_manager_snapshot",
                    return_value=normalized,
                ) as normalize_call:
                    result = dashboard_app.latest_etsy_manager_snapshot()
                normalize_call.assert_called_once()
                # raw_counts should keep a compact diagnostic with total.
                self.assertEqual({**normalized["raw_counts"], "total": 5}, result["raw_counts"])
                self.assertEqual({**normalized["counts"], "total": 3}, result["counts"])
                self.assertEqual(normalized["duplicate_count"], result["duplicate_count"])
                self.assertEqual(normalized["listings"], result["listings"])
            finally:
                dashboard_app.BASE_DIR = original_base_dir
                dashboard_app._active_shop_id = original_shop_id


if __name__ == "__main__":
    unittest.main()
