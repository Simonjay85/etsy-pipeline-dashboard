"""Shared asset workflow helpers for the Etsy multi-shop pipeline."""

import shutil
from pathlib import Path


IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def resolve_shop_source_dir(
    output_root: Path,
    shop_id: str,
    configured_shop_ids=(),
) -> Path:
    """Use output/<shop> for the new layout, with legacy-root compatibility."""
    shop_dir = output_root / shop_id
    if shop_dir.exists():
        return shop_dir

    # Once any shop-specific folder exists, do not accidentally scan another
    # shop's assets when the selected shop has not received files yet.
    if any((output_root / configured).is_dir() for configured in configured_shop_ids):
        return shop_dir
    return output_root


def get_watermark_text(shop_id: str, shop_config: dict) -> str:
    """Return the configured public brand name used on listing images."""
    return str(
        shop_config.get("watermark")
        or shop_config.get("name")
        or shop_id
    ).strip()


def _font(size: int):
    from PIL import ImageFont

    for font_path in (
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Georgia.ttf",
        "/Library/Fonts/Arial.ttf",
    ):
        if Path(font_path).exists():
            try:
                return ImageFont.truetype(font_path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def copy_image_with_watermark(src: Path, dst: Path, watermark: str) -> None:
    """Copy a listing image and apply the shop watermark without touching src."""
    if not watermark:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return

    from PIL import Image, ImageDraw

    with Image.open(src) as opened:
        image = opened.convert("RGBA")

    width, height = image.size
    short_edge = max(1, min(width, height))
    watermark_font = _font(max(24, int(short_edge * 0.08)))
    logo_font = _font(max(16, int(short_edge * 0.022)))

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Keep the established diagonal, low-opacity style from Image Factory.
    bbox = draw.textbbox((0, 0), watermark, font=watermark_font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    pad = min(max(int(text_width * 1.5), 256), max(width, height))
    text_layer = Image.new("RGBA", (pad, pad), (0, 0, 0, 0))
    text_draw = ImageDraw.Draw(text_layer)
    text_draw.text(
        ((pad - text_width) // 2, (pad - text_height) // 2),
        watermark,
        fill=(123, 92, 62, 90),
        font=watermark_font,
    )
    rotated = text_layer.rotate(30, resample=Image.Resampling.BILINEAR, expand=False)
    overlay.alpha_composite(rotated, ((width - pad) // 2, (height - pad) // 2))

    logo_bbox = draw.textbbox((0, 0), watermark, font=logo_font)
    logo_width = logo_bbox[2] - logo_bbox[0]
    logo_height = logo_bbox[3] - logo_bbox[1]
    margin_x = int(width * 0.04)
    margin_y = int(height * 0.04)
    logo_x = max(margin_x, width - logo_width - margin_x)
    logo_y = margin_y
    bg_padding_x = 30
    bg_padding_y = 16
    draw.rounded_rectangle(
        [
            logo_x - bg_padding_x,
            logo_y - bg_padding_y,
            min(width, logo_x + logo_width + bg_padding_x),
            min(height, logo_y + logo_height + bg_padding_y),
        ],
        radius=15,
        fill=(255, 255, 255, 170),
    )
    draw.text((logo_x, logo_y), watermark, fill=(123, 92, 62, 230), font=logo_font)

    final_image = Image.alpha_composite(image, overlay)
    dst.parent.mkdir(parents=True, exist_ok=True)
    suffix = dst.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        final_image.convert("RGB").save(dst, format="JPEG", quality=95)
    elif suffix == ".webp":
        final_image.convert("RGB").save(dst, format="WEBP", quality=95)
    else:
        final_image.save(dst, format="PNG")
