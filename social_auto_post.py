#!/usr/bin/env python3
"""
Social Media Auto Poster
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Dùng Playwright với Chrome session thực (.browser-session)
• Đọc SEO data từ shop's Etsy_SEO_Generator.xlsx
• Lấy ảnh cover đầu tiên của sản phẩm
• Tự động đăng lên: Instagram, Pinterest, Facebook, Twitter/X, Medium, Reddit
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
from urllib.parse import urlsplit
from social_browser_session import (
    SOCIAL_URLS,
    is_session_ready,
    load_social_session,
    open_social_browser,
)
from social_post_store import record_social_post
from medium_content import (
    make_medium_article_title,
    make_medium_research_article,
    render_medium_plain_text,
)

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

BASE_DIR = Path(__file__).resolve().parent

COMMON_HASHTAGS = "#digitaldownload #printable #instantdownload #etsyshop #etsyseller #digitalart"
PINTEREST_TITLE_MAX_LENGTH = 100
PINTEREST_DESCRIPTION_MAX_LENGTH = 800
PINTEREST_PUBLISH_PATH_PATTERN = r"/pin/\d+/?(?:[?#].*)?$"
PINTEREST_PUBLISH_URL_PATTERN = r"^" + PINTEREST_PUBLISH_PATH_PATTERN
PINTEREST_PUBLISH_ABSOLUTE_URL_PATTERN = (
    r"^https?://"
    r"(?:(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)?)"
    r"pinterest\.com"
    + PINTEREST_PUBLISH_PATH_PATTERN
)
PINTEREST_PUBLISH_ERROR_KEYWORDS = {
    "long",
    "trim",
    "must fix",
    "required",
    "error",
    "oops",
    "invalid",
    "missing",
}


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).strip())


def _truncate_text_tail(text: str, max_length: int) -> str:
    if max_length <= 0:
        return ""
    return text[-max_length:] if len(text) > max_length else text


def _truncate_to_word_boundary(text: str, max_length: int) -> str:
    if max_length <= 0:
        return ""
    if len(text) <= max_length:
        return text

    words = text.split(" ")
    chosen_parts: list[str] = []
    total = 0
    for word in words:
        separator_len = 1 if chosen_parts else 0
        if total + separator_len + len(word) > max_length:
            break
        chosen_parts.append(word)
        total += separator_len + len(word)

    if chosen_parts:
        return " ".join(chosen_parts)

    return text[:max_length]


def _normalize_pinterest_title(title: str, max_length: int = PINTEREST_TITLE_MAX_LENGTH) -> str:
    """Normalize Pinterest title and trim deterministically to <= max_length.

    Prefer word-boundary truncation and always return a non-empty string when the
    input has visible content.
    """
    if not title:
        return ""
    normalized = _normalize_text(title)
    if len(normalized) <= max_length:
        return normalized

    return _truncate_to_word_boundary(normalized, max_length)


def _fit_title_and_keywords_with_budget(title: str, keywords: str, budget: int) -> str:
    if budget <= 0:
        return ""

    separator = "\n\n"
    if title and not keywords:
        return _truncate_to_word_boundary(title, budget)
    if keywords and not title:
        return _truncate_to_word_boundary(keywords, budget)

    if len(title) <= budget:
        remaining = budget - len(title)
        if remaining <= len(separator):
            return title
        keyword_budget = remaining - len(separator)
        trimmed_keywords = _truncate_to_word_boundary(keywords, keyword_budget)
        if trimmed_keywords:
            return f"{title}{separator}{trimmed_keywords}"
        return title

    return _truncate_to_word_boundary(title, budget)


def _build_pinterest_description(
    title: str,
    desc: str,
    tags: str,
    etsy_url: str,
) -> tuple[str, str, str]:
    normalized_title = _normalize_text(title)
    sentences = [s.strip() for s in _normalize_text(desc).replace(".", ". ").split(". ") if s.strip()]
    short_desc = ". ".join(sentences[:3]) + "." if sentences else _normalize_text(desc)[:200]
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
    keywords = " | ".join(tag_list[:5])
    if keywords:
        keywords = f"{keywords} | Instant Digital Download"
    else:
        keywords = "Instant Digital Download"
    clean_url = _normalize_text(etsy_url) or "https://www.etsy.com"
    cta = f"🛒 Shop now → {clean_url}"
    return normalized_title, short_desc, keywords, cta


def _normalize_pinterest_description(
    title: str,
    desc: str,
    tags: str,
    etsy_url: str,
    max_length: int = PINTEREST_DESCRIPTION_MAX_LENGTH,
) -> tuple[str, bool]:
    """
    Build and normalize Pinterest description text.

    Returns:
        tuple[description, was_truncated]
    """
    if max_length <= 0:
        return "", False

    normalized_title, short_desc, keywords, cta_line = _build_pinterest_description(
        title, desc, tags, etsy_url
    )

    if not cta_line:
        cta_line = "🛒 Shop now → https://www.etsy.com"

    raw_prefix = "\n\n".join(
        [part for part in (normalized_title, short_desc, keywords) if part]
    )
    raw = f"{raw_prefix}\n\n{cta_line}" if raw_prefix else cta_line
    if len(raw) <= max_length:
        return raw, False

    if len(cta_line) >= max_length:
        return _truncate_text_tail(cta_line, max_length), True

    separator = "\n\n"
    separator_len = len(separator)
    budget_for_prefix = max_length - len(cta_line) - separator_len
    if budget_for_prefix <= 0:
        return _truncate_text_tail(cta_line, max_length), True

    if not normalized_title and not short_desc and not keywords:
        return _truncate_text_tail(cta_line, max_length), True

    # Keep title + keywords whenever possible, then trim description.
    title_keyword_prefix = "\n\n".join(
        [part for part in (normalized_title, keywords) if part]
    )
    if title_keyword_prefix:
        if len(title_keyword_prefix) <= budget_for_prefix:
            desc_budget = budget_for_prefix - len(title_keyword_prefix)
            if normalized_title and keywords:
                desc_budget -= separator_len
            elif normalized_title or keywords:
                desc_budget -= separator_len
            desc_budget = max(0, desc_budget)
            desc_trimmed = _truncate_to_word_boundary(short_desc, desc_budget)
            prefix = "\n\n".join(
                [part for part in (normalized_title, desc_trimmed, keywords) if part]
            )
            return f"{prefix}\n\n{cta_line}", True

    prefix = _fit_title_and_keywords_with_budget(
        normalized_title, keywords, budget_for_prefix
    )
    if not prefix:
        return _truncate_text_tail(cta_line, max_length), True

    prefix = _truncate_to_word_boundary(prefix, budget_for_prefix)
    return f"{prefix}\n\n{cta_line}", True


# ── Caption Generators ────────────────────────────────────────────────────────
def make_instagram_caption(title, desc, tags, etsy_url):
    sentences = [s.strip() for s in desc.replace("\n", " ").split(".") if s.strip()]
    hook = ". ".join(sentences[:2]) + "." if sentences else title
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    hashtags = " ".join(f"#{t.replace(' ', '')}" for t in tag_list[:10])
    hashtags += f" {COMMON_HASHTAGS}"
    return f"{hook}\n\n✨ Get it instantly as a digital download!\n👇 Link in bio or search on Etsy: \"{title[:40]}\"\n\n{hashtags}"

def make_pinterest_description(title, desc, tags, etsy_url):
    normalized_desc, _ = _normalize_pinterest_description(
        title, desc, tags, etsy_url, max_length=PINTEREST_DESCRIPTION_MAX_LENGTH
    )
    return normalized_desc

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

def make_medium_intro(title, desc, tags, etsy_url, *, include_heading=True):
    """Compatibility wrapper for the shared Medium article builder."""
    return make_medium_research_article(
        title,
        desc,
        tags,
        etsy_url,
        include_heading=include_heading,
    )

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

async def _wait_for_confirmation(page, selectors, url_pattern=None, timeout=15000):
    """Require a visible success signal or a platform-specific published URL."""
    deadline = asyncio.get_running_loop().time() + timeout / 1000
    while asyncio.get_running_loop().time() < deadline:
        if url_pattern and re.search(url_pattern, page.url):
            return True
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    return True
            except Exception:
                pass
        await page.wait_for_timeout(350)
    return False


async def _is_locator_disabled(locator) -> bool:
    try:
        if await locator.is_disabled():
            return True
    except Exception:
        pass

    try:
        aria_disabled = await locator.get_attribute("aria-disabled")
        if str(aria_disabled).strip().lower() == "true":
            return True
    except Exception:
        pass

    try:
        # Native boolean attributes are enabled by presence, including disabled="".
        return await locator.get_attribute("disabled") is not None
    except Exception:
        return False


async def _find_visible_locator(page, selectors):
    for selector in selectors:
        locator = page.locator(selector).first
        if not await locator.count():
            continue
        if await locator.is_visible():
            return locator
    return None


def _looks_like_pinterest_validation_message(text: str | None) -> str | None:
    if not text:
        return None
    normalized = " ".join(text.replace("\n", " ").split()).strip().lower()
    if not normalized:
        return None
    for keyword in PINTEREST_PUBLISH_ERROR_KEYWORDS:
        if keyword in normalized:
            return normalized
    return None


async def _read_pinterest_validation_text(page) -> str | None:
    validation_selectors = [
        '[role="alert"]',
        '[role="status"]',
        '[aria-live="polite"]',
        '[aria-live="assertive"]',
        '[data-test-id*="error" i]',
    ]
    for selector in validation_selectors:
        locator = page.locator(selector).first
        if not await locator.count() or not await locator.is_visible():
            continue
        text = (await locator.text_content()) or ""
        maybe_error = _looks_like_pinterest_validation_message(text)
        if maybe_error:
            return text.strip()
    return None


def _extract_pin_url(url: str | None) -> str | None:
    if not url:
        return None

    candidate = str(url).strip()
    if not candidate:
        return None
    parsed_candidate = urlsplit(candidate)

    if candidate.startswith("/") and re.match(PINTEREST_PUBLISH_URL_PATTERN, candidate):
        return f"https://www.pinterest.com{parsed_candidate.path}"

    if re.match(PINTEREST_PUBLISH_ABSOLUTE_URL_PATTERN, candidate):
        normalized = f"{parsed_candidate.scheme}://{parsed_candidate.netloc}{parsed_candidate.path}"
        return normalized

    return None


async def _read_pinterest_confirmation_pin_url(page) -> str | None:
    pin_link_selectors = [
        '[role="link"]:has-text("See your Pin")',
        'a:has-text("See your Pin")',
    ]
    for selector in pin_link_selectors:
        locator = page.locator(selector).first
        if not await locator.count() or not await locator.is_visible():
            continue

        pin_href = (await locator.get_attribute("href")) or ""
        extracted = _extract_pin_url(pin_href)
        if extracted:
            return extracted

    return None


async def _wait_for_pinterest_publish_result(page, *, timeout_ms: int = 20000, poll_ms: int = 350):
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    success_text = "You created a Pin!"
    success_selectors = [
        '[role="alert"]',
        '[role="status"]',
        '[aria-live="polite"]',
        '[aria-live="assertive"]',
    ]
    while asyncio.get_running_loop().time() < deadline:
        page_pin_url = _extract_pin_url(page.url)
        if page_pin_url:
            return True, page_pin_url

        for selector in success_selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    raw_text = (await locator.text_content()) or ""
                    normalized = _normalize_text(raw_text).lower()
                    if normalized == success_text.lower():
                        return True, _extract_pin_url(page.url)
            except Exception:
                pass

        pin_url = await _read_pinterest_confirmation_pin_url(page)
        if pin_url:
            return True, pin_url

        validation_text = await _read_pinterest_validation_text(page)
        if validation_text:
            return False, validation_text
        await page.wait_for_timeout(poll_ms)
    return False, None


async def _fill_pinterest_title_with_dom_events(page, title: str) -> bool:
    """Fill the title without interpolating user data into JavaScript source."""
    script = """
        (value) => {
            const element = document.querySelector(
                '[data-testid*="title" i] input, '
                + 'input[placeholder*="title" i], '
                + 'textarea[placeholder*="title" i], '
                + 'input[type="text"]'
            );
            if (!element) return false;

            const prototype = element instanceof HTMLTextAreaElement
                ? HTMLTextAreaElement.prototype
                : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
            if (!setter) return false;

            setter.call(element, value);
            element.dispatchEvent(new Event("input", { bubbles: true }));
            element.dispatchEvent(new Event("change", { bubbles: true }));
            return element.value === value;
        }
    """
    try:
        return bool(await page.evaluate(script, title))
    except Exception:
        return False


async def _pinterest_board_needs_selection(trigger) -> bool:
    try:
        selected = await trigger.get_attribute("data-selected")
        if selected is not None:
            return str(selected).strip().lower() in ("false", "0")
    except Exception:
        pass

    labels = []
    for attribute in ("aria-label", "title"):
        try:
            labels.append((await trigger.get_attribute(attribute)) or "")
        except Exception:
            pass
    try:
        labels.append((await trigger.text_content()) or "")
    except Exception:
        pass

    selection_prompts = {
        "select",
        "select a board",
        "select board",
        "choose a board",
        "choose board",
        "chọn bảng",
    }
    return any(
        " ".join(label.split()).strip().lower() in selection_prompts
        for label in labels
    )


async def _select_default_pinterest_board_if_needed(page) -> bool:
    """Select the first board only when Pinterest explicitly requests one."""
    trigger_selectors = [
        '[data-test-id="board-dropdown-select-button"]',
        '[data-testid="board-dropdown-select-button"]',
        '[data-test-id="board-select"]',
        '[data-testid="board-select"]',
        '[role="button"][aria-label="Select board"]',
        '[role="button"][aria-label="Select a board"]',
        'button[aria-label="Select board"]',
        'button[aria-label="Select a board"]',
    ]
    trigger = await _find_visible_locator(page, trigger_selectors)
    if trigger is None or not await _pinterest_board_needs_selection(trigger):
        return False

    print("▶ Đang mở danh sách bảng (Board dropdown)...")
    await trigger.click()
    await page.wait_for_timeout(1500)

    board_option = await _find_visible_locator(
        page,
        [
            '[data-test-id="board-row"]',
            '[data-testid="board-row"]',
            '[role="option"]',
        ],
    )
    if board_option is None:
        return False

    board_name = ((await board_option.text_content()) or "").strip()
    if board_name:
        print(f"👉 Chọn bảng đầu tiên: '{board_name.splitlines()[0]}'")
    await board_option.click()
    await page.wait_for_timeout(1500)
    return True


async def _click_pinterest_publish(page):
    publish_selectors = [
        '[data-test-id="board-dropdown-save-button"]',
        '[data-testid="board-dropdown-save-button"]',
        'div[role="button"]:has-text("Publish")',
        'div[role="button"]:has-text("Đăng")',
        'div[role="button"]:has-text("Lưu")',
        '[data-test-id*="save-button" i]',
        '[data-testid*="save-button" i]',
        'button[aria-label="Publish"]',
        'button:has-text("Publish")',
        '[data-test-id*="publish" i] button',
        '[data-testid*="publish" i] button',
        'button[type="submit"]',
    ]

    publish_btn = await _find_visible_locator(page, publish_selectors)
    if publish_btn is None:
        return False, "❌ Không tìm thấy nút Publish trên Pinterest."

    if await _is_locator_disabled(publish_btn):
        validation_text = await _read_pinterest_validation_text(page)
        if validation_text:
            return False, validation_text
        return False, "Nút Publish trên Pinterest đang bị vô hiệu hóa."

    await publish_btn.scroll_into_view_if_needed()
    await page.wait_for_timeout(500)
    await publish_btn.click()
    return await _wait_for_pinterest_publish_result(page)


async def post_instagram(page, p):
    print("▶ Điều hướng tới Instagram...")
    await page.goto(SOCIAL_URLS["instagram"], wait_until="domcontentloaded")
    await page.wait_for_timeout(3500)
    if "accounts/login" in page.url or await page.locator('input[name="username"]').count():
        print("❌ Chưa đăng nhập Instagram trong session social của shop.")
        return False
    if not p["cover_image"]:
        print("❌ Không tìm thấy ảnh cover để đăng Instagram.")
        return False

    create_selectors = [
        '[aria-label="New post"]',
        '[aria-label="Bài viết mới"]',
        'a[href="#"]:has-text("Create")',
        'span:has-text("Create")',
        'span:has-text("Tạo")',
    ]
    opened = False
    for selector in create_selectors:
        try:
            control = page.locator(selector).first
            if await control.count() and await control.is_visible():
                await control.click()
                opened = True
                break
        except Exception:
            pass
    if not opened:
        print("❌ Không tìm thấy nút tạo bài viết Instagram.")
        return False

    file_input = page.locator('input[type="file"][accept*="image"]').first
    try:
        await file_input.wait_for(state="attached", timeout=10000)
        await file_input.set_input_files(p["cover_image"])
    except Exception as exc:
        print(f"❌ Không tải được ảnh lên Instagram: {exc}")
        return False

    for _ in range(2):
        await page.wait_for_timeout(1800)
        next_button = page.locator(
            'div[role="button"]:has-text("Next"), '
            'div[role="button"]:has-text("Tiếp"), '
            'button:has-text("Next"), button:has-text("Tiếp")'
        ).first
        if not await next_button.count() or not await next_button.is_visible():
            print("❌ Không tìm thấy bước Tiếp theo của Instagram.")
            return False
        await next_button.click()

    caption = make_instagram_caption(
        p["title"], p["desc"], p["tags"], p["etsy_url"]
    )
    caption_box = page.locator(
        'textarea[aria-label*="caption" i], '
        'textarea[aria-label*="chú thích" i], '
        'div[contenteditable="true"][role="textbox"]'
    ).first
    try:
        await caption_box.wait_for(state="visible", timeout=10000)
        await caption_box.fill(caption)
    except Exception as exc:
        print(f"❌ Không điền được caption Instagram: {exc}")
        return False

    share = page.locator(
        'div[role="button"]:has-text("Share"), '
        'div[role="button"]:has-text("Chia sẻ"), '
        'button:has-text("Share"), button:has-text("Chia sẻ")'
    ).first
    if not await share.count() or not await share.is_visible():
        print("❌ Không tìm thấy nút Share Instagram.")
        return False
    await share.click()
    confirmed = await _wait_for_confirmation(
        page,
        [
            'text="Your post has been shared."',
            'text="Đã chia sẻ bài viết của bạn."',
            '[role="dialog"]:has-text("Post shared")',
        ],
        timeout=25000,
    )
    if not confirmed:
        print("❌ Instagram chưa trả về xác nhận đã đăng; không ghi nhận thành công.")
        return False
    print("✅ Instagram xác nhận bài viết đã được chia sẻ.")
    return True

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
    normalized_title = _normalize_pinterest_title(p["title"])
    if normalized_title != p["title"]:
        print(
            f"⚠ Tiêu đề Pinterest vượt quá {PINTEREST_TITLE_MAX_LENGTH} ký tự, "
            f"đã cắt ngắn từ {len(p['title'])} xuống {len(normalized_title)} ký tự."
        )
    title_filled = False
    for sel in ['[data-testid*="title" i] input', 'input[placeholder*="title" i]', 'textarea[placeholder*="title" i]', 'input[type="text"]']:
        try:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.click()
                await el.fill(normalized_title)
                title_filled = True
                break
        except: pass
    if not title_filled:
        print("⚠ Không điền được tiêu đề bằng cách thường, thử DOM fallback an toàn...")
        title_filled = await _fill_pinterest_title_with_dom_events(
            page, normalized_title
        )
    if not title_filled:
        print("❌ Không thể điền tiêu đề Pinterest; dừng trước khi đăng.")
        return False
        
    # 3. Fill Description
    print("▶ Điền mô tả...")
    destination_link = p.get("shop_etsy_url") or p["etsy_url"]
    desc_text, was_desc_truncated = _normalize_pinterest_description(
        p["title"],
        p["desc"],
        p["tags"],
        destination_link,
        max_length=PINTEREST_DESCRIPTION_MAX_LENGTH,
    )
    if was_desc_truncated:
        print(
            f"⚠ Mô tả Pinterest vượt quá {PINTEREST_DESCRIPTION_MAX_LENGTH} "
            "ký tự, đã tự động rút ngắn."
        )

    if len(desc_text) > PINTEREST_DESCRIPTION_MAX_LENGTH:
        print("⚠ Mô tả Pinterest vẫn vượt quá giới hạn, bỏ qua bước điền mô tả.")
        return False
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
                    await el.fill(normalized_title[:500])
                    alt_filled = True
                    break
    except Exception as alt_err:
        print(f"⚠ Lỗi khi điền Alt Text: {alt_err}")

    # 5. Fill Destination Link
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
    
    # Chỉ chọn board khi Pinterest hiển thị đúng trigger "Select board".
    # Nếu đã có board, helper không thay đổi lựa chọn hiện tại.
    try:
        await _select_default_pinterest_board_if_needed(page)
    except Exception as board_err:
        print(f"  ⚠ Lỗi khi chọn bảng (bỏ qua và tự động dùng bảng mặc định): {board_err}")

    publish_ok, publish_result = await _click_pinterest_publish(page)
    if publish_ok:
        if publish_result:
            print(f"✅ Pinterest đã xác nhận Pin được xuất bản: {publish_result}")
        else:
            print("✅ Pinterest đã xác nhận Pin được xuất bản.")
        return True, publish_result
    if publish_result:
        print(f"❌ {publish_result}")
    else:
        print("❌ Pinterest chưa trả về xác nhận đã xuất bản.")

    return False, publish_result


async def _dismiss_x_overlays(page):
    """Wait for and dismiss X/Twitter's React Native Web overlay layers that intercept clicks."""
    overlay_sel = ".r-1p0dtai.r-1d2f490.r-1xcajam.r-zchlnj.r-ipm5af"
    try:
        await page.wait_for_selector(overlay_sel, state="hidden", timeout=6000)
    except Exception:
        # Overlay may persist — force-remove via JS so clicks land
        await page.evaluate(
            """(sel) => document.querySelectorAll(sel).forEach(el => el.remove())""",
            overlay_sel,
        )


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
    
    await _dismiss_x_overlays(page)
    textbox = page.locator('[data-testid="tweetTextarea_0"], .public-DraftEditor-content, [role="textbox"]').first
    await textbox.wait_for(state="visible", timeout=10000)
    await textbox.click(force=True)
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
    await _dismiss_x_overlays(page)
    post_btn = page.locator('[data-testid="tweetButton"], [data-testid="tweetButtonInline"], button:has-text("Post"), button:has-text("Tweet")').first
    if await post_btn.count() > 0 and await post_btn.is_visible():
        await post_btn.click(force=True)
        confirmed = await _wait_for_confirmation(
            page,
            [
                '[data-testid="toast"]:has-text("sent")',
                '[role="alert"]:has-text("sent")',
                '[role="alert"]:has-text("đã được gửi")',
            ],
            url_pattern=r"/status/\d+",
            timeout=18000,
        )
        if confirmed:
            print("✅ X/Twitter đã xác nhận bài viết được gửi.")
            return True
        print("❌ X/Twitter chưa trả về xác nhận đã đăng.")
        return False
    else:
        print("❌ Không tìm thấy nút Post/Tweet.")
        return False


MEDIUM_TITLE_SELECTOR = (
    'h3[placeholder="Title"], h3[class*="title"], h3[id*="title"]'
)
MEDIUM_BODY_SELECTORS = (
    'p[placeholder*="Tell your story" i]',
    '[data-testid="story-body"]',
    '[aria-label*="story" i][contenteditable="true"]',
    'section[class*="story" i] [contenteditable="true"]',
)


async def _locators_are_distinct(first, second) -> bool:
    """Return false when editor identity cannot be proven."""
    try:
        first_handle = await first.element_handle()
        second_handle = await second.element_handle()
        if first_handle is None or second_handle is None:
            return False
        return bool(
            await first_handle.evaluate(
                "(element, other) => element !== other", second_handle
            )
        )
    except Exception:
        return False


async def _resolve_medium_editors(page):
    """Resolve distinct Medium title/body editors, failing closed on ambiguity."""
    title_el = page.locator(MEDIUM_TITLE_SELECTOR).first
    if await title_el.count() > 0:
        for selector in MEDIUM_BODY_SELECTORS:
            body_el = page.locator(selector).first
            if await body_el.count() > 0 and await body_el.is_visible():
                if await _locators_are_distinct(title_el, body_el):
                    return title_el, body_el
        return None, None

    editable_nodes = page.locator('[contenteditable="true"]')
    if await editable_nodes.count() != 2:
        return None, None
    fallback_title = editable_nodes.nth(0)
    fallback_body = editable_nodes.nth(1)
    if not await _locators_are_distinct(fallback_title, fallback_body):
        return None, None
    return fallback_title, fallback_body


async def _read_medium_editor_text(locator) -> str:
    last_value = ""
    for reader_name in ("inner_text", "text_content", "input_value"):
        reader = getattr(locator, reader_name, None)
        if not callable(reader):
            continue
        try:
            value = await reader()
        except Exception:
            continue
        last_value = str(value or "")
        if last_value.strip():
            break
    return re.sub(r"\s+", " ", last_value).strip()


async def post_medium(page, p):
    print("▶ Điều hướng tới Medium New Story...")
    await page.goto("https://medium.com/new-story", wait_until="domcontentloaded")
    await page.wait_for_timeout(4000)
    
    if "login" in page.url or "m/signin" in page.url:
        print("❌ Chưa đăng nhập Medium! Vui lòng chạy MỞ_CHROME_DEBUG.command và đăng nhập vào Medium trước.")
        return False
        
    print("✓ Đã đăng nhập Medium.")
    
    # 1. Resolve distinct title/body editors without guessing an arbitrary node.
    title_el, body_el = await _resolve_medium_editors(page)
    if title_el is None or body_el is None:
        print("❌ Không xác định được editor tiêu đề/nội dung Medium an toàn; dừng trước khi đăng.")
        return False

    article_title = make_medium_article_title(p["title"], p["desc"], p["tags"])
    body_markdown = make_medium_intro(
        p["title"],
        p["desc"],
        p["tags"],
        p["etsy_url"],
        include_heading=False,
    )
    body_text = render_medium_plain_text(body_markdown)

    # 2. Fill Title
    print("▶ Điền tiêu đề Medium...")
    await title_el.wait_for(state="visible", timeout=10000)
    await title_el.click()
    await title_el.fill(article_title)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(1000)
    
    # 3. Fill Body as plain text so Medium does not interpret raw Markdown as
    # malformed editor content.
    print("▶ Điền nội dung bài viết...")
    await body_el.click()
    await body_el.fill(body_text)
    await page.wait_for_timeout(2000)

    # Fail closed if either editor did not retain the expected content. Do this
    # before opening the Publish menu.
    title_readback = await _read_medium_editor_text(title_el)
    body_readback = await _read_medium_editor_text(body_el)
    required_body_markers = ("Abstract", "Research Question", "Practical Method")
    if article_title not in title_readback or not all(
        marker in body_readback for marker in required_body_markers
    ):
        print("❌ Medium không giữ đúng tiêu đề/nội dung bài viết; dừng trước khi Publish.")
        return False
    
    # 4. Publish
    print("▶ Click Publish menu...")
    pub_menu = page.locator('button:has-text("Publish"), [data-action="publish-overlay"]').first
    if await pub_menu.count() > 0:
        await pub_menu.click()
        await page.wait_for_timeout(2000)
        
        print("▶ Click Publish Now thực tế...")
        pub_now = page.locator('button:has-text("Publish now"), button[class*="publish"]').first
        if await pub_now.count() > 0:
            await pub_now.click()
            confirmed = await _wait_for_confirmation(
                page,
                [
                    'text="Your story is published"',
                    '[role="alert"]:has-text("published")',
                ],
                url_pattern=r"medium\.com/(?!new-story).+",
                timeout=20000,
            )
            if confirmed:
                print("✅ Medium đã xác nhận bài viết được xuất bản.")
                return True
            print("❌ Medium chưa trả về xác nhận đã xuất bản.")
            return False
            
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
        print("⏳ Đang đợi Facebook xác nhận đăng tải...")
        confirmed = await _wait_for_confirmation(
            page,
            [
                '[role="alert"]:has-text("Your post is now published")',
                '[role="alert"]:has-text("Bài viết của bạn hiện đã được đăng")',
                '[role="status"]:has-text("published")',
                '[role="status"]:has-text("đã đăng")',
            ],
            timeout=25000,
        )
        if confirmed:
            print("✅ Facebook đã xác nhận bài viết được đăng.")
            return True
        print("❌ Facebook chưa trả về xác nhận đã đăng; không ghi nhận thành công.")
        return False
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
        print("⏳ Đang đợi Reddit xác nhận đăng bài...")
        confirmed = await _wait_for_confirmation(
            page,
            [
                '[role="alert"]:has-text("successfully")',
                '[role="status"]:has-text("posted")',
            ],
            url_pattern=r"/comments/[a-z0-9]+/",
            timeout=20000,
        )
        if confirmed:
            print("✅ Reddit đã xác nhận bài viết được đăng.")
            return True
        print("❌ Reddit chưa trả về xác nhận đã đăng.")
        return False
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
    
    try:
        session = load_social_session(BASE_DIR, args.shop)
    except (OSError, ValueError, KeyError) as exc:
        print(f"❌ Không đọc được session social riêng của shop: {exc}")
        sys.exit(1)

    print(
        f"🌐 Session riêng: port {session.debug_port} | "
        f"profile {session.profile_dir}"
    )
    if not is_session_ready(session):
        print("ℹ️ Chrome social của shop chưa mở. Đang mở đúng profile riêng...")
        try:
            ready = await asyncio.to_thread(
                open_social_browser, session, args.platform
            )
        except (FileNotFoundError, OSError, ValueError) as exc:
            print(f"❌ Không mở được Chrome social: {exc}")
            sys.exit(1)
        if not ready:
            print("❌ Chrome social chưa sẵn sàng trên đúng cổng debug của shop.")
            sys.exit(1)
        
    async with async_playwright() as pw:
        try:
            print(f"⏳ Kết nối Chrome social của shop tại cổng {session.debug_port}...")
            browser = await pw.chromium.connect_over_cdp(session.cdp_url)
        except Exception as exc:
            print(f"❌ Không kết nối được đúng Chrome social của shop: {exc}")
            sys.exit(1)
        if not browser.contexts:
            print("❌ Chrome social không có browser context.")
            sys.exit(1)
        ctx = browser.contexts[0]

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        
        success = False
        confirmation_detail = ""
        try:
            if args.platform == "instagram":
                success = await post_instagram(page, p)
            elif args.platform == "pinterest":
                pinterest_result = await post_pinterest(page, p)
                if isinstance(pinterest_result, tuple):
                    success, confirmation_detail = pinterest_result
                else:
                    success = bool(pinterest_result)
            elif args.platform == "twitter":
                success = await post_twitter(page, p)
            elif args.platform == "medium":
                success = await post_medium(page, p)
            elif args.platform == "facebook":
                success = await post_facebook(page, p)
            elif args.platform == "reddit":
                success = await post_reddit(page, p, args.subreddit)
            else:
                print(f"❌ Nền tảng '{args.platform}' không được hỗ trợ.")
                success = False
        except Exception as e:
            print(f"❌ Xảy ra lỗi ngoài ý muốn: {e}")
            import traceback
            traceback.print_exc()
            success = False
        finally:
            # This Chrome is the user's persistent login browser. Leaving the
            # CDP connection must not close its context or delete lock files.
            pass
            
    if success:
        confirmation_url = (
            confirmation_detail
            if str(confirmation_detail).startswith(("http://", "https://"))
            else ""
        )
        try:
            record_social_post(
                BASE_DIR,
                args.shop,
                p["folder"],
                args.row,
                args.platform,
                url=confirmation_url,
                detail=confirmation_detail
                or f"{args.platform.upper()} đã xác nhận xuất bản",
            )
            print("🧾 Đã lưu trạng thái social theo shop, sản phẩm và kênh.")
        except Exception as record_error:
            print(
                "⚠ Đã đăng thành công nhưng không lưu được trạng thái social: "
                f"{record_error}"
            )
        print(f"\n🎉 [HOÀN TẤT] Tự động đăng {args.platform.upper()} thành công!")
        sys.exit(0)
    else:
        print(f"\n❌ [THẤT BẠI] Đăng {args.platform.upper()} thất bại.")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
