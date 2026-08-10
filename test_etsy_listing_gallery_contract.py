from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.validate_etsy_listing_gallery_contract import validate_contract


ROOT = Path(__file__).resolve().parent
EXAMPLE = ROOT / "examples" / "etsy_listing_gallery_contract.example.json"


class EtsyListingGalleryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.valid_contract = json.loads(EXAMPLE.read_text(encoding="utf-8"))

    def test_example_contract_is_valid(self) -> None:
        report = validate_contract(self.valid_contract)

        self.assertTrue(report["valid"], report)
        self.assertEqual(report["summary"]["slot_count"], 10)
        self.assertGreaterEqual(report["summary"]["computed_source_coverage"], 0.1)

    def test_channel_must_be_etsy_listing_gallery(self) -> None:
        for value in ("website", "homepage"):
            with self.subTest(value=value):
                contract = copy.deepcopy(self.valid_contract)
                contract["channel"] = value

                report = validate_contract(contract)

                self.assertFalse(report["valid"])
                self.assertTrue(
                    any("channel must be exactly" in error for error in report["errors"])
                )

    def test_prompt_markers_are_rejected(self) -> None:
        for marker in ("website", "homepage", "landing page", "blank-mockup-only"):
            with self.subTest(marker=marker):
                contract = copy.deepcopy(self.valid_contract)
                contract["prompt"] = f"Build a {marker} for this project."

                report = validate_contract(contract)

                self.assertFalse(report["valid"])
                self.assertTrue(
                    any("rejected prompt marker" in error for error in report["errors"])
                )

    def test_contract_requires_exactly_ten_slots(self) -> None:
        contract = copy.deepcopy(self.valid_contract)
        contract["slots"] = contract["slots"][:-1]

        report = validate_contract(contract)

        self.assertFalse(report["valid"])
        self.assertTrue(any("exactly 10" in error for error in report["errors"]))

    def test_every_slot_requires_source_page_and_buyer_question(self) -> None:
        contract = copy.deepcopy(self.valid_contract)
        del contract["slots"][3]["source_page"]
        contract["slots"][4]["buyer_question"] = ""

        report = validate_contract(contract)

        self.assertFalse(report["valid"])
        self.assertTrue(any("source_page" in error for error in report["errors"]))
        self.assertTrue(any("buyer_question" in error for error in report["errors"]))

    def test_source_coverage_threshold_is_enforced(self) -> None:
        contract = copy.deepcopy(self.valid_contract)
        contract["source_coverage"]["threshold"] = 0.5

        report = validate_contract(contract)

        self.assertFalse(report["valid"])
        self.assertTrue(
            any("source coverage is below threshold" in error for error in report["errors"])
        )

    def test_slot_one_and_ten_semantics_are_required(self) -> None:
        contract = copy.deepcopy(self.valid_contract)
        contract["slots"][0]["semantic"] = "included"
        contract["slots"][9]["semantic"] = "purchase_clarity"

        report = validate_contract(contract)

        self.assertFalse(report["valid"])
        self.assertTrue(any("slot 01 semantic" in error for error in report["errors"]))
        self.assertTrue(any("slot 10 semantic" in error for error in report["errors"]))

    def test_cli_returns_nonzero_and_json_report_for_invalid_contract(self) -> None:
        contract = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        contract["slots"] = contract["slots"][:9]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps(contract), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "validate_etsy_listing_gallery_contract.py"),
                    str(path),
                    "--json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 2)
        report = json.loads(result.stdout)
        self.assertFalse(report["valid"])
        self.assertTrue(any("exactly 10" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()
