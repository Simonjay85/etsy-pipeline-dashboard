"""
together_qwen.py
─────────────────
Module để gọi Qwen3.7-Max API qua Together.ai

Together API docs: https://docs.together.ai/reference/chat-completions
Model: Qwen/Qwen3.7-Max
"""
import json
import urllib.request
import urllib.error
import os

# ─── Config ────────────────────────────────────────────────────────────────────
TOGETHER_API_KEY = os.environ.get("TOGETHER_API_KEY", "")
TOGETHER_API_URL = "https://api.together.xyz/v1/chat/completions"
MODEL_NAME = "Qwen/Qwen3.7-Max"

# ─── Qwen API Client ────────────────────────────────────────────────────────────
def call_qwen(prompt: str, system_prompt: str = "", max_tokens: int = 2000, temperature: float = 0.2) -> str:
    """
    Gọi Qwen3.7-Max API qua Together.ai
    
    Args:
        prompt: User prompt
        system_prompt: System instruction (optional)
        max_tokens: Max tokens to generate
        temperature: Sampling temperature (0.0-1.0)
    
    Returns:
        Generated text response
    """
    if not TOGETHER_API_KEY:
        raise ValueError("TOGETHER_API_KEY not set. Please set environment variable.")
    
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    
    payload = json.dumps({
        "model": MODEL_NAME,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": 0.9,
        "repetition_penalty": 1.1,
    }).encode("utf-8")
    
    req = urllib.request.Request(
        TOGETHER_API_URL,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TOGETHER_API_KEY}",
        }
    )
    
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        return content
    except urllib.error.HTTPError as e:
        error_body = e.read() if e.fp else ""
        raise Exception(f"API Error {e.code}: {error_body}")
    except Exception as e:
        raise Exception(f"API call failed: {e}")


# ─── Translation Functions ──────────────────────────────────────────────────────
def translate_title(title: str, target_lang: str) -> str:
    """Dịch tiêu đề sản phẩm sang ngôn ngữ target."""
    system_prompt = "You are a professional translator for Etsy product listings."
    prompt = (
        f"Translate this Etsy product title to {target_lang}.\n"
        f"Rules:\n"
        f"- Keep pipe separators ( | ) between keyword phrases.\n"
        f"- MAXIMUM 140 characters total.\n"
        f"- Do NOT cut mid-word or mid-phrase.\n"
        f"- Return ONLY the translated title, no JSON, no quotes.\n\n"
        f"English title: {title}"
    )
    result = call_qwen(prompt, system_prompt, max_tokens=250, temperature=0.2)
    return result.strip()


def translate_description(description: str, target_lang: str) -> str:
    """Dịch mô tả sản phẩm sang ngôn ngữ target."""
    system_prompt = "You are a professional translator for Etsy product descriptions."
    prompt = (
        f"Translate this Etsy product description to {target_lang}.\n"
        f"IMPORTANT: Preserve ALL newline characters, blank lines between paragraphs.\n"
        f"Keep all emojis, bullet points (•), and formatting structure.\n"
        f"Keep store links unchanged.\n"
        f"Return ONLY the translated description.\n\n"
        f"English description:\n{description}"
    )
    result = call_qwen(prompt, system_prompt, max_tokens=2000, temperature=0.2)
    return result.strip()


# ─── SEO & Tags Functions ───────────────────────────────────────────────────────
def generate_tags(title: str, description: str, num_tags: int = 13) -> list[str]:
    """Generate SEO tags cho Etsy listing."""
    system_prompt = "You are an Etsy SEO expert. Generate high-converting tags."
    prompt = (
        f"Generate {num_tags} SEO tags for this Etsy product.\n"
        f"Rules:\n"
        f"- Each tag max 20 characters.\n"
        f"- Use relevant keywords for Etsy search.\n"
        f"- Return as comma-separated list, no extra text.\n\n"
        f"Title: {title}\n"
        f"Description: {description[:500]}"
    )
    result = call_qwen(prompt, system_prompt, max_tokens=300, temperature=0.3)
    tags = [t.strip()[:20] for t in result.split(",") if t.strip()]
    return tags[:num_tags]


def generate_seo_description(title: str, features: list[str] | None = None) -> str:
    """Generate mô tả sản phẩm chuẩn SEO."""
    system_prompt = "You are an Etsy SEO copywriter. Write compelling, keyword-rich descriptions."
    features_text = "\n".join(features) if features else ""
    prompt = (
        f"Write an Etsy product description (SEO optimized) for this product.\n"
        f"Include: features, use cases, dimensions, digital delivery info.\n"
        f"Use bullet points and clear sections.\n\n"
        f"Title: {title}\n"
        f"Key features:\n{features_text}"
    )
    result = call_qwen(prompt, system_prompt, max_tokens=1500, temperature=0.4)
    return result.strip()


# ─── Test ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("Testing Qwen3.7-Max via Together.ai API")
    print("=" * 60)
    
    # Test API key
    if not TOGETHER_API_KEY:
        print("\n❌ TOGETHER_API_KEY not set!")
        print("   Set env var: export TOGETHER_API_KEY='your_key_here'")
        exit(1)
    print(f"\n✅ API Key loaded (length: {len(TOGETHER_API_KEY)})")
    
    # Test translation
    test_title = "Digital Planner for iPad | GoodNotes Planner | Hyperlinked Tabs"
    print(f"\n📝 Original title: {test_title}")
    
    try:
        translated = translate_title(test_title, "Spanish")
        print(f"✅ Spanish translation: {translated}")
    except Exception as e:
        print(f"❌ Translation error: {e}")
    
    # Test tags
    test_desc = "Digital planner for iPad and Android tablets. Compatible with GoodNotes, Notability, Xodo. Includes hyperlinked tabs, monthly/weekly/daily pages, stickers."
    print(f"\n🔖 Generating tags for: {test_title[:50]}...")
    try:
        tags = generate_tags(test_title, test_desc)
        print(f"✅ Generated {len(tags)} tags: {tags}")
    except Exception as e:
        print(f"❌ Tags error: {e}")
    
    print("\n" + "=" * 60)