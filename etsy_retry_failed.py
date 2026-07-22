"""
etsy_retry_failed.py — Chạy lại các listing bị lỗi / thiếu tag
───────────────────────────────────────────────────────────────
Chỉnh FAILED_IDS nếu cần, rồi chạy:
  cd "/Users/aaronnguyen/Documents/Claude/Projects/Etsy"
  .venv/bin/python3 etsy_retry_failed.py
"""

import asyncio
import sys
from playwright.async_api import async_playwright

# ── Nhúng toàn bộ logic từ etsy_translate_existing ──────────────────────────────
sys.argv = ["etsy_translate_existing.py"]
import importlib.util, pathlib

spec = importlib.util.spec_from_file_location(
    "etsy_main",
    pathlib.Path(__file__).parent / "etsy_translate_existing.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# ── Danh sách listing ID cần retry ─────────────────────────────────────────────
# Ghi chú lý do để dễ theo dõi
FAILED = [
    # Tags thiếu (không phải lỗi save)
    ("4431904444", "Japanese 11/13, Spanish 10/13"),   # [5]
    ("4434273252", "Italian 0 tags, Spanish 5/13"),     # [6]
    ("4432249360", "Dutch 6/13"),                       # [7]
    ("4431897220", "Dutch 6/13"),                       # [8]
    ("4431879249", "Dutch 6/13"),                       # [9]
    ("4431348276", "Dutch 6/13"),                       # [10]
    ("4431331976", "Dutch 6/13"),                       # [11]
    ("4421467312", "German 2/13, Portuguese 6/13"),     # [12]
    ("4431238317", "German 12/13, Portuguese 8/13"),    # [13]
    ("4431208297", "French 1/13, no save"),             # [14]
    ("4428012935", "Italian 6/13, some tabs missing"),  # [20]
    ("4420005648", "German 11, Polish 10, Russian timeout, Spanish 6"), # [21]
    ("4483791495", "French 1/13"),                      # [24]
    ("4418856394", "Italian 1/13, Russian 6/13"),       # [26]
    ("4447326615", "Crashed at Polish (browser closed)"), # [27]
    # Listings 15-18 (4430854483, 4424146199, 4424136723, 4419825830)
    # Đã đủ 13 tags mọi ngôn ngữ — chỉ cần lưu lại title/desc nếu chưa có
]

FAILED_IDS = [lid for lid, _ in FAILED]

async def main():
    print("🔁 etsy_retry_failed.py")
    print(f"   Sẽ retry: {len(FAILED_IDS)} listings")
    for lid, reason in FAILED:
        print(f"   • {lid}: {reason}")
    print("=" * 60)

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            print("✅ Kết nối Chrome debug (port 9222)")

            page = None
            for p in ctx.pages:
                if "etsy.com" in p.url and "signin" not in p.url:
                    page = p
                    break
            if page is None:
                page = await ctx.new_page()

            page.set_default_timeout(30000)

            async def _handle_dialog(dialog):
                try:
                    await dialog.accept()
                except:
                    pass
            page.on("dialog", _handle_dialog)

        except Exception as e:
            print(f"❌ Không kết nối được Chrome: {e}")
            print("👉 Hãy mở Chrome debug trước!")
            return

        ok_count = 0
        still_failed = []

        async def get_active_page():
            """Lấy page Etsy đang mở, hoặc mở page mới nếu cần."""
            for p in ctx.pages:
                if not p.is_closed() and "etsy.com" in p.url and "signin" not in p.url:
                    return p
            # Không có page Etsy nào → mở mới
            p = await ctx.new_page()
            await p.goto("https://www.etsy.com/your/shops/me/listings")
            await asyncio.sleep(3)
            return p

        active_page = await get_active_page()
        active_page.set_default_timeout(30000)
        async def _handle_dialog(dialog):
            try:
                await dialog.accept()
            except:
                pass
        active_page.on("dialog", _handle_dialog)

        for i, listing_id in enumerate(FAILED_IDS, 1):
            reason = dict(FAILED).get(listing_id, "")
            print(f"\n[{i}/{len(FAILED_IDS)}] 📝 {listing_id}  ({reason})")

            # Reconnect nếu page bị đóng
            if active_page.is_closed():
                print("  ⚠ Page bị đóng, đang reconnect...")
                active_page = await get_active_page()
                active_page.set_default_timeout(30000)
                active_page.on("dialog", _handle_dialog)

            try:
                result = await mod.translate_listing(active_page, listing_id, i, len(FAILED_IDS))
                if result:
                    ok_count += 1
                else:
                    still_failed.append(listing_id)
            except Exception as e:
                print(f"  ❌ Lỗi: {e}")
                still_failed.append(listing_id)

            # Dùng asyncio.sleep thay vì page.wait_for_timeout để tránh TargetClosedError
            await asyncio.sleep(8)

        print("\n" + "=" * 60)
        print(f"🎉 HOÀN THÀNH RETRY!")
        print(f"   ✅ Thành công: {ok_count}/{len(FAILED_IDS)}")
        if still_failed:
            print(f"   ❌ Vẫn lỗi : {still_failed}")
        print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
