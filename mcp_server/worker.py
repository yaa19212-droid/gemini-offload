from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import psutil

from .artifacts import atomic_write_text, utc_now, validate_managed_run_dir, validate_run_id
from .output_policy import _apply_output_policy
from .run_service import (
    RunService,
    WorkerOwnershipLost,
    classify_gemini_error,
    generate_raw_from_request,
)
from .run_store import RunStore


async def run_owned_worker(
    store: RunStore,
    run_id: str,
    generation: int,
    token: str,
    *,
    execute: Callable[[], Awaitable[None]],
    on_failure: Callable[[BaseException], Awaitable[None]],
    ownership_lost: Callable[[str], BaseException],
    heartbeat_interval: float = 10.0,
    lease_seconds: float = 30.0,
) -> None:
    async def heartbeat() -> None:
        while True:
            await anyio.sleep(heartbeat_interval)
            if not store.heartbeat_lease(
                run_id,
                generation,
                token,
                lease_seconds=lease_seconds,
            ):
                raise ownership_lost(f"Worker lease no longer owns run {run_id}.")

    try:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(heartbeat)
            await execute()
            task_group.cancel_scope.cancel()
    except BaseException as exc:
        if store.lease_matches(run_id, generation, token):
            await on_failure(exc)
        raise
    finally:
        store.release_lease(run_id, generation, token)


ENV_RUN_DIR = "GEMINI_OFFLOAD_RUN_DIR"


def _resolve_run_root() -> pathlib.Path:
    configured = os.environ.get(ENV_RUN_DIR)
    if configured is not None and configured.strip():
        run_root = pathlib.Path(configured)
        if not run_root.is_absolute():
            raise ValueError(f"{ENV_RUN_DIR} must be an absolute path: {configured}")
    else:
        run_root = pathlib.Path(tempfile.gettempdir()) / "gemini-offload" / "runs"
    run_root.mkdir(parents=True, exist_ok=True)
    return run_root.resolve()


def _write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _read_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} contains invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return data


def _append_event_file(run_dir: pathlib.Path, event: dict[str, Any]) -> None:
    events_path = run_dir / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _locator_matches_worker(run_dir: pathlib.Path, run_id: str, token: str) -> bool:
    locator_path = run_dir / "locator.json"
    if not locator_path.exists():
        return False
    try:
        locator = _read_json(locator_path)
    except Exception:
        return False
    return locator.get("run_id") == run_id and locator.get("run_token") == token


def _ensure_worker_owns_run(
    store: RunStore,
    run_dir: pathlib.Path | None,
    run_id: str,
    token: str | None,
    generation: int | None,
) -> None:
    if run_dir is None or token is None:
        return
    if not _locator_matches_worker(run_dir, run_id, token):
        raise WorkerOwnershipLost(f"Worker no longer owns run {run_id}.")
    if generation is None:
        generation = _read_json(run_dir / "locator.json").get("lease_generation")
    if not isinstance(generation, int) or not store.lease_matches(run_id, generation, token):
        raise WorkerOwnershipLost(f"Worker lease no longer owns run {run_id}.")


def _read_control_action(run_dir: pathlib.Path) -> str | None:
    control_dir = run_dir / "control"
    if (control_dir / "cancel.json").exists():
        return "cancel"
    if (control_dir / "stop.json").exists():
        return "stop"
    return None


def _service(store: RunStore, run_root: pathlib.Path) -> RunService:
    return RunService(
        store,
        run_root,
        now=utc_now,
        export_status=lambda path, status: _write_json(path / "status.json", status),
        export_event=_append_event_file,
    )


async def run_worker_from_dir(
    run_dir: str,
    run_id: str,
    run_token: str,
    *,
    generate: Callable[[dict[str, Any]], Awaitable[tuple[dict[str, Any], bool]]] = generate_raw_from_request,
    apply_output: Callable[[dict[str, Any], Any, bool], dict[str, Any]] | None = None,
    classify_error: Callable[[Exception], dict[str, Any] | None] = classify_gemini_error,
) -> None:
    run_root = _resolve_run_root()
    if apply_output is None:
        apply_output = lambda result, path, expect_json: _apply_output_policy(
            result, path, expect_json_response=expect_json
        )
    run_path = validate_managed_run_dir(run_dir, run_root)
    validated_run_id = validate_run_id(run_id)
    if run_path.name != validated_run_id:
        raise ValueError(f"run_dir does not match run_id for worker: {run_id}")

    deadline = time.monotonic() + 10
    while not _locator_matches_worker(run_path, run_id, run_token):
        if time.monotonic() >= deadline:
            raise ValueError(f"locator.json does not match worker identity for run: {run_id}")
        await anyio.sleep(0.05)
    locator = _read_json(run_path / "locator.json")
    generation = locator.get("lease_generation")
    if not isinstance(generation, int):
        raise ValueError(f"locator.json is missing lease generation for run: {run_id}")

    store = RunStore(run_root)
    service = _service(store, run_root)
    service.reconcile_stale_runs(exclude_run_id=validated_run_id)
    _ensure_worker_owns_run(store, run_path, run_id, run_token, generation)
    service.append_event(
        run_id,
        "worker_started",
        lease_generation=generation,
        lease_token=run_token,
    )

    async def execute_owned_run() -> None:
        plan = _read_json(run_path / "plan.json")
        if plan.get("run_id") != validated_run_id or plan.get("run_dir") != str(run_path):
            raise ValueError(f"run plan identity mismatch for worker: {run_id}")
        if plan.get("lifecycle") != "background":
            raise ValueError(f"worker plan must use background lifecycle: {run_id}")
        await service.execute_plan(
            plan,
            run_dir=run_path,
            background=True,
            worker_token=run_token,
            worker_generation=generation,
            generate=generate,
            apply_output=apply_output,
            aggregate=lambda summary, _raw: summary,
            ensure_owner=lambda path, rid, token, gen: _ensure_worker_owns_run(
                store, path, rid, token, gen
            ),
            control_action=_read_control_action,
            classify_error=classify_error,
        )

    async def persist_failure(exc: BaseException) -> None:
        service.persist_worker_failure(run_id, generation, run_token, exc)

    await run_owned_worker(
        store,
        run_id,
        generation,
        run_token,
        execute=execute_owned_run,
        on_failure=persist_failure,
        ownership_lost=WorkerOwnershipLost,
    )


def spawn_worker(run_dir: pathlib.Path, run_id: str, run_token: str) -> dict[str, Any]:
    store = RunStore(_resolve_run_root())
    lease_generation = store.acquire_lease(run_id, run_token)
    command = [
        sys.executable,
        "-m",
        "mcp_server.run_worker",
        "--run-dir",
        str(run_dir),
        "--run-id",
        run_id,
        "--run-token",
        run_token,
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(
            command,
            cwd=str(pathlib.Path(__file__).resolve().parents[1]),
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except BaseException:
        store.release_lease(run_id, lease_generation, run_token)
        raise
    if not store.bind_lease_owner(run_id, lease_generation, run_token, process.pid):
        try:
            process.terminate()
        finally:
            store.release_lease(run_id, lease_generation, run_token)
        raise RuntimeError(f"worker lease was superseded during spawn: {run_id}")
    try:
        create_time = psutil.Process(process.pid).create_time()
    except Exception:
        create_time = None
    locator = {
        "run_id": run_id,
        "pid": process.pid,
        "create_time": create_time,
        "run_token": run_token,
        "lease_generation": lease_generation,
        "command": command,
        "spawned_at": utc_now(),
    }
    try:
        _write_json(run_dir / "locator.json", locator)
    except BaseException:
        try:
            process.terminate()
        finally:
            store.release_lease(run_id, lease_generation, run_token)
        raise
    return locator

def verified_process_from_locator(locator: dict[str, Any]) -> psutil.Process | None:
    pid = locator.get("pid")
    if not isinstance(pid, int):
        return None
    try:
        process = psutil.Process(pid)
        create_time = locator.get("create_time")
        if isinstance(create_time, (int, float)) and abs(process.create_time() - float(create_time)) > 0.01:
            return None
        token = locator.get("run_token")
        command_line = " ".join(process.cmdline())
        if isinstance(token, str) and token and token not in command_line:
            return None
        return process
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None

def inspect_run_liveness(run_dir: pathlib.Path) -> dict[str, Any]:
    status_path = run_dir / "status.json"
    status: dict[str, Any] = {}
    status_error = None
    if status_path.exists():
        try:
            status = _read_json(status_path)
        except Exception as exc:
            status_error = f"{type(exc).__name__}: {exc}"
    terminal = {"completed", "failed", "canceled", "stopped"}
    locator_path = run_dir / "locator.json"
    if locator_path.exists():
        try:
            locator = _read_json(locator_path)
        except Exception as exc:
            payload = {"process_alive": False, "live_status": "unknown", "reason": f"invalid locator.json: {exc}"}
            if status_error is not None:
                payload["status_error"] = status_error
            return payload
        process = verified_process_from_locator(locator)
        if process is not None:
            payload = {"process_alive": True, "live_status": status.get("status", "running"), "pid": process.pid}
            if status.get("status") in terminal:
                payload["terminal_status_with_live_process"] = True
            if status_error is not None:
                payload["status_error"] = status_error
            return payload
    elif status.get("status") not in terminal:
        payload = {"process_alive": False, "live_status": "unknown", "reason": "missing locator.json"}
        if status_error is not None:
            payload["status_error"] = status_error
        return payload

    if status.get("status") in terminal:
        payload = {"process_alive": False, "live_status": status.get("status")}
        if status_error is not None:
            payload["status_error"] = status_error
        return payload

    payload = {"process_alive": False, "live_status": "unknown", "reason": "worker process not verified"}
    if status_error is not None:
        payload["status_error"] = status_error
    return payload

def write_control(run_dir: pathlib.Path, action: str) -> pathlib.Path:
    control_path = run_dir / "control" / f"{action}.json"
    payload = {"action": action, "requested_at": utc_now()}
    _write_json(control_path, payload)
    return control_path

def terminate_verified_process_tree(run_dir: pathlib.Path) -> dict[str, Any]:
    locator_path = run_dir / "locator.json"
    if not locator_path.exists():
        return {"all_gone": True, "terminated": False, "termination_failed": False, "reason": "missing locator.json"}
    try:
        locator = _read_json(locator_path)
    except Exception as exc:
        return {
            "all_gone": False,
            "terminated": False,
            "termination_failed": True,
            "alive_pids": [],
            "reason": f"could not read locator.json: {exc}",
        }
    process = verified_process_from_locator(locator)
    if process is None:
        return {"all_gone": True, "terminated": False, "termination_failed": False, "reason": "worker process not verified"}
    try:
        children = process.children(recursive=True)
    except psutil.Error as exc:
        return {
            "all_gone": False,
            "terminated": False,
            "termination_failed": True,
            "alive_pids": [process.pid],
            "reason": f"could not inspect process tree: {exc}",
        }
    for child in children:
        try:
            child.terminate()
        except psutil.Error:
            pass
    try:
        process.terminate()
    except psutil.Error:
        pass
    gone, alive = psutil.wait_procs(children + [process], timeout=5)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass
    _, still_alive = psutil.wait_procs(alive, timeout=5)
    alive_pids = [proc.pid for proc in still_alive if proc.is_running()]
    return {
        "all_gone": not alive_pids,
        "terminated": bool(gone or alive) and not alive_pids,
        "termination_failed": bool(alive_pids),
        "alive_pids": alive_pids,
    }
