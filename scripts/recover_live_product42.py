from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bubble_redesign_cdp_batch as batch

product_name = "product-42"
product = batch.worker.SHOP_ROOT / product_name
source = json.loads((product / "source_catalog" / "source.json").read_text(encoding="utf-8"))
dimensions = source.get("english_product_details", {}).get("standard_dimensions", "")
page = batch.CDP(batch.page_target()["webSocketDebuggerUrl"])
try:
    current_url = page.evaluate("location.href") or ""
    if "/c/6aa94fc0-6e04-83ec-af57-203ce691218a" not in current_url:
        raise RuntimeError(f"live page is not product-42 conversation: {current_url}")
    urls = batch.generated_urls(page)
    if len(urls) != 10:
        raise RuntimeError(f"expected 10 live generated assets, found {len(urls)}")
    run_dir = batch.RUN_ROOT / product_name / f"run-{int(time.time())}"
    run_dir.mkdir(parents=True, exist_ok=False)
    hashes: set[str] = set()
    for index, url in enumerate(urls, 1):
        data = batch.download_from_page(page, url)
        width, height = batch.read_png_size(data)
        if width != height:
            raise RuntimeError(f"download {index} is not square: {width}x{height}")
        digest = hashlib.sha256(data).hexdigest()
        if digest in hashes:
            raise RuntimeError(f"duplicate generated PNG at index {index}")
        hashes.add(digest)
        (run_dir / f"etsy_{index:02d}.png").write_bytes(data)
    batch.worker.validate_set(sorted(run_dir.glob("etsy_*.png")))
    role_file = batch.reorder_dimensions(run_dir, dimensions)
    batch.worker.validate_set(sorted(run_dir.glob("etsy_*.png")))
    backup = batch.promote(product, run_dir)
    state = batch.load_state()
    state.setdefault("products", {})[product_name] = {
        "status": "promoted",
        "run_dir": str(run_dir),
        "backup": backup,
        "dimensions": dimensions,
        "dimensions_file": role_file,
        "recovered_from_live_conversation": True,
    }
    batch.save_state(state)
    print(f"PROMOTED {product / 'images'}")
    print(f"RUN {run_dir}")
    print(f"BACKUP {backup}")
finally:
    page.close()
