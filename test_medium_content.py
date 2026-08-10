#!/usr/bin/env python3
from __future__ import annotations

import unittest
import inspect
import re

import dashboard_app
import generate_social_posts
import social_auto_post
from medium_content import (
    make_medium_article_title,
    make_medium_research_article,
    render_medium_plain_text,
)


class TestMediumResearchArticle(unittest.TestCase):
    TITLE = "Nursing Student Planner Printable"
    DESC = "A nursing planner for lecture notes, clinical rotations, and weekly review."
    TAGS = "nursing planner, clinical rotation, study schedule"
    URL = "https://www.etsy.com/listing/123456789/nursing-planner"

    def test_article_is_research_shaped_and_contextual(self):
        article = make_medium_research_article(
            self.TITLE, self.DESC, self.TAGS, self.URL
        )

        for heading in (
            "## Abstract",
            "## Research Question",
            "## Practical Method: A Small Workflow",
            "## Product-Specific Tips",
            "## A Simple Way to Measure Whether It Helps",
            "## Limitations",
            "## Conclusion",
            "## Related Resource",
        ):
            self.assertIn(heading, article)
        self.assertIn("lecture", article.lower())
        self.assertIn("clinical", article.lower())
        self.assertIn("review", article.lower())
        self.assertNotIn("Get It Now", article)
        self.assertNotIn("instant digital download", article.lower())
        self.assertIn("no clinical or scientific study", article.lower())

    def test_article_uses_bounded_core_title_for_article_facing_text(self):
        full_title = (
            "Nursing School Planner 2027 | Nursing Student Clinical Schedule "
            "Study Organizer Printable PDF"
        )
        article_title = make_medium_article_title(
            full_title,
            "A planner for lecture review and clinical blocks.",
            "nursing planner, clinical schedule",
        )
        article = make_medium_research_article(
            full_title,
            "A planner for lecture review and clinical blocks.",
            "nursing planner, clinical schedule",
            "https://www.etsy.com/listing/123456789/nursing-planner",
        )

        self.assertIn("Nursing School Planner", article_title)
        self.assertNotIn("2027", article_title)
        self.assertNotIn(full_title, article_title)
        self.assertIn("“Nursing School Planner”", article)
        self.assertIn("[Nursing School Planner](<https://www.etsy.com/listing/123456789/nursing-planner>)", article)
        self.assertNotIn(full_title, article)
        self.assertNotIn("2027", article)

    def test_modern_aesthetic_description_is_not_misclassified_as_nursing(self):
        article = make_medium_research_article(
            "Modern Aesthetic Wall Art",
            "A modern aesthetic printable decor piece for a gallery wall.",
            "wall art, printable decor",
            "",
        )

        self.assertNotIn("nursing study and clinical planning", article)
        self.assertIn("wall-art and printable-decor planning", article)

    def test_description_context_requires_topic_overlap_with_title_or_tags(self):
        mismatched = make_medium_research_article(
            "Daily Planner",
            "Safety Plan 2027 is a separate emergency-planning document.",
            "daily planner, schedule",
            "",
        )
        matching = make_medium_research_article(
            "Daily Planner",
            "A daily planner for weekly tasks and review.",
            "daily planner, schedule",
            "",
        )

        self.assertNotIn("Safety Plan", mismatched)
        self.assertIn("No usable factual product context was supplied.", mismatched)
        self.assertIn("daily planner for weekly tasks", matching)

    def test_tracker_language_does_not_shadow_a_planner(self):
        article = make_medium_research_article(
            "Minimalist Daily Planner",
            "A simple way to track tasks and review the day.",
            "tracker, daily planning",
            "",
        )

        self.assertIn("personal planning and time-blocking", article)
        self.assertNotIn("spreadsheet and tracking workflow", article)

    def test_explicit_planner_signal_beats_journal_description(self):
        article = make_medium_research_article(
            "Daily Planner",
            "A digital daily journal for planning tasks and reviewing the day.",
            "planner, schedule",
            "",
        )

        self.assertIn("personal planning and time-blocking", article)
        self.assertNotIn("journal and workbook reflection", article)

    def test_printable_wall_art_does_not_shadow_to_checklist(self):
        article = make_medium_research_article(
            "Printable Wall Art",
            "Printable decor artwork for a gallery wall.",
            "checklist, home decor",
            "",
        )

        self.assertIn("wall-art and printable-decor planning", article)
        self.assertNotIn("printable checklist workflow", article)

    def test_rn_is_still_supported_as_a_standalone_nursing_token(self):
        article = make_medium_research_article(
            "RN Study Planner",
            "A planner for study review.",
            "student planning",
            "",
        )

        self.assertIn("nursing study and clinical planning", article)

    def test_plain_text_renderer_preserves_sections_without_markdown_syntax(self):
        markdown = (
            "# Article title\n\n"
            "## **Abstract**\n\n"
            "Use `one field`, [the Etsy resource](<https://www.etsy.com/listing/123456789/title>), "
            "and [the arbitrary resource](<https://example.com>).\n"
        )

        plain = render_medium_plain_text(markdown)

        self.assertEqual(
            plain,
            "Article title\n\nAbstract\n\n"
            "Use one field, the Etsy resource (https://www.etsy.com/listing/123456789/title), "
            "and the arbitrary resource.\n",
        )
        self.assertNotIn("#", plain)
        self.assertNotIn("**", plain)
        self.assertNotIn("https://example.com", plain)

    def test_url_is_only_a_restrained_final_resource(self):
        article = make_medium_research_article(
            self.TITLE, self.DESC, self.TAGS, self.URL
        )

        self.assertEqual(article.count(self.URL), 1)
        self.assertGreater(article.rfind(self.URL), article.rfind("## Related Resource"))
        self.assertTrue(article.rstrip().endswith(f"<{self.URL}>)."))

    def test_invalid_or_placeholder_etsy_urls_omit_the_cta(self):
        invalid_urls = (
            "",
            "https://www.etsy.com",
            "https://www.etsy.com/shop/YourShop",
            "https://www.etsy.com/listing/product-01",
            "https://www.etsy.com/listing/not-a-number/title",
            "http://www.etsy.com/listing/123456789/title",
        )
        for invalid_url in invalid_urls:
            with self.subTest(invalid_url=invalid_url):
                article = make_medium_research_article(
                    self.TITLE, self.DESC, self.TAGS, invalid_url
                )
                self.assertNotIn("## Related Resource", article)
                self.assertNotIn("etsy.com", article)

        valid_variant = "https://www.etsy.com/listing/123456789/nursing-planner?ref=research#details"
        article = make_medium_research_article(
            self.TITLE, self.DESC, self.TAGS, valid_variant
        )
        self.assertIn("## Related Resource", article)
        self.assertEqual(article.count(valid_variant), 1)

    def test_description_is_normalized_filtered_and_bounded(self):
        description = """
        <p>A nursing planner for lecture review &amp; clinical blocks.</p>
        [GOOGLE-OPTIMIZED SEO TITLE: CRITICAL INSTRUCTION]
        <p>CRITICAL: Buy now and get an instant digital download.</p>
        <p>A second planner sentence about weekly review.</p>
        <p>This long third sentence should not be copied into the article because
        the article only needs a short context note.</p>
        """

        article = make_medium_research_article(
            "Nursing Student Planner", description, "student planner", ""
        )

        context_match = re.search(
            r'The short factual context is: “([^”]*)”', article
        )
        self.assertIsNotNone(context_match)
        context = context_match.group(1)
        self.assertLessEqual(len(context), 320)
        self.assertLessEqual(len(re.findall(r"[.!?]", context)), 2)
        self.assertIn("lecture review & clinical blocks", context)
        self.assertIn("weekly review", context)
        self.assertNotIn("GOOGLE-OPTIMIZED", article)
        self.assertNotIn("CRITICAL", article)
        self.assertNotIn("Buy now", article)
        self.assertNotIn("instant digital download", article.lower())
        self.assertNotIn("This long third sentence", article)

    def test_topic_specific_workflows_are_derived_from_input(self):
        wedding = make_medium_research_article(
            "Wedding Budget Planner",
            "A simple wedding budget organizer.",
            "wedding budget, vendor payments",
            "https://www.etsy.com/shop/YourShop",
        )
        generic = make_medium_research_article(
            "Daily Planner",
            "A printable planner for everyday tasks.",
            "planner, time blocking",
            "https://www.etsy.com/shop/YourShop",
        )

        self.assertIn("vendor payments", wedding.lower())
        self.assertIn("categories", wedding.lower())
        self.assertIn("time blocks", generic.lower())
        self.assertIn("weekly review", generic.lower())

    def test_wedding_invitation_and_budget_are_distinct_archetypes(self):
        invitation = make_medium_research_article(
            "Wedding Stationery Template",
            "A wedding invitation card for ceremony details and guest mailing.",
            "editable template",
            "",
        )
        budget = make_medium_research_article(
            "Wedding Budget Spreadsheet",
            "Track vendor payments and budget categories.",
            "wedding planning",
            "",
        )

        self.assertIn("wedding invitation and card design", invitation)
        self.assertNotIn("vendor payments", invitation)
        self.assertIn("wedding budget and vendor planning", budget)
        self.assertIn("vendor payments", budget)

    def test_safe_non_planner_archetypes_and_neutral_fallback(self):
        cases = (
            (
                "Inventory Sheet",
                "A spreadsheet tracker for recurring stock entries.",
                "inventory",
                "spreadsheet and tracking workflow",
            ),
            (
                "Editable Canva Template",
                "A Canva template for editable text.",
                "design",
                "Canva and editable-template workflow",
            ),
            (
                "Cricut SVG Cut File",
                "An SVG cut file for a small craft project.",
                "craft",
                "SVG and cut-file preparation",
            ),
            (
                "Printable Wall Art",
                "Printable decor for a gallery wall.",
                "home decor",
                "wall-art and printable-decor planning",
            ),
            (
                "Guided Journal Workbook",
                "A workbook with reflection prompts.",
                "journal",
                "journal and workbook reflection",
            ),
            (
                "Daily Checklist",
                "A printable checklist for a repeatable routine.",
                "printable",
                "printable checklist workflow",
            ),
        )
        for title, desc, tags, expected_topic in cases:
            with self.subTest(title=title):
                self.assertIn(
                    expected_topic,
                    make_medium_research_article(title, desc, tags, ""),
                )

        fallback = make_medium_research_article(
            "A Useful Resource", "A short factual note.", "general", ""
        )
        self.assertIn("product-context workflow planning", fallback)
        self.assertIn("smallest actions", fallback)
        self.assertNotIn("vendor payments", fallback.lower())
        self.assertNotIn("clinical blocks", fallback.lower())


class TestMediumEntryPointContracts(unittest.IsolatedAsyncioTestCase):
    TITLE = "Nursing Student Planner Printable"
    DESC = "A nursing planner for lecture notes, clinical rotations, and weekly review."
    TAGS = "nursing planner, clinical rotation, study schedule"
    URL = "https://www.etsy.com/listing/123456789/nursing-planner"

    def test_legacy_wrappers_use_the_same_shared_output(self):
        expected = make_medium_research_article(
            self.TITLE, self.DESC, self.TAGS, self.URL
        )

        self.assertEqual(
            social_auto_post.make_medium_intro(
                self.TITLE, self.DESC, self.TAGS, self.URL
            ),
            expected,
        )
        self.assertEqual(
            generate_social_posts.make_medium_intro(
                self.TITLE, self.DESC, self.TAGS, self.URL
            ),
            expected,
        )

    def test_medium_title_and_heading_free_body_contract(self):
        article_title = make_medium_article_title(
            self.TITLE, self.DESC, self.TAGS
        )
        preview = social_auto_post.make_medium_intro(
            self.TITLE, self.DESC, self.TAGS, self.URL
        )
        poster_body = social_auto_post.make_medium_intro(
            self.TITLE,
            self.DESC,
            self.TAGS,
            self.URL,
            include_heading=False,
        )

        self.assertTrue(preview.startswith(f"# {article_title}\n"))
        self.assertTrue(poster_body.startswith("## Abstract\n"))
        self.assertNotIn(f"# {article_title}", poster_body)
        self.assertNotIn(self.TITLE, poster_body)
        self.assertIn("make_medium_article_title", inspect.getsource(social_auto_post.post_medium))
        self.assertIn("include_heading=False", inspect.getsource(social_auto_post.post_medium))

        resolve_source = inspect.getsource(social_auto_post._resolve_medium_editors)
        read_source = inspect.getsource(social_auto_post._read_medium_editor_text)
        post_source = inspect.getsource(social_auto_post.post_medium)
        self.assertIn("MEDIUM_BODY_SELECTORS", resolve_source)
        self.assertIn("count() != 2", resolve_source)
        self.assertIn("_locators_are_distinct", resolve_source)
        self.assertIn("inner_text", read_source)
        self.assertIn("text_content", read_source)
        self.assertIn("input_value", read_source)
        self.assertIn("render_medium_plain_text", post_source)
        self.assertIn("_read_medium_editor_text", post_source)
        self.assertLess(post_source.index("title_readback"), post_source.index("pub_menu"))

    async def test_dashboard_api_uses_the_same_medium_contract(self):
        product = {
            "title": self.TITLE,
            "description": self.DESC,
            "tags": self.TAGS,
            "folder": "nursing-planner",
            "etsy_url": self.URL,
        }
        expected = make_medium_research_article(
            self.TITLE, self.DESC, self.TAGS, self.URL
        )
        original_get_product = dashboard_app.get_product_by_row
        original_get_statuses = dashboard_app.get_product_social_statuses
        try:
            dashboard_app.get_product_by_row = lambda row: product
            dashboard_app.get_product_social_statuses = lambda *args: {}
            response = await dashboard_app.get_social_posts(7)
        finally:
            dashboard_app.get_product_by_row = original_get_product
            dashboard_app.get_product_social_statuses = original_get_statuses

        self.assertEqual(response["posts"]["medium"], expected)
        self.assertEqual(response["etsy_url"], self.URL)


if __name__ == "__main__":
    unittest.main()
