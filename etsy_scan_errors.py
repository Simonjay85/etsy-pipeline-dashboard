import asyncio, sys, re, json
from playwright.async_api import async_playwright

LANGUAGES = ["Dutch", "French", "German", "Italian", "Japanese", "Polish", "Portuguese", "Russian", "Spanish"]

async def get_all_listing_ids(page) -> list[dict]:
    listings = []
    seen_ids = set()
    page_num = 1
    while True:
        url = f"https://www.etsy.com/your/shops/me/tools/listings?ref=listings_manager_prototype&sort_order=custom&page={page_num}"
        print(json.dumps({"type": "info", "msg": f"Đang quét trang listings #{page_num}..."}), flush=True)
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        hrefs = await page.evaluate("() => Array.from(document.querySelectorAll('a')).map(a => a.href).filter(href => href && (href.includes('/listing-editor/edit/') || href.includes('listing_id=')))")
        found = 0
        for href in hrefs:
            m = re.search(r'/listing-editor/edit/(\d+)', href) or re.search(r'listing_id=(\d+)', href)
            if m:
                lid = m.group(1)
                if lid not in seen_ids:
                    seen_ids.add(lid)
                    listings.append({"id": lid})
                    found += 1
        if found == 0: break
        next_btn = page.locator('[aria-label*="Next"], a[rel="next"], button:has-text("Next page")').first
        if await next_btn.count() > 0 and await next_btn.is_visible():
            page_num += 1
        else:
            break
    return listings

async def scan_listing(page, listing_id: str):
    edit_url = f"https://www.etsy.com/your/shops/me/listing-editor/edit/{listing_id}"
    try:
        await page.goto(edit_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2500)
    except Exception as e:
        return {"id": listing_id, "error": f"Không load được: {e}"}

    # title
    title = ""
    for sel in ['textarea[name="title"]', 'input[name="title"]', '#title-input']:
        el = page.locator(sel).first
        if await el.count() > 0:
            title = (await el.input_value()).strip()
            if title: break
            
    if not title:
        return {"id": listing_id, "error": "Không đọc được title"}

    # tags count
    tags_count = 0
    try:
        raw_tags = await page.evaluate(r'''() => {
            let tagSection = null;
            let allEls = Array.from(document.querySelectorAll("legend, label, h2, h3, p, span"));
            for (let el of allEls) {
                if ((el.innerText || "").trim() === "Tags") {
                    tagSection = el.closest("fieldset") || el.parentElement;
                    break;
                }
            }
            if (!tagSection) return [];
            let seen = new Set();
            let count = 0;
            for (let el of Array.from(tagSection.querySelectorAll("button, span, div, li"))) {
                let txt = (el.innerText || "").replace(/[×✕]/g, "").trim();
                if (txt.length >= 2 && txt.length <= 40 && !txt.includes("\n") && /[a-z]/i.test(txt) && txt.split(" ").length <= 5 && !seen.has(txt.toLowerCase()) && !["Add", "Tags", "Remove", "Add tag", "used", "left", "Add up to", "Shape, color"].some(k => txt.toLowerCase().startsWith(k.toLowerCase()))) {
                    seen.add(txt.toLowerCase());
                    count++;
                }
            }
            return count;
        }''')
        tags_count = int(raw_tags)
    except: pass

    # check languages
    missing_langs = []
    for trans_sel in ['text="Translations"', '[id*="translations"]', 'h2:has-text("Translation")']:
        try:
            el = page.locator(trans_sel).first
            if await el.count() > 0:
                await el.scroll_into_view_if_needed()
                await page.wait_for_timeout(500)
                break
        except: pass

    for lang in LANGUAGES:
        found = False
        for sel in [f'button:has-text("{lang}")', f'[role="tab"]:has-text("{lang}")', f'li:has-text("{lang}")']:
            if await page.locator(sel).count() > 0:
                found = True
                break
        if not found:
            missing_langs.append(lang)

    errors = []
    if missing_langs:
        errors.append(f"Thiếu tab ngôn ngữ: {', '.join(missing_langs)}")
    if tags_count < 13:
        errors.append(f"Thiếu tags (hiện có {tags_count}/13)")
        
    return {"id": listing_id, "title": title[:40]+"...", "errors": errors}

async def main():
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = None
            for p in ctx.pages:
                if "etsy.com" in p.url: page = p; break
            if page is None: page = await ctx.new_page()
            
            listings = await get_all_listing_ids(page)
            print(json.dumps({"type": "info", "msg": f"Đã tìm thấy {len(listings)} listings. Bắt đầu quét lỗi..."}), flush=True)
            
            for index, l in enumerate(listings, 1):
                print(json.dumps({"type": "info", "msg": f"[{index}/{len(listings)}] Đang quét {l['id']}..."}), flush=True)
                res = await scan_listing(page, l['id'])
                if res.get("error"):
                    print(json.dumps({"type": "error", "id": res['id'], "msg": res["error"]}), flush=True)
                elif res.get("errors"):
                    print(json.dumps({"type": "found", "id": res['id'], "title": res['title'], "errors": res['errors']}), flush=True)
                else:
                    print(json.dumps({"type": "ok", "id": res['id'], "msg": "Tốt"}), flush=True)
                
            print(json.dumps({"type": "done", "msg": "Hoàn tất quét!"}), flush=True)
                
        except Exception as e:
            print(json.dumps({"type": "fatal", "msg": str(e)}), flush=True)

if __name__ == "__main__":
    asyncio.run(main())
