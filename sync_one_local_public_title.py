from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import openpyxl

ROOT = Path(__file__).resolve().parent
WORKBOOK = ROOT / "shops/templystudios/Etsy_SEO_Generator.xlsx"
PUBLIC = ROOT / "scratch/etsy_public_current_templystudios_20260913_192855.json"
EDITOR = ROOT / "scratch/temply_editor_readonly_4428935365_20260913_193337.json"
LISTING_ID = "4428935365"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    public = json.loads(PUBLIC.read_text(encoding="utf-8"))
    editor = json.loads(EDITOR.read_text(encoding="utf-8"))
    public_title = next(str(x.get("title") or "") for x in public["items"] if str(x.get("id")) == LISTING_ID)
    assert editor["title"] == public_title
    wb = openpyxl.load_workbook(WORKBOOK)
    ws = wb["Listings"]
    row_number = None
    for n, row in enumerate(ws.iter_rows(), start=1):
        if re.search(rf"/listing/{LISTING_ID}(?:\D|$)", str(row[15].value or "")):
            row_number = n
            break
    assert row_number is not None
    old_title = str(ws.cell(row_number, 8).value or "")
    assert old_title != public_title
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = ROOT / "output/backup/templystudios" / f"Etsy_SEO_Generator.before_public_title_sync_{LISTING_ID}_{timestamp}.xlsx"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(WORKBOOK, backup)
    pre_hash = sha256(WORKBOOK)
    assert pre_hash == sha256(backup)
    cast(Any, ws.cell(row_number, 8)).value = public_title
    fd, temp_name = tempfile.mkstemp(prefix=".Etsy_SEO_Generator.", suffix=".xlsx", dir=str(WORKBOOK.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        wb.save(temp_path)
        os.replace(temp_path, WORKBOOK)
    finally:
        if temp_path.exists(): temp_path.unlink()
    check = openpyxl.load_workbook(WORKBOOK, read_only=True, data_only=True)["Listings"]
    assert str(check.cell(row_number, 8).value or "") == public_title
    receipt = ROOT / "output/backup/templystudios" / f"local_public_title_sync_receipt_{LISTING_ID}_{timestamp}.json"
    receipt.write_text(json.dumps({
        "operation": "templystudios-local-public-title-sync",
        "scope": "local-only; one title field",
        "listing_id": LISTING_ID,
        "row": row_number,
        "old_local_title": old_title,
        "live_public_title": public_title,
        "editor_readonly": str(EDITOR),
        "workbook": str(WORKBOOK),
        "backup": str(backup),
        "pre_write_sha256": pre_hash,
        "backup_sha256": sha256(backup),
        "post_write_sha256": sha256(WORKBOOK),
        "live_write": False,
        "verified": True,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"listing_id": LISTING_ID, "row": row_number, "receipt": str(receipt), "backup": str(backup), "verified": True, "live_write": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
