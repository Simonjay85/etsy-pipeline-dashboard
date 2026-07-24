#!/usr/bin/env python3
"""
Etsy Drafts Duplicate Cleaner
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Kết nối tới Chrome debug hoặc dùng .browser-session
• Vào trang Drafts trên Etsy
• Phát hiện các listing trùng tiêu đề
• Tự động tích chọn checkbox và thực hiện lệnh Xóa (Delete)
• Chạy: python3 etsy_clean_duplicates.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import argparse
import asyncio
import json
import os
import re
from collections import defaultdict
from pathlib import Path

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).parent
CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
SCRAPE_PAGES = 30
DRAFT_LISTING_URL = "https://www.etsy.com/your/shops/me/tools/listings/page:{page},state:draft"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shop", required=False, default="templystudios")
    parser.add_argument("--listing-id", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true", help="Chỉ quét và in kết quả, không click xoá")
    return parser.parse_args()


def browser_dir_for_shop(shop_id: str) -> Path:
    config_path = BASE_DIR / "shops_config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        configured = str(config.get(shop_id, {}).get("browser_session") or "").strip()
        if configured:
            return Path(os.path.expanduser(configured))
    except (OSError, json.JSONDecodeError):
        pass
    shop_session = BASE_DIR / f".browser-session-{shop_id}"
    if shop_session.exists():
        return shop_session
    return BASE_DIR / ".browser-session" if shop_id == "templystudios" else Path.home() / f".etsy_browser_session_{shop_id}"


def expected_shop_slug(shop_id: str) -> str:
    try:
        config = json.loads((BASE_DIR / "shops_config.json").read_text(encoding="utf-8"))
        url = str(config.get(shop_id, {}).get("etsy_link") or "")
    except (OSError, json.JSONDecodeError):
        return ""
    match = re.search(r"/shop/([^/?#]+)", url, re.I)
    if match:
        return match.group(1).lower()
    host = re.search(r"https?://([^.]+)\.etsy\.com", url, re.I)
    return host.group(1).lower() if host else ""


async def verify_shop_identity(page, shop_id: str) -> None:
    expected = expected_shop_slug(shop_id)
    if not expected:
        raise RuntimeError(f"Shop {shop_id} chưa có Etsy URL hợp lệ")
    slugs = await page.evaluate(r'''() => Array.from(document.querySelectorAll('a[href*="/shop/"]'))
      .map(a => (a.href || '').match(/\/shop\/([^/?#]+)/i)).filter(Boolean).map(m => m[1].toLowerCase())''')
    actual = next((slug for slug in slugs if slug != "me"), "")
    if actual != expected:
        raise RuntimeError(f"Phiên Etsy sai shop: hiện tại={actual or 'không xác định'}, yêu cầu={expected}")


def _safe_listing_map(all_listings: dict[str, dict], selected_ids: list[str]) -> dict[int, list[str]]:
    ids_by_page: dict[int, list[str]] = defaultdict(list)
    for listing_id in selected_ids:
        listing = all_listings.get(listing_id)
        if not listing:
            continue
        ids_by_page[int(listing["page"])].append(listing_id)
    return ids_by_page


def _select_targets(all_listings: dict[str, dict], explicit_ids: list[str]) -> tuple[list[str], list[str], list[str], dict[int, list[str]]]:
    unique_listings = list(all_listings.values())
    missing: list[str] = []
    selected_ids: list[str] = []
    selected_names: list[str] = []
    if explicit_ids:
        present = {item["id"]: item["title"] for item in unique_listings}
        missing = [listing_id for listing_id in explicit_ids if listing_id not in present]
        selected_ids = explicit_ids
        selected_names = [present.get(listing_id, f"Listing {listing_id}") for listing_id in explicit_ids]
    else:
        seen_titles: dict[str, str] = {}
        for item in unique_listings:
            norm = normalize_title(item["title"])
            if norm in seen_titles:
                selected_ids.append(item["id"])
                selected_names.append(item["title"])
            else:
                seen_titles[norm] = item["id"]

    ids_by_page = _safe_listing_map(all_listings, selected_ids)
    return selected_ids, selected_names, missing, ids_by_page


def _build_dry_run_report(shop: str, selected_ids: list[str], selected_names: list[str], missing: list[str], ids_by_page: dict[int, list[str]], discovered: dict[str, dict]) -> dict:
    return {
        "ok": bool(selected_ids) and not missing,
        "dry_run": True,
        "shop": shop,
        "selected_count": len(selected_ids),
        "selected_names": selected_names,
        "selected_ids": selected_ids,
        "missing_ids": missing,
        "by_page": {str(page_num): ids for page_num, ids in sorted(ids_by_page.items())},
        "discovered": discovered,
    }


def normalize_title(value: str) -> str:
    return "".join(c for c in str(value).lower() if c.isalnum())


def draft_listing_url(page_number: int) -> str:
    return DRAFT_LISTING_URL.format(page=page_number)


async def scrape_draft_listings(page, page_number: int) -> list[dict]:
    raw_items = await page.evaluate(
        """({ pageNumber }) => {
            const isNoise = (text) => {
                if (!text) return true;
                const cleaned = text.trim();
                if (!cleaned) return true;
                if (cleaned.length > 220) return true;
                if (/^(digital|video|select this listing)$/i.test(cleaned)) return true;
                if (/(in stock|auto-renews|updated on|views|favorites|edit|preview|copy|delete|stats)/i.test(cleaned)) return true;
                return false;
            };

            const anchors = Array.from(document.querySelectorAll('a[href*="/listing-editor/edit/"]'));
            const out = [];
            const seen = new Set();
            for (const anchor of anchors) {
                const href = anchor.getAttribute('href') || '';
                const match = href.match(/\\/listing-editor\\/edit\\/(\\d+)/);
                if (!match) continue;
                const id = match[1];
                if (seen.has(id)) continue;
                seen.add(id);

                const card = anchor.closest('[class*=\"card\"]') || anchor.closest('tr') || anchor.closest('[class*=\"item\"]') || anchor.parentElement;
                const candidates = [anchor.innerText || ''];

                if (card) {
                    const selectorList = ['h1', 'h2', 'h3', '[class*=\"title\"]', 'a[href*="/listing/"]:not([href*="/listing-editor/"])', 'p', 'span', 'div'];
                    for (const selector of selectorList) {
                        for (const el of Array.from(card.querySelectorAll(selector))) {
                            candidates.push(el.innerText || '');
                        }
                    }
                }

                let title = '';
                for (const raw of candidates) {
                    const text = (raw || '').replace(/\\n+/g, ' ').trim();
                    if (isNoise(text)) continue;
                    title = text;
                    break;
                }
                out.push({id, title: title || `Listing ${id}`, page: pageNumber});
            }
            return out;
        }""",
        {"pageNumber": page_number},
    )
    items = []
    for item in raw_items:
        listing_id = str(item.get("id", "")).strip()
        if not listing_id:
            continue
        items.append({
            "id": listing_id,
            "title": str(item.get("title", f"Listing {listing_id}")).strip(),
            "page": int(item.get("page", page_number)),
        })
    return items


async def verify_absent_after_delete(page, shop_id: str, target_ids: list[str], page_number: int) -> list[str]:
    if not target_ids:
        return []

    current_page = await scrape_draft_listings(page, page_number)
    current_ids = {item["id"] for item in current_page}
    maybe_remaining = [listing_id for listing_id in target_ids if listing_id in current_ids]
    if maybe_remaining:
        print(f"🔎 Trên page:{page_number} vẫn thấy: {', '.join(maybe_remaining)}")
    else:
        print(f"🔎 Trên page:{page_number} không thấy target, đang kiểm tra toàn bộ draft...")

    # Re-check toàn bộ draft để tránh âm bản khi listing chuyển trang ngay sau khi xoá.
    all_listings = await collect_all_draft_listings(page, shop_id)
    return [listing_id for listing_id in target_ids if listing_id in all_listings]


async def collect_all_draft_listings(page, shop_id: str) -> dict[str, dict]:
    all_listings: dict[str, dict] = {}
    for page_number in range(1, SCRAPE_PAGES + 1):
        page_url = draft_listing_url(page_number)
        await page.goto(page_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        await verify_shop_identity(page, shop_id)
        if "signin" in page.url or "join" in page.url:
            raise RuntimeError("Session Etsy không còn đăng nhập hợp lệ (signin/join). Vui lòng đăng nhập rồi chạy lại.")

        listings = await scrape_draft_listings(page, page_number)
        if not listings and page_number > 1:
            break
        before = len(all_listings)
        for item in listings:
            all_listings.setdefault(item["id"], item)
        print(f"📄 Page {page_number}: +{len(listings)} bản nháp (tổng {len(all_listings)})")
        if page_number > 1 and len(all_listings) == before:
            break
    return all_listings


async def select_listing_checkbox(page, listing_id: str) -> bool:
    return await page.evaluate(
        """async (listingId) => {
            const extractListingId = (anchor) => {
                const href = anchor ? String(anchor.getAttribute('href') || '') : '';
                const match = href.match(/\\/listing-editor\\/edit\\/(\\d+)(?=$|[/?#?])/);
                return match ? match[1] : null;
            };

            const targetAnchor = Array.from(document.querySelectorAll('a[href*=\"/listing-editor/edit/\"]'))
                .find((link) => extractListingId(link) === listingId);
            if (!targetAnchor || !targetAnchor.parentElement) return false;

            let cursor = targetAnchor.parentElement;
            while (cursor) {
                const listingIds = new Set();
                const links = Array.from(cursor.querySelectorAll('a[href*=\"/listing-editor/edit/\"]'));
                for (const link of links) {
                    const linkId = extractListingId(link);
                    if (linkId) listingIds.add(linkId);
                }

                if (listingIds.size > 0) {
                    if (listingIds.size !== 1 || !listingIds.has(listingId)) return false;

                    const checkbox = cursor.querySelector('input[type=\"checkbox\"]') || cursor.querySelector('[role=\"checkbox\"]');
                    if (!checkbox) {
                        cursor = cursor.parentElement;
                        continue;
                    }

                    if (checkbox.tagName && checkbox.tagName.toLowerCase() === 'input' && checkbox.type === 'checkbox') {
                        if (!checkbox.checked) checkbox.click();
                        return !!checkbox.checked;
                    }

                    if (checkbox.getAttribute('role') === 'checkbox') {
                        const initial = String(checkbox.getAttribute('aria-checked') || '').toLowerCase() === 'true';
                        if (!initial) checkbox.click();
                        return String(checkbox.getAttribute('aria-checked') || '').toLowerCase() === 'true';
                    }

                    return false;
                }

                cursor = cursor.parentElement;
            }
            return false;
        }""",
        listing_id,
    )


async def click_delete_and_confirm(page) -> None:
    delete_btn = page.locator(
        'clg-button[data-action="delete"]:visible, '
        'clg-button:has-text("Delete"):visible, '
        'button:has-text("Delete"):visible, '
        '[class*="bulk"] clg-button:has-text("Delete"):visible'
    ).first
    await delete_btn.scroll_into_view_if_needed()
    await delete_btn.click()

    confirm_btn = page.locator(
        'div[role="dialog"] clg-button:has-text("Delete"):visible, '
        'div[role="dialog"] button:has-text("Delete"):visible, '
        '[class*="modal"] clg-button:has-text("Delete"):visible, '
        '[class*="modal"] button:has-text("Delete"):visible, '
        '[class*="dialog"] clg-button:has-text("Delete"):visible, '
        '[class*="dialog"] button:has-text("Delete"):visible'
    ).first
    if await confirm_btn.count() == 0:
        raise RuntimeError("Không tìm được nút xác nhận Delete; chưa xác nhận xoá")
    await confirm_btn.click(force=True)


async def execute_deletions(page, shop_id: str, ids_by_page: dict[int, list[str]]) -> list[str]:
    deleted_ids: list[str] = []
    for page_num in sorted(ids_by_page.keys(), reverse=True):
        target_ids = ids_by_page[page_num]
        print(f"\n▶ Đang mở page:{page_num} để xử lý {len(target_ids)} listing...")
        await page.goto(draft_listing_url(page_num), wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        if "signin" in page.url or "join" in page.url:
            raise RuntimeError(f"Session Etsy mất đăng nhập khi xử lý page:{page_num}")
        await verify_shop_identity(page, shop_id)

        current_page = await scrape_draft_listings(page, page_num)
        current_by_id = {item["id"]: item for item in current_page}
        missing_page = [listing_id for listing_id in target_ids if listing_id not in current_by_id]
        if missing_page:
            raise RuntimeError(f"Không tìm thấy listing trên page:{page_num}: {', '.join(missing_page)}")

        selected_count = 0
        for listing_id in target_ids:
            clicked = await select_listing_checkbox(page, listing_id)
            if clicked:
                selected_count += 1
            await page.wait_for_timeout(300)

        print(f"✓ page:{page_num} đã chọn {selected_count}/{len(target_ids)} listing")
        if selected_count != len(target_ids):
            raise RuntimeError(f"Không chọn đủ listing trên page:{page_num}; dừng để tránh xoá một phần")

        print("\n▶ Đang click Delete...")
        await click_delete_and_confirm(page)
        print("⏳ Đang đợi Etsy xử lý lệnh xoá...")
        await page.wait_for_timeout(5000)

        still_present = await verify_absent_after_delete(page, shop_id, target_ids, page_num)
        if still_present:
            raise RuntimeError(
                f"Sau khi xoá page:{page_num}, vẫn còn listing chưa bị xoá: {', '.join(still_present)}"
            )

        deleted_ids.extend(target_ids)

    return deleted_ids


async def main():
    args = parse_args()
    explicit_ids = list(dict.fromkeys(str(value) for value in args.listing_id))
    if any(not value.isdigit() for value in explicit_ids):
        raise RuntimeError("Listing ID phải là số")

    browser_dir = browser_dir_for_shop(args.shop)
    print(f"\n{'=' * 60}")
    print(f"  🧹 ETSY DRAFTS DUPLICATE CLEANER")
    print(f"  🤖 Tự động tìm và xoá sản phẩm nháp trùng lặp trên Etsy")
    if args.dry_run:
        print("  [DRY RUN] Chỉ quét & báo cáo, KHÔNG xoá")
    print(f"{'=' * 60}\n")

    browser_dir.mkdir(exist_ok=True)
    if not args.dry_run:
        for lock_file in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
            try:
                (browser_dir / lock_file).unlink(missing_ok=True)
            except Exception:
                pass

    async with async_playwright() as pw:
        browser = None
        ctx = None
        launched_persistent = False
        try:
            print("⏳ Đang thử kết nối tới Chrome đang mở (cổng 9222)...")
            browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
            ctx = browser.contexts[0]
            print("✅ Đã kết nối thành công tới Chrome đang mở!")
        except Exception:
            print("ℹ️ Chrome debug cổng 9222 không mở. Đang khởi chạy Chrome session mới...")
            launched_persistent = True
            launch_kw = dict(
                user_data_dir=str(browser_dir),
                headless=False,
                args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
                viewport=None,
            )
            if CHROME_PATH.exists():
                launch_kw["executable_path"] = str(CHROME_PATH)
            ctx = await pw.chromium.launch_persistent_context(**launch_kw)

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(40000)

        try:
            print("▶ Điều hướng tới trang Drafts trên Etsy...")
            await page.goto(draft_listing_url(1), wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            if "signin" in page.url or "join" in page.url:
                print("❌ Chưa đăng nhập Etsy! Vui lòng đăng nhập trên trình duyệt trước.")
                raise RuntimeError("Chưa đăng nhập Etsy")

            await verify_shop_identity(page, args.shop)
            print(f"✅ Xác nhận shop đúng: {args.shop}")

            print(f"▶ Đang quét toàn bộ draft listing (tối đa {SCRAPE_PAGES} trang)...")
            all_listings = await collect_all_draft_listings(page, args.shop)
            unique_listings = list(all_listings.values())

            if not unique_listings:
                print("📭 Không tìm thấy sản phẩm nháp nào trên các trang draft.")
                if args.dry_run:
                    print(json.dumps({"ok": True, "dry_run": True, "shop": args.shop, "discovered": {}}, ensure_ascii=False))
                    return
                if explicit_ids:
                    raise RuntimeError("Không tìm thấy draft đã chọn; không có listing nào bị xoá")
                print("🎉 Không có draft để xử lý.")
                return

            print(f"📊 Tổng số bản nháp duy nhất tìm thấy: {len(unique_listings)}")
            for idx, item in enumerate(unique_listings, 1):
                print(f"  {idx}. ID: {item['id']} | Page: {item['page']} | Title: {item['title'][:60]}...")

            selected_ids, selected_names, missing, ids_by_page = _select_targets(all_listings, explicit_ids)

            if args.dry_run:
                print(json.dumps(
                    _build_dry_run_report(args.shop, selected_ids, selected_names, missing, ids_by_page, all_listings),
                    ensure_ascii=False,
                    indent=2,
                ))
                return

            if not selected_ids and not missing:
                print("🎉 Tuyệt vời! Không phát hiện sản phẩm nháp nào bị trùng lặp tiêu đề.")
                return

            if missing:
                raise RuntimeError(f"Không tìm thấy draft đã chọn trên Etsy: {', '.join(missing)}")

            print("⚠️ Lưu ý: xoá trên nhiều trang KHÔNG phải là atomic.")
            print("🧭 Sẽ xử lý từ trang cao xuống thấp để giảm rủi ro trượt phân trang.")
            for page_num in sorted(ids_by_page.keys(), reverse=True):
                print(f"  - page {page_num}: {len(ids_by_page[page_num])} listing")

            deleted_ids = await execute_deletions(page, args.shop, ids_by_page)

            print(f"🎉 [THÀNH CÔNG] Đã dọn dẹp sạch sẽ {len(deleted_ids)} sản phẩm trùng lặp trên Etsy!")
            print(json.dumps({"ok": True, "shop": args.shop, "deleted_listing_ids": deleted_ids}, ensure_ascii=False))
        finally:
            if ctx is not None and launched_persistent:
                await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
