from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

import etsy_image_alt_text


class _FakeResponse:
    def __init__(self, payload: object):
        self._body = json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self):
        return self._body

    def close(self):
        self.closed = True


class TogetherAltTextTests(unittest.TestCase):
    def setUp(self):
        self._together_env_patch = patch.dict(
            os.environ,
            {
                "TOGETHER_API_KEY": "",
                "TOGETHER_API_BASE_URL": "",
                "TOGETHER_SEO_MODEL": "",
                "TOGETHER_MODEL": "",
                "TOGETHER_ALT_TEXT_API_URL": "",
                "TOGETHER_ALT_TEXT_MODEL": "",
                "BAI_API_KEY": "",
            },
            clear=False,
        )
        self._together_env_patch.start()
        self.addCleanup(self._together_env_patch.stop)
        image_file = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        image_file.close()
        self.image_path = Path(image_file.name)
        self.image_path.write_bytes(b"small-test-image")

    def tearDown(self):
        self.image_path.unlink(missing_ok=True)

    def test_success_builds_multimodal_payload_and_parses_content(self):
        response = _FakeResponse(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '"A printable planner page"'},
                    }
                ]
            }
        )
        with (
            patch.dict(
                os.environ,
                {
                    "TOGETHER_API_KEY": "test-together-key",
                    "TOGETHER_API_BASE_URL": "https://runtime.example.test/v1",
                    "TOGETHER_SEO_MODEL": "runtime/seo-model",
                    "TOGETHER_MODEL": "runtime/generic-model",
                    "TOGETHER_ALT_TEXT_API_URL": "https://example.test/alt",
                    "TOGETHER_ALT_TEXT_MODEL": "custom-together-vision-model",
                },
                clear=False,
            ),
            patch(
                "etsy_image_alt_text.urllib.request.urlopen",
                return_value=response,
            ) as urlopen,
        ):
            result = etsy_image_alt_text.generate_visual_alt_text(
                self.image_path, "Planner title", "digital planner, printable"
            )

        self.assertEqual(result, "A printable planner page")
        request, = urlopen.call_args.args
        self.assertEqual(request.full_url, "https://example.test/alt")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            request.get_header("Authorization"), "Bearer test-together-key"
        )
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("User-agent"), "EtsyDashboard/1.0")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "custom-together-vision-model")
        self.assertFalse(payload["stream"])
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual(payload["max_tokens"], 1000)
        self.assertNotIn("max_completion_tokens", payload)
        content = payload["messages"][0]["content"]
        self.assertIn("Planner title", content[0]["text"])
        self.assertIn("screen-reader", content[0]["text"])
        self.assertIn("up to two candidate keyword phrases", content[0]["text"])
        self.assertIn("use none when no candidate fits", content[0]["text"])
        self.assertIn("180–240 characters", content[0]["text"])
        self.assertIn("Never exceed 250 characters", content[0]["text"])
        self.assertIn("<listing_title>", content[0]["text"])
        self.assertIn("reference data only", content[0]["text"])
        self.assertEqual(content[1]["type"], "image_url")
        self.assertEqual(content[1]["image_url"]["detail"], "low")
        self.assertEqual(
            content[1]["image_url"]["url"],
            "data:image/png;base64," + base64.b64encode(b"small-test-image").decode("ascii"),
        )
        self.assertTrue(response.closed)

    def test_defaults_use_together_endpoint_and_model(self):
        response = _FakeResponse(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "Default Together response"},
                    }
                ]
            }
        )
        with (
            patch.dict(
                os.environ,
                {
                    "TOGETHER_API_KEY": "test-together-key",
                    "TOGETHER_API_BASE_URL": "",
                    "TOGETHER_SEO_MODEL": "",
                    "TOGETHER_MODEL": "",
                    "TOGETHER_ALT_TEXT_API_URL": "",
                    "TOGETHER_ALT_TEXT_MODEL": "",
                },
                clear=False,
            ),
            patch(
                "etsy_image_alt_text.urllib.request.urlopen",
                return_value=response,
            ) as urlopen,
        ):
            result = etsy_image_alt_text.generate_visual_alt_text(
                self.image_path, "Planner title", "digital planner"
            )

        self.assertEqual(result, "Default Together response")
        request, = urlopen.call_args.args
        self.assertEqual(
            request.full_url, "https://api.together.xyz/v1/chat/completions"
        )
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "deepseek-ai/DeepSeek-V4.1-Flash")
        self.assertFalse(payload["stream"])
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual(payload["max_tokens"], 1000)
        self.assertNotIn("temperature", payload)

    def test_runtime_base_and_seo_model_are_used_when_provider_overrides_are_empty(self):
        response = _FakeResponse(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "Runtime bridge response"},
                    }
                ]
            }
        )
        with (
            patch.dict(
                os.environ,
                {
                    "TOGETHER_API_KEY": "test-together-key",
                    "TOGETHER_API_BASE_URL": "https://proxy.example.test/compat/v1/",
                    "TOGETHER_SEO_MODEL": "runtime/seo-model",
                    "TOGETHER_MODEL": "runtime/generic-model",
                    "TOGETHER_ALT_TEXT_API_URL": "",
                    "TOGETHER_ALT_TEXT_MODEL": "",
                },
                clear=False,
            ),
            patch(
                "etsy_image_alt_text.urllib.request.urlopen",
                return_value=response,
            ) as urlopen,
        ):
            result = etsy_image_alt_text.generate_visual_alt_text(
                self.image_path, "Planner title", "digital planner"
            )

        self.assertEqual(result, "Runtime bridge response")
        request, = urlopen.call_args.args
        self.assertEqual(
            request.full_url,
            "https://proxy.example.test/compat/v1/chat/completions",
        )
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "runtime/seo-model")

    def test_generic_model_is_used_after_empty_seo_runtime_override(self):
        response = _FakeResponse(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "Generic model response"},
                    }
                ]
            }
        )
        with (
            patch.dict(
                os.environ,
                {
                    "TOGETHER_API_KEY": "test-together-key",
                    "TOGETHER_API_BASE_URL": "https://proxy.example.test/v1",
                    "TOGETHER_SEO_MODEL": "",
                    "TOGETHER_MODEL": "runtime/generic-model",
                    "TOGETHER_ALT_TEXT_API_URL": "",
                    "TOGETHER_ALT_TEXT_MODEL": "",
                },
                clear=False,
            ),
            patch(
                "etsy_image_alt_text.urllib.request.urlopen",
                return_value=response,
            ) as urlopen,
        ):
            result = etsy_image_alt_text.generate_visual_alt_text(
                self.image_path, "Planner title", "digital planner"
            )

        self.assertEqual(result, "Generic model response")
        request, = urlopen.call_args.args
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "runtime/generic-model")

    def test_invalid_runtime_base_falls_back_to_safe_default_endpoint(self):
        response = _FakeResponse(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "Safe default response"},
                    }
                ]
            }
        )
        with (
            patch.dict(
                os.environ,
                {
                    "TOGETHER_API_KEY": "test-together-key",
                    "TOGETHER_API_BASE_URL": "https://proxy.example.test/v1?token=unsafe",
                    "TOGETHER_SEO_MODEL": "runtime/seo-model",
                    "TOGETHER_ALT_TEXT_API_URL": "",
                    "TOGETHER_ALT_TEXT_MODEL": "",
                },
                clear=False,
            ),
            patch(
                "etsy_image_alt_text.urllib.request.urlopen",
                return_value=response,
            ) as urlopen,
        ):
            result = etsy_image_alt_text.generate_visual_alt_text(
                self.image_path, "Planner title", "digital planner"
            )

        self.assertEqual(result, "Safe default response")
        request, = urlopen.call_args.args
        self.assertEqual(
            request.full_url,
            "https://api.together.xyz/v1/chat/completions",
        )

    def test_missing_key_skips_network_call(self):
        with (
                patch.dict(
                    os.environ,
                    {"TOGETHER_API_KEY": "", "BAI_API_KEY": "unused-bai-key"},
                ),
            patch("etsy_image_alt_text.urllib.request.urlopen") as urlopen,
        ):
            result = etsy_image_alt_text.generate_visual_alt_text(
                self.image_path, "Title", "keyword"
            )

        self.assertIsNone(result)
        urlopen.assert_not_called()

    def test_malformed_response_returns_none(self):
        response = Mock()
        response.read.return_value = b"not-json"
        with (
            patch.dict(os.environ, {"TOGETHER_API_KEY": "test-together-key"}),
            patch("etsy_image_alt_text.urllib.request.urlopen", return_value=response),
        ):
            result = etsy_image_alt_text.generate_visual_alt_text(
                self.image_path, "Title", "keyword"
            )

        self.assertIsNone(result)

    def test_response_requires_stop_finish_reason(self):
        responses = [
            ("missing", {"choices": [{"message": {"content": "Not accepted"}}]}),
            (
                "length",
                {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": "Not accepted"},
                        }
                    ]
                },
            ),
            (
                "content_filter",
                {
                    "choices": [
                        {
                            "finish_reason": "content_filter",
                            "message": {"content": "Not accepted"},
                        }
                    ]
                },
            ),
            (
                "tool_calls",
                {
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {"content": "Not accepted"},
                        }
                    ]
                },
            ),
        ]

        for name, payload in responses:
            with self.subTest(finish_reason=name):
                self.assertIsNone(etsy_image_alt_text._response_content(payload))

    def test_empty_response_content_returns_none(self):
        response = _FakeResponse(
            {
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "   "}}
                ]
            }
        )
        with (
            patch.dict(os.environ, {"TOGETHER_API_KEY": "test-together-key"}),
            patch("etsy_image_alt_text.urllib.request.urlopen", return_value=response),
        ):
            result = etsy_image_alt_text.generate_visual_alt_text(
                self.image_path, "Title", "keyword"
            )

        self.assertIsNone(result)

    def test_transient_http_error_retries_then_succeeds(self):
        transient = urllib.error.HTTPError(
            "https://example.test/alt",
            429,
            "rate limited",
            {},
            io.BytesIO(b"private response body"),
        )
        success = _FakeResponse(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "Retry succeeded"},
                    }
                ]
            }
        )
        with (
            patch.dict(os.environ, {"TOGETHER_API_KEY": "test-together-key"}),
            patch(
                "etsy_image_alt_text.urllib.request.urlopen",
                side_effect=[transient, success],
            ) as urlopen,
            patch("etsy_image_alt_text.random.uniform", return_value=0.05),
            patch("etsy_image_alt_text.time.sleep") as sleep,
        ):
            result = etsy_image_alt_text.generate_visual_alt_text(
                self.image_path, "Title", "keyword"
            )

        self.assertEqual(result, "Retry succeeded")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(0.25)
        self.assertTrue(transient.fp.closed)

    def test_retry_after_header_is_capped_and_jittered(self):
        transient = urllib.error.HTTPError(
            "https://example.test/alt",
            429,
            "rate limited",
            {"Retry-After": "5"},
            io.BytesIO(b"private response body"),
        )
        success = _FakeResponse(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "Retry-After succeeded"},
                    }
                ]
            }
        )
        with (
            patch.dict(os.environ, {"TOGETHER_API_KEY": "test-together-key"}),
            patch(
                "etsy_image_alt_text.urllib.request.urlopen",
                side_effect=[transient, success],
            ),
            patch("etsy_image_alt_text.random.uniform", return_value=0.05),
            patch("etsy_image_alt_text.time.sleep") as sleep,
        ):
            result = etsy_image_alt_text.generate_visual_alt_text(
                self.image_path, "Title", "keyword"
            )

        self.assertEqual(result, "Retry-After succeeded")
        sleep.assert_called_once_with(etsy_image_alt_text.ALT_TEXT_RETRY_MAX_DELAY_SECONDS)
        self.assertTrue(transient.fp.closed)

    def test_numeric_retry_after_is_honored_before_cap(self):
        error = urllib.error.HTTPError(
            "https://example.test/alt",
            429,
            "rate limited",
            {"Retry-After": "0.6"},
            io.BytesIO(b"private response body"),
        )
        with patch("etsy_image_alt_text.random.uniform", return_value=0.05):
            delay = etsy_image_alt_text._retry_delay_seconds(0, error)

        self.assertAlmostEqual(delay, 0.65)
        error.close()

    def test_permanent_http_error_does_not_retry_and_closes_error_response(self):
        error = urllib.error.HTTPError(
            "https://example.test/alt",
            401,
            "unauthorized",
            {},
            io.BytesIO(b"private response body"),
        )
        with (
            patch.dict(os.environ, {"TOGETHER_API_KEY": "test-together-key"}),
            patch(
                "etsy_image_alt_text.urllib.request.urlopen",
                side_effect=error,
            ) as urlopen,
            patch("etsy_image_alt_text.time.sleep") as sleep,
        ):
            result = etsy_image_alt_text.generate_visual_alt_text(
                self.image_path, "Title", "keyword"
            )

        self.assertIsNone(result)
        urlopen.assert_called_once()
        sleep.assert_not_called()
        self.assertTrue(error.fp.closed)

    def test_transient_network_error_retries_with_bounded_timeout_then_returns_none(self):
        with (
            patch.dict(os.environ, {"TOGETHER_API_KEY": "test-together-key"}),
            patch(
                "etsy_image_alt_text.urllib.request.urlopen",
                side_effect=urllib.error.URLError("timed out"),
            ) as urlopen,
            patch("etsy_image_alt_text.random.uniform", return_value=0.05),
            patch("etsy_image_alt_text.time.sleep") as sleep,
        ):
            result = etsy_image_alt_text.generate_visual_alt_text(
                self.image_path, "Title", "keyword"
            )

        self.assertIsNone(result)
        self.assertEqual(urlopen.call_count, etsy_image_alt_text.ALT_TEXT_MAX_ATTEMPTS)
        self.assertEqual(
            urlopen.call_args.kwargs["timeout"],
            etsy_image_alt_text.ALT_TEXT_REQUEST_TIMEOUT_SECONDS,
        )
        sleep.assert_called_once_with(0.25)

    def test_retry_backoff_progresses_exponentially_with_jitter(self):
        network_errors = [urllib.error.URLError("timed out") for _ in range(3)]
        with (
            patch.dict(os.environ, {"TOGETHER_API_KEY": "test-together-key"}),
            patch(
                "etsy_image_alt_text.urllib.request.urlopen",
                side_effect=network_errors,
            ) as urlopen,
            patch.object(etsy_image_alt_text, "ALT_TEXT_MAX_ATTEMPTS", 3),
            patch(
                "etsy_image_alt_text.random.uniform",
                side_effect=[0.01, 0.02],
            ) as uniform,
            patch("etsy_image_alt_text.time.sleep") as sleep,
        ):
            result = etsy_image_alt_text.generate_visual_alt_text(
                self.image_path, "Title", "keyword"
            )

        self.assertIsNone(result)
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(uniform.call_count, 2)
        self.assertEqual(sleep.call_count, 2)
        self.assertAlmostEqual(sleep.call_args_list[0].args[0], 0.21)
        self.assertAlmostEqual(sleep.call_args_list[1].args[0], 0.42)

    def test_list_content_response_is_joined(self):
        response = _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "Planner "},
                                {"type": "text", "text": "page"},
                            ]
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        )
        with (
            patch.dict(os.environ, {"TOGETHER_API_KEY": "test-together-key"}),
            patch(
                "etsy_image_alt_text.urllib.request.urlopen",
                return_value=response,
            ),
        ):
            result = etsy_image_alt_text.generate_visual_alt_text(
                self.image_path, "Title", "keyword"
            )

        self.assertEqual(result, "Planner page")

    def test_cleanup_strips_code_fence_quotes_and_stays_at_most_250(self):
        long_text = "x" * 260
        response = _FakeResponse(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": f'```text\n"{long_text}"\n```'},
                    }
                ]
            }
        )
        with (
            patch.dict(os.environ, {"TOGETHER_API_KEY": "test-together-key"}),
            patch("etsy_image_alt_text.urllib.request.urlopen", return_value=response),
        ):
            result = etsy_image_alt_text.generate_visual_alt_text(
                self.image_path, "Title", "keyword"
            )

        self.assertIsNotNone(result)
        self.assertLessEqual(len(result), 250)
        self.assertEqual(len(result), 250)
        self.assertFalse(result.startswith(("`", '"')))
        self.assertTrue(result.endswith("..."))

    def test_metadata_fallback_stays_truthful_and_uses_gallery_context(self):
        result = etsy_image_alt_text.build_text_fallback_alt_text(
            "ADHD Student Planner for College",
            "GoodNotes compatible, ADHD planner",
            image_index=2,
            image_count=10,
        )

        self.assertEqual(
            result,
            "ADHD Student Planner for College. "
            "Etsy listing preview, gallery image 2 of 10.",
        )
        self.assertLessEqual(
            len(result), etsy_image_alt_text.ALT_TEXT_MAX_CHARACTERS
        )
        self.assertNotIn("GoodNotes", result)
        self.assertNotIn("Accessible description", result)
        self.assertNotIn("visible layout", result)

    def test_metadata_fallback_long_title_is_word_bounded_and_under_limit(self):
        result = etsy_image_alt_text.build_text_fallback_alt_text(
            "A very long product title with many descriptive words for a planning "
            "resource that should not be cut in the middle of a word or claim",
            "editable PDF, digital download",
            image_index=1,
            image_count=3,
        )

        self.assertLessEqual(
            len(result), etsy_image_alt_text.ALT_TEXT_MAX_CHARACTERS
        )
        self.assertNotIn("...", result)
        self.assertFalse(result.endswith((" ", ",", ";", ":", "-")))


if __name__ == "__main__":
    unittest.main()
