#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import shops_config_validation as validator


class ShopsConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self.example_path = Path(__file__).resolve().parent / "shops_config.example.json"

    def test_example_config_schema_and_fake_values(self) -> None:
        """The committed example should pass strict schema validation without real data."""

        raw = json.loads(self.example_path.read_text(encoding="utf-8"))
        parsed = validator.validate_shops_config(raw)

        self.assertGreater(len(parsed), 0)
        for shop_id, payload in parsed.items():
            self.assertIn("id", payload)
            self.assertEqual(shop_id, payload["id"])
            self.assertNotIn("/Users/", payload["browser_session"])
            self.assertNotIn("/home/", payload["browser_session"])

            # Keep example values clearly synthetic.
            self.assertTrue(shop_id.startswith("sample-"))

            description = payload["shop_info"].lower()
            self.assertNotIn("temply", description)
            self.assertNotIn("daisy", description)

    def test_load_shops_config_requires_real_local_file(self) -> None:
        """Missing required local config should return a safe, actionable error."""

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "shops_config.json"
            with self.assertRaises(FileNotFoundError) as exc:
                validator.load_shops_config(config_path)

        message = str(exc.exception)
        self.assertIn("Missing required local config file", message)
        self.assertIn("shops_config.example.json", message)
        self.assertNotIn("{", message)

    def test_validation_errors_do_not_leak_config_values(self) -> None:
        """Error messages must expose field names, not raw config values."""

        secret_like = "shh-this-should-not-leak-12345"
        bad = {
            "sample-planner": {
                "id": "sample-planner",
                "name": 7,
                "emoji": "🎯",
                "etsy_link": "https://www.etsy.com/shop/sample-planner",
                "browser_session": "replace-with-browser-session-path",
                "social_links": "https://instagram.com/your-instagram-here",
                "shop_info": secret_like,
            }
        }

        with self.assertRaises(validator.ShopsConfigError) as exc:
            validator.validate_shops_config(bad)

        message = str(exc.exception)
        self.assertIn("field 'name'", message)
        self.assertNotIn(secret_like, message)

    def test_empty_social_links_and_shop_info_are_allowed(self) -> None:
        raw = {
            "shop3": {
                "id": "shop3",
                "name": "Legacy shop",
                "emoji": "🗂️",
                "etsy_link": "https://www.etsy.com/shop/shop3",
                "social_links": "",
                "shop_info": "",
                "legacy_note": "legacy metadata",
            }
        }

        parsed = validator.validate_shops_config(raw)
        self.assertEqual("", parsed["shop3"]["social_links"])
        self.assertEqual("", parsed["shop3"]["shop_info"])
        self.assertEqual("legacy metadata", parsed["shop3"]["legacy_note"])

    def test_empty_identity_fields_still_rejected(self) -> None:
        raw = {
            "bad-id": {
                "id": "bad-id",
                "name": "   ",
                "emoji": "🎯",
                "etsy_link": "https://www.etsy.com/shop/bad-id",
                "social_links": "",
                "shop_info": "",
            }
        }

        with self.assertRaises(validator.ShopsConfigError) as exc:
            validator.validate_shops_config(raw)

        self.assertIn("field 'name'", str(exc.exception))

    def test_optional_browser_session_defaults_to_empty(self) -> None:
        """Missing browser_session must remain compatible for inactive shops."""

        raw = {
            "shop3": {
                "id": "shop3",
                "name": "Inactive Shop",
                "emoji": "🗂️",
                "etsy_link": "https://www.etsy.com/shop/shop3",
                "social_links": "https://instagram.com/shop3",
                "shop_info": "Placeholder shop info for migration testing.",
                "legacy_note": "legacy runtime metadata",
            }
        }

        parsed = validator.validate_shops_config(raw)
        self.assertIn("shop3", parsed)
        self.assertIn("browser_session", parsed["shop3"])
        self.assertEqual("", parsed["shop3"]["browser_session"])
        self.assertEqual("legacy runtime metadata", parsed["shop3"]["legacy_note"])


if __name__ == "__main__":
    unittest.main()
