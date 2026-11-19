# Etsy Translation Service

All Etsy translation workflows use `translation_service.py`.

## Provider order

Default order:

1. `together` — Together AI keyed provider. Enabled when `TOGETHER_API_KEY` is set.
2. `google_web` — no-key `deep-translator` Google fallback.
3. `mymemory` — no-key final fallback.

Together AI translation uses:

- `TOGETHER_API_KEY`
- `TOGETHER_API_BASE_URL` as the runtime API base. It is normalized to one
  `/v1` suffix before `/chat/completions` is added; malformed values fall back
  to the Together default endpoint.
- `ETSY_TRANSLATION_TOGETHER_MODEL` as the provider-specific model override.
  When it is unset, the runtime bridge falls back to `TOGETHER_SEO_MODEL`, then
  `TOGETHER_MODEL`, and finally `deepseek-ai/DeepSeek-V4.1-Flash`.

Override the order with:

```bash
export ETSY_TRANSLATION_PROVIDERS="together,google_web,mymemory"
```

The legacy `bai` provider is not registered by this service. Any `bai` entry in
`ETSY_TRANSLATION_PROVIDERS` is ignored, and the translation service does not
read `BAI_*` credentials.

The poster prints the active chain when it reaches the Etsy Translations section.

## Reliability behavior

- Each provider is retried in a bounded way before the next provider is tried.
- HTML/server-error responses are rejected instead of being written into Etsy.
- URLs are protected during translation and restored afterward.
- Long descriptions are chunked safely.
- Tags are translated as one batch per language; if line structure changes, the service falls back to individual tag translation.
- Successful translations are cached in `.etsy-dashboard-diagnostics/translation_cache.sqlite`.
- The cache namespace includes the active provider chain, so enabling Together
  later causes new translations to use Together instead of being blocked by
  older no-key cache entries.

## Recommended production setup

For the production path, configure `TOGETHER_API_KEY`. The no-key Google Web
and MyMemory providers are retained only as fallbacks.

Do not commit API keys into the repository or `shops_config.json`, and do not
print key values in diagnostics or test output.
