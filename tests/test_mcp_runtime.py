from __future__ import annotations

import unittest

import anyio
import mcp.types as mcp_types
from mcp.server import Server


class McpRuntimeCompatibilityTests(unittest.TestCase):
    def test_real_mcp_runtime_imports_and_lists_tools(self) -> None:
        from mcp_server import server

        names = [tool.name for tool in server._tool_definitions()]
        self.assertEqual(
            names,
            ["call_gemini", "manage_gemini_run", "list_gemini_models", "detect_mime"],
        )
        self.assertEqual(server.SERVER_VERSION, "0.2.0")

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
        self.assertEqual(len(listed.tools), 4)
        wire = called.model_dump(by_alias=True)
        self.assertIn("structuredContent", wire)
        self.assertIn("models", wire["structuredContent"])
        self.assertFalse(wire["isError"])


if __name__ == "__main__":
    unittest.main()
