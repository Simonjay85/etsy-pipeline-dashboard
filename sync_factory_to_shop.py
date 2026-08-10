#!/usr/bin/env python3
"""
sync_factory_to_shop.py - Synchronize and watch Etsy Image Factory output and import to dashboard.

Usage:
  python3 sync_factory_to_shop.py [--shop templystudios] [--watch] [--poll 5]
"""
import argparse
import json
import time
import shutil
import re
import urllib.parse
from pathlib import Path
import openpyxl

from shop_asset_workflow import (
    copy_image_with_watermark,
    get_watermark_text,
)

BASE_DIR = Path(__file__).parent
FACTORY_SHOP_ID = "templystudios"
SRC_DIR = BASE_DIR / "shops" / FACTORY_SHOP_ID
ACTIVE_SHOP_FILE = BASE_DIR / "active_shop.txt"
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
CONFIG_FILE = BASE_DIR / "shops_config.json"
_FACTORY_IMAGE_DIR_NAME = "etsy-images-chatgpt-final"
_FACTORY_ZIP_DIR_NAME = "source-zip"
_FACTORY_PRODUCT_RE = re.compile(r"^product-\d+$")
_FACTORY_SOURCE_EXCLUDE_DIRS = {
    "_deleted_products",
    "_asset_quarantine",
    "_failed_local_imports",
    "master_products",
}


def is_supported_factory_source(folder: Path) -> bool:
    if folder.is_symlink() or not folder.is_dir():
        return False
    if folder.name.startswith(".") or folder.name.startswith("_"):
        return False
    if _FACTORY_PRODUCT_RE.fullmatch(folder.name) or folder.name in _FACTORY_SOURCE_EXCLUDE_DIRS:
        return False
    image_dir = folder / _FACTORY_IMAGE_DIR_NAME
    zip_dir = folder / _FACTORY_ZIP_DIR_NAME
    return (
        not image_dir.is_symlink()
        and image_dir.is_dir()
        and not zip_dir.is_symlink()
        and zip_dir.is_dir()
    )

def _factory_asset_is_usable(path: Path, allowed_suffixes: set[str]) -> bool:
    if path.is_symlink() or not path.is_file() or path.suffix.lower() not in allowed_suffixes:
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    return stat.st_size > 0 and getattr(stat, "st_blocks", 1) > 0

def _collect_image_nodes(parent: Path) -> list[Path]:
    if not parent.is_dir():
        return []
    return sorted([
        f for f in parent.iterdir()
        if _factory_asset_is_usable(f, IMG_EXTS)
    ], key=lambda p: (p.name != "01_hero_image.png", p.name.lower()))

def _collect_download_nodes(parent: Path) -> list[Path]:
    if not parent.is_dir():
        return []
    return sorted([
        f for f in parent.iterdir()
        if _factory_asset_is_usable(f, {".zip"})
    ], key=lambda p: p.name.lower())


def load_shop_config(shop_id: str) -> dict:
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            return json.load(f).get(shop_id, {})
    except (OSError, json.JSONDecodeError):
        return {}


def clean_html(text: str) -> str:
    return (text or "").replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'").strip()

def normalize_name(name: object) -> str:
    text = str(name or "").lower()
    text = text.replace("-", " ").replace("_", " ").strip()
    return re.sub(r"\s+", " ", text)

def get_factory_images(folder: Path) -> list[Path]:
    candidates = _collect_image_nodes(folder / _FACTORY_IMAGE_DIR_NAME)
    # Deduplicate paths
    seen = set()
    deduped = []
    for p in candidates:
        if p.name not in seen:
            seen.add(p.name)
            deduped.append(p)
    return sorted(deduped, key=lambda p: (p.name != "01_hero_image.png", p.name.lower()))

def get_factory_files(folder: Path) -> list[Path]:
    return _collect_download_nodes(folder / _FACTORY_ZIP_DIR_NAME)

def is_folder_stable(folder: Path, wait_secs: int = 5) -> bool:
    """Checks if files size/mtime haven't changed for wait_secs seconds."""
    def snapshot(f):
        assets = get_factory_images(f) + get_factory_files(f)
        return {
            str(p.relative_to(f)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in assets
        }
    try:
        s1 = snapshot(folder)
        time.sleep(wait_secs)
        s2 = snapshot(folder)
        return s1 == s2
    except Exception:
        return False

def _read_active_shop_id() -> str:
    try:
        return ACTIVE_SHOP_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""

def sync_shop(shop_id: str) -> bool:
    if shop_id != FACTORY_SHOP_ID:
        print(f"❌ Sync only supports Temply Studio: {FACTORY_SHOP_ID}")
        return False
    active_shop = _read_active_shop_id()
    if active_shop != FACTORY_SHOP_ID:
        print(f"❌ Sync blocked: active_shop.txt must be {FACTORY_SHOP_ID!r}, got {active_shop!r}")
        return False
    dst_dir = BASE_DIR / "shops" / shop_id
    shop_config = load_shop_config(shop_id)
    factory_dir = SRC_DIR
    excel_path = dst_dir / "Etsy_SEO_Generator.xlsx"

    if not dst_dir.exists():
        print(f"❌ Shop directory not found: {dst_dir}")
        return False
    if not excel_path.exists():
        print(f"❌ Excel file not found: {excel_path}")
        return False
    if not factory_dir.exists():
        print(f"❌ Source directory not found: {factory_dir}")
        return False

    print(f"🔄 Starting sync for shop {shop_id}...")
    print(f"   Source: {factory_dir}")
    print(f"   Destination: {dst_dir}")
    watermark = get_watermark_text(shop_id, shop_config)
    print(f"   Watermark: {watermark}")

    # Load spreadsheet
    wb = openpyxl.load_workbook(excel_path)
    ws = wb["Listings"]

    # Build mapping only from catalog-backed product folders in this Temply
    # shop. Raw source slugs and unrelated folders never participate.
    imported_map = {}
    for r in range(4, ws.max_row + 1):
        folder = ws.cell(row=r, column=2).value
        kw = ws.cell(row=r, column=3).value
        folder_name = str(folder or "").strip()
        product_dir = dst_dir / folder_name
        if (
            not _FACTORY_PRODUCT_RE.fullmatch(folder_name)
            or product_dir.is_symlink()
            or not product_dir.is_dir()
            or product_dir.resolve().parent != dst_dir.resolve()
        ):
            continue
        if kw:
            imported_map[normalize_name(kw)] = (folder_name, r)

    existing_numbers = []
    for item in dst_dir.glob("product-*"):
        if not item.is_dir():
            continue
        try:
            existing_numbers.append(int(item.name.split("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    next_product_num = max(existing_numbers, default=0) + 1

    # Scan factory directories
    factory_folders = [
        d for d in sorted(factory_dir.iterdir(), key=lambda d: d.name.lower())
        if is_supported_factory_source(d)
    ]

    wb_modified = False

    for src_folder in factory_folders:
        images = get_factory_images(src_folder)
        files = get_factory_files(src_folder)

        if not images or not files:
            continue  # incomplete factory folder, skip

        # The downloadable filename is the stable product identity used in the
        # dashboard keyword column.
        source_keyword = files[0].stem.replace("-", " ").replace("_", " ").strip()
        folder_keyword = normalize_name(source_keyword)

        matched = imported_map.get(folder_keyword)
        if matched:
            folder_name, row_num = matched
            print(f"  ⏭ Skipped {src_folder.name}: already cataloged as {folder_name} (row {row_num})")
            continue
        else:
            # Always allocate a collision-free folder number in the target shop.
            while (dst_dir / f"product-{next_product_num:02d}").exists():
                next_product_num += 1
            folder_name = f"product-{next_product_num:02d}"
            next_product_num += 1
            prod_path = dst_dir / folder_name
            img_dst = prod_path / "images"
            file_dst = prod_path / "files"
            img_dst.mkdir(parents=True, exist_ok=True)
            file_dst.mkdir(parents=True, exist_ok=True)

            copied_imgs = 0
            for f in images:
                copy_image_with_watermark(f, img_dst / f.name, watermark)
                copied_imgs += 1

            copied_files = 0
            file_stems = []
            for f in files:
                shutil.copy2(f, file_dst / f.name)
                copied_files += 1
                file_stems.append(f.stem)

            # Build keyword
            keyword = source_keyword

            # Find empty row in Excel B column or append
            target_row = None
            for r in range(4, ws.max_row + 2):
                val = ws.cell(row=r, column=2).value
                if val is None or str(val).strip() == "":
                    target_row = r
                    break
            if not target_row:
                target_row = ws.max_row + 1

            # Write Excel row
            ws.cell(row=target_row, column=1, value=target_row - 3)      # A: STT
            ws.cell(row=target_row, column=2, value=folder_name)        # B: Folder
            ws.cell(row=target_row, column=3, value=keyword)            # C: Keywords
            ws.cell(row=target_row, column=5, value=4.99)               # E: Price
            ws.cell(row=target_row, column=11, value=999)               # K: Quantity
            ws.cell(row=target_row, column=12, value="I did")           # L: Who Made
            ws.cell(row=target_row, column=13, value="2020_2026")       # M: When Made
            ws.cell(row=target_row, column=14, value="⏳ Chờ đăng")       # N: Status
            ws.cell(row=target_row, column=15, value="Digital Planner")  # O: Section

            print(f"  🆕 Imported {src_folder.name} -> {folder_name} (Row {target_row}) | Keyword: {keyword}")
            # Update map to avoid duplicate rows within this run
            imported_map[normalize_name(keyword)] = (folder_name, target_row)
            wb_modified = True

    if wb_modified:
        wb.save(excel_path)
        print("💾 Excel spreadsheet updated and saved.")
    else:
        print("✅ Sync completed. No spreadsheet changes needed.")
    return True

def get_dir_fingerprint(directory: Path) -> dict:
    """Return exact direct asset fingerprints for complete Temply sources."""
    fingerprint = {}
    if not directory.exists():
        return fingerprint
    for item in directory.iterdir():
        if is_supported_factory_source(item):
            assets = get_factory_images(item) + get_factory_files(item)
            fingerprint[item.name] = tuple(
                (
                    str(path.relative_to(item)),
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                )
                for path in sorted(assets, key=lambda p: str(p.relative_to(item)))
            )
    return fingerprint

def watch_folders(shop_id: str, poll_interval: int):
    if shop_id != FACTORY_SHOP_ID:
        print(f"❌ Watcher only supports Temply Studio: {FACTORY_SHOP_ID}")
        return
    factory_dir = SRC_DIR
    print(f"👁 Watcher started. Monitoring: {factory_dir}")
    print(f"   Press Ctrl+C to stop.")
    
    # If the active shop is not Temply, keep the state empty so a later switch
    # back to Temply re-evaluates all complete source folders.
    last_active_shop = _read_active_shop_id()
    known_state = get_dir_fingerprint(factory_dir) if last_active_shop == FACTORY_SHOP_ID else {}
    
    while True:
        try:
            time.sleep(poll_interval)
            active_shop = _read_active_shop_id()
            if active_shop != FACTORY_SHOP_ID:
                known_state = {}
                last_active_shop = active_shop
                continue
            if last_active_shop != FACTORY_SHOP_ID:
                known_state = {}
            last_active_shop = active_shop
            current_state = get_dir_fingerprint(factory_dir)
            
            # Check for additions or modifications
            changed = False
            blocked = False
            for folder, fingerprint in current_state.items():
                if known_state.get(folder) != fingerprint:
                    # Folder is new or has new files
                    folder_path = factory_dir / folder
                    print(f"🔔 Detected change in Image Factory folder: {folder} — waiting to stabilize...")
                    if is_folder_stable(folder_path, wait_secs=5):
                        print(f"📈 Folder {folder} is stable. Triggering sync...")
                        if sync_shop(shop_id):
                            changed = True
                        else:
                            blocked = True
                    else:
                        print(f"⏳ Folder {folder} is still writing/copying files...")
            
            if changed:
                known_state = get_dir_fingerprint(factory_dir)
            elif not blocked:
                # Update known state for stable-check failures only when sync was
                # not blocked; blocked changes must remain pending for Temply.
                known_state = current_state
                
        except KeyboardInterrupt:
            print("\n👋 Watcher stopped.")
            break
        except Exception as e:
            print(f"❌ Watcher error: {e}")
            time.sleep(poll_interval)

def main():
    parser = argparse.ArgumentParser(description="Sync Image Factory to Dashboard Shop")
    parser.add_argument("--shop", default="templystudios", help="Active shop ID")
    parser.add_argument("--watch", action="store_true", help="Run in daemon watcher mode")
    parser.add_argument("--poll", type=int, default=5, help="Poll interval in seconds")
    args = parser.parse_args()

    if args.shop != FACTORY_SHOP_ID:
        print(f"❌ Unsupported shop '{args.shop}'. This sync helper is Temply-only: {FACTORY_SHOP_ID}")
        return

    # Initial sync
    sync_shop(args.shop)

    if args.watch:
        watch_folders(args.shop, args.poll)

if __name__ == "__main__":
    main()
