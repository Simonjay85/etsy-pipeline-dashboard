#!/bin/bash
# Double-click file này để cài và chạy tự động

echo "================================================"
echo "  Etsy Auto Poster — Cài đặt lần đầu"
echo "================================================"
echo ""

# Cài pip packages
echo "▶ Cài thư viện Python..."
pip3 install playwright openpyxl --break-system-packages --quiet
echo "▶ Cài Chromium cho Playwright..."
python3 -m playwright install chromium

echo ""
echo "================================================"
echo "  Bắt đầu đăng sản phẩm lên Etsy..."
echo "================================================"
echo ""

# Chạy script
cd "$(dirname "$0")"
python3 etsy_auto_post.py

echo ""
echo "Hoàn tất! Nhấn Enter để đóng."
read
