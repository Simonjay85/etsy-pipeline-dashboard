"""
Re-translate descriptions paragraph-by-paragraph + translate tags.
Đảm bảo description giữ đúng cấu trúc xuống dòng.
"""
import json, re, urllib.request, csv
from pathlib import Path

MLX_URL  = "http://localhost:8000"
CACHE    = Path("translations_cache.json")
CSV_PATH = Path("/Users/aaronnguyen/Downloads/EtsyListingsDownload.csv")

TARGET_LANGS = ["Dutch","French","German","Italian","Japanese","Polish","Portuguese","Russian","Spanish"]

# ── Đọc data gốc từ CSV ─────────────────────────────────────────────
orig = {}  # key -> {desc_paragraphs: [...], tags: [...]}
with open(CSV_PATH, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        title = row.get("TITLE", "").strip()
        desc  = row.get("DESCRIPTION", "").strip()
        tags_raw = row.get("TAGS", "").strip()
        if not title: continue
        key = title[:80]
        # Tách paragraphs (split by \n, giữ dòng có nội dung)
        paragraphs = [p.strip() for p in desc.split("\n") if p.strip()]
        # Tách tags (dấu phẩy hoặc underscore)
        tags = [t.strip().replace("_", " ") for t in re.split(r"[,|]", tags_raw) if t.strip()][:13]
        orig[key] = {"paragraphs": paragraphs, "tags": tags}

cache = json.loads(CACHE.read_text())

def call_mlx(prompt: str, max_tokens=300) -> str:
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
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read())["choices"][0]["message"]["content"]

def translate_para(text: str, lang: str) -> str:
    """Dịch 1 đoạn văn, trả về plain text."""
    prompt = (
        f"Translate this text to {lang}. Keep all emojis, bullet points (✔•), and formatting.\n"
        f"Return ONLY the translated text, no extra commentary.\n\n"
        f"{text}"
    )
    content = call_mlx(prompt, max_tokens=400)
    # Loại bỏ dấu nháy bao quanh nếu có
    content = content.strip().strip('"').strip("'")
    return content

def translate_tag(tag: str, lang: str) -> str:
    """Dịch 1 tag, kết quả phải ≤30 chars."""
    prompt = (
        f"Translate this Etsy product tag to {lang}.\n"
        f"MUST be ≤20 characters total. Return ONLY the translated tag, nothing else.\n\n"
        f"Tag: {tag}"
    )
    content = call_mlx(prompt, max_tokens=50).strip().strip('"').strip("'")
    return content[:20]

# ── Xóa descriptions cũ không có newline ────────────────────────────
cleared = 0
for key in cache:
    for lang in cache[key]:
        d = cache[key][lang].get("description", "")
        if d and "\n" not in d:
            cache[key][lang]["description"] = ""
            cleared += 1
print(f"Đã xóa {cleared} desc thiếu newline → re-translate\n")

# ── Đếm pending ─────────────────────────────────────────────────────
pending_desc = 0; pending_tags = 0
for key in cache:
    for lang in TARGET_LANGS:
        entry = cache[key].get(lang, {})
        if not entry.get("description", "").strip(): pending_desc += 1
        if not entry.get("tags"): pending_tags += 1

print(f"Cần dịch: {pending_desc} descriptions + {pending_tags} tag-sets\n")

done_d = done_t = err = 0

for listing_idx, (key, data) in enumerate(orig.items()):
    paragraphs = data["paragraphs"]
    tags_en    = data["tags"]

    for lang in TARGET_LANGS:
        if key not in cache:
            cache[key] = {}
        if lang not in cache[key]:
            cache[key][lang] = {}

        entry = cache[key][lang]

        # ── Description: dịch từng đoạn ──
        if not entry.get("description", "").strip():
            trans_paras = []
            ok = True
            for para in paragraphs:
                if not para.strip():
                    trans_paras.append("")
                    continue
                try:
                    tp = translate_para(para, lang)
                    trans_paras.append(tp)
                except Exception as ex:
                    print(f"  ❌ desc-para {lang} [{key[:30]}]: {ex}")
                    ok = False
                    break

            if ok and trans_paras:
                result = "\n".join(trans_paras)
                entry["description"] = result
                done_d += 1
                print(f"  ✅ desc {lang:12}: [{key[:35]}] ({len(result)} chars, {len(trans_paras)} paras)")

        # ── Tags: dịch từng tag ──
        if not entry.get("tags"):
            trans_tags = []
            for tag in tags_en:
                try:
                    tt = translate_tag(tag, lang)
                    if tt:
                        trans_tags.append(tt)
                except Exception as ex:
                    print(f"  ❌ tag {lang} [{tag}]: {ex}")
            if trans_tags:
                entry["tags"] = trans_tags
                done_t += 1
                print(f"  ✅ tags {lang:12}: [{key[:35]}] → {trans_tags[:3]}...")

    # Lưu cache sau mỗi listing
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
    print(f"  💾 Saved [{listing_idx+1}/{len(orig)}] {key[:40]}")

CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
print(f"\n{'='*60}")
print(f"✅ Done: {done_d} descs + {done_t} tag-sets | Lỗi: {err}")
print(f"Cache: {CACHE.stat().st_size // 1024} KB")
