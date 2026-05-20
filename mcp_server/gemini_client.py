"""Standalone Gemini helpers extracted from the GUI processor."""

from __future__ import annotations

import json
import pathlib
import time
from dataclasses import dataclass
from typing import Any

import httpx
from google import genai
from google.genai import types

from .keys import (
    DEFAULT_KEY_COOLDOWN_SECONDS,
    ApiKeyLease,
    NoAvailableQuotaSlotError,
    acquire_vertex_credential_lease,
    get_key_count,
)


DEFAULT_MODEL_NAME = "gemini-3.1-pro-preview"
BLOCKED_MODEL_PREFIXES = ("gemini-2.5",)
RATE_LIMIT_MODE_FAIL_FAST = "fail_fast"
RATE_LIMIT_MODE_WAIT = "wait"
RATE_LIMIT_MODES = {RATE_LIMIT_MODE_FAIL_FAST, RATE_LIMIT_MODE_WAIT}
DEFAULT_RATE_LIMIT_MAX_WAIT_SECONDS = 120.0


@dataclass(frozen=True)
class ModelSpec:
    supports_thinking: bool = True
    supports_image_output: bool = False
    description: str = ""


MODEL_SPECS: dict[str, ModelSpec] = {
    "gemini-3.1-pro-preview": ModelSpec(
        description=(
            "Best overall quality for complex OCR, long-context synthesis, "
            "multimodal reasoning, and difficult agentic or coding work."
        ),
    ),
    "gemini-3-flash-preview": ModelSpec(
        description=(
            "Emergency fallback when the primary model is unavailable or too slow; "
            "keeps Gemini 3 reasoning and multimodal coverage with Flash latency."
        ),
    ),
    "gemini-3.5-flash": ModelSpec(
        description=(
            "Fast default for throughput-sensitive jobs; near-Pro agentic and coding "
            "capability at Flash speed, and better than 3.1 Pro for some workloads."
        ),
    ),
}

AVAILABLE_MODELS = list(MODEL_SPECS)
MODEL_CHARACTERISTICS = {
    model_name: spec.description for model_name, spec in MODEL_SPECS.items()
}


class GeminiRateLimitError(RuntimeError):
    """Structured rate-limit failure returned to MCP callers."""

    def __init__(
        self,
        *,
        model: str,
        attempted_models: list[str],
        retry_after_seconds: float,
        quota_slots: list[str],
    ) -> None:
        self.model = model
        self.attempted_models = attempted_models
        self.retry_after_seconds = retry_after_seconds
        self.quota_slots = quota_slots
        self.available_fallback_models = [
            model_name
            for model_name in AVAILABLE_MODELS
            if model_name not in attempted_models
        ]
        super().__init__(self.message)

    @property
    def message(self) -> str:
        fallback_note = (
            f" Try fallback_models={self.available_fallback_models}."
            if self.available_fallback_models
            else ""
        )
        return (
            f"Vertex quota slot for {self.model} is cooling down. "
            f"Retry after about {self.retry_after_seconds:.1f} seconds, "
            "or set rate_limit_mode='wait' to let the server wait before retrying."
            f"{fallback_note}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": "vertex_rate_limited",
            "message": self.message,
            "model": self.model,
            "attempted_models": self.attempted_models,
            "retry_after_seconds": self.retry_after_seconds,
            "quota_slots": self.quota_slots,
            "available_fallback_models": self.available_fallback_models,
            "recommendation": (
                "Retry later, set rate_limit_mode='wait', or pass fallback_models "
                "with another supported Gemini model."
            ),
        }

MIME_TYPE_MAP = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
}

DEFAULT_SYSTEM_PROMPT = (
    "You are a multimodal content processing engine. "
    "Return only the requested result text with no extra commentary."
)


@dataclass(frozen=True)
class HistoryTurn:
    role: str
    text: str


def detect_mime_type(file_path: str) -> str:
    """Detect MIME type from file extension."""

    ext = pathlib.Path(file_path).suffix.lower()
    return MIME_TYPE_MAP.get(ext, "application/octet-stream")


def sanitize_path(path: str) -> pathlib.Path:
    """Resolve a local absolute path and reject non-absolute inputs."""

    try:
        raw_path = pathlib.Path(path)
    except Exception as exc:
        raise ValueError(f"Invalid path: {path}. Error: {exc}") from exc

    if not raw_path.is_absolute():
        raise ValueError(f"Path must be absolute: {path}")

    try:
        return raw_path.resolve()
    except Exception as exc:
        raise ValueError(f"Invalid path: {path}. Error: {exc}") from exc


def is_supported_mime(mime_type: str) -> bool:
    return mime_type in MIME_TYPE_MAP.values()


def _assert_allowed_model(model_name: str) -> None:
    if any(model_name.startswith(prefix) for prefix in BLOCKED_MODEL_PREFIXES):
        raise ValueError(
            f"Blocked outdated Gemini model '{model_name}'. "
            f"Use one of: {', '.join(AVAILABLE_MODELS)}."
        )
    if model_name not in AVAILABLE_MODELS:
        raise ValueError(f"Unsupported model '{model_name}'. Use list_gemini_models to inspect supported models.")


def _normalize_rate_limit_mode(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("rate_limit_mode must be one of: fail_fast, wait.")
    normalized = value.strip().lower()
    if normalized not in RATE_LIMIT_MODES:
        raise ValueError("rate_limit_mode must be one of: fail_fast, wait.")
    return normalized


def _normalize_rate_limit_max_wait_seconds(value: float | int | None) -> float:
    if value is None:
        return DEFAULT_RATE_LIMIT_MAX_WAIT_SECONDS
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("rate_limit_max_wait_seconds must be a non-negative number.")
    if value < 0:
        raise ValueError("rate_limit_max_wait_seconds must be a non-negative number.")
    return float(value)


def _normalize_model_sequence(model: str, fallback_models: list[str] | None) -> list[str]:
    _assert_allowed_model(model)
    sequence = [model]
    if fallback_models is None:
        return sequence
    if not isinstance(fallback_models, list):
        raise ValueError("fallback_models must be an array of supported model names.")

    seen = {model}
    for idx, fallback_model in enumerate(fallback_models, start=1):
        if not isinstance(fallback_model, str) or not fallback_model.strip():
            raise ValueError(f"fallback_models[{idx}] must be a non-empty string.")
        normalized_model = fallback_model.strip()
        _assert_allowed_model(normalized_model)
        if normalized_model not in seen:
            seen.add(normalized_model)
            sequence.append(normalized_model)
    return sequence


def detect_mime(path: str) -> dict[str, Any]:
    """Validate a path and return its MIME metadata."""

    path_obj = sanitize_path(path)
    if not path_obj.exists():
        raise FileNotFoundError(f"Missing file: {path}")
    if not path_obj.is_file():
        raise ValueError(f"Path is not a file: {path}")

    mime_type = detect_mime_type(str(path_obj))
    return {
        "mime": mime_type,
        "supported": is_supported_mime(mime_type),
    }


def _normalize_history(history: list[dict[str, Any]] | None) -> list[HistoryTurn]:
    if not history:
        return []

    normalized: list[HistoryTurn] = []
    for idx, item in enumerate(history, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"history[{idx}] must be an object with role and text.")

        role = item.get("role")
        text = item.get("text")

        if not isinstance(role, str) or not role.strip():
            raise ValueError(f"history[{idx}].role must be a non-empty string.")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"history[{idx}].text must be a non-empty string.")

        normalized_role = role.strip().lower()
        if normalized_role == "assistant":
            normalized_role = "model"
        if normalized_role not in {"user", "model"}:
            raise ValueError("History role must be one of: user, model, assistant.")

        normalized.append(HistoryTurn(role=normalized_role, text=text))

    return normalized


def _serialize_response_to_data(response) -> dict[str, Any]:
    """Best-effort conversion of a Gemini response object into JSON-safe data."""

    data: Any = None

    if hasattr(response, "model_dump_json"):
        try:
            data = json.loads(response.model_dump_json(indent=2))
        except Exception:
            data = None

    if data is None and hasattr(response, "model_dump"):
        try:
            data = response.model_dump(mode="json")
        except TypeError:
            data = response.model_dump()
        except Exception:
            data = None

    if not isinstance(data, dict):
        data = {"raw_response": str(response)}

    return data


def _extract_usage(response) -> dict[str, Any]:
    data = _serialize_response_to_data(response)
    usage = data.get("usage_metadata") or data.get("usageMetadata")
    return usage if isinstance(usage, dict) else {}


def _extract_response_model(response, requested_model: str) -> str:
    data = _serialize_response_to_data(response)
    response_model = data.get("model_version") or data.get("modelVersion")
    return response_model if isinstance(response_model, str) and response_model else requested_model


def _extract_response_text(response) -> str:
    payload = _extract_response_payload(response)
    return payload["text"]


def _iter_response_parts(response) -> list[Any]:
    parts = getattr(response, "parts", None)
    if parts:
        return list(parts)

    candidates = getattr(response, "candidates", None) or []
    if candidates:
        candidate = candidates[0]
        content = getattr(candidate, "content", None)
        candidate_parts = getattr(content, "parts", None) or []
        return list(candidate_parts)

    return []


def _extract_response_payload(response) -> dict[str, Any]:
    """Extract text and image parts from the API response."""

    answer_parts: list[str] = []
    images: list[dict[str, Any]] = []

    try:
        for part in _iter_response_parts(response):
            part_text = getattr(part, "text", None)
            if isinstance(part_text, str) and part_text and not getattr(part, "thought", False):
                answer_parts.append(part_text)

            inline_data = getattr(part, "inline_data", None)
            mime_type = getattr(inline_data, "mime_type", None)
            image_bytes = getattr(inline_data, "data", None)
            if isinstance(image_bytes, bytearray):
                image_bytes = bytes(image_bytes)
            if (
                isinstance(mime_type, str)
                and mime_type.startswith("image/")
                and isinstance(image_bytes, bytes)
                and image_bytes
            ):
                images.append({"mime_type": mime_type, "data": image_bytes})

        response_text = getattr(response, "text", None) if hasattr(response, "text") else None
        if not answer_parts and isinstance(response_text, str) and response_text:
            answer_parts.append(response_text)

    except Exception:
        response_text = getattr(response, "text", None) if hasattr(response, "text") else None
        if isinstance(response_text, str) and response_text:
            answer_parts.append(response_text)
        elif response is not None and not images:
            answer_parts.append(str(response))

    answer_text = "".join(part for part in answer_parts if isinstance(part, str)).strip()
    return {
        "text": answer_text if answer_text else "",
        "images": images,
    }


def _load_file_part(file_path: str) -> types.Part:
    """Load a local file as an inline part for Vertex AI Gemini."""

    path_obj = sanitize_path(file_path)
    if not path_obj.exists():
        raise FileNotFoundError(f"Missing file: {file_path}")
    if not path_obj.is_file():
        raise ValueError(f"Path is not a file: {file_path}")

    detected_mime = detect_mime_type(str(path_obj))
    if not is_supported_mime(detected_mime):
        raise ValueError(f"Unsupported MIME type for file '{file_path}': {detected_mime}")

    return types.Part.from_bytes(data=path_obj.read_bytes(), mime_type=detected_mime)


def _call_api(
    client: genai.Client,
    model_name: str,
    contents_list: list[types.Content],
    system_prompt: str,
    include_thinking: bool,
):
    """Call Gemini API with the prepared contents."""

    _assert_allowed_model(model_name)
    model_spec = MODEL_SPECS.get(model_name, ModelSpec())

    config_kwargs = {"system_instruction": system_prompt}
    if model_spec.supports_image_output:
        config_kwargs["response_modalities"] = ["TEXT", "IMAGE"]
    if include_thinking and model_spec.supports_thinking:
        config_kwargs["thinking_config"] = types.ThinkingConfig(include_thoughts=True)

    config = types.GenerateContentConfig(**config_kwargs)
    return client.models.generate_content(
        model=model_name,
        contents=contents_list,
        config=config,
    )


def _is_transient_error(exc: Exception) -> bool:
    if isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            httpx.ProxyError,
        ),
    ):
        return True

    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int) and status_code in {408, 429, 500, 502, 503, 504}:
        return True

    message = str(exc).lower()
    transient_markers = (
        "timeout",
        "temporar",
        "connection reset",
        "connection aborted",
        "service unavailable",
        "rate limit",
        "too many requests",
    )
    return any(marker in message for marker in transient_markers)


def _status_code(exc: Exception) -> int | None:
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    return status_code if isinstance(status_code, int) else None


def _is_rate_limit_error(exc: Exception) -> bool:
    return _status_code(exc) == 429 or "rate limit" in str(exc).lower()


def _retry_after_seconds(exc: Exception) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return DEFAULT_KEY_COOLDOWN_SECONDS

    retry_after = None
    try:
        retry_after = headers.get("retry-after")
    except Exception:
        retry_after = None

    if retry_after is None:
        return DEFAULT_KEY_COOLDOWN_SECONDS

    try:
        return max(float(retry_after), 1.0)
    except ValueError:
        return DEFAULT_KEY_COOLDOWN_SECONDS


def _call_with_retry(operation, max_attempts: int = 2):
    last_exception: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except Exception as exc:
            last_exception = exc
            if _is_rate_limit_error(exc) or attempt >= max_attempts or not _is_transient_error(exc):
                raise
            time.sleep(min(attempt, 2))

    raise last_exception or RuntimeError("Operation failed without raising an exception.")


def _build_contents(
    prompt: str,
    file_parts: list[types.Part],
    history: list[HistoryTurn],
) -> list[types.Content]:
    contents: list[types.Content] = []

    for turn in history:
        contents.append(
            types.Content(
                role=turn.role,
                parts=[types.Part.from_text(text=turn.text)],
            )
        )

    prompt_parts = list(file_parts)
    prompt_parts.append(types.Part.from_text(text=prompt))
    contents.append(types.Content(role="user", parts=prompt_parts))

    return contents


def _generate_with_lease(
    prompt: str,
    requested_files: list[str],
    effective_system_prompt: str,
    model: str,
    include_thinking: bool,
    normalized_history: list[HistoryTurn],
    lease: ApiKeyLease,
) -> dict[str, Any]:
    client = genai.Client(
        vertexai=True,
        credentials=lease.credentials,
        project=lease.project_id,
        location=lease.location,
    )
    started_at = time.perf_counter()

    file_parts = [
        _call_with_retry(lambda path=file_path: _load_file_part(path))
        for file_path in requested_files
    ]
    contents = _build_contents(prompt=prompt, file_parts=file_parts, history=normalized_history)
    response = _call_with_retry(
        lambda: _call_api(
            client=client,
            model_name=model,
            contents_list=contents,
            system_prompt=effective_system_prompt,
            include_thinking=include_thinking,
        )
    )
    payload = _extract_response_payload(response)

    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    return {
        "text": payload["text"],
        "images": payload["images"],
        "model": _extract_response_model(response, model),
        "usage": _extract_usage(response),
        "elapsed_ms": elapsed_ms,
    }


def generate(
    prompt: str,
    files: list[str] | None = None,
    system_prompt: str | None = None,
    model: str = DEFAULT_MODEL_NAME,
    include_thinking: bool = False,
    history: list[dict[str, Any]] | None = None,
    rate_limit_mode: str = RATE_LIMIT_MODE_FAIL_FAST,
    fallback_models: list[str] | None = None,
    rate_limit_max_wait_seconds: float | int | None = None,
) -> dict[str, Any]:
    """Execute a single Gemini request from prompt plus local files."""

    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string.")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string.")
    primary_model = model.strip()
    model_sequence = _normalize_model_sequence(primary_model, fallback_models)
    normalized_rate_limit_mode = _normalize_rate_limit_mode(rate_limit_mode)
    max_wait_seconds = _normalize_rate_limit_max_wait_seconds(rate_limit_max_wait_seconds)
    if files is not None and not isinstance(files, list):
        raise ValueError("files must be a list of absolute file paths.")
    if not isinstance(include_thinking, bool):
        raise ValueError("include_thinking must be a boolean.")

    normalized_history = _normalize_history(history)
    requested_files = files or []
    for file_path in requested_files:
        if not isinstance(file_path, str) or not file_path.strip():
            raise ValueError("files must contain only non-empty string paths.")
        detect_mime(file_path)

    effective_system_prompt = (
        system_prompt.strip()
        if isinstance(system_prompt, str) and system_prompt.strip()
        else DEFAULT_SYSTEM_PROMPT
    )

    attempted_models: list[str] = []
    last_rate_limit: GeminiRateLimitError | None = None
    max_key_attempts = max(1, get_key_count())
    wait_for_cooldown = normalized_rate_limit_mode == RATE_LIMIT_MODE_WAIT
    attempts_per_model = max_key_attempts * (2 if wait_for_cooldown else 1)

    for active_model in model_sequence:
        attempted_models.append(active_model)
        for _ in range(attempts_per_model):
            try:
                with acquire_vertex_credential_lease(
                    model=active_model,
                    wait_for_cooldown=wait_for_cooldown,
                    max_wait_seconds=max_wait_seconds,
                ) as acquired:
                    try:
                        return _generate_with_lease(
                            prompt=prompt,
                            requested_files=requested_files,
                            effective_system_prompt=effective_system_prompt,
                            model=active_model,
                            include_thinking=include_thinking,
                            normalized_history=normalized_history,
                            lease=acquired.lease,
                        )
                    except Exception as exc:
                        if not _is_rate_limit_error(exc):
                            raise
                        acquired.mark_cooldown(_retry_after_seconds(exc))
                        last_rate_limit = GeminiRateLimitError(
                            model=active_model,
                            attempted_models=list(attempted_models),
                            retry_after_seconds=_retry_after_seconds(exc),
                            quota_slots=[acquired.quota_slot],
                        )
            except NoAvailableQuotaSlotError as exc:
                last_rate_limit = GeminiRateLimitError(
                    model=active_model,
                    attempted_models=list(attempted_models),
                    retry_after_seconds=exc.retry_after_seconds,
                    quota_slots=exc.quota_slots,
                )
                break

        if last_rate_limit is None:
            continue

    raise last_rate_limit or RuntimeError("Gemini request failed without raising an exception.")
