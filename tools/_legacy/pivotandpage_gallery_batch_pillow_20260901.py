#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

import openpyxl
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageStat

ROOT = Path(__file__).resolve().parents[1]
SHOP_ID = "shop3"
SHOP_ROOT = ROOT / "shops" / SHOP_ID
WORKBOOK = SHOP_ROOT / "Etsy_SEO_Generator.xlsx"
WORKING_ROOT = ROOT / "working" / "pivotandpage-furniture-gallery"
BACKUP_ROOT = ROOT / "output" / "backup" / "pivotandpage-furniture-gallery"
REPORT_ROOT = ROOT / "output" / "diagnostics"
DASHBOARD = "http://127.0.0.1:8090"

TARGET_PREFIXES = (
    "Wooden Stump Stool",
    "Pair of Wooden Stools",
    "Wood Display Pedestal",
    "Wood Furniture Set",
    "Wood Table Base",
    "Wooden Table",
    "Wood Table",
)

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
OUTPUT_NAMES = [f"etsy_{i:02d}.jpg" for i in range(1, 11)]
CANVAS = 2000
CREAM = "#F2ECE2"
CREAM_2 = "#E4D8C9"
PAPER = "#FBF8F2"
INK = "#2D211A"
BROWN = "#60402F"
MUTED = "#75675D"
OLIVE = "#66705A"
CLAY = "#A56F50"
WHITE = "#FFFDF8"

FONT_SERIF = "/System/Library/Fonts/Supplemental/Georgia.ttf"
FONT_SERIF_BOLD = "/System/Library/Fonts/Supplemental/Georgia Bold.ttf"
FONT_SANS = "/System/Library/Fonts/Supplemental/Arial.ttf"
FONT_SANS_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"

LOGO_X1 = 0.27
LOGO_X2 = 0.73
LOGO_Y1 = 0.02
LOGO_Y2 = 0.32
MAX_LOGO_MASK_RATIO = 0.42


@dataclass
class Product:
    row: int
    folder: str
    title: str
    seed: str
    before_count: int
    product_type: str
    wood: str
    model: str
    source: dict
    dimensions: list[tuple[str, str, str]]
    quantity: str


class Blocked(RuntimeError):
    pass


def now_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def json_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def font(path: str, size: int):
    try:
        return ImageFont.truetype(path, size=size)
    except OSError:
        return ImageFont.load_default()


def normalize_text(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def split_title(title: str) -> tuple[str, str, str]:
    parts = [p.strip() for p in title.split("|")]
    product_type = parts[0] if parts else "Wood Furniture"
    wood = parts[1] if len(parts) > 1 else "Natural wood"
    details = parts[2] if len(parts) > 2 else title
    return product_type, wood, details


def model_from_title(title: str, source: dict) -> str:
    m = re.search(r"\bModel\s+([^|]+?)\s*$", title, flags=re.I)
    if m:
        return m.group(1).strip()
    src = normalize_text(source.get("title") or source.get("list_title"))
    m = re.search(r"\b([A-Z]{1,4}\d{2,6}|\d{4,6})\s*$", src, flags=re.I)
    if m:
        return m.group(1).upper()
    candidate = normalize_text(source.get("template_id"))
    return candidate or "Confirm model"


def numeric_sequence(text: str) -> list[float]:
    vals = []
    for token in re.split(r"[-/]", text.replace(",", ".")):
        token = token.strip()
        if not token:
            continue
        try:
            vals.append(float(token))
        except ValueError:
            return []
    return vals


def fmt_num(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def cm_inches(raw: str) -> tuple[str, str]:
    vals = numeric_sequence(raw)
    if not vals:
        return raw, ""
    cm = "–".join(fmt_num(v) for v in vals)
    inches = "–".join(f"{v / 2.54:.1f}" for v in vals)
    return cm, inches


def parse_dimensions(details: str, source_title: str) -> list[tuple[str, str, str]]:
    text = f"{details} {source_title}"
    patterns = [
        ("Height", r"\bH\s*([0-9]+(?:[.,][0-9]+)?(?:-[0-9]+(?:[.,][0-9]+)?)*)"),
        ("Width", r"\bW\s*([0-9]+(?:[.,][0-9]+)?(?:-[0-9]+(?:[.,][0-9]+)?)*)"),
        ("Depth", r"\bD\s*([0-9]+(?:[.,][0-9]+)?(?:-[0-9]+(?:[.,][0-9]+)?)*)"),
        ("Length", r"\bL\s*([0-9]+(?:[.,][0-9]+)?(?:-[0-9]+(?:[.,][0-9]+)?)*)"),
        ("Top diameter", r"\bTop\s+Diameter\s*([0-9]+(?:[.,][0-9]+)?(?:-[0-9]+(?:[.,][0-9]+)?)*)"),
        ("Top square", r"\bTop\s+Square\s*([0-9]+(?:[.,][0-9]+)?(?:-[0-9]+(?:[.,][0-9]+)?)*)"),
        ("Top", r"\bTop\s*(?!Diameter|Square)([0-9]+(?:[.,][0-9]+)?(?:-[0-9]+(?:[.,][0-9]+)?)*)"),
        ("Bottom", r"\bBottom\s*([0-9]+(?:[.,][0-9]+)?(?:-[0-9]+(?:[.,][0-9]+)?)*)"),
        ("Diameter", r"\bDiameter\s*([0-9]+(?:[.,][0-9]+)?(?:-[0-9]+(?:[.,][0-9]+)?)*)"),
    ]
    result: list[tuple[str, str, str]] = []
    seen = set()
    for label, pat in patterns:
        m = re.search(pat, text, flags=re.I)
        if not m:
            continue
        raw = m.group(1)
        cm, inch = cm_inches(raw)
        key = (label.lower(), cm)
        if key in seen:
            continue
        seen.add(key)
        result.append((label, cm, inch))
    return result[:6]


def quantity_for(product_type: str, source_title: str) -> str:
    low = product_type.lower()
    if "pair" in low:
        return "2 stools"
    if "furniture set" in low:
        ascii_title = source_title
        m = re.search(r"(?:Bộ|Bo)\s+(?:Bàn|Ban).*?(\d+)\s*(?:Đôn|Don)", ascii_title, flags=re.I)
        if not m:
            m = re.search(r"(\d+)\s*(?:Đôn|Don)", ascii_title, flags=re.I)
        if not m:
            raise Blocked("Furniture set quantity is not explicit in source title")
        return f"1 table + {int(m.group(1))} stools"
    if "table base" in low:
        return "1 table base"
    if "pedestal" in low:
        return "1 pedestal"
    if "stool" in low:
        return "1 stool"
    if "table" in low:
        return "1 table"
    return "1 physical item"


def load_catalog() -> list[Product]:
    wb = openpyxl.load_workbook(WORKBOOK, read_only=True, data_only=True)
    ws = wb["Listings"]
    products: list[Product] = []
    try:
        for row in range(4, ws.max_row + 1):
            folder = normalize_text(ws.cell(row, 2).value)
            if not re.fullmatch(r"product-\d+", folder):
                continue
            root = SHOP_ROOT / folder
            if not root.is_dir():
                continue
            title = normalize_text(ws.cell(row, 8).value or ws.cell(row, 4).value)
            if not title.startswith(TARGET_PREFIXES):
                continue
            image_dir = root / "images"
            count = sum(1 for p in image_dir.iterdir() if p.is_file() and p.suffix.lower() in IMG_EXTS) if image_dir.is_dir() else 0
            if count >= 10:
                continue
            source_path = root / "source_catalog" / "source.json"
            if not source_path.is_file():
                source = {}
            else:
                try:
                    source = json.loads(source_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    raise Blocked(f"{folder}: invalid source.json: {exc}") from exc
            product_type, wood, details = split_title(title)
            source_title = normalize_text(source.get("title") or source.get("list_title") or ws.cell(row, 4).value)
            dims = parse_dimensions(details, source_title)
            qty = quantity_for(product_type, source_title)
            products.append(Product(
                row=row,
                folder=folder,
                title=title,
                seed=normalize_text(ws.cell(row, 4).value),
                before_count=count,
                product_type=product_type,
                wood=wood,
                model=model_from_title(title, source),
                source=source,
                dimensions=dims,
                quantity=qty,
            ))
    finally:
        wb.close()
    products.sort(key=lambda p: (p.before_count, int(p.folder.split("-")[-1])))
    return products


def image_records(source: dict) -> list[dict]:
    records = []
    for item in source.get("output_images") or []:
        if not isinstance(item, dict):
            continue
        url = normalize_text(item.get("download_url") or item.get("source_url"))
        if not url:
            continue
        records.append({
            "url": url,
            "sha256": normalize_text(item.get("source_sha256")),
            "source_index": item.get("source_index"),
        })
    if records:
        return records
    for idx, url in enumerate(source.get("image_urls") or [], 1):
        url = normalize_text(url)
        if url:
            records.append({"url": url, "sha256": "", "source_index": idx})
    return records


def download(url: str, destination: Path, expected_sha: str = "") -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size > 0:
        if not expected_sha or sha256(destination) == expected_sha:
            return
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 PivotAndPageGallery/1.0"})
    with urllib.request.urlopen(req, timeout=45) as response:
        data = response.read()
    destination.write_bytes(data)
    if expected_sha and sha256(destination) != expected_sha:
        destination.unlink(missing_ok=True)
        raise Blocked("Downloaded source image checksum does not match source catalog")
    try:
        with Image.open(destination) as im:
            im.verify()
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise Blocked(f"Downloaded source image is not decodable: {exc}") from exc


def is_wood_pixel(pixel: tuple[int, int, int]) -> bool:
    r, g, b = pixel
    mx, mn = max(pixel), min(pixel)
    return (
        r >= 75
        and r > g * 1.04
        and g > b * 1.02
        and (mx - mn) >= 34
        and (r - b) >= 38
        and g <= 190
    )


def roi_wood_ratio(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    crop = image.crop(box).convert("RGB")
    crop.thumbnail((160, 100), Image.Resampling.BILINEAR)
    total = crop.width * crop.height
    if total <= 0:
        return 1.0
    wood = 0
    for px in crop.getdata():
        if is_wood_pixel(px):
            wood += 1
    return wood / total


def neutral_background_color(image: Image.Image) -> tuple[int, int, int]:
    w, h = image.size
    patches = [
        image.crop((0, 0, max(1, int(w * 0.18)), max(1, int(h * 0.28)))),
        image.crop((max(0, int(w * 0.82)), 0, w, max(1, int(h * 0.28)))),
    ]
    channels = [[], [], []]
    for patch in patches:
        patch = patch.convert("RGB")
        patch.thumbnail((60, 60), Image.Resampling.BILINEAR)
        for r, g, b in patch.getdata():
            if max(r, g, b) - min(r, g, b) < 55 and max(r, g, b) > 90:
                channels[0].append(r); channels[1].append(g); channels[2].append(b)
    if not channels[0]:
        return (235, 229, 218)
    return tuple(int(statistics.median(ch)) for ch in channels)  # type: ignore[return-value]


def sanitize_source(image: Image.Image) -> tuple[Image.Image, float, float]:
    """Remove high-contrast achromatic source-logo strokes without repainting product geometry.

    The source watermark sits over product photography, so replacing a whole rectangle
    would destroy grain/edge fidelity.  Instead, detect only achromatic high-contrast
    strokes inside the bounded logo zone, dilate that sparse mask, then clone nearby
    texture from both horizontal directions under the feathered mask.
    """
    image = ImageOps.exif_transpose(image).convert("RGB")
    w, h = image.size
    box = (int(w * LOGO_X1), int(h * LOGO_Y1), int(w * LOGO_X2), int(h * LOGO_Y2))
    wood_ratio = roi_wood_ratio(image, box)
    crop = image.crop(box).convert("RGB")

    gray = crop.convert("L")
    hsv = crop.convert("HSV")
    saturation = hsv.getchannel("S")
    achromatic = saturation.point(lambda p: 255 if p < 82 else 0)
    extremes = gray.point(lambda p: 255 if p < 58 or p > 222 else 0)
    edges = gray.filter(ImageFilter.FIND_EDGES).point(lambda p: 255 if p > 26 else 0)
    mask = ImageChops.multiply(ImageChops.multiply(achromatic, extremes), edges)

    # Expand from stroke edges to cover antialiasing/outline interiors, then feather.
    scale = max(1, int(round(min(crop.size) / 600)))
    kernel = max(9, 9 + 2 * scale)
    if kernel % 2 == 0:
        kernel += 1
    mask = mask.filter(ImageFilter.MaxFilter(kernel))
    raw_mask = mask.copy()
    thumb = raw_mask.copy(); thumb.thumbnail((160, 120), Image.Resampling.NEAREST)
    mask_ratio = sum(1 for v in thumb.getdata() if v > 0) / max(1, thumb.width * thumb.height)
    if mask_ratio > MAX_LOGO_MASK_RATIO:
        raise Blocked(f"Logo mask is too broad for safe texture repair ({mask_ratio:.1%})")

    if mask_ratio < 0.001:
        return image, wood_ratio, mask_ratio

    shift = max(24, int(crop.width * 0.055))
    left = ImageChops.offset(crop, -shift, 0)
    right = ImageChops.offset(crop, shift, 0)
    fill = Image.blend(left, right, 0.5).filter(ImageFilter.GaussianBlur(max(1, shift // 24)))
    feather = mask.filter(ImageFilter.GaussianBlur(max(2, kernel // 4)))
    repaired_crop = Image.composite(fill, crop, feather)
    result = image.copy()
    result.paste(repaired_crop, (box[0], box[1]))
    return result, wood_ratio, mask_ratio


def enhance(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image).convert("RGB")
    image = ImageEnhance.Contrast(image).enhance(1.035)
    image = ImageEnhance.Color(image).enhance(1.025)
    image = ImageEnhance.Sharpness(image).enhance(1.06)
    return image


def fit_cover(image: Image.Image, size: tuple[int, int], focal_y: float = 0.54, focal_x: float = 0.5) -> Image.Image:
    w, h = size
    src_ratio = image.width / image.height
    dst_ratio = w / h
    if src_ratio > dst_ratio:
        crop_h = image.height
        crop_w = int(crop_h * dst_ratio)
        left = int((image.width - crop_w) * max(0, min(1, focal_x)))
        left = min(max(0, left), image.width - crop_w)
        crop = image.crop((left, 0, left + crop_w, crop_h))
    else:
        crop_w = image.width
        crop_h = int(crop_w / dst_ratio)
        top = int((image.height - crop_h) * max(0, min(1, focal_y)))
        top = min(max(0, top), image.height - crop_h)
        crop = image.crop((0, top, crop_w, top + crop_h))
    return crop.resize(size, Image.Resampling.LANCZOS)


def fit_contain(image: Image.Image, size: tuple[int, int], bg=WHITE) -> Image.Image:
    contained = ImageOps.contain(image, size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, bg)
    canvas.paste(contained, ((size[0] - contained.width) // 2, (size[1] - contained.height) // 2))
    return canvas


def rounded_paste(canvas: Image.Image, image: Image.Image, box: tuple[int, int, int, int], radius=48, mode="cover", focal_y=0.54) -> None:
    x1, y1, x2, y2 = box
    size = (x2 - x1, y2 - y1)
    tile = fit_cover(image, size, focal_y=focal_y) if mode == "cover" else fit_contain(image, size)
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((x1 + 18, y1 + 24, x2 + 18, y2 + 24), radius=radius, fill=(40, 28, 22, 55))
    shadow = shadow.filter(ImageFilter.GaussianBlur(22))
    canvas.alpha_composite(shadow)
    rgba = tile.convert("RGBA"); rgba.putalpha(mask)
    canvas.alpha_composite(rgba, (x1, y1))


def wrap(draw: ImageDraw.ImageDraw, text: str, f, max_width: int) -> str:
    lines: list[str] = []
    cur = ""
    for word in text.split():
        cand = (cur + " " + word).strip()
        if not cur or draw.textlength(cand, font=f) <= max_width:
            cur = cand
        else:
            lines.append(cur); cur = word
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def clean_wood_name(wood: str) -> str:
    text = wood.strip()
    text = re.sub(r"\s+wood\s*$", " Wood", text, flags=re.I)
    if not re.search(r"\bwood\b", text, flags=re.I):
        text += " Wood"
    return text


def dims_summary(dims: Sequence[tuple[str, str, str]], max_items=4) -> str:
    if not dims:
        return "Measurements: please confirm current dimensions"
    parts = []
    for label, cm, inch in dims[:max_items]:
        parts.append(f"{label}: {cm} cm" + (f" / {inch} in" if inch else ""))
    return " • ".join(parts)


def dimension_lines(dims: Sequence[tuple[str, str, str]]) -> list[str]:
    if not dims:
        return ["Please confirm current dimensions before ordering."]
    lines = []
    for label, cm, inch in dims:
        line = f"{label}: {cm} cm"
        if inch:
            line += f" / {inch} in"
        lines.append(line)
    return lines


def wood_focus_crop(image: Image.Image, kind: str = "grain") -> Image.Image:
    # Deliberately avoid the sanitized top band. Detail crops use the lower 70%.
    w, h = image.size
    if kind == "grain":
        box = (int(w * 0.08), int(h * 0.30), int(w * 0.92), int(h * 0.66))
    elif kind == "base":
        box = (int(w * 0.08), int(h * 0.48), int(w * 0.92), int(h * 0.96))
    else:
        box = (int(w * 0.12), int(h * 0.34), int(w * 0.88), int(h * 0.82))
    crop = image.crop(box)
    return crop if crop.width > 10 and crop.height > 10 else image


def base_canvas(color=CREAM) -> Image.Image:
    return Image.new("RGBA", (CANVAS, CANVAS), color)


def badge(draw: ImageDraw.ImageDraw, text: str, xy=(110, 110), fill=OLIVE, fg=WHITE) -> None:
    f = font(FONT_SANS_BOLD, 34)
    bbox = draw.textbbox((0, 0), text, font=f)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x, y = xy
    draw.rounded_rectangle((x, y, x + w + 64, y + h + 36), radius=38, fill=fill)
    draw.text((x + 32, y + 18 - bbox[1]), text, font=f, fill=fg)


def footer(draw: ImageDraw.ImageDraw, product: Product, light=False) -> None:
    fg = WHITE if light else MUTED
    draw.line((105, 1872, 1895, 1872), fill=fg, width=2)
    draw.text((105, 1906), product.folder.upper(), font=font(FONT_SANS_BOLD, 28), fill=fg)
    draw.text((1895, 1906), f"MODEL {product.model}", font=font(FONT_SANS, 28), fill=fg, anchor="ra")


def save_slide(canvas: Image.Image, path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    rgb = canvas.convert("RGB")
    rgb.save(path, "JPEG", quality=93, subsampling=0, optimize=True, dpi=(300, 300))
    with Image.open(path) as check:
        check.verify()
    with Image.open(path) as check:
        if check.size != (CANVAS, CANVAS) or check.format != "JPEG":
            raise Blocked(f"Generated image invalid: {path.name} {check.size} {check.format}")
    return {"file": path.name, "bytes": path.stat().st_size, "sha256": sha256(path), "width": CANVAS, "height": CANVAS}


def build_gallery(product: Product, sources: list[Image.Image], draft_dir: Path) -> list[dict]:
    if not sources:
        raise Blocked("No clean source images available")
    s = [enhance(im) for im in sources]
    def src(index: int) -> Image.Image:
        return s[index % len(s)]
    records = []
    wood = clean_wood_name(product.wood)
    title = product.product_type.upper()

    # 01 HERO
    c = base_canvas(); d = ImageDraw.Draw(c)
    d.ellipse((1190, -300, 2300, 810), fill=CREAM_2)
    badge(d, "NATURAL WOOD FURNITURE", fill=OLIVE)
    d.text((105, 285), title, font=font(FONT_SERIF_BOLD, 96), fill=INK)
    d.text((110, 405), wood, font=font(FONT_SANS_BOLD, 46), fill=BROWN)
    d.text((110, 485), f"Model {product.model}", font=font(FONT_SANS, 38), fill=MUTED)
    rounded_paste(c, src(0), (640, 200, 1900, 1660), radius=72, mode="contain")
    d = ImageDraw.Draw(c)
    footer(d, product)
    records.append(save_slide(c, draft_dir / OUTPUT_NAMES[0]))

    # 02 CLEAN VIEW
    c = base_canvas(PAPER); d = ImageDraw.Draw(c)
    rounded_paste(c, src(1), (120, 110, 1880, 1650), radius=70, mode="contain")
    d = ImageDraw.Draw(c)
    d.text((125, 1705), "ORGANIC FORM, NATURAL CHARACTER", font=font(FONT_SERIF_BOLD, 60), fill=INK)
    d.text((128, 1790), "A clear source-derived product view", font=font(FONT_SANS, 34), fill=MUTED)
    footer(d, product)
    records.append(save_slide(c, draft_dir / OUTPUT_NAMES[1]))

    # 03 ALTERNATE VIEW
    c = base_canvas(CREAM_2); d = ImageDraw.Draw(c)
    d.text((105, 100), "ANOTHER VIEW", font=font(FONT_SANS_BOLD, 34), fill=CLAY)
    d.text((105, 165), "Shape • Base • Proportions", font=font(FONT_SERIF_BOLD, 75), fill=INK)
    rounded_paste(c, src(2), (100, 315, 1900, 1745), radius=64, mode="contain")
    d = ImageDraw.Draw(c); footer(d, product)
    records.append(save_slide(c, draft_dir / OUTPUT_NAMES[2]))

    # 04 GRAIN / EDGE DETAIL
    c = base_canvas(INK); d = ImageDraw.Draw(c)
    d.text((105, 95), "NATURAL WOOD GRAIN", font=font(FONT_SERIF_BOLD, 76), fill=WHITE)
    d.text((108, 195), "Rich variation • Organic edge • One-of-a-kind character", font=font(FONT_SANS, 34), fill="#DCCFC2")
    rounded_paste(c, wood_focus_crop(src(0), "grain"), (95, 320, 1905, 1765), radius=62, mode="cover", focal_y=0.5)
    d = ImageDraw.Draw(c); footer(d, product, light=True)
    records.append(save_slide(c, draft_dir / OUTPUT_NAMES[3]))

    # 05 DIMENSIONS
    c = base_canvas(); d = ImageDraw.Draw(c)
    badge(d, "APPROXIMATE DIMENSIONS", fill=BROWN)
    rounded_paste(c, src(0), (90, 300, 1000, 1730), radius=58, mode="contain")
    d = ImageDraw.Draw(c)
    d.text((1110, 320), "DIMENSIONS", font=font(FONT_SERIF_BOLD, 82), fill=INK)
    y = 470
    for line in dimension_lines(product.dimensions):
        d.text((1110, y), line, font=font(FONT_SANS_BOLD, 35), fill=BROWN)
        y += 115
    d.rounded_rectangle((1080, 1215, 1900, 1540), radius=52, fill=WHITE, outline=CREAM_2, width=4)
    note = wrap(d, "Approximate dimensions. Natural form may vary slightly. Please confirm exact measurements before ordering.", font(FONT_SANS, 34), 720)
    d.multiline_text((1130, 1270), note, font=font(FONT_SANS, 34), fill=MUTED, spacing=12)
    footer(d, product)
    records.append(save_slide(c, draft_dir / OUTPUT_NAMES[4]))

    # 06 MATERIAL
    c = base_canvas(CREAM_2); d = ImageDraw.Draw(c)
    d.text((105, 100), f"NATURAL {wood.upper()}", font=font(FONT_SERIF_BOLD, 72), fill=INK)
    d.text((110, 205), "Natural material, individual character", font=font(FONT_SANS, 35), fill=MUTED)
    rounded_paste(c, wood_focus_crop(src(1), "grain"), (1010, 90, 1910, 1780), radius=65, mode="cover")
    d = ImageDraw.Draw(c)
    bullets = ["Distinctive grain", "Organic color variation", "Natural edge character", "Each piece may vary"]
    y = 475
    for item in bullets:
        d.ellipse((120, y + 5, 170, y + 55), fill=OLIVE)
        d.text((145, y + 30), "•", font=font(FONT_SANS_BOLD, 30), fill=WHITE, anchor="mm")
        d.text((205, y), item, font=font(FONT_SANS_BOLD, 41), fill=INK)
        y += 165
    footer(d, product)
    records.append(save_slide(c, draft_dir / OUTPUT_NAMES[5]))

    # 07 CONSTRUCTION DETAILS (source-derived only)
    c = base_canvas(); d = ImageDraw.Draw(c)
    d.text((100, 90), "SOURCE-DERIVED DETAILS", font=font(FONT_SERIF_BOLD, 72), fill=INK)
    panels = [
        (wood_focus_crop(src(0), "grain"), "WOOD GRAIN"),
        (wood_focus_crop(src(1), "base"), "BASE FORM"),
        (wood_focus_crop(src(2), "edge"), "NATURAL EDGE"),
    ]
    x = 90
    for image, label in panels:
        rounded_paste(c, image, (x, 300, x + 570, 1530), radius=50, mode="cover")
        d = ImageDraw.Draw(c)
        d.rounded_rectangle((x + 35, 1415, x + 535, 1510), radius=45, fill=(255, 253, 248, 235))
        d.text((x + 285, 1445), label, font=font(FONT_SANS_BOLD, 28), fill=INK, anchor="ma")
        x += 625
    d = ImageDraw.Draw(c)
    d.text((100, 1640), "Visible details are taken from the source product photography.", font=font(FONT_SANS, 34), fill=MUTED)
    footer(d, product)
    records.append(save_slide(c, draft_dir / OUTPUT_NAMES[6]))

    # 08 LIFESTYLE / USE IDEAS without fabricating a new room
    c = base_canvas(INK); d = ImageDraw.Draw(c)
    rounded_paste(c, src(3), (760, 85, 1915, 1810), radius=64, mode="cover", focal_y=0.58)
    d = ImageDraw.Draw(c)
    d.text((105, 115), "STYLING IDEAS", font=font(FONT_SERIF_BOLD, 78), fill=WHITE)
    low = product.product_type.lower()
    if "pedestal" in low:
        ideas = ["Sculpture display", "Ceramic or vase stand", "Entryway accent", "Gallery-style decor"]
    elif "table base" in low:
        ideas = ["Custom table project", "Interior design project", "Statement wood base", "Confirm compatible tabletop"]
    elif "table" in low or "furniture set" in low:
        ideas = ["Living room", "Tea or lounge area", "Reading space", "Statement furniture"]
    else:
        ideas = ["Beside a sofa", "Reading corner", "Entryway accent", "Plant or decor stand"]
    y = 430
    for item in ideas:
        d.rounded_rectangle((110, y, 650, y + 108), radius=50, fill="#3B2B22")
        d.text((155, y + 30), item, font=font(FONT_SANS_BOLD, 32), fill="#EEE3D7")
        y += 155
    d.text((110, 1160), "Use suggestions only.\nConfirm dimensions for your space.", font=font(FONT_SANS, 36), fill="#CFC2B7", spacing=12)
    footer(d, product, light=True)
    records.append(save_slide(c, draft_dir / OUTPUT_NAMES[7]))

    # 09 VARIATION + CARE
    c = base_canvas(); d = ImageDraw.Draw(c)
    d.text((100, 100), "NATURAL VARIATION & CARE", font=font(FONT_SERIF_BOLD, 72), fill=INK)
    rounded_paste(c, src(4), (90, 300, 900, 1710), radius=58, mode="contain")
    d = ImageDraw.Draw(c)
    d.text((1020, 360), "NATURAL VARIATION", font=font(FONT_SANS_BOLD, 36), fill=CLAY)
    var = wrap(d, "Wood grain, color, edge shape and natural marks may vary from piece to piece.", font(FONT_SANS, 39), 790)
    d.multiline_text((1020, 440), var, font=font(FONT_SANS, 39), fill=INK, spacing=12)
    d.line((1020, 790, 1880, 790), fill=CREAM_2, width=4)
    d.text((1020, 880), "CARE", font=font(FONT_SANS_BOLD, 36), fill=OLIVE)
    care = wrap(d, "Wipe with a soft dry or slightly damp cloth. Avoid prolonged moisture and harsh cleaning products.", font(FONT_SANS, 39), 790)
    d.multiline_text((1020, 960), care, font=font(FONT_SANS, 39), fill=INK, spacing=12)
    d.rounded_rectangle((1010, 1330, 1885, 1535), radius=48, fill=WHITE, outline=CREAM_2, width=4)
    d.text((1060, 1390), "Please ask about intended indoor/outdoor use.", font=font(FONT_SANS_BOLD, 30), fill=MUTED)
    footer(d, product)
    records.append(save_slide(c, draft_dir / OUTPUT_NAMES[8]))

    # 10 BUYER INFORMATION
    c = base_canvas(CREAM_2); d = ImageDraw.Draw(c)
    d.text((100, 100), "BUYER INFORMATION", font=font(FONT_SERIF_BOLD, 76), fill=INK)
    rounded_paste(c, src(0), (105, 310, 880, 1660), radius=56, mode="contain")
    d = ImageDraw.Draw(c)
    facts = [
        f"Model: {product.model}",
        f"Wood: {wood}",
        f"Quantity included: {product.quantity}",
        "Physical furniture item",
    ]
    y = 355
    for fact in facts:
        d.ellipse((1015, y + 8, 1065, y + 58), fill=BROWN)
        d.text((1040, y + 32), "✓", font=font(FONT_SANS_BOLD, 28), fill=WHITE, anchor="mm")
        d.text((1105, y), fact, font=font(FONT_SANS_BOLD, 35), fill=INK)
        y += 130
    d.text((1015, y + 10), "MEASUREMENTS", font=font(FONT_SANS_BOLD, 30), fill=CLAY)
    y += 80
    for line in dimension_lines(product.dimensions)[:4]:
        d.text((1015, y), line, font=font(FONT_SANS, 31), fill=MUTED)
        y += 70
    d.rounded_rectangle((985, 1250, 1900, 1635), radius=55, fill=INK)
    note = wrap(d, "Please confirm current availability, exact dimensions, finish and shipping details before ordering. Contact us for current shipping options.", font(FONT_SANS, 32), 800)
    d.multiline_text((1040, 1310), note, font=font(FONT_SANS, 32), fill=WHITE, spacing=12)
    footer(d, product)
    records.append(save_slide(c, draft_dir / OUTPUT_NAMES[9]))

    hashes = [r["sha256"] for r in records]
    if len(set(hashes)) != 10:
        raise Blocked("Generated gallery contains duplicate output bytes")
    return records


def source_images_for(product: Product, cache_dir: Path, commit: bool) -> tuple[list[Image.Image], list[dict]]:
    recs = image_records(product.source)
    if not recs:
        raise Blocked("source_catalog has no source image URLs")
    images = []
    provenance = []
    persistent_root = SHOP_ROOT / product.folder / "source_catalog" / "original_images"
    root = persistent_root if commit else cache_dir / "original_images"
    for idx, rec in enumerate(recs, 1):
        suffix = Path(urllib.parse.urlparse(rec["url"]).path).suffix.lower()
        if suffix not in IMG_EXTS:
            suffix = ".jpg"
        dest = root / f"source_{idx:02d}{suffix}"
        try:
            download(rec["url"], dest, rec.get("sha256") or "")
            with Image.open(dest) as im:
                original = im.convert("RGB").copy()
            clean, wood_ratio, logo_mask_ratio = sanitize_source(original)
        except Blocked as exc:
            provenance.append({"index": idx, "url": rec["url"], "status": "rejected", "reason": str(exc)})
            continue
        clean_root = cache_dir / "clean_sources"
        clean_root.mkdir(parents=True, exist_ok=True)
        clean_path = clean_root / f"clean_{idx:02d}.jpg"
        clean.save(clean_path, "JPEG", quality=95, subsampling=0)
        images.append(clean)
        provenance.append({
            "index": idx,
            "url": rec["url"],
            "status": "usable",
            "file": str(dest.relative_to(ROOT)) if dest.is_relative_to(ROOT) else str(dest),
            "sha256": sha256(dest),
            "logo_zone_wood_ratio": wood_ratio,
            "logo_mask_ratio": logo_mask_ratio,
        })
    if not images:
        reasons = "; ".join(p.get("reason", "") for p in provenance if p.get("reason"))
        raise Blocked(f"All source images rejected by logo/product safety guard: {reasons[:600]}")
    return images, provenance


def contact_sheet(draft_dir: Path, output: Path) -> None:
    files = [draft_dir / name for name in OUTPUT_NAMES]
    tiles = []
    for i, p in enumerate(files, 1):
        with Image.open(p) as im:
            tile = ImageOps.fit(im.convert("RGB"), (420, 420), Image.Resampling.LANCZOS)
        panel = Image.new("RGB", (440, 470), WHITE)
        panel.paste(tile, (10, 10))
        ImageDraw.Draw(panel).text((14, 438), f"{i:02d} {p.name}", font=font(FONT_SANS_BOLD, 20), fill=INK)
        tiles.append(panel)
    sheet = Image.new("RGB", (2200, 940), "#D8D0C6")
    for i, panel in enumerate(tiles):
        sheet.paste(panel, ((i % 5) * 440, (i // 5) * 470))
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, "JPEG", quality=90, optimize=True)


def workbook_row_values(row: int) -> list[object]:
    wb = openpyxl.load_workbook(WORKBOOK, read_only=True, data_only=False)
    ws = wb["Listings"]
    values = [ws.cell(row, c).value for c in range(1, 19)]
    wb.close()
    return values


def update_workbook_images(row: int) -> None:
    wb = openpyxl.load_workbook(WORKBOOK)
    ws = wb["Listings"]
    ws.cell(row=row, column=7, value="; ".join(OUTPUT_NAMES))
    tmp = WORKBOOK.with_name(f".{WORKBOOK.name}.{os.getpid()}.gallery.tmp.xlsx")
    wb.save(tmp); wb.close()
    verify = openpyxl.load_workbook(tmp, read_only=True, data_only=True)
    value = verify["Listings"].cell(row, 7).value
    verify.close()
    if value != "; ".join(OUTPUT_NAMES):
        tmp.unlink(missing_ok=True)
        raise Blocked("Workbook image inventory verification failed")
    os.replace(tmp, WORKBOOK)


def install_gallery(product: Product, draft_dir: Path, records: list[dict], provenance: list[dict], backup_root: Path) -> tuple[Path, Path]:
    root = SHOP_ROOT / product.folder
    image_dir = root / "images"
    backup = backup_root / product.folder
    backup.mkdir(parents=True, exist_ok=False)
    if image_dir.is_dir():
        shutil.copytree(image_dir, backup / "images")
    source_json = root / "source_catalog" / "source.json"
    if source_json.is_file():
        shutil.copy2(source_json, backup / "source.json")
    listing_manifest = root / "source_catalog" / "listing-gallery.json"
    if listing_manifest.is_file():
        shutil.copy2(listing_manifest, backup / "listing-gallery.before.json")
    row_before = workbook_row_values(product.row)
    json_write(backup / "workbook-row-before.json", {"row": product.row, "values": row_before})

    stage = root / f".images-stage-{os.getpid()}-{int(time.time())}"
    old = root / f".images-old-{os.getpid()}-{int(time.time())}"
    stage.mkdir()
    for name in OUTPUT_NAMES:
        shutil.copy2(draft_dir / name, stage / name)
    try:
        if image_dir.exists():
            image_dir.rename(old)
        stage.rename(image_dir)
        update_workbook_images(product.row)
    except Exception:
        if image_dir.exists() and old.exists():
            shutil.rmtree(image_dir, ignore_errors=True)
        if old.exists() and not image_dir.exists():
            old.rename(image_dir)
        raise
    else:
        if old.exists():
            shutil.rmtree(old)

    manifest = {
        "schema": 2,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "shop_id": SHOP_ID,
        "folder": product.folder,
        "row": product.row,
        "title": product.title,
        "product_type": product.product_type,
        "wood": product.wood,
        "model": product.model,
        "dimensions": [{"label": a, "cm": b, "in": c} for a, b, c in product.dimensions],
        "quantity": product.quantity,
        "before_count": product.before_count,
        "after_count": 10,
        "source_provenance": provenance,
        "gallery": records,
        "backup": str(backup.relative_to(ROOT)),
        "guards": {
            "english_only_layout_copy": True,
            "no_shop_watermark_added": True,
            "source_logo_zone_sanitized": True,
            "source_geometry_not_synthesized": True,
            "etsy_live_mutation": False,
        },
    }
    manifest_path = root / "source_catalog" / "listing-gallery.json"
    json_write(manifest_path, manifest)
    return backup, manifest_path


def local_validate(product: Product) -> None:
    image_dir = SHOP_ROOT / product.folder / "images"
    files = sorted(p for p in image_dir.iterdir() if p.is_file() and p.suffix.lower() in IMG_EXTS)
    if [p.name for p in files] != OUTPUT_NAMES:
        raise Blocked(f"Installed image inventory invalid: {[p.name for p in files]}")
    hashes = set()
    for path in files:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            if im.size != (CANVAS, CANVAS) or im.format != "JPEG":
                raise Blocked(f"Installed image invalid: {path.name} {im.size} {im.format}")
        digest = sha256(path)
        if digest in hashes:
            raise Blocked(f"Installed duplicate image bytes: {path.name}")
        hashes.add(digest)
    wb = openpyxl.load_workbook(WORKBOOK, read_only=True, data_only=True)
    value = wb["Listings"].cell(product.row, 7).value
    wb.close()
    if value != "; ".join(OUTPUT_NAMES):
        raise Blocked("Workbook image inventory is not 10 canonical names")


def dashboard_validate(product: Product) -> dict:
    with urllib.request.urlopen(DASHBOARD + "/api/products", timeout=30) as response:
        payload = json.load(response)
    if payload.get("active_shop", {}).get("id") != SHOP_ID:
        raise Blocked(f"Dashboard active shop mismatch: {payload.get('active_shop')}")
    row = next((p for p in payload.get("products", []) if p.get("folder") == product.folder), None)
    if not row:
        raise Blocked("Product missing from dashboard API")
    if int(row.get("image_count") or 0) != 10 or len(row.get("all_images") or []) != 10:
        raise Blocked(f"Dashboard image_count mismatch: {row.get('image_count')} / {len(row.get('all_images') or [])}")
    checks = []
    for index, url in enumerate(row.get("all_images") or [], 1):
        with urllib.request.urlopen(DASHBOARD + url, timeout=30) as response:
            body = response.read()
        local = SHOP_ROOT / product.folder / "images" / f"etsy_{index:02d}.jpg"
        remote_hash = hashlib.sha256(body).hexdigest()
        local_hash = sha256(local)
        if remote_hash != local_hash:
            raise Blocked(f"Dashboard checksum mismatch for etsy_{index:02d}.jpg")
        checks.append({"image": index, "sha256": local_hash})
    return {"image_count": 10, "checksums": checks}


def operation_queue() -> list[dict]:
    try:
        with urllib.request.urlopen(DASHBOARD + "/api/operation-queue", timeout=10) as response:
            payload = json.load(response)
        return payload.get("commands") or []
    except Exception:
        return []


def process_one(product: Product, *, run_root: Path, backup_root: Path, commit: bool, dashboard: bool) -> dict:
    product_run = run_root / product.folder
    draft_dir = product_run / "drafts"
    product_run.mkdir(parents=True, exist_ok=True)
    result = {
        "product": product.folder,
        "model": product.model,
        "product_type": product.product_type,
        "wood": product.wood,
        "before": product.before_count,
        "after": None,
        "row": product.row,
        "status": "BLOCKED",
        "notes": "",
        "backup": None,
        "manifest": None,
        "contact_sheet": None,
    }
    try:
        sources, provenance = source_images_for(product, product_run, commit)
        records = build_gallery(product, sources, draft_dir)
        sheet = product_run / "contact-sheet.jpg"
        contact_sheet(draft_dir, sheet)
        result["contact_sheet"] = str(sheet.relative_to(ROOT))
        if commit:
            backup, manifest = install_gallery(product, draft_dir, records, provenance, backup_root)
            local_validate(product)
            dash = dashboard_validate(product) if dashboard else {"skipped": True}
            result.update({
                "after": 10,
                "status": "PASS",
                "backup": str(backup.relative_to(ROOT)),
                "manifest": str(manifest.relative_to(ROOT)),
                "dashboard": dash,
                "notes": f"{len([p for p in provenance if p.get('status') == 'usable'])} clean source image(s) used",
            })
        else:
            result.update({"after": 10, "status": "DRAFT_PASS", "notes": f"{len(sources)} clean source image(s) used"})
        return result
    except Exception as exc:
        result["notes"] = f"{type(exc).__name__}: {exc}"
        return result


def select_products(products: list[Product], args) -> list[Product]:
    if args.product:
        requested = set(args.product)
        selected = [p for p in products if p.folder in requested]
        missing = requested - {p.folder for p in selected}
        if missing:
            raise SystemExit(f"Requested products are not current <10 furniture targets: {sorted(missing)}")
        return selected
    selected = products
    if args.max_before is not None:
        selected = [p for p in selected if p.before_count <= args.max_before]
    if args.limit:
        selected = selected[:args.limit]
    return selected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-before", type=int)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--dashboard", action="store_true")
    parser.add_argument("--run-id", default=now_stamp())
    args = parser.parse_args()

    products = load_catalog()
    selected = select_products(products, args)
    run_root = WORKING_ROOT / args.run_id
    backup_root = BACKUP_ROOT / args.run_id
    run_root.mkdir(parents=True, exist_ok=True)
    if args.commit:
        backup_root.mkdir(parents=True, exist_ok=True)
        workbook_backup = backup_root / "Etsy_SEO_Generator.before.xlsx"
        if not workbook_backup.exists():
            shutil.copy2(WORKBOOK, workbook_backup)

    pre_queue = operation_queue()
    selected_folders = {p.folder for p in selected}
    conflicts = [
        cmd for cmd in pre_queue
        if str(cmd.get("shop_id") or "") == SHOP_ID
        and Path(str(cmd.get("target") or "")).name in selected_folders
        and str(cmd.get("status") or "").lower() in {"queued", "running"}
    ]
    if conflicts:
        print(json.dumps({
            "ok": False,
            "error": "pre-existing operation queue conflicts with selected product(s)",
            "conflicts": conflicts,
        }, ensure_ascii=False, indent=2))
        return 2
    pre_command_ids = {str(cmd.get("command_id") or "") for cmd in pre_queue}

    print(f"targets={len(selected)} commit={args.commit} run_id={args.run_id} preexisting_queue={len(pre_queue)}", flush=True)
    results = []
    for idx, product in enumerate(selected, 1):
        print(f"[{idx}/{len(selected)}] {product.folder} before={product.before_count} {product.title[:90]}", flush=True)
        result = process_one(product, run_root=run_root, backup_root=backup_root, commit=args.commit, dashboard=args.dashboard)
        results.append(result)
        print(f"  -> {result['status']} {result['notes']}", flush=True)
        json_write(run_root / "report.json", {
            "run_id": args.run_id,
            "commit": args.commit,
            "targets": len(selected),
            "results": results,
        })

    post_queue = operation_queue()
    post_command_ids = {str(cmd.get("command_id") or "") for cmd in post_queue}
    new_command_ids = sorted(cid for cid in post_command_ids - pre_command_ids if cid)
    payload = {
        "run_id": args.run_id,
        "commit": args.commit,
        "targets": len(selected),
        "passed": sum(r["status"] in {"PASS", "DRAFT_PASS"} for r in results),
        "blocked": sum(r["status"] == "BLOCKED" for r in results),
        "preexisting_operation_count": len(pre_queue),
        "post_operation_count": len(post_queue),
        "new_operation_command_ids": new_command_ids,
        "results": results,
    }
    json_write(run_root / "report.json", payload)
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    json_write(REPORT_ROOT / f"pivotandpage-gallery-{args.run_id}.json", payload)
    print(json.dumps({k: payload[k] for k in ("run_id", "commit", "targets", "passed", "blocked", "preexisting_operation_count", "post_operation_count", "new_operation_command_ids")}, ensure_ascii=False, indent=2))
    return 0 if not new_command_ids else 3


if __name__ == "__main__":
    raise SystemExit(main())
