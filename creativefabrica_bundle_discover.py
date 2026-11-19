#!/usr/bin/env python3
"""Slow, checkpointed discovery of additional Creative Fabrica bundle URLs."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

DEFAULT_KNOWN = Path("/Users/aaronnguyen/Developer/Etsy/output/creativeca_catalog/bundles_survey.csv")
DEFAULT_OUTPUT = Path("/Users/aaronnguyen/Developer/Etsy/output/creativeca_catalog/bundles_survey_next9000_source.csv")
DEFAULT_CHECKPOINT = Path("/Users/aaronnguyen/Developer/Etsy/output/creativeca_catalog/bundles_survey_next9000_source.checkpoint.jsonl")
CDP_URL = "http://127.0.0.1:9333"


def canonical(url: str) -> str:
    return url.split("?", 1)[0].rstrip("/") + "/"


def product_id_from_image(src: str) -> str:
    patterns = (
        r"-(\d+)-\d+-\d+x\d+\.(?:jpg|jpeg|png|webp)",
        r"-(\d+)-1-(?:\d+x\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, src, re.I)
        if match:
            return match.group(1)
    return ""


def title_from_card(text: str) -> str:
    ignored = {"bundles", "in"}
    for line in (part.strip() for part in text.splitlines()):
        if line and line.lower() not in ignored and line.upper() != "BUNDLES":
            return line
    return ""


def load_ids(path: Path) -> set[str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {row["product_id"].strip() for row in csv.DictReader(handle) if row.get("product_id")}


def load_checkpoint(path: Path) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    last_page = 0
    if not path.exists():
        return rows, last_page
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        last_page = max(last_page, int(record["page"]))
        rows.extend(record["rows"])
    by_id = {row["product_id"]: row for row in rows if row.get("product_id")}
    return list(by_id.values()), last_page


def write_source(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["page", "page_position", "source_position", "title", "canonical_product_url", "product_id", "discovered_at_utc"]
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


async def discover(target: int, known_path: Path, output: Path, checkpoint: Path, start_page: int, page_pause: float, block_every: int, block_pause: float) -> dict[str, Any]:
    known = load_ids(known_path)
    rows, checkpoint_page = load_checkpoint(checkpoint)
    seen = known | {row["product_id"] for row in rows}
    page = max(start_page, checkpoint_page + 1)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL, timeout=30_000)
        context = browser.contexts[0]
        tab = await context.new_page()
        try:
            while len(rows) < target:
                url = f"https://www.creativefabrica.com/bundles/page/{page}/"
                response = await tab.goto(url, wait_until="domcontentloaded", timeout=60_000)
                await tab.wait_for_timeout(3_000)
                cards = tab.locator('[data-testid="product-card"]')
                if not await cards.count():
                    raise RuntimeError(f"No product cards on page {page}; HTTP {response.status if response else 'N/A'}")
                page_rows: list[dict[str, Any]] = []
                for position in range(await cards.count()):
                    card = cards.nth(position)
                    hrefs = await card.locator('a[href*="/product/"]').evaluate_all("els => els.map(e => e.href)")
                    if not hrefs:
                        continue
                    product_url = canonical(hrefs[0])
                    image_srcs = await card.locator("img").evaluate_all("els => els.map(e => e.currentSrc || e.src || e.dataset.src || '')")
                    product_id = next((product_id_from_image(src) for src in image_srcs if product_id_from_image(src)), "")
                    if not product_id or product_id in seen:
                        continue
                    page_rows.append({
                        "page": page,
                        "page_position": position + 1,
                        "source_position": 1000 + len(rows) + len(page_rows) + 1,
                        "title": title_from_card(await card.inner_text()),
                        "canonical_product_url": product_url,
                        "product_id": product_id,
                        "discovered_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    })
                for row in page_rows:
                    if len(rows) >= target:
                        break
                    rows.append(row)
                    seen.add(row["product_id"])
                write_source(output, rows)
                with checkpoint.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"page": page, "rows": page_rows}, ensure_ascii=False) + "\n")
                print(f"DISCOVER page={page} cards={await cards.count()} new={len(page_rows)} total={len(rows)}/{target}", flush=True)
                page += 1
                if len(rows) >= target:
                    break
                await tab.wait_for_timeout(int(page_pause * 1000))
                if block_every and (page - start_page) % block_every == 0:
                    print(f"DISCOVERY_PAUSE seconds={block_pause}", flush=True)
                    await asyncio.sleep(block_pause)
        finally:
            await tab.close()
            await browser.close()
    result = {
        "source_url": "https://www.creativefabrica.com/bundles/",
        "target_new_urls": target,
        "rows": len(rows),
        "pages_last_checked": page - 1,
        "known_ids_excluded": len(known),
        "output": str(output),
        "checkpoint": str(checkpoint),
        "started_utc": started,
        "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, default=9000)
    parser.add_argument("--known-csv", type=Path, default=DEFAULT_KNOWN)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--start-page", type=int, default=30)
    parser.add_argument("--page-pause", type=float, default=3.0)
    parser.add_argument("--block-every", type=int, default=10)
    parser.add_argument("--block-pause", type=float, default=60.0)
    args = parser.parse_args()
    asyncio.run(discover(args.target, args.known_csv, args.output_csv, args.checkpoint, args.start_page, args.page_pause, args.block_every, args.block_pause))


if __name__ == "__main__":
    main()
