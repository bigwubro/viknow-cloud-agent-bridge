"""Evidence review service contracts for ViKnow."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter
from typing import Protocol

from agents.runtime.core.evidence import Evidence, EvidencePart, EvidencePartKind
from agents.runtime.core.multimodal.evidence_utils import first_video_part, resolve_media_path


class VideoInspectionClient(Protocol):
    """Port implemented by model/provider adapters that can inspect video."""

    def inspect_video(
        self,
        video_path: Path,
        question: str,
        *,
        time_range: tuple[float, float] | None = None,
    ) -> tuple[str, str]: ...


class EvidenceReviewer:
    """Application service backing video inspection tools."""

    def __init__(
        self,
        *,
        repo_root: Path | None = None,
        inspection_client: VideoInspectionClient,
        video_url_builder: Callable[[EvidencePart], str] | None = None,
    ) -> None:
        self.repo_root = (repo_root or Path(__file__).resolve().parents[4]).resolve()
        self.inspection_client = inspection_client
        self.video_url_builder = video_url_builder

    def review_evidence(self, evidence: Evidence, question: str) -> Evidence:
        query = question.strip()
        if not query:
            raise ValueError("question is required")
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
            answer, result_evidence = self.inspection_client.inspect_video(
                submitted_video_path,
                query,
                time_range=time_range,
            )
        elapsed_seconds = perf_counter() - started
        metadata = {
            **evidence.metadata,
            "reviewed": True,
            "review_question": query,
            "original_evidence_id": evidence.id,
            "review_added_parts": ["question_focused_answer", "question_focused_observation"],
            "review_limitations": [],
            "review_elapsed_seconds": round(elapsed_seconds, 3),
            "review_source_kind": "video",
            "review_source_uri": part.uri,
            "review_video_submitted": True,
            "review_video_submitted_kind": "clip" if time_range is not None else "source",
            "review_source_video_bytes": video_path.stat().st_size,
            "review_video_submitted_bytes": submitted_video_bytes,
        }
        if time_range is not None:
            metadata["review_time_range"] = {
                "start_seconds": time_range[0],
                "end_seconds": time_range[1],
            }
            metadata["review_video_submitted_clip"] = True
            metadata["review_clip_extract_elapsed_seconds"] = round(clip_elapsed_seconds, 3)
        for metadata_key, client_attr in {
            "review_vlm_video_bytes": "last_video_bytes",
            "review_vlm_video_encode_elapsed_seconds": "last_video_encode_elapsed_seconds",
            "review_vlm_request_payload_bytes": "last_request_payload_bytes",
            "review_vlm_http_elapsed_seconds": "last_http_elapsed_seconds",
        }.items():
            value = getattr(self.inspection_client, client_attr, None)
            if value is not None:
                metadata[metadata_key] = value
        model = getattr(self.inspection_client, "model", None)
        base_url = getattr(self.inspection_client, "base_url", None)
        if model:
            metadata["review_model"] = model
        if base_url:
            metadata["review_base_url"] = base_url
        if self.video_url_builder is not None:
            metadata["video_url"] = self.video_url_builder(part)
        return Evidence(
            id=f"{evidence.id}:video_inspect",
            source="video_inspect",
            title="视频检查结果",
            parts=[
                EvidencePart(kind=EvidencePartKind.TEXT, text=answer),
                EvidencePart(
                    kind=EvidencePartKind.TEXT,
                    text=result_evidence,
                    metadata={"role": "question_focused_observation"},
                ),
                part.model_copy(deep=True),
            ],
            metadata=metadata,
        )


@contextmanager
def review_video_path(
    video_path: Path,
    time_range: tuple[float, float] | None,
) -> Iterator[Path]:
    """Yield the video file that should be submitted to the VLM.

    When evidence has a time range, create a temporary mp4 clip with ffmpeg so
    the VLM request uploads only the relevant segment instead of the full source
    video. Without a time range, the original video path is yielded unchanged.
    """

    if time_range is None:
        yield video_path
        return

    start_seconds, end_seconds = time_range
    if end_seconds <= start_seconds:
        raise ValueError("time_range end_seconds must be greater than start_seconds")

    with tempfile.TemporaryDirectory(prefix="viknow-video-review-") as temp_dir:
        output_name = f"{video_path.stem}-{start_seconds:.1f}-{end_seconds:.1f}.mp4"
        output_path = Path(temp_dir) / output_name
        extract_video_clip(video_path, output_path, start_seconds, end_seconds)
        yield output_path


def extract_video_clip(
    video_path: Path,
    output_path: Path,
    start_seconds: float,
    end_seconds: float,
) -> None:
    """Extract a short mp4 clip from a source video using ffmpeg."""

    duration_seconds = end_seconds - start_seconds
    attempts = [
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start_seconds:.3f}",
            "-t",
            f"{duration_seconds:.3f}",
            "-i",
            str(video_path),
            "-map",
            "0",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start_seconds:.3f}",
            "-t",
            f"{duration_seconds:.3f}",
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-ss",
            f"{start_seconds:.3f}",
            "-t",
            f"{duration_seconds:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
    ]
    errors: list[str] = []
    for command in attempts:
        if output_path.exists():
            output_path.unlink()
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            return
        stderr = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
        errors.append(f"exit={result.returncode}: {stderr}")
    raise RuntimeError(
        "ffmpeg video clip extraction failed after copy/restricted-copy/reencode attempts: "
        + " | ".join(errors)
    )
