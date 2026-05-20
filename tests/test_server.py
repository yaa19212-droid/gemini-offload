from __future__ import annotations

import base64
import importlib
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import anyio


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
    def test_gemini_generate_tool_schema_uses_current_default_model(self) -> None:
        server = _load_server_module()

        tools = server._tool_definitions()
        gemini_generate = next(tool for tool in tools if tool.name == "gemini_generate")
        model_schema = gemini_generate.inputSchema["properties"]["model"]

        self.assertEqual(model_schema["default"], "gemini-3.1-pro-preview")

    def test_gemini_generate_tool_schema_includes_rate_limit_options(self) -> None:
        server = _load_server_module()

        tools = server._tool_definitions()
        gemini_generate = next(tool for tool in tools if tool.name == "gemini_generate")
        properties = gemini_generate.inputSchema["properties"]

        self.assertEqual(properties["rate_limit_mode"]["default"], "fail_fast")
        self.assertEqual(properties["rate_limit_mode"]["enum"], ["fail_fast", "wait"])
        self.assertIn("fallback_models", properties)
        self.assertEqual(properties["rate_limit_max_wait_seconds"]["default"], 120.0)

    def test_inline_image_output_becomes_mcp_image_content(self) -> None:
        server = _load_server_module()

        wrapped = server._wrap_result(
            server._apply_output_policy(
                {
                    "text": "caption",
                    "model": "gemini-3.5-flash",
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
                        "model": "gemini-3.5-flash",
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

    def test_batch_tool_runs_jobs_concurrently(self) -> None:
        server = _load_server_module()

        def fake_generate(*args):
            time.sleep(0.2)
            return {
                "text": "ok",
                "model": "gemini-3.1-pro-preview",
                "usage": {},
                "elapsed_ms": 200,
                "images": [],
            }

        async def run_batch():
            with patch.object(server, "generate", fake_generate):
                started_at = time.perf_counter()
                wrapped = await server.handle_call_tool(
                    "gemini_generate_batch",
                    {
                        "max_concurrency": 2,
                        "jobs": [
                            {"id": "a", "prompt": "one"},
                            {"id": "b", "prompt": "two"},
                        ],
                    },
                )
                elapsed = time.perf_counter() - started_at
            return wrapped, elapsed

        wrapped, elapsed = anyio.run(run_batch)

        self.assertLess(elapsed, 0.35)
        self.assertEqual(wrapped.structuredContent["job_count"], 2)
        self.assertEqual(wrapped.structuredContent["ok_count"], 2)
        self.assertEqual([item["id"] for item in wrapped.structuredContent["results"]], ["a", "b"])

    def test_single_generate_returns_structured_rate_limit_result(self) -> None:
        server = _load_server_module()

        def fake_generate(*args):
            raise server.GeminiRateLimitError(
                model="gemini-3.5-flash",
                attempted_models=["gemini-3.5-flash"],
                retry_after_seconds=9.0,
                quota_slots=["project1/global/gemini-3.5-flash"],
            )

        async def run_generate():
            with patch.object(server, "generate", fake_generate):
                return await server.handle_call_tool(
                    "gemini_generate",
                    {"prompt": "one", "model": "gemini-3.5-flash"},
                )

        wrapped = anyio.run(run_generate)

        self.assertTrue(wrapped.isError)
        self.assertEqual(wrapped.structuredContent["error_type"], "vertex_rate_limited")
        self.assertEqual(wrapped.structuredContent["retry_after_seconds"], 9.0)

    def test_batch_tool_keeps_rate_limit_as_per_job_error(self) -> None:
        server = _load_server_module()

        def fake_generate(*args):
            raise server.GeminiRateLimitError(
                model="gemini-3.5-flash",
                attempted_models=["gemini-3.5-flash"],
                retry_after_seconds=9.0,
                quota_slots=["project1/global/gemini-3.5-flash"],
            )

        async def run_batch():
            with patch.object(server, "generate", fake_generate):
                return await server.handle_call_tool(
                    "gemini_generate_batch",
                    {
                        "jobs": [
                            {"id": "a", "prompt": "one", "model": "gemini-3.5-flash"},
                        ],
                    },
                )

        wrapped = anyio.run(run_batch)

        result = wrapped.structuredContent["results"][0]
        self.assertFalse(result["ok"])
        self.assertEqual(result["id"], "a")
        self.assertEqual(result["error_type"], "vertex_rate_limited")
        self.assertEqual(wrapped.structuredContent["error_count"], 1)

    def test_list_gemini_models_includes_characteristics(self) -> None:
        server = _load_server_module()

        async def run_list_models():
            return await server.handle_call_tool("list_gemini_models", {})

        wrapped = anyio.run(run_list_models)

        self.assertEqual(
            wrapped.structuredContent["models"],
            ["gemini-3.1-pro-preview", "gemini-3-flash-preview", "gemini-3.5-flash"],
        )
        self.assertIn("gemini-3.5-flash", wrapped.structuredContent["model_characteristics"])

    def test_plugin_mcp_config_has_no_personal_absolute_paths(self) -> None:
        root = Path(__file__).resolve().parents[1]
        mcp_config = (root / "plugins" / "gemini-offload" / ".mcp.json").read_text(encoding="utf-8")
        start_script = (
            root / "plugins" / "gemini-offload" / "scripts" / "start-gemini-offload.ps1"
        ).read_text(encoding="utf-8")

        self.assertNotIn("D:/work/gemini-offload", mcp_config)
        self.assertNotIn("C:/Users/yaa19212-droid", mcp_config)
        self.assertIn("./scripts/start-gemini-offload.ps1", mcp_config)
        self.assertIn("GEMINI_OFFLOAD_REPO", start_script)
        self.assertIn("mcp_server", start_script)
