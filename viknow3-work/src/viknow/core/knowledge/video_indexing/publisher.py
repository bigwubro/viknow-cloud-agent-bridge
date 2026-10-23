"""Build and publish extracted video KB indexes."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from viknow.core.knowledge.video_indexing.domain import ValidationReport
from viknow.core.knowledge.video_indexing.index_support import (
    OpenAICompatibleEmbeddingClient,
    VideoKnowledgeIndexBuilder,
    read_json,
    write_json,
)
from viknow.core.knowledge.video_indexing.validator import SpecValidator


@dataclass(frozen=True)
class PublishResult:
    staging_dir: Path
    published_dir: Path | None
    report: ValidationReport


class VideoIndexPublisher:
    """Build a staging index from a spec and optionally publish it atomically."""

    def __init__(self, *, repo_root: Path) -> None:
        self.repo_root = repo_root

    def build_from_spec(
        self,
        *,
        spec_path: Path,
        staging_dir: Path,
        publish_dir: Path | None,
    ) -> PublishResult:
        spec = read_json(spec_path)
        report = SpecValidator().validate_spec(spec)
        if report.has_errors:
            return PublishResult(staging_dir=staging_dir, published_dir=None, report=report)
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        builder = VideoKnowledgeIndexBuilder(
            repo_root=self.repo_root,
            index_dir=staging_dir,
            spec_path=spec_path,
            embedding_client=OpenAICompatibleEmbeddingClient(timeout_seconds=120),
        )
        builder.build(force=True)
        if publish_dir is not None:
            self.publish(staging_dir=staging_dir, publish_dir=publish_dir)
            return PublishResult(staging_dir=staging_dir, published_dir=publish_dir, report=report)
        return PublishResult(staging_dir=staging_dir, published_dir=None, report=report)

    def publish(self, *, staging_dir: Path, publish_dir: Path) -> None:
        staging_dir = self._resolve_repo_path(staging_dir)
        publish_dir = self._resolve_repo_path(publish_dir)
        self._rewrite_staging_frame_uris(staging_dir=staging_dir, publish_dir=publish_dir)
        publish_dir.parent.mkdir(parents=True, exist_ok=True)
        backup_dir = publish_dir.with_name(f"{publish_dir.name}.previous")
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        if publish_dir.exists():
            publish_dir.rename(backup_dir)
        try:
            staging_dir.rename(publish_dir)
        except Exception:
            if publish_dir.exists():
                shutil.rmtree(publish_dir)
            if backup_dir.exists():
                backup_dir.rename(publish_dir)
            raise
        if backup_dir.exists():
            shutil.rmtree(backup_dir)

    def _rewrite_staging_frame_uris(self, *, staging_dir: Path, publish_dir: Path) -> None:
        segments_path = staging_dir / "segments.json"
        if not segments_path.exists():
            return
        payload = read_json(segments_path)
        for segment in payload.get("segments", []):
            frame_uri = segment.get("frame_uri")
            if not isinstance(frame_uri, str):
                continue
            frame_path = Path(frame_uri)
            if not frame_path.is_absolute():
                continue
            try:
                relative_frame_path = frame_path.relative_to(staging_dir)
            except ValueError:
                continue
            published_frame_path = publish_dir / relative_frame_path
            try:
                segment["frame_uri"] = str(published_frame_path.relative_to(self.repo_root))
            except ValueError:
                segment["frame_uri"] = str(published_frame_path)
        write_json(segments_path, payload)

    def _resolve_repo_path(self, path: Path) -> Path:
        if path.is_absolute():
            return path
        return self.repo_root / path
