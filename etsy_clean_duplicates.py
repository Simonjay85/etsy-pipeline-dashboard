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
import asyncio
import argparse
import json
import re
import sys
import os
from pathlib import Path
from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).parent
CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shop", required=False, default="templystudios")
    parser.add_argument("--listing-id", action="append", default=[])
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

async def main():
    args = parse_args()
    explicit_ids = list(dict.fromkeys(str(value) for value in args.listing_id))
    if any(not value.isdigit() for value in explicit_ids):
        raise RuntimeError("Listing ID phải là số")
    browser_dir = browser_dir_for_shop(args.shop)
    print(f"\n{'='*60}")
    print(f"  🧹 ETSY DRAFTS DUPLICATE CLEANER")
    print(f"  🤖 Tự động tìm và xoá sản phẩm nháp trùng lặp trên Etsy")
    print(f"{'='*60}\n")

    # Clear locks
    browser_dir.mkdir(exist_ok=True)
    for lf in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        try: (browser_dir / lf).unlink(missing_ok=True)
        except: pass

    async with async_playwright() as pw:
        # Thử kết nối cổng debug trước (nếu người dùng đang mở Chrome debug)
        browser = None
        ctx = None
        try:
            print("⏳ Đang thử kết nối tới Chrome đang mở (cổng 9222)...")
            browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
            ctx = browser.contexts[0]
            print("✅ Đã kết nối thành công tới Chrome đang mở!")
        except Exception:
            print("ℹ️ Chrome debug cổng 9222 không mở. Đang khởi chạy Chrome session mới...")
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

        # 1. Đi tới trang nháp
        print("▶ Điều hướng tới trang Drafts trên Etsy...")
        await page.goto("https://www.etsy.com/your/shops/me/tools/listings?status=draft", wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)

        if "signin" in page.url or "join" in page.url:
            print("❌ Chưa đăng nhập Etsy! Vui lòng đăng nhập trên trình duyệt trước.")
            await ctx.close()
            sys.exit(1)

        await verify_shop_identity(page, args.shop)

        # Chủ động click chuyển sang tab Drafts (Etsy mặc định load Active)
        print("⏳ Đang chuyển sang tab Drafts và kiểm tra...")
        draft_selected = False
        for attempt in range(3):
            # Kiểm tra xem đã ở tab Draft chưa bằng cách check radio button #status-draft
            is_checked = await page.evaluate('() => { let el = document.querySelector("#status-draft"); return el ? el.checked : false; }')
            if is_checked:
                print("✅ Đã xác nhận đang ở tab Drafts!")
                draft_selected = True
                break
                
            print(f"🔄 Lần thử {attempt + 1}: Thử click chuyển sang tab Drafts...")
            try:
                # Tìm label có thuộc tính for="status-draft"
                draft_label = page.locator('label[for="status-draft"]').first
                if await draft_label.count() > 0:
                    await draft_label.click(force=True)
                else:
                    # Thử click input trực tiếp
                    draft_input = page.locator('#status-draft, input[value="draft"]').first
                    await draft_input.click(force=True)
            except Exception as e:
                print(f"  ⚠ Lỗi khi click Draft filter: {e}")
                
            await page.wait_for_timeout(4000)
            
        # Thử kiểm tra lại lần cuối
        is_checked = await page.evaluate('() => { let el = document.querySelector("#status-draft"); return el ? el.checked : false; }')
        if not is_checked:
            # Nếu vẫn không chuyển được, kiểm tra xem có đang ở trang active không
            is_active_checked = await page.evaluate('() => { let el = document.querySelector("#status-active"); return el ? el.checked : false; }')
            if is_active_checked or "status=draft" not in page.url:
                print("❌ CẢNH BÁO NGUY HIỂM: Trình duyệt đang mở tab 'Active' (Đang hoạt động) chứ không phải 'Draft' (Bản nháp)!")
                print("❌ Để bảo vệ dữ liệu, script sẽ TỰ ĐỘNG DỪNG và không xóa bất kỳ sản phẩm nào.")
                print("💡 Giải pháp: Vui lòng tự click chọn tab 'Draft' trên màn hình Chrome đang mở, sau đó chạy lại script.")
                await ctx.close()
                sys.exit(1)
        else:
            print("▶ Đã ở tab Drafts! Chờ 6 giây để trang ổn định...")
            await page.wait_for_timeout(6000)

        # 2. Quét các listing nháp và gom nhóm trùng tiêu đề
        print("▶ Đang quét danh sách bản nháp để tìm sản phẩm trùng lặp...")
        listings = await page.evaluate(r'''() => {
            let itemsMap = {};
            let anchors = Array.from(document.querySelectorAll('a[href*="/listing-editor/edit/"]'));
            for (let a of anchors) {
                let href = a.getAttribute('href') || '';
                let match = href.match(/\/listing-editor\/edit\/(\d+)/);
                if (match) {
                    let id = match[1];
                    let text = a.innerText.trim();
                    
                    // Nếu là anchor card-body chính chứa toàn bộ thông tin card
                    if (text && a.className.includes('card-body')) {
                        let lines = text.split('\n').map(l => l.trim()).filter(l => l);
                        let title = "";
                        for (let line of lines) {
                            if (line === "Digital" || line === "Video" || line === "Select this listing" || line.includes("in stock") || line.includes("Auto-renews") || line.includes("Updated on")) {
                                continue;
                            }
                            title = line;
                            break;
                        }
                        if (title) {
                            itemsMap[id] = { id: id, title: title };
                        }
                    }
                }
            }
            return Object.values(itemsMap);
        }''')

        if not listings:
            print("📭 Không tìm thấy sản phẩm nháp nào trên trang hiện tại.")
            await ctx.close()
            if explicit_ids:
                raise RuntimeError("Không tìm thấy draft đã chọn; không có listing nào bị xoá")
            return

        # Loại bỏ các listing ID bị lặp lại (nếu có)
        unique_listings = []
        seen_ids = set()
        for item in listings:
            if item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                unique_listings.append(item)

        print(f"📊 Tổng số bản nháp duy nhất tìm thấy: {len(unique_listings)}")
        for idx, item in enumerate(unique_listings, 1):
            print(f"  {idx}. ID: {item['id']} | Title: {item['title'][:60]}...")

        # Explicit IDs are authoritative for the dashboard bulk-delete path.
        def clean_t(t):
            return "".join(c for c in str(t).lower() if c.isalnum())

        seen_titles = {}
        to_delete_ids = []
        to_delete_names = []
        if explicit_ids:
            visible = {str(item["id"]): item for item in unique_listings}
            missing = [listing_id for listing_id in explicit_ids if listing_id not in visible]
            if missing:
                raise RuntimeError(f"Không tìm thấy draft đã chọn trên trang Etsy: {', '.join(missing)}")
            to_delete_ids = explicit_ids
            to_delete_names = [visible[listing_id]["title"] for listing_id in explicit_ids]
        else:
            for item in unique_listings:
                cleaned = clean_t(item["title"])
                if cleaned in seen_titles:
                    to_delete_ids.append(item["id"])
                    to_delete_names.append(item["title"])
                else:
                    seen_titles[cleaned] = item["id"]

        if not to_delete_ids:
            print("🎉 Tuyệt vời! Không phát hiện sản phẩm nháp nào bị trùng lặp tiêu đề.")
            await ctx.close()
            return

        print(f"\n⚠️ Phát hiện {len(to_delete_ids)} bản nháp bị trùng lặp:")
        for idx, name in enumerate(to_delete_names, 1):
            print(f"  {idx}. {name}")

        # 3. Tích chọn các bản nháp trùng
        print("\n▶ Đang tự động tích chọn (checkbox) các bản nháp trùng...")
        selected_count = 0
        for lid in to_delete_ids:
            # Click checkbox của listing đó thông qua evaluate
            clicked = await page.evaluate('''async (id) => {
                let anchor = document.querySelector(`a[href*="/listing-editor/edit/${id}"]`);
                if (anchor) {
                    let card = anchor.parentElement.closest('[class*="card"]') || anchor.closest('tr') || anchor.closest('[class*="item"]') || anchor.parentElement.parentElement;
                    if (card) {
                        let cb = card.querySelector('input[type="checkbox"]') || card.querySelector('[role="checkbox"]');
                        if (cb) {
                            if (!cb.checked) {
                                cb.click();
                            }
                            return cb.checked;
                        }
                    }
                }
                return false;
            }''', lid)
            if clicked:
                selected_count += 1
                await page.wait_for_timeout(300)

        print(f"✓ Đã tích chọn thành công {selected_count}/{len(to_delete_ids)} checkbox trùng.")

        if selected_count != len(to_delete_ids):
            print(f"❌ Chỉ chọn được {selected_count}/{len(to_delete_ids)}. Dừng để tránh xoá một phần.")
            await ctx.close()
            raise RuntimeError("Không chọn đủ toàn bộ listing; không có lệnh xoá nào được gửi")

        # 4. Click nút Xóa ở top/bottom bulk action bar (phải lọc lấy nút visible)
        print("\n▶ Đang kích hoạt lệnh Xóa trên Etsy...")
        delete_btn = page.locator('clg-button[data-action="delete"]:visible, clg-button:has-text("Delete"):visible, button:has-text("Delete"):visible, [class*="bulk"] clg-button:has-text("Delete"):visible').first
        await delete_btn.scroll_into_view_if_needed()
        await delete_btn.click()
        await page.wait_for_timeout(3000)

        # 5. Xác nhận xóa trong Modal xác nhận của Etsy
        print("▶ Đang xác nhận xóa vĩnh viễn sản phẩm trùng...")
        # Đợi 2 giây cho modal dialog xuất hiện hoàn toàn
        await page.wait_for_timeout(2500)
        
        confirm_btn = page.locator(
            'div[role="dialog"] clg-button:has-text("Delete"):visible, '
            'div[role="dialog"] button:has-text("Delete"):visible, '
            '[class*="modal"] clg-button:has-text("Delete"):visible, '
            '[class*="modal"] button:has-text("Delete"):visible, '
            '[class*="dialog"] clg-button:has-text("Delete"):visible, '
            '[class*="dialog"] button:has-text("Delete"):visible'
        ).first
        
        if await confirm_btn.count() > 0:
            print("✅ Đã tìm thấy nút xác nhận Xóa trong modal. Đang click...")
            await confirm_btn.click(force=True)
            print("⏳ Đang đợi Etsy xử lý lệnh xóa...")
            await page.wait_for_timeout(6000)
            print(f"🎉 [THÀNH CÔNG] Đã dọn dẹp sạch sẽ {selected_count} sản phẩm trùng lặp trên Etsy!")
            print(json.dumps({"ok": True, "shop": args.shop, "deleted_listing_ids": to_delete_ids}, ensure_ascii=False))
        else:
            await ctx.close()
            raise RuntimeError("Không tìm được nút xác nhận Delete; chưa xác nhận xoá")

        await ctx.close()

if __name__ == "__main__":
    asyncio.run(main())
