from __future__ import annotations

import json
import os
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server.keys import (
    NoAvailableQuotaSlotError,
    VertexCredentialLease,
    VertexCredentialRotator,
    _resolve_vertex_manifest,
)


class VertexCredentialTests(unittest.TestCase):
    def test_resolve_vertex_manifest_uses_env_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "manifest.json"
            path.write_text("[]", encoding="utf-8")
            old_env = os.environ.get("GEMINI_OFFLOAD_VERTEX_CREDENTIALS")
            try:
                os.environ["GEMINI_OFFLOAD_VERTEX_CREDENTIALS"] = str(path)
                self.assertEqual(_resolve_vertex_manifest(), path)
            finally:
                if old_env is None:
                    os.environ.pop("GEMINI_OFFLOAD_VERTEX_CREDENTIALS", None)
                else:
                    os.environ["GEMINI_OFFLOAD_VERTEX_CREDENTIALS"] = old_env

    def test_rotator_skips_credential_on_cooldown(self) -> None:
        credentials = [
            VertexCredentialLease("key1", "project1", "global", Path("key1.json"), object()),
            VertexCredentialLease("key2", "project2", "global", Path("key2.json"), object()),
        ]
        rotator = VertexCredentialRotator(credentials)

        first = rotator.next_lease()
        rotator.mark_key_cooldown(first.name, seconds=10)

        self.assertEqual(rotator.next_lease().name, "key2")

    def test_rotator_shares_cooldown_by_project_location_model_slot(self) -> None:
        credentials = [
            VertexCredentialLease("key1", "project1", "global", Path("key1.json"), object()),
            VertexCredentialLease("key2", "project1", "global", Path("key2.json"), object()),
        ]
        rotator = VertexCredentialRotator(credentials)

        with rotator.acquire_lease(model="gemini-3.5-flash") as acquired:
            acquired.mark_cooldown(seconds=10)

        with self.assertRaises(NoAvailableQuotaSlotError):
            rotator.acquire_lease(model="gemini-3.5-flash")

        other_model = rotator.acquire_lease(model="gemini-3.1-pro-preview")
        try:
            self.assertEqual(other_model.quota_slot, "project1/global/gemini-3.1-pro-preview")
        finally:
            other_model.release()

    def test_rotator_waits_for_same_slot_in_flight_limit(self) -> None:
        credentials = [
            VertexCredentialLease("key1", "project1", "global", Path("key1.json"), object()),
            VertexCredentialLease("key2", "project1", "global", Path("key2.json"), object()),
        ]
        rotator = VertexCredentialRotator(credentials, slot_concurrency=1)
        first = rotator.acquire_lease(model="gemini-3.5-flash")
        acquired_names: list[str] = []
        done = threading.Event()

        def acquire_second() -> None:
            with rotator.acquire_lease(model="gemini-3.5-flash") as second:
                acquired_names.append(second.lease.name)
            done.set()

        thread = threading.Thread(target=acquire_second)
        thread.start()
        try:
            time.sleep(0.05)
            self.assertFalse(done.is_set())
            first.release()
            self.assertTrue(done.wait(timeout=1.0))
            self.assertEqual(acquired_names, ["key2"])
        finally:
            first.release()
            thread.join(timeout=1.0)

    def test_manifest_loads_service_account_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            key_path = root / "key.json"
            key_path.write_text(
                json.dumps(
                    {
                        "type": "service_account",
                        "project_id": "project-test",
                        "client_email": "svc@example.iam.gserviceaccount.com",
                    }
                ),
                encoding="utf-8",
            )
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps([{"path": str(key_path)}]), encoding="utf-8")

            with patch("mcp_server.keys.service_account.Credentials.from_service_account_file") as loader:
                fake_credentials = unittest.mock.Mock()
                fake_credentials.with_quota_project.return_value = fake_credentials
                loader.return_value = fake_credentials
                with patch.dict(os.environ, {"GEMINI_OFFLOAD_VERTEX_CREDENTIALS": str(manifest_path)}, clear=False):
                    from mcp_server.keys import load_vertex_credentials

                    result = load_vertex_credentials()

            self.assertEqual(result[0].project_id, "project-test")

    def test_manifest_and_service_account_key_accept_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            key_path = root / "key.json"
            key_path.write_text(
                json.dumps(
                    {
                        "type": "service_account",
                        "project_id": "project-test",
                        "client_email": "svc@example.iam.gserviceaccount.com",
                    }
                ),
                encoding="utf-8-sig",
            )
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps([{"path": str(key_path)}]), encoding="utf-8-sig")

            with patch("mcp_server.keys.service_account.Credentials.from_service_account_file") as loader:
                fake_credentials = unittest.mock.Mock()
                fake_credentials.with_quota_project.return_value = fake_credentials
                loader.return_value = fake_credentials
                with patch.dict(os.environ, {"GEMINI_OFFLOAD_VERTEX_CREDENTIALS": str(manifest_path)}, clear=False):
                    from mcp_server.keys import load_vertex_credentials

                    result = load_vertex_credentials()

            self.assertEqual(result[0].project_id, "project-test")
