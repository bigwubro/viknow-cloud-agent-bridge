"""Index-status API + catalog writeback (knowledge module only)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pg_test_env import apply as _apply_test_pg

_apply_test_pg()
os.environ.setdefault("VIKNOW_USE_SCRIPT_MODEL", "1")

import tempfile

from fastapi.testclient import TestClient

from viknow3.server import app


class IndexStatusApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        scratch = Path(tempfile.gettempdir()) / f"viknow3-index-status-{os.getpid()}"
        scratch.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("VIKNOW_SCRATCH_DIR", str(scratch))

        from viknow3.application.hyperrag import client as hyper_client
        from hyperrag_stub import ScriptHyperrag

        import viknow3.access.rag.service as rag_service

        cls._real_hyperag = hyper_client.HYPERAG
        cls._stub = ScriptHyperrag()
        hyper_client.HYPERAG = cls._stub
        rag_service.HYPERAG = cls._stub

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        from viknow3.application.hyperrag import client as hyper_client
        import viknow3.access.rag.service as rag_service

        hyper_client.HYPERAG = cls._real_hyperag
        rag_service.HYPERAG = cls._real_hyperag

    def setUp(self) -> None:
        self._stub.reset()

    def request(self, method: str, path: str, body: dict | None = None):
        headers = {"Authorization": "Bearer dev-app-key"}
        response = self.client.request(method, path, json=body, headers=headers)
        return response.status_code, response.json() if response.content else None

    def test_job_row_created_and_index_status_survives_refresh(self) -> None:
        lib = f"lib-idx-{uuid4().hex[:10]}"
        asset = f"a-idx-{uuid4().hex[:10]}"
        status, _ = self.request("POST", "/api/v1/knowledge/libraries", {"library_id": lib, "title": "t"})
        self.assertIn(status, (201, 409))
        asset_path = f"knowledge/assets/{asset}/upload/{asset}.mp4"
        with patch("viknow3.access.knowledge.service.s3.head_object", return_value={}):
            status, created = self.request(
                "POST",
                f"/api/v1/knowledge/libraries/{lib}/assets",
                {"asset_id": asset, "asset_path": asset_path, "title": "v", "modality": "video"},
            )
        self.assertEqual(status, 201, created)
        status_assets, assets = self.request("GET", f"/api/v1/knowledge/libraries/{lib}/assets?limit=100")
        self.assertEqual(status_assets, 200)
        existing = next((row for row in assets if row.get("asset_id") == asset), None)
        self.assertIsNotNone(existing, assets)
        asset_path = existing["asset_path"]

        from viknow3.application.hyperrag.object_gateway import install_object_store, reset_object_store

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
                {"library_id": lib, "asset_id": asset, "asset_path": asset_path},
            )
            self.assertEqual(status, 200, job)
            self.assertTrue(job.get("job_id"))

            status, latest = self.request(
                "GET",
                f"/api/v1/knowledge/libraries/{lib}/assets/{asset}/index-jobs/latest",
            )
            self.assertEqual(status, 200)
            self.assertEqual(latest["job_id"], job["job_id"])
            self.assertEqual(latest["status"], "pending")

            status, refreshing = self.request(
                "GET",
                f"/api/v1/knowledge/libraries/{lib}/assets/{asset}/index-status",
            )
            self.assertEqual(status, 200)
            self.assertEqual(refreshing["status"], "indexing")
            self.assertEqual(refreshing["job_id"], job["job_id"])

            status, done = self.request("GET", f"/api/v1/knowledge/index-jobs/{job['job_id']}")
            self.assertEqual(done["status"], "done")

            status, index_status = self.request(
                "GET",
                f"/api/v1/knowledge/libraries/{lib}/assets/{asset}/index-status",
            )
            self.assertEqual(index_status["status"], "indexed")
            self.assertEqual(index_status["current_index_version_id"], job["job_id"])

            status, batch = self.request(
                "GET",
                f"/api/v1/knowledge/libraries/{lib}/assets/index-status-batch",
            )
            self.assertEqual(status, 200)
            self.assertEqual(len(batch), 1)
            self.assertEqual(batch[0]["asset_id"], asset)
            self.assertEqual(batch[0]["status"], "indexed")
        finally:
            reset_object_store()

    def test_index_status_survives_after_job_poll(self) -> None:
        lib = f"lib-idx-{uuid4().hex[:10]}"
        asset = f"a-idx-{uuid4().hex[:10]}"
        status, _ = self.request("POST", "/api/v1/knowledge/libraries", {"library_id": lib, "title": "t"})
        self.assertIn(status, (201, 409))
        asset_path = f"knowledge/assets/{asset}/upload/{asset}.mp4"
        with patch("viknow3.access.knowledge.service.s3.head_object", return_value={}):
            status, created = self.request(
                "POST",
                f"/api/v1/knowledge/libraries/{lib}/assets",
                {"asset_id": asset, "asset_path": asset_path, "title": "v", "modality": "video"},
            )
        self.assertEqual(status, 201, created)
        status_assets, assets = self.request("GET", f"/api/v1/knowledge/libraries/{lib}/assets?limit=100")
        self.assertEqual(status_assets, 200)
        existing = next((row for row in assets if row.get("asset_id") == asset), None)
        self.assertIsNotNone(existing, assets)
        asset_path = existing["asset_path"]

        from viknow3.application.hyperrag.object_gateway import install_object_store, reset_object_store

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
                {"library_id": lib, "asset_id": asset, "asset_path": asset_path},
            )
            self.assertEqual(status, 200, job)
            status, done = self.request("GET", f"/api/v1/knowledge/index-jobs/{job['job_id']}")
            self.assertEqual(done["status"], "done")

            status, index_status = self.request(
                "GET",
                f"/api/v1/knowledge/libraries/{lib}/assets/{asset}/index-status",
            )
            self.assertEqual(status, 200)
            self.assertEqual(index_status["status"], "indexed")
            self.assertEqual(index_status["current_index_version_id"], job["job_id"])

            with patch("viknow3.access.knowledge.service.s3.head_object", return_value={}):
                status, assets = self.request("GET", f"/api/v1/knowledge/libraries/{lib}/assets")
            self.assertEqual(assets[0]["current_index_version_id"], job["job_id"])
        finally:
            reset_object_store()


if __name__ == "__main__":
    unittest.main()
