#!/usr/bin/env python3
"""Audit Daisy product folders against the Etsy asset baseline.

The baseline is intentionally strict for newly synced listings: 10 image files
and exactly one .zip or .pdf in files/. Existing legacy folders are reported,
not rewritten, because some valid products contain split files or extra pages.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import dashboard_app


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
DIGITAL_EXTS = {".zip", ".pdf"}


def audit() -> dict:
    products = dashboard_app.products_from_excel()
    shop_dir = dashboard_app.SHOP_DIR()
    records = []
    for product in products:
        folder = str(product.get("folder") or "")
        folder_path = shop_dir / folder
        image_dir = folder_path / "images"
        files_dir = folder_path / "files"
        images = sorted(
            p.name for p in image_dir.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS
        ) if image_dir.is_dir() else []
        digital = sorted(
            p.name for p in files_dir.iterdir()
            if p.is_file() and p.suffix.lower() in DIGITAL_EXTS
        ) if files_dir.is_dir() else []
        records.append({
            "folder": folder,
            "row": product.get("row"),
            "title": product.get("title") or folder,
            "etsy_url": product.get("etsy_url") or "",
            "status": product.get("status") or "",
            "image_count": len(images),
            "images": images,
            "digital_file_count": len(digital),
            "digital_files": digital,
            "strict_baseline_ok": len(images) == 10 and len(digital) == 1,
            "image_baseline_ok": len(images) == 10,
            "digital_baseline_ok": len(digital) == 1,
        })
    return {
        "shop": dashboard_app._active_shop_id,
        "workbook": str(dashboard_app.EXCEL_FILE()),
        "baseline": {"image_count": 10, "digital_file_count": 1, "digital_extensions": sorted(DIGITAL_EXTS)},
        "counts": {
            "products": len(records),
            "strict_ok": sum(1 for r in records if r["strict_baseline_ok"]),
            "image_outliers": sum(1 for r in records if not r["image_baseline_ok"]),
            "digital_outliers": sum(1 for r in records if not r["digital_baseline_ok"]),
        },
        "products": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "counts": report["counts"]}, ensure_ascii=False))
    for record in report["products"]:
        if not record["strict_baseline_ok"]:
            print(
                f"OUTLIER {record['folder']} images={record['image_count']} "
                f"digital={record['digital_file_count']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
