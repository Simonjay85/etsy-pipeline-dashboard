from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import etsy_auto_post
import pytest
import translation_service
from translation_service import TranslationService


@pytest.fixture(autouse=True)
def isolate_runtime_provider_environment(monkeypatch):
    for name in (
        "TOGETHER_API_KEY",
        "TOGETHER_API_BASE_URL",
        "TOGETHER_SEO_MODEL",
        "TOGETHER_MODEL",
        "ETSY_TRANSLATION_TOGETHER_MODEL",
        "BAI_API_KEY",
        "BAI_API_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


class FakeProvider:
    available = True

    def __init__(self, name: str, responses):
        self.name = name
        self.responses = iter(responses)
        self.calls = []

    def translate(self, text: str, source: str, target: str) -> str:
        self.calls.append((text, source, target))
        result = next(self.responses)
        if isinstance(result, BaseException):
            raise result
        return result


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def service_with(tmp_path: Path, providers, *, attempts: int = 1) -> TranslationService:
    return TranslationService(
        providers=list(providers),
        cache_path=tmp_path / "translation.sqlite",
        attempts_per_provider=attempts,
    )


def test_provider_fallback_and_cache(tmp_path):
    primary = FakeProvider("primary", [RuntimeError("temporary outage")])
    fallback = FakeProvider("fallback", ["Bonjour"])
    service = service_with(tmp_path, [primary, fallback])

    first = service.translate("Hello", "fr")
    second = service.translate("Hello", "fr")

    assert first.text == "Bonjour"
    assert first.provider == "fallback"
    assert not first.cached
    assert second.text == "Bonjour"
    assert second.provider == "fallback"
    assert second.cached
    assert len(primary.calls) == 1
    assert len(fallback.calls) == 1


def test_invalid_google_error_page_falls_through(tmp_path):
    broken = FakeProvider(
        "google_web",
        ["Error 500 (Server Error)!! That’s an error. There was an error. Please try again."],
    )
    fallback = FakeProvider("mymemory", ["Tradução válida"])
    service = service_with(tmp_path, [broken, fallback])

    result = service.translate("A better organized life", "pt")

    assert result.text == "Tradução válida"
    assert result.provider == "mymemory"
    assert len(fallback.calls) == 1


def test_url_is_protected_and_restored(tmp_path):
    provider = FakeProvider("provider", ["Visitez ⟪URL0⟫ maintenant"])
    service = service_with(tmp_path, [provider])

    result = service.translate("Visit https://example.com/path?q=1 now", "fr")

    assert result.text == "Visitez https://example.com/path?q=1 maintenant"
    assert "https://example.com/path?q=1" not in provider.calls[0][0]


def test_text_chunking_uses_same_service(tmp_path):
    provider = FakeProvider("provider", ["Un", "Deux"])
    service = service_with(tmp_path, [provider])

    output = service.translate_text("First paragraph\n\nSecond paragraph", "fr", max_chars=18)

    assert output == "Un\n\nDeux"
    assert len(provider.calls) == 2


def test_together_is_preferred_when_key_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("TOGETHER_API_KEY", "test-key")
    monkeypatch.delenv("ETSY_TRANSLATION_PROVIDERS", raising=False)

    service = TranslationService(cache_path=tmp_path / "translation.sqlite")

    assert service.provider_names() == ["together", "google_web", "mymemory"]


def test_bai_is_not_a_configured_translation_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("BAI_API_KEY", "test-bai-key")
    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)
    monkeypatch.setenv("ETSY_TRANSLATION_PROVIDERS", "bai,google_web,mymemory")

    service = TranslationService(cache_path=tmp_path / "translation.sqlite")

    assert service.provider_names() == ["google_web", "mymemory"]
    assert not hasattr(translation_service, "BaiProvider")


def test_together_provider_uses_default_and_optional_model_override(monkeypatch):
    monkeypatch.setenv("TOGETHER_API_KEY", "test-key")
    monkeypatch.delenv("ETSY_TRANSLATION_TOGETHER_MODEL", raising=False)

    provider = translation_service.TogetherProvider()

    assert provider.available
    assert provider.endpoint == "https://api.together.xyz/v1/chat/completions"
    assert provider.model == "deepseek-ai/DeepSeek-V4.1-Flash"

    monkeypatch.setenv("ETSY_TRANSLATION_TOGETHER_MODEL", "custom/model")
    assert translation_service.TogetherProvider().model == "custom/model"


def test_together_provider_uses_dashboard_runtime_endpoint_and_model_precedence(monkeypatch):
    monkeypatch.setenv("TOGETHER_API_KEY", "test-key")
    monkeypatch.setenv("TOGETHER_API_BASE_URL", "https://proxy.example.test/compat/v1/")
    monkeypatch.setenv("TOGETHER_SEO_MODEL", "runtime/seo-model")
    monkeypatch.setenv("TOGETHER_MODEL", "runtime/generic-model")
    monkeypatch.delenv("ETSY_TRANSLATION_TOGETHER_MODEL", raising=False)

    provider = translation_service.TogetherProvider()

    assert provider.endpoint == "https://proxy.example.test/compat/v1/chat/completions"
    assert provider.model == "runtime/seo-model"

    monkeypatch.setenv("ETSY_TRANSLATION_TOGETHER_MODEL", "translation/provider-model")
    provider = translation_service.TogetherProvider()
    assert provider.model == "translation/provider-model"


def test_together_provider_uses_generic_runtime_model_after_empty_seo_override(monkeypatch):
    monkeypatch.setenv("TOGETHER_API_KEY", "test-key")
    monkeypatch.setenv("TOGETHER_API_BASE_URL", "https://proxy.example.test/v1")
    monkeypatch.setenv("TOGETHER_SEO_MODEL", "")
    monkeypatch.setenv("TOGETHER_MODEL", "runtime/generic-model")
    monkeypatch.delenv("ETSY_TRANSLATION_TOGETHER_MODEL", raising=False)

    provider = translation_service.TogetherProvider()

    assert provider.endpoint == "https://proxy.example.test/v1/chat/completions"
    assert provider.model == "runtime/generic-model"


def test_together_provider_ignores_bai_runtime_credentials(monkeypatch):
    monkeypatch.setenv("BAI_API_KEY", "unused-bai-key")
    monkeypatch.setenv("BAI_API_BASE_URL", "https://bai.example.test")
    monkeypatch.setenv("TOGETHER_API_KEY", "test-key")
    monkeypatch.setenv("TOGETHER_API_BASE_URL", "https://proxy.example.test/v1")
    monkeypatch.setenv("TOGETHER_SEO_MODEL", "runtime/seo-model")

    provider = translation_service.TogetherProvider()

    assert provider.available
    assert provider.endpoint == "https://proxy.example.test/v1/chat/completions"
    assert provider.model == "runtime/seo-model"
    assert not hasattr(translation_service, "BaiProvider")


def test_together_provider_request_is_mocked_and_uses_expected_contract(monkeypatch):
    monkeypatch.setenv("TOGETHER_API_KEY", "test-key")
    monkeypatch.delenv("TOGETHER_API_BASE_URL", raising=False)
    monkeypatch.delenv("TOGETHER_SEO_MODEL", raising=False)
    monkeypatch.delenv("TOGETHER_MODEL", raising=False)
    monkeypatch.delenv("ETSY_TRANSLATION_TOGETHER_MODEL", raising=False)
    provider = translation_service.TogetherProvider()
    response = FakeHTTPResponse(
        {"choices": [{"message": {"content": "Bonjour ⟪URL0⟫"}}]}
    )

    with patch.object(translation_service.urllib.request, "urlopen", return_value=response) as urlopen:
        translated = provider.translate("Visit ⟪URL0⟫", "en", "fr")

    request = urlopen.call_args.args[0]
    payload = json.loads(request.data)
    assert translated == "Bonjour ⟪URL0⟫"
    assert request.full_url == "https://api.together.xyz/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer test-key"
    assert request.get_header("User-agent") == "EtsyDashboard/1.0"
    assert payload["model"] == "deepseek-ai/DeepSeek-V4.1-Flash"


def test_poster_compatibility_wrappers_use_central_service():
    class FakeService:
        def translate_text(self, text, lang, max_chars=4200):
            return f"TEXT:{lang}:{text}:{max_chars}"

        def translate_tag(self, tag, lang):
            return f"TAG:{lang}:{tag}"

    fake = FakeService()
    with patch.object(etsy_auto_post, "get_translation_service", return_value=fake):
        assert etsy_auto_post.translate_text("hello", "fr", 123) == "TEXT:fr:hello:123"
        assert etsy_auto_post.translate_tag("keyword", "fr") == "TAG:fr:keyword"


def test_repair_and_existing_scripts_import_central_translation_functions():
    repair_source = Path("etsy_repair.py").read_text(encoding="utf-8")
    existing_source = Path("etsy_translate_existing.py").read_text(encoding="utf-8")

    assert "from translation_service import translate_text, translate_tags" in repair_source
    assert "from translation_service import translate_text, translate_tags" in existing_source
    assert "GoogleTranslator" not in repair_source
    assert "GoogleTranslator" not in existing_source


def test_batch_tags_use_one_provider_call(tmp_path):
    provider = FakeProvider("provider", ["planner numérique\nmodèle imprimable\norganisation adhd"])
    service = service_with(tmp_path, [provider])

    result = service.translate_tags(
        ["digital planner", "printable template", "adhd organization"],
        "fr",
    )

    assert result == ["planner numérique", "modèle imprimable", "organisation adhd"]
    assert len(provider.calls) == 1
    assert provider.calls[0][0].count("\n") == 2


def test_batch_tags_fall_back_to_individual_when_line_count_changes(tmp_path):
    provider = FakeProvider(
        "provider",
        [
            "merged translation",
            "premier",
            "deuxième",
        ],
    )
    service = service_with(tmp_path, [provider])

    result = service.translate_tags(["first", "second"], "fr")

    assert result == ["premier", "deuxième"]
    assert len(provider.calls) == 3
