import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from collabstr_page_selection import select_page_indexes


class SelectPageIndexesTests(unittest.TestCase):
    def test_finds_filtered_listing_even_when_creator_tabs_come_first(self):
        urls = [
            "https://collabstr.com/sanasdiary?p=tiktok",
            "https://collabstr.com/sigourneys?p=tiktok",
            "https://collabstr.com/influencers?ph_id=1715618&p=tiktok&c=Lifestyle&pg=52",
            "https://collabstr.com/influencers?ph_id=1715618&p=tiktok&c=Lifestyle&pg=48",
        ]

        listing_index, profile_index = select_page_indexes(
            urls,
            "https://collabstr.com/influencers?ph_id=1715618&p=tiktok&c=Lifestyle&pg=51",
        )

        self.assertEqual(listing_index, 2)
        self.assertEqual(profile_index, 0)

    def test_requests_new_listing_when_no_matching_filtered_page_exists(self):
        urls = ["https://collabstr.com/sanasdiary?p=tiktok"]

        self.assertEqual(
            select_page_indexes(
                urls,
                "https://collabstr.com/influencers?ph_id=1715618&p=tiktok&c=Lifestyle&pg=52",
            ),
            (None, 0),
        )

    def test_other_filtered_listing_is_not_reused_as_profile_page(self):
        urls = [
            "https://collabstr.com/influencers?ph_id=1715618&p=tiktok&c=Lifestyle&pg=150",
            "https://collabstr.com/jadeliu?p=tiktok",
        ]

        self.assertEqual(
            select_page_indexes(
                urls,
                "https://collabstr.com/influencers?ph_id=1715618&p=tiktok&c=Beauty",
            ),
            (None, 1),
        )


if __name__ == "__main__":
    unittest.main()
