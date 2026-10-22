"""HyperragClient — always delegates to HyperRAG adapter (external/HyperRAG)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from viknow3.application.hyperrag.config import load_app_config
from viknow3.application.hyperrag.job_progress import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    HyperRAGJobProgressReader,
    index_job_from_progress,
    index_job_tasks_from_progress,
)
from viknow3.application.hyperrag.runtime import get_engine_adapter


class HyperragClientError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body


class HyperragClient:
    def __init__(self) -> None:
        self._config = load_app_config()
        self._adapter = get_engine_adapter()
        self._progress = HyperRAGJobProgressReader(self._config)

    def reset(self) -> None:
        """No-op. HyperRAG index state lives in Redis/Postgres/Neo4j, not in this client."""

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
        """Index one asset that already exists in the catalog.

        Callers create the library and the asset first. This method only submits
        the index job for an object that is already stored.
        """
        params: dict[str, Any] = {}
        if title:
            params["title"] = title
        if object_key:
            params["object_key"] = object_key
        accepted = self._adapter.submit_index(
            library_id=library_id,
            asset_id=asset_id,
            asset_path=asset_path,
            compile_mode=compile_mode,
            params=params or None,
        )
        return {
            "job_id": accepted.job_id,
            "library_id": accepted.library_id,
            "asset_id": accepted.asset_id,
            "status": "pending",
            "error_message": None,
            "metadata": None,
        }

    def get_index(self, job_id: str) -> dict[str, Any]:
        progress = self._progress.get_job(job_id)
        if progress is None:
            raise HyperragClientError(404, {"detail": "index job not found"})
        status = self._map_job_status(progress.status)
        metadata = None
        if status == "done":
            metadata = self._asset_metadata(progress.workspace, progress.asset_id)
        return index_job_from_progress(progress, status=status, metadata=metadata)

    def get_index_job_tasks(self, job_id: str) -> list[dict[str, Any]]:
        progress = self._progress.get_job(job_id)
        if progress is None:
            raise HyperragClientError(404, {"detail": "index job not found"})
        return index_job_tasks_from_progress(progress)

    def delete_index(self, library_id: str, asset_id: str) -> dict[str, Any]:
        result = self._adapter.delete_index(library_id=library_id, asset_id=asset_id)
        return {
            "job_id": result.job_id,
            "library_id": result.library_id,
            "asset_id": result.asset_id,
            "deleted": result.deleted,
            "message": result.message,
        }

    def discover(
        self,
        query: str,
        *,
        library_ids: list[str] | None = None,
        top_k: int | None = None,
    ) -> dict[str, Any]:
        """Rank libraries. ``library_ids`` limits the catalog the caller may read."""
        matches = self._adapter.discover_libraries(
            query or "",
            top_k=top_k,
            library_ids=library_ids,
        )
        libraries: list[dict[str, Any]] = []
        for match in matches:
            library = match.library
            libraries.append(
                {
                    "library_id": library.id,
                    "title": library.name,
                    "summary": library.description or "",
                    "score": match.score,
                    "reason": match.reason,
                    "top_files": [
                        {
                            "asset_id": asset.asset_id,
                            "title": asset.title,
                            "modality": asset.modality,
                            "summary": asset.summary,
                            "score": asset.score,
                        }
                        for asset in match.asset_matches
                    ],
                }
            )
        if top_k is not None:
            libraries = libraries[:top_k]
        return {"query": query or "", "libraries": libraries, "source": "hyperrag"}

    def search(
        self,
        library_id: str,
        query: str,
        keywords: list[str] | None,
        *,
        top_k: int | None = None,
        threshold: float | None = None,
    ) -> dict[str, Any]:
        try:
            payload = self._adapter.search(
                library_id=library_id,
                query=query,
                top_k=top_k,
                threshold=threshold,
            )
        except Exception as exc:
            message = str(exc)
            if "library" in message.lower() and "not" in message.lower():
                return _error(library_id, query, "unknown_library", message)
            raise
        return _search_payload_to_api(payload, keywords)

    def search_by_image(
        self,
        library_id: str,
        image_data_url: str,
        query: str = "",
        *,
        mix_search: bool = True,
        top_k: int | None = None,
    ) -> dict[str, Any]:
        payload = self._adapter.search_by_image(
            library_id=library_id,
            image_data_url=image_data_url,
            query=query,
            mix_search=mix_search,
            top_k=top_k,
        )
        return _search_payload_to_api(payload, None)

    def list_segments(
        self,
        library_id: str,
        asset_id: str,
        *,
        modality: str,
        mode: str = "map",
        catalog_id: str | None = None,
        cursor: str | None = None,
        page_size: int | None = None,
        query: str | None = None,
        limit: int | None = None,
        neighbor_count: int | None = None,
    ) -> dict[str, Any]:
        """Page indexed chunks for one asset. ``modality`` is ``video`` or ``document``."""
        if mode not in {"map", "page", "select", "coverage"}:
            raise HyperragClientError(422, {"detail": "mode must be map, page, select, or coverage"})
        if modality not in {"video", "document"}:
            raise HyperragClientError(422, {"detail": "modality must be video or document"})
        try:
            chunks = self._adapter.list_asset_segments(library_id=library_id, asset_id=asset_id)
        except ValueError as exc:
            raise HyperragClientError(404, {"detail": str(exc)}) from exc
        rows = [
            _segment_row(library_id, asset_id, chunk, index)
            for index, chunk in enumerate(chunks)
        ]
        rows = [row for row in rows if _segment_matches_modality(row["modality"], modality)]
        window = _segment_window(
            rows,
            mode=mode,
            cursor=cursor,
            page_size=page_size,
            query=query,
            limit=limit,
            neighbor_count=neighbor_count,
        )
        tool = "list_video_segments" if modality == "video" else "list_doc_segments"
        return {
            "tool": tool,
            "library_id": library_id,
            "asset_id": asset_id,
            "mode": mode,
            "catalog_id": catalog_id or f"cat-{library_id}-{asset_id}",
            "segments": window["segments"],
            "next_cursor": window["next_cursor"],
            "source": "hyperrag",
        }

    def asset_index(self, library_id: str, asset_id: str) -> dict[str, Any]:
        try:
            segments = self._adapter.list_asset_segments(library_id=library_id, asset_id=asset_id)
        except ValueError as exc:
            raise HyperragClientError(404, {"detail": str(exc)}) from exc
        items = []
        for index, segment in enumerate(segments):
            time_range = segment.get("time_range") if isinstance(segment.get("time_range"), dict) else {}
            items.append(
                {
                    "id": str(segment.get("id") or segment.get("chunk_id") or f"seg-{index}"),
                    "start_seconds": float(time_range.get("start_seconds") or 0),
                    "end_seconds": float(time_range.get("end_seconds") or 0),
                    "type": str(segment.get("type") or segment.get("chunk_type") or "caption"),
                    "text": str(segment.get("text") or segment.get("content") or ""),
                }
            )
        return {
            "library_id": library_id,
            "asset_id": asset_id,
            "index_status": "completed",
            "items": items,
        }

    def media(self, library_id: str, asset_id: str, kind: str) -> tuple[str, bytes]:
        path = self._adapter.resolve_asset_file_path(library_id=library_id, asset_id=asset_id)
        if not path:
            detail = {
                "video": "asset video not found",
                "document": "document render source not found",
                "image": "asset image not found",
            }[kind]
            raise HyperragClientError(404, {"detail": detail})
        file_path = Path(path)
        if not file_path.is_file():
            raise HyperragClientError(404, {"detail": "asset file not found on disk"})
        data = file_path.read_bytes()
        suffix = file_path.suffix.lower()
        if kind == "video" or suffix in {".mp4", ".mov", ".webm"}:
            return "video/mp4", data
        if kind == "document" or suffix in {".pdf"}:
            return "application/pdf", data
        return "image/png", data

    def _map_job_status(self, raw: str) -> str:
        normalized = str(raw or "").strip().lower()
        if normalized in {JOB_STATUS_COMPLETED, "completed", "done"}:
            return "done"
        if normalized in {JOB_STATUS_FAILED, "failed"}:
            return "failed"
        if normalized in {JOB_STATUS_RUNNING, "running"}:
            return "running"
        return "pending"

    def _asset_metadata(self, library_id: str, asset_id: str) -> dict[str, Any]:
        status = self._adapter.get_asset_status(library_id=library_id, asset_id=asset_id)
        chunk_count = self._adapter.get_asset_chunk_count(library_id=library_id, asset_id=asset_id)
        metadata: dict[str, Any] = {"chunk_nums": chunk_count}
        if status is None:
            return metadata
        status_name = str(getattr(status, "name", status)).lower()
        if "video" in status_name:
            metadata["asset_type"] = "video"
        else:
            metadata["doc_type"] = "pdf"
        return metadata


def _error(library_id: str, query: str, code: str, message: str) -> dict[str, Any]:
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


def _kind(part: Any) -> str:
    kind = getattr(part, "kind", "")
    return str(getattr(kind, "value", kind))


def _evidence_view(item: Any) -> dict[str, Any]:
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    snippet = ""
    time_range = None
    modality = "document"
    for part in item.parts:
        if part.text and not snippet:
            snippet = str(part.text)
        kind = _kind(part)
        if kind == "video":
            modality = "video"
            if part.time_range is not None:
                time_range = {
                    "start_seconds": float(part.time_range.start_seconds),
                    "end_seconds": float(part.time_range.end_seconds),
                }
        elif kind == "image" and modality != "video":
            modality = "image"
    if str(metadata.get("chunk_type") or "").lower() == "video":
        modality = "video"
    view = {
        "id": item.id,
        "evidence_id": item.id,
        "source": item.source,
        "title": item.title,
        "score": item.score,
        "library_id": metadata.get("library_id"),
        "asset_id": str(metadata.get("asset_id") or "").strip(),
        "modality": modality,
        "snippet": snippet,
        "parts": [part.model_dump(mode="json") for part in item.parts],
        "metadata": metadata,
    }
    if time_range is not None:
        view["time_range"] = time_range
    return view


def _segment_matches_modality(actual: str, requested: str) -> bool:
    if requested == "video":
        return actual == "video"
    return actual in {"document", "image"}


def _segment_row(library_id: str, asset_id: str, chunk: dict[str, Any], index: int) -> dict[str, Any]:
    metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
    time_range = chunk.get("time_range") if isinstance(chunk.get("time_range"), dict) else None
    begin = metadata.get("begin_ts")
    end = metadata.get("end_ts")
    if time_range is None and begin is not None and end is not None:
        time_range = {"start_seconds": float(begin), "end_seconds": float(end)}
    chunk_type = str(metadata.get("chunk_type") or chunk.get("chunk_type") or "").lower()
    if chunk_type == "video" or time_range is not None:
        modality = "video"
    elif chunk_type == "image":
        modality = "image"
    else:
        modality = "document"
    text = str(chunk.get("text") or chunk.get("content") or "")
    segment_id = str(chunk.get("id") or chunk.get("chunk_id") or metadata.get("segment_id") or f"seg-{index}")
    order = chunk.get("order")
    row = {
        "segment_id": segment_id,
        "summary": text[:180],
        "text": text,
        "modality": modality,
        "locator": f"{library_id}/{asset_id}#{segment_id}",
        "order": int(order) if order is not None else index,
    }
    if time_range is not None:
        row["time_range"] = {
            "start_seconds": float(time_range.get("start_seconds") or 0),
            "end_seconds": float(time_range.get("end_seconds") or 0),
        }
    return row


def _public_segment(row: dict[str, Any], *, include_text: bool) -> dict[str, Any]:
    if include_text:
        return row
    return {key: value for key, value in row.items() if key != "text"}


def _segment_window(
    rows: list[dict[str, Any]],
    *,
    mode: str,
    cursor: str | None,
    page_size: int | None,
    query: str | None,
    limit: int | None,
    neighbor_count: int | None,
) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: (row["order"], row["segment_id"]))
    if mode in {"map", "page"}:
        size = 20 if page_size is None else page_size
        size = max(1, min(int(size), 50))
        start = 0
        if cursor:
            ids = [row["segment_id"] for row in ordered]
            if cursor in ids:
                start = ids.index(cursor) + 1
        page = ordered[start : start + size]
        next_cursor = page[-1]["segment_id"] if page and start + size < len(ordered) else None
        return {
            "segments": [_public_segment(row, include_text=mode == "page") for row in page],
            "next_cursor": next_cursor,
        }
    needle = (query or "").strip().lower()

    def hits(row: dict[str, Any]) -> bool:
        if not needle:
            return True
        return needle in row["text"].lower() or needle in row["summary"].lower()

    if mode == "select":
        cap = 8 if limit is None else limit
        cap = max(1, min(int(cap), 20))
        return {"segments": [row for row in ordered if hits(row)][:cap], "next_cursor": None}
    neighbors = 0 if neighbor_count is None else max(0, min(int(neighbor_count), 2))
    indexes = [index for index, row in enumerate(ordered) if hits(row)]
    chosen: set[int] = set()
    for index in indexes:
        for offset in range(index - neighbors, index + neighbors + 1):
            if 0 <= offset < len(ordered):
                chosen.add(offset)
    cap = 20 if limit is None else limit
    cap = max(1, min(int(cap), 20))
    return {
        "segments": [ordered[index] for index in sorted(chosen)][:cap],
        "next_cursor": None,
    }


def _search_payload_to_api(payload: Any, keywords: list[str] | None) -> dict[str, Any]:
    evidence = [_evidence_view(item) for item in payload.evidence]
    result: dict[str, Any] = {
        "tool": "knowledge_search",
        "status": "ok",
        "code": "ok",
        "data": {
            "library_id": payload.library_id,
            "query": payload.query,
            "evidence": evidence,
            "metadata": payload.metadata,
        },
        "library_id": payload.library_id,
        "query": payload.query,
        "evidence": evidence,
        "metadata": payload.metadata,
        "source": "hyperrag",
    }
    if keywords is not None:
        result["bm25_keywords"] = keywords
        result["data"]["bm25_keywords"] = keywords
    return result


_client: HyperragClient | None = None

_NOT_INITIALIZED = (
    "HyperRAG client is not initialized. The application must call init_hyperrag() "
    "during process startup (before serving requests)."
)


def init_hyperrag_client() -> None:
    global _client
    if _client is None:
        _client = HyperragClient()


def reset_hyperrag_client() -> None:
    global _client
    _client = None


def _require_client() -> HyperragClient:
    if _client is None:
        raise RuntimeError(_NOT_INITIALIZED)
    return _client




class _HyperragModule:
    """Process-wide HyperRAG facade. Initialized at application startup via init_hyperrag().

    Indexing is not the first step. Create the library, create the asset, then
    call ``create_index``. ``access.rag.service.create_index_job`` enforces that
    order before it reaches this client.
    """

    def reset(self) -> None:
        """No-op (real HyperRAG state is external)."""

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
        return _require_client().create_index(
            library_id,
            asset_id,
            asset_path,
            title,
            compile_mode=compile_mode,
            object_key=object_key,
        )

    def get_index(self, job_id: str) -> dict[str, Any]:
        return _require_client().get_index(job_id)

    def get_index_job_tasks(self, job_id: str) -> list[dict[str, Any]]:
        return _require_client().get_index_job_tasks(job_id)

    def delete_index(self, library_id: str, asset_id: str) -> dict[str, Any]:
        return _require_client().delete_index(library_id, asset_id)

    def discover(
        self,
        query: str,
        *,
        library_ids: list[str] | None = None,
        top_k: int | None = None,
    ) -> dict[str, Any]:
        return _require_client().discover(query, library_ids=library_ids, top_k=top_k)

    def search(
        self,
        library_id: str,
        query: str,
        keywords: list[str] | None,
        *,
        top_k: int | None = None,
        threshold: float | None = None,
    ) -> dict[str, Any]:
        return _require_client().search(
            library_id,
            query,
            keywords,
            top_k=top_k,
            threshold=threshold,
        )

    def search_by_image(
        self,
        library_id: str,
        image_data_url: str,
        query: str = "",
        *,
        mix_search: bool = True,
        top_k: int | None = None,
    ) -> dict[str, Any]:
        return _require_client().search_by_image(
            library_id,
            image_data_url,
            query,
            mix_search=mix_search,
            top_k=top_k,
        )

    def list_segments(self, library_id: str, asset_id: str, **kwargs: Any) -> dict[str, Any]:
        return _require_client().list_segments(library_id, asset_id, **kwargs)

    def asset_index(self, library_id: str, asset_id: str) -> dict[str, Any]:
        return _require_client().asset_index(library_id, asset_id)

    def media(self, library_id: str, asset_id: str, kind: str) -> tuple[str, bytes]:
        return _require_client().media(library_id, asset_id, kind)


HYPERAG = _HyperragModule()
