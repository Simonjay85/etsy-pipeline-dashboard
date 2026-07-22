"""
run_translate_only.py
─────────────────────
Chỉ chạy phần dịch MLX AI (không mở Chrome).
Kết quả lưu vào translations_cache.json để kiểm tra trước.
"""
import json, csv, time, urllib.request
from pathlib import Path

# ─── Config ────────────────────────────────────────────────────────────────────
CSV_PATH   = "/Users/aaronnguyen/Downloads/EtsyListingsDownload.csv"
CACHE_FILE = Path("/Users/aaronnguyen/Documents/Claude/Projects/Etsy/translations_cache.json")
MLX_URL    = "http://localhost:8000"

TARGET_LANGS = ["Dutch", "French", "German", "Italian", "Japanese", "Polish", "Portuguese", "Russian", "Spanish"]

# ─── Read CSV ──────────────────────────────────────────────────────────────────
def read_csv(path: str) -> list[dict]:
    listings = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            title = (row.get("TITLE") or "").strip()
            desc  = (row.get("DESCRIPTION") or "").strip()
            if title:
                listings.append({"title": title, "description": desc})
    return listings

# ─── MLX Call ──────────────────────────────────────────────────────────────────
def call_mlx(prompt: str, max_tokens: int) -> str:
    payload = json.dumps({
        "model": "default",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "enable_thinking": False,
    }).encode()
    req = urllib.request.Request(
        f"{MLX_URL}/v1/chat/completions", data=payload, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]

def translate_title(title: str, lang: str) -> str:
    prompt = (
        f"Translate this Etsy product title to {lang}.\n"
        f"Rules:\n"
        f"- Keep pipe separators ( | ) between keyword phrases.\n"
        f"- MAXIMUM 140 characters total — the translation MUST fit within 140 characters. If needed, shorten phrases but keep all keywords.\n"
        f"- Do NOT cut mid-word or mid-phrase.\n\n"
        f"English: {title}\n\n"
        f'Return ONLY JSON: {{"title": "translated title"}}'
    )
    content = call_mlx(prompt, max_tokens=250)
    s = content.find("{"); e = content.rfind("}") + 1
    result = json.loads(content[s:e]).get("title", "")
    # Safety: nếu vẫn dài hơn 140, cắt ở ranh giới từ
    if len(result) > 140:
        result = result[:140].rsplit(' ', 1)[0].rsplit('|', 1)[0].strip()
    return result

def translate_desc(desc: str, lang: str) -> str:
    # Giới hạn ~400 words để tránh timeout
    words = desc.split()
    desc_short = " ".join(words[:400])
    prompt = (
        f"Translate this Etsy product description to {lang}.\n"
        f"IMPORTANT: Preserve ALL newline characters (\\n) exactly as in the original — keep blank lines between paragraphs.\n"
        f"Keep all emojis (✨📌💡🔗✔️⚡❌•), bullet points, and formatting.\n"
        f"Keep any URLs (https://...) unchanged.\n\n"
        f"English:\n{desc_short}\n\n"
        f'Return ONLY JSON: {{"description": "translated text"}}'
    )
    content = call_mlx(prompt, max_tokens=2000)
    s = content.find("{"); e = content.rfind("}") + 1
    return json.loads(content[s:e]).get("description", "")

# ─── Main ──────────────────────────────────────────────────────────────────────
def main():
    listings = read_csv(CSV_PATH)
    cache    = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}

    # Count pending
    pending_titles = 0
    pending_descs  = 0
    for lst in listings:
        key = lst["title"][:80]
        for lang in TARGET_LANGS:
            if "title" not in cache.get(key, {}).get(lang, {}):
                pending_titles += 1
            if "description" not in cache.get(key, {}).get(lang, {}):
                pending_descs += 1

    total = pending_titles + pending_descs
    print("=" * 60)
    print("🌍 Translation Phase (MLX only)")
    print("=" * 60)
    print(f"📋 {len(listings)} listings × {len(TARGET_LANGS)} ngôn ngữ")
    print(f"⏳ Còn lại: {pending_titles} title + {pending_descs} desc = {total} calls")
    if CACHE_FILE.exists():
        print(f"💾 Cache: {CACHE_FILE}")
    print()

    done = 0
    errors = 0

    for i, listing in enumerate(listings):
        key       = listing["title"][:80]
        desc_words = listing["description"].split()

        if key not in cache:
            cache[key] = {}

        print(f"\n[{i+1}/{len(listings)}] {listing['title'][:60]}")

        for lang in TARGET_LANGS:
            if lang not in cache[key]:
                cache[key][lang] = {}

            # ── Title ──────────────────────────────────────────
            if "title" not in cache[key][lang]:
                try:
                    t = translate_title(listing["title"], lang)
                    cache[key][lang]["title"] = t
                    done += 1
                    print(f"  ✅ {lang:12s} title  ({len(t):3d} chars): {t[:50]}")
                    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
                    time.sleep(0.3)
                except Exception as ex:
                    errors += 1
                    print(f"  ❌ {lang:12s} title  ERROR: {ex}")
                    cache[key][lang]["title"] = ""
            else:
                done += 1
                t = cache[key][lang]["title"]
                print(f"  ✓  {lang:12s} title  (cached): {t[:50]}")

            # ── Description ────────────────────────────────────
            if "description" not in cache[key][lang]:
                try:
                    d = translate_desc(listing["description"], lang)
                    cache[key][lang]["description"] = d
                    done += 1
                    print(f"  ✅ {lang:12s} desc   ({len(d):4d} chars)")
                    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
                    time.sleep(0.3)
                except Exception as ex:
                    errors += 1
                    print(f"  ❌ {lang:12s} desc   ERROR: {ex}")
                    cache[key][lang]["description"] = ""
            else:
                done += 1
                print(f"  ✓  {lang:12s} desc   (cached)")

    # Final save
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))

    print("\n" + "=" * 60)
    print(f"✅ Hoàn thành! {done} thành công, {errors} lỗi")
    print(f"💾 Cache: {CACHE_FILE}")
    print(f"📊 File size: {CACHE_FILE.stat().st_size / 1024:.1f} KB")
    print()
    print("Kiểm tra kết quả:")
    print(f"  cat {CACHE_FILE} | python3 -m json.tool | head -60")
    print()
    print("Nếu OK, chạy tiếp phần điền Etsy:")
    print(f"  python3 /Users/aaronnguyen/Documents/Claude/Projects/Etsy/etsy_translate.py")

if __name__ == "__main__":
    main()
