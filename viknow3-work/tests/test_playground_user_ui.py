"""Playground UI: static pages + /api/v1/* mount (no /ui/playground business BFF)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

sys.path.insert(0, str(ROOT / "tests"))
from pg_test_env import apply as _apply_test_pg

_apply_test_pg()
os.environ.setdefault("VIKNOW3_APP_KEY", "dev-app-key")

from fastapi.testclient import TestClient

from ui import server as ui_server


class PlaygroundUserUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(ui_server.app)

    def test_playground_pages(self) -> None:
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 302)
        self.assertEqual(self.client.get("/playground").status_code, 200)
        self.assertEqual(self.client.get("/playground/workspace").status_code, 200)
        self.assertEqual(self.client.get("/playground/agents").status_code, 200)
        self.assertEqual(self.client.get("/playground/agent/demo-agent").status_code, 200)

    def test_library_detail_page_index_actions(self) -> None:
        response = self.client.get("/playground/library/demo-lib")
        self.assertEqual(response.status_code, 200)
        text = response.text
        self.assertIn("index-progress-modal", text)
        self.assertIn("index-compile-mode", text)
        self.assertIn("/playground/assets/library.js", text)
        library_js = self.client.get("/playground/assets/library.js")
        self.assertEqual(library_js.status_code, 200)
        self.assertIn("建索引", library_js.text)
        self.assertIn("index-jobs", library_js.text)
        self.assertIn("index-status-batch", library_js.text)
        self.assertNotIn("viknow_playground_index_jobs", library_js.text)
        self.assertIn("未索引", library_js.text)
        self.assertIn("已索引", library_js.text)
        self.assertIn("失败", library_js.text)
        self.assertIn('kind === "failed"', library_js.text)
        self.assertIn("isIndexJobFailed", library_js.text)
        self.assertNotIn("查看进度", library_js.text)
        self.assertNotIn("查看索引状态", library_js.text)

    @mock.patch("viknow3.storage.s3.put_object_stream", return_value="knowledge/assets/a1/upload/a1.normalized.mp4")
    @mock.patch("viknow3.access.knowledge.store.get_library", return_value={"library_id": "demo-lib"})
    def test_direct_library_upload_route(self, _lib: mock.Mock, _put: mock.Mock) -> None:
        response = self.client.post(
            "/api/v1/knowledge/libraries/demo-lib/uploads/direct",
            files={"file": ("clip.mp4", b"fake-video", "video/mp4")},
            headers={"X-App-Key": "dev-app-key"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["asset_id"], body["asset_path"].split("/")[2])
        self.assertIn(".normalized.mp4", body["asset_path"])

    def test_legacy_bff_removed(self) -> None:
        self.assertEqual(self.client.post("/ui/playground/users", json={"display_name": "x"}).status_code, 404)
        self.assertEqual(self.client.post("/ui/playground/login", json={"user_id": "x"}).status_code, 404)
        self.assertEqual(
            self.client.post("/ui/playground/s3/presign-put", json={"library_id": "l", "file_name": "a.pdf"}).status_code,
            404,
        )

    @mock.patch("viknow3.access.user.service.create_user")
    def test_users_api_mounted(self, create_user: mock.Mock) -> None:
        create_user.return_value = {
            "id": "usr_test",
            "display_name": "Demo",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "memory_enabled": False,
        }
        response = self.client.post("/api/v1/users", json={"display_name": "Demo"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], "usr_test")

    def test_agent_list_api(self) -> None:
        response = self.client.get("/api/v1/agent/agents")
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json(), list)

    def test_index_jobs_api_mounted(self) -> None:
        response = self.client.post("/api/v1/knowledge/index-jobs", json={})
        self.assertNotEqual(response.status_code, 404)

    @mock.patch("viknow3.access.agent.service.session_runs")
    def test_nested_session_runs_route(self, session_runs: mock.Mock) -> None:
        session_runs.return_value = [{"id": "run-1", "session_id": "sess-1", "status": "completed"}]
        response = self.client.get("/api/v1/agent/agents/expertbuddy-deepagents/sessions/sess-1/runs")
        self.assertEqual(response.status_code, 200)
        session_runs.assert_called_once()
        self.assertEqual(response.json()[0]["id"], "run-1")


if __name__ == "__main__":
    unittest.main()
