"""Print compact active gemini-offload run status for Codex hook experiments."""

from __future__ import annotations

import json
import os
import pathlib
import tempfile


TERMINAL_STATUSES = {"completed", "failed", "canceled", "stopped"}


def _run_root() -> pathlib.Path:
    configured = os.environ.get("GEMINI_OFFLOAD_RUN_DIR")
    if configured and configured.strip():
        return pathlib.Path(configured)
    return pathlib.Path(tempfile.gettempdir()) / "gemini-offload" / "runs"


def main() -> None:
    root = _run_root()
    if not root.exists():
        return

    active: list[str] = []
    for status_path in sorted(root.glob("run-*/status.json"), reverse=True):
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        run_status = status.get("status", "unknown")
        if run_status in TERMINAL_STATUSES:
            continue
        active.append(
            "Gemini background run "
            f"{status.get('run_id', status_path.parent.name)}: "
            f"{run_status}, {status.get('completed_count', 0)}/{status.get('item_count', '?')} items. "
            f"Use manage_gemini_run with run_dir={status_path.parent} for details."
        )
        if len(active) >= 5:
            break

    if active:
        print("\n".join(active))


if __name__ == "__main__":
    main()
