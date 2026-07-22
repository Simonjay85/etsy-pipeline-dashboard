#!/usr/bin/env python3
"""Idempotently create local products for every unmapped Etsy listing.

This is the command behind the dashboard's "Tạo product mới + Sync" bulk
action.  It calls the same endpoint implementation one listing at a time and
writes one JSON result per line, so an interrupted run can be resumed safely.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import re
import sys
import time
from pathlib import Path

import dashboard_app as dashboard


class JsonRequest:
    def __init__(self, payload: dict[str, str]):
        self.payload = payload

    async def json(self) -> dict[str, str]:
        return self.payload


def listing_id_from_product(product: dict) -> str | None:
    match = re.search(r"/listing/(\d+)", str(product.get("etsy_url") or ""))
    return match.group(1) if match else None


def candidates() -> list[dict]:
    snapshot = dashboard.latest_etsy_manager_snapshot()
    mapped = {
        listing_id_from_product(product)
        for product in dashboard.products_from_excel()
    }
    mapped.discard(None)
    result = []
    for listing in snapshot.get("listings", []):
        listing_id = str(listing.get("id") or "")
        if listing_id and listing_id not in mapped:
            result.append(listing)
    return result


def current_mapped_listing_ids() -> set[str]:
    return {listing_id for listing_id in map(listing_id_from_product, dashboard.products_from_excel()) if listing_id}


async def run(output_path: Path, limit: int | None) -> int:
    todo = candidates()
    if limit is not None:
        todo = todo[:limit]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as output:
        total = len(todo)
        print(f"[BULK] active_shop={dashboard._active_shop_id} candidates={total}", flush=True)
        for index, listing in enumerate(todo, 1):
            listing_id = str(listing.get("id"))
            started = time.time()
            try:
                result = await dashboard.create_local_product_from_etsy(
                    JsonRequest({"listing_id": listing_id})
                )
                payload = {
                    "index": index,
                    "total": total,
                    "listing_id": listing_id,
                    "title": listing.get("title"),
                    "elapsed_seconds": round(time.time() - started, 2),
                    "result": result,
                }
            except Exception as exc:  # noqa: BLE001 - persist per-listing failure
                payload = {
                    "index": index,
                    "total": total,
                    "listing_id": listing_id,
                    "title": listing.get("title"),
                    "elapsed_seconds": round(time.time() - started, 2),
                    "result": {"ok": False, "error": str(exc)},
                }
            output.write(json.dumps(payload, ensure_ascii=False) + "\n")
            output.flush()
            result = payload["result"]
            marker = "OK" if result.get("ok") and result.get("sync_ok") else "WARN" if result.get("ok") else "FAIL"
            print(
                f"[BULK] {index}/{total} {marker} Etsy {listing_id} "
                f"folder={result.get('folder', '-') } error={result.get('sync_error') or result.get('error') or '-'}",
                flush=True,
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit phải lớn hơn 0")

    if dashboard._active_shop_id != "daisyflowdigital":
        raise SystemExit(
            f"Active shop hiện là {dashboard._active_shop_id}; script này chỉ chạy Daisy Flow Digital."
        )
    stamp = time.strftime("%Y%m%d_%H%M%S")
    output_path = args.output or (dashboard.BASE_DIR / "output" / f"bulk_create_daisy_{stamp}.jsonl")
    lock_path = Path("/tmp/etsy-bulk-create-daisyflowdigital.lock")
    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Một bulk create Daisy khác đang chạy.")
        return asyncio.run(run(output_path, args.limit))


if __name__ == "__main__":
    sys.exit(main())
