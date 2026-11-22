from __future__ import annotations
import hashlib
import time
from pathlib import Path
from bubble_redesign_57 import main
import bubble_redesign_57 as worker

worker.PRODUCTS = ("product-07",)
worker.STATE_PATH = worker.SHOP_ROOT / "bubble_redesign_product07_test_state.json"
worker.LOG_PATH = worker.REPO_ROOT / "bubble_redesign_product07_test.log"


def stage_only(product_dir, assets, context, logger):
    run_dir = worker.REPO_ROOT / "output" / "bubble_redesign_product07_test" / f"run-{int(time.time())}"
    run_dir.mkdir(parents=True, exist_ok=False)
    for index, (_asset_id, url) in enumerate(assets, 1):
        response = context.request.get(url, timeout=180_000)
        if response.status != 200 or not response.body():
            raise RuntimeError(f"asset download failed ({response.status})")
        (run_dir / f"etsy_{index:02d}.png").write_bytes(response.body())
    digest = worker.validate_set(sorted(run_dir.glob("*.png")))
    logger.info("stage-only validated path=%s set_sha256=%s", run_dir, digest)
    print(f"STAGED {run_dir}")
    return digest


worker.replace_after_validation = stage_only
raise SystemExit(main())
