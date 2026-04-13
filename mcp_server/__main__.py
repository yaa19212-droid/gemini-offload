"""Module entrypoint for python -m mcp_server."""

from __future__ import annotations

import anyio

from .server import main


if __name__ == "__main__":
    anyio.run(main)
