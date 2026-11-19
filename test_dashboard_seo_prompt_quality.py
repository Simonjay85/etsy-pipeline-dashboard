"""Offline prompt contracts; never contact providers or edit shop workbooks."""

import unittest
from unittest import mock

import dashboard_app
from etsy_seo_prompts import (
    SEO_DESCRIPTION_PROMPT_TEMPLATE,
    SEO_TAG_PROMPT_RULES,
    SEO_TITLE_PROMPT_RULES,
)


TAGS = (
    "bedside cabinet, wood nightstand, bedroom storage, bedside storage, "
    "ash wood cabinet, small nightstand, modern nightstand, wooden bedside, "
    "night table, storage cabinet, bedroom cabinet, wood side table, bedside furniture"
)
TITLE = "Ash Wood Bedside Cabinet for Bedroom Storage"
DESCRIPTION = "Bedside cabinet with ash wood options for bedroom storage."
FULL_RESPONSE = (
    f"<etsy_title>{TITLE}</etsy_title><etsy_tags>{TAGS}</etsy_tags>"
    f"<description>{DESCRIPTION}</description>"
)


class SeoPromptRulesTests(unittest.TestCase):
    def test_title_prioritizes_readability_not_filling_limit(self):
        self.assertIn("no more than 140 characters", SEO_TITLE_PROMPT_RULES)
        self.assertIn("including all spaces, punctuation and symbols", SEO_TITLE_PROMPT_RULES)
        self.assertIn("Count only the title text before returning it", SEO_TITLE_PROMPT_RULES)
        self.assertIn("Never rely on downstream truncation", SEO_TITLE_PROMPT_RULES)
        self.assertIn("fewer than 15 words", SEO_TITLE_PROMPT_RULES)
        self.assertIn("Never pad", SEO_TITLE_PROMPT_RULES)
        self.assertNotIn("135-140", SEO_TITLE_PROMPT_RULES)

    def test_tags_cover_relevant_intent_with_hard_platform_limits(self):
        self.assertIn("exactly 13 unique", SEO_TAG_PROMPT_RULES)
        self.assertIn("20 characters or fewer, including spaces", SEO_TAG_PROMPT_RULES)
        self.assertIn("multi-word search phrases", SEO_TAG_PROMPT_RULES)
        self.assertIn("singular/plural duplicates", SEO_TAG_PROMPT_RULES)
        self.assertIn("Do not claim keyword research", SEO_TAG_PROMPT_RULES)

    def test_longer_description_target_remains_grounded_and_not_rank_promise(self):
        rules = SEO_DESCRIPTION_PROMPT_TEMPLATE
        self.assertIn("500-800 words", rules)
        self.assertIn("3,500-6,000 characters", rules)
        self.assertIn("soft editorial target, not a ranking rule", rules)
        self.assertIn("When evidence is sparse, write a shorter", rules)
        self.assertIn("Do not promise top 1", rules)
        self.assertNotIn("150-160 characters", rules)
        for section in ("PRODUCT DETAILS", "WHAT'S INCLUDED", "FAQ", "BEFORE ORDERING"):
            self.assertIn(section, rules)

    def test_physical_digital_and_policy_claims_are_conditional(self):
        rules = SEO_DESCRIPTION_PROMPT_TEMPLATE
        self.assertIn("physical versus digital only from the authoritative product evidence", rules)
        self.assertIn("For confirmed physical goods", rules)
        self.assertIn("For confirmed digital goods", rules)
        self.assertIn("Do not equate digital with instant delivery", rules)
        self.assertIn("Never manufacture shop policies", rules)
        self.assertIn("Exclude internal supplier prices", rules)
        self.assertIn("invent dimension units/conversions", rules)


class SeoPromptIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def _capture(self, field, *, keywords="", evidence="Physical ash wood bedside cabinet.", provider_response=None):
        response = provider_response or {
            "all": FULL_RESPONSE,
            "title": f"<etsy_title>{TITLE}</etsy_title>",
            "tags": f"<etsy_tags>{TAGS}</etsy_tags>",
            "description": f"<description>{DESCRIPTION}</description>",
        }[field]
        generate = mock.AsyncMock(return_value=response)
        product = {
            "row": 28,
            "folder": "product-25",
            "title": "Stale unrelated planner title",
            "keywords": keywords,
            "_shop_config": {"id": "test-shop", "etsy_link": "https://example.etsy.com"},
        }
        with mock.patch.object(
            dashboard_app, "_generate_bai_content_with_retry", new=generate,
        ), mock.patch.object(
            dashboard_app, "_build_seo_asset_context_details", return_value=(evidence, True),
        ) as assets, mock.patch.object(
            dashboard_app, "_load_seo_image_evidence", return_value=([], False),
        ) as images, mock.patch.object(
            dashboard_app, "save_to_excel",
        ) as save, mock.patch.object(dashboard_app, "broadcast") as log:
            result = await dashboard_app._run_seo(product, field=field)
        self.assertTrue(result)
        generate.assert_awaited_once()
        save.assert_called_once()
        if keywords:
            assets.assert_not_called()
            images.assert_not_called()
        else:
            assets.assert_called_once()
        return generate.call_args.args[0], result, save.call_args.args[1], log

    async def test_all_modes_use_shared_rules_once_and_preserve_requested_fields(self):
        for keyword in ("", "bedside cabinet, ash wood, bedroom storage"):
            for field in ("all", "title", "tags", "description"):
                with self.subTest(keyword_mode=bool(keyword), field=field):
                    prompt, _, saved, _ = await self._capture(field, keywords=keyword)
                    for rule_field, rules in (
                        ("title", SEO_TITLE_PROMPT_RULES),
                        ("tags", SEO_TAG_PROMPT_RULES),
                        ("description", SEO_DESCRIPTION_PROMPT_TEMPLATE),
                    ):
                        self.assertEqual(prompt.count(rules), int(field in ("all", rule_field)))
                        self.assertEqual(rule_field in saved, field in ("all", rule_field))
                    self.assertNotIn("Stale unrelated planner title", prompt)
                    self.assertNotIn("product-25", prompt)

    async def test_physical_furniture_has_no_unconditional_digital_boilerplate(self):
        for keyword in ("", "physical bedside cabinet, ash wood"):
            for field in ("all", "description"):
                with self.subTest(keyword=keyword, field=field):
                    prompt, _, _, _ = await self._capture(field, keywords=keyword)
                    for obsolete in (
                        "for a digital download product",
                        "Use only generic digital-download/footer language",
                        "⚡ INSTANT DOWNLOAD",
                        "• Digital product only — no physical item shipped",
                        "• Files available immediately after purchase",
                        "• Personal use of the digital product",
                        "• Do not resell, redistribute, or share the files",
                        "[Feature 1", "[Audience 1", "[Included item 1",
                    ):
                        self.assertNotIn(obsolete, prompt)

    async def test_digital_source_keeps_delivery_and_license_evidence_requirements(self):
        prompt, _, _, _ = await self._capture(
            "description", evidence="Digital made-to-order PDF planner; personal-use license.",
        )
        self.assertIn("Digital made-to-order PDF planner; personal-use license.", prompt)
        self.assertIn("For confirmed digital goods", prompt)
        self.assertIn("made-to-order digital products may not be instant", prompt)
        self.assertIn("only when explicitly supplied", prompt)

    async def test_sparse_evidence_can_return_short_copy_without_false_meta_warning(self):
        prompt, result, saved, log = await self._capture("description", keywords="bedside cabinet")
        self.assertIn("write a shorter useful description", prompt)
        self.assertEqual(result["description"], DESCRIPTION)
        self.assertEqual(saved["description"], DESCRIPTION)
        self.assertFalse(any("Meta opening" in str(call) for call in log.call_args_list))

    async def test_overlong_generated_titles_are_shortened_before_save_in_single_and_full_modes(self):
        raw_title = "Ash wood accent chair, black upholstered seat for a modern living room, reading corner, home office, dining space and contemporary interior styling with a natural wood frame"
        self.assertGreater(len(raw_title), 140)
        responses = {
            "title": f"<etsy_title>{raw_title}</etsy_title>",
            "all": (
                f"<etsy_title>{raw_title}</etsy_title><etsy_tags>{TAGS}</etsy_tags>"
                f"<description>{DESCRIPTION}</description>"
            ),
        }
        for field in ("title", "all"):
            with self.subTest(field=field):
                prompt, result, saved, _ = await self._capture(
                    field, keywords="ash wood accent chair", provider_response=responses[field],
                )
                self.assertIn("including all spaces, punctuation and symbols", prompt)
                self.assertLessEqual(len(result["title"]), 140)
                self.assertEqual(saved["title"], result["title"])

    def test_existing_output_contract_still_rejects_duplicate_or_long_tags(self):
        valid = dashboard_app._parse_full_seo_xml(FULL_RESPONSE)
        self.assertTrue(dashboard_app._full_seo_values_pass_validation(valid))
        for invalid_tags in (
            TAGS.replace("wood nightstand", "bedside cabinet"),
            TAGS.replace("wood nightstand", "this tag has more than twenty characters"),
            ", ".join(TAGS.split(", ")[:-1]),
        ):
            with self.subTest(tags=invalid_tags):
                self.assertFalse(dashboard_app._full_seo_values_pass_validation({
                    **valid, "etsy_tags": invalid_tags,
                }))


if __name__ == "__main__":
    unittest.main()
