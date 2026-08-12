#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import dashboard_app


class TestSeoAssetContext(unittest.TestCase):
    def _product_folder(self, shop: Path) -> Path:
        product = shop / "product-389"
        (product / "images").mkdir(parents=True)
        (product / "files").mkdir()
        (product / "images" / "Wildflower-Svg-Bundle-Celestial-Svg-Graphics-1.png").write_bytes(b"image")
        with zipfile.ZipFile(product / "files" / "Wildflower-Svg-Bundle-Celestial-Svg-Graphics.zip", "w") as archive:
            archive.writestr("Wildflower bundle/10.svg", "<svg/>")
            archive.writestr("Wildflower bundle/4.eps", "eps")
        return product

    def test_asset_context_flags_stale_planner_title_for_svg_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            shop = Path(tmpdir)
            self._product_folder(shop)
            product = {
                "folder": "product-389",
                "seed_title": "Wildflower Svg Bundle Celestial Svg Graphics",
                "title": "Digital ADHD Planner for Adults | Goodnotes and iPad Planner",
            }
            with patch.object(dashboard_app, "SHOP_DIR", return_value=shop):
                context = dashboard_app._build_seo_asset_context(product)

        self.assertIn("Wildflower Svg Bundle Celestial Svg Graphics", context)
        self.assertIn("Wildflower-Svg-Bundle-Celestial-Svg-Graphics.zip", context)
        self.assertIn("Wildflower bundle/10.svg", context)
        self.assertIn("Treat the existing title as stale", context)

    def test_full_seo_prompt_prefers_assets_over_planner_title(self) -> None:
        captured = {}

        class FakeModels:
            def generate_content(self, *, model, contents, config):
                captured["prompt"] = contents
                text = (
                    "<etsy_title>Wildflower SVG Bundle | Celestial Flower Clipart | Cricut Cut Files</etsy_title>"
                    "<etsy_tags>wildflower svg, celestial svg, cricut files</etsy_tags>"
                    "<description>Wildflower SVG bundle for Cricut and Silhouette projects.</description>"
                )
                return types.SimpleNamespace(text=text)

        class FakeClient:
            models = FakeModels()

        fake_genai = types.SimpleNamespace(
            Client=lambda **kwargs: FakeClient(),
            types=types.SimpleNamespace(GenerateContentConfig=lambda **kwargs: kwargs),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            shop = Path(tmpdir)
            self._product_folder(shop)
            product = {
                "row": 65,
                "folder": "product-389",
                "seed_title": "Wildflower Svg Bundle Celestial Svg Graphics",
                "title": "Digital ADHD Planner for Adults | Goodnotes and iPad Planner",
                "keywords": "",
            }
            with patch.dict(sys.modules, {"google": types.SimpleNamespace(genai=fake_genai), "google.genai": fake_genai}), \
                patch.object(dashboard_app, "SHOP_DIR", return_value=shop), \
                patch.object(dashboard_app, "get_active_shop", return_value={}), \
                patch.object(dashboard_app, "save_to_excel", return_value=None):
                result = asyncio.run(dashboard_app._run_seo(product))

        prompt = captured["prompt"]
        self.assertIn("PRODUCT CONTEXT FROM LOCAL ASSETS", prompt)
        self.assertIn("Wildflower Svg Bundle Celestial Svg Graphics", prompt)
        self.assertIn("the product is a craft/design bundle, not a planner", prompt)
        self.assertIn("Do NOT mention planner, GoodNotes, iPad", prompt)
        self.assertEqual("Wildflower SVG Bundle | Celestial Flower Clipart | Cricut Cut Files", result["title"])


if __name__ == "__main__":
    unittest.main()
