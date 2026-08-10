#!/usr/bin/env python3
from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import shops_config_validation


PROJECT_ROOT = Path(__file__).resolve().parent


class DashboardConfigValidationIntegrationTests(unittest.TestCase):
    def _default_loader_config(self) -> dict:
        return {
            "templystudios": {
                "id": "templystudios",
                "name": "Temply Studios",
                "emoji": "🧵",
                "etsy_link": "https://www.etsy.com/shop/templystudios",
                "social_links": "https://www.instagram.com/templystudios",
                "shop_info": "Placeholder info.",
            }
        }

    def _load_dashboard_app(self):
        if "dashboard_app" in sys.modules:
            del sys.modules["dashboard_app"]

        real_loader = shops_config_validation.load_shops_config

        def _fallback_loader(config_path: str | Path = shops_config_validation.DEFAULT_CONFIG_NAME, **kwargs):
            resolved = Path(config_path)
            if not resolved.is_absolute():
                cfg_base = Path(kwargs.get("base_dir") or Path.cwd())
                resolved = (cfg_base / resolved).resolve()
            if resolved == (PROJECT_ROOT / shops_config_validation.DEFAULT_CONFIG_NAME).resolve():
                return self._default_loader_config()
            return real_loader(config_path, **kwargs)

        with patch("shops_config_validation.load_shops_config", side_effect=_fallback_loader):
            return importlib.import_module("dashboard_app")

    def _write_config(self, root: str | Path, payload: dict) -> Path:
        config_path = Path(root) / "shops_config.json"
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        return config_path

    def test_load_shops_uses_validation_error_on_missing_file(self) -> None:
        dashboard_app = self._load_dashboard_app()
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(RuntimeError) as exc:
                dashboard_app.load_shops(Path(tmpdir) / "shops_config.json")
        message = str(exc.exception)
        self.assertIn("Missing required local config file", message)
        self.assertIn("Copy shops_config.example.json", message)
        self.assertNotIn("/tmp", message)

    def test_load_shops_does_not_leak_config_values(self) -> None:
        dashboard_app = self._load_dashboard_app()
        secret_like = "super-secret-id-9a8b7c6d5"
        raw = {
            "sample-planner": {
                "id": "sample-planner",
                "name": {"value": secret_like},
                "emoji": "📘",
                "etsy_link": "https://www.etsy.com/shop/sample-planner",
                "social_links": "https://instagram.com/sample-planner",
                "shop_info": "Placeholder info.",
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_config(tmpdir, raw)
            with self.assertRaises(RuntimeError) as exc:
                dashboard_app.load_shops(path)
        self.assertIn("Malformed local shop config", str(exc.exception))
        self.assertIn("field 'name'", str(exc.exception))
        self.assertNotIn(secret_like, str(exc.exception))

    def test_load_shops_normalizes_missing_browser_session_and_preserves_extra(self) -> None:
        dashboard_app = self._load_dashboard_app()
        raw = {
            "shop3": {
                "id": "shop3",
                "name": "Inactive shop",
                "emoji": "🧩",
                "etsy_link": "https://www.etsy.com/shop/shop3",
                "social_links": "https://instagram.com/shop3",
                "shop_info": "Inactive shop metadata.",
                "legacy_shop_tag": "legacy-field",
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_config(tmpdir, raw)
            parsed = dashboard_app.load_shops(path)
        self.assertIn("shop3", parsed)
        self.assertEqual("", parsed["shop3"]["browser_session"])
        self.assertIn("legacy_shop_tag", parsed["shop3"])
        self.assertEqual("legacy-field", parsed["shop3"]["legacy_shop_tag"])


if __name__ == "__main__":
    unittest.main()
