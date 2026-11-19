#!/usr/bin/env python3
"""Slow authenticated Creative Fabrica ZIP downloader for the top-100 POD source set."""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from playwright.async_api import async_playwright

ROOT = Path('/Users/aaronnguyen/Developer/Etsy')
DEFAULT_MANIFEST = ROOT / 'output/creativeca_catalog/top100_verified/top100_verified.csv'
DEFAULT_OUT = ROOT / 'output/creativeca_catalog/top100_verified'
DEFAULT_CDP = 'http://127.0.0.1:9333'


def safe_name(value: str) -> str:
    value = re.sub(r'[^A-Za-z0-9._-]+', '-', value).strip('-._')
    return value[:180] or 'creative-fabrica-source'


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def load_done(path: Path) -> dict[int, dict[str, Any]]:
    done: dict[int, dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                record = json.loads(line)
                if record.get('status') in ('downloaded', 'downloaded_valid_zip'):
                    done[int(record['rank'])] = record
    return done


def write_summary(path: Path, rows: list[dict[str, str]], done: dict[int, dict[str, Any]], checkpoint: Path) -> None:
    summary = {
        'target': len(rows),
        'downloaded_valid_zip': len(done),
        'pending': len(rows) - len(done),
        'checkpoint': str(checkpoint),
        'updated_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'license_status': 'pending_not_checked',
        'etsy_status': 'not_created',
    }
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')


async def run(manifest: Path, out: Path, cdp: str, delay: float, pause_every: int, pause_seconds: float, max_new: int, min_rank: int, products_root: Path | None) -> None:
    rows = load_rows(manifest)
    checkpoint = out / 'download_checkpoint.jsonl'
    summary_path = out / 'download_summary.json'
    out.mkdir(parents=True, exist_ok=True)
    products_base = products_root if products_root is not None else out / 'products'
    done = load_done(checkpoint)
    pending = [row for row in rows if int(row['rank']) not in done and int(row['rank']) >= min_rank]
    if max_new > 0:
        pending = pending[:max_new]
    print(f'RESUME downloaded={len(done)}/{len(rows)} pending_this_run={len(pending)}', flush=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp, timeout=30_000, no_defaults=True)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        download_dir = out / '.browser_downloads'
        download_dir.mkdir(parents=True, exist_ok=True)
        page_cdp = await context.new_cdp_session(page)
        try:
            await page_cdp.send('Page.setDownloadBehavior', {'behavior': 'allow', 'downloadPath': str(download_dir)})
        except Exception as exc:
            print(f'DOWNLOAD_BEHAVIOR_WARNING {exc}', flush=True)
        try:
            for offset, row in enumerate(pending, 1):
                rank = int(row['rank'])
                product_id = row['product_id']
                record: dict[str, Any] = {
                    'rank': rank,
                    'product_id': product_id,
                    'title': row['title'],
                    'source_url': row['canonical_product_url'],
                    'started_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                    'license_status': 'pending_not_checked',
                }
                try:
                    same_product_page = page.url.rstrip('/') == row['canonical_product_url'].rstrip('/')
                    response = None if same_product_page else await page.goto(row['canonical_product_url'], wait_until='domcontentloaded', timeout=60_000)
                    await page.wait_for_timeout(3_000)
                    if not response and not same_product_page:
                        raise RuntimeError('product page returned no response')
                    record['product_http_status'] = response.status if response else 200
                    if record['product_http_status'] in (403, 429):
                        record['status'] = 'blocked'
                        record['error'] = f"product page HTTP {record['product_http_status']}"
                        with checkpoint.open('a', encoding='utf-8') as handle:
                            handle.write(json.dumps(record, ensure_ascii=False) + '\n')
                        raise RuntimeError(record['error'])
                    buttons = page.locator('a.product-download-button')
                    button_count = await buttons.count()
                    if button_count == 0:
                        raise RuntimeError('download button not found')
                    product_buttons = []
                    for button_index in range(button_count):
                        href = await buttons.nth(button_index).get_attribute('href')
                        if href and 'cfsecure/download' in href and f'df={product_id}' in href:
                            product_buttons.append(button_index)
                    if not product_buttons:
                        raise RuntimeError('product download href missing or unexpected')
                    record['part_count'] = len(product_buttons)
                    product_dir = products_base / f'rank-{rank:03d}-{product_id}' / 'source_assets'
                    product_dir.mkdir(parents=True, exist_ok=True)
                    progress_path = product_dir / 'download_progress.json'
                    part_receipts = []
                    if progress_path.exists():
                        try:
                            saved = json.loads(progress_path.read_text(encoding='utf-8'))
                            part_receipts = [part for part in saved.get('parts', []) if Path(part.get('local_path', '')).is_file() and int(part.get('bytes', 0)) > 0]
                        except Exception:
                            part_receipts = []
                    for part_number, button_index in enumerate(product_buttons, 1):
                        existing = next((part for part in part_receipts if int(part.get('part', 0)) == part_number), None)
                        if existing:
                            print(f'SKIP_EXISTING_PART rank={rank} part={part_number}/{len(product_buttons)} bytes={existing["bytes"]}', flush=True)
                            continue
                        free_bytes = shutil.disk_usage(out).free
                        if free_bytes < 2 * 1024**3:
                            raise RuntimeError(f'disk safety stop: only {free_bytes} bytes free')
                        await page.goto(row['canonical_product_url'], wait_until='domcontentloaded', timeout=60_000)
                        await page.wait_for_timeout(3_000)
                        buttons = page.locator('a.product-download-button')
                        href = await buttons.nth(button_index).get_attribute('href')
                        if not href or 'cfsecure/download' not in href:
                            raise RuntimeError(f'part {part_number} download href missing')
                        download_started = time.time()
                        try:
                            await page_cdp.send('Page.navigate', {'url': href})
                        except Exception as exc:
                            raise RuntimeError(f'CDP download navigation failed on part {part_number}: {exc}') from exc
                        source_path = None
                        for _ in range(180):
                            candidates = [candidate for candidate in download_dir.glob('*') if candidate.is_file() and not candidate.name.endswith('.crdownload') and candidate.stat().st_size > 0 and candidate.stat().st_mtime >= download_started - 2]
                            if candidates:
                                source_path = max(candidates, key=lambda candidate: candidate.stat().st_mtime)
                                break
                            await asyncio.sleep(1)
                        if source_path is None:
                            raise RuntimeError(f'part {part_number} browser download did not produce a file')
                        filename = safe_name(source_path.name)
                        if not filename.lower().endswith('.zip'):
                            filename = safe_name(f"{row['title']}-{product_id}-part-{part_number}") + '.zip'
                        zip_path = product_dir / filename
                        shutil.copy2(source_path, zip_path)
                        if source_path.parent == download_dir:
                            source_path.unlink(missing_ok=True)
                        if not zip_path.exists() or zip_path.stat().st_size == 0:
                            raise RuntimeError(f'part {part_number} downloaded as empty file')
                        with zip_path.open('rb') as handle:
                            prefix = handle.read(4)
                        if prefix != b'PK\x03\x04':
                            raise RuntimeError(f'part {part_number} is not a ZIP signature: {prefix.hex()}')
                        size = zip_path.stat().st_size
                        sha256 = hashlib.sha256()
                        with zip_path.open('rb') as handle:
                            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                                sha256.update(cast(bytes, chunk))
                        part_receipts.append({'part': part_number, 'local_path': str(zip_path), 'filename': filename, 'sha256': sha256.hexdigest(), 'bytes': size})
                        progress_path.write_text(json.dumps({'rank': rank, 'product_id': product_id, 'part_count': len(product_buttons), 'parts': part_receipts}, ensure_ascii=False, indent=2), encoding='utf-8')
                        print(f'DOWNLOADED_PART rank={rank} part={part_number}/{len(product_buttons)} bytes={size}', flush=True)
                        await asyncio.sleep(2)
                    total_bytes = sum(int(part['bytes']) for part in part_receipts)
                    receipt = {
                        'status': 'downloaded_valid_zip',
                        'rank': rank,
                        'product_id': product_id,
                        'title': row['title'],
                        'source_url': row['canonical_product_url'],
                        'local_paths': [part['local_path'] for part in part_receipts],
                        'parts': part_receipts,
                        'bytes': total_bytes,
                        'download_method': 'playwright_expect_download',
                        'downloaded_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                        'license_status': 'pending_not_checked',
                        'customer_deliverable': False,
                    }
                    receipt_path = product_dir / 'download_receipt.json'
                    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
                    done[rank] = receipt
                    with checkpoint.open('a', encoding='utf-8') as handle:
                        handle.write(json.dumps(receipt, ensure_ascii=False) + '\n')
                    write_summary(summary_path, rows, done, checkpoint)
                    print(f'DOWNLOADED rank={rank} product={product_id} parts={len(part_receipts)} bytes={total_bytes}', flush=True)
                except Exception as exc:
                    print(f'ERROR rank={rank} product={product_id}: {exc}', flush=True)
                    write_summary(summary_path, rows, done, checkpoint)
                    raise
                await asyncio.sleep(delay)
                if pause_every and offset % pause_every == 0 and offset < len(pending):
                    print(f'RATE_LIMIT_PAUSE seconds={pause_seconds}', flush=True)
                    await asyncio.sleep(pause_seconds)
        finally:
            await page.close()
            # Do not close the connected browser/profile.
            write_summary(summary_path, rows, done, checkpoint)
    print(json.dumps({'target': len(rows), 'downloaded_valid_zip': len(done), 'pending': len(rows)-len(done), 'summary': str(summary_path)}, ensure_ascii=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUT)
    parser.add_argument('--cdp', default=DEFAULT_CDP)
    parser.add_argument('--delay', type=float, default=5.0)
    parser.add_argument('--pause-every', type=int, default=5)
    parser.add_argument('--pause-seconds', type=float, default=60.0)
    parser.add_argument('--max-new', type=int, default=0)
    parser.add_argument('--min-rank', type=int, default=1)
    parser.add_argument('--products-root', type=Path, default=None)
    args = parser.parse_args()
    asyncio.run(run(args.manifest, args.output_dir, args.cdp, args.delay, args.pause_every, args.pause_seconds, args.max_new, args.min_rank, args.products_root))


if __name__ == '__main__':
    main()
