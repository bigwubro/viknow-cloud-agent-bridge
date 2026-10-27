"""Multimodal tools exposed to agent adapters."""

from __future__ import annotations

import base64
import contextlib
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field, is_dataclass
from hashlib import sha1
from json import JSONDecodeError
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agents.runtime.core.actio.client import ActioClient
from agents.runtime.core.config import ModelProvider, ToolConfigs
from agents.runtime.core.evidence import Evidence, EvidencePart, EvidencePartKind, EvidenceTimeRange
from agents.runtime.core.knowledge.object_key import resolve_asset_object_key
from agents.runtime.core.model_gateway import model_connection_options
from agents.runtime.core.multimodal.asr import EvidenceTranscriber
from agents.runtime.core.multimodal.document_page_renderer import (
    MAX_DOCUMENT_INSPECT_PAGES,
    SpreadsheetRenderContext,
    document_page_count,
    render_document_page,
)
from agents.runtime.core.multimodal.image_inspect_claim import ImageReviewResult, ImageVisualClaim
from agents.runtime.core.multimodal.inspect_citation import InspectClaimRender, InspectClaimSource
from agents.runtime.core.multimodal.locate_anything import LocateAnythingClient
from agents.runtime.core.multimodal.ocr import EvidenceOCRReader
from agents.runtime.core.multimodal.review import EvidenceReviewer
from agents.runtime.core.multimodal.video_discovery import (
    VideoDiscoveryClient,
    VideoDiscoveryService,
)
from agents.runtime.core.multimodal.video_review_claim import VideoReviewResult, VideoVisualClaim
from agents.runtime.core.scheduler.domain import Priority, extra_body_with_priority
from agents.runtime.core.tools.base import ToolErrorCode, ToolResult, map_error_code
from agents.runtime.core.tools.context import (
    get_allowed_library_ids,
    get_tool_run_context,
    is_library_allowed,
)
from agents.runtime.core.tools.knowledge import KnowledgeToolHandlers

if TYPE_CHECKING:
    from agents.runtime.core.multimodal.advanced_video_extractor import AdvancedVideoEvidenceExtractor
    from agents.runtime.core.multimodal.image_review_extractor import ImageEvidenceExtractor
    from agents.runtime.core.multimodal.text_inspect_extractor import TextEvidenceInspector
    from agents.runtime.core.multimodal.video_review_extractor import VideoEvidenceExtractor

_VIDEO_EVIDENCE_BOUNDARY_POLICY = (
    "Use the original evidence boundary from knowledge_search or list_video_segments. "
    "Copy library_id, asset_id, evidence_id, and time_range from the same selected "
    "evidence item whenever available. Do not invent, expand, shrink, or merge time "
    "ranges. Avoid overlapping calls for the same asset and tool; if a narrower call "
    "replaces an earlier broad result, use the narrower result in final synthesis."
)


VIDEO_INSPECT_SCHEMA = {
    "description": (
        "Inspect visual evidence from a video asset returned by knowledge_search. "
        + _VIDEO_EVIDENCE_BOUNDARY_POLICY
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "library_id": {
                "type": "string",
                "description": (
                    "Knowledge library id copied from the knowledge_search result. It is "
                    "used only to resolve asset status in the backend."
                ),
            },
            "asset_id": {
                "type": "string",
                "description": "Video asset id copied from the selected search evidence.",
            },
            "time_range": {
                "type": "object",
                "description": (
                    "Video time range copied from the selected evidence item's original "
                    "evidence boundary. Do not invent, expand, shrink, or merge time ranges."
                ),
                "properties": {
                    "start_seconds": {"type": "number"},
                    "end_seconds": {"type": "number"},
                },
                "required": ["start_seconds", "end_seconds"],
                "additionalProperties": False,
            },
            "evidence_id": {
                "type": "string",
                "description": (
                    "Selected evidence id copied from the same evidence item as time_range "
                    "when available."
                ),
            },
            "title": {
                "type": "string",
                "description": "Optional selected evidence title for tracing.",
            },
            "question": {
                "type": "string",
                "description": (
                    "An evidence-specific focused question. Rewrite the user's original "
                    "question for this evidence before calling; do not pass the original "
                    "question unchanged unless it is already specific to this evidence. "
                    "Preserve candidate state and location terms from the selected evidence; "
                    "do not introduce new alternative location labels unless the user asked "
                    "for those labels."
                ),
            },
        },
        "required": ["library_id", "asset_id", "time_range", "question"],
        "additionalProperties": False,
    },
}


ADVANCED_VIDEO_INSPECT_SCHEMA = {
    "description": (
        "Extract cited visual facts from one selected short video evidence range using "
        "coarse coverage and optional focused inspection."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "library_id": {"type": "string"},
            "asset_id": {"type": "string"},
            "evidence_id": {"type": "string"},
            "title": {"type": "string"},
            "time_range": {
                "type": "object",
                "properties": {
                    "start_seconds": {"type": "number"},
                    "end_seconds": {"type": "number"},
                },
                "required": ["start_seconds", "end_seconds"],
                "additionalProperties": False,
            },
            "fps": {
                "oneOf": [
                    {"type": "string", "enum": ["auto"]},
                    {"type": "number", "minimum": 0.1, "maximum": 8},
                ],
                "default": "auto",
            },
            "crop": {
                "type": "object",
                "properties": {
                    "x": {"type": "number", "minimum": 0, "maximum": 1},
                    "y": {"type": "number", "minimum": 0, "maximum": 1},
                    "width": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
                    "height": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
                },
                "required": ["x", "y", "width", "height"],
                "additionalProperties": False,
            },
            "visual_budget": {
                "type": "string",
                "enum": ["low", "balanced", "high"],
                "default": "balanced",
            },
            "instruction": {"type": "string"},
        },
        "required": ["library_id", "asset_id", "time_range", "question"],
        "additionalProperties": False,
    },
}


IMAGE_INSPECT_SCHEMA = {
    "description": (
        "Inspect image evidence returned by knowledge_search and extract structured "
        "claims for final answers. Use only after knowledge_search has returned a "
        "selected image evidence item with library_id, asset_id, and evidence_id. "
        "Do not use this tool for user-uploaded workspace images; use "
        "uploaded_image_search for uploaded images. Caption text is supplementary "
        "context. Do NOT cite image visual details from knowledge results without "
        "calling this tool first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "library_id": {
                "type": "string",
                "description": "Knowledge library id copied from the knowledge_search result.",
            },
            "asset_id": {
                "type": "string",
                "description": "Asset id copied from the selected search evidence.",
            },
            "evidence_id": {
                "type": "string",
                "description": (
                    "Selected evidence id (chunk_id) copied from the selected "
                    "knowledge_search evidence item's id. Required to reconstruct "
                    "the exact image chunk instead of falling back to the whole asset."
                ),
            },
            "title": {
                "type": "string",
                "description": "Optional selected evidence title for tracing.",
            },
            "question": {
                "type": "string",
                "description": "Focused question about image content.",
            },
        },
        "required": ["library_id", "asset_id", "evidence_id", "question"],
        "additionalProperties": False,
    },
}


LOCATE_ANYTHING_SCHEMA = {
    "description": (
        "Locate objects, regions, text, GUI elements, or other visible targets in an "
        "image using Actio LocateAnything. Use for grounding and counting when a precise "
        "visual location or count is useful. Accepts a workspace image path, an image or "
        "document evidence item returned by knowledge_search, or a video evidence item. "
        "Documents are grounded on one selected page; videos are grounded on one selected "
        "frame."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workspace_path": {
                "type": "string",
                "description": (
                    "Relative workspace path for an uploaded image, for example "
                    "uploads/example.png. Use this for user-uploaded images."
                ),
            },
            "library_id": {
                "type": "string",
                "description": "Knowledge library id copied from knowledge_search.",
            },
            "asset_id": {
                "type": "string",
                "description": "Media asset id copied from the selected search evidence.",
            },
            "evidence_id": {
                "type": "string",
                "description": "Selected image, document, or video evidence id.",
            },
            "query": {
                "type": "string",
                "description": "Target object, phrase, text, or region to locate.",
            },
            "mode": {
                "type": "string",
                "enum": ["detect", "ground", "point"],
                "description": "Optional LocateAnything prompt mode.",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional maximum number of returned detections.",
            },
            "generation_mode": {
                "type": "string",
                "description": "Optional LocateAnything generation mode, e.g. hybrid.",
            },
            "page_number": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "One-based page number for document evidence. When omitted, the page is "
                    "resolved from the selected evidence metadata. Mutually exclusive with "
                    "frame_time."
                ),
            },
            "frame_time": {
                "type": "number",
                "minimum": 0,
                "description": (
                    "Video evidence frame time in seconds: ground the target on the single "
                    "frame at this timestamp. Only used when the selected evidence is a video."
                ),
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


UPLOADED_IMAGE_SEARCH_SCHEMA = {
    "description": (
        "Analyze one user-uploaded workspace image before retrieval or inspection. "
        "Use for uploaded files referenced by workspace_path, not for image evidence "
        "returned by knowledge_search. Use for direct uploaded image understanding "
        "such as OCR, visible text reading, counting, object identification, scene "
        "description, screenshots, charts, labels, interfaces, and photos. Decide "
        "whether the uploaded image can answer the query directly, should feed "
        "knowledge search, mismatches the query or library scope, or cannot be "
        "processed. When an uploaded image may affect a knowledge-base answer, call "
        "this before query_refine, knowledge_search, and all inspect/evidence tools."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workspace_path": {
                "type": "string",
                "description": (
                    "Relative workspace path copied from the uploaded files list, "
                    "for example uploads/example.png."
                ),
            },
            "query": {
                "type": "string",
                "description": "The user's original question about or alongside the image.",
            },
            "library_id": {
                "type": "string",
                "description": (
                    "Optional knowledge library id selected by knowledge_discover. "
                    "When omitted, the tool only performs image-only understanding."
                ),
            },
        },
        "required": ["workspace_path", "query"],
        "additionalProperties": False,
    },
}


TEXT_INSPECT_SCHEMA = {
    "description": (
        "Extract factual claims from text evidence with supporting quotes. "
        "Each claim's supporting quote is verified against the source text "
        "via fuzzy matching. Use this before citing specific details from "
        "long or dense text evidence."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "library_id": {
                "type": "string",
                "description": "Knowledge library id copied from the knowledge_search result.",
            },
            "asset_id": {
                "type": "string",
                "description": "Asset id copied from the selected search evidence.",
            },
            "evidence_id": {
                "type": "string",
                "description": (
                    "Selected evidence id (chunk_id) copied from the selected "
                    "knowledge_search evidence item's id. Required to reconstruct "
                    "the exact text chunk instead of falling back to the whole asset."
                ),
            },
            "title": {
                "type": "string",
                "description": "Optional selected evidence title for tracing.",
            },
            "question": {
                "type": "string",
                "description": "Focused question about text content.",
            },
        },
        "required": ["library_id", "asset_id", "evidence_id", "question"],
        "additionalProperties": False,
    },
}


DOCUMENT_INSPECT_SCHEMA = {
    "description": (
        "Inspect a selected document evidence item using the complete original source page at "
        "144 DPI. Use this before citing facts from text, tables, charts, or figures returned "
        "by knowledge_search. Copy library_id, asset_id, and evidence_id from one selected "
        "knowledge_search result. By default inspect all pages containing that chunk. "
        "For document-wide questions or missing context, use page_indices to examine other "
        "pages of the SAME asset in bounded batches. Returns total_pages for coverage planning. "
        "Indices are zero-based physical PDF pages, not printed page labels."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "library_id": {
                "type": "string",
                "description": "Knowledge library id copied from the knowledge_search result.",
            },
            "asset_id": {
                "type": "string",
                "description": "Asset id copied from the selected search evidence.",
            },
            "evidence_id": {
                "type": "string",
                "description": "Selected evidence id copied from the knowledge_search result.",
            },
            "title": {"type": "string", "description": "Optional selected evidence title."},
            "page_indices": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0},
                "minItems": 1,
                "maxItems": MAX_DOCUMENT_INSPECT_PAGES,
                "uniqueItems": True,
                "description": (
                    "Optional zero-based physical PDF page indices to inspect, at most 8 per call. "
                    "Omit to inspect the retrieved chunk's pages. For whole-document counts, "
                    "cover every index from 0 to total_pages-1 across successive calls."
                ),
            },
            "question": {
                "type": "string",
                "description": "Focused question to answer from the complete source page.",
            },
        },
        "required": ["library_id", "asset_id", "evidence_id", "question"],
        "additionalProperties": False,
    },
}


ASR_SCHEMA = {
    "description": (
        "Transcribe spoken audio from a ViKnow Evidence object. "
        "Use this only when the question depends on speech, narration, dialogue, "
        "commands, or other audio content in a video/file evidence. "
        + _VIDEO_EVIDENCE_BOUNDARY_POLICY
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "library_id": {
                "type": "string",
                "description": "Knowledge library id copied from the knowledge_search result.",
            },
            "asset_id": {
                "type": "string",
                "description": "Asset id copied from the selected search evidence.",
            },
            "evidence_id": {
                "type": "string",
                "description": (
                    "Selected evidence id copied from the same evidence item as time_range "
                    "when available."
                ),
            },
            "title": {
                "type": "string",
                "description": "Optional selected evidence title for tracing.",
            },
            "time_range": {
                "type": "object",
                "description": (
                    "Time range copied from the selected evidence item's original evidence "
                    "boundary. Required when the evidence contains a video segment; omit "
                    "only for audio-only files. Do not invent, expand, shrink, or merge "
                    "time ranges."
                ),
                "properties": {
                    "start_seconds": {"type": "number"},
                    "end_seconds": {"type": "number"},
                },
                "required": ["start_seconds", "end_seconds"],
                "additionalProperties": False,
            },
            "language": {
                "type": "string",
                "description": "Optional source language hint for ASR, such as zh or en.",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "Optional ASR prompt containing domain terms, names, product names, "
                    "or expected vocabulary that may improve transcription accuracy."
                ),
            },
        },
        "required": ["library_id", "asset_id", "time_range"],
        "additionalProperties": False,
    },
}

OCR_SCHEMA = {
    "description": (
        "Recognize visible text from a ViKnow Evidence object. "
        "Use this when the question depends on text shown in an image, video frame, "
        "document page, screen, label, sign, panel, or instrument display. "
        + _VIDEO_EVIDENCE_BOUNDARY_POLICY
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "library_id": {
                "type": "string",
                "description": "Knowledge library id copied from the knowledge_search result.",
            },
            "asset_id": {
                "type": "string",
                "description": "Asset id copied from the selected search evidence.",
            },
            "evidence_id": {
                "type": "string",
                "description": (
                    "Selected evidence id copied from the same evidence item as time_range "
                    "when available."
                ),
            },
            "title": {
                "type": "string",
                "description": "Optional selected evidence title for tracing.",
            },
            "time_range": {
                "type": "object",
                "description": (
                    "Time range copied from the selected evidence item's original evidence "
                    "boundary. Required when the evidence contains a video segment; omit "
                    "only for image or document evidence that does not have a video time "
                    "range. Do not invent, expand, shrink, or merge time ranges."
                ),
                "properties": {
                    "start_seconds": {"type": "number"},
                    "end_seconds": {"type": "number"},
                },
                "required": ["start_seconds", "end_seconds"],
                "additionalProperties": False,
            },
            "instruction": {
                "type": "string",
                "description": (
                    "Optional focused OCR instruction, such as which panel, label, "
                    "document region, or visible text type should be prioritized."
                ),
            },
        },
        "required": ["library_id", "asset_id", "time_range"],
        "additionalProperties": False,
    },
}

CLIP_SEARCH_SCHEMA = {
    "description": (
        "Search or narrow video clip evidence. Prefer passing complete Evidence objects "
        "returned by knowledge_search; otherwise provide library_id/query to search the "
        "configured knowledge backend."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "library_id": {
                "type": "string",
                "description": "Knowledge library id copied from knowledge_search.",
            },
            "asset_id": {
                "type": "string",
                "description": (
                    "Optional video asset id copied from a selected knowledge_search result. "
                    "Used only when no complete Evidence object is passed."
                ),
            },
            "evidence_id": {
                "type": "string",
                "description": "Optional selected evidence id for tracing or lookup.",
            },
            "title": {
                "type": "string",
                "description": "Optional selected evidence title for tracing.",
            },
            "time_range": {
                "type": "object",
                "description": "Optional video time range copied from selected evidence.",
                "properties": {
                    "start_seconds": {"type": "number"},
                    "end_seconds": {"type": "number"},
                },
                "required": ["start_seconds", "end_seconds"],
                "additionalProperties": False,
            },
            "evidence": {
                "type": "object",
                "description": ("One complete Evidence object returned by knowledge_search."),
            },
            "evidence_list": {
                "type": "array",
                "description": "Complete Evidence objects returned by knowledge_search.",
                "items": {"type": "object"},
            },
            "query": {
                "type": "string",
                "description": "Natural language description of the clip content to find.",
            },
            "top_k": {
                "type": "integer",
                "description": "Maximum number of clips to return.",
                "default": 5,
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}

GLOBAL_BROWSE_SCHEMA = {
    "description": (
        "Answer a question from video clip evidence. Prefer passing Evidence objects "
        "returned by knowledge_search; otherwise provide library_id/question to search "
        "the configured knowledge backend first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "library_id": {
                "type": "string",
                "description": "Knowledge library id copied from knowledge_search.",
            },
            "evidence": {
                "type": "object",
                "description": ("One complete Evidence object returned by knowledge_search."),
            },
            "evidence_list": {
                "type": "array",
                "description": "Complete Evidence objects returned by knowledge_search.",
                "items": {"type": "object"},
            },
            "question": {
                "type": "string",
                "description": "A global question to answer from related video clips.",
            },
            "top_k": {
                "type": "integer",
                "description": "Maximum number of clip captions to browse.",
                "default": 8,
            },
            "instruction": {
                "type": "string",
                "description": "Optional answer instruction, such as focus or output style.",
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    },
}

FRAME_INSPECT_SCHEMA = {
    "description": (
        "Inspect sampled frames from a ViKnow video Evidence object for fine-grained "
        "visual details."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "library_id": {
                "type": "string",
                "description": "Knowledge library id copied from the knowledge_search result.",
            },
            "asset_id": {
                "type": "string",
                "description": "Asset id copied from the selected search evidence.",
            },
            "evidence_id": {
                "type": "string",
                "description": "Optional selected evidence id for tracing.",
            },
            "title": {
                "type": "string",
                "description": "Optional selected evidence title for tracing.",
            },
            "time_range": {
                "type": "object",
                "description": "Video time range copied from the selected search evidence.",
                "properties": {
                    "start_seconds": {"type": "number"},
                    "end_seconds": {"type": "number"},
                },
                "required": ["start_seconds", "end_seconds"],
                "additionalProperties": False,
            },
            "question": {
                "type": "string",
                "description": "Focused visual question to answer from sampled frames.",
            },
            "sample_count": {
                "type": "integer",
                "description": "Number of evenly spaced frames to sample from the clip.",
                "default": 3,
            },
            "sampling_strategy": {
                "type": "string",
                "enum": ["uniform", "dense", "adaptive"],
                "description": "Frame sampling strategy.",
                "default": "uniform",
            },
            "crop_time_range": {
                "type": "object",
                "description": "Optional subrange inside the evidence time_range.",
                "properties": {
                    "start_seconds": {"type": "number"},
                    "end_seconds": {"type": "number"},
                },
                "required": ["start_seconds", "end_seconds"],
                "additionalProperties": False,
            },
            "number_frames": {
                "type": "boolean",
                "description": "Whether to overlay visible numeric labels on sampled frames.",
                "default": False,
            },
            "instruction": {
                "type": "string",
                "description": "Optional inspection instruction or visual focus.",
            },
        },
        "required": ["library_id", "asset_id", "time_range", "question"],
        "additionalProperties": False,
    },
}


class VideoInspectLocator(BaseModel):
    """Minimal backend locator for inspecting a video evidence range."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    library_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    time_range: EvidenceTimeRange
    evidence_id: str | None = None
    title: str | None = None


class AdvancedVideoInspectClipLocator(VideoInspectLocator):
    fps: str | float = "auto"
    crop: dict[str, float] | None = None


class LocateAnythingCountResult(BaseModel):
    """Aggregated LocateAnything count over sampled video frames."""

    query: str
    count: int = Field(ge=0)
    aggregation: str = "nonzero_mode"
    frame_count: int = Field(ge=0)
    nonzero_frame_count: int = Field(ge=0)
    counts: list[int] = Field(default_factory=list)
    selected_count_frequency: int = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    fps: float
    max_frames: int
    sampled_timestamps: list[float] = Field(default_factory=list)
    verify_reason: str = ""


class LocateAnythingFrameResult(BaseModel):
    """One sampled frame LocateAnything result."""

    frame_index: int = Field(ge=1)
    timestamp_seconds: float = Field(ge=0)
    count: int = Field(ge=0)
    detections: list[dict[str, Any]] = Field(default_factory=list)
    request_id: str | None = None
    text: str | None = None


class LocateAnythingToolResult(BaseModel):
    """Tool-level LocateAnything return contract with inspect-style claims."""

    model_config = ConfigDict(extra="allow")

    success: bool
    error: str | None = None
    claims: list[VideoVisualClaim] = Field(default_factory=list)
    count_result: LocateAnythingCountResult | None = None
    frame_results: list[LocateAnythingFrameResult] = Field(default_factory=list)


@dataclass
class MultimodalToolHandlers:
    """Handlers for multimodal video inspection tools."""

    knowledge: KnowledgeToolHandlers
    repo_root: Path | None = None
    tool_configs: ToolConfigs | None = None
    video_inspector_factory: Callable[[], EvidenceReviewer] | None = None
    video_review_extractor_factory: Callable[[], VideoEvidenceExtractor] | None = None
    advanced_video_extractor_factory: Callable[[], AdvancedVideoEvidenceExtractor] | None = None
    image_inspect_extractor_factory: Callable[[], ImageEvidenceExtractor] | None = None
    document_inspect_extractor_factory: Callable[[], ImageEvidenceExtractor] | None = None
    video_discovery_client_factory: Callable[[], VideoDiscoveryClient] | None = None
    video_url_builder: Callable[[EvidencePart], str] | None = None
    workspace_upload_path_resolver: Callable[[str], Path] | None = None
    actio: ActioClient | None = None
    _video_inspector: EvidenceReviewer | None = field(default=None, init=False, repr=False)
    _video_review_extractor: VideoEvidenceExtractor | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _advanced_video_extractor: AdvancedVideoEvidenceExtractor | None = field(
        default=None, init=False, repr=False
    )
    _image_inspect_extractor: ImageEvidenceExtractor | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _document_inspect_extractor: ImageEvidenceExtractor | None = field(
        default=None,
        init=False,
        repr=False,
    )
    text_inspect_extractor_factory: Callable[[], TextEvidenceInspector] | None = None
    _text_inspect_extractor: TextEvidenceInspector | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _evidence_transcriber: EvidenceTranscriber | None = field(default=None, init=False, repr=False)
    _evidence_ocr_reader: EvidenceOCRReader | None = field(default=None, init=False, repr=False)
    _locate_anything_client: LocateAnythingClient | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _video_discovery_service: VideoDiscoveryService | None = field(
        default=None, init=False, repr=False
    )

    def _require_actio(self) -> ActioClient:
        if self.actio is None:
            raise RuntimeError("actio client is required")
        return self.actio

    def resolved_repo_root(self) -> Path:
        return self.knowledge.resolved_repo_root()

    def resolve_workspace_upload_path(self, workspace_path: str) -> Path:
        if self.workspace_upload_path_resolver is not None:
            return self.workspace_upload_path_resolver(workspace_path)
        return Path(workspace_path)

    def get_video_inspector(self) -> EvidenceReviewer:
        if self.video_inspector_factory is not None:
            return self.video_inspector_factory()
        if self._video_inspector is None:
            if self.tool_configs is None or self.tool_configs.multimodal is None:
                raise RuntimeError("multimodal tool config is required for video_inspect")
            review_model = self.tool_configs.multimodal.review_model
            if review_model.provider != ModelProvider.OPENAI_COMPATIBLE:
                raise ValueError(f"unsupported video inspect provider: {review_model.provider}")
            base_url = review_model.resolve_base_url()
            if not base_url:
                raise RuntimeError("video inspect model base_url or base_url_env is required")
            inspection_client = self._require_actio().video_inspection_client(
                model=review_model.require_model(),
                base_url=base_url,
                api_key=review_model.require_api_key(),
                timeout_seconds=review_model.timeout_seconds,
                gateway=review_model.gateway,
            )
            self._video_inspector = EvidenceReviewer(
                repo_root=self.resolved_repo_root(),
                inspection_client=inspection_client,
                video_url_builder=self.video_url_builder,
            )
        return self._video_inspector

    def get_video_review_extractor(self) -> VideoEvidenceExtractor:
        if self.video_review_extractor_factory is not None:
            return self.video_review_extractor_factory()
        if self._video_review_extractor is None:
            if self.tool_configs is None or self.tool_configs.multimodal is None:
                raise RuntimeError("multimodal tool config is required for video_review")
            review_model = self.tool_configs.multimodal.review_model
            if review_model.provider != ModelProvider.OPENAI_COMPATIBLE:
                raise ValueError(f"unsupported video_review provider: {review_model.provider}")
            base_url = review_model.resolve_base_url()
            if not base_url:
                raise RuntimeError("video_review model base_url or base_url_env is required")
            from langchain_openai import ChatOpenAI

            chat_model = ChatOpenAI(
                model=review_model.require_model(),
                base_url=base_url,
                api_key=review_model.require_api_key(),
                temperature=0,
                timeout=review_model.timeout_seconds,
                extra_body=extra_body_with_priority(None, Priority.INTERACTIVE),
                **model_connection_options(review_model),
            )
            from agents.runtime.core.multimodal.video_review_extractor import VideoEvidenceExtractor

            self._video_review_extractor = VideoEvidenceExtractor(
                chat_model=chat_model,
                repo_root=self.resolved_repo_root(),
            )
        return self._video_review_extractor

    def get_advanced_video_extractor(self) -> AdvancedVideoEvidenceExtractor:
        if self.advanced_video_extractor_factory is not None:
            return self.advanced_video_extractor_factory()
        if self._advanced_video_extractor is None:
            if self.tool_configs is None or self.tool_configs.multimodal is None:
                raise RuntimeError("multimodal tool config is required for video_inspect_2_0")
            review_model = self.tool_configs.multimodal.review_model
            if review_model.provider != ModelProvider.OPENAI_COMPATIBLE:
                raise ValueError(
                    f"unsupported advanced video inspect provider: {review_model.provider}"
                )
            base_url = review_model.resolve_base_url()
            if not base_url:
                raise RuntimeError(
                    "advanced video inspect model base_url or base_url_env is required"
                )
            from langchain_openai import ChatOpenAI

            from agents.runtime.core.multimodal.advanced_video_extractor import (
                AdvancedVideoEvidenceExtractor,
            )

            self._advanced_video_extractor = AdvancedVideoEvidenceExtractor(
                chat_model=ChatOpenAI(
                    model=review_model.require_model(),
                    base_url=base_url,
                    api_key=review_model.require_api_key(),
                    temperature=0,
                    timeout=review_model.timeout_seconds,
                    extra_body=extra_body_with_priority(None, Priority.INTERACTIVE),
                    **model_connection_options(review_model),
                ),
                repo_root=self.resolved_repo_root(),
                grounding_client=self._optional_advanced_video_grounding_client(),
            )
        return self._advanced_video_extractor

    def _optional_advanced_video_grounding_client(self) -> LocateAnythingClient | None:
        if (
            self.tool_configs is None
            or self.tool_configs.multimodal is None
            or self.tool_configs.multimodal.locate_anything is None
        ):
            return None
        try:
            return self.get_locate_anything_client()
        except Exception as exc:
            logger.warning("video_inspect_2_0 grounding disabled: {}", exc)
            return None

    def get_image_inspect_extractor(self) -> ImageEvidenceExtractor:
        if self.image_inspect_extractor_factory is not None:
            return self.image_inspect_extractor_factory()
        if self._image_inspect_extractor is None:
            if self.tool_configs is None or self.tool_configs.multimodal is None:
                raise RuntimeError("multimodal tool config is required for image_inspect")
            review_model = self.tool_configs.multimodal.review_model
            if review_model.provider != ModelProvider.OPENAI_COMPATIBLE:
                raise ValueError(f"unsupported image_inspect provider: {review_model.provider}")
            base_url = review_model.resolve_base_url()
            if not base_url:
                raise RuntimeError("image_inspect model base_url or base_url_env is required")
            from langchain_openai import ChatOpenAI

            from agents.runtime.core.multimodal.image_review_extractor import ImageEvidenceExtractor

            chat_model = ChatOpenAI(
                model=review_model.require_model(),
                base_url=base_url,
                api_key=review_model.require_api_key(),
                temperature=0,
                timeout=review_model.timeout_seconds,
                extra_body=extra_body_with_priority(None, Priority.INTERACTIVE),
                **model_connection_options(review_model),
            )
            self._image_inspect_extractor = ImageEvidenceExtractor(
                chat_model=chat_model,
                repo_root=self.resolved_repo_root(),
            )
        return self._image_inspect_extractor

    def get_document_inspect_extractor(self) -> ImageEvidenceExtractor:
        if self.document_inspect_extractor_factory is not None:
            return self.document_inspect_extractor_factory()
        if self._document_inspect_extractor is None:
            if self.tool_configs is None or self.tool_configs.multimodal is None:
                raise RuntimeError("multimodal tool config is required for document_inspect")
            review_model = self.tool_configs.multimodal.review_model
            if review_model.provider != ModelProvider.OPENAI_COMPATIBLE:
                raise ValueError(f"unsupported document_inspect provider: {review_model.provider}")
            base_url = review_model.resolve_base_url()
            if not base_url:
                raise RuntimeError("document_inspect model base_url or base_url_env is required")
            from langchain_openai import ChatOpenAI

            from agents.runtime.core.multimodal.image_review_extractor import ImageEvidenceExtractor

            self._document_inspect_extractor = ImageEvidenceExtractor(
                chat_model=ChatOpenAI(
                    model=review_model.require_model(),
                    base_url=base_url,
                    api_key=review_model.require_api_key(),
                    temperature=0,
                    timeout=review_model.timeout_seconds,
                    extra_body=extra_body_with_priority(None, Priority.INTERACTIVE),
                    **model_connection_options(review_model),
                ),
                repo_root=self.resolved_repo_root(),
                tool_name="document_inspect",
            )
        return self._document_inspect_extractor

    def get_text_inspect_extractor(self) -> TextEvidenceInspector:
        if self.text_inspect_extractor_factory is not None:
            return self.text_inspect_extractor_factory()
        if self._text_inspect_extractor is None:
            if self.tool_configs is None or self.tool_configs.multimodal is None:
                raise RuntimeError("multimodal tool config is required for text_inspect")
            review_model = self.tool_configs.multimodal.review_model
            if review_model.provider != ModelProvider.OPENAI_COMPATIBLE:
                raise ValueError(f"unsupported text_inspect provider: {review_model.provider}")
            base_url = review_model.resolve_base_url()
            if not base_url:
                raise RuntimeError("text_inspect model base_url or base_url_env is required")
            from langchain_openai import ChatOpenAI

            from agents.runtime.core.multimodal.text_inspect_extractor import TextEvidenceInspector

            chat_model = ChatOpenAI(
                model=review_model.require_model(),
                base_url=base_url,
                api_key=review_model.require_api_key(),
                temperature=0,
                timeout=review_model.timeout_seconds,
                extra_body=extra_body_with_priority(None, Priority.INTERACTIVE),
                **model_connection_options(review_model),
            )
            self._text_inspect_extractor = TextEvidenceInspector(
                chat_model=chat_model,
                repo_root=self.resolved_repo_root(),
            )
        return self._text_inspect_extractor

    def get_evidence_transcriber(self) -> EvidenceTranscriber:
        if self._evidence_transcriber is None:
            if (
                self.tool_configs is None
                or self.tool_configs.multimodal is None
                or self.tool_configs.multimodal.asr is None
            ):
                raise RuntimeError("multimodal asr tool config is required for evidence_asr")
            asr_config = self.tool_configs.multimodal.asr
            base_url = asr_config.resolve_base_url()
            if not base_url:
                raise RuntimeError("asr base_url or base_url_env is required")
            if asr_config.provider == ModelProvider.OPENAI_COMPATIBLE:
                asr_client = self._require_actio().asr_client(
                    provider=str(asr_config.provider),
                    model=asr_config.require_model(),
                    base_url=base_url,
                    api_key=asr_config.require_api_key(),
                    timeout_seconds=asr_config.timeout_seconds,
                    vad_enabled=asr_config.vad_enabled,
                    vad_frame_ms=asr_config.vad_frame_ms,
                    vad_min_region_s=asr_config.vad_min_region_s,
                    vad_merge_gap_s=asr_config.vad_merge_gap_s,
                    vad_padding_s=asr_config.vad_padding_s,
                    vad_energy_threshold=asr_config.vad_energy_threshold,
                )
            elif asr_config.provider in {ModelProvider.GRANITE, ModelProvider.ARK}:
                asr_client = self._require_actio().asr_client(
                    provider=str(asr_config.provider),
                    model=asr_config.require_model(),
                    base_url=base_url,
                    api_key=asr_config.require_api_key(),
                    timeout_seconds=asr_config.timeout_seconds,
                    region_mode=asr_config.region_mode,
                    transport=asr_config.transport,
                    max_upload_mb=asr_config.max_upload_mb,
                    chunk_seconds=asr_config.chunk_seconds,
                    job_timeout_seconds=asr_config.job_timeout_seconds,
                    preparation_timeout_seconds=asr_config.preparation_timeout_seconds,
                    queue_timeout_seconds=asr_config.queue_timeout_seconds,
                    transcription_timeout_seconds=asr_config.transcription_timeout_seconds,
                    poll_interval_seconds=asr_config.poll_interval_seconds,
                )
            else:
                raise ValueError(f"unsupported asr provider: {asr_config.provider}")
            self._evidence_transcriber = EvidenceTranscriber(
                repo_root=self.resolved_repo_root(),
                asr_client=asr_client,
            )
        return self._evidence_transcriber

    def get_evidence_ocr_reader(self) -> EvidenceOCRReader:
        if self._evidence_ocr_reader is None:
            if (
                self.tool_configs is None
                or self.tool_configs.multimodal is None
                or self.tool_configs.multimodal.ocr is None
            ):
                raise RuntimeError("multimodal ocr tool config is required for evidence_ocr")
            ocr_config = self.tool_configs.multimodal.ocr
            base_url = ocr_config.resolve_base_url()
            if not base_url:
                raise RuntimeError("ocr base_url or base_url_env is required")
            ocr_kwargs: dict[str, Any] = {
                "provider": str(ocr_config.provider),
                "model": ocr_config.require_model(),
                "base_url": base_url,
                "api_key": ocr_config.require_api_key(),
                "timeout_seconds": ocr_config.timeout_seconds,
            }
            if ocr_config.provider == ModelProvider.OPENAI_COMPATIBLE:
                ocr_kwargs["priority"] = Priority.INTERACTIVE.vllm_priority
            self._evidence_ocr_reader = EvidenceOCRReader(
                repo_root=self.resolved_repo_root(),
                ocr_client=self._require_actio().ocr_client(**ocr_kwargs),
            )
        return self._evidence_ocr_reader

    def get_locate_anything_client(self) -> LocateAnythingClient:
        if self._locate_anything_client is None:
            if (
                self.tool_configs is None
                or self.tool_configs.multimodal is None
                or self.tool_configs.multimodal.locate_anything is None
            ):
                raise RuntimeError(
                    "multimodal locate_anything tool config is required for locate_anything"
                )
            locate_config = self.tool_configs.multimodal.locate_anything
            base_url = locate_config.resolve_base_url()
            if not base_url:
                raise RuntimeError("locate_anything base_url or base_url_env is required")
            self._locate_anything_client = self._require_actio().locate_anything_client(
                base_url=base_url,
                api_key=locate_config.require_api_key(),
                timeout_seconds=locate_config.timeout_seconds,
            )
        return self._locate_anything_client

    def get_video_discovery_service(self) -> VideoDiscoveryService:
        if self._video_discovery_service is None:
            if self.video_discovery_client_factory is not None:
                discovery_client = self.video_discovery_client_factory()
            else:
                if (
                    self.tool_configs is None
                    or self.tool_configs.multimodal is None
                    or self.tool_configs.multimodal.video_discovery is None
                ):
                    raise RuntimeError(
                        "multimodal video discovery tool config is required for "
                        "global_browse and frame_inspect"
                    )
                discovery_config = self.tool_configs.multimodal.video_discovery
                if discovery_config.provider != ModelProvider.OPENAI_COMPATIBLE:
                    raise ValueError(
                        f"unsupported video discovery provider: {discovery_config.provider}"
                    )
                base_url = discovery_config.resolve_base_url()
                if not base_url:
                    raise RuntimeError("video discovery base_url or base_url_env is required")
                discovery_client = self._require_actio().video_discovery_client(
                    model=discovery_config.require_model(),
                    base_url=base_url,
                    api_key=discovery_config.require_api_key(),
                    timeout_seconds=discovery_config.timeout_seconds,
                    gateway=discovery_config.gateway,
                )
            self._video_discovery_service = VideoDiscoveryService(
                repo_root=self.resolved_repo_root(),
                discovery_client=discovery_client,
                frames=self.actio,
            )
        return self._video_discovery_service

    def video_inspect(self, args: dict[str, Any]) -> str:
        from agents.runtime.core.tools.video_inspect import VideoInspectTool

        timeout = None
        if self.tool_configs is not None and self.tool_configs.multimodal is not None:
            timeout = self.tool_configs.multimodal.review_model.timeout_seconds
        return VideoInspectTool(self, execution_timeout_seconds=timeout).invoke(args)

    def video_inspect_2_0(self, args: dict[str, Any]) -> str:
        from agents.runtime.core.multimodal.advanced_video_claim import (
            AdvancedVideoClipInput,
            AdvancedVideoOptions,
        )

        started = perf_counter()
        question = str(args.get("question", "")).strip()
        try:
            if "clips" in args:
                raise ValueError(
                    "video_inspect_2_0 accepts one top-level video locator; "
                    "the agent must issue separate tool calls for separate evidence ranges"
                )
            locator = AdvancedVideoInspectClipLocator.model_validate(
                {
                    key: args.get(key)
                    for key in (
                        "library_id",
                        "asset_id",
                        "evidence_id",
                        "title",
                        "time_range",
                        "fps",
                        "crop",
                    )
                    if args.get(key) is not None
                }
            )
            evidence = self._video_inspect_evidence_from_locator(
                VideoInspectLocator.model_validate(
                    locator.model_dump(
                        include={"library_id", "asset_id", "time_range", "evidence_id", "title"}
                    )
                )
            )
            clip_input = AdvancedVideoClipInput(
                clip_id="clip-1", fps=locator.fps, crop=locator.crop
            )
            options = AdvancedVideoOptions(
                visual_budget=args.get("visual_budget", "balanced"),
                instruction=args.get("instruction"),
            )
        except Exception as exc:
            _log_inspect_tool_failed(
                "video_inspect_2_0", args, question, started=started, error=exc
            )
            return _tool_error_payload(
                "video_inspect_2_0",
                "invalid_video_locators",
                "A valid library_id, asset_id, and original time_range are required.",
                exc,
            )
        _log_inspect_tool_started("video_inspect_2_0", args, question, evidence=evidence)
        try:
            result = self.get_advanced_video_extractor().extract_video(
                evidence, question, clip_input=clip_input, options=options
            )
        except Exception as exc:
            _log_inspect_tool_failed(
                "video_inspect_2_0",
                args,
                question,
                evidence=evidence,
                started=started,
                error=exc,
            )
            return _tool_error_payload("video_inspect_2_0", "invalid_review_request", str(exc), exc)
        enriched = _enrich_evidence_with_video_url(evidence)
        result = _with_advanced_claim_citations(result, [enriched], [clip_input])
        _log_inspect_tool_completed(
            "video_inspect_2_0",
            args,
            question,
            evidence=enriched,
            result=result,
            started=started,
        )
        return ToolResult.ok(
            "video_inspect_2_0",
            {
                "result": result.model_dump(mode="json"),
                "evidence": enriched.model_dump(mode="json"),
            },
        ).to_json()

    def _video_inspect_evidence_from_locator(self, locator: VideoInspectLocator) -> Evidence:
        _require_library_allowed(locator.library_id)
        status = self._video_asset_status(locator)
        file_path = self._status_file_path(status)
        if not file_path:
            raise ValueError(
                f"asset status for asset_id={locator.asset_id} does not include file_path"
            )
        parse_mode = self._status_parse_mode(status)
        if parse_mode and parse_mode not in {"video", "sop_video"}:
            raise ValueError(
                f"asset_id={locator.asset_id} is not a video asset: parse_mode={parse_mode}"
            )
        return Evidence(
            id=locator.evidence_id or locator.asset_id,
            source="knowledge_search",
            title=locator.title or locator.asset_id,
            parts=[
                EvidencePart(
                    kind=EvidencePartKind.VIDEO,
                    uri=file_path,
                    mime_type="video/mp4",
                    time_range=locator.time_range,
                    metadata={
                        "library_id": locator.library_id,
                        "asset_id": locator.asset_id,
                    },
                )
            ],
            metadata={
                "library_id": locator.library_id,
                "asset_id": locator.asset_id,
                "file_path": file_path,
                "parse_mode": parse_mode,
            },
        )

    def _video_asset_status(self, locator: VideoInspectLocator) -> Any:
        if self.knowledge.knowledge_adapter is None:
            raise RuntimeError("knowledge adapter is not configured")
        status = self.knowledge.knowledge_adapter.get_asset_status(
            library_id=locator.library_id,
            asset_id=locator.asset_id,
        )
        if status is None:
            raise ValueError(f"video asset not found: {locator.asset_id}")
        return status

    @staticmethod
    def _status_file_path(status: Any) -> str:
        if isinstance(status, dict):
            return str(status.get("file_path") or "")
        return str(getattr(status, "file_path", "") or "")

    @staticmethod
    def _status_parse_mode(status: Any) -> str:
        if isinstance(status, dict):
            return str(status.get("parse_mode") or "")
        return str(getattr(status, "parse_mode", "") or "")

    def image_inspect(self, args: dict[str, Any]) -> str:
        started = perf_counter()
        question = str(args.get("question", "")).strip()
        try:
            evidence = self._resolve_chunk_evidence_or_parse(args, tool_name="image_inspect")
            if not _has_part_with_uri(evidence, EvidencePartKind.IMAGE):
                raise ValueError(
                    "image_inspect requires the selected evidence chunk to contain an IMAGE part"
                )
        except Exception as exc:
            _log_inspect_tool_failed(
                "image_inspect",
                args,
                question,
                started=started,
                error=exc,
            )
            return _tool_error_payload(
                "image_inspect",
                "invalid_evidence",
                (
                    "image_inspect requires a valid image evidence reference. Pass "
                    "library_id, asset_id, and evidence_id copied from the selected "
                    "knowledge_search evidence item, or one complete evidence object."
                ),
                exc,
            )
        _log_inspect_tool_started("image_inspect", args, question, evidence=evidence)
        try:
            result = self.get_image_inspect_extractor().extract(evidence, question)
        except Exception as exc:
            _log_inspect_tool_failed(
                "image_inspect",
                args,
                question,
                evidence=evidence,
                started=started,
                error=exc,
            )
            return _tool_error_payload(
                "image_inspect",
                "invalid_review_request",
                str(exc),
                exc,
            )
        result = _with_inspect_claim_citations("image_inspect", result, evidence)
        _log_inspect_tool_completed(
            "image_inspect",
            args,
            question,
            evidence=evidence,
            result=result,
            started=started,
        )
        return json.dumps(
            {
                "tool": "image_inspect",
                "result": result.model_dump(mode="json"),
                "evidence": evidence.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )

    def document_inspect(self, args: dict[str, Any]) -> str:
        started = perf_counter()
        question = str(args.get("question", "")).strip()
        try:
            evidence = self._resolve_chunk_evidence_or_parse(args, tool_name="document_inspect")
            source_path = _document_source_path(self, evidence)
            total_pages = (
                document_page_count(source_path) if source_path.suffix.lower() == ".pdf" else None
            )
            page_numbers, spreadsheet_context = _document_requested_pages(
                evidence, source_path, args.get("page_indices"), total_pages=total_pages
            )
        except Exception as exc:
            _log_inspect_tool_failed("document_inspect", args, question, started=started, error=exc)
            return _tool_error_payload(
                "document_inspect",
                "invalid_document_evidence",
                (
                    "document_inspect requires a selected document evidence reference with "
                    "a source page and a renderable PDF derivative or .xlsx source."
                ),
                exc,
            )

        _log_inspect_tool_started("document_inspect", args, question, evidence=evidence)
        try:
            all_claims = []
            for page_number in page_numbers:
                citation_evidence = evidence.model_copy(
                    update={"metadata": {**evidence.metadata, "document_inspect_page": page_number}}
                )
                with render_document_page(
                    source_path,
                    page_number=page_number,
                    spreadsheet_context=spreadsheet_context,
                ) as rendered_page:
                    page_evidence = citation_evidence.model_copy(
                        update={
                            "parts": [
                                *(
                                    [
                                        EvidencePart(
                                            kind=EvidencePartKind.TEXT,
                                            text=(
                                                "Text layer from this same source page "
                                                "(reading order may differ from visual layout):\n"
                                                + rendered_page.source_text
                                            ),
                                        )
                                    ]
                                    if rendered_page.source_text.strip()
                                    else (
                                        _text_parts(citation_evidence)
                                        if args.get("page_indices") is None
                                        else []
                                    )
                                ),
                                EvidencePart(
                                    kind=EvidencePartKind.IMAGE,
                                    uri=str(rendered_page.path),
                                    mime_type="image/png",
                                    metadata={"document_page": page_number},
                                ),
                            ]
                        }
                    )
                    result = self.get_document_inspect_extractor().extract(page_evidence, question)
                if not result.success:
                    raise RuntimeError(result.error or "document page inspection failed")
                result = _with_inspect_claim_citations(
                    "document_inspect", result, citation_evidence
                )
                all_claims.extend(result.claims)
            result = result.model_copy(update={"claims": all_claims})
        except Exception as exc:
            _log_inspect_tool_failed(
                "document_inspect",
                args,
                question,
                evidence=evidence,
                started=started,
                error=exc,
            )
            return _tool_error_payload(
                "document_inspect",
                "document_page_render_failed",
                str(exc),
                exc,
            )
        _log_inspect_tool_completed(
            "document_inspect",
            args,
            question,
            evidence=evidence,
            result=result,
            started=started,
        )
        return json.dumps(
            {
                "tool": "document_inspect",
                "result": result.model_dump(mode="json"),
                "evidence": evidence.model_dump(mode="json"),
                "inspected_pages": page_numbers,
                "inspected_page_indices": [page - 1 for page in page_numbers],
                "total_pages": total_pages,
            },
            ensure_ascii=False,
        )

    def locate_anything(self, args: dict[str, Any]) -> str:
        query = str(args.get("query") or args.get("question") or "").strip()
        if not query:
            return ToolResult.fail(
                "visual_ground",
                ToolErrorCode.INVALID_ARGUMENTS,
                "query is required",
            ).to_json()
        unsupported_reason = _unsupported_visual_ground_query_reason(query)
        if unsupported_reason is not None:
            return ToolResult.fail(
                "visual_ground",
                ToolErrorCode.UNSUPPORTED,
                unsupported_reason,
            ).to_json()
        if args.get("page_number") is not None and args.get("frame_time") is not None:
            return ToolResult.fail(
                "visual_ground",
                ToolErrorCode.INVALID_ARGUMENTS,
                "page_number and frame_time are mutually exclusive",
            ).to_json()
        try:
            with self._resolve_locate_anything_image(args) as resolved:
                image_path, source = resolved
                result = self.get_locate_anything_client().locate_path_async(
                    image_path,
                    query=query,
                    mode=_optional_string(args.get("mode")),
                    max_results=_optional_int(args.get("max_results")),
                    generation_mode=_optional_string(args.get("generation_mode")),
                )
                # 前端可视化：把抽出的帧图（若有）带回给前端展示
                if source.get("type") == "evidence_frame" and image_path.exists():
                    with contextlib.suppress(Exception):
                        source["frame_image_base64"] = base64.b64encode(
                            image_path.read_bytes()
                        ).decode("ascii")
        except Exception as exc:
            return _tool_error_payload(
                "visual_ground",
                "invalid_locate_anything_request",
                str(exc),
                exc,
            )
        result_payload = result.model_dump(mode="json")
        detections = result_payload.get("detections")
        count = len(detections) if isinstance(detections, list) else 0
        # 生成可引用的计数 claim（与 video_inspect 的 claims 同构），
        # 否则 LLM 无法按 skill 的 "claims 是最终事实来源" 规则引用 visual_ground 的计数。
        claims: list[VideoVisualClaim] = []
        boxes = [d.get("bbox") or d.get("box") for d in detections if isinstance(d, dict)]
        frame_time = float(
            source.get("frame_time") or _optional_float(args.get("frame_time")) or 0.0
        )
        if count > 0:
            boxes = [d.get("bbox") or d.get("box") for d in detections if isinstance(d, dict)]
            claim_text = (
                f"视觉定位检测到 {count} 个目标（query: {query}），"
                f"目标位置坐标：{json.dumps(boxes, ensure_ascii=False)}"
            )
        else:
            claim_text = f"视觉定位未检测到目标（query: {query}），检测数量为 0。"
        claims.append(
            VideoVisualClaim(
                claim=claim_text,
                timestamp_start=frame_time,
                timestamp_end=frame_time,
                entity_description=query,
                confidence=1.0 if count > 0 else 0.6,
                verify_reason="LocateAnything detection count",
            )
        )
        claim_result = VideoReviewResult(success=True, error=None, claims=claims)
        evidence_payload = source.get("evidence")
        if isinstance(evidence_payload, dict):
            try:
                claim_result = _with_inspect_claim_citations(
                    "visual_ground",
                    claim_result,
                    Evidence.model_validate(evidence_payload),
                )
            except ValidationError:
                logger.debug("visual_ground source evidence failed validation", exc_info=True)

        result_payload = {
            **result_payload,
            "success": True,
            "error": None,
            "claims": [c.model_dump(mode="json") for c in claim_result.claims],
            "count_result": {
                "query": query,
                "count": count,
                "aggregation": "single_image",
                "frame_count": 1,
                "nonzero_frame_count": 1 if count > 0 else 0,
                "counts": [count],
                "selected_count_frequency": 1,
                "confidence": 1.0,
                "fps": 0.0,
                "max_frames": 1,
                "sampled_timestamps": [],
                "verify_reason": "single image LocateAnything detection count",
            },
            "frame_results": [],
        }
        return ToolResult.ok(
            "visual_ground",
            {"source": source, "result": result_payload},
        ).to_json()

    @contextlib.contextmanager
    def _resolve_locate_anything_image(
        self,
        args: dict[str, Any],
    ) -> Iterator[tuple[Path, dict[str, Any]]]:
        workspace_path = str(args.get("workspace_path") or "").strip()
        if workspace_path:
            image_path = self.resolve_workspace_upload_path(workspace_path)
            yield (
                image_path,
                {
                    "type": "workspace",
                    "media_kind": "image",
                    "workspace_path": workspace_path,
                },
            )
            return

        evidence = self._resolve_chunk_evidence_or_parse(args, tool_name="visual_ground")
        page_number_raw = args.get("page_number")
        page_number = _optional_int(page_number_raw)
        if page_number_raw is not None and (page_number is None or page_number < 1):
            raise ValueError("page_number must be a positive one-based integer")
        if page_number is not None:
            with _render_visual_ground_document_page(
                self,
                evidence,
                page_number=page_number,
            ) as rendered:
                yield rendered
            return

        image_part = _first_part(evidence, EvidencePartKind.IMAGE)
        if image_part is not None and (image_part.uri or "").strip():
            image_path = Path(image_part.uri or "")
            if not image_path.is_absolute():
                image_path = self.resolved_repo_root() / image_path
            yield (
                image_path.resolve(),
                {
                    "type": "evidence",
                    "media_kind": "image",
                    "evidence": evidence.model_dump(mode="json"),
                    "image_uri": image_part.uri,
                },
            )
            return

        video_part = _first_part(evidence, EvidencePartKind.VIDEO)
        frame_time = _optional_float(args.get("frame_time"))
        if video_part is not None and (video_part.uri or "").strip():
            if frame_time is None:
                frame_time = _evidence_midpoint_seconds(evidence)
            video_path = Path(video_part.uri or "")
            if not video_path.is_absolute():
                video_path = self.resolved_repo_root() / video_path
            video_path = video_path.resolve()
            if not _path_exists_with_timeout(video_path, timeout_seconds=30.0):
                raise ValueError(f"visual_ground video file does not exist: {video_path}")
            import uuid as _uuid

            frame_dir = Path("/tmp/viknow-locate-frames") / _uuid.uuid4().hex
            frame_paths = self._require_actio().sample_video_frames_at_timestamps(
                video_path=video_path,
                output_dir=frame_dir,
                timestamps=[frame_time],
            )
            if not frame_paths:
                raise ValueError(f"visual_ground failed to extract frame at {frame_time}s")
            yield (
                frame_paths[0].resolve(),
                {
                    "type": "evidence_frame",
                    "media_kind": "video_frame",
                    "evidence": evidence.model_dump(mode="json"),
                    "video_uri": video_part.uri,
                    "frame_time": frame_time,
                },
            )
            return

        with _render_visual_ground_document_page(
            self,
            evidence,
            page_number=_document_page_number(evidence),
        ) as rendered:
            yield rendered

    def uploaded_image_search(self, args: dict[str, Any]) -> str:
        workspace_path = str(args.get("workspace_path") or "").strip()
        if not workspace_path:
            return ToolResult.fail(
                "uploaded_image_search",
                ToolErrorCode.INVALID_ARGUMENTS,
                "workspace_path is required",
            ).to_json()
        query = str(args.get("query") or args.get("question") or "").strip()
        if not query:
            return ToolResult.fail(
                "uploaded_image_search",
                ToolErrorCode.INVALID_ARGUMENTS,
                "query is required",
            ).to_json()
        library_id = str(args.get("library_id") or "").strip()
        if library_id and not is_library_allowed(library_id):
            error = ValueError(f"knowledge library is not allowed for this run: {library_id}")
            return _tool_error_payload(
                "uploaded_image_search",
                "library_not_allowed",
                str(error),
                error,
            )
        search_scope = "library_image_search" if library_id else "image_only"
        try:
            image_path = str(self.resolve_workspace_upload_path(workspace_path))
            image_query = self.knowledge.image_query_relevance(query, image_path)
            if not image_query.relevant:
                return json.dumps(
                    _uploaded_image_search_mismatch_payload(
                        search_scope=search_scope,
                        mismatch_type="image_query",
                        confidence=image_query.confidence,
                        visual_summary=image_query.image_description,
                        query_summary=image_query.query_summary,
                        mismatch_reason=image_query.reason,
                    ),
                    ensure_ascii=False,
                )

            if not library_id:
                return json.dumps(
                    _uploaded_image_search_answer_from_image_payload(
                        search_scope=search_scope,
                        confidence=image_query.confidence,
                        visual_summary=image_query.image_description,
                        query_summary=image_query.query_summary,
                        reason=image_query.reason,
                    ),
                    ensure_ascii=False,
                )

            search_result = self.knowledge.image_search(
                library_id=library_id,
                query=query,
                image_list=[image_path],
                mix_search=True,
            )
            if not _uploaded_image_search_has_evidence(search_result):
                return json.dumps(
                    _uploaded_image_search_mismatch_payload(
                        search_scope=search_scope,
                        mismatch_type="image_knowledge",
                        confidence=image_query.confidence,
                        visual_summary=image_query.image_description,
                        query_summary=image_query.query_summary,
                        mismatch_reason=(
                            "image_search returned no evidence related to the uploaded image."
                        ),
                    ),
                    ensure_ascii=False,
                )

            search_relevance = self.knowledge.image_search_result_relevance(
                query=query,
                image_path=image_path,
                search_result=search_result,
            )
            if not search_relevance.relevant:
                return json.dumps(
                    _uploaded_image_search_mismatch_payload(
                        search_scope=search_scope,
                        mismatch_type="image_knowledge",
                        confidence=search_relevance.confidence,
                        visual_summary=image_query.image_description,
                        query_summary=image_query.query_summary,
                        mismatch_reason=search_relevance.reason,
                    ),
                    ensure_ascii=False,
                )

            return json.dumps(
                _uploaded_image_search_knowledge_payload(
                    search_scope=search_scope,
                    confidence=search_relevance.confidence,
                    visual_summary=image_query.image_description,
                    query_summary=image_query.query_summary,
                    background_for_query_refine=search_relevance.background_for_query_refine,
                    image_search_results=_project_uploaded_image_search_results(
                        search_result,
                        relevant_evidence_ids=search_relevance.relevant_evidence_ids,
                        reason=search_relevance.reason,
                    ),
                ),
                ensure_ascii=False,
            )
        except Exception as exc:
            return ToolResult.fail(
                "uploaded_image_search",
                ToolErrorCode.UPSTREAM_ERROR,
                "uploaded image could not be processed",
                detail=str(exc),
                next_action={
                    "type": "ask_user_for_clarification",
                    "reason": "uploaded image could not be processed.",
                },
                data={
                    "search_scope": search_scope,
                    "decision": "cannot_process_image",
                    "mismatch_type": None,
                    "confidence": 0.0,
                    "visual_summary": "",
                    "query_summary": query,
                    "mismatch_reason": str(exc),
                    "search_hints": _empty_uploaded_image_search_hints(),
                    "image_search_results": [],
                },
            ).to_json()

    def text_inspect(self, args: dict[str, Any]) -> str:
        started = perf_counter()
        question = str(args.get("question", "")).strip()
        try:
            evidence = self._resolve_chunk_evidence_or_parse(args, tool_name="text_inspect")
            _require_text_inspect_text(evidence, args)
        except Exception as exc:
            _log_inspect_tool_failed(
                "text_inspect",
                args,
                question,
                started=started,
                error=exc,
            )
            return _tool_error_payload(
                "text_inspect",
                "invalid_evidence",
                (
                    "text_inspect requires a valid text evidence reference. Pass "
                    "library_id, asset_id, and evidence_id copied from the selected "
                    "knowledge_search evidence item, or one complete evidence object."
                ),
                exc,
            )
        _log_inspect_tool_started("text_inspect", args, question, evidence=evidence)
        try:
            result = self.get_text_inspect_extractor().extract(evidence, question)
        except Exception as exc:
            _log_inspect_tool_failed(
                "text_inspect",
                args,
                question,
                evidence=evidence,
                started=started,
                error=exc,
            )
            return _tool_error_payload(
                "text_inspect",
                "invalid_review_request",
                str(exc),
                exc,
            )
        result = _with_inspect_claim_citations("text_inspect", result, evidence)
        _log_inspect_tool_completed(
            "text_inspect",
            args,
            question,
            evidence=evidence,
            result=result,
            started=started,
        )
        return json.dumps(
            {
                "tool": "text_inspect",
                "result": result.model_dump(mode="json"),
                "evidence": evidence.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )

    def evidence_asr(self, args: dict[str, Any]) -> str:
        try:
            evidence = self._resolve_evidence_or_parse(args)
        except Exception as exc:
            return _tool_error_payload(
                "evidence_asr",
                "invalid_evidence",
                (
                    "evidence_asr requires a valid evidence reference. Pass "
                    "library_id and asset_id copied from the knowledge_search result, "
                    "or one complete item from the knowledge_search evidence list."
                ),
                exc,
            )
        language = _optional_string(args.get("language"))
        prompt = _optional_string(args.get("prompt"))
        try:
            result = self.get_evidence_transcriber().transcribe_evidence(
                evidence,
                language=language,
                prompt=prompt,
            )
        except Exception as exc:
            return _tool_error_payload("evidence_asr", "invalid_asr_request", str(exc), exc)
        inspect_result = _with_inspect_claim_citations(
            "evidence_asr",
            _asr_inspect_result_from_evidence(result),
            result,
        )
        return json.dumps(
            {
                "tool": "evidence_asr",
                "result": inspect_result.model_dump(mode="json"),
                "evidence": result.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )

    def evidence_ocr(self, args: dict[str, Any]) -> str:
        try:
            evidence = self._resolve_evidence_or_parse(args)
        except Exception as exc:
            return _tool_error_payload(
                "evidence_ocr",
                "invalid_evidence",
                (
                    "evidence_ocr requires a valid evidence reference. Pass "
                    "library_id and asset_id copied from the knowledge_search result, "
                    "or one complete item from the knowledge_search evidence list."
                ),
                exc,
            )
        instruction = _optional_string(args.get("instruction"))
        try:
            result = self.get_evidence_ocr_reader().recognize_evidence(
                evidence,
                instruction=instruction,
            )
        except Exception as exc:
            return _tool_error_payload("evidence_ocr", "invalid_ocr_request", str(exc), exc)
        inspect_result = _with_inspect_claim_citations(
            "evidence_ocr",
            _ocr_inspect_result_from_evidence(result),
            result,
        )
        return json.dumps(
            {
                "tool": "evidence_ocr",
                "result": inspect_result.model_dump(mode="json"),
                "evidence": result.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )

    def clip_search(self, args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        top_k = int(args.get("top_k") or 5)
        try:
            results = self._clip_search_evidence(args, query=query, top_k=top_k)
        except (JSONDecodeError, TypeError, RuntimeError, ValueError, ValidationError) as exc:
            return _tool_error_payload("clip_search", "invalid_clip_search_request", str(exc), exc)
        payload = {
            "tool": "clip_search",
            "query": query,
            "evidence": [item.model_dump(mode="json") for item in results],
        }
        return json.dumps(payload, ensure_ascii=False)

    def global_browse(self, args: dict[str, Any]) -> str:
        question = str(args.get("question", "")).strip()
        top_k = int(args.get("top_k") or 8)
        instruction = _optional_string(args.get("instruction"))
        try:
            service = self.get_video_discovery_service()
            clips = self._clip_search_evidence(args, query=question, top_k=top_k)
            result = service.answer_from_clips(
                question,
                clips=clips,
                instruction=instruction,
            )
        except (
            JSONDecodeError,
            TypeError,
            RuntimeError,
            ValueError,
            ValidationError,
        ) as exc:
            return _tool_error_payload(
                "global_browse",
                "invalid_global_browse_request",
                str(exc),
                exc,
            )
        return json.dumps(
            {
                "tool": "global_browse",
                "evidence": result.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )

    def _clip_search_evidence(
        self,
        args: dict[str, Any],
        *,
        query: str,
        top_k: int,
    ) -> list[Evidence]:
        evidence_items = self._parse_evidence_list_arg(args)
        if evidence_items:
            return evidence_items[:top_k]

        library_id = str(args.get("library_id") or self.knowledge.default_library_id()).strip()
        _require_library_allowed(library_id)
        asset_id = str(args.get("asset_id") or "").strip()
        if asset_id:
            return [self._resolve_evidence_or_parse(args)]

        if not query:
            raise ValueError(
                "clip_search requires query, evidence, evidence_list, or library_id and asset_id"
            )

        if self.knowledge.knowledge_adapter is not None:
            result = self.knowledge.knowledge_adapter.search(
                library_id=library_id,
                query=query,
                top_k=top_k,
            )
            return result.evidence
        raise RuntimeError("knowledge adapter is not configured")

    def _parse_evidence_list_arg(self, args: dict[str, Any]) -> list[Evidence]:
        raw_items = args.get("evidence_list")
        if raw_items is None:
            raw_items = args.get("evidences")
        if raw_items is None:
            raw_evidence = args.get("evidence")
            if raw_evidence is None:
                return []
            if isinstance(raw_evidence, str):
                parsed = json.loads(raw_evidence)
                if isinstance(parsed, list):
                    raw_items = parsed
                else:
                    return [self.parse_evidence_arg(parsed)]
            elif isinstance(raw_evidence, list):
                raw_items = raw_evidence
            else:
                return [self.parse_evidence_arg(raw_evidence)]

        if isinstance(raw_items, str):
            raw_items = json.loads(raw_items)
        if not isinstance(raw_items, list):
            raise ValueError("evidence_list must be a list of Evidence objects")
        return [self.parse_evidence_arg(item) for item in raw_items]

    def frame_inspect(self, args: dict[str, Any]) -> str:
        try:
            evidence = self._resolve_evidence_or_parse(args)
        except Exception as exc:
            return _tool_error_payload(
                "frame_inspect",
                "invalid_evidence",
                (
                    "frame_inspect requires a valid evidence reference. Pass "
                    "library_id, asset_id, and time_range copied from the knowledge_search "
                    "or clip_search result."
                ),
                exc,
            )
        question = str(args.get("question", "")).strip()
        sample_count = int(args.get("sample_count") or 3)
        sampling_strategy = str(args.get("sampling_strategy") or "uniform").strip()
        crop_time_range = _optional_time_range(args.get("crop_time_range"))
        number_frames = bool(args.get("number_frames") or False)
        instruction = _optional_string(args.get("instruction"))
        try:
            result = self.get_video_discovery_service().frame_inspect(
                evidence,
                question,
                sample_count=sample_count,
                sampling_strategy=sampling_strategy,
                crop_time_range=crop_time_range,
                number_frames=number_frames,
                instruction=instruction,
            )
        except Exception as exc:
            return _tool_error_payload(
                "frame_inspect", "invalid_frame_inspect_request", str(exc), exc
            )
        return json.dumps(
            {
                "tool": "frame_inspect",
                "evidence": result.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )

    def parse_evidence_arg(self, value: Any) -> Evidence:
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            evidence = Evidence.model_validate(value)
            _require_evidence_library_allowed(evidence)
            return evidence

        normalized = dict(value)
        if normalized.get("source") is None:
            normalized["source"] = "knowledge_search"
        if normalized.get("title") is None:
            normalized["title"] = normalized.get("id")
        if "parts" not in normalized:
            raise ValueError(
                "evidence must be a complete Evidence object returned by knowledge_search"
            )
        evidence = Evidence.model_validate(normalized)
        _require_evidence_library_allowed(evidence)
        return evidence

    def _resolve_evidence_or_parse(self, args: dict[str, Any]) -> Evidence:
        """Resolve evidence from either a full evidence object or a lightweight locator."""
        raw_evidence = args.get("evidence")
        if raw_evidence is not None:
            return self.parse_evidence_arg(raw_evidence)

        from agents.runtime.core.evidence import EvidenceLocator

        library_id = str(args.get("library_id") or "").strip()
        asset_id = str(args.get("asset_id") or "").strip()
        if not library_id or not asset_id:
            raise ValueError(
                "evidence reference requires library_id and asset_id "
                "(or pass a complete evidence object)"
            )
        _require_library_allowed(library_id)

        locator = EvidenceLocator(
            library_id=library_id,
            asset_id=asset_id,
            evidence_id=_optional_string(args.get("evidence_id")),
            title=_optional_string(args.get("title")),
            time_range=_optional_time_range(args.get("time_range")),
        )
        evidence = self.knowledge.resolve_evidence(locator)
        metadata = dict(evidence.metadata)
        metadata.setdefault("library_id", library_id)
        metadata.setdefault("asset_id", asset_id)
        return evidence.model_copy(update={"metadata": metadata})

    def _resolve_chunk_evidence_or_parse(self, args: dict[str, Any], *, tool_name: str) -> Evidence:
        raw_evidence = args.get("evidence")
        if raw_evidence is not None:
            return self.parse_evidence_arg(raw_evidence)

        evidence_id = _optional_string(args.get("evidence_id"))
        if not evidence_id:
            raise ValueError(
                f"{tool_name} requires evidence_id copied from the selected "
                "knowledge_search evidence item's id"
            )
        return self._resolve_evidence_or_parse(args)


def _require_library_allowed(library_id: str) -> None:
    if not is_library_allowed(library_id):
        raise ValueError(f"knowledge library is not allowed for this run: {library_id}")


def _require_evidence_library_allowed(evidence: Evidence) -> None:
    allowed = get_allowed_library_ids()
    if allowed is None:
        return
    library_id = str(evidence.metadata.get("library_id") or "").strip()
    if not library_id:
        raise ValueError("evidence does not identify a library allowed for this run")
    _require_library_allowed(library_id)


def _evidence_asset_identity(evidence: Evidence) -> tuple[str, str, EvidencePart | None]:
    video_part = next((part for part in evidence.parts if part.kind == EvidencePartKind.VIDEO), None)
    part_meta = video_part.metadata if video_part is not None else {}
    if not isinstance(part_meta, dict):
        part_meta = {}
    library_id = str(evidence.metadata.get("library_id") or "").strip()
    asset_id = str(
        part_meta.get("source_video_id")
        or part_meta.get("video_id")
        or evidence.metadata.get("source_video_id")
        or evidence.metadata.get("video_id")
        or part_meta.get("asset_id")
        or evidence.metadata.get("asset_id")
        or evidence.metadata.get("full_doc_id")
        or ""
    ).strip()
    if not asset_id and video_part is not None and video_part.uri:
        asset_id = Path(video_part.uri).stem
    return library_id, asset_id, video_part


def _library_asset_object_key(evidence: Evidence) -> str | None:
    """Catalog object key for this evidence. Never a /media URL."""

    library_id, asset_id, video_part = _evidence_asset_identity(evidence)
    return resolve_asset_object_key(
        library_id,
        asset_id,
        evidence.metadata.get("asset_path"),
        evidence.metadata.get("file_path"),
        evidence.metadata.get("source_uri"),
        video_part.uri if video_part is not None else None,
    )


def _video_source_with_object_key(
    evidence: Evidence,
    source: InspectClaimSource,
) -> InspectClaimSource:
    key = _library_asset_object_key(evidence)
    if not key:
        return source
    return source.model_copy(update={"asset_path": key})


def _library_asset_video_url(evidence: Evidence) -> str | None:
    """Object key of the original video. Playback range stays on the timestamp fields."""

    return _library_asset_object_key(evidence)


def _enrich_evidence_with_video_url(evidence: Evidence) -> Evidence:
    """Record the catalog object key on evidence. Drop any /media locator."""

    metadata = dict(evidence.metadata)
    metadata["video_timestamps_absolute"] = True
    key = _library_asset_object_key(evidence)
    if key:
        metadata["asset_path"] = key
        metadata["video_url"] = key
    else:
        metadata.pop("video_url", None)
    return evidence.model_copy(update={"metadata": metadata})


def _asr_inspect_result_from_evidence(evidence: Evidence) -> VideoReviewResult:
    transcript = _first_text(evidence).strip()
    if not transcript or evidence.metadata.get("asr_empty") is True:
        return VideoReviewResult(success=True, claims=[])
    video_part = _first_part(evidence, EvidencePartKind.VIDEO)
    clip_start = (
        video_part.time_range.start_seconds
        if video_part is not None and video_part.time_range is not None
        else 0.0
    )
    clip_end = (
        video_part.time_range.end_seconds
        if video_part is not None and video_part.time_range is not None
        else clip_start
    )
    claims: list[VideoVisualClaim] = []
    segments = evidence.metadata.get("asr_segments")
    if isinstance(segments, list):
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            text = str(segment.get("text") or "").strip()
            if not text:
                continue
            start = _optional_float(segment.get("start_seconds"))
            end = _optional_float(segment.get("end_seconds"))
            claims.append(
                VideoVisualClaim(
                    claim=text,
                    timestamp_start=clip_start + (start if start is not None else 0.0),
                    timestamp_end=clip_start + (end if end is not None else clip_end - clip_start),
                    confidence=_confidence_from_metadata(segment.get("metadata"), default=0.8),
                    verify_reason="ASR transcript segment",
                )
            )
    if not claims:
        claims.append(
            VideoVisualClaim(
                claim=transcript,
                timestamp_start=clip_start,
                timestamp_end=clip_end,
                confidence=_confidence_from_metadata(evidence.metadata, default=0.75),
                verify_reason="ASR transcript",
            )
        )
    return VideoReviewResult(success=True, claims=claims)


def _ocr_inspect_result_from_evidence(evidence: Evidence) -> ImageReviewResult | VideoReviewResult:
    text = _first_text(evidence).strip()
    if not text:
        return ImageReviewResult(success=True, claims=[])
    confidence = _confidence_from_metadata(evidence.metadata, key="ocr_confidence", default=0.8)
    video_part = _first_part(evidence, EvidencePartKind.VIDEO)
    if video_part is not None:
        start = video_part.time_range.start_seconds if video_part.time_range is not None else 0.0
        end = video_part.time_range.end_seconds if video_part.time_range is not None else start
        return VideoReviewResult(
            success=True,
            claims=[
                VideoVisualClaim(
                    claim=text,
                    timestamp_start=start,
                    timestamp_end=end,
                    confidence=confidence,
                    verify_reason="OCR recognized visible text",
                )
            ],
        )
    image_part = _first_part(evidence, EvidencePartKind.IMAGE)
    return ImageReviewResult(
        success=True,
        claims=[
            ImageVisualClaim(
                claim=text,
                source_image_id=(image_part.uri if image_part is not None else None) or evidence.id,
                confidence=confidence,
                verify_reason="OCR recognized visible text",
            )
        ],
    )


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_plain_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return value
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    raise TypeError(f"cannot convert {type(value).__name__} to dict")


def _confidence_from_metadata(
    metadata: Any,
    *,
    key: str = "confidence",
    default: float,
) -> float:
    if not isinstance(metadata, dict):
        return default
    confidence = _optional_float(metadata.get(key))
    if confidence is None:
        return default
    return max(0.0, min(1.0, confidence))


def _with_inspect_claim_citations(tool_name: str, result: Any, evidence: Evidence) -> Any:
    claims = getattr(result, "claims", None)
    if not isinstance(claims, list):
        return result
    enriched_claims = [
        _with_one_inspect_claim_citation(tool_name, claim, evidence, index)
        for index, claim in enumerate(claims, start=1)
    ]
    if hasattr(result, "model_copy"):
        return result.model_copy(update={"claims": enriched_claims})
    return result


def _with_advanced_claim_citations(
    result: Any, evidences: list[Evidence], clip_inputs: list[Any]
) -> Any:
    claims = getattr(result, "claims", None)
    if not isinstance(claims, list):
        return result
    evidence_by_clip = {
        item.clip_id: evidence for item, evidence in zip(clip_inputs, evidences, strict=True)
    }
    enriched_claims = []
    for index, claim in enumerate(claims, start=1):
        sources: list[InspectClaimSource] = []
        for support in getattr(claim, "supports", []) or []:
            evidence = evidence_by_clip.get(support.clip_id)
            if evidence is None:
                continue
            source_claim = claim.model_copy(
                update={
                    "timestamp_start": support.timestamp_start,
                    "timestamp_end": support.timestamp_end,
                }
            )
            source = _inspect_claim_source("video_inspect_2_0", source_claim, evidence)
            source = source.model_copy(
                update={
                    "metadata": {
                        **source.metadata,
                        "clip_id": support.clip_id,
                        "inspection_fps": support.fps,
                        "inspection_crop": (
                            support.crop.model_dump(mode="json") if support.crop else None
                        ),
                    }
                }
            )
            sources.append(source)
        if not sources:
            enriched_claims.append(claim)
            continue
        primary = sources[0].model_copy(
            update={
                "metadata": {
                    **sources[0].metadata,
                    "supporting_sources": [
                        source.model_dump(mode="json", exclude_none=True) for source in sources
                    ],
                }
            }
        )
        primary_evidence = evidence_by_clip[claim.supports[0].clip_id]
        render = _inspect_claim_render("video_inspect_2_0", claim, primary_evidence, primary)
        display_label = (
            render.label if len(sources) == 1 else f"{render.label} 等{len(sources)}个片段"
        )
        claim_id = _inspect_claim_id(
            "video_inspect_2_0",
            evidence_id="|".join(source.evidence_id or "" for source in sources),
            source=primary,
            claim_text=claim.claim,
            fallback_index=index,
        )
        enriched_claims.append(
            claim.model_copy(
                update={
                    "claim_id": claim_id,
                    "citation_index": getattr(claim, "citation_index", None) or index,
                    "agent_citation": getattr(claim, "agent_citation", "") or f"[{index}]",
                    "display_label": display_label,
                    "source": primary,
                    "sources": sources,
                    "render": render,
                }
            )
        )
    return result.model_copy(update={"claims": enriched_claims})


def _with_one_inspect_claim_citation(
    tool_name: str,
    claim: Any,
    evidence: Evidence,
    index: int,
) -> Any:
    if not hasattr(claim, "model_copy"):
        return claim
    claim = _with_video_claim_timestamps_in_evidence_range(tool_name, claim, evidence)
    source = _inspect_claim_source(tool_name, claim, evidence)
    render = _inspect_claim_render(tool_name, claim, evidence, source)
    display_label = render.label
    claim_id = _inspect_claim_id(
        tool_name,
        evidence_id=evidence.id,
        source=source,
        claim_text=str(getattr(claim, "claim", "") or ""),
        fallback_index=index,
    )
    return claim.model_copy(
        update={
            "claim_id": claim_id,
            "citation_index": getattr(claim, "citation_index", None) or index,
            "agent_citation": getattr(claim, "agent_citation", "") or f"[{index}]",
            "display_label": display_label,
            "source": source,
            "render": render,
        }
    )


def _with_video_claim_timestamps_in_evidence_range(
    tool_name: str,
    claim: Any,
    evidence: Evidence,
) -> Any:
    if tool_name not in {"video_inspect", "evidence_asr", "evidence_ocr"}:
        return claim
    if not hasattr(claim, "model_copy"):
        return claim
    video_part = _first_part(evidence, EvidencePartKind.VIDEO)
    if video_part is None or video_part.time_range is None:
        return claim
    ts_start = _optional_float_value(getattr(claim, "timestamp_start", None))
    ts_end = _optional_float_value(getattr(claim, "timestamp_end", None))
    if ts_start is None or ts_end is None:
        return claim
    clip_start = video_part.time_range.start_seconds
    clip_end = video_part.time_range.end_seconds
    clamped_start = min(max(ts_start, clip_start), clip_end)
    clamped_end = min(max(ts_end, clip_start), clip_end)
    if clamped_end < clamped_start:
        clamped_start, clamped_end = clamped_end, clamped_start
    if clamped_start == ts_start and clamped_end == ts_end:
        return claim
    return claim.model_copy(
        update={
            "timestamp_start": clamped_start,
            "timestamp_end": clamped_end,
        }
    )


def _inspect_claim_source(tool_name: str, claim: Any, evidence: Evidence) -> InspectClaimSource:
    library_id = _metadata_string(evidence.metadata, "library_id")
    asset_id = _metadata_string(evidence.metadata, "asset_id") or _metadata_string(
        evidence.metadata, "video_id"
    )
    if tool_name == "visual_ground" and evidence.metadata.get("visual_ground_page") is not None:
        page_number = int(evidence.metadata["visual_ground_page"])
        document_url = _library_asset_document_url(evidence)
        return InspectClaimSource(
            type="image",
            library_id=library_id,
            asset_id=asset_id,
            evidence_id=evidence.id,
            title=evidence.title,
            segment_id=evidence.id,
            source_image_id=f"page-{page_number}.png",
            uri=document_url,
            anchors=[
                _drop_empty_fields(
                    {
                        "kind": "document_page",
                        "document_url": document_url,
                        "page": page_number - 1,
                        "segment_id": evidence.id,
                    }
                )
            ],
            metadata={
                **_base_source_metadata(evidence),
                "document_page": page_number,
                "inspection_dpi": 144,
            },
        )
    if tool_name == "document_inspect":
        page_number = evidence.metadata["document_inspect_page"]
        document_url = _library_asset_document_url(evidence)
        source_metadata = {
            **_base_source_metadata(evidence),
            "document_page": page_number,
            "inspection_dpi": 144,
        }
        return InspectClaimSource(
            type="image",
            library_id=library_id,
            asset_id=asset_id,
            evidence_id=evidence.id,
            title=evidence.title,
            segment_id=evidence.id,
            source_image_id=f"page-{page_number}.png",
            uri=document_url,
            anchors=[
                _drop_empty_fields(
                    {
                        "kind": "document_page",
                        "document_url": document_url,
                        "page": page_number - 1,
                        "segment_id": evidence.id,
                    }
                )
            ],
            metadata=source_metadata,
        )
    if tool_name == "text_inspect":
        source_segments = getattr(claim, "source_segments", []) or []
        segment_id = str(source_segments[0]) if source_segments else evidence.id
        source_metadata = _base_source_metadata(evidence)
        source_metadata["source_segments"] = [str(item) for item in source_segments]
        evidence_spans = _text_evidence_span_payloads(claim)
        if evidence_spans:
            source_metadata["evidence_spans"] = evidence_spans
        verified_source_segments = [
            str(item) for item in getattr(claim, "verified_source_segments", []) or []
        ]
        unverified_source_segments = [
            str(item) for item in getattr(claim, "unverified_source_segments", []) or []
        ]
        if verified_source_segments:
            source_metadata["verified_source_segments"] = verified_source_segments
        if unverified_source_segments:
            source_metadata["unverified_source_segments"] = unverified_source_segments
        source_metadata["segment_content"] = _segment_text(evidence, segment_id)
        video_part = _first_part(evidence, EvidencePartKind.VIDEO)
        if video_part is not None:
            clip_start = (
                video_part.time_range.start_seconds if video_part.time_range is not None else None
            )
            clip_end = (
                video_part.time_range.end_seconds if video_part.time_range is not None else None
            )
            if clip_start is not None:
                source_metadata["clip_start_seconds"] = clip_start
            if clip_end is not None:
                source_metadata["clip_end_seconds"] = clip_end
            return _video_source_with_object_key(
                evidence,
                InspectClaimSource(
                    type="video",
                    library_id=library_id,
                    asset_id=asset_id,
                    evidence_id=evidence.id,
                    title=evidence.title,
                    segment_id=segment_id,
                    timestamp_start=clip_start,
                    timestamp_end=clip_end,
                    uri=video_part.uri,
                    anchors=_video_source_anchors(
                        evidence,
                        uri=video_part.uri,
                        absolute_start=clip_start,
                        absolute_end=clip_end,
                    ),
                    metadata=source_metadata,
                ),
            )
        image_part = _first_part(evidence, EvidencePartKind.IMAGE)
        if image_part is not None or _source_refs(evidence, kind="image"):
            image_uri = image_part.uri if image_part is not None else None
            source_image_id = image_uri or evidence.id
            return InspectClaimSource(
                type="image",
                library_id=library_id,
                asset_id=asset_id,
                evidence_id=evidence.id,
                title=evidence.title,
                segment_id=segment_id,
                source_image_id=source_image_id,
                uri=image_uri,
                anchors=_image_source_anchors(
                    evidence,
                    source_image_id=source_image_id,
                    image_uri=image_uri,
                    include_document_context=False,
                ),
                metadata=source_metadata,
            )
        return InspectClaimSource(
            type="text",
            library_id=library_id,
            asset_id=asset_id,
            evidence_id=evidence.id,
            title=evidence.title,
            segment_id=segment_id,
            anchors=_text_source_anchors(evidence, source_segments or [segment_id]),
            metadata=source_metadata,
        )
    if tool_name == "image_inspect" or (
        tool_name in {"visual_ground", "evidence_ocr"}
        and _first_part(evidence, EvidencePartKind.IMAGE) is not None
    ):
        image_part = _first_part(evidence, EvidencePartKind.IMAGE)
        source_image_id = str(getattr(claim, "source_image_id", "") or "")
        image_uri = image_part.uri if image_part is not None else None
        source_metadata = _base_source_metadata(evidence)
        return InspectClaimSource(
            type="image",
            library_id=library_id,
            asset_id=asset_id,
            evidence_id=evidence.id,
            title=evidence.title,
            segment_id=evidence.id,
            source_image_id=source_image_id or image_uri,
            uri=image_uri,
            anchors=_image_source_anchors(
                evidence,
                source_image_id=source_image_id or image_uri,
                image_uri=image_uri,
            ),
            metadata=source_metadata,
        )
    video_part = _first_part(evidence, EvidencePartKind.VIDEO)
    ts_start = float(getattr(claim, "timestamp_start", 0) or 0)
    ts_end = float(getattr(claim, "timestamp_end", 0) or 0)
    clip_start = (
        video_part.time_range.start_seconds
        if video_part is not None and video_part.time_range is not None
        else None
    )
    clip_end = (
        video_part.time_range.end_seconds
        if video_part is not None and video_part.time_range is not None
        else None
    )
    metadata: dict[str, Any] = {}
    if clip_start is not None:
        metadata["clip_start_seconds"] = clip_start
    if clip_end is not None:
        metadata["clip_end_seconds"] = clip_end
    metadata["absolute_timestamp_start"] = ts_start
    metadata["absolute_timestamp_end"] = ts_end
    if clip_start is not None:
        metadata["relative_timestamp_start"] = ts_start - clip_start
    if clip_end is not None:
        metadata["relative_timestamp_end"] = ts_end - clip_start if clip_start is not None else None
    metadata.update(_base_source_metadata(evidence))
    return _video_source_with_object_key(
        evidence,
        InspectClaimSource(
            type="video",
            library_id=library_id,
            asset_id=asset_id,
            evidence_id=evidence.id,
            title=evidence.title,
            segment_id=evidence.id,
            timestamp_start=ts_start,
            timestamp_end=ts_end,
            uri=video_part.uri if video_part is not None else None,
            anchors=_video_source_anchors(
                evidence,
                uri=video_part.uri if video_part is not None else None,
                absolute_start=ts_start,
                absolute_end=ts_end,
            ),
            metadata=metadata,
        ),
    )


def _inspect_claim_render(
    tool_name: str,
    claim: Any,
    evidence: Evidence,
    source: InspectClaimSource,
) -> InspectClaimRender:
    if source.type == "image":
        source_label = source.source_image_id or source.uri or evidence.id
        label = f"图片 {Path(source_label).name or source_label}"
        return InspectClaimRender(kind="image", label=label, url=source.uri)
    if source.type == "video":
        ts_start = float(source.timestamp_start or 0)
        ts_end = float(source.timestamp_end or 0)
        label = f"视频 {_format_seconds(ts_start)}-{_format_seconds(ts_end)}"
        return InspectClaimRender(
            kind="video_clip",
            label=label,
            url=_metadata_string(evidence.metadata, "video_url")
            or _library_asset_video_url(evidence),
        )
    if tool_name == "text_inspect":
        label = f"文本 {source.segment_id or evidence.id}"
        return InspectClaimRender(
            kind="text_quote",
            label=label,
            text_quote=_text_render_quote(claim),
        )
    ts_start = float(source.timestamp_start or 0)
    ts_end = float(source.timestamp_end or 0)
    label = f"视频 {_format_seconds(ts_start)}-{_format_seconds(ts_end)}"
    return InspectClaimRender(
        kind="video_clip",
        label=label,
        url=_metadata_string(evidence.metadata, "video_url"),
    )


def _inspect_claim_id(
    tool_name: str,
    *,
    evidence_id: str,
    source: InspectClaimSource,
    claim_text: str,
    fallback_index: int,
) -> str:
    source_payload = source.model_dump(mode="json", exclude_none=True)
    raw = json.dumps(
        {
            "tool": tool_name,
            "evidence_id": evidence_id,
            "source": source_payload,
            "claim": claim_text,
            "index": fallback_index,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"claim_{sha1(raw.encode('utf-8')).hexdigest()[:12]}"


def _first_part(evidence: Evidence, kind: EvidencePartKind) -> EvidencePart | None:
    return next((part for part in evidence.parts if part.kind == kind), None)


def _first_text(evidence: Evidence) -> str:
    text_part = _first_part(evidence, EvidencePartKind.TEXT)
    return text_part.text if text_part is not None and text_part.text is not None else ""


def _text_parts(evidence: Evidence) -> list[EvidencePart]:
    return [part for part in evidence.parts if part.kind == EvidencePartKind.TEXT]


def _document_render_location(
    evidence: Evidence,
    source_path: Path,
) -> tuple[int, SpreadsheetRenderContext | None]:
    if source_path.suffix.lower() != ".xlsx":
        return _document_page_number(evidence), None

    for location in _document_locations(evidence):
        sheet_index = _location_page(location)
        row_index = _location_index(location, "row")
        column_start = _location_index(location, "col_start")
        column_end = _location_index(location, "col_end")
        if (
            sheet_index is not None
            and sheet_index >= 0
            and row_index is not None
            and row_index >= 0
            and column_start is not None
            and column_start >= 0
            and column_end is not None
            and column_end >= column_start
        ):
            return (
                sheet_index + 1,
                SpreadsheetRenderContext(
                    sheet_index=sheet_index,
                    row_index=row_index,
                    column_start=column_start,
                    column_end=column_end,
                ),
            )
    raise ValueError(
        f".xlsx evidence {evidence.id!r} does not include a worksheet and bounded source location"
    )


def _document_requested_pages(
    evidence: Evidence,
    source_path: Path,
    page_indices: object,
    *,
    total_pages: int | None,
) -> tuple[list[int], SpreadsheetRenderContext | None]:
    if page_indices is not None:
        if total_pages is None:
            raise ValueError("explicit page_indices require a PDF document")
        if (
            not isinstance(page_indices, list)
            or not 1 <= len(page_indices) <= MAX_DOCUMENT_INSPECT_PAGES
            or any(type(page) is not int or not 0 <= page < total_pages for page in page_indices)
        ):
            raise ValueError(
                f"page_indices must contain 1-8 integer indices between 0 and {total_pages - 1}"
            )
        if len(set(page_indices)) != len(page_indices):
            raise ValueError("page_indices must be unique")
        return [page + 1 for page in sorted(page_indices)], None
    page_number, spreadsheet_context = _document_render_location(evidence, source_path)
    if spreadsheet_context is not None:
        return [page_number], spreadsheet_context
    pages = _document_page_numbers(evidence)
    if len(pages) > MAX_DOCUMENT_INSPECT_PAGES:
        raise ValueError(
            f"retrieved evidence spans {len(pages)} pages; use page_indices to inspect "
            f"at most {MAX_DOCUMENT_INSPECT_PAGES} pages per call"
        )
    if total_pages is not None and any(page > total_pages for page in pages):
        raise ValueError("retrieved evidence points outside the source PDF")
    return pages, None


def _document_page_number(evidence: Evidence) -> int:
    return _document_page_numbers(evidence)[0]


def _document_page_numbers(evidence: Evidence) -> list[int]:
    pages: list[int] = []
    for location in _document_locations(evidence):
        page_index = _location_index(location, "page")
        if page_index is not None:
            if page_index < 0:
                raise ValueError("document page index must be nonnegative")
            pages.append(page_index + 1)
            continue
        page_number = _location_index(location, "page_number")
        if page_number is not None:
            if page_number < 1:
                raise ValueError("document page number must be positive")
            pages.append(page_number)
    if not pages:
        raise ValueError(f"document evidence {evidence.id!r} does not include a source page")
    return sorted(set(pages))


def _document_locations(evidence: Evidence) -> list[dict[str, Any]]:
    locations: list[dict[str, Any]] = [evidence.metadata]
    for key in ("locations", "source_refs"):
        value = evidence.metadata.get(key)
        if isinstance(value, list):
            locations.extend(item for item in value if isinstance(item, dict))
    return locations


def _location_index(location: dict[str, Any], key: str) -> int | None:
    value = location.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    metadata = location.get("metadata")
    if isinstance(metadata, dict):
        return _location_index(metadata, key)
    return None


@contextlib.contextmanager
def _render_visual_ground_document_page(
    self: MultimodalToolHandlers,
    evidence: Evidence,
    *,
    page_number: int,
) -> Iterator[tuple[Path, dict[str, Any]]]:
    source_path = _visual_ground_document_source_path(self, evidence)
    if source_path.suffix.lower() != ".pdf":
        raise ValueError(
            "visual_ground document input requires a PDF source, "
            f"got {source_path.suffix or 'no extension'}"
        )
    citation_evidence = evidence.model_copy(
        update={"metadata": {**evidence.metadata, "visual_ground_page": page_number}}
    )
    with render_document_page(source_path, page_number=page_number) as rendered_page:
        yield (
            rendered_page.path,
            {
                "type": "document_page",
                "media_kind": "document_page",
                "evidence": citation_evidence.model_dump(mode="json"),
                "document_uri": str(source_path),
                "page_number": page_number,
            },
        )


def _visual_ground_document_source_path(
    self: MultimodalToolHandlers,
    evidence: Evidence,
) -> Path:
    file_part = _first_part(evidence, EvidencePartKind.FILE)
    if file_part is not None and (file_part.uri or "").strip():
        source_path = Path(file_part.uri or "")
        if not source_path.is_absolute():
            source_path = self.resolved_repo_root() / source_path
        source_path = source_path.resolve()
        if _path_exists_with_timeout(source_path, timeout_seconds=30.0):
            return source_path
    try:
        return _document_source_path(self, evidence)
    except Exception as exc:
        raise ValueError(
            "visual_ground document evidence requires a local PDF file or a "
            "resolvable knowledge asset"
        ) from exc


def _document_source_path(self: MultimodalToolHandlers, evidence: Evidence) -> Path:
    library_id = _metadata_string(evidence.metadata, "library_id")
    asset_id = _metadata_string(evidence.metadata, "asset_id") or _metadata_string(
        evidence.metadata, "full_doc_id"
    )
    adapter = self.knowledge.knowledge_adapter
    if adapter is None or not library_id or not asset_id:
        raise ValueError("document evidence does not identify a resolvable knowledge asset")
    source_path = adapter.resolve_document_render_path(library_id=library_id, asset_id=asset_id)
    if source_path is None:
        raise FileNotFoundError(f"no renderable source found for document asset {asset_id}")
    return Path(source_path)


def _require_text_inspect_text(evidence: Evidence, args: dict[str, Any]) -> None:
    text_part = _first_part(evidence, EvidencePartKind.TEXT)
    if text_part is None:
        raise ValueError(
            "text_inspect resolved evidence has no TEXT part. "
            f"library_id={args.get('library_id')!r}, asset_id={args.get('asset_id')!r}, "
            f"evidence_id={args.get('evidence_id')!r}, resolved_evidence_id={evidence.id!r}, "
            f"resolved_part_kinds={_evidence_part_kinds(evidence)}. "
            "Check that asset_id and evidence_id were copied from the same "
            "knowledge_search evidence item; do not pass segment_id or document_id as evidence_id."
        )
    if not (text_part.text or "").strip():
        raise ValueError(
            "text_inspect resolved evidence TEXT part is empty. "
            f"library_id={args.get('library_id')!r}, asset_id={args.get('asset_id')!r}, "
            f"evidence_id={args.get('evidence_id')!r}, resolved_evidence_id={evidence.id!r}. "
            "Use a knowledge_search evidence item whose text part contains chunk_content, "
            "or pass the complete evidence object returned by knowledge_search."
        )


def _evidence_part_kinds(evidence: Evidence) -> list[str]:
    return [
        str(part.kind.value if hasattr(part.kind, "value") else part.kind)
        for part in evidence.parts
    ]


def _has_part_with_uri(evidence: Evidence, kind: EvidencePartKind) -> bool:
    return any(part.kind == kind and (part.uri or "").strip() for part in evidence.parts)


def _empty_uploaded_image_search_hints() -> dict[str, Any]:
    return {
        "visible_text": [],
        "entities": [],
        "objects": [],
        "background_for_query_refine": "",
        "query_rewrite": "",
    }


def _uploaded_image_search_mismatch_payload(
    *,
    search_scope: str,
    mismatch_type: str,
    confidence: float,
    visual_summary: str,
    query_summary: str,
    mismatch_reason: str,
) -> dict[str, Any]:
    return {
        "tool": "uploaded_image_search",
        "status": "ok",
        "search_scope": search_scope,
        "decision": "mismatch",
        "mismatch_type": mismatch_type,
        "confidence": confidence,
        "visual_summary": visual_summary,
        "query_summary": query_summary,
        "mismatch_reason": mismatch_reason,
        "search_hints": _empty_uploaded_image_search_hints(),
        "image_search_results": [],
        "next_action": {
            "type": "ask_user_for_clarification",
            "reason": mismatch_reason,
        },
    }


def _uploaded_image_search_answer_from_image_payload(
    *,
    search_scope: str,
    confidence: float,
    visual_summary: str,
    query_summary: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "tool": "uploaded_image_search",
        "status": "ok",
        "search_scope": search_scope,
        "decision": "answer_from_image",
        "mismatch_type": None,
        "confidence": confidence,
        "visual_summary": visual_summary,
        "query_summary": query_summary,
        "mismatch_reason": None,
        "search_hints": _empty_uploaded_image_search_hints(),
        "image_search_results": [],
        "next_action": {
            "type": "final_answer",
            "reason": reason,
        },
    }


def _uploaded_image_search_knowledge_payload(
    *,
    search_scope: str,
    confidence: float,
    visual_summary: str,
    query_summary: str,
    background_for_query_refine: str,
    image_search_results: list[dict[str, Any]],
) -> dict[str, Any]:
    search_hints = _empty_uploaded_image_search_hints()
    search_hints["background_for_query_refine"] = background_for_query_refine
    return {
        "tool": "uploaded_image_search",
        "status": "ok",
        "search_scope": search_scope,
        "decision": "search_knowledge",
        "mismatch_type": None,
        "confidence": confidence,
        "visual_summary": visual_summary,
        "query_summary": query_summary,
        "mismatch_reason": None,
        "search_hints": search_hints,
        "image_search_results": image_search_results,
        "next_action": {
            "type": "query_refine",
            "reason": "uploaded image and image search results are relevant to the query.",
        },
    }


def _uploaded_image_search_has_evidence(search_result: list[Any]) -> bool:
    return any(isinstance(group, dict) and group.get("evidence") for group in search_result)


def _project_uploaded_image_search_results(
    search_result: list[Any],
    *,
    relevant_evidence_ids: list[str],
    reason: str,
) -> list[dict[str, Any]]:
    relevant = {str(item) for item in relevant_evidence_ids if str(item).strip()}
    projected: list[dict[str, Any]] = []
    for group in search_result:
        if not isinstance(group, dict):
            continue
        library_id = str(group.get("library_id") or "")
        for evidence in group.get("evidence") or []:
            if not isinstance(evidence, dict):
                continue
            evidence_id = str(evidence.get("id") or "")
            if relevant and evidence_id not in relevant:
                continue
            projected.append(
                {
                    "library_id": library_id,
                    "asset_id": _uploaded_image_search_evidence_asset_id(evidence),
                    "evidence_id": evidence_id,
                    "score": evidence.get("score"),
                    "reason": reason,
                }
            )
    return projected


def _uploaded_image_search_evidence_asset_id(evidence: dict[str, Any]) -> str:
    metadata = evidence.get("metadata")
    if isinstance(metadata, dict) and metadata.get("asset_id"):
        return str(metadata["asset_id"])
    if evidence.get("asset_id"):
        return str(evidence["asset_id"])
    return ""


def _base_source_metadata(evidence: Evidence) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in (
        "chunk_id",
        "full_doc_id",
        "file_path",
        "asset_id",
        "video_id",
        "source_video_id",
        "begin_ts",
        "end_ts",
        "asset_path",
        "video_url",
    ):
        value = evidence.metadata.get(key)
        if value not in (None, ""):
            metadata[key] = value
    chunk_content = _first_text(evidence)
    if chunk_content:
        metadata["chunk_content"] = chunk_content
    return metadata


def _text_source_anchors(evidence: Evidence, segment_ids: list[str]) -> list[dict[str, Any]]:
    image_part = _first_part(evidence, EvidencePartKind.IMAGE)
    if image_part is not None or _source_refs(evidence, kind="image"):
        return _image_source_anchors(
            evidence,
            source_image_id=image_part.uri if image_part is not None else None,
            image_uri=image_part.uri if image_part is not None else None,
            include_document_context=False,
        )
    anchors: list[dict[str, Any]] = []
    locations = evidence.metadata.get("locations")
    document_path = _metadata_string(evidence.metadata, "file_path")
    document_url = _library_asset_document_url(evidence)
    if isinstance(locations, list):
        for segment_id in segment_ids:
            for location in locations:
                if not isinstance(location, dict):
                    continue
                if str(location.get("segment_id") or "") != str(segment_id):
                    continue
                anchors.append(
                    _drop_empty_fields(
                        {
                            "kind": "text_span",
                            "document_path": document_path,
                            "document_url": document_url,
                            "page": _location_page(location),
                            "bbox": _location_bbox(location),
                            "page_size": _location_page_size(location),
                            "segment_id": segment_id,
                            "text": str(location.get("text") or ""),
                        }
                    )
                )
    if anchors:
        return anchors
    return [
        _drop_empty_fields(
            {
                "kind": "text_span",
                "document_path": document_path,
                "document_url": document_url,
                "segment_id": str(segment_ids[0]) if segment_ids else evidence.id,
                "text": _segment_text(evidence, str(segment_ids[0])) if segment_ids else None,
            }
        )
    ]


def _image_source_anchors(
    evidence: Evidence,
    *,
    source_image_id: str | None,
    image_uri: str | None,
    include_document_context: bool = True,
) -> list[dict[str, Any]]:
    document_path = _metadata_string(evidence.metadata, "file_path")
    document_url = _library_asset_document_url(evidence)
    refs = _source_refs(evidence, kind="image")
    anchors: list[dict[str, Any]] = []
    for ref in refs:
        ref_uri = str(ref.get("uri") or "")
        if source_image_id and not _image_ref_matches(ref, source_image_id):
            continue
        resolved_source_image_id = (
            source_image_id or _image_ref_identifier(ref) or ref_uri or image_uri
        )
        anchors.append(
            _drop_empty_fields(
                {
                    "kind": "image_region",
                    "document_path": document_path if include_document_context else None,
                    "document_url": document_url if include_document_context else None,
                    "page": _location_page(ref),
                    "bbox": _location_bbox(ref),
                    "page_size": _location_page_size(ref),
                    "image_uri": ref_uri or image_uri,
                    "image_url": _library_asset_image_url(
                        evidence,
                        source_image_id=resolved_source_image_id,
                        image_uri=ref_uri or image_uri,
                    ),
                    "source_image_id": resolved_source_image_id,
                }
            )
        )
    if anchors:
        return anchors
    return [
        _drop_empty_fields(
            {
                "kind": "image_region",
                "document_path": document_path,
                "document_url": document_url,
                "image_uri": image_uri,
                "image_url": _library_asset_image_url(
                    evidence,
                    source_image_id=source_image_id,
                    image_uri=image_uri,
                ),
                "source_image_id": source_image_id or image_uri,
            }
        )
    ]


def _video_source_anchors(
    evidence: Evidence,
    *,
    uri: str | None,
    absolute_start: Any,
    absolute_end: Any,
) -> list[dict[str, Any]]:
    object_key = _library_asset_object_key(evidence)
    start = _optional_float_value(absolute_start)
    end = _optional_float_value(absolute_end)
    return [
        _drop_empty_fields(
            {
                "kind": "video_clip",
                "asset_path": object_key,
                "video_url": object_key,
                "uri": uri,
                "evidence_id": evidence.id,
                "timestamp_start": start,
                "timestamp_end": end,
                "absolute_start_seconds": start,
                "absolute_end_seconds": end,
            }
        )
    ]


def _source_refs(evidence: Evidence, *, kind: str) -> list[dict[str, Any]]:
    refs = evidence.metadata.get("source_refs")
    if not isinstance(refs, list):
        return []
    return [ref for ref in refs if isinstance(ref, dict) and str(ref.get("kind") or "") == kind]


def _location_page(location: dict[str, Any]) -> int | None:
    for key in ("page", "page_num", "page_number"):
        value = location.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    metadata = location.get("metadata")
    if isinstance(metadata, dict):
        return _location_page(metadata)
    return None


def _location_bbox(location: dict[str, Any]) -> list[float] | None:
    for key in ("bbox", "bounding_box", "box"):
        value = location.get(key)
        bbox = _normalize_bbox(value)
        if bbox is not None:
            return bbox
    metadata = location.get("metadata")
    if isinstance(metadata, dict):
        return _location_bbox(metadata)
    return None


def _location_page_size(location: dict[str, Any]) -> list[float] | None:
    for key in ("page_size", "page_dimensions"):
        value = location.get(key)
        page_size = _normalize_page_size(value)
        if page_size is not None:
            return page_size
    metadata = location.get("metadata")
    if isinstance(metadata, dict):
        return _location_page_size(metadata)
    return None


def _normalize_bbox(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    coords: list[float] = []
    for item in value:
        if isinstance(item, (int, float)):
            coords.append(float(item))
        elif isinstance(item, str):
            try:
                coords.append(float(item))
            except ValueError:
                return None
        else:
            return None
    return coords


def _normalize_page_size(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    coords: list[float] = []
    for item in value:
        if isinstance(item, (int, float)):
            coords.append(float(item))
        elif isinstance(item, str):
            try:
                coords.append(float(item))
            except ValueError:
                return None
        else:
            return None
    return coords


def _library_asset_document_url(evidence: Evidence) -> str | None:
    return _library_asset_object_key(evidence)


def _library_asset_image_url(
    evidence: Evidence,
    *,
    source_image_id: str | None,
    image_uri: str | None,
) -> str | None:
    del source_image_id, image_uri
    return _library_asset_object_key(evidence)


def _image_ref_matches(ref: dict[str, Any], source_image_id: str) -> bool:
    if not source_image_id:
        return True
    normalized = str(source_image_id).strip()
    if not normalized:
        return True
    candidates = {
        str(ref.get("uri") or ""),
        Path(str(ref.get("uri") or "")).name,
        str(ref.get("source_image_id") or ""),
        str(ref.get("image_id") or ""),
        str(ref.get("id") or ""),
    }
    return normalized in candidates


def _image_ref_identifier(ref: dict[str, Any]) -> str | None:
    for key in ("source_image_id", "image_id", "id", "uri"):
        value = str(ref.get(key) or "").strip()
        if value:
            return Path(value).name if key == "uri" else value
    return None


def _optional_float_value(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _drop_empty_fields(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [])}


def _segment_text(evidence: Evidence, segment_id: str) -> str | None:
    locations = evidence.metadata.get("locations")
    if isinstance(locations, list):
        for location in locations:
            if not isinstance(location, dict):
                continue
            if str(location.get("segment_id") or "") == segment_id:
                return str(location.get("text") or "")
    return None


def _text_evidence_span_payloads(claim: Any) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for span in getattr(claim, "evidence_spans", []) or []:
        if isinstance(span, BaseModel):
            payload = span.model_dump(mode="json")
        elif isinstance(span, dict):
            payload = dict(span)
        else:
            payload = {
                "segment_id": getattr(span, "segment_id", ""),
                "supporting_quote": getattr(span, "supporting_quote", ""),
                "quote_verified": getattr(span, "quote_verified", False),
                "match_ratio": getattr(span, "match_ratio", 0.0),
                "verify_reason": getattr(span, "verify_reason", ""),
            }
        payloads.append(payload)
    return payloads


def _text_render_quote(claim: Any) -> str | None:
    supporting_quote = str(getattr(claim, "supporting_quote", "") or "")
    if supporting_quote:
        return supporting_quote
    verified_quotes = [
        str(span.get("supporting_quote") or "")
        for span in _text_evidence_span_payloads(claim)
        if span.get("quote_verified") and str(span.get("supporting_quote") or "")
    ]
    if verified_quotes:
        return "\n".join(verified_quotes)
    return None


def _metadata_string(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    if value in (None, ""):
        return None
    return str(value)


def _format_seconds(value: float) -> str:
    minutes, seconds = divmod(max(value, 0), 60)
    return f"{int(minutes):02d}:{seconds:04.1f}"


def _log_inspect_tool_started(
    tool_name: str,
    args: dict[str, Any],
    question: str,
    *,
    evidence: Evidence | None,
) -> None:
    logger.bind(
        **_inspect_tool_log_context(tool_name, args, question, evidence=evidence),
    ).info("inspect tool started")


def _log_inspect_tool_completed(
    tool_name: str,
    args: dict[str, Any],
    question: str,
    *,
    evidence: Evidence | None,
    result: Any,
    started: float,
) -> None:
    logger.bind(
        **_inspect_tool_log_context(
            tool_name,
            args,
            question,
            evidence=evidence,
            result=result,
            started=started,
        ),
    ).info("inspect tool completed")


def _log_inspect_tool_failed(
    tool_name: str,
    args: dict[str, Any],
    question: str,
    *,
    started: float,
    error: Exception,
    evidence: Evidence | None = None,
) -> None:
    logger.bind(
        **_inspect_tool_log_context(
            tool_name,
            args,
            question,
            evidence=evidence,
            started=started,
            error=error,
        ),
    ).warning("inspect tool failed")


def _inspect_tool_log_context(
    tool_name: str,
    args: dict[str, Any],
    question: str,
    *,
    evidence: Evidence | None = None,
    result: Any = None,
    started: float | None = None,
    error: Exception | None = None,
) -> dict[str, Any]:
    metadata = evidence.metadata if evidence is not None else {}
    context: dict[str, Any] = {
        "tool_name": tool_name,
        "library_id": str(args.get("library_id") or metadata.get("library_id") or ""),
        "asset_id": str(args.get("asset_id") or metadata.get("asset_id") or ""),
        "evidence_id": str(
            args.get("evidence_id") or metadata.get("chunk_id") or getattr(evidence, "id", "") or ""
        ),
        "question_chars": len(question),
    }
    if evidence is not None:
        context["evidence_source"] = evidence.source
        context["evidence_parts"] = len(evidence.parts)
    if started is not None:
        context["elapsed_seconds"] = round(perf_counter() - started, 3)
    if result is not None:
        claims = getattr(result, "claims", None)
        if isinstance(claims, list):
            context["claims_count"] = len(claims)
        success = getattr(result, "success", None)
        if isinstance(success, bool):
            context["inspect_success"] = success
        result_error = getattr(result, "error", None)
        if result_error:
            context["inspect_error"] = str(result_error)
    if error is not None:
        context["error_type"] = type(error).__name__
        context["error"] = str(error)
    return context


def _tool_error_payload(tool: str, code: str, message: str, exc: Exception) -> str:
    return ToolResult.fail(
        tool,
        map_error_code(code),
        message,
        detail=str(exc),
    ).to_json()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _path_exists_with_timeout(path: Any, *, timeout_seconds: float = 30.0) -> bool:
    """Check path existence with a hard timeout.

    JuiceFS/FUSE 或网络存储偶发卡死时, stat/exists 可能永久阻塞; 用线程池
    包裹并设超时, 超时视为不存在(返回 False)而不是无限等待。
    """
    import concurrent.futures

    def _check() -> bool:
        try:
            return Path(path).exists()
        except Exception:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_check)
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError:
            return False


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_time_range(value: Any) -> EvidenceTimeRange | None:
    if value in (None, ""):
        return None
    return EvidenceTimeRange.model_validate(value)


_UNSUPPORTED_VISUAL_GROUND_QUERY_RE = re.compile(
    r"(几次|多少次|出现.{0,10}(?:几次|多少次)|几圈|多少圈|转动|擦拭|"
    r"拔了几个|拆了几个|加入.{0,8}(?:几次|多少次)|字幕|文字|台词|"
    r"几位|小数|多久|时长|持续|周期|多少(?:秒|分钟|小时)|"
    r"流程中|使用了|用了|耗用了|共用|要检查|"
    r"几种.{0,8}(?:工具|功能|轴体|类型|类别)|"
    r"多少种.{0,8}(?:工具|功能|类型|类别))",
    re.IGNORECASE,
)


def _unsupported_visual_ground_query_reason(query: str) -> str | None:
    if not _UNSUPPORTED_VISUAL_GROUND_QUERY_RE.search(query):
        return None
    return (
        "visual_ground only supports static physical-object counting or spatial "
        "grounding in one image/frame. This query requires temporal, text, duration, "
        "or cross-segment aggregation; use list_video_segments plus video_inspect/"
        "text_inspect and aggregate verified occurrences instead."
    )


def _evidence_midpoint_seconds(evidence: Evidence) -> float:
    """Return the middle timestamp of a selected evidence clip."""

    video_part = _first_part(evidence, EvidencePartKind.VIDEO)
    time_range = video_part.time_range if video_part is not None else None
    if time_range is None:
        raw_range = evidence.metadata.get("time_range")
        if isinstance(raw_range, dict):
            with contextlib.suppress(Exception):
                time_range = EvidenceTimeRange.model_validate(raw_range)
    if time_range is None:
        return 0.0
    start = max(0.0, float(time_range.start_seconds))
    end = max(start, float(time_range.end_seconds))
    return (start + end) / 2.0
