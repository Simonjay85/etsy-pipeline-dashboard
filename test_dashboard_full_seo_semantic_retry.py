import unittest
from unittest import mock

import dashboard_app


VALID_FULL_SEO = (
    "<etsy_title>Printable planner</etsy_title>"
    "<etsy_tags>planner, printable, organizer, checklist, calendar, journal, "
    "template, download, digital, planning, notes, schedule, worksheet</etsy_tags>"
    "<description>Evidence-supported finished product description.</description>"
)


def _product():
    return {
        "row": 6,
        "folder": "product-03",
        "title": "Existing title",
        "keywords": "",
        "_shop_config": {"id": "daisyflowdigital"},
    }


class FullSeoSemanticRetryTests(unittest.IsolatedAsyncioTestCase):
    def _patch_context(self):
        return mock.patch.multiple(
            dashboard_app,
            _build_seo_asset_context_details=mock.DEFAULT,
            _load_seo_image_evidence=mock.DEFAULT,
        )

    async def test_retries_parse_failure_with_same_evidence_and_prompt(self):
        image = {"name": "preview.png", "data": b"image", "mime_type": "image/png"}
        calls = []

        async def generate(prompt, image_evidence_items=None, deadline=None):
            calls.append((prompt, image_evidence_items, deadline))
            return "<etsy_title>partial</etsy_title>" if len(calls) == 1 else VALID_FULL_SEO

        with mock.patch.object(
            dashboard_app,
            "_build_seo_asset_context_details",
            return_value=("Readable text evidence: yes", True),
        ), mock.patch.object(
            dashboard_app,
            "_load_seo_image_evidence",
            return_value=([image], True),
        ), mock.patch.object(
            dashboard_app, "_generate_bai_content_with_retry", side_effect=generate
        ), mock.patch.object(dashboard_app, "save_to_excel") as save:
            result = await dashboard_app._run_seo(_product(), field="all")

        self.assertEqual(set(result), {"title", "tags", "description"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertEqual(calls[0][2], calls[1][2])
        self.assertIn("CORRECTIVE RETRY", calls[1][0])
        self.assertIn("Readable text evidence: yes", calls[1][0])
        save.assert_called_once()

    async def test_retries_validation_failure_and_saves_only_second_response(self):
        invalid = VALID_FULL_SEO.replace("worksheet", "planner")
        calls = []

        async def generate(prompt, image_evidence_items=None, deadline=None):
            calls.append(prompt)
            return invalid if len(calls) == 1 else VALID_FULL_SEO

        with mock.patch.object(
            dashboard_app,
            "_build_seo_asset_context_details",
            return_value=("Readable text evidence: yes", True),
        ), mock.patch.object(
            dashboard_app, "_load_seo_image_evidence", return_value=([], False)
        ), mock.patch.object(
            dashboard_app, "_generate_bai_content_with_retry", side_effect=generate
        ), mock.patch.object(dashboard_app, "save_to_excel") as save:
            result = await dashboard_app._run_seo(_product(), field="all")

        self.assertEqual(set(result), {"title", "tags", "description"})
        self.assertEqual(len(calls), 2)
        self.assertIn("exactly 13 distinct", calls[1])
        save.assert_called_once()

    async def test_second_invalid_response_does_not_trigger_third_call_or_save(self):
        calls = []

        async def generate(prompt, image_evidence_items=None, deadline=None):
            calls.append(prompt)
            return "<etsy_title>partial</etsy_title>"

        with mock.patch.object(
            dashboard_app,
            "_build_seo_asset_context_details",
            return_value=("Readable text evidence: yes", True),
        ), mock.patch.object(
            dashboard_app, "_load_seo_image_evidence", return_value=([], False)
        ), mock.patch.object(
            dashboard_app, "_generate_bai_content_with_retry", side_effect=generate
        ), mock.patch.object(dashboard_app, "save_to_excel") as save:
            result = await dashboard_app._run_seo(_product(), field="all")

        self.assertEqual(result, {})
        self.assertEqual(len(calls), 2)
        save.assert_not_called()

    async def test_exhausted_deadline_skips_semantic_retry(self):
        generate = mock.AsyncMock(return_value="<etsy_title>partial</etsy_title>")
        error_out = {}

        with mock.patch.object(
            dashboard_app,
            "_build_seo_asset_context_details",
            return_value=("Readable text evidence: yes", True),
        ), mock.patch.object(
            dashboard_app, "_load_seo_image_evidence", return_value=([], False)
        ), mock.patch.object(
            dashboard_app, "_generate_bai_content_with_retry", new=generate
        ), mock.patch.object(dashboard_app, "save_to_excel") as save, mock.patch.object(
            dashboard_app.time, "monotonic", return_value=20
        ):
            result = await dashboard_app._run_seo(
                _product(), field="all", deadline=10, error_out=error_out
            )

        self.assertEqual(result, {})
        generate.assert_not_called()
        save.assert_not_called()
        self.assertEqual(error_out["error_code"], "SEO_PROVIDER_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
