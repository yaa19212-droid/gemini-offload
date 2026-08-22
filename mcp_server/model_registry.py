"""Curated Gemini model capabilities used by gemini-offload.

This registry is maintainer-owned. Runtime discovery never authorizes new models.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


CapabilityStatus = Literal["supported", "unsupported", "unverified"]
ReleaseStage = Literal["stable", "preview", "deprecated"]
SelectionRole = Literal["default", "quality", "rate_limit_fallback"]

SUPPORTED: CapabilityStatus = "supported"
UNSUPPORTED: CapabilityStatus = "unsupported"
UNVERIFIED: CapabilityStatus = "unverified"

MEDIA_LOW = "low"
MEDIA_MEDIUM = "medium"
MEDIA_HIGH = "high"
MEDIA_ULTRA_HIGH = "ultra_high"


@dataclass(frozen=True)
class MediaResolutionSpec:
    status: CapabilityStatus
    image: tuple[str, ...]
    pdf: tuple[str, ...]
    video: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "image": list(self.image),
            "pdf": list(self.pdf),
            "video": list(self.video),
        }


GEMINI3_MEDIA_RESOLUTION = MediaResolutionSpec(
    status=SUPPORTED,
    image=(MEDIA_LOW, MEDIA_MEDIUM, MEDIA_HIGH, MEDIA_ULTRA_HIGH),
    pdf=(MEDIA_LOW, MEDIA_MEDIUM, MEDIA_HIGH),
    video=(MEDIA_LOW, MEDIA_MEDIUM, MEDIA_HIGH),
)


@dataclass(frozen=True)
class ModelCapability:
    model_id: str
    description: str
    release_stage: ReleaseStage
    selection_role: SelectionRole
    input_modalities: tuple[str, ...]
    output_modalities: tuple[str, ...]
    thinking_levels: tuple[str, ...]
    supports_thought_summary: CapabilityStatus
    google_search: CapabilityStatus
    json_schema: CapabilityStatus
    media_resolution: MediaResolutionSpec
    safety_off: CapabilityStatus
    vertex_location: CapabilityStatus
    replacement_model: str | None = None

    @property
    def supports_thinking(self) -> bool:
        return bool(self.thinking_levels)

    @property
    def supports_image_output(self) -> bool:
        return "image" in self.output_modalities

    def supports_input_modality(self, modality: str) -> bool:
        return modality in self.input_modalities

    def supports_media_resolution(self, kind: str, level: str) -> bool:
        allowed = getattr(self.media_resolution, kind, ())
        return level in allowed

    def supports_thinking_level(self, level: str) -> bool:
        return level in self.thinking_levels

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "release_stage": self.release_stage,
            "selection_role": self.selection_role,
            "input_modalities": list(self.input_modalities),
            "output_modalities": list(self.output_modalities),
            "thinking_levels": list(self.thinking_levels),
            "thought_summary": self.supports_thought_summary,
            "google_search": self.google_search,
            "json_schema": self.json_schema,
            "media_resolution": self.media_resolution.to_dict(),
            "safety_off": self.safety_off,
            "vertex_location": self.vertex_location,
        }
        if self.replacement_model is not None:
            payload["replacement_model"] = self.replacement_model
        return payload


MODEL_CAPABILITIES: dict[str, ModelCapability] = {
    "gemini-3.7-flash": ModelCapability(
        model_id="gemini-3.7-flash",
        description=(
            "Default model for normal gemini-offload work: latest GA Flash with "
            "strong multimodal, coding, and agentic capability."
        ),
        release_stage="stable",
        selection_role="default",
        input_modalities=("text", "image", "audio", "video", "pdf"),
        output_modalities=("text",),
        thinking_levels=("low", "medium", "high"),
        supports_thought_summary=SUPPORTED,
        google_search=SUPPORTED,
        json_schema=SUPPORTED,
        media_resolution=GEMINI3_MEDIA_RESOLUTION,
        safety_off=SUPPORTED,
        vertex_location=SUPPORTED,
    ),
    "gemini-3.1-pro-preview": ModelCapability(
        model_id="gemini-3.1-pro-preview",
        description=(
            "Quality-first option for especially difficult OCR, long-context synthesis, "
            "multimodal reasoning, and complex agentic or coding work."
        ),
        release_stage="preview",
        selection_role="quality",
        input_modalities=("text", "image", "audio", "video", "pdf"),
        output_modalities=("text",),
        thinking_levels=("low", "medium", "high"),
        supports_thought_summary=SUPPORTED,
        google_search=SUPPORTED,
        json_schema=SUPPORTED,
        media_resolution=GEMINI3_MEDIA_RESOLUTION,
        safety_off=SUPPORTED,
        vertex_location=UNVERIFIED,
    ),
    "gemini-3.6-flash": ModelCapability(
        model_id="gemini-3.6-flash",
        description=(
            "429 rate-limit fallback only. Prefer Gemini 3.7 Flash during normal operation."
        ),
        release_stage="stable",
        selection_role="rate_limit_fallback",
        input_modalities=("text", "image", "audio", "video", "pdf"),
        output_modalities=("text",),
        thinking_levels=("minimal", "low", "medium", "high"),
        supports_thought_summary=SUPPORTED,
        google_search=SUPPORTED,
        json_schema=SUPPORTED,
        media_resolution=GEMINI3_MEDIA_RESOLUTION,
        safety_off=SUPPORTED,
        vertex_location=SUPPORTED,
    ),
    "gemini-3.5-flash": ModelCapability(
        model_id="gemini-3.5-flash",
        description=(
            "Secondary 429 rate-limit fallback only. Prefer Gemini 3.7 Flash during normal operation."
        ),
        release_stage="stable",
        selection_role="rate_limit_fallback",
        input_modalities=("text", "image", "audio", "video", "pdf"),
        output_modalities=("text",),
        thinking_levels=("minimal", "low", "medium", "high"),
        supports_thought_summary=SUPPORTED,
        google_search=SUPPORTED,
        json_schema=SUPPORTED,
        media_resolution=GEMINI3_MEDIA_RESOLUTION,
        safety_off=SUPPORTED,
        vertex_location=UNVERIFIED,
    ),
}

AVAILABLE_MODEL_IDS = list(MODEL_CAPABILITIES)


def get_model_capability(model_id: str) -> ModelCapability:
    try:
        return MODEL_CAPABILITIES[model_id]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported model '{model_id}'. Use list_gemini_models to inspect supported models."
        ) from exc


def model_capabilities_dict() -> dict[str, dict[str, object]]:
    return {model: spec.to_dict() for model, spec in MODEL_CAPABILITIES.items()}


def require_supported_capability(
    model: ModelCapability,
    feature: str,
    status: CapabilityStatus,
) -> None:
    if status == SUPPORTED:
        return
    raise ValueError(
        f"Model '{model.model_id}' capability '{feature}' is {status}; "
        "choose a supported model from list_gemini_models."
    )
