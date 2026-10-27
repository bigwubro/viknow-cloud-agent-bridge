"""DVD-style video discovery services for ViKnow Evidence."""

from __future__ import annotations

import tempfile
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

from pydantic import BaseModel, Field

from agents.runtime.core.evidence import Evidence, EvidencePart, EvidencePartKind, EvidenceTimeRange
from agents.runtime.core.multimodal.evidence_utils import first_video_part, resolve_media_path


class GlobalBrowseResult(BaseModel):
    """Normalized answer produced from retrieved clip captions."""

    answer: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class FrameInspectResult(BaseModel):
    """Normalized detailed visual answer produced from sampled frames."""

    answer: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class VideoDiscoveryClient(Protocol):
    """Port implemented by remote video discovery provider adapters."""

    def answer_from_clips(
        self,
        *,
        question: str,
        clips: list[Evidence],
        instruction: str | None = None,
    ) -> GlobalBrowseResult: ...

    def inspect_frames(
        self,
        *,
        question: str,
        frame_paths: list[Path],
        source_time_ranges: list[EvidenceTimeRange],
        number_frames: bool = False,
        instruction: str | None = None,
    ) -> FrameInspectResult: ...


class VideoFrameOps(Protocol):
    """Frame sampling the shell's Actio client provides."""

    def validate_crop_time_range(
        self,
        *,
        source_time_range: tuple[float, float],
        crop_time_range: tuple[float, float] | None,
    ) -> tuple[float, float]: ...

    def effective_sample_count(
        self,
        *,
        requested_sample_count: int,
        sampling_strategy: str,
        question: str,
        instruction: str | None,
    ) -> int: ...

    def sample_video_frames(
        self,
        *,
        video_path: Path,
        output_dir: Path,
        time_range: tuple[float, float],
        sample_count: int,
        sampling_strategy: str = "uniform",
        number_frames: bool = False,
    ) -> list[Path]: ...


class VideoDiscoveryService:
    """Application service backing global browse, clip search, and frame inspect."""

    def __init__(
        self,
        *,
        repo_root: Path | None = None,
        index: Any | None = None,
        discovery_client: VideoDiscoveryClient,
        frames: VideoFrameOps | None = None,
    ) -> None:
        self.repo_root = (repo_root or Path(__file__).resolve().parents[4]).resolve()
        self.index = index
        self.discovery_client = discovery_client
        self.frames = frames

    def clip_search(self, query: str, *, top_k: int = 5) -> list[Evidence]:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("query is required")
        if self.index is None:
            raise RuntimeError("clip_search requires a video knowledge index")
        return self.index.search(normalized_query, top_k=top_k)

    def global_browse(
        self,
        question: str,
        *,
        top_k: int = 8,
        instruction: str | None = None,
    ) -> Evidence:
        normalized_question = question.strip()
        if not normalized_question:
            raise ValueError("question is required")
        if self.index is None:
            raise RuntimeError("global_browse requires a video knowledge index")
        clips = self.index.search(normalized_question, top_k=top_k)
        return self.answer_from_clips(
            normalized_question,
            clips=clips,
            instruction=instruction,
        )

    def answer_from_clips(
        self,
        question: str,
        *,
        clips: list[Evidence],
        instruction: str | None = None,
    ) -> Evidence:
        normalized_question = question.strip()
        if not normalized_question:
            raise ValueError("question is required")
        if not clips:
            raise ValueError("global_browse requires at least one video evidence")
        started = perf_counter()
        result = self.discovery_client.answer_from_clips(
            question=normalized_question,
            clips=clips,
            instruction=instruction,
        )
        elapsed_seconds = perf_counter() - started
        metadata = {
            **result.metadata,
            "global_browse_question": normalized_question,
            "global_browse_instruction": instruction,
            "global_browse_clip_ids": [clip.id for clip in clips],
            "global_browse_elapsed_seconds": round(elapsed_seconds, 3),
        }
        _copy_client_metadata(
            metadata,
            self.discovery_client,
            prefix="global_browse",
            media_key="frame",
        )
        return Evidence(
            id="global_browse:answer",
            source="global_browse",
            title="全局浏览结果",
            parts=[
                EvidencePart(
                    kind=EvidencePartKind.TEXT,
                    text=result.answer,
                    metadata={"role": "global_answer"},
                ),
                *[_as_reference_part(clip) for clip in clips],
            ],
            metadata=metadata,
        )

    def frame_inspect(
        self,
        evidence: Evidence,
        question: str,
        *,
        sample_count: int = 3,
        sampling_strategy: str = "uniform",
        crop_time_range: EvidenceTimeRange | None = None,
        number_frames: bool = False,
        instruction: str | None = None,
    ) -> Evidence:
        normalized_question = question.strip()
        if not normalized_question:
            raise ValueError("question is required")
        if sample_count < 1:
            raise ValueError("sample_count must be greater than zero")
        video_part = first_video_part(
            evidence,
            missing_message="frame_inspect requires a video evidence part",
            uri_message="frame_inspect video part requires uri",
        )
        if video_part.time_range is None:
            raise ValueError("frame_inspect requires a video evidence time_range")
        if self.frames is None:
            raise RuntimeError("actio client is required")
        effective_time_range_tuple = self.frames.validate_crop_time_range(
            source_time_range=_time_range_tuple(video_part.time_range),
            crop_time_range=_time_range_tuple(crop_time_range) if crop_time_range else None,
        )
        effective_time_range = _evidence_time_range(effective_time_range_tuple)
        effective_sample_count = self.frames.effective_sample_count(
            requested_sample_count=sample_count,
            sampling_strategy=sampling_strategy,
            question=normalized_question,
            instruction=instruction,
        )
        video_path = resolve_media_path(self.repo_root, video_part.uri or "")
        started = perf_counter()
        with tempfile.TemporaryDirectory(prefix="viknow-frame-inspect-") as temp_dir:
            frame_paths = self.frames.sample_video_frames(
                video_path=video_path,
                output_dir=Path(temp_dir),
                time_range=_time_range_tuple(effective_time_range),
                sample_count=effective_sample_count,
                sampling_strategy=sampling_strategy,
                number_frames=number_frames,
            )
            submitted_frame_bytes = sum(path.stat().st_size for path in frame_paths)
            result = self.discovery_client.inspect_frames(
                question=normalized_question,
                frame_paths=frame_paths,
                source_time_ranges=[effective_time_range],
                number_frames=number_frames,
                instruction=instruction,
            )
        elapsed_seconds = perf_counter() - started
        metadata = {
            **evidence.metadata,
            **result.metadata,
            "frame_inspected": True,
            "frame_inspect_question": normalized_question,
            "frame_inspect_instruction": instruction,
            "frame_inspect_sampling_strategy": sampling_strategy,
            "frame_inspect_requested_sample_count": sample_count,
            "frame_inspect_sample_count": effective_sample_count,
            "frame_inspect_numbered_frames": number_frames,
            "original_evidence_id": evidence.id,
            "frame_inspect_source_uri": video_part.uri,
            "frame_inspect_time_range": video_part.time_range.model_dump(mode="json"),
            "frame_inspect_effective_time_range": effective_time_range.model_dump(mode="json"),
            "frame_inspect_source_video_bytes": video_path.stat().st_size,
            "frame_inspect_submitted_frame_bytes": submitted_frame_bytes,
            "frame_inspect_elapsed_seconds": round(elapsed_seconds, 3),
        }
        if number_frames:
            metadata["frame_inspect_frame_labels"] = [
                {"label": str(index), "filename": path.name}
                for index, path in enumerate(frame_paths, start=1)
            ]
        if crop_time_range is not None:
            metadata["frame_inspect_crop_time_range"] = crop_time_range.model_dump(mode="json")
            metadata["frame_inspect_video_crop_applied"] = True
        _copy_client_metadata(
            metadata,
            self.discovery_client,
            prefix="frame_inspect",
            media_key="frame",
        )
        return Evidence(
            id=f"{evidence.id}:frame_inspect",
            source="frame_inspect",
            title="帧检查结果",
            parts=[
                EvidencePart(
                    kind=EvidencePartKind.TEXT,
                    text=result.answer,
                    metadata={"role": "frame_inspection_answer"},
                ),
                video_part.model_copy(deep=True),
            ],
            metadata=metadata,
        )


def _time_range_tuple(time_range: EvidenceTimeRange) -> tuple[float, float]:
    return (time_range.start_seconds, time_range.end_seconds)


def _evidence_time_range(time_range: tuple[float, float]) -> EvidenceTimeRange:
    return EvidenceTimeRange(start_seconds=time_range[0], end_seconds=time_range[1])


def _clip_context(evidence: Evidence) -> str:
    texts = [
        part.text for part in evidence.parts if part.kind == EvidencePartKind.TEXT and part.text
    ]
    time_range = None
    for part in evidence.parts:
        if part.time_range is not None:
            time_range = part.time_range
            break
    time_text = ""
    if time_range is not None:
        time_text = f" time={time_range.start_seconds:.1f}-{time_range.end_seconds:.1f}s"
    return f"[{evidence.id}{time_text}] {evidence.title or ''}\n" + "\n".join(texts)


def _as_reference_part(evidence: Evidence) -> EvidencePart:
    return EvidencePart(
        kind=EvidencePartKind.TEXT,
        text=_clip_context(evidence),
        metadata={"role": "global_browse_reference", "evidence_id": evidence.id},
    )


def _copy_client_metadata(
    metadata: dict[str, Any],
    client: VideoDiscoveryClient,
    *,
    prefix: str,
    media_key: str,
) -> None:
    for metadata_key, client_attr in {
        f"{prefix}_model": "model",
        f"{prefix}_base_url": "base_url",
        f"{prefix}_remote_{media_key}_bytes": f"last_{media_key}_bytes",
        f"{prefix}_{media_key}_encode_elapsed_seconds": (
            f"last_{media_key}_encode_elapsed_seconds"
        ),
        f"{prefix}_request_payload_bytes": "last_request_payload_bytes",
        f"{prefix}_http_elapsed_seconds": "last_http_elapsed_seconds",
    }.items():
        value = getattr(client, client_attr, None)
        if value is not None:
            metadata[metadata_key] = value
