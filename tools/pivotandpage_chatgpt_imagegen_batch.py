#!/usr/bin/env python3
"""Prepare 10 independent ChatGPT Image Generate jobs for PivotAndPage furniture.

This module deliberately does NOT render listing graphics locally and does not use
Canva.  It creates source-led generation manifests that are intended to be run as
10 separate calls with ChatGPT's built-in image generation tool.

The generated manifests are non-mutating: they do not replace shop images or edit
the Etsy workbook.  Final image installation must happen only after all 10 image
outputs exist and pass QA.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
SHOP_ID = "shop3"
SHOP_ROOT = ROOT / "shops" / SHOP_ID
WORKBOOK = SHOP_ROOT / "Etsy_SEO_Generator.xlsx"
WORKING_ROOT = ROOT / "working" / "pivotandpage-chatgpt-imagegen"

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
OUTPUT_NAMES = [f"etsy_{i:02d}.png" for i in range(1, 11)]


@dataclass(frozen=True)
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
    selection_reason: str = "under_10_images"


def now_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")


def normalize_text(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def split_title(title: str) -> tuple[str, str]:
    parts = [part.strip() for part in title.split("|")]
    product_type = parts[0] if parts else "Wood Furniture"
    wood = parts[1] if len(parts) > 1 else "Natural wood"
    return product_type, wood


def model_from_title(title: str, source: dict) -> str:
    match = re.search(r"\bModel\s+([^|]+?)\s*$", title, flags=re.I)
    if match:
        return match.group(1).strip()
    source_title = normalize_text(source.get("title") or source.get("list_title"))
    match = re.search(r"\b([A-Z]{1,4}\d{2,6}|\d{4,6})\s*$", source_title, flags=re.I)
    if match:
        return match.group(1).upper()
    return normalize_text(source.get("template_id")) or "unconfirmed"


def has_legacy_local_gallery(product_root: Path) -> bool:
    """Return True for galleries installed by the retired local Pillow renderer."""
    manifest_path = product_root / "source_catalog" / "listing-gallery.json"
    if not manifest_path.is_file():
        return False
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    guards = payload.get("guards") or {}
    return bool(
        payload.get("schema") == 2
        and isinstance(guards, dict)
        and guards.get("source_geometry_not_synthesized") is True
    )


def load_catalog() -> list[Product]:
    wb = openpyxl.load_workbook(WORKBOOK, read_only=True, data_only=True)
    ws = wb["Listings"]
    products: list[Product] = []
    try:
        for row in range(4, ws.max_row + 1):
            folder = normalize_text(ws.cell(row, 2).value)
            if not re.fullmatch(r"product-\d+", folder):
                continue
            product_root = SHOP_ROOT / folder
            if not product_root.is_dir():
                continue
            title = normalize_text(ws.cell(row, 8).value or ws.cell(row, 4).value)
            if not title.startswith(TARGET_PREFIXES):
                continue
            image_dir = product_root / "images"
            before_count = 0
            if image_dir.is_dir():
                before_count = sum(
                    1 for path in image_dir.iterdir()
                    if path.is_file() and path.suffix.lower() in IMG_EXTS
                )
            legacy_local_gallery = has_legacy_local_gallery(product_root)
            if before_count >= 10 and not legacy_local_gallery:
                continue
            source_path = product_root / "source_catalog" / "source.json"
            source: dict = {}
            if source_path.is_file():
                try:
                    loaded = json.loads(source_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        source = loaded
                except (OSError, json.JSONDecodeError):
                    source = {}
            product_type, wood = split_title(title)
            products.append(
                Product(
                    row=row,
                    folder=folder,
                    title=title,
                    seed=normalize_text(ws.cell(row, 4).value),
                    before_count=before_count,
                    product_type=product_type,
                    wood=wood,
                    model=model_from_title(title, source),
                    source=source,
                    selection_reason=(
                        "replace_legacy_pillow_gallery"
                        if legacy_local_gallery
                        else "under_10_images"
                    ),
                )
            )
    finally:
        wb.close()
    products.sort(key=lambda item: (item.before_count, int(item.folder.split("-")[-1])))
    return products


def source_references(product: Product) -> list[dict]:
    product_root = SHOP_ROOT / product.folder
    refs: list[dict] = []
    seen: set[str] = set()

    def add(kind: str, value: str, priority: int) -> None:
        key = f"{kind}:{value}"
        if not value or key in seen:
            return
        seen.add(key)
        refs.append({"kind": kind, "value": value, "priority": priority})

    original_dir = product_root / "source_catalog" / "original_images"
    if original_dir.is_dir():
        for path in sorted(original_dir.iterdir()):
            if path.is_file() and path.suffix.lower() in IMG_EXTS:
                add("local_original", path.relative_to(ROOT).as_posix(), 1)

    for item in product.source.get("output_images") or []:
        if not isinstance(item, dict):
            continue
        url = normalize_text(item.get("download_url") or item.get("source_url"))
        add("source_url", url, 2)
    for url in product.source.get("image_urls") or []:
        add("source_url", normalize_text(url), 2)

    # Never feed an installed legacy Pillow gallery back into Image Generate: those
    # files can contain composed text/branding and are not product-truth references.
    if product.selection_reason != "replace_legacy_pillow_gallery":
        image_dir = product_root / "images"
        if image_dir.is_dir():
            for path in sorted(image_dir.iterdir()):
                if path.is_file() and path.suffix.lower() in IMG_EXTS:
                    add("current_image_fallback", path.relative_to(ROOT).as_posix(), 9)

    return sorted(refs, key=lambda item: (item["priority"], item["value"]))


def scene_for_slot_07(product_type: str) -> str:
    low = product_type.lower()
    if "furniture set" in low or "table" in low:
        return (
            "Place the exact referenced furniture in a refined tea/lounge or dining-scale interior, "
            "with limewash walls, pale stone or oak flooring, one quiet linen accent, and generous negative space."
        )
    return (
        "Place the exact referenced piece beside a low platform bed in a calm Japandi bedroom with warm linen, "
        "limewash wall, pale oak floor, and very minimal styling."
    )


def global_identity_rules(product: Product) -> str:
    return (
        f"The product is {product.product_type}, material/style label: {product.wood}, model {product.model}. "
        "Use ALL attached original reference photos as strict product-identity references. Preserve the exact real product: "
        "same silhouette, proportions, tabletop/seat shape, base shape, number of legs/supports, joinery, edge character, "
        "piece count, visible defects/marks, and overall wood tone/grain character. Do not redesign, beautify, simplify, "
        "symmetrize, add or remove parts, invent hidden geometry, add extra matching stools/tables, or turn one furniture type "
        "into another. Remove any source watermark/logo/text artifact from the generated scene while keeping product geometry intact."
    )


def global_visual_rules() -> str:
    return (
        "Premium international artisan furniture catalog photography. Warm natural wood, warm cream/sand/walnut/muted-olive palette, "
        "Japandi / Wabi-Sabi / Quiet Luxury mood, natural daylight, soft realistic shadows, restrained styling, realistic material detail, "
        "high-end editorial photography, product remains the hero. One square standalone Etsy image only. NO collage, NO contact sheet, "
        "NO split panels, NO inset thumbnails, NO text, NO typography, NO captions, NO badges, NO infographic, NO logo, NO watermark, "
        "NO border, NO neon, NO childish pastel styling, NO fantasy furniture, NO busy background."
    )


def make_assets(product: Product) -> list[dict]:
    identity = global_identity_rules(product)
    visual = global_visual_rules()
    scenes = [
        (
            "Hero studio 3/4",
            "Create a clean premium hero image on a warm cream seamless studio background. Camera at natural eye level with a gentle three-quarter view; show the entire product with comfortable breathing room. Soft window daylight from one side, subtle floor shadow, no props except at most one tiny neutral ceramic object far from the product.",
        ),
        (
            "Clean catalog front",
            "Create a distinct clean catalog view on a sand-toned plaster studio set. Camera more frontal and slightly farther back than the hero; entire product visible and centered. Use diffuse daylight and a soft grounding shadow. No decorative props.",
        ),
        (
            "Opposite 3/4 angle",
            "Create a separate alternate three-quarter catalog photograph from the opposite side, but only reveal geometry that can be supported by the reference photos. Warm ivory studio, soft daylight, product fully visible, no props.",
        ),
        (
            "Wood surface close-up",
            "Create a dedicated macro/detail photograph of the most characteristic real wood surface from the referenced product. Crop tightly enough to reveal authentic grain, tone transitions, live/organic edge or surface character while still making the product context recognizable. Neutral warm background, raking daylight, realistic texture.",
        ),
        (
            "Construction detail close-up",
            "Create a separate close-up focused on one real construction detail supported by the references: base transition, joinery, carved edge, leg/support shape, or stool/body profile. Do not invent hardware or joinery. Warm natural daylight, shallow but realistic depth of field, premium craft catalog feel.",
        ),
        (
            "Living room placement",
            "Place the exact referenced product in a calm high-end living room beside a warm cream or oatmeal sofa. Limewash/plaster wall, natural oak or pale stone floor, one muted-olive textile accent, minimal decor. Keep realistic scale and leave the product unobstructed and visually dominant.",
        ),
        (
            "Bedroom or lounge placement",
            scene_for_slot_07(product.product_type),
        ),
        (
            "Wabi-Sabi editorial interior",
            "Create a distinctly different Wabi-Sabi editorial interior: warm textured plaster wall, soft linen curtain, aged neutral stone or oak floor, one sculptural branch or ceramic object. Natural side light with soft shadows. Keep the exact product isolated as the hero and do not add matching furniture pieces.",
        ),
        (
            "Quiet luxury architectural scene",
            "Create a quiet-luxury architectural scene with generous negative space, warm cream wall planes, muted olive accent in the distance, and soft morning daylight. Product placed alone at realistic scale, photographed like a premium interior design magazine. No people, no text, no extra matching pieces.",
        ),
        (
            "Craftsmanship atmosphere",
            "Create a premium artisan-workshop atmosphere around the exact finished product: clean woodworker studio, soft daylight, a few restrained hand tools in the background only. The product must remain fully finished exactly like the references; do not show invented construction stages, unverified hand-carving, or change its finish. No visible brand marks or signage.",
        ),
    ]

    assets: list[dict] = []
    for index, (title, scene) in enumerate(scenes, 1):
        prompt = f"{identity} {visual} SCENE: {scene}"
        assets.append(
            {
                "slot": f"{index:02d}",
                "output": OUTPUT_NAMES[index - 1],
                "title": title,
                "generation_mode": "independent_single_image",
                "reference_policy": "attach original source photos; prefer local_original/source_url over current_image_fallback",
                "prompt": prompt,
                "qa": [
                    "single standalone image; not collage or multi-panel",
                    "no logo, watermark, text, labels, badges, or infographic",
                    "product identity and geometry match source references",
                    "no extra matching furniture pieces invented",
                    "premium warm Japandi/Wabi-Sabi/Quiet Luxury art direction",
                ],
            }
        )
    return assets


def build_manifest(product: Product) -> dict:
    refs = source_references(product)
    return {
        "schema": "etsy-image-generation-v3",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "shop_id": SHOP_ID,
        "folder": product.folder,
        "row": product.row,
        "listing_title": product.title,
        "product_type": product.product_type,
        "wood": product.wood,
        "model": product.model,
        "before_image_count": product.before_count,
        "selection_reason": product.selection_reason,
        "tool_mode": "chatgpt-built-in-imagegen",
        "generation_count": 10,
        "independent_generation": True,
        "one_generation_call_per_asset": True,
        "canva_used": False,
        "local_compositor_used": False,
        "contact_sheet_output": False,
        "branding_overlay": False,
        "logo_policy": "none",
        "text_overlay_policy": "none",
        "target_format": "PNG",
        "target_aspect_ratio": "1:1",
        "target_quality": "highest available; prefer 2048x2048 or larger square output when supported",
        "source_references": refs,
        "source_reference_note": (
            "When running ChatGPT Image Generate, attach the original source photographs for every asset. "
            "Do not use earlier composed listing graphics as product truth when original references are available."
        ),
        "run_instruction": (
            "Run assets 01 through 10 as TEN SEPARATE image-generation calls. Never ask for a 10-image grid, contact sheet, collage, "
            "or multi-panel output. Save each returned image under its declared output filename."
        ),
        "assets": make_assets(product),
    }


def validate_manifest(manifest: dict) -> None:
    if manifest.get("tool_mode") != "chatgpt-built-in-imagegen":
        raise ValueError("tool_mode must be chatgpt-built-in-imagegen")
    if manifest.get("canva_used") is not False or manifest.get("local_compositor_used") is not False:
        raise ValueError("Canva/local compositor must be disabled")
    assets = manifest.get("assets") or []
    if len(assets) != 10:
        raise ValueError(f"expected 10 assets, found {len(assets)}")
    outputs = [asset.get("output") for asset in assets]
    prompts = [asset.get("prompt") for asset in assets]
    if outputs != OUTPUT_NAMES or len(set(outputs)) != 10:
        raise ValueError("asset outputs are not the 10 canonical independent filenames")
    if len(set(prompts)) != 10:
        raise ValueError("image-generation prompts must be distinct")
    for asset in assets:
        if asset.get("generation_mode") != "independent_single_image":
            raise ValueError(f"slot {asset.get('slot')} is not independent_single_image")
        prompt = str(asset.get("prompt") or "")
        required = ("NO collage", "NO text", "NO logo", "NO watermark")
        if not all(token in prompt for token in required):
            raise ValueError(f"slot {asset.get('slot')} is missing no-brand/no-collage output guards")


def write_manifest(product: Product, run_root: Path) -> Path:
    out_dir = run_root / product.folder
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(product)
    validate_manifest(manifest)
    path = out_dir / "generation-manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def select_products(products: Iterable[Product], requested: list[str], limit: int) -> list[Product]:
    selected = list(products)
    if requested:
        wanted = set(requested)
        selected = [product for product in selected if product.folder in wanted]
        missing = wanted - {product.folder for product in selected}
        if missing:
            raise SystemExit(f"Requested products are not current <10 furniture targets: {sorted(missing)}")
    if limit:
        selected = selected[:limit]
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", action="append", default=[], help="Prepare one current furniture target, e.g. product-101")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of current under-10 targets")
    parser.add_argument("--run-id", default=now_stamp())
    args = parser.parse_args(argv)

    products = load_catalog()
    selected = select_products(products, args.product, args.limit)
    run_root = WORKING_ROOT / args.run_id
    run_root.mkdir(parents=True, exist_ok=True)

    results = []
    for product in selected:
        manifest_path = write_manifest(product, run_root)
        results.append(
            {
                "product": product.folder,
                "before": product.before_count,
                "generation_jobs": 10,
                "manifest": manifest_path.relative_to(ROOT).as_posix(),
                "source_reference_count": len(source_references(product)),
            }
        )

    report = {
        "schema": 1,
        "run_id": args.run_id,
        "shop_id": SHOP_ID,
        "tool_mode": "chatgpt-built-in-imagegen",
        "canva_used": False,
        "local_compositor_used": False,
        "targets": len(selected),
        "jobs": len(selected) * 10,
        "mutated_shop_images": False,
        "mutated_workbook": False,
        "results": results,
    }
    report_path = run_root / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
