from __future__ import annotations

import base64
import datetime
import json
import mimetypes
import os
import pathlib
import tempfile
import uuid
from typing import Any

from .artifacts import atomic_write_bytes as _atomic_write_bytes, atomic_write_text as _atomic_write_text

INLINE_OUTPUT_BYTE_LIMIT = 4096
SPILL_PREVIEW_CHARS = 100
ENV_OUTPUT_DIR = "GEMINI_OFFLOAD_OUTPUT_DIR"
IMAGE_EXTENSION_OVERRIDES = {"image/jpeg": ".jpg"}

def _write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))

def _line_count(text: str) -> int:
    return text.count("\n") + 1 if text else 0

def _resolve_auto_output_dir() -> pathlib.Path:
    configured = os.environ.get(ENV_OUTPUT_DIR)
    if configured is not None and configured.strip():
        output_dir = pathlib.Path(configured)
        if not output_dir.is_absolute():
            raise ValueError(f"{ENV_OUTPUT_DIR} must be an absolute path: {configured}")
    else:
        output_dir = pathlib.Path(tempfile.gettempdir()) / "gemini-offload" / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir.resolve()

def _new_auto_output_path(extension: str, *, prefix: str = "response") -> pathlib.Path:
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    suffix = extension if extension.startswith(".") else f".{extension}"
    return _resolve_auto_output_dir() / f"{prefix}-{timestamp}-{uuid.uuid4().hex[:8]}{suffix}"

def _resolve_output_path(output_path: Any, extension: str) -> pathlib.Path:
    if output_path is not None:
        if not isinstance(output_path, str) or not output_path.strip():
            raise ValueError("output_path must be a non-empty string when provided.")
        path_obj = pathlib.Path(output_path)
        if not path_obj.is_absolute():
            raise ValueError(f"output_path must be absolute: {output_path}")
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        return path_obj
    return _new_auto_output_path(extension)

def _write_text_output(full_text: str, output_path: Any, extension: str) -> pathlib.Path:
    path_obj = _resolve_output_path(output_path, extension)
    _atomic_write_text(path_obj, full_text)
    return path_obj

def _write_auto_json_payload(payload: dict[str, Any], *, prefix: str) -> pathlib.Path:
    path_obj = _new_auto_output_path(".json", prefix=prefix)
    _write_json(path_obj, payload)
    return path_obj

def _add_read_guidance(result: dict[str, Any]) -> None:
    result["read_guidance"] = (
        f"Full response is {result['byte_count']} bytes across {result['line_count']} lines "
        "and was saved to output_path. Avoid reading the entire file unless needed; "
        "inspect targeted sections or ranges first."
    )

def _apply_text_output(
    result: dict[str, Any],
    full_text: str,
    output_path: Any,
    *,
    preview_field: str,
    extension: str,
    force_file: bool = False,
) -> pathlib.Path | None:
    byte_count = result["byte_count"]
    should_write = force_file or output_path is not None or byte_count > INLINE_OUTPUT_BYTE_LIMIT
    if not should_write:
        result["text"] = full_text
        result["truncated"] = False
        return None

    path_obj = _write_text_output(full_text, output_path, extension)
    result["output_path"] = str(path_obj)
    result[preview_field] = full_text[:SPILL_PREVIEW_CHARS]
    result["truncated"] = len(full_text) > SPILL_PREVIEW_CHARS
    if byte_count > INLINE_OUTPUT_BYTE_LIMIT:
        _add_read_guidance(result)
    return path_obj

def _apply_json_output(
    result: dict[str, Any],
    full_text: str,
    output_path: Any,
    *,
    force_file: bool = False,
) -> pathlib.Path | None:
    try:
        parsed = json.loads(full_text)
    except json.JSONDecodeError as exc:
        result["response_json_error"] = f"{type(exc).__name__}: {exc}"
        return _apply_text_output(
            result,
            full_text,
            output_path,
            preview_field="text_preview",
            extension=".txt",
            force_file=force_file,
        )

    byte_count = result["byte_count"]
    if not force_file and output_path is None and byte_count <= INLINE_OUTPUT_BYTE_LIMIT:
        result["response_json"] = parsed
        result["truncated"] = False
        return None

    path_obj = _write_text_output(full_text, output_path, ".json")
    result["output_path"] = str(path_obj)
    result["response_json_preview"] = full_text[:SPILL_PREVIEW_CHARS]
    result["truncated"] = len(full_text) > SPILL_PREVIEW_CHARS
    if byte_count > INLINE_OUTPUT_BYTE_LIMIT:
        _add_read_guidance(result)
    return path_obj

def _apply_output_policy(
    result: dict[str, Any],
    output_path: Any,
    *,
    expect_json_response: bool = False,
    force_file: bool = False,
) -> dict[str, Any]:
    full_text: str = result.pop("text", "") or ""
    raw_images = result.pop("images", []) or []
    char_count = len(full_text)
    byte_count = len(full_text.encode("utf-8"))
    trimmed = dict(result)
    trimmed["char_count"] = char_count
    trimmed["byte_count"] = byte_count
    trimmed["line_count"] = _line_count(full_text)
    trimmed["image_count"] = 0

    if expect_json_response:
        path_obj = _apply_json_output(trimmed, full_text, output_path, force_file=force_file)
    else:
        path_obj = _apply_text_output(
            trimmed,
            full_text,
            output_path,
            preview_field="text_preview",
            extension=".txt",
            force_file=force_file,
        )

    image_summaries: list[dict[str, Any]] = []
    inline_images: list[dict[str, str]] = []
    for idx, image in enumerate(raw_images, start=1):
        mime_type = image.get("mime_type")
        image_bytes = image.get("data")
        if isinstance(image_bytes, bytearray):
            image_bytes = bytes(image_bytes)
        if not isinstance(mime_type, str) or not mime_type.startswith("image/"):
            raise ValueError(f"Unsupported response image MIME type: {mime_type!r}")
        if not isinstance(image_bytes, bytes) or not image_bytes:
            raise ValueError("Response images must include non-empty bytes data.")

        image_summary = {
            "index": idx,
            "mime_type": mime_type,
            "byte_count": len(image_bytes),
        }
        if path_obj is not None:
            image_path = _build_image_output_path(path_obj, idx, mime_type)
            _atomic_write_bytes(image_path, image_bytes)
            image_summary["output_path"] = str(image_path)
        else:
            inline_images.append(
                {
                    "mime_type": mime_type,
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }
            )
        image_summaries.append(image_summary)

    trimmed["image_count"] = len(image_summaries)
    if image_summaries:
        trimmed["images"] = image_summaries
    if inline_images:
        trimmed["_inline_images"] = inline_images
    return trimmed

def _build_image_output_path(
    output_path: pathlib.Path,
    image_index: int,
    mime_type: str,
) -> pathlib.Path:
    image_extension = IMAGE_EXTENSION_OVERRIDES.get(mime_type)
    if image_extension is None:
        image_extension = mimetypes.guess_extension(mime_type) or ".bin"

    image_stem = output_path.stem if output_path.suffix else output_path.name
    image_name = f"{image_stem}.image-{image_index}{image_extension}"
    return output_path.with_name(image_name)
