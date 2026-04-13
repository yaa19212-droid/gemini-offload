"""Standalone MCP server for Gemini subtask offload."""

__all__ = ["main"]


def main():
    from .server import main as _main
    return _main()
