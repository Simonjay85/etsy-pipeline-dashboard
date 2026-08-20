#!/usr/bin/env python3
"""Focused tests for read-only Etsy Manager link decoration."""

from __future__ import annotations

import unittest

from dashboard_app import enrich_products_with_etsy_manager


class TestDashboardEtsyManagerLinks(unittest.TestCase):
    def test_exact_non_active_match_exposes_manager_link_without_changing_excel_url(self) -> None:
        product = {"folder": "product-03", "etsy_url": "https://www.etsy.com/listing/123"}
        snapshot = {
            "freshness": {"snapshotAt": "2026-07-23T23:45:14", "source": "snapshot.json", "stale": False},
            "listings": [{
                "id": "123",
                "managerStatus": "expired",
                "url": "https://www.etsy.com/listing/123",
                "editUrl": "https://www.etsy.com/your/shops/me/listing-editor/edit/123",
            }],
        }

        enriched = enrich_products_with_etsy_manager([product], snapshot)[0]

        self.assertEqual(product["etsy_url"], "https://www.etsy.com/listing/123")
        self.assertEqual(enriched["etsy_manager_status"], "expired")
        self.assertEqual(enriched["etsy_edit_url"], "https://www.etsy.com/your/shops/me/listing-editor/edit/123")
        self.assertEqual(enriched["etsy_manage_url"], enriched["etsy_edit_url"])
        self.assertEqual(enriched["etsy_link_type"], "manager")

    def test_active_match_exposes_public_link_and_not_manager_link(self) -> None:
        enriched = enrich_products_with_etsy_manager(
            [{"etsy_url": "https://www.etsy.com/listing/456"}],
            {"listings": [{
                "id": "456",
                "managerStatus": "active",
                "url": "https://www.etsy.com/listing/456",
                "editUrl": "https://www.etsy.com/your/shops/me/listing-editor/edit/456",
            }]},
        )[0]

        self.assertEqual(enriched["etsy_manager_status"], "active")
        self.assertEqual(enriched["etsy_public_url"], "https://www.etsy.com/listing/456")
        self.assertEqual(enriched["etsy_edit_url"], "https://www.etsy.com/your/shops/me/listing-editor/edit/456")
        self.assertEqual(enriched["etsy_link_type"], "public")
        self.assertIsNone(enriched["etsy_link_warning_reason"])

    def test_active_match_accepts_locale_prefix_listing_url(self) -> None:
        enriched = enrich_products_with_etsy_manager(
            [{"etsy_url": "https://www.etsy.com/ca/listing/9999999999/some-listing-name"}],
            {"listings": [{
                "id": "9999999999",
                "managerStatus": "active",
                "url": "https://www.etsy.com/ca/listing/9999999999/some-listing-name",
                "editUrl": "https://www.etsy.com/your/shops/me/listing-editor/edit/9999999999",
            }]},
        )[0]

        self.assertEqual(enriched["etsy_listing_id"], "9999999999")
        self.assertEqual(enriched["etsy_manager_status"], "active")
        self.assertEqual(enriched["etsy_public_url"], "https://www.etsy.com/ca/listing/9999999999/some-listing-name")
        self.assertEqual(enriched["etsy_link_type"], "public")
        self.assertIsNone(enriched["etsy_link_warning_reason"])

    def test_missing_listing_with_stale_snapshot_uses_manager_fallback_for_valid_draft(self) -> None:
        enriched = enrich_products_with_etsy_manager(
            [{"etsy_url": "https://www.etsy.com/listing/4554394648", "status": "✅ Đã đăng draft"}],
            {
                "freshness": {"snapshotAt": "2026-08-06T10:00:00", "source": "snapshot_2026-08-06.json", "stale": True},
                "listings": [],
            },
        )[0]

        self.assertEqual(enriched["etsy_listing_id"], "4554394648")
        self.assertIsNone(enriched["etsy_manager_status"])
        self.assertEqual(enriched["etsy_edit_url"], "https://www.etsy.com/your/shops/me/listing-editor/edit/4554394648")
        self.assertEqual(enriched["etsy_link_type"], "manager_fallback")
        self.assertEqual(enriched["etsy_link_warning_reason"], "listing_not_in_stale_snapshot")
        self.assertIsNone(enriched["etsy_link_unavailable_reason"])
        self.assertFalse(enriched["etsy_link_verified"])

    def test_missing_listing_with_fresh_snapshot_uses_unverified_manager_fallback(self) -> None:
        enriched = enrich_products_with_etsy_manager(
            [{"etsy_url": "https://www.etsy.com/listing/4554423428", "status": "✅ Đã đăng draft"}],
            {
                "freshness": {"snapshotAt": "2026-08-06T10:00:00", "source": "snapshot_2026-08-06.json", "stale": False},
                "listings": [],
            },
        )[0]

        self.assertEqual(enriched["etsy_listing_id"], "4554423428")
        self.assertIsNone(enriched["etsy_manager_status"])
        self.assertIsNone(enriched["etsy_public_url"])
        self.assertIsNone(enriched["etsy_manage_url"])
        self.assertEqual(enriched["etsy_edit_url"], "https://www.etsy.com/your/shops/me/listing-editor/edit/4554423428")
        self.assertEqual(enriched["etsy_link_type"], "manager_fallback")
        self.assertFalse(enriched["etsy_link_verified"])
        self.assertIsNone(enriched["etsy_link_unavailable_reason"])
        self.assertIsNone(enriched["etsy_link_warning_reason"])
        self.assertFalse(enriched["etsy_snapshot_stale"])

    def test_known_unverified_url_draft_status_variant_uses_manager_fallback(self) -> None:
        enriched = enrich_products_with_etsy_manager(
            [{
                "etsy_url": "https://www.etsy.com/listing/4554423428",
                "status": "  ✅ Đã đăng draft (URL chưa xác minh)  ",
            }],
            {
                "freshness": {"snapshotAt": "2026-08-06T10:00:00", "source": "snapshot_2026-08-06.json", "stale": False},
                "listings": [],
            },
        )[0]

        self.assertEqual(enriched["etsy_link_type"], "manager_fallback")
        self.assertEqual(enriched["etsy_edit_url"], "https://www.etsy.com/your/shops/me/listing-editor/edit/4554423428")
        self.assertFalse(enriched["etsy_link_verified"])

    def test_negated_or_error_draft_text_never_uses_manager_fallback(self) -> None:
        for status in ("not draft", "❌ Lỗi: draft creation failed", "draft"):
            with self.subTest(status=status):
                enriched = enrich_products_with_etsy_manager(
                    [{"etsy_url": "https://www.etsy.com/listing/4554423428", "status": status}],
                    {
                        "freshness": {"snapshotAt": "2026-08-06T10:00:00", "source": "snapshot_2026-08-06.json", "stale": False},
                        "listings": [],
                    },
                )[0]

                self.assertEqual(enriched["etsy_link_type"], "unavailable")
                self.assertIsNone(enriched["etsy_edit_url"])
                self.assertEqual(enriched["etsy_link_unavailable_reason"], "listing_not_in_snapshot")
                self.assertFalse(enriched["etsy_link_verified"])

    def test_non_draft_status_never_uses_manager_fallback(self) -> None:
        enriched = enrich_products_with_etsy_manager(
            [{"etsy_url": "https://www.etsy.com/listing/4554394648", "status": "✅ Đã đăng"}],
            {
                "freshness": {"snapshotAt": "2026-08-06T10:00:00", "source": "snapshot_2026-08-06.json", "stale": True},
                "listings": [],
            },
        )[0]

        self.assertEqual(enriched["etsy_link_type"], "local_unverified")
        self.assertEqual(
            "https://www.etsy.com/listing/4554394648",
            enriched["etsy_public_url"],
        )
        self.assertEqual("listing_not_in_snapshot", enriched["etsy_link_unavailable_reason"])
        self.assertEqual("listing_not_in_snapshot", enriched["etsy_link_warning_reason"])
        self.assertFalse(enriched["etsy_link_verified"])
        self.assertIsNone(enriched["etsy_edit_url"])

    def test_non_successful_status_remains_unavailable_for_local_unverified(self) -> None:
        for status in (
            "draft",
            "⏳ Chờ đăng",
            "❌ Lỗi: draft creation failed",
            "Lỗi: Đã đăng thất bại",
            "Lỗi Đã đăng thất bại",
        ):
            with self.subTest(status=status):
                enriched = enrich_products_with_etsy_manager(
                    [{"etsy_url": "https://www.etsy.com/listing/4554394648", "status": status}],
                    {
                        "freshness": {"snapshotAt": "2026-08-06T10:00:00", "source": "snapshot_2026-08-06.json", "stale": True},
                        "listings": [],
                    },
                )[0]

                self.assertEqual(enriched["etsy_link_type"], "unavailable")
                self.assertIsNone(enriched["etsy_edit_url"])
                self.assertIsNone(enriched["etsy_public_url"])
                self.assertEqual("listing_not_in_snapshot", enriched["etsy_link_unavailable_reason"])
                self.assertFalse(enriched["etsy_link_verified"])

    def test_foreign_host_listing_url_is_not_accepted_for_fallback(self) -> None:
        enriched = enrich_products_with_etsy_manager(
            [{"etsy_url": "https://example.invalid/listing/4554394648", "status": "✅ Đã đăng draft"}],
            {
                "freshness": {"snapshotAt": "2026-08-06T10:00:00", "source": "snapshot_2026-08-06.json", "stale": True},
                "listings": [],
            },
        )[0]

        self.assertIsNone(enriched["etsy_listing_id"])
        self.assertEqual(enriched["etsy_link_type"], "unavailable")
        self.assertEqual(enriched["etsy_link_unavailable_reason"], "no_listing_id")

    def test_invalid_listing_id_no_fallback(self) -> None:
        enriched = enrich_products_with_etsy_manager(
            [{"etsy_url": "https://www.etsy.com/listing/abc", "status": "✅ Đã đăng draft"}],
            {
                "freshness": {"snapshotAt": "2026-08-06T10:00:00", "source": "snapshot_2026-08-06.json", "stale": True},
                "listings": [],
            },
        )[0]

        self.assertEqual(enriched["etsy_listing_id"], None)
        self.assertEqual(enriched["etsy_link_type"], "unavailable")
        self.assertEqual(enriched["etsy_link_unavailable_reason"], "no_listing_id")

    def test_exact_snapshot_match_has_precedence_over_fallback(self) -> None:
        enriched = enrich_products_with_etsy_manager(
            [{"etsy_url": "https://www.etsy.com/listing/999", "status": "✅ Đã đăng draft"}],
            {
                "freshness": {"snapshotAt": "2026-08-06T10:00:00", "source": "snapshot_2026-08-06.json", "stale": True},
                "listings": [{
                    "id": "999",
                    "managerStatus": "draft",
                    "editUrl": "https://www.etsy.com/your/shops/me/listing-editor/edit/999",
                }],
            },
        )[0]

        self.assertEqual(enriched["etsy_link_type"], "manager")
        self.assertEqual(enriched["etsy_edit_url"], "https://www.etsy.com/your/shops/me/listing-editor/edit/999")
        self.assertIsNone(enriched["etsy_link_warning_reason"])

    def test_matched_listing_with_mismatched_editor_id_remains_unavailable(self) -> None:
        enriched = enrich_products_with_etsy_manager(
            [{"etsy_url": "https://www.etsy.com/listing/999", "status": "✅ Đã đăng draft"}],
            {
                "freshness": {"snapshotAt": "2026-08-06T10:00:00", "source": "snapshot_2026-08-06.json", "stale": False},
                "listings": [{
                    "id": "999",
                    "managerStatus": "draft",
                    "editUrl": "https://www.etsy.com/your/shops/me/listing-editor/edit/998",
                }],
            },
        )[0]

        self.assertEqual(enriched["etsy_manager_status"], "draft")
        self.assertIsNone(enriched["etsy_edit_url"])
        self.assertEqual(enriched["etsy_link_type"], "unavailable")
        self.assertEqual(enriched["etsy_link_unavailable_reason"], "manager_url_missing")
        self.assertFalse(enriched["etsy_link_verified"])
        self.assertIsNone(enriched["etsy_link_warning_reason"])

    def test_unknown_status_exposes_metadata_but_does_not_select_manager_link(self) -> None:
        enriched = enrich_products_with_etsy_manager(
            [{"etsy_url": "https://www.etsy.com/listing/789"}],
            {"listings": [{
                "id": "789",
                "managerStatus": "archived",
                "editUrl": "https://www.etsy.com/your/shops/me/listing-editor/edit/789",
            }]},
        )[0]

        self.assertEqual(enriched["etsy_manager_status"], "archived")
        self.assertEqual(enriched["etsy_edit_url"], "https://www.etsy.com/your/shops/me/listing-editor/edit/789")
        self.assertEqual(enriched["etsy_link_type"], "unavailable")


if __name__ == "__main__":
    unittest.main()
