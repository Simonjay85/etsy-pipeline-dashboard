#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import dashboard_app
import etsy_clean_duplicates


SNAPSHOT = {
    "source": "/tmp/not-written.json",
    "listings": [
        {"id": "101", "managerStatus": "draft"},
        {"id": "202", "managerStatus": "active"},
    ],
}


class Request:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class FakeProcess:
    returncode = 0

    async def communicate(self):
        result = {"ok": True, "shop": "daisyflowdigital", "deleted_listing_ids": ["101"]}
        return json.dumps(result).encode(), b""


class TestEtsyDraftDelete(unittest.TestCase):
    def test_cleaner_resolves_shop_specific_session_and_identity(self):
        self.assertEqual("daisyflowdigital", etsy_clean_duplicates.expected_shop_slug("daisyflowdigital"))
        self.assertEqual(
            ".browser-session-daisyflowdigital",
            etsy_clean_duplicates.browser_dir_for_shop("daisyflowdigital").name,
        )

    def test_validation_accepts_unique_numeric_drafts(self):
        self.assertEqual(["101"], dashboard_app._validate_draft_listing_ids([101], SNAPSHOT))

    def test_validation_rejects_duplicate_missing_and_non_draft(self):
        for ids in (["101", "101"], ["999"], ["202"], ["bad"]):
            with self.subTest(ids=ids), self.assertRaises(HTTPException):
                dashboard_app._validate_draft_listing_ids(ids, SNAPSHOT)

    def test_route_pins_shop_and_invokes_explicit_id_without_real_delete(self):
        create = AsyncMock(return_value=FakeProcess())
        with patch.object(dashboard_app, "_active_shop_id", "daisyflowdigital"), patch.object(
            dashboard_app, "latest_etsy_manager_snapshot", return_value=SNAPSHOT
        ), patch.object(dashboard_app.asyncio, "create_subprocess_exec", create), patch.object(
            dashboard_app, "_remove_deleted_drafts_from_snapshot"
        ) as remove:
            result = asyncio.run(dashboard_app.delete_selected_etsy_drafts(Request({"listing_ids": ["101"]})))
        self.assertTrue(result["ok"])
        command = create.await_args.args
        self.assertIn("--shop", command)
        self.assertIn("daisyflowdigital", command)
        self.assertEqual(("101",), tuple(command[index + 1] for index, value in enumerate(command) if value == "--listing-id"))
        remove.assert_called_once_with(SNAPSHOT, ["101"])

    def test_route_rejects_requested_non_active_shop_before_subprocess(self):
        create = AsyncMock()
        with patch.object(dashboard_app, "_active_shop_id", "daisyflowdigital"), patch.object(
            dashboard_app.asyncio, "create_subprocess_exec", create
        ), self.assertRaises(HTTPException):
            asyncio.run(dashboard_app.delete_selected_etsy_drafts(Request({"shop": "templystudios", "listing_ids": ["101"]})))
        create.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
