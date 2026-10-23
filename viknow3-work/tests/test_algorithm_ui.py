"""The algorithm page uploads through the SDK, then indexes only when asked."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


def _load_ui():
    path = Path(__file__).resolve().parents[1] / "ui" / "server.py"
    spec = importlib.util.spec_from_file_location("viknow3_algorithm_ui", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Asset:
    library_id = "library-1"
    asset_id = "asset-1"
    asset_path = "knowledge/assets/asset-1/upload/asset-1.pdf"
    file_name = "notes.pdf"
    size_bytes = 4


class _Job:
    def __init__(self, status: str = "pending") -> None:
        self.job_id = "job-1"
        self.library_id = "library-1"
        self.asset_id = "asset-1"
        self.status = status
        self.error_message = None
        self.metadata = None


class _Knowledge:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str]] = []
        self.jobs: list[tuple[str, str, str]] = []

    def upload(self, path: str, library_id: str, *, asset_id: str | None = None):
        del asset_id
        self.uploads.append((Path(path).read_bytes().decode(), library_id))
        return _Asset()

    def create_index_job(self, library_id: str, asset_id: str, asset_path: str, **kwargs):
        self.jobs.append((library_id, asset_id, asset_path, kwargs.get("title")))
        return _Job()

    def get_index_job(self, job_id: str):
        del job_id
        return _Job("running")


class _Client:
    def __init__(self, knowledge: _Knowledge) -> None:
        self.knowledge = knowledge


class AlgorithmUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ui = _load_ui()
        self.knowledge = _Knowledge()
        self.ui.sdk = lambda: _Client(self.knowledge)
        self.client = TestClient(self.ui.app)

    def test_upload_then_manual_index(self) -> None:
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("建索引", page.text)

        uploaded = self.client.post(
            "/ui/knowledge/uploads",
            data={"library_id": "library-1"},
            files={"file": ("notes.pdf", b"%PDF", "application/pdf")},
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        body = uploaded.json()
        self.assertEqual(body["asset_path"], "knowledge/assets/asset-1/upload/asset-1.pdf")
        self.assertEqual(self.knowledge.uploads, [("%PDF", "library-1")])
        self.assertEqual(self.knowledge.jobs, [])

        indexed = self.client.post(
            "/ui/knowledge/index-jobs",
            json={
                "library_id": body["library_id"],
                "asset_id": body["asset_id"],
                "asset_path": body["asset_path"],
                "title": body["file_name"],
            },
        )
        self.assertEqual(indexed.status_code, 200, indexed.text)
        self.assertEqual(indexed.json()["status"], "pending")
        self.assertEqual(
            self.knowledge.jobs,
            [("library-1", "asset-1", "knowledge/assets/asset-1/upload/asset-1.pdf", "notes.pdf")],
        )
        status = self.client.get("/ui/knowledge/index-jobs/job-1")
        self.assertEqual(status.json()["status"], "running")
