from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from PIL import Image, ImageOps
from websockets.sync.client import connect

import bubble_redesign_57 as worker

ALL_PRODUCTS = ("product-12", *(f"product-{n:02d}" for n in range(14, 65)), "product-174", "product-175")
ONLY_PRODUCT = os.environ.get("BUBBLE_CDP_ONLY", "").strip()
FORCE_PRODUCT = os.environ.get("BUBBLE_CDP_FORCE", "").strip()
PRODUCTS = (ONLY_PRODUCT,) if ONLY_PRODUCT else ALL_PRODUCTS
STATE_PATH = worker.SHOP_ROOT / "bubble_redesign_cdp_batch_state.json"
RUN_ROOT = worker.REPO_ROOT / "output" / "bubble_redesign_cdp_batch"


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"version": 1, "products": {}}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


class CDP:
    def __init__(self, url: str):
        self.ws = connect(url, max_size=50 * 1024 * 1024)
        self.next_id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        ident = self.next_id
        self.ws.send(json.dumps({"id": ident, "method": method, "params": params or {}}))
        while True:
            result = json.loads(self.ws.recv())
            if result.get("id") == ident:
                if "error" in result:
                    raise RuntimeError(f"{method}: {result['error']}")
                return result.get("result", {})

    def evaluate(self, expression: str):
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        return result.get("result", {}).get("value")

    def close(self) -> None:
        self.ws.close()


def page_target() -> dict:
    with urllib.request.urlopen("http://127.0.0.1:9333/json/list", timeout=10) as response:
        targets = json.load(response)
    for target in targets:
        if target.get("type") == "page" and "chatgpt.com" in target.get("url", ""):
            if target.get("webSocketDebuggerUrl"):
                return target
    raise RuntimeError("no ChatGPT page target available")


def wait_for_editor(page: CDP, timeout: int = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if page.evaluate("!!document.querySelector('#prompt-textarea')"):
            return
        time.sleep(1)
    raise RuntimeError("ChatGPT prompt editor unavailable")


def open_product_chat(page: CDP) -> None:
    """Open a fresh regular conversation in the existing tab."""
    page.call("Page.navigate", {"url": "https://chatgpt.com/"})
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        state = page.evaluate(
            """({
              url: location.href,
              ready: document.readyState,
              editor: !!document.querySelector('#prompt-textarea')
            })"""
        ) or {}
        if state.get("url", "").rstrip("/") == "https://chatgpt.com" and state.get("ready") == "complete" and state.get("editor"):
            break
        time.sleep(1)
    else:
        raise RuntimeError("temporary ChatGPT conversation did not load")
    time.sleep(2)
    if generated_urls(page):
        raise RuntimeError("temporary conversation still exposes previous generated assets")


def select_create_image(page: CDP) -> None:
    active = page.evaluate("!!document.querySelector('[data-inline-selection-pill][data-keyword=\"Create image\"]')")
    if active:
        if page.evaluate("document.querySelectorAll('[data-inline-selection-pill][data-keyword=\"Create image\"]').length") != 1:
            raise RuntimeError("composer has duplicate Create image pills")
        return
    clicked = False
    for _ in range(20):
        if not page.evaluate("document.querySelector('.__menu-item') && [...document.querySelectorAll('.__menu-item')].some(x => (x.textContent || '').trim().startsWith('Create image'))"):
            page.evaluate("document.querySelector('button[data-testid=\"composer-plus-btn\"]')?.click()")
        clicked = page.evaluate(
            """(() => {
              const item = [...document.querySelectorAll('.__menu-item')].find(x =>
                (x.textContent || '').trim().startsWith('Create image')
              );
              if (!item) return false;
              item.click();
              return true;
            })()"""
        )
        if clicked:
            break
        time.sleep(0.5)
    if not clicked:
        raise RuntimeError("Create image menu item unavailable after 10s")
    time.sleep(1)
    if page.evaluate("document.querySelectorAll('[data-inline-selection-pill][data-keyword=\"Create image\"]').length") != 1:
        raise RuntimeError("Create image pill was not activated exactly once")


def prompt_for(product_dir: Path) -> str:
    return worker.product_prompt(product_dir, worker.PROMPT_PATH.read_text(encoding="utf-8"))


def clean_editor_text(page: CDP) -> str:
    return page.evaluate(
        """(() => {
          const e = document.querySelector('#prompt-textarea');
          if (!e) return '';
          const c = e.cloneNode(true);
          c.querySelectorAll('[data-inline-selection-pill],[data-inline-selection-pill-cursor-target]').forEach(x => x.remove());
          return c.textContent || '';
        })()"""
    ) or ""


def clear_composer(page: CDP) -> None:
    page.evaluate("document.querySelector('#prompt-textarea')?.focus()")
    for _ in range(2):
        page.call(
            "Input.dispatchKeyEvent",
            {
                "type": "keyDown",
                "key": "a",
                "code": "KeyA",
                "windowsVirtualKeyCode": 65,
                "nativeVirtualKeyCode": 65,
                "modifiers": 4,
            },
        )
        page.call(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": "a",
                "code": "KeyA",
                "windowsVirtualKeyCode": 65,
                "nativeVirtualKeyCode": 65,
                "modifiers": 4,
            },
        )
        page.call(
            "Input.dispatchKeyEvent",
            {
                "type": "keyDown",
                "key": "Backspace",
                "code": "Backspace",
                "windowsVirtualKeyCode": 8,
                "nativeVirtualKeyCode": 8,
            },
        )
        page.call(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": "Backspace",
                "code": "Backspace",
                "windowsVirtualKeyCode": 8,
                "nativeVirtualKeyCode": 8,
            },
        )
        if not clean_editor_text(page).strip():
            return
        time.sleep(0.25)
    if clean_editor_text(page).strip():
        raise RuntimeError("composer did not clear previous prompt")


def wait_until_idle(page: CDP, timeout: int = 300) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = page.evaluate(
            """({
              editor: !!document.querySelector('#prompt-textarea'),
              stop: [...document.querySelectorAll('button[aria-label*="Stop" i]')]
                .some(x => x.offsetParent !== null),
              send: [...document.querySelectorAll('button[aria-label="Send prompt"]')]
                .some(x => x.offsetParent !== null && !x.disabled)
            })"""
        ) or {}
        if state.get("editor") and not state.get("stop"):
            return
        time.sleep(2)
    raise RuntimeError("composer did not become idle before next request")


def upload_and_send(page: CDP, product_dir: Path, prompt: str) -> None:
    sources = sorted((product_dir / "images").glob("*.png"))
    if len(sources) != 10:
        raise RuntimeError(f"expected 10 source PNGs, found {len(sources)}")
    wait_until_idle(page)
    clear_composer(page)
    select_create_image(page)
    root = page.call("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
    input_node = page.call("DOM.querySelector", {"nodeId": root, "selector": "#upload-files"}).get("nodeId")
    if not input_node:
        raise RuntimeError("upload input unavailable")
    page.call("DOM.setFileInputFiles", {"nodeId": input_node, "files": [str(x) for x in sources]})
    state = {}
    for _ in range(45):
        state = page.evaluate(
            """({
              files: document.querySelector('#upload-files')?.files?.length || 0,
              remove: [...document.querySelectorAll('button[aria-label*="Remove file" i]')]
                .filter(x => x.offsetParent !== null).length
            })"""
        ) or {}
        if state.get("files") == 10 and state.get("remove", 0) >= 10:
            break
        time.sleep(1)
    if state.get("files") != 10 or state.get("remove", 0) < 10:
        raise RuntimeError(f"upload verification failed: {state}")
    page.evaluate("document.querySelector('#prompt-textarea')?.focus()")
    page.call("Input.insertText", {"text": prompt})
    entered = clean_editor_text(page)
    if re.sub(r"\s+", "", entered) != re.sub(r"\s+", "", prompt):
        raise RuntimeError(f"prompt verification failed expected={len(prompt)} actual={len(entered)}")
    sent = page.evaluate(
        """(() => {
          const buttons = [...document.querySelectorAll('button[aria-label="Send prompt"]')]
            .filter(x => x.offsetParent !== null);
          if (!buttons.length) return false;
          buttons.at(-1).click();
          return true;
        })()"""
    )
    if not sent:
        raise RuntimeError("send button unavailable")


def generated_urls(page: CDP) -> list[str]:
    return page.evaluate(
        """(() => {
          const output = [], seen = new Set();
          for (const img of document.querySelectorAll('img')) {
            if (!(img.alt || '').toLowerCase().includes('generated image')) continue;
            const src = img.currentSrc || img.src || '';
            const match = src.match(/[?&]id=([^&]+)/);
            if (match && src.includes('/backend-api/estuary/content') && !seen.has(match[1])) {
              seen.add(match[1]);
              output.push(src);
            }
          }
          return output;
        })()"""
    ) or []


def asset_id(url: str) -> str | None:
    match = re.search(r"[?&]id=([^&]+)", url)
    return match.group(1) if match else None


def download_from_page(page: CDP, url: str) -> bytes:
    expression = """async (url) => {
      const response = await fetch(url, {credentials: 'include'});
      if (!response.ok) return {status: response.status};
      const bytes = new Uint8Array(await response.arrayBuffer());
      let binary = '';
      for (let i = 0; i < bytes.length; i += 32768) {
        binary += String.fromCharCode(...bytes.subarray(i, i + 32768));
      }
      return {status: response.status, b64: btoa(binary)};
    }"""
    value = page.evaluate(f"({expression})({json.dumps(url)})")
    if not value or value.get("status") != 200:
        raise RuntimeError(f"authenticated Estuary fetch failed: {value}")
    return base64.b64decode(value["b64"])


def read_png_size(data: bytes) -> tuple[int, int]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return struct.unpack(">II", data[16:24])


def reorder_dimensions(run_dir: Path, dimensions: str) -> str:
    match = re.search(
        r"W\s*([0-9]+(?:\.[0-9]+)?)\s*x\s*D\s*([0-9]+(?:\.[0-9]+)?)\s*x\s*H\s*([0-9]+(?:\.[0-9]+)?)\s*(mm|cm|in|\")",
        dimensions,
        flags=re.IGNORECASE,
    )
    if not match:
        return "none"
    width, depth, height, unit = match.groups()
    compact_signature = f"w{width}xd{depth}xh{height}{unit}".replace('"', "")
    candidates: list[Path] = []
    for path in sorted(run_dir.glob("etsy_*.png")):
        ocr_texts: list[str] = []

        def read_ocr(image_path: Path, psm: int) -> None:
            ocr_texts.append(
                subprocess.run(
                    ["/opt/homebrew/bin/tesseract", str(image_path), "stdout", "--psm", str(psm)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                ).stdout
            )

        def has_match() -> bool:
            compact_all = "".join(re.sub(r"[^a-z0-9.]", "", text.lower()) for text in ocr_texts)
            component_hits = all(
                re.search(rf"{letter}{re.escape(value)}(?:mm|cm|in)?", compact_all)
                for letter, value in (("w", width), ("d", depth), ("h", height))
            )
            return compact_signature in compact_all or component_hits

        read_ocr(path, 11)
        if not has_match():
            with tempfile.TemporaryDirectory(prefix=".ocr-", dir=run_dir) as temp_dir:
                image = Image.open(path).convert("L")
                enlarged = ImageOps.autocontrast(image).resize((image.width * 2, image.height * 2))
                thresholded = enlarged.point([0 if value < 170 else 255 for value in range(256)])
                for name, variant in (("enlarged", enlarged), ("thresholded", thresholded)):
                    variant_path = Path(temp_dir) / f"{name}.png"
                    variant.save(variant_path)
                    read_ocr(variant_path, 6)
                    read_ocr(variant_path, 11)
                    if has_match():
                        break
                if not has_match():
                    for angle in (90, 270):
                        rotated = image.rotate(angle, expand=True)
                        rotated_path = Path(temp_dir) / f"rotated-{angle}.png"
                        rotated.save(rotated_path)
                        read_ocr(rotated_path, 11)
                        if has_match():
                            break

        if has_match():
            candidates.append(path)
    if len(candidates) != 1:
        raise RuntimeError(f"dimensions image OCR expected 1 candidate, found {len(candidates)}")
    dimensions_path = candidates[0]
    if dimensions_path.name == "etsy_04.png":
        return dimensions_path.name
    temp = run_dir / ".dimensions-order-temp.png"
    if temp.exists():
        raise RuntimeError(f"temporary dimensions file already exists: {temp}")
    target = run_dir / "etsy_04.png"
    dimensions_path.rename(temp)
    target.rename(run_dir / ".etsy04-order-temp.png")
    temp.rename(target)
    (run_dir / ".etsy04-order-temp.png").rename(dimensions_path)
    return target.name


def promote(product_dir: Path, run_dir: Path) -> str:
    source = sorted(run_dir.glob("etsy_*.png"))
    worker.validate_set(source)
    destination = product_dir / "images"
    old_images = sorted(destination.glob("*.png"))
    if len(old_images) != 10:
        raise RuntimeError(f"expected 10 existing PNGs, found {len(old_images)}")
    stamp = int(time.time())
    backup = worker.REPO_ROOT / "output" / "backup" / f"BubbleByJulie-{product_dir.name}-pre-redesign-{stamp}" / "images"
    new_dir = product_dir / f".{product_dir.name}-images-new"
    if new_dir.exists():
        raise RuntimeError(f"temporary directory already exists: {new_dir}")
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
    return str(backup)


def process_product(page: CDP, product_name: str) -> dict:
    product_dir = worker.SHOP_ROOT / product_name
    dimensions = worker.product_prompt(product_dir, worker.PROMPT_PATH.read_text(encoding="utf-8"))
    verified_dimensions = json.loads((product_dir / "source_catalog" / "source.json").read_text(encoding="utf-8"))
    dims = verified_dimensions.get("english_product_details", {}).get("standard_dimensions", "")
    prompt = dimensions
    open_product_chat(page)
    upload_and_send(page, product_dir, prompt)
    print(f"SENT {product_name} single-request", flush=True)
    deadline = time.monotonic() + worker.MAX_WAIT_SECONDS
    captured_urls: dict[str, str] = {}
    urls: list[str] = []
    while time.monotonic() < deadline:
        for url in generated_urls(page):
            asset = asset_id(url)
            if asset:
                captured_urls.setdefault(asset, url)
        urls = list(captured_urls.values())
        print(f"POLL {product_name} generated={len(urls)}", flush=True)
        if len(captured_urls) == 10:
            break
        time.sleep(5)
    if len(urls) != 10:
        raise RuntimeError(f"expected 10 generated images, got {len(urls)}")
    run_dir = RUN_ROOT / product_name / f"run-{int(time.time())}"
    run_dir.mkdir(parents=True, exist_ok=False)
    hashes: set[str] = set()
    for index, url in enumerate(urls, 1):
        data = download_from_page(page, url)
        width, height = read_png_size(data)
        if width != height:
            raise RuntimeError(f"download {index} is not square: {width}x{height}")
        digest = hashlib.sha256(data).hexdigest()
        if digest in hashes:
            raise RuntimeError(f"duplicate generated PNG at index {index}")
        hashes.add(digest)
        (run_dir / f"etsy_{index:02d}.png").write_bytes(data)
    worker.validate_set(sorted(run_dir.glob("etsy_*.png")))
    role_file = reorder_dimensions(run_dir, dims)
    worker.validate_set(sorted(run_dir.glob("etsy_*.png")))
    backup = promote(product_dir, run_dir)
    return {"status": "promoted", "run_dir": str(run_dir), "backup": backup, "dimensions": dims, "dimensions_file": role_file}


def main() -> int:
    state = load_state()
    state.setdefault("products", {})
    page: CDP | None = None
    for product_name in PRODUCTS:
        if state["products"].get(product_name, {}).get("status") == "promoted" and product_name != FORCE_PRODUCT:
            continue
        try:
            if page is None:
                page = CDP(page_target()["webSocketDebuggerUrl"])
            result = process_product(page, product_name)
            state["products"][product_name] = result
            save_state(state)
            print(f"DONE {product_name} {result['run_dir']}", flush=True)
        except Exception as exc:
            state["products"][product_name] = {"status": "failed", "error": repr(exc), "updated": int(time.time())}
            save_state(state)
            print(f"FAILED {product_name}: {exc}", flush=True)
            try:
                if page is not None:
                    page.evaluate(
                        """(() => {
                          const button = [...document.querySelectorAll('button[aria-label*="Stop" i]')]
                            .find(x => x.offsetParent !== null);
                          if (button) button.click();
                          return !!button;
                        })()"""
                    )
                    time.sleep(3)
            except Exception:
                if page is not None:
                    page.close()
                page = None
            continue
    if page is not None:
        page.close()
    summary = {name: state["products"].get(name, {}).get("status", "pending") for name in PRODUCTS}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if all(value == "promoted" for value in summary.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
