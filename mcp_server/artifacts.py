from __future__ import annotations

import datetime
import hashlib
import os
import pathlib
import re
import tempfile
from typing import Any


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def atomic_write_bytes(path: pathlib.Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temp_path = pathlib.Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        descriptor = -1
        os.replace(temp_path, path)
    finally:
        if descriptor != -1:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def atomic_write_text(path: pathlib.Path, payload: str) -> None:
    atomic_write_bytes(path, payload.encode("utf-8"))


def artifact_metadata(path: pathlib.Path, *, role: str, managed: bool) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"artifact is not a regular file: {resolved}")
    digest = hashlib.sha256()
    byte_count = 0
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            byte_count += len(chunk)
    return {
        "role": role,
        "path": str(resolved),
        "byte_count": byte_count,
        "sha256": digest.hexdigest(),
        "managed": managed,
        "created_at": utc_now(),
    }


def verify_recorded_artifacts(
    run_dir: pathlib.Path, artifacts: list[dict[str, Any]]
) -> tuple[bool, str]:
    if not artifacts:
        return False, "missing artifact integrity metadata"
    outputs_root = (run_dir / "outputs").resolve(strict=False)
    for artifact in artifacts:
        try:
            path = pathlib.Path(str(artifact["path"])).resolve(strict=True)
        except (FileNotFoundError, OSError, KeyError, TypeError, ValueError) as exc:
            return False, f"artifact unavailable: {exc}"
        if not path.is_file():
            return False, f"artifact is not a regular file: {path}"
        if artifact.get("managed") and path.parent != outputs_root:
            return False, f"managed artifact escaped outputs directory: {path}"
        try:
            current = artifact_metadata(
                path,
                role=str(artifact.get("role", "artifact")),
                managed=bool(artifact.get("managed")),
            )
        except Exception as exc:
            return False, f"artifact verification failed: {exc}"
        if current["byte_count"] != artifact.get("byte_count"):
            return False, f"artifact size mismatch: {path}"
        if current["sha256"] != artifact.get("sha256"):
            return False, f"artifact checksum mismatch: {path}"
    return True, "verified"


def collect_item_artifacts(item: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    managed = bool(item["request"].get("output_managed"))
    artifacts: list[dict[str, Any]] = []
    output_path = result.get("output_path") or item["request"].get("output_path")
    if isinstance(output_path, str) and output_path:
        artifacts.append(artifact_metadata(pathlib.Path(output_path), role="output", managed=managed))
    for image in result.get("images", []) or []:
        if not isinstance(image, dict):
            continue
        image_path = image.get("output_path")
        if isinstance(image_path, str) and image_path:
            artifacts.append(
                artifact_metadata(
                    pathlib.Path(image_path),
                    role=f"image:{image.get('index', len(artifacts))}",
                    managed=managed,
                )
            )
    return artifacts


RUN_ID_RE = re.compile(r"^run-[A-Za-z0-9][A-Za-z0-9._-]{0,122}$")


def validate_run_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("run_id must be a non-empty string.")
    run_id = value.strip()
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError(
            "run_id must be a managed run identifier beginning with 'run-' and containing "
            "only letters, numbers, '.', '_', or '-'."
        )
    return run_id


def validate_managed_run_dir(value: Any, run_root: pathlib.Path) -> pathlib.Path:
    path_value = str(value)
    path_obj = pathlib.Path(path_value)
    if not path_obj.is_absolute():
        raise ValueError(f"run_dir must be absolute: {value}")
    candidate = path_obj.resolve(strict=False)
    validate_run_id(candidate.name)
    root = run_root.resolve(strict=False)
    if candidate.parent != root:
        raise ValueError(f"run_dir must be an immediate child of {root}: {value}")
    return candidate


def managed_output_path(
    run_dir: pathlib.Path,
    storage_key: str,
    extension: str,
    run_root: pathlib.Path,
) -> pathlib.Path:
    managed_run_dir = validate_managed_run_dir(run_dir, run_root)
    outputs_root = (managed_run_dir / "outputs").resolve(strict=False)
    if outputs_root.parent != managed_run_dir:
        raise ValueError(f"Managed outputs directory escapes run_dir: {outputs_root}")
    suffix = extension if extension.startswith(".") else f".{extension}"
    candidate = (outputs_root / f"{storage_key}{suffix}").resolve(strict=False)
    if candidate.parent != outputs_root:
        raise ValueError(f"Managed output path escapes outputs directory: {candidate}")
    return candidate
