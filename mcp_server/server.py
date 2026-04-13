"""Low-level stdio MCP server for Gemini offload."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import anyio
import mcp.types as mcp_types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError

from .gemini_client import AVAILABLE_MODELS, detect_mime, generate


SERVER_NAME = "gemini-offload"
SERVER_VERSION = "0.1.0"


def _raise_mcp_error(code: int, message: str, data: Any = None) -> None:
    raise McpError(mcp_types.ErrorData(code=code, message=message, data=data))


INLINE_PREVIEW_CHARS = 300
FILE_PREVIEW_CHARS = 100


def _apply_output_policy(result: dict[str, Any], output_path: Any) -> dict[str, Any]:
    full_text: str = result.pop("text", "") or ""
    char_count = len(full_text)
    trimmed = {k: v for k, v in result.items() if k != "text"}
    trimmed["char_count"] = char_count

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
        return trimmed

    trimmed["text_preview"] = full_text[:INLINE_PREVIEW_CHARS]
    trimmed["truncated"] = char_count > INLINE_PREVIEW_CHARS
    return trimmed


def _wrap_result(result: dict[str, Any]) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(
        content=[
            mcp_types.TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, indent=2),
            )
        ],
        structuredContent=result,
        isError=False,
    )


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
                "omit only for small one-off calls."
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
                        "default": "gemini-2.5-flash",
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
                    "output_path": {
                        "type": "string",
                        "description": (
                            "Recommended. Absolute path to write the full UTF-8 response text. "
                            "When set, only a 100-char head is returned inline."
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
                    "text_preview": {"type": "string"},
                    "truncated": {"type": "boolean"},
                    "output_path": {"type": "string"},
                    "byte_count": {"type": "integer"},
                },
                "required": ["model", "usage", "elapsed_ms", "char_count", "text_preview", "truncated"],
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
    instructions="Thin stdio MCP wrapper for Gemini file uploads and generate_content calls.",
)


@server.list_tools()
async def handle_list_tools() -> list[mcp_types.Tool]:
    return _tool_definitions()


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict[str, Any] | None):
    args = arguments or {}

    try:
        if name == "gemini_generate":
            result = await anyio.to_thread.run_sync(
                generate,
                args["prompt"],
                args.get("files"),
                args.get("system_prompt"),
                args.get("model", "gemini-2.5-flash"),
                args.get("include_thinking", False),
                args.get("history"),
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
