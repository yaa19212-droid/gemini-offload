"""Child-process entrypoint for background Gemini runs."""

from __future__ import annotations

import argparse
import datetime
import os
import pathlib
import tempfile
from typing import Any

import anyio

from .run_store import RunStore


def _run_root() -> pathlib.Path:
    configured = os.environ.get("GEMINI_OFFLOAD_RUN_DIR")
    if configured and configured.strip():
        root = pathlib.Path(configured)
        if not root.is_absolute():
            raise ValueError("GEMINI_OFFLOAD_RUN_DIR must be absolute")
    else:
        root = pathlib.Path(tempfile.gettempdir()) / "gemini-offload" / "runs"
    return root.resolve(strict=False)


def _root_failure(exc: BaseException) -> BaseException:
    current = exc
    while True:
        nested = getattr(current, "exceptions", None)
        if not isinstance(nested, (tuple, list)) or len(nested) != 1:
            return current
        child = nested[0]
        if not isinstance(child, BaseException):
            return current
        current = child


def _record_bootstrap_failure(
    run_id: str,
    run_token: str,
    exc: BaseException,
) -> None:
    try:
        store = RunStore(_run_root())
        generation = store.active_lease_generation(run_id, run_token)
    except Exception:
        return
    if generation is None:
        return
    root = _root_failure(exc)
    try:
        status = store.read_run_snapshot(run_id)
        if isinstance(status, dict) and status.get("status") in {"starting", "running"}:
            status["status"] = "failed"
            status["worker_error"] = f"{type(root).__name__}: {root}"
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            event: dict[str, Any] = {
                "timestamp": now,
                "source": "gemini-offload",
                "run_id": run_id,
                "event": "worker_failed",
                "error_type": type(root).__name__,
                "message": str(root),
                "bootstrap_fallback": True,
            }
            store.persist_status_and_event(
                status,
                event,
                lease_generation=generation,
                lease_token=run_token,
            )
    finally:
        store.release_lease(run_id, generation, run_token)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a persisted gemini-offload background run."
    )
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-token", required=True)
    args = parser.parse_args()

    try:
        from .worker import run_worker_from_dir

        anyio.run(run_worker_from_dir, args.run_dir, args.run_id, args.run_token)
    except BaseException as exc:
        _record_bootstrap_failure(args.run_id, args.run_token, exc)
        raise


if __name__ == "__main__":
    main()
