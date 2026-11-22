#!/usr/bin/env python3
"""Read-only Creative Fabrica Bundles favorite survey.

The current Creative Fabrica product page renders the live product counter in
#js-counter-banner .favorite-count. The legacy #favorite-count selector is not
present on the current layout, so this script also verifies the banner product
ID before accepting its metric.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, BrowserContext, Page

DEFAULT_INPUT = Path("/Users/aaronnguyen/Developer/Etsy/output/creativeca_catalog/bundles_survey.csv")
DEFAULT_OUTPUT = Path("/Users/aaronnguyen/Developer/Etsy/output/creativeca_catalog")
DEFAULT_CDP = "http://127.0.0.1:9333"


def parse_metric(raw: str | None) -> int | float | None:
    if raw is None:
        return None
    value = raw.strip().replace(",", "").replace(" ", "")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KkMm]?)[Xx]?", value)
    if not match:
        return None
    number = float(match.group(1))
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000}[match.group(2).upper()]
    result = number * multiplier
    return int(result) if result.is_integer() else result


def load_source(path: Path, target: int) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    for source_position, row in enumerate(rows, 1):
        url = (row.get("canonical_product_url") or row.get("product_url") or "").strip()
        product_id = (row.get("product_id") or "").strip()
        if not url or not product_id or product_id in seen:
            continue
        seen.add(product_id)
        selected.append(
            {
                "page": int(row.get("page") or 0),
                "page_position": int(row.get("page_position") or source_position),
                "source_position": source_position,
                "title": (row.get("title") or "").strip(),
                "canonical_product_url": url,
                "product_id": product_id,
            }
        )
        if len(selected) >= target:
            break
    if len(selected) != target:
        raise RuntimeError(f"Cần {target} sản phẩm unique, chỉ có {len(selected)} trong source")
    return selected


async def read_current_metric(page: Page, expected_id: str) -> tuple[str, str, str, str | None]:
    """Return title, product id, raw metric, and an error if unresolved."""
    deadline = time.monotonic() + 15.0
    title = ""
    observed_id = ""
    raw = ""
    while time.monotonic() < deadline:
        try:
            h1 = page.locator("h1").first
            if await h1.count():
                title = (await h1.inner_text()).strip()
            banner = page.locator("#js-counter-banner").first
            if await banner.count():
                id_nodes = banner.locator('[id^="product-"]')
                if await id_nodes.count():
                    observed_id = ((await id_nodes.first.get_attribute("id")) or "").removeprefix("product-")
                if observed_id == expected_id:
                    metric = banner.locator(".favorite-count").first
                    if await metric.count():
                        raw = (await metric.inner_text()).strip()
                        if raw:
                            return title, observed_id, raw, None
                    # The current banner is loaded but has no metric. Do not
                    # read a recommendation card or a stale previous banner.
                    return title, observed_id, "", "favorite metric unresolved in current product banner"
        except Exception:
            pass
        await page.wait_for_timeout(500)
    if observed_id and observed_id != expected_id:
        return title, observed_id, "", f"banner product_id mismatch: expected {expected_id}, got {observed_id}"
    return title, observed_id, "", "current product banner unresolved"


async def survey_one(page: Page, item: dict[str, Any], delay_ms: int) -> dict[str, Any]:
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status: int | None = None
    title = item["title"]
    observed_id = ""
    raw = ""
    error: str | None = None
    try:
        response = await page.goto(item["canonical_product_url"], wait_until="domcontentloaded", timeout=60_000)
        status = response.status if response else None
        await page.wait_for_timeout(delay_ms)
        title, observed_id, raw, error = await read_current_metric(page, item["product_id"])
    except Exception as exc:  # preserve failures in the export
        error = f"{type(exc).__name__}: {str(exc)[:240]}"
    estimate = parse_metric(raw)
    if not raw and not error:
        error = "favorite metric unresolved"
    return {
        "page": item["page"],
        "page_position": item["page_position"],
        "source_position": item["source_position"],
        "title": title,
        "canonical_product_url": item["canonical_product_url"],
        "product_id": item["product_id"],
        "observed_banner_product_id": observed_id,
        "raw_favorite_display": raw or None,
        "favorite_estimate": estimate,
        "rank_by_favorites": None,
        "http_status": status,
        "status": "parsed" if estimate is not None else "unresolved",
        "error": error,
        "metric_selector": "#js-counter-banner .favorite-count",
        "scraped_at_utc": started,
    }


async def run_survey(
    items: list[dict[str, Any]],
    cdp_url: str,
    concurrency: int,
    delay_ms: int,
    checkpoint: Path,
    pause_every: int = 0,
    pause_seconds: float = 0,
    max_new_records: int = 0,
) -> list[dict[str, Any]]:
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    # Resume from an existing checkpoint. Validate its identity against the
    # current source list so a different crawl cannot be mixed in silently.
    records_by_index: dict[int, dict[str, Any]] = {}
    if checkpoint.exists():
        for line in checkpoint.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            idx = int(record.get("crawl_index", -1))
            if 0 <= idx < len(items) and record.get("product_id") == items[idx]["product_id"]:
                records_by_index[idx] = record
        if records_by_index:
            print(f"RESUME checkpoint={checkpoint} completed={len(records_by_index)}/{len(items)}", flush=True)
    records: list[dict[str, Any]] = list(records_by_index.values())
    pending_indices = [idx for idx in range(len(items)) if idx not in records_by_index]
    if max_new_records > 0:
        pending_indices = pending_indices[:max_new_records]
    lock = asyncio.Lock()
    pause_gate = asyncio.Lock()
    completed_start = len(records)
    pause_state = {
        "next": ((completed_start // pause_every) + 1) * pause_every
        if pause_every > 0 else 0
    }
    next_index = 0
    with checkpoint.open("a", encoding="utf-8") as handle:
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(cdp_url, timeout=30_000)
            context: BrowserContext = browser.contexts[0]
            pages = [await context.new_page() for _ in range(min(concurrency, len(items)))]

            async def worker(page: Page) -> None:
                nonlocal next_index
                while True:
                    async with lock:
                        if next_index >= len(pending_indices):
                            return
                        idx = pending_indices[next_index]
                        next_index += 1
                    record = await survey_one(page, items[idx], delay_ms)
                    record["crawl_index"] = idx
                    async with lock:
                        records.append(record)
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                        handle.flush()
                        completed = len(records)
                    should_pause = False
                    if pause_every > 0 and pause_seconds > 0:
                        async with pause_gate:
                            if completed >= pause_state["next"] and completed < len(items):
                                pause_state["next"] += pause_every
                                should_pause = True
                    print(
                        f"{idx + 1}/{len(items)} {record['product_id']} "
                        f"{record['raw_favorite_display'] or 'N/A'} {record['status']}",
                        flush=True,
                    )
                    if should_pause:
                        print(
                            f"RATE_LIMIT_PAUSE completed={completed} seconds={pause_seconds}",
                            flush=True,
                        )
                        await asyncio.sleep(pause_seconds)

            try:
                await asyncio.gather(*(worker(page) for page in pages))
            finally:
                for page in pages:
                    await page.close()
                # Do not call browser.close(): with CDP this can close the
                # user's worker browser/profile. Closing the temporary pages
                # above is sufficient; Playwright disconnects on loop exit.
    return sorted(records, key=lambda row: row["crawl_index"])


def write_outputs(
    records: list[dict[str, Any]],
    output_dir: Path,
    source_url: str,
    pages_checked: int,
    scope_label: str,
    output_prefix: str,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    parsed = [row for row in records if isinstance(row["favorite_estimate"], (int, float))]
    parsed.sort(key=lambda row: (-row["favorite_estimate"], row["crawl_index"]))
    for rank, row in enumerate(parsed, 1):
        row["rank_by_favorites"] = rank
    by_index = {row["crawl_index"]: row for row in records}
    records = [by_index[index] for index in sorted(by_index)]
    for row in records:
        row.pop("crawl_index", None)
    qualified = [row for row in parsed if row["favorite_estimate"] > 100]
    unresolved = [row for row in records if row["favorite_estimate"] is None]
    summary = {
        "source_url": source_url,
        "scope": scope_label,
        "pages_checked": pages_checked,
        "rows": len(records),
        "unique_product_ids": len({row["product_id"] for row in records}),
        "unique_canonical_urls": len({row["canonical_product_url"] for row in records}),
        "parsed_count": len(parsed),
        "unresolved_count": len(unresolved),
        "qualified_count": len(qualified),
        "metric_selector": "#js-counter-banner .favorite-count",
        "retrieved_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    fields = [
        "page", "page_position", "source_position", "title", "canonical_product_url",
        "product_id", "observed_banner_product_id", "raw_favorite_display", "favorite_estimate",
        "rank_by_favorites", "http_status", "status", "error", "metric_selector", "scraped_at_utc",
    ]
    csv_path = output_dir / f"{output_prefix}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
    json_path = output_dir / f"{output_prefix}.json"
    json_path.write_text(json.dumps({**summary, "products": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = output_dir / f"{output_prefix}.md"
    md_path.write_text(
        "\n".join(
            [
                "# Creative Fabrica Bundles survey (fixed favorite extractor)",
                "",
                f"- Source: {source_url}",
                f"- Scope: {scope_label}",
                f"- Pages checked: {pages_checked}",
                f"- Rows: {summary['rows']}",
                f"- Unique IDs: {summary['unique_product_ids']}",
                f"- Unique canonical URLs: {summary['unique_canonical_urls']}",
                f"- Parsed Favorites: {summary['parsed_count']}",
                f"- Unresolved/N/A: {summary['unresolved_count']}",
                f"- Qualified (>100): {summary['qualified_count']}",
                f"- Selector: `{summary['metric_selector']}` with current banner product-ID verification",
                "",
                "## Top parsed products",
                "",
                "| Rank | Title | Raw Favorites | Estimate | URL |",
                "|---:|---|---:|---:|---|",
            ]
            + [
                f"| {row['rank_by_favorites']} | {row['title']} | {row['raw_favorite_display']} | {row['favorite_estimate']} | {row['canonical_product_url']} |"
                for row in parsed[:50]
            ]
            + [
                "",
                "## Notes",
                "",
                "- Only the counter attached to the current product banner was accepted.",
                "- Recommendation cards, the global Favorites menu, and stale banner IDs were rejected.",
                "- Missing counters remain unresolved; they are never coerced to zero.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return {**summary, "csv": str(csv_path), "json": str(json_path), "report": str(md_path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-csv", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cdp-url", default=DEFAULT_CDP)
    parser.add_argument("--target", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--delay-ms", type=int, default=1500)
    parser.add_argument("--pause-every", type=int, default=0)
    parser.add_argument("--pause-seconds", type=float, default=0)
    parser.add_argument("--max-new-records", type=int, default=0)
    parser.add_argument("--scope-label", default="first 1,000 unique product URLs")
    parser.add_argument("--output-prefix", default="bundles_survey_fixed")
    args = parser.parse_args()
    if args.concurrency < 1 or args.concurrency > 4:
        raise SystemExit("concurrency phải trong khoảng 1..4")
    if args.pause_every < 0 or args.pause_seconds < 0:
        raise SystemExit("pause-every và pause-seconds không được âm")
    if args.max_new_records < 0:
        raise SystemExit("max-new-records không được âm")
    items = load_source(args.input_csv, args.target)
    checkpoint = args.output_dir / "bundles_survey_fixed.checkpoint.jsonl"
    records = asyncio.run(
        run_survey(
            items,
            args.cdp_url,
            args.concurrency,
            args.delay_ms,
            checkpoint,
            args.pause_every,
            args.pause_seconds,
            args.max_new_records,
        )
    )
    pages_checked = max(int(row["page"] or 0) for row in records)
    result = write_outputs(
        records,
        args.output_dir,
        "https://www.creativefabrica.com/bundles/",
        pages_checked,
        args.scope_label,
        args.output_prefix,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
