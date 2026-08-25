from __future__ import annotations

import datetime
import json
import pathlib
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import anyio

from .artifacts import collect_item_artifacts, managed_output_path, verify_recorded_artifacts
from .gemini_client import (
    DEFAULT_MODEL_NAME,
    DEFAULT_RATE_LIMIT_MAX_WAIT_SECONDS,
    GeminiRateLimitError,
    RATE_LIMIT_MODE_FAIL_FAST,
    detect_mime_type,
    generate_request,
    is_supported_mime,
    normalize_media_resolution_override,
    normalize_media_resolution_policy,
    validate_media_resolution_for_mime,
)
from .keys import get_vertex_credential_count
from .run_store import LeaseFenceLost, RunLeaseConflict, RunStore


class WorkerOwnershipLost(RuntimeError):
    """Raised when a background worker loses its active run fence."""


def _root_failure(exc: BaseException) -> BaseException:
    """Unwrap single-cause task-group failures for durable diagnostics."""
    current = exc
    while True:
        nested = getattr(current, "exceptions", None)
        if not isinstance(nested, (tuple, list)) or len(nested) != 1:
            return current
        child = nested[0]
        if not isinstance(child, BaseException):
            return current
        current = child


async def generate_raw_from_request(request: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    rate_limit = request["rate_limit"]
    tools = request["tools"]
    result = await anyio.to_thread.run_sync(
        lambda: generate_request(
            request["contents"],
            request.get("system_prompt"),
            request.get("model", DEFAULT_MODEL_NAME),
            request.get("include_thinking", False),
            rate_limit.get("mode", RATE_LIMIT_MODE_FAIL_FAST),
            rate_limit.get("fallback_models"),
            rate_limit.get("max_wait_seconds"),
            tools.get("google_search", False),
            request.get("response_json_schema"),
            request.get("media_resolution"),
        )
    )
    return result, request.get("expect_json_response") is True


def classify_gemini_error(exc: Exception) -> dict[str, Any] | None:
    if isinstance(exc, GeminiRateLimitError):
        return {
            "result": exc.to_dict(),
            "message": exc.message,
            "error_type": "vertex_rate_limited",
        }
    return None


class RunService:
    def __init__(
        self,
        store: RunStore,
        run_root: pathlib.Path,
        *,
        now: Callable[[], str],
        export_status: Callable[[pathlib.Path, dict[str, Any]], None] | None = None,
        export_event: Callable[[pathlib.Path, dict[str, Any]], None] | None = None,
    ) -> None:
        self.store = store
        self.run_root = run_root
        self.now = now
        self.export_status = export_status
        self.export_event = export_event

    def list_runs(self) -> list[dict[str, Any]]:
        return self.store.list_run_snapshots()

    def append_event(
        self,
        run_id: str,
        event_name: str,
        *,
        lease_generation: int | None = None,
        lease_token: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        event = {
            "timestamp": self.now(),
            "source": "gemini-offload",
            "run_id": run_id,
            "event": event_name,
            **extra,
        }
        try:
            self.store.append_event(
                run_id,
                event,
                lease_generation=lease_generation,
                lease_token=lease_token,
            )
        except LeaseFenceLost as exc:
            raise WorkerOwnershipLost(str(exc)) from exc
        run_dir = self.run_root / run_id
        if run_dir.exists() and self.export_event is not None:
            self.export_event(run_dir, event)
        return event

    def persist_worker_failure(
        self,
        run_id: str,
        generation: int,
        token: str,
        exc: BaseException,
    ) -> None:
        status = self.store.read_run_snapshot(run_id)
        if not isinstance(status, dict) or status.get("status") not in {"starting", "running"}:
            return
        root = _root_failure(exc)
        status["status"] = "failed"
        status["worker_error"] = f"{type(root).__name__}: {root}"
        self._persist_status_event(
            run_id,
            status,
            "worker_failed",
            lease_generation=generation,
            lease_token=token,
            error_type=type(root).__name__,
            message=str(root),
        )

    def status(self, run_id: str) -> dict[str, Any] | None:
        return self.store.read_run_snapshot(run_id)

    def progress(
        self, run_id: str, *, after_sequence: int = 0, max_events: int = 50
    ) -> dict[str, Any]:
        stored = self.store.list_events(run_id, after_sequence=after_sequence)
        events = stored[:max_events]
        return {
            "events": events,
            "event_offset": after_sequence,
            "next_event_offset": events[-1]["sequence"] if events else after_sequence,
            "events_truncated": len(stored) > len(events),
        }

    def initial_status(self, plan: dict[str, Any], state: str) -> dict[str, Any]:
        return {
            "run_id": plan["run_id"],
            "lifecycle": plan["lifecycle"],
            "status": state,
            "created_at": self.now(),
            "updated_at": self.now(),
            "item_count": len(plan["items"]),
            "completed_count": 0,
            "ok_count": 0,
            "error_count": 0,
            "max_concurrency": plan["max_concurrency"],
            "items": [
                {
                    "id": item["id"],
                    "index": item["index"],
                    "status": "pending",
                    "output_path": item["request"].get("output_path"),
                    "output_managed": item["request"].get("output_managed", False),
                }
                for item in plan["items"]
            ],
        }

    def _persist_status(
        self,
        run_id: str,
        status: dict[str, Any],
        *,
        lease_generation: int | None = None,
        lease_token: str | None = None,
    ) -> None:
        status["updated_at"] = self.now()
        try:
            self.store.persist_status_snapshot(
                status,
                lease_generation=lease_generation,
                lease_token=lease_token,
            )
        except LeaseFenceLost as exc:
            raise WorkerOwnershipLost(str(exc)) from exc
        run_dir = self.run_root / run_id
        if run_dir.exists() and self.export_status is not None:
            self.export_status(run_dir, status)

    def _persist_status_event(
        self,
        run_id: str,
        status: dict[str, Any],
        event_name: str,
        *,
        lease_generation: int | None = None,
        lease_token: str | None = None,
        **extra: Any,
    ) -> None:
        status["updated_at"] = self.now()
        event = {
            "timestamp": self.now(),
            "source": "gemini-offload",
            "run_id": run_id,
            "event": event_name,
            **extra,
        }
        try:
            self.store.persist_status_and_event(
                status,
                event,
                lease_generation=lease_generation,
                lease_token=lease_token,
            )
        except LeaseFenceLost as exc:
            raise WorkerOwnershipLost(str(exc)) from exc
        self._export(run_id, status, event)

    def _background_run_dir(self, plan: dict[str, Any]) -> tuple[str, pathlib.Path]:
        run_id = plan.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("background plan requires run_id")
        if plan.get("lifecycle") != "background":
            raise ValueError("background plan requires background lifecycle")
        run_dir = pathlib.Path(str(plan.get("run_dir", ""))).resolve(strict=False)
        expected = (self.run_root / run_id).resolve(strict=False)
        if run_dir != expected:
            raise ValueError("background plan run_dir does not match run root/run_id")
        return run_id, run_dir

    def _spawn_token(self, token_factory: Callable[[], str] | None) -> str:
        return token_factory() if token_factory is not None else uuid.uuid4().hex

    def _record_spawn_failure(self, run_id: str, exc: BaseException) -> None:
        if self.store.has_active_lease(run_id):
            return
        status = self.store.read_run_snapshot(run_id)
        if not isinstance(status, dict) or status.get("status") not in {"starting", "running"}:
            return
        status["status"] = "failed"
        status["worker_error"] = f"{type(exc).__name__}: {exc}"
        self._persist_status_event(
            run_id,
            status,
            "worker_spawn_failed",
            error_type=type(exc).__name__,
            message=str(exc),
        )

    def start_background(
        self,
        plan: dict[str, Any],
        *,
        write_plan: Callable[[pathlib.Path, dict[str, Any]], None],
        spawn_worker: Callable[[pathlib.Path, str, str], dict[str, Any]],
        token_factory: Callable[[], str] | None = None,
    ) -> dict[str, Any]:
        run_id, run_dir = self._background_run_dir(plan)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
        (run_dir / "control").mkdir(parents=True, exist_ok=True)
        write_plan(run_dir, plan)
        status = self.initial_status(plan, "starting")
        self._persist_status_event(run_id, status, "run_queued")
        token = self._spawn_token(token_factory)
        try:
            locator = spawn_worker(run_dir, run_id, token)
        except RunLeaseConflict:
            raise
        except BaseException as exc:
            self._record_spawn_failure(run_id, exc)
            raise
        current = self.store.read_run_snapshot(run_id) or status
        current["pid"] = locator["pid"]
        self._persist_status(run_id, current)
        return {"run_dir": run_dir, "status": current, "locator": locator}

    def resume_background(
        self,
        plan: dict[str, Any],
        *,
        legacy_status: dict[str, Any] | None,
        spawn_worker: Callable[[pathlib.Path, str, str], dict[str, Any]],
        token_factory: Callable[[], str] | None = None,
    ) -> dict[str, Any]:
        run_id, run_dir = self._background_run_dir(plan)
        for control_name in ("stop.json", "cancel.json"):
            control_path = run_dir / "control" / control_name
            if control_path.exists():
                control_path.unlink()
        status = self.store.read_run_snapshot(run_id)
        if status is None:
            status = legacy_status or self.initial_status(plan, "starting")
            self.store.persist_status_snapshot(status)
        self.reconcile_stale_runs()
        status = self.store.read_run_snapshot(run_id) or status
        status["status"] = "starting"
        status.pop("pid", None)
        self._persist_status_event(run_id, status, "resume_queued")
        token = self._spawn_token(token_factory)
        try:
            locator = spawn_worker(run_dir, run_id, token)
        except RunLeaseConflict:
            raise
        except BaseException as exc:
            self._record_spawn_failure(run_id, exc)
            raise
        current = self.store.read_run_snapshot(run_id) or status
        current["pid"] = locator["pid"]
        self._persist_status(run_id, current)
        return {"run_dir": run_dir, "status": current, "locator": locator}

    def reconcile_stale_runs(self, *, exclude_run_id: str | None = None) -> list[str]:
        reconciled: list[str] = []
        for status in self.store.list_run_snapshots():
            run_id = status.get("run_id")
            if not isinstance(run_id, str) or run_id == exclude_run_id:
                continue
            current_state = status.get("status")
            if current_state not in {"starting", "running", "stopping", "canceling"}:
                continue
            if self.store.has_active_lease(run_id):
                continue
            if current_state == "stopping":
                recovered_state = "stopped"
                event_name = "run_recovered_stopped"
            elif current_state == "canceling":
                recovered_state = "canceled"
                event_name = "run_recovered_canceled"
            else:
                recovered_state = "failed"
                event_name = "run_recovered_failed"
                status["worker_error"] = "stale run recovered without an active worker lease"
            status["status"] = recovered_state
            status["recovered_at"] = self.now()
            event = {
                "timestamp": self.now(),
                "source": "gemini-offload",
                "run_id": run_id,
                "event": event_name,
                "reason": "missing_or_expired_worker_lease",
            }
            self.store.persist_status_and_event(status, event)
            run_dir = self.run_root / run_id
            if run_dir.exists() and self.export_status is not None:
                self.export_status(run_dir, status)
            if run_dir.exists() and self.export_event is not None:
                self.export_event(run_dir, event)
            reconciled.append(run_id)
        return reconciled

    def _export(self, run_id: str, status: dict[str, Any], event: dict[str, Any]) -> None:
        run_dir = self.run_root / run_id
        if not run_dir.exists():
            return
        if self.export_status is not None:
            self.export_status(run_dir, status)
        if self.export_event is not None:
            self.export_event(run_dir, event)

    def request_control(self, run_id: str, action: str) -> dict[str, Any] | None:
        if action not in {"stop", "cancel"}:
            raise ValueError("control action must be stop or cancel")
        status = self.store.read_run_snapshot(run_id)
        if not isinstance(status, dict):
            return None
        target = "canceling" if action == "cancel" else "stopping"
        sources = {"starting", "running"}
        if action == "cancel":
            sources.add("stopping")
        if status.get("status") not in sources:
            return status
        status["status"] = target
        event = {
            "timestamp": self.now(),
            "source": "gemini-offload",
            "run_id": run_id,
            "event": f"{action}_requested",
        }
        self.store.persist_status_and_event(status, event)
        self._export(run_id, status, event)
        return status

    def finalize_forced_cancel(self, run_id: str) -> dict[str, Any] | None:
        self.store.revoke_lease(run_id)
        status = self.store.read_run_snapshot(run_id)
        if not isinstance(status, dict):
            return None
        if status.get("status") != "canceling":
            status["status"] = "canceling"
            requested = {
                "timestamp": self.now(),
                "source": "gemini-offload",
                "run_id": run_id,
                "event": "cancel_requested",
                "forced": True,
            }
            self.store.persist_status_and_event(status, requested)
            self._export(run_id, status, requested)
        status["status"] = "canceled"
        event = {
            "timestamp": self.now(),
            "source": "gemini-offload",
            "run_id": run_id,
            "event": "run_canceled",
            "forced": True,
        }
        self.store.persist_status_and_event(status, event)
        self._export(run_id, status, event)
        return status


    async def execute_plan(
        self,
        plan: dict[str, Any],
        *,
        generate: Callable[[dict[str, Any]], Awaitable[tuple[dict[str, Any], bool]]],
        apply_output: Callable[[dict[str, Any], Any, bool], dict[str, Any]],
        aggregate: Callable[[dict[str, Any], list[dict[str, Any] | None]], dict[str, Any]],
        ensure_owner: Callable[[pathlib.Path | None, str, str | None, int | None], None],
        control_action: Callable[[pathlib.Path], str | None],
        classify_error: Callable[[Exception], dict[str, Any] | None] | None = None,
        run_dir: pathlib.Path | None = None,
        background: bool = False,
        worker_token: str | None = None,
        worker_generation: int | None = None,
    ) -> dict[str, Any]:
        run_id = str(plan["run_id"])
        max_concurrency = int(plan["max_concurrency"])
        limiter = anyio.Semaphore(max_concurrency)
        results: list[dict[str, Any] | None] = [None] * len(plan["items"])
        raw_successes: list[dict[str, Any] | None] | None = (
            None if background else [None] * len(plan["items"])
        )
        status_lock = anyio.Lock()
        status_data = self.initial_status(plan, "running") if background and run_dir is not None else None
        if status_data is not None:
            ensure_owner(run_dir, run_id, worker_token, worker_generation)
            existing = self.store.read_run_snapshot(run_id)
            if isinstance(existing, dict):
                if isinstance(existing.get("items"), list):
                    status_data["items"] = existing["items"]
                if existing.get("status") in {"stopping", "canceling"}:
                    status_data["status"] = existing["status"]
                else:
                    status_data["status"] = "running"
            self._persist_status_event(
                run_id,
                status_data,
                "run_started",
                lease_generation=worker_generation,
                lease_token=worker_token,
            )

        async def update_item_status(
            index: int,
            item_state: str,
            extra: dict[str, Any] | None = None,
            event_name: str | None = None,
            event_extra: dict[str, Any] | None = None,
        ) -> None:
            if status_data is None or run_dir is None:
                return
            async with status_lock:
                ensure_owner(run_dir, run_id, worker_token, worker_generation)
                durable = self.store.read_run_snapshot(run_id)
                if isinstance(durable, dict) and durable.get("status") in {"stopping", "canceling"}:
                    status_data["status"] = durable["status"]
                item_entry = status_data["items"][index]
                item_entry["status"] = item_state
                if extra:
                    item_entry.update(extra)
                terminal = {"completed", "failed", "stopped", "canceled"}
                status_data["completed_count"] = sum(
                    1 for item in status_data["items"] if item.get("status") in terminal
                )
                status_data["ok_count"] = sum(
                    1 for item in status_data["items"] if item.get("status") == "completed"
                )
                status_data["error_count"] = sum(
                    1 for item in status_data["items"] if item.get("status") == "failed"
                )
                if event_name is None:
                    self._persist_status(
                        run_id,
                        status_data,
                        lease_generation=worker_generation,
                        lease_token=worker_token,
                    )
                else:
                    self._persist_status_event(
                        run_id,
                        status_data,
                        event_name,
                        lease_generation=worker_generation,
                        lease_token=worker_token,
                        **(event_extra or {}),
                    )

        async def run_item(index: int, item: dict[str, Any]) -> None:
            if status_data is not None and status_data["items"][index].get("status") == "completed":
                artifacts = self.store.list_item_artifacts(run_id, item["id"])
                verified, reason = await anyio.to_thread.run_sync(
                    verify_recorded_artifacts,
                    run_dir,
                    artifacts,
                )
                if verified:
                    results[index] = {
                        "index": index,
                        "id": item["id"],
                        "ok": True,
                        "status": "completed",
                        "skipped": True,
                        "output_path": status_data["items"][index].get("output_path"),
                    }
                    return
                await update_item_status(
                    index,
                    "pending",
                    {"recovery_reason": reason, "recovered_at": self.now()},
                    "item_recovery_required",
                    {"item_id": item["id"], "reason": reason},
                )

            async with limiter:
                ensure_owner(run_dir, run_id, worker_token, worker_generation)
                if run_dir is not None:
                    action = control_action(run_dir)
                    if action in {"stop", "cancel"}:
                        terminal_state = "canceled" if action == "cancel" else "stopped"
                        results[index] = {
                            "index": index,
                            "id": item["id"],
                            "ok": False,
                            "skipped": True,
                            "status": terminal_state,
                        }
                        await update_item_status(
                            index,
                            terminal_state,
                            event_name=f"item_{terminal_state}",
                            event_extra={"item_id": item["id"]},
                        )
                        return
                await update_item_status(
                    index,
                    "running",
                    {"started_at": self.now()},
                    "item_started",
                    {"item_id": item["id"]},
                )
                try:
                    raw_result, expect_json = await generate(item["request"])
                    ensure_owner(run_dir, run_id, worker_token, worker_generation)
                    if raw_successes is not None:
                        raw_successes[index] = {
                            "result": raw_result,
                            "output_path": item["request"].get("output_path"),
                            "expect_json_response": expect_json,
                        }
                    if run_dir is not None:
                        if worker_generation is None or worker_token is None:
                            raise WorkerOwnershipLost(
                                "background artifact publication requires an active worker fence"
                            )

                        def publish() -> tuple[dict[str, Any], list[dict[str, Any]]]:
                            published = apply_output(
                                dict(raw_result),
                                item["request"].get("output_path"),
                                expect_json,
                            )
                            return published, collect_item_artifacts(item, published)
                        try:
                            item_result = self.store.publish_item_artifacts(
                                run_id,
                                item["id"],
                                worker_generation,
                                worker_token,
                                publish,
                            )
                        except LeaseFenceLost as exc:
                            raise WorkerOwnershipLost(str(exc)) from exc
                    else:
                        item_result = apply_output(
                            dict(raw_result),
                            item["request"].get("output_path"),
                            expect_json,
                        )
                    item_result["index"] = index
                    item_result["id"] = item["id"]
                    item_result["ok"] = True
                    if background:
                        results[index] = {
                            key: item_result[key]
                            for key in (
                                "index", "id", "ok", "output_path", "char_count",
                                "byte_count", "line_count", "image_count",
                            )
                            if key in item_result
                        }
                    else:
                        results[index] = item_result
                    await update_item_status(
                        index,
                        "completed",
                        {
                            "completed_at": self.now(),
                            "output_path": item_result.get(
                                "output_path", item["request"].get("output_path")
                            ),
                        },
                        "item_completed",
                        {
                            "item_id": item["id"],
                            "output_path": item_result.get("output_path"),
                        },
                    )
                except WorkerOwnershipLost:
                    raise
                except Exception as exc:
                    classified = classify_error(exc) if classify_error is not None else None
                    if classified is not None:
                        message = str(classified.get("message", exc))
                        error_type = str(classified.get("error_type", type(exc).__name__))
                        error_result = dict(classified.get("result") or {})
                    else:
                        message = f"{type(exc).__name__}: {exc}"
                        error_type = type(exc).__name__
                        error_result = {}
                    error_result.update(
                        {"index": index, "id": item["id"], "ok": False, "error": message}
                    )
                    results[index] = error_result
                    await update_item_status(
                        index,
                        "failed",
                        {"error": message, "error_type": error_type},
                        "item_failed",
                        {
                            "item_id": item["id"],
                            "error_type": error_type,
                            "message": str(classified.get("message", exc)) if classified else str(exc),
                        },
                    )

        async with anyio.create_task_group() as task_group:
            for index, item in enumerate(plan["items"]):
                task_group.start_soon(run_item, index, item)

        finalized = [result for result in results if result is not None]
        ok_count = sum(1 for result in finalized if result.get("ok") is True)
        error_count = len(finalized) - ok_count
        run_state = "completed" if error_count == 0 else "failed"
        if run_dir is not None:
            ensure_owner(run_dir, run_id, worker_token, worker_generation)
            action = control_action(run_dir)
            if action == "cancel":
                run_state = "canceled"
            elif action == "stop":
                run_state = "stopped"
            if status_data is not None:
                ensure_owner(run_dir, run_id, worker_token, worker_generation)
                status_data["status"] = run_state
                status_data["completed_count"] = len(finalized)
                status_data["ok_count"] = ok_count
                status_data["error_count"] = error_count
                self._persist_status_event(
                    run_id,
                    status_data,
                    f"run_{run_state}",
                    lease_generation=worker_generation,
                    lease_token=worker_token,
                )

        summary = {
            "run_id": run_id,
            "lifecycle": plan["lifecycle"],
            "item_count": len(plan["items"]),
            "ok_count": ok_count,
            "error_count": error_count,
            "max_concurrency": max_concurrency,
            "results": finalized,
        }
        if background:
            return summary
        return aggregate(
            summary,
            raw_successes or [None] * len(plan["items"]),
        )


# Run-plan normalization/materialization domain logic.
MAX_BATCH_CONCURRENCY = 32
MAX_ITEM_ID_LENGTH = 256
PLACEHOLDER_RE = re.compile(r"\{\{([^{}\r\n]+)\}\}")
PLACEHOLDER_ALLOWED_PUNCTUATION = set("_-.()[]@+=,~ ")
PLACEHOLDER_FORBIDDEN_CHARS = set('<>:"/\\|?*{}')

def _new_run_id() -> str:
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"run-{timestamp}-{uuid.uuid4().hex[:8]}"

def load_json_schema(inline: Any, path: Any) -> Any:
    if path is None:
        if inline is None:
            return None
        if not isinstance(inline, dict):
            raise ValueError("output.json_schema must be a JSON object.")
        return inline
    if inline is not None:
        raise ValueError("Pass either output.json_schema or output.json_schema_path, not both.")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("output.json_schema_path must be a non-empty string.")
    p = pathlib.Path(path)
    if not p.is_absolute():
        raise ValueError(f"output.json_schema_path must be absolute: {path}")
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"output.json_schema_path file must contain valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("output.json_schema_path file must contain a JSON object.")
    return data

def _normalize_batch_concurrency(value: Any) -> int:
    if value is None:
        return min(max(get_vertex_credential_count(), 1), MAX_BATCH_CONCURRENCY)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("max_concurrency must be an integer.")
    if value < 1:
        raise ValueError("max_concurrency must be at least 1.")
    return min(value, MAX_BATCH_CONCURRENCY)

def _validate_absolute_path(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    path_obj = pathlib.Path(value)
    if not path_obj.is_absolute():
        raise ValueError(f"{field_name} must be absolute: {value}")
    return str(path_obj)

def _normalize_item_id(value: Any, index: int) -> str:
    if value is None:
        return f"item-{index + 1:04d}"
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"items[{index}].id must be a non-empty string when provided.")
    if len(value) > MAX_ITEM_ID_LENGTH:
        raise ValueError(f"items[{index}].id must be at most {MAX_ITEM_ID_LENGTH} characters.")
    return value

def _normalize_system(system: Any) -> str | None:
    if system is None:
        return None
    if not isinstance(system, dict):
        raise ValueError("system must be an object with exactly one of text or path.")
    fields = [field for field in ("text", "path") if field in system]
    if len(fields) != 1:
        raise ValueError("system must contain exactly one of text or path.")
    if fields[0] == "text":
        value = system["text"]
        if not isinstance(value, str):
            raise ValueError("system.text must be a string.")
        return value
    path_value = _validate_absolute_path(system["path"], "system.path")
    return pathlib.Path(path_value).read_text(encoding="utf-8")

def _normalize_contents(contents: Any) -> list[dict[str, Any]]:
    if not isinstance(contents, list) or not contents:
        raise ValueError("request.contents must be a non-empty array.")
    normalized_contents: list[dict[str, Any]] = []
    for content_index, content in enumerate(contents, start=1):
        if not isinstance(content, dict):
            raise ValueError(f"contents[{content_index}] must be an object.")
        role = content.get("role")
        if not isinstance(role, str) or role.strip().lower() not in {"user", "model"}:
            raise ValueError(f"contents[{content_index}].role must be one of: user, model.")
        parts = content.get("parts")
        if not isinstance(parts, list) or not parts:
            raise ValueError(f"contents[{content_index}].parts must be a non-empty array.")
        normalized_parts: list[dict[str, Any]] = []
        for part_index, part in enumerate(parts, start=1):
            if not isinstance(part, dict):
                raise ValueError(f"contents[{content_index}].parts[{part_index}] must be an object.")
            fields = [field for field in ("text", "text_path", "file_path", "file_uri") if field in part]
            if len(fields) != 1:
                raise ValueError(
                    f"contents[{content_index}].parts[{part_index}] must contain exactly one of "
                    "text, text_path, file_path, or file_uri."
                )
            field = fields[0]
            value = part[field]
            part_field_name = f"contents[{content_index}].parts[{part_index}]"
            media_resolution = None
            if "media_resolution" in part:
                media_resolution = normalize_media_resolution_override(
                    part["media_resolution"],
                    f"{part_field_name}.media_resolution",
                )
            if field == "text":
                if media_resolution is not None:
                    raise ValueError(f"{part_field_name}.media_resolution is not valid for text parts.")
                if "mime_type" in part:
                    raise ValueError(f"{part_field_name}.mime_type is only valid for file_uri parts.")
                if not isinstance(value, str):
                    raise ValueError(f"contents[{content_index}].parts[{part_index}].text must be a string.")
                normalized_parts.append({"text": value})
            elif field == "text_path":
                if media_resolution is not None:
                    raise ValueError(f"{part_field_name}.media_resolution is not valid for text_path parts.")
                if "mime_type" in part:
                    raise ValueError(f"{part_field_name}.mime_type is only valid for file_uri parts.")
                normalized_parts.append(
                    {"text_path": _validate_absolute_path(value, f"contents[{content_index}].parts[{part_index}].text_path")}
                )
            elif field == "file_path":
                if "mime_type" in part:
                    raise ValueError(f"{part_field_name}.mime_type is only valid for file_uri parts.")
                file_path = _validate_absolute_path(value, f"{part_field_name}.file_path")
                if media_resolution is not None:
                    validate_media_resolution_for_mime(
                        detect_mime_type(file_path),
                        media_resolution,
                        f"{part_field_name}.media_resolution",
                    )
                normalized_part = {"file_path": file_path}
                if media_resolution is not None:
                    normalized_part["media_resolution"] = media_resolution
                normalized_parts.append(normalized_part)
            else:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{part_field_name}.file_uri must be a non-empty string.")
                mime_type = part.get("mime_type")
                if not isinstance(mime_type, str) or not mime_type.strip():
                    raise ValueError(f"{part_field_name}.mime_type is required for file_uri parts.")
                normalized_mime = mime_type.strip()
                if not is_supported_mime(normalized_mime):
                    raise ValueError(f"Unsupported MIME type for {part_field_name}.file_uri: {normalized_mime}")
                if media_resolution is not None:
                    validate_media_resolution_for_mime(
                        normalized_mime,
                        media_resolution,
                        f"{part_field_name}.media_resolution",
                    )
                normalized_part = {"file_uri": value.strip(), "mime_type": normalized_mime}
                if media_resolution is not None:
                    normalized_part["media_resolution"] = media_resolution
                normalized_parts.append(normalized_part)
        normalized_contents.append({"role": role.strip().lower(), "parts": normalized_parts})
    return normalized_contents

def _normalize_tools(tools: Any) -> dict[str, Any]:
    if tools is None:
        return {"google_search": False}
    if not isinstance(tools, dict):
        raise ValueError("tools must be an object.")
    google_search = tools.get("google_search", False)
    if not isinstance(google_search, bool):
        raise ValueError("tools.google_search must be a boolean.")
    return {"google_search": google_search}

def _normalize_rate_limit(rate_limit: Any) -> dict[str, Any]:
    if rate_limit is None:
        rate_limit = {}
    if not isinstance(rate_limit, dict):
        raise ValueError("rate_limit must be an object.")
    mode = rate_limit.get("mode", RATE_LIMIT_MODE_FAIL_FAST)
    fallback_models = rate_limit.get("fallback_models", [])
    max_wait_seconds = rate_limit.get("max_wait_seconds", DEFAULT_RATE_LIMIT_MAX_WAIT_SECONDS)
    return {
        "mode": mode,
        "fallback_models": fallback_models,
        "max_wait_seconds": max_wait_seconds,
    }

def _normalize_output(
    output: Any,
    *,
    lifecycle: str,
    run_dir: pathlib.Path | None,
    run_root: pathlib.Path,
    item_id: str,
    storage_key: str | None = None,
) -> dict[str, Any]:
    if output is None:
        output = {}
    if not isinstance(output, dict):
        raise ValueError("output must be an object.")
    mode = output.get("mode", "text")
    if mode not in {"text", "json_schema"}:
        raise ValueError("output.mode must be one of: text, json_schema.")

    output_path = output.get("path")
    output_managed = False
    if output_path is None and lifecycle == "background":
        if run_dir is None:
            raise ValueError("background output auto path requires a run directory.")
        if storage_key is None:
            raise ValueError(f"background item {item_id} is missing an internal storage key.")
        extension = ".json" if mode == "json_schema" else ".txt"
        output_path = str(managed_output_path(run_dir, storage_key, extension, run_root))
        output_managed = True
    elif output_path is not None:
        output_path = _validate_absolute_path(output_path, "output.path")

    response_json_schema = None
    if mode == "json_schema":
        has_inline_schema = output.get("json_schema") is not None
        has_schema_path = output.get("json_schema_path") is not None
        if has_inline_schema == has_schema_path:
            raise ValueError(
                "output.mode='json_schema' requires exactly one of output.json_schema "
                "or output.json_schema_path."
            )
        response_json_schema = load_json_schema(output.get("json_schema"), output.get("json_schema_path"))
    else:
        if output.get("json_schema") is not None or output.get("json_schema_path") is not None:
            raise ValueError("output json_schema fields require output.mode='json_schema'.")

    return {
        "mode": mode,
        "path": output_path,
        "managed": output_managed,
        "response_json_schema": response_json_schema,
        "expect_json_response": mode == "json_schema",
    }

def normalize_request(
    request: Any,
    *,
    lifecycle: str,
    run_dir: pathlib.Path | None,
    run_root: pathlib.Path,
    item_id: str,
    storage_key: str | None = None,
) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ValueError("item.request must be an object.")
    model = request.get("model", DEFAULT_MODEL_NAME)
    include_thinking = request.get("include_thinking", False)
    if not isinstance(include_thinking, bool):
        raise ValueError("request.include_thinking must be a boolean.")
    output = _normalize_output(
        request.get("output"),
        lifecycle=lifecycle,
        run_dir=run_dir,
        run_root=run_root,
        item_id=item_id,
        storage_key=storage_key,
    )
    media_resolution_policy = normalize_media_resolution_policy(request.get("media_resolution"))
    return {
        "model": model,
        "include_thinking": include_thinking,
        "system_prompt": _normalize_system(request.get("system")),
        "contents": _normalize_contents(request.get("contents")),
        "media_resolution": media_resolution_policy,
        "output_path": output["path"],
        "output_managed": output["managed"],
        "expect_json_response": output["expect_json_response"],
        "response_json_schema": output["response_json_schema"],
        "tools": _normalize_tools(request.get("tools")),
        "rate_limit": _normalize_rate_limit(request.get("rate_limit")),
    }

def _validate_placeholder_name(name: str) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("placeholder names must be non-empty.")
    if any(ch in name for ch in PLACEHOLDER_FORBIDDEN_CHARS):
        raise ValueError(f"Invalid placeholder name: {name}")
    if "\n" in name or "\r" in name:
        raise ValueError(f"Invalid placeholder name: {name}")
    for ch in name:
        if ch.isalnum() or ch in PLACEHOLDER_ALLOWED_PUNCTUATION:
            continue
        raise ValueError(f"Invalid placeholder name: {name}")

def _stringify_var(value: Any, name: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value)
    raise ValueError(f"vars.{name} must be a scalar string, number, or boolean.")

def _substitute_template_value(value: Any, vars_map: dict[str, Any], used_vars: set[str]) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            _validate_placeholder_name(name)
            if name not in vars_map:
                raise ValueError(f"Missing template var: {name}")
            used_vars.add(name)
            return _stringify_var(vars_map[name], name)

        substituted = PLACEHOLDER_RE.sub(replace, value)
        if "{{" in substituted or "}}" in substituted:
            raise ValueError(f"Invalid or unresolved placeholder in template string: {value}")
        return substituted
    if isinstance(value, list):
        return [_substitute_template_value(item, vars_map, used_vars) for item in value]
    if isinstance(value, dict):
        return {
            key: _substitute_template_value(item, vars_map, used_vars)
            for key, item in value.items()
        }
    return value

def _normalize_execution(execution: Any) -> dict[str, Any]:
    if execution is None:
        execution = {}
    if not isinstance(execution, dict):
        raise ValueError("execution must be an object.")
    lifecycle = execution.get("lifecycle", "blocking")
    if lifecycle not in {"blocking", "background"}:
        raise ValueError("execution.lifecycle must be one of: blocking, background.")
    return {
        "lifecycle": lifecycle,
        "max_concurrency": _normalize_batch_concurrency(execution.get("max_concurrency")),
    }

def normalize_run_plan(args: dict[str, Any], run_root: pathlib.Path) -> dict[str, Any]:
    execution = _normalize_execution(args.get("execution"))
    lifecycle = execution["lifecycle"]
    run_id = _new_run_id()
    run_dir = run_root / run_id if lifecycle == "background" else None
    if run_dir is not None:
        (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
        (run_dir / "control").mkdir(parents=True, exist_ok=True)

    raw_items = args.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("items must be a non-empty array.")

    has_template = "template_path" in args
    materialized_items: list[dict[str, Any]] = []
    if has_template:
        template_path = pathlib.Path(_validate_absolute_path(args.get("template_path"), "template_path"))
        template_data = json.loads(template_path.read_text(encoding="utf-8-sig"))
        if not isinstance(template_data, dict):
            raise ValueError("template_path file must contain a JSON object request template.")
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                raise ValueError(f"items[{index}] must be an object.")
            if "request" in item:
                raise ValueError("template items must not include request.")
            item_id = _normalize_item_id(item.get("id"), index)
            storage_key = f"item-{index + 1:06d}"
            vars_map = item.get("vars", {})
            if not isinstance(vars_map, dict):
                raise ValueError(f"items[{index}].vars must be an object.")
            for var_name in vars_map:
                _validate_placeholder_name(var_name)
            used_vars: set[str] = set()
            request = _substitute_template_value(template_data, vars_map, used_vars)
            unused_vars = sorted(set(vars_map) - used_vars)
            if unused_vars:
                raise ValueError(f"Unused template vars for item {item_id}: {', '.join(unused_vars)}")
            materialized_items.append(
                {
                    "id": item_id,
                    "index": index,
                    "storage_key": storage_key,
                    "request": normalize_request(
                        request,
                        lifecycle=lifecycle,
                        run_dir=run_dir,
                        run_root=run_root,
                        item_id=item_id,
                        storage_key=storage_key,
                    ),
                }
            )
    else:
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                raise ValueError(f"items[{index}] must be an object.")
            if "vars" in item:
                raise ValueError("explicit items must not include vars.")
            item_id = _normalize_item_id(item.get("id"), index)
            storage_key = f"item-{index + 1:06d}"
            materialized_items.append(
                {
                    "id": item_id,
                    "index": index,
                    "storage_key": storage_key,
                    "request": normalize_request(
                        item.get("request"),
                        lifecycle=lifecycle,
                        run_dir=run_dir,
                        run_root=run_root,
                        item_id=item_id,
                        storage_key=storage_key,
                    ),
                }
            )

    item_ids = [item["id"] for item in materialized_items]
    duplicate_ids = sorted({item_id for item_id in item_ids if item_ids.count(item_id) > 1})
    if duplicate_ids:
        raise ValueError(f"item ids must be unique within a run: {', '.join(duplicate_ids)}")

    return {
        "run_id": run_id,
        "lifecycle": lifecycle,
        "max_concurrency": execution["max_concurrency"],
        "run_dir": str(run_dir) if run_dir is not None else None,
        "items": materialized_items,
    }
