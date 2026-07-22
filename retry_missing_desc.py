"""
Retry missing descriptions with robust JSON extraction.
Dùng regex để tránh JSON parse error khi description có ký tự đặc biệt.
"""
import json, re, urllib.request, csv
from pathlib import Path

MLX_URL  = "http://localhost:8000"
CACHE    = Path("translations_cache.json")
CSV_PATH = Path("/Users/aaronnguyen/Downloads/EtsyListingsDownload.csv")

# ── Đọc desc gốc từ CSV ──────────────────────────────────────────────
orig = {}
with open(CSV_PATH, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        t = row.get("TITLE", "").strip()
        d = row.get("DESCRIPTION", "").strip()
        if t and d:
            orig[t[:80]] = d

cache = json.loads(CACHE.read_text())

def call_mlx(prompt: str, max_tokens=800) -> str:
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

def extract_desc(content: str) -> str:
    """Dùng regex để lấy phần description, tránh JSON broken."""
    # Thử json parse trước
    try:
        s = content.find("{"); e = content.rfind("}") + 1
        return json.loads(content[s:e]).get("description", "")
    except Exception:
        pass
    # Fallback: regex lấy value của "description": "..."
    m = re.search(r'"description"\s*:\s*"([\s\S]+?)"\s*\}', content)
    if m:
        return m.group(1).replace("\\n", "\n").replace('\\"', '"')
    # Fallback 2: lấy mọi text sau "description":
    m2 = re.search(r'"description"\s*:\s*"([\s\S]+)', content)
    if m2:
        raw = m2.group(1)
        # Cắt đến dấu " cuối (loại escape)
        result = []
        i = 0
        while i < len(raw):
            c = raw[i]
            if c == "\\" and i+1 < len(raw):
                nc = raw[i+1]
                if nc == "n": result.append("\n"); i += 2; continue
                if nc == '"': result.append('"'); i += 2; continue
                if nc == "\\": result.append("\\"); i += 2; continue
            if c == '"':  # kết thúc
                break
            result.append(c)
            i += 1
        return "".join(result).strip()
    return ""

# ── Tìm entries thiếu desc ───────────────────────────────────────────
missing = [(key, lang)
           for key in cache
           for lang in cache[key]
           if not cache[key][lang].get("description", "").strip()]

print(f"Retry {len(missing)} entries thiếu description...\n")
done = errors = 0

for key, lang in missing:
    eng_desc = orig.get(key, "")
    if not eng_desc:
        print(f"  ⚠ Không có desc gốc: {key[:50]}")
        continue

    # Giới hạn 180 words để tránh timeout
    short_desc = " ".join(eng_desc.split()[:180])

    try:
        prompt = (
            f"Translate this product description to {lang}.\n"
            f"Keep all emojis and bullet points (•). Keep URLs unchanged.\n"
            f"Respond with a valid JSON object: {{\"description\": \"...\"}}\n\n"
            f"English:\n{short_desc}"
        )
        content = call_mlx(prompt, max_tokens=900)
        result = extract_desc(content)
        if result.strip():
            cache[key][lang]["description"] = result
            done += 1
            print(f"  ✅ {lang:12}: {key[:40]} ({len(result)} chars)")
        else:
            errors += 1
            print(f"  ❌ {lang:12}: empty — {key[:40]}")
    except Exception as ex:
        errors += 1
        print(f"  ❌ {lang:12}: {ex}")

CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
print(f"\n✅ Done {done}/{len(missing)} | Lỗi: {errors}")
print(f"Cache: {CACHE.stat().st_size // 1024} KB")
