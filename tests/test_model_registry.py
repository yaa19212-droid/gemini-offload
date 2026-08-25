from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from mcp_server import gemini_client, model_registry


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
        self.calls.append({"model": model, "contents": contents, "config": config})
        return "ok"


class _RecordingClient:
    def __init__(self) -> None:
        self.models = _RecordingModels()


class ModelRegistryTests(unittest.TestCase):
    def test_registry_is_authority_for_compatibility_surfaces(self) -> None:
        self.assertEqual(model_registry.AVAILABLE_MODEL_IDS, EXPECTED_MODELS)
        self.assertIs(gemini_client.MODEL_SPECS, model_registry.MODEL_CAPABILITIES)
        self.assertEqual(gemini_client.AVAILABLE_MODELS, EXPECTED_MODELS)
        self.assertEqual(
            gemini_client.MODEL_CHARACTERISTICS,
            {model: spec.description for model, spec in model_registry.MODEL_CAPABILITIES.items()},
        )

    def test_public_matrix_is_compact_and_machine_readable(self) -> None:
        matrix = gemini_client.MODEL_CAPABILITY_MATRIX
        self.assertEqual(list(matrix), EXPECTED_MODELS)
        for model_id, capability in matrix.items():
            with self.subTest(model=model_id):
                self.assertNotIn("description", capability)
                self.assertIn(capability["release_stage"], {"stable", "preview", "deprecated"})
                self.assertIn(capability["selection_role"], {"default", "quality", "rate_limit_fallback"})
                self.assertEqual(capability["google_search"], "supported")
                self.assertEqual(capability["json_schema"], "supported")
                self.assertIn("text", capability["input_modalities"])
                self.assertIn("media_resolution", capability)

    def test_selection_roles_match_product_policy(self) -> None:
        self.assertEqual(model_registry.MODEL_CAPABILITIES["gemini-3.7-flash"].selection_role, "default")
        self.assertEqual(model_registry.MODEL_CAPABILITIES["gemini-3.1-pro-preview"].selection_role, "quality")
        self.assertEqual(model_registry.MODEL_CAPABILITIES["gemini-3.6-flash"].selection_role, "rate_limit_fallback")
        self.assertEqual(model_registry.MODEL_CAPABILITIES["gemini-3.5-flash"].selection_role, "rate_limit_fallback")
        self.assertNotIn("gemini-3-flash-preview", model_registry.MODEL_CAPABILITIES)

    def test_unverified_google_search_fails_before_api_call(self) -> None:
        base = model_registry.MODEL_CAPABILITIES["gemini-3.5-flash"]
        replacement = replace(base, google_search=model_registry.UNVERIFIED)
        client = _RecordingClient()
        with patch.dict(model_registry.MODEL_CAPABILITIES, {base.model_id: replacement}):
            with self.assertRaisesRegex(ValueError, "google_search.*unverified"):
                gemini_client._call_api(
                    client=client,
                    model_name=base.model_id,
                    contents_list=[],
                    system_prompt="system",
                    include_thinking=False,
                    google_search=True,
                )
        self.assertEqual(client.models.calls, [])


    def test_refreshed_flash_capabilities_match_verified_contracts(self) -> None:
        flash_37 = model_registry.MODEL_CAPABILITIES["gemini-3.7-flash"]
        flash_36 = model_registry.MODEL_CAPABILITIES["gemini-3.6-flash"]
        self.assertEqual(flash_37.release_stage, "stable")
        self.assertEqual(flash_36.release_stage, "stable")
        self.assertEqual(flash_37.thinking_levels, ("low", "medium", "high"))
        self.assertEqual(
            flash_36.thinking_levels,
            ("minimal", "low", "medium", "high"),
        )
        for capability in (flash_37, flash_36):
            with self.subTest(model=capability.model_id):
                self.assertEqual(capability.vertex_location, model_registry.SUPPORTED)
                self.assertEqual(capability.google_search, model_registry.SUPPORTED)
                self.assertEqual(capability.json_schema, model_registry.SUPPORTED)
                self.assertTrue(capability.supports_input_modality("image"))
                self.assertTrue(capability.supports_input_modality("pdf"))
                self.assertTrue(capability.supports_media_resolution("image", "high"))
                self.assertTrue(capability.supports_media_resolution("pdf", "high"))
