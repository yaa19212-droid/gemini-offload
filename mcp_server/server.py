"""Low-level stdio MCP server for Gemini offload."""

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

import anyio
import mcp.types as mcp_types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError

from .gemini_client import (
    AVAILABLE_MODELS,
    DEFAULT_MODEL_NAME,
    DEFAULT_RATE_LIMIT_MAX_WAIT_SECONDS,
    GeminiRateLimitError,
    MODEL_CHARACTERISTICS,
    RATE_LIMIT_MODE_FAIL_FAST,
    detect_mime,
    generate,
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
IMAGE_EXTENSION_OVERRIDES = {
    "image/jpeg": ".jpg",
}
MAX_BATCH_CONCURRENCY = 32


def _load_system_prompt(inline: Any, path: Any) -> Any:
    if path is None:
        return inline
    if inline is not None:
        raise ValueError("Pass either system_prompt or system_prompt_path, not both.")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("system_prompt_path must be a non-empty string.")
    p = pathlib.Path(path)
    if not p.is_absolute():
        raise ValueError(f"system_prompt_path must be absolute: {path}")
    return p.read_text(encoding="utf-8")


def _load_history(inline: Any, path: Any) -> Any:
    if path is None:
        return inline
    if inline:
        raise ValueError("Pass either history or history_path, not both.")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("history_path must be a non-empty string.")
    p = pathlib.Path(path)
    if not p.is_absolute():
        raise ValueError(f"history_path must be absolute: {path}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("history_path file must contain a JSON array of {role, text} objects.")
    return data


def _load_json_schema(inline: Any, path: Any) -> Any:
    if path is None:
        if inline is None:
            return None
        if not isinstance(inline, dict):
            raise ValueError("response_json_schema must be a JSON object.")
        return inline
    if inline is not None:
        raise ValueError("Pass either response_json_schema or response_json_schema_path, not both.")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("response_json_schema_path must be a non-empty string.")
    p = pathlib.Path(path)
    if not p.is_absolute():
        raise ValueError(f"response_json_schema_path must be absolute: {path}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"response_json_schema_path file must contain valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("response_json_schema_path file must contain a JSON object.")
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


async def _generate_raw_from_args(args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    system_prompt = _load_system_prompt(args.get("system_prompt"), args.get("system_prompt_path"))
    history = _load_history(args.get("history"), args.get("history_path"))
    response_json_schema = _load_json_schema(
        args.get("response_json_schema"),
        args.get("response_json_schema_path"),
    )
    result = await anyio.to_thread.run_sync(
        generate,
        args["prompt"],
        args.get("files"),
        system_prompt,
        args.get("model", DEFAULT_MODEL_NAME),
        args.get("include_thinking", False),
        history,
        args.get("rate_limit_mode", RATE_LIMIT_MODE_FAIL_FAST),
        args.get("fallback_models"),
        args.get("rate_limit_max_wait_seconds"),
        args.get("google_search", False),
        response_json_schema,
    )
    return result, response_json_schema is not None


async def _run_generate_from_args(args: dict[str, Any]) -> dict[str, Any]:
    result, expect_json_response = await _generate_raw_from_args(args)
    return _apply_output_policy(
        result,
        args.get("output_path"),
        expect_json_response=expect_json_response,
    )


def _batch_job_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "Optional caller-provided job id echoed in the result.",
            },
            "prompt": {
                "type": "string",
                "description": "Required user prompt text.",
            },
            "files": {
                "type": "array",
                "description": "Optional local absolute file paths.",
                "items": {"type": "string"},
                "default": [],
            },
            "system_prompt": {"type": "string"},
            "model": {
                "type": "string",
                "description": "Gemini model name.",
                "default": DEFAULT_MODEL_NAME,
            },
            "include_thinking": {"type": "boolean", "default": False},
            "google_search": {
                "type": "boolean",
                "description": "Enable Gemini Google Search grounding for this request.",
                "default": False,
            },
            "response_json_schema": {
                "type": "object",
                "description": (
                    "Optional JSON Schema object for structured JSON output. "
                    "Mutually exclusive with `response_json_schema_path`."
                ),
            },
            "response_json_schema_path": {
                "type": "string",
                "description": (
                    "Absolute path to a JSON file containing a JSON Schema object. "
                    "Mutually exclusive with `response_json_schema`."
                ),
            },
            "history": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["role", "text"],
                    "additionalProperties": False,
                },
                "default": [],
            },
            "system_prompt_path": {"type": "string"},
            "history_path": {"type": "string"},
            "output_path": {
                "type": "string",
                "description": (
                    "Recommended absolute path for the full response text. "
                    "When provided, the response is written to this path even if it is short."
                ),
            },
            "rate_limit_mode": {
                "type": "string",
                "enum": ["fail_fast", "wait"],
                "default": RATE_LIMIT_MODE_FAIL_FAST,
                "description": (
                    "How to handle a cooled-down Vertex project/location/model quota slot. "
                    "`fail_fast` returns a structured rate-limit message; `wait` waits and retries."
                ),
            },
            "fallback_models": {
                "type": "array",
                "description": (
                    "Optional supported model names to try only after the requested model is rate-limited."
                ),
                "items": {"type": "string"},
                "default": [],
            },
            "rate_limit_max_wait_seconds": {
                "type": "number",
                "description": "Maximum cooldown wait in seconds when rate_limit_mode is `wait`.",
                "default": DEFAULT_RATE_LIMIT_MAX_WAIT_SECONDS,
            },
        },
        "required": ["prompt"],
        "additionalProperties": False,
    }


def _tool_definitions() -> list[mcp_types.Tool]:
    return [
        mcp_types.Tool(
            name="gemini_generate",
            description=(
                "Upload local absolute-path files to Gemini and return the response. "
                "Short responses are returned inline unless `output_path` is provided. "
                "Larger responses are written to `output_path` when provided or to the "
                "configured spill directory, with a preview and read guidance returned inline. "
                "Tool content text is a short receipt; use structuredContent for result fields. "
                "For concurrent work, use `gemini_generate_batch` with one job per artifact. "
                "Each job should use a unique output_path."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Required user prompt text.",
                    },
                    "files": {
                        "type": "array",
                        "description": "Optional local absolute file paths.",
                        "items": {"type": "string"},
                        "default": [],
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "Optional system prompt override.",
                    },
                    "model": {
                        "type": "string",
                        "description": "Gemini model name.",
                        "default": DEFAULT_MODEL_NAME,
                    },
                    "include_thinking": {
                        "type": "boolean",
                        "description": "Whether to include model thinking in the response when supported.",
                        "default": False,
                    },
                    "google_search": {
                        "type": "boolean",
                        "description": "Enable Gemini Google Search grounding for this request.",
                        "default": False,
                    },
                    "response_json_schema": {
                        "type": "object",
                        "description": (
                            "Optional JSON Schema object for structured JSON output. "
                            "Mutually exclusive with `response_json_schema_path`."
                        ),
                    },
                    "response_json_schema_path": {
                        "type": "string",
                        "description": (
                            "Absolute path to a JSON file containing a JSON Schema object. "
                            "Mutually exclusive with `response_json_schema`."
                        ),
                    },
                    "history": {
                        "type": "array",
                        "description": "Optional few-shot turns with role/text pairs.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string"},
                                "text": {"type": "string"},
                            },
                            "required": ["role", "text"],
                            "additionalProperties": False,
                        },
                        "default": [],
                    },
                    "system_prompt_path": {
                        "type": "string",
                        "description": (
                            "Absolute path to a UTF-8 text file whose contents are used as the "
                            "system prompt. Mutually exclusive with `system_prompt`. Use this for "
                            "large or repeated prompts to avoid resending the text on every call."
                        ),
                    },
                    "history_path": {
                        "type": "string",
                        "description": (
                            "Absolute path to a JSON file containing a list of {role, text} "
                            "few-shot turns. Mutually exclusive with `history`."
                        ),
                    },
                    "output_path": {
                        "type": "string",
                        "description": (
                            "Recommended. Absolute path to write the full UTF-8 response text, "
                            "even when the response is short. "
                            "Non-text response parts, if any, are written as sibling files "
                            "next to this path and kept out of the inline MCP payload."
                        ),
                    },
                    "rate_limit_mode": {
                        "type": "string",
                        "enum": ["fail_fast", "wait"],
                        "description": (
                            "How to handle a cooled-down Vertex project/location/model quota slot. "
                            "`fail_fast` returns a structured rate-limit message; `wait` waits and retries."
                        ),
                        "default": RATE_LIMIT_MODE_FAIL_FAST,
                    },
                    "fallback_models": {
                        "type": "array",
                        "description": (
                            "Optional supported model names to try only after the requested model is rate-limited."
                        ),
                        "items": {"type": "string"},
                        "default": [],
                    },
                    "rate_limit_max_wait_seconds": {
                        "type": "number",
                        "description": "Maximum cooldown wait in seconds when rate_limit_mode is `wait`.",
                        "default": DEFAULT_RATE_LIMIT_MAX_WAIT_SECONDS,
                    },
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "model": {"type": "string"},
                    "usage": {"type": "object"},
                    "elapsed_ms": {"type": "integer"},
                    "char_count": {"type": "integer"},
                    "byte_count": {"type": "integer"},
                    "line_count": {"type": "integer"},
                    "image_count": {"type": "integer"},
                    "text": {"type": "string"},
                    "text_preview": {"type": "string"},
                    "truncated": {"type": "boolean"},
                    "output_path": {"type": "string"},
                    "read_guidance": {"type": "string"},
                    "response_json": {},
                    "response_json_preview": {"type": "string"},
                    "response_json_error": {"type": "string"},
                    "images": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                    "grounding": {"type": "object"},
                },
                "required": [
                    "model",
                    "usage",
                    "elapsed_ms",
                    "char_count",
                    "byte_count",
                    "line_count",
                    "image_count",
                    "truncated",
                ],
                "additionalProperties": True,
            },
        ),
        mcp_types.Tool(
            name="gemini_generate_batch",
            description=(
                "Run multiple independent Gemini jobs concurrently inside this server. "
                "Each job has the same shape as `gemini_generate` and should use a unique "
                "`output_path`. Rate limits are tracked per Vertex project/location/model "
                "quota slot; a 429 response cools that slot before another slot or explicit "
                "fallback model is tried. If the aggregate batch response exceeds the inline "
                "budget, successful job outputs are saved to files and the response returns "
                "receipts plus read guidance."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "jobs": {
                        "type": "array",
                        "items": _batch_job_schema(),
                        "minItems": 1,
                    },
                    "max_concurrency": {
                        "type": "integer",
                        "description": (
                            "Maximum concurrent jobs, capped at "
                            f"{MAX_BATCH_CONCURRENCY}. Defaults to the configured Vertex credential count."
                        ),
                    },
                },
                "required": ["jobs"],
                "additionalProperties": False,
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "job_count": {"type": "integer"},
                    "ok_count": {"type": "integer"},
                    "error_count": {"type": "integer"},
                    "max_concurrency": {"type": "integer"},
                    "results": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                    "results_compacted": {"type": "boolean"},
                    "aggregate_byte_count": {"type": "integer"},
                    "aggregate_inline_limit": {"type": "integer"},
                    "read_guidance": {"type": "string"},
                    "results_path": {"type": "string"},
                    "results_omitted": {"type": "boolean"},
                    "omitted_result_count": {"type": "integer"},
                },
                "required": [
                    "job_count",
                    "ok_count",
                    "error_count",
                    "max_concurrency",
                    "results",
                ],
                "additionalProperties": False,
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
        "Thin stdio MCP wrapper for Gemini file uploads and generate_content calls. "
        "Use `gemini_generate` for one artifact or `gemini_generate_batch` for concurrent "
        "independent jobs. Vertex rate limits are tracked per project/location/model quota slot."
    ),
)


@server.list_tools()
async def handle_list_tools() -> list[mcp_types.Tool]:
    return _tool_definitions()


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict[str, Any] | None):
    args = arguments or {}

    try:
        if name == "gemini_generate":
            return _wrap_result(await _run_generate_from_args(args))

        if name == "gemini_generate_batch":
            jobs = args.get("jobs")
            if not isinstance(jobs, list) or not jobs:
                raise ValueError("jobs must be a non-empty array.")
            max_concurrency = _normalize_batch_concurrency(args.get("max_concurrency"))
            limiter = anyio.Semaphore(max_concurrency)
            results: list[dict[str, Any] | None] = [None] * len(jobs)
            raw_successes: list[dict[str, Any] | None] = [None] * len(jobs)

            async def run_job(index: int, job_args: Any) -> None:
                if not isinstance(job_args, dict):
                    results[index] = {
                        "index": index,
                        "ok": False,
                        "error": "ValueError: each job must be an object.",
                    }
                    return

                async with limiter:
                    job_id = job_args.get("id")
                    try:
                        raw_result, expect_json_response = await _generate_raw_from_args(job_args)
                        raw_successes[index] = {
                            "result": raw_result,
                            "output_path": job_args.get("output_path"),
                            "expect_json_response": expect_json_response,
                        }
                        job_result = _apply_output_policy(
                            dict(raw_result),
                            job_args.get("output_path"),
                            expect_json_response=expect_json_response,
                        )
                        job_result["index"] = index
                        job_result["ok"] = True
                        if isinstance(job_id, str) and job_id:
                            job_result["id"] = job_id
                        results[index] = job_result
                    except GeminiRateLimitError as exc:
                        error_result = exc.to_dict()
                        error_result.update(
                            {
                                "index": index,
                                "ok": False,
                                "error": exc.message,
                            }
                        )
                        if isinstance(job_id, str) and job_id:
                            error_result["id"] = job_id
                        results[index] = error_result
                    except Exception as exc:
                        error_result = {
                            "index": index,
                            "ok": False,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                        if isinstance(job_id, str) and job_id:
                            error_result["id"] = job_id
                        results[index] = error_result

            async with anyio.create_task_group() as task_group:
                for index, job in enumerate(jobs):
                    task_group.start_soon(run_job, index, job)

            finalized_results = [result for result in results if result is not None]
            ok_count = sum(1 for result in finalized_results if result.get("ok") is True)
            batch_result = {
                "job_count": len(jobs),
                "ok_count": ok_count,
                "error_count": len(jobs) - ok_count,
                "max_concurrency": max_concurrency,
                "results": finalized_results,
            }
            return _wrap_result(_apply_batch_aggregate_policy(batch_result, raw_successes))

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


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
