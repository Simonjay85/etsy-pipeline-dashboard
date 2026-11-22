from __future__ import annotations

import shutil
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bubble_redesign_57 as worker

product = worker.SHOP_ROOT / "product-09"
staged = worker.REPO_ROOT / "output" / "bubble_redesign_product09_test" / "run-1789293807"
source = sorted(staged.glob("*.png"))
worker.validate_set(source)

destination = product / "images"
old_images = sorted(destination.glob("*.png"))
if len(old_images) != 10:
    raise RuntimeError(f"refusing to replace product-09: expected 10 existing PNGs, found {len(old_images)}")

stamp = int(time.time())
backup = worker.REPO_ROOT / "output" / "backup" / f"BubbleByJulie-product-09-pre-redesign-{stamp}" / "images"
new_dir = product / ".product09-images-new"
if new_dir.exists():
    raise RuntimeError(f"refusing to replace: temporary directory already exists: {new_dir}")

new_dir.mkdir()
for path in source:
    shutil.copy2(path, new_dir / path.name)
worker.validate_set(sorted(new_dir.glob("*.png")))

backup.parent.mkdir(parents=True, exist_ok=False)
shutil.move(str(destination), str(backup))
try:
    shutil.move(str(new_dir), str(destination))
except Exception:
    shutil.move(str(backup), str(destination))
    raise

worker.validate_set(sorted(destination.glob("*.png")))
print(f"PROMOTED {destination}")
print(f"BACKUP {backup}")
