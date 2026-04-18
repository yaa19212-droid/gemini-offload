from __future__ import annotations

import base64
import importlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
import sys
import tempfile
import types
import unittest


def _load_server_module():
    for module_name in [
        "mcp",
        "mcp.types",
        "mcp.server",
        "mcp.server.stdio",
        "mcp.shared",
        "mcp.shared.exceptions",
        "mcp_server.server",
    ]:
        sys.modules.pop(module_name, None)

    @dataclass
    class ErrorData:
        code: int
        message: str
        data: object = None

    @dataclass
    class TextContent:
        type: str
        text: str

    @dataclass
    class ImageContent:
        type: str
        data: str
        mimeType: str

    @dataclass
    class Tool:
        name: str
        description: str
        inputSchema: dict
        outputSchema: dict

    @dataclass
    class CallToolResult:
        content: list
        structuredContent: dict
        isError: bool

    class DummyServer:
        def __init__(self, *args, **kwargs) -> None:
            self.args = args
            self.kwargs = kwargs

        def list_tools(self):
            def decorator(func):
                return func

            return decorator

        def call_tool(self):
            def decorator(func):
                return func

            return decorator

        async def run(self, *args, **kwargs) -> None:
            return None

        def create_initialization_options(self) -> dict:
            return {}

    class McpError(Exception):
        def __init__(self, error_data: ErrorData) -> None:
            super().__init__(error_data.message)
            self.error_data = error_data

    @asynccontextmanager
    async def stdio_server():
        yield (None, None)

    mcp_module = types.ModuleType("mcp")
    mcp_types_module = types.ModuleType("mcp.types")
    mcp_types_module.ErrorData = ErrorData
    mcp_types_module.TextContent = TextContent
    mcp_types_module.ImageContent = ImageContent
    mcp_types_module.Tool = Tool
    mcp_types_module.CallToolResult = CallToolResult
    mcp_types_module.INVALID_PARAMS = -32602
    mcp_types_module.INTERNAL_ERROR = -32603

    mcp_server_module = types.ModuleType("mcp.server")
    mcp_server_module.Server = DummyServer

    mcp_server_stdio_module = types.ModuleType("mcp.server.stdio")
    mcp_server_stdio_module.stdio_server = stdio_server

    mcp_shared_module = types.ModuleType("mcp.shared")
    mcp_shared_exceptions_module = types.ModuleType("mcp.shared.exceptions")
    mcp_shared_exceptions_module.McpError = McpError

    mcp_module.types = mcp_types_module

    sys.modules["mcp"] = mcp_module
    sys.modules["mcp.types"] = mcp_types_module
    sys.modules["mcp.server"] = mcp_server_module
    sys.modules["mcp.server.stdio"] = mcp_server_stdio_module
    sys.modules["mcp.shared"] = mcp_shared_module
    sys.modules["mcp.shared.exceptions"] = mcp_shared_exceptions_module

    return importlib.import_module("mcp_server.server")


class ServerOutputTests(unittest.TestCase):
    def test_inline_image_output_becomes_mcp_image_content(self) -> None:
        server = _load_server_module()

        wrapped = server._wrap_result(
            server._apply_output_policy(
                {
                    "text": "caption",
                    "model": "gemini-2.5-flash-image",
                    "usage": {},
                    "elapsed_ms": 12,
                    "images": [{"mime_type": "image/png", "data": b"png-bytes"}],
                },
                None,
            )
        )

        self.assertEqual(wrapped.structuredContent["image_count"], 1)
        self.assertEqual(wrapped.structuredContent["images"][0]["mime_type"], "image/png")
        self.assertEqual(len(wrapped.content), 2)
        self.assertEqual(wrapped.content[1].mimeType, "image/png")
        self.assertEqual(
            wrapped.content[1].data,
            base64.b64encode(b"png-bytes").decode("ascii"),
        )

    def test_output_path_writes_image_sidecar_files(self) -> None:
        server = _load_server_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "response.txt"
            wrapped = server._wrap_result(
                server._apply_output_policy(
                    {
                        "text": "caption",
                        "model": "gemini-2.5-flash-image",
                        "usage": {},
                        "elapsed_ms": 12,
                        "images": [{"mime_type": "image/png", "data": b"png-bytes"}],
                    },
                    str(output_path),
                )
            )

            self.assertEqual(output_path.read_text(encoding="utf-8"), "caption")
            self.assertEqual(wrapped.structuredContent["image_count"], 1)
            image_path = Path(wrapped.structuredContent["images"][0]["output_path"])
            self.assertTrue(image_path.exists())
            self.assertEqual(image_path.read_bytes(), b"png-bytes")
            self.assertEqual(image_path.name, "response.image-1.png")
            self.assertEqual(len(wrapped.content), 1)

