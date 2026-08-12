"""
etsy_push_update.py — Đồng bộ đầy đủ từ Dashboard (local) lên Etsy.

Cập nhật các trường được chọn:
  - title, description, tags  (Item Details tab)
  - price, qty               (Pricing & Shipping tab)
  - images                   (Photo & Video tab — so sánh trước, bỏ qua nếu local trống)
  - files                    (Item Details tab — bỏ qua nếu local trống)

KHÔNG đụng đến translations, category, shop section.
Dùng Playwright + Chrome session có sẵn (không cần đăng nhập lại).

Cách dùng:
    python3 etsy_push_update.py --listing-id 4511156098 --row 117 --shop templystudios \\
        --fields title,description,tags,price,qty,images,files
"""
import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "shops_config.json"
DEFAULT_BROWSER_DIR = BASE_DIR / ".browser-session"
CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
IMG_EXTS    = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
FILE_EXTS   = {".pdf", ".zip", ".001", ".002", ".003", ".004", ".005"}

def ensure_deps():
    try:
        from playwright.async_api import async_playwright  # noqa
    except ImportError:
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "playwright", "--quiet"], check=True)

ensure_deps()

import openpyxl
from playwright.async_api import async_playwright


# ── Logging ────────────────────────────────────────────────────────────────────
def log(msg: str):
    print(msg, flush=True)


def load_shop_config(shop_id: str) -> dict:
    if not CONFIG_FILE.exists():
        return {}
    shops = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return shops.get(shop_id, {})


def browser_dir_for_shop(shop_id: str) -> Path:
    cfg = load_shop_config(shop_id)
    raw = cfg.get("browser_session")
    if raw:
        return Path(os.path.expanduser(raw))
    if shop_id == "templystudios":
        return DEFAULT_BROWSER_DIR
    return Path.home() / f".etsy_browser_session_{shop_id}"


async def assert_expected_shop(page, shop_id: str):
    """Fail fast if a shop-specific run is using another shop's browser session."""
    cfg = load_shop_config(shop_id)
    expected_terms = [shop_id.lower()]
    if cfg.get("name"):
        expected_terms.append(str(cfg["name"]).lower())

    text = ""
    try:
        text = (await page.locator("body").inner_text(timeout=8000)).lower()
    except Exception:
        text = ""

    if any(term and term in text for term in expected_terms):
        log(f"  ✅ Đúng shop session: {cfg.get('name', shop_id)}")
        return

    if CONFIG_FILE.exists():
        shops = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        for other_id, other_cfg in shops.items():
            if other_id == shop_id:
                continue
            other_terms = [other_id.lower(), str(other_cfg.get("name", "")).lower()]
            if any(term and term in text for term in other_terms):
                raise RuntimeError(
                    f"Đang mở nhầm shop/session: thấy '{other_cfg.get('name', other_id)}', "
                    f"nhưng lệnh yêu cầu '{cfg.get('name', shop_id)}'."
                )

    log("  ⚠️ Không xác nhận được tên shop trên trang; tiếp tục bằng profile theo --shop.")


# ── Excel helpers ──────────────────────────────────────────────────────────────
def trim_title(title: str, max_len: int = 140) -> str:
    """Cắt title tối đa max_len ký tự, không cắt giữa từ và dọn dẹp ký tự đặc biệt, giới hạn viết hoa."""
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

    # ── Đảm bảo không quá 3 từ viết hoa toàn bộ (ALL CAPS) để tránh lỗi Etsy ──
    words = title.split()
    new_words = []
    capitalized_count = 0
    for w in words:
        clean_w = re.sub(r'^[^a-zA-Z0-9]+|[^a-zA-Z0-9]+$', '', w)
        if clean_w.isupper() and re.search(r'[A-Z]', clean_w):
            if capitalized_count < 3:
                new_words.append(w)
                capitalized_count += 1
            else:
                w_cap = w
                match = re.search(r'([a-zA-Z0-9]+)', w)
                if match:
                    span = match.span(1)
                    w_cap = w[:span[0]] + w[span[0]:span[1]].capitalize() + w[span[1]:]
                new_words.append(w_cap)
        else:
            new_words.append(w)
    title = " ".join(new_words)

    if len(title) <= max_len:
        return title
    cut = title[:max_len].rsplit(" ", 1)[0].rstrip(",|;- ")
    return cut


def read_product_from_excel(row: int, shop_id: str) -> dict:
    excel_file = BASE_DIR / "shops" / shop_id / "Etsy_SEO_Generator.xlsx"
    wb = openpyxl.load_workbook(excel_file, data_only=True)
    ws = wb["Listings"]
    c = lambda col: ws.cell(row=row, column=col).value

    folder = str(c(2) or "")
    shop_dir = BASE_DIR / "shops" / shop_id / folder

    # Collect local images sorted by name
    img_dir = shop_dir / "images"
    image_paths = sorted(
        [str(p) for p in img_dir.iterdir() if p.suffix.lower() in IMG_EXTS],
        key=lambda p: Path(p).name
    ) if img_dir.exists() else []

    # Collect local files
    files_dir = shop_dir / "files"
    file_paths = sorted(
        [str(p) for p in files_dir.iterdir() if p.suffix.lower() in FILE_EXTS],
        key=lambda p: Path(p).name
    ) if files_dir.exists() else []

    val_price = c(5)
    price = float(val_price) if isinstance(val_price, (int, float)) else (float(str(val_price)) if val_price else 0.0)

    val_qty = c(11)
    qty = int(val_qty) if isinstance(val_qty, (int, float)) else (int(str(val_qty)) if val_qty else 999)

    return {
        "row":         row,
        "folder":      folder,
        "title":       trim_title(str(c(8) or "")),
        "description": str(c(9) or ""),
        "tags":        str(c(10) or ""),
        "price":       price,
        "qty":         qty,
        "etsy_url":    str(c(16) or ""),
        "image_paths": image_paths,
        "file_paths":  file_paths,
    }


# ── URL helpers ────────────────────────────────────────────────────────────────
def build_edit_url(listing_id: str) -> str:
    lid = listing_id.strip()
    if lid.startswith("http") or "/" in lid:
        import re
        m = re.search(r"(?:/listing/|/listing-editor/edit/|/edit/|/listing-editor/)(\d+)", lid)
        if m:
            lid = m.group(1)
        else:
            m_fb = re.search(r"\b(\d{8,15})\b", lid)
            if m_fb:
                lid = m_fb.group(1)
    return f"https://www.etsy.com/your/shops/me/listing-editor/edit/{lid}"


def clean_tags(raw: str) -> list[str]:
    tags = [t.strip() for t in re.split(r"[,\n;]+", raw) if t.strip()]
    seen, result = set(), []
    for t in tags:
        t = t[:20]
        if t.lower() not in seen:
            seen.add(t.lower())
            result.append(t)
    return result[:13]


# ── Playwright helpers ─────────────────────────────────────────────────────────
async def dismiss_alerts(page):
    try:
        for sel in [
            '[role="alert"] button[aria-label*="close" i]',
            '[role="alert"] button[aria-label*="dismiss" i]',
            '.wt-alert button',
        ]:
            btns = page.locator(sel)
            for i in range(await btns.count()):
                try:
                    btn = btns.nth(i)
                    if await btn.is_visible():
                        await btn.click()
                        await page.wait_for_timeout(400)
                except Exception:
                    pass
        await page.wait_for_timeout(300)
    except Exception:
        pass


async def smart_fill(page, selector: str, value: str, timeout: int = 6000) -> bool:
    try:
        el = page.locator(selector).first
        await el.wait_for(state="visible", timeout=timeout)
        await el.scroll_into_view_if_needed()
        await el.click(click_count=3)
        await page.wait_for_timeout(150)
        await el.fill(value)
        await page.wait_for_timeout(300)
        return True
    except Exception:
        return False


async def click_tab(page, *tab_names: str) -> bool:
    for name in tab_names:
        for sel in [
            f'button:has-text("{name}")',
            f'[role="tab"]:has-text("{name}")',
            f'a:has-text("{name}")',
        ]:
            el = page.locator(sel).first
            if await el.count() > 0:
                try:
                    if await el.is_visible():
                        await el.scroll_into_view_if_needed()
                        await el.click()
                        await page.wait_for_timeout(1000)
                        return True
                except Exception:
                    pass
    return False


async def detect_form_type(page) -> str:
    try:
        el = page.locator('[role="tablist"]')
        if await el.count() > 0 and await el.is_visible():
            return "tabs"
    except Exception:
        pass
    return "classic"


# ── Push: Title / Description / Tags ──────────────────────────────────────────
async def push_text_fields(page, product: dict, fields: set):
    form_type = await detect_form_type(page)
    if form_type == "tabs":
        clicked = await click_tab(page, "Item Details", "Details")
        if clicked:
            log("[PUSH] 📑 Vào tab Item Details")
        await page.wait_for_timeout(1200)
    await dismiss_alerts(page)

    if "title" in fields:
        title = trim_title(product["title"])
        ok = await smart_fill(page, '#listing-title-input, textarea[name="title"]', title, 8000)
        log(f"[PUSH] 📝 Title: {'✅' if ok else '⚠️ thất bại'}")
        await page.wait_for_timeout(400)

    if "description" in fields:
        ok = await smart_fill(
            page,
            '#listing-description-textarea, textarea[name="description"]',
            product["description"], 8000
        )
        log(f"[PUSH] 📄 Description: {'✅' if ok else '⚠️ thất bại'}")
        await page.wait_for_timeout(400)

    if "tags" in fields and product["tags"].strip():
        tag_list = clean_tags(product["tags"])
        log(f"[PUSH] 🏷  Điền {len(tag_list)} tags...")
        try:
            await dismiss_alerts(page)
            # Xóa tags cũ
            try:
                for _ in range(15):
                    delete_btn = page.locator(
                        '#field-tags button[aria-label^="Delete tag" i], '
                        '#field-tags span.wt-tag button, '
                        '#field-tags [data-testid="tag-pill"] button'
                    ).first
                    if await delete_btn.count() == 0:
                        break
                    await delete_btn.click()
                    await page.wait_for_timeout(250)
                await page.wait_for_timeout(600)
            except Exception:
                pass

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
                        filled += 1
                    except Exception:
                        skipped += 1
            log(f"[PUSH] 🏷  Tags: {filled} ✅" + (f" ({skipped} bỏ qua)" if skipped else ""))
        except Exception as e:
            log(f"[PUSH] ⚠️ Tags: {e}")


# ── Push: Price / Quantity ─────────────────────────────────────────────────────
async def push_pricing(page, product: dict, fields: set):
    form_type = await detect_form_type(page)
    if form_type == "tabs":
        clicked = await click_tab(page, "Pricing & Shipping", "Pricing")
        if clicked:
            log("[PUSH] 💰 Vào tab Pricing & Shipping")
        await page.wait_for_timeout(1200)
    await dismiss_alerts(page)

    if "price" in fields:
        ok = await smart_fill(
            page,
            '#listing-price-input, [data-testid="price-input"], input[name="price"]',
            f"{product['price']:.2f}"
        )
        log(f"[PUSH] 💲 Price ${product['price']:.2f}: {'✅' if ok else '⚠️ thất bại'}")
        await page.wait_for_timeout(400)

    if "qty" in fields:
        ok = await smart_fill(
            page,
            '#listing-quantity-input, input[name="quantity"]',
            str(product["qty"])
        )
        log(f"[PUSH] 🔢 Quantity {product['qty']}: {'✅' if ok else '⚠️ thất bại'}")
        await page.wait_for_timeout(400)


# ── Push: Images ───────────────────────────────────────────────────────────────
async def push_images(page, product: dict):
    local_images = product["image_paths"]

    async def photo_count() -> int:
        counts = []
        for sel in [
            'button[data-testid="image-delete-button"]',
            'button.le-aspect-ratio--square',
            '[data-testid*="photo" i] button[aria-label*="Remove" i]',
        ]:
            try:
                counts.append(await page.locator(sel).count())
            except Exception:
                pass
        return max(counts or [0])

    def photo_delete_button():
        return page.locator(
            'button[data-testid="image-delete-button"], '
            '[data-testid*="photo" i] button[aria-label*="Remove" i]'
        ).first

    # Safety guard: nếu local trống → bỏ qua
    if not local_images:
        log("[PUSH] 🖼  Images: local folder trống — BỎ QUA để không xóa ảnh Etsy")
        return

    log(f"[PUSH] 🖼  Images: {len(local_images)} ảnh local — đang xử lý...")

    form_type = await detect_form_type(page)
    if form_type == "tabs":
        await click_tab(page, "Photo & Video", "Photos")
        await page.wait_for_timeout(1500)

    # Đọc số ảnh hiện tại trên Etsy
    existing_count = await photo_count()
    log(f"[PUSH] 🖼  Etsy đang có {existing_count} ảnh")

    # Giữ lại một ảnh cũ tạm thời. Etsy hiện có thể làm biến mất ô upload khi
    # gallery về 0 ảnh; thay theo hai đợt sẽ tránh trạng thái đó.
    if existing_count > 0:
        remove_now = max(0, existing_count - 1)
        log(f"[PUSH] 🧹 Đang xóa trước {remove_now}/{existing_count} ảnh cũ trên Etsy...")
        removed = 0
        for _ in range(remove_now):
            btn = photo_delete_button()
            if await btn.count() == 0:
                break
            try:
                before_delete_buttons = await page.locator('button[data-testid="image-delete-button"]').count()
                await btn.scroll_into_view_if_needed()
                await btn.click()
                # The new Etsy editor removes the thumbnail asynchronously and
                # briefly renders the tile button in its place. Do not fall
                # through to the tile itself; wait for the trash-button count
                # to actually decrease before attempting the next deletion.
                for _wait in range(20):
                    await page.wait_for_timeout(250)
                    current_delete_buttons = await page.locator('button[data-testid="image-delete-button"]').count()
                    if current_delete_buttons < before_delete_buttons:
                        break
                confirm = page.locator(
                    'div[role="dialog"]:visible button:has-text("Delete"), '
                    'div[role="dialog"]:visible button:has-text("Remove"), '
                    'div[role="dialog"]:visible button:has-text("Xóa"), '
                    'div[role="dialog"]:visible button:has-text("Gỡ bỏ"), '
                    'button:has-text("Delete photo"):visible, '
                    'button:has-text("Remove photo"):visible, '
                    'button:has-text("Xóa ảnh"):visible'
                ).first
                if await confirm.count() > 0 and await confirm.is_visible():
                    await confirm.click()
                    await page.wait_for_timeout(800)
                removed += 1
            except Exception:
                break
        log(f"[PUSH] 🧹 Đã xóa trước {removed} ảnh cũ ✅")

    # Upload ảnh mới từ local (tối đa 10), chừa ảnh cuối cho bước thay nốt ảnh cũ.
    upload_imgs = local_images[:10]
    first_batch = upload_imgs[:-1] if existing_count > 0 and len(upload_imgs) > 1 else upload_imgs
    last_batch = upload_imgs[-1:] if existing_count > 0 and len(upload_imgs) > 1 else []

    async def upload_photo_batch(paths):
        if not paths:
            return
        upload_selectors = [
            '[data-testid="empty-photo-thumbnail"] input[name="listing-media-upload"]',
            'input[name="listing-media-upload"]',
            'label[for="listing-photos"] ~ * input[type="file"]',
            'input[type="file"]',
        ]
        fi = None
        for sel in upload_selectors:
            candidate = page.locator(sel).first
            if await candidate.count() > 0:
                fi = candidate
                break
        if fi is not None:
            try:
                await fi.wait_for(state="attached", timeout=15000)
                await fi.set_input_files(paths, timeout=60000)
                return
            except Exception as direct_err:
                log(f"[PUSH] ⚠️ Input upload ảnh không nhận file: {direct_err}")

        # Etsy's newer Photo & Video editor creates the file input only after
        # the user clicks Add photo(s). Use the native chooser as the primary
        # fallback so this also works when no input exists in the DOM yet.
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
                log(f"[PUSH] ⚠️ File chooser ảnh thất bại với {sel}: {chooser_err}")

        # One last generic fallback for Etsy variants that expose a file input
        # only after the gallery button has been clicked.
        fi = page.locator('input[type="file"]').last
        if await fi.count() > 0:
            await fi.set_input_files(paths, timeout=60000)
            return

        try:
            await page.screenshot(path=str(BASE_DIR / "save_push_image_upload_failure.png"), full_page=True)
        except Exception:
            pass
        raise Exception("Không tìm thấy nút/input upload ảnh trên Etsy.")

    log(f"[PUSH] 📤 Đang upload đợt 1: {len(first_batch)} ảnh từ local...")
    await upload_photo_batch(first_batch)

    expected_first = (1 if existing_count > 0 else 0) + len(first_batch)
    for _ in range(60):
        await page.wait_for_timeout(1000)
        if await photo_count() >= expected_first:
            break

    if last_batch:
        # Xóa ảnh cũ cuối cùng (đang ở vị trí đầu), rồi thêm ảnh local cuối.
        btn = photo_delete_button()
        await btn.click()
        await page.wait_for_timeout(600)
        confirm = page.locator(
            'div[role="dialog"]:visible button:has-text("Delete"), '
            'div[role="dialog"]:visible button:has-text("Remove")'
        ).first
        if await confirm.count() > 0 and await confirm.is_visible():
            await confirm.click()
            await page.wait_for_timeout(800)
        log("[PUSH] 📤 Đang upload ảnh local cuối...")
        await upload_photo_batch(last_batch)

    # Chờ số ảnh trên Etsy hiển thị đủ (Cơ chế an toàn)
    log(f"[PUSH] ⏳ Đợi ảnh hiển thị trên Etsy...")
    uploaded = False
    for _ in range(60):
        await page.wait_for_timeout(1000)
        uploaded_count = await photo_count()
        if uploaded_count >= len(upload_imgs):
            uploaded = True
            break
    
    final_count = await photo_count()
    if final_count == 0:
        raise Exception("Đồng bộ ảnh thất bại! Không thấy có ảnh nào tải lên được Etsy.")
    elif final_count < len(upload_imgs):
        log(f"[PUSH] ⚠️ Chỉ tìm thấy {final_count}/{len(upload_imgs)} ảnh tải lên thành công. Vẫn tiếp tục...")
    else:
        log(f"[PUSH] 🖼  Đồng bộ ảnh hoàn tất: {final_count} ảnh đã lên Etsy ✅")


# ── Push: Digital Files ────────────────────────────────────────────────────────
async def push_files(page, product: dict):
    from pathlib import Path
    import re
    local_files = product["file_paths"]

    # Safety guard
    if not local_files:
        log("[PUSH] 📎 Files: local folder trống — BỎ QUA để không xóa files Etsy")
        return

    if len(local_files) > 5:
        log(f"[PUSH] ⚠️ Files: {len(local_files)} files > giới hạn 5 của Etsy — chỉ upload 5 file đầu")
        local_files = local_files[:5]

    log(f"[PUSH] 📎 Files: {len(local_files)} files local — đang xử lý...")

    # Vào tab Item Details
    form_type = await detect_form_type(page)
    if form_type == "tabs":
        await click_tab(page, "Item Details", "Details")
        await page.wait_for_timeout(1500)

    # Helper function to normalize/clean filename stem for fuzzy matching
    def clean_filename_stem(filename: str) -> tuple[str, str]:
        import unicodedata
        path = Path(filename.lower())
        ext = path.suffix
        stem = path.stem
        # Khử dấu tiếng Việt
        stem = ''.join(c for c in unicodedata.normalize('NFD', stem) if unicodedata.category(c) != 'Mn')
        # Giữ lại chỉ chữ cái và chữ số, loại bỏ khoảng trắng, gạch ngang, gạch dưới
        stem = re.sub(r'[^a-z0-9]+', '', stem)
        return stem, ext

    # Helper function to query files currently listed on Etsy (hỗ trợ đa ngôn ngữ, trích xuất chuẩn xác)
    async def get_etsy_files_with_buttons(page):
        return await page.evaluate('''() => {
            const container = document.getElementById('field-digitalFiles');
            if (!container) return [];
            
            const items = Array.from(container.querySelectorAll('[data-clg-id="WtUploadItem"], .wt-upload__item'));
            return items.map((item, idx) => {
                const removeBtn = item.querySelector('button[data-testid="digital_file_action_remove"], button[aria-label*="Remove" i], button[aria-label*="Xóa" i], button[aria-label*="gỡ" i]');
                let filename = "";
                if (removeBtn) {
                    const descId = removeBtn.getAttribute('aria-describedby');
                    if (descId) {
                        const descEl = document.getElementById(descId);
                        if (descEl) {
                            const truncateSpan = descEl.querySelector('.wt-text-truncate');
                            if (truncateSpan) {
                                filename = truncateSpan.innerText.trim();
                            } else {
                                filename = descEl.innerText.replace(/[\\d\\.]+\\s*(?:mb|kb|gb|b)\\b/i, '').trim();
                            }
                        }
                    }
                }
                if (!filename) {
                    const truncateSpan = item.querySelector('.wt-text-truncate');
                    if (truncateSpan) {
                        filename = truncateSpan.innerText.trim();
                    }
                }
                if (!filename) {
                    // Fallback duyệt tìm text chứa dấu chấm
                    const els = Array.from(item.querySelectorAll('span, p, div'));
                    for (const el of els) {
                        if (el.children.length === 0) {
                            const txt = el.innerText.trim();
                            if (txt && txt.includes('.') && !txt.toLowerCase().includes('remove') && !txt.toLowerCase().includes('delete') && !txt.toLowerCase().includes('xóa') && !txt.toLowerCase().includes('gỡ')) {
                                filename = txt;
                                break;
                            }
                        }
                    }
                }
                return { index: idx, filename: filename };
            });
        }''')

    local_files_clean = [clean_filename_stem(Path(lf).name) for lf in local_files]

    # 1. Loop to delete extra files on Etsy (files not matching local list)
    while True:
        etsy_files = await get_etsy_files_with_buttons(page)
        to_remove = None
        for item in etsy_files:
            # RÀNG BUỘC AN TOÀN: Nếu không đọc được tên file (rỗng), bỏ qua không xóa để tránh xóa nhầm sạch file
            if not item['filename']:
                continue
                
            etsy_stem, etsy_ext = clean_filename_stem(item['filename'])
            # Đối soát khớp một phần (partial match) của stem, đề phòng tên file bị cắt ngắn (ellipsis) trên Etsy
            matched = False
            for lf_stem, lf_ext in local_files_clean:
                if lf_ext == etsy_ext:
                    # Nếu một trong hai stem chứa nhau (đảm bảo hỗ trợ ellipsis cắt ngắn)
                    if etsy_stem in lf_stem or lf_stem in etsy_stem:
                        matched = True
                        break
            if not matched:
                to_remove = item
                break
        
        if not to_remove:
            break
            
        idx = to_remove['index']
        name = to_remove['filename']
        log(f"[PUSH] 🧹 Phát hiện file dư thừa trên Etsy: '{name}' — đang xóa...")
        
        try:
            # Bấm nút xóa trực tiếp trong trình duyệt để tránh lỗi locator theo tiếng Anh
            await page.evaluate('''async (index) => {
                const container = document.getElementById('field-digitalFiles');
                if (!container) return;
                const btns = Array.from(container.querySelectorAll('button')).filter(btn => {
                    const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                    const text = (btn.innerText || '').toLowerCase();
                    return label.includes('remove') || label.includes('delete') || label.includes('xóa') || label.includes('gỡ') ||
                           text.includes('remove') || text.includes('delete') || text.includes('xóa') || text.includes('gỡ');
                });
                if (btns[index]) {
                    btns[index].scrollIntoView();
                    btns[index].click();
                }
            }''', idx)
            await page.wait_for_timeout(600)
            confirm = page.locator(
                'div[role="dialog"]:visible button:has-text("Remove"), '
                'div[role="dialog"]:visible button:has-text("Delete"), '
                'div[role="dialog"]:visible button:has-text("Xóa"), '
                'div[role="dialog"]:visible button:has-text("Gỡ bỏ"), '
                'button:has-text("Remove file"):visible, '
                'button:has-text("Xóa file"):visible'
            ).first
            if await confirm.count() > 0 and await confirm.is_visible():
                await confirm.click()
                await page.wait_for_timeout(1000)
        except Exception as e:
            log(f"[PUSH] ⚠️ Không thể xóa file '{name}': {e}")
            break

    # 2. Filter local files that are not already on Etsy
    etsy_files = await get_etsy_files_with_buttons(page)
    
    files_to_upload = []
    for lf in local_files:
        lf_stem, lf_ext = clean_filename_stem(Path(lf).name)
        matched = False
        for item in etsy_files:
            if not item['filename']:
                continue
            ef_stem, ef_ext = clean_filename_stem(item['filename'])
            if lf_ext == ef_ext:
                if ef_stem in lf_stem or lf_stem in ef_stem:
                    matched = True
                    break
        if not matched:
            files_to_upload.append(lf)
            
    if not files_to_upload:
        log("[PUSH] 📎 Tất cả files local đều đã tồn tại trên Etsy — KHÔNG cần upload lại ✅")
        return

    # 3. Upload only missing files
    log(f"[PUSH] 📤 Đang upload {len(files_to_upload)} files mới...")
    try:
        # Step 1: Tìm nút Add File (hỗ trợ cả tiếng Anh và tiếng Việt)
        add_btn = page.locator(
            '#field-digitalFiles button:has-text("Add file"), '
            '#field-digitalFiles button:has-text("Thêm file"), '
            '#field-digitalFiles button:has-text("Tải file"), '
            'button:has-text("Add file"), '
            'button:has-text("Thêm file"), '
            'button:has-text("Upload file"), '
            'button:has-text("Upload"), '
            'button:has-text("Add a file")'
        ).first

        btn_disabled = False
        if await add_btn.count() > 0:
            btn_disabled = await add_btn.is_disabled()

        uploaded_via_chooser = False
        if not btn_disabled and await add_btn.count() > 0:
            try:
                async with page.expect_file_chooser(timeout=15000) as fc_info:
                    await add_btn.scroll_into_view_if_needed()
                    await add_btn.click()
                file_chooser = await fc_info.value
                await file_chooser.set_files(files_to_upload)
                uploaded_via_chooser = True
            except Exception as fc_err:
                log(f"[PUSH] ⚠️ File chooser failed: {fc_err} — trying direct input fallback...")

        if not uploaded_via_chooser:
            # Fallback: Thử set_input_files trực tiếp trên file input
            uploaded_fallback = False
            for sel in [
                '#field-digitalFiles input[type="file"]',
                'input[type="file"][accept*="pdf"]',
                'input[type="file"][accept*="zip"]',
            ]:
                fi = page.locator(sel).first
                if await fi.count() > 0:
                    await fi.set_input_files(files_to_upload, timeout=60000)
                    uploaded_fallback = True
                    break
            else:
                # Fallback: thử input file chung
                fi = page.locator('input[type="file"]').last
                if await fi.count() > 0:
                    await fi.set_input_files(files_to_upload, timeout=60000)
                    uploaded_fallback = True

        # Đợi cho từng file hoàn tất upload và xuất hiện trong danh sách hiển thị
        for lf in files_to_upload:
            file_name = Path(lf).name
            lf_stem, lf_ext = clean_filename_stem(file_name)
            log(f"[PUSH] ⏳ Đợi upload file hoàn tất: {file_name} (tối đa 60s)...")
            uploaded = False
            for _ in range(60):
                await page.wait_for_timeout(1000)
                
                # Truy vấn danh sách file thực tế trên Etsy bằng JS
                etsy_files_current = await get_etsy_files_with_buttons(page)
                
                matched = False
                for item in etsy_files_current:
                    if not item['filename']:
                        continue
                    ef_stem, ef_ext = clean_filename_stem(item['filename'])
                    if lf_ext == ef_ext:
                        if ef_stem in lf_stem or lf_stem in ef_stem:
                            matched = True
                            break
                if matched:
                    uploaded = True
                    break
                    
            if uploaded:
                log(f"[PUSH] 📎 Upload file thành công: {file_name} ✅")
            else:
                raise Exception(f"Upload file {file_name} thất bại! Không thấy xuất hiện trên danh sách Etsy.")
                
    except Exception as e:
        # Cơ chế an toàn: Ném lỗi để ngắt tiến trình lưu listing nếu gặp sự cố upload
        raise Exception(f"Lỗi khi tải file lên Etsy: {e}")

    # 4. Đối soát cuối cùng trước khi hoàn tất
    etsy_files_final = await get_etsy_files_with_buttons(page)
    
    missing_on_etsy = []
    for lf in local_files:
        lf_stem, lf_ext = clean_filename_stem(Path(lf).name)
        matched = False
        for item in etsy_files_final:
            if not item['filename']:
                continue
            ef_stem, ef_ext = clean_filename_stem(item['filename'])
            if lf_ext == ef_ext:
                if ef_stem in lf_stem or lf_stem in ef_stem:
                    matched = True
                    break
        if not matched:
            missing_on_etsy.append(Path(lf).name)

    if missing_on_etsy:
        raise Exception(f"Đồng bộ file thất bại! Thiếu các file trên Etsy: {', '.join(missing_on_etsy)}")

    log("[PUSH] 📎 Đồng bộ files hoàn tất và khớp với local! ✅")


# ── Save listing ───────────────────────────────────────────────────────────────
async def save_listing(page) -> bool:
    log("[PUSH] 💾 Đang lưu thay đổi (Save/Publish)...")
    try:
        # 1. Scroll xuống cuối trang để nút Publish/Save xuất hiện
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1000)

        SAVE_BTN_SELECTOR = (
            'button:has-text("Publish changes"):visible, '
            'button:has-text("Publish listing"):visible, '
            'button:has-text("Publish"):visible, '
            'button:has-text("Save changes"):visible, '
            'button:has-text("Save draft"):visible, '
            'button:has-text("Save as draft"):visible, '
            'button:has-text("Save Draft"):visible, '
            'button:has-text("Save"):visible, '
            'button:has-text("Save and continue"):visible, '
            'button[data-testid*="save" i]:visible, '
            'button[type="submit"]:visible'
        )
        
        btn = page.locator(SAVE_BTN_SELECTOR).first
        clicked_text = ""
        
        try:
            await btn.wait_for(state="visible", timeout=10000)
            if await btn.count() > 0 and await btn.is_visible():
                await btn.scroll_into_view_if_needed()
                clicked_text = (await btn.inner_text() or "").strip()
                await btn.click()
                log(f"[PUSH] 🖱️ Đã click nút '{clicked_text}' bằng Playwright")
        except Exception:
            # Fallback: tìm và click bằng JS nếu selector Playwright không khớp
            log("[PUSH] ⚠️ Không tìm thấy nút Save bằng selector chuẩn — thử fallback JS...")
            clicked_text = await page.evaluate('''() => {
                const keywords = ["Publish changes", "Publish listing", "Publish", "Save changes", "Save draft", "Save as draft", "Save Draft", "Save", "Save and continue"];
                const buttons = Array.from(document.querySelectorAll("button"));
                for (const kw of keywords) {
                    const btn = buttons.find(b => b.innerText.trim().startsWith(kw) && b.offsetParent !== null);
                    if (btn) { btn.click(); return btn.innerText.trim(); }
                }
                return null;
            }''')
            if clicked_text:
                log(f"[PUSH] ✅ Đã click nút '{clicked_text}' qua JS fallback")
            else:
                log("[PUSH] ❌ Không tìm thấy nút Save/Publish trên trang — Etsy UI có thể đã thay đổi")
                return False

        # Đợi xử lý lưu và kiểm tra lỗi validation
        log("[PUSH] ⏳ Đang đợi Etsy xử lý lưu...")
        errors_found = []
        saved_successfully = False
        
        for _wait in range(25):
            await page.wait_for_timeout(1000)
            
            # Kiểm tra lỗi validation trên giao diện
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
                
            # Nếu URL thay đổi (không còn ở edit page) hoặc có thông báo thành công
            current_url = page.url
            if "edit/" not in current_url or "listings" in current_url:
                saved_successfully = True
                break
                
            # Đôi khi lưu xong không redirect nhưng hiển thị banner thành công
            try:
                success_banner = await page.locator('.wt-alert--success:visible, [role="alert"]:has-text("published" i):visible, [role="alert"]:has-text("saved" i):visible').count()
                if success_banner > 0:
                    saved_successfully = True
                    break
            except Exception:
                pass

        # Kiểm tra xem có dòng chữ báo không có thay đổi nào chưa lưu (tức là đã đồng bộ khớp hoàn toàn)
        no_changes_detected = False
        try:
            no_changes_text = await page.locator(':has-text("You have no unsaved changes")').count()
            if no_changes_text > 0:
                no_changes_detected = True
        except Exception:
            pass

        if no_changes_detected:
            log("[PUSH] ℹ️ Etsy báo: 'You have no unsaved changes' (Không có thay đổi nào cần lưu)")
            saved_successfully = True

        if errors_found or (not saved_successfully and not errors_found):
            # Kiểm tra xem có từ khóa lỗi thực sự trong body không (loại bỏ 'required', 'bắt buộc' vì là label tĩnh)
            try:
                body_text = await page.inner_text("body")
                for err_word in ["invalid", "lỗi", "cannot"]:
                    if err_word in body_text.lower():
                        if not any(msg for msg in errors_found if err_word in msg.lower()):
                            errors_found.append(f"Phát hiện từ khóa lỗi '{err_word}'")
            except Exception:
                pass

        if errors_found:
            log("[PUSH] ❌ Lưu thay đổi thất bại! Phát hiện lỗi trên trang:")
            for err in errors_found:
                log(f"     • {err}")
            # Chụp màn hình lỗi để debug
            try:
                screenshot_path = str(BASE_DIR / "save_push_failure.png")
                await page.screenshot(path=screenshot_path, full_page=True)
                log(f"[PUSH] 📸 Đã chụp ảnh màn hình lỗi tại: {screenshot_path}")
            except Exception as ss_ex:
                log(f"[PUSH] ⚠️ Không thể chụp ảnh màn hình: {ss_ex}")
            return False

        if not saved_successfully:
            try:
                is_still_visible = False
                if clicked_text:
                    still_btn = page.locator(f'button:has-text("{clicked_text}"):visible').first
                    if await still_btn.count() > 0 and await still_btn.is_visible():
                        is_still_visible = True
                if is_still_visible:
                    log("[PUSH] ❌ Lưu thất bại (Nút Save vẫn còn hiển thị trên màn hình sau 25 giây)")
                    try:
                        screenshot_path = str(BASE_DIR / "save_push_timeout.png")
                        await page.screenshot(path=screenshot_path, full_page=True)
                    except Exception:
                        pass
                    return False
            except Exception:
                pass

        log("[PUSH] ✅ Đã lưu thay đổi thành công!")
        return True

    except Exception as e:
        log(f"[PUSH] ❌ Lỗi khi thực hiện Save: {e}")
        try:
            screenshot_path = str(BASE_DIR / "save_push_exception.png")
            await page.screenshot(path=screenshot_path, full_page=True)
        except Exception:
            pass
        return False


# ── Main push orchestrator ─────────────────────────────────────────────────────
async def push_all(page, listing_id: str, product: dict, fields: set) -> bool:
    edit_url = build_edit_url(listing_id)
    log(f"[PUSH] 🌐 Đang vào: {edit_url}")

    try:
        await page.goto(edit_url, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        log("[PUSH] ⚠️ domcontentloaded timeout — thử lại với networkidle...")
        try:
            await page.goto(edit_url, wait_until="networkidle", timeout=60000)
        except Exception as e:
            log(f"[PUSH] ❌ Không mở được trang editor: {e}")
            return False
    # Tăng thời gian chờ lên 8s để đảm bảo các thành phần React nặng (như digital files) được load đầy đủ
    await page.wait_for_timeout(8000)

    if "listing-editor" not in page.url:
        log(f"[PUSH] ❌ Không vào được editor. URL: {page.url}")
        return False

    # Etsy can keep the listing-editor URL while rendering its generic 404
    # page (for example when the listing was deleted, moved to another shop,
    # or the listing ID is stale). Do this check before touching any fields;
    # otherwise the later image step misleadingly reports a missing uploader.
    try:
        body_text = (await page.locator("body").inner_text(timeout=10000)).lower()
    except Exception:
        body_text = ""
    not_found_markers = (
        "page you were looking for was not found",
        "the page you were looking for was not found",
        "uh oh!",
        "listing not found",
        "couldn't find that listing",
    )
    if any(marker in body_text for marker in not_found_markers):
        log(f"[PUSH] ❌ Etsy không tìm thấy listing {listing_id}. URL: {page.url}")
        try:
            await page.screenshot(path=str(BASE_DIR / "save_push_listing_not_found.png"), full_page=True)
        except Exception:
            pass
        return False

    await dismiss_alerts(page)

    # --- 1. Text fields (title, desc, tags) ---
    text_fields = fields & {"title", "description", "tags"}
    if text_fields:
        await push_text_fields(page, product, text_fields)

    # --- 2. Price / Qty ---
    pricing_fields = fields & {"price", "qty"}
    if pricing_fields:
        await push_pricing(page, product, pricing_fields)

    # --- 3. Images ---
    if "images" in fields:
        await push_images(page, product)

    # --- 4. Files ---
    if "files" in fields:
        await push_files(page, product)

    # --- 5. Save/Publish ---
    ok = await save_listing(page)
    if not ok:
        log("[PUSH] ❌ Lưu thay đổi thất bại!")
        return False

    return True


# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser(description="Push local data lên Etsy listing")
    parser.add_argument("--listing-id",  required=True,  help="Etsy listing ID hoặc URL")
    parser.add_argument("--row",         type=int, default=None, help="Row trong Excel")
    parser.add_argument("--shop",        default="templystudios")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Chỉ kiểm tra đúng profile/shop và quyền mở Shop Manager; không sửa listing.",
    )
    parser.add_argument(
        "--fields",
        default="title,description,tags,price,qty",
        help="Trường cần push, cách nhau bằng dấu phẩy. Mặc định: title,description,tags,price,qty. "
             "Thêm 'images' và/hoặc 'files' nếu muốn."
    )
    args = parser.parse_args()

    fields = set(f.strip().lower() for f in args.fields.split(",") if f.strip())
    valid_fields = {"title", "description", "tags", "price", "qty", "images", "files"}
    fields = fields & valid_fields
    if not fields:
        log("[PUSH] ❌ Không có trường hợp lệ nào để push")
        sys.exit(1)

    if not args.row:
        log("[PUSH] ❌ Cần --row để đọc dữ liệu từ Excel")
        sys.exit(1)

    log(f"[PUSH] 📖 Đọc dữ liệu từ Excel row {args.row}...")
    product = read_product_from_excel(args.row, args.shop)

    log(f"\n{'='*60}")
    log(f"  🚀 Push to Etsy — {product['folder']} → listing {args.listing_id}")
    log(f"  📋 Fields: {', '.join(sorted(fields))}")
    log(f"  📝 Title: {product['title'][:55]}...")
    log(f"  💲 Price: ${product['price']:.2f} | Qty: {product['qty']}")
    log(f"  🖼  Images local: {len(product['image_paths'])} ảnh")
    log(f"  📎 Files local: {len(product['file_paths'])} files")
    log(f"{'='*60}\n")

    browser_dir = browser_dir_for_shop(args.shop)
    log(f"  🔑 Browser session: {browser_dir}")

    # Clear Chrome lock files
    browser_dir.mkdir(parents=True, exist_ok=True)
    for lock in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        try:
            (browser_dir / lock).unlink(missing_ok=True)
        except Exception:
            pass

    async with async_playwright() as pw:
        launch_kw = dict(
            user_data_dir=str(browser_dir),
            headless=False,
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            viewport=None,
        )
        if CHROME_PATH.exists():
            launch_kw["executable_path"] = str(CHROME_PATH)
            log("  🌐 Dùng Google Chrome thật")

        ctx  = await pw.chromium.launch_persistent_context(**launch_kw)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Kiểm tra đăng nhập — tăng timeout lên 60s, fallback nếu bị lỗi
        try:
            await page.goto("https://www.etsy.com/your/shops/me/tools/listings",
                            wait_until="domcontentloaded", timeout=60000)
        except Exception:
            log("[PUSH] ⚠️ domcontentloaded timeout — thử lại với networkidle...")
            try:
                await page.goto("https://www.etsy.com/your/shops/me/tools/listings",
                                wait_until="networkidle", timeout=60000)
            except Exception as e:
                log(f"[PUSH] ❌ Không thể mở trang Etsy: {e}")
                sys.exit(1)
        await page.wait_for_timeout(4000)

        if "signin" in page.url or "join" in page.url:
            log("\n  ⚠  Cần đăng nhập Etsy! Đợi tối đa 3 phút...")
            for _wait in range(180):
                await page.wait_for_timeout(1000)
                if "signin" not in page.url and "join" not in page.url:
                    break
                if _wait % 15 == 0:
                    log(f"  ⏳ Đợi đăng nhập... ({_wait}s)")
            try:
                await page.goto("https://www.etsy.com/your/shops/me/tools/listings",
                                wait_until="domcontentloaded", timeout=60000)
            except Exception:
                await page.goto("https://www.etsy.com/your/shops/me/tools/listings",
                                wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(3000)

        log("  ✅ Đã vào Shop Manager!\n")
        await assert_expected_shop(page, args.shop)

        if args.check_only:
            log(f"[PUSH-CHECK] ✅ Profile và shop hợp lệ cho {args.shop}; không thay đổi listing.")
            await ctx.close()
            return

        ok = await push_all(page, args.listing_id, product, fields)

        if ok:
            log(f"\n[PUSH] ✅ Hoàn tất! Đã push {', '.join(sorted(fields))} → listing {args.listing_id}")
        else:
            log(f"\n[PUSH] ❌ Thất bại.")
            sys.exit(1)

        await page.wait_for_timeout(2000)
        await ctx.close()


if __name__ == "__main__":
    os.environ.setdefault("ALLOW_ETSY_POST", "1")
    asyncio.run(main())
