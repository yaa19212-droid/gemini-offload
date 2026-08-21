from __future__ import annotations

import contextlib
import datetime
import json
import pathlib
import sqlite3
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

SCHEMA_VERSION = 1
DEFAULT_DB_NAME = ".gemini-offload-runs.sqlite3"
T = TypeVar("T")


class RunLeaseConflict(RuntimeError):
    """Raised when an active worker lease already owns a run."""


class InvalidStateTransition(RuntimeError):
    """Raised when a persisted run or item state transition is illegal."""


class LeaseFenceLost(RuntimeError):
    """Raised when a worker mutation no longer owns the active lease fence."""


RUN_TRANSITIONS = {
    "queued": {"starting", "canceled"},
    "starting": {"running", "failed", "stopping", "canceling"},
    "running": {"completed", "failed", "stopping", "canceling"},
    "stopping": {"stopped", "failed", "canceling"},
    "canceling": {"canceled", "failed"},
    "completed": {"starting"},
    "failed": {"starting"},
    "stopped": {"starting"},
    "canceled": {"starting"},
}
ITEM_TRANSITIONS = {
    "pending": {"running", "stopped", "canceled"},
    "running": {"completed", "failed", "stopped", "canceled"},
    "completed": {"pending", "running"},
    "failed": {"pending", "running"},
    "stopped": {"pending", "running"},
    "canceled": {"pending", "running"},
}


class RunStore:
    """SQLite-backed durable metadata store for Gemini background runs."""

    def __init__(self, run_root: pathlib.Path | str, *, db_name: str = DEFAULT_DB_NAME) -> None:
        self.run_root = pathlib.Path(run_root).resolve(strict=False)
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.run_root / db_name
        self._initialize()

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    lifecycle TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT,
                    updated_at TEXT,
                    lease_generation INTEGER NOT NULL DEFAULT 0,
                    snapshot_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS items (
                    run_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    item_index INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    output_path TEXT,
                    output_managed INTEGER NOT NULL DEFAULT 0,
                    snapshot_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, item_id),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );
                """
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    path TEXT NOT NULL,
                    byte_count INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    managed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT,
                    UNIQUE (run_id, item_id, role, path),
                    FOREIGN KEY (run_id, item_id) REFERENCES items(run_id, item_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );
                """
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS worker_leases (
                    run_id TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL,
                    token TEXT NOT NULL,
                    owner_pid INTEGER,
                    owner_started_at TEXT,
                    heartbeat_at TEXT,
                    lease_expires_at TEXT,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_runs_state ON runs(state);
                CREATE INDEX IF NOT EXISTS idx_items_run_state ON items(run_id, state);
                CREATE INDEX IF NOT EXISTS idx_events_run_sequence ON events(run_id, sequence);
                """
            )
            version_row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if version_row is None:
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                try:
                    existing_version = int(version_row["value"])
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("run store schema version is invalid") from exc
                if existing_version != SCHEMA_VERSION:
                    raise RuntimeError(
                        "unsupported run store schema version: "
                        f"{existing_version} (expected {SCHEMA_VERSION})"
                    )

    @staticmethod
    def _json(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    def schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
        if row is None:
            raise RuntimeError("run store schema version is missing")
        return int(row["value"])

    def journal_mode(self) -> str:
        with self._connect() as connection:
            row = connection.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower()

    @staticmethod
    def _validate_transition(
        current: str | None,
        target: str,
        transitions: dict[str, set[str]],
        label: str,
    ) -> None:
        if target not in transitions:
            raise InvalidStateTransition(f"unknown {label} state: {target}")
        if current is None or current == target:
            return
        if target not in transitions.get(current, set()):
            raise InvalidStateTransition(f"illegal {label} transition: {current} -> {target}")

    def _validate_snapshot_transitions(
        self, connection: sqlite3.Connection, status: dict[str, Any]
    ) -> tuple[str, str, str]:
        run_id = status.get("run_id")
        lifecycle = status.get("lifecycle")
        state = status.get("status")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("status.run_id must be a non-empty string")
        if not isinstance(lifecycle, str) or not lifecycle:
            raise ValueError("status.lifecycle must be a non-empty string")
        if not isinstance(state, str) or not state:
            raise ValueError("status.status must be a non-empty string")
        current_run = connection.execute(
            "SELECT state FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        self._validate_transition(
            None if current_run is None else str(current_run["state"]),
            state,
            RUN_TRANSITIONS,
            "run",
        )
        for item in status.get("items", []):
            if not isinstance(item, dict):
                raise ValueError("status.items entries must be objects")
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                raise ValueError("status item id must be a non-empty string")
            item_state = str(item.get("status", "pending"))
            current_item = connection.execute(
                "SELECT state FROM items WHERE run_id = ? AND item_id = ?",
                (run_id, item_id),
            ).fetchone()
            self._validate_transition(
                None if current_item is None else str(current_item["state"]),
                item_state,
                ITEM_TRANSITIONS,
                f"item {item_id}",
            )
        return run_id, lifecycle, state

    def _persist_status_snapshot(
        self, connection: sqlite3.Connection, status: dict[str, Any]
    ) -> str:
        run_id, lifecycle, state = self._validate_snapshot_transitions(connection, status)
        connection.execute(
            """
            INSERT INTO runs(run_id, lifecycle, state, created_at, updated_at, snapshot_json)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                lifecycle = excluded.lifecycle,
                state = excluded.state,
                created_at = COALESCE(runs.created_at, excluded.created_at),
                updated_at = excluded.updated_at,
                snapshot_json = excluded.snapshot_json
            """,
            (
                run_id,
                lifecycle,
                state,
                status.get("created_at"),
                status.get("updated_at"),
                self._json(status),
            ),
        )
        for item in status.get("items", []):
            item_id = str(item["id"])
            connection.execute(
                """
                INSERT INTO items(
                    run_id, item_id, item_index, state, output_path, output_managed, snapshot_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, item_id) DO UPDATE SET
                    item_index = excluded.item_index,
                    state = excluded.state,
                    output_path = excluded.output_path,
                    output_managed = excluded.output_managed,
                    snapshot_json = excluded.snapshot_json
                """,
                (
                    run_id,
                    item_id,
                    int(item.get("index", 0)),
                    str(item.get("status", "pending")),
                    item.get("output_path"),
                    1 if item.get("output_managed") else 0,
                    self._json(item),
                ),
            )
        return run_id

    def _assert_lease_fence(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        generation: int | None,
        token: str | None,
    ) -> None:
        if generation is None and token is None:
            return
        if generation is None or not token:
            raise ValueError("lease generation and token must be provided together")
        row = connection.execute(
            "SELECT generation, token, lease_expires_at FROM worker_leases WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise LeaseFenceLost(f"worker lease fence no longer exists: {run_id}")
        expires_text = row["lease_expires_at"]
        if (
            int(row["generation"]) != generation
            or row["token"] != token
            or not isinstance(expires_text, str)
            or datetime.datetime.fromisoformat(expires_text) <= self._utc_now()
        ):
            raise LeaseFenceLost(f"worker lease fence no longer owns run: {run_id}")

    def persist_status_snapshot(
        self,
        status: dict[str, Any],
        *,
        lease_generation: int | None = None,
        lease_token: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease_fence(connection, str(status.get("run_id", "")), lease_generation, lease_token)
            self._persist_status_snapshot(connection, status)

    def persist_status_and_event(
        self,
        status: dict[str, Any],
        event: dict[str, Any],
        *,
        lease_generation: int | None = None,
        lease_token: str | None = None,
    ) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run_id = str(status.get("run_id", ""))
            self._assert_lease_fence(connection, run_id, lease_generation, lease_token)
            run_id = self._persist_status_snapshot(connection, status)
            return self._insert_event(connection, run_id, event)

    def read_run_snapshot(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return None if row is None else json.loads(row["snapshot_json"])

    def list_run_snapshots(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT snapshot_json FROM runs ORDER BY updated_at DESC, run_id DESC"
            ).fetchall()
        return [json.loads(row["snapshot_json"]) for row in rows]

    def _insert_event(
        self, connection: sqlite3.Connection, run_id: str, event: dict[str, Any]
    ) -> int:
        created_at = event.get("timestamp") or event.get("created_at")
        event_type = event.get("type") or event.get("event") or "event"
        if not isinstance(created_at, str) or not created_at:
            raise ValueError("event timestamp is required")
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("event type is required")
        cursor = connection.execute(
            "INSERT INTO events(run_id, created_at, event_type, payload_json) VALUES(?, ?, ?, ?)",
            (run_id, created_at, event_type, self._json(event)),
        )
        sequence = cursor.lastrowid
        if sequence is None:
            raise RuntimeError("event insert did not return a sequence")
        return int(sequence)

    def append_event(
        self,
        run_id: str,
        event: dict[str, Any],
        *,
        lease_generation: int | None = None,
        lease_token: str | None = None,
    ) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease_fence(connection, run_id, lease_generation, lease_token)
            return self._insert_event(connection, run_id, event)

    def list_events(self, run_id: str, *, after_sequence: int = 0) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sequence, payload_json FROM events
                WHERE run_id = ? AND sequence > ? ORDER BY sequence ASC
                """,
                (run_id, after_sequence),
            ).fetchall()
        return [{"sequence": int(row["sequence"]), **json.loads(row["payload_json"])} for row in rows]

    def _replace_item_artifacts(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        item_id: str,
        artifacts: list[dict[str, Any]],
    ) -> None:
        connection.execute(
            "DELETE FROM artifacts WHERE run_id = ? AND item_id = ?",
            (run_id, item_id),
        )
        for artifact in artifacts:
            connection.execute(
                """
                INSERT INTO artifacts(
                    run_id, item_id, role, path, byte_count, sha256, managed, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    item_id,
                    str(artifact["role"]),
                    str(artifact["path"]),
                    int(artifact["byte_count"]),
                    str(artifact["sha256"]),
                    1 if artifact.get("managed") else 0,
                    artifact.get("created_at"),
                ),
            )

    def replace_item_artifacts(
        self,
        run_id: str,
        item_id: str,
        artifacts: list[dict[str, Any]],
        *,
        lease_generation: int | None = None,
        lease_token: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease_fence(connection, run_id, lease_generation, lease_token)
            self._replace_item_artifacts(connection, run_id, item_id, artifacts)

    def publish_item_artifacts(
        self,
        run_id: str,
        item_id: str,
        generation: int,
        token: str,
        publisher: Callable[[], tuple[T, list[dict[str, Any]]]],
    ) -> T:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease_fence(connection, run_id, generation, token)
            value, artifacts = publisher()
            self._replace_item_artifacts(connection, run_id, item_id, artifacts)
            return value

    def list_item_artifacts(self, run_id: str, item_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT role, path, byte_count, sha256, managed, created_at
                FROM artifacts WHERE run_id = ? AND item_id = ?
                ORDER BY artifact_id ASC
                """,
                (run_id, item_id),
            ).fetchall()
        return [
            {
                "role": row["role"],
                "path": row["path"],
                "byte_count": int(row["byte_count"]),
                "sha256": row["sha256"],
                "managed": bool(row["managed"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def has_active_lease(self, run_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT lease_expires_at FROM worker_leases WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None or not isinstance(row["lease_expires_at"], str):
            return False
        return datetime.datetime.fromisoformat(row["lease_expires_at"]) > self._utc_now()

    def active_lease_generation(self, run_id: str, token: str) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT generation, lease_expires_at FROM worker_leases
                WHERE run_id = ? AND token = ?
                """,
                (run_id, token),
            ).fetchone()
        if row is None or not isinstance(row["lease_expires_at"], str):
            return None
        if datetime.datetime.fromisoformat(row["lease_expires_at"]) <= self._utc_now():
            return None
        return int(row["generation"])

    @staticmethod
    def _utc_now() -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc)

    @staticmethod
    def _iso(value: datetime.datetime) -> str:
        return value.astimezone(datetime.timezone.utc).isoformat()

    def acquire_lease(
        self,
        run_id: str,
        token: str,
        *,
        owner_pid: int | None = None,
        lease_seconds: float = 30.0,
    ) -> int:
        if not token:
            raise ValueError("lease token must be non-empty")
        now = self._utc_now()
        expires_at = now + datetime.timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT lease_generation FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError(f"run is not registered in store: {run_id}")
            lease = connection.execute(
                "SELECT generation, token, lease_expires_at FROM worker_leases WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if lease is not None:
                expires_text = lease["lease_expires_at"]
                if isinstance(expires_text, str) and expires_text:
                    expires = datetime.datetime.fromisoformat(expires_text)
                    if expires > now:
                        raise RunLeaseConflict(
                            f"run already has an active worker lease: {run_id}"
                        )
            generation = int(run["lease_generation"]) + 1
            connection.execute(
                "UPDATE runs SET lease_generation = ? WHERE run_id = ?",
                (generation, run_id),
            )
            connection.execute(
                """
                INSERT INTO worker_leases(
                    run_id, generation, token, owner_pid, owner_started_at,
                    heartbeat_at, lease_expires_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    generation = excluded.generation,
                    token = excluded.token,
                    owner_pid = excluded.owner_pid,
                    owner_started_at = excluded.owner_started_at,
                    heartbeat_at = excluded.heartbeat_at,
                    lease_expires_at = excluded.lease_expires_at
                """,
                (
                    run_id,
                    generation,
                    token,
                    owner_pid,
                    self._iso(now),
                    self._iso(now),
                    self._iso(expires_at),
                ),
            )
        return generation

    def bind_lease_owner(
        self, run_id: str, generation: int, token: str, owner_pid: int
    ) -> bool:
        now = self._iso(self._utc_now())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE worker_leases
                SET owner_pid = ?, owner_started_at = ?, heartbeat_at = ?
                WHERE run_id = ? AND generation = ? AND token = ?
                """,
                (owner_pid, now, now, run_id, generation, token),
            )
        return cursor.rowcount == 1

    def lease_matches(self, run_id: str, generation: int, token: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT lease_expires_at FROM worker_leases
                WHERE run_id = ? AND generation = ? AND token = ?
                """,
                (run_id, generation, token),
            ).fetchone()
        if row is None or not isinstance(row["lease_expires_at"], str):
            return False
        return datetime.datetime.fromisoformat(row["lease_expires_at"]) > self._utc_now()

    def heartbeat_lease(
        self,
        run_id: str,
        generation: int,
        token: str,
        *,
        lease_seconds: float = 30.0,
    ) -> bool:
        now = self._utc_now()
        expires_at = now + datetime.timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE worker_leases
                SET heartbeat_at = ?, lease_expires_at = ?
                WHERE run_id = ? AND generation = ? AND token = ?
                """,
                (self._iso(now), self._iso(expires_at), run_id, generation, token),
            )
        return cursor.rowcount == 1

    def release_lease(self, run_id: str, generation: int, token: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM worker_leases WHERE run_id = ? AND generation = ? AND token = ?",
                (run_id, generation, token),
            )
        return cursor.rowcount == 1

    def revoke_lease(self, run_id: str) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM worker_leases WHERE run_id = ?",
                (run_id,),
            )
        return cursor.rowcount == 1
