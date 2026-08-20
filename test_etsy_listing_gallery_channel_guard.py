import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.external_contract

SKILL_SCRIPTS = Path.home() / ".codex" / "skills" / "etsy-10-image-maker" / "scripts"
GUARD_PATH = SKILL_SCRIPTS / "etsy_listing_gallery_channel_guard.py"
CONTRACT_PATH = SKILL_SCRIPTS / "etsy_listing_gallery_contract.example.json"


@pytest.fixture(scope="module")
def guard():
    if not GUARD_PATH.is_file() or not CONTRACT_PATH.is_file():
        pytest.skip(
            "requires the optional local Codex Etsy image-maker skill "
            f"({SKILL_SCRIPTS})"
        )

    spec = importlib.util.spec_from_file_location("etsy_listing_gallery_channel_guard", GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_contract():
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_contract_example_passes_guard(guard):
    assert guard.validate_contract(load_contract(), example_mode=True) == []


def test_cli_passes_contract_example(guard):
    result = subprocess.run(
        [sys.executable, str(GUARD_PATH), "--example", str(CONTRACT_PATH)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"valid": true' in result.stdout


def test_rejects_website_framing_marker(guard):
    contract = load_contract()
    contract["slots"][0]["prompt"] = "Create a premium homepage hero for a website."
    failures = guard.validate_contract(contract, example_mode=True)
    assert any("homepage" in failure for failure in failures)


def test_rejects_blank_mockup_only_marker(guard):
    contract = load_contract()
    contract["slots"][0]["prompt"] = "Create a blank-mockup-only Etsy asset with no product content."
    failures = guard.validate_contract(contract, example_mode=True)
    assert any("blank-mockup-only" in failure for failure in failures)


def test_rejects_wrong_channel_and_non_ten_slots(guard):
    contract = load_contract()
    contract["channel"] = "website_homepage"
    contract["slots"] = contract["slots"][:9]
    failures = guard.validate_contract(contract, example_mode=True)
    assert any("channel must be exactly" in failure for failure in failures)
    assert any("exactly 10" in failure for failure in failures)


def test_rejects_missing_source_asset_and_buyer_question(guard):
    contract = load_contract()
    contract["slots"][4].pop("source_assets")
    contract["slots"][6]["buyer_question"] = ""
    failures = guard.validate_contract(contract, example_mode=True)
    assert any("slot 05" in failure and "source_asset" in failure for failure in failures)
    assert any("slot 07" in failure and "buyer_question" in failure for failure in failures)


def test_rejects_product_evidence_below_channel_floor(guard):
    contract = load_contract()
    contract["slots"][0]["product_evidence_target"] = 0.39
    failures = guard.validate_contract(contract, example_mode=True)
    assert any(
        "slot 01 product_evidence_target must be >= product_evidence_rules.min_union_canvas_ratio"
        in failure
        for failure in failures
    )


def test_rejects_missing_slot_ten_thank_you_and_usage_hint(guard):
    contract = load_contract()
    contract["slots"][9]["prompt"] = "Create the closing Etsy listing-gallery image with an exact source-page preview."
    failures = guard.validate_contract(contract, example_mode=True)
    assert any("slot 10 must include THANK YOU wording" in failure for failure in failures)
    assert any("slot 10 prompt neutral usage hint missing" in failure for failure in failures)


def test_guard_does_not_mutate_contract(guard):
    contract = load_contract()
    original = copy.deepcopy(contract)
    guard.validate_contract(contract, example_mode=True)
    assert contract == original
