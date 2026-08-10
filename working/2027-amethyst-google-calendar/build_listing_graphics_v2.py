#!/usr/bin/env python3
"""Build source-led Etsy listing graphics for the Amethyst planner.

The previous gallery used generated lifestyle scenes as the visual subject.
This builder deliberately does the opposite: the inspected PDF pages are the
hero evidence, while Image Generation is used only for a quiet brand
background. All buyer-facing copy and page placement are deterministic so
the listing remains readable and truthful at thumbnail size.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


CANVAS = (2000, 2000)
SOURCE_DIR = Path(
    "/Users/aaronnguyen/Developer/Etsy/working/2027-amethyst-google-calendar/"
    "source-review/rendered-final-144"
)


TEMPLY = {
    "ink": "#272724",
    "muted": "#6F7168",
    "accent": "#7C8470",
    "accent_light": "#D8DED1",
    "mauve": "#C5A9C9",
    "paper": "#FBF8F2",
    "line": "#D6D1C7",
}

DAISY = {
    "ink": "#24202E",
    "muted": "#5C5269",
    "accent": "#7B55C7",
    "teal": "#21AEB7",
    "coral": "#F56B72",
    "yellow": "#F6C744",
    "paper": "#FFF9FF",
    "line": "#D7C8ED",
}


def rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
        if bold
        else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica Bold.ttf"
        if bold
        else "/System/Library/Fonts/Supplemental/Helvetica.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def load_page(page_number: str) -> Image.Image:
    path = SOURCE_DIR / f"page-{page_number}.png"
    if not path.is_file():
        raise FileNotFoundError(path)
    with Image.open(path) as opened:
        return ImageOps.exif_transpose(opened).convert("RGB")


def base_canvas(background: Path, palette: dict[str, str], shop: str) -> Image.Image:
    with Image.open(background) as opened:
        image = ImageOps.fit(opened.convert("RGB"), CANVAS, method=Image.Resampling.LANCZOS)
    # Keep the generated texture subordinate to exact PDF evidence.
    veil = Image.new("RGBA", CANVAS, (*rgb(palette["paper"]), 142 if shop == "temply" else 78))
    image = Image.alpha_composite(image.convert("RGBA"), veil)
    return image.convert("RGB")


def draw_text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, palette: dict[str, str], size: int, bold: bool = False, fill: str | None = None) -> None:
    draw.text(xy, text, font=font(size, bold), fill=rgb(fill or palette["ink"]))


def header(canvas: Image.Image, palette: dict[str, str], shop: str, kicker: str, title: str, subtitle: str, number: str) -> None:
    draw = ImageDraw.Draw(canvas)
    if shop == "temply":
        draw.rectangle((120, 124, 140, 360), fill=rgb(palette["accent"]))
        draw_text(draw, (175, 126), kicker.upper(), palette, 30, True, palette["accent"])
        draw_text(draw, (175, 178), title, palette, 92, True)
        draw_text(draw, (178, 292), subtitle, palette, 34, False, palette["muted"])
    else:
        draw.rounded_rectangle((120, 120, 460, 178), radius=29, fill=rgb(palette["accent"]))
        draw_text(draw, (151, 131), kicker.upper(), palette, 27, True, "#FFFFFF")
        draw_text(draw, (120, 214), title, palette, 94, True)
        draw_text(draw, (124, 328), subtitle, palette, 34, False, palette["muted"])
    draw_text(draw, (1790, 128), number, palette, 30, True, palette["muted"])


def section_rule(canvas: Image.Image, y: int, palette: dict[str, str], shop: str) -> None:
    draw = ImageDraw.Draw(canvas)
    if shop == "temply":
        draw.line((120, y, 1880, y), fill=rgb(palette["line"]), width=3)
        draw.ellipse((120, y - 8, 136, y + 8), fill=rgb(palette["accent"]))
    else:
        draw.rounded_rectangle((120, y - 7, 410, y + 7), radius=7, fill=rgb(palette["accent"]))
        draw.line((430, y, 1880, y), fill=rgb(palette["line"]), width=3)


def card_shadow(canvas: Image.Image, box: tuple[int, int, int, int], radius: int = 34) -> None:
    shadow = Image.new("RGBA", CANVAS, (255, 255, 255, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    x1, y1, x2, y2 = box
    shadow_draw.rounded_rectangle((x1 + 18, y1 + 20, x2 + 18, y2 + 20), radius=radius, fill=(34, 30, 42, 52))
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    canvas.paste(shadow, (0, 0), shadow)


def place_page(canvas: Image.Image, page_number: str, box: tuple[int, int, int, int], palette: dict[str, str], label: str | None = None, label_color: str | None = None) -> None:
    x1, y1, x2, y2 = box
    card_shadow(canvas, box)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(box, radius=34, fill=rgb(palette["paper"]), outline=rgb(palette["line"]), width=4)
    page = load_page(page_number)
    inner = (x1 + 26, y1 + 26, x2 - 26, y2 - 26)
    fitted = ImageOps.contain(page, (inner[2] - inner[0], inner[3] - inner[1]), method=Image.Resampling.LANCZOS)
    px = inner[0] + ((inner[2] - inner[0]) - fitted.width) // 2
    py = inner[1] + ((inner[3] - inner[1]) - fitted.height) // 2
    canvas.paste(fitted, (px, py))
    if label:
        label_font = font(27, True)
        width = ImageDraw.Draw(canvas).textbbox((0, 0), label.upper(), font=label_font)[2] + 46
        ly = max(26, y1 - 58)
        draw.rounded_rectangle((x1, ly, x1 + width, ly + 44), radius=22, fill=rgb(label_color or palette["accent"]))
        draw.text((x1 + 23, ly + 6), label.upper(), font=label_font, fill=(255, 255, 255))


def chip(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, fill: str, ink: str = "#FFFFFF") -> None:
    f = font(25, True)
    bbox = draw.textbbox((0, 0), text.upper(), font=f)
    width = bbox[2] + 44
    draw.rounded_rectangle((xy[0], xy[1], xy[0] + width, xy[1] + 46), radius=23, fill=rgb(fill))
    draw.text((xy[0] + 22, xy[1] + 8), text.upper(), font=f, fill=rgb(ink))


def finish_slot(canvas: Image.Image, palette: dict[str, str], shop: str, note: str | None = None) -> Image.Image:
    draw = ImageDraw.Draw(canvas)
    if note:
        if shop == "temply":
            draw_text(draw, (140, 1800), note, palette, 27, False, palette["muted"])
        else:
            draw_text(draw, (140, 1800), note, palette, 27, True, palette["muted"])
    return canvas.convert("RGB")


def build_gallery(shop: str, background: Path, output_dir: Path) -> None:
    palette = TEMPLY if shop == "temply" else DAISY
    output_dir.mkdir(parents=True, exist_ok=True)

    def new() -> Image.Image:
        return base_canvas(background, palette, shop)

    # 01 — the actual product is the thumbnail subject.
    canvas = new()
    header(canvas, palette, shop, "Amethyst 2027", "DIGITAL PLANNER", "Google Calendar • Monday start", "01 / 10")
    place_page(canvas, "021", (260, 505, 1740, 1510), palette, "Monthly dashboard", palette["accent"])
    draw = ImageDraw.Draw(canvas)
    chip(draw, (260, 1550), "DIGITAL DOWNLOAD", palette["accent"])
    chip(draw, (650, 1550), "NO PHYSICAL ITEM", palette["mauve"] if shop == "temply" else palette["coral"])
    canvas = finish_slot(canvas, palette, shop)
    canvas.save(output_dir / "01-hero.png", format="PNG", optimize=True)

    # 02 — an overview grid, with pages rather than devices or props.
    canvas = new()
    header(canvas, palette, shop, "Inside the planner", "WHAT YOU GET", "A clear look at the core planning pages", "02 / 10")
    place_page(canvas, "021", (120, 560, 940, 1080), palette, "Monthly", palette["accent"])
    place_page(canvas, "030", (1060, 560, 1880, 1080), palette, "Weekly", palette["teal"] if shop == "daisy" else palette["mauve"])
    place_page(canvas, "035", (120, 1190, 940, 1710), palette, "Daily", palette["coral"] if shop == "daisy" else palette["accent"])
    place_page(canvas, "028", (1060, 1190, 1880, 1710), palette, "Wellness", palette["yellow"] if shop == "daisy" else palette["mauve"])
    canvas = finish_slot(canvas, palette, shop, "Monthly • weekly • daily • wellness")
    canvas.save(output_dir / "02-format-overview.png", format="PNG", optimize=True)

    # 03 — section map, using only sections visibly present in the PDF.
    canvas = new()
    header(canvas, palette, shop, "Planner sections", "PLAN WITH INTENTION", "Everything is shown from the supplied PDF", "03 / 10")
    place_page(canvas, "026", (120, 560, 940, 1080), palette, "Goals", palette["accent"])
    place_page(canvas, "028", (1060, 560, 1880, 1080), palette, "Habits + hydration", palette["teal"] if shop == "daisy" else palette["mauve"])
    place_page(canvas, "740", (120, 1190, 940, 1710), palette, "Recipes", palette["coral"] if shop == "daisy" else palette["accent"])
    place_page(canvas, "811", (1060, 1190, 1880, 1710), palette, "Projects", palette["yellow"] if shop == "daisy" else palette["mauve"])
    canvas = finish_slot(canvas, palette, shop, "Goals • habits • hydration • recipes • projects")
    canvas.save(output_dir / "03-toolkit.png", format="PNG", optimize=True)

    # 04 — monthly proof.
    canvas = new()
    header(canvas, palette, shop, "See the layout", "MONTHLY OVERVIEW", "A spacious view for the month ahead", "04 / 10")
    place_page(canvas, "021", (220, 520, 1780, 1510), palette, "January 2027", palette["accent"])
    canvas = finish_slot(canvas, palette, shop, "Use the full monthly view for dates, priorities, and plans.")
    canvas.save(output_dir / "04-monthly-view.png", format="PNG", optimize=True)

    # 05 — daily proof.
    canvas = new()
    header(canvas, palette, shop, "Make space for today", "DAILY PLANNING", "A focused page for the details that matter", "05 / 10")
    place_page(canvas, "035", (220, 520, 1780, 1510), palette, "Daily page", palette["teal"] if shop == "daisy" else palette["accent"])
    canvas = finish_slot(canvas, palette, shop, "A source-page preview, kept sharp and readable.")
    canvas.save(output_dir / "05-daily-page.png", format="PNG", optimize=True)

    # 06 — wellness pages.
    canvas = new()
    header(canvas, palette, shop, "Build supportive routines", "WELLNESS PAGES", "Track the habits and hydration that support your week", "06 / 10")
    place_page(canvas, "028", (120, 560, 940, 1710), palette, "Habit tracker", palette["accent"])
    place_page(canvas, "029", (1060, 560, 1880, 1710), palette, "Hydration", palette["yellow"] if shop == "daisy" else palette["mauve"])
    canvas = finish_slot(canvas, palette, shop, "Two exact interior previews from the supplied planner PDF.")
    canvas.save(output_dir / "06-wellness-pages.png", format="PNG", optimize=True)

    # 07 — rhythm pair.
    canvas = new()
    header(canvas, palette, shop, "Plan at two levels", "WEEKLY + DAILY", "Step back for the week, then focus on today", "07 / 10")
    place_page(canvas, "030", (120, 560, 940, 1710), palette, "Weekly view", palette["accent"])
    place_page(canvas, "035", (1060, 560, 1880, 1710), palette, "Daily view", palette["coral"] if shop == "daisy" else palette["mauve"])
    canvas = finish_slot(canvas, palette, shop, "A simple planning rhythm: overview first, detail second.")
    canvas.save(output_dir / "07-weekly-rhythm.png", format="PNG", optimize=True)

    # 08 — navigation / orientation.
    canvas = new()
    header(canvas, palette, shop, "Find your way around", "NAVIGATION + TABS", "A quick visual of the planner's orientation pages", "08 / 10")
    place_page(canvas, "002", (120, 560, 940, 1710), palette, "Index", palette["teal"] if shop == "daisy" else palette["accent"])
    place_page(canvas, "023", (1060, 560, 1880, 1710), palette, "Calendar section", palette["yellow"] if shop == "daisy" else palette["mauve"])
    canvas = finish_slot(canvas, palette, shop, "Shown as source-page previews; no compatibility claim added.")
    canvas.save(output_dir / "08-navigation.png", format="PNG", optimize=True)

    # 09 — additional pages, not a generic lifestyle collage.
    canvas = new()
    header(canvas, palette, shop, "More ways to plan", "EXTRA PAGES", "Recipes and projects round out the planning system", "09 / 10")
    place_page(canvas, "740", (120, 560, 940, 1710), palette, "Recipe page", palette["coral"] if shop == "daisy" else palette["accent"])
    place_page(canvas, "811", (1060, 560, 1880, 1710), palette, "Project page", palette["teal"] if shop == "daisy" else palette["mauve"])
    canvas = finish_slot(canvas, palette, shop, "Exact interior previews from the supplied PDF.")
    canvas.save(output_dir / "09-extras.png", format="PNG", optimize=True)

    # 10 — a quiet closeout with the supplied thank-you page.
    canvas = new()
    header(canvas, palette, shop, "Before you download", "THANK YOU", "Please review the digital-download note on the listing", "10 / 10")
    place_page(canvas, "001", (260, 505, 1740, 1510), palette, "Thank-you page", palette["accent"])
    draw = ImageDraw.Draw(canvas)
    chip(draw, (260, 1550), "PERSONAL USE ONLY", palette["accent"])
    chip(draw, (720, 1550), "NO RESALE", palette["mauve"] if shop == "temply" else palette["coral"])
    canvas = finish_slot(canvas, palette, shop)
    canvas.save(output_dir / "10-thank-you.png", format="PNG", optimize=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shop", choices=("temply", "daisy"), required=True)
    parser.add_argument("--background", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    build_gallery(args.shop, args.background, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
