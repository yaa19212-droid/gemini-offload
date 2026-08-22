"""Agent-facing projections for MCP structured results."""

from __future__ import annotations

from typing import Any, Iterable

from .model_registry import ModelCapability


def project_usage(usage: Any) -> dict[str, int]:
    """Normalize Google SDK usage metadata to stable accounting fields."""
    if not isinstance(usage, dict):
        return {}
    mapping = (
        ("input_tokens", "prompt_token_count"),
        ("output_tokens", "candidates_token_count"),
        ("thinking_tokens", "thoughts_token_count"),
        ("total_tokens", "total_token_count"),
    )
    projected: dict[str, int] = {}
    for public_name, sdk_name in mapping:
        value = usage.get(sdk_name)
        if isinstance(value, int) and not isinstance(value, bool):
            projected[public_name] = value
    return projected


def _project_media_resolution(model: ModelCapability) -> dict[str, Any]:
    spec = model.media_resolution
    return {
        "status": spec.status,
        "image": list(spec.image),
        "pdf": list(spec.pdf),
        "video": list(spec.video),
    }

def project_models(models: Iterable[ModelCapability]) -> dict[str, list[dict[str, Any]]]:
    projected: list[dict[str, Any]] = []
    for model in models:
        item: dict[str, Any] = {
            "id": model.model_id,
            "selection_role": model.selection_role,
            "release_stage": model.release_stage,
            "input_modalities": list(model.input_modalities),
            "output_modalities": list(model.output_modalities),
            "thinking_levels": list(model.thinking_levels),
            "thought_summary": model.supports_thought_summary,
            "google_search": model.google_search,
            "json_schema": model.json_schema,
            "media_resolution": _project_media_resolution(model),
        }
        if model.description.strip() and model.selection_role != "rate_limit_fallback":
            item["guidance"] = model.description.strip()
        if model.replacement_model is not None:
            item["replacement_model"] = model.replacement_model
        projected.append(item)
    return {"models": projected}


def project_setup(result: dict[str, Any]) -> dict[str, Any]:
    projected = dict(result)
    projected.pop("ready", None)
    projected.pop("credential_count", None)
    if projected.get("next_action") == "Gemini offload is ready.":
        projected.pop("next_action", None)
    if not projected.get("next_action"):
        projected.pop("next_action", None)
    credentials = []
    for item in projected.get("credentials", []):
        if not isinstance(item, dict):
            continue
        clean = dict(item)
        clean.pop("index", None)
        if clean.get("credential_path"):
            clean.pop("key_file", None)
        credentials.append(clean)
    projected["credentials"] = credentials
    return projected


def _project_item(result: dict[str, Any]) -> dict[str, Any]:
    projected = dict(result)
    projected.pop("index", None)
    projected.pop("elapsed_ms", None)
    projected.pop("char_count", None)

    usage = project_usage(projected.get("usage"))
    if usage:
        projected["usage"] = usage
    else:
        projected.pop("usage", None)

    file_backed = bool(projected.get("output_path"))
    if not file_backed:
        projected.pop("byte_count", None)
        projected.pop("line_count", None)
    if projected.get("image_count") == 0:
        projected.pop("image_count", None)
    if projected.get("truncated") is False:
        projected.pop("truncated", None)
    return projected


def project_blocking_run(result: dict[str, Any]) -> dict[str, Any]:
    projected = dict(result)
    projected.pop("run_id", None)
    projected.pop("lifecycle", None)
    projected.pop("max_concurrency", None)
    projected.pop("ok_count", None)
    projected.pop("aggregate_inline_limit", None)
    projected.pop("omitted_result_count", None)

    raw_results = projected.get("results")
    if isinstance(raw_results, list):
        projected["results"] = [
            _project_item(item) if isinstance(item, dict) else item
            for item in raw_results
        ]
        item_count = projected.get("item_count")
        if item_count == 1:
            projected.pop("item_count", None)
            projected.pop("error_count", None)
    return projected


def project_background_start(result: dict[str, Any]) -> dict[str, Any]:
    projected = dict(result)
    projected.pop("lifecycle", None)
    projected.pop("plan_path", None)
    projected.pop("max_concurrency", None)
    projected.pop("pid", None)
    return projected
