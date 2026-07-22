#!/bin/bash
echo "================================================"
echo "  Mở Chrome với Debug Port cho Etsy Auto Post"
echo "================================================"
echo ""
echo "▶ Đóng Chrome cũ..."
pkill -x "Google Chrome" 2>/dev/null
sleep 2

echo "▶ Mở Chrome mới (profile riêng cho Etsy debug)..."
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/chrome_etsy_debug" \
  --no-first-run \
  "https://www.etsy.com/your/listings" 2>/dev/null &

echo ""
echo "✅ Chrome đang mở..."
echo ""
echo "⚠️  NẾU thấy trang Etsy Shop Manager → đã sẵn sàng!"
echo "⚠️  NẾU thấy trang login → đăng nhập Etsy trước, rồi mới chạy script"
echo ""
echo "Sau khi Chrome load xong, mở Terminal mới và chạy:"
echo "  python3 ~/Documents/Claude/Projects/Etsy/etsy_auto_post.py"
echo ""
echo "⚠️  Đừng đóng cửa sổ này!"
wait
