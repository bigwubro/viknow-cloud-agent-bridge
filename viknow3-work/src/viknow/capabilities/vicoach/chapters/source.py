"""Read existing knowledge only when learning is explicitly requested."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from pydantic import Field

from .domain import Model


class IndexedChapterSource:
    def __init__(self, adapter):
        if adapter is None:
            raise RuntimeError("knowledge source is not configured")
        self.adapter = adapter

    async def aget_asset_status(self, *, workspace, asset_id):
        status = await asyncio.to_thread(
            self.adapter.get_asset_status, library_id=workspace, asset_id=asset_id
        )
        if status is None:
            return None

        def value(key, default=None):
            return (
                status.get(key, default)
                if isinstance(status, dict)
                else getattr(status, key, default)
            )

        file_data = value("file_data") or {}
        path = self.adapter.resolve_video_semantic_path(library_id=workspace, asset_id=asset_id)
        return SimpleNamespace(
            status=value("status"),
            sop_path=path,
            file_title=file_data.get("title", "") if isinstance(file_data, dict) else "",
            duration_seconds=value("duration_seconds"),
            file_path=value("file_path", ""),
        )

    async def aget_video_semantic(self, workspace, asset_id):
        return await self.adapter.get_video_semantic_async(library_id=workspace, asset_id=asset_id)


class ChapterSnapshot(Model):
    """Server-resolved source metadata; never accepted as public file-path input."""

    sop_path: Path
    video_path: Path
    file_title: str = Field(min_length=1)
    duration_seconds: float = Field(gt=0, allow_inf_nan=False)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    def read(self):
        from .jobs import fingerprint

        payload = json.loads(self.sop_path.read_text())
        if fingerprint(payload) != self.source_sha256 or not self.video_path.is_file():
            raise ValueError("pinned course source changed or video is unavailable")
        return SimpleNamespace(
            status="completed",
            sop_path=self.sop_path,
            file_path=self.video_path,
            file_title=self.file_title,
            duration_seconds=self.duration_seconds,
        ), payload
