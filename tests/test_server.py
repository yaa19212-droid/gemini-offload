from __future__ import annotations

import base64
import importlib
import json
import os
import threading
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


def _text_request(prompt: str, *, output: dict | None = None, model: str | None = None) -> dict:
    request: dict = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
    }
    if output is not None:
        request["output"] = output
    if model is not None:
        request["model"] = model
    return request


def _text_item(item_id: str, prompt: str, *, output: dict | None = None, model: str | None = None) -> dict:
    return {"id": item_id, "request": _text_request(prompt, output=output, model=model)}


def _prompt_from_contents(contents: list[dict]) -> str:
    return contents[-1]["parts"][-1]["text"]


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
    def test_call_gemini_tool_schema_replaces_old_generate_tools(self) -> None:
        server = _load_server_module()

        tools = server._tool_definitions()
        names = [tool.name for tool in tools]

        self.assertIn("call_gemini", names)
        self.assertIn("manage_gemini_run", names)
        self.assertNotIn("gemini_generate", names)
        self.assertNotIn("gemini_generate_batch", names)

    def test_call_gemini_tool_schema_includes_explicit_and_template_shapes(self) -> None:
        server = _load_server_module()

        tools = server._tool_definitions()
        call_gemini = next(tool for tool in tools if tool.name == "call_gemini")
        schema = call_gemini.inputSchema

        self.assertEqual(schema["type"], "object")
        self.assertIn("items", schema["properties"])
        self.assertIn("template_path", schema["properties"])
        self.assertIn("execution", schema["properties"])
        lifecycle = schema["properties"]["execution"]["properties"]["lifecycle"]
        self.assertIn("Prefer background", lifecycle["description"])
        self.assertIn("prefer `background`", call_gemini.description)
        item_properties = schema["properties"]["items"]["items"]["properties"]
        self.assertIn("request", item_properties)
        self.assertIn("vars", item_properties)

        def contains_one_of(value):
            if isinstance(value, dict):
                return "oneOf" in value or any(contains_one_of(child) for child in value.values())
            if isinstance(value, list):
                return any(contains_one_of(child) for child in value)
            return False

        self.assertFalse(contains_one_of(schema))

    def test_internal_error_unwraps_task_group_and_returns_diagnostic_path(self) -> None:
        server = _load_server_module()

        class TaskGroupLikeError(RuntimeError):
            def __init__(self) -> None:
                super().__init__("task group failed")
                self.exceptions = (ValueError("inner failure"),)

        with tempfile.TemporaryDirectory() as temp_dir:
            run_root = Path(temp_dir)

            async def fail_execute(plan, **_kwargs):
                diagnostic = run_root / "blocking-failures" / f"{plan['run_id']}.json"
                diagnostic.parent.mkdir(parents=True, exist_ok=True)
                diagnostic.write_text("{}", encoding="utf-8")
                raise TaskGroupLikeError()

            async def scenario():
                with (
                    patch.dict(os.environ, {server.ENV_RUN_DIR: temp_dir}, clear=False),
                    patch.object(server, "_execute_run_plan", fail_execute),
                ):
                    await server.handle_call_tool(
                        "call_gemini",
                        {"items": [_text_item("a", "one")]},
                    )

            with self.assertRaises(Exception) as raised:
                anyio.run(scenario)

            error_data = raised.exception.error_data
            self.assertEqual(error_data.code, -32603)
            self.assertEqual(error_data.message, "ValueError: inner failure")
            self.assertEqual(Path(error_data.data["diagnostic_path"]).parent, run_root / "blocking-failures")

    def test_call_gemini_tool_schema_includes_rate_limit_tools_and_json_output(self) -> None:
        server = _load_server_module()

        tools = server._tool_definitions()
        call_gemini = next(tool for tool in tools if tool.name == "call_gemini")
        request_schema = call_gemini.inputSchema["properties"]["items"]["items"]["properties"]["request"]
        properties = request_schema["properties"]

        self.assertEqual(properties["model"]["default"], "gemini-3.7-flash")
        self.assertEqual(properties["tools"]["properties"]["google_search"]["default"], False)
        self.assertEqual(properties["rate_limit"]["properties"]["mode"]["default"], "fail_fast")
        self.assertEqual(properties["rate_limit"]["properties"]["mode"]["enum"], ["fail_fast", "wait"])
        self.assertIn("json_schema", properties["output"]["properties"])
        self.assertIn("json_schema_path", properties["output"]["properties"])
        self.assertNotIn("safety", properties)
        self.assertNotIn("safety_settings", properties)
        self.assertEqual(
            properties["media_resolution"]["properties"]["image"]["default"],
            "ultra_high",
        )
        self.assertEqual(
            properties["media_resolution"]["properties"]["pdf"]["default"],
            "high",
        )
        self.assertNotIn(
            "ultra_high",
            properties["media_resolution"]["properties"]["pdf"]["enum"],
        )
        self.assertNotIn(
            "ultra_high",
            properties["media_resolution"]["properties"]["video"]["enum"],
        )
        part_schema = request_schema["properties"]["contents"]["items"]["properties"]["parts"]["items"]
        part_properties = part_schema["properties"]
        self.assertIn("file_uri", part_properties)
        self.assertIn("mime_type", part_properties)
        self.assertEqual(
            part_properties["media_resolution"]["enum"],
            ["low", "medium", "high", "ultra_high", "off"],
        )

    def test_normalize_request_keeps_media_resolution_policy_and_file_uri_parts(self) -> None:
        server = _load_server_module()

        request = server._normalize_request(
            {
                "media_resolution": {"image": "high", "pdf": "medium", "video": "off"},
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {
                                "file_uri": "gs://bucket/doc.pdf",
                                "mime_type": "application/pdf",
                                "media_resolution": "low",
                            },
                            {"text": "OCR this."},
                        ],
                    }
                ],
            },
            lifecycle="blocking",
            run_dir=None,
            item_id="one",
        )

        self.assertEqual(
            request["media_resolution"],
            {"image": "high", "pdf": "medium", "video": "off"},
        )
        self.assertEqual(
            request["contents"][0]["parts"][0],
            {
                "file_uri": "gs://bucket/doc.pdf",
                "mime_type": "application/pdf",
                "media_resolution": "low",
            },
        )

    def test_normalize_request_rejects_invalid_media_resolution_inputs(self) -> None:
        server = _load_server_module()

        invalid_requests = [
            (
                {
                    "media_resolution": {"audio": "high"},
                    "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
                },
                "only supports keys",
            ),
            (
                {
                    "contents": [{"role": "user", "parts": [{"text": "hello", "media_resolution": "low"}]}],
                },
                "not valid for text",
            ),
            (
                {
                    "contents": [
                        {
                            "role": "user",
                            "parts": [
                                {
                                    "file_uri": "gs://bucket/audio.mp3",
                                    "mime_type": "audio/mpeg",
                                    "media_resolution": "high",
                                }
                            ],
                        }
                    ],
                },
                "not supported for audio",
            ),
        ]
        for request, message in invalid_requests:
            with self.subTest(request=request):
                with self.assertRaisesRegex(ValueError, message):
                    server._normalize_request(
                        request,
                        lifecycle="blocking",
                        run_dir=None,
                        item_id="one",
                    )

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
            bom_schema_path = Path(temp_dir) / "schema-bom.json"
            bom_schema_path.write_text(json.dumps(schema), encoding="utf-8-sig")
            self.assertEqual(server._load_json_schema(None, str(bom_schema_path)), schema)
            with self.assertRaisesRegex(ValueError, "Pass either output.json_schema"):
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

        with self.assertRaisesRegex(ValueError, "requires exactly one"):
            server._normalize_run_plan(
                {
                    "items": [
                        {
                            "request": {
                                "contents": [{"role": "user", "parts": [{"text": "Extract JSON."}]}],
                                "output": {"mode": "json_schema"},
                            }
                        }
                    ]
                }
            )

    def test_template_materialization_validates_placeholders_and_paths(self) -> None:
        server = _load_server_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "chunk.pdf"
            input_path.write_bytes(b"%PDF")
            template_path = root / "template.json"
            template_path.write_text(
                json.dumps(
                    {
                        "contents": [
                            {
                                "role": "user",
                                "parts": [
                                    {"file_path": "{{chunk_path}}"},
                                    {"text": "OCR page {{page}}."},
                                ],
                            }
                        ],
                        "output": {"mode": "text", "path": str(root / "out-{{page}}.md")},
                    }
                ),
                encoding="utf-8",
            )

            plan = server._normalize_run_plan(
                {
                    "template_path": str(template_path),
                    "items": [{"id": "p1", "vars": {"chunk_path": str(input_path), "page": 1}}],
                }
            )

            request = plan["items"][0]["request"]
            self.assertEqual(request["contents"][0]["parts"][0]["file_path"], str(input_path))
            self.assertEqual(request["contents"][0]["parts"][1]["text"], "OCR page 1.")
            self.assertEqual(request["output_path"], str(root / "out-1.md"))

            bom_template_path = root / "template-bom.json"
            bom_template_path.write_text(template_path.read_text(encoding="utf-8"), encoding="utf-8-sig")
            bom_plan = server._normalize_run_plan(
                {
                    "template_path": str(bom_template_path),
                    "items": [{"id": "p1", "vars": {"chunk_path": str(input_path), "page": 1}}],
                }
            )
            self.assertEqual(bom_plan["items"][0]["request"]["output_path"], str(root / "out-1.md"))

            with self.assertRaisesRegex(ValueError, "Missing template var"):
                server._normalize_run_plan(
                    {
                        "template_path": str(template_path),
                        "items": [{"id": "p1", "vars": {"chunk_path": str(input_path)}}],
                    }
                )

            with self.assertRaisesRegex(ValueError, "Unused template vars"):
                server._normalize_run_plan(
                    {
                        "template_path": str(template_path),
                        "items": [
                            {
                                "id": "p1",
                                "vars": {"chunk_path": str(input_path), "page": 1, "unused": "x"},
                            }
                        ],
                    }
                )

            with self.assertRaisesRegex(ValueError, "Invalid placeholder name"):
                server._normalize_run_plan(
                    {
                        "template_path": str(template_path),
                        "items": [
                            {
                                "id": "p1",
                                "vars": {"chunk_path": str(input_path), "page": 1, "bad/name": "x"},
                            }
                        ],
                    }
                )

    def test_background_call_returns_receipt_and_persists_run_files(self) -> None:
        server = _load_server_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            def fake_spawn(run_dir, run_id, run_token):
                locator = {"run_id": run_id, "pid": 12345, "create_time": 1.0, "run_token": run_token}
                server._write_json(Path(run_dir) / "locator.json", locator)
                return locator

            async def run_call():
                with patch.dict(os.environ, {server.ENV_RUN_DIR: temp_dir}, clear=False):
                    with patch.object(server, "_spawn_worker", fake_spawn):
                        return await server.handle_call_tool(
                            "call_gemini",
                            {
                                "execution": {"lifecycle": "background", "max_concurrency": 2},
                                "items": [_text_item("p1", "OCR this.")],
                            },
                        )

            wrapped = anyio.run(run_call)
            data = wrapped.structuredContent
            run_dir = Path(data["run_dir"])

            self.assertNotIn("lifecycle", data)
            self.assertIn("run_id", data)
            self.assertEqual(data["status"], "starting")
            self.assertTrue((run_dir / "plan.json").exists())
            self.assertTrue((run_dir / "status.json").exists())
            self.assertTrue((run_dir / "events.jsonl").exists())
            self.assertTrue((run_dir / "locator.json").exists())
            plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
            item = plan["items"][0]
            self.assertEqual(item["id"], "p1")
            self.assertEqual(item["storage_key"], "item-000001")
            self.assertTrue(item["request"]["output_managed"])
            self.assertEqual(Path(item["request"]["output_path"]).name, "item-000001.txt")
            self.assertEqual(
                wrapped.content[0].text,
                "Background Gemini run started. Use structuredContent paths and manage_gemini_run for progress.",
            )

    def test_background_item_id_is_not_used_as_managed_filename(self) -> None:
        server = _load_server_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {server.ENV_RUN_DIR: temp_dir}, clear=False):
                plan = server._normalize_run_plan(
                    {
                        "execution": {"lifecycle": "background"},
                        "items": [_text_item("../../outside", "OCR this.")],
                    }
                )

            item = plan["items"][0]
            output_path = Path(item["request"]["output_path"])
            self.assertEqual(item["id"], "../../outside")
            self.assertEqual(item["storage_key"], "item-000001")
            self.assertEqual(output_path.name, "item-000001.txt")
            self.assertEqual(output_path.parent, Path(plan["run_dir"]) / "outputs")

    def test_background_explicit_output_path_remains_unmanaged(self) -> None:
        server = _load_server_module()

        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as output_dir:
            explicit_path = Path(output_dir) / "caller-selected.txt"
            with patch.dict(os.environ, {server.ENV_RUN_DIR: temp_dir}, clear=False):
                plan = server._normalize_run_plan(
                    {
                        "execution": {"lifecycle": "background"},
                        "items": [
                            _text_item(
                                "opaque/id",
                                "OCR this.",
                                output={"path": str(explicit_path)},
                            )
                        ],
                    }
                )

            request = plan["items"][0]["request"]
            self.assertEqual(request["output_path"], str(explicit_path))
            self.assertFalse(request["output_managed"])
            self.assertNotEqual(explicit_path.parent, Path(plan["run_dir"]) / "outputs")

    def test_run_plan_rejects_duplicate_item_ids(self) -> None:
        server = _load_server_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {server.ENV_RUN_DIR: temp_dir}, clear=False):
                with self.assertRaisesRegex(ValueError, "item ids must be unique"):
                    server._normalize_run_plan(
                        {
                            "execution": {"lifecycle": "background"},
                            "items": [_text_item("same", "one"), _text_item("same", "two")],
                        }
                    )

    def test_manage_run_rejects_traversal_and_out_of_root_run_dirs(self) -> None:
        server = _load_server_module()

        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside_dir:
            with patch.dict(os.environ, {server.ENV_RUN_DIR: temp_dir}, clear=False):
                with self.assertRaisesRegex(ValueError, "managed run identifier"):
                    server._run_dir_from_args({"run_id": "run-../escape"})

                outside_run = Path(outside_dir) / "run-outside"
                with self.assertRaisesRegex(ValueError, "immediate child"):
                    server._run_dir_from_args({"run_dir": str(outside_run)})

    def test_manage_run_rejects_symlink_escape_when_supported(self) -> None:
        server = _load_server_module()

        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside_dir:
            run_root = Path(temp_dir)
            outside_run = Path(outside_dir) / "run-linked"
            outside_run.mkdir()
            linked_run = run_root / "run-linked"
            try:
                os.symlink(outside_run, linked_run, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")

            with patch.dict(os.environ, {server.ENV_RUN_DIR: str(run_root)}, clear=False):
                with self.assertRaisesRegex(ValueError, "immediate child"):
                    server._run_dir_from_args({"run_dir": str(linked_run)})

    def test_atomic_text_write_preserves_old_content_when_replace_fails(self) -> None:
        server = _load_server_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "result.txt"
            target.write_text("old", encoding="utf-8")
            with patch.object(server.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    server._atomic_write_text(target, "new")
            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            self.assertEqual(list(Path(temp_dir).glob(".result.txt.*.tmp")), [])

    def test_background_worker_writes_outputs_status_and_progress_events(self) -> None:
        server = _load_server_module()

        def fake_generate_request(*args):
            return {
                "text": "worker result",
                "model": "gemini-3.5-flash",
                "usage": {},
                "elapsed_ms": 1,
                "images": [],
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {server.ENV_RUN_DIR: temp_dir}, clear=False):
                plan = server._normalize_run_plan(
                    {
                        "execution": {"lifecycle": "background"},
                        "items": [_text_item("p1", "OCR this.")],
                    }
                )
                run_dir = Path(plan["run_dir"])
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "control").mkdir(parents=True, exist_ok=True)
                (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
                server._write_json(run_dir / "plan.json", plan)
                server._write_status(run_dir, server._initial_status(plan, "starting"))
                lease_generation = server._run_store().acquire_lease(
                    plan["run_id"], "token", owner_pid=1
                )
                server._write_json(
                    run_dir / "locator.json",
                    {
                        "run_id": plan["run_id"],
                        "run_token": "token",
                        "lease_generation": lease_generation,
                        "pid": 1,
                    },
                )

                with patch.object(server, "generate_request", fake_generate_request):
                    anyio.run(server.run_worker_from_dir, str(run_dir), plan["run_id"], "token")

                status = server._manage_gemini_run({"action": "status", "run_dir": str(run_dir)})
                progress = server._manage_gemini_run({"action": "progress", "run_dir": str(run_dir)})
                listed = server._manage_gemini_run({"action": "list"})

            output_path = Path(plan["items"][0]["request"]["output_path"])
            self.assertEqual(output_path.read_text(encoding="utf-8"), "worker result")
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["ok_count"], 1)
            self.assertFalse(status["process_alive"])
            self.assertGreaterEqual(len(progress["events"]), 3)
            self.assertEqual(progress["next_event_offset"], len(progress["events"]))
            self.assertIn(plan["run_id"], [run["run_id"] for run in listed["runs"]])
            stored_status = server.RunStore(temp_dir).read_run_snapshot(plan["run_id"])
            stored_events = server.RunStore(temp_dir).list_events(plan["run_id"])
            self.assertEqual(stored_status["status"], "completed")
            self.assertGreaterEqual(len(stored_events), 3)

    def test_manage_gemini_run_stop_cancel_and_resume_write_control_and_spawn(self) -> None:
        server = _load_server_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {server.ENV_RUN_DIR: temp_dir}, clear=False):
                plan = server._normalize_run_plan(
                    {
                        "execution": {"lifecycle": "background"},
                        "items": [_text_item("p1", "OCR this.")],
                    }
                )
                run_dir = Path(plan["run_dir"])
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "control").mkdir(parents=True, exist_ok=True)
                server._write_json(run_dir / "plan.json", plan)
                server._write_status(run_dir, server._initial_status(plan, "running"))

                stop_result = server._manage_gemini_run({"action": "stop", "run_dir": str(run_dir)})
                self.assertTrue(Path(stop_result["control_path"]).exists())

                cancel_result = server._manage_gemini_run({"action": "cancel", "run_dir": str(run_dir)})
                self.assertTrue(Path(cancel_result["control_path"]).exists())

                def fake_spawn(run_dir_arg, run_id, run_token):
                    locator = {"run_id": run_id, "pid": 5678, "create_time": 1.0, "run_token": run_token}
                    server._write_json(Path(run_dir_arg) / "locator.json", locator)
                    return locator

                with patch.object(server, "_spawn_worker", fake_spawn):
                    resume_result = server._manage_gemini_run({"action": "resume", "run_dir": str(run_dir)})

                self.assertEqual(resume_result["status"], "starting")
                self.assertEqual(resume_result["pid"], 5678)
                self.assertFalse((run_dir / "control" / "stop.json").exists())
                self.assertFalse((run_dir / "control" / "cancel.json").exists())

    def test_concurrent_resume_attempts_spawn_only_one_worker(self) -> None:
        server = _load_server_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {server.ENV_RUN_DIR: temp_dir}, clear=False):
                plan = server._normalize_run_plan(
                    {
                        "execution": {"lifecycle": "background"},
                        "items": [_text_item("p1", "OCR this.")],
                    }
                )
                run_dir = Path(plan["run_dir"])
                server._write_json(run_dir / "plan.json", plan)
                failed = server._initial_status(plan, "failed")
                server._write_status(run_dir, failed)

                barrier = threading.Barrier(2)
                lock = threading.Lock()
                spawned: list[str] = []
                outcomes: list[tuple[str, object]] = []

                def fenced_spawn(run_dir_arg, run_id, run_token):
                    barrier.wait(timeout=5)
                    generation = server._run_store().acquire_lease(run_id, run_token)
                    with lock:
                        spawned.append(run_token)
                    locator = {
                        "run_id": run_id,
                        "pid": 6000 + generation,
                        "create_time": 1.0,
                        "run_token": run_token,
                        "lease_generation": generation,
                    }
                    server._write_json(Path(run_dir_arg) / "locator.json", locator)
                    return locator

                def resume() -> None:
                    try:
                        result = server._manage_gemini_run(
                            {"action": "resume", "run_dir": str(run_dir)}
                        )
                        outcome: tuple[str, object] = ("ok", result["pid"])
                    except ValueError as exc:
                        outcome = ("error", str(exc))
                    with lock:
                        outcomes.append(outcome)

                with patch.object(server, "_spawn_worker", fenced_spawn):
                    threads = [threading.Thread(target=resume) for _ in range(2)]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=10)

                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertEqual([kind for kind, _ in outcomes].count("ok"), 1)
                self.assertEqual([kind for kind, _ in outcomes].count("error"), 1)
                self.assertEqual(len(spawned), 1)
                self.assertIn("active worker lease", next(value for kind, value in outcomes if kind == "error"))
                server._run_store().revoke_lease(plan["run_id"])

    def test_force_cancel_revokes_worker_fence_and_persists_canceled(self) -> None:
        server = _load_server_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {server.ENV_RUN_DIR: temp_dir}, clear=False):
                plan = server._normalize_run_plan(
                    {
                        "execution": {"lifecycle": "background"},
                        "items": [_text_item("p1", "OCR this.")],
                    }
                )
                run_dir = Path(plan["run_dir"])
                server._write_json(run_dir / "plan.json", plan)
                status = server._initial_status(plan, "running")
                server._write_status(run_dir, status)
                generation = server._run_store().acquire_lease(plan["run_id"], "token")
                with patch.object(
                    server,
                    "_terminate_verified_process_tree",
                    return_value={"all_gone": True, "terminated": True, "termination_failed": False, "alive_pids": []},
                ):
                    result = server._manage_gemini_run(
                        {"action": "cancel", "run_dir": str(run_dir), "force": True}
                    )
                self.assertTrue(result["forced_termination"])
                self.assertEqual(server._run_store().read_run_snapshot(plan["run_id"])["status"], "canceled")
                self.assertFalse(server._run_store().lease_matches(plan["run_id"], generation, "token"))

    def test_recorded_artifact_verification_detects_tampering(self) -> None:
        server = _load_server_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            run_root = Path(temp_dir) / "runs"
            run_dir = run_root / "run-artifact-check"
            outputs = run_dir / "outputs"
            outputs.mkdir(parents=True)
            output = outputs / "item-000001.txt"
            output.write_text("original", encoding="utf-8")
            with patch.dict(os.environ, {server.ENV_RUN_DIR: str(run_root)}):
                metadata = server._artifact_metadata(output, role="output", managed=True)
                self.assertEqual(server._verify_recorded_artifacts(run_dir, [metadata]), (True, "verified"))
                output.write_text("tampered", encoding="utf-8")
                verified, reason = server._verify_recorded_artifacts(run_dir, [metadata])
                self.assertFalse(verified)
                self.assertIn("mismatch", reason)

    def test_reconcile_stale_running_run_marks_failed(self) -> None:
        server = _load_server_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            run_root = Path(temp_dir) / "runs"
            run_dir = run_root / "run-stale"
            run_dir.mkdir(parents=True)
            with patch.dict(os.environ, {server.ENV_RUN_DIR: str(run_root)}):
                status = {
                    "run_id": "run-stale",
                    "lifecycle": "background",
                    "status": "running",
                    "items": [{"id": "a", "index": 0, "status": "running"}],
                }
                server._run_store().persist_status_snapshot(status)
                reconciled = server._reconcile_stale_runs()
                self.assertEqual(reconciled, ["run-stale"])
                recovered = server._run_store().read_run_snapshot("run-stale")
                self.assertEqual(recovered["status"], "failed")
                self.assertEqual(server._run_store().list_events("run-stale")[-1]["event"], "run_recovered_failed")

    def test_call_gemini_runs_items_concurrently(self) -> None:
        server = _load_server_module()

        def fake_generate_request(*args):
            time.sleep(0.2)
            return {
                "text": "ok",
                "model": "gemini-3.1-pro-preview",
                "usage": {},
                "elapsed_ms": 200,
                "images": [],
            }

        async def run_call():
            with patch.object(server, "generate_request", fake_generate_request):
                started_at = time.perf_counter()
                wrapped = await server.handle_call_tool(
                    "call_gemini",
                    {
                        "execution": {"max_concurrency": 2},
                        "items": [
                            _text_item("a", "one"),
                            _text_item("b", "two"),
                        ],
                    },
                )
                elapsed = time.perf_counter() - started_at
            return wrapped, elapsed

        wrapped, elapsed = anyio.run(run_call)

        self.assertLess(elapsed, 0.35)
        self.assertEqual(wrapped.structuredContent["item_count"], 2)
        self.assertEqual(wrapped.structuredContent["error_count"], 0)
        self.assertNotIn("ok_count", wrapped.structuredContent)
        self.assertEqual([item["id"] for item in wrapped.structuredContent["results"]], ["a", "b"])
        self.assertEqual(wrapped.content[0].text, "Gemini run returned in structuredContent.")
        self.assertNotIn('"results"', wrapped.content[0].text)

    def test_run_aggregate_budget_spills_success_text_items(self) -> None:
        server = _load_server_module()

        def fake_generate_request(contents, *args):
            prompt = _prompt_from_contents(contents)
            return {
                "text": f"{prompt}-" + ("x" * 900),
                "model": "gemini-3.5-flash",
                "usage": {},
                "elapsed_ms": 1,
                "images": [],
            }

        async def run_call():
            with patch.object(server, "generate_request", fake_generate_request):
                return await server.handle_call_tool(
                    "call_gemini",
                    {
                        "items": [
                            _text_item(f"job-{index}", f"job-{index}")
                            for index in range(5)
                        ],
                    },
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {server.ENV_OUTPUT_DIR: temp_dir}, clear=False):
                wrapped = anyio.run(run_call)

            data = wrapped.structuredContent
            self.assertTrue(data["results_compacted"])
            self.assertGreater(data["aggregate_byte_count"], server.BATCH_AGGREGATE_BYTE_LIMIT)
            self.assertLessEqual(
                server._structured_content_byte_count(data),
                server.BATCH_AGGREGATE_BYTE_LIMIT,
            )
            self.assertNotIn("aggregate_inline_limit", data)
            self.assertNotIn("ok_count", data)
            self.assertEqual(len(data["results"]), 5)
            self.assertIn("Successful item outputs were saved", data["read_guidance"])
            self.assertEqual(
                wrapped.content[0].text,
                "Gemini run returned in structuredContent. Follow read_guidance before reading output files.",
            )
            for result in data["results"]:
                self.assertTrue(result["ok"])
                self.assertNotIn("text", result)
                self.assertIn("text_preview", result)
                output_path = Path(result["output_path"])
                self.assertEqual(output_path.parent, Path(temp_dir).resolve())
                self.assertEqual(output_path.suffix, ".txt")
                self.assertTrue(output_path.read_text(encoding="utf-8").startswith(result["id"]))

    def test_run_aggregate_budget_spills_success_json_items(self) -> None:
        server = _load_server_module()

        def fake_generate_request(contents, *args):
            prompt = _prompt_from_contents(contents)
            return {
                "text": json.dumps({"id": prompt, "value": "x" * 900}),
                "model": "gemini-3.5-flash",
                "usage": {},
                "elapsed_ms": 1,
                "images": [],
            }

        async def run_call():
            with patch.object(server, "generate_request", fake_generate_request):
                return await server.handle_call_tool(
                    "call_gemini",
                    {
                        "items": [
                            _text_item(
                                f"json-{index}",
                                f"json-{index}",
                                output={"mode": "json_schema", "json_schema": {"type": "object"}},
                            )
                            for index in range(5)
                        ],
                    },
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {server.ENV_OUTPUT_DIR: temp_dir}, clear=False):
                wrapped = anyio.run(run_call)

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

    def test_run_aggregate_budget_writes_manifest_when_compacted_results_are_large(self) -> None:
        server = _load_server_module()

        def fake_generate_request(contents, *args):
            prompt = _prompt_from_contents(contents)
            return {
                "text": f"{prompt}-" + ("x" * 500),
                "model": "gemini-3.5-flash",
                "usage": {},
                "elapsed_ms": 1,
                "images": [],
            }

        async def run_call():
            with patch.object(server, "generate_request", fake_generate_request):
                return await server.handle_call_tool(
                    "call_gemini",
                    {
                        "items": [
                            _text_item(f"job-{index}", f"job-{index}")
                            for index in range(30)
                        ],
                    },
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {server.ENV_OUTPUT_DIR: temp_dir}, clear=False):
                wrapped = anyio.run(run_call)

            data = wrapped.structuredContent
            self.assertTrue(data["results_compacted"])
            self.assertTrue(data["results_omitted"])
            self.assertEqual(data["results"], [])
            self.assertEqual(data["item_count"], 30)
            self.assertNotIn("omitted_result_count", data)
            self.assertNotIn("aggregate_inline_limit", data)
            self.assertLessEqual(
                server._structured_content_byte_count(data),
                server.BATCH_AGGREGATE_BYTE_LIMIT,
            )
            results_path = Path(data["results_path"])
            self.assertEqual(results_path.parent, Path(temp_dir).resolve())
            self.assertTrue(results_path.name.startswith("run-results-"))
            manifest = json.loads(results_path.read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["results"]), 30)
            self.assertIn("Full compacted run results were saved", data["read_guidance"])

    def test_run_compaction_force_spills_invalid_json_schema_text(self) -> None:
        server = _load_server_module()

        def fake_generate_request(contents, *args):
            prompt = _prompt_from_contents(contents)
            return {
                "text": f"{{not-json-{prompt}-" + ("x" * 900),
                "model": "gemini-3.5-flash",
                "usage": {},
                "elapsed_ms": 1,
                "images": [],
            }

        async def run_call():
            with patch.object(server, "generate_request", fake_generate_request):
                return await server.handle_call_tool(
                    "call_gemini",
                    {
                        "items": [
                            _text_item(
                                f"bad-json-{index}",
                                f"bad-json-{index}",
                                output={"mode": "json_schema", "json_schema": {"type": "object"}},
                            )
                            for index in range(5)
                        ],
                    },
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {server.ENV_OUTPUT_DIR: temp_dir}, clear=False):
                wrapped = anyio.run(run_call)

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

    def test_run_compaction_reuses_already_spilled_item_outputs(self) -> None:
        server = _load_server_module()

        def fake_generate_request(contents, *args):
            prompt = _prompt_from_contents(contents)
            return {
                "text": f"{prompt}-" + ("x" * server.INLINE_OUTPUT_BYTE_LIMIT),
                "model": "gemini-3.5-flash",
                "usage": {},
                "elapsed_ms": 1,
                "images": [],
            }

        async def run_call():
            with patch.object(server, "generate_request", fake_generate_request):
                return await server.handle_call_tool(
                    "call_gemini",
                    {
                        "items": [
                            _text_item(f"job-{index}", f"job-{index}")
                            for index in range(10)
                        ],
                    },
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {server.ENV_OUTPUT_DIR: temp_dir}, clear=False):
                wrapped = anyio.run(run_call)

            data = wrapped.structuredContent
            result_files = sorted(path for path in Path(temp_dir).iterdir() if path.name.startswith("response-"))
            manifest_files = sorted(path for path in Path(temp_dir).iterdir() if path.name.startswith("run-results-"))
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

    def test_run_aggregate_budget_preserves_inline_error_items(self) -> None:
        server = _load_server_module()

        def fake_generate_request(contents, *args):
            prompt = _prompt_from_contents(contents)
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

        async def run_call():
            with patch.object(server, "generate_request", fake_generate_request):
                return await server.handle_call_tool(
                    "call_gemini",
                    {
                        "items": [
                            _text_item(f"job-{index}", f"job-{index}")
                            for index in range(4)
                        ]
                        + [_text_item("bad", "bad", model="gemini-3.5-flash")],
                    },
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {server.ENV_OUTPUT_DIR: temp_dir}, clear=False):
                wrapped = anyio.run(run_call)

            data = wrapped.structuredContent
            self.assertEqual(data["item_count"], 5)
            self.assertEqual(data["error_count"], 1)
            self.assertNotIn("ok_count", data)
            error_result = next(result for result in data["results"] if result["ok"] is False)
            self.assertEqual(error_result["id"], "bad")
            self.assertEqual(error_result["error_type"], "vertex_rate_limited")
            self.assertIn("error", error_result)

    def test_google_search_option_is_passed_to_generate(self) -> None:
        server = _load_server_module()
        observed: list[bool] = []

        def fake_generate_request(
            contents,
            system_prompt,
            model,
            include_thinking,
            rate_limit_mode,
            fallback_models,
            rate_limit_max_wait_seconds,
            google_search,
            response_json_schema,
            media_resolution_policy,
        ):
            observed.append((google_search, response_json_schema, media_resolution_policy))
            return {
                "text": "ok",
                "model": model,
                "usage": {},
                "elapsed_ms": 1,
                "images": [],
            }

        async def run_generate():
            with patch.object(server, "generate_request", fake_generate_request):
                return await server.handle_call_tool(
                    "call_gemini",
                    {
                        "items": [
                            {
                                "id": "one",
                                "request": {
                                    **_text_request(
                                        "one",
                                        output={"mode": "json_schema", "json_schema": {"type": "object"}},
                                    ),
                                    "tools": {"google_search": True},
                                },
                            }
                        ],
                        "execution": {"lifecycle": "blocking"},
                    },
                )

        wrapped = anyio.run(run_generate)

        self.assertEqual(
            observed,
            [(True, {"type": "object"}, {"image": "ultra_high", "pdf": "high", "video": "high"})],
        )
        result = wrapped.structuredContent["results"][0]
        self.assertEqual(result["response_json_error"].split(":", 1)[0], "JSONDecodeError")
        self.assertEqual(result["text"], "ok")

    def test_call_gemini_returns_rate_limit_as_item_result(self) -> None:
        server = _load_server_module()

        def fake_generate_request(*args):
            raise server.GeminiRateLimitError(
                model="gemini-3.5-flash",
                attempted_models=["gemini-3.5-flash"],
                retry_after_seconds=9.0,
                quota_slots=["project1/global/gemini-3.5-flash"],
            )

        async def run_generate():
            with patch.object(server, "generate_request", fake_generate_request):
                return await server.handle_call_tool(
                    "call_gemini",
                    {"items": [_text_item("one", "one", model="gemini-3.5-flash")]},
                )

        wrapped = anyio.run(run_generate)

        self.assertFalse(wrapped.isError)
        result = wrapped.structuredContent["results"][0]
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "vertex_rate_limited")
        self.assertEqual(result["retry_after_seconds"], 9.0)

    def test_call_gemini_keeps_rate_limit_as_per_item_error(self) -> None:
        server = _load_server_module()

        def fake_generate_request(*args):
            raise server.GeminiRateLimitError(
                model="gemini-3.5-flash",
                attempted_models=["gemini-3.5-flash"],
                retry_after_seconds=9.0,
                quota_slots=["project1/global/gemini-3.5-flash"],
            )

        async def run_call():
            with patch.object(server, "generate_request", fake_generate_request):
                return await server.handle_call_tool(
                    "call_gemini",
                    {
                        "items": [
                            _text_item("a", "one", model="gemini-3.5-flash"),
                        ],
                    },
                )

        wrapped = anyio.run(run_call)

        result = wrapped.structuredContent["results"][0]
        self.assertFalse(result["ok"])
        self.assertEqual(result["id"], "a")
        self.assertEqual(result["error_type"], "vertex_rate_limited")
        self.assertNotIn("error_count", wrapped.structuredContent)

    def test_list_gemini_models_returns_registry_projection(self) -> None:
        server = _load_server_module()
        tool = next(item for item in server._tool_definitions() if item.name == "list_gemini_models")
        self.assertEqual(tool.outputSchema["required"], ["models"])
        self.assertEqual(tool.outputSchema["properties"]["models"]["items"]["type"], "object")
        self.assertNotIn("model_characteristics", tool.outputSchema["properties"])
        self.assertNotIn("model_capabilities", tool.outputSchema["properties"])

        async def run_list_models():
            return await server.handle_call_tool("list_gemini_models", {})

        wrapped = anyio.run(run_list_models)
        models = wrapped.structuredContent["models"]

        self.assertEqual(
            [item["id"] for item in models],
            [
                "gemini-3.7-flash",
                "gemini-3.1-pro-preview",
                "gemini-3.6-flash",
                "gemini-3.5-flash",
            ],
        )
        self.assertNotIn("model_characteristics", wrapped.structuredContent)
        self.assertNotIn("model_capabilities", wrapped.structuredContent)
        capability = next(item for item in models if item["id"] == "gemini-3.5-flash")
        self.assertEqual(capability["google_search"], "supported")
        self.assertEqual(capability["json_schema"], "supported")
        self.assertIn("high", capability["thinking_levels"])
        self.assertNotIn("safety_off", capability)
        self.assertNotIn("guidance", capability)

    def test_plugin_mcp_config_has_no_personal_absolute_paths(self) -> None:
        root = Path(__file__).resolve().parents[1]
        mcp_config = (root / "plugins" / "gemini-offload" / ".mcp.json").read_text(encoding="utf-8")
        start_script = (
            root / "plugins" / "gemini-offload" / "scripts" / "start-gemini-offload.ps1"
        ).read_text(encoding="utf-8")

        self.assertNotIn("D:/", mcp_config)
        self.assertNotIn("C:/Users/", mcp_config)
        self.assertNotIn("CLAUDE_PLUGIN_ROOT", mcp_config)
        self.assertNotIn("CLAUDE_PLUGIN_ROOT", start_script)
        self.assertIn("./scripts/start-gemini-offload.ps1", mcp_config)
        self.assertIn("GEMINI_OFFLOAD_REPO", start_script)
        self.assertIn("GEMINI_OFFLOAD_OUTPUT_DIR", start_script)
        self.assertIn("mcp_server", start_script)

    def test_installer_emits_persistent_run_dir_config(self) -> None:
        root = Path(__file__).resolve().parents[1]
        installer = (root / "install-local.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('[string]$RunDir = (Join-Path $env:LOCALAPPDATA "gemini-offload\\runs")', installer)
        self.assertIn('GEMINI_OFFLOAD_RUN_DIR = ""$runDirConfigPath""', installer)
        self.assertIn("GetUnresolvedProviderPathFromPSPath($RunDir)", installer)

    def test_plugin_workflow_skill_matches_repo_skill(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source_root = root / "skills" / "gemini-offload-workflows"
        plugin_root = root / "plugins" / "gemini-offload" / "skills" / "gemini-offload-workflows"

        source_files = {
            path.relative_to(source_root): path
            for path in source_root.rglob("*")
            if path.is_file()
        }
        plugin_files = {
            path.relative_to(plugin_root): path
            for path in plugin_root.rglob("*")
            if path.is_file()
        }

        self.assertEqual(set(plugin_files), set(source_files))
        for relative_path, source_path in source_files.items():
            with self.subTest(path=str(relative_path)):
                self.assertEqual(
                    plugin_files[relative_path].read_bytes(),
                    source_path.read_bytes(),
                )

        skill_text = (source_root / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("file_uri", skill_text)
        self.assertIn("media_resolution", skill_text)
        self.assertIn("SQLite run store", skill_text)

    def test_package_server_and_plugin_versions_match(self) -> None:
        server = _load_server_module()
        root = Path(__file__).resolve().parents[1]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        package_version = next(
            line.split('"')[1]
            for line in pyproject.splitlines()
            if line.startswith("version = ")
        )
        plugin = json.loads(
            (root / "plugins" / "gemini-offload" / ".codex-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(package_version, "0.3.0")
        self.assertEqual(server.SERVER_VERSION, package_version)
        self.assertEqual(plugin["version"], package_version)

    def test_hook_reads_authoritative_run_store(self) -> None:
        root = Path(__file__).resolve().parents[1]
        hook = (
            root / "plugins" / "gemini-offload" / "hooks" / "gemini_run_status.py"
        ).read_text(encoding="utf-8")
        self.assertIn("RunStore", hook)
        self.assertIn("DEFAULT_DB_NAME", hook)
        self.assertNotIn('glob("run-*/status.json")', hook)
