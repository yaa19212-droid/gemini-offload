from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import anyio

from mcp_server.artifacts import artifact_metadata
from mcp_server.run_service import RunService
from mcp_server.run_store import RunStore


class RunServiceTests(unittest.TestCase):
    def test_progress_is_cursorable_without_process_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunStore(temp_dir)
            status = {"run_id": "run-progress", "lifecycle": "background", "status": "running", "items": []}
            store.persist_status_snapshot(status)
            store.append_event("run-progress", {"timestamp": "t1", "event": "one"})
            store.append_event("run-progress", {"timestamp": "t2", "event": "two"})
            service = RunService(store, Path(temp_dir), now=lambda: "now")
            first = service.progress("run-progress", max_events=1)
            self.assertEqual([event["event"] for event in first["events"]], ["one"])
            self.assertTrue(first["events_truncated"])
            second = service.progress("run-progress", after_sequence=first["next_event_offset"])
            self.assertEqual([event["event"] for event in second["events"]], ["two"])

    def test_reconcile_stale_run_uses_injected_exports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "run-stale"
            run_dir.mkdir()
            store = RunStore(root)
            store.persist_status_snapshot(
                {"run_id": "run-stale", "lifecycle": "background", "status": "running", "items": []}
            )
            exported_statuses: list[str] = []
            exported_events: list[str] = []
            service = RunService(
                store,
                root,
                now=lambda: "2026-08-21T00:00:00+00:00",
                export_status=lambda _path, status: exported_statuses.append(status["status"]),
                export_event=lambda _path, event: exported_events.append(event["event"]),
            )
            self.assertEqual(service.reconcile_stale_runs(), ["run-stale"])
            self.assertEqual(store.read_run_snapshot("run-stale")["status"], "failed")
            self.assertEqual(exported_statuses, ["failed"])
            self.assertEqual(exported_events, ["run_recovered_failed"])

    def test_control_state_and_forced_cancel_are_process_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "run-control").mkdir()
            store = RunStore(root)
            status = {"run_id": "run-control", "lifecycle": "background", "status": "running", "items": []}
            store.persist_status_snapshot(status)
            generation = store.acquire_lease("run-control", "token")
            service = RunService(store, root, now=lambda: "2026-08-21T00:00:00+00:00")
            requested = service.request_control("run-control", "cancel")
            self.assertEqual(requested["status"], "canceling")
            canceled = service.finalize_forced_cancel("run-control")
            self.assertEqual(canceled["status"], "canceled")
            self.assertFalse(store.lease_matches("run-control", generation, "token"))
            self.assertEqual(
                [event["event"] for event in store.list_events("run-control")],
                ["cancel_requested", "run_canceled"],
            )

    def test_start_background_uses_injected_spawn_and_persists_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "run-start"
            store = RunStore(root)
            service = RunService(store, root, now=lambda: "2026-08-21T00:00:00+00:00")
            plan = {
                "run_id": "run-start",
                "run_dir": str(run_dir),
                "lifecycle": "background",
                "max_concurrency": 2,
                "items": [
                    {
                        "id": "a",
                        "index": 0,
                        "request": {"output_path": str(run_dir / "outputs" / "item-000001.txt"), "output_managed": True},
                    }
                ],
            }
            written: list[str] = []

            result = service.start_background(
                plan,
                write_plan=lambda path, _plan: written.append(str(path)),
                spawn_worker=lambda _path, _run_id, token: {"pid": 123, "run_token": token},
                token_factory=lambda: "token-a",
            )

            self.assertEqual(written, [str(run_dir)])
            self.assertEqual(result["locator"]["run_token"], "token-a")
            self.assertEqual(store.read_run_snapshot("run-start")["pid"], 123)
            self.assertEqual(store.list_events("run-start")[-1]["event"], "run_queued")

    def test_resume_background_clears_controls_and_requeues(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "run-resume"
            (run_dir / "control").mkdir(parents=True)
            (run_dir / "control" / "stop.json").write_text("{}", encoding="utf-8")
            store = RunStore(root)
            plan = {
                "run_id": "run-resume",
                "run_dir": str(run_dir),
                "lifecycle": "background",
                "max_concurrency": 1,
                "items": [],
            }
            store.persist_status_snapshot(
                {"run_id": "run-resume", "lifecycle": "background", "status": "failed", "items": []}
            )
            service = RunService(store, root, now=lambda: "2026-08-21T00:00:00+00:00")
            result = service.resume_background(
                plan,
                legacy_status=None,
                spawn_worker=lambda _path, _run_id, token: {"pid": 456, "run_token": token},
                token_factory=lambda: "token-b",
            )
            self.assertFalse((run_dir / "control" / "stop.json").exists())
            self.assertEqual(result["locator"]["pid"], 456)
            self.assertEqual(store.read_run_snapshot("run-resume")["status"], "starting")
            self.assertEqual(store.list_events("run-resume")[-1]["event"], "resume_queued")

    def test_spawn_failure_persists_terminal_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "run-spawn-fail"
            store = RunStore(root)
            service = RunService(store, root, now=lambda: "2026-08-21T00:00:00+00:00")
            plan = {
                "run_id": "run-spawn-fail",
                "run_dir": str(run_dir),
                "lifecycle": "background",
                "max_concurrency": 1,
                "items": [],
            }

            def fail_spawn(_path, _run_id, _token):
                raise OSError("spawn failed")

            with self.assertRaisesRegex(OSError, "spawn failed"):
                service.start_background(
                    plan,
                    write_plan=lambda _path, _plan: None,
                    spawn_worker=fail_spawn,
                    token_factory=lambda: "token-c",
                )
            status = store.read_run_snapshot("run-spawn-fail")
            self.assertEqual(status["status"], "failed")
            self.assertIn("spawn failed", status["worker_error"])
            self.assertEqual(store.list_events("run-spawn-fail")[-1]["event"], "worker_spawn_failed")

    def test_execute_plan_blocking_uses_injected_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RunService(
                RunStore(temp_dir),
                Path(temp_dir),
                now=lambda: "2026-08-21T00:00:00+00:00",
            )
            plan = {
                "run_id": "run-blocking",
                "run_dir": None,
                "lifecycle": "blocking",
                "max_concurrency": 2,
                "items": [
                    {"id": "a", "index": 0, "request": {"prompt": "one", "output_path": None}},
                    {"id": "b", "index": 1, "request": {"prompt": "two", "output_path": None}},
                ],
            }

            async def generate(request):
                return {"text": request["prompt"]}, False

            def apply_output(raw, _path, _expect_json):
                text = raw["text"]
                return {
                    "text": text,
                    "char_count": len(text),
                    "byte_count": len(text.encode("utf-8")),
                    "line_count": 1,
                    "image_count": 0,
                }

            async def scenario():
                return await service.execute_plan(
                    plan,
                    generate=generate,
                    apply_output=apply_output,
                    aggregate=lambda summary, _raw: summary,
                    ensure_owner=lambda *_args: None,
                    control_action=lambda _path: None,
                )

            result = anyio.run(scenario)
            self.assertEqual(result["ok_count"], 2)
            self.assertEqual([item["text"] for item in result["results"]], ["one", "two"])

    def test_tampered_completed_artifact_is_reexecuted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "run-recovery"
            output = run_dir / "outputs" / "item-000001.txt"
            output.parent.mkdir(parents=True)
            output.write_text("original", encoding="utf-8")
            store = RunStore(root)
            plan = {
                "run_id": "run-recovery",
                "run_dir": str(run_dir),
                "lifecycle": "background",
                "max_concurrency": 1,
                "items": [
                    {
                        "id": "a",
                        "index": 0,
                        "request": {
                            "output_path": str(output),
                            "output_managed": True,
                        },
                    }
                ],
            }
            completed = {
                "run_id": "run-recovery",
                "lifecycle": "background",
                "status": "completed",
                "items": [
                    {
                        "id": "a",
                        "index": 0,
                        "status": "completed",
                        "output_path": str(output),
                        "output_managed": True,
                    }
                ],
            }
            store.persist_status_snapshot(completed)
            store.replace_item_artifacts(
                "run-recovery",
                "a",
                [artifact_metadata(output, role="output", managed=True)],
            )
            starting = {**completed, "status": "starting"}
            store.persist_status_snapshot(starting)
            output.write_text("tampered", encoding="utf-8")
            generation = store.acquire_lease("run-recovery", "token")
            calls: list[str] = []
            service = RunService(
                store,
                root,
                now=lambda: "2026-08-21T00:00:00+00:00",
            )

            async def generate(_request):
                calls.append("generated")
                return {"text": "recovered"}, False

            def apply_output(raw, path, _expect_json):
                Path(path).write_text(raw["text"], encoding="utf-8")
                return {
                    "output_path": path,
                    "char_count": len(raw["text"]),
                    "byte_count": len(raw["text"].encode("utf-8")),
                    "line_count": 1,
                    "image_count": 0,
                }

            async def scenario():
                return await service.execute_plan(
                    plan,
                    run_dir=run_dir,
                    background=True,
                    worker_token="token",
                    worker_generation=generation,
                    generate=generate,
                    apply_output=apply_output,
                    aggregate=lambda summary, _raw: summary,
                    ensure_owner=lambda *_args: None,
                    control_action=lambda _path: None,
                )

            result = anyio.run(scenario)
            self.assertEqual(calls, ["generated"])
            self.assertEqual(output.read_text(encoding="utf-8"), "recovered")
            self.assertEqual(result["ok_count"], 1)
            self.assertEqual(store.read_run_snapshot("run-recovery")["status"], "completed")
            self.assertIn(
                "item_recovery_required",
                [event["event"] for event in store.list_events("run-recovery")],
            )


if __name__ == "__main__":
    unittest.main()
