"""Child-process entrypoint for background Gemini runs."""

from __future__ import annotations

import argparse

import anyio

from .server import run_worker_from_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a persisted gemini-offload background run.")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-token", required=True)
    args = parser.parse_args()

    anyio.run(run_worker_from_dir, args.run_dir, args.run_id, args.run_token)


if __name__ == "__main__":
    main()
