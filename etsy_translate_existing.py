"""
etsy_translate_existing.py
──────────────────────────
Tự động thêm translations (9 ngôn ngữ) vào TẤT CẢ listings đang có trên Etsy shop.

Cách dùng:
    python3 etsy_translate_existing.py            # dịch hết
    python3 etsy_translate_existing.py --skip 5   # bỏ qua 5 listing đầu
    python3 etsy_translate_existing.py --limit 10 # chỉ dịch 10 listing
"""

import asyncio, sys, subprocess, argparse, re
from pathlib import Path

# ── Auto-install ───────────────────────────────────────────────────────────────
def ensure_deps():
    pkgs = {"playwright": "playwright", "deep_translator": "deep-translator"}
    for mod, pkg in pkgs.items():
        try:
            __import__(mod)
        except ImportError:
            print(f"▶ Cài {pkg}...")
            subprocess.run([sys.executable, "-m", "pip", "install", pkg, "--quiet"], check=True)

ensure_deps()

from playwright.async_api import async_playwright
from deep_translator import GoogleTranslator

BASE_DIR    = Path(__file__).parent
BROWSER_DIR = BASE_DIR / ".browser-session"
CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

LANGUAGES = [
    ("nl", "Dutch",      0),
    ("fr", "French",     1),
    ("de", "German",     2),
    ("it", "Italian",    3),
    ("ja", "Japanese",   4),
    ("pl", "Polish",     5),
    ("pt", "Portuguese", 6),
    ("ru", "Russian",    7),
    ("es", "Spanish",    8),
]

# ── Helpers ────────────────────────────────────────────────────────────────────
def translate_text(text: str, lang: str, max_chars=4800) -> str:
    if not text or not text.strip():
        return text
    try:
        if len(text) <= max_chars:
            return GoogleTranslator(source="en", target=lang).translate(text) or text
            
        # Chia theo dòng để không bị cắt ngang từ
        lines = text.split('\n')
        parts = []
        current_chunk = ""
        for line in lines:
            if len(current_chunk) + len(line) + 1 > max_chars:
                parts.append(GoogleTranslator(source="en", target=lang).translate(current_chunk) or current_chunk)
                current_chunk = line + "\n"
            else:
                current_chunk += line + "\n"
                
        if current_chunk.strip():
            parts.append(GoogleTranslator(source="en", target=lang).translate(current_chunk) or current_chunk)
            
        return "".join(parts).strip()
    except Exception as e:
        print(f"    ⚠ translate {lang}: {e}")
        return text

def translate_tag(tag: str, lang: str) -> str:
    try:
        t = (GoogleTranslator(source="en", target=lang).translate(tag) or tag).strip()
        if len(t) > 20:
            t = t[:20].rsplit(" ", 1)[0].rstrip(",- ")
        return t if 1 <= len(t) <= 20 else tag
    except:
        return tag

def trim_title(title: str, max_len=140) -> str:
    """Cắt title tối đa max_len ký tự, không cắt giữa từ và dọn dẹp ký tự đặc biệt."""
    import re, html
    
    # Giải mã thực thể HTML (ví dụ: &amp; -> &, &quot; -> ", etc.)
    title = html.unescape(title)
    
    # Helper to replace subsequent occurrences of a character
    def replace_subsequent(s: str, char: str, replacement: str) -> str:
        parts = s.split(char)
        if len(parts) > 2:
            return parts[0] + char + replacement.join(parts[1:])
        return s

    # Giữ lại tối đa 1 ký tự đặc biệt theo quy định của Etsy, các ký tự thừa sẽ được chuyển đổi an toàn
    title = replace_subsequent(title, "&", " and ")
    title = replace_subsequent(title, "%", " percent")
    title = replace_subsequent(title, ":", " -")
    
    # Thu gọn nhiều khoảng trắng liên tiếp
    title = re.sub(r'\s+', ' ', title).strip()

    if len(title) <= max_len:
        return title
    cut = title[:max_len].rsplit(" ", 1)[0].rstrip(",|;- ")
    return cut

async def safe_fill(el, value):
    await el.click(click_count=3)
    await el.press("Backspace")
    await el.fill(value)

# ── Get all listing IDs from shop ──────────────────────────────────────────────
async def get_all_listing_ids(page) -> list[dict]:
    """Lấy toàn bộ listing ID + title từ trang listings manager."""
    listings = []
    seen_ids = set()
    page_num = 1

    while True:
        url = (
            f"https://www.etsy.com/your/shops/me/tools/listings"
            f"?ref=listings_manager_prototype&sort_order=custom&page={page_num}"
        )
        print(f"  📄 Trang listings #{page_num}...")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(4000)

        # Lấy tất cả links trên trang bằng JS để đảm bảo không bị miss (vì lazy load)
        hrefs = await page.evaluate("() => Array.from(document.querySelectorAll('a')).map(a => a.href).filter(href => href && (href.includes('/listing-editor/edit/') || href.includes('listing_id=')))")
        
        found_this_page = 0
        for href in hrefs:
            m = re.search(r'/listing-editor/edit/(\d+)', href) or re.search(r'listing_id=(\d+)', href)
            if m:
                lid = m.group(1)
                if lid not in seen_ids:
                    seen_ids.add(lid)
                    listings.append({"id": lid, "title": f"Listing #{lid}"})
                    found_this_page += 1

        print(f"    → Tìm được {found_this_page} listings (tổng: {len(listings)})")

        if found_this_page == 0:
            break

        # Kiểm tra có trang tiếp không
        next_btn = page.locator('[aria-label*="Next"], a[rel="next"], button:has-text("Next page")').first
        if await next_btn.count() > 0 and await next_btn.is_visible():
            page_num += 1
            await page.wait_for_timeout(1000)
        else:
            break

    return listings

# ── Fill translations for one listing ─────────────────────────────────────────
async def translate_listing(page, listing_id: str, listing_num: int, total: int) -> bool:
    edit_url = f"https://www.etsy.com/your/shops/me/listing-editor/edit/{listing_id}"
    print(f"\n[{listing_num}/{total}] 📝 Listing {listing_id}")
    print(f"  🔗 {edit_url}")

    try:
        await page.goto(edit_url, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(3000)
    except Exception as e:
        print(f"  ❌ Không load được: {e}")
        return False

    # ── Đọc nội dung tiếng Anh ────────────────────────────────────────────────
    title = ""
    for sel in ['textarea[name="title"]', 'input[name="title"]', '#title-input',
                'textarea[id*="title"]', 'input[id*="title"]']:
        el = page.locator(sel).first
        if await el.count() > 0:
            title = (await el.input_value()).strip()
            if title:
                break

    desc = ""
    for sel in ['textarea[name="description"]', '#description-textarea',
                'textarea[id*="description"]']:
        el = page.locator(sel).first
        if await el.count() > 0:
            desc = (await el.input_value()).strip()
            if desc:
                break

    # Lấy tags đang có bằng DOM — tìm đúng section "Tags" heading
    tags = []
    try:
        raw_tags = await page.evaluate(r'''() => {
            let tagSection = null;
            let allEls = Array.from(document.querySelectorAll("legend, label, h2, h3, p, span"));
            for (let el of allEls) {
                let txt = (el.innerText || "").trim();
                if (txt === "Tags") {
                    // Walk up to the fieldset/section wrapper
                    tagSection = el.closest("fieldset") || el.parentElement;
                    break;
                }
            }
            if (!tagSection) return [];
            // Extract all short text nodes inside the section (tag pills)
            let tags = [];
            let seen = new Set();
            let candidates = Array.from(tagSection.querySelectorAll("button, span, div, li"));
            for (let el of candidates) {
                let txt = (el.innerText || "").replace(/[×✕]/g, "").trim();
                // Tag: 2-40 chars, no newline, at least one letter, <= 5 words
                if (
                    txt.length >= 2 && txt.length <= 40 &&
                    !txt.includes("\n") &&
                    /[a-z]/i.test(txt) &&
                    txt.split(" ").length <= 5 &&
                    !seen.has(txt.toLowerCase()) &&
                    !["Add", "Tags", "Remove", "Add tag", "used", "left", "Add up to", "Shape, color"].some(k => txt.toLowerCase().startsWith(k.toLowerCase()))
                ) {
                    seen.add(txt.toLowerCase());
                    tags.push(txt);
                }
            }
            return tags;
        }''')
        tags = [t for t in raw_tags if t][:13]
    except:
        pass
    
    # Fallback: parse body text looking for the comma-delimited English tag list
    if not tags:
        try:
            body_text = await page.evaluate("() => document.body.innerText")
            best_line = None
            best_count = 0
            for line in body_text.split('\n'):
                if ',' in line:
                    tokens = [t.strip() for t in line.split(',') if t.strip()]
                    if len(tokens) >= 5 and all(2 <= len(t) <= 35 for t in tokens):
                        if len(tokens) > best_count:
                            best_count = len(tokens)
                            best_line = tokens
            if best_line:
                tags = best_line[:13]
        except:
            pass

    if not title:
        print(f"  ⚠ Không đọc được title — bỏ qua")
        return False

    print(f"  📌 {title[:70]}...")
    print(f"  🏷  {len(tags)} tags, desc={len(desc)} ký tự")

    # ── Scroll đến phần Translations ──────────────────────────────────────────
    trans_found = False
    for trans_sel in ['text="Translations"', '[id*="translations"]', 'h2:has-text("Translation")']:
        try:
            el = page.locator(trans_sel).first
            if await el.count() > 0:
                await el.scroll_into_view_if_needed()
                await page.wait_for_timeout(1000)
                trans_found = True
                break
        except:
            pass

    if not trans_found:
        print(f"  ⚠ Không tìm thấy phần Translations trên trang này")
        # Thử scroll xuống cuối
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1500)

    # ── Điền từng ngôn ngữ ────────────────────────────────────────────────────
    langs_done = 0
    for lang_code, lang_name, idx in LANGUAGES:
        try:
            # Click tab ngôn ngữ
            clicked = False
            for sel in [
                f'button:has-text("{lang_name}")',
                f'[role="tab"]:has-text("{lang_name}")',
                f'li:has-text("{lang_name}")',
                f'[data-language="{lang_code}"]',
            ]:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.scroll_into_view_if_needed()
                    await el.click()
                    await page.wait_for_timeout(2500)
                    clicked = True
                    break

            if not clicked:
                print(f"    ⚠ {lang_name}: không thấy tab")
                continue

            # Dịch
            trans_title = trim_title(translate_text(title, lang_code))
            trans_desc  = translate_text(desc, lang_code) if desc else ""

            # Điền title
            filled_title = False
            for sel in [
                f'textarea[name="translations.{idx}.title"]',
                f'#field-translations-{idx}-title-input',
                f'textarea[id*="translations"][id*="{idx}"][id*="title"]',
                f'textarea[placeholder*="{lang_name}" i][id*="title"]',
            ]:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await safe_fill(el, trans_title)
                    await page.wait_for_timeout(1500)
                    filled_title = True
                    break

            # Điền description
            if trans_desc:
                for sel in [
                    f'textarea[name="translations.{idx}.description"]',
                    f'#listing-{lang_code}-translation-description-textarea',
                    f'textarea[id*="translations"][id*="{idx}"][id*="description"]',
                    f'textarea[placeholder*="{lang_name}" i][id*="description"]',
                ]:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        try:
                            await el.scroll_into_view_if_needed()
                            await page.wait_for_timeout(500)
                        except:
                            pass
                        if await el.is_visible():
                            await safe_fill(el, trans_desc)
                            await page.wait_for_timeout(1500)
                            break
                        else:
                            # Force-fill qua JS khi element không visible
                            try:
                                await page.evaluate(
                                    """([sel, val]) => {
                                        const el = document.querySelector(sel);
                                        if (el) {
                                            el.value = val;
                                            el.dispatchEvent(new Event('input', {bubbles: true}));
                                            el.dispatchEvent(new Event('change', {bubbles: true}));
                                        }
                                    }""",
                                    [sel, trans_desc]
                                )
                                await page.wait_for_timeout(1000)
                                break
                            except:
                                pass

            # Điền tags
            tags_filled = 0
            if tags:
                try:
                    # Tìm ô input tag theo ID chính xác trước, fallback dần
                    tag_input = None
                    for t_sel in [
                        f'[id="listing-translations.{idx}.tags-input"]',
                        f'input[name="translations.{idx}.tags"]',
                        f'input[aria-describedby="translations.{idx}.tags-helper"]',
                    ]:
                        el = page.locator(t_sel).first
                        if await el.count() > 0:
                            tag_input = el
                            break

                    if tag_input is None:
                        raise Exception("tag input not found")

                    await tag_input.scroll_into_view_if_needed()
                    await page.wait_for_timeout(500)

                    # Đếm tag đã có sẵn (tranạlation tags đã được điền từ lần trước)
                    existing_count_text = await page.evaluate(f'''() => {{
                        let inp = document.querySelector('[id="listing-translations.{idx}.tags-input"]');
                        if (!inp) return "0 left";
                        let wrap = inp.closest("fieldset") || inp.parentElement.parentElement;
                        let txt = wrap ? wrap.innerText : "";
                        let m = txt.match(/(\\d+) left/);
                        return m ? m[0] : "0 left";
                    }}''')
                    slots_left = int((existing_count_text or "0 left").split()[0])

                    if slots_left == 0:
                        # All slots full, nothing to add
                        tags_filled = 13
                    else:
                        for tag in tags[:slots_left]:
                            trans_tag = translate_tag(tag, lang_code)
                            await tag_input.fill(trans_tag)
                            await page.wait_for_timeout(800)
                            await tag_input.press("Enter")
                            await page.wait_for_timeout(800)
                            tags_filled += 1
                except Exception as te:
                    pass  # Tags optional

            status = "✓"
            if not filled_title:
                status = "⚠ title field not found"
            if tags_filled:
                status += f" ({tags_filled} tags)"
            print(f"    🌍 {lang_name} {status}")
            langs_done += 1

        except Exception as e:
            print(f"    ❌ {lang_name}: {e}")

        await page.wait_for_timeout(1500)

    # ── Save listing ──────────────────────────────────────────────────────────
    saved = False
    # Chờ Etsy enable nút Publish (tối đa 12 giây)
    for _ in range(24):  # 24 x 500ms = 12 giây
        is_enabled = await page.evaluate('''
            () => {
                const btn = document.getElementById("shop-manager--listing-publish-edit")
                    || Array.from(document.querySelectorAll("button")).find(b => b.innerText && b.innerText.includes("Publish changes"));
                if (!btn) return "notfound";
                if (btn.disabled || btn.getAttribute("aria-disabled") === "true") return "disabled";
                return "enabled";
            }
        ''')
        if is_enabled == "enabled":
            break
        if is_enabled == "notfound":
            break
        await page.wait_for_timeout(500)

    # Click bằng JS để bypass tất cả vấn đề selector
    clicked = await page.evaluate('''
        () => {
            const btn = document.getElementById("shop-manager--listing-publish-edit")
                || Array.from(document.querySelectorAll("button")).find(b => b.innerText && b.innerText.includes("Publish changes") && !b.disabled);
            if (btn && !btn.disabled) { btn.click(); return true; }
            return false;
        }
    ''')
    if clicked:
        await page.wait_for_timeout(3000)
        saved = True

    # Fallback: Playwright selectors
    if not saved:
        for save_sel in [
            'button[id*="publish"]:not([disabled])',
            'button:has-text("Publish changes")',
            'button:has-text("Save"):not([disabled])',
        ]:
            try:
                btn = page.locator(save_sel).first
                if await btn.count() > 0 and await btn.is_enabled():
                    await btn.scroll_into_view_if_needed()
                    await btn.click()
                    await page.wait_for_timeout(3000)
                    saved = True
                    break
            except:
                pass

    if saved:
        print(f"  ✅ Saved! ({langs_done}/9 ngôn ngữ)")
    else:
        print(f"  ⚠ Không tìm thấy nút Save — anh tự save tay nhé")

    return langs_done > 0

# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip",  type=int, default=0,  help="Bỏ qua N listing đầu")
    parser.add_argument("--limit", type=int, default=999, help="Chỉ dịch N listings")
    args = parser.parse_args()

    print("🚀 etsy_translate_existing.py")
    print(f"   Skip={args.skip} | Limit={args.limit}")
    print("=" * 55)

    BROWSER_DIR.mkdir(exist_ok=True)

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            print(f"✅ Kết nối Chrome debug thành công (port 9222)")
            
            page = None
            for p in ctx.pages:
                if "etsy.com" in p.url and "signin" not in p.url and "login" not in p.url:
                    page = p
                    break
            if page is None:
                page = await ctx.new_page()
                
            page.set_default_timeout(30000)
            
            # Tự động đồng ý nếu có popup "You have unsaved changes"
            async def _handle_dialog(dialog):
                try:
                    await dialog.accept()
                except:
                    pass
            page.on("dialog", _handle_dialog)
            
        except Exception as e:
            print(f"❌ Không kết nối được Chrome debug: {e}")
            print("👉 Hãy chạy file MỞ_CHROME_DEBUG.command trước!")
            return

        # Kiểm tra đăng nhập
        await page.goto("https://www.etsy.com/your/shops/me/tools/listings", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        if "sign_in" in page.url or "signin" in page.url:
            print("❌ Chưa đăng nhập Etsy! Anh đăng nhập vào Chrome rồi chạy lại.")
            await browser.close()
            return

        # Lấy danh sách listings
        print("\n🔍 Đang lấy danh sách listings từ shop...")
        listings = await get_all_listing_ids(page)
        print(f"\n✅ Tổng: {len(listings)} listings trên shop")

        # Apply skip/limit
        listings = listings[args.skip : args.skip + args.limit]
        print(f"▶ Sẽ dịch: {len(listings)} listings\n")

        if not listings:
            print("Không có listing nào để dịch.")
            await browser.close()
            return

        # Dịch từng listing
        success = 0
        failed  = []
        for i, listing in enumerate(listings, 1):
            ok = await translate_listing(page, listing["id"], i, len(listings))
            if ok:
                success += 1
            else:
                failed.append(listing["id"])
            # Delay giữa listings để tránh rate limit
            await asyncio.sleep(10)

        # Tổng kết
        print("\n" + "=" * 55)
        print(f"🎉 HOÀN THÀNH!")
        print(f"   ✅ Dịch thành công: {success}/{len(listings)} listings")
        if failed:
            print(f"   ❌ Thất bại: {failed}")
        print("=" * 55)

if __name__ == "__main__":
    asyncio.run(main())
