"""Index-status API + catalog writeback (storage-backed index jobs)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pg_test_env import apply as _apply_test_pg

_apply_test_pg()
os.environ.setdefault("VIKNOW_USE_SCRIPT_MODEL", "1")

from fastapi.testclient import TestClient

from viknow3.server import app


class IndexStatusApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        scratch = Path(tempfile.gettempdir()) / f"viknow3-index-status-{os.getpid()}"
        scratch.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("VIKNOW_SCRATCH_DIR", str(scratch))

        from viknow3.service.hyperrag import client as hyper_client
        from hyperrag_stub import ScriptHyperrag

        import viknow3.access.knowledge.service as rag_service

        cls._real_hyperag = hyper_client.HYPERAG
        cls._stub = ScriptHyperrag()
        hyper_client.HYPERAG = cls._stub
        rag_service.HYPERAG = cls._stub

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        from viknow3.service.hyperrag import client as hyper_client
        import viknow3.access.knowledge.service as rag_service

        hyper_client.HYPERAG = cls._real_hyperag
        rag_service.HYPERAG = cls._real_hyperag

    def setUp(self) -> None:
        self._stub.reset()

    def request(self, method: str, path: str, body: dict | None = None):
        headers = {"Authorization": "Bearer dev-app-key"}
        response = self.client.request(method, path, json=body, headers=headers)
        return response.status_code, response.json() if response.content else None

    def _storage_client(self, lib: str, asset: str):
        class _StorageClient:
            def asset_exists(self, library_id: str, asset_id: str) -> bool:
                return library_id == lib and asset_id == asset

            def get_asset(self, library_id: str, asset_id: str) -> dict:
                return {"title": "v", "modality": "video"}

            def list_assets(self, library_id: str, *, limit: int | None = None):
                return [{"asset_id": asset}] if library_id == lib else []

            def get_library(self, library_id: str) -> dict:
                return {"library_id": library_id}

        return _StorageClient()

    def _run_index_flow(self, lib: str, asset: str) -> dict:
        from viknow3.service.hyperrag.object_gateway import IndexObject, install_object_store, reset_object_store

        class _Objects:
            def exists(self, key: str) -> bool:
                return True

            def get_to_path(self, key: str, path: Path) -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"video")

            def put_bytes(self, key: str, payload: bytes, *, content_type: str = "application/octet-stream") -> None:
                del key, payload, content_type

        install_object_store(_Objects())
        client = self._storage_client(lib, asset)
        try:
            with (
                patch("viknow3.access.knowledge.service.storage_configured", return_value=True),
                patch("viknow3.access.knowledge.service.get_storage_client", return_value=client),
                patch("viknow3.access.knowledge.index_status.storage_configured", return_value=True),
                patch("viknow3.access.knowledge.index_status.get_storage_client", return_value=client),
                patch(
                    "viknow3.service.hyperrag.object_gateway.fetch_index_asset",
                    return_value=IndexObject(f"storage://{lib}/{asset}", Path(f"/tmp/{asset}.mp4")),
                ),
            ):
                status, job = self.request(
                    "POST",
                    "/api/v1/knowledge/index-jobs",
                    {"library_id": lib, "asset_id": asset},
                )
                self.assertEqual(status, 200, job)
                return job
        finally:
            reset_object_store()

    def test_job_row_created_and_index_status_survives_refresh(self) -> None:
        lib = f"lib-idx-{uuid4().hex[:10]}"
        asset = f"a-idx-{uuid4().hex[:10]}"
        job = self._run_index_flow(lib, asset)
        self.assertTrue(job.get("job_id"))

        status, latest = self.request(
            "GET",
            f"/api/v1/knowledge/libraries/{lib}/assets/{asset}/index-jobs/latest",
        )
        self.assertEqual(status, 200)
        self.assertEqual(latest["job_id"], job["job_id"])

        status, refreshing = self.request(
            "GET",
            f"/api/v1/knowledge/libraries/{lib}/assets/{asset}/index-status",
        )
        self.assertEqual(status, 200)
        self.assertIn(refreshing["status"], ("indexing", "indexed"))

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
            f"/api/v1/knowledge/libraries/{lib}/assets/index-status-batch?limit=100",
        )
        self.assertEqual(status, 200)
        self.assertTrue(any(row.get("asset_id") == asset for row in batch))

    def test_index_status_survives_after_job_poll(self) -> None:
        lib = f"lib-idx-{uuid4().hex[:10]}"
        asset = f"a-idx-{uuid4().hex[:10]}"
        job = self._run_index_flow(lib, asset)
        status, done = self.request("GET", f"/api/v1/knowledge/index-jobs/{job['job_id']}")
        self.assertEqual(done["status"], "done")

        status, index_status = self.request(
            "GET",
            f"/api/v1/knowledge/libraries/{lib}/assets/{asset}/index-status",
        )
        self.assertEqual(status, 200)
        self.assertEqual(index_status["status"], "indexed")
        self.assertEqual(index_status["current_index_version_id"], job["job_id"])


if __name__ == "__main__":
    unittest.main()
