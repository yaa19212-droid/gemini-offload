from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from google.genai import types

from mcp_server import gemini_client


EXPECTED_MODELS = [
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.1-flash-image-preview",
    "gemini-3-pro-image-preview",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash-image",
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
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.models = self
        self.files = SimpleNamespace(upload=lambda *args, **kwargs: None)

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


class GeminiClientTests(unittest.TestCase):
    def test_available_models_match_supported_surface(self) -> None:
        self.assertEqual(gemini_client.AVAILABLE_MODELS, EXPECTED_MODELS)
        self.assertNotIn("gemini-3-pro-preview", gemini_client.AVAILABLE_MODELS)
        self.assertNotIn("gemini-2.5-flash-preview-09-2025", gemini_client.AVAILABLE_MODELS)
        self.assertNotIn("gemini-2.5-flash-lite-preview-09-2025", gemini_client.AVAILABLE_MODELS)

    def test_gemini_3_thinking_uses_include_thoughts_config(self) -> None:
        client = _RecordingClient()

        gemini_client._call_api(
            client=client,
            model_name="gemini-3-flash-preview",
            contents_list=[],
            system_prompt="system",
            include_thinking=True,
        )

        config = client.models.calls[0]["config"]
        self.assertEqual(config.system_instruction, "system")
        self.assertEqual(config.thinking_config.include_thoughts, True)
        self.assertIsNone(config.response_modalities)

    def test_image_models_request_image_output_and_skip_thinking_config(self) -> None:
        client = _RecordingClient()

        gemini_client._call_api(
            client=client,
            model_name="gemini-2.5-flash-image",
            contents_list=[],
            system_prompt="system",
            include_thinking=True,
        )

        config = client.models.calls[0]["config"]
        self.assertEqual(config.response_modalities, ["TEXT", "IMAGE"])
        self.assertIsNone(config.thinking_config)

    def test_generate_preserves_inline_image_parts(self) -> None:
        with patch("mcp_server.gemini_client.get_next_api_key", return_value="test-key"):
            with patch("mcp_server.gemini_client.genai.Client", _FakeGenAIClient):
                result = gemini_client.generate(
                    prompt="draw a cat",
                    model="gemini-2.5-flash-image",
                )

        self.assertEqual(result["text"], "caption")
        self.assertEqual(result["model"], "gemini-2.5-flash-image")
        self.assertEqual(result["usage"], {})
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(result["images"][0]["mime_type"], "image/png")
        self.assertEqual(result["images"][0]["data"], b"png-bytes")

