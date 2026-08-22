"""Read-only Vertex setup diagnostics."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx
from google.auth import exceptions as google_auth_exceptions
from google.auth.transport import Response
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from . import keys

SETUP_STATUS_INVALID = "invalid"
SETUP_STATUS_UNVERIFIED = "unverified"
SETUP_STATUS_VERIFIED = "verified"
VERTEX_SCOPE = keys.VERTEX_SCOPE
OAUTH_REFRESH_TIMEOUT_SECONDS = 3

ENV_VERTEX_CREDENTIALS = keys.ENV_VERTEX_CREDENTIALS
ENV_VERTEX_MANIFEST = keys.ENV_VERTEX_CREDENTIALS
DEFAULT_VERTEX_MANIFEST = keys.DEFAULT_VERTEX_MANIFEST


class _HttpxAuthResponse(Response):
    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    @property
    def status(self) -> int:
        return self._response.status_code

    @property
    def data(self) -> bytes:
        return self._response.content

    @property
    def headers(self):
        return self._response.headers


class _HttpxAuthRequest(Request):
    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        try:
            response = httpx.request(
                method,
                url,
                content=body,
                headers=headers,
                timeout=OAUTH_REFRESH_TIMEOUT_SECONDS,
            )
        except httpx.RequestError as exc:
            raise google_auth_exceptions.TransportError(str(exc)) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise google_auth_exceptions.TransportError(
                f"OAuth transport returned HTTP {response.status_code}."
            )
        return _HttpxAuthResponse(response)


def _manifest_info() -> tuple[str, Path]:
    source, path = keys.resolve_vertex_manifest_info()
    if source == "default":
        path = DEFAULT_VERTEX_MANIFEST
    return source, path


def _safe_entry_name(entry: dict[str, Any], index: int) -> str:
    name = entry.get("name")
    return str(name) if isinstance(name, str) and name.strip() else f"credential-{index}"


def _credential_entry(entry: Any, *, index: int, manifest_path: Path) -> dict[str, Any]:
    item: dict[str, Any] = {"index": index, "status": SETUP_STATUS_INVALID}
    if not isinstance(entry, dict):
        item["error"] = "Manifest entry must be an object."
        return item
    item["name"] = _safe_entry_name(entry, index)
    raw_path = entry.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        item["error"] = "Credential entry is missing path."
        return item
    key_path = Path(raw_path).expanduser()
    if not key_path.is_absolute():
        key_path = manifest_path.parent / key_path
    key_path = key_path.resolve(strict=False)
    item["credential_path"] = str(key_path)
    if not key_path.exists():
        item["error"] = f"Credential file not found: {key_path.name}"
        return item
    try:
        payload = json.loads(key_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        item["error"] = f"Credential JSON is invalid: {key_path.name}"
        return item
    project_id = entry.get("project_id") or (payload.get("project_id") if isinstance(payload, dict) else None)
    if not isinstance(project_id, str) or not project_id.strip():
        item["error"] = "Credential is missing project_id."
        return item
    item["project_id"] = project_id.strip()
    try:
        credential = service_account.Credentials.from_service_account_file(
            str(key_path), scopes=[VERTEX_SCOPE]
        ).with_quota_project(project_id.strip())
        credential.refresh(_HttpxAuthRequest())
    except (google_auth_exceptions.TransportError, httpx.RequestError) as exc:
        item["status"] = SETUP_STATUS_UNVERIFIED
        item["error"] = f"Authentication could not be verified: {type(exc).__name__}"
        return item
    except Exception as exc:
        item["error"] = f"Credential authentication failed: {type(exc).__name__}"
        return item
    item["status"] = SETUP_STATUS_VERIFIED
    return item


def inspect_gemini_setup() -> dict[str, Any]:
    """Return compact setup diagnostics without mutating credential rotation state."""
    source, manifest = _manifest_info()
    result: dict[str, Any] = {
        "ready": False,
        "status": SETUP_STATUS_INVALID,
        "credential_count": 0,
        "manifest_source": source,
        "manifest_path": str(manifest),
        "manifest_exists": manifest.exists(),
        "location": keys._resolve_vertex_location(),
        "next_action": None,
    }
    if not manifest.exists():
        result["next_action"] = "Set a valid Vertex credential manifest path."
        return _add_roots(result)
    try:
        entries = keys._load_manifest_entries(manifest)
    except json.JSONDecodeError:
        result["next_action"] = "Fix manifest JSON and run this check again."
        return _add_roots(result)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["next_action"] = "Fix the Vertex credential manifest and run this check again."
        return _add_roots(result)
    items = [
        _credential_entry(entry, index=index, manifest_path=manifest)
        for index, entry in enumerate(entries, start=1)
    ]
    result["credentials"] = items
    result["credential_count"] = len(items)
    statuses = {item["status"] for item in items}
    if SETUP_STATUS_INVALID in statuses:
        result["status"] = SETUP_STATUS_INVALID
        result["next_action"] = "Fix invalid Vertex credentials and run this check again."
    elif SETUP_STATUS_UNVERIFIED in statuses:
        result["status"] = SETUP_STATUS_UNVERIFIED
        result["next_action"] = "Retry credential verification when network access is available."
    else:
        result["ready"] = True
        result["status"] = SETUP_STATUS_VERIFIED
        result["next_action"] = "Gemini offload is ready."
    return _add_roots(result)


def _add_roots(result: dict[str, Any]) -> dict[str, Any]:
    configured_run_root = os.environ.get("GEMINI_OFFLOAD_RUN_DIR")
    run_root_configured = bool(configured_run_root and configured_run_root.strip())
    if run_root_configured:
        run_root = Path(configured_run_root).expanduser()
    else:
        run_root = Path(tempfile.gettempdir()) / "gemini-offload" / "runs"
    result["run_root"] = str(run_root.resolve(strict=False))
    result["run_root_temporary"] = not run_root_configured

    configured_output_root = os.environ.get("GEMINI_OFFLOAD_OUTPUT_DIR")
    if configured_output_root and configured_output_root.strip():
        output_root = Path(configured_output_root).expanduser()
    else:
        output_root = Path(tempfile.gettempdir()) / "gemini-offload" / "outputs"
    result["output_root"] = str(output_root.resolve(strict=False))

    if result.get("ready") and not run_root_configured:
        result["next_action"] = (
            "Gemini offload is ready; configure GEMINI_OFFLOAD_RUN_DIR for durable background runs."
        )
    return result


check_gemini_setup = inspect_gemini_setup
