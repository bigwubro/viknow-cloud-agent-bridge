"""Playground UI: static pages only; ViKnow API is a separate process (VIKNOW_PLAYGROUND_API_URL)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

sys.path.insert(0, str(ROOT / "tests"))
from pg_test_env import apply as _apply_test_pg

_apply_test_pg()

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

    def test_runtime_config_and_api_base_scripts(self) -> None:
        cfg = self.client.get("/playground/assets/runtime-config.js")
        self.assertEqual(cfg.status_code, 200)
        self.assertIn("VIKNOW_API_BASE", cfg.text)
        self.assertIn("5218", cfg.text)
        for page in ("/playground", "/playground/workspace", "/playground/agents"):
            html = self.client.get(page).text
            self.assertIn("runtime-config.js", html)
            self.assertIn("api-base.js", html)
        base = self.client.get("/playground/assets/api-base.js")
        self.assertEqual(base.status_code, 200)
        self.assertIn("viknowApiUrl", base.text)

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
        self.assertIn("viknowApiUrl", library_js.text)
        self.assertNotIn("viknow_playground_index_jobs", library_js.text)
        self.assertIn("未索引", library_js.text)
        self.assertIn("已索引", library_js.text)
        self.assertIn("失败", library_js.text)

    def test_access_api_not_mounted_on_ui(self) -> None:
        self.assertEqual(self.client.get("/api/v1/agent/agents").status_code, 404)
        self.assertEqual(self.client.post("/api/v1/users", json={"display_name": "x"}).status_code, 404)

    def test_removed_ui_bff_routes(self) -> None:
        self.assertEqual(self.client.post("/ui/playground/users", json={"display_name": "x"}).status_code, 404)
        self.assertEqual(self.client.post("/ui/playground/login", json={"user_id": "x"}).status_code, 404)
        self.assertEqual(
            self.client.post("/ui/playground/s3/presign-put", json={"library_id": "l", "file_name": "a.pdf"}).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
