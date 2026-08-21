"""Print compact active gemini-offload run status for Codex hook experiments."""

from __future__ import annotations

import os
import pathlib
import tempfile

from mcp_server.run_store import DEFAULT_DB_NAME, RunStore


TERMINAL_STATUSES = {"completed", "failed", "canceled", "stopped"}


def _run_root() -> pathlib.Path:
    configured = os.environ.get("GEMINI_OFFLOAD_RUN_DIR")
    if configured and configured.strip():
        return pathlib.Path(configured).resolve()
    return (pathlib.Path(tempfile.gettempdir()) / "gemini-offload" / "runs").resolve()


def main() -> None:
    root = _run_root()
    if not (root / DEFAULT_DB_NAME).exists():
        return

    active: list[str] = []
    for status in RunStore(root).list_run_snapshots():
        run_status = status.get("status", "unknown")
        if run_status in TERMINAL_STATUSES:
            continue
        run_id = status.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        active.append(
            "Gemini background run "
            f"{run_id}: {run_status}, "
            f"{status.get('completed_count', 0)}/{status.get('item_count', '?')} items. "
            f"Use manage_gemini_run with run_dir={root / run_id} for details."
        )
        if len(active) >= 5:
            break

    if active:
        print("\n".join(active))


if __name__ == "__main__":
    main()
