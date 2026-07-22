"""Check an Etsy listing for missing images and optionally upload local images.

Usage:
  ./check_and_fix_images.py --listing 4528906889 --images ./shops/daisyflowdigital/product-01/images/*.jpg

This script connects to an existing Chrome debugging session at
http://localhost:9222 (Playwright CDP). It opens the Etsy listing editor,
checks the number of images, and if none found and --images provided it will
attempt to upload them using the page file chooser.

It is conservative: if no images are provided it will only report findings and
print instructions to fix manually.
"""
import argparse
import asyncio
import glob
import json
import os
from pathlib import Path

from playwright.async_api import async_playwright


async def count_images_on_page(page) -> int:
    # Try several selectors used across Etsy listing editor variants
    selectors = [
        'div[data-testid*="photos"] img',
        '[data-testid*="photo"] img',
        'figure img',
        'ul li img',
        '.photo-list img',
        '.image-list img',
        'img[src*="/il/"]',
    ]
    for sel in selectors:
        try:
            n = await page.evaluate(f"() => (document.querySelectorAll('{sel}')||[]).length")
            if n and n > 0:
                return int(n)
        except Exception:
            pass
    # Fallback: try to detect placeholders / empty gallery area
    try:
        empty_text = await page.evaluate("() => Array.from(document.querySelectorAll('div, p, span')).map(e=>e.innerText||'').find(t=>/add photos|add images|upload images/i.test(t)) || ''")
        if empty_text:
            return 0
    except Exception:
        pass
    return 0


async def try_set_files_by_click(page, click_selector: str, files: list[Path]) -> bool:
    try:
        btn = page.locator(click_selector).first
        if await btn.count() == 0 or not await btn.is_visible():
            return False
        async with page.expect_file_chooser(timeout=10000) as fc_info:
            await btn.click()
        fc = await fc_info.value
        await fc.set_files([str(p) for p in files])
        return True
    except Exception:
        return False


async def try_set_files_by_input(page, files: list[Path]) -> bool:
    try:
        inputs = page.locator('input[type="file"]')
        if await inputs.count() == 0:
            return False
        # Prefer first visible input
        for i in range(await inputs.count()):
            inp = inputs.nth(i)
            try:
                await inp.set_input_files([str(p) for p in files], timeout=15000)
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def upload_images(page, image_paths: list[Path]) -> bool:
    # Try clicking common buttons/labels that open file chooser
    click_selectors = [
        'button:has-text("Upload images or videos")',
        'button:has-text("Add photos")',
        'button:has-text("Add images")',
        'label:has-text("Computer files")',
        'label:has-text("Upload")',
        'button:has-text("Add more")',
    ]
    for sel in click_selectors:
        ok = await try_set_files_by_click(page, sel, image_paths)
        if ok:
            await page.wait_for_timeout(3000)
            return True

    # Fallback: set input[type=file]
    if await try_set_files_by_input(page, image_paths):
        await page.wait_for_timeout(3000)
        return True

    return False


async def check_and_fix(listing_id: str, images: list[Path], cdp: str):
    edit_url = f"https://www.etsy.com/your/shops/me/listing-editor/edit/{listing_id}"
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp)
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = None
        for p in ctx.pages:
            if 'etsy.com' in p.url:
                page = p
                break
        if page is None:
            page = await ctx.new_page()

        await page.goto(edit_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2500)

        title = await page.evaluate("() => (document.querySelector('textarea[name=\"title\"]')||document.querySelector('input[name=\"title\"]')||{value:''}).value || ''")
        title = (title or '').strip()
        print(json.dumps({"id": listing_id, "title": title}, ensure_ascii=False))

        img_count = await count_images_on_page(page)
        print(json.dumps({"id": listing_id, "images_found": img_count}, ensure_ascii=False))

        if img_count > 0:
            print(json.dumps({"id": listing_id, "status": "ok", "msg": f"Có {img_count} ảnh, không cần upload."}, ensure_ascii=False))
            return

        # No images found
        if not images:
            print(json.dumps({"id": listing_id, "status": "missing_images", "msg": "Listing không có ảnh. Không có file ảnh được cung cấp; vui lòng upload thủ công hoặc chạy với --images."}, ensure_ascii=False))
            return

        # Attempt upload
        print(json.dumps({"id": listing_id, "status": "uploading", "msg": f"Đang upload {len(images)} file..."}, ensure_ascii=False))
        ok = await upload_images(page, images)
        if not ok:
            print(json.dumps({"id": listing_id, "status": "upload_failed", "msg": "Không tìm thấy vùng upload tự động. Thử upload thủ công."}, ensure_ascii=False))
            return

        # wait and re-check
        await page.wait_for_timeout(5000)
        new_count = await count_images_on_page(page)
        if new_count > 0:
            print(json.dumps({"id": listing_id, "status": "fixed", "images_now": new_count}, ensure_ascii=False))
        else:
            print(json.dumps({"id": listing_id, "status": "still_missing", "msg": "Upload dường như không thành công."}, ensure_ascii=False))


def expand_image_paths(images_arg: str) -> list[Path]:
    if not images_arg:
        return []
    parts = images_arg.split(',')
    paths = []
    for p in parts:
        p = os.path.expanduser(p.strip())
        # glob support
        matched = glob.glob(p)
        if matched:
            for m in matched:
                paths.append(Path(m))
        else:
            paths.append(Path(p))
    # filter to existing files
    paths = [p for p in paths if p.exists() and p.is_file()]
    return paths


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listing", required=True, help="Listing id to check")
    parser.add_argument("--images", default="", help="Comma-separated glob or file paths to upload")
    parser.add_argument("--cdp", default="http://localhost:9222", help="CDP endpoint for Chrome")
    args = parser.parse_args()

    images = expand_image_paths(args.images)

    try:
        asyncio.run(check_and_fix(args.listing, images, args.cdp))
    except Exception as e:
        print(json.dumps({"type": "fatal", "msg": str(e)}))


if __name__ == "__main__":
    main()
