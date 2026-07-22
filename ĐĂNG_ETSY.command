#!/bin/bash
# Double-click để đăng sản phẩm lên Etsy (sau khi đã cài lần đầu)

cd "$(dirname "$0")"
python3 etsy_auto_post.py

echo ""
echo "Hoàn tất! Nhấn Enter để đóng."
read
