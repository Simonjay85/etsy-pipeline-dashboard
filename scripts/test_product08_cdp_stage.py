from __future__ import annotations
import json
import re
import time
import urllib.request
from pathlib import Path
from websockets.sync.client import connect
import bubble_redesign_57 as worker

PRODUCT = "product-08"
product_dir = worker.SHOP_ROOT / PRODUCT
source_files = sorted((product_dir / "images").glob("*.png"))
if len(source_files) != 10:
    raise SystemExit(f"expected 10 source PNGs, found {len(source_files)}")
prompt = worker.product_prompt(product_dir, worker.PROMPT_PATH.read_text(encoding="utf-8"))


def browser_ws_url():
    with urllib.request.urlopen("http://127.0.0.1:9333/json/version", timeout=10) as r:
        return json.load(r)["webSocketDebuggerUrl"]


def page_targets():
    with urllib.request.urlopen("http://127.0.0.1:9333/json/list", timeout=10) as r:
        return json.load(r)


class CDP:
    def __init__(self, url):
        self.ws = connect(url, max_size=50 * 1024 * 1024)
        self.next_id = 0

    def call(self, method, params=None):
        self.next_id += 1
        ident = self.next_id
        self.ws.send(json.dumps({"id": ident, "method": method, "params": params or {}}))
        while True:
            result = json.loads(self.ws.recv())
            if result.get("id") == ident:
                if "error" in result:
                    raise RuntimeError(f"{method}: {result['error']}")
                return result.get("result", {})

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        return result.get("result", {}).get("value")

    def close(self):
        self.ws.close()


page_target = next(
    (target for target in page_targets() if target.get("type") == "page" and target.get("url", "").startswith("https://chatgpt.com/")),
    None,
)
if not page_target or not page_target.get("webSocketDebuggerUrl"):
    raise SystemExit("existing ChatGPT page target unavailable")
page = CDP(page_target["webSocketDebuggerUrl"])
try:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if page.evaluate("!!document.querySelector('#prompt-textarea')"):
            break
        time.sleep(2)
    else:
        raise SystemExit("prompt editor unavailable")

    page.evaluate("document.querySelector('button[data-testid=\"composer-plus-btn\"]')?.click()")
    time.sleep(2)
    clicked = page.evaluate("""(() => {
      const nodes = [...document.querySelectorAll('*')].filter(x =>
        (x.textContent || '').trim() === 'Create image' && x.offsetParent !== null
      )
      const node = nodes[nodes.length - 1]
      if (!node) return false
      node.click()
      return true
    })()""")
    if not clicked:
        raise SystemExit("Create image menu item unavailable")
    time.sleep(2)
    root = page.call("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
    input_node = page.call("DOM.querySelector", {"nodeId": root, "selector": "#upload-files"}).get("nodeId")
    if not input_node:
        raise SystemExit("upload input unavailable")
    page.call("DOM.setFileInputFiles", {"nodeId": input_node, "files": [str(x) for x in source_files]})
    uploaded = page.evaluate("document.querySelector('#upload-files')?.files?.length || 0")
    if uploaded != 10:
        raise SystemExit(f"upload verification failed: {uploaded}")
    page.evaluate("document.querySelector('#prompt-textarea')?.focus()")
    page.call("Input.insertText", {"text": prompt})
    entered = page.evaluate("""(() => {
      const x = document.querySelector('#prompt-textarea');
      return x ? (('value' in x) ? x.value : (x.innerText || x.textContent || '')) : ''
    })()""")
    if re.sub(r"\s+", "", entered) != re.sub(r"\s+", "", prompt):
        raise SystemExit("prompt verification failed")
    sent = page.evaluate("""(() => {
      const b = [...document.querySelectorAll('button[aria-label="Send prompt"]')].at(-1)
      if (!b) return false
      b.click(); return true
    })()""")
    if not sent:
        raise SystemExit("send button unavailable")
    print("SENT product-08 single-request", flush=True)

    end = time.monotonic() + 900
    generated_urls = []
    while time.monotonic() < end:
        generated_urls = page.evaluate("""() => {
          const out = [], seen = new Set();
          for (const img of [...document.querySelectorAll('img')]) {
            if (!(img.alt || '').toLowerCase().includes('generated image')) continue;
            const src = img.currentSrc || img.src || '';
            const m = src.match(/[?&]id=([^&]+)/);
            if (m && src.includes('/backend-api/estuary/content') && !seen.has(m[1])) {
              seen.add(m[1]); out.push(src);
            }
          }
          return out;
        }""") or []
        all_count = page.evaluate("() => [...document.images].filter(x => (x.currentSrc || x.src || '').includes('/backend-api/estuary/content')).length")
        stop = page.evaluate("""() => [...document.querySelectorAll('button[aria-label*="Stop" i]')].some(x => x.offsetParent !== null)""")
        print(f"poll generated={len(generated_urls)} all_estuary={all_count} stop={stop}", flush=True)
        if len(generated_urls) == 10:
            break
        time.sleep(5)
    if len(generated_urls) != 10:
        raise SystemExit(f"generation did not expose exactly 10 generated images: {len(generated_urls)}")
    run_dir = worker.REPO_ROOT / "output" / "bubble_redesign_product08_test" / f"run-{int(time.time())}"
    run_dir.mkdir(parents=True, exist_ok=False)
    for index, url in enumerate(generated_urls, 1):
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=180) as response:
            data = response.read()
        if len(data) < 10_000:
            raise SystemExit(f"image {index} too small")
        (run_dir / f"etsy_{index:02d}.png").write_bytes(data)
    digest = worker.validate_set(sorted(run_dir.glob("*.png")))
    print(f"STAGED {run_dir} set_sha256={digest}", flush=True)
finally:
    page.close()
