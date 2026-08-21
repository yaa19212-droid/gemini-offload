"""Low-level stdio MCP server for Gemini offload."""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import tempfile
from typing import Any

import anyio
import mcp.types as mcp_types
from mcp.server import Server
from mcp.server.stdio import stdio_server

try:
    from mcp.shared.exceptions import McpError as _McpError
    _MCP_ERROR_USES_ERROR_DATA = True
except ImportError:  # mcp >= 2.0 renamed the exception and changed its constructor.
    from mcp.shared.exceptions import MCPError as _McpError
    _MCP_ERROR_USES_ERROR_DATA = False

from .gemini_client import (
    AVAILABLE_MODELS,
    DEFAULT_MODEL_NAME,
    DEFAULT_MEDIA_RESOLUTION_POLICY,
    DEFAULT_RATE_LIMIT_MAX_WAIT_SECONDS,
    GeminiRateLimitError,
    MEDIA_RESOLUTION_INPUT_VALUES,
    MEDIA_RESOLUTION_POLICY_KEYS,
    MODEL_CHARACTERISTICS,
    RATE_LIMIT_MODE_FAIL_FAST,
    detect_mime,
    generate_request,
)
from .artifacts import (
    artifact_metadata as _artifact_metadata,
    atomic_write_text as _atomic_write_text,
    validate_managed_run_dir as _artifact_validate_managed_run_dir,
    validate_run_id as _artifact_validate_run_id,
    verify_recorded_artifacts as _verify_recorded_artifacts,
)
from .output_policy import (
    ENV_OUTPUT_DIR,
    INLINE_OUTPUT_BYTE_LIMIT,
    SPILL_PREVIEW_CHARS,
    _apply_output_policy,
    _resolve_auto_output_dir,
    _write_auto_json_payload,
)
from .run_service import (
    RunService,
    WorkerOwnershipLost,
    load_json_schema as _service_load_json_schema,
    normalize_request as _service_normalize_request,
    normalize_run_plan as _service_normalize_run_plan,
)
from .run_store import LeaseFenceLost, RunLeaseConflict, RunStore
from .worker import (
    inspect_run_liveness as _worker_inspect_run_liveness,
    run_worker_from_dir as _run_worker_runtime,
    spawn_worker as _worker_spawn_worker,
    terminate_verified_process_tree as _worker_terminate_verified_process_tree,
    write_control as _worker_write_control,
)


SERVER_NAME = "gemini-offload"
SERVER_VERSION = "0.2.0"


def _raise_mcp_error(code: int, message: str, data: Any = None) -> None:
    if _MCP_ERROR_USES_ERROR_DATA:
        raise _McpError(mcp_types.ErrorData(code=code, message=message, data=data))
    raise _McpError(code=code, message=message, data=data)


BATCH_AGGREGATE_BYTE_LIMIT = 4096
ENV_RUN_DIR = "GEMINI_OFFLOAD_RUN_DIR"
MAX_BATCH_CONCURRENCY = 32


def _load_json_schema(inline: Any, path: Any) -> Any:
    return _service_load_json_schema(inline, path)





def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


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
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _read_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} contains invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return data


def _run_store() -> RunStore:
    return RunStore(_resolve_run_root())


def _run_service() -> RunService:
    return RunService(
        _run_store(),
        _resolve_run_root(),
        now=_utc_now,
        export_status=lambda run_dir, status: _write_json(run_dir / "status.json", status),
        export_event=_append_event_file,
    )




def _append_event_file(run_dir: pathlib.Path, event_payload: dict[str, Any]) -> None:
    events_path = run_dir / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event_payload, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())






def _locator_matches_worker(run_dir: pathlib.Path, run_id: str, run_token: str) -> bool:
    locator_path = run_dir / "locator.json"
    if not locator_path.exists():
        return False
    try:
        locator = _read_json(locator_path)
    except Exception:
        return False
    return locator.get("run_id") == run_id and locator.get("run_token") == run_token


def _ensure_worker_owns_run(
    run_dir: pathlib.Path | None,
    run_id: str,
    run_token: str | None,
    lease_generation: int | None = None,
) -> None:
    if run_dir is None or run_token is None:
        return
    if not _locator_matches_worker(run_dir, run_id, run_token):
        raise WorkerOwnershipLost(f"Worker no longer owns run {run_id}.")
    if lease_generation is None:
        locator = _read_json(run_dir / "locator.json")
        lease_generation = locator.get("lease_generation")
    if not isinstance(lease_generation, int) or not _run_store().lease_matches(
        run_id, lease_generation, run_token
    ):
        raise WorkerOwnershipLost(f"Worker lease no longer owns run {run_id}.")


















def _structured_content_byte_count(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))




def _compact_success_job_result(
    result: dict[str, Any],
    raw_successes: list[dict[str, Any] | None],
) -> dict[str, Any]:
    index = result.get("index")
    if not isinstance(index, int) or index < 0 or index >= len(raw_successes):
        return dict(result)

    raw_success = raw_successes[index]
    if raw_success is None:
        return dict(result)
    if "output_path" in result and "text" not in result and "response_json" not in result:
        return dict(result)

    compacted = _apply_output_policy(
        dict(raw_success["result"]),
        raw_success["output_path"],
        expect_json_response=raw_success["expect_json_response"],
        force_file=True,
    )
    compacted["index"] = index
    compacted["ok"] = True
    if "id" in result:
        compacted["id"] = result["id"]
    return compacted




def _result_receipt_text(structured_content: dict[str, Any], *, is_error: bool) -> str:
    if is_error:
        return "Tool call failed. See structuredContent for error details."
    if structured_content.get("lifecycle") == "background":
        return "Background Gemini run started. Use structuredContent paths and manage_gemini_run for progress."
    if "item_count" in structured_content and "results" in structured_content:
        if structured_content.get("read_guidance"):
            return "Gemini run returned in structuredContent. Follow read_guidance before reading output files."
        return "Gemini run returned in structuredContent."
    if "job_count" in structured_content and "results" in structured_content:
        if structured_content.get("read_guidance"):
            return "Batch result returned in structuredContent. Follow read_guidance before reading output files."
        return "Batch result returned in structuredContent."
    if "response_json_error" in structured_content:
        return "JSON parsing failed. See structuredContent.response_json_error and fallback text fields."
    if "response_json" in structured_content:
        return "Structured JSON returned in structuredContent.response_json."
    if "response_json_preview" in structured_content:
        return "Structured JSON saved to output_path. See structuredContent.response_json_preview."
    if "output_path" in structured_content:
        if structured_content.get("read_guidance"):
            return "Full result saved to output_path. Follow structuredContent.read_guidance."
        return "Result saved to output_path. See structuredContent for details."
    if "text" in structured_content:
        return "Result returned in structuredContent.text."
    return "Result returned in structuredContent."


def _wrap_result(result: dict[str, Any], *, is_error: bool = False) -> mcp_types.CallToolResult:
    structured_content = dict(result)
    inline_images = structured_content.pop("_inline_images", [])
    content: list[Any] = [
        mcp_types.TextContent(
            type="text",
            text=_result_receipt_text(structured_content, is_error=is_error),
        )
    ]
    for image in inline_images:
        content.append(
            mcp_types.ImageContent(
                type="image",
                data=image["data"],
                mimeType=image["mime_type"],
            )
        )

    return mcp_types.CallToolResult(
        content=content,
        structuredContent=structured_content,
        isError=is_error,
    )




def _validate_run_id(value: Any) -> str:
    return _artifact_validate_run_id(value)


def _validate_managed_run_dir(value: Any) -> pathlib.Path:
    return _artifact_validate_managed_run_dir(value, _resolve_run_root())




def _normalize_request(
    request: Any,
    *,
    lifecycle: str,
    run_dir: pathlib.Path | None,
    item_id: str,
    storage_key: str | None = None,
) -> dict[str, Any]:
    return _service_normalize_request(
        request,
        lifecycle=lifecycle,
        run_dir=run_dir,
        run_root=_resolve_run_root(),
        item_id=item_id,
        storage_key=storage_key,
    )


def _normalize_run_plan(args: dict[str, Any]) -> dict[str, Any]:
    return _service_normalize_run_plan(args, _resolve_run_root())

def _run_read_guidance(result: dict[str, Any], *, manifest_saved: bool) -> str:
    byte_count = result["aggregate_byte_count"]
    limit = result["aggregate_inline_limit"]
    if manifest_saved:
        return (
            f"Run response was {byte_count} bytes before compaction, above the {limit}-byte "
            "inline budget. Full compacted run results were saved to results_path. Avoid "
            "reading the entire manifest or every output file unless needed; inspect targeted "
            "items, output_path files, or ranges first."
        )
    return (
        f"Run response was {byte_count} bytes before compaction, above the {limit}-byte "
        "inline budget. Successful item outputs were saved to per-item output_path values. "
        "Avoid reading every output file unless needed; inspect targeted items or ranges first."
    )


def _apply_run_aggregate_policy(
    run_result: dict[str, Any],
    raw_successes: list[dict[str, Any] | None],
) -> dict[str, Any]:
    aggregate_byte_count = _structured_content_byte_count(run_result)
    if aggregate_byte_count <= BATCH_AGGREGATE_BYTE_LIMIT:
        return run_result

    compacted_results: list[dict[str, Any]] = []
    for result in run_result["results"]:
        if result.get("ok") is True:
            compacted_results.append(_compact_success_job_result(result, raw_successes))
        else:
            compacted_results.append(dict(result))

    compacted = dict(run_result)
    compacted.update(
        {
            "results": compacted_results,
            "results_compacted": True,
            "aggregate_byte_count": aggregate_byte_count,
            "aggregate_inline_limit": BATCH_AGGREGATE_BYTE_LIMIT,
        }
    )
    compacted["read_guidance"] = _run_read_guidance(compacted, manifest_saved=False)
    if _structured_content_byte_count(compacted) <= BATCH_AGGREGATE_BYTE_LIMIT:
        return compacted

    manifest_path = _write_auto_json_payload(compacted, prefix="run-results")
    omitted = dict(compacted)
    omitted.update(
        {
            "results": [],
            "results_path": str(manifest_path),
            "results_omitted": True,
            "omitted_result_count": len(compacted_results),
        }
    )
    omitted["read_guidance"] = _run_read_guidance(omitted, manifest_saved=True)
    return omitted


async def _generate_raw_from_request(request: dict[str, Any]) -> tuple[dict[str, Any], bool]:
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


def _initial_status(plan: dict[str, Any], status: str) -> dict[str, Any]:
    return _run_service().initial_status(plan, status)


def _write_status(
    run_dir: pathlib.Path,
    status: dict[str, Any],
    *,
    lease_generation: int | None = None,
    lease_token: str | None = None,
) -> None:
    status["updated_at"] = _utc_now()
    try:
        _run_store().persist_status_snapshot(
            status,
            lease_generation=lease_generation,
            lease_token=lease_token,
        )
    except LeaseFenceLost as exc:
        raise WorkerOwnershipLost(str(exc)) from exc
    _write_json(run_dir / "status.json", status)


def _read_control_action(run_dir: pathlib.Path) -> str | None:
    control_dir = run_dir / "control"
    if (control_dir / "cancel.json").exists():
        return "cancel"
    if (control_dir / "stop.json").exists():
        return "stop"
    return None


def _classify_run_item_error(exc: Exception) -> dict[str, Any] | None:
    if isinstance(exc, GeminiRateLimitError):
        return {
            "result": exc.to_dict(),
            "message": exc.message,
            "error_type": "vertex_rate_limited",
        }
    return None


async def _execute_run_plan(
    plan: dict[str, Any],
    *,
    run_dir: pathlib.Path | None = None,
    background: bool = False,
    worker_token: str | None = None,
    worker_generation: int | None = None,
) -> dict[str, Any]:
    return await _run_service().execute_plan(
        plan,
        run_dir=run_dir,
        background=background,
        worker_token=worker_token,
        worker_generation=worker_generation,
        generate=_generate_raw_from_request,
        apply_output=lambda result, path, expect_json: _apply_output_policy(
            result, path, expect_json_response=expect_json
        ),
        aggregate=_apply_run_aggregate_policy,
        ensure_owner=_ensure_worker_owns_run,
        control_action=_read_control_action,
        classify_error=_classify_run_item_error,
    )

def _call_gemini_input_schema() -> dict[str, Any]:
    media_resolution_value_schema = {
        "type": "string",
        "enum": list(MEDIA_RESOLUTION_INPUT_VALUES),
    }
    non_image_media_resolution_value_schema = {
        "type": "string",
        "enum": [value for value in MEDIA_RESOLUTION_INPUT_VALUES if value != "ultra_high"],
    }
    part_schema = {
        "type": "object",
        "description": "Content part. Set exactly one of text, text_path, file_path, or file_uri.",
        "properties": {
            "text": {"type": "string"},
            "text_path": {"type": "string", "description": "Absolute path to a UTF-8 text file."},
            "file_path": {"type": "string", "description": "Absolute path to a local file part."},
            "file_uri": {"type": "string", "description": "Remote file URI such as gs://bucket/object."},
            "mime_type": {"type": "string", "description": "Required MIME type for file_uri parts."},
            "media_resolution": {
                **media_resolution_value_schema,
                "description": "Optional per-file override. Applies only to image, PDF, and video parts.",
            },
        },
        "additionalProperties": False,
    }
    content_schema = {
        "type": "object",
        "properties": {
            "role": {"type": "string", "enum": ["user", "model"]},
            "parts": {"type": "array", "items": part_schema, "minItems": 1},
        },
        "required": ["role", "parts"],
        "additionalProperties": False,
    }
    request_schema = {
        "type": "object",
        "properties": {
            "model": {"type": "string", "default": DEFAULT_MODEL_NAME},
            "include_thinking": {"type": "boolean", "default": False},
            "system": {
                "type": "object",
                "description": "Optional system instruction. Set exactly one of text or path.",
                "properties": {
                    "text": {"type": "string"},
                    "path": {"type": "string", "description": "Absolute path to a UTF-8 system prompt file."},
                },
                "additionalProperties": False,
            },
            "contents": {"type": "array", "items": content_schema, "minItems": 1},
            "media_resolution": {
                "type": "object",
                "description": (
                    "Optional request-level defaults for image, PDF, and video parts. "
                    "Omit for image=ultra_high, pdf=high, video=high."
                ),
                "properties": {
                    key: {
                        **(
                            media_resolution_value_schema
                            if key == "image"
                            else non_image_media_resolution_value_schema
                        ),
                        "default": DEFAULT_MEDIA_RESOLUTION_POLICY[key],
                    }
                    for key in MEDIA_RESOLUTION_POLICY_KEYS
                },
                "additionalProperties": False,
            },
            "output": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["text", "json_schema"], "default": "text"},
                    "path": {"type": "string", "description": "Absolute output path. Forces file-backed output when set."},
                    "json_schema": {
                        "type": "object",
                        "description": "Inline JSON Schema object. Only valid with output.mode: json_schema.",
                    },
                    "json_schema_path": {
                        "type": "string",
                        "description": "Absolute path to a JSON Schema file. Only valid with output.mode: json_schema.",
                    },
                },
                "additionalProperties": False,
            },
            "tools": {
                "type": "object",
                "properties": {"google_search": {"type": "boolean", "default": False}},
                "additionalProperties": False,
            },
            "rate_limit": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["fail_fast", "wait"], "default": RATE_LIMIT_MODE_FAIL_FAST},
                    "fallback_models": {"type": "array", "items": {"type": "string"}, "default": []},
                    "max_wait_seconds": {"type": "number", "default": DEFAULT_RATE_LIMIT_MAX_WAIT_SECONDS},
                },
                "additionalProperties": False,
            },
        },
        "required": ["contents"],
        "additionalProperties": False,
    }
    execution_schema = {
        "type": "object",
        "properties": {
            "lifecycle": {"type": "string", "enum": ["blocking", "background"], "default": "blocking"},
            "max_concurrency": {
                "type": "integer",
                "description": f"Maximum concurrent items, capped at {MAX_BATCH_CONCURRENCY}.",
            },
        },
        "additionalProperties": False,
    }
    explicit_item_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "request": request_schema,
        },
        "required": ["request"],
        "additionalProperties": False,
    }
    template_item_schema = {
        "type": "object",
        "description": "Template item. Use with top-level template_path and provide per-item vars.",
        "properties": {
            "id": {"type": "string"},
            "vars": {"type": "object"},
        },
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "description": (
            "Use either explicit items with items[].request, or template_path with "
            "items[].vars. The server validates that the two shapes are not mixed."
        ),
        "properties": {
            "template_path": {
                "type": "string",
                "description": (
                    "Absolute path to a JSON request template. When provided, each item "
                    "must provide vars instead of request."
                ),
            },
            "items": {
                "type": "array",
                "description": (
                    "Run items. Explicit calls use items[].request; template calls use "
                    "template_path plus items[].vars."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "request": explicit_item_schema["properties"]["request"],
                        "vars": template_item_schema["properties"]["vars"],
                    },
                    "additionalProperties": False,
                },
                "minItems": 1,
            },
            "execution": execution_schema,
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def _run_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "lifecycle": {"type": "string"},
            "status": {"type": "string"},
            "run_dir": {"type": "string"},
            "plan_path": {"type": "string"},
            "status_path": {"type": "string"},
            "events_path": {"type": "string"},
            "locator_path": {"type": "string"},
            "read_guidance": {"type": "string"},
            "item_count": {"type": "integer"},
            "ok_count": {"type": "integer"},
            "error_count": {"type": "integer"},
            "max_concurrency": {"type": "integer"},
            "results": {"type": "array", "items": {"type": "object"}},
            "results_compacted": {"type": "boolean"},
            "aggregate_byte_count": {"type": "integer"},
            "aggregate_inline_limit": {"type": "integer"},
            "results_path": {"type": "string"},
        },
        "required": ["run_id", "lifecycle"],
        "additionalProperties": True,
    }


def _manage_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "status", "progress", "stop", "cancel", "resume"],
            },
            "run_id": {"type": "string"},
            "run_dir": {"type": "string"},
            "event_offset": {"type": "integer", "default": 0},
            "max_events": {"type": "integer", "default": 50},
            "force": {"type": "boolean", "default": False},
        },
        "required": ["action"],
        "additionalProperties": False,
    }


def _run_dir_from_args(args: dict[str, Any]) -> pathlib.Path:
    run_dir_arg = args.get("run_dir")
    run_id_arg = args.get("run_id")
    if run_dir_arg is not None:
        run_dir = _validate_managed_run_dir(run_dir_arg)
        if run_id_arg is not None and _validate_run_id(run_id_arg) != run_dir.name:
            raise ValueError("run_id does not match run_dir.")
        return run_dir
    if run_id_arg is None:
        raise ValueError("run_id or run_dir is required for this action.")
    run_id = _validate_run_id(run_id_arg)
    return _validate_managed_run_dir(_resolve_run_root() / run_id)


def _spawn_worker(run_dir: pathlib.Path, run_id: str, run_token: str) -> dict[str, Any]:
    return _worker_spawn_worker(run_dir, run_id, run_token)



def _start_background_run(plan: dict[str, Any]) -> dict[str, Any]:
    run_dir = _validate_managed_run_dir(plan["run_dir"])
    if _validate_run_id(plan.get("run_id")) != run_dir.name or plan.get("lifecycle") != "background":
        raise ValueError("Background plan identity does not match its managed run directory.")
    started = _run_service().start_background(
        plan,
        write_plan=lambda path, payload: _write_json(path / "plan.json", payload),
        spawn_worker=_spawn_worker,
    )
    locator = started["locator"]
    return {
        "run_id": plan["run_id"],
        "lifecycle": "background",
        "status": "starting",
        "run_dir": str(run_dir),
        "plan_path": str(run_dir / "plan.json"),
        "status_path": str(run_dir / "status.json"),
        "events_path": str(run_dir / "events.jsonl"),
        "locator_path": str(run_dir / "locator.json"),
        "item_count": len(plan["items"]),
        "max_concurrency": plan["max_concurrency"],
        "pid": locator["pid"],
        "read_guidance": (
            "Background Gemini run started. Results will be written under run_dir. "
            "Use manage_gemini_run for status/progress, or inspect appended events_path "
            "from the last read offset instead of rereading the full log."
        ),
    }




def _inspect_run_liveness(run_dir: pathlib.Path) -> dict[str, Any]:
    return _worker_inspect_run_liveness(run_dir)





def _write_control(run_dir: pathlib.Path, action: str) -> pathlib.Path:
    return _worker_write_control(run_dir, action)



def _terminate_verified_process_tree(run_dir: pathlib.Path) -> dict[str, Any]:
    return _worker_terminate_verified_process_tree(run_dir)



def _reconcile_stale_runs(*, exclude_run_id: str | None = None) -> list[str]:
    return _run_service().reconcile_stale_runs(exclude_run_id=exclude_run_id)


def _manage_gemini_run(args: dict[str, Any]) -> dict[str, Any]:
    action = args.get("action")
    if action == "list":
        run_root = _resolve_run_root()
        runs = [
            {
                "run_id": status["run_id"],
                "run_dir": str(run_root / status["run_id"]),
                "status": status.get("status", "unknown"),
                "item_count": status.get("item_count"),
                "completed_count": status.get("completed_count"),
                "updated_at": status.get("updated_at"),
            }
            for status in _run_service().list_runs()
        ]
        return {"runs": runs, "run_root": str(run_root)}

    run_dir = _run_dir_from_args(args)
    if not run_dir.exists():
        raise ValueError(f"run_dir does not exist: {run_dir}")

    if action == "status":
        status = _run_service().status(run_dir.name)
        if status is None:
            raise ValueError(f"run is not registered in store: {run_dir.name}")
        status.update(_inspect_run_liveness(run_dir))
        status["run_dir"] = str(run_dir)
        return status

    if action == "progress":
        offset = args.get("event_offset", 0)
        max_events = args.get("max_events", 50)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("event_offset must be a non-negative integer.")
        if not isinstance(max_events, int) or isinstance(max_events, bool) or max_events < 1:
            raise ValueError("max_events must be a positive integer.")
        max_events = min(max_events, 500)
        payload = _run_service().progress(run_dir.name, after_sequence=offset, max_events=max_events)
        payload.update({
            "events_path": str(run_dir / "events.jsonl"),
            "run_id": run_dir.name,
            "run_dir": str(run_dir),
        })
        payload["liveness"] = _inspect_run_liveness(run_dir)
        return payload

    if action in {"stop", "cancel"}:
        control_path = _write_control(run_dir, action)
        service = _run_service()
        service.request_control(run_dir.name, action)
        force = args.get("force", False)
        termination = {
            "all_gone": False,
            "terminated": False,
            "termination_failed": False,
            "alive_pids": [],
        }
        if action == "cancel" and force is True:
            termination = _terminate_verified_process_tree(run_dir)
            if termination["all_gone"]:
                service.finalize_forced_cancel(run_dir.name)
            else:
                service.append_event(
                    run_dir.name,
                    "cancel_failed",
                    forced=True,
                    alive_pids=termination.get("alive_pids", []),
                )
        return {
            "run_id": run_dir.name,
            "run_dir": str(run_dir),
            "action": action,
            "control_path": str(control_path),
            "forced_termination": termination["terminated"],
            "termination_failed": termination["termination_failed"],
            "alive_pids": termination.get("alive_pids", []),
            "liveness": _inspect_run_liveness(run_dir),
        }

    if action == "resume":
        plan_path = run_dir / "plan.json"
        if not plan_path.exists():
            raise ValueError(f"Missing plan.json for run: {run_dir}")
        liveness = _inspect_run_liveness(run_dir)
        if liveness.get("process_alive") is True:
            raise ValueError(f"Cannot resume run while a verified worker is still alive: {run_dir}")
        plan = _read_json(plan_path)
        if (
            plan.get("run_id") != run_dir.name
            or plan.get("run_dir") != str(run_dir)
            or plan.get("lifecycle") != "background"
        ):
            raise ValueError(f"Persisted plan identity mismatch for run: {run_dir}")
        legacy_status = (
            _read_json(run_dir / "status.json")
            if (run_dir / "status.json").exists()
            else None
        )
        try:
            resumed = _run_service().resume_background(
                plan,
                legacy_status=legacy_status,
                spawn_worker=_spawn_worker,
            )
        except RunLeaseConflict as exc:
            raise ValueError(str(exc)) from exc
        locator = resumed["locator"]
        return {
            "run_id": plan["run_id"],
            "run_dir": str(run_dir),
            "status": "starting",
            "pid": locator["pid"],
            "locator_path": str(run_dir / "locator.json"),
        }

    raise ValueError("action must be one of: list, status, progress, stop, cancel, resume.")


def _tool_definitions() -> list[mcp_types.Tool]:
    return [
        mcp_types.Tool(
            name="call_gemini",
            description=(
                "Run one or more Gemini request items using explicit request envelopes or a "
                "template plus per-item vars. Set execution.lifecycle to `blocking` for an "
                "inline/spill result or `background` to start a child worker process and return "
                "run paths immediately. Tool content text is a short receipt; use structuredContent."
            ),
            inputSchema=_call_gemini_input_schema(),
            outputSchema=_run_output_schema(),
        ),
        mcp_types.Tool(
            name="manage_gemini_run",
            description=(
                "Inspect or control background Gemini runs. Use action=list, status, progress, "
                "stop, cancel, or resume. Live state is checked from runtime process state, not "
                "only from the append-only event log."
            ),
            inputSchema=_manage_input_schema(),
            outputSchema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "run_dir": {"type": "string"},
                    "status": {"type": "string"},
                    "runs": {"type": "array", "items": {"type": "object"}},
                    "events": {"type": "array", "items": {"type": "object"}},
                    "liveness": {"type": "object"},
                },
                "additionalProperties": True,
            },
        ),
        mcp_types.Tool(
            name="list_gemini_models",
            description="List the Gemini models supported by this server with selection guidance.",
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "models": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "model_characteristics": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    }
                },
                "required": ["models", "model_characteristics"],
                "additionalProperties": False,
            },
        ),
        mcp_types.Tool(
            name="detect_mime",
            description="Detect the MIME type for a local absolute-path file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Local absolute path to a file.",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "mime": {"type": "string"},
                    "supported": {"type": "boolean"},
                },
                "required": ["mime", "supported"],
                "additionalProperties": False,
            },
        ),
    ]


_SERVER_INSTRUCTIONS = (
    "Run-oriented stdio MCP wrapper for Gemini generate_content calls. "
    "Use `call_gemini` for blocking or background runs and `manage_gemini_run` "
    "to inspect or control background runs. Vertex rate limits are tracked per "
    "project/location/model quota slot."
)


async def handle_list_tools() -> list[mcp_types.Tool]:
    return _tool_definitions()


async def handle_call_tool(name: str, arguments: dict[str, Any] | None):
    args = arguments or {}

    try:
        if name == "call_gemini":
            plan = _normalize_run_plan(args)
            if plan["lifecycle"] == "background":
                return _wrap_result(_start_background_run(plan))
            return _wrap_result(await _execute_run_plan(plan))

        if name == "manage_gemini_run":
            return _wrap_result(_manage_gemini_run(args))

        if name == "list_gemini_models":
            return _wrap_result(
                {
                    "models": AVAILABLE_MODELS,
                    "model_characteristics": MODEL_CHARACTERISTICS,
                }
            )

        if name == "detect_mime":
            result = await anyio.to_thread.run_sync(detect_mime, args["path"])
            return _wrap_result(result)

    except (FileNotFoundError, ValueError) as exc:
        _raise_mcp_error(mcp_types.INVALID_PARAMS, str(exc))
    except GeminiRateLimitError as exc:
        return _wrap_result(exc.to_dict(), is_error=True)
    except Exception as exc:
        _raise_mcp_error(mcp_types.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    _raise_mcp_error(mcp_types.INVALID_PARAMS, f"Unknown tool: {name}")


async def _v2_list_tools(_context: Any, _params: Any) -> mcp_types.ListToolsResult:
    return mcp_types.ListToolsResult(tools=await handle_list_tools())


async def _v2_call_tool(_context: Any, params: Any):
    return await handle_call_tool(params.name, params.arguments)


def _create_mcp_server() -> Server:
    if hasattr(Server, "list_tools") and hasattr(Server, "call_tool"):
        instance = Server(SERVER_NAME, version=SERVER_VERSION, instructions=_SERVER_INSTRUCTIONS)
        instance.list_tools()(handle_list_tools)
        instance.call_tool()(handle_call_tool)
        return instance
    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        instructions=_SERVER_INSTRUCTIONS,
        on_list_tools=_v2_list_tools,
        on_call_tool=_v2_call_tool,
    )


server = _create_mcp_server()

async def run_worker_from_dir(run_dir: str, run_id: str, run_token: str) -> None:
    """Compatibility adapter; the child entrypoint imports worker.py directly."""
    await _run_worker_runtime(
        run_dir,
        run_id,
        run_token,
        generate=_generate_raw_from_request,
        apply_output=lambda result, path, expect_json: _apply_output_policy(
            result, path, expect_json_response=expect_json
        ),
        classify_error=_classify_run_item_error,
    )


async def main() -> None:
    _reconcile_stale_runs()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
