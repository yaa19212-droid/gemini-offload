from __future__ import annotations

from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from google.genai import types

from mcp_server import gemini_client
from mcp_server.keys import ApiKeyLease, NoAvailableQuotaSlotError


EXPECTED_MODELS = [
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "gemini-3.5-flash",
]


class _RecordingModels:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate_content(self, *, model, contents, config):
        self.calls.append(
            {
                "model": model,
                "contents": contents,
                "config": config,
            }
        )
        return "ok"


class _RecordingClient:
    def __init__(self) -> None:
        self.models = _RecordingModels()


class _FakeGenAIClient:
    def __init__(self, *, vertexai=None, credentials=None, project=None, location=None) -> None:
        self.project = project
        self.models = self

    def generate_content(self, *, model, contents, config):
        return SimpleNamespace(
            parts=[
                types.Part(text="caption"),
                types.Part(
                    inline_data=types.Blob(
                        mime_type="image/png",
                        data=b"png-bytes",
                    )
                ),
            ],
            text="caption",
        )


class _FakeAcquiredLease:
    def __init__(self, lease: ApiKeyLease) -> None:
        self.lease = lease
        self.quota_slot = f"{lease.project_id}/{lease.location}/gemini-3.5-flash"
        self.cooldowns: list[float] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def mark_cooldown(self, seconds: float) -> None:
        self.cooldowns.append(seconds)


class GeminiClientTests(unittest.TestCase):
    def test_available_models_match_supported_surface(self) -> None:
        self.assertEqual(gemini_client.AVAILABLE_MODELS, EXPECTED_MODELS)
        self.assertNotIn("gemini-3-pro-preview", gemini_client.AVAILABLE_MODELS)
        self.assertNotIn("gemini-2.5-pro", gemini_client.AVAILABLE_MODELS)
        self.assertNotIn("gemini-2.5-flash", gemini_client.AVAILABLE_MODELS)
        self.assertNotIn("gemini-2.5-flash-preview-09-2025", gemini_client.AVAILABLE_MODELS)
        self.assertNotIn("gemini-2.5-flash-lite-preview-09-2025", gemini_client.AVAILABLE_MODELS)
        self.assertNotIn("gemini-3.1-flash-lite-preview", gemini_client.AVAILABLE_MODELS)
        self.assertNotIn("gemini-3.1-flash-image-preview", gemini_client.AVAILABLE_MODELS)
        self.assertNotIn("gemini-3-pro-image-preview", gemini_client.AVAILABLE_MODELS)

    def test_model_characteristics_cover_supported_models(self) -> None:
        self.assertEqual(
            sorted(gemini_client.MODEL_CHARACTERISTICS),
            sorted(gemini_client.AVAILABLE_MODELS),
        )
        self.assertIn("Best overall quality", gemini_client.MODEL_CHARACTERISTICS["gemini-3.1-pro-preview"])
        self.assertIn("Emergency fallback", gemini_client.MODEL_CHARACTERISTICS["gemini-3-flash-preview"])
        self.assertIn("Fast default", gemini_client.MODEL_CHARACTERISTICS["gemini-3.5-flash"])

    def test_gemini_25_models_are_blocked_before_api_call(self) -> None:
        client = _RecordingClient()

        with self.assertRaisesRegex(ValueError, "Blocked outdated Gemini model"):
            gemini_client._call_api(
                client=client,
                model_name="gemini-2.5-pro",
                contents_list=[],
                system_prompt="system",
                include_thinking=False,
            )

        self.assertEqual(client.models.calls, [])

    def test_generate_rejects_gemini_25_models(self) -> None:
        with self.assertRaisesRegex(ValueError, "Blocked outdated Gemini model"):
            gemini_client.generate(prompt="hello", model="gemini-2.5-flash")

    def test_call_api_rejects_removed_models_before_api_call(self) -> None:
        client = _RecordingClient()

        with self.assertRaisesRegex(ValueError, "Unsupported model"):
            gemini_client._call_api(
                client=client,
                model_name="gemini-3.1-flash-image-preview",
                contents_list=[],
                system_prompt="system",
                include_thinking=False,
            )

        self.assertEqual(client.models.calls, [])

    def test_gemini_3_thinking_uses_include_thoughts_config(self) -> None:
        client = _RecordingClient()

        gemini_client._call_api(
            client=client,
            model_name="gemini-3.5-flash",
            contents_list=[],
            system_prompt="system",
            include_thinking=True,
        )

        config = client.models.calls[0]["config"]
        self.assertEqual(config.system_instruction, "system")
        self.assertEqual(config.thinking_config.include_thoughts, True)
        self.assertIsNone(config.response_modalities)

    def test_generate_preserves_inline_image_parts(self) -> None:
        with patch(
            "mcp_server.gemini_client.acquire_vertex_credential_lease",
            return_value=_FakeAcquiredLease(
                ApiKeyLease("key1", "project1", "global", Path("key1.json"), object())
            ),
        ):
            with patch("mcp_server.gemini_client.get_key_count", return_value=1):
                with patch("mcp_server.gemini_client.genai.Client", _FakeGenAIClient):
                    result = gemini_client.generate(
                        prompt="draw a cat",
                        model="gemini-3.5-flash",
                    )

        self.assertEqual(result["text"], "caption")
        self.assertEqual(result["model"], "gemini-3.5-flash")
        self.assertEqual(result["usage"], {})
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(result["images"][0]["mime_type"], "image/png")
        self.assertEqual(result["images"][0]["data"], b"png-bytes")

    def test_generate_retries_next_key_after_rate_limit(self) -> None:
        leases = [
            ApiKeyLease("key1", "limited-project", "global", Path("key1.json"), object()),
            ApiKeyLease("key2", "good-project", "global", Path("key2.json"), object()),
        ]

        class RateLimitedClient(_FakeGenAIClient):
            def generate_content(self, *, model, contents, config):
                if self.project == "limited-project":
                    exc = RuntimeError("rate limit")
                    exc.status_code = 429
                    raise exc
                return super().generate_content(model=model, contents=contents, config=config)

        acquired_leases = [_FakeAcquiredLease(lease) for lease in leases]
        with patch("mcp_server.gemini_client.acquire_vertex_credential_lease", side_effect=acquired_leases):
            with patch("mcp_server.gemini_client.get_key_count", return_value=2):
                with patch("mcp_server.gemini_client.genai.Client", RateLimitedClient):
                    result = gemini_client.generate(
                        prompt="draw a cat",
                        model="gemini-3.5-flash",
                    )

        self.assertEqual(len(acquired_leases[0].cooldowns), 1)
        self.assertEqual(result["text"], "caption")

    def test_generate_fail_fast_returns_structured_rate_limit_error(self) -> None:
        slot_error = NoAvailableQuotaSlotError(
            model="gemini-3.5-flash",
            retry_after_seconds=12.0,
            quota_slots=["project1/global/gemini-3.5-flash"],
        )

        with patch("mcp_server.gemini_client.acquire_vertex_credential_lease", side_effect=slot_error):
            with patch("mcp_server.gemini_client.get_key_count", return_value=1):
                with self.assertRaises(gemini_client.GeminiRateLimitError) as raised:
                    gemini_client.generate(
                        prompt="hello",
                        model="gemini-3.5-flash",
                    )

        payload = raised.exception.to_dict()
        self.assertEqual(payload["error_type"], "vertex_rate_limited")
        self.assertEqual(payload["model"], "gemini-3.5-flash")
        self.assertEqual(payload["retry_after_seconds"], 12.0)
        self.assertIn("gemini-3.1-pro-preview", payload["available_fallback_models"])

    def test_generate_uses_explicit_fallback_model_after_rate_limit(self) -> None:
        def acquire_for_model(**kwargs):
            if kwargs["model"] == "gemini-3.1-pro-preview":
                raise NoAvailableQuotaSlotError(
                    model="gemini-3.1-pro-preview",
                    retry_after_seconds=5.0,
                    quota_slots=["project1/global/gemini-3.1-pro-preview"],
                )
            return _FakeAcquiredLease(
                ApiKeyLease("key2", "project2", "global", Path("key2.json"), object())
            )

        with patch("mcp_server.gemini_client.acquire_vertex_credential_lease", side_effect=acquire_for_model):
            with patch("mcp_server.gemini_client.get_key_count", return_value=1):
                with patch("mcp_server.gemini_client.genai.Client", _FakeGenAIClient):
                    result = gemini_client.generate(
                        prompt="hello",
                        model="gemini-3.1-pro-preview",
                        fallback_models=["gemini-3.5-flash"],
                    )

        self.assertEqual(result["model"], "gemini-3.5-flash")

    def test_generate_passes_wait_mode_to_quota_acquire(self) -> None:
        calls: list[dict[str, object]] = []

        def acquire_for_model(**kwargs):
            calls.append(kwargs)
            return _FakeAcquiredLease(
                ApiKeyLease("key1", "project1", "global", Path("key1.json"), object())
            )

        with patch("mcp_server.gemini_client.acquire_vertex_credential_lease", side_effect=acquire_for_model):
            with patch("mcp_server.gemini_client.get_key_count", return_value=1):
                with patch("mcp_server.gemini_client.genai.Client", _FakeGenAIClient):
                    gemini_client.generate(
                        prompt="hello",
                        model="gemini-3.5-flash",
                        rate_limit_mode="wait",
                        rate_limit_max_wait_seconds=7,
                    )

        self.assertEqual(calls[0]["wait_for_cooldown"], True)
        self.assertEqual(calls[0]["max_wait_seconds"], 7.0)

    def test_generate_wait_mode_retries_after_api_rate_limit(self) -> None:
        leases = [
            ApiKeyLease("key1", "project1", "global", Path("key1.json"), object()),
            ApiKeyLease("key1", "project1", "global", Path("key1.json"), object()),
        ]
        calls: list[str | None] = []

        class RateLimitedOnceClient(_FakeGenAIClient):
            def generate_content(self, *, model, contents, config):
                calls.append(self.project)
                if len(calls) == 1:
                    exc = RuntimeError("rate limit")
                    exc.status_code = 429
                    raise exc
                return super().generate_content(model=model, contents=contents, config=config)

        acquired_leases = [_FakeAcquiredLease(lease) for lease in leases]
        with patch("mcp_server.gemini_client.acquire_vertex_credential_lease", side_effect=acquired_leases):
            with patch("mcp_server.gemini_client.get_key_count", return_value=1):
                with patch("mcp_server.gemini_client.genai.Client", RateLimitedOnceClient):
                    result = gemini_client.generate(
                        prompt="hello",
                        model="gemini-3.5-flash",
                        rate_limit_mode="wait",
                    )

        self.assertEqual(len(calls), 2)
        self.assertEqual(len(acquired_leases[0].cooldowns), 1)
        self.assertEqual(result["text"], "caption")
