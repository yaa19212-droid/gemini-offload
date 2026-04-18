"""Low-level stdio MCP server for Gemini offload."""

from __future__ import annotations

import base64
import json
import mimetypes
import pathlib
from typing import Any

import anyio
import mcp.types as mcp_types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError

from .gemini_client import AVAILABLE_MODELS, DEFAULT_MODEL_NAME, detect_mime, generate


SERVER_NAME = "gemini-offload"
SERVER_VERSION = "0.1.0"


def _raise_mcp_error(code: int, message: str, data: Any = None) -> None:
    raise McpError(mcp_types.ErrorData(code=code, message=message, data=data))


INLINE_PREVIEW_CHARS = 300
FILE_PREVIEW_CHARS = 100
IMAGE_EXTENSION_OVERRIDES = {
    "image/jpeg": ".jpg",
}


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


def _apply_output_policy(result: dict[str, Any], output_path: Any) -> dict[str, Any]:
    full_text: str = result.pop("text", "") or ""
    raw_images = result.pop("images", []) or []
    char_count = len(full_text)
    trimmed = dict(result)
    trimmed["char_count"] = char_count
    trimmed["image_count"] = 0

    path_obj: pathlib.Path | None = None
    if output_path is not None:
        if not isinstance(output_path, str) or not output_path.strip():
            raise ValueError("output_path must be a non-empty string when provided.")
        path_obj = pathlib.Path(output_path)
        if not path_obj.is_absolute():
            raise ValueError(f"output_path must be absolute: {output_path}")
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        encoded = full_text.encode("utf-8")
        path_obj.write_bytes(encoded)
        trimmed["output_path"] = str(path_obj)
        trimmed["byte_count"] = len(encoded)
        trimmed["text_preview"] = full_text[:FILE_PREVIEW_CHARS]
        trimmed["truncated"] = char_count > FILE_PREVIEW_CHARS
    else:
        trimmed["text_preview"] = full_text[:INLINE_PREVIEW_CHARS]
        trimmed["truncated"] = char_count > INLINE_PREVIEW_CHARS

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


def _wrap_result(result: dict[str, Any]) -> mcp_types.CallToolResult:
    structured_content = dict(result)
    inline_images = structured_content.pop("_inline_images", [])
    content: list[Any] = [
        mcp_types.TextContent(
            type="text",
            text=json.dumps(structured_content, ensure_ascii=False, indent=2),
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
        isError=False,
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


def _tool_definitions() -> list[mcp_types.Tool]:
    return [
        mcp_types.Tool(
            name="gemini_generate",
            description=(
                "Upload local absolute-path files to Gemini and return the response. "
                "RECOMMENDED: pass `output_path` (absolute path) so the full text is written "
                "to disk and only a 100-char head preview is returned inline. "
                "If `output_path` is omitted the response is truncated to the first 300 chars "
                "(full text is NOT recoverable from the inline response in that case) — "
                "omit only for small one-off calls. "
                "Calls are processed sequentially over stdio — batching multiple calls in one "
                "message does NOT parallelize. Issue calls one at a time and verify each "
                "result's quality before continuing to the next."
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
                            "Recommended. Absolute path to write the full UTF-8 response text. "
                            "If Gemini returns images, they are also written as sibling files "
                            "next to this path and omitted from the inline MCP payload."
                        ),
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
                    "image_count": {"type": "integer"},
                    "text_preview": {"type": "string"},
                    "truncated": {"type": "boolean"},
                    "output_path": {"type": "string"},
                    "byte_count": {"type": "integer"},
                    "images": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                },
                "required": [
                    "model",
                    "usage",
                    "elapsed_ms",
                    "char_count",
                    "image_count",
                    "text_preview",
                    "truncated",
                ],
                "additionalProperties": True,
            },
        ),
        mcp_types.Tool(
            name="list_gemini_models",
            description="List the Gemini models supported by this server.",
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
                    }
                },
                "required": ["models"],
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
        "NOTE: This server processes tool calls sequentially over a single stdio channel — "
        "issuing multiple `gemini_generate` calls in one message does NOT run them in parallel. "
        "Call sequentially and inspect each result before issuing the next so quality issues "
        "are caught early instead of amplified across a batch."
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
            system_prompt = _load_system_prompt(
                args.get("system_prompt"), args.get("system_prompt_path")
            )
            history = _load_history(args.get("history"), args.get("history_path"))
            result = await anyio.to_thread.run_sync(
                generate,
                args["prompt"],
                args.get("files"),
                system_prompt,
                args.get("model", DEFAULT_MODEL_NAME),
                args.get("include_thinking", False),
                history,
            )
            return _wrap_result(_apply_output_policy(result, args.get("output_path")))

        if name == "list_gemini_models":
            return _wrap_result({"models": AVAILABLE_MODELS})

        if name == "detect_mime":
            result = await anyio.to_thread.run_sync(detect_mime, args["path"])
            return _wrap_result(result)

    except (FileNotFoundError, ValueError) as exc:
        _raise_mcp_error(mcp_types.INVALID_PARAMS, str(exc))
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
