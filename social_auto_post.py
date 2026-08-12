#!/usr/bin/env python3
"""
Social Media Auto Poster
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Dùng Playwright với Chrome session thực (.browser-session)
• Đọc SEO data từ shop's Etsy_SEO_Generator.xlsx
• Lấy ảnh cover đầu tiên của sản phẩm
• Tự động đăng lên: Pinterest, Twitter/X, Medium
• Chạy: python3 social_auto_post.py --row <ROW> --platform <PLATFORM> --shop <SHOP_ID>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import asyncio
import sys
import os
import argparse
import re
import subprocess
from pathlib import Path

# ── Auto-install dependencies ──────────────────────────────────────────────────
def ensure_deps():
    pkgs = {"openpyxl": "openpyxl", "playwright": "playwright"}
    for mod, pkg in pkgs.items():
        try:
            __import__(mod)
        except ImportError:
            print(f"▶ Cài đặt {pkg}...")
            subprocess.run([sys.executable, "-m", "pip", "install", pkg, "--quiet"], check=True)

ensure_deps()

import openpyxl
from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).parent
BROWSER_DIR = BASE_DIR / ".browser-session"
CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

COMMON_HASHTAGS = "#digitaldownload #printable #instantdownload #etsyshop #etsyseller #digitalart"

# ── Caption Generators ────────────────────────────────────────────────────────
def make_instagram_caption(title, desc, tags, etsy_url):
    sentences = [s.strip() for s in desc.replace("\n", " ").split(".") if s.strip()]
    hook = ". ".join(sentences[:2]) + "." if sentences else title
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    hashtags = " ".join(f"#{t.replace(' ', '')}" for t in tag_list[:10])
    hashtags += f" {COMMON_HASHTAGS}"
    return f"{hook}\n\n✨ Get it instantly as a digital download!\n👇 Link in bio or search on Etsy: \"{title[:40]}\"\n\n{hashtags}"

def make_pinterest_description(title, desc, tags, etsy_url):
    sentences = [s.strip() for s in desc.replace("\n", " ").split(".") if s.strip()]
    short_desc = ". ".join(sentences[:3]) + "." if sentences else desc[:200]
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    keywords = " | ".join(tag_list[:5])
    return f"{title}\n\n{short_desc}\n\n{keywords} | Instant Digital Download | Printable PDF\n\n🛒 Shop now → {etsy_url}"

def make_facebook_post(title, desc, tags, etsy_url):
    sentences = [s.strip() for s in desc.replace("\n", " ").split(".") if s.strip()]
    body = " ".join(sentences[:4]) + "." if sentences else desc[:300]
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    hashtags = " ".join(f"#{t.replace(' ', '')}" for t in tag_list[:6])
    return f"🆕 New listing just dropped!\n\n📌 {title}\n\n{body}\n\n✅ Instant digital download — print at home or at any print shop!\n🔗 Get it here: {etsy_url}\n\n{hashtags} {COMMON_HASHTAGS}"

def make_twitter_post(title, etsy_url):
    short_title = title[:60] if len(title) > 60 else title
    tweet = f"🆕 {short_title} — instant digital download! ✨\n\n🛒 {etsy_url}\n\n#printable #digitaldownload #etsyshop"
    return tweet[:280]

def make_medium_intro(title, desc, tags, etsy_url):
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    keywords_prose = ", ".join(tag_list[:5])
    return f"# {title}\n\n{desc}\n\n---\n\n## Get It Now\n\nThis is an **instant digital download** — you'll receive the file immediately after purchase. No waiting, no shipping.\n\n👉 **[Get it on Etsy]({etsy_url})**\n\n---\n\n*Tags: {keywords_prose}*"

# ── Read Product Data ─────────────────────────────────────────────────────────
def read_product_data(shop_id: str, row_num: int):
    shop_dir = BASE_DIR / "shops" / shop_id
    excel_file = shop_dir / "Etsy_SEO_Generator.xlsx"
    
    if not excel_file.exists():
        print(f"❌ Không tìm thấy Excel tại: {excel_file}")
        return None
        
    wb = openpyxl.load_workbook(excel_file, data_only=True)
    ws = wb["Listings"]
    
    row = [ws.cell(row=row_num, column=c).value for c in range(1, 18)]
    # A=stt B=folder C=keywords D=notes E=price F=cat G=imgs H=title I=desc J=tags K=qty L=who M=when N=status O=section P=etsy_url
    folder = row[1]
    title = row[7]
    desc = row[8]
    tags = row[9]
    etsy_url = row[15]
    
    if not folder:
        print(f"❌ Hàng {row_num} không có folder sản phẩm.")
        return None
        
    if not title or str(title).startswith("[Cần SEO]"):
        print(f"❌ Sản phẩm hàng {row_num} chưa được làm SEO. Vui lòng tạo SEO trước.")
        return None
        
    # Lấy ảnh cover
    img_dir = shop_dir / str(folder) / "images"
    img_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    img_paths = sorted([str(f) for f in img_dir.iterdir() if f.suffix.lower() in img_exts]) if img_dir.exists() else []
    cover_image = img_paths[0] if img_paths else None
    
    # Lấy shop_etsy_url
    shop_etsy_url = "https://www.etsy.com"
    try:
        import json
        with open(BASE_DIR / "shops_config.json") as f:
            cfg = json.load(f)
        shop_etsy_url = cfg.get(shop_id, {}).get("etsy_link", "https://www.etsy.com")
    except:
        pass

    # URL default nếu chưa có
    if not etsy_url or not str(etsy_url).strip():
        if shop_etsy_url and shop_etsy_url != "https://www.etsy.com":
            etsy_url = f"{shop_etsy_url.rstrip('/')}/listing/{folder}"
        else:
            etsy_url = "https://www.etsy.com"
            
    return {
        "folder": str(folder),
        "title": str(title),
        "desc": str(desc or ""),
        "tags": str(tags or ""),
        "etsy_url": str(etsy_url),
        "shop_etsy_url": str(shop_etsy_url),
        "cover_image": cover_image,
        "img_paths": img_paths,
        "shop_dir": str(shop_dir)
    }

# ── Automation Handlers ───────────────────────────────────────────────────────

async def post_pinterest(page, p):
    print("▶ Điều hướng tới Pinterest Pin Builder...")
    await page.goto("https://www.pinterest.com/pin-builder/", wait_until="domcontentloaded")
    await page.wait_for_timeout(4000)
    
    if "login" in page.url or await page.locator('input[id="email"]').count() > 0:
        print("❌ Chưa đăng nhập Pinterest! Vui lòng chạy MỞ_CHROME_DEBUG.command và đăng nhập vào Pinterest trước.")
        return False
        
    print("✓ Đã đăng nhập Pinterest.")
    
    # 1. Upload Cover or Carousel Images
    if not p["cover_image"]:
        print("❌ Không tìm thấy ảnh của sản phẩm để đăng Pinterest.")
        return False
        
    img_paths = p.get("img_paths", [])
    if len(img_paths) > 1:
        print(f"▶ Phát hiện {len(img_paths)} hình ảnh. Bắt đầu tạo Carousel Pin...")
        # Upload ảnh thứ nhất
        print(f"👉 Tải ảnh 1: {Path(img_paths[0]).name}...")
        file_input = page.locator('input[type="file"]').first
        await file_input.wait_for(state="attached", timeout=10000)
        await file_input.set_input_files(img_paths[0])
        await page.wait_for_timeout(2000)
        
        # Click Create carousel
        carousel_btn = page.locator('[data-test-id="create-carousel-button"] button, button:has-text("Create carousel")').first
        if await carousel_btn.count() > 0:
            print("👉 Click nút 'Create carousel'...")
            await carousel_btn.click()
            await page.wait_for_timeout(2000)
            
            # Loop upload các ảnh tiếp theo (tối đa 5 ảnh để tránh quá tải)
            for idx, img_path in enumerate(img_paths[1:5], start=2):
                print(f"👉 Thêm ảnh {idx}: {Path(img_path).name}...")
                add_btn = page.locator('button[aria-label="Add"]').first
                if await add_btn.count() > 0:
                    await add_btn.click()
                    await page.wait_for_timeout(1500)
                    
                    inp = page.locator('input[type="file"]').first
                    await inp.set_input_files(img_path)
                    await page.wait_for_timeout(2000)
                else:
                    print(f"⚠ Không tìm thấy nút 'Add' cho ảnh {idx}, dừng thêm Carousel.")
                    break
        else:
            print("⚠ Không tìm thấy nút 'Create carousel', chuyển sang đăng 1 ảnh đơn.")
    else:
        print(f"▶ Tải ảnh đơn: {Path(p['cover_image']).name}...")
        file_input = page.locator('input[type="file"]').first
        await file_input.wait_for(state="attached", timeout=10000)
        await file_input.set_input_files(p["cover_image"])
        await page.wait_for_timeout(2000)
    
    # 2. Fill Title
    print("▶ Điền tiêu đề...")
    title_filled = False
    for sel in ['[data-testid*="title" i] input', 'input[placeholder*="title" i]', 'textarea[placeholder*="title" i]', 'input[type="text"]']:
        try:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.click()
                await el.fill(p["title"])
                title_filled = True
                break
        except: pass
    if not title_filled:
        print("⚠ Không điền được tiêu đề bằng cách thường, thử ép kiểu JavaScript...")
        await page.evaluate(f"document.querySelector('input[type=\"text\"]').value = '{p['title']}'")
        
    # 3. Fill Description
    print("▶ Điền mô tả...")
    desc_text = make_pinterest_description(p["title"], p["desc"], p["tags"], p["etsy_url"])
    desc_filled = False
    for sel in ['[data-testid*="description" i] [contenteditable="true"]', '[data-testid*="description" i] textarea', 'textarea[placeholder*="about" i]', '[contenteditable="true"]']:
        try:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.click()
                await el.fill(desc_text)
                desc_filled = True
                break
        except: pass
        
    # 4. Fill Alt Text
    try:
        alt_btn = page.locator('button:has-text("Add alt text"), [data-test-id="pin-draft-alt-text-button"] button').first
        if await alt_btn.count() > 0 and await alt_btn.is_visible():
            print("▶ Mở và điền Alt Text...")
            await alt_btn.click()
            await page.wait_for_timeout(1000)
            
            alt_filled = False
            for sel in ['textarea[id*="-alttext-"]', 'textarea[placeholder*="alt text" i]']:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    await el.fill(p["title"][:500])
                    alt_filled = True
                    break
    except Exception as alt_err:
        print(f"⚠ Lỗi khi điền Alt Text: {alt_err}")

    # 5. Fill Destination Link
    destination_link = p.get("shop_etsy_url") or p["etsy_url"]
    print(f"▶ Điền Destination Link (Shop Etsy): {destination_link}...")
    link_filled = False
    link_selectors = [
        'textarea[id*="-link-"]',
        'textarea[placeholder*="link" i]',
        'textarea[placeholder*="destination" i]',
        '[data-testid*="link" i] input',
        'input[placeholder*="link" i]',
        'input[placeholder*="website" i]'
    ]
    for sel in link_selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.click()
                await el.fill(destination_link)
                link_filled = True
                break
        except: pass

    await page.wait_for_timeout(1000)
    
    # 6. Select Board & Publish
    print("▶ Chọn bảng và Đăng...")
    published = False
    
    # Thử tìm và click Board Dropdown để chọn bảng nếu có
    try:
        dropdown = page.locator('[data-test-id*="board-dropdown"], [data-testid*="board-dropdown"], [data-test-id*="board-select"], [role="button"]:has-text("Select"), [aria-label*="Select board"]').first
        if await dropdown.count() > 0 and await dropdown.is_visible():
            print("▶ Đang mở danh sách bảng (Board dropdown)...")
            await dropdown.click(force=True)
            await page.wait_for_timeout(1500)
            
            # Chọn board đầu tiên
            board_opt = page.locator('[data-test-id*="board-row"], [data-testid*="board-row"], [role="option"]').first
            if await board_opt.count() > 0:
                board_name = await board_opt.inner_text()
                print(f"👉 Chọn bảng đầu tiên: '{board_name.splitlines()[0]}'")
                await board_opt.click(force=True)
                await page.wait_for_timeout(1500)
    except Exception as board_err:
        print(f"  ⚠ Lỗi khi chọn bảng (bỏ qua và tự động dùng bảng mặc định): {board_err}")

    # Lọc qua các selector của nút Publish
    publish_selectors = [
        '[data-test-id="board-dropdown-save-button"]:visible',
        '[data-testid="board-dropdown-save-button"]:visible',
        'div[role="button"]:has-text("Publish"):visible',
        'div[role="button"]:has-text("Đăng"):visible',
        'div[role="button"]:has-text("Lưu"):visible',
        '[data-test-id*="save-button" i]:visible',
        '[data-testid*="save-button" i]:visible',
        'button[aria-label="Publish"]:visible',
        'button:has-text("Publish"):visible',
        'clg-button:has-text("Publish"):visible',
        '[data-test-id*="publish" i] button:visible',
        '[data-testid*="publish" i] button:visible',
        'button[type="submit"]:visible'
    ]

    for btn_sel in publish_selectors:
        try:
            btn = page.locator(btn_sel).first
            if await btn.count() > 0:
                print(f"👉 Tìm thấy nút Publish với selector: {btn_sel}")
                await btn.scroll_into_view_if_needed()
                await page.wait_for_timeout(500)
                await btn.click(force=True)
                
                print("⏳ Đang lưu Pin...")
                await page.wait_for_timeout(8000)
                published = True
                break
        except Exception as e:
            print(f"⚠ Thử click Publish lỗi: {e}")
            
    if published:
        print("✅ Đã đăng Pin lên Pinterest thành công!")
        return True
    else:
        print("❌ Không tìm thấy nút Publish trên Pinterest.")
        return False


async def post_twitter(page, p):
    print("▶ Điều hướng tới Twitter/X compose...")
    await page.goto("https://x.com/compose/post", wait_until="domcontentloaded")
    await page.wait_for_timeout(4000)
    
    if "login" in page.url or "i/flow/login" in page.url:
        # Thử fallback twitter.com
        await page.goto("https://twitter.com/compose/tweet", wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        
    if "login" in page.url or "i/flow/login" in page.url:
        print("❌ Chưa đăng nhập X/Twitter! Vui lòng chạy MỞ_CHROME_DEBUG.command và đăng nhập vào X trước.")
        return False
        
    print("✓ Đã đăng nhập Twitter/X.")
    
    # 1. Fill Tweet Content
    tweet_text = make_twitter_post(p["title"], p["etsy_url"])
    print(f"▶ Điền nội dung Tweet ({len(tweet_text)} ký tự)...")
    
    textbox = page.locator('[data-testid="tweetTextarea_0"], .public-DraftEditor-content, [role="textbox"]').first
    await textbox.wait_for(state="visible", timeout=10000)
    await textbox.click()
    await page.wait_for_timeout(500)
    await textbox.fill(tweet_text)
    await page.wait_for_timeout(1000)
    
    # 2. Upload cover image
    if p["cover_image"]:
        print("▶ Đang đính kèm ảnh...")
        file_input = page.locator('input[type="file"][accept*="image"]').first
        if await file_input.count() > 0:
            await file_input.set_input_files(p["cover_image"])
            await page.wait_for_timeout(3000)
            
    # 3. Click Post Button
    print("▶ Đang click nút Đăng bài (Post)...")
    post_btn = page.locator('[data-testid="tweetButton"], [data-testid="tweetButtonInline"], button:has-text("Post"), button:has-text("Tweet")').first
    if await post_btn.count() > 0 and await post_btn.is_visible():
        await post_btn.click()
        await page.wait_for_timeout(4000)
        print("✅ Đã đăng bài lên Twitter/X thành công!")
        return True
    else:
        print("❌ Không tìm thấy nút Post/Tweet.")
        return False


async def post_medium(page, p):
    print("▶ Điều hướng tới Medium New Story...")
    await page.goto("https://medium.com/new-story", wait_until="domcontentloaded")
    await page.wait_for_timeout(4000)
    
    if "login" in page.url or "m/signin" in page.url:
        print("❌ Chưa đăng nhập Medium! Vui lòng chạy MỞ_CHROME_DEBUG.command và đăng nhập vào Medium trước.")
        return False
        
    print("✓ Đã đăng nhập Medium.")
    
    # 1. Fill Title
    print("▶ Điền tiêu đề Medium...")
    title_el = page.locator('h3[placeholder="Title"], h3[class*="title"], h3[id*="title"], [contenteditable="true"]').first
    await title_el.wait_for(state="visible", timeout=10000)
    await title_el.click()
    await title_el.fill(p["title"])
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(1000)
    
    # 2. Fill Body
    print("▶ Điền mô tả sản phẩm...")
    body_text = make_medium_intro(p["title"], p["desc"], p["tags"], p["etsy_url"])
    # Paste body text cleanly
    body_el = page.locator('p[placeholder*="Tell your story" i], section[class*="story" i], [contenteditable="true"]').nth(1)
    if await body_el.count() > 0:
        await body_el.click()
        await body_el.fill(body_text)
    else:
        # Fallback: type
        await page.keyboard.type(body_text)
    await page.wait_for_timeout(2000)
    
    # 3. Publish
    print("▶ Click Publish menu...")
    pub_menu = page.locator('button:has-text("Publish"), [data-action="publish-overlay"]').first
    if await pub_menu.count() > 0:
        await pub_menu.click()
        await page.wait_for_timeout(2000)
        
        print("▶ Click Publish Now thực tế...")
        pub_now = page.locator('button:has-text("Publish now"), button[class*="publish"]').first
        if await pub_now.count() > 0:
            await pub_now.click()
            await page.wait_for_timeout(4000)
            print("✅ Đã đăng bài lên Medium thành công!")
            return True
            
    print("❌ Không tự động click được nút Publish trên Medium.")
    return False

async def post_facebook(page, p):
    print("▶ Điều hướng tới Facebook...")
    await page.goto("https://www.facebook.com", wait_until="domcontentloaded")
    await page.wait_for_timeout(4000)
    
    if "login" in page.url or await page.locator('input[id="email"]').count() > 0:
        print("❌ Chưa đăng nhập Facebook! Vui lòng mở Chrome đăng nhập vào Facebook trước.")
        return False
        
    print("✓ Đã đăng nhập Facebook.")
    
    # 1. Click "What's on your mind?" button
    print("▶ Mở khung soạn thảo bài viết...")
    composer_selectors = [
        '[role="button"]:has-text("What\'s on your mind?")',
        '[role="button"]:has-text("Bạn đang nghĩ gì?")',
        '[role="button"]:has-text("Create a post")',
        '[role="button"]:has-text("Tạo bài viết")',
        'span:has-text("What\'s on your mind?")',
        'span:has-text("Bạn đang nghĩ gì?")'
    ]
    
    clicked = False
    for sel in composer_selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.click(force=True)
                clicked = True
                break
        except: pass
        
    if not clicked:
        try:
            el = page.locator('[role="button"]').filter(has_text=re.compile("What's on your mind|Bạn đang nghĩ gì", re.I)).first
            if await el.count() > 0:
                await el.click(force=True)
                clicked = True
        except: pass
        
    if not clicked:
        print("❌ Không mở được khung soạn bài viết trên Facebook.")
        return False
        
    await page.wait_for_timeout(2000)
    
    # 2. Fill Post Content
    fb_text = make_facebook_post(p["title"], p["desc"], p["tags"], p["etsy_url"])
    print("▶ Điền nội dung bài viết...")
    textbox_selectors = [
        '[role="textbox"]',
        '[contenteditable="true"]',
        '[aria-label*="What\'s on your mind?"]',
        '[aria-label*="Bạn đang nghĩ gì?"]'
    ]
    
    filled = False
    for sel in textbox_selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.click(force=True)
                await page.wait_for_timeout(500)
                await el.fill(fb_text)
                filled = True
                break
        except: pass
        
    if not filled:
        await page.keyboard.type(fb_text)
        
    await page.wait_for_timeout(1500)
    
    # 3. Upload cover image
    if p["cover_image"]:
        print(f"▶ Đính kèm ảnh bìa: {Path(p['cover_image']).name}...")
        try:
            file_input = page.locator('input[type="file"]').first
            await file_input.wait_for(state="attached", timeout=10000)
            await file_input.set_input_files(p["cover_image"])
            await page.wait_for_timeout(3000)
        except Exception as img_err:
            print(f"  ⚠ Lỗi khi đính kèm ảnh (bỏ qua và đăng dạng text): {img_err}")
            
    # 4. Click Post button
    print("▶ Tiến hành đăng bài...")
    post_selectors = [
        '[role="button"]:has-text("Post")',
        '[role="button"]:has-text("Đăng")',
        'span:has-text("Post")',
        'span:has-text("Đăng")',
        '[aria-label="Post"]',
        '[aria-label="Đăng"]'
    ]
    
    posted = False
    for sel in post_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible():
                is_disabled = await btn.get_attribute("aria-disabled")
                if is_disabled == "true":
                    await page.wait_for_timeout(3000)
                await btn.click(force=True)
                posted = True
                break
        except: pass
        
    if posted:
        print("⏳ Đang đợi Facebook hoàn tất đăng tải...")
        await page.wait_for_timeout(6000)
        print("✅ Đã đăng bài lên Facebook Fanpage thành công!")
        return True
    else:
        print("❌ Không tìm thấy nút Đăng (Post) trên Facebook.")
        return False


async def post_reddit(page, p, subreddit="u_SimonJay0805"):
    sub_name = subreddit.replace("u/", "u_").replace("r/", "")
    submit_url = f"https://www.reddit.com/r/{sub_name}/submit" if not sub_name.startswith("u_") else f"https://www.reddit.com/submit"
    
    print(f"▶ Điều hướng tới Reddit Submit ({sub_name})...")
    await page.goto(submit_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(4000)
    
    # Check if login is needed
    if "login" in page.url or await page.locator('a[href*="login"]').count() > 0 and await page.locator('a[href*="login"]').first.is_visible():
        print("❌ Chưa đăng nhập Reddit! Vui lòng mở Chrome đăng nhập vào Reddit trước.")
        return False
        
    print("✓ Đã đăng nhập Reddit.")
    
    # 1. Choose Community if generic submit page
    if sub_name.startswith("u_"):
        print(f"▶ Chọn cộng đồng: u/{sub_name[2:]}...")
        try:
            community_input = page.locator('[placeholder="Choose a community"], [aria-label="Choose a community"], input[id*="community"]').first
            await community_input.wait_for(state="visible", timeout=10000)
            await community_input.click()
            await page.wait_for_timeout(500)
            
            profile_username = sub_name[2:]
            await page.keyboard.type(profile_username)
            await page.wait_for_timeout(1500)
            
            await page.keyboard.press("ArrowDown")
            await page.wait_for_timeout(500)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(1500)
        except Exception as sub_err:
            print(f"  ⚠ Lỗi chọn cộng đồng (bỏ qua nếu đã tự động chọn): {sub_err}")

    # 2. Fill Title
    print("▶ Điền tiêu đề bài đăng...")
    title_selectors = [
        'textarea[placeholder="Title"]',
        'textarea[name="title"]',
        '[placeholder="Title"]'
    ]
    title_filled = False
    for sel in title_selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.click()
                await el.fill(p["title"])
                title_filled = True
                break
        except: pass
        
    if not title_filled:
        print("❌ Không điền được tiêu đề trên Reddit.")
        return False
        
    await page.wait_for_timeout(1000)
    
    # 3. Fill Body
    print("▶ Điền nội dung bài viết...")
    body_text = f"{p['desc']}\n\n🛒 Shop now → {p['etsy_url']}\n\n#digitalplanner #printable #etsyshop"
    body_selectors = [
        'textarea[placeholder="Text (optional)"]',
        '[placeholder="Text (optional)"]',
        '[role="textbox"]',
        '[contenteditable="true"]'
    ]
    body_filled = False
    for sel in body_selectors:
        try:
            el = page.locator(sel).nth(1) if "textbox" in sel or "contenteditable" in sel else page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.click()
                await el.fill(body_text)
                body_filled = True
                break
        except: pass
        
    if not body_filled:
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(500)
        await page.keyboard.insert_text(body_text)
        
    await page.wait_for_timeout(1500)
    
    # 4. Click Post
    print("▶ Click nút đăng bài (Post)...")
    post_selectors = [
        'button:has-text("Post")',
        'button:has-text("Đăng")',
        'button[type="submit"]',
        '[data-testid="submit-button"]'
    ]
    posted = False
    for sel in post_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(force=True)
                posted = True
                break
        except: pass
        
    if posted:
        print("⏳ Đang đợi đăng bài Reddit...")
        await page.wait_for_timeout(6000)
        print("✅ Đã đăng bài lên Reddit thành công!")
        return True
    else:
        print("❌ Không tìm thấy nút Post trên Reddit.")
        return False


# ── Main Engine ───────────────────────────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser(description="Social Auto Poster")
    parser.add_argument("--row", type=int, required=True, help="Hàng của sản phẩm trong Excel")
    parser.add_argument("--platform", type=str, required=True, choices=["instagram", "pinterest", "facebook", "twitter", "medium", "reddit"], help="Nền tảng muốn đăng")
    parser.add_argument("--shop", type=str, required=True, help="ID của shop hiện tại")
    parser.add_argument("--subreddit", type=str, default="u_SimonJay0805", help="Reddit Subreddit name to post to")
    args = parser.parse_args()
    
    p = read_product_data(args.shop, args.row)
    if p is None:
        sys.exit(1)
        
    print(f"\n{'='*60}")
    print(f"  📢 SOCIAL AUTO POSTER — Khởi động đăng bài tự động")
    print(f"  📦 Shop: {args.shop} | Platform: {args.platform.upper()} | Row: {args.row}")
    print(f"  📌 Folder: {p['folder']} | Title: {p['title'][:40]}...")
    print(f"{'='*60}\n")
    
    BROWSER_DIR.mkdir(exist_ok=True)
    # Xoá singleton lock files Chrome
    for lf in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        try: (BROWSER_DIR / lf).unlink(missing_ok=True)
        except: pass
        
    async with async_playwright() as pw:
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
                user_data_dir=str(BROWSER_DIR),
                headless=False,
                args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
                viewport=None,
            )
            if CHROME_PATH.exists():
                launch_kw["executable_path"] = str(CHROME_PATH)
                print("🌐 Dùng Google Chrome thật")
            else:
                print("🌐 Dùng Chromium")
            ctx = await pw.chromium.launch_persistent_context(**launch_kw)

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        
        success = False
        try:
            if args.platform == "pinterest":
                success = await post_pinterest(page, p)
            elif args.platform == "twitter":
                success = await post_twitter(page, p)
            elif args.platform == "medium":
                success = await post_medium(page, p)
            elif args.platform == "facebook":
                success = await post_facebook(page, p)
            elif args.platform == "reddit":
                success = await post_reddit(page, p, args.subreddit)
            else:
                print(f"❌ Nền tảng '{args.platform}' tự động đăng sẽ được cập nhật trong phiên bản tiếp theo.")
                print(f"👉 Vui lòng sử dụng nút 'Copy Caption' để đăng thủ công lên {args.platform.upper()}!")
                success = False
        except Exception as e:
            print(f"❌ Xảy ra lỗi ngoài ý muốn: {e}")
            import traceback
            traceback.print_exc()
            success = False
        finally:
            await ctx.close()
            
    if success:
        print(f"\n🎉 [HOÀN TẤT] Tự động đăng {args.platform.upper()} thành công!")
        sys.exit(0)
    else:
        print(f"\n❌ [THẤT BẠI] Đăng {args.platform.upper()} thất bại.")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
