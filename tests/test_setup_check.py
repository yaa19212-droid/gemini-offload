from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from google.auth import exceptions as google_auth_exceptions

from mcp_server import setup_check


class SetupCheckTests(unittest.TestCase):
    def _manifest_with_key(self, root: Path, key_payload: dict, *, entry: dict | None = None) -> Path:
        key_path = root / "vertex-key.json"
        key_path.write_text(json.dumps(key_payload), encoding="utf-8")
        manifest_path = root / "manifest.json"
        manifest_entry = {"path": key_path.name}
        if entry:
            manifest_entry.update(entry)
        manifest_path.write_text(json.dumps([manifest_entry]), encoding="utf-8")
        return manifest_path

    def _fake_credentials(self, *, refresh_error: Exception | None = None) -> Mock:
        credentials = Mock()
        credentials.with_quota_project.return_value = credentials
        if refresh_error is not None:
            credentials.refresh.side_effect = refresh_error
        return credentials

    def test_missing_manifest_returns_invalid_with_next_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing-manifest.json"
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(setup_check, "DEFAULT_VERTEX_MANIFEST", missing),
            ):
                result = setup_check.inspect_gemini_setup()

        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], setup_check.SETUP_STATUS_INVALID)
        self.assertEqual(result["credential_count"], 0)
        self.assertIn("manifest", result["next_action"].lower())

    def test_malformed_manifest_returns_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = Path(temp_dir) / "manifest.json"
            manifest.write_text("{not-json", encoding="utf-8")
            with patch.dict(os.environ, {setup_check.ENV_VERTEX_CREDENTIALS: str(manifest)}, clear=True):
                result = setup_check.inspect_gemini_setup()

        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], setup_check.SETUP_STATUS_INVALID)
        self.assertIn("Fix manifest JSON", result["next_action"])

    def test_missing_key_file_is_invalid_without_path_leak(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps([{"path": "missing-key.json"}]), encoding="utf-8")
            with patch.dict(os.environ, {setup_check.ENV_VERTEX_CREDENTIALS: str(manifest)}, clear=True):
                result = setup_check.inspect_gemini_setup()

        item = result["credentials"][0]
        self.assertEqual(item["status"], setup_check.SETUP_STATUS_INVALID)
        self.assertIn("missing-key.json", item["error"])
        self.assertNotIn(str(root), item["error"])

    def test_invalid_credential_json_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            key = root / "vertex-key.json"
            key.write_text("{bad-json", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps([{"path": key.name}]), encoding="utf-8")
            with patch.dict(os.environ, {setup_check.ENV_VERTEX_CREDENTIALS: str(manifest)}, clear=True):
                result = setup_check.inspect_gemini_setup()

        self.assertEqual(result["credentials"][0]["status"], setup_check.SETUP_STATUS_INVALID)
        self.assertIn("Credential JSON is invalid", result["credentials"][0]["error"])

    def test_verified_refresh_uses_vertex_scope_and_does_not_leak_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = self._manifest_with_key(
                root,
                {
                    "type": "service_account",
                    "project_id": "project-a",
                    "client_email": "private-account@example.invalid",
                    "private_key": "SUPER-SECRET-PRIVATE-KEY",
                },
                entry={"client_email": "manifest-secret@example.invalid"},
            )
            credentials = self._fake_credentials()
            credentials.token = "ya29.SUPER-SECRET-TOKEN"
            with (
                patch.dict(os.environ, {setup_check.ENV_VERTEX_CREDENTIALS: str(manifest)}, clear=True),
                patch.object(
                    setup_check.service_account.Credentials,
                    "from_service_account_file",
                    return_value=credentials,
                ) as loader,
            ):
                result = setup_check.inspect_gemini_setup()

        loader.assert_called_once_with(str(root / "vertex-key.json"), scopes=[setup_check.VERTEX_SCOPE])
        credentials.with_quota_project.assert_called_once_with("project-a")
        credentials.refresh.assert_called_once()
        self.assertTrue(result["ready"])
        self.assertEqual(result["status"], setup_check.SETUP_STATUS_VERIFIED)

    def test_oauth_transport_caps_requested_timeout(self) -> None:
        response = unittest.mock.Mock()
        response.status_code = 200
        response.headers = {}
        response.content = b"{}"
        with patch.object(setup_check.httpx, "request", return_value=response) as request:
            setup_check._HttpxAuthRequest()(
                "https://oauth2.googleapis.com/token",
                method="POST",
                timeout=120,
            )

        self.assertEqual(request.call_args.kwargs["timeout"], setup_check.OAUTH_REFRESH_TIMEOUT_SECONDS)

    def test_oauth_transient_http_failure_is_transport_error(self) -> None:
        response = unittest.mock.Mock()
        response.status_code = 503
        with patch.object(setup_check.httpx, "request", return_value=response):
            with self.assertRaises(google_auth_exceptions.TransportError):
                setup_check._HttpxAuthRequest()("https://oauth2.googleapis.com/token")

    def test_network_failure_is_unverified_not_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = self._manifest_with_key(
                root,
                {"type": "service_account", "project_id": "project-a"},
            )
            credentials = self._fake_credentials(
                refresh_error=google_auth_exceptions.TransportError("offline")
            )
            with (
                patch.dict(os.environ, {setup_check.ENV_VERTEX_CREDENTIALS: str(manifest)}, clear=True),
                patch.object(
                    setup_check.service_account.Credentials,
                    "from_service_account_file",
                    return_value=credentials,
                ),
            ):
                result = setup_check.inspect_gemini_setup()

        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], setup_check.SETUP_STATUS_UNVERIFIED)
        self.assertEqual(
            result["credentials"][0]["status"],
            setup_check.SETUP_STATUS_UNVERIFIED,
        )

    def test_known_invalid_status_takes_precedence_over_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            good_key = root / "good.json"
            good_key.write_text(
                json.dumps({"project_id": "project-a", "private_key": "secret"}),
                encoding="utf-8",
            )
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps([{ "path": "good.json" }, { "path": "missing.json" }]),
                encoding="utf-8",
            )
            credentials = self._fake_credentials()
            credentials.refresh.side_effect = google_auth_exceptions.TransportError("offline")
            with (
                patch.dict(os.environ, {setup_check.ENV_VERTEX_CREDENTIALS: str(manifest)}, clear=True),
                patch.object(
                    setup_check.service_account.Credentials,
                    "from_service_account_file",
                    return_value=credentials,
                ),
            ):
                result = setup_check.inspect_gemini_setup()

        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], setup_check.SETUP_STATUS_INVALID)
        self.assertEqual(
            [item["status"] for item in result["credentials"]],
            [setup_check.SETUP_STATUS_UNVERIFIED, setup_check.SETUP_STATUS_INVALID],
        )

    def test_setup_result_does_not_expose_private_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = self._manifest_with_key(
                root,
                {
                    "type": "service_account",
                    "project_id": "project-a",
                    "private_key": "PRIVATE",
                    "client_email": "secret@example.invalid",
                },
            )
            with patch.dict(os.environ, {setup_check.ENV_VERTEX_CREDENTIALS: str(manifest)}, clear=True):
                result = setup_check.inspect_gemini_setup()

        dumped = json.dumps(result)
        self.assertNotIn("PRIVATE", dumped)
        self.assertNotIn("secret@example.invalid", dumped)
        self.assertNotIn("private_key", dumped)

    def test_ready_setup_warns_when_run_root_is_temporary(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = setup_check._add_roots({"ready": True, "next_action": "Gemini offload is ready."})
        self.assertTrue(result["run_root_temporary"])
        self.assertEqual(
            Path(result["run_root"]),
            (Path(tempfile.gettempdir()) / "gemini-offload" / "runs").resolve(),
        )
        self.assertEqual(
            Path(result["output_root"]),
            (Path(tempfile.gettempdir()) / "gemini-offload" / "outputs").resolve(),
        )
        self.assertIn("GEMINI_OFFLOAD_RUN_DIR", result["next_action"])


if __name__ == "__main__":
    unittest.main()
