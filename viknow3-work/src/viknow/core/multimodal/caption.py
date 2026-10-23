"""Video caption services and ViKnow Evidence adapter."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

from pydantic import BaseModel, Field

from viknow.core.evidence import Evidence, EvidencePart, EvidencePartKind
from viknow.core.multimodal.evidence_utils import first_video_part, resolve_media_path
from viknow.core.multimodal.review import review_video_path


def _load_actio_video_caption():
    try:
        return importlib.import_module("actio.video_caption")
    except ModuleNotFoundError:
        repo_root = Path(__file__).resolve().parents[4]
        actio_src = repo_root / "external" / "actio" / "src"
        if actio_src.exists() and str(actio_src) not in sys.path:
            sys.path.insert(0, str(actio_src))
        return importlib.import_module("actio.video_caption")


_actio_video_caption = _load_actio_video_caption()
_ActioOpenAICompatibleVideoCaptionClient = _actio_video_caption.OpenAICompatibleVideoCaptionClient


class VideoCaption(BaseModel):
    """Normalized caption returned by video caption providers."""

    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class VideoCaptionClient(Protocol):
    """Port implemented by remote video caption provider adapters."""

    def caption(
        self,
        video_path: Path,
        *,
        prompt: str | None = None,
        language: str | None = None,
        time_range: tuple[float, float] | None = None,
    ) -> VideoCaption: ...


class OpenAICompatibleVideoCaptionClient(_ActioOpenAICompatibleVideoCaptionClient):
    """ViKnow-compatible wrapper for actio OpenAI-compatible caption client."""

    def caption(
        self,
        video_path: Path,
        *,
        prompt: str | None = None,
        language: str | None = None,
        time_range: tuple[float, float] | None = None,
    ) -> VideoCaption:
        result = super().caption(
            video_path,
            prompt=prompt,
            language=language,
            time_range=time_range,
        )
        return VideoCaption(text=result.text, metadata=result.metadata)


class EvidenceCaptioner:
    """Application service backing video caption workflows."""

    def __init__(
        self,
        *,
        repo_root: Path | None = None,
        caption_client: VideoCaptionClient,
    ) -> None:
        self.repo_root = (repo_root or Path(__file__).resolve().parents[4]).resolve()
        self.caption_client = caption_client

    def caption_evidence(
        self,
        evidence: Evidence,
        *,
        prompt: str | None = None,
        language: str | None = None,
    ) -> Evidence:
        part = first_video_part(evidence)
        video_path = resolve_media_path(self.repo_root, part.uri or "")
        time_range = None
        if part.time_range is not None:
            time_range = (part.time_range.start_seconds, part.time_range.end_seconds)

        started = perf_counter()
        clip_started = perf_counter()
        with review_video_path(video_path, time_range) as submitted_video_path:
            clip_elapsed_seconds = perf_counter() - clip_started
            submitted_video_bytes = submitted_video_path.stat().st_size
            caption = self.caption_client.caption(
                submitted_video_path,
                prompt=prompt,
                language=language,
                time_range=time_range,
            )
        elapsed_seconds = perf_counter() - started

        metadata = {
            **evidence.metadata,
            **caption.metadata,
            "captioned": True,
            "original_evidence_id": evidence.id,
            "caption_prompt": prompt,
            "caption_language": language,
            "caption_elapsed_seconds": round(elapsed_seconds, 3),
            "caption_source_kind": part.kind.value,
            "caption_source_uri": part.uri,
            "caption_video_submitted": True,
            "caption_video_submitted_kind": "clip" if time_range is not None else "source",
            "caption_source_video_bytes": video_path.stat().st_size,
            "caption_video_submitted_bytes": submitted_video_bytes,
        }
        if time_range is not None:
            metadata["caption_time_range"] = {
                "start_seconds": time_range[0],
                "end_seconds": time_range[1],
            }
            metadata["caption_video_submitted_clip"] = True
            metadata["caption_clip_extract_elapsed_seconds"] = round(clip_elapsed_seconds, 3)
        for metadata_key, client_attr in {
            "caption_model": "model",
            "caption_base_url": "base_url",
            "caption_vlm_video_bytes": "last_video_bytes",
            "caption_vlm_video_encode_elapsed_seconds": "last_video_encode_elapsed_seconds",
            "caption_request_payload_bytes": "last_request_payload_bytes",
            "caption_http_elapsed_seconds": "last_http_elapsed_seconds",
        }.items():
            value = getattr(self.caption_client, client_attr, None)
            if value is not None:
                metadata[metadata_key] = value
        return Evidence(
            id=f"{evidence.id}:evidence_caption",
            source="evidence_caption",
            title="视频 Caption 结果",
            parts=[
                EvidencePart(
                    kind=EvidencePartKind.TEXT,
                    text=caption.text,
                    metadata={"role": "caption"},
                ),
                part.model_copy(deep=True),
            ],
            metadata=metadata,
        )
