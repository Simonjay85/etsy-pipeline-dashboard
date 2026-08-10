#!/usr/bin/env python3
"""Validate the pre-generation contract for an Etsy listing gallery.

This guard is intentionally narrow: it validates the buyer-facing gallery
contract, not image pixels, branding, source rendering, or listing state.
Keeping it independent means generation code can fail closed before any scene
or source asset work begins.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


EXPECTED_CHANNEL = "etsy_listing_gallery"
EXPECTED_SLOTS = tuple(f"{number:02d}" for number in range(1, 11))

_PROMPT_MARKERS = (
    re.compile(r"\bwebsite\b", re.IGNORECASE),
    re.compile(r"\bhomepage\b", re.IGNORECASE),
    re.compile(r"\blanding(?:\s+page)?\b", re.IGNORECASE),
    re.compile(r"\bblank[\s_-]+mockup[\s_-]+only\b", re.IGNORECASE),
)

_HERO_QUESTION = re.compile(r"\b(click|hero|first\s+impression)\b", re.IGNORECASE)
_THANK_YOU_QUESTION = re.compile(
    r"\b(thank|support|license|licen[cs]e|usage|use)\b", re.IGNORECASE
)


class ContractValidationError(ValueError):
    """Raised for malformed top-level contract data."""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_slot_id(value: Any) -> str | None:
    if isinstance(value, int) and 1 <= value <= 10:
        return f"{value:02d}"
    if isinstance(value, str) and value.isdigit() and 1 <= int(value) <= 10:
        return f"{int(value):02d}"
    return None


def _iter_prompt_texts(value: Any, key_path: tuple[str, ...] = ()) -> Iterable[tuple[str, str]]:
    """Yield strings held by keys that describe a generation prompt."""

    if isinstance(value, dict):
        for key, child in value.items():
            path = (*key_path, str(key))
            key_name = str(key).lower().replace("-", "_")
            if "prompt" in key_name and isinstance(child, str):
                yield ".".join(path), child
            else:
                yield from _iter_prompt_texts(child, path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_prompt_texts(child, (*key_path, str(index)))


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _source_pages(value: Any) -> list[int] | None:
    """Normalize a slot's source_page field and reject ambiguous references."""

    raw_pages: list[Any]
    if isinstance(value, int) and not isinstance(value, bool):
        raw_pages = [value]
    elif isinstance(value, list):
        raw_pages = value
    else:
        return None

    if not raw_pages or any(
        isinstance(page, bool) or not isinstance(page, int) or page < 1 for page in raw_pages
    ):
        return None
    return sorted(set(raw_pages))


def validate_contract(
    contract: Any,
    *,
    minimum_source_coverage: float | None = None,
) -> dict[str, Any]:
    """Return a structured validation report without mutating the contract."""

    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(contract, dict):
        return {
            "valid": False,
            "errors": ["contract must be a JSON object"],
            "warnings": [],
        }

    if contract.get("channel") != EXPECTED_CHANNEL:
        errors.append(f"channel must be exactly {EXPECTED_CHANNEL!r}")

    for path, prompt in _iter_prompt_texts(contract):
        for marker in _PROMPT_MARKERS:
            match = marker.search(prompt)
            if match:
                errors.append(
                    f"{path} contains rejected prompt marker {match.group(0)!r}"
                )
                break

    slots = contract.get("slots")
    if not isinstance(slots, list):
        errors.append("slots must be an array containing exactly 10 slots")
        slots = []
    elif len(slots) != 10:
        errors.append(f"slots must contain exactly 10 entries; found {len(slots)}")

    normalized_slots: dict[str, dict[str, Any]] = {}
    for index, slot in enumerate(slots):
        path = f"slots[{index}]"
        if not isinstance(slot, dict):
            errors.append(f"{path} must be an object")
            continue

        slot_id = _as_slot_id(slot.get("slot"))
        if slot_id is None:
            errors.append(f"{path}.slot must be an integer or string from 01 through 10")
            continue
        if slot_id in normalized_slots:
            errors.append(f"duplicate slot id {slot_id}")
        normalized_slots[slot_id] = slot

        if not _nonempty_string(slot.get("buyer_question")):
            errors.append(f"{path}.buyer_question must be a non-empty string")

        pages = _source_pages(slot.get("source_page"))
        if pages is None:
            errors.append(f"{path}.source_page must contain at least one positive page reference")

    missing_slots = [slot_id for slot_id in EXPECTED_SLOTS if slot_id not in normalized_slots]
    if missing_slots:
        errors.append(f"missing required slot ids: {', '.join(missing_slots)}")

    coverage = contract.get("source_coverage")
    source_page_count: int | None = None
    threshold: float | None = None
    if not isinstance(coverage, dict):
        errors.append("source_coverage must be an object with source_page_count and threshold")
    else:
        source_page_count = coverage.get("source_page_count")
        threshold = coverage.get("threshold")
        if (
            isinstance(source_page_count, bool)
            or not isinstance(source_page_count, int)
            or source_page_count < 1
        ):
            errors.append("source_coverage.source_page_count must be a positive integer")
            source_page_count = None
        if not _is_number(threshold) or not 0 < float(threshold) <= 1:
            errors.append("source_coverage.threshold must be greater than 0 and at most 1")
            threshold = None

    if minimum_source_coverage is not None:
        if not 0 < minimum_source_coverage <= 1:
            errors.append("--min-source-coverage must be greater than 0 and at most 1")
        elif threshold is not None and threshold < minimum_source_coverage:
            errors.append(
                "source_coverage.threshold is below the requested "
                f"minimum {minimum_source_coverage:.4f}"
            )

    referenced_pages = {
        page
        for slot in normalized_slots.values()
        for page in (_source_pages(slot.get("source_page")) or [])
    }
    if source_page_count is not None:
        out_of_range = sorted(page for page in referenced_pages if page > source_page_count)
        if out_of_range:
            errors.append(
                "source_page references exceed source_coverage.source_page_count: "
                + ", ".join(map(str, out_of_range))
            )

    computed_coverage = None
    if source_page_count:
        computed_coverage = len(referenced_pages) / source_page_count
        if threshold is not None and computed_coverage < float(threshold):
            errors.append(
                "source coverage is below threshold: "
                f"{computed_coverage:.4f} < {float(threshold):.4f}"
            )

    slot_01 = normalized_slots.get("01")
    if slot_01 is not None:
        semantic = str(slot_01.get("semantic", "")).strip().lower().replace("-", "_")
        if semantic != "hero":
            errors.append("slot 01 semantic must be 'hero'")
        if not _nonempty_string(slot_01.get("buyer_question")) or not _HERO_QUESTION.search(
            slot_01["buyer_question"]
        ):
            errors.append("slot 01 buyer_question must express the hero/click-through question")

    slot_10 = normalized_slots.get("10")
    if slot_10 is not None:
        semantic = str(slot_10.get("semantic", "")).strip().lower().replace("-", "_")
        if semantic != "thank_you":
            errors.append("slot 10 semantic must be 'thank_you'")
        if not _nonempty_string(slot_10.get("buyer_question")) or not _THANK_YOU_QUESTION.search(
            slot_10["buyer_question"]
        ):
            errors.append(
                "slot 10 buyer_question must address thank-you, usage, license, or support clarity"
            )

    if not _nonempty_string(contract.get("source")):
        warnings.append("source path is not recorded; source-page evidence should remain traceable")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "channel": contract.get("channel"),
            "slot_count": len(slots),
            "expected_slot_count": 10,
            "referenced_source_pages": sorted(referenced_pages),
            "referenced_source_page_count": len(referenced_pages),
            "source_page_count": source_page_count,
            "computed_source_coverage": computed_coverage,
            "required_source_coverage": threshold,
        },
    }


def _load_contract(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractValidationError(f"contract file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ContractValidationError(f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contract", type=Path, help="JSON gallery contract to validate")
    parser.add_argument(
        "--min-source-coverage",
        type=float,
        default=None,
        help="optional additional lower bound for source_coverage.threshold",
    )
    parser.add_argument("--json", action="store_true", help="print the structured validation report")
    args = parser.parse_args(argv)

    try:
        report = validate_contract(
            _load_contract(args.contract),
            minimum_source_coverage=args.min_source_coverage,
        )
    except ContractValidationError as exc:
        report = {"valid": False, "errors": [str(exc)], "warnings": []}

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    elif report["valid"]:
        summary = report.get("summary", {})
        print(
            "VALID: Etsy listing-gallery contract "
            f"with {summary.get('slot_count', 0)} slots and "
            f"{summary.get('computed_source_coverage', 0):.2%} source coverage"
        )
    else:
        print("INVALID: Etsy listing-gallery contract", file=sys.stderr)
        for error in report["errors"]:
            print(f"- {error}", file=sys.stderr)

    return 0 if report["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
