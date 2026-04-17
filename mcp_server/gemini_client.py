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

from .keys import get_next_api_key


DEFAULT_MODEL_NAME = "gemini-3.1-pro-preview"

AVAILABLE_MODELS = [
    "gemini-3.1-pro-preview",
    "gemini-3-pro-preview",
    "gemini-3.0-pro",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-preview-09-2025",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash-lite-preview-09-2025",
    "gemini-2.5-flash-image",
]

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
    """Extract text from API response, excluding thinking parts."""

    answer_parts: list[str] = []

    try:
        if hasattr(response, "candidates") and len(response.candidates) > 0:
            candidate = response.candidates[0]
            if hasattr(candidate, "content") and hasattr(candidate.content, "parts"):
                parts = candidate.content.parts or []
                for part in parts:
                    part_text = getattr(part, "text", None)
                    if isinstance(part_text, str) and part_text and not getattr(part, "thought", False):
                        answer_parts.append(part_text)

        response_text = getattr(response, "text", None) if hasattr(response, "text") else None
        if not answer_parts and isinstance(response_text, str) and response_text:
            answer_parts.append(response_text)

    except Exception:
        response_text = getattr(response, "text", None) if hasattr(response, "text") else None
        if isinstance(response_text, str) and response_text:
            answer_parts.append(response_text)
        elif response is not None:
            answer_parts.append(str(response))

    answer_text = "".join(part for part in answer_parts if isinstance(part, str)).strip()
    return answer_text if answer_text else "[Empty response]"


def _upload_file(file_path: str, client: genai.Client):
    """Upload file to Gemini API with MIME type detection."""

    path_obj = sanitize_path(file_path)
    if not path_obj.exists():
        raise FileNotFoundError(f"Missing file: {file_path}")
    if not path_obj.is_file():
        raise ValueError(f"Path is not a file: {file_path}")

    detected_mime = detect_mime_type(str(path_obj))
    if not is_supported_mime(detected_mime):
        raise ValueError(f"Unsupported MIME type for file '{file_path}': {detected_mime}")

    with open(path_obj, "rb") as file_handle:
        uploaded_file = client.files.upload(
            file=file_handle,
            config=dict(mime_type=detected_mime, display_name=path_obj.name),
        )

    return uploaded_file


def _call_api(
    client: genai.Client,
    model_name: str,
    contents_list: list[types.Content],
    system_prompt: str,
    include_thinking: bool,
):
    """Call Gemini API with the prepared contents."""

    if model_name.startswith("gemini-3"):
        if include_thinking:
            thinking_config = types.ThinkingConfig(thinking_level="HIGH")
        else:
            thinking_config = None
    else:
        thinking_config = types.ThinkingConfig(include_thoughts=include_thinking)

    config_kwargs = {"system_instruction": system_prompt}
    if thinking_config is not None:
        config_kwargs["thinking_config"] = thinking_config

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


def _call_with_retry(operation, max_attempts: int = 2):
    last_exception: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except Exception as exc:
            last_exception = exc
            if attempt >= max_attempts or not _is_transient_error(exc):
                raise
            time.sleep(min(attempt, 2))

    raise last_exception or RuntimeError("Operation failed without raising an exception.")


def _build_contents(
    prompt: str,
    uploaded_files: list[Any],
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

    prompt_parts = [
        types.Part.from_uri(file_uri=file_obj.uri, mime_type=file_obj.mime_type)
        for file_obj in uploaded_files
    ]
    prompt_parts.append(types.Part.from_text(text=prompt))
    contents.append(types.Content(role="user", parts=prompt_parts))

    return contents


def generate(
    prompt: str,
    files: list[str] | None = None,
    system_prompt: str | None = None,
    model: str = DEFAULT_MODEL_NAME,
    include_thinking: bool = False,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute a single Gemini request from prompt plus local files."""

    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string.")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string.")
    if model not in AVAILABLE_MODELS:
        raise ValueError(f"Unsupported model '{model}'. Use list_gemini_models to inspect supported models.")
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

    client = genai.Client(api_key=get_next_api_key())
    started_at = time.perf_counter()

    uploaded_files = [
        _call_with_retry(lambda path=file_path: _upload_file(path, client))
        for file_path in requested_files
    ]
    contents = _build_contents(prompt=prompt, uploaded_files=uploaded_files, history=normalized_history)
    response = _call_with_retry(
        lambda: _call_api(
            client=client,
            model_name=model,
            contents_list=contents,
            system_prompt=effective_system_prompt,
            include_thinking=include_thinking,
        )
    )

    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    return {
        "text": _extract_response_text(response),
        "model": _extract_response_model(response, model),
        "usage": _extract_usage(response),
        "elapsed_ms": elapsed_ms,
    }
