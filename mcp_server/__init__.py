"""Standalone MCP server for Gemini subtask offload."""

import anyio

__all__ = ["main"]


def main() -> None:
    from .server import main as _main
    anyio.run(_main)
