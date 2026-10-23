"""Explicit frozen-source adapter for isolated demos; no database initialization."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace


class FrozenChapterSource:
    def __init__(self, manifest: Path):
        data = json.loads(manifest.read_text())
        root = manifest.resolve().parent
        paths = [Path(item["sop_path"]).resolve() for item in data["sources"]]
        if any(root not in path.parents for path in paths):
            raise ValueError("frozen SOP must stay inside the private manifest directory")
        if len(set(paths)) != len(paths):
            raise ValueError("each frozen video requires a distinct SOP file")
        self.sources = {(x["workspace"], x["asset_id"]): x for x in data["sources"]}
        if len(self.sources) != len(data["sources"]):
            raise ValueError("duplicate frozen source identity")

    async def aget_asset_status(self, *, workspace, asset_id):
        source = self.sources.get((workspace, asset_id))
        if source is None:
            return None
        return SimpleNamespace(
            status=source["status"],
            sop_path=source["sop_path"],
            file_title=source.get("file_title", ""),
            file_path=source.get("video_path", ""),
            duration_seconds=source.get("duration_seconds"),
        )

    async def aget_video_semantic(self, workspace, asset_id):
        source = self.sources.get((workspace, asset_id))
        if source is None or source["status"] != "completed":
            return None
        raw = Path(source["sop_path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != source["source_sha256"]:
            raise ValueError("frozen semantic source fingerprint mismatch")
        return json.loads(raw)
