import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import crawl_tiktok_beauty_listing as crawler


class FakeLocator:
    def __init__(self, hrefs):
        self.hrefs = hrefs

    async def evaluate_all(self, _expression):
        return self.hrefs


class FakePage:
    def __init__(self, hrefs):
        self.hrefs = hrefs

    def locator(self, selector):
        if selector != "a":
            raise AssertionError(f"unexpected selector: {selector}")
        return FakeLocator(self.hrefs)


class BeautyListingCrawlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_listing_route_is_not_counted_as_creator(self):
        hrefs = [
            "https://collabstr.com/anhlinhhz?p=tiktok",
            "https://collabstr.com/influencers?ph_id=1715618&p=tiktok&c=Beauty",
            "https://collabstr.com/other-creator?p=instagram",
            "https://collabstr.com/creator/nested?p=tiktok",
        ]
        self.assertEqual(
            await crawler.extract_listing_urls(FakePage(hrefs)),
            ["https://collabstr.com/anhlinhhz?p=tiktok"],
        )

    def test_resumed_manifest_backlog_is_drained_before_boundary_pause(self):
        discovered = [
            "https://collabstr.com/creator-a?p=tiktok",
            "https://collabstr.com/creator-b?p=tiktok",
            "https://collabstr.com/creator-c?p=tiktok",
            "https://collabstr.com/creator-d?p=tiktok",
            "https://collabstr.com/creator-e?p=tiktok",
            "https://collabstr.com/influencers?p=tiktok",
        ]
        done = {crawler.key(url) for url in discovered[:3]}
        attempted_this_run = set()
        pending = crawler.pending_profile_urls(discovered, done, attempted_this_run)

        self.assertEqual(
            pending,
            [
                "https://collabstr.com/creator-d?p=tiktok",
                "https://collabstr.com/creator-e?p=tiktok",
            ],
        )
        self.assertFalse(
            crawler.should_pause_at_pagination_boundary(
                discovered, done, attempted_this_run
            )
        )
        attempted_this_run.update(crawler.key(url) for url in pending)
        self.assertTrue(
            crawler.should_pause_at_pagination_boundary(
                discovered, done, attempted_this_run
            )
        )


    def test_discovered_urls_filters_old_listing_row_without_mutating_manifest(self):
        rows = [
            {"source_url": "https://collabstr.com/creator-a?p=tiktok"},
            {"source_url": "https://collabstr.com/influencers?p=tiktok"},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = Path(temp_dir) / "urls.jsonl"
            manifest.write_text("".join(json.dumps(row) + "\n" for row in rows))
            original = crawler.URLS
            crawler.URLS = manifest
            try:
                discovered = crawler.discovered_urls()
            finally:
                crawler.URLS = original

            self.assertEqual(discovered, ["https://collabstr.com/creator-a?p=tiktok"])
            self.assertEqual(len(manifest.read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
