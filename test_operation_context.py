#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import FrozenInstanceError
import unittest

from operation_context import OperationContext, OperationContextError


class OperationContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.product = {
            "folder": "product-01",
            "etsy_url": "https://www.etsy.com/listing/1234567890",
        }

    def test_resolves_and_normalizes_operation(self) -> None:
        context = OperationContext.from_request(
            row=10,
            payload={"shop": "templystudios", "folder": "product-01", "listing_id": "1234567890", "request_id": "abc-123"},
            active_shop_id="templystudios",
            current_folder=self.product["folder"],
            current_etsy_url=self.product["etsy_url"],
            operation="push-to-etsy",
        )

        self.assertEqual(context.shop_id, "templystudios")
        self.assertEqual(context.operation, "etsy_push_update")
        self.assertEqual(context.key, "templystudios:etsy_push_update:product-01")

    def test_rejects_stale_identity(self) -> None:
        with self.assertRaises(OperationContextError):
            OperationContext.from_request(
                row=10,
                payload={"shop": "other-shop", "folder": "product-01", "listing_id": "1234567890"},
                active_shop_id="templystudios",
                current_folder=self.product["folder"],
                current_etsy_url=self.product["etsy_url"],
                operation="etsy_push_update",
            )

    def test_rejects_missing_listing_id(self) -> None:
        with self.assertRaises(OperationContextError):
            OperationContext.from_request(
                row=10,
                payload=None,
                active_shop_id="templystudios",
                current_folder="product-01",
                current_etsy_url="",
                operation="etsy_push_update",
            )

    def test_context_is_immutable(self) -> None:
        context = OperationContext.from_request(
            row=4,
            payload={"shop": "templystudios", "folder": "product-01", "listing_id": "1234567890"},
            active_shop_id="templystudios",
            current_folder=self.product["folder"],
            current_etsy_url=self.product["etsy_url"],
            operation="etsy_sync_from",
        )

        with self.assertRaises(FrozenInstanceError):
            context.row = 99

    def test_row_is_valid_positive_integer(self) -> None:
        context = OperationContext.from_request(
            row=4,
            payload=None,
            active_shop_id="templystudios",
            current_folder=self.product["folder"],
            current_etsy_url=self.product["etsy_url"],
            operation="etsy_push_update",
        )
        self.assertEqual(context.row, 4)

    def test_rejects_invalid_row_formats(self) -> None:
        with self.assertRaises(OperationContextError):
            OperationContext.from_request(
                row=0,
                payload=None,
                active_shop_id="templystudios",
                current_folder=self.product["folder"],
                current_etsy_url=self.product["etsy_url"],
                operation="etsy_push_update",
            )
        with self.assertRaises(OperationContextError):
            OperationContext.from_request(
                row=-1,
                payload=None,
                active_shop_id="templystudios",
                current_folder=self.product["folder"],
                current_etsy_url=self.product["etsy_url"],
                operation="etsy_push_update",
            )

if __name__ == "__main__":
    unittest.main()
