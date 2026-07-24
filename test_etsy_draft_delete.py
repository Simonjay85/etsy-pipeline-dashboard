#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import tempfile
import json
import unittest
from collections import defaultdict
from unittest.mock import AsyncMock, patch
from pathlib import Path
from datetime import datetime, timedelta

from fastapi import HTTPException
from playwright.async_api import async_playwright

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


class FakePage:
    def __init__(self):
        self.goto_calls = []
        self.wait_calls = []
        self.url = "https://www.etsy.com/your/shops/me/tools/listings"

    async def goto(self, url, wait_until=None):
        self.url = url
        self.goto_calls.append(url)

    async def wait_for_timeout(self, ms):
        self.wait_calls.append(ms)

    def set_default_timeout(self, *args, **kwargs):
        return None


class TestEtsyDraftDelete(unittest.TestCase):
    async def _launch_browser(self, pw):
        try:
            return await pw.chromium.launch(channel="chrome", headless=True)
        except Exception:
            return await pw.chromium.launch(
                executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                headless=True,
            )

    def _run_select_checkbox_with_html(self, html: str, listing_id: str) -> dict[str, bool] | bool:
        async def runner():
            async with async_playwright() as pw:
                browser = await self._launch_browser(pw)
                try:
                    page = await browser.new_page()
                    await page.set_content(html)
                    return await etsy_clean_duplicates.select_listing_checkbox(page, listing_id)
                finally:
                    await browser.close()

        return asyncio.run(runner())

    def _run_select_checkbox_with_html_and_state(self, html: str, listing_id: str) -> tuple[bool, dict[str, bool]]:
        async def runner():
            async with async_playwright() as pw:
                browser = await self._launch_browser(pw)
                try:
                    page = await browser.new_page()
                    await page.set_content(html)
                    selected = await etsy_clean_duplicates.select_listing_checkbox(page, listing_id)
                    states = await page.evaluate(
                        """() => {
                            return {
                                native_111: !!document.querySelector('#cb-111') && document.querySelector('#cb-111').checked,
                                native_222: !!document.querySelector('#cb-222') && document.querySelector('#cb-222').checked,
                                aria_222: !!document.querySelector('#cb-222-aria') && document.querySelector('#cb-222-aria').getAttribute('aria-checked')
                            };
                        }"""
                    )
                    return selected, states
                finally:
                    await browser.close()

        return asyncio.run(runner())

    def test_select_targets_maps_ids_to_expected_pages(self):
        all_listings = {
            "4528885906": {"id": "4528885906", "title": "A", "page": 3},
            "4528917703": {"id": "4528917703", "title": "B", "page": 2},
            "4529166986": {"id": "4529166986", "title": "C", "page": 3},
        }
        selected_ids, selected_names, missing, ids_by_page = etsy_clean_duplicates._select_targets(
            all_listings, ["4528885906", "4528917703", "4529166986"]
        )
        self.assertEqual([], missing)
        self.assertEqual(["4528885906", "4528917703", "4529166986"], selected_ids)
        self.assertEqual(["A", "B", "C"], selected_names)
        self.assertEqual({3: ["4528885906", "4529166986"], 2: ["4528917703"]}, ids_by_page)

    def test_dry_run_payload_no_delete_fields(self):
        all_listings = {
            "111": {"id": "111", "title": "One", "page": 2},
            "222": {"id": "222", "title": "Two", "page": 2},
            "333": {"id": "333", "title": "Three", "page": 3},
        }
        selected_ids, selected_names, missing, ids_by_page = etsy_clean_duplicates._select_targets(
            all_listings, ["111", "333"]
        )
        payload = etsy_clean_duplicates._build_dry_run_report(
            "daisyflowdigital", selected_ids, selected_names, missing, ids_by_page, all_listings
        )
        self.assertTrue(payload["dry_run"])
        self.assertNotIn("deleted_listing_ids", payload)
        self.assertEqual(payload["selected_count"], 2)
        self.assertEqual(payload["by_page"], {"2": ["111"], "3": ["333"]})
        self.assertEqual(payload["missing_ids"], [])

    def test_missing_preflight_for_explicit_ids(self):
        all_listings = {
            "111": {"id": "111", "title": "One", "page": 2},
            "222": {"id": "222", "title": "Two", "page": 2},
        }
        selected_ids, selected_names, missing, ids_by_page = etsy_clean_duplicates._select_targets(
            all_listings, ["111", "999", "222"]
        )
        self.assertEqual(["999"], missing)
        self.assertEqual(["111", "999", "222"], selected_ids)
        self.assertIn("999", selected_names[1])  # fallback name for missing
        self.assertEqual(defaultdict(list, {2: ["111", "222"]}), ids_by_page)

    def test_execute_deletions_processes_pages_descending(self):
        page = FakePage()
        ids_by_page = {2: ["111"], 3: ["222", "333"]}
        listings_by_page = {
            2: [{"id": "111", "title": "One", "page": 2}],
            3: [{"id": "222", "title": "Two", "page": 3}, {"id": "333", "title": "Three", "page": 3}],
        }

        async def fake_scrape(fake_page, page_number):
            return listings_by_page.get(page_number, [])

        async def fake_select(fake_page, listing_id):
            return True

        async def fake_verify_after(fake_page, shop_id, target_ids, page_number):
            return []

        async def fake_verify_identity(_page, _shop):
            return None

        async def fake_click(_page):
            return None

        with patch.object(etsy_clean_duplicates, "scrape_draft_listings", new=fake_scrape), \
                patch.object(etsy_clean_duplicates, "select_listing_checkbox", new=fake_select), \
                patch.object(etsy_clean_duplicates, "click_delete_and_confirm", new=fake_click), \
                patch.object(etsy_clean_duplicates, "verify_shop_identity", new=fake_verify_identity), \
                patch.object(etsy_clean_duplicates, "verify_absent_after_delete", new=fake_verify_after):
            deleted = asyncio.run(etsy_clean_duplicates.execute_deletions(page, "daisyflowdigital", ids_by_page))

        self.assertEqual(["222", "333", "111"], deleted)
        self.assertEqual([
            "https://www.etsy.com/your/shops/me/tools/listings/page:3,state:draft",
            "https://www.etsy.com/your/shops/me/tools/listings/page:2,state:draft",
        ], page.goto_calls)

    def test_select_listing_checkbox_uses_parent_card_for_card_body_anchor(self):
        html = """
        <div class="card">
          <div class="card-body">
            <a href="/listing-editor/edit/4528263097">Business Planner 2027</a>
          </div>
          <input id="cb-111" type="checkbox"/>
        </div>
        """
        selected = self._run_select_checkbox_with_html(html, "4528263097")
        self.assertTrue(selected)

    def test_select_listing_checkbox_selects_only_target_when_cards_adjacent(self):
        html = """
        <div id="list">
          <div class="card">
            <div class="card-body">
              <a href="/listing-editor/edit/111">One</a>
            </div>
            <input id="cb-111" type="checkbox"/>
          </div>
          <div class="card">
            <div class="card-body">
              <a href="/listing-editor/edit/222">Two</a>
            </div>
            <input id="cb-222" type="checkbox"/>
          </div>
        </div>
        """
        selected, states = self._run_select_checkbox_with_html_and_state(html, "111")
        self.assertTrue(selected)
        self.assertTrue(states["native_111"])
        self.assertFalse(states["native_222"])

    def test_select_listing_checkbox_does_not_match_listing_id_prefix(self):
        html = """
        <div class="card">
          <div class="card-body">
            <a href="/listing-editor/edit/45282630970">Not target listing</a>
          </div>
          <input id="cb-222" type="checkbox"/>
        </div>
        """
        selected = self._run_select_checkbox_with_html(html, "4528263097")
        self.assertFalse(selected)

    def test_select_listing_checkbox_fails_without_checkbox(self):
        html = """
        <div class="card">
          <a href="/listing-editor/edit/111">Missing checkbox</a>
        </div>
        """
        selected = self._run_select_checkbox_with_html(html, "111")
        self.assertFalse(selected)

    def test_select_listing_checkbox_fails_with_multiple_listing_ids_in_same_container(self):
        html = """
        <div class="card">
          <a href="/listing-editor/edit/111">One</a>
          <input id="cb-111" type="checkbox"/>
          <a href="/listing-editor/edit/222">Two</a>
        </div>
        """
        selected = self._run_select_checkbox_with_html(html, "111")
        self.assertFalse(selected)

    def test_select_listing_checkbox_supports_checked_native_and_aria(self):
        html = """
        <div id="root">
          <div class="card">
            <a href="/listing-editor/edit/111">Native prechecked</a>
            <input id="cb-111" type="checkbox" checked />
          </div>
          <div class="card">
            <a href="/listing-editor/edit/222">Aria prechecked</a>
            <div id="cb-222-aria" role="checkbox" aria-checked="true"></div>
          </div>
        </div>
        """
        selected, states = self._run_select_checkbox_with_html_and_state(html, "222")
        self.assertTrue(selected)
        self.assertTrue(states["aria_222"] == "true")

        selected_native, states_native = self._run_select_checkbox_with_html_and_state(html, "111")
        self.assertTrue(selected_native)
        self.assertTrue(states_native["native_111"])

    def test_execute_deletions_never_clicks_delete_if_selection_fails(self):
        page = FakePage()
        ids_by_page = {2: ["111", "222"]}
        listings_by_page = {
            2: [{"id": "111", "title": "One", "page": 2}, {"id": "222", "title": "Two", "page": 2}],
        }

        async def fake_scrape(fake_page, page_number):
            return listings_by_page.get(page_number, [])

        async def fake_select(_fake_page, listing_id):
            return listing_id == "111"

        async def fake_verify_identity(_page, _shop):
            return None

        async def fake_verify_after(*args, **kwargs):
            return ["222"]

        click_delete = AsyncMock()

        with patch.object(etsy_clean_duplicates, "scrape_draft_listings", new=fake_scrape), \
                patch.object(etsy_clean_duplicates, "select_listing_checkbox", new=fake_select), \
                patch.object(etsy_clean_duplicates, "click_delete_and_confirm", new=click_delete), \
                patch.object(etsy_clean_duplicates, "verify_shop_identity", new=fake_verify_identity), \
                patch.object(etsy_clean_duplicates, "verify_absent_after_delete", new=fake_verify_after):
            with self.assertRaises(RuntimeError):
                asyncio.run(etsy_clean_duplicates.execute_deletions(page, "daisyflowdigital", ids_by_page))

        self.assertFalse(click_delete.called)

    def test_verify_absent_after_delete_detects_moved_listing_globally(self):
        page = FakePage()
        all_pages_listing = {
            "777": {"id": "777", "title": "Moved", "page": 2},
            "888": {"id": "888", "title": "Other", "page": 2},
        }

        async def fake_current_page(_page, page_number):
            # On the page just deleted, target listing appears moved away (simulating pagination shift).
            return []

        async def fake_collect(_page, _shop_id):
            return all_pages_listing

        with patch.object(etsy_clean_duplicates, "scrape_draft_listings", new=fake_current_page), \
                patch.object(etsy_clean_duplicates, "collect_all_draft_listings", new=fake_collect):
            remaining = asyncio.run(etsy_clean_duplicates.verify_absent_after_delete(page, "daisyflowdigital", ["777"], 3))

        self.assertEqual(["777"], remaining)

    def test_cleaner_resolves_shop_specific_session_and_identity(self):
        self.assertEqual("daisyflowdigital", etsy_clean_duplicates.expected_shop_slug("daisyflowdigital"))
        self.assertTrue(
            etsy_clean_duplicates.browser_dir_for_shop("daisyflowdigital").name.endswith("daisyflowdigital"),
            f"Session path: {etsy_clean_duplicates.browser_dir_for_shop('daisyflowdigital')}",
        )

    def test_validation_accepts_unique_numeric_drafts(self):
        self.assertEqual(["101"], dashboard_app._validate_draft_listing_ids([101], SNAPSHOT))

    def test_validation_rejects_duplicate_missing_and_non_draft(self):
        for ids in (["101", "101"], ["999"], ["202"], ["bad"]):
            with self.subTest(ids=ids), self.assertRaises(HTTPException):
                dashboard_app._validate_draft_listing_ids(ids, SNAPSHOT)

    def test_route_pins_shop_and_invokes_explicit_id_without_real_delete(self):
        create = AsyncMock(return_value=FakeProcess())
        source = Path(tempfile.gettempdir()) / f"etsy_manager_current_daisyflowdigital_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        source.write_text("{}")
        fresh_snapshot = dict(SNAPSHOT)
        fresh_snapshot["source"] = str(source)
        with patch.object(dashboard_app, "_active_shop_id", "daisyflowdigital"), patch.object(
            dashboard_app, "latest_etsy_manager_snapshot", return_value=fresh_snapshot
        ), patch.object(dashboard_app.asyncio, "create_subprocess_exec", create), patch.object(
            dashboard_app, "_remove_deleted_drafts_from_snapshot"
        ) as remove:
            result = asyncio.run(dashboard_app.delete_selected_etsy_drafts(Request({"listing_ids": ["101"]})))
        self.assertTrue(result["ok"])
        command = create.await_args.args
        self.assertIn("--shop", command)
        self.assertIn("daisyflowdigital", command)
        self.assertEqual(("101",), tuple(command[index + 1] for index, value in enumerate(command) if value == "--listing-id"))
        remove.assert_called_once_with(fresh_snapshot, ["101"])

    def test_route_rejects_requested_non_active_shop_before_subprocess(self):
        create = AsyncMock()
        with patch.object(dashboard_app, "_active_shop_id", "daisyflowdigital"), patch.object(
            dashboard_app.asyncio, "create_subprocess_exec", create
        ), self.assertRaises(HTTPException):
            asyncio.run(dashboard_app.delete_selected_etsy_drafts(Request({"shop": "templystudios", "listing_ids": ["101"]})))
        create.assert_not_awaited()

    def test_route_rejects_old_snapshot_before_subprocess(self):
        old = Path(tempfile.gettempdir()) / f"etsy_manager_current_daisyflowdigital_{(datetime.now() - timedelta(days=2)).strftime('%Y%m%d_%H%M%S')}.json"
        old.write_text("{}")
        old_snapshot = {
            "source": str(old),
            "listings": [
                {"id": "101", "managerStatus": "draft"},
            ],
        }
        create = AsyncMock()
        with patch.object(dashboard_app, "_active_shop_id", "daisyflowdigital"), patch.object(
            dashboard_app, "latest_etsy_manager_snapshot", return_value=old_snapshot
        ), patch.object(
            dashboard_app.asyncio, "create_subprocess_exec", create
        ), patch.object(dashboard_app, "_assert_shop_identity", return_value=None), self.assertRaises(HTTPException) as context:
            asyncio.run(dashboard_app.delete_selected_etsy_drafts(Request({"listing_ids": ["101"]})))
        self.assertEqual(409, context.exception.status_code)
        create.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
