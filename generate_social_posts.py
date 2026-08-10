"""
Social Media Content Generator
• Đọc data từ Etsy_SEO_Generator.xlsx (title, desc, tags, Etsy URL)
• Generate caption tối ưu cho Pinterest, Instagram, Facebook, Twitter/X, Medium
• Xuất ra file social_posts.md để copy-paste hoặc dùng với auto-poster
• Chạy: python3 generate_social_posts.py
"""
import sys, subprocess
from pathlib import Path
from medium_content import make_medium_research_article

def ensure_deps():
    for mod, pkg in {"openpyxl": "openpyxl"}.items():
        try:
            __import__(mod)
        except ImportError:
            subprocess.run([sys.executable, "-m", "pip", "install", pkg, "--quiet"], check=True)

ensure_deps()
import openpyxl

BASE_DIR   = Path(__file__).parent
EXCEL_FILE = BASE_DIR / "Etsy_SEO_Generator.xlsx"
OUTPUT_MD  = BASE_DIR / "social_posts.md"

# ── Hashtag templates per niche ───────────────────────────────────────────────
COMMON_HASHTAGS = "#digitaldownload #printable #instantdownload #etsyshop #etsyseller #digitalart"

def make_instagram_caption(title, desc, tags, etsy_url):
    # Lấy 2 câu đầu của description
    sentences = [s.strip() for s in desc.replace("\n", " ").split(".") if s.strip()]
    hook = ". ".join(sentences[:2]) + "." if sentences else title

    # Tags thành hashtags IG
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    hashtags = " ".join(f"#{t.replace(' ', '')}" for t in tag_list[:10])
    hashtags += f" {COMMON_HASHTAGS}"

    return f"""{hook}

✨ Get it instantly as a digital download!
👇 Link in bio or search on Etsy: "{title[:40]}"

{hashtags}"""

def make_pinterest_description(title, desc, tags, etsy_url):
    sentences = [s.strip() for s in desc.replace("\n", " ").split(".") if s.strip()]
    short_desc = ". ".join(sentences[:3]) + "." if sentences else desc[:200]
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    keywords = " | ".join(tag_list[:5])

    return f"""{title}

{short_desc}

{keywords} | Instant Digital Download | Printable PDF

🛒 Shop now → {etsy_url}"""

def make_facebook_post(title, desc, tags, etsy_url):
    sentences = [s.strip() for s in desc.replace("\n", " ").split(".") if s.strip()]
    body = " ".join(sentences[:4]) + "." if sentences else desc[:300]
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    hashtags = " ".join(f"#{t.replace(' ', '')}" for t in tag_list[:6])

    return f"""🆕 New listing just dropped!

📌 {title}

{body}

✅ Instant digital download — print at home or at any print shop!
🔗 Get it here: {etsy_url}

{hashtags} {COMMON_HASHTAGS}"""

def make_twitter_post(title, etsy_url):
    # Twitter limit ~280 chars
    short_title = title[:60] if len(title) > 60 else title
    tweet = f"🆕 {short_title} — instant digital download! ✨\n\n🛒 {etsy_url}\n\n#printable #digitaldownload #etsyshop"
    return tweet[:280]

def make_medium_intro(title, desc, tags, etsy_url):
    """Compatibility wrapper for the shared Medium article builder."""
    return make_medium_research_article(title, desc, tags, etsy_url)

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print("\n" + "="*55)
    print("  📱 Social Media Content Generator")
    print("="*55 + "\n")

    wb = openpyxl.load_workbook(EXCEL_FILE, data_only=True)
    ws = wb["Listings"]

    products = []
    for row in ws.iter_rows(min_row=4, max_row=60, values_only=True):
        cols = (list(row) + [None]*16)[:16]
        stt, folder, keywords, note, price, category, imgs, title, desc, tags, qty, who, when, status, section, etsy_url = cols

        if not folder or not title or str(title).startswith("←"):
            continue
        if not etsy_url:
            etsy_url = f"https://www.etsy.com/shop/YourShopName"  # placeholder

        products.append({
            "folder":    str(folder),
            "title":     str(title),
            "desc":      str(desc or ""),
            "tags":      str(tags or ""),
            "price":     price,
            "etsy_url":  str(etsy_url),
            "status":    str(status or ""),
        })

    if not products:
        print("  ⚠  Không tìm thấy sản phẩm nào có SEO trong Excel.")
        return

    print(f"  Tìm thấy {len(products)} sản phẩm\n")

    lines = ["# SOCIAL MEDIA POSTS — Etsy Digital Products\n",
             f"*Generated for {len(products)} products*\n",
             "---\n"]

    for i, p in enumerate(products, 1):
        print(f"  {i:2}. {p['folder']} | {p['title'][:50]}...")

        lines.append(f"\n## {i}. {p['folder']} — {p['title'][:60]}\n")
        lines.append(f"> 🔗 Etsy URL: {p['etsy_url']}\n")
        lines.append(f"> 💲 Giá: ${p['price'] or 'N/A'} | Status: {p['status']}\n")
        lines.append("\n---\n")

        # Instagram
        lines.append("### 📸 INSTAGRAM\n")
        lines.append("```\n")
        lines.append(make_instagram_caption(p["title"], p["desc"], p["tags"], p["etsy_url"]))
        lines.append("\n```\n")

        # Pinterest
        lines.append("### 📌 PINTEREST (Pin Description)\n")
        lines.append("```\n")
        lines.append(make_pinterest_description(p["title"], p["desc"], p["tags"], p["etsy_url"]))
        lines.append("\n```\n")

        # Facebook
        lines.append("### 👥 FACEBOOK\n")
        lines.append("```\n")
        lines.append(make_facebook_post(p["title"], p["desc"], p["tags"], p["etsy_url"]))
        lines.append("\n```\n")

        # Twitter/X
        lines.append("### 𝕏 TWITTER / X\n")
        lines.append("```\n")
        lines.append(make_twitter_post(p["title"], p["etsy_url"]))
        lines.append("\n```\n")

        # Medium
        lines.append("### ✍️ MEDIUM (Research Article)\n")
        lines.append("```\n")
        lines.append(make_medium_intro(p["title"], p["desc"], p["tags"], p["etsy_url"]))
        lines.append("\n```\n")

        lines.append("\n" + "="*60 + "\n")

    with open(OUTPUT_MD, "w", encoding="utf-8") as f:
        f.writelines(lines)

    print(f"\n  ✅ Đã tạo: social_posts.md")
    print(f"  📂 Mở file để copy content cho từng kênh")
    print(f"\n  Tip: Chạy get_etsy_links.py trước để có link thật")
    print(f"       thay vì placeholder URL.\n")
    print("="*55 + "\n")

if __name__ == "__main__":
    main()
