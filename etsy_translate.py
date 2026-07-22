"""
etsy_translate.py
─────────────────
1. Đọc listings từ CSV (TITLE + DESCRIPTION)
2. Dùng MLX AI dịch sang các ngôn ngữ được chọn
3. Playwright crawl Etsy để lấy Listing IDs (match theo title)
4. Playwright điền translation vào từng listing
"""
import asyncio, json, csv, re, time, urllib.request
from pathlib import Path
from playwright.async_api import async_playwright

# ─── Stealth JS: ẩn webdriver flag để bypass bot detection ──────────────────────
STEALTH_JS = """
() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    delete window.__playwright;
    delete window.__pw_manual;
}
"""

# ─── Config ────────────────────────────────────────────────────────────────────
CSV_PATH       = "/Users/aaronnguyen/Downloads/EtsyListingsDownload.csv"
CACHE_FILE     = Path("/Users/aaronnguyen/Documents/Claude/Projects/Etsy/translations_cache.json")
CHROME_EXE     = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_PROFILE = "/Users/aaronnguyen/Library/Application Support/Google/Chrome/Default"
MLX_URL        = "http://localhost:8000"

# Tất cả 9 ngôn ngữ Etsy hỗ trợ
TARGET_LANGS = ["Dutch", "French", "German", "Italian", "Japanese", "Polish", "Portuguese", "Russian", "Spanish"]

# ─── CSV Reader ─────────────────────────────────────────────────────────────────
def read_csv(path: str) -> list[dict]:
    listings = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            title = (row.get("TITLE") or "").strip()
            desc  = (row.get("DESCRIPTION") or "").strip()
            if title:
                listings.append({"title": title, "description": desc})
    return listings

# ─── MLX Translation ────────────────────────────────────────────────────────────
def translate_mlx(text: str, lang: str, mode: str = "title") -> dict:
    """Dịch title hoặc description sang ngôn ngữ target."""
    if mode == "title":
        prompt = (
            f"Translate this Etsy product title to {lang}.\n"
            f"Rules:\n"
            f"- Keep pipe separators ( | ) between keyword phrases.\n"
            f"- MAXIMUM 140 characters total — the translation MUST fit within 140 characters. If needed, shorten phrases but keep all keywords.\n"
            f"- Do NOT cut mid-word or mid-phrase.\n\n"
            f"English title: {text}\n\n"
            f"Return ONLY a JSON object: {{\"title\": \"translated title here\"}}"
        )
        max_tokens = 250
    else:  # description
        prompt = (
            f"Translate this Etsy product description to {lang}.\n"
            f"IMPORTANT: Preserve ALL newline characters (\\n) exactly as in the original — keep blank lines between paragraphs.\n"
            f"Keep all emojis (✨📌💡🔗✔️⚡❌), bullet points (•), and formatting structure.\n"
            f"Keep store links (https://...) unchanged.\n\n"
            f"English description:\n{text}\n\n"
            f"Return ONLY a JSON object: {{\"description\": \"translated description here\"}}"
        )
        max_tokens = 2000

    payload = json.dumps({
        "model": "default",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "enable_thinking": False,
    }).encode()
    req = urllib.request.Request(
        f"{MLX_URL}/v1/chat/completions", data=payload, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    content = data["choices"][0]["message"]["content"]
    s = content.find("{"); e = content.rfind("}") + 1
    return json.loads(content[s:e])


def build_translations(listings: list[dict]) -> dict:
    """
    Dịch title + description cho tất cả listings.
    Cache: {title_key: {lang: {"title": ..., "description": ...}}}
    """
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    total = len(listings) * len(TARGET_LANGS) * 2  # title + desc
    done  = 0

    for listing in listings:
        key  = listing["title"][:80]
        # Truncate description to ~600 words for faster translation
        desc_short = " ".join(listing["description"].split()[:600])
        if key not in cache:
            cache[key] = {}

        for lang in TARGET_LANGS:
            if lang not in cache[key]:
                cache[key][lang] = {}

            # ── Title
            if "title" not in cache[key][lang]:
                done += 1
                print(f"  [{done}/{total}] 🏷  {lang} title: {listing['title'][:45]}...")
                try:
                    r = translate_mlx(listing["title"], lang, mode="title")
                    cache[key][lang]["title"] = r.get("title", "")[:140]
                    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
                    time.sleep(0.3)
                except Exception as ex:
                    print(f"    ❌ Title error: {ex}")
                    cache[key][lang]["title"] = ""
            else:
                done += 1

            # ── Description
            if "description" not in cache[key][lang]:
                done += 1
                print(f"  [{done}/{total}] 📝 {lang} desc:  {listing['title'][:45]}...")
                try:
                    r = translate_mlx(desc_short, lang, mode="description")
                    cache[key][lang]["description"] = r.get("description", "")
                    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
                    time.sleep(0.3)
                except Exception as ex:
                    print(f"    ❌ Desc error: {ex}")
                    cache[key][lang]["description"] = ""
            else:
                done += 1

    print(f"  ✅ Translation cache saved → {CACHE_FILE}")
    return cache

# ─── Playwright: Get Listing IDs ────────────────────────────────────────────────
async def crawl_listing_ids(page) -> dict:
    """
    Crawl Etsy listings bằng regex trên HTML thô.
    URL đúng: /your/shops/me/tools/listings
    Pattern đúng: /stats/listings/{ID}
    """
    id_map   = {}
    MAX_PAGES = 1  # Anh chỉ có 1 trang listings
    page_num  = 1

    print(f"\n🔍 Đang quét listing IDs từ Etsy Shop Manager...")

    while page_num <= MAX_PAGES:
        if page_num == 1:
            url = "https://www.etsy.com/your/shops/me/tools/listings"
        else:
            url = f"https://www.etsy.com/your/shops/me/tools/listings?page={page_num}"

        print(f"  📄 Trang {page_num}: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2500)

        html = await page.content()

        # Pattern đúng: /stats/listings/XXXXXXXXX
        found_ids = re.findall(r'/stats/listings/(\d{7,12})', html)
        found_ids = list(dict.fromkeys(found_ids))  # dedupe

        if not found_ids:
            print(f"  ⛔ Trang {page_num}: không có listing nào → dừng.")
            break

        print(f"  ✅ Tìm thấy {len(found_ids)} listings ở trang {page_num}")

        # Lấy title từ HTML — tìm trong các thẻ gần listing ID
        for lid in found_ids:
            if lid in id_map:
                continue
            # Tìm title từ DOM
            title = ""
            try:
                el = page.locator(f'[href*="/stats/listings/{lid}"]').first
                if await el.count() > 0:
                    title = await el.evaluate("""el => {
                        const card = el.closest('li') || el.closest('tr') || el.closest('[data-listing-id]') || el.parentElement?.parentElement;
                        if (!card) return '';
                        const h = card.querySelector('h3,h2,p[class*=title],[class*=title] p,strong');
                        return h ? h.textContent.trim() : '';
                    }""")
            except Exception:
                pass
            id_map[lid] = title.strip()
            print(f"    #{lid}: {title[:65] or '(title chưa lấy được)'}")

        page_num += 1

    print(f"\n  ✅ Tổng cộng: {len(id_map)} listings trên Etsy")
    return id_map

# ─── Match CSV listings → Listing IDs ──────────────────────────────────────────
def match_listings(csv_listings: list[dict], etsy_id_map: dict) -> list[dict]:
    """
    Ghép CSV title với Etsy listing ID theo fuzzy match.
    """
    matched = []
    def normalize(s): return re.sub(r"[^a-z0-9]", "", s.lower())

    for listing in csv_listings:
        csv_norm = normalize(listing["title"][:60])
        best_id, best_score = None, 0
        for lid, etsy_title in etsy_id_map.items():
            etsy_norm = normalize(etsy_title[:60])
            # Simple: count matching bigrams
            score = sum(1 for i in range(len(csv_norm)-1)
                        if csv_norm[i:i+2] in etsy_norm)
            if score > best_score:
                best_score = score
                best_id = lid
        matched.append({
            **listing,
            "listing_id": best_id,
            "match_score": best_score,
        })
    return matched

# ─── Playwright: Fill Translation ───────────────────────────────────────────────
async def fill_translation(page, listing_id: str, lang: str, translated_title: str, translated_desc: str = "", translated_tags: list = None) -> bool:
    """Điền translation title + description + tags cho 1 listing 1 ngôn ngữ."""
    if not translated_title.strip():
        print(f"    ⚠ Không có bản dịch {lang}, bỏ qua")
        return False

    # URL đúng: /your/shops/me/listing-editor/{id}
    edit_url = f"https://www.etsy.com/your/shops/me/listing-editor/{listing_id}"
    await page.add_init_script(STEALTH_JS)
    await page.set_extra_http_headers({
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
    })
    # Auto-accept "Leave page?" dialog
    page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
    try:
        await page.goto(edit_url, wait_until="networkidle", timeout=45000)
    except Exception:
        try:
            await page.goto(edit_url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
    await page.wait_for_timeout(3000)

    # Check trang có load đúng không
    body_text = await page.locator('body').inner_text()
    if "not found" in body_text.lower() or "uh oh" in body_text.lower():
        print(f"    ❌ Trang lỗi (not found) — bỏ qua listing #{listing_id}")
        return False

    # Scroll xuống để load Translations section (lazy render)
    for _ in range(15):
        await page.keyboard.press("PageDown")
        await page.wait_for_timeout(350)
    await page.wait_for_timeout(2000)

    # ── Tìm section {lang} Tags (vd: "Dutch Tags", "French Tags") ──
    # Đây là heading của từng language section trong editor mới
    lang_section = page.get_by_text(f"{lang} Tags", exact=True).first
    if await lang_section.count() == 0:
        lang_section = page.get_by_text("Translations", exact=True).first
    if await lang_section.count() == 0:
        print(f"    ⚠ Không tìm thấy section '{lang} Tags' (listing #{listing_id})")
        return False

    # Scroll đến section ngôn ngữ bằng JS (không timeout)
    await page.evaluate(f"""
        () => {{
            const els = [...document.querySelectorAll('h4, h3, h2, legend, strong')];
            const el = els.find(e => e.textContent.trim() === '{lang} Tags');
            if (el) el.scrollIntoView({{behavior: 'smooth', block: 'center'}});
        }}
    """)
    await page.wait_for_timeout(1000)

    async def fill_by_heading(heading_text: str, value: str) -> bool:
        """
        Tìm heading rồi fill input/textarea theo đúng cấu trúc DOM Etsy:
        - Label → next sibling TEXTAREA (Title)
        - Label → skip 1 sibling → TEXTAREA (Description)
        """
        try:
            field = await page.evaluate_handle(f"""
                () => {{
                    // Tìm label/legend với text chính xác
                    const all = [...document.querySelectorAll('label, legend, span, div, p')];
                    const hd = all.find(el =>
                        el.children.length === 0 &&
                        el.textContent.trim() === '{heading_text}'
                    );
                    if (!hd) return null;

                    // Thử next siblings trực tiếp (textarea/input)
                    let sib = hd.nextElementSibling;
                    for (let i = 0; i < 10 && sib; i++) {{
                        if (sib.tagName === 'TEXTAREA') return sib;
                        if (sib.tagName === 'INPUT' && sib.type !== 'hidden') return sib;
                        const ta = sib.querySelector('textarea');
                        if (ta) return ta;
                        const inp = sib.querySelector('input[type="text"]');
                        if (inp) return inp;
                        sib = sib.nextElementSibling;
                    }}

                    // Thử parent's next siblings
                    const parent = hd.parentElement;
                    if (parent) {{
                        let psib = parent.nextElementSibling;
                        for (let i = 0; i < 8 && psib; i++) {{
                            const ta = psib.querySelector('textarea');
                            if (ta) return ta;
                            const inp = psib.querySelector('input[type="text"]');
                            if (inp) return inp;
                            psib = psib.nextElementSibling;
                        }}
                    }}
                    return null;
                }}
            """)
            if not field or await page.evaluate("el => el === null", field):
                return False
            await field.scroll_into_view_if_needed()
            await page.wait_for_timeout(300)
            await field.click()
            await field.fill(value)
            return True
        except Exception as e:
            return False

    # ── Fill {lang} Title ──
    ok_title = await fill_by_heading(f"{lang} Title", translated_title[:140])
    if ok_title:
        print(f"    ✅ {lang} title: {translated_title[:55]}")
    else:
        print(f"    ⚠ Không tìm thấy '{lang} Title' ({lang})")

    # ── Fill {lang} Description ──
    if translated_desc.strip():
        ok_desc = await fill_by_heading(f"{lang} Description", translated_desc)
        if ok_desc:
            print(f"    ✅ {lang} desc filled ({len(translated_desc)} chars)")
        else:
            print(f"    ⚠ Không tìm thấy '{lang} Description' ({lang})")
    # ── Fill {lang} Tags ──
    if translated_tags:
        try:
            tags_filled = 0
            for tag in translated_tags[:13]:
                tag = tag.strip()[:20]
                if not tag: continue
                # Theo DOM: LEGEND 'Dutch Tags' → parent FIELDSET → sibling DIV có input
                tag_input = await page.evaluate_handle(f"""
                    () => {{
                        // Tìm legend/label có text '{lang} Tags'
                        const all = [...document.querySelectorAll('legend, label, span, div, p')];
                        const hd = all.find(el =>
                            el.children.length === 0 &&
                            el.textContent.trim() === '{lang} Tags'
                        );
                        if (!hd) return null;

                        // Thử next siblings trực tiếp
                        let sib = hd.nextElementSibling;
                        for (let i = 0; i < 8 && sib; i++) {{
                            if (sib.tagName === 'INPUT' && sib.type !== 'hidden') return sib;
                            const inp = sib.querySelector('input[type="text"]');
                            if (inp) return inp;
                            sib = sib.nextElementSibling;
                        }}

                        // Thử parent next siblings
                        const parent = hd.parentElement;
                        if (parent) {{
                            let psib = parent.nextElementSibling;
                            for (let i = 0; i < 8 && psib; i++) {{
                                const inp = psib.querySelector('input[type="text"]');
                                if (inp) return inp;
                                psib = psib.nextElementSibling;
                            }}
                        }}
                        return null;
                    }}
                """)
                if tag_input and not await page.evaluate("el => el === null", tag_input):
                    await tag_input.scroll_into_view_if_needed()
                    await tag_input.click()
                    await tag_input.fill(tag)
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(400)
                    tags_filled += 1
            print(f"    ✅ {lang} tags: {tags_filled}/{len(translated_tags)} filled")
        except Exception as e:
            print(f"    ⚠ Tags error: {e}")



    # Thử các selector theo thứ tự ưu tiên
    save_selectors = [
        '#locale-overlay-save',
        'button[name="save"]',
        'button:has-text("Publish changes")',
        'button:has-text("Save and continue")',
        'button:has-text("Publish")',
        'button:has-text("Save")',
    ]
    saved = False
    for sel in save_selectors:
        btn = page.locator(sel).last
        if await btn.count() > 0:
            try:
                await btn.scroll_into_view_if_needed()
                await page.wait_for_timeout(500)
                await btn.click(timeout=10000, force=True)
                await page.wait_for_timeout(3000)
                print(f"    💾 Saved!")
                saved = True
                break
            except Exception:
                continue
    if not saved:
        print(f"    ⚠ Không click được Save button — kiểm tra thủ công")

    return True

# ─── Main ───────────────────────────────────────────────────────────────────────
async def main():
    print("=" * 60)
    print("🌍 Etsy Translation Automator")
    print("=" * 60)

    # 1) Đọc CSV
    listings = read_csv(CSV_PATH)
    print(f"\n📋 Đọc được {len(listings)} sản phẩm từ CSV")

    # 2) Generate translations
    print(f"\n🤖 Generating translations: {', '.join(TARGET_LANGS)}")
    cache = build_translations(listings)

    # 3) Playwright — kết nối vào Chrome debug đang chạy (port 9222)
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            print(f"✅ Kết nối Chrome debug thành công (port 9222) — {len(ctx.pages)} tab đang mở")

            # Tìm tab Etsy đã login (có etsy.com trong URL, không phải signin)
            page = None
            for p in ctx.pages:
                url = p.url
                if "etsy.com" in url and "signin" not in url and "login" not in url:
                    page = p
                    print(f"  ✅ Dùng tab: {url[:80]}")
                    break

            # Nếu không tìm thấy tab Etsy sẵn, mở tab mới
            if page is None:
                page = await ctx.new_page()
                print("  ℹ️  Mở tab mới và navigate đến Etsy...")

        except Exception as e:
            print(f"❌ Không kết nối được Chrome debug: {e}")
            print("👉 Hãy chạy file MỞ_CHROME_DEBUG.command trước!")
            return

        # 4) Crawl listing IDs từ Etsy
        etsy_id_map = await crawl_listing_ids(page)

        # 5) Match CSV → IDs
        matched = match_listings(listings, etsy_id_map)
        print(f"\n🔗 Match results:")
        for m in matched:
            status = f"✅ #{m['listing_id']}" if m["listing_id"] else "❌ không match"
            print(f"  {status} (score={m['match_score']}) — {m['title'][:55]}")

        # 6) Fill translations
        print(f"\n✏️ Bắt đầu điền translations...")
        for i, listing in enumerate(matched):
            lid = listing.get("listing_id")
            if not lid:
                print(f"\n[{i+1}/{len(matched)}] ⚠ Bỏ qua (không match): {listing['title'][:50]}")
                continue

            cache_key = listing["title"][:80]
            trans = cache.get(cache_key, {})

            print(f"\n[{i+1}/{len(matched)}] 📝 #{lid}: {listing['title'][:50]}")
            for lang in TARGET_LANGS:
                t_data = trans.get(lang, {})
                t_title = t_data.get("title", "") if isinstance(t_data, dict) else str(t_data)
                t_desc  = t_data.get("description", "") if isinstance(t_data, dict) else ""
                t_tags  = t_data.get("tags", []) if isinstance(t_data, dict) else []
                if t_title:
                    try:
                        await fill_translation(page, lid, lang, t_title, t_desc, t_tags)
                    except Exception as e:
                        print(f"    ❌ Lỗi {lang}: {e}")
                    await asyncio.sleep(1)

        print("\n✅ Hoàn thành! (Chrome vẫn mở để anh kiểm tra)")

if __name__ == "__main__":
    asyncio.run(main())
