from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mcp_server.keys import _parse_keys_file


class ApiKeyFileTests(unittest.TestCase):
    def test_parse_keys_file_accepts_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api_keys.json"
            path.write_text('{"GOOGLE_API_KEY": " test-key "}', encoding="utf-8-sig")

            self.assertEqual(_parse_keys_file(path), {"GOOGLE_API_KEY": " test-key "})
