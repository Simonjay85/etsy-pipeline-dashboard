"""
Etsy Auto Draft Poster
• Dùng Chrome thật (tránh bot detection)
• Hỗ trợ form mới dạng tab của Etsy
• Tự upload ảnh + PDF
• Tự dịch 9 ngôn ngữ (title + description + tags)
• Chọn Shop Section từ Excel
• Delay hợp lý để tránh lỗi
Dùng: python3 etsy_auto_post.py [--batch 5] [--skip N]
"""
import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
import re
from difflib import SequenceMatcher

# ── Auto-install ───────────────────────────────────────────────────────────────
def ensure_deps():
    pkgs = {"openpyxl": "openpyxl", "playwright": "playwright", "deep_translator": "deep-translator"}
    for mod, pkg in pkgs.items():
        try:
            __import__(mod)
        except ImportError:
            print(f"▶ Cài {pkg}...")
            subprocess.run([sys.executable, "-m", "pip", "install", pkg, "--quiet"], check=True)
    try:
        from google import genai
    except ImportError:
        print("▶ Cài google-genai...")
        subprocess.run([sys.executable, "-m", "pip", "install", "google-genai", "--quiet"], check=True)

ensure_deps()

import openpyxl
from playwright.async_api import async_playwright
from deep_translator import GoogleTranslator

try:
    from google import genai
    from google.genai import types
    has_genai = True
except ImportError:
    has_genai = False

BASE_DIR    = Path(__file__).parent
SHOP_DIR    = BASE_DIR
EXCEL_FILE  = SHOP_DIR / "Etsy_SEO_Generator.xlsx"
CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
CONFIG_FILE = BASE_DIR / "shops_config.json"
SHOPS      = {}

def _load_shop_configs(config_file: Path = CONFIG_FILE) -> dict:
    if not config_file.exists():
        return {}
    try:
        with config_file.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                return loaded
    except Exception:
        pass
    return {}

SHOPS = _load_shop_configs()

def _expand_user_path(value: str, home_dir: Path) -> Path:
    raw = str(value or "").strip()
    if not raw:
        return Path(raw)
    if raw.startswith("~"):
        return Path(str(home_dir) + raw[1:])
    return Path(raw)

def resolve_browser_session_dir(
    shop_id: str,
    *,
    config: dict | None = None,
    base_dir: Path | None = None,
    home_dir: Path | None = None,
) -> Path:
    target_shop = str(shop_id or "").strip()
    target_shop_key = target_shop.lower()
    shops_config = config if config is not None else SHOPS
    target_shop_cfg = shops_config.get(target_shop, {}) if isinstance(shops_config, dict) else {}
    raw_session = str(target_shop_cfg.get("browser_session", "")).strip()
    base_root = base_dir or BASE_DIR
    home_root = home_dir or Path.home()

    if raw_session:
        return _expand_user_path(raw_session, home_root)

    legacy_session = base_root / ".browser-session"
    if target_shop_key == "templystudios" and legacy_session.exists():
        return legacy_session

    return home_root / f".etsy_browser_session_{target_shop}"

def _is_profile_in_use(profile_dir: Path) -> tuple[bool, Path | None]:
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock_path = profile_dir / lock_name
        if lock_path.exists():
            return True, lock_path
    return False, None

# ── Config ─────────────────────────────────────────────────────────────────────
FILL_TRANSLATIONS = True
DEFAULT_BATCH     = 5
DRAFT_LISTINGS_URL = "https://www.etsy.com/your/shops/me/tools/listings/page:1,state:draft"
DRAFT_FILTER_POLL_MS = 250
DRAFT_FILTER_CHECK_TIMEOUT_MS = 1200
DRAFT_FILTER_FORCE_ATTEMPTS = 10
UNVERIFIED_DRAFT_URL_SENTINEL = "<URL chưa xác minh>"
DRAFT_DUPLICATE_CHECK_FAILED_SENTINEL = "<DRAFT_DUPLICATE_CHECK_FAILED>"

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

def trim_title(title: str, max_len: int = 140) -> str:
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

# ── Alt text generator ────────────────────────────────────────────────────────
def generate_alt_texts(title: str, keywords: str, count: int) -> list:
    """Tạo alt text cho từng ảnh dựa vào title và keywords (tối đa 250 ký tự/ảnh)."""
    clean_title = title[:200].strip()
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]

    suffixes = [
        "instant download",
        "printable PDF",
        "digital download",
        "printable template",
        "digital planner",
        "PDF download",
        "printable download",
        "digital file",
        "instant printable",
        "editable template",
    ]

    alt_texts = []
    for i in range(count):
        if i == 0:
            # Ảnh đầu tiên dùng full title
            alt_texts.append(clean_title[:250])
        else:
            suffix = suffixes[i % len(suffixes)]
            if kw_list:
                kw = kw_list[i % len(kw_list)]
                text = f"{kw} - {suffix}"
            else:
                text = f"{clean_title[:200]} - {suffix}"
            alt_texts.append(text[:250])
    return alt_texts

# ── Tag cleaner ───────────────────────────────────────────────────────────────
def clean_tags(raw: str, max_tags: int = 13) -> list:
    """Chuẩn hoá danh sách tag: mỗi tag 1-20 ký tự, tối đa max_tags tag duy nhất (case-insensitive)."""
    result = []
    seen = set()
    for t in str(raw or "").split(","):
        t = t.strip()
        if not t:
            continue
        if 1 <= len(t) <= 20:
            tag_str = t
        else:
            tag_str = t[:20].rsplit(" ", 1)[0].rstrip(",- ")
        if 1 <= len(tag_str) <= 20:
            key = tag_str.lower()
            if key not in seen:
                seen.add(key)
                result.append(tag_str)
    return result[:max_tags]


def normalize_category_text(value: str) -> str:
    """Chuẩn hóa chữ cho so sánh danh mục."""
    return re.sub(r"\s+", " ", str(value or "").lower().replace("✓", " ").strip())


def category_option_matches(target_category: str, option_text: str) -> bool:
    """Cho phép khớp đúng leaf category, hoặc leaf + suffix metadata hợp lệ."""
    target_norm = normalize_category_text(target_category)
    option_norm = normalize_category_text(option_text)
    if not target_norm or not option_norm:
        return False
    if option_norm == target_norm:
        return True
    allowed_suffixes = ("digital", "physical", "physical or digital")
    return any(option_norm == f"{target_norm} {suffix}" for suffix in allowed_suffixes)


def extract_category_leaf(category_value: str) -> str:
    """Lấy leaf name từ chuỗi danh mục dạng 'A > B > C' hoặc path."
    """
    category_value = str(category_value or "").strip()
    if not category_value:
        return ""
    # Ưu tiên tách bằng các delimiter phổ biến của breadcrumb
    parts = re.split(r"\s*>\s*|\s*/\s*|\\", category_value)
    leaf = parts[-1] if parts else category_value
    return leaf.replace("✓", "").strip()


def infer_listing_category(product: dict) -> str:
    """Suy luận danh mục từ nội dung sản phẩm (title/keywords/tags/description)."""
    combined = " ".join([
        str(product.get("title", "")),
        str(product.get("keywords", "")),
        str(product.get("tags", "")),
        str(product.get("description", "")),
    ]).lower()
    norm = re.sub(r"[^a-z0-9]+", " ", combined)

    if re.search(r"\b(svg|dxf|eps|vector|cut file|cricut|silhouette)\b", norm):
        return "Cutting Machine Files"
    if re.search(r"\b(resume|curriculum)\b|\bcv\b", norm):
        return "Résumé Templates"
    if re.search(r"\b(planner|journal|calendar|workbook|worksheet|checklist)\b", norm):
        return "Planner Templates"
    return ""


def resolve_listing_category(product: dict) -> str:
    """Giải quyết danh mục cuối cùng để chọn: ưu tiên category trong workbook, nếu trống thì suy luận."""
    explicit = extract_category_leaf(product.get("category", ""))
    if explicit:
        return explicit
    return infer_listing_category(product)


def normalize_search_text(value: str) -> str:
    """Chuẩn hóa chuỗi cho so khớp tiêu đề/lưu trữ."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip())


def normalize_sku(value: str) -> str:
    """Chuẩn hóa SKU để so sánh chính xác."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


async def _force_draft_filter(page):
    """Điều hướng về drafts và ép filter về Draft status an toàn trước khi đọc listings."""
    print("  🔧 Ép bộ lọc Draft để đọc đúng danh sách.")
    await page.goto(DRAFT_LISTINGS_URL, wait_until="domcontentloaded")
    for _ in range(24):
        status_state = await _collect_status_radio_state(page)
        if status_state["radio_count"] > 0 and status_state["draft_indexes"]:
            break
        await page.wait_for_timeout(250)

    status_state = await _collect_status_radio_state(page)
    if status_state["radio_count"] == 0:
        raise RuntimeError("Không tìm thấy bộ lọc Draft trên danh sách listings.")

    all_status_locator = page.locator('input[name="item_status"]')
    for _ in range(DRAFT_FILTER_FORCE_ATTEMPTS):
        status_state = await _collect_status_radio_state(page)
        if _is_exact_draft_filter(status_state):
            return

        if not status_state["draft_indexes"]:
            await page.wait_for_timeout(DRAFT_FILTER_POLL_MS)
            continue

        # Ưu tiên radio Draft đang enabled để tránh lỗi intercept/pointer events.
        draft_radio_index = None
        for idx in status_state["draft_indexes"]:
            if status_state["radios"][idx]["enabled"]:
                draft_radio_index = idx
                break

        if draft_radio_index is None:
            await page.wait_for_timeout(DRAFT_FILTER_POLL_MS)
            continue

        draft_radio = all_status_locator.nth(draft_radio_index)
        try:
            await draft_radio.check(force=True, timeout=DRAFT_FILTER_CHECK_TIMEOUT_MS)
        except Exception as err:
            raise RuntimeError(f"Không thể bật radio Draft: {err}")

        for _ in range(3):
            status_state = await _collect_status_radio_state(page)
            if _is_exact_draft_filter(status_state):
                return
            await page.wait_for_timeout(DRAFT_FILTER_POLL_MS)

    raise RuntimeError("Không xác nhận được filter Draft đã được bật.")


def _is_exact_draft_filter(state: dict) -> bool:
    checked = state.get("checked_values", [])
    return len(checked) == 1 and checked[0] == "draft"


async def _collect_status_radio_state(page) -> dict:
    """Thu thập trạng thái nhóm filter item_status trước khi quét listing."""
    status_locator = page.locator('input[name="item_status"]')
    status_count = await status_locator.count()
    radios = []
    draft_indexes = []
    for i in range(status_count):
        loc = status_locator.nth(i)
        raw_value = await loc.get_attribute("value")
        value = (raw_value or "").strip().lower()
        checked = bool(await loc.is_checked())
        enabled = bool(await loc.is_enabled())
        radio = {
            "value": value,
            "checked": checked,
            "enabled": enabled,
        }
        radios.append(radio)
        if value == "draft":
            draft_indexes.append(i)
    checked_values = sorted([r["value"] for r in radios if r.get("checked") and r.get("value")])

    return {
        "radio_count": status_count,
        "radios": radios,
        "draft_indexes": draft_indexes,
        "checked_count": len(checked_values),
        "checked_values": checked_values,
    }


async def _ensure_draft_filter_after_grid(page) -> None:
    status_state = await _collect_status_radio_state(page)
    if not _is_exact_draft_filter(status_state):
        raise RuntimeError(
            "Lọc Draft không còn chính xác sau khi grid ổn định. "
            "Dừng để tránh xác minh trùng lặp sai."
        )


def _normalize_listing_field(value) -> str:
    v = str(value or "").strip()
    return v


def _is_meaningful_title(title: str) -> bool:
    """Loại các text UI không phải tiêu đề sản phẩm thực tế."""
    norm = normalize_category_text(title)
    if not norm:
        return False
    if len(norm) <= 2:
        return False
    banned = {
        "edit",
        "view",
        "save",
        "edit listing",
        "preview",
        "delete",
        "listings",
    }
    return norm not in banned and norm != "actions"


def _pick_best_title(existing_title: str, incoming_title: str) -> str:
    """
    Chọn tiêu đề đáng tin nhất khi cùng một listing ID bị trùng.
    Ưu tiên meaningful và dài hơn.
    """
    cur = existing_title or ""
    inc = incoming_title or ""

    cur_norm = normalize_category_text(cur)
    inc_norm = normalize_category_text(inc)
    cur_meaningful = _is_meaningful_title(cur_norm)
    inc_meaningful = _is_meaningful_title(inc_norm)

    if cur_meaningful != inc_meaningful:
        return inc if inc_meaningful else cur

    if inc_meaningful and len(inc_norm) > len(cur_norm):
        return inc

    return cur


def _dedupe_draft_cards(cards: list[dict]) -> list[dict]:
    """
    Deduplicate listings by ID and merge sparse fields (title/SKU) so we don't
    drop information due to one imperfect DOM snapshot.
    """
    merged: dict[str, dict] = {}
    for raw in cards:
        if not isinstance(raw, dict):
            continue
        card_id = _normalize_listing_field(raw.get("id"))
        if not card_id:
            continue

        existing = merged.get(card_id)
        if existing is None:
            merged[card_id] = {**raw}
            merged[card_id]["id"] = card_id
            continue

        for key, value in raw.items():
            if key == "id":
                continue
            if key == "title":
                existing["title"] = _pick_best_title(str(existing.get("title", "")), str(value))
                continue

            existing_norm = _normalize_listing_field(existing.get(key))
            incoming_norm = _normalize_listing_field(value)
            if not existing_norm and incoming_norm:
                existing[key] = value

        if _normalize_listing_field(existing.get("status")).lower() != "draft":
            incoming_status = _normalize_listing_field(raw.get("status")).lower()
            if incoming_status == "draft":
                existing["status"] = raw.get("status")

    return list(merged.values())


def _get_media_thumbnail_selectors() -> list[str]:
    """Primary selectors for uploaded listing photos (delete/remove controls)."""
    return [
        'button[data-testid="image-delete-button"]',
        '[data-testid*="photo" i] button[aria-label*="Remove" i]',
        '[data-testid*="photo" i] button[aria-label*="Delete" i]',
    ]


def _get_media_thumbnail_fallback_selectors() -> list[str]:
    """Legacy thumbnail tiles — may include the empty upload slot."""
    return [
        "button.le-aspect-ratio--square",
    ]


PHOTO_UPLOAD_BATCH_SIZE = 5
PHOTO_UPLOAD_WAIT_MS = 90000


async def _count_listing_image_thumbs(page) -> int:
    """Đếm ảnh listing đã upload trên UI.

    Prefer delete/remove buttons. Square aspect-ratio tiles can include Etsy's
    empty upload slot, which falsely inflates exact-count checks (e.g. 11 vs 10).
    """
    primary_counts: list[int] = []
    for sel in _get_media_thumbnail_selectors():
        try:
            primary_counts.append(await page.locator(sel).count())
        except Exception:
            continue
    primary = max(primary_counts or [0])
    if primary > 0:
        return primary

    fallback_counts: list[int] = []
    for sel in _get_media_thumbnail_fallback_selectors():
        try:
            fallback_counts.append(await page.locator(sel).count())
        except Exception:
            continue
    return max(fallback_counts or [0])


async def _wait_for_expected_image_count(page, expected_count: int, exact: bool = False,
                                        timeout_ms: int = PHOTO_UPLOAD_WAIT_MS,
                                        log_progress: bool = False) -> bool:
    checks = max(1, timeout_ms // 500)
    last_count = -1
    for _ in range(checks):
        try:
            current = await _count_listing_image_thumbs(page)
        except Exception:
            current = 0

        if exact:
            if current == expected_count:
                return True
        elif current >= expected_count:
            return True

        if current != last_count:
            if log_progress and current >= 0:
                print(f"  ⏳ Ảnh trên UI: {current}/{expected_count}")
            last_count = current
        await page.wait_for_timeout(500)

    return False


async def _upload_listing_photos(page, paths: list[str]) -> None:
    """Upload listing photos using Etsy listing-media input + file-chooser fallbacks."""
    if not paths:
        return

    upload_selectors = [
        '[data-testid="empty-photo-thumbnail"] input[name="listing-media-upload"]',
        'input[name="listing-media-upload"]',
        'input[type="file"][accept*="image"]',
        'input[type="file"][accept*="jpeg"]',
        'label[for="listing-photos"] ~ * input[type="file"]',
        'input[type="file"]',
    ]
    for sel in upload_selectors:
        fi = page.locator(sel).first
        if await fi.count() == 0:
            continue
        try:
            await fi.wait_for(state="attached", timeout=15000)
            await fi.set_input_files(paths, timeout=60000)
            return
        except Exception as direct_err:
            print(f"  ⚠️ Input upload ảnh thất bại ({sel}): {direct_err}")

    add_photo_selectors = [
        'button:has-text("Add photos")',
        'button:has-text("Add photo")',
        'button:has-text("Upload photos")',
        'button:has-text("Upload photo")',
        'button[aria-label*="Add photo" i]',
        'button[aria-label*="Upload photo" i]',
        '[role="button"]:has-text("Add photos")',
        '[role="button"]:has-text("Add photo")',
        'label:has-text("Add photos")',
        'label:has-text("Add photo")',
    ]
    for sel in add_photo_selectors:
        add_btn = page.locator(sel).first
        if await add_btn.count() == 0:
            continue
        try:
            if not await add_btn.is_visible():
                continue
            await add_btn.scroll_into_view_if_needed()
            async with page.expect_file_chooser(timeout=15000) as fc_info:
                await add_btn.click()
            await fc_info.value.set_files(paths)
            return
        except Exception as chooser_err:
            print(f"  ⚠️ File chooser ảnh thất bại ({sel}): {chooser_err}")

    fi = page.locator('input[type="file"]').last
    if await fi.count() > 0:
        await fi.set_input_files(paths, timeout=60000)
        return

    raise RuntimeError("Không tìm thấy nút/input upload ảnh phù hợp trên form Etsy.")


async def _upload_listing_photos_until_count(
    page,
    paths: list[str],
    *,
    expected_total: int,
    exact: bool = True,
    batch_size: int = PHOTO_UPLOAD_BATCH_SIZE,
    timeout_ms: int = PHOTO_UPLOAD_WAIT_MS,
) -> int:
    """Upload in small batches and top-up missing images until UI count matches."""
    paths = [p for p in (paths or []) if p][:10]
    if not paths:
        return await _count_listing_image_thumbs(page)

    expected_total = max(1, min(int(expected_total), 10))
    batch_size = max(1, min(int(batch_size), 10))

    for start in range(0, len(paths), batch_size):
        batch = paths[start:start + batch_size]
        before = await _count_listing_image_thumbs(page)
        if before >= expected_total:
            break
        batch_target = min(expected_total, before + len(batch))
        print(f"  📤 Upload ảnh đợt {start // batch_size + 1}: {len(batch)} file (UI {before} → mục tiêu {batch_target})")
        await _upload_listing_photos(page, batch)
        await _wait_for_expected_image_count(
            page,
            expected_count=batch_target,
            exact=False,
            timeout_ms=timeout_ms,
            log_progress=True,
        )

    current = await _count_listing_image_thumbs(page)
    # Top-up once if Etsy silently dropped some large files from a multi-select.
    if current < expected_total:
        missing = expected_total - current
        retry_paths = paths[-missing:]
        print(f"  🔄 Thiếu {missing}/{expected_total} ảnh trên UI — upload bổ sung {len(retry_paths)} file...")
        await _upload_listing_photos(page, retry_paths)
        await _wait_for_expected_image_count(
            page,
            expected_count=expected_total,
            exact=False,
            timeout_ms=timeout_ms,
            log_progress=True,
        )
        current = await _count_listing_image_thumbs(page)

    if exact:
        ok = current == expected_total
    else:
        ok = current >= expected_total
    if not ok:
        raise RuntimeError(
            f"Số ảnh trên UI chưa đạt kỳ vọng. Expected_count={expected_total}"
            f", actual={current}, exact={exact}."
        )

    print(f"  📷 {current} ảnh ✓")
    return current


def _pick_draft_card_id(cards: list[dict], product: dict) -> str | None:
    """
    Chọn listing ID an toàn:
      1) khớp SKU exact nếu có,
      2) khớp title exact đã normalize,
      3) chỉ fuzzy nhất (không ambiguous), tỷ lệ cao.
    """
    target_title = normalize_search_text(product.get("title", ""))
    target_sku = normalize_sku(product.get("sku"))
    if not cards:
        return None

    draft_cards = []
    for c in cards:
        status = (c.get("status") or "").lower()
        if status and status != "draft":
            continue
        draft_cards.append(c)

    # Ưu tiên khớp SKU chính xác
    if target_sku:
        sku_exact = [c for c in draft_cards if normalize_sku(c.get("sku", "")) == target_sku]
        if len(sku_exact) == 1:
            return str(sku_exact[0]["id"])
        if len(sku_exact) > 1:
            print(f"  ⚠️ Trùng lặp SKU trong draft theo index ({len(sku_exact)} kết quả): {target_sku}")
            return None

    # Khớp title exact sau khi normalize
    if target_title:
        exact_title = [c for c in draft_cards if normalize_search_text(c.get("title", "")) == target_title]
        if len(exact_title) == 1:
            return str(exact_title[0]["id"])
        if len(exact_title) > 1:
            print(f"  ⚠️ Có {len(exact_title)} draft khớp tiêu đề exact; tránh tự động chọn để không sai.")
            return None

    # Fuzzy khớp conservatively, chỉ nhận khi best đủ áp đảo runner-up.
    best_match = None
    best_ratio = 0.0
    best_ratio_second = 0.0
    for c in draft_cards:
        title_norm = normalize_search_text(c.get("title", ""))
        if not title_norm or not target_title:
            continue
        ratio = SequenceMatcher(None, target_title, title_norm).ratio()
        if ratio <= 0.92:
            continue
        if ratio > best_ratio:
            best_ratio_second = best_ratio
            best_ratio = ratio
            best_match = c
        elif ratio > best_ratio_second:
            best_ratio_second = ratio

    if best_match is None:
        return None

    if (best_ratio - best_ratio_second) < 0.03:
        print(
            f"  ⚠️ Fuzzy khớp tiêu đề không đủ khoảng cách an toàn (best={best_ratio:.2f}, runner-up={best_ratio_second:.2f}); bỏ qua xác minh URL."
        )
        return None

    return str(best_match["id"])


async def _editor_product_signature_matches(page, product: dict) -> bool:
    """Chỉ tin ID trên URL nếu title/SKU trong editor khớp sản phẩm."""
    target_title = normalize_search_text(product.get("title", ""))
    target_sku = normalize_sku(product.get("sku"))
    try:
        sig = await page.evaluate(r'''() => {
            const titleEl = document.querySelector('#listing-title-input, textarea[name="title"]');
            const skuEl = document.querySelector('#listing-sku-input, input[name="sku"], [data-testid="sku-input"]');
            return {
                title: (titleEl && (titleEl.value || titleEl.innerText) || "").trim(),
                sku: (skuEl && skuEl.value || "").trim()
            };
        }''')
        editor_title = normalize_search_text(sig.get("title", ""))
        editor_sku = normalize_sku(sig.get("sku", ""))
        if not editor_title or editor_title != target_title:
            return False
        if target_sku:
            return editor_sku == target_sku and bool(editor_sku)
        return True
    except Exception:
        return False


async def _collect_draft_cards(page):
    """Lấy raw card data từ Drafts list sau khi đã ép filter."""
    await _force_draft_filter(page)
    await _wait_for_draft_grid_stable(page)
    await _ensure_draft_filter_after_grid(page)
    cards = await page.evaluate(r'''() => {
        let items = [];
        let anchors = Array.from(document.querySelectorAll('a[href*="/listing-editor/edit/"]'));
        for (let a of anchors) {
            let href = a.getAttribute('href') || '';
            let match = href.match(/\/listing-editor\/edit\/(\d+)/);
            if (!match) continue;
            let id = match[1];
            let row = a.closest('tr') || a.closest('[class*="card"]') || a.closest('div');
            let title = (a.innerText || '').trim();
            if (!title && row) {
                let titleEl = row.querySelector('[class*="title"], [class*="name"], h3, h4, [data-testid*="title"]');
                if (titleEl) title = (titleEl.innerText || '').trim();
            }
            if (!title && row) {
                let textNodes = Array.from(row.querySelectorAll('a,span,div')).map(el => (el.innerText || '').trim()).filter(Boolean);
                if (textNodes.length > 0) title = textNodes[0];
            }
            let rowText = row ? (row.innerText || '').toLowerCase() : '';
            let sku = '';
            if (rowText) {
                let skuMatch = rowText.match(/\bsku\b\s*[:#]?\s*([a-z0-9][a-z0-9_-]{2,})/i);
                if (skuMatch) sku = skuMatch[1];
            }
            if (!id) continue;
            items.push({ id: id, title: title || "", sku: sku || "", status: 'draft' });
        }
        return items;
    }''')
    return _dedupe_draft_cards(cards)


async def _wait_for_draft_grid_stable(
    page,
    max_wait_ms: int = 6000,
    pause_ms: int = 250,
    min_settle_ms: int = 400,
    stable_repeats: int = 6,
    empty_stable_repeats: int = 3,
    min_stable_wait_ms: int = 1500,
) -> None:
    """
    Chờ danh sách draft ổn định sau khi bật filter:
    - không loading
    - hoặc marker rỗng draft rõ ràng
    - hoặc tập ID không đổi liên tiếp cho danh sách không rỗng.
    """
    await page.wait_for_timeout(min_settle_ms)
    end_at = asyncio.get_running_loop().time() + max_wait_ms / 1000

    last_id_tuple = None
    stable_count = 0
    empty_count = 0
    min_stable_until = asyncio.get_running_loop().time() + min_stable_wait_ms / 1000
    state_script = r'''() => {
        const __etsyDraftGridStateMarker = true;
        const anchors = Array.from(document.querySelectorAll('a[href*="/listing-editor/edit/"]'));
        const re = /\/listing-editor\/edit\/(\d+)/;
        const ids = Array.from(new Set(
            anchors.map((a) => {
                const href = a.getAttribute("href") || "";
                const match = href.match(re);
                return match && match[1] ? match[1] : "";
            }).filter(Boolean)
        )).sort();

        const loadingNodes = Array.from(document.querySelectorAll(
            '[aria-busy="true"], .wt-spinner, .wt-loading, .loading-spinner, [data-state="loading"]'
        ));
        const loading = loadingNodes.some((n) => {
            const style = window.getComputedStyle(n);
            return n && n.offsetParent !== null && style && style.visibility !== "hidden" && style.display !== "none";
        });

        const emptyStateMarkers = Array.from(document.querySelectorAll('.wt-empty-state, [role="status"], [data-testid*="empty"]'));
        const emptyState = emptyStateMarkers.some((el) => {
            if (!el || el.offsetParent === null) return false;
            const text = (el.textContent || "").toLowerCase();
            return (
                text.includes("no listings") ||
                text.includes("no results") ||
                text.includes("you have no drafts") ||
                text.includes("nothing to show")
            );
        });

        return { loading, ids, emptyState };
    }'''

    while True:
        if asyncio.get_running_loop().time() > end_at:
            raise RuntimeError(
                "Quá thời gian chờ draft grid ổn định. "
                "Không thể xác nhận filter Draft an toàn trước khi quét listings."
            )

        try:
            state = await page.evaluate(state_script)
        except Exception:
            state = {"loading": True, "ids": [], "emptyState": False}

        ids = state.get("ids", [])
        if not isinstance(ids, list):
            ids = list(ids) if ids is not None else []
        id_tuple = tuple(sorted({str(i).strip() for i in ids if str(i).strip()}))
        loading = bool(state.get("loading", True))
        empty_state = bool(state.get("emptyState", False))
        now = asyncio.get_running_loop().time()

        if not loading:
            if id_tuple:
                if last_id_tuple is not None and id_tuple == last_id_tuple:
                    stable_count += 1
                    empty_count = 0
                    if stable_count >= stable_repeats and now >= min_stable_until:
                        return
                else:
                    stable_count = 1
                    last_id_tuple = id_tuple
                    empty_count = 0
            elif empty_state:
                empty_count += 1
                if empty_count >= empty_stable_repeats and now >= min_stable_until:
                    return
            else:
                empty_count = 0
                last_id_tuple = None
                stable_count = 0
        else:
            last_id_tuple = None
            stable_count = 0
            empty_count = 0

        await page.wait_for_timeout(pause_ms)


def _get_save_button_selector(explicit_edit: bool = False) -> str:
    """Return the appropriate save button selector for create vs explicit edit flow."""
    if explicit_edit:
        return (
            'button:has-text("Save changes"):visible, '
            'button:has-text("Save as draft"):visible, '
            'button:has-text("Save Draft"):visible, '
            'button:has-text("Save draft"):visible'
        )

    return (
        'button:has-text("Save draft"):visible, '
        'button:has-text("Save as draft"):visible, '
        'button:has-text("Save Draft"):visible'
    )
# ── Translate ──────────────────────────────────────────────────────────────────
def translate_text(text: str, lang: str, max_chars=4800) -> str:
    if not text: return ""
    try:
        if len(text) <= max_chars:
            return GoogleTranslator(source="en", target=lang).translate(text) or text
        parts, chunk = [], ""
        for para in text.split("\n\n"):
            if len(chunk) + len(para) + 2 > max_chars:
                if chunk:
                    parts.append(GoogleTranslator(source="en", target=lang).translate(chunk) or chunk)
                chunk = para
            else:
                chunk = (chunk + "\n\n" + para).strip()
        if chunk:
            parts.append(GoogleTranslator(source="en", target=lang).translate(chunk) or chunk)
        return "\n\n".join(parts)
    except Exception as e:
        print(f"    ⚠ dịch {lang}: {e}")
        return text

def translate_tag(tag: str, lang: str) -> str:
    """Dịch 1 tag, giữ ≤20 ký tự."""
    try:
        translated = GoogleTranslator(source="en", target=lang).translate(tag) or tag
        translated = translated.strip()
        if len(translated) > 20:
            short = translated[:20].rsplit(" ", 1)[0].rstrip(",- ")
            if 1 <= len(short) <= 20:
                translated = short
            else:
                translated = translated[:20].rstrip(",- ")
        return translated if 1 <= len(translated) <= 20 else tag[:20].rstrip(",- ")
    except Exception:
        return tag[:20].rstrip(",- ")

def get_sku_prefix(shop_id: str) -> str:
    shop_id_lower = str(shop_id or "templystudios").lower()
    if "temply" in shop_id_lower:
        return "TS"
    elif "daisy" in shop_id_lower:
        return "dd"
    else:
        return str(shop_id or "TS")[:2].upper()

def generate_sku(shop_id: str, folder_name: str) -> str:
    prefix = get_sku_prefix(shop_id)
    import re
    clean_folder = "".join(c if c.isalnum() else "_" for c in folder_name).lower()
    clean_folder = re.sub(r'_+', '_', clean_folder).strip('_')
    return f"{prefix}_{clean_folder}"


def _normalize_requested_products(raw_products: list[str] | None) -> list[str]:
    """Chuẩn hoá --products: hỗ trợ repeat + comma-separated; giữ đúng thứ tự, bỏ trùng."""
    if not raw_products:
        return []

    def _normalize_one_folder(raw: str) -> str:
        folder = str(raw).strip()
        if not folder:
            raise ValueError("folder trống không hợp lệ")
        if not re.fullmatch(r"product-\d+", folder):
            raise ValueError(f"Định dạng folder không hợp lệ: {folder}")
        return folder

    normalized = []
    seen = set()
    for raw in raw_products:
        if raw is None:
            continue
        items = str(raw).split(",")
        for item in items:
            folder = _normalize_one_folder(item)
            if not folder:
                continue
            if folder in seen:
                continue
            normalized.append(folder)
            seen.add(folder)
    return normalized


def _normalize_selected_products(raw_items: list[str] | None) -> list[tuple[int, str]]:
    if not raw_items:
        return []
    normalized = []
    seen_rows = set()
    seen_folders = set()
    for raw in raw_items:
        match = re.fullmatch(r"(\d+):(product-\d+)", str(raw or "").strip())
        if not match:
            raise ValueError(f"Cặp row:folder không hợp lệ: {raw}")
        row = int(match.group(1))
        folder = match.group(2)
        if row < 4:
            raise ValueError(f"Row không hợp lệ: {row}")
        if row in seen_rows or folder in seen_folders:
            raise ValueError(f"Cặp row/folder bị lặp: {raw}")
        normalized.append((row, folder))
        seen_rows.add(row)
        seen_folders.add(folder)
    return normalized


_LISTING_REFERENCE_RE = re.compile(r"/(?:listing/|listing-editor/edit/)(\d+)")


def _listing_reference_has_id(value: object) -> bool:
    text = str(value or "").strip()
    return bool(_LISTING_REFERENCE_RE.search(text) or re.fullmatch(r"\d+", text))


# ── Read Excel ─────────────────────────────────────────────────────────────────
def read_products(
    batch=DEFAULT_BATCH,
    skip=0,
    product_folder=None,
    shop_id="templystudios",
    product_folders: list[str] | None = None,
    selected_products: list[tuple[int, str]] | None = None,
):
    selected_products = list(selected_products or [])
    if product_folders is None:
        product_folders = []
    if product_folder:
        product_folders = [str(product_folder)] + product_folders
    requested_folders = _normalize_requested_products(product_folders)
    selected_by_row = {row: folder for row, folder in selected_products}
    selected_order = {row: index for index, (row, _) in enumerate(selected_products)}
    requested_set = set(requested_folders)

    wb = openpyxl.load_workbook(EXCEL_FILE, data_only=True)
    ws = wb["Listings"]
    all_products = []
    max_row = max(ws.max_row, 4)
    for row_num, row in enumerate(ws.iter_rows(min_row=4, max_row=max_row, values_only=True), start=4):
        cols = (list(row) + [None]*19)[:19]
        _, folder, _, _, price, category, _, title, description, tags, qty, _, when_made, status, section = cols[:15]
        etsy_reference = cols[15] if len(cols) > 15 else ""
        sku = cols[17] if len(cols) > 17 else ""
        if not folder or not title or str(title).startswith("←"):
            continue
        if selected_products:
            expected_folder = selected_by_row.get(row_num)
            if expected_folder is None:
                continue
            if str(folder) != expected_folder:
                raise RuntimeError(
                    f"❌ Row {row_num} đã đổi folder từ {expected_folder} thành {folder}, dừng để tránh đăng nhầm"
                )
        # If targeting a specific product, ignore status filter for that product
        if selected_products or requested_folders:
            if str(folder) not in requested_set:
                if not selected_products:
                    continue
            if status and "Đã đăng" in str(status):
                raise RuntimeError(
                    f"❌ {folder} đã có trạng thái '{status}', dừng để tránh đăng trùng"
                )
            if _listing_reference_has_id(etsy_reference):
                raise RuntimeError(
                    f"❌ {folder} đã có Etsy listing ID/URL, dừng để tránh đăng trùng"
                )
        elif status and "Đã đăng" in str(status):
            continue

        # Auto-generate default SKU if empty
        if not sku or not str(sku).strip():
            sku = generate_sku(shop_id, str(folder))

        img_dir  = SHOP_DIR / str(folder) / "images"
        file_dir = SHOP_DIR / str(folder) / "files"
        img_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

        img_paths = sorted([str(f) for f in img_dir.iterdir()
                            if f.suffix.lower() in img_exts])[:10] if img_dir.exists() else []
        # Collect all digital files: PDF + ZIP
        DIGITAL_EXTS = {".pdf", ".zip"}
        pdf_paths = sorted([
            str(f) for f in file_dir.iterdir()
            if f.suffix.lower() in DIGITAL_EXTS
        ]) if file_dir.exists() else []

        clean_title = trim_title(str(title))
        clean_keywords = str(cols[2] or "")  # column C = keywords

        all_products.append({
            "folder":      folder,
            "title":       clean_title,
            "description": str(description or ""),
            "price":       float(str(price)) if price is not None and str(price).strip() else 4.99,
            "category":    str(category or ""),
            "tags":        str(tags or ""),
            "qty":         int(float(str(qty))) if qty is not None and str(qty).strip() else 999,
            "when_made":   str(when_made) if when_made else "2020_2026",
            "section":     str(section).strip() if section else "",
            "sku":         str(sku).strip(),
            "image_paths": img_paths,
            "pdf_paths":   pdf_paths,
            "row":         row_num,
            "keywords":    clean_keywords,
            "alt_texts":   generate_alt_texts(clean_title, clean_keywords, len(img_paths)),
        })

    if selected_products:
        all_products = sorted(all_products, key=lambda item: selected_order[item["row"]])
    elif requested_folders:
        folder_index = {folder: idx for idx, folder in enumerate(requested_folders)}
        all_products = sorted(all_products, key=lambda item: folder_index[item["folder"]])
    else:
        all_products = all_products[skip: skip + batch]

    return all_products, wb, ws, len(all_products)

def save_status(wb, ws, row, text, url=None):
    ws.cell(row=row, column=14, value=text)
    if url:
        ws.cell(row=row, column=16, value=url)
    wb.save(EXCEL_FILE)

# ── Helpers ────────────────────────────────────────────────────────────────────
async def click_tab(page, *names):
    """Click tab theo tên (thử nhiều selector)."""
    for name in names:
        for sel in [
            f'[role="tab"]:has-text("{name}")',
            f'button:has-text("{name}")',
            f'a:has-text("{name}")',
            f'li:has-text("{name}")',
            f'[class*="tab"]:has-text("{name}")',
        ]:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.click()
                await page.wait_for_timeout(1500)
                return True
    return False

async def detect_form_type(page):
    """Phát hiện form Etsy dạng tab mới hay trang dài cũ."""
    new_form_selectors = [
        'a:has-text("Item Details")',
        'a:has-text("Pricing & Shipping")',
        'button:has-text("Item Details")',
        '[href*="details"]',
        '[href*="pricing"]',
    ]
    for sel in new_form_selectors:
        if await page.locator(sel).count() > 0:
            return "tabs"
    if await page.locator('#listing-title-input, textarea[name="title"]').count() > 0:
        return "single"
    return "tabs"

async def smart_fill(page, selector, value, timeout=6000):
    """Điền giá trị vào input/textarea, hỗ trợ React."""
    try:
        el = page.locator(selector).first
        await el.wait_for(state="visible", timeout=timeout)
        await el.scroll_into_view_if_needed()
        await page.wait_for_timeout(300)
        await el.click(click_count=3)
        await page.wait_for_timeout(200)
        await el.fill(str(value))
        await page.wait_for_timeout(300)
        return True
    except Exception as e:
        return False

async def dismiss_alerts(page):
    """Đóng các popup/alert của Etsy nếu có."""
    try:
        for sel in [
            '[role="alert"] button[aria-label*="close" i]',
            '[role="alert"] button[aria-label*="dismiss" i]',
            '.wt-alert button',
            '[data-clg-id="WtAlert"] button',
        ]:
            btns = page.locator(sel)
            cnt = await btns.count()
            for i in range(cnt):
                try:
                    btn = btns.nth(i)
                    if await btn.is_visible():
                        await btn.click()
                        await page.wait_for_timeout(500)
                except Exception:
                    pass
        await page.wait_for_timeout(600)
    except Exception:
        pass

async def clear_and_fill(el, value):
    """Clear và điền giá trị — tương thích mọi phiên bản Playwright."""
    await el.scroll_into_view_if_needed()
    await el.click(click_count=3)
    await el.fill(value)

# ── Fill alt text cho từng ảnh ────────────────────────────────────────────────
# ── Visual Alt Text Generator ──────────────────────────────────────────────────
def generate_visual_alt_text(img_path, title, keywords):
    """
    Sử dụng Gemini 2.5 Flash thông qua Vertex AI để phân tích hình ảnh và sinh Alt Text chuẩn SEO.
    Nếu thất bại hoặc không có thư viện, trả về None để dùng cơ chế fallback dựa trên text.
    """
    if not has_genai:
        print("      ⚠️ Không có thư viện google-genai. Sử dụng Text SEO fallback.")
        return None

    try:
        # Đọc ảnh dưới dạng bytes
        with open(img_path, "rb") as f:
            img_bytes = f.read()

        # Xác định mime_type
        suffix = Path(img_path).suffix.lower()
        mime_type = "image/png"
        if suffix in [".jpg", ".jpeg"]:
            mime_type = "image/jpeg"
        elif suffix == ".webp":
            mime_type = "image/webp"
        elif suffix == ".gif":
            mime_type = "image/gif"

        # Khởi tạo client Vertex AI
        client = genai.Client(vertexai=True, project="temply-ai-lab", location="us-central1")

        prompt = f"""
Analyze the provided product image and generate a highly descriptive, SEO-optimized Alt Text for Etsy.

Instructions:
1. Describe the layout, visual design, text, and specific pages or features shown in the image in detail.
2. Incorporate 1-2 relevant SEO keywords from the following list: {keywords}.
3. The description must be natural, engaging, and extremely helpful for visually impaired buyers.
4. Keep the length strictly under 250 characters.
5. Do NOT use generic phrases like "image of" or "screenshot of".
6. Output ONLY the raw alt text, no quotes, no markdown, no comments, just the plain text.

Listing Title: {title}
"""
        part = types.Part.from_bytes(
            data=img_bytes,
            mime_type=mime_type
        )

        import time
        max_retries = 3
        backoff_sec = 3
        response = None
        for attempt in range(max_retries + 1):
            try:
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[part, prompt]
                )
                break
            except Exception as e:
                err_msg = str(e).lower()
                if ("429" in err_msg or "resource exhausted" in err_msg or "resource_exhausted" in err_msg) and attempt < max_retries:
                    print(f"      ⚠️ Gặp lỗi 429 (Resource Exhausted). Đợi {backoff_sec}s và thử lại #{attempt + 1}...")
                    time.sleep(backoff_sec)
                    backoff_sec *= 2
                else:
                    raise e

        text = (response.text or "").strip()
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1].strip()
        if text.startswith("'") and text.endswith("'"):
            text = text[1:-1].strip()

        if len(text) > 250:
            text = text[:247] + "..."

        return text
    except Exception as e:
        print(f"      ❌ Lỗi khi phân tích ảnh qua Gemini: {e}")
        return None

# ── Fill alt text cho từng ảnh ────────────────────────────────────────────────
async def fill_image_alt_texts(page, product):
    """Điền alt text cho từng ảnh sau khi upload lên Etsy."""
    title = product.get("title", "")
    keywords = product.get("keywords", "")
    image_paths = product.get("image_paths", [])

    print(f"  ✍️ Bắt đầu điền Alt Text cho các ảnh...")
    await page.wait_for_timeout(3000)  # Đợi ảnh load/upload hoàn tất

    # Định vị các nút thumbnail hình vuông đại diện cho từng ảnh trên lưới
    thumb_btns = page.locator('button.le-aspect-ratio--square')
    cnt = await thumb_btns.count()
    print(f"    🔍 Tìm thấy {cnt} ảnh trên giao diện Etsy.")

    if cnt == 0:
        print("    ⚠️ Không tìm thấy ảnh nào trên giao diện để điền Alt Text.")
        return

    # Sinh danh sách Alt Text dạng văn bản dự phòng (fallback)
    fallback_alts = generate_alt_texts(title, keywords, cnt)
    filled = 0

    for i in range(cnt):
        print(f"\n    🎬 Xử lý ảnh #{i+1}...")
        btn = thumb_btns.nth(i)
        
        try:
            # 1. Hover và click thumbnail
            await btn.scroll_into_view_if_needed()
            await page.wait_for_timeout(400)
            await btn.hover()
            await page.wait_for_timeout(400)
            await btn.click(force=True)
            await page.wait_for_timeout(2000)

            # 2. Đợi nút "+ Alt text" hoặc "Alt text" trong dialog trung gian xuất hiện và click
            alt_btn = page.locator('div[role="dialog"] button:has-text("Alt text"), button:has-text("Alt text"), button:has-text("+ Alt text")').first
            try:
                await alt_btn.wait_for(state="visible", timeout=3000)
                await alt_btn.click()
                await page.wait_for_timeout(1500)
            except Exception as alt_btn_ex:
                print(f"      ❌ Không tìm thấy nút mở Alt Text trong dialog: {alt_btn_ex}")
                try:
                    await page.keyboard.press("Escape")
                except:
                    pass
                await page.wait_for_timeout(1000)
                continue

            # 3. Tạo nội dung Alt Text (Thử Gemini trước, nếu lỗi/thiếu ảnh thì dùng Fallback)
            alt_val = None
            if i < len(image_paths):
                img_path = image_paths[i]
                print(f"      📸 Đang phân tích ảnh cục bộ qua Gemini: {Path(img_path).name}...")
                try:
                    alt_val = await asyncio.wait_for(
                        asyncio.to_thread(generate_visual_alt_text, img_path, title, keywords),
                        timeout=15.0
                    )
                except asyncio.TimeoutError:
                    print("      ⚠️ Gemini API call timed out after 15 seconds. Using Text SEO fallback.")
                    alt_val = None
                except Exception as g_err:
                    print(f"      ⚠️ Lỗi khi gọi Gemini: {g_err}. Using Text SEO fallback.")
                    alt_val = None
            
            if not alt_val:
                alt_val = fallback_alts[i]
                print(f"      ⚠️ Sử dụng Alt Text văn bản SEO (fallback): '{alt_val[:50]}...'")
            else:
                print(f"      ✨ Sinh Alt Text bằng Gemini thành công: '{alt_val}'")

            # 4. Tìm textarea trong Alt Text modal và điền
            alt_input = page.locator('div[role="dialog"]:visible textarea, div[class*="dialog"]:visible textarea, [class*="modal"]:visible textarea').first
            await alt_input.wait_for(state="visible", timeout=3000)
            await alt_input.click()
            await alt_input.fill(alt_val)
            await page.wait_for_timeout(500)

            # 5. Click Apply để lưu Alt Text hiện tại
            apply_btn = page.locator('div[role="dialog"]:visible button:has-text("Apply")').first
            await apply_btn.wait_for(state="visible", timeout=2000)
            await apply_btn.click()
            await page.wait_for_timeout(500)
            
            # Đợi modal điền Alt Text đóng hoàn toàn
            await alt_input.wait_for(state="hidden", timeout=5000)

            # 6. Click Done để lưu và đóng dialog ảnh hiện tại
            done_btn = page.locator('div[role="dialog"]:visible button:has-text("Done")').first
            await done_btn.wait_for(state="visible", timeout=2000)
            await done_btn.click(force=True)
            await page.wait_for_timeout(1000)
            
            print(f"      ✓ Đã điền và lưu Alt Text cho ảnh #{i+1} thành công!")
            filled += 1

        except Exception as e:
            print(f"      ❌ Lỗi khi xử lý ảnh #{i+1}: {e}")
            try:
                # Tránh kẹt modal: Nhấn Escape 2 lần để đóng các dialog đang mở
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(1000)
                await page.keyboard.press("Escape")
            except:
                pass
            await page.wait_for_timeout(1000)

    if filled > 0:
        print(f"  🖼️  Alt text: Đã điền thành công {filled}/{cnt} ảnh ✓")
    else:
        print("  ⚠️ Alt text: không điền được ảnh nào — bỏ qua")


# ── Tab: Photo & Video ─────────────────────────────────────────────────────────
async def fill_photo_tab(page, product, explicit_edit: bool = False):
    await click_tab(page, "Photo & Video", "Photos")
    await page.wait_for_timeout(800)
    paths = list(product.get("image_paths") or [])[:10]
    if not paths:
        return

    before = await _count_listing_image_thumbs(page)
    if not explicit_edit:
        expected_total = len(paths)
        exact_expected = True
    else:
        local_target = len(paths)
        expected_total = max(before, local_target)
        exact_expected = False

    await _upload_listing_photos_until_count(
        page,
        paths,
        expected_total=expected_total,
        exact=exact_expected,
    )

    # Điền alt text sau khi ảnh đã upload xong
    await fill_image_alt_texts(page, product)

# ── Tab: Category ──────────────────────────────────────────────────────────────
async def fill_category_tab(page, product):
    await click_tab(page, "Category")
    await page.wait_for_timeout(800)

    category_str = resolve_listing_category(product)
    if not category_str:
        raise RuntimeError(
            f"Không suy luận được danh mục cho '{product.get('title', '').strip()}'. "
            f"Vui lòng điền cột Category."
        )
    if product.get("category"):
        print(f"  📂 Sử dụng category từ workbook: '{category_str}'")
    else:
        print(f"  💡 Cột Category trống. Tự động suy luận danh mục: '{category_str}'")

    search_term = extract_category_leaf(category_str).strip()
    if not search_term:
        raise RuntimeError(f"Category sau khi chuẩn hóa rỗng: '{category_str}'")

    print(f"  🔍 Tìm kiếm danh mục cho term: '{search_term}'...")

    cat_input = page.locator(
        '#category-field-search, '
        '#listing-editor_category-search-typeahead, '
        'input[placeholder*="Examples:"], '
        'input.wt-input.le-category-search__input[placeholder*="Type to search" i], '
        'input[placeholder*="category" i], '
        'input[role="combobox"][placeholder*="Type to search" i], '
        'input[aria-label*="category" i]'
    ).first

    await cat_input.wait_for(state="visible", timeout=6000)
    await cat_input.click()
    await cat_input.fill("")
    await cat_input.fill(search_term)

    target_norm = normalize_category_text(search_term)
    selected = False
    options_selector = '[role="option"], li[class*="option"], li[class*="result"], div[role="option"]'
    for _ in range(15):
        options = page.locator(options_selector)
        option_count = await options.count()
        for idx in range(option_count):
            opt = options.nth(idx)
            if not await opt.count() > 0:
                continue
            if not await opt.is_visible():
                continue
            opt_text = (await opt.inner_text()) or ""
            if category_option_matches(search_term, opt_text):
                await opt.click()
                selected = True
                break
        if selected:
            break
        await page.wait_for_timeout(300)

    if not selected:
        raise RuntimeError(
            f"Không tìm thấy tùy chọn danh mục chính xác cho '{search_term}'. "
            f"Giá trị gợi ý không khớp (target_norm='{target_norm}')."
        )

    # Verify selected category is visibly reflected in UI
    matched = False
    for _ in range(12):
        current_text = ""
        try:
            current_text = (await cat_input.input_value()).strip()
        except Exception:
            pass
        if normalize_category_text(current_text) == target_norm:
            matched = True
            break
        await page.wait_for_timeout(300)

    if not matched:
        raise RuntimeError(f"Không xác nhận được danh mục đã chọn: '{search_term}'.")

    print(f"  📂 Category: {search_term} ✓")

# ── Tab: Item Details ──────────────────────────────────────────────────────────
async def fill_item_details_tab(page, product):
    await dismiss_alerts(page)
    await click_tab(page, "Item Details", "Details")
    await page.wait_for_timeout(1200)
    await dismiss_alerts(page)

    # Title
    if await smart_fill(page, '#listing-title-input, textarea[name="title"]', product["title"]):
        print("  📝 Title ✓")
    await page.wait_for_timeout(500)

    # Description
    if await smart_fill(page, '#listing-description-textarea, textarea[name="description"]',
                        product["description"], timeout=6000):
        print("  📄 Description ✓")
    await page.wait_for_timeout(500)

    # Tags (EN)
    try:
        await dismiss_alerts(page)
        # Delete existing tags first to make room
        try:
            await page.evaluate('''() => {
                let removeButtons = Array.from(document.querySelectorAll('span.wt-tag button, [data-testid="tag-pill"] button, button[aria-label*="remove" i], button[aria-label*="delete" i], button[class*="remove" i]'));
                for (let btn of removeButtons) {
                    btn.click();
                }
            }''')
            await page.wait_for_timeout(1000)
        except Exception as tag_clear_ex:
            print(f"  ⚠ Lỗi xóa tag cũ: {tag_clear_ex}")

        tag_list = clean_tags(product["tags"])
        filled = skipped = 0
        for tag in tag_list:
            await dismiss_alerts(page)
            tag_el = page.locator('#listing-tags-input, input[placeholder*="tag" i]').first
            if await tag_el.is_visible() and await tag_el.is_editable():
                try:
                    await tag_el.fill(tag, timeout=3000)
                    await page.wait_for_timeout(300)
                    await tag_el.press("Enter")
                    await page.wait_for_timeout(600)
                    err = page.locator('.wt-alert--error-01, [role="alert"]').first
                    if await err.count() > 0 and await err.is_visible():
                        await dismiss_alerts(page)
                        skipped += 1
                    else:
                        filled += 1
                except Exception as fill_ex:
                    print(f"      ⚠️ Không điền được tag '{tag}': {fill_ex}")
                    skipped += 1
        msg = f"  🏷  {filled} tags ✓"
        if skipped: msg += f" ({skipped} bị reject/bỏ qua)"
        print(msg)
    except Exception as e:
        print(f"  ⚠ tags: {e}")
    await page.wait_for_timeout(500)

    # Shop Section
    section_name = product.get("section", "").strip()
    if section_name:
        try:
            await dismiss_alerts(page)
            # Tìm dropdown Shop Section
            section_found = False
            for sel in [
                'select[name*="section" i]',
                'select[id*="section" i]',
                '[data-testid*="section"] select',
            ]:
                sel_el = page.locator(sel).first
                if await sel_el.count() > 0 and await sel_el.is_visible():
                    await sel_el.select_option(label=section_name)
                    await page.wait_for_timeout(800)
                    print(f"  📁 Section: {section_name} ✓")
                    section_found = True
                    break

            if not section_found:
                # Thử click vào combobox rồi chọn option
                for btn_sel in [
                    'button[id*="section" i]',
                    '[data-testid*="section"] button',
                    'div[class*="section"] button',
                ]:
                    btn = page.locator(btn_sel).first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click()
                        await page.wait_for_timeout(800)
                        opt = page.locator(f'[role="option"]:has-text("{section_name}"), li:has-text("{section_name}")').first
                        if await opt.count() > 0:
                            await opt.click()
                            await page.wait_for_timeout(800)
                            print(f"  📁 Section: {section_name} ✓")
                            section_found = True
                            break

            if not section_found:
                print(f"  ⚠ Section: không tìm thấy dropdown — bỏ qua")
        except Exception as e:
            print(f"  ⚠ section: {e}")

# ── Tab: Item Options ──────────────────────────────────────────────────────────
async def fill_item_options_tab(page, product):
    await click_tab(page, "Item Options", "Options")
    await page.wait_for_timeout(1000)

    # Listing type = Digital
    try:
        digital = page.locator('input[name="listing_type_options_group"]').nth(1)
        if await digital.count() > 0:
            handle = await digital.element_handle()
            if handle:
                await page.evaluate("el => el.click()", handle)
                await page.wait_for_timeout(800)
                print("  💻 Listing type: Digital ✓")
    except Exception as e:
        print(f"  ⚠ listing type: {e}")

    # Who made = I did
    try:
        who = page.locator('input[name="whoMade"]').first
        if await who.count() > 0:
            handle = await who.element_handle()
            if handle:
                await page.evaluate("el => el.click()", handle)
                await page.wait_for_timeout(600)
                print("  👤 Who made: I did ✓")
    except Exception as e:
        print(f"  ⚠ whoMade: {e}")

    # What is it = Finished product
    try:
        supply = page.locator('input[name="isSupply"]').first
        if await supply.count() > 0:
            handle = await supply.element_handle()
            if handle:
                await page.evaluate("el => el.click()", handle)
                await page.wait_for_timeout(600)
                print("  🏷 Finished product ✓")
    except Exception as e:
        print(f"  ⚠ isSupply: {e}")

    # When made
    try:
        when = page.locator('#when-made-select').first
        if await when.count() > 0 and await when.is_visible():
            await when.select_option(value=product["when_made"])
            await page.wait_for_timeout(600)
            print(f"  📅 When made ✓")
    except Exception as e:
        print(f"  ⚠ whenMade: {e}")

    # How is this digital content created? → "Created by me"
    await page.wait_for_timeout(800)
    try:
        # Tìm radio button "Created by me"
        created_by_me = False
        for sel in [
            'input[type="radio"][value*="created_by_me" i]',
            'input[type="radio"][value*="human" i]',
            'input[type="radio"][value*="manually" i]',
        ]:
            rb = page.locator(sel).first
            if await rb.count() > 0:
                handle = await rb.element_handle()
                if handle:
                    await page.evaluate("el => el.click()", handle)
                    await page.wait_for_timeout(600)
                    print("  ✍️  Digital content: Created by me ✓")
                created_by_me = True
                break

        if not created_by_me:
            # Tìm theo label text
            for label_text in ["Created by me", "I created this"]:
                label = page.locator(f'label:has-text("{label_text}")').first
                if await label.count() > 0 and await label.is_visible():
                    await label.click()
                    await page.wait_for_timeout(600)
                    print(f"  ✍️  Digital content: {label_text} ✓")
                    created_by_me = True
                    break

        if not created_by_me:
            # Thử radio đầu tiên trong nhóm liên quan
            radios = page.locator('input[type="radio"]')
            cnt = await radios.count()
            for i in range(cnt):
                r = radios.nth(i)
                # Tìm radio gần text "Created by me"
                parent = r.locator('xpath=../..')
                txt = await parent.inner_text()
                if "created" in txt.lower() or "Created" in txt:
                    handle = await r.element_handle()
                    if handle:
                        await page.evaluate("el => el.click()", handle)
                        await page.wait_for_timeout(600)
                        print("  ✍️  Digital content: Created by me ✓")
                    created_by_me = True
                    break
    except Exception as e:
        print(f"  ⚠ digital content created: {e}")

    # Upload PDF digital file
    if product["pdf_paths"]:
        await upload_digital_files(page, product)

# ── Tab: Pricing & Shipping ────────────────────────────────────────────────────
async def fill_pricing_tab(page, product):
    await dismiss_alerts(page)
    await click_tab(page, "Pricing & Shipping", "Pricing")
    await page.wait_for_timeout(1200)
    await dismiss_alerts(page)

    if await smart_fill(page, '#listing-price-input, [data-testid="price-input"], input[name="price"]',
                        f"{product['price']:.2f}"):
        print(f"  💲 Price: ${product['price']:.2f} ✓")
    else:
        print(f"  ⚠ Price: không điền được — kiểm tra thủ công")
    await page.wait_for_timeout(500)

    if await smart_fill(page, '#listing-quantity-input, input[name="quantity"]', str(product["qty"])):
        print(f"  🔢 Qty: {product['qty']} ✓")

    if "sku" in product and product["sku"]:
        if await smart_fill(page, '#listing-sku-input, input[name="sku"], [data-testid="sku-input"]', str(product["sku"])):
            print(f"  🔑 SKU: {product['sku']} ✓")

# ── Upload Digital Files ───────────────────────────────────────────────────────
async def upload_digital_files(page, product):
    if not product["pdf_paths"]:
        return
    try:
        # Kiểm tra kích thước file
        for path in product["pdf_paths"]:
            size_mb = Path(path).stat().st_size / (1024 * 1024)
            if size_mb > 20:
                print(f"  ❌ CẢNH BÁO: File '{Path(path).name}' ({size_mb:.2f} MB) vượt quá giới hạn 20MB của Etsy!")
                print("     • Etsy chỉ cho phép upload file dưới 20MB.")
                print("     • Hướng xử lý: Hãy nén file PDF lại, hoặc đổi sang upload 1 file PDF/TXT hướng dẫn có chứa link tải từ Google Drive.")
        # Step 1: Navigate to Item Details tab (where Digital files section lives)
        for tab_text in ["Item Details", "Details"]:
            tab = page.locator(
                f'a:has-text("{tab_text}"), '
                f'button:has-text("{tab_text}"), '
                f'[role="tab"]:has-text("{tab_text}")'
            ).first
            if await tab.count() > 0 and await tab.is_visible():
                await tab.click()
                await page.wait_for_timeout(2000)
                break

        await dismiss_alerts(page)
        await page.wait_for_timeout(500)

        # Step 2: Scroll the "Add file" button into view
        add_btn = page.locator(
            'button:has-text("Add file"), '
            '[data-testid*="add-file"], '
            'label:has-text("Add file")'
        ).first
        if await add_btn.count() > 0:
            await add_btn.scroll_into_view_if_needed()
            await page.wait_for_timeout(500)

        # Step 3: Use expect_file_chooser to intercept the file dialog
        # This is the CORRECT way - Etsy's button triggers a file chooser dialog
        btn_disabled = False
        if await add_btn.count() > 0:
            btn_disabled = await add_btn.is_disabled()

        if not btn_disabled:
            try:
                async with page.expect_file_chooser(timeout=10000) as fc_info:
                    if await add_btn.count() > 0:
                        await add_btn.click()
                    else:
                        await page.click('text="Add file"', timeout=5000)
                file_chooser = await fc_info.value
                await file_chooser.set_files(product["pdf_paths"])

                # Wait for upload to complete
                file_name = Path(product["pdf_paths"][0]).name
                file_stem = Path(product["pdf_paths"][0]).stem
                print(f"  ⏳ Đợi upload file hoàn tất: {file_name} (có thể mất 30-60s)...")
                uploaded = False
                for _ in range(60):
                    await page.wait_for_timeout(1000)
                    page_text = await page.inner_text("body")
                    if file_name in page_text or file_stem in page_text:
                        uploaded = True
                        break
                    check = page.locator(
                        '[class*="file-name"], [class*="filename"], '
                        '[data-testid*="file"] .name, '
                        'p:has-text(".pdf")'
                    ).first
                    if await check.count() > 0:
                        uploaded = True
                        break

                if uploaded:
                    print(f"  📎 {len(product['pdf_paths'])} file(s) ✓ (PDF/ZIP)")
                else:
                    print(f"  📎 PDF uploaded (chờ đủ 60s) — kiểm tra Etsy xem có chưa")
                return
            except Exception as fc_err:
                print(f"  ⚠ File chooser failed: {fc_err} — trying direct input...")
        else:
            print(f"  ℹ️  'Add file' button disabled — using direct input fallback")



        # Step 4: Fallback — try set_input_files on any hidden input
        # (works on some Etsy form versions)
        all_inputs = page.locator('input[type="file"]')
        count = await all_inputs.count()
        for idx in range(count):
            inp = all_inputs.nth(idx)
            try:
                await inp.set_input_files(product["pdf_paths"], timeout=15000)
                await page.wait_for_timeout(6000)
                print(f"  📎 {len(product['pdf_paths'])} file(s) ✓ (PDF/ZIP) (input #{idx})")
                return
            except Exception:
                continue

        print("  ⚠ Không upload được PDF — anh upload thủ công sau khi bot xong nhé!")
    except Exception as e:
        print(f"  ⚠ PDF upload error: {e}")




# ── Translations ───────────────────────────────────────────────────────────────
async def fill_translations(page, product):
    print("  🌐 Đang dịch và điền Translations...")
    try:
        h = page.locator('text="Translations"').first
        if await h.count() > 0:
            await h.scroll_into_view_if_needed()
            await page.wait_for_timeout(800)
    except Exception:
        pass

    en_tags = clean_tags(product["tags"])

    for lang_code, lang_name, idx in LANGUAGES:
        try:
            # Click tab ngôn ngữ
            clicked = False
            for sel in [f'button:has-text("{lang_name}")',
                        f'[role="tab"]:has-text("{lang_name}")',
                        f'li:has-text("{lang_name}")']:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.scroll_into_view_if_needed()
                    await el.click()
                    await page.wait_for_timeout(1000)
                    clicked = True
                    break
            if not clicked:
                print(f"    ⚠ tab {lang_name} không thấy")
                continue

            # Dịch title và description
            trans_title = trim_title(translate_text(product["title"], lang_code))
            trans_desc  = translate_text(product["description"], lang_code)

            # Điền title
            for sel in [f'textarea[name="translations.{idx}.title"]',
                        f'#field-translations-{idx}-title-input',
                        f'textarea[id*="translations"][id*="{idx}"][id*="title"]',
                        f'textarea[id*="{lang_code}"][id*="title"]']:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await clear_and_fill(el, trans_title)
                    await page.wait_for_timeout(400)
                    break

            # Điền description
            for sel in [f'textarea[name="translations.{idx}.description"]',
                        f'#listing-{lang_code}-translation-description-textarea',
                        f'textarea[id*="{lang_code}"][id*="description"]',
                        f'textarea[id*="translations"][id*="{idx}"][id*="description"]']:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await clear_and_fill(el, trans_desc)
                    await page.wait_for_timeout(400)
                    break

            # Điền tags dịch
            try:
                # Delete existing translation tags for this language
                try:
                    await page.evaluate('''() => {
                        let activePanel = document.querySelector('[role="tabpanel"]:not([class*="hidden"]), [role="tabpanel"]:not([style*="display: none"])');
                        if (activePanel) {
                            let removeButtons = Array.from(activePanel.querySelectorAll('span.wt-tag button, [data-testid="tag-pill"] button, button[aria-label*="remove" i], button[aria-label*="delete" i], button[class*="remove" i]'));
                            for (let btn of removeButtons) {
                                btn.click();
                            }
                        }
                    }''')
                    await page.wait_for_timeout(500)
                except Exception:
                    pass

                tag_input_sel = (
                    f'input[name="translations.{idx}.tags"], '
                    f'input[id*="{lang_code}"][id*="tag"], '
                    f'input[id*="translations"][id*="{idx}"][id*="tag"], '
                    f'input[placeholder*="tag" i]'
                )
                tag_input = page.locator(tag_input_sel).first
                if await tag_input.count() > 0 and await tag_input.is_visible() and await tag_input.is_editable():
                    seen_trans_tags = set()
                    try:
                        existing_pills = await page.evaluate('''() => {
                            let activePanel = document.querySelector('[role="tabpanel"]:not([class*="hidden"]), [role="tabpanel"]:not([style*="display: none"])');
                            if (!activePanel) return [];
                            let pills = Array.from(activePanel.querySelectorAll('span.wt-tag, [data-testid="tag-pill"]'));
                            return pills.map(p => (p.innerText || '').trim().toLowerCase()).filter(Boolean);
                        }''')
                        for ep in (existing_pills or []):
                            seen_trans_tags.add(ep)
                    except Exception:
                        pass

                    tags_filled = 0
                    for tag in en_tags:
                        trans_tag = translate_tag(tag, lang_code)
                        if not trans_tag:
                            continue
                        norm_tag = trans_tag.strip().lower()
                        if norm_tag in seen_trans_tags:
                            continue
                        seen_trans_tags.add(norm_tag)

                        try:
                            await tag_input.fill(trans_tag, timeout=3000)
                            await page.wait_for_timeout(300)
                            await tag_input.press("Enter")
                            await page.wait_for_timeout(500)
                            tags_filled += 1
                        except Exception as fill_ex:
                            print(f"      ⚠️ Không điền được trans tag '{trans_tag}': {fill_ex}")
                    if tags_filled:
                        print(f"    🌍 {lang_name} ✓ ({tags_filled} tags)")
                    else:
                        print(f"    🌍 {lang_name} ✓")
                else:
                    print(f"    🌍 {lang_name} ✓")
            except Exception as te:
                print(f"    🌍 {lang_name} ✓ (tags: {te})")

        except Exception as e:
            print(f"    ⚠ {lang_name}: {e}")

        await page.wait_for_timeout(500)

# ── Fill One Listing ───────────────────────────────────────────────────────────
async def check_duplicate_draft(page, product: dict) -> bool:
    """Quét Drafts và kiểm tra trùng lặp theo SKU/tiêu đề cho đúng sản phẩm."""
    try:
        print("  🔍 Đang kiểm tra trùng lặp trên Etsy Drafts...")
        drafts = await _collect_draft_cards(page)
        product["_draft_ids_before_create"] = sorted({
            str(card.get("id", "")).strip()
            for card in drafts
            if isinstance(card, dict) and str(card.get("id", "")).strip()
        })
        title = str(product.get("title", ""))
        if not title:
            return False

        matched_id = _pick_draft_card_id(drafts, product)
        if matched_id:
            print(f"  ⚠️ Trùng lặp phát hiện: listing {matched_id}")
            return True

        print("  ✓ Không phát hiện trùng lặp trên Etsy.")
        return False
    except Exception as e:
        print(f"  ⚠ Lỗi kiểm tra trùng lặp: {e}")
        return DRAFT_DUPLICATE_CHECK_FAILED_SENTINEL


async def get_newly_created_listing_url(page, product: dict):
    """
    Lấy URL listing vừa tạo từ editor hoặc danh sách Drafts theo cơ chế fail-safe.
    Trả về string URL (e.g. 'https://www.etsy.com/listing/4509048784'),
    hoặc UNVERIFIED_DRAFT_URL_SENTINEL nếu chưa xác minh được.
    """
    import re
    target_title = str(product.get("title", ""))

    # Phương pháp 1: Dùng trực tiếp URL hiện tại nếu đang ở trang listing editor/listing
    # và editor đang khớp đúng sản phẩm hiện tại.
    if target_title:
        current_url = page.url
        match = re.search(r'(?:edit|listing)/(\d+)', current_url)
        if match and await _editor_product_signature_matches(page, product):
            lid = match.group(1)
            print(f"  🎯 Lấy được Listing ID từ URL editor (đã xác thực sản phẩm): {lid}")
            return f"https://www.etsy.com/listing/{lid}"

    # Phương pháp 2: Quét Drafts để chọn đúng listing vừa tạo.
    try:
        links = await _collect_draft_cards(page)
        matched_id = _pick_draft_card_id(links, product)
        if matched_id:
            print(f"  🎯 Quét Drafts chọn được Listing ID duy nhất: {matched_id}")
            return f"https://www.etsy.com/listing/{matched_id}"

        baseline_ids = {
            str(listing_id).strip()
            for listing_id in product.get("_draft_ids_before_create", [])
            if str(listing_id).strip()
        }
        if "_draft_ids_before_create" in product:
            current_ids = {
                str(card.get("id", "")).strip()
                for card in links
                if isinstance(card, dict) and str(card.get("id", "")).strip()
            }
            new_ids = sorted(current_ids - baseline_ids)
            if len(new_ids) == 1:
                new_id = new_ids[0]
                print(f"  🎯 Xác minh được Listing ID mới duy nhất so với baseline Drafts: {new_id}")
                return f"https://www.etsy.com/listing/{new_id}"
            if len(new_ids) > 1:
                print(f"  ⚠ Có {len(new_ids)} Draft ID mới; không tự động chọn để tránh sai listing.")
    except Exception as e:
        print(f"  ⚠ Lỗi khi quét tìm Listing ID: {e}")
        
    print("  ⚠ Không xác minh được listing URL sau lưu draft.")
    return UNVERIFIED_DRAFT_URL_SENTINEL


async def fill_listing(page, product, edit_url=None):
    print(f"\n{'─'*55}")
    print(f"  📦 {product['folder']} | {len(product['image_paths'])} ảnh | {len(product['pdf_paths'])} PDF")
    print(f"     {product['title'][:60]}...")

    # Early local validations to save time and provide clear feedback
    early_errors = []
    if product.get("price", 0) < 0.20:
        early_errors.append(f"Giá bán ${product.get('price', 0):.2f} không hợp lệ (tối thiểu $0.20)")
    
    for path in product.get("pdf_paths", []):
        size_mb = Path(path).stat().st_size / (1024 * 1024)
        if size_mb > 20:
            early_errors.append(f"File PDF '{Path(path).name}' ({size_mb:.2f} MB) vượt quá giới hạn 20MB")
            
    if early_errors:
        err_msg = " | ".join(early_errors)
        print(f"  ❌ Lỗi validate sớm: {err_msg}")
        return False, err_msg

    if edit_url:
        print(f"  📝 Đang sửa listing tại {edit_url}...")
        await page.goto(edit_url, wait_until="domcontentloaded")
    else:
        # ── Kiểm tra trùng draft trên Etsy trước khi tạo mới ──────────────────
        is_dup = await check_duplicate_draft(page, product)
        if is_dup == DRAFT_DUPLICATE_CHECK_FAILED_SENTINEL:
            return False, "Không thể xác minh trùng lặp draft trước khi tạo mới, dừng để tránh lỗi trùng"
        if is_dup:
            return "DUPLICATE"   # Caller sẽ mark Excel là đã đăng

        await page.goto("https://www.etsy.com/your/shops/me/listing-editor/create",
                        wait_until="domcontentloaded")
    await page.wait_for_timeout(5000)

    if "listing-editor" not in page.url:
        print(f"  ❌ Không vào được editor. URL hiện tại: {page.url}")
        return False, "Không vào được Etsy listing editor"

    await page.wait_for_timeout(2500)


    form_type = await detect_form_type(page)
    print(f"  📋 Form: {form_type}")

    if form_type == "tabs":
        await fill_photo_tab(page, product, explicit_edit=bool(edit_url))
        await page.wait_for_timeout(1000)

        await fill_category_tab(page, product)
        await page.wait_for_timeout(1000)

        await fill_item_details_tab(page, product)
        await page.wait_for_timeout(1000)

        await fill_item_options_tab(page, product)
        await page.wait_for_timeout(1000)

        await fill_pricing_tab(page, product)
        await page.wait_for_timeout(1000)

    else:
        # ── Form trang dài cũ (fallback) ──────────────────────────────────
        try:
            d = page.locator('input[name="listing_type_options_group"]').nth(1)
            if await d.count() > 0 and await d.is_visible():
                handle = await d.element_handle()
                if handle:
                    await page.evaluate("el => el.click()", handle)
                    print("  💻 Digital ✓")
        except Exception: pass

        ok = await smart_fill(page, '#listing-title-input, textarea[name="title"]', product["title"])
        print(f"  📝 Title {'✓' if ok else '⚠ không điền được'}")
        await page.wait_for_timeout(500)
        ok = await smart_fill(page, '#listing-description-textarea, textarea[name="description"]',
                              product["description"], timeout=6000)
        print(f"  📄 Description {'✓' if ok else '⚠ không điền được'}")
        await page.wait_for_timeout(500)

        try:
            tags = clean_tags(product["tags"])
            filled = 0
            for tag in tags:
                el = page.locator('#listing-tags-input').first
                if await el.is_visible():
                    await el.fill(tag); await el.press("Enter")
                    await page.wait_for_timeout(400); filled += 1
            print(f"  🏷  {filled} tags ✓")
        except Exception as e: print(f"  ⚠ tags: {e}")

        ok = await smart_fill(page, '#listing-price-input, [data-testid="price-input"]', f"{product['price']:.2f}")
        print(f"  💲 Price {'✓' if ok else '⚠ không điền được'}")
        ok = await smart_fill(page, '#listing-quantity-input, input[name="quantity"]', str(product["qty"]))
        print(f"  🔢 Qty {'✓' if ok else '⚠ không điền được'}")

        if "sku" in product and product["sku"]:
            ok = await smart_fill(page, '#listing-sku-input, input[name="sku"], [data-testid="sku-input"]', str(product["sku"]))
            print(f"  🔑 SKU: {product['sku']} {'✓' if ok else '⚠ không điền được'}")

        if product["image_paths"]:
            before_count = await _count_listing_image_thumbs(page)
            expected = before_count + len(product["image_paths"][:10])
            await _upload_listing_photos_until_count(
                page,
                product["image_paths"],
                expected_total=expected,
                exact=False,
            )

        # Digital file upload is handled inside fill_item_details_tab


    # Translations (chạy cuối, sau khi đã điền hết)
    if FILL_TRANSLATIONS:
        await page.wait_for_timeout(1000)
        await fill_translations(page, product)

    # Save listing
    await page.wait_for_timeout(1500)
    try:
        btn = page.locator(_get_save_button_selector(explicit_edit=bool(edit_url))).first
        await btn.wait_for(state="visible", timeout=10000)
        await btn.click()
        
        print("  ⏳ Đang đợi Etsy xử lý lưu bản nháp...")
        
        # Check for validation errors and wait for redirect
        errors_found = []
        saved_successfully = False
        
        for _wait in range(30):
            await page.wait_for_timeout(1000)
            
            # Evaluate visible validation errors using JS
            try:
                found_msgs = await page.evaluate('''() => {
                    const list = [];
                    const selectors = [
                        '.wt-validation__message:visible',
                        '.wt-alert--error:visible',
                        '[class*="error-message"]:visible',
                        '[class*="validation-message"]:visible',
                        '[role="alert"]:visible',
                        'span.wt-text-danger',
                        'p.wt-text-danger',
                        '[class*="wt-validation__message"]'
                    ];
                    for (const sel of selectors) {
                        try {
                            const els = document.querySelectorAll(sel);
                            for (const el of els) {
                                if (el.getBoundingClientRect().height > 0) {
                                    const txt = el.innerText.trim();
                                    if (txt && !list.includes(txt) && !txt.toLowerCase().includes("loading") && txt.length < 250) {
                                        list.push(txt);
                                    }
                                }
                            }
                        } catch(e) {}
                    }
                    return list;
                }''')
                for msg in found_msgs:
                    if msg not in errors_found:
                        errors_found.append(msg)
            except Exception:
                pass
            
            if errors_found:
                break
                
            # Check if URL changed (saved successfully)
            if "edit/" in page.url or "listings" in page.url:
                saved_successfully = True
                break

        if not saved_successfully and not errors_found:
            # Check if there is some generic error text on screen
            try:
                body_text = await page.inner_text("body")
                for err_word in ["error", "required", "invalid", "lỗi", "bắt buộc", "cannot"]:
                    if err_word in body_text.lower():
                        errors_found.append(f"Phát hiện từ khóa lỗi '{err_word}'")
            except Exception:
                pass

        if errors_found or not saved_successfully:
            print(f"  ❌ Save draft failed! saved_successfully={saved_successfully}")
            for err in errors_found:
                print(f"     • {err}")
            # Take screenshot to help debug
            try:
                screenshot_path = str(BASE_DIR / "save_draft_failure.png")
                await page.screenshot(path=screenshot_path, full_page=True)
                print(f"  📸 Đã chụp ảnh màn hình lỗi tại: {screenshot_path}")
            except Exception as ss_ex:
                print(f"  ⚠ Không thể chụp ảnh màn hình: {ss_ex}")
            err_reason = " | ".join(errors_found) if errors_found else "Lưu bản nháp thất bại"
            return False, err_reason

        print("  💾 Saved as draft ✅")
        # Trích xuất và trả về URL của listing vừa tạo để lưu vào Excel
        new_url = await get_newly_created_listing_url(page, product)
        if new_url:
            return new_url
        return True
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"  ❌ Save draft: {e}")
        try:
            screenshot_path = str(BASE_DIR / "save_draft_exception.png")
            await page.screenshot(path=screenshot_path, full_page=True)
            print(f"  📸 Đã chụp ảnh màn hình exception tại: {screenshot_path}")
        except Exception:
            pass
        return False, f"Exception: {str(e)[:100]}"

def _is_etsy_signin_page(url: str, content: str) -> bool:
    url_lower = (url or "").lower()
    content_lower = (content or "").lower()
    return ("signin" in url_lower or "join" in url_lower or "access is temporarily" in content_lower)


def _expected_etsy_shop_slug(shop_id: str) -> str:
    shop_config = SHOPS.get(shop_id, {})
    shop_link = str(shop_config.get("etsy_link") or "").strip()
    match = re.search(r"(?:etsy\.com)?/shop/([^/?#]+)", shop_link, flags=re.IGNORECASE)
    return (match.group(1) if match else shop_id).strip().lower()


async def _assert_shop_manager_identity(page, shop_id: str) -> None:
    expected_slug = _expected_etsy_shop_slug(shop_id)
    current_url = str(page.url or "").lower()
    if f"/shop/{expected_slug}" in current_url or f"/shops/{expected_slug}/" in current_url:
        return

    public_shop_hrefs = await page.locator('a[href*="/shop/"]').evaluate_all(
        "(links) => links.map((link) => link.href || link.getAttribute('href') || '')"
    )
    observed_slugs = set()
    for href in public_shop_hrefs or []:
        match = re.search(r"(?:etsy\.com)?/shop/([^/?#]+)", str(href), flags=re.IGNORECASE)
        if match:
            observed_slugs.add(match.group(1).strip().lower())
    if expected_slug in observed_slugs:
        return

    observed = ", ".join(sorted(observed_slugs)) or "không xác định"
    raise RuntimeError(
        f"❌ Phiên Chrome không xác minh được đúng shop '{shop_id}' "
        f"(cần {expected_slug}, thấy {observed}). Dừng để tránh đăng nhầm shop."
    )


# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    global EXCEL_FILE, SHOP_DIR
    parser = argparse.ArgumentParser(description="Etsy Auto Poster")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                        help=f"Số sản phẩm mỗi lần chạy (mặc định {DEFAULT_BATCH})")
    parser.add_argument("--skip", type=int, default=0,
                        help="Bỏ qua N sản phẩm đầu (để chạy tiếp từ giữa chừng)")
    parser.add_argument("--product", type=str, default=None,
                        help="Chạy 1 sản phẩm cụ thể theo folder name (vd: product-41)")
    parser.add_argument(
        "--products",
        action="append",
        default=None,
        help="Chạy nhiều sản phẩm theo folder name (vd: --products product-1 --products product-3 hoặc --products product-1,product-3)",
    )
    parser.add_argument(
        "--selected-product",
        action="append",
        default=None,
        help="Chạy đúng cặp row:folder đã chọn (vd: --selected-product 42:product-39)",
    )
    parser.add_argument("--shop", type=str, default="templystudios",
                        help="Mã shop cần chạy")
    parser.add_argument("--edit-url", type=str, default=None,
                        help="Edit an existing listing URL instead of creating a new one")
    args = parser.parse_args()

    SHOP_DIR = BASE_DIR / "shops" / args.shop
    EXCEL_FILE = SHOP_DIR / "Etsy_SEO_Generator.xlsx"

    try:
        requested_products = _normalize_requested_products(args.products)
        selected_products = _normalize_selected_products(args.selected_product)
    except ValueError as exc:
        raise RuntimeError(f"❌ Danh sách sản phẩm không hợp lệ: {exc}")
    if selected_products and (requested_products or args.product):
        raise RuntimeError("❌ Không dùng --selected-product cùng --product/--products")

    if args.product:
        normalized_product = args.product.strip() if args.product is not None else ""
        if not re.fullmatch(r"product-\d+", normalized_product):
            raise RuntimeError(f"❌ --product không hợp lệ: {args.product}")
        if normalized_product and normalized_product not in requested_products:
            requested_products.insert(0, normalized_product)

    products, wb, ws, total = read_products(
        batch=args.batch,
        skip=args.skip,
        product_folder=args.product,
        product_folders=requested_products,
        selected_products=selected_products,
        shop_id=args.shop,
    )

    if selected_products:
        found_pairs = {(int(p["row"]), str(p["folder"]).strip()) for p in products}
        missing_pairs = [f"{row}:{folder}" for row, folder in selected_products if (row, folder) not in found_pairs]
        if missing_pairs:
            raise RuntimeError(f"❌ Không tìm thấy đúng row:folder: {', '.join(missing_pairs)}")
    elif requested_products:
        requested_lookup = {str(p).strip() for p in requested_products if str(p).strip()}
        found_folders = {str(p["folder"]).strip() for p in products}
        missing = [folder for folder in requested_products if folder not in found_folders]
        if missing:
            raise RuntimeError(f"❌ Không tìm thấy folder: {', '.join(missing)}")

    if not products:
        print("\n⚠  Không có sản phẩm nào cần đăng.\n")
        return

    print(f"\n{'='*55}")
    print(f"  🛍  Etsy Auto Poster")
    print(f"  📦 {total} tổng | Batch: {args.batch} | Skip: {args.skip}")
    print(f"{'='*55}")
    for p in products:
        print(f"   • {p['folder']} | {len(p['image_paths'])} ảnh | {len(p['pdf_paths'])} PDF | {p['title'][:45]}...")

    if FILL_TRANSLATIONS:
        langs = ", ".join(n for _, n, _ in LANGUAGES)
        print(f"\n  🌐 Dịch sang: {langs}")

    BROWSER_DIR = resolve_browser_session_dir(
        args.shop,
        config=SHOPS,
        base_dir=BASE_DIR,
        home_dir=Path.home(),
    )

    print()
    BROWSER_DIR.mkdir(exist_ok=True, parents=True)
    in_use, lock_path = _is_profile_in_use(BROWSER_DIR)
    if in_use and lock_path is not None:
        raise RuntimeError(
            f"❌ Chrome profile đang bị khóa ({lock_path.name}), có thể đang được dùng bởi phiên bản Chrome khác. "
            f"Đóng profile trước khi POST hoặc dùng cửa sổ/nguồn profile khác."
        )

    async with async_playwright() as pw:
        launch_kw = dict(
            user_data_dir=str(BROWSER_DIR),
            headless=False,
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            viewport=None,
        )
        if CHROME_PATH.exists():
            launch_kw["executable_path"] = str(CHROME_PATH)
            print("  🌐 Dùng Google Chrome thật")
        else:
            print("  🌐 Dùng Chromium")

        ctx  = await pw.chromium.launch_persistent_context(**launch_kw)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Kiểm tra đăng nhập
        await page.goto("https://www.etsy.com/your/shops/me/tools/listings",
                        wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        if _is_etsy_signin_page(page.url, await page.content()):
            raise RuntimeError(
                "❌ Cần đăng nhập Etsy trước khi post. "
                "Profile hiện tại đang ở trang đăng nhập, không thể tiếp tục đăng listing."
            )
        await _assert_shop_manager_identity(page, args.shop)

        print("  ✅ Đã vào Shop Manager!\n")

        success = failed = skipped = 0
        for i, product in enumerate(products, 1):
            print(f"\n[{i}/{len(products)}]", end="")
            try:
                ok = await fill_listing(page, product, edit_url=args.edit_url)
                if ok == "DUPLICATE":
                    skipped += 1
                    save_status(wb, ws, product["row"], "✅ Đã đăng draft")
                    print(f"  ↩️  Bỏ qua (đã có draft trên Etsy) — cập nhật Excel")
                elif ok == UNVERIFIED_DRAFT_URL_SENTINEL:
                    success += 1
                    save_status(wb, ws, product["row"], "✅ Đã đăng draft (URL chưa xác minh)")
                elif isinstance(ok, str) and ok.startswith("http"):
                    success += 1
                    save_status(wb, ws, product["row"], "✅ Đã đăng draft", url=ok)
                elif ok is True:
                    success += 1
                    save_status(wb, ws, product["row"], "✅ Đã đăng draft")
                elif isinstance(ok, tuple) and ok[0] is False:
                    failed += 1
                    err_reason = ok[1]
                    save_status(wb, ws, product["row"], f"❌ Lỗi: {err_reason}")
                else:
                    failed += 1
                    save_status(wb, ws, product["row"], "❌ Lỗi")
            except Exception as e:
                print(f"  ❌ {e}")
                failed += 1
                save_status(wb, ws, product["row"], f"❌ Lỗi: {str(e)[:100]}")

            # Delay giữa các sản phẩm (tránh bị Etsy block)
            if i < len(products):
                wait_sec = 8
                print(f"\n  ⏳ Nghỉ {wait_sec}s trước sản phẩm tiếp theo...")
                await asyncio.sleep(wait_sec)

        print(f"\n{'='*55}")
        print(f"  ✅ Thành công : {success}/{len(products)}")
        print(f"  ↩️  Bỏ qua (trùng): {skipped}/{len(products)}")
        print(f"  ❌ Thất bại  : {failed}/{len(products)}")
        remaining = total - args.skip - len(products)
        if remaining > 0:
            next_skip = args.skip + len(products)
            print(f"\n  📌 Còn {remaining} sản phẩm. Lần sau chạy:")
            print(f"     python3 etsy_auto_post.py --batch {args.batch} --skip {next_skip}")
        print(f"{'='*55}\n")
        await ctx.close()

if __name__ == "__main__":
    asyncio.run(main())
