from __future__ import annotations

import unittest

from mcp_server.response_projection import (
    project_blocking_run,
    project_usage,
)


class ResponseProjectionTests(unittest.TestCase):
    def test_usage_normalizes_sdk_fields_and_omits_noise(self) -> None:
        usage = {
            "prompt_token_count": 30,
            "candidates_token_count": 6,
            "thoughts_token_count": 88,
            "total_token_count": 124,
            "traffic_type": "ON_DEMAND",
            "cache_tokens_details": None,
            "prompt_tokens_details": [{"modality": "TEXT", "token_count": 30}],
        }

        self.assertEqual(
            project_usage(usage),
            {
                "input_tokens": 30,
                "output_tokens": 6,
                "thinking_tokens": 88,
                "total_tokens": 124,
            },
        )
    def test_single_blocking_run_drops_internal_envelope_noise(self) -> None:
        projected = project_blocking_run(
            {
                "run_id": "run-test",
                "lifecycle": "blocking",
                "item_count": 1,
                "ok_count": 1,
                "error_count": 0,
                "max_concurrency": 1,
                "results": [
                    {
                        "index": 0,
                        "id": "smoke-test",
                        "ok": True,
                        "model": "gemini-3.7-flash",
                        "usage": {"prompt_token_count": 3, "total_token_count": 4},
                        "elapsed_ms": 12,
                        "char_count": 2,
                        "byte_count": 2,
                        "line_count": 1,
                        "image_count": 0,
                        "text": "ok",
                        "truncated": False,
                    }
                ],
            }
        )

        self.assertEqual(set(projected), {"results"})
        result = projected["results"][0]
        self.assertEqual(result["id"], "smoke-test")
        self.assertEqual(result["usage"], {"input_tokens": 3, "total_tokens": 4})
        for field in ("index", "elapsed_ms", "char_count", "byte_count", "line_count", "image_count", "truncated"):
            self.assertNotIn(field, result)


if __name__ == "__main__":
    unittest.main()