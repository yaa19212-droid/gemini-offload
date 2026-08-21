from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio

from mcp_server import run_worker as run_worker_module
from mcp_server import worker as worker_module
from mcp_server.run_store import RunStore
from mcp_server.worker import run_owned_worker, run_worker_from_dir


class WorkerLifecycleTests(unittest.TestCase):
    def _store_with_lease(self, temp_dir: str) -> tuple[RunStore, int]:
        store = RunStore(temp_dir)
        store.persist_status_snapshot(
            {"run_id": "run-worker", "lifecycle": "background", "status": "starting", "items": []}
        )
        generation = store.acquire_lease("run-worker", "token")
        return store, generation

    def test_success_releases_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store, generation = self._store_with_lease(temp_dir)

            async def scenario() -> None:
                async def execute() -> None:
                    return None

                async def on_failure(_exc: BaseException) -> None:
                    self.fail("failure callback should not run")

                await run_owned_worker(
                    store,
                    "run-worker",
                    generation,
                    "token",
                    execute=execute,
                    on_failure=on_failure,
                    ownership_lost=RuntimeError,
                    heartbeat_interval=0.01,
                )

            anyio.run(scenario)
            self.assertFalse(store.lease_matches("run-worker", generation, "token"))

    def test_failure_callback_runs_before_lease_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store, generation = self._store_with_lease(temp_dir)
            observed: list[bool] = []

            async def scenario() -> None:
                async def execute() -> None:
                    raise RuntimeError("boom")

                async def on_failure(_exc: BaseException) -> None:
                    observed.append(store.lease_matches("run-worker", generation, "token"))

                with self.assertRaises(Exception):
                    await run_owned_worker(
                        store,
                        "run-worker",
                        generation,
                        "token",
                        execute=execute,
                        on_failure=on_failure,
                        ownership_lost=RuntimeError,
                    )

            anyio.run(scenario)
            self.assertEqual(observed, [True])
            self.assertFalse(store.lease_matches("run-worker", generation, "token"))

    def test_owned_bootstrap_failure_persists_terminal_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "run-bootstrap"
            run_dir.mkdir()
            store = RunStore(root)
            store.persist_status_snapshot(
                {
                    "run_id": "run-bootstrap",
                    "lifecycle": "background",
                    "status": "starting",
                    "items": [],
                }
            )
            generation = store.acquire_lease("run-bootstrap", "token", owner_pid=1)
            (run_dir / "plan.json").write_text(
                json.dumps(
                    {
                        "run_id": "run-wrong",
                        "run_dir": str(run_dir),
                        "lifecycle": "background",
                        "max_concurrency": 1,
                        "items": [],
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "locator.json").write_text(
                json.dumps(
                    {
                        "run_id": "run-bootstrap",
                        "run_token": "token",
                        "lease_generation": generation,
                        "pid": 1,
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"GEMINI_OFFLOAD_RUN_DIR": str(root)}, clear=False):
                with self.assertRaises(Exception):
                    anyio.run(
                        run_worker_from_dir,
                        str(run_dir),
                        "run-bootstrap",
                        "token",
                    )

            status = store.read_run_snapshot("run-bootstrap")
            self.assertEqual(status["status"], "failed")
            self.assertIn("run plan identity mismatch", status["worker_error"])
            self.assertEqual(store.list_events("run-bootstrap")[-1]["event"], "worker_failed")
            self.assertFalse(store.lease_matches("run-bootstrap", generation, "token"))

    def test_locator_write_failure_terminates_child_and_releases_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "run-locator-fail"
            run_dir.mkdir()
            store = RunStore(root)
            store.persist_status_snapshot(
                {
                    "run_id": "run-locator-fail",
                    "lifecycle": "background",
                    "status": "starting",
                    "items": [],
                }
            )

            class FakeProcess:
                pid = 4321

                def __init__(self) -> None:
                    self.terminated = False

                def terminate(self) -> None:
                    self.terminated = True

            class FakePsutilProcess:
                def create_time(self) -> float:
                    return 1.0

            process = FakeProcess()
            with (
                patch.dict(os.environ, {"GEMINI_OFFLOAD_RUN_DIR": str(root)}, clear=False),
                patch.object(worker_module.subprocess, "Popen", return_value=process),
                patch.object(worker_module.psutil, "Process", return_value=FakePsutilProcess()),
                patch.object(worker_module, "_write_json", side_effect=OSError("locator write failed")),
            ):
                with self.assertRaisesRegex(OSError, "locator write failed"):
                    worker_module.spawn_worker(run_dir, "run-locator-fail", "token")

            self.assertTrue(process.terminated)
            self.assertFalse(store.has_active_lease("run-locator-fail"))

    def test_entrypoint_fallback_persists_early_bootstrap_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = RunStore(root)
            store.persist_status_snapshot(
                {
                    "run_id": "run-entry-fail",
                    "lifecycle": "background",
                    "status": "starting",
                    "items": [],
                }
            )
            generation = store.acquire_lease("run-entry-fail", "token")

            with patch.dict(os.environ, {"GEMINI_OFFLOAD_RUN_DIR": str(root)}, clear=False):
                run_worker_module._record_bootstrap_failure(
                    "run-entry-fail",
                    "token",
                    ImportError("worker import failed"),
                )

            status = store.read_run_snapshot("run-entry-fail")
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["worker_error"], "ImportError: worker import failed")
            event = store.list_events("run-entry-fail")[-1]
            self.assertEqual(event["event"], "worker_failed")
            self.assertTrue(event["bootstrap_fallback"])
            self.assertFalse(store.lease_matches("run-entry-fail", generation, "token"))


if __name__ == "__main__":
    unittest.main()
