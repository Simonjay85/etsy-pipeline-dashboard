# Creative Fabrica → ChatGPT → Etsy Bot

Bot canonical: `creativeca_etsy_bot.py`

## Luồng

1. Mở URL sản phẩm Creative Fabrica trong Chrome CDP đã đăng nhập và tải ZIP + ảnh tham chiếu.
2. Giữ ZIP/ảnh nguồn trong `source_assets/`; không bao giờ upload source ZIP thành file giao cho khách.
3. Nếu ZIP vượt giới hạn composer, bot tự tạo `chatgpt-source-evidence.zip` gồm manifest/hash ZIP canonical + preview đã xác minh; ZIP gốc vẫn authoritative và không bị thay thế.
4. Dùng **Chat Images 2.5** tại `https://chatgpt.com/images/`, không dùng Work/project. Upload đúng attachment names, gửi prompt nguyên văn, chờ Chat redirect sang conversation thật, đếm ID mới trong conversation (không đếm Library thumbnails), rồi tải/kiểm tra HTTP/PNG/hash/kích thước.
5. Tạo contact sheet để review trực quan; near-duplicate/layout lặp hoặc nội dung không bám nguồn phải dừng stage-only, không tạo listing.
6. Tạo thư mục sản phẩm mới dưới `shops/daisyflowdigital/product-NNN`, copy đúng customer PDF/ZIP vào `files/`, ghi license receipt và pipeline manifest.
7. Backup workbook, thêm dòng listing, claim JobStore, gọi poster Etsy hiện có bằng đúng `row:folder`, rồi đọc lại receipt/listing URL.
8. Mặc định dừng ở **draft đã xác minh**. Chỉ publish live khi truyền `--publish --confirm-publish daisyflowdigital`; sau đó bot đọc lại public URL và title.

## Chuẩn bị một sản phẩm

Tạo 4 file local:

- `license.json`: phải có `source_url` đúng URL sản phẩm, `license`, `resale_allowed: true`.
- `listing.json`: title, description có URL evidence, price, đúng 13 tag Etsy duy nhất (mỗi tag ≤20 ký tự), qty/category/section/when_made.
- `prompt.txt`: prompt đầy đủ muốn gửi cho ChatGPT; bot kiểm tra prompt trong composer khớp nguyên văn trước khi gửi.
- `customer.pdf` hoặc `customer.zip`: file cuối giao cho người mua, không phải ZIP nguồn Creative Fabrica nếu hai file khác nhau.

Chrome cần có:

- Creative Fabrica session/CDP: `--cf-cdp-url http://127.0.0.1:<port>`.
- DaisyFlow **Chat Images 2.5** session/CDP: mặc định `http://127.0.0.1:9333`; route Chat `https://chatgpt.com/images/`, không dùng route Work `/project`.
- Etsy session cho `daisyflowdigital` theo cấu hình local.

## Chạy kiểm tra không side effect

```bash
cd /Users/aaronnguyen/Developer/Etsy
~/Developer/Etsy/.venv/bin/python creativeca_etsy_bot.py \
  --shop daisyflowdigital \
  --source-url 'https://www.creativefabrica.com/product/SLUG/' \
  --prompt-file /path/prompt.txt \
  --listing-json /path/listing.json \
  --license-json /path/license.json \
  --customer-file /path/customer.pdf \
  --dry-run
```

## Chạy tạo draft đã xác minh

```bash
~/Developer/Etsy/.venv/bin/python creativeca_etsy_bot.py \
  --shop daisyflowdigital \
  --source-url 'https://www.creativefabrica.com/product/SLUG/' \
  --cf-cdp-url 'http://127.0.0.1:<creative-fabrica-port>' \
  --chatgpt-cdp-url 'http://127.0.0.1:9333' \
  --prompt-file /path/prompt.txt \
  --listing-json /path/listing.json \
  --license-json /path/license.json \
  --customer-file /path/customer.pdf
```

## Publish live (chỉ sau khi draft/readback đã đạt)

Thêm:

```bash
--publish --confirm-publish daisyflowdigital
```

Mỗi run có checkpoint và receipt tại:
`output/creativeca_bot/<run-id>/`.

Nếu source đã tải trước, dùng `--source-file /path/source.zip` để bỏ qua bước download Creative Fabrica; license vẫn phải được lưu để chứng minh quyền resale.
