"""Central resilient translation service for Etsy listing workflows.

Provider order is configurable with ETSY_TRANSLATION_PROVIDERS. The default
uses Together AI when its key is configured and keeps no-key web translators
only as fallbacks:

    together,google_web,mymemory

Supported credentials:
- TOGETHER_API_KEY

Optional Together AI override:
- ETSY_TRANSLATION_TOGETHER_MODEL (default: deepseek-ai/DeepSeek-V4.1-Flash)

Translations are cached in a local SQLite file under .etsy-dashboard-diagnostics
so repeated listing repairs/posts do not repeatedly hit external services.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit
import urllib.request

from deep_translator import GoogleTranslator


LANGUAGE_NAMES = {
    "nl": "Dutch",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "ja": "Japanese",
    "pl": "Polish",
    "pt": "Portuguese",
    "ru": "Russian",
    "es": "Spanish",
    "vi": "Vietnamese",
}

MYMEMORY_CODES = {
    "nl": "nl-NL",
    "fr": "fr-FR",
    "de": "de-DE",
    "it": "it-IT",
    "ja": "ja-JP",
    "pl": "pl-PL",
    "pt": "pt-PT",
    "ru": "ru-RU",
    "es": "es-ES",
    "vi": "vi-VN",
}

_INVALID_MARKERS = (
    "error 500 (server error)",
    "that's an error",
    "that’s an error",
    "there was an error. please try again",
    "no translation was found using the current translator",
    "<!doctype html",
    "<html",
)
_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


class TranslationError(RuntimeError):
    """Raised when all configured translation providers fail safely."""


@dataclass(frozen=True)
class TranslationResult:
    text: str
    provider: str
    cached: bool = False


class TranslationProvider(Protocol):
    name: str

    @property
    def available(self) -> bool: ...

    def translate(self, text: str, source: str, target: str) -> str: ...


DEFAULT_TOGETHER_API_BASE_URL = "https://api.together.xyz/v1"
DEFAULT_TOGETHER_MODEL = "deepseek-ai/DeepSeek-V4.1-Flash"
TOGETHER_MODEL_MAX_LENGTH = 256


def _normalize_together_base_url(value: object) -> str:
    """Return the dashboard-compatible, credential-free API base URL."""
    if value is None or (isinstance(value, str) and not value.strip()):
        value = DEFAULT_TOGETHER_API_BASE_URL
    if not isinstance(value, str):
        raise ValueError("Together base URL must be a valid http(s) URL")

    raw = value.strip().rstrip("/")
    if not raw or any(char.isspace() for char in raw):
        raise ValueError("Together base URL must be a valid http(s) URL")
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        # Accessing .port validates malformed ports such as :not-a-port.
        _ = parsed.port
    except (TypeError, ValueError):
        raise ValueError("Together base URL must be a valid http(s) URL") from None

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Together base URL must be http(s), include a host, and have no credentials")

    path = parsed.path.rstrip("/")
    if path.lower().endswith("/v1"):
        path = path[:-3].rstrip("/")
    canonical_path = f"{path}/v1" if path else "/v1"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, canonical_path, "", ""))


def _together_chat_completion_endpoint() -> str:
    raw_base_url = os.environ.get("TOGETHER_API_BASE_URL", DEFAULT_TOGETHER_API_BASE_URL)
    try:
        base_url = _normalize_together_base_url(raw_base_url)
    except ValueError:
        # Do not let malformed inherited dashboard configuration become a
        # network destination; the normal Together default remains usable.
        base_url = DEFAULT_TOGETHER_API_BASE_URL
    return f"{base_url}/chat/completions"


def _configured_together_model(provider_env: str, default_model: str) -> str:
    """Resolve provider-specific, dashboard SEO, then generic model envs."""
    for env_name in (provider_env, "TOGETHER_SEO_MODEL", "TOGETHER_MODEL"):
        value = os.environ.get(env_name, "").strip()
        if not value or len(value) > TOGETHER_MODEL_MAX_LENGTH or any(
            char in value for char in "\r\n"
        ):
            continue
        return value
    return default_model


def _validate_output(result: object, *, target: str) -> str:
    text = html.unescape(str(result or "")).strip()
    lowered = text.lower()
    if not text:
        raise TranslationError(f"empty translation for {target}")
    if any(marker in lowered for marker in _INVALID_MARKERS):
        raise TranslationError(f"invalid translator response for {target}")
    return text


def _protect_urls(text: str) -> tuple[str, dict[str, str]]:
    mapping: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        token = f"⟪URL{len(mapping)}⟫"
        mapping[token] = match.group(0)
        return token

    return _URL_RE.sub(replace, text), mapping


def _restore_urls(text: str, mapping: dict[str, str]) -> str:
    restored = text
    for token, url in mapping.items():
        restored = restored.replace(token, url)
        # Some providers add spaces inside the sentinel.
        match = re.fullmatch(r"⟪URL(\d+)⟫", token)
        if match:
            restored = re.sub(
                rf"⟪\s*URL\s*{re.escape(match.group(1))}\s*⟫",
                lambda _m, value=url: value,
                restored,
                flags=re.IGNORECASE,
            )
    return restored


def _chunks(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(paragraph) <= max_chars:
            current = paragraph
            continue

        # Very long paragraph: split conservatively on words.
        words = paragraph.split()
        piece = ""
        for word in words:
            candidate = word if not piece else f"{piece} {word}"
            if len(candidate) > max_chars and piece:
                chunks.append(piece)
                piece = word
            else:
                piece = candidate
        if piece:
            current = piece

    if current:
        chunks.append(current)
    return chunks


class TogetherProvider:
    name = "together"
    endpoint = "https://api.together.xyz/v1/chat/completions"
    default_model = DEFAULT_TOGETHER_MODEL
    model_env = "ETSY_TRANSLATION_TOGETHER_MODEL"

    def __init__(self) -> None:
        self.api_key = os.environ.get("TOGETHER_API_KEY", "").strip()
        self.endpoint = _together_chat_completion_endpoint()
        self.model = _configured_together_model(self.model_env, self.default_model)

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.model)

    @staticmethod
    def _message_text(payload: object) -> str:
        if not isinstance(payload, dict):
            raise TranslationError("Together response is not an object")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise TranslationError("Together response has no choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise TranslationError("Together response has no message")
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            joined = "".join(parts).strip()
            if joined:
                return joined
        raise TranslationError("Together response has no text content")

    def translate(self, text: str, source: str, target: str) -> str:
        language_name = LANGUAGE_NAMES.get(target, target)
        source_name = LANGUAGE_NAMES.get(source, source) if source not in {"", "auto"} else "the source language"
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a precise ecommerce translator for Etsy listings. Translate faithfully, "
                        "preserve URLs/placeholders, emojis, bullets, product names, punctuation, and line/paragraph structure. "
                        "If the input contains one tag or phrase per line, return exactly one translated tag or phrase per line "
                        "in the same order. Return only the translated content with no commentary, labels, quotes, or markdown fences."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Translate from {source_name} to {language_name}:\n\n{text}",
                },
            ],
            "stream": False,
            "temperature": 0.1,
            "max_tokens": max(256, min(8192, len(text) * 2)),
        }
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "EtsyDashboard/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as response:
            data = json.loads(response.read())
        return _validate_output(self._message_text(data), target=target)


class GoogleWebProvider:
    name = "google_web"

    @property
    def available(self) -> bool:
        return True

    def translate(self, text: str, source: str, target: str) -> str:
        result = GoogleTranslator(source="auto", target=target).translate(text)
        return _validate_output(result, target=target)


class MyMemoryProvider:
    name = "mymemory"

    @property
    def available(self) -> bool:
        return True

    def translate(self, text: str, source: str, target: str) -> str:
        from deep_translator import MyMemoryTranslator

        target_code = MYMEMORY_CODES.get(target, target)
        source_code = "en-US" if source in {"", "auto", "en"} else source
        # MyMemory has a much smaller request-size ceiling than Google. Chunk
        # inside this provider so it remains a real fallback for descriptions.
        translated_parts = []
        for chunk in _chunks(text, 450):
            result = MyMemoryTranslator(source=source_code, target=target_code).translate(chunk)
            translated_parts.append(_validate_output(result, target=target))
        return "\n\n".join(translated_parts)


class TranslationCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS translations (
                    cache_key TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    target TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    output TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def key(text: str, source: str, target: str, kind: str) -> str:
        raw = f"v1\0{source}\0{target}\0{kind}\0{text}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def get(self, text: str, source: str, target: str, kind: str) -> TranslationResult | None:
        key = self.key(text, source, target, kind)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT output, provider FROM translations WHERE cache_key = ?",
                (key,),
            ).fetchone()
        if not row:
            return None
        return TranslationResult(text=row[0], provider=row[1], cached=True)

    def put(self, text: str, source: str, target: str, kind: str, result: TranslationResult) -> None:
        key = self.key(text, source, target, kind)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO translations
                (cache_key, source, target, kind, input_sha256, output, provider, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (key, source, target, kind, digest, result.text, result.provider, time.time()),
            )


class TranslationService:
    def __init__(
        self,
        *,
        providers: list[TranslationProvider] | None = None,
        cache_path: Path | None = None,
        attempts_per_provider: int = 2,
    ) -> None:
        self.attempts_per_provider = max(1, attempts_per_provider)
        if cache_path is None:
            cache_env = os.environ.get("ETSY_TRANSLATION_CACHE", "").strip()
            cache_path = (
                Path(cache_env).expanduser()
                if cache_env
                else Path(__file__).resolve().parent / ".etsy-dashboard-diagnostics" / "translation_cache.sqlite"
            )
        self.cache = TranslationCache(cache_path)
        self.providers = providers if providers is not None else self._providers_from_environment()

    @staticmethod
    def _providers_from_environment() -> list[TranslationProvider]:
        registry = {
            "together": TogetherProvider,
            "google_web": GoogleWebProvider,
            "mymemory": MyMemoryProvider,
        }
        configured = os.environ.get(
            "ETSY_TRANSLATION_PROVIDERS",
            "together,google_web,mymemory",
        )
        providers: list[TranslationProvider] = []
        for raw_name in configured.split(","):
            name = raw_name.strip().lower()
            factory = registry.get(name)
            if factory is None:
                continue
            provider = factory()
            if provider.available:
                providers.append(provider)
        if not providers:
            raise TranslationError("No translation providers are available")
        return providers

    def provider_names(self) -> list[str]:
        return [provider.name for provider in self.providers]

    def translate(self, text: str, target: str, *, source: str = "en", kind: str = "text") -> TranslationResult:
        if not text or not text.strip():
            return TranslationResult(text=text, provider="none", cached=True)

        # Include the active provider chain in the cache namespace. If the
        # keyed provider becomes available later, older no-key fallback cache
        # entries will not prevent the higher-priority provider from being used.
        cache_kind = f"{kind}|{'/'.join(self.provider_names())}"
        cached = self.cache.get(text, source, target, cache_kind)
        if cached is not None:
            return cached

        protected_text, urls = _protect_urls(text)
        errors: list[str] = []
        for provider in self.providers:
            for attempt in range(self.attempts_per_provider):
                try:
                    translated = provider.translate(protected_text, source, target)
                    translated = _restore_urls(_validate_output(translated, target=target), urls)
                    result = TranslationResult(text=translated, provider=provider.name)
                    self.cache.put(text, source, target, cache_kind, result)
                    return result
                # Optional providers are external boundaries; any ordinary
                # provider failure must continue through the configured chain.
                except Exception as exc:  # noqa: B902
                    errors.append(f"{provider.name}#{attempt + 1}:{type(exc).__name__}")
                    if attempt + 1 < self.attempts_per_provider:
                        time.sleep(0.35 * (attempt + 1))

        raise TranslationError(
            f"Translation failed for target={target}; providers=" + ", ".join(errors[-8:])
        )

    def translate_text(
        self,
        text: str,
        target: str,
        *,
        source: str = "en",
        max_chars: int = 4200,
        kind: str = "text",
    ) -> str:
        if not text or not text.strip():
            return text
        translated_chunks = [
            self.translate(chunk, target, source=source, kind=kind).text
            for chunk in _chunks(text, max_chars)
        ]
        return "\n\n".join(translated_chunks)

    @staticmethod
    def _normalize_tag_output(tag: str, max_len: int) -> str:
        normalized = re.sub(r"^\s*(?:[-•*]|\d+[.)])\s*", "", str(tag or "")).strip()
        if len(normalized) > max_len:
            shortened = normalized[:max_len].rsplit(" ", 1)[0].rstrip(",- ")
            normalized = shortened if shortened else normalized[:max_len].rstrip(",- ")
        return normalized

    def translate_tag(self, tag: str, target: str, *, max_len: int = 20) -> str:
        translated = self.translate(tag, target, source="en", kind="tag").text
        normalized = self._normalize_tag_output(translated, max_len)
        if not normalized:
            raise TranslationError(f"empty translated tag for {target}")
        return normalized

    def translate_tags(self, tags: list[str], target: str, *, max_len: int = 20) -> list[str]:
        clean = [str(tag or "").strip() for tag in tags if str(tag or "").strip()]
        if not clean:
            return []

        # One request per language instead of one request per tag. Newlines are
        # stable with the configured providers; if a provider changes the line
        # count, fail safely to per-tag translation for this language only.
        batch_input = "\n".join(clean)
        batch_output = self.translate(batch_input, target, source="en", kind="tags_batch").text
        lines = [line for line in batch_output.splitlines() if line.strip()]
        if len(lines) != len(clean):
            return [self.translate_tag(tag, target, max_len=max_len) for tag in clean]

        normalized = [self._normalize_tag_output(line, max_len) for line in lines]
        if any(not tag for tag in normalized):
            return [self.translate_tag(tag, target, max_len=max_len) for tag in clean]
        return normalized


_DEFAULT_SERVICE: TranslationService | None = None


def get_translation_service() -> TranslationService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = TranslationService()
    return _DEFAULT_SERVICE


def translate_text(text: str, lang: str, max_chars: int = 4200) -> str:
    """Compatibility wrapper used by Etsy automation scripts."""
    return get_translation_service().translate_text(text, lang, max_chars=max_chars)


def translate_tag(tag: str, lang: str) -> str:
    """Compatibility wrapper used by Etsy automation scripts."""
    return get_translation_service().translate_tag(tag, lang)


def translate_tags(tags: list[str], lang: str) -> list[str]:
    """Batch compatibility wrapper used by Etsy automation scripts."""
    return get_translation_service().translate_tags(tags, lang)
