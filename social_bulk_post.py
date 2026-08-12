#!/usr/bin/env python3
"""
Social Media Bulk Auto Poster Helper
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Hỗ trợ đăng bài hàng loạt từ Excel lên các mạng xã hội
• Tự động nghỉ 180 giây giữa các lần đăng thành công để tránh bị nhận diện là bot
• Nếu hàng bị bỏ qua hoặc lỗi, tự động chuyển hàng tiếp theo nhanh chóng (chờ 5 giây)
• Chạy: python3 social_bulk_post.py --shop <SHOP_ID> --platform <PLATFORM> --start <START_ROW> --end <END_ROW> [--delay 180]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import asyncio
import sys
import argparse
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).parent

async def run_single_post(shop: str, platform: str, row: int) -> int:
    """Chạy script social_auto_post.py cho một dòng cụ thể."""
    script_path = str(BASE_DIR / "social_auto_post.py")
    python_bin = sys.executable
    
    cmd = [python_bin, "-u", script_path, "--row", str(row), "--platform", platform, "--shop", shop]
    
    print(f"\n{'─'*60}")
    print(f"📦 DÒNG {row} | Bắt đầu đăng lên {platform.upper()}...")
    print(f"{'─'*60}")
    
    # Chạy và in log trực tiếp
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(BASE_DIR)
    )
    
    if proc.stdout:
        async for line in proc.stdout:
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                print(f"  [AutoPost] {text}")
                
    code = await proc.wait()
    return code

async def main():
    parser = argparse.ArgumentParser(description="Social Media Bulk Auto Poster Helper")
    parser.add_argument("--shop", type=str, required=True, help="ID của shop (ví dụ: templystudios)")
    parser.add_argument("--platform", type=str, required=True, choices=["pinterest", "twitter", "medium"], help="Nền tảng muốn đăng")
    parser.add_argument("--start", type=int, required=True, help="Dòng bắt đầu trong Excel (ví dụ: 4)")
    parser.add_argument("--end", type=int, required=True, help="Dòng kết thúc trong Excel (ví dụ: 10)")
    parser.add_argument("--delay", type=int, default=180, help="Thời gian chờ giữa các lần đăng thành công (giây, mặc định 180)")
    
    args = parser.parse_args()
    
    print(f"\n{'='*70}")
    print(f"  🚀 KHỞI ĐỘNG TIẾN TRÌNH ĐĂNG BÀI HÀNG LOẠT (BULK AUTO POST)")
    print(f"  📦 Shop: {args.shop} | Platform: {args.platform.upper()}")
    print(f"  📊 Hàng từ: {args.start} ➔ {args.end}")
    print(f"  ⏳ Thời gian giãn cách: {args.delay} giây (giãn cách an toàn)")
    print(f"{'='*70}\n")
    
    success_count = 0
    fail_count = 0
    
    for row in range(args.start, args.end + 1):
        exit_code = await run_single_post(args.shop, args.platform, row)
        
        if exit_code == 0:
            success_count += 1
            print(f"\n✅ Đã đăng thành công sản phẩm dòng {row}!")
            
            # Nếu chưa phải dòng cuối cùng, thực hiện giãn cách an toàn
            if row < args.end:
                print(f"⏳ Đang nghỉ giãn cách {args.delay} giây trước sản phẩm tiếp theo để tránh bị nhận diện là bot...")
                await asyncio.sleep(args.delay)
        else:
            fail_count += 1
            print(f"\n⚠ Dòng {row} bỏ qua hoặc gặp lỗi (Exit code: {exit_code}).")
            
            if row < args.end:
                print("⏳ Chuyển nhanh sang hàng tiếp theo sau 5 giây...")
                await asyncio.sleep(5)
                
    print(f"\n{'='*70}")
    print(f"  🎉 TIẾN TRÌNH HOÀN THÀNH!")
    print(f"  ✅ Đăng thành công: {success_count} sản phẩm")
    print(f"  ❌ Gặp lỗi / Bỏ qua: {fail_count} sản phẩm")
    print(f"{'='*70}\n")

if __name__ == "__main__":
    asyncio.run(main())
