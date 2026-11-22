from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from output import apply_bulk_seo_cdp as bulk


ROOT = Path(__file__).resolve().parent
PAYLOAD_PATH = ROOT / "output" / "daisyflow_seo_bulk_payload_20260911.json"


def test_real_payload_is_exactly_109_safe_listings() -> None:
    records = bulk.load_payload_records(PAYLOAD_PATH)
    assert len(records) == 109
    assert bulk.SHOP == "daisyflowdigital"
    assert not {record.listing_id for record in records} & bulk.MANUAL_HOLD_LISTING_IDS
    assert all(len(record.recommended_tags) == 13 for record in records)
    assert all(len(set(record.recommended_tags)) == 13 for record in records)
    assert all(max(map(len, record.recommended_tags)) <= 20 for record in records)


def test_selection_is_bounded_and_preserves_explicit_order() -> None:
    records = bulk.load_payload_records(PAYLOAD_PATH)
    requested = [records[4].listing_id, records[1].listing_id]
    selected = bulk.select_listing_ids(
        records,
        explicit_listing_ids=requested,
        start=1,
        limit=1,
        checkpointed_ids=set(),
    )
    assert selected == [records[4].listing_id]


def test_selection_rejects_checkpoint_duplicates_even_for_explicit_ids() -> None:
    records = bulk.load_payload_records(PAYLOAD_PATH)
    with pytest.raises(ValueError, match="checkpoint already contains"):
        bulk.select_listing_ids(
            records,
            explicit_listing_ids=[records[0].listing_id],
            start=1,
            limit=None,
            checkpointed_ids={records[0].listing_id},
        )


def test_apply_requires_clear_confirmation() -> None:
    parser = bulk.build_parser()
    args = parser.parse_args(["--apply"])
    assert args.apply is True
    assert bulk.DEFAULT_LIVE_CONFIRM_TOKEN
    with pytest.raises(ValueError, match="confirmation"):
        bulk.validate_cli(args)


def test_raw_cdp_staging_contains_known_title_and_tag_add_interaction() -> None:
    title_script = bulk.build_set_title_script("title")
    assert "#listing-title-input" in title_script
    assert "el.value = value;" in title_script
    assert "Object.getOwnPropertyDescriptor" not in title_script
    tag_input = bulk.build_input_script("tag")
    assert "#listing-tags-input" in tag_input
    assert "InputEvent" in tag_input
    assert "#listing-tags-button" in bulk.build_click_add_script()
    assert ".click()" in bulk.build_click_add_script()
    assert "#listing-category" not in bulk.build_set_title_script("title")
    assert "#listing-files" not in tag_input
    save_script = bulk.build_save_script()
    assert "Save changes" in save_script
    assert "Publish changes" in save_script
    assert "Publish listing" not in save_script
    assert "querySelectorAll" in save_script


def test_tag_input_staging_uses_direct_value_assignment_and_events() -> None:
    script = bulk.build_input_script("tag")

    assert "const value = \"tag\";" in script
    assert "input.value = value;" in script
    assert "new InputEvent('input'" in script
    assert "data: value" in script
    assert "new Event('change'" in script
    assert "Object.getOwnPropertyDescriptor" not in script
    assert "descriptor.set.call" not in script


def test_zero_based_selection_rejects_duplicates_and_holds() -> None:
    records = bulk.load_payload_records(PAYLOAD_PATH)
    selected = bulk.select_listings(
        records,
        requested_ids=[records[4].listing_id, records[1].listing_id],
        start_offset=0,
        limit=1,
    )
    assert [record.listing_id for record in selected] == [records[4].listing_id]
    with pytest.raises(bulk.BatchInputError, match="duplicate"):
        bulk.select_listings(records, requested_ids=[records[0].listing_id, records[0].listing_id])
    with pytest.raises(bulk.BatchInputError, match="manual hold"):
        bulk.parse_listing_ids(["4528255712"])


def test_payload_validation_rejects_duplicate_tags() -> None:
    payload = json.loads(PAYLOAD_PATH.read_text(encoding="utf-8"))
    payload["listings"][0]["recommended_tags"][1] = payload["listings"][0]["recommended_tags"][0]
    with pytest.raises(bulk.BatchInputError, match="unique"):
        bulk.validate_payload(payload)


def test_checkpoint_is_atomic_and_rejects_repeated_listing_without_resume(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.jsonl"
    result = {"listing_id": "123", "status": "dry_run_ready"}
    bulk.append_checkpoint_result(checkpoint, result)
    assert checkpoint.read_text(encoding="utf-8").count("\n") == 1
    with pytest.raises(bulk.BatchInputError, match="already exists"):
        bulk.ensure_checkpoint_ids(checkpoint, ["123"], resume=False)
    assert bulk.ensure_checkpoint_ids(checkpoint, ["123"], resume=True) == {"123"}


class _DryRunFakeCDP:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state
        self.expressions: list[str] = []

    async def command(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        self.expressions.append(f"command:{method}:{json.dumps(params or {}, sort_keys=True)}")
        return {}

    async def notify(self, method: str, params: dict[str, object] | None = None) -> None:
        self.expressions.append(f"notify:{method}:{json.dumps(params or {}, sort_keys=True)}")

    async def evaluate(self, expression: str, await_promise: bool = False):
        self.expressions.append(expression)
        if "Boolean(document.querySelector('#listing-title-input'))" in expression:
            return True
        if "location.pathname" in expression and "daisyflowdigital" in expression:
            return {
                "url": bulk.EDIT_URL_TEMPLATE.format(listing_id="4434259578"),
                "is_expected_path": True,
                "has_shop_identity": True,
                "host": "www.etsy.com",
            }
        if "const title = document.querySelector('#listing-title-input')" in expression:
            return self.state
        raise AssertionError(f"unexpected CDP expression: {expression[:100]}")


def test_dry_run_reads_only_and_never_stages_or_saves(monkeypatch: pytest.MonkeyPatch) -> None:
    record = bulk.load_payload_records(PAYLOAD_PATH)[0]
    state = {
        "ready": True,
        "title": record.live_current_title,
        "description": "description",
        "price": "4.99",
        "tags": ["existing"],
        "image_count": 4,
    }
    fake = _DryRunFakeCDP(state)
    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(bulk.asyncio, "sleep", no_sleep)
    result = asyncio.run(
        bulk.apply_listing(
            fake,
            record,
            apply_mode=False,
            accept_current_baseline=False,
        )
    )
    assert result["status"] == "dry_run_ready"
    assert result["save_called"] is False
    assert not any("listing-tags-input" in expression for expression in fake.expressions)
    assert not any("listing-tags-button" in expression for expression in fake.expressions)
    assert not any("Publish" in expression or "Save" in expression for expression in fake.expressions)
