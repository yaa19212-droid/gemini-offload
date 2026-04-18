from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

import mcp_server


class EntrypointTests(unittest.TestCase):
    def test_package_main_runs_server_with_anyio(self) -> None:
        fake_server = types.ModuleType("mcp_server.server")

        async def fake_main() -> None:
            return None

        fake_server.main = fake_main

        with patch.dict(sys.modules, {"mcp_server.server": fake_server}):
            with patch("mcp_server.anyio.run") as run_mock:
                mcp_server.main()

        run_mock.assert_called_once_with(fake_main)
