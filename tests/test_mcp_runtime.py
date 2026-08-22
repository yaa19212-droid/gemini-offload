from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio
import mcp.types as mcp_types
from mcp.server import Server


class McpRuntimeCompatibilityTests(unittest.TestCase):
    def test_real_mcp_runtime_imports_and_lists_tools(self) -> None:
        from mcp_server import server

        names = [tool.name for tool in server._tool_definitions()]
        self.assertEqual(
            names,
            ["call_gemini", "manage_gemini_run", "check_gemini_setup", "list_gemini_models", "detect_mime"],
        )
        self.assertEqual(server.SERVER_VERSION, "0.3.0")

    def test_setup_tool_surface_stays_compact(self) -> None:
        from mcp_server import server

        setup_tool = next(
            tool for tool in server._tool_definitions() if tool.name == "check_gemini_setup"
        )
        payload = setup_tool.model_dump(by_alias=True)
        self.assertEqual(
            payload["inputSchema"],
            {"type": "object", "properties": {}, "additionalProperties": False},
        )
        self.assertLessEqual(len(payload["description"]), 64)
        serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        self.assertLessEqual(len(serialized.encode("utf-8")), 512)

    def test_setup_failure_is_normal_structured_result(self) -> None:
        from mcp_server import server

        with tempfile.TemporaryDirectory() as temp_dir:
            missing_manifest = Path(temp_dir) / "missing-manifest.json"
            with patch.dict(
                os.environ,
                {"GEMINI_OFFLOAD_VERTEX_CREDENTIALS": str(missing_manifest)},
                clear=False,
            ):
                result = anyio.run(
                    server.handle_call_tool,
                    "check_gemini_setup",
                    {},
                )

        wire = result.model_dump(by_alias=True)
        self.assertFalse(wire["isError"])
        self.assertFalse(wire["structuredContent"]["ready"])
        self.assertEqual(wire["structuredContent"]["status"], "invalid")
        self.assertTrue(wire["structuredContent"]["next_action"])

    def test_mcp2_callback_preserves_wire_aliases(self) -> None:
        if hasattr(Server, "list_tools"):
            self.skipTest("MCP 1.x runtime uses decorator registration")

        from mcp_server import server

        async def scenario():
            listed = await server._v2_list_tools(None, None)
            called = await server._v2_call_tool(
                None,
                mcp_types.CallToolRequestParams(name="list_gemini_models", arguments={}),
            )
            return listed, called

        listed, called = anyio.run(scenario)
        self.assertEqual(len(listed.tools), 5)
        wire = called.model_dump(by_alias=True)
        self.assertIn("structuredContent", wire)
        self.assertIn("models", wire["structuredContent"])
        self.assertFalse(wire["isError"])


if __name__ == "__main__":
    unittest.main()
