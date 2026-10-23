"""Read-only normalization helpers for persisted HyperRAG review data."""

import json
from pathlib import Path
from typing import Any, Protocol

from .models import AssetReviewData, NormalizedChunk


class ReviewRuntime(Protocol):
    async def initialize_readonly(self) -> None: ...

    async def aget_asset_status(self, workspace: str, asset_id: str) -> Any: ...

    async def aget_file_snapshot(self, workspace: str, file_id: str) -> dict[str, Any]: ...

    async def aget_asset_chunks(
        self, workspace: str, file_id: str, page: dict[str, int] | None = None
    ) -> list[dict[str, Any]]: ...


class AssetReviewDataLoader:
    def __init__(self, runtime: ReviewRuntime):
        self.runtime = runtime

    async def load(self, *, workspace: str, asset_id: str) -> AssetReviewData:
        await self.runtime.initialize_readonly()
        status = await self.runtime.aget_asset_status(workspace, asset_id)
        if status is None:
            raise ValueError(f"asset not found: {workspace}/{asset_id}")

        file_path = str(getattr(status, "file_path", "") or "")
        if not file_path:
            raise ValueError(f"asset has no source file path: {workspace}/{asset_id}")
        if not Path(file_path).is_file():
            raise FileNotFoundError(f"asset source file does not exist: {file_path}")

        snapshot = await self.runtime.aget_file_snapshot(workspace, asset_id)
        segments = snapshot.get("segments")
        if not isinstance(segments, list) or not segments:
            raise ValueError(f"parser snapshot is missing segments: {workspace}/{asset_id}")

        raw_chunks = await self.runtime.aget_asset_chunks(workspace, asset_id)
        chunks = [normalize_chunk(chunk, asset_id=asset_id) for chunk in raw_chunks]
        chunks.sort(key=lambda chunk: (chunk.order, chunk.chunk_id))
        warnings = []
        if not chunks:
            warnings.append("no_chunks_found")
        warnings.extend(
            [f"chunk:{chunk.chunk_id}:{warning}" for chunk in chunks for warning in chunk.warnings]
        )

        asset = snapshot.get("asset")
        return AssetReviewData(
            workspace=workspace,
            asset_id=asset_id,
            source_path=file_path,
            parse_mode=str(snapshot.get("parse_mode") or "document"),
            asset=asset if isinstance(asset, dict) else {},
            segments=[segment for segment in segments if isinstance(segment, dict)],
            chunks=chunks,
            warnings=warnings,
        )


def normalize_chunk(chunk: dict[str, Any], *, asset_id: str) -> NormalizedChunk:
    """Normalize a persisted KG chunk without hiding legacy data shapes."""
    raw_content = str(chunk.get("content") or "")
    content = raw_content
    warnings: list[str] = []
    wrapped_order: int | None = None

    try:
        payload = json.loads(raw_content)
    except (json.JSONDecodeError, TypeError):
        warnings.append("legacy_plain_text_content")
    else:
        if isinstance(payload, dict) and isinstance(payload.get("content"), str):
            content = payload["content"]
            payload_order = payload.get("order")
            if isinstance(payload_order, int):
                wrapped_order = payload_order
        else:
            warnings.append("unexpected_json_content_shape")

    metadata = chunk.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        if chunk.get("metadata") is not None:
            warnings.append("invalid_metadata_shape")

    segment_ids = chunk.get("segment_ids")
    if not isinstance(segment_ids, list):
        segment_ids = metadata.get("segment_ids", [])
    segment_ids = [str(segment_id) for segment_id in segment_ids]

    order_value = chunk.get("chunk_order_index", chunk.get("order", wrapped_order))
    try:
        order = int(order_value) if order_value is not None else 0
    except (TypeError, ValueError):
        order = 0
        warnings.append("invalid_chunk_order")

    token_value = chunk.get("tokens", chunk.get("token_count"))
    try:
        token_count = int(token_value) if token_value is not None else None
    except (TypeError, ValueError):
        token_count = None
        warnings.append("invalid_token_count")

    return NormalizedChunk(
        chunk_id=str(chunk.get("chunk_id") or ""),
        asset_id=asset_id,
        order=order,
        content=content,
        raw_content=raw_content,
        token_count=token_count,
        segment_ids=segment_ids,
        metadata=metadata,
        warnings=warnings,
    )
