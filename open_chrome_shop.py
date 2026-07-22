#!/usr/bin/env python3
"""
Mở Chrome debug với đúng profile cho từng shop Etsy.
Dùng: python3 open_chrome_shop.py [shop_id]
Ví dụ: python3 open_chrome_shop.py daisyflowdigital
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "shops_config.json"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

def load_shops():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return {}

def kill_chrome_on_port(port: int):
    """Kill Chrome instance using this debug port if any."""
    result = subprocess.run(
        ["lsof", "-ti", f"tcp:{port}"],
        capture_output=True, text=True
    )
    pids = result.stdout.strip().split()
    for pid in pids:
        try:
            subprocess.run(["kill", "-9", pid])
        except:
            pass

def open_chrome(shop_id: str):
    shops = load_shops()
    if shop_id not in shops:
        print(f"❌ Shop '{shop_id}' không tìm thấy trong shops_config.json")
        print(f"   Danh sách: {', '.join(shops.keys())}")
        sys.exit(1)

    cfg  = shops[shop_id]
    name = cfg.get("name", shop_id)
    port = int(cfg.get("debug_port", 9222))
    raw_session = cfg.get("browser_session", "~/.etsy_browser_session")
    session_dir = raw_session.replace("~", str(Path.home()))

    # Tạo thư mục session nếu chưa có
    Path(session_dir).mkdir(parents=True, exist_ok=True)

    # Xoá singleton lock cũ
    for lock in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        p = Path(session_dir) / lock
        p.unlink(missing_ok=True)

    # Kiểm tra port đang dùng
    check = subprocess.run(["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True)
    if check.stdout.strip():
        print(f"⚠️  Port {port} đang bận — có thể Chrome cho {name} đã mở rồi.")
        ans = input("   Muốn mở cửa sổ mới? (y/n): ").strip().lower()
        if ans != "y":
            print("✅ Giữ nguyên Chrome cũ. Dùng cửa sổ đang mở để đăng nhập.")
            return

    print(f"")
    print(f"{'='*55}")
    print(f"  🌐 Mở Chrome cho shop: {cfg.get('emoji','')} {name}")
    print(f"  🔑 Session: {session_dir}")
    print(f"  🔌 Debug port: {port}")
    print(f"{'='*55}")

    subprocess.Popen([
        CHROME,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={session_dir}",
        "--no-first-run",
        "--disable-blink-features=AutomationControlled",
        "https://www.etsy.com/your/listings"
    ], stderr=subprocess.DEVNULL)

    # Chờ Chrome khởi động
    print(f"\n⏳ Đang chờ Chrome khởi động...")
    for i in range(10):
        time.sleep(1)
        result = subprocess.run(
            ["curl", "-s", f"http://localhost:{port}/json/version"],
            capture_output=True, text=True
        )
        if result.returncode == 0 and "Browser" in result.stdout:
            print(f"✅ Chrome sẵn sàng! (port {port})")
            break
        print(f"   ... {i+1}s")
    else:
        print(f"⚠️  Chrome mở rồi nhưng chưa phản hồi debug port.")

    print(f"\n📋 Hướng dẫn:")
    print(f"   1. Đăng nhập tài khoản Etsy cho '{name}'")
    print(f"   2. Vào Shop Manager để xác nhận đúng shop")
    print(f"   3. Session sẽ được LƯU lại — lần sau không cần đăng nhập lại")
    print(f"\n   Để chạy poster cho shop này:")
    print(f"   python3 etsy_auto_post.py --shop {shop_id}")

def main():
    shops = load_shops()

    if len(sys.argv) < 2:
        print("📋 Danh sách shops:")
        for sid, cfg in shops.items():
            port = cfg.get("debug_port", "?")
            name = cfg.get("name", sid)
            emoji = cfg.get("emoji", "🏪")
            print(f"   {emoji} {sid:25s} → port {port} | {name}")
        print(f"\nDùng: python3 {Path(__file__).name} <shop_id>")
        sys.exit(0)

    open_chrome(sys.argv[1])

if __name__ == "__main__":
    main()
