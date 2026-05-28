from __future__ import annotations

import base64
import importlib
import json
import os
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

    def test_gemini_generate_tool_schema_includes_google_search_option(self) -> None:
        server = _load_server_module()

        tools = server._tool_definitions()
        gemini_generate = next(tool for tool in tools if tool.name == "gemini_generate")
        batch_job_schema = server._batch_job_schema()

        self.assertEqual(
            gemini_generate.inputSchema["properties"]["google_search"]["default"],
            False,
        )
        self.assertEqual(
            batch_job_schema["properties"]["google_search"]["default"],
            False,
        )

    def test_gemini_generate_tool_schema_includes_response_json_schema_options(self) -> None:
        server = _load_server_module()

        tools = server._tool_definitions()
        gemini_generate = next(tool for tool in tools if tool.name == "gemini_generate")
        batch_job_schema = server._batch_job_schema()

        self.assertIn("response_json_schema", gemini_generate.inputSchema["properties"])
        self.assertIn("response_json_schema_path", gemini_generate.inputSchema["properties"])
        self.assertIn("response_json_schema", batch_job_schema["properties"])
        self.assertIn("response_json_schema_path", batch_job_schema["properties"])

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

        self.assertEqual(wrapped.structuredContent["text"], "caption")
        self.assertEqual(wrapped.content[0].text, "Result returned in structuredContent.text.")
        self.assertNotIn('"text": "caption"', wrapped.content[0].text)
        self.assertNotIn("text_preview", wrapped.structuredContent)
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
            self.assertEqual(wrapped.content[0].text, "Result saved to output_path. See structuredContent for details.")
            self.assertEqual(len(wrapped.content), 1)

    def test_short_text_output_is_returned_inline_without_preview(self) -> None:
        server = _load_server_module()

        wrapped = server._wrap_result(
            server._apply_output_policy(
                {
                    "text": "short response",
                    "model": "gemini-3.5-flash",
                    "usage": {},
                    "elapsed_ms": 12,
                    "images": [],
                },
                None,
            )
        )

        self.assertEqual(wrapped.structuredContent["text"], "short response")
        self.assertEqual(wrapped.content[0].text, "Result returned in structuredContent.text.")
        self.assertNotIn("short response", wrapped.content[0].text)
        self.assertEqual(wrapped.structuredContent["byte_count"], len(b"short response"))
        self.assertEqual(wrapped.structuredContent["line_count"], 1)
        self.assertFalse(wrapped.structuredContent["truncated"])
        self.assertNotIn("text_preview", wrapped.structuredContent)
        self.assertNotIn("output_path", wrapped.structuredContent)

    def test_long_text_output_auto_spills_to_configured_dir(self) -> None:
        server = _load_server_module()

        long_text = "\n".join(["x" * 80 for _ in range(60)])
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {server.ENV_OUTPUT_DIR: temp_dir}, clear=False):
                wrapped = server._wrap_result(
                    server._apply_output_policy(
                        {
                            "text": long_text,
                            "model": "gemini-3.5-flash",
                            "usage": {},
                            "elapsed_ms": 12,
                            "images": [],
                        },
                        None,
                    )
                )

            output_path = Path(wrapped.structuredContent["output_path"])
            self.assertEqual(output_path.parent, Path(temp_dir).resolve())
            self.assertEqual(output_path.suffix, ".txt")
            self.assertEqual(output_path.read_text(encoding="utf-8"), long_text)
            self.assertEqual(wrapped.structuredContent["text_preview"], long_text[: server.SPILL_PREVIEW_CHARS])
            self.assertNotIn("text", wrapped.structuredContent)
            self.assertIn(f"{wrapped.structuredContent['byte_count']} bytes", wrapped.structuredContent["read_guidance"])
            self.assertIn("60 lines", wrapped.structuredContent["read_guidance"])
            self.assertEqual(wrapped.content[0].text, "Full result saved to output_path. Follow structuredContent.read_guidance.")

    def test_short_text_output_path_manual_spills(self) -> None:
        server = _load_server_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "short.txt"
            wrapped = server._wrap_result(
                server._apply_output_policy(
                    {
                        "text": "short response",
                        "model": "gemini-3.5-flash",
                        "usage": {},
                        "elapsed_ms": 12,
                        "images": [],
                    },
                    str(output_path),
                )
            )

            self.assertEqual(output_path.read_text(encoding="utf-8"), "short response")
            self.assertEqual(wrapped.structuredContent["text_preview"], "short response")
            self.assertNotIn("text", wrapped.structuredContent)
            self.assertEqual(wrapped.content[0].text, "Result saved to output_path. See structuredContent for details.")

    def test_configured_auto_spill_dir_must_be_absolute(self) -> None:
        server = _load_server_module()

        with patch.dict(os.environ, {server.ENV_OUTPUT_DIR: "relative-output"}, clear=False):
            with self.assertRaisesRegex(ValueError, "must be an absolute path"):
                server._resolve_auto_output_dir()

    def test_short_json_output_is_returned_as_response_json(self) -> None:
        server = _load_server_module()

        wrapped = server._wrap_result(
            server._apply_output_policy(
                {
                    "text": '{"name":"Ada"}',
                    "model": "gemini-3.5-flash",
                    "usage": {},
                    "elapsed_ms": 12,
                    "images": [],
                },
                None,
                expect_json_response=True,
            )
        )

        self.assertEqual(wrapped.structuredContent["response_json"], {"name": "Ada"})
        self.assertEqual(wrapped.content[0].text, "Structured JSON returned in structuredContent.response_json.")
        self.assertNotIn("Ada", wrapped.content[0].text)
        self.assertNotIn("text", wrapped.structuredContent)
        self.assertNotIn("text_preview", wrapped.structuredContent)
        self.assertNotIn("response_json_preview", wrapped.structuredContent)

    def test_short_json_output_path_manual_spills(self) -> None:
        server = _load_server_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "short.json"
            wrapped = server._wrap_result(
                server._apply_output_policy(
                    {
                        "text": '{"name":"Ada"}',
                        "model": "gemini-3.5-flash",
                        "usage": {},
                        "elapsed_ms": 12,
                        "images": [],
                    },
                    str(output_path),
                    expect_json_response=True,
                )
            )

            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8")), {"name": "Ada"})
            self.assertEqual(wrapped.structuredContent["response_json_preview"], '{"name":"Ada"}')
            self.assertNotIn("response_json", wrapped.structuredContent)
            self.assertEqual(wrapped.content[0].text, "Structured JSON saved to output_path. See structuredContent.response_json_preview.")

    def test_long_json_output_auto_spills_with_response_json_preview(self) -> None:
        server = _load_server_module()

        payload = {"items": ["x" * 80 for _ in range(80)]}
        full_text = json.dumps(payload, ensure_ascii=False)
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {server.ENV_OUTPUT_DIR: temp_dir}, clear=False):
                wrapped = server._wrap_result(
                    server._apply_output_policy(
                        {
                            "text": full_text,
                            "model": "gemini-3.5-flash",
                            "usage": {},
                            "elapsed_ms": 12,
                            "images": [],
                        },
                        None,
                        expect_json_response=True,
                    )
                )

            output_path = Path(wrapped.structuredContent["output_path"])
            self.assertEqual(output_path.parent, Path(temp_dir).resolve())
            self.assertEqual(output_path.suffix, ".json")
            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8")), payload)
            self.assertEqual(
                wrapped.structuredContent["response_json_preview"],
                full_text[: server.SPILL_PREVIEW_CHARS],
            )
            self.assertNotIn("response_json", wrapped.structuredContent)
            self.assertIn("read_guidance", wrapped.structuredContent)
            self.assertEqual(wrapped.content[0].text, "Structured JSON saved to output_path. See structuredContent.response_json_preview.")

    def test_whitespace_heavy_json_uses_raw_byte_count_for_spill(self) -> None:
        server = _load_server_module()

        compact_payload = {"name": "Ada"}
        full_text = "{\n" + (" " * server.INLINE_OUTPUT_BYTE_LIMIT) + '"name": "Ada"\n}'
        self.assertEqual(json.loads(full_text), compact_payload)
        self.assertGreater(len(full_text.encode("utf-8")), server.INLINE_OUTPUT_BYTE_LIMIT)
        self.assertLess(
            len(json.dumps(compact_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
            server.INLINE_OUTPUT_BYTE_LIMIT,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {server.ENV_OUTPUT_DIR: temp_dir}, clear=False):
                wrapped = server._wrap_result(
                    server._apply_output_policy(
                        {
                            "text": full_text,
                            "model": "gemini-3.5-flash",
                            "usage": {},
                            "elapsed_ms": 12,
                            "images": [],
                        },
                        None,
                        expect_json_response=True,
                    )
                )

            output_path = Path(wrapped.structuredContent["output_path"])
            self.assertEqual(output_path.parent, Path(temp_dir).resolve())
            self.assertEqual(output_path.suffix, ".json")
            self.assertEqual(output_path.read_text(encoding="utf-8"), full_text)
            self.assertNotIn("response_json", wrapped.structuredContent)
            self.assertIn("response_json_preview", wrapped.structuredContent)
            self.assertIn("read_guidance", wrapped.structuredContent)

    def test_json_parse_failure_falls_back_to_text_output_with_error(self) -> None:
        server = _load_server_module()

        wrapped = server._wrap_result(
            server._apply_output_policy(
                {
                    "text": "{not-json",
                    "model": "gemini-3.5-flash",
                    "usage": {},
                    "elapsed_ms": 12,
                    "images": [],
                },
                None,
                expect_json_response=True,
            )
        )

        self.assertIn("JSONDecodeError", wrapped.structuredContent["response_json_error"])
        self.assertEqual(wrapped.structuredContent["text"], "{not-json")
        self.assertEqual(wrapped.content[0].text, "JSON parsing failed. See structuredContent.response_json_error and fallback text fields.")

    def test_response_json_schema_loader_validates_inputs(self) -> None:
        server = _load_server_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            schema_path = Path(temp_dir) / "schema.json"
            schema = {"type": "object", "properties": {"name": {"type": "string"}}}
            schema_path.write_text(json.dumps(schema), encoding="utf-8")

            self.assertEqual(server._load_json_schema(None, str(schema_path)), schema)
            with self.assertRaisesRegex(ValueError, "Pass either response_json_schema"):
                server._load_json_schema(schema, str(schema_path))
            with self.assertRaisesRegex(ValueError, "must be absolute"):
                server._load_json_schema(None, "schema.json")

            invalid_path = Path(temp_dir) / "invalid.json"
            invalid_path.write_text("{bad", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "valid JSON"):
                server._load_json_schema(None, str(invalid_path))

            list_path = Path(temp_dir) / "list.json"
            list_path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "JSON object"):
                server._load_json_schema(None, str(list_path))

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
        self.assertEqual(wrapped.content[0].text, "Batch result returned in structuredContent.")
        self.assertNotIn('"results"', wrapped.content[0].text)

    def test_batch_aggregate_budget_spills_success_text_jobs(self) -> None:
        server = _load_server_module()

        def fake_generate(prompt, *args):
            return {
                "text": f"{prompt}-" + ("x" * 900),
                "model": "gemini-3.5-flash",
                "usage": {},
                "elapsed_ms": 1,
                "images": [],
            }

        async def run_batch():
            with patch.object(server, "generate", fake_generate):
                return await server.handle_call_tool(
                    "gemini_generate_batch",
                    {
                        "jobs": [
                            {"id": f"job-{index}", "prompt": f"job-{index}"}
                            for index in range(5)
                        ],
                    },
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {server.ENV_OUTPUT_DIR: temp_dir}, clear=False):
                wrapped = anyio.run(run_batch)

            data = wrapped.structuredContent
            self.assertTrue(data["results_compacted"])
            self.assertGreater(data["aggregate_byte_count"], data["aggregate_inline_limit"])
            self.assertLessEqual(
                server._structured_content_byte_count(data),
                data["aggregate_inline_limit"],
            )
            self.assertEqual(data["ok_count"], 5)
            self.assertEqual(len(data["results"]), 5)
            self.assertIn("Successful job outputs were saved", data["read_guidance"])
            self.assertEqual(
                wrapped.content[0].text,
                "Batch result returned in structuredContent. Follow read_guidance before reading output files.",
            )
            for result in data["results"]:
                self.assertTrue(result["ok"])
                self.assertNotIn("text", result)
                self.assertIn("text_preview", result)
                output_path = Path(result["output_path"])
                self.assertEqual(output_path.parent, Path(temp_dir).resolve())
                self.assertEqual(output_path.suffix, ".txt")
                self.assertTrue(output_path.read_text(encoding="utf-8").startswith(result["id"]))

    def test_batch_aggregate_budget_spills_success_json_jobs(self) -> None:
        server = _load_server_module()

        def fake_generate(prompt, *args):
            return {
                "text": json.dumps({"id": prompt, "value": "x" * 900}),
                "model": "gemini-3.5-flash",
                "usage": {},
                "elapsed_ms": 1,
                "images": [],
            }

        async def run_batch():
            with patch.object(server, "generate", fake_generate):
                return await server.handle_call_tool(
                    "gemini_generate_batch",
                    {
                        "jobs": [
                            {
                                "id": f"json-{index}",
                                "prompt": f"json-{index}",
                                "response_json_schema": {"type": "object"},
                            }
                            for index in range(5)
                        ],
                    },
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {server.ENV_OUTPUT_DIR: temp_dir}, clear=False):
                wrapped = anyio.run(run_batch)

            data = wrapped.structuredContent
            self.assertTrue(data["results_compacted"])
            self.assertEqual(len(data["results"]), 5)
            for result in data["results"]:
                self.assertNotIn("response_json", result)
                self.assertIn("response_json_preview", result)
                output_path = Path(result["output_path"])
                self.assertEqual(output_path.parent, Path(temp_dir).resolve())
                self.assertEqual(output_path.suffix, ".json")
                self.assertEqual(json.loads(output_path.read_text(encoding="utf-8"))["id"], result["id"])

    def test_batch_aggregate_budget_writes_manifest_when_compacted_results_are_large(self) -> None:
        server = _load_server_module()

        def fake_generate(prompt, *args):
            return {
                "text": f"{prompt}-" + ("x" * 500),
                "model": "gemini-3.5-flash",
                "usage": {},
                "elapsed_ms": 1,
                "images": [],
            }

        async def run_batch():
            with patch.object(server, "generate", fake_generate):
                return await server.handle_call_tool(
                    "gemini_generate_batch",
                    {
                        "jobs": [
                            {"id": f"job-{index}", "prompt": f"job-{index}"}
                            for index in range(30)
                        ],
                    },
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {server.ENV_OUTPUT_DIR: temp_dir}, clear=False):
                wrapped = anyio.run(run_batch)

            data = wrapped.structuredContent
            self.assertTrue(data["results_compacted"])
            self.assertTrue(data["results_omitted"])
            self.assertEqual(data["results"], [])
            self.assertEqual(data["omitted_result_count"], 30)
            self.assertLessEqual(
                server._structured_content_byte_count(data),
                data["aggregate_inline_limit"],
            )
            results_path = Path(data["results_path"])
            self.assertEqual(results_path.parent, Path(temp_dir).resolve())
            self.assertTrue(results_path.name.startswith("batch-results-"))
            manifest = json.loads(results_path.read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["results"]), 30)
            self.assertIn("Full compacted batch results were saved", data["read_guidance"])

    def test_batch_compaction_force_spills_invalid_json_schema_text(self) -> None:
        server = _load_server_module()

        def fake_generate(prompt, *args):
            return {
                "text": f"{{not-json-{prompt}-" + ("x" * 900),
                "model": "gemini-3.5-flash",
                "usage": {},
                "elapsed_ms": 1,
                "images": [],
            }

        async def run_batch():
            with patch.object(server, "generate", fake_generate):
                return await server.handle_call_tool(
                    "gemini_generate_batch",
                    {
                        "jobs": [
                            {
                                "id": f"bad-json-{index}",
                                "prompt": f"bad-json-{index}",
                                "response_json_schema": {"type": "object"},
                            }
                            for index in range(5)
                        ],
                    },
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {server.ENV_OUTPUT_DIR: temp_dir}, clear=False):
                wrapped = anyio.run(run_batch)

            data = wrapped.structuredContent
            self.assertTrue(data["results_compacted"])
            for result in data["results"]:
                self.assertTrue(result["ok"])
                self.assertIn("response_json_error", result)
                self.assertNotIn("text", result)
                self.assertIn("text_preview", result)
                output_path = Path(result["output_path"])
                self.assertEqual(output_path.parent, Path(temp_dir).resolve())
                self.assertEqual(output_path.suffix, ".txt")
                self.assertTrue(output_path.read_text(encoding="utf-8").startswith("{not-json-"))

    def test_batch_compaction_reuses_already_spilled_job_outputs(self) -> None:
        server = _load_server_module()

        def fake_generate(prompt, *args):
            return {
                "text": f"{prompt}-" + ("x" * server.INLINE_OUTPUT_BYTE_LIMIT),
                "model": "gemini-3.5-flash",
                "usage": {},
                "elapsed_ms": 1,
                "images": [],
            }

        async def run_batch():
            with patch.object(server, "generate", fake_generate):
                return await server.handle_call_tool(
                    "gemini_generate_batch",
                    {
                        "jobs": [
                            {"id": f"job-{index}", "prompt": f"job-{index}"}
                            for index in range(10)
                        ],
                    },
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {server.ENV_OUTPUT_DIR: temp_dir}, clear=False):
                wrapped = anyio.run(run_batch)

            data = wrapped.structuredContent
            result_files = sorted(path for path in Path(temp_dir).iterdir() if path.name.startswith("response-"))
            manifest_files = sorted(path for path in Path(temp_dir).iterdir() if path.name.startswith("batch-results-"))
            self.assertEqual(len(result_files), 10)
            self.assertLessEqual(len(manifest_files), 1)
            if data.get("results_omitted"):
                manifest = json.loads(Path(data["results_path"]).read_text(encoding="utf-8"))
                job_results = manifest["results"]
            else:
                job_results = data["results"]
            self.assertEqual(
                sorted(Path(result["output_path"]) for result in job_results),
                result_files,
            )

    def test_batch_aggregate_budget_preserves_inline_error_jobs(self) -> None:
        server = _load_server_module()

        def fake_generate(prompt, *args):
            if prompt == "bad":
                raise server.GeminiRateLimitError(
                    model="gemini-3.5-flash",
                    attempted_models=["gemini-3.5-flash"],
                    retry_after_seconds=9.0,
                    quota_slots=["project1/global/gemini-3.5-flash"],
                )
            return {
                "text": f"{prompt}-" + ("x" * 900),
                "model": "gemini-3.5-flash",
                "usage": {},
                "elapsed_ms": 1,
                "images": [],
            }

        async def run_batch():
            with patch.object(server, "generate", fake_generate):
                return await server.handle_call_tool(
                    "gemini_generate_batch",
                    {
                        "jobs": [
                            {"id": f"job-{index}", "prompt": f"job-{index}"}
                            for index in range(4)
                        ]
                        + [{"id": "bad", "prompt": "bad", "model": "gemini-3.5-flash"}],
                    },
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {server.ENV_OUTPUT_DIR: temp_dir}, clear=False):
                wrapped = anyio.run(run_batch)

            data = wrapped.structuredContent
            self.assertEqual(data["ok_count"], 4)
            self.assertEqual(data["error_count"], 1)
            error_result = next(result for result in data["results"] if result["ok"] is False)
            self.assertEqual(error_result["id"], "bad")
            self.assertEqual(error_result["error_type"], "vertex_rate_limited")
            self.assertIn("error", error_result)

    def test_google_search_option_is_passed_to_generate(self) -> None:
        server = _load_server_module()
        observed: list[bool] = []

        def fake_generate(
            prompt,
            files,
            system_prompt,
            model,
            include_thinking,
            history,
            rate_limit_mode,
            fallback_models,
            rate_limit_max_wait_seconds,
            google_search,
            response_json_schema,
        ):
            observed.append((google_search, response_json_schema))
            return {
                "text": "ok",
                "model": model,
                "usage": {},
                "elapsed_ms": 1,
                "images": [],
            }

        async def run_generate():
            with patch.object(server, "generate", fake_generate):
                return await server.handle_call_tool(
                    "gemini_generate",
                    {
                        "prompt": "one",
                        "google_search": True,
                        "response_json_schema": {"type": "object"},
                    },
                )

        wrapped = anyio.run(run_generate)

        self.assertEqual(observed, [(True, {"type": "object"})])
        self.assertEqual(wrapped.structuredContent["response_json_error"].split(":", 1)[0], "JSONDecodeError")
        self.assertEqual(wrapped.structuredContent["text"], "ok")

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

        self.assertNotIn("D:/", mcp_config)
        self.assertNotIn("C:/Users/", mcp_config)
        self.assertIn("./scripts/start-gemini-offload.ps1", mcp_config)
        self.assertIn("GEMINI_OFFLOAD_REPO", start_script)
        self.assertIn("GEMINI_OFFLOAD_OUTPUT_DIR", start_script)
        self.assertIn("mcp_server", start_script)
