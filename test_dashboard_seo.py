import sys
import types
import unittest
import tempfile
import zipfile
import json
import os
from pathlib import Path
from unittest import mock

import dashboard_app


class _Response:
    text = "<etsy_title>Planner</etsy_title>"


class _FullSeoResponse:
    text = (
        "<etsy_title>Printable wedding planner | PDF organizer | digital download | planning pages</etsy_title>"
        "<etsy_tags>wedding planner, printable planner, pdf organizer, digital download, planning pages, bride planner, ceremony plan, wedding checklist, event planner, printable pdf, wedding binder, planning kit, bridal organizer</etsy_tags>"
        "<description>Wedding planner PDF organizer for printable digital planning pages."
        " Product Details: local asset content only."
        "</description>"
    )


class _FakeVertexClientError(Exception):
    def __init__(self, code: int, status: str):
        super().__init__(f"mock vertex error {status} ({code})")
        self.code = code
        self.status = status


def _pdf_bytes(*pages: str) -> bytes:
    import fitz

    document = fitz.open()
    document.set_metadata({"title": "Wedding planner source", "keywords": "ceremony planning"})
    for page_text in pages:
        page = document.new_page()
        page.insert_text((72, 72), page_text)
    raw = document.tobytes()
    document.close()
    return raw


class SeoAssetContextTests(unittest.TestCase):
    def test_direct_pdf_context_contains_bounded_text_markers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            shop_dir = Path(temp_dir) / "shop"
            files_dir = shop_dir / "product-01" / "files"
            files_dir.mkdir(parents=True)
            pdf_path = files_dir / "wedding-planner.pdf"
            pdf_path.write_bytes(_pdf_bytes(
                "Wedding planner ceremony timeline and guest checklist",
                "Reception seating plan and vendor notes",
                "Budget worksheet for wedding planning",
            ))

            context = dashboard_app._build_seo_asset_context({
                "folder": "product-01",
                "keywords": "wedding planner, printable ceremony organizer",
                "_shop_dir": shop_dir,
            })

        self.assertIn("Readable text evidence: yes", context)
        self.assertNotIn("Target SEO keywords from workbook column C", context)
        self.assertIn("PDF pages=3", context)
        self.assertIn("ceremony timeline", context)
        self.assertIn("metadata=", context)

    def test_zip_context_reads_nested_pdf_and_text_but_skips_unsafe_members(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            shop_dir = Path(temp_dir) / "shop"
            files_dir = shop_dir / "product-02" / "files"
            files_dir.mkdir(parents=True)
            zip_path = files_dir / "bundle.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("docs/readme.md", "Printable ceremony organizer with vendor checklist")
                archive.writestr("docs/source.pdf", _pdf_bytes("Reception seating chart template"))
                archive.writestr("../unsafe.txt", "must not appear")
                archive.writestr("__MACOSX/._bundle", "must not appear")
                archive.writestr(".hidden.txt", "must not appear")
                archive.writestr("docs/", "")

            context = dashboard_app._build_seo_asset_context({
                "folder": "product-02",
                "keywords": "ceremony organizer",
                "_shop_dir": shop_dir,
            })

        self.assertIn("docs/readme.md", context)
        self.assertIn("ceremony organizer", context)
        self.assertIn("Reception seating chart template", context)
        self.assertNotIn("unsafe.txt", context)
        self.assertNotIn("must not appear", context)
        self.assertLessEqual(len(context), dashboard_app._SEO_CONTEXT_MAX_CHARS + 80)

    def test_zip_content_budget_skips_oversized_member(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = Path(temp_dir) / "oversized.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("large.txt", "x" * (dashboard_app._SEO_ZIP_TEXT_MEMBER_MAX_BYTES + 1))
            summaries = dashboard_app._seo_zip_content_summaries(zip_path)

        self.assertTrue(summaries)
        self.assertIn("bounded member-size limit", summaries[0])

    def test_readable_flag_requires_payload_in_final_context_with_long_headers(self):
        payload_marker = "ACTUAL_ZIP_PAYLOAD_MARKER"
        with tempfile.TemporaryDirectory() as temp_dir:
            shop_dir = Path(temp_dir) / "shop"
            files_dir = shop_dir / "product-04" / "files"
            files_dir.mkdir(parents=True)
            zip_path = files_dir / "bundle.zip"
            long_member_name = "header-" + ("h" * 9000) + ".txt"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr(long_member_name, payload_marker)

            context, readable = dashboard_app._build_seo_asset_context_details({
                "folder": "product-04",
                "seed_title": "SEED-" + ("s" * 9000),
                "title": "TITLE-" + ("t" * 9000),
                "keywords": "payload keyword",
                "_shop_dir": shop_dir,
            })

        self.assertTrue(readable)
        self.assertIn("Readable text evidence: yes", context)
        self.assertIn("bounded text sample:", context)
        self.assertIn(payload_marker, context)
        self.assertNotIn("SEED-", context)
        self.assertNotIn("TITLE-", context)
        self.assertIn("Prohibited topic metadata:", context)

    def test_symlinked_asset_is_rejected_from_product_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            shop_dir = root / "shop"
            product_dir = shop_dir / "product-03"
            files_dir = product_dir / "files"
            files_dir.mkdir(parents=True)
            outside = root / "outside.pdf"
            outside.write_bytes(_pdf_bytes("secret outside product evidence"))
            try:
                os.symlink(outside, files_dir / "leaked.pdf")
            except OSError:
                self.skipTest("symlink creation is unavailable in this environment")

            context = dashboard_app._build_seo_asset_context({
                "folder": "product-03",
                "keywords": "planner",
                "_shop_dir": shop_dir,
            })

        self.assertNotIn("leaked.pdf", context)
        self.assertNotIn("secret outside product evidence", context)
        self.assertIn("Readable text evidence: no", context)

    def test_full_xml_parser_accepts_one_outer_code_fence_only(self):
        fenced = """```xml
<etsy_title>Digital planner &amp; template</etsy_title>
<etsy_tags>digital planner</etsy_tags>
<description>Printable planner &amp; editable template.</description>
```"""

        parsed = dashboard_app._parse_full_seo_xml(fenced)

        self.assertEqual(parsed["etsy_title"], "Digital planner & template")
        self.assertEqual(parsed["description"], "Printable planner & editable template.")
        self.assertIsNone(dashboard_app._parse_full_seo_xml("note\n" + fenced))

    def test_full_xml_parser_accepts_bare_ampersands_and_preserves_terms_ampersand(self):
        content = (
            "<etsy_title>TERMS & CONDITIONS for templates</etsy_title>"
            "<etsy_tags>planner,template,digital download,planner template,checklist,organizer,pdf,printable,notion,planner kit,customization,ecommerce,sales,download</etsy_tags>"
            "<description>Works with TEAM & GUEST workflows and supports copy/paste usage.</description>"
        )
        parsed = dashboard_app._parse_full_seo_xml(content)

        self.assertEqual(parsed["etsy_title"], "TERMS & CONDITIONS for templates")
        self.assertEqual(parsed["description"], "Works with TEAM & GUEST workflows and supports copy/paste usage.")

    def test_full_xml_parser_preserves_existing_entities_without_double_escaping(self):
        content = (
            "<etsy_title>Terms &amp; conditions</etsy_title>"
            "<etsy_tags>planner,template,digital&amp;printable,download,guide,checklist,pdf,editable,journal,planner</etsy_tags>"
            "<description>Use &lt; and &gt; safely with &#x3C;proof&#62; and &#60;strong&#62; examples.</description>"
        )
        parsed = dashboard_app._parse_full_seo_xml(content)

        self.assertEqual(parsed["etsy_title"], "Terms & conditions")
        self.assertEqual(parsed["etsy_tags"], "planner,template,digital&printable,download,guide,checklist,pdf,editable,journal,planner")
        self.assertEqual(parsed["description"], "Use < and > safely with <proof> and <strong> examples.")

    def test_full_xml_parser_rejects_malformed_or_surrounding_content(self):
        partial = "<etsy_title>Planner</etsy_title><etsy_tags>planner,template,digital,printable,organizer,checklist,pdf,download,design,ecommerce,notebook,planner binder,customizable</etsy_tags>"
        self.assertIsNone(dashboard_app._parse_full_seo_xml(partial))
        self.assertIsNone(dashboard_app._parse_full_seo_xml(
            "<description>Complete but starts with description</description>" + partial
        ))
        self.assertIsNone(dashboard_app._parse_full_seo_xml(
            "prefix <etsy_title>Planner</etsy_title><etsy_tags>planner,template,digital,printable,organizer,checklist,pdf,download,design,ecommerce,notebook,planner binder,customizable</etsy_tags><description>Text</description>"
        ))
        self.assertIsNone(dashboard_app._parse_full_seo_xml(
            "<etsy_title>Planner</etsy_title><etsy_tags>planner,template,digital,printable,organizer,checklist,pdf,download,design,ecommerce,notebook,planner binder,customizable</etsy_tags><description>Text</description> suffix"
        ))

    def test_full_xml_parser_rejects_wrong_order(self):
        wrong_order = (
            "<etsy_tags>planner,template,digital,printable,organizer,checklist,pdf,download,design,ecommerce,notebook,planner binder,customizable</etsy_tags>"
            "<etsy_title>Planner</etsy_title>"
            "<description>Text</description>"
        )
        self.assertIsNone(dashboard_app._parse_full_seo_xml(wrong_order))


class SeoPromptTests(unittest.IsolatedAsyncioTestCase):
    def test_keyword_prompts_share_current_title_tag_and_full_description_rules(self):
        common = {
            "keyword_context": "undated digital planner",
            "etsy_link": "https://example.etsy.com",
            "social_links": "",
        }

        title_prompt = dashboard_app._build_keyword_only_seo_prompt(field="title", **common)
        tags_prompt = dashboard_app._build_keyword_only_seo_prompt(field="tags", **common)
        description_prompt = dashboard_app._build_keyword_only_seo_prompt(field="description", **common)
        full_prompt = dashboard_app._build_keyword_only_seo_prompt(field="all", **common)

        self.assertIn("preferably 10-15 words", title_prompt)
        self.assertIn("Avoid keyword stuffing", title_prompt)
        self.assertNotIn("135 and 140", title_prompt)
        self.assertNotIn("5-7 segments", title_prompt)
        self.assertIn("exactly 13 unique", tags_prompt)
        self.assertIn("20 characters or fewer", tags_prompt)
        for prompt in (description_prompt, full_prompt):
            self.assertIn("2,500-4,500 characters", prompt)
            self.assertIn("150-160 characters", prompt)
            self.assertIn('"📄 PRODUCT DETAILS"', prompt)
            self.assertIn('"📦 WHAT\'S INCLUDED"', prompt)
            self.assertIn('"❓ FAQ"', prompt)
            self.assertIn('"📜 TERMS & CONDITIONS"', prompt)

    def test_keyword_only_full_prompt_requires_xml_escaping_of_special_chars(self):
        prompt = dashboard_app._build_keyword_only_seo_prompt(
            field="all",
            keyword_context="planner",
            etsy_link="https://example.etsy.com",
            social_links="",
        )
        self.assertIn("XML must be valid and well-formed", prompt)
        self.assertIn("Escape &, <, > inside field text as &amp;, &lt;, &gt;", prompt)
        self.assertIn("no raw &, <, > appear in the generated values", prompt)

    async def test_asset_single_description_prompt_contains_full_description_template(self):
        captured = {}

        class Models:
            def generate_content(self, **kwargs):
                captured["contents"] = kwargs["contents"]
                return types.SimpleNamespace(text="<description>Evidence-supported digital product overview for buyers seeking a clear and useful download with documented details and practical guidance included here.\n\n📄 PRODUCT DETAILS\nEvidence only.</description>")

        fake_genai = types.SimpleNamespace(
            Client=lambda **kwargs: types.SimpleNamespace(models=Models()),
            types=types.SimpleNamespace(
                GenerateContentConfig=lambda **kwargs: kwargs,
                HttpOptions=lambda **kwargs: kwargs,
            ),
        )
        fake_google = types.ModuleType("google")
        fake_google.genai = fake_genai
        product = {
            "row": 6,
            "folder": "product-03",
            "title": "Ignored title",
            "keywords": "",
            "_shop_config": {"id": "daisyflowdigital"},
        }
        with mock.patch.dict(sys.modules, {"google": fake_google}), mock.patch.object(
            dashboard_app,
            "_build_seo_asset_context_details",
            return_value=("Readable text evidence: yes\nbounded text sample: useful digital product", True),
        ), mock.patch.object(
            dashboard_app, "_load_seo_image_evidence", return_value=([], False)
        ), mock.patch.object(dashboard_app, "save_to_excel"):
            await dashboard_app._run_seo(product, field="description")

        prompt = captured["contents"][0]
        self.assertIn("2,500-4,500 characters", prompt)
        self.assertIn("150-160 characters", prompt)
        self.assertIn('"✨ KEY FEATURES"', prompt)
        self.assertIn('"🖥️ COMPATIBILITY"', prompt)

    async def test_single_tags_rejects_incomplete_response_without_saving(self):
        class Models:
            def generate_content(self, **kwargs):
                return types.SimpleNamespace(text="<etsy_tags>planner, printable planner</etsy_tags>")

        fake_genai = types.SimpleNamespace(
            Client=lambda **kwargs: types.SimpleNamespace(models=Models()),
            types=types.SimpleNamespace(
                GenerateContentConfig=lambda **kwargs: kwargs,
                HttpOptions=lambda **kwargs: kwargs,
            ),
        )
        fake_google = types.ModuleType("google")
        fake_google.genai = fake_genai
        product = {
            "row": 6,
            "folder": "product-03",
            "title": "Ignored title",
            "keywords": "planner",
            "_shop_config": {"id": "daisyflowdigital"},
        }
        error_out = {}
        with mock.patch.dict(sys.modules, {"google": fake_google}), mock.patch.object(
            dashboard_app, "save_to_excel"
        ) as save_to_excel:
            result = await dashboard_app._run_seo(product, field="tags", error_out=error_out)

        self.assertEqual(result, {})
        self.assertEqual(error_out["error_code"], "SEO_PROVIDER_INVALID_RESPONSE")
        save_to_excel.assert_not_called()

    async def test_keyword_mode_uses_keyword_only_source_and_no_asset_loader(self):
        captured = {}

        class Models:
            def generate_content(self, **kwargs):
                captured["prompt"] = kwargs["contents"]
                return _FullSeoResponse()

        class _FakeHttpOptions:
            def __init__(self, **kwargs):
                pass

        fake_genai = types.SimpleNamespace(
            Client=lambda **kwargs: types.SimpleNamespace(models=Models()),
            types=types.SimpleNamespace(
                GenerateContentConfig=lambda **kwargs: kwargs,
                HttpOptions=_FakeHttpOptions,
            ),
        )
        fake_google = types.ModuleType("google")
        fake_google.genai = fake_genai
        product = {
            "row": 6,
            "folder": "product-03",
            "title": "Old title",
            "keywords": "wedding planner, ceremony organizer",
            "_shop_config": {"id": "daisyflowdigital"},
        }

        with mock.patch.dict(sys.modules, {"google": fake_google}), \
                mock.patch.object(dashboard_app, "_build_seo_asset_context_details", side_effect=AssertionError("keyword mode must not read asset context")), \
                mock.patch.object(dashboard_app, "_load_seo_image_evidence", side_effect=AssertionError("keyword mode must not load images")), \
                mock.patch.object(dashboard_app, "save_to_excel") as save_to_excel:
            result = await dashboard_app._run_seo(product, field="all")

        prompt = captured["prompt"]
        self.assertIsInstance(prompt, str)
        self.assertEqual(set(result), {"title", "tags", "description"})
        self.assertIn("sole product-topic source of truth", prompt)
        self.assertIn("wedding planner, ceremony organizer", prompt)
        self.assertIn("No local asset context was loaded or attached", prompt)
        self.assertNotIn("Old title", prompt)
        self.assertNotIn("ceremony timeline", prompt)
        save_to_excel.assert_called_once()
        updates = save_to_excel.call_args.args[1]
        self.assertTrue({"title", "tags", "description"}.issubset(updates))

    async def test_full_keyword_mode_normalizes_bare_ampersands_and_saves_only_after_validation(self):
        full_response = (
            "<etsy_title>TERMS & CONDITIONS Template Set</etsy_title>"
            "<etsy_tags>planner,template,digital,printable,organizer,checklist,pdf,download,design,ecommerce,journal,binding,brand</etsy_tags>"
            "<description>This template includes TERMS & CONDITIONS notes and works with TEAM & GUEST plans.</description>"
        )

        class Models:
            def generate_content(self, **kwargs):
                return types.SimpleNamespace(text=full_response)

        fake_genai = types.SimpleNamespace(
            Client=lambda **kwargs: types.SimpleNamespace(models=Models()),
            types=types.SimpleNamespace(
                GenerateContentConfig=lambda **kwargs: kwargs,
                HttpOptions=lambda **kwargs: kwargs,
            ),
        )
        fake_google = types.ModuleType("google")
        fake_google.genai = fake_genai
        product = {
            "row": 6,
            "folder": "product-03",
            "title": "Old title",
            "keywords": "planner, template",
            "_shop_config": {"id": "daisyflowdigital"},
        }

        with mock.patch.dict(sys.modules, {"google": fake_google}), \
                mock.patch.object(
                    dashboard_app,
                    "_build_seo_asset_context_details",
                    side_effect=AssertionError("keyword mode must not read asset context"),
                ), \
                mock.patch.object(dashboard_app, "_load_seo_image_evidence", side_effect=AssertionError("keyword mode must not load images")), \
                mock.patch.object(dashboard_app, "save_to_excel") as save_to_excel:
            result = await dashboard_app._run_seo(product, field="all")

        self.assertEqual(result["title"], "TERMS & CONDITIONS Template Set")
        self.assertIn("This template includes TERMS & CONDITIONS notes and works with TEAM & GUEST plans.", result["description"])
        self.assertIn("TERMS &", result["title"])
        self.assertIn("TERMS &", result["description"])
        self.assertEqual(len(result["tags"].split(",")), 13)
        self.assertNotIn("[", result["tags"])
        self.assertNotIn("]", result["tags"])
        self.assertEqual(save_to_excel.call_count, 1)
        updates = save_to_excel.call_args.args[1]
        self.assertEqual(updates["title"], "TERMS & CONDITIONS Template Set")
        self.assertEqual(
            updates["description"],
            "planner — This template includes TERMS & CONDITIONS notes and works with TEAM & GUEST plans.",
        )
        self.assertEqual(len(updates["tags"].split(",")), 13)

    async def test_asset_mode_attaches_image_evidence_without_file_text(self):
        captured = {}

        class _FakePart:
            @classmethod
            def from_bytes(cls, *, data, mime_type):
                return {"kind": "image", "data": data, "mime_type": mime_type}

        class Models:
            def generate_content(self, **kwargs):
                captured["contents"] = kwargs["contents"]
                return _FullSeoResponse()

        fake_genai = types.SimpleNamespace(
            Client=lambda **kwargs: types.SimpleNamespace(models=Models()),
            types=types.SimpleNamespace(
                GenerateContentConfig=lambda **kwargs: kwargs,
                HttpOptions=lambda **kwargs: kwargs,
                Part=_FakePart,
            ),
        )
        fake_google = types.ModuleType("google")
        fake_google.genai = fake_genai
        product = {
            "row": 6,
            "folder": "product-03",
            "title": "Ignored existing title",
            "keywords": "",
            "_shop_config": {"id": "daisyflowdigital"},
        }
        image = {"name": "preview.png", "data": b"image-bytes", "mime_type": "image/png"}

        with mock.patch.dict(sys.modules, {"google": fake_google}), \
                mock.patch.object(
                    dashboard_app,
                    "_build_seo_asset_context_details",
                    return_value=("Readable text evidence: no", False),
                ), \
                mock.patch.object(dashboard_app, "_load_seo_image_evidence", return_value=([image], True)), \
                mock.patch.object(dashboard_app, "save_to_excel") as save_to_excel:
            result = await dashboard_app._run_seo(product, field="all")

        contents = captured["contents"]
        self.assertIsInstance(contents, list)
        self.assertEqual(contents[1]["kind"], "image")
        self.assertEqual(contents[1]["mime_type"], "image/png")
        self.assertEqual(contents[1]["data"], b"image-bytes")
        self.assertIn("attached local product images only", contents[0])
        self.assertIn("no readable file text was found", contents[0])
        self.assertNotIn("both bounded extracted file text and attached", contents[0])
        self.assertIn("No user keyword is provided", contents[0])
        self.assertNotIn("supported primary keyword", contents[0].lower())
        self.assertEqual(set(result), {"title", "tags", "description"})
        save_to_excel.assert_called_once()

    async def test_asset_mode_text_only_prompt_uses_file_text_only(self):
        captured = {}

        class Models:
            def generate_content(self, **kwargs):
                captured["contents"] = kwargs["contents"]
                return _FullSeoResponse()

        fake_genai = types.SimpleNamespace(
            Client=lambda **kwargs: types.SimpleNamespace(models=Models()),
            types=types.SimpleNamespace(
                GenerateContentConfig=lambda **kwargs: kwargs,
                HttpOptions=lambda **kwargs: kwargs,
            ),
        )
        fake_google = types.ModuleType("google")
        fake_google.genai = fake_genai
        product = {
            "row": 6,
            "folder": "product-03",
            "title": "Ignored existing title",
            "keywords": "",
            "_shop_config": {"id": "daisyflowdigital"},
        }
        with mock.patch.dict(sys.modules, {"google": fake_google}), \
                mock.patch.object(
                    dashboard_app,
                    "_build_seo_asset_context_details",
                    return_value=("Readable text evidence: yes\nbounded text sample: factual file phrase", True),
                ), \
                mock.patch.object(dashboard_app, "_load_seo_image_evidence", return_value=([], False)), \
                mock.patch.object(dashboard_app, "save_to_excel"):
            await dashboard_app._run_seo(product, field="all")

        contents = captured["contents"]
        self.assertIsInstance(contents, list)
        self.assertEqual(len(contents), 1)
        self.assertIn("bounded extracted file text only", contents[0])
        self.assertIn("do not invent visual details", contents[0])
        self.assertNotIn("both bounded extracted file text and attached", contents[0])
        self.assertNotIn("attached local product images only", contents[0])
        self.assertNotIn("primary keyword", contents[0].lower())

    async def test_full_generation_fails_closed_without_readable_asset_evidence(self):
        product = {
            "row": 6,
            "folder": "product-03",
            "title": "Old title",
            "keywords": "",
            "_shop_config": {"id": "daisyflowdigital"},
        }
        error_out = {}
        with mock.patch.object(
            dashboard_app,
            "_build_seo_asset_context_details",
            return_value=("Readable text evidence: no", False),
        ), mock.patch.object(
            dashboard_app,
            "_load_seo_image_evidence",
            return_value=([], False),
        ), mock.patch.object(
            dashboard_app,
            "_generate_vertex_content_with_retry",
            new=mock.AsyncMock(side_effect=AssertionError("provider must not be called")),
        ), mock.patch.object(dashboard_app, "save_to_excel") as save_to_excel:
            result = await dashboard_app._run_seo(product, field="all", error_out=error_out)

        self.assertEqual(result, {})
        self.assertEqual(error_out["error_code"], "SEO_INSUFFICIENT_ASSET_EVIDENCE")
        self.assertEqual(error_out["http_status"], 422)
        save_to_excel.assert_not_called()

    async def test_full_generation_rejects_partial_xml_response(self):
        class Models:
            def generate_content(self, **kwargs):
                return types.SimpleNamespace(
                    text="<etsy_title>Planner</etsy_title><etsy_tags>planner</etsy_tags>"
                )

        fake_genai = types.SimpleNamespace(
            Client=lambda **kwargs: types.SimpleNamespace(models=Models()),
            types=types.SimpleNamespace(
                GenerateContentConfig=lambda **kwargs: kwargs,
                HttpOptions=lambda **kwargs: kwargs,
            ),
        )
        fake_google = types.ModuleType("google")
        fake_google.genai = fake_genai
        product = {
            "row": 6,
            "folder": "product-03",
            "title": "Old title",
            "keywords": "",
            "_shop_config": {"id": "daisyflowdigital"},
        }
        error_out = {}
        with mock.patch.dict(sys.modules, {"google": fake_google}), mock.patch.object(
            dashboard_app,
            "_build_seo_asset_context_details",
            return_value=("Readable text evidence: yes\nbounded text sample: planner pages", True),
        ), mock.patch.object(
            dashboard_app,
            "_load_seo_image_evidence",
            return_value=([], False),
        ), mock.patch.object(dashboard_app, "save_to_excel") as save_to_excel:
            result = await dashboard_app._run_seo(product, field="all", error_out=error_out)

        self.assertEqual(result, {})
        self.assertEqual(error_out["error_code"], "SEO_PROVIDER_INVALID_RESPONSE")
        save_to_excel.assert_not_called()


class SeoRouteContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_regen_route_preserves_column_c_when_form_keywords_empty(self):
        product = {
            "row": 6,
            "folder": "product-03",
            "title": "Existing title",
            "keywords": "column c primary keyword",
        }
        captured = {}

        async def fake_run(product_arg, *args, **kwargs):
            captured["product"] = product_arg
            return {"title": "t", "tags": "a", "description": "d"}

        async def receive():
            return {
                "type": "http.request",
                "body": json.dumps({"keywords": "", "title": "Existing title"}).encode(),
                "more_body": False,
            }

        request = dashboard_app.Request(
            {"type": "http", "method": "POST", "path": "/api/products/6/regen-seo"},
            receive,
        )
        with mock.patch.object(dashboard_app, "SHOP_DIR", return_value=Path("/tmp/seo-shop")), \
                mock.patch.object(dashboard_app, "EXCEL_FILE", return_value=Path("/tmp/seo.xlsx")), \
                mock.patch.object(dashboard_app, "get_active_shop", return_value={"id": "daisyflowdigital"}), \
                mock.patch.object(dashboard_app, "get_product_by_row", return_value=product), \
                mock.patch.object(dashboard_app, "_run_seo", side_effect=fake_run):
            result = await dashboard_app.regen_seo(6, request)

        self.assertTrue(result["ok"])
        self.assertEqual(captured["product"]["keywords"], "column c primary keyword")
        self.assertEqual(captured["product"]["_excel_file"], Path("/tmp/seo.xlsx"))


class VertexRetryTests(unittest.IsolatedAsyncioTestCase):
    def test_production_budget_allows_full_seo_with_bounded_retry_window(self):
        self.assertEqual(dashboard_app._VERTEX_GENERATE_TIMEOUT_SECONDS, 180.0)
        self.assertEqual(dashboard_app._VERTEX_GENERATE_BUDGET_SECONDS, 190.0)
        self.assertEqual(dashboard_app._VERTEX_GENERATE_MAX_ATTEMPTS, 2)

    async def test_timeout_error_is_not_retried(self):
        calls = []

        class Models:
            def generate_content(self, **kwargs):
                calls.append(kwargs)
                raise TimeoutError("temporary timeout")

        client = types.SimpleNamespace(models=Models())
        with mock.patch.object(dashboard_app.asyncio, "sleep", new=mock.AsyncMock()) as sleep:
            with self.assertRaises(TimeoutError):
                await dashboard_app._generate_vertex_content_with_retry(
                    client, "prompt", object()
                )

        self.assertEqual(len(calls), 1)
        sleep.assert_not_awaited()

    async def test_timeout_is_not_retried_and_excel_is_not_saved(self):
        calls = []

        class Models:
            def generate_content(self, **kwargs):
                calls.append(kwargs)
                raise TimeoutError("temporary timeout")

        class _FakeHttpOptions:
            def __init__(self, **kwargs):
                pass

        class _Client:
            def __init__(self, **kwargs):
                self.models = Models()

        fake_genai = types.SimpleNamespace(
            Client=lambda **kwargs: _Client(**kwargs),
            types=types.SimpleNamespace(
                GenerateContentConfig=lambda **kwargs: kwargs,
                HttpOptions=_FakeHttpOptions,
            ),
        )
        fake_google = types.ModuleType("google")
        fake_google.genai = fake_genai
        error_out = {}
        product = {
            "row": 6,
            "folder": "product-03",
            "title": "Product 03",
            "keywords": "planner",
            "_shop_config": {"id": "daisyflowdigital"},
        }

        with mock.patch.dict(sys.modules, {"google": fake_google}), \
                mock.patch.object(dashboard_app, "_build_seo_asset_context_details", return_value=("PDF", False)), \
                mock.patch.object(dashboard_app, "save_to_excel") as save_to_excel:
            result = await dashboard_app._run_seo(
                product,
                field="title",
                error_out=error_out,
            )

        self.assertEqual(result, {})
        self.assertEqual(len(calls), 1)
        self.assertIn("không phản hồi trong", error_out["error"])
        save_to_excel.assert_not_called()

    async def test_client_uses_http_timeout_setting(self):
        captured = {}

        class Models:
            def generate_content(self, **kwargs):
                return _Response()

        class _FakeHttpOptions:
            def __init__(self, **kwargs):
                captured["http_options"] = kwargs

        class _Client:
            def __init__(self, **kwargs):
                captured["client_kwargs"] = kwargs
                self.models = Models()

        fake_genai = types.SimpleNamespace(
            Client=lambda **kwargs: _Client(**kwargs),
            types=types.SimpleNamespace(
                GenerateContentConfig=lambda **kwargs: kwargs,
                HttpOptions=_FakeHttpOptions,
            ),
        )
        fake_google = types.ModuleType("google")
        fake_google.genai = fake_genai
        product = {
            "row": 6,
            "folder": "product-03",
            "title": "Product 03",
            "keywords": "planner",
            "_shop_config": {"id": "daisyflowdigital"},
        }

        with mock.patch.dict(sys.modules, {"google": fake_google}), \
                mock.patch.object(dashboard_app, "_build_seo_asset_context_details", return_value=("PDF", False)), \
                mock.patch.object(dashboard_app, "save_to_excel") as save_to_excel:
            result = await dashboard_app._run_seo(product, field="title")

        self.assertIsInstance(result, dict)
        self.assertEqual(
            captured["http_options"].get("timeout"),
            int(dashboard_app._VERTEX_GENERATE_TIMEOUT_SECONDS * 1000),
        )
        self.assertIn("http_options", captured["client_kwargs"])
        save_to_excel.assert_called_once()

        options_object = captured["client_kwargs"]["http_options"]
        if hasattr(options_object, "timeout"):
            self.assertEqual(
                options_object.timeout,
                int(dashboard_app._VERTEX_GENERATE_TIMEOUT_SECONDS * 1000),
            )
        else:
            self.assertEqual(
                captured["http_options"]["timeout"],
                int(dashboard_app._VERTEX_GENERATE_TIMEOUT_SECONDS * 1000),
            )

    async def test_client_error_429_retries_then_succeeds(self):
        calls = []

        class Models:
            def generate_content(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    raise _FakeVertexClientError(429, "RESOURCE_EXHAUSTED")
                return _Response()

        client = types.SimpleNamespace(models=Models())
        with mock.patch.object(dashboard_app.asyncio, "sleep", new=mock.AsyncMock()) as sleep, \
                mock.patch.object(dashboard_app, "broadcast", new=mock.Mock()) as broadcast:
            result = await dashboard_app._generate_vertex_content_with_retry(
                client, "prompt", object()
            )

        self.assertIsInstance(result, _Response)
        self.assertEqual(len(calls), 2)
        sleep.assert_awaited_once_with(dashboard_app._VERTEX_RETRY_BACKOFF_SECONDS[0])
        broadcast.assert_called_once()
        warning_text = broadcast.call_args.args[0]
        self.assertIn("⚠️ Vertex tạm hết capacity (429); thử lại sau", warning_text)
        self.assertIn("lần 2/2", warning_text)

    async def test_client_error_503_unavailable_retries_then_succeeds(self):
        calls = []

        class Models:
            def generate_content(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    raise _FakeVertexClientError(503, "UNAVAILABLE")
                return _Response()

        client = types.SimpleNamespace(models=Models())
        with mock.patch.object(dashboard_app.asyncio, "sleep", new=mock.AsyncMock()) as sleep, \
                mock.patch.object(dashboard_app, "broadcast", new=mock.Mock()):
            result = await dashboard_app._generate_vertex_content_with_retry(
                client, "prompt", object()
            )

        self.assertIsInstance(result, _Response)
        self.assertEqual(len(calls), 2)
        sleep.assert_awaited_once_with(dashboard_app._VERTEX_RETRY_BACKOFF_SECONDS[0])

    async def test_client_error_400_is_not_retried(self):
        calls = []

        class Models:
            def generate_content(self, **kwargs):
                calls.append(kwargs)
                raise _FakeVertexClientError(400, "INVALID_ARGUMENT")

        client = types.SimpleNamespace(models=Models())
        with mock.patch.object(dashboard_app.asyncio, "sleep", new=mock.AsyncMock()) as sleep:
            with self.assertRaises(_FakeVertexClientError):
                await dashboard_app._generate_vertex_content_with_retry(
                    client, "prompt", object()
                )

        self.assertEqual(len(calls), 1)
        sleep.assert_not_awaited()

    async def test_non_transient_failure_is_not_retried_and_detail_is_exposed(self):
        calls = []

        class Models:
            def generate_content(self, **kwargs):
                calls.append(kwargs)
                raise ValueError("request configuration rejected")

        class _FakeHttpOptions:
            def __init__(self, **kwargs):
                pass

        fake_genai = types.SimpleNamespace(
            Client=lambda **kwargs: types.SimpleNamespace(models=Models()),
            types=types.SimpleNamespace(
                GenerateContentConfig=lambda **kwargs: kwargs,
                HttpOptions=_FakeHttpOptions,
            ),
        )
        fake_google = types.ModuleType("google")
        fake_google.genai = fake_genai
        error_out = {}
        product = {
            "row": 6,
            "folder": "product-03",
            "title": "Product 03",
            "keywords": "planner",
            "_shop_config": {"id": "daisyflowdigital"},
        }

        with mock.patch.dict(sys.modules, {"google": fake_google}), \
                mock.patch.object(dashboard_app, "_build_seo_asset_context_details", return_value=("PDF", False)), \
                mock.patch.object(dashboard_app, "save_to_excel") as save_to_excel:
            result = await dashboard_app._run_seo(
                product,
                field="title",
                error_out=error_out,
            )

        self.assertEqual(result, {})
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            error_out["error"],
            "Vertex AI lỗi: request configuration rejected",
        )
        save_to_excel.assert_not_called()

    async def test_429_retries_to_retry_limit_and_re_raises_original_error(self):
        self.assertEqual((1.0,), dashboard_app._VERTEX_RETRY_BACKOFF_SECONDS)

        calls = []

        class Models:
            def generate_content(self, **kwargs):
                calls.append(kwargs)
                raise _FakeVertexClientError(429, "RESOURCE_EXHAUSTED")

        client = types.SimpleNamespace(models=Models())
        with mock.patch.object(dashboard_app.asyncio, "sleep", new=mock.AsyncMock()) as sleep, \
                mock.patch.object(dashboard_app, "broadcast", new=mock.Mock()) as broadcast:
            with self.assertRaises(_FakeVertexClientError):
                await dashboard_app._generate_vertex_content_with_retry(
                    client, "prompt", object()
                )

        self.assertEqual(2, len(calls))
        sleep.assert_awaited_once_with(1.0)
        self.assertEqual(1, broadcast.call_count)


if __name__ == "__main__":
    unittest.main()
