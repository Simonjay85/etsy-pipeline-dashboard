import asyncio
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

from playwright.async_api import async_playwright


CDP = "http://127.0.0.1:41822"
DASH = "http://127.0.0.1:8090"
MONITOR_LABEL = "com.user.etsy-single-sync-fix"
RETRIES = [
    (8, "product-280", "4419825830"),
    (9, "product-281", "4420005648"),
    (10, "product-282", "4421467312"),
]


async def dashboard_state():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(CDP, timeout=10_000)
        for context in browser.contexts:
            for page in context.pages:
                if page.url.rstrip("/") == DASH:
                    return await page.evaluate(
                        """() => ({
                            bulk: typeof etsyBulkSyncInFlight !== 'undefined'
                                ? etsyBulkSyncInFlight : null,
                            single: typeof etsySingleSyncInFlight !== 'undefined'
                                ? etsySingleSyncInFlight : null,
                            progress: document.getElementById('local-batch-pull-btn')?.innerText || ''
                        })"""
                    )
    return None


def request_json(url, payload=None, timeout=180):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode())


async def main():
    idle_seen = 0
    while True:
        try:
            state = await dashboard_state()
        except Exception as exc:
            print(time.strftime("%H:%M:%S"), "monitor-error", type(exc).__name__, str(exc), flush=True)
            state = None
        print(time.strftime("%H:%M:%S"), "state", state, flush=True)
        if state and state.get("bulk") is False and state.get("single") is False:
            idle_seen += 1
            if idle_seen >= 2:
                break
        else:
            idle_seen = 0
        await asyncio.sleep(15)

    label = f"gui/{os.getuid()}/com.user.etsy-dashboard"
    print("idle-confirmed restart", label, flush=True)
    subprocess.run(["launchctl", "kickstart", "-k", label], check=True)

    deadline = time.monotonic() + 45
    session = None
    while time.monotonic() < deadline:
        try:
            status, session = request_json(f"{DASH}/api/etsy/session", timeout=3)
            if status == 200 and session.get("session", {}).get("shop_id") == "templystudios":
                break
        except Exception:
            pass
        await asyncio.sleep(1)
    else:
        raise RuntimeError("dashboard restart verification timed out")
    print("restart-verified", session, flush=True)

    for row, folder, listing_id in RETRIES:
        payload = {
            "shop": "templystudios",
            "folder": folder,
            "listing_id": listing_id,
        }
        try:
            status, body = await asyncio.to_thread(
                request_json,
                f"{DASH}/api/products/{row}/sync-from-etsy",
                payload,
                240,
            )
            print("retry-result", row, folder, listing_id, status, body, flush=True)
        except urllib.error.HTTPError as exc:
            print("retry-http-error", row, exc.code, exc.read().decode(errors="replace"), flush=True)
        await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        print("monitor-finished; removing one-shot launchctl job", flush=True)
        subprocess.run(["launchctl", "remove", MONITOR_LABEL], check=False)
