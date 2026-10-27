"""Lightweight HYPERAG stand-in for unit tests (no engine boot)."""

from __future__ import annotations

from typing import Any, Literal


def _search_error(library_id: str, query: str, code: str, message: str) -> dict:
    return {
        "tool": "knowledge_search",
        "status": "error",
        "code": code,
        "message": message,
        "error": {"code": code, "message": message, "retryable": False},
        "data": {"library_id": library_id, "query": query},
        "library_id": library_id,
        "query": query,
        "evidence": [],
        "metadata": {},
    }


class _SearchResult:
    def __init__(self, library_id: str, query: str) -> None:
        self.library_id = library_id
        self.query = query
        self.evidence: list[Any] = []
        self.metadata: dict[str, Any] = {}


class ScriptKnowledgeAdapter:
    """Enough of the injected knowledge client for a scripted turn to call search."""

    default_library_id = ""

    def search(self, library_id: str, query: str, top_k: int | None = None, threshold: float | None = None, **kwargs: Any):
        del top_k, threshold, kwargs
        return _SearchResult(library_id, query)


def install_script_engine() -> None:
    """Tests do not boot HyperRAG. The runtime still wants a non-empty adapter."""
    from viknow3.application.hyperrag import runtime as hyper_runtime

    adapter = ScriptKnowledgeAdapter()
    hyper_runtime.get_engine_adapter = lambda: adapter


class ScriptHyperrag:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._job_seq = 0

    def reset(self) -> None:
        self._jobs.clear()
        self._job_seq = 0

    def discover(self, query: str, *, library_ids: list[str], top_k: int | None = None) -> dict:
        libraries = [
            {"library_id": library_id, "title": library_id, "summary": "", "top_files": [], "score": 1.0}
            for library_id in library_ids
        ]
        if top_k is not None:
            libraries = libraries[:top_k]
        return {"query": query or "", "libraries": libraries, "source": "hyperrag"}

    def search(self, library_id: str, query: str, keywords, *, top_k=None, threshold=None) -> dict:
        del keywords, top_k, threshold
        if library_id == "nope":
            return _search_error(library_id, query, "unknown_library", "library not found")
        return {
            "library_id": library_id,
            "query": query,
            "evidence": [
                {
                    "id": "ev-1",
                    "evidence_id": "ev-1",
                    "snippet": query,
                    "parts": [{"kind": "text", "text": query}],
                    "asset_id": "asset-1",
                    "modality": "document",
                }
            ],
            "source": "hyperrag",
        }

    def list_segments(self, library_id: str, asset_id: str, **kwargs) -> dict:
        return {
            "library_id": library_id,
            "asset_id": asset_id,
            "mode": kwargs.get("mode"),
            "segments": [],
            "source": "hyperrag",
        }

    def search_by_image(self, library_id: str, image_data_url: str, query: str = "", **kwargs) -> dict:
        del image_data_url, kwargs
        return self.search(library_id, query, None)

    def create_index(
        self,
        library_id: str,
        asset_id: str,
        asset_path: str,
        title: str | None,
        *,
        compile_mode: Literal["pre_compile", "fast_ingest"] | None = None,
        object_key: str | None = None,
    ) -> dict[str, Any]:
        del asset_path, title, compile_mode, object_key
        from uuid import uuid4

        self._job_seq += 1
        job_id = f"job-{self._job_seq}-{uuid4().hex[:8]}"
        self._jobs[job_id] = {
            "library_id": library_id,
            "asset_id": asset_id,
            "asset_type": "video",
        }
        return {
            "job_id": job_id,
            "library_id": library_id,
            "asset_id": asset_id,
            "status": "pending",
            "error_message": None,
            "metadata": None,
        }

    def get_index(self, job_id: str) -> dict[str, Any]:
        record = self._jobs.get(job_id)
        if record is None:
            from viknow3.application.hyperrag.client import HyperragClientError

            raise HyperragClientError(404, {"detail": "index job not found"})
        return {
            "job_id": job_id,
            "library_id": record["library_id"],
            "asset_id": record["asset_id"],
            "status": "done",
            "error_message": None,
            "metadata": {"asset_type": record["asset_type"]},
            "created_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:01:00+00:00",
            "progress": {
                "overall": 1.0,
                "current_stage": None,
                "current_stage_index": 0,
                "stage_count": 0,
                "completed_stage_count": 0,
                "running_duration_seconds": None,
                "stages": [],
            },
        }

    def get_index_job_tasks(self, job_id: str) -> list[dict[str, Any]]:
        if job_id not in self._jobs:
            from viknow3.application.hyperrag.client import HyperragClientError

            raise HyperragClientError(404, {"detail": "index job not found"})
        return []

    def delete_index(self, library_id: str, asset_id: str) -> dict[str, Any]:
        return {
            "job_id": None,
            "library_id": library_id,
            "asset_id": asset_id,
            "deleted": True,
            "message": "ok",
        }

    def asset_index(self, library_id: str, asset_id: str) -> dict[str, Any]:
        return {"library_id": library_id, "asset_id": asset_id, "status": "indexed"}
