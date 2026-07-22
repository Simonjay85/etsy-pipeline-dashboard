import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = ctx.pages[0]
        
        # Try to find tag pills - Etsy renders tags as buttons/spans with × to remove
        result = await page.evaluate('''() => {
            // Approach 1: find the tags section by looking for the "All 13 used" or "N left" text
            let allEls = Array.from(document.querySelectorAll("*"));
            
            // Find any element that contains tag-like badge elements with X marks
            // Tag pills usually have a child button for deletion
            let tagSection = null;
            
            // Look for the Tags heading
            for (let el of allEls) {
                if (el.tagName === "LEGEND" || el.tagName === "H2" || el.tagName === "LABEL") {
                    if (el.innerText && el.innerText.trim() === "Tags") {
                        tagSection = el.parentElement;
                        break;
                    }
                }
            }
            
            if (!tagSection) return {method: "no tags section found"};
            
            // Try to find tag values in that section
            // Look for spans/divs that are siblings and have short text + a remove button
            let tags = [];
            let children = Array.from(tagSection.querySelectorAll("button, span, div, li"));
            for (let child of children) {
                let txt = child.innerText || "";
                // Clean up: remove × character
                txt = txt.replace(/×/g, "").replace(/✕/g, "").replace(/x$/i, "").trim();
                // Tag-like: short (2-4 words), no newlines, contains letters
                if (txt && txt.length > 2 && txt.length <= 40 && !txt.includes("\\n") && /[a-z]/i.test(txt) && txt.split(" ").length <= 5) {
                    if (!["Add", "Tags", "Remove", "Add tag", "used", "left"].some(k => txt.toLowerCase().includes(k.toLowerCase()))) {
                        tags.push(txt);
                    }
                }
            }
            
            return {method: "tag_section", tagsSection: tagSection.innerText.substring(0, 200), tags: tags};
        }''')
        print("RESULT:", result)

asyncio.run(main())
