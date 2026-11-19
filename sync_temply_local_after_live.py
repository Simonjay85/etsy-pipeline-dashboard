from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent
WORKBOOK = ROOT / "shops/templystudios/Etsy_SEO_Generator.xlsx"
LIVE_LOG = ROOT / "output/audit/templystudios/live_year_title_change_log_20260913_192824.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    live = json.loads(LIVE_LOG.read_text(encoding="utf-8"))
    records = live["records"]
    assert live["live_write_count"] == 5
    assert live["verified_count"] == 5
    assert len(records) == 5

    wb = openpyxl.load_workbook(WORKBOOK)
    ws = wb["Listings"]
    rows_by_id = {}
    for row_number, row in enumerate(ws.iter_rows(), start=1):
        match = re.search(r"/listing/(\d+)", str(row[15].value or ""))
        if match:
            rows_by_id[match.group(1)] = row_number
    assert len(rows_by_id) == 185
    for record in records:
        listing_id = record["listing_id"]
        row_number = rows_by_id[listing_id]
        current = str(ws.cell(row_number, 8).value or "")
        assert current == record["before"]["title"], (listing_id, current, record["before"]["title"])

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = ROOT / "output/backup/templystudios" / f"Etsy_SEO_Generator.before_live_year_sync_{timestamp}.xlsx"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(WORKBOOK, backup)
    pre_hash = sha256(WORKBOOK)
    backup_hash = sha256(backup)
    assert pre_hash == backup_hash

    changes = []
    for record in records:
        listing_id = record["listing_id"]
        row_number = rows_by_id[listing_id]
        old = str(ws.cell(row_number, 8).value or "")
        new = record["after"]["title"]
        ws.cell(row_number, 8).value = new
        changes.append({"listing_id": listing_id, "row": row_number, "old_title": old, "new_title": new})

    fd, temp_name = tempfile.mkstemp(prefix=".Etsy_SEO_Generator.", suffix=".xlsx", dir=str(WORKBOOK.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        wb.save(temp_path)
        os.replace(temp_path, WORKBOOK)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    check = openpyxl.load_workbook(WORKBOOK, read_only=True, data_only=True)["Listings"]
    ids_after = {}
    for row_number, row in enumerate(check.iter_rows(values_only=True), start=1):
        match = re.search(r"/listing/(\d+)", str(row[15] or ""))
        if match:
            ids_after[match.group(1)] = row_number
    assert ids_after == rows_by_id
    for change in changes:
        assert str(check.cell(change["row"], 8).value or "") == change["new_title"]
    post_hash = sha256(WORKBOOK)

    receipt = ROOT / "output/backup/templystudios" / f"local_live_year_sync_receipt_{timestamp}.json"
    receipt.write_text(json.dumps({
        "operation": "templystudios-local-sync-after-live-year-update",
        "scope": "five verified title fields only",
        "live_log": str(LIVE_LOG),
        "workbook": str(WORKBOOK),
        "backup": str(backup),
        "pre_write_sha256": pre_hash,
        "backup_sha256": backup_hash,
        "post_write_sha256": post_hash,
        "changes": changes,
        "verified": True,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"receipt": str(receipt), "backup": str(backup), "changes": len(changes), "verified": True, "post_write_sha256": post_hash}, ensure_ascii=False))


if __name__ == "__main__":
    main()
