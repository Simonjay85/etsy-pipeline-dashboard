from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bubble_redesign_cdp_batch as batch

entries = {
    "product-17": "run-1789319877",
    "product-23": "run-1789322258",
}
state = batch.load_state()
for product_name, run_name in entries.items():
    product = batch.worker.SHOP_ROOT / product_name
    run_dir = batch.worker.REPO_ROOT / "output" / "bubble_redesign_cdp_batch" / product_name / run_name
    source = json.loads((product / "source_catalog" / "source.json").read_text(encoding="utf-8"))
    dimensions = source.get("english_product_details", {}).get("standard_dimensions", "")
    role_file = batch.reorder_dimensions(run_dir, dimensions)
    backup = batch.promote(product, run_dir)
    state.setdefault("products", {})[product_name] = {
        "status": "promoted",
        "run_dir": str(run_dir),
        "backup": backup,
        "dimensions": dimensions,
        "dimensions_file": role_file,
        "recovered_from_stage": True,
    }
    print(f"PROMOTED {product / 'images'}")
    print(f"BACKUP {backup}")
batch.save_state(state)
