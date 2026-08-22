from __future__ import annotations

from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from google.genai import types

from mcp_server import gemini_client
from mcp_server.keys import ApiKeyLease, NoAvailableQuotaSlotError


EXPECTED_MODELS = [
    "gemini-3.7-flash",
    "gemini-3.1-pro-preview",
    "gemini-3.6-flash",
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
        self.assertNotIn("gemini-3-flash-preview", gemini_client.AVAILABLE_MODELS)
        self.assertEqual(gemini_client.DEFAULT_MODEL_NAME, "gemini-3.7-flash")
        self.assertEqual(gemini_client.RATE_LIMIT_FALLBACK_MODELS, ("gemini-3.6-flash", "gemini-3.5-flash"))

    def test_model_characteristics_cover_supported_models(self) -> None:
        self.assertEqual(
            sorted(gemini_client.MODEL_CHARACTERISTICS),
            sorted(gemini_client.AVAILABLE_MODELS),
        )
        self.assertIn("Default model", gemini_client.MODEL_CHARACTERISTICS["gemini-3.7-flash"])
        self.assertIn("Quality-first option", gemini_client.MODEL_CHARACTERISTICS["gemini-3.1-pro-preview"])
        self.assertIn("429 rate-limit fallback only", gemini_client.MODEL_CHARACTERISTICS["gemini-3.6-flash"])
        self.assertIn("Secondary 429 rate-limit fallback only", gemini_client.MODEL_CHARACTERISTICS["gemini-3.5-flash"])

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

    def test_removed_gemini_3_flash_preview_points_to_current_default(self) -> None:
        client = _RecordingClient()
        with self.assertRaisesRegex(ValueError, "Removed Gemini model.*gemini-3.7-flash"):
            gemini_client._call_api(
                client=client,
                model_name="gemini-3-flash-preview",
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

    def test_all_supported_models_use_explicit_off_safety_filters(self) -> None:
        expected_categories = set(gemini_client.DEFAULT_SAFETY_CATEGORIES)
        for model_name in gemini_client.AVAILABLE_MODELS:
            with self.subTest(model=model_name):
                client = _RecordingClient()
                gemini_client._call_api(
                    client=client,
                    model_name=model_name,
                    contents_list=[],
                    system_prompt="system",
                    include_thinking=False,
                )
                settings = client.models.calls[0]["config"].safety_settings
                self.assertEqual({setting.category for setting in settings}, expected_categories)
                self.assertTrue(
                    all(setting.threshold == types.HarmBlockThreshold.OFF for setting in settings)
                )

    def test_google_search_uses_grounding_tool_config(self) -> None:
        client = _RecordingClient()

        gemini_client._call_api(
            client=client,
            model_name="gemini-3.5-flash",
            contents_list=[],
            system_prompt="system",
            include_thinking=False,
            google_search=True,
        )

        config = client.models.calls[0]["config"]
        self.assertEqual(len(config.tools), 1)
        self.assertIsNotNone(config.tools[0].google_search)

    def test_response_json_schema_sets_json_output_config(self) -> None:
        client = _RecordingClient()
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }

        gemini_client._call_api(
            client=client,
            model_name="gemini-3.5-flash",
            contents_list=[],
            system_prompt="system",
            include_thinking=False,
            google_search=True,
            response_json_schema=schema,
        )

        config = client.models.calls[0]["config"]
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertEqual(config.response_json_schema, schema)
        self.assertEqual(len(config.tools), 1)
        self.assertIsNotNone(config.tools[0].google_search)

    def test_grounding_metadata_is_normalized_for_agents(self) -> None:
        response = {
            "candidates": [
                {
                    "groundingMetadata": {
                        "webSearchQueries": [
                            "UEFA Euro 2024 winner",
                            "Spain England Euro 2024 final score",
                        ],
                        "searchEntryPoint": {
                            "renderedContent": "<style>...</style><div>...</div>",
                            "sdkBlob": "opaque",
                        },
                        "groundingChunks": [
                            {"web": {"title": "uefa.com", "uri": "https://example.test/uefa"}},
                            {"web": {"title": "bbc.com", "uri": "https://example.test/bbc"}},
                        ],
                        "groundingSupports": [
                            {
                                "segment": {
                                    "startIndex": 0,
                                    "endIndex": 43,
                                    "text": "Spain won Euro 2024 by defeating England 2-1.",
                                },
                                "groundingChunkIndices": [0, 1],
                                "confidenceScores": [0.92, 0.84],
                            }
                        ],
                        "retrievalMetadata": {},
                        "retrievalQueries": [],
                        "sourceFlaggingUris": [],
                    }
                }
            ]
        }

        grounding = gemini_client._normalize_grounding_metadata(response)

        self.assertEqual(
            grounding,
            {
                "queries": [
                    "UEFA Euro 2024 winner",
                    "Spain England Euro 2024 final score",
                ],
                "sources": [
                    {"index": 0, "title": "uefa.com", "uri": "https://example.test/uefa"},
                    {"index": 1, "title": "bbc.com", "uri": "https://example.test/bbc"},
                ],
                "supports": [
                    {
                        "text": "Spain won Euro 2024 by defeating England 2-1.",
                        "sources": [
                            {"index": 0, "grounding_confidence": 0.92},
                            {"index": 1, "grounding_confidence": 0.84},
                        ],
                    }
                ],
            },
        )
        self.assertNotIn("renderedContent", str(grounding))
        self.assertNotIn("startIndex", str(grounding))

    def test_extract_response_payload_preserves_outer_whitespace_for_budgeting(self) -> None:
        padded_text = ("\n" * 4) + '{"name":"Ada"}' + (" " * 5000)
        response = SimpleNamespace(text=padded_text)

        payload = gemini_client._extract_response_payload(response)

        self.assertEqual(payload["text"], padded_text)
        self.assertGreater(len(payload["text"].encode("utf-8")), 4096)

    def test_generate_preserves_inline_image_parts(self) -> None:
        with patch(
            "mcp_server.gemini_client.acquire_vertex_credential_lease",
            return_value=_FakeAcquiredLease(
                ApiKeyLease("key1", "project1", "global", Path("key1.json"), object())
            ),
        ):
            with patch("mcp_server.gemini_client.get_vertex_credential_count", return_value=1):
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

    def test_build_contents_applies_default_media_resolution_by_mime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            image_path = temp_path / "image.png"
            pdf_path = temp_path / "doc.pdf"
            video_path = temp_path / "clip.mp4"
            audio_path = temp_path / "audio.mp3"
            for path in (image_path, pdf_path, video_path, audio_path):
                path.write_bytes(b"data")

            contents = gemini_client._build_contents_from_spec(
                [
                    {
                        "role": "user",
                        "parts": [
                            {"file_path": str(image_path)},
                            {"file_path": str(pdf_path)},
                            {"file_path": str(video_path)},
                            {"file_path": str(audio_path)},
                            {"text": "analyze"},
                        ],
                    }
                ]
            )

        part_payloads = [
            part.model_dump(mode="json", by_alias=True, exclude_none=True)
            for part in contents[0].parts
        ]
        self.assertEqual(
            part_payloads[0]["mediaResolution"]["level"],
            "MEDIA_RESOLUTION_ULTRA_HIGH",
        )
        self.assertEqual(
            part_payloads[1]["mediaResolution"]["level"],
            "MEDIA_RESOLUTION_HIGH",
        )
        self.assertEqual(
            part_payloads[2]["mediaResolution"]["level"],
            "MEDIA_RESOLUTION_HIGH",
        )
        self.assertNotIn("mediaResolution", part_payloads[3])
        self.assertNotIn("mediaResolution", part_payloads[4])

    def test_build_contents_applies_request_policy_and_part_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            image_path = temp_path / "image.png"
            pdf_path = temp_path / "doc.pdf"
            video_path = temp_path / "clip.mp4"
            for path in (image_path, pdf_path, video_path):
                path.write_bytes(b"data")

            contents = gemini_client._build_contents_from_spec(
                [
                    {
                        "role": "user",
                        "parts": [
                            {"file_path": str(image_path)},
                            {"file_path": str(pdf_path), "media_resolution": "medium"},
                            {"file_path": str(video_path), "media_resolution": "off"},
                        ],
                    }
                ],
                media_resolution_policy={"image": "high", "pdf": "low", "video": "high"},
            )

        part_payloads = [
            part.model_dump(mode="json", by_alias=True, exclude_none=True)
            for part in contents[0].parts
        ]
        self.assertEqual(part_payloads[0]["mediaResolution"]["level"], "MEDIA_RESOLUTION_HIGH")
        self.assertEqual(part_payloads[1]["mediaResolution"]["level"], "MEDIA_RESOLUTION_MEDIUM")
        self.assertNotIn("mediaResolution", part_payloads[2])

    def test_build_contents_rejects_invalid_media_resolution_combinations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            pdf_path = temp_path / "doc.pdf"
            audio_path = temp_path / "audio.mp3"
            text_path = temp_path / "notes.txt"
            for path in (pdf_path, audio_path, text_path):
                path.write_bytes(b"data")

            invalid_cases = [
                ({"file_path": str(pdf_path), "media_resolution": "ultra_high"}, "only supported for image"),
                ({"file_path": str(audio_path), "media_resolution": "high"}, "not supported for audio"),
                ({"file_path": str(text_path), "media_resolution": "low"}, "only supported for image, PDF, or video"),
                ({"text": "hello", "media_resolution": "low"}, "not valid for text"),
            ]
            for part, message in invalid_cases:
                with self.subTest(part=part):
                    with self.assertRaisesRegex(ValueError, message):
                        gemini_client._build_contents_from_spec([{"role": "user", "parts": [part]}])

    def test_build_contents_uses_file_uri_media_resolution(self) -> None:
        contents = gemini_client._build_contents_from_spec(
            [
                {
                    "role": "user",
                    "parts": [
                        {
                            "file_uri": "gs://bucket/image.png",
                            "mime_type": "image/png",
                            "media_resolution": "ultra_high",
                        },
                        {
                            "file_uri": "gs://bucket/doc.pdf",
                            "mime_type": "application/pdf",
                        },
                    ],
                }
            ],
            media_resolution_policy={"pdf": "medium"},
        )

        part_payloads = [
            part.model_dump(mode="json", by_alias=True, exclude_none=True)
            for part in contents[0].parts
        ]
        self.assertEqual(part_payloads[0]["fileData"]["fileUri"], "gs://bucket/image.png")
        self.assertEqual(part_payloads[0]["mediaResolution"]["level"], "MEDIA_RESOLUTION_ULTRA_HIGH")
        self.assertEqual(part_payloads[1]["fileData"]["fileUri"], "gs://bucket/doc.pdf")
        self.assertEqual(part_payloads[1]["mediaResolution"]["level"], "MEDIA_RESOLUTION_MEDIUM")

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
            with patch("mcp_server.gemini_client.get_vertex_credential_count", return_value=2):
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
            with patch("mcp_server.gemini_client.get_vertex_credential_count", return_value=1):
                with self.assertRaises(gemini_client.GeminiRateLimitError) as raised:
                    gemini_client.generate(
                        prompt="hello",
                        model="gemini-3.5-flash",
                    )

        payload = raised.exception.to_dict()
        self.assertEqual(payload["error_type"], "vertex_rate_limited")
        self.assertEqual(payload["model"], "gemini-3.5-flash")
        self.assertEqual(payload["retry_after_seconds"], 12.0)
        self.assertEqual(payload["available_fallback_models"], ["gemini-3.6-flash"])

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
            with patch("mcp_server.gemini_client.get_vertex_credential_count", return_value=1):
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
            with patch("mcp_server.gemini_client.get_vertex_credential_count", return_value=1):
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
            with patch("mcp_server.gemini_client.get_vertex_credential_count", return_value=1):
                with patch("mcp_server.gemini_client.genai.Client", RateLimitedOnceClient):
                    result = gemini_client.generate(
                        prompt="hello",
                        model="gemini-3.5-flash",
                        rate_limit_mode="wait",
                    )

        self.assertEqual(len(calls), 2)
        self.assertEqual(len(acquired_leases[0].cooldowns), 1)
        self.assertEqual(result["text"], "caption")
