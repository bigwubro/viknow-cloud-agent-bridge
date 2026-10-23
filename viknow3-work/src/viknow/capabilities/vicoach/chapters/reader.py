"""Validated filesystem access to ViCoach chapter metadata."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import ValidationError

from viknow.capabilities.vicoach.domain import VideoChapterMetadata
from viknow.capabilities.vicoach.errors import (
    ChapterMetadataNotFoundError,
    ChapterMetadataReadError,
    ChapterMetadataValidationError,
    InvalidChapterIdentifierError,
)

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class FilesystemChapterReader:
    """Read one strict chapter manifest below the configured files root."""

    def __init__(self, files_root: Path) -> None:
        self._chapters_root = (Path(files_root) / "knowledge" / "vicoach" / "chapters").resolve()

    def read(self, *, library_id: str, asset_id: str) -> VideoChapterMetadata:
        _require_safe_identifier("library_id", library_id)
        _require_safe_identifier("asset_id", asset_id)
        path = self._chapter_path(library_id=library_id, asset_id=asset_id)
        return self.read_path(path=path, library_id=library_id, asset_id=asset_id)

    def read_path(
        self,
        *,
        path: Path,
        library_id: str,
        asset_id: str,
    ) -> VideoChapterMetadata:
        """Read one explicitly supplied manifest path for controlled import/testing."""

        _require_safe_identifier("library_id", library_id)
        _require_safe_identifier("asset_id", asset_id)
        if not path.is_file():
            raise ChapterMetadataNotFoundError(
                f"chapter metadata not found for library_id={library_id!r}, asset_id={asset_id!r}"
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ChapterMetadataReadError(f"invalid chapter metadata JSON at {path}") from exc
        if isinstance(payload, list):
            payload = _manifest_from_chapter_list(
                payload,
                library_id=library_id,
                asset_id=asset_id,
            )
        if not isinstance(payload, dict):
            raise ChapterMetadataValidationError(
                f"chapter metadata at {path} must be a JSON object"
            )
        try:
            metadata = VideoChapterMetadata.model_validate(payload)
        except ValidationError as exc:
            raise ChapterMetadataValidationError(
                f"chapter metadata at {path} failed schema validation: {exc}"
            ) from exc
        if metadata.metadata.library_id != library_id or metadata.metadata.asset_id != asset_id:
            raise ChapterMetadataValidationError(
                f"chapter metadata identity at {path} does not match its requested "
                "library and asset"
            )
        return metadata

    def _chapter_path(self, *, library_id: str, asset_id: str) -> Path:
        path = (self._chapters_root / library_id / f"{asset_id}.json").resolve()
        try:
            path.relative_to(self._chapters_root)
        except ValueError as exc:
            raise InvalidChapterIdentifierError(
                "chapter metadata path escapes its files root"
            ) from exc
        return path


def _require_safe_identifier(name: str, value: str) -> None:
    if not _SAFE_IDENTIFIER.fullmatch(value):
        raise InvalidChapterIdentifierError(
            f"{name} must contain only letters, numbers, '.', '_' or '-' and cannot "
            "start with punctuation"
        )


def _manifest_from_chapter_list(
    chapters: list[object],
    *,
    library_id: str,
    asset_id: str,
) -> dict[str, object]:
    """Adapt the colleague's current chapter-array artifact to the stable manifest."""

    if not chapters:
        raise ChapterMetadataValidationError("chapter metadata array must not be empty")
    first = chapters[0]
    if not isinstance(first, dict):
        raise ChapterMetadataValidationError("chapter metadata array items must be objects")
    source_metadata = first.get("metadata")
    if not isinstance(source_metadata, dict):
        raise ChapterMetadataValidationError("chapter metadata item metadata must be an object")
    title = source_metadata.get("file_title")
    if not isinstance(title, str) or not title.strip():
        raise ChapterMetadataValidationError("chapter metadata item requires metadata.file_title")
    return {
        "metadata": {
            "library_id": library_id,
            "asset_id": asset_id,
            "title": title,
        },
        "chapters": chapters,
    }


__all__ = ["FilesystemChapterReader"]
