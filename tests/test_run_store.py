from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from mcp_server.run_store import (
    InvalidStateTransition,
    LeaseFenceLost,
    RunLeaseConflict,
    RunStore,
    SCHEMA_VERSION,
)


class RunStoreTests(unittest.TestCase):
    def test_initializes_wal_schema_and_expected_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunStore(temp_dir)
            self.assertEqual(store.schema_version(), SCHEMA_VERSION)
            self.assertEqual(store.journal_mode(), "wal")

            with contextlib.closing(sqlite3.connect(store.db_path)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertTrue(
                {"schema_meta", "runs", "items", "artifacts", "events", "worker_leases"}
                <= tables
            )
    def test_status_snapshot_upserts_run_and_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunStore(temp_dir)
            status = {
                "run_id": "run-test",
                "lifecycle": "background",
                "status": "running",
                "created_at": "2026-08-21T00:00:00Z",
                "updated_at": "2026-08-21T00:00:01Z",
                "items": [
                    {
                        "id": "opaque/../id",
                        "index": 0,
                        "status": "completed",
                        "output_path": str(Path(temp_dir) / "run-test" / "outputs" / "item-000001.txt"),
                        "output_managed": True,
                    }
                ],
            }
            store.persist_status_snapshot(status)
            loaded = store.read_run_snapshot("run-test")
            self.assertEqual(loaded, status)

            with contextlib.closing(sqlite3.connect(store.db_path)) as connection:
                item = connection.execute(
                    "SELECT item_id, state, output_managed FROM items WHERE run_id = ?",
                    ("run-test",),
                ).fetchone()
            self.assertEqual(item, ("opaque/../id", "completed", 1))

    def test_events_are_ordered_and_cursorable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunStore(temp_dir)
            store.persist_status_snapshot(
                {
                    "run_id": "run-events",
                    "lifecycle": "background",
                    "status": "running",
                    "items": [],
                }
            )
            first = store.append_event(
                "run-events", {"timestamp": "2026-08-21T00:00:00Z", "type": "started"}
            )
            second = store.append_event(
                "run-events", {"timestamp": "2026-08-21T00:00:01Z", "type": "progress"}
            )
            self.assertGreater(second, first)
            self.assertEqual(
                [event["type"] for event in store.list_events("run-events", after_sequence=first)],
                ["progress"],
            )

    def test_active_lease_blocks_second_acquire_and_fences_old_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunStore(temp_dir)
            store.persist_status_snapshot(
                {"run_id": "run-lease", "lifecycle": "background", "status": "starting", "items": []}
            )
            generation = store.acquire_lease("run-lease", "token-a", owner_pid=101)
            self.assertEqual(generation, 1)
            self.assertTrue(store.lease_matches("run-lease", generation, "token-a"))
            with self.assertRaises(RunLeaseConflict):
                store.acquire_lease("run-lease", "token-b", owner_pid=202)

            self.assertTrue(store.release_lease("run-lease", generation, "token-a"))
            next_generation = store.acquire_lease("run-lease", "token-b", owner_pid=202)
            self.assertEqual(next_generation, 2)
            self.assertFalse(store.lease_matches("run-lease", generation, "token-a"))
            self.assertTrue(store.lease_matches("run-lease", next_generation, "token-b"))

    def test_expired_lease_can_be_reclaimed_with_higher_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunStore(temp_dir)
            store.persist_status_snapshot(
                {"run_id": "run-stale", "lifecycle": "background", "status": "running", "items": []}
            )
            first = store.acquire_lease("run-stale", "old-token", lease_seconds=-1)
            second = store.acquire_lease("run-stale", "new-token")
            self.assertEqual((first, second), (1, 2))
            self.assertFalse(store.lease_matches("run-stale", first, "old-token"))
            self.assertTrue(store.heartbeat_lease("run-stale", second, "new-token"))

    def test_concurrent_lease_acquire_has_single_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunStore(temp_dir)
            store.persist_status_snapshot(
                {"run_id": "run-race", "lifecycle": "background", "status": "starting", "items": []}
            )
            barrier = threading.Barrier(2)
            outcomes: list[str] = []

            def acquire(token: str) -> None:
                barrier.wait()
                try:
                    store.acquire_lease("run-race", token)
                    outcomes.append("acquired")
                except RunLeaseConflict:
                    outcomes.append("conflict")

            threads = [threading.Thread(target=acquire, args=(f"token-{index}",)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertCountEqual(outcomes, ["acquired", "conflict"])

    def test_rejects_illegal_run_and_item_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunStore(temp_dir)
            starting = {
                "run_id": "run-state",
                "lifecycle": "background",
                "status": "starting",
                "items": [{"id": "a", "index": 0, "status": "pending"}],
            }
            store.persist_status_snapshot(starting)
            illegal_run = {**starting, "status": "completed"}
            with self.assertRaises(InvalidStateTransition):
                store.persist_status_snapshot(illegal_run)
            illegal_item = {
                **starting,
                "status": "running",
                "items": [{"id": "a", "index": 0, "status": "completed"}],
            }
            with self.assertRaises(InvalidStateTransition):
                store.persist_status_snapshot(illegal_item)

    def test_artifact_metadata_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunStore(temp_dir)
            status = {
                "run_id": "run-artifact",
                "lifecycle": "background",
                "status": "starting",
                "items": [{"id": "a", "index": 0, "status": "pending"}],
            }
            store.persist_status_snapshot(status)
            artifacts = [{
                "role": "output",
                "path": str(Path(temp_dir) / "a.txt"),
                "byte_count": 3,
                "sha256": "abc",
                "managed": True,
                "created_at": "2026-08-21T00:00:00Z",
            }]
            store.replace_item_artifacts("run-artifact", "a", artifacts)
            self.assertEqual(store.list_item_artifacts("run-artifact", "a"), artifacts)

    def test_status_and_event_commit_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunStore(temp_dir)
            starting = {
                "run_id": "run-atomic",
                "lifecycle": "background",
                "status": "starting",
                "items": [{"id": "a", "index": 0, "status": "pending"}],
            }
            store.persist_status_snapshot(starting)
            running = {
                **starting,
                "status": "running",
                "items": [{"id": "a", "index": 0, "status": "running"}],
            }
            sequence = store.persist_status_and_event(
                running,
                {"timestamp": "2026-08-21T00:00:00Z", "event": "item_started"},
            )
            self.assertGreater(sequence, 0)
            self.assertEqual(store.read_run_snapshot("run-atomic")["status"], "running")
            self.assertEqual(store.list_events("run-atomic")[-1]["event"], "item_started")

    def test_fenced_mutation_rejects_revoked_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunStore(temp_dir)
            status = {
                "run_id": "run-fence",
                "lifecycle": "background",
                "status": "starting",
                "items": [{"id": "a", "index": 0, "status": "pending"}],
            }
            store.persist_status_snapshot(status)
            generation = store.acquire_lease("run-fence", "token-a")
            running = {
                **status,
                "status": "running",
                "items": [{"id": "a", "index": 0, "status": "running"}],
            }
            store.persist_status_snapshot(
                running,
                lease_generation=generation,
                lease_token="token-a",
            )
            self.assertTrue(store.revoke_lease("run-fence"))
            with self.assertRaises(LeaseFenceLost):
                store.persist_status_and_event(
                    running,
                    {"timestamp": "2026-08-21T00:00:00Z", "event": "late_publish"},
                    lease_generation=generation,
                    lease_token="token-a",
                )

    def test_fenced_artifact_publisher_does_not_run_after_revoke(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunStore(temp_dir)
            status = {
                "run_id": "run-publish",
                "lifecycle": "background",
                "status": "starting",
                "items": [{"id": "a", "index": 0, "status": "pending"}],
            }
            store.persist_status_snapshot(status)
            generation = store.acquire_lease("run-publish", "token-a")
            store.revoke_lease("run-publish")
            called = False

            def publisher():
                nonlocal called
                called = True
                return "value", []

            with self.assertRaises(LeaseFenceLost):
                store.publish_item_artifacts(
                    "run-publish", "a", generation, "token-a", publisher
                )
            self.assertFalse(called)

    def test_status_event_failure_rolls_back_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunStore(temp_dir)
            starting = {
                "run_id": "run-rollback",
                "lifecycle": "background",
                "status": "starting",
                "items": [{"id": "a", "index": 0, "status": "pending"}],
            }
            store.persist_status_snapshot(starting)
            running = {
                **starting,
                "status": "running",
                "items": [{"id": "a", "index": 0, "status": "running"}],
            }

            with self.assertRaisesRegex(ValueError, "event timestamp is required"):
                store.persist_status_and_event(running, {"event": "item_started"})

            self.assertEqual(store.read_run_snapshot("run-rollback"), starting)
            self.assertEqual(store.list_events("run-rollback"), [])

    def test_unknown_schema_version_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunStore(temp_dir)
            with contextlib.closing(sqlite3.connect(store.db_path)) as connection:
                connection.execute(
                    "UPDATE schema_meta SET value = '999' WHERE key = 'schema_version'"
                )
                connection.commit()

            with self.assertRaisesRegex(RuntimeError, "unsupported run store schema version"):
                RunStore(temp_dir)


if __name__ == "__main__":
    unittest.main()
