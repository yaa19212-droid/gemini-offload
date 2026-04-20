"""API key loading and round-robin helpers for the MCP server.

Resolution order:
  1. File at env var `GEMINI_OFFLOAD_KEYS` (if set)
  2. `./api_keys.json` in the current working directory
  3. Env vars `GEMINI_API_KEY` and/or `GOOGLE_API_KEY`
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path


ENV_KEYS_FILE = "GEMINI_OFFLOAD_KEYS"
ENV_VAR_NAMES = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
CWD_KEYS_FILENAME = "api_keys.json"

_rotator_lock = threading.Lock()
_rotator: "ApiKeyRotator | None" = None


class ApiKeyRotator:
    """Thread-safe round-robin API key selector."""

    def __init__(self, api_keys: dict[str, str]):
        ordered_items = sorted(
            (name, value.strip())
            for name, value in api_keys.items()
            if isinstance(name, str) and isinstance(value, str) and value.strip()
        )
        if not ordered_items:
            raise ValueError("No API keys available.")

        self._ordered_keys = ordered_items
        self._index = 0
        self._lock = threading.Lock()

    def next_key(self) -> str:
        with self._lock:
            _, api_key = self._ordered_keys[self._index]
            self._index = (self._index + 1) % len(self._ordered_keys)
            return api_key


def _load_from_env() -> dict[str, str]:
    return {
        name: value.strip()
        for name in ENV_VAR_NAMES
        if (value := os.environ.get(name)) and value.strip()
    }


def _parse_keys_file(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in API key file: {path}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object of name -> key strings.")

    api_keys = {
        name: value
        for name, value in payload.items()
        if isinstance(name, str) and isinstance(value, str) and value.strip()
    }
    if not api_keys:
        raise ValueError(f"{path} does not contain any non-empty API keys.")
    return api_keys


def _resolve_keys_file() -> Path | None:
    env_path = os.environ.get(ENV_KEYS_FILE)
    if env_path and env_path.strip():
        candidate = Path(env_path).expanduser()
        if not candidate.exists():
            raise FileNotFoundError(f"{ENV_KEYS_FILE} points to missing file: {candidate}")
        return candidate

    cwd_candidate = Path.cwd() / CWD_KEYS_FILENAME
    if cwd_candidate.exists():
        return cwd_candidate

    return None


def load_api_keys() -> dict[str, str]:
    """Load API keys from file if available, else fall back to env vars."""

    key_path = _resolve_keys_file()
    if key_path is not None:
        return _parse_keys_file(key_path)

    env_keys = _load_from_env()
    if env_keys:
        return env_keys

    raise ValueError(
        "No API keys available. Provide one of: "
        f"${ENV_KEYS_FILE}=<path>, ./{CWD_KEYS_FILENAME}, "
        f"or env vars {', '.join(ENV_VAR_NAMES)}."
    )


def get_key_rotator() -> ApiKeyRotator:
    """Return the shared API key rotator for this process."""

    global _rotator
    with _rotator_lock:
        if _rotator is None:
            _rotator = ApiKeyRotator(load_api_keys())
        return _rotator


def get_next_api_key() -> str:
    """Return the next API key using process-local round-robin selection."""

    return get_key_rotator().next_key()
