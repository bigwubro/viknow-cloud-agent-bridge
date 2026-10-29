"""Access-layer API, one module at a time."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pg_test_env import apply as _apply_test_pg

_apply_test_pg()
os.environ.setdefault("VIKNOW_USE_SCRIPT_MODEL", "1")

from fastapi.testclient import TestClient

from viknow_platform.server import app


_REAL_HYPERAG = None


def setUpModule() -> None:
    import tempfile
    from pathlib import Path

    scratch = Path(tempfile.gettempdir()) / f"viknow-platform-scratch-{os.getpid()}"
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("VIKNOW_SCRATCH_DIR", str(scratch))

    from viknow_platform.application.agentcore.runtime.checkpoint import reset_agent_tables
    from viknow_platform.application.hyperrag import client as hyper_client
    from viknow_platform.store import STORE

    from hyperrag_stub import ScriptHyperrag

    global _REAL_HYPERAG
    _REAL_HYPERAG = hyper_client.HYPERAG
    stub = ScriptHyperrag()
    hyper_client.HYPERAG = stub
    import viknow_platform.access.rag.service as rag_service

    rag_service.HYPERAG = stub
    STORE.reset()
    stub.reset()
    reset_agent_tables()


def tearDownModule() -> None:
    from viknow_platform.application.hyperrag import client as hyper_client
    import viknow_platform.access.rag.service as rag_service

    global _REAL_HYPERAG
    if _REAL_HYPERAG is not None:
        hyper_client.HYPERAG = _REAL_HYPERAG
        rag_service.HYPERAG = _REAL_HYPERAG




class AccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def request(self, method: str, path: str, body: dict | None = None, key: str = "dev-app-key"):
        headers = {"Authorization": f"Bearer {key}"}
        response = self.client.request(method, path, json=body, headers=headers)
        if response.status_code == 204 or not response.content:
            return response.status_code, None
        return response.status_code, response.json()

    def test_modules(self) -> None:
        status, user = self.request("POST", "/api/v1/users", {"display_name": "张三"})
        self.assertEqual(status, 200)
        self.assertEqual(user["display_name"], "张三")

        status, _ = self.request("GET", "/api/v1/agent/profiles", key="nope")
        self.assertEqual(status, 401)

        status, profiles = self.request("GET", "/api/v1/agent/profiles")
        self.assertEqual(profiles["default_profile"], "deepagents")
        self.assertEqual([item["id"] for item in profiles["profiles"]], ["deepagents", "adk"])

        status, agents = self.request("GET", "/api/v1/agent/agents")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in agents], ["deepagents", "adk"])

        status, _ = self.request("GET", "/api/v1/agent/profiles/deepagents", key="nope")
        self.assertEqual(status, 401)

        status, no_lib = self.request(
            "POST",
            "/api/v1/agent/runs",
            {"user_id": user["id"], "message": "你好", "profile_id": "deepagents"},
        )
        self.assertEqual(status, 422)

        status, run = self.request(
            "POST",
            "/api/v1/agent/runs",
            {"user_id": user["id"], "message": "你好", "library_ids": ["library-1"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(run["status"], "running")

        status, busy = self.request(
            "POST",
            "/api/v1/agent/runs",
            {"user_id": user["id"], "message": "第二句", "session_id": run["session_id"]},
        )
        self.assertEqual(status, 409)
        self.assertEqual(busy["code"], "session_busy")

        status, allowed = self.request(
            "GET",
            f"/api/v1/agent/permissions?user_id={user['id']}&library_id=library-1",
        )
        self.assertTrue(allowed["allowed"])

        raw = self.client.get(
            f"/api/v1/agent/runs/{run['run_id']}/events",
            headers={"Authorization": "Bearer dev-app-key"},
        ).text
        self.assertIn("event: run_completed", raw)
        self.assertIn("tool_call_started", raw)
        self.assertIn("knowledge_discover", raw)
        self.assertIn("message_delta", raw)

        status, again = self.request(
            "POST",
            "/api/v1/agent/runs",
            {"user_id": user["id"], "message": "继续", "session_id": run["session_id"]},
        )
        self.assertEqual(status, 200, again)
        status, interrupted = self.request("POST", f"/api/v1/agent/runs/{again['run_id']}/interrupt", {})
        self.assertEqual(status, 200)

        status, missing = self.request("POST", "/api/v1/knowledge/search", {"library_id": "nope", "query": "步骤"})
        self.assertEqual(status, 200)
        self.assertEqual(missing["status"], "error")
        self.assertEqual(missing["code"], "unknown_library")

        status, library = self.request(
            "POST",
            "/api/v1/knowledge/libraries",
            {"library_id": "library-1", "title": "库"},
        )
        self.assertIn(status, (201, 409), library)
        asset_path = "knowledge/assets/asset-1/upload/asset-1.mp4"
        with patch("viknow_platform.access.knowledge.storage_bridge.asset_exists_in_storage", return_value=None):
            status, asset = self.request(
                "POST",
                "/api/v1/knowledge/libraries/library-1/assets",
                {
                    "asset_id": "asset-1",
                    "asset_path": asset_path,
                    "title": "动作演示.mp4",
                    "modality": "video",
                },
            )
        self.assertIn(status, (201, 409), asset)
        from viknow_platform.service.hyperrag.object_gateway import IndexObject

        local = Path(os.environ["VIKNOW_SCRATCH_DIR"]) / "access-test-asset.mp4"
        local.write_bytes(b"video")
        fetched = IndexObject(asset_path, local)
        with patch("viknow_platform.access.rag.service.fetch_index_asset", return_value=fetched):
            status, job = self.request(
                "POST",
                "/api/v1/knowledge/index-jobs",
                {
                    "library_id": "library-1",
                    "asset_id": "asset-1",
                    "asset_path": asset_path,
                    "compile_mode": "fast_ingest",
                    "params": {"title": "动作演示.mp4"},
                },
            )
            self.assertEqual(job["status"], "pending")
            self.assertIsNone(job["metadata"])
            status, done = self.request("GET", f"/api/v1/knowledge/index-jobs/{job['job_id']}")
            self.assertEqual(done["status"], "done")
            self.assertEqual(done["metadata"]["asset_type"], "video")
            self.assertIn("progress", done)
            self.assertEqual(done["progress"]["overall"], 1.0)
            status, index_status = self.request(
                "GET",
                "/api/v1/knowledge/libraries/library-1/assets/asset-1/index-status",
            )
            self.assertEqual(status, 200)
            self.assertEqual(index_status["status"], "indexed")
            self.assertEqual(index_status["current_index_version_id"], job["job_id"])
            with patch("viknow_platform.access.knowledge.storage_bridge.asset_exists_in_storage", return_value=None):
                status, assets = self.request("GET", "/api/v1/knowledge/libraries/library-1/assets")
            self.assertEqual(status, 200)
            self.assertEqual(assets[0]["current_index_version_id"], job["job_id"])

        status, learning_body = self.request("GET", "/api/v1/learning")
        self.assertEqual(learning_body["module"], "self-learning")

        status, _om = self.request("GET", "/api/v1/om/config")
        self.assertEqual(status, 401)
        status, config = self.request("GET", "/api/v1/om/config", key="dev-admin-key")
        self.assertIn("VIKNOW_LANGFUSE_ENABLED", config["switches"])
        status, backup = self.request("POST", "/api/v1/om/backups", {}, key="dev-admin-key")
        self.assertIn("backup_id", backup)


if __name__ == "__main__":
    unittest.main()
