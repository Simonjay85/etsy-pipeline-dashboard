from __future__ import annotations
import shutil
import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bubble_redesign_57 as worker

product = worker.SHOP_ROOT / "product-07"
staged = worker.REPO_ROOT / "output" / "bubble_redesign_product07_test" / "run-1789181316"
source = sorted(staged.glob("*.png"))
worker.validate_set(source)
destination = product / "images"
backup = worker.REPO_ROOT / "output" / "backup" / f"BubbleByJulie-product-07-pre-redesign-{int(time.time())}" / "images"
new_dir = product / ".product07-images-new"
if new_dir.exists():
    shutil.rmtree(new_dir)
new_dir.mkdir()
for path in source:
    shutil.copy2(path, new_dir / path.name)
worker.validate_set(sorted(new_dir.glob("*.png")))
backup.parent.mkdir(parents=True, exist_ok=True)
shutil.move(str(destination), str(backup))
shutil.move(str(new_dir), str(destination))
worker.validate_set(sorted(destination.glob("*.png")))
print(f"PROMOTED {destination}")
print(f"BACKUP {backup}")
