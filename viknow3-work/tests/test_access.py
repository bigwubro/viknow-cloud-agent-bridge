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

from viknow3.server import app


_REAL_HYPERAG = None


def setUpModule() -> None:
    import tempfile
    from pathlib import Path

    scratch = Path(tempfile.gettempdir()) / f"viknow3-scratch-{os.getpid()}"
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("VIKNOW_SCRATCH_DIR", str(scratch))

    from viknow3.agents.runtimes.deepagents.agent_adapters.checkpointer import reset_checkpoint_tables
    from viknow3.pg import AGENT, dsn, require_test_database
    from viknow3.service.hyperrag import client as hyper_client
    from viknow3.store import STORE

    from hyperrag_stub import ScriptHyperrag

    global _REAL_HYPERAG
    _REAL_HYPERAG = hyper_client.HYPERAG
    stub = ScriptHyperrag()
    hyper_client.HYPERAG = stub
    import viknow3.access.knowledge.service as rag_service

    rag_service.HYPERAG = stub
    STORE.reset()
    stub.reset()
    require_test_database(AGENT)
    reset_checkpoint_tables(dsn(AGENT))


def tearDownModule() -> None:
    from viknow3.service.hyperrag import client as hyper_client
    import viknow3.access.knowledge.service as rag_service

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
        self.assertEqual(profiles["default_profile"], "expertbuddy-deepagents")
        from viknow3.agents.layout import discover_agents

        expected_ids = [public for package in discover_agents() for public in package.public_ids()]
        self.assertEqual([item["id"] for item in profiles["profiles"]], expected_ids)
        self.assertIn("expertbuddy-deepagents", expected_ids)

        status, agents = self.request("GET", "/api/v1/agent/agents")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in agents], expected_ids)

        status, _ = self.request("GET", "/api/v1/agent/profiles/deepagents", key="nope")
        self.assertEqual(status, 401)

        agent = "/api/v1/agent/agents/expertbuddy-deepagents"
        status, no_lib = self.request(
            "POST",
            f"{agent}/sessions",
            {"user_id": user["id"], "message": "你好"},
        )
        self.assertEqual(status, 422)

        status, run = self.request(
            "POST",
            f"{agent}/sessions",
            {"user_id": user["id"], "message": "你好", "library_ids": ["library-1"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(run["agent_id"], "expertbuddy-deepagents")

        status, busy = self.request(
            "POST",
            f"{agent}/sessions/{run['session_id']}/runs",
            {"user_id": user["id"], "message": "第二句"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(busy["code"], "session_busy")

        status, allowed = self.request(
            "GET",
            f"/api/v1/agent/permissions?user_id={user['id']}&library_id=library-1",
        )
        self.assertTrue(allowed["allowed"])

        raw = self.client.get(
            f"{agent}/sessions/{run['session_id']}/runs/{run['run_id']}/events",
            headers={"Authorization": "Bearer dev-app-key"},
        ).text
        self.assertIn("event: run_completed", raw)
        self.assertIn("tool_call_started", raw)
        self.assertIn("knowledge_discover", raw)
        self.assertIn("message_delta", raw)

        status, again = self.request(
            "POST",
            f"{agent}/sessions/{run['session_id']}/runs",
            {"user_id": user["id"], "message": "继续"},
        )
        self.assertEqual(status, 200, again)
        status, interrupted = self.request(
            "POST",
            f"{agent}/sessions/{again['session_id']}/runs/{again['run_id']}/interrupt",
            {},
        )
        self.assertEqual(status, 200)
        self.assertEqual(interrupted["run_id"], again["run_id"])
        self.assertEqual(interrupted["session_id"], again["session_id"])

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
        with patch("viknow3.access.knowledge.service.s3.head_object", return_value={}):
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
        from pathlib import Path

        from viknow3.service.hyperrag.object_gateway import install_object_store, reset_object_store

        class _Objects:
            def exists(self, key: str) -> bool:
                return key == asset_path

            def get_to_path(self, key: str, path: Path) -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"video")

            def put_bytes(self, key: str, payload: bytes, *, content_type: str = "application/octet-stream") -> None:
                del key, payload, content_type

        install_object_store(_Objects())
        try:
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
            with patch("viknow3.access.knowledge.service.s3.head_object", return_value={}):
                status, assets = self.request("GET", "/api/v1/knowledge/libraries/library-1/assets")
            self.assertEqual(status, 200)
            self.assertEqual(assets[0]["current_index_version_id"], job["job_id"])
        finally:
            reset_object_store()

        status, _om = self.request("GET", "/api/v1/om/config")
        self.assertEqual(status, 401)
        status, config = self.request("GET", "/api/v1/om/config", key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertIn("observe_setting", config)
        self.assertIn("model", config)
        status, changes = self.request("GET", "/api/v1/om/config/changes", key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertIn("changes", changes)


if __name__ == "__main__":
    unittest.main()
