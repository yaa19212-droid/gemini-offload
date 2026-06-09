"""Low-level stdio MCP server for Gemini offload."""

from __future__ import annotations

import base64
import datetime
import json
import mimetypes
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any

import anyio
import mcp.types as mcp_types
import psutil
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError

from .gemini_client import (
    AVAILABLE_MODELS,
    DEFAULT_MODEL_NAME,
    DEFAULT_MEDIA_RESOLUTION_POLICY,
    DEFAULT_RATE_LIMIT_MAX_WAIT_SECONDS,
    GeminiRateLimitError,
    MEDIA_RESOLUTION_INPUT_VALUES,
    MEDIA_RESOLUTION_POLICY_KEYS,
    MODEL_CHARACTERISTICS,
    RATE_LIMIT_MODE_FAIL_FAST,
    detect_mime_type,
    detect_mime,
    generate_request,
    is_supported_mime,
    normalize_media_resolution_override,
    normalize_media_resolution_policy,
    validate_media_resolution_for_mime,
)
from .keys import get_key_count


SERVER_NAME = "gemini-offload"
SERVER_VERSION = "0.1.0"


def _raise_mcp_error(code: int, message: str, data: Any = None) -> None:
    raise McpError(mcp_types.ErrorData(code=code, message=message, data=data))


INLINE_OUTPUT_BYTE_LIMIT = 4096
BATCH_AGGREGATE_BYTE_LIMIT = 4096
SPILL_PREVIEW_CHARS = 100
ENV_OUTPUT_DIR = "GEMINI_OFFLOAD_OUTPUT_DIR"
ENV_RUN_DIR = "GEMINI_OFFLOAD_RUN_DIR"
IMAGE_EXTENSION_OVERRIDES = {
    "image/jpeg": ".jpg",
}
MAX_BATCH_CONCURRENCY = 32
PLACEHOLDER_RE = re.compile(r"\{\{([^{}\r\n]+)\}\}")
PLACEHOLDER_ALLOWED_PUNCTUATION = set("_-.()[]@+=,~ ")
PLACEHOLDER_FORBIDDEN_CHARS = set('<>:"/\\|?*{}')


class WorkerOwnershipLost(RuntimeError):
    """Raised when a background worker is no longer the active run owner."""


def _load_json_schema(inline: Any, path: Any) -> Any:
    if path is None:
        if inline is None:
            return None
        if not isinstance(inline, dict):
            raise ValueError("output.json_schema must be a JSON object.")
        return inline
    if inline is not None:
        raise ValueError("Pass either output.json_schema or output.json_schema_path, not both.")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("output.json_schema_path must be a non-empty string.")
    p = pathlib.Path(path)
    if not p.is_absolute():
        raise ValueError(f"output.json_schema_path must be absolute: {path}")
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"output.json_schema_path file must contain valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("output.json_schema_path file must contain a JSON object.")
    return data


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


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _resolve_run_root() -> pathlib.Path:
    configured = os.environ.get(ENV_RUN_DIR)
    if configured is not None and configured.strip():
        run_root = pathlib.Path(configured)
        if not run_root.is_absolute():
            raise ValueError(f"{ENV_RUN_DIR} must be an absolute path: {configured}")
    else:
        run_root = pathlib.Path(tempfile.gettempdir()) / "gemini-offload" / "runs"
    run_root.mkdir(parents=True, exist_ok=True)
    return run_root.resolve()


def _new_run_id() -> str:
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"run-{timestamp}-{uuid.uuid4().hex[:8]}"


def _write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _read_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} contains invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return data


def _append_event(run_dir: pathlib.Path, event: dict[str, Any]) -> None:
    event_payload = {
        "timestamp": _utc_now(),
        "source": "gemini-offload",
        **event,
    }
    events_path = run_dir / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event_payload, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _locator_matches_worker(run_dir: pathlib.Path, run_id: str, run_token: str) -> bool:
    locator_path = run_dir / "locator.json"
    if not locator_path.exists():
        return False
    try:
        locator = _read_json(locator_path)
    except Exception:
        return False
    return locator.get("run_id") == run_id and locator.get("run_token") == run_token


def _ensure_worker_owns_run(run_dir: pathlib.Path | None, run_id: str, run_token: str | None) -> None:
    if run_dir is None or run_token is None:
        return
    if not _locator_matches_worker(run_dir, run_id, run_token):
        raise WorkerOwnershipLost(f"Worker no longer owns run {run_id}.")


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
    path_obj.write_bytes(full_text.encode("utf-8"))
    return path_obj


def _write_auto_json_payload(payload: dict[str, Any], *, prefix: str) -> pathlib.Path:
    path_obj = _new_auto_output_path(".json", prefix=prefix)
    path_obj.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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
            image_path.write_bytes(image_bytes)
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


def _structured_content_byte_count(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def _batch_read_guidance(result: dict[str, Any], *, manifest_saved: bool) -> str:
    byte_count = result["aggregate_byte_count"]
    limit = result["aggregate_inline_limit"]
    if manifest_saved:
        return (
            f"Batch response was {byte_count} bytes before compaction, above the {limit}-byte "
            "inline budget. Full compacted batch results were saved to results_path. Avoid "
            "reading the entire manifest or every output file unless needed; inspect targeted "
            "jobs, output_path files, or ranges first."
        )
    return (
        f"Batch response was {byte_count} bytes before compaction, above the {limit}-byte "
        "inline budget. Successful job outputs were saved to per-job output_path values. "
        "Avoid reading every output file unless needed; inspect targeted jobs or ranges first."
    )


def _compact_success_job_result(
    result: dict[str, Any],
    raw_successes: list[dict[str, Any] | None],
) -> dict[str, Any]:
    index = result.get("index")
    if not isinstance(index, int) or index < 0 or index >= len(raw_successes):
        return dict(result)

    raw_success = raw_successes[index]
    if raw_success is None:
        return dict(result)
    if "output_path" in result and "text" not in result and "response_json" not in result:
        return dict(result)

    compacted = _apply_output_policy(
        dict(raw_success["result"]),
        raw_success["output_path"],
        expect_json_response=raw_success["expect_json_response"],
        force_file=True,
    )
    compacted["index"] = index
    compacted["ok"] = True
    if "id" in result:
        compacted["id"] = result["id"]
    return compacted


def _apply_batch_aggregate_policy(
    batch_result: dict[str, Any],
    raw_successes: list[dict[str, Any] | None],
) -> dict[str, Any]:
    aggregate_byte_count = _structured_content_byte_count(batch_result)
    if aggregate_byte_count <= BATCH_AGGREGATE_BYTE_LIMIT:
        return batch_result

    compacted_results: list[dict[str, Any]] = []
    for result in batch_result["results"]:
        if result.get("ok") is True:
            compacted_results.append(_compact_success_job_result(result, raw_successes))
        else:
            compacted_results.append(dict(result))

    compacted = dict(batch_result)
    compacted.update(
        {
            "results": compacted_results,
            "results_compacted": True,
            "aggregate_byte_count": aggregate_byte_count,
            "aggregate_inline_limit": BATCH_AGGREGATE_BYTE_LIMIT,
        }
    )
    compacted["read_guidance"] = _batch_read_guidance(compacted, manifest_saved=False)
    if _structured_content_byte_count(compacted) <= BATCH_AGGREGATE_BYTE_LIMIT:
        return compacted

    manifest_path = _write_auto_json_payload(compacted, prefix="batch-results")
    omitted = dict(compacted)
    omitted.update(
        {
            "results": [],
            "results_path": str(manifest_path),
            "results_omitted": True,
            "omitted_result_count": len(compacted_results),
        }
    )
    omitted["read_guidance"] = _batch_read_guidance(omitted, manifest_saved=True)
    return omitted


def _result_receipt_text(structured_content: dict[str, Any], *, is_error: bool) -> str:
    if is_error:
        return "Tool call failed. See structuredContent for error details."
    if structured_content.get("lifecycle") == "background":
        return "Background Gemini run started. Use structuredContent paths and manage_gemini_run for progress."
    if "item_count" in structured_content and "results" in structured_content:
        if structured_content.get("read_guidance"):
            return "Gemini run returned in structuredContent. Follow read_guidance before reading output files."
        return "Gemini run returned in structuredContent."
    if "job_count" in structured_content and "results" in structured_content:
        if structured_content.get("read_guidance"):
            return "Batch result returned in structuredContent. Follow read_guidance before reading output files."
        return "Batch result returned in structuredContent."
    if "response_json_error" in structured_content:
        return "JSON parsing failed. See structuredContent.response_json_error and fallback text fields."
    if "response_json" in structured_content:
        return "Structured JSON returned in structuredContent.response_json."
    if "response_json_preview" in structured_content:
        return "Structured JSON saved to output_path. See structuredContent.response_json_preview."
    if "output_path" in structured_content:
        if structured_content.get("read_guidance"):
            return "Full result saved to output_path. Follow structuredContent.read_guidance."
        return "Result saved to output_path. See structuredContent for details."
    if "text" in structured_content:
        return "Result returned in structuredContent.text."
    return "Result returned in structuredContent."


def _wrap_result(result: dict[str, Any], *, is_error: bool = False) -> mcp_types.CallToolResult:
    structured_content = dict(result)
    inline_images = structured_content.pop("_inline_images", [])
    content: list[Any] = [
        mcp_types.TextContent(
            type="text",
            text=_result_receipt_text(structured_content, is_error=is_error),
        )
    ]
    for image in inline_images:
        content.append(
            mcp_types.ImageContent(
                type="image",
                data=image["data"],
                mimeType=image["mime_type"],
            )
        )

    return mcp_types.CallToolResult(
        content=content,
        structuredContent=structured_content,
        isError=is_error,
    )


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


def _normalize_batch_concurrency(value: Any) -> int:
    if value is None:
        return min(max(get_key_count(), 1), MAX_BATCH_CONCURRENCY)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("max_concurrency must be an integer.")
    if value < 1:
        raise ValueError("max_concurrency must be at least 1.")
    return min(value, MAX_BATCH_CONCURRENCY)


def _validate_absolute_path(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    path_obj = pathlib.Path(value)
    if not path_obj.is_absolute():
        raise ValueError(f"{field_name} must be absolute: {value}")
    return str(path_obj)


def _normalize_system(system: Any) -> str | None:
    if system is None:
        return None
    if not isinstance(system, dict):
        raise ValueError("system must be an object with exactly one of text or path.")
    fields = [field for field in ("text", "path") if field in system]
    if len(fields) != 1:
        raise ValueError("system must contain exactly one of text or path.")
    if fields[0] == "text":
        value = system["text"]
        if not isinstance(value, str):
            raise ValueError("system.text must be a string.")
        return value
    path_value = _validate_absolute_path(system["path"], "system.path")
    return pathlib.Path(path_value).read_text(encoding="utf-8")


def _normalize_contents(contents: Any) -> list[dict[str, Any]]:
    if not isinstance(contents, list) or not contents:
        raise ValueError("request.contents must be a non-empty array.")
    normalized_contents: list[dict[str, Any]] = []
    for content_index, content in enumerate(contents, start=1):
        if not isinstance(content, dict):
            raise ValueError(f"contents[{content_index}] must be an object.")
        role = content.get("role")
        if not isinstance(role, str) or role.strip().lower() not in {"user", "model"}:
            raise ValueError(f"contents[{content_index}].role must be one of: user, model.")
        parts = content.get("parts")
        if not isinstance(parts, list) or not parts:
            raise ValueError(f"contents[{content_index}].parts must be a non-empty array.")
        normalized_parts: list[dict[str, Any]] = []
        for part_index, part in enumerate(parts, start=1):
            if not isinstance(part, dict):
                raise ValueError(f"contents[{content_index}].parts[{part_index}] must be an object.")
            fields = [field for field in ("text", "text_path", "file_path", "file_uri") if field in part]
            if len(fields) != 1:
                raise ValueError(
                    f"contents[{content_index}].parts[{part_index}] must contain exactly one of "
                    "text, text_path, file_path, or file_uri."
                )
            field = fields[0]
            value = part[field]
            part_field_name = f"contents[{content_index}].parts[{part_index}]"
            media_resolution = None
            if "media_resolution" in part:
                media_resolution = normalize_media_resolution_override(
                    part["media_resolution"],
                    f"{part_field_name}.media_resolution",
                )
            if field == "text":
                if media_resolution is not None:
                    raise ValueError(f"{part_field_name}.media_resolution is not valid for text parts.")
                if "mime_type" in part:
                    raise ValueError(f"{part_field_name}.mime_type is only valid for file_uri parts.")
                if not isinstance(value, str):
                    raise ValueError(f"contents[{content_index}].parts[{part_index}].text must be a string.")
                normalized_parts.append({"text": value})
            elif field == "text_path":
                if media_resolution is not None:
                    raise ValueError(f"{part_field_name}.media_resolution is not valid for text_path parts.")
                if "mime_type" in part:
                    raise ValueError(f"{part_field_name}.mime_type is only valid for file_uri parts.")
                normalized_parts.append(
                    {"text_path": _validate_absolute_path(value, f"contents[{content_index}].parts[{part_index}].text_path")}
                )
            elif field == "file_path":
                if "mime_type" in part:
                    raise ValueError(f"{part_field_name}.mime_type is only valid for file_uri parts.")
                file_path = _validate_absolute_path(value, f"{part_field_name}.file_path")
                if media_resolution is not None:
                    validate_media_resolution_for_mime(
                        detect_mime_type(file_path),
                        media_resolution,
                        f"{part_field_name}.media_resolution",
                    )
                normalized_part = {"file_path": file_path}
                if media_resolution is not None:
                    normalized_part["media_resolution"] = media_resolution
                normalized_parts.append(normalized_part)
            else:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{part_field_name}.file_uri must be a non-empty string.")
                mime_type = part.get("mime_type")
                if not isinstance(mime_type, str) or not mime_type.strip():
                    raise ValueError(f"{part_field_name}.mime_type is required for file_uri parts.")
                normalized_mime = mime_type.strip()
                if not is_supported_mime(normalized_mime):
                    raise ValueError(f"Unsupported MIME type for {part_field_name}.file_uri: {normalized_mime}")
                if media_resolution is not None:
                    validate_media_resolution_for_mime(
                        normalized_mime,
                        media_resolution,
                        f"{part_field_name}.media_resolution",
                    )
                normalized_part = {"file_uri": value.strip(), "mime_type": normalized_mime}
                if media_resolution is not None:
                    normalized_part["media_resolution"] = media_resolution
                normalized_parts.append(normalized_part)
        normalized_contents.append({"role": role.strip().lower(), "parts": normalized_parts})
    return normalized_contents


def _normalize_tools(tools: Any) -> dict[str, Any]:
    if tools is None:
        return {"google_search": False}
    if not isinstance(tools, dict):
        raise ValueError("tools must be an object.")
    google_search = tools.get("google_search", False)
    if not isinstance(google_search, bool):
        raise ValueError("tools.google_search must be a boolean.")
    return {"google_search": google_search}


def _normalize_rate_limit(rate_limit: Any) -> dict[str, Any]:
    if rate_limit is None:
        rate_limit = {}
    if not isinstance(rate_limit, dict):
        raise ValueError("rate_limit must be an object.")
    mode = rate_limit.get("mode", RATE_LIMIT_MODE_FAIL_FAST)
    fallback_models = rate_limit.get("fallback_models", [])
    max_wait_seconds = rate_limit.get("max_wait_seconds", DEFAULT_RATE_LIMIT_MAX_WAIT_SECONDS)
    return {
        "mode": mode,
        "fallback_models": fallback_models,
        "max_wait_seconds": max_wait_seconds,
    }


def _normalize_output(
    output: Any,
    *,
    lifecycle: str,
    run_dir: pathlib.Path | None,
    item_id: str,
) -> dict[str, Any]:
    if output is None:
        output = {}
    if not isinstance(output, dict):
        raise ValueError("output must be an object.")
    mode = output.get("mode", "text")
    if mode not in {"text", "json_schema"}:
        raise ValueError("output.mode must be one of: text, json_schema.")

    output_path = output.get("path")
    if output_path is None and lifecycle == "background":
        if run_dir is None:
            raise ValueError("background output auto path requires a run directory.")
        extension = ".json" if mode == "json_schema" else ".txt"
        output_path = str(run_dir / "outputs" / f"{item_id}{extension}")
    elif output_path is not None:
        output_path = _validate_absolute_path(output_path, "output.path")

    response_json_schema = None
    if mode == "json_schema":
        has_inline_schema = output.get("json_schema") is not None
        has_schema_path = output.get("json_schema_path") is not None
        if has_inline_schema == has_schema_path:
            raise ValueError(
                "output.mode='json_schema' requires exactly one of output.json_schema "
                "or output.json_schema_path."
            )
        response_json_schema = _load_json_schema(output.get("json_schema"), output.get("json_schema_path"))
    else:
        if output.get("json_schema") is not None or output.get("json_schema_path") is not None:
            raise ValueError("output json_schema fields require output.mode='json_schema'.")

    return {
        "mode": mode,
        "path": output_path,
        "response_json_schema": response_json_schema,
        "expect_json_response": mode == "json_schema",
    }


def _normalize_request(
    request: Any,
    *,
    lifecycle: str,
    run_dir: pathlib.Path | None,
    item_id: str,
) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ValueError("item.request must be an object.")
    model = request.get("model", DEFAULT_MODEL_NAME)
    include_thinking = request.get("include_thinking", False)
    if not isinstance(include_thinking, bool):
        raise ValueError("request.include_thinking must be a boolean.")
    output = _normalize_output(request.get("output"), lifecycle=lifecycle, run_dir=run_dir, item_id=item_id)
    media_resolution_policy = normalize_media_resolution_policy(request.get("media_resolution"))
    return {
        "model": model,
        "include_thinking": include_thinking,
        "system_prompt": _normalize_system(request.get("system")),
        "contents": _normalize_contents(request.get("contents")),
        "media_resolution": media_resolution_policy,
        "output_path": output["path"],
        "expect_json_response": output["expect_json_response"],
        "response_json_schema": output["response_json_schema"],
        "tools": _normalize_tools(request.get("tools")),
        "rate_limit": _normalize_rate_limit(request.get("rate_limit")),
    }


def _validate_placeholder_name(name: str) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("placeholder names must be non-empty.")
    if any(ch in name for ch in PLACEHOLDER_FORBIDDEN_CHARS):
        raise ValueError(f"Invalid placeholder name: {name}")
    if "\n" in name or "\r" in name:
        raise ValueError(f"Invalid placeholder name: {name}")
    for ch in name:
        if ch.isalnum() or ch in PLACEHOLDER_ALLOWED_PUNCTUATION:
            continue
        raise ValueError(f"Invalid placeholder name: {name}")


def _stringify_var(value: Any, name: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value)
    raise ValueError(f"vars.{name} must be a scalar string, number, or boolean.")


def _substitute_template_value(value: Any, vars_map: dict[str, Any], used_vars: set[str]) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            _validate_placeholder_name(name)
            if name not in vars_map:
                raise ValueError(f"Missing template var: {name}")
            used_vars.add(name)
            return _stringify_var(vars_map[name], name)

        substituted = PLACEHOLDER_RE.sub(replace, value)
        if "{{" in substituted or "}}" in substituted:
            raise ValueError(f"Invalid or unresolved placeholder in template string: {value}")
        return substituted
    if isinstance(value, list):
        return [_substitute_template_value(item, vars_map, used_vars) for item in value]
    if isinstance(value, dict):
        return {
            key: _substitute_template_value(item, vars_map, used_vars)
            for key, item in value.items()
        }
    return value


def _normalize_execution(execution: Any) -> dict[str, Any]:
    if execution is None:
        execution = {}
    if not isinstance(execution, dict):
        raise ValueError("execution must be an object.")
    lifecycle = execution.get("lifecycle", "blocking")
    if lifecycle not in {"blocking", "background"}:
        raise ValueError("execution.lifecycle must be one of: blocking, background.")
    return {
        "lifecycle": lifecycle,
        "max_concurrency": _normalize_batch_concurrency(execution.get("max_concurrency")),
    }


def _normalize_run_plan(args: dict[str, Any]) -> dict[str, Any]:
    execution = _normalize_execution(args.get("execution"))
    lifecycle = execution["lifecycle"]
    run_id = _new_run_id()
    run_dir = _resolve_run_root() / run_id if lifecycle == "background" else None
    if run_dir is not None:
        (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
        (run_dir / "control").mkdir(parents=True, exist_ok=True)

    raw_items = args.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("items must be a non-empty array.")

    has_template = "template_path" in args
    materialized_items: list[dict[str, Any]] = []
    if has_template:
        template_path = pathlib.Path(_validate_absolute_path(args.get("template_path"), "template_path"))
        template_data = json.loads(template_path.read_text(encoding="utf-8-sig"))
        if not isinstance(template_data, dict):
            raise ValueError("template_path file must contain a JSON object request template.")
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                raise ValueError(f"items[{index}] must be an object.")
            if "request" in item:
                raise ValueError("template items must not include request.")
            item_id = item.get("id") if isinstance(item.get("id"), str) and item.get("id") else f"item-{index + 1:04d}"
            vars_map = item.get("vars", {})
            if not isinstance(vars_map, dict):
                raise ValueError(f"items[{index}].vars must be an object.")
            for var_name in vars_map:
                _validate_placeholder_name(var_name)
            used_vars: set[str] = set()
            request = _substitute_template_value(template_data, vars_map, used_vars)
            unused_vars = sorted(set(vars_map) - used_vars)
            if unused_vars:
                raise ValueError(f"Unused template vars for item {item_id}: {', '.join(unused_vars)}")
            materialized_items.append(
                {
                    "id": item_id,
                    "index": index,
                    "request": _normalize_request(
                        request,
                        lifecycle=lifecycle,
                        run_dir=run_dir,
                        item_id=item_id,
                    ),
                }
            )
    else:
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                raise ValueError(f"items[{index}] must be an object.")
            if "vars" in item:
                raise ValueError("explicit items must not include vars.")
            item_id = item.get("id") if isinstance(item.get("id"), str) and item.get("id") else f"item-{index + 1:04d}"
            materialized_items.append(
                {
                    "id": item_id,
                    "index": index,
                    "request": _normalize_request(
                        item.get("request"),
                        lifecycle=lifecycle,
                        run_dir=run_dir,
                        item_id=item_id,
                    ),
                }
            )

    return {
        "run_id": run_id,
        "lifecycle": lifecycle,
        "max_concurrency": execution["max_concurrency"],
        "run_dir": str(run_dir) if run_dir is not None else None,
        "items": materialized_items,
    }


def _run_read_guidance(result: dict[str, Any], *, manifest_saved: bool) -> str:
    byte_count = result["aggregate_byte_count"]
    limit = result["aggregate_inline_limit"]
    if manifest_saved:
        return (
            f"Run response was {byte_count} bytes before compaction, above the {limit}-byte "
            "inline budget. Full compacted run results were saved to results_path. Avoid "
            "reading the entire manifest or every output file unless needed; inspect targeted "
            "items, output_path files, or ranges first."
        )
    return (
        f"Run response was {byte_count} bytes before compaction, above the {limit}-byte "
        "inline budget. Successful item outputs were saved to per-item output_path values. "
        "Avoid reading every output file unless needed; inspect targeted items or ranges first."
    )


def _apply_run_aggregate_policy(
    run_result: dict[str, Any],
    raw_successes: list[dict[str, Any] | None],
) -> dict[str, Any]:
    aggregate_byte_count = _structured_content_byte_count(run_result)
    if aggregate_byte_count <= BATCH_AGGREGATE_BYTE_LIMIT:
        return run_result

    compacted_results: list[dict[str, Any]] = []
    for result in run_result["results"]:
        if result.get("ok") is True:
            compacted_results.append(_compact_success_job_result(result, raw_successes))
        else:
            compacted_results.append(dict(result))

    compacted = dict(run_result)
    compacted.update(
        {
            "results": compacted_results,
            "results_compacted": True,
            "aggregate_byte_count": aggregate_byte_count,
            "aggregate_inline_limit": BATCH_AGGREGATE_BYTE_LIMIT,
        }
    )
    compacted["read_guidance"] = _run_read_guidance(compacted, manifest_saved=False)
    if _structured_content_byte_count(compacted) <= BATCH_AGGREGATE_BYTE_LIMIT:
        return compacted

    manifest_path = _write_auto_json_payload(compacted, prefix="run-results")
    omitted = dict(compacted)
    omitted.update(
        {
            "results": [],
            "results_path": str(manifest_path),
            "results_omitted": True,
            "omitted_result_count": len(compacted_results),
        }
    )
    omitted["read_guidance"] = _run_read_guidance(omitted, manifest_saved=True)
    return omitted


async def _generate_raw_from_request(request: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    rate_limit = request["rate_limit"]
    tools = request["tools"]
    result = await anyio.to_thread.run_sync(
        lambda: generate_request(
            request["contents"],
            request.get("system_prompt"),
            request.get("model", DEFAULT_MODEL_NAME),
            request.get("include_thinking", False),
            rate_limit.get("mode", RATE_LIMIT_MODE_FAIL_FAST),
            rate_limit.get("fallback_models"),
            rate_limit.get("max_wait_seconds"),
            tools.get("google_search", False),
            request.get("response_json_schema"),
            request.get("media_resolution"),
        )
    )
    return result, request.get("expect_json_response") is True


def _initial_status(plan: dict[str, Any], status: str) -> dict[str, Any]:
    return {
        "run_id": plan["run_id"],
        "lifecycle": plan["lifecycle"],
        "status": status,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "item_count": len(plan["items"]),
        "completed_count": 0,
        "ok_count": 0,
        "error_count": 0,
        "max_concurrency": plan["max_concurrency"],
        "items": [
            {
                "id": item["id"],
                "index": item["index"],
                "status": "pending",
                "output_path": item["request"].get("output_path"),
            }
            for item in plan["items"]
        ],
    }


def _write_status(run_dir: pathlib.Path, status: dict[str, Any]) -> None:
    status["updated_at"] = _utc_now()
    _write_json(run_dir / "status.json", status)


def _read_control_action(run_dir: pathlib.Path) -> str | None:
    control_dir = run_dir / "control"
    if (control_dir / "cancel.json").exists():
        return "cancel"
    if (control_dir / "stop.json").exists():
        return "stop"
    return None


async def _execute_run_plan(
    plan: dict[str, Any],
    *,
    run_dir: pathlib.Path | None = None,
    background: bool = False,
    worker_token: str | None = None,
) -> dict[str, Any]:
    max_concurrency = plan["max_concurrency"]
    limiter = anyio.Semaphore(max_concurrency)
    results: list[dict[str, Any] | None] = [None] * len(plan["items"])
    raw_successes: list[dict[str, Any] | None] | None = None if background else [None] * len(plan["items"])
    status_lock = anyio.Lock()
    status_data = _initial_status(plan, "running") if background and run_dir is not None else None

    if status_data is not None:
        _ensure_worker_owns_run(run_dir, plan["run_id"], worker_token)
        existing_status_path = run_dir / "status.json"
        if existing_status_path.exists():
            try:
                existing_status = _read_json(existing_status_path)
                if isinstance(existing_status.get("items"), list):
                    status_data["items"] = existing_status["items"]
            except Exception:
                pass
        _write_status(run_dir, status_data)

    async def update_item_status(index: int, item_status: str, extra: dict[str, Any] | None = None) -> None:
        if status_data is None or run_dir is None:
            return
        async with status_lock:
            _ensure_worker_owns_run(run_dir, plan["run_id"], worker_token)
            item_entry = status_data["items"][index]
            item_entry["status"] = item_status
            if extra:
                item_entry.update(extra)
            completed_items = [
                item
                for item in status_data["items"]
                if item.get("status") in {"completed", "failed", "stopped", "canceled"}
            ]
            status_data["completed_count"] = len(completed_items)
            status_data["ok_count"] = sum(1 for item in status_data["items"] if item.get("status") == "completed")
            status_data["error_count"] = sum(1 for item in status_data["items"] if item.get("status") == "failed")
            _write_status(run_dir, status_data)

    async def run_item(index: int, item: dict[str, Any]) -> None:
        if status_data is not None and status_data["items"][index].get("status") == "completed":
            results[index] = {
                "index": index,
                "id": item["id"],
                "ok": True,
                "status": "completed",
                "skipped": True,
                "output_path": status_data["items"][index].get("output_path"),
            }
            return
        async with limiter:
            _ensure_worker_owns_run(run_dir, plan["run_id"], worker_token)
            if run_dir is not None:
                control_action = _read_control_action(run_dir)
                if control_action in {"stop", "cancel"}:
                    stopped_status = "canceled" if control_action == "cancel" else "stopped"
                    results[index] = {
                        "index": index,
                        "id": item["id"],
                        "ok": False,
                        "skipped": True,
                        "status": stopped_status,
                    }
                    await update_item_status(index, stopped_status)
                    return

            await update_item_status(index, "running", {"started_at": _utc_now()})
            if run_dir is not None:
                _ensure_worker_owns_run(run_dir, plan["run_id"], worker_token)
                _append_event(run_dir, {"run_id": plan["run_id"], "event": "item_started", "item_id": item["id"]})
            try:
                raw_result, expect_json_response = await _generate_raw_from_request(item["request"])
                _ensure_worker_owns_run(run_dir, plan["run_id"], worker_token)
                if raw_successes is not None:
                    raw_successes[index] = {
                        "result": raw_result,
                        "output_path": item["request"].get("output_path"),
                        "expect_json_response": expect_json_response,
                    }
                item_result = _apply_output_policy(
                    dict(raw_result),
                    item["request"].get("output_path"),
                    expect_json_response=expect_json_response,
                )
                item_result["index"] = index
                item_result["id"] = item["id"]
                item_result["ok"] = True
                if background:
                    results[index] = {
                        key: item_result[key]
                        for key in (
                            "index",
                            "id",
                            "ok",
                            "output_path",
                            "char_count",
                            "byte_count",
                            "line_count",
                            "image_count",
                        )
                        if key in item_result
                    }
                else:
                    results[index] = item_result
                await update_item_status(
                    index,
                    "completed",
                    {
                        "completed_at": _utc_now(),
                        "output_path": item_result.get("output_path", item["request"].get("output_path")),
                    },
                )
                if run_dir is not None:
                    _ensure_worker_owns_run(run_dir, plan["run_id"], worker_token)
                    _append_event(
                        run_dir,
                        {
                            "run_id": plan["run_id"],
                            "event": "item_completed",
                            "item_id": item["id"],
                            "output_path": item_result.get("output_path"),
                        },
                    )
            except WorkerOwnershipLost:
                raise
            except GeminiRateLimitError as exc:
                error_result = exc.to_dict()
                error_result.update({"index": index, "id": item["id"], "ok": False, "error": exc.message})
                results[index] = error_result
                await update_item_status(index, "failed", {"error": exc.message, "error_type": "vertex_rate_limited"})
                if run_dir is not None:
                    _append_event(
                        run_dir,
                        {
                            "run_id": plan["run_id"],
                            "event": "item_failed",
                            "item_id": item["id"],
                            "error_type": "vertex_rate_limited",
                            "message": exc.message,
                        },
                    )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                results[index] = {"index": index, "id": item["id"], "ok": False, "error": message}
                await update_item_status(index, "failed", {"error": message, "error_type": type(exc).__name__})
                if run_dir is not None:
                    _append_event(
                        run_dir,
                        {
                            "run_id": plan["run_id"],
                            "event": "item_failed",
                            "item_id": item["id"],
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                        },
                    )

    if run_dir is not None:
        _ensure_worker_owns_run(run_dir, plan["run_id"], worker_token)
        _append_event(run_dir, {"run_id": plan["run_id"], "event": "run_started"})

    async with anyio.create_task_group() as task_group:
        for index, item in enumerate(plan["items"]):
            task_group.start_soon(run_item, index, item)

    finalized_results = [result for result in results if result is not None]
    ok_count = sum(1 for result in finalized_results if result.get("ok") is True)
    error_count = len(finalized_results) - ok_count
    run_status = "completed" if error_count == 0 else "failed"
    if run_dir is not None:
        _ensure_worker_owns_run(run_dir, plan["run_id"], worker_token)
        control_action = _read_control_action(run_dir)
        if control_action == "cancel":
            run_status = "canceled"
        elif control_action == "stop":
            run_status = "stopped"
        if status_data is not None:
            _ensure_worker_owns_run(run_dir, plan["run_id"], worker_token)
            status_data["status"] = run_status
            status_data["completed_count"] = len(finalized_results)
            status_data["ok_count"] = ok_count
            status_data["error_count"] = error_count
            _write_status(run_dir, status_data)
        _append_event(run_dir, {"run_id": plan["run_id"], "event": f"run_{run_status}"})

    if background:
        return {
            "run_id": plan["run_id"],
            "lifecycle": plan["lifecycle"],
            "item_count": len(plan["items"]),
            "ok_count": ok_count,
            "error_count": error_count,
            "max_concurrency": max_concurrency,
            "results": finalized_results,
        }

    return _apply_run_aggregate_policy(
        {
            "run_id": plan["run_id"],
            "lifecycle": plan["lifecycle"],
            "item_count": len(plan["items"]),
            "ok_count": ok_count,
            "error_count": error_count,
            "max_concurrency": max_concurrency,
            "results": finalized_results,
        },
        raw_successes or [None] * len(plan["items"]),
    )


def _call_gemini_input_schema() -> dict[str, Any]:
    media_resolution_value_schema = {
        "type": "string",
        "enum": list(MEDIA_RESOLUTION_INPUT_VALUES),
    }
    non_image_media_resolution_value_schema = {
        "type": "string",
        "enum": [value for value in MEDIA_RESOLUTION_INPUT_VALUES if value != "ultra_high"],
    }
    part_schema = {
        "type": "object",
        "description": "Content part. Set exactly one of text, text_path, file_path, or file_uri.",
        "properties": {
            "text": {"type": "string"},
            "text_path": {"type": "string", "description": "Absolute path to a UTF-8 text file."},
            "file_path": {"type": "string", "description": "Absolute path to a local file part."},
            "file_uri": {"type": "string", "description": "Remote file URI such as gs://bucket/object."},
            "mime_type": {"type": "string", "description": "Required MIME type for file_uri parts."},
            "media_resolution": {
                **media_resolution_value_schema,
                "description": "Optional per-file override. Applies only to image, PDF, and video parts.",
            },
        },
        "additionalProperties": False,
    }
    content_schema = {
        "type": "object",
        "properties": {
            "role": {"type": "string", "enum": ["user", "model"]},
            "parts": {"type": "array", "items": part_schema, "minItems": 1},
        },
        "required": ["role", "parts"],
        "additionalProperties": False,
    }
    request_schema = {
        "type": "object",
        "properties": {
            "model": {"type": "string", "default": DEFAULT_MODEL_NAME},
            "include_thinking": {"type": "boolean", "default": False},
            "system": {
                "type": "object",
                "description": "Optional system instruction. Set exactly one of text or path.",
                "properties": {
                    "text": {"type": "string"},
                    "path": {"type": "string", "description": "Absolute path to a UTF-8 system prompt file."},
                },
                "additionalProperties": False,
            },
            "contents": {"type": "array", "items": content_schema, "minItems": 1},
            "media_resolution": {
                "type": "object",
                "description": (
                    "Optional request-level defaults for image, PDF, and video parts. "
                    "Omit for image=ultra_high, pdf=high, video=high."
                ),
                "properties": {
                    key: {
                        **(
                            media_resolution_value_schema
                            if key == "image"
                            else non_image_media_resolution_value_schema
                        ),
                        "default": DEFAULT_MEDIA_RESOLUTION_POLICY[key],
                    }
                    for key in MEDIA_RESOLUTION_POLICY_KEYS
                },
                "additionalProperties": False,
            },
            "output": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["text", "json_schema"], "default": "text"},
                    "path": {"type": "string", "description": "Absolute output path. Forces file-backed output when set."},
                    "json_schema": {
                        "type": "object",
                        "description": "Inline JSON Schema object. Only valid with output.mode: json_schema.",
                    },
                    "json_schema_path": {
                        "type": "string",
                        "description": "Absolute path to a JSON Schema file. Only valid with output.mode: json_schema.",
                    },
                },
                "additionalProperties": False,
            },
            "tools": {
                "type": "object",
                "properties": {"google_search": {"type": "boolean", "default": False}},
                "additionalProperties": False,
            },
            "rate_limit": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["fail_fast", "wait"], "default": RATE_LIMIT_MODE_FAIL_FAST},
                    "fallback_models": {"type": "array", "items": {"type": "string"}, "default": []},
                    "max_wait_seconds": {"type": "number", "default": DEFAULT_RATE_LIMIT_MAX_WAIT_SECONDS},
                },
                "additionalProperties": False,
            },
        },
        "required": ["contents"],
        "additionalProperties": False,
    }
    execution_schema = {
        "type": "object",
        "properties": {
            "lifecycle": {"type": "string", "enum": ["blocking", "background"], "default": "blocking"},
            "max_concurrency": {
                "type": "integer",
                "description": f"Maximum concurrent items, capped at {MAX_BATCH_CONCURRENCY}.",
            },
        },
        "additionalProperties": False,
    }
    explicit_item_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "request": request_schema,
        },
        "required": ["request"],
        "additionalProperties": False,
    }
    template_item_schema = {
        "type": "object",
        "description": "Template item. Use with top-level template_path and provide per-item vars.",
        "properties": {
            "id": {"type": "string"},
            "vars": {"type": "object"},
        },
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "description": (
            "Use either explicit items with items[].request, or template_path with "
            "items[].vars. The server validates that the two shapes are not mixed."
        ),
        "properties": {
            "template_path": {
                "type": "string",
                "description": (
                    "Absolute path to a JSON request template. When provided, each item "
                    "must provide vars instead of request."
                ),
            },
            "items": {
                "type": "array",
                "description": (
                    "Run items. Explicit calls use items[].request; template calls use "
                    "template_path plus items[].vars."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "request": explicit_item_schema["properties"]["request"],
                        "vars": template_item_schema["properties"]["vars"],
                    },
                    "additionalProperties": False,
                },
                "minItems": 1,
            },
            "execution": execution_schema,
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def _run_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "lifecycle": {"type": "string"},
            "status": {"type": "string"},
            "run_dir": {"type": "string"},
            "plan_path": {"type": "string"},
            "status_path": {"type": "string"},
            "events_path": {"type": "string"},
            "locator_path": {"type": "string"},
            "read_guidance": {"type": "string"},
            "item_count": {"type": "integer"},
            "ok_count": {"type": "integer"},
            "error_count": {"type": "integer"},
            "max_concurrency": {"type": "integer"},
            "results": {"type": "array", "items": {"type": "object"}},
            "results_compacted": {"type": "boolean"},
            "aggregate_byte_count": {"type": "integer"},
            "aggregate_inline_limit": {"type": "integer"},
            "results_path": {"type": "string"},
        },
        "required": ["run_id", "lifecycle"],
        "additionalProperties": True,
    }


def _manage_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "status", "progress", "stop", "cancel", "resume"],
            },
            "run_id": {"type": "string"},
            "run_dir": {"type": "string"},
            "event_offset": {"type": "integer", "default": 0},
            "max_events": {"type": "integer", "default": 50},
            "force": {"type": "boolean", "default": False},
        },
        "required": ["action"],
        "additionalProperties": False,
    }


def _run_dir_from_args(args: dict[str, Any]) -> pathlib.Path:
    run_dir_arg = args.get("run_dir")
    if run_dir_arg is not None:
        return pathlib.Path(_validate_absolute_path(run_dir_arg, "run_dir"))
    run_id = args.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id or run_dir is required for this action.")
    return _resolve_run_root() / run_id


def _spawn_worker(run_dir: pathlib.Path, run_id: str, run_token: str) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "mcp_server.run_worker",
        "--run-dir",
        str(run_dir),
        "--run-id",
        run_id,
        "--run-token",
        run_token,
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        command,
        cwd=str(pathlib.Path(__file__).resolve().parents[1]),
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    try:
        create_time = psutil.Process(process.pid).create_time()
    except Exception:
        create_time = None
    locator = {
        "run_id": run_id,
        "pid": process.pid,
        "create_time": create_time,
        "run_token": run_token,
        "command": command,
        "spawned_at": _utc_now(),
    }
    _write_json(run_dir / "locator.json", locator)
    return locator


def _start_background_run(plan: dict[str, Any]) -> dict[str, Any]:
    run_dir = pathlib.Path(plan["run_dir"])
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    (run_dir / "control").mkdir(parents=True, exist_ok=True)
    _write_json(run_dir / "plan.json", plan)
    _write_status(run_dir, _initial_status(plan, "starting"))
    _append_event(run_dir, {"run_id": plan["run_id"], "event": "run_queued"})
    run_token = uuid.uuid4().hex
    locator = _spawn_worker(run_dir, plan["run_id"], run_token)
    status = _read_json(run_dir / "status.json")
    status["status"] = "running"
    status["pid"] = locator["pid"]
    _write_status(run_dir, status)
    return {
        "run_id": plan["run_id"],
        "lifecycle": "background",
        "status": "running",
        "run_dir": str(run_dir),
        "plan_path": str(run_dir / "plan.json"),
        "status_path": str(run_dir / "status.json"),
        "events_path": str(run_dir / "events.jsonl"),
        "locator_path": str(run_dir / "locator.json"),
        "item_count": len(plan["items"]),
        "max_concurrency": plan["max_concurrency"],
        "read_guidance": (
            "Background Gemini run started. Results will be written under run_dir. "
            "Use manage_gemini_run for status/progress, or inspect appended events_path "
            "from the last read offset instead of rereading the full log."
        ),
    }


def _verified_process_from_locator(locator: dict[str, Any]) -> psutil.Process | None:
    pid = locator.get("pid")
    if not isinstance(pid, int):
        return None
    try:
        process = psutil.Process(pid)
        create_time = locator.get("create_time")
        if isinstance(create_time, (int, float)) and abs(process.create_time() - float(create_time)) > 0.01:
            return None
        token = locator.get("run_token")
        command_line = " ".join(process.cmdline())
        if isinstance(token, str) and token and token not in command_line:
            return None
        return process
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def _inspect_run_liveness(run_dir: pathlib.Path) -> dict[str, Any]:
    status_path = run_dir / "status.json"
    status: dict[str, Any] = {}
    status_error = None
    if status_path.exists():
        try:
            status = _read_json(status_path)
        except Exception as exc:
            status_error = f"{type(exc).__name__}: {exc}"
    terminal = {"completed", "failed", "canceled", "stopped"}
    locator_path = run_dir / "locator.json"
    if locator_path.exists():
        try:
            locator = _read_json(locator_path)
        except Exception as exc:
            payload = {"process_alive": False, "live_status": "unknown", "reason": f"invalid locator.json: {exc}"}
            if status_error is not None:
                payload["status_error"] = status_error
            return payload
        process = _verified_process_from_locator(locator)
        if process is not None:
            payload = {"process_alive": True, "live_status": status.get("status", "running"), "pid": process.pid}
            if status.get("status") in terminal:
                payload["terminal_status_with_live_process"] = True
            if status_error is not None:
                payload["status_error"] = status_error
            return payload
    elif status.get("status") not in terminal:
        payload = {"process_alive": False, "live_status": "unknown", "reason": "missing locator.json"}
        if status_error is not None:
            payload["status_error"] = status_error
        return payload

    if status.get("status") in terminal:
        payload = {"process_alive": False, "live_status": status.get("status")}
        if status_error is not None:
            payload["status_error"] = status_error
        return payload

    payload = {"process_alive": False, "live_status": "unknown", "reason": "worker process not verified"}
    if status_error is not None:
        payload["status_error"] = status_error
    return payload


def _read_events(run_dir: pathlib.Path, offset: Any, max_events: Any) -> dict[str, Any]:
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("event_offset must be a non-negative integer.")
    if not isinstance(max_events, int) or isinstance(max_events, bool) or max_events < 1:
        raise ValueError("max_events must be a positive integer.")
    max_events = min(max_events, 500)
    events_path = run_dir / "events.jsonl"
    events: list[dict[str, Any]] = []
    line_count = 0
    events_truncated = False
    if events_path.exists():
        with events_path.open("r", encoding="utf-8") as handle:
            for line_count, line in enumerate(handle, start=1):
                if line_count <= offset:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    events.append({"event": "invalid_event_line", "line": line_count})
                if len(events) >= max_events:
                    events_truncated = next(handle, None) is not None
                    break
    return {
        "events": events,
        "event_offset": offset,
        "next_event_offset": min(offset + len(events), line_count),
        "events_path": str(events_path),
        "events_truncated": events_truncated,
    }


def _write_control(run_dir: pathlib.Path, action: str) -> pathlib.Path:
    control_path = run_dir / "control" / f"{action}.json"
    payload = {"action": action, "requested_at": _utc_now()}
    _write_json(control_path, payload)
    _append_event(run_dir, {"run_id": run_dir.name, "event": f"{action}_requested"})
    return control_path


def _terminate_verified_process_tree(run_dir: pathlib.Path) -> dict[str, Any]:
    locator_path = run_dir / "locator.json"
    if not locator_path.exists():
        return {"all_gone": True, "terminated": False, "termination_failed": False, "reason": "missing locator.json"}
    try:
        locator = _read_json(locator_path)
    except Exception as exc:
        return {
            "all_gone": False,
            "terminated": False,
            "termination_failed": True,
            "alive_pids": [],
            "reason": f"could not read locator.json: {exc}",
        }
    process = _verified_process_from_locator(locator)
    if process is None:
        return {"all_gone": True, "terminated": False, "termination_failed": False, "reason": "worker process not verified"}
    try:
        children = process.children(recursive=True)
    except psutil.Error as exc:
        return {
            "all_gone": False,
            "terminated": False,
            "termination_failed": True,
            "alive_pids": [process.pid],
            "reason": f"could not inspect process tree: {exc}",
        }
    for child in children:
        try:
            child.terminate()
        except psutil.Error:
            pass
    try:
        process.terminate()
    except psutil.Error:
        pass
    gone, alive = psutil.wait_procs(children + [process], timeout=5)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass
    _, still_alive = psutil.wait_procs(alive, timeout=5)
    alive_pids = [proc.pid for proc in still_alive if proc.is_running()]
    return {
        "all_gone": not alive_pids,
        "terminated": bool(gone or alive) and not alive_pids,
        "termination_failed": bool(alive_pids),
        "alive_pids": alive_pids,
    }


def _manage_gemini_run(args: dict[str, Any]) -> dict[str, Any]:
    action = args.get("action")
    if action == "list":
        run_root = _resolve_run_root()
        runs: list[dict[str, Any]] = []
        for status_path in sorted(run_root.glob("run-*/status.json"), reverse=True):
            try:
                status = _read_json(status_path)
            except Exception as exc:
                runs.append(
                    {
                        "run_id": status_path.parent.name,
                        "run_dir": str(status_path.parent),
                        "status": "corrupt",
                        "error": f"Could not read status.json: {type(exc).__name__}: {exc}",
                    }
                )
                continue
            runs.append(
                {
                    "run_id": status.get("run_id", status_path.parent.name),
                    "run_dir": str(status_path.parent),
                    "status": status.get("status", "unknown"),
                    "item_count": status.get("item_count"),
                    "completed_count": status.get("completed_count"),
                    "updated_at": status.get("updated_at"),
                }
            )
        return {"runs": runs, "run_root": str(run_root)}

    run_dir = _run_dir_from_args(args)
    if not run_dir.exists():
        raise ValueError(f"run_dir does not exist: {run_dir}")

    if action == "status":
        status = _read_json(run_dir / "status.json")
        status.update(_inspect_run_liveness(run_dir))
        status["run_dir"] = str(run_dir)
        return status

    if action == "progress":
        payload = _read_events(run_dir, args.get("event_offset", 0), args.get("max_events", 50))
        payload.update({"run_id": run_dir.name, "run_dir": str(run_dir)})
        payload["liveness"] = _inspect_run_liveness(run_dir)
        return payload

    if action in {"stop", "cancel"}:
        control_path = _write_control(run_dir, action)
        force = args.get("force", False)
        termination = {
            "all_gone": False,
            "terminated": False,
            "termination_failed": False,
            "alive_pids": [],
        }
        if action == "cancel" and force is True:
            termination = _terminate_verified_process_tree(run_dir)
            if termination["all_gone"]:
                try:
                    status = _read_json(run_dir / "status.json")
                    status["status"] = "canceled"
                    _write_status(run_dir, status)
                except Exception:
                    pass
                _append_event(run_dir, {"run_id": run_dir.name, "event": "run_canceled", "forced": True})
            else:
                _append_event(
                    run_dir,
                    {
                        "run_id": run_dir.name,
                        "event": "cancel_failed",
                        "forced": True,
                        "alive_pids": termination.get("alive_pids", []),
                    },
                )
        return {
            "run_id": run_dir.name,
            "run_dir": str(run_dir),
            "action": action,
            "control_path": str(control_path),
            "forced_termination": termination["terminated"],
            "termination_failed": termination["termination_failed"],
            "alive_pids": termination.get("alive_pids", []),
            "liveness": _inspect_run_liveness(run_dir),
        }

    if action == "resume":
        plan_path = run_dir / "plan.json"
        if not plan_path.exists():
            raise ValueError(f"Missing plan.json for run: {run_dir}")
        liveness = _inspect_run_liveness(run_dir)
        if liveness.get("process_alive") is True:
            raise ValueError(f"Cannot resume run while a verified worker is still alive: {run_dir}")
        plan = _read_json(plan_path)
        for control_name in ("stop.json", "cancel.json"):
            control_path = run_dir / "control" / control_name
            if control_path.exists():
                control_path.unlink()
        run_token = uuid.uuid4().hex
        locator = _spawn_worker(run_dir, plan["run_id"], run_token)
        status = _read_json(run_dir / "status.json") if (run_dir / "status.json").exists() else _initial_status(plan, "running")
        status["status"] = "running"
        status["pid"] = locator["pid"]
        _write_status(run_dir, status)
        _append_event(run_dir, {"run_id": plan["run_id"], "event": "resume_started", "pid": locator["pid"]})
        return {
            "run_id": plan["run_id"],
            "run_dir": str(run_dir),
            "status": "running",
            "pid": locator["pid"],
            "locator_path": str(run_dir / "locator.json"),
        }

    raise ValueError("action must be one of: list, status, progress, stop, cancel, resume.")


def _tool_definitions() -> list[mcp_types.Tool]:
    return [
        mcp_types.Tool(
            name="call_gemini",
            description=(
                "Run one or more Gemini request items using explicit request envelopes or a "
                "template plus per-item vars. Set execution.lifecycle to `blocking` for an "
                "inline/spill result or `background` to start a child worker process and return "
                "run paths immediately. Tool content text is a short receipt; use structuredContent."
            ),
            inputSchema=_call_gemini_input_schema(),
            outputSchema=_run_output_schema(),
        ),
        mcp_types.Tool(
            name="manage_gemini_run",
            description=(
                "Inspect or control background Gemini runs. Use action=list, status, progress, "
                "stop, cancel, or resume. Live state is checked from runtime process state, not "
                "only from the append-only event log."
            ),
            inputSchema=_manage_input_schema(),
            outputSchema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "run_dir": {"type": "string"},
                    "status": {"type": "string"},
                    "runs": {"type": "array", "items": {"type": "object"}},
                    "events": {"type": "array", "items": {"type": "object"}},
                    "liveness": {"type": "object"},
                },
                "additionalProperties": True,
            },
        ),
        mcp_types.Tool(
            name="list_gemini_models",
            description="List the Gemini models supported by this server with selection guidance.",
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "models": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "model_characteristics": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    }
                },
                "required": ["models", "model_characteristics"],
                "additionalProperties": False,
            },
        ),
        mcp_types.Tool(
            name="detect_mime",
            description="Detect the MIME type for a local absolute-path file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Local absolute path to a file.",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "mime": {"type": "string"},
                    "supported": {"type": "boolean"},
                },
                "required": ["mime", "supported"],
                "additionalProperties": False,
            },
        ),
    ]


server = Server(
    SERVER_NAME,
    version=SERVER_VERSION,
    instructions=(
        "Run-oriented stdio MCP wrapper for Gemini generate_content calls. "
        "Use `call_gemini` for blocking or background runs and `manage_gemini_run` "
        "to inspect or control background runs. Vertex rate limits are tracked per "
        "project/location/model quota slot."
    ),
)


@server.list_tools()
async def handle_list_tools() -> list[mcp_types.Tool]:
    return _tool_definitions()


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict[str, Any] | None):
    args = arguments or {}

    try:
        if name == "call_gemini":
            plan = _normalize_run_plan(args)
            if plan["lifecycle"] == "background":
                return _wrap_result(_start_background_run(plan))
            return _wrap_result(await _execute_run_plan(plan))

        if name == "manage_gemini_run":
            return _wrap_result(_manage_gemini_run(args))

        if name == "list_gemini_models":
            return _wrap_result(
                {
                    "models": AVAILABLE_MODELS,
                    "model_characteristics": MODEL_CHARACTERISTICS,
                }
            )

        if name == "detect_mime":
            result = await anyio.to_thread.run_sync(detect_mime, args["path"])
            return _wrap_result(result)

    except (FileNotFoundError, ValueError) as exc:
        _raise_mcp_error(mcp_types.INVALID_PARAMS, str(exc))
    except GeminiRateLimitError as exc:
        return _wrap_result(exc.to_dict(), is_error=True)
    except Exception as exc:
        _raise_mcp_error(mcp_types.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    _raise_mcp_error(mcp_types.INVALID_PARAMS, f"Unknown tool: {name}")


async def run_worker_from_dir(run_dir: str, run_id: str, run_token: str) -> None:
    run_path = pathlib.Path(run_dir)
    plan = _read_json(run_path / "plan.json")
    if plan.get("run_id") != run_id:
        raise ValueError(f"run_id mismatch for worker: {run_id}")
    deadline = time.monotonic() + 10
    while not _locator_matches_worker(run_path, run_id, run_token):
        if time.monotonic() >= deadline:
            raise ValueError(f"locator.json does not match worker identity for run: {run_id}")
        await anyio.sleep(0.05)
    _append_event(run_path, {"run_id": run_id, "event": "worker_started"})
    await _execute_run_plan(plan, run_dir=run_path, background=True, worker_token=run_token)


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
