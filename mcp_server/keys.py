"""Vertex AI credential loading and round-robin helpers for the MCP server.

Resolution order:
  1. Manifest file at env var `GEMINI_OFFLOAD_VERTEX_CREDENTIALS` or `VERTEX_AI_CREDENTIALS`
  2. `C:/Users/<user>/.secrets/vertex-ai/service-accounts/manifest.json`
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.oauth2 import service_account


ENV_VERTEX_CREDENTIALS = "GEMINI_OFFLOAD_VERTEX_CREDENTIALS"
ENV_SHARED_VERTEX_CREDENTIALS = "VERTEX_AI_CREDENTIALS"
ENV_VERTEX_LOCATION = "GOOGLE_CLOUD_LOCATION"
ENV_ALT_VERTEX_LOCATION = "VERTEX_AI_LOCATION"
ENV_SLOT_CONCURRENCY = "GEMINI_OFFLOAD_SLOT_CONCURRENCY"
DEFAULT_VERTEX_LOCATION = "global"
DEFAULT_VERTEX_MANIFEST = (
    Path.home() / ".secrets" / "vertex-ai" / "service-accounts" / "manifest.json"
)
VERTEX_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

_rotator_lock = threading.Lock()
_rotator: "VertexCredentialRotator | None" = None


DEFAULT_KEY_COOLDOWN_SECONDS = 60.0
DEFAULT_SLOT_CONCURRENCY = 1


@dataclass(frozen=True)
class VertexCredentialLease:
    """A selected Vertex AI service-account slot."""

    name: str
    project_id: str
    location: str
    path: Path
    credentials: service_account.Credentials


class NoAvailableQuotaSlotError(RuntimeError):
    """Raised when every Vertex quota slot for a model is cooling down."""

    def __init__(
        self,
        *,
        model: str,
        retry_after_seconds: float,
        quota_slots: list[str],
    ) -> None:
        self.model = model
        self.retry_after_seconds = retry_after_seconds
        self.quota_slots = quota_slots
        super().__init__(
            "No Vertex quota slot is currently available for "
            f"{model}; retry after about {retry_after_seconds:.1f} seconds."
        )


class AcquiredVertexCredentialLease:
    """Context manager for one in-flight Vertex quota slot."""

    def __init__(
        self,
        rotator: "VertexCredentialRotator",
        lease: VertexCredentialLease,
        quota_slot: str,
        model: str,
    ) -> None:
        self.lease = lease
        self.quota_slot = quota_slot
        self.model = model
        self._rotator = rotator
        self._released = False

    def __enter__(self) -> "AcquiredVertexCredentialLease":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()

    def mark_cooldown(self, seconds: float = DEFAULT_KEY_COOLDOWN_SECONDS) -> None:
        self._rotator.mark_quota_slot_cooldown(self.quota_slot, seconds)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._rotator.release_quota_slot(self.quota_slot)


class VertexCredentialRotator:
    """Thread-safe round-robin Vertex credential selector with quota-slot cooldowns."""

    def __init__(
        self,
        credentials: list[VertexCredentialLease],
        *,
        slot_concurrency: int | None = None,
    ):
        ordered_items = sorted(
            credentials,
            key=lambda item: (item.project_id, item.name),
        )
        if not ordered_items:
            raise ValueError("No Vertex AI credentials available.")

        self._ordered_credentials = ordered_items
        self._index = 0
        self._cooldowns: dict[str, float] = {}
        self._legacy_credential_cooldowns: dict[str, float] = {}
        self._in_flight: dict[str, int] = {}
        self._slot_concurrency = max(slot_concurrency or _resolve_slot_concurrency(), 1)
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)

    def next_lease(self) -> VertexCredentialLease:
        """Compatibility selector that only applies legacy credential cooldowns."""

        with self._lock:
            now = time.monotonic()
            credential_count = len(self._ordered_credentials)
            chosen_index: int | None = None

            for offset in range(credential_count):
                idx = (self._index + offset) % credential_count
                credential = self._ordered_credentials[idx]
                if self._legacy_credential_cooldowns.get(credential.name, 0.0) <= now:
                    chosen_index = idx
                    break

            if chosen_index is None:
                chosen_index = min(
                    range(credential_count),
                    key=lambda idx: self._legacy_credential_cooldowns.get(self._ordered_credentials[idx].name, 0.0),
                )

            credential = self._ordered_credentials[chosen_index]
            self._index = (chosen_index + 1) % credential_count
            return credential

    def acquire_lease(
        self,
        *,
        model: str,
        wait_for_cooldown: bool = False,
        max_wait_seconds: float = 0.0,
    ) -> AcquiredVertexCredentialLease:
        model_name = model.strip()
        if not model_name:
            raise ValueError("model must be a non-empty string.")

        deadline = time.monotonic() + max(max_wait_seconds, 0.0)
        with self._condition:
            while True:
                now = time.monotonic()
                chosen = self._find_available_credential(model_name, now)
                if chosen is not None:
                    chosen_index, credential, quota_slot = chosen
                    self._index = (chosen_index + 1) % len(self._ordered_credentials)
                    self._in_flight[quota_slot] = self._in_flight.get(quota_slot, 0) + 1
                    return AcquiredVertexCredentialLease(
                        self,
                        credential,
                        quota_slot,
                        model_name,
                    )

                if self._has_non_cooled_busy_slot(model_name, now):
                    self._condition.wait(timeout=0.1)
                    continue

                retry_after_seconds = self._retry_after_seconds(model_name, now)
                if not wait_for_cooldown:
                    raise NoAvailableQuotaSlotError(
                        model=model_name,
                        retry_after_seconds=retry_after_seconds,
                        quota_slots=self.quota_slots(model_name),
                    )

                remaining_wait = deadline - now
                if remaining_wait <= 0:
                    raise NoAvailableQuotaSlotError(
                        model=model_name,
                        retry_after_seconds=retry_after_seconds,
                        quota_slots=self.quota_slots(model_name),
                    )

                self._condition.wait(timeout=min(retry_after_seconds, remaining_wait))

    def key_count(self) -> int:
        return len(self._ordered_credentials)

    def quota_slot_count(self, model: str) -> int:
        return len(self.quota_slots(model))

    def quota_slots(self, model: str) -> list[str]:
        return sorted(
            {
                self._quota_slot_id(credential, model)
                for credential in self._ordered_credentials
            }
        )

    def mark_key_cooldown(
        self,
        key_name: str,
        seconds: float = DEFAULT_KEY_COOLDOWN_SECONDS,
    ) -> None:
        if not key_name:
            return
        with self._condition:
            self._legacy_credential_cooldowns[key_name] = max(
                self._legacy_credential_cooldowns.get(key_name, 0.0),
                time.monotonic() + max(seconds, 0.0),
            )
            self._condition.notify_all()

    def mark_quota_slot_cooldown(
        self,
        quota_slot: str,
        seconds: float = DEFAULT_KEY_COOLDOWN_SECONDS,
    ) -> None:
        if not quota_slot:
            return
        with self._condition:
            self._cooldowns[quota_slot] = max(
                self._cooldowns.get(quota_slot, 0.0),
                time.monotonic() + max(seconds, 0.0),
            )
            self._condition.notify_all()

    def release_quota_slot(self, quota_slot: str) -> None:
        if not quota_slot:
            return
        with self._condition:
            current = self._in_flight.get(quota_slot, 0)
            if current <= 1:
                self._in_flight.pop(quota_slot, None)
            else:
                self._in_flight[quota_slot] = current - 1
            self._condition.notify_all()

    def _find_available_credential(
        self,
        model: str,
        now: float,
    ) -> tuple[int, VertexCredentialLease, str] | None:
        credential_count = len(self._ordered_credentials)
        for offset in range(credential_count):
            idx = (self._index + offset) % credential_count
            credential = self._ordered_credentials[idx]
            quota_slot = self._quota_slot_id(credential, model)
            if self._cooldowns.get(quota_slot, 0.0) > now:
                continue
            if self._in_flight.get(quota_slot, 0) >= self._slot_concurrency:
                continue
            return idx, credential, quota_slot
        return None

    def _has_non_cooled_busy_slot(self, model: str, now: float) -> bool:
        for credential in self._ordered_credentials:
            quota_slot = self._quota_slot_id(credential, model)
            if self._cooldowns.get(quota_slot, 0.0) <= now:
                if self._in_flight.get(quota_slot, 0) >= self._slot_concurrency:
                    return True
        return False

    def _retry_after_seconds(self, model: str, now: float) -> float:
        remaining = [
            self._cooldowns.get(quota_slot, 0.0) - now
            for quota_slot in self.quota_slots(model)
        ]
        positive = [value for value in remaining if value > 0]
        if not positive:
            return 1.0
        return max(min(positive), 1.0)

    @staticmethod
    def _quota_slot_id(credential: VertexCredentialLease, model: str) -> str:
        return f"{credential.project_id}/{credential.location}/{model}"


def _resolve_slot_concurrency() -> int:
    raw_value = os.environ.get(ENV_SLOT_CONCURRENCY)
    if not raw_value or not raw_value.strip():
        return DEFAULT_SLOT_CONCURRENCY
    try:
        return max(int(raw_value), 1)
    except ValueError:
        return DEFAULT_SLOT_CONCURRENCY


def _resolve_vertex_location() -> str:
    for name in (ENV_VERTEX_LOCATION, ENV_ALT_VERTEX_LOCATION):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return DEFAULT_VERTEX_LOCATION


def _resolve_vertex_manifest() -> Path:
    for name in (ENV_VERTEX_CREDENTIALS, ENV_SHARED_VERTEX_CREDENTIALS):
        value = os.environ.get(name)
        if value and value.strip():
            path = Path(value).expanduser()
            if not path.exists():
                raise FileNotFoundError(f"{name} points to missing file: {path}")
            return path

    if DEFAULT_VERTEX_MANIFEST.exists():
        return DEFAULT_VERTEX_MANIFEST

    raise FileNotFoundError(
        "No Vertex AI credential manifest found. Provide "
        f"${ENV_VERTEX_CREDENTIALS}=<manifest.json> or ${ENV_SHARED_VERTEX_CREDENTIALS}=<manifest.json>."
    )


def _load_manifest_entries(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict) and isinstance(payload.get("credentials"), list):
        entries = payload["credentials"]
    else:
        raise ValueError("Vertex credential manifest must be a list or contain a credentials list")

    if not entries:
        raise ValueError(f"Vertex credential manifest is empty: {path}")
    return entries


def load_vertex_credentials() -> list[VertexCredentialLease]:
    """Load Vertex AI service-account credentials from a manifest."""

    manifest_path = _resolve_vertex_manifest()
    location = _resolve_vertex_location()
    credentials: list[VertexCredentialLease] = []

    for index, entry in enumerate(_load_manifest_entries(manifest_path), start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Vertex credential manifest entry #{index} must be an object")

        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError(f"Vertex credential manifest entry #{index} is missing path")

        key_path = Path(raw_path).expanduser()
        if not key_path.is_absolute():
            key_path = (manifest_path.parent / key_path).resolve()
        if not key_path.exists():
            raise FileNotFoundError(f"Vertex service account key not found: {key_path}")

        key_payload = json.loads(key_path.read_text(encoding="utf-8"))
        project_id = str(entry.get("project_id") or key_payload.get("project_id") or "").strip()
        if not project_id:
            raise ValueError(f"Vertex service account key has no project_id: {key_path}")

        creds = service_account.Credentials.from_service_account_file(
            str(key_path),
            scopes=[VERTEX_SCOPE],
        ).with_quota_project(project_id)
        credentials.append(
            VertexCredentialLease(
                name=str(entry.get("name") or entry.get("client_email") or key_payload.get("client_email") or f"vertex#{index}"),
                project_id=project_id,
                location=location,
                path=key_path,
                credentials=creds,
            )
        )

    return credentials


def get_key_rotator() -> VertexCredentialRotator:
    """Return the shared Vertex credential rotator for this process."""

    global _rotator
    with _rotator_lock:
        if _rotator is None:
            _rotator = VertexCredentialRotator(load_vertex_credentials())
        return _rotator


def get_next_api_key_lease() -> VertexCredentialLease:
    """Compatibility name: return the next Vertex credential lease."""

    return get_key_rotator().next_lease()


def acquire_vertex_credential_lease(
    *,
    model: str,
    wait_for_cooldown: bool = False,
    max_wait_seconds: float = 0.0,
) -> AcquiredVertexCredentialLease:
    """Acquire a Vertex credential for one project/location/model quota slot."""

    return get_key_rotator().acquire_lease(
        model=model,
        wait_for_cooldown=wait_for_cooldown,
        max_wait_seconds=max_wait_seconds,
    )


def mark_key_cooldown(
    key_name: str,
    seconds: float = DEFAULT_KEY_COOLDOWN_SECONDS,
) -> None:
    """Compatibility name: temporarily avoid a credential."""

    get_key_rotator().mark_key_cooldown(key_name, seconds)


def mark_quota_slot_cooldown(
    quota_slot: str,
    seconds: float = DEFAULT_KEY_COOLDOWN_SECONDS,
) -> None:
    """Temporarily avoid a Vertex project/location/model quota slot."""

    get_key_rotator().mark_quota_slot_cooldown(quota_slot, seconds)


def get_key_count() -> int:
    return get_key_rotator().key_count()


def get_quota_slot_count(model: str) -> int:
    return get_key_rotator().quota_slot_count(model)


# Backwards-compatible aliases for older tests and imports.
ApiKeyLease = VertexCredentialLease
ApiKeyRotator = VertexCredentialRotator
