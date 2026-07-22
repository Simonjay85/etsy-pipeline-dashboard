"""
Quét listings thiếu English tags, generate bằng MLX, điền vào Etsy.
"""
import asyncio, csv, json, re, urllib.request
from pathlib import Path
from playwright.async_api import async_playwright

CSV_PATH = Path("/Users/aaronnguyen/Downloads/EtsyListingsDownload.csv")
MLX_URL  = "http://localhost:8000"
TAGS_CACHE = Path("/Users/aaronnguyen/Documents/Claude/Projects/Etsy/english_tags_cache.json")

import sys
sys.path.insert(0, '/Users/aaronnguyen/Documents/Claude/Projects/Etsy')
from etsy_translate import crawl_listing_ids, match_listings, STEALTH_JS


# ── MLX helper ────────────────────────────────────────────────────────
def call_mlx(prompt: str, max_tokens=300) -> str:
    payload = json.dumps({
        "model": "default",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "enable_thinking": False,
    }).encode()
    req = urllib.request.Request(
        f"{MLX_URL}/v1/chat/completions", data=payload, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())["choices"][0]["message"]["content"]

def generate_tags(title: str, existing: list, need: int) -> list:
    """Generate thêm 'need' tags SEO cho Etsy dựa vào title + tags hiện có."""
    existing_str = ", ".join(existing) if existing else "none"
    prompt = (
        f"Generate exactly {need} additional Etsy SEO tags for this product.\n"
        f"Product title: {title}\n"
        f"Existing tags: {existing_str}\n\n"
        f"Rules:\n"
        f"- Each tag must be ≤20 characters\n"
        f"- Do NOT repeat existing tags\n"
        f"- Use relevant keywords buyers would search\n"
        f"- Return ONLY JSON: {{\"tags\": [\"tag1\", \"tag2\", ...]}}"
    )
    content = call_mlx(prompt, max_tokens=200)
    try:
        s = content.find("{"); e = content.rfind("}") + 1
        tags = json.loads(content[s:e]).get("tags", [])
        return [t.strip()[:20] for t in tags if t.strip()][:need]
    except Exception:
        # Fallback: extract quoted strings
        return [m[:20] for m in re.findall(r'"([^"]+)"', content) if len(m) <= 20][:need]

# ── Đọc CSV ────────────────────────────────────────────────────────────
listings = []
with open(CSV_PATH, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        title = row.get("TITLE", "").strip()
        if not title: continue
        tags = [t.strip().replace("_", " ") for t in re.split(r"[,|]", row.get("TAGS", "")) if t.strip()]
        listings.append({"title": title, "tags": tags, "listing_id": row.get("LISTING ID", "").strip()})

# ── Load/save tags cache ───────────────────────────────────────────────
tags_cache = json.loads(TAGS_CACHE.read_text()) if TAGS_CACHE.exists() else {}

# ── Generate missing tags ──────────────────────────────────────────────
print("=" * 60)
print("🏷️  Generate missing English tags")
print("=" * 60)

needs_fill = []
for lst in listings:
    key = lst["title"][:80]
    current_tags = lst["tags"].copy()
    missing = 13 - len(current_tags)

    if missing <= 0:
        continue

    # Check cache
    cached_extra = tags_cache.get(key, [])
    if cached_extra:
        all_tags = (current_tags + cached_extra)[:13]
        print(f"  ✅ (cached) {lst['title'][:45]} → +{len(cached_extra)} tags")
    else:
        print(f"  🤖 Generating {missing} tags for: {lst['title'][:45]}")
        try:
            extra = generate_tags(lst["title"], current_tags, missing)
            tags_cache[key] = extra
            TAGS_CACHE.write_text(json.dumps(tags_cache, ensure_ascii=False, indent=2))
            all_tags = (current_tags + extra)[:13]
            print(f"     → {extra}")
        except Exception as ex:
            print(f"     ❌ {ex}")
            continue

    needs_fill.append({**lst, "all_tags": all_tags, "extra_tags": tags_cache.get(key, [])})

print(f"\n📋 {len(needs_fill)} listings cần fill thêm tags vào Etsy")

# ── Playwright: fill English tags ─────────────────────────────────────
async def fill_english_tags(page, listing_id: str, extra_tags: list, title: str):
    """Điền extra tags vào English Tags field của listing."""
    edit_url = f"https://www.etsy.com/your/shops/me/listing-editor/{listing_id}"
    await page.add_init_script(STEALTH_JS)

    try:
        await page.goto(edit_url, wait_until="networkidle", timeout=45000)
    except Exception:
        try:
            await page.goto(edit_url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
    await page.wait_for_timeout(3000)

    # Scroll để load
    for _ in range(8):
        await page.keyboard.press("PageDown")
        await page.wait_for_timeout(300)
    await page.wait_for_timeout(1500)

    filled = 0
    for tag in extra_tags:
        tag = tag.strip()[:20]
        if not tag: continue
        try:
            # Tìm English Tags input (label text = "Tags")
            tag_input = await page.evaluate_handle("""
                () => {
                    // Tìm section "Tags" (English, không phải Dutch Tags etc.)
                    const labels = [...document.querySelectorAll('legend, label')];
                    const lbl = labels.find(el =>
                        el.children.length === 0 &&
                        el.textContent.trim() === 'Tags'
                    );
                    if (!lbl) return null;
                    let sib = lbl.nextElementSibling;
                    for (let i = 0; i < 8 && sib; i++) {
                        const inp = sib.querySelector('input[type="text"]');
                        if (inp) return inp;
                        if (sib.tagName === 'INPUT') return sib;
                        sib = sib.nextElementSibling;
                    }
                    const parent = lbl.parentElement;
                    if (parent) {
                        let psib = parent.nextElementSibling;
                        for (let i = 0; i < 6 && psib; i++) {
                            const inp = psib.querySelector('input[type="text"]');
                            if (inp) return inp;
                            psib = psib.nextElementSibling;
                        }
                    }
                    return null;
                }
            """)
            if tag_input and not await page.evaluate("el => el === null", tag_input):
                await tag_input.scroll_into_view_if_needed()
                await tag_input.click()
                await tag_input.fill(tag)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(400)
                filled += 1
            else:
                print(f"    ⚠ Không tìm được Tags input")
                break
        except Exception as ex:
            print(f"    ❌ Tag '{tag}': {ex}")

    print(f"    ✅ {filled}/{len(extra_tags)} English tags added: {extra_tags}")

    # Save
    for sel in ['button:has-text("Publish changes")', 'button:has-text("Publish")', 'button:has-text("Save and continue")', 'button:has-text("Save")', 'button[name="save"]']:
        btn = page.locator(sel).last
        if await btn.count() > 0:
            try:
                # Nếu có thanh sticky header/footer che mất nút, click force=True
                await btn.click(timeout=8000, force=True)
                await page.wait_for_timeout(2000)
                print(f"    💾 Saved (Publish changes)!")
                break
            except Exception:
                continue

async def main():
    if not needs_fill:
        print("\n✅ Tất cả listings đã đủ 13 tags!")
        return

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if "etsy.com" in p.url), None)
        if not page:
            page = await ctx.new_page()

        async def safe_accept_dialog(d):
            try:
                await d.accept()
            except Exception:
                pass
        # Handle dialogs once for the whole page
        page.on("dialog", lambda d: asyncio.ensure_future(safe_accept_dialog(d)))

        # Scrape listing IDs từ Etsy Shop Manager
        print("\n🔍 Đang quét listing IDs từ Etsy Shop Manager...")
        etsy_listings = await crawl_listing_ids(page)
        print(f"  ✅ Tìm thấy {len(etsy_listings)} listings\n")

        # Match CSV listings với Etsy IDs
        matched = match_listings(listings, etsy_listings)
        id_map = {m["title"][:80]: m.get("listing_id","") for m in matched}

        print(f"\n✏️  Điền English tags vào {len(needs_fill)} listings...\n")
        filled_count = 0
        for i, lst in enumerate(needs_fill):
            lid = id_map.get(lst["title"][:80], "")
            if not lid:
                print(f"[{i+1}] ⚠ Không match được listing_id: {lst['title'][:50]}")
                continue
            print(f"[{i+1}/{len(needs_fill)}] #{lid}: {lst['title'][:50]}")
            try:
                await fill_english_tags(page, lid, lst["extra_tags"], lst["title"])
                filled_count += 1
            except Exception as ex:
                print(f"    ❌ Lỗi: {ex}")
            await asyncio.sleep(1.5)

    print(f"\n✅ Hoàn thành! Đã fill {filled_count}/{len(needs_fill)} listings")


asyncio.run(main())
