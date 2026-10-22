"""
HyperRAG Schema Definitions

This module defines the core data models for HyperRAG's three-layer intermediate representation:
- Asset: Source file representation (document/video/image/audio)
- Segment: Atomic content units (text blocks, tables, images, video/audio clips)
- Chunk: Aggregated retrieval units (combining segments for vector search)

All models use Pydantic for validation and serialization, with extensible 'extra' fields
for future enhancements while maintaining backward compatibility.
"""

from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator
import html
import time


def _unescape_extra_fields(extra: dict) -> dict:
    if not extra:
        return extra
    return {
        key: html.unescape(value) if isinstance(value, str) else value
        for key, value in extra.items()
    }


class Asset(BaseModel):
    """
    Represents a source file or media asset.

    Serves as the top-level container for all content derived from a single source
    (document, video, audio file, or image). Maintains stable identification and
    metadata for tracking and deduplication.
    """

    doc_id: str = Field(..., description="Globally unique document identifier")
    source_uri: str = Field(
        ..., description="File path, URL, or other source identifier"
    )
    modality: Literal["document", "video", "image", "audio"] = Field(
        ..., description="Primary content type of the source"
    )
    created_at: int = Field(
        default_factory=lambda: int(time.time()),
        description="Creation timestamp (Unix epoch seconds)",
    )
    segments_count: int = Field(..., description="Number of segments in this asset")
    title: Optional[str] = Field(None, description="Human-readable title")
    language: Optional[str] = Field(None, description="Primary language (ISO code)")
    extra: dict = Field(default_factory=dict, description="Extension fields")
    page_info: dict = Field(default_factory=dict, description="Page information")

    @field_validator("segments_count")
    @classmethod
    def validate_segments_count(cls, v: int) -> int:
        if v < 0:
            raise ValueError("segments_count must be non-negative")
        return v

    @field_validator("title")
    @classmethod
    def unescape_title(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return html.unescape(v)

    @field_validator("extra")
    @classmethod
    def unescape_extra(cls, v: dict) -> dict:
        return _unescape_extra_fields(v)


class Segment(BaseModel):
    """
    Represents an atomic content unit within an asset.

    Segments are the smallest meaningful content units that preserve semantic boundaries.
    They can represent text blocks, tables, images, or temporal clips from audio/video.
    Each segment maintains its original location and content type.
    """

    segment_id: str = Field(..., description="Unique segment identifier")
    doc_id: str = Field(..., description="Parent asset identifier")
    order: int = Field(..., description="Sequential order within asset (0-based)")
    type: Literal["text", "image", "table", "video_clip", "audio_clip"] = Field(
        ..., description="Content type of this segment"
    )
    text: str = Field(..., description="Textual content or representation")
    main_struct: dict = Field(
        default_factory=dict,
        description="Structured caption, ASR, OCR, and object content for media segments",
    )
    location: dict = Field(
        default_factory=dict,
        description="Position metadata (page/bbox for docs, timestamps for media)",
    )
    extra: dict = Field(default_factory=dict, description="Extension fields")

    @field_validator("order")
    @classmethod
    def validate_order(cls, v: int) -> int:
        if v < 0:
            raise ValueError("order must be non-negative")
        return v

    @field_validator("text")
    @classmethod
    def unescape_text(cls, v: str) -> str:
        return html.unescape(v)

    @field_validator("extra")
    @classmethod
    def unescape_extra(cls, v: dict) -> dict:
        return _unescape_extra_fields(v)

    @field_validator("location")
    @classmethod
    def validate_location(cls, v: dict) -> dict:
        # Lightweight validation for common location patterns
        if "bbox" in v:
            bbox = v["bbox"]
            if (
                isinstance(bbox, list)
                and len(bbox) == 4
                and all(isinstance(x, (int, float)) for x in bbox)
            ):
                x1, y1, x2, y2 = bbox
                if not (x2 > x1 and y2 > y1):
                    raise ValueError("bbox must have x2 > x1 and y2 > y1")

        if "begin_ts" in v and "end_ts" in v:
            if not v["end_ts"] > v["begin_ts"]:
                raise ValueError("end_ts must be greater than begin_ts")

        return v


class Chunk(BaseModel):
    """
    Represents an aggregated unit optimized for retrieval and vectorization.

    Chunks combine one or more segments into text units suitable for embedding
    and vector search. They maintain references to source segments while providing
    merged content for efficient retrieval operations.
    """

    chunk_id: str = Field(..., description="Unique chunk identifier")
    doc_id: str = Field(..., description="Parent asset identifier")
    segment_ids: list[str] = Field(..., description="Source segments in order")
    text: str = Field(..., description="Merged text content for indexing")
    token_count: Optional[int] = Field(None, description="Estimated token count")
    order: Optional[int] = Field(None, description="Sequential order within asset")
    extra: dict = Field(default_factory=dict, description="Extension fields")

    @field_validator("segment_ids")
    @classmethod
    def validate_segment_ids(cls, v: list[str]) -> list[str]:
        if len(v) == 0:
            raise ValueError("segment_ids cannot be empty")
        if len(set(v)) != len(v):
            raise ValueError("segment_ids must be unique")
        return v

    @field_validator("order")
    @classmethod
    def validate_order(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 0:
            raise ValueError("order must be non-negative")
        return v

    @field_validator("token_count")
    @classmethod
    def validate_token_count(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 0:
            raise ValueError("token_count must be non-negative")
        return v

    @field_validator("extra")
    @classmethod
    def unescape_extra(cls, v: dict) -> dict:
        return _unescape_extra_fields(v)


# Type aliases for convenience
AssetType = Asset
SegmentType = Segment
ChunkType = Chunk
