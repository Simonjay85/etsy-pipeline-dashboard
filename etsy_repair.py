import asyncio, sys, argparse, re
from playwright.async_api import async_playwright
from deep_translator import GoogleTranslator

# Simplified structure for repairing a listing
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

def translate_text(text: str, lang: str, max_chars=4800) -> str:
    if not text or not text.strip(): return text
    try:
        if len(text) <= max_chars:
            return GoogleTranslator(source="en", target=lang).translate(text) or text
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
        if len(t) > 20: t = t[:20].rsplit(" ", 1)[0].rstrip(",- ")
        return t if 1 <= len(t) <= 20 else tag
    except: return tag

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

async def repair_listing(page, listing_id: str, fix_tabs: bool, fix_desc: bool, fix_tags: bool):
    edit_url = f"https://www.etsy.com/your/shops/me/listing-editor/edit/{listing_id}"
    print(f"\n🚀 Sửa lỗi Listing {listing_id}")
    try:
        await page.goto(edit_url, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(3000)
    except Exception as e:
        print(f"❌ Không load được: {e}")
        return False

    title = ""
    for sel in ['textarea[name="title"]', 'input[name="title"]', '#title-input', 'textarea[id*="title"]']:
        el = page.locator(sel).first
        if await el.count() > 0:
            title = (await el.input_value()).strip()
            if title: break

    desc = ""
    for sel in ['textarea[name="description"]', '#description-textarea', 'textarea[id*="description"]']:
        el = page.locator(sel).first
        if await el.count() > 0:
            desc = (await el.input_value()).strip()
            if desc: break

    # Extract tags
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
        let tags = [];
        let seen = new Set();
        for (let el of Array.from(tagSection.querySelectorAll("button, span, div, li"))) {
            let txt = (el.innerText || "").replace(/[×✕]/g, "").trim();
            if (txt.length >= 2 && txt.length <= 40 && !txt.includes("\n") && /[a-z]/i.test(txt) && txt.split(" ").length <= 5 && !seen.has(txt.toLowerCase()) && !["Add", "Tags", "Remove", "Add tag", "used", "left", "Add up to", "Shape, color"].some(k => txt.toLowerCase().startsWith(k.toLowerCase()))) {
                seen.add(txt.toLowerCase());
                tags.push(txt);
            }
        }
        return tags;
    }''')
    tags = [t for t in raw_tags if t][:13]

    if not title:
        print("⚠ Không đọc được title — bỏ qua")
        return False

    # Scroll to translations
    for trans_sel in ['text="Translations"', '[id*="translations"]', 'h2:has-text("Translation")']:
        try:
            el = page.locator(trans_sel).first
            if await el.count() > 0:
                await el.scroll_into_view_if_needed()
                await page.wait_for_timeout(1000)
                break
        except: pass

    # Apply fixes
    if fix_desc or fix_tags or fix_tabs:
        for lang_code, lang_name, idx in LANGUAGES:
            try:
                # Click tab
                clicked = False
                for sel in [f'button:has-text("{lang_name}")', f'[role="tab"]:has-text("{lang_name}")', f'li:has-text("{lang_name}")', f'[data-language="{lang_code}"]']:
                    el = page.locator(sel).first
                    if await el.count() > 0 and await el.is_visible():
                        await el.scroll_into_view_if_needed()
                        await el.click()
                        await page.wait_for_timeout(2500)
                        clicked = True
                        break

                if not clicked: continue

                trans_title = trim_title(translate_text(title, lang_code))
                trans_desc  = translate_text(desc, lang_code) if desc else ""

                if fix_tabs:
                    # Title
                    for sel in [f'textarea[name="translations.{idx}.title"]', f'#field-translations-{idx}-title-input', f'textarea[id*="translations"][id*="{idx}"][id*="title"]']:
                        el = page.locator(sel).first
                        if await el.count() > 0 and await el.is_visible():
                            await safe_fill(el, trans_title)
                            await page.wait_for_timeout(1000)
                            break

                if fix_desc or fix_tabs:
                    for sel in [f'textarea[name="translations.{idx}.description"]', f'#listing-{lang_code}-translation-description-textarea', f'textarea[id*="translations"][id*="{idx}"][id*="description"]']:
                        el = page.locator(sel).first
                        if await el.count() > 0:
                            if await el.is_visible():
                                await safe_fill(el, trans_desc)
                            else:
                                await page.evaluate("([sel, val]) => { const el = document.querySelector(sel); if (el) { el.value = val; el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); } }", [sel, trans_desc])
                            await page.wait_for_timeout(1000)
                            break

                if fix_tags or fix_tabs:
                    if tags:
                        try:
                            tag_input = None
                            for t_sel in [f'[id="listing-translations.{idx}.tags-input"]', f'input[name="translations.{idx}.tags"]', f'input[aria-describedby="translations.{idx}.tags-helper"]']:
                                el = page.locator(t_sel).first
                                if await el.count() > 0: tag_input = el; break
                            if tag_input:
                                await tag_input.scroll_into_view_if_needed()
                                await page.wait_for_timeout(500)
                                slots_left = int((await page.evaluate(f'''() => {{ let inp = document.querySelector('[id="listing-translations.{idx}.tags-input"]'); if (!inp) return "0 left"; let wrap = inp.closest("fieldset") || inp.parentElement.parentElement; let m = (wrap ? wrap.innerText : "").match(/(\d+) left/); return m ? m[0] : "0 left"; }}''') or "0 left").split()[0])
                                for tag in tags[:slots_left]:
                                    await tag_input.fill(translate_tag(tag, lang_code))
                                    await page.wait_for_timeout(500)
                                    await tag_input.press("Enter")
                                    await page.wait_for_timeout(500)
                        except: pass

                print(f"    🌍 {lang_name} repaired")
            except Exception as e:
                print(f"    ❌ {lang_name}: {e}")

    # Save
    clicked = await page.evaluate('() => { const btn = document.getElementById("shop-manager--listing-publish-edit") || Array.from(document.querySelectorAll("button")).find(b => b.innerText && b.innerText.includes("Publish changes") && !b.disabled); if (btn && !btn.disabled) { btn.click(); return true; } return false; }')
    if clicked:
        await page.wait_for_timeout(3000)
        print("  ✅ Saved!")
        return True
    return False

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True)
    parser.add_argument("--tabs", action="store_true")
    parser.add_argument("--desc", action="store_true")
    parser.add_argument("--tags", action="store_true")
    args = parser.parse_args()

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = None
            for p in ctx.pages:
                if "etsy.com" in p.url: page = p; break
            if page is None: page = await ctx.new_page()
            page.set_default_timeout(30000)
            await repair_listing(page, args.id, args.tabs, args.desc, args.tags)
        except Exception as e:
            print(f"❌ Lỗi: {e}")

if __name__ == "__main__":
    asyncio.run(main())
