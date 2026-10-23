"""Read the same verified publication used by the course workbench."""

from pathlib import Path

from viknow.capabilities.vicoach.domain import VideoChapterMetadata
from viknow.capabilities.vicoach.errors import (
    ChapterMetadataNotFoundError,
    ChapterMetadataValidationError,
)

from .jobs import ChapterJobs
from .reader import FilesystemChapterReader, _manifest_from_chapter_list, _require_safe_identifier


class PublishedChapterReader:
    def __init__(self, files_root: Path, jobs: ChapterJobs | None = None):
        self.filesystem = FilesystemChapterReader(files_root)
        self.jobs = jobs

    def read(self, *, library_id: str, asset_id: str) -> VideoChapterMetadata:
        _require_safe_identifier("library_id", library_id)
        _require_safe_identifier("asset_id", asset_id)
        if self.jobs is not None:
            try:
                published = self.jobs.published_view(library_id, asset_id)
                if published["status"] == "completed":
                    return VideoChapterMetadata.model_validate(
                        _manifest_from_chapter_list(
                            published["chapters"], library_id=library_id, asset_id=asset_id
                        )
                    )
                if published["status"] != "not_generated":
                    raise ChapterMetadataNotFoundError("当前视频尚无已发布的训练章节")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ChapterMetadataValidationError(
                    "已发布课程章节校验失败，请重新加载课程"
                ) from exc
        # Standalone consumers use the documented manifest when no publication exists.
        return self.filesystem.read(library_id=library_id, asset_id=asset_id)
