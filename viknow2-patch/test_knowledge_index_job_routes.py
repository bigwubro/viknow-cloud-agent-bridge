from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from viknow.api.app import create_app
from viknow.core.knowledge.hyperrag_adapter import DeleteIndexResult, IndexJobAccepted
from viknow.core.knowledge.job_progress import JobProgress


@dataclass
class FakeAssetStatus:
    metadata: dict[str, Any] | None = None
    parse_mode: str | None = None
    file_path: str | None = None


class FakeKnowledgeAdapter:
    def __init__(self, asset_status: FakeAssetStatus | None = None) -> None:
        self.captured: dict[str, object] = {}
        self._asset_status = asset_status

    def get_asset_status(self, *, library_id: str, asset_id: str) -> FakeAssetStatus | None:
        return self._asset_status

    async def submit_index_async(self, **kwargs):
        self.captured.update(kwargs)
        return IndexJobAccepted(
            job_id="job-index-1",
            library_id=str(kwargs["library_id"]),
            asset_id=str(kwargs["asset_id"]),
        )

    async def delete_index_async(self, **kwargs):
        return DeleteIndexResult(job_id="job-del", library_id="kb-1", asset_id="asset-1")

    def shutdown(self) -> None:
        pass


class FakeJobProgressReader:
    def __init__(self, job: JobProgress | None, *, redis_record: dict[str, Any] | None = None) -> None:
        self._job = job
        self._redis_record = redis_record or {}

    def get_job(self, job_id: str) -> JobProgress | None:
        if self._job is None or self._job.job_id != job_id:
            return None
        return self._job

    def _client(self):
        return self

    def get(self, _key: str) -> str | None:
        import json

        return json.dumps(self._redis_record) if self._redis_record else None


def test_create_knowledge_index_job_requires_existing_file(monkeypatch, tmp_path: Path) -> None:
    adapter = FakeKnowledgeAdapter()
    app = create_app()
    monkeypatch.setattr(
        "viknow.api.app.request_knowledge_adapter",
        lambda _request: adapter,
    )
    missing = tmp_path / "missing.md"
    response = TestClient(app).post(
        "/api/v1/knowledge/index-jobs",
        json={
            "library_id": "kb-1",
            "asset_id": "asset-1",
            "asset_path": str(missing),
        },
    )
    assert response.status_code == 400
    assert adapter.captured == {}


def test_create_knowledge_index_job_submits_and_returns_pending(
    monkeypatch, tmp_path: Path
) -> None:
    adapter = FakeKnowledgeAdapter()
    asset = tmp_path / "demo.md"
    asset.write_text("hello", encoding="utf-8")
    app = create_app()
    monkeypatch.setattr(
        "viknow.api.app.request_knowledge_adapter",
        lambda _request: adapter,
    )
    response = TestClient(app).post(
        "/api/v1/knowledge/index-jobs",
        json={
            "library_id": "kb-1",
            "asset_id": "asset-1",
            "asset_path": str(asset),
            "compile_mode": "fast_ingest",
            "params": {"title": "demo"},
        },
    )
    assert response.status_code == 200
    assert adapter.captured["library_id"] == "kb-1"
    assert adapter.captured["asset_id"] == "asset-1"
    assert adapter.captured["asset_path"] == str(asset)
    assert adapter.captured["compile_mode"] == "fast_ingest"
    assert adapter.captured["params"] == {"title": "demo"}
    assert response.json() == {
        "job_id": "job-index-1",
        "library_id": "kb-1",
        "asset_id": "asset-1",
        "status": "pending",
        "error_message": None,
        "metadata": None,
    }


def test_create_knowledge_index_job_rejects_relative_path(monkeypatch) -> None:
    adapter = FakeKnowledgeAdapter()
    app = create_app()
    monkeypatch.setattr(
        "viknow.api.app.request_knowledge_adapter",
        lambda _request: adapter,
    )
    response = TestClient(app).post(
        "/api/v1/knowledge/index-jobs",
        json={
            "library_id": "kb-1",
            "asset_id": "asset-1",
            "asset_path": "relative.md",
        },
    )
    assert response.status_code == 400
    assert adapter.captured == {}


def test_get_knowledge_index_job_maps_completed_to_done(monkeypatch) -> None:
    adapter = FakeKnowledgeAdapter(
        FakeAssetStatus(metadata={"doc_type": "document", "page_count": 12})
    )
    app = create_app()
    reader = FakeJobProgressReader(
        JobProgress(
            job_id="job-index-1",
            job_name="doc",
            workspace="kb-1",
            asset_id="asset-1",
            status="completed",
        )
    )
    monkeypatch.setattr(
        "viknow.api.app.request_knowledge_adapter",
        lambda _request: adapter,
    )
    monkeypatch.setattr(
        "viknow.api.app.request_job_progress_reader",
        lambda _request: reader,
    )
    response = TestClient(app).get("/api/v1/knowledge/index-jobs/job-index-1")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "done"
    assert body["library_id"] == "kb-1"
    assert body["asset_id"] == "asset-1"
    assert body["job_id"] == "job-index-1"
    assert body["metadata"] == {"doc_type": "document", "page_count": 12}


def test_get_knowledge_index_job_running_has_no_metadata(monkeypatch) -> None:
    adapter = FakeKnowledgeAdapter(FakeAssetStatus(metadata={"page_count": 99}))
    app = create_app()
    reader = FakeJobProgressReader(
        JobProgress(
            job_id="job-index-1",
            job_name="doc",
            workspace="kb-1",
            asset_id="asset-1",
            status="running",
        )
    )
    monkeypatch.setattr(
        "viknow.api.app.request_knowledge_adapter",
        lambda _request: adapter,
    )
    monkeypatch.setattr(
        "viknow.api.app.request_job_progress_reader",
        lambda _request: reader,
    )
    response = TestClient(app).get("/api/v1/knowledge/index-jobs/job-index-1")
    assert response.status_code == 200
    assert response.json()["metadata"] is None


def test_get_knowledge_index_job_derives_metadata_when_hyperrag_metadata_missing(
    monkeypatch, tmp_path: Path
) -> None:
    pdf = tmp_path / "demo.pdf"
    pdf.write_bytes(
        b"%PDF-1.1\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n",
    )
    adapter = FakeKnowledgeAdapter(
        FakeAssetStatus(metadata=None, parse_mode="document", file_path=str(pdf))
    )
    app = create_app()
    reader = FakeJobProgressReader(
        JobProgress(
            job_id="job-index-1",
            job_name="doc",
            workspace="kb-1",
            asset_id="asset-1",
            status="completed",
        ),
        redis_record={
            "job_id": "job-index-1",
            "asset_path": f"/data/viknow/knowledge/uploads/kb-1/asset-1.pdf",
        },
    )
    monkeypatch.setattr(
        "viknow.api.app.request_knowledge_adapter",
        lambda _request: adapter,
    )
    monkeypatch.setattr(
        "viknow.api.app.request_job_progress_reader",
        lambda _request: reader,
    )
    response = TestClient(app).get("/api/v1/knowledge/index-jobs/job-index-1")
    assert response.status_code == 200
    body = response.json()
    assert body["metadata"] is not None
    assert body["metadata"]["doc_type"] == "document"
    assert body["metadata"]["page_count"] == 1


def test_get_knowledge_index_job_not_found(monkeypatch) -> None:
    app = create_app()
    monkeypatch.setattr(
        "viknow.api.app.request_job_progress_reader",
        lambda _request: FakeJobProgressReader(None),
    )
    response = TestClient(app).get("/api/v1/knowledge/index-jobs/missing")
    assert response.status_code == 404
