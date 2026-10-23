"""Semantic video slicing using real multimodal embeddings."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from viknow.core.knowledge.video_indexing.domain import EmbeddedWindow, SemanticSlice, VideoWindow
from viknow.core.knowledge.video_indexing.media import (
    extract_frame,
    image_data_url,
    probe_duration_seconds,
)

_MIN_TAIL_WINDOW_SECONDS = 0.5


class VideoSemanticSegmenter:
    """Sample a video into fixed windows and aggregate adjacent embedding neighbors."""

    def __init__(
        self,
        *,
        embedding_client: Any,
        window_seconds: float = 3.0,
        min_segment_seconds: float = 3.0,
        max_segment_seconds: float = 18.0,
        similarity_threshold: float = 0.82,
    ) -> None:
        self.embedding_client = embedding_client
        self.window_seconds = window_seconds
        self.min_segment_seconds = min_segment_seconds
        self.max_segment_seconds = max_segment_seconds
        self.similarity_threshold = similarity_threshold

    def sample_windows(self, video_path: Path, frame_dir: Path) -> list[VideoWindow]:
        duration = probe_duration_seconds(video_path)
        windows = []
        start = 0.0
        index = 1
        while start < duration:
            end = min(duration, start + self.window_seconds)
            tail_duration = duration - end
            min_tail_duration = min(_MIN_TAIL_WINDOW_SECONDS, self.window_seconds / 2)
            if 0 < tail_duration < min_tail_duration:
                end = duration
            midpoint = (start + end) / 2
            frame_path = frame_dir / f"Window_{index}.jpg"
            extract_frame(video_path, frame_path, midpoint)
            windows.append(
                VideoWindow(
                    id=f"Window_{index}",
                    start_seconds=round(start, 3),
                    end_seconds=round(end, 3),
                    frame_uri=str(frame_path),
                )
            )
            start = end
            index += 1
        return windows

    def embed_windows(self, windows: list[VideoWindow]) -> list[EmbeddedWindow]:
        embedded = []
        for window in windows:
            embedding = self.embedding_client.embed(
                [
                    {"text": (f"视频窗口 {window.start_seconds:.1f}-{window.end_seconds:.1f} 秒")},
                    {"image": image_data_url(Path(window.frame_uri))},
                ]
            )
            embedded.append(
                EmbeddedWindow(
                    window=window,
                    embedding=embedding,
                    model=getattr(self.embedding_client, "model", "unknown"),
                )
            )
        return embedded

    def aggregate(self, windows: list[EmbeddedWindow]) -> list[SemanticSlice]:
        if not windows:
            return []
        slices = []
        current = [windows[0]]
        for previous, item in zip(windows, windows[1:], strict=False):
            similarity = cosine_similarity(previous.embedding, item.embedding)
            previous_duration = previous.window.end_seconds - current[0].window.start_seconds
            current_duration = item.window.end_seconds - current[0].window.start_seconds
            split_by_similarity = (
                similarity < self.similarity_threshold
                and previous_duration >= self.min_segment_seconds
            )
            split_by_length = current_duration > self.max_segment_seconds
            if split_by_similarity or split_by_length:
                slices.append(
                    self._slice_from_windows(
                        current,
                        index=len(slices) + 1,
                        confidence=similarity,
                        reason=(
                            "max segment duration"
                            if split_by_length
                            else "embedding similarity boundary"
                        ),
                    )
                )
                current = [item]
            else:
                current.append(item)
        slices.append(
            self._slice_from_windows(
                current,
                index=len(slices) + 1,
                confidence=1.0,
                reason="final slice",
            )
        )
        return slices

    def _slice_from_windows(
        self,
        windows: list[EmbeddedWindow],
        *,
        index: int,
        confidence: float,
        reason: str,
    ) -> SemanticSlice:
        return SemanticSlice(
            id=f"Slice_{index}",
            start_seconds=windows[0].window.start_seconds,
            end_seconds=windows[-1].window.end_seconds,
            window_ids=[item.window.id for item in windows],
            boundary_reason=reason,
            confidence=max(0.0, min(1.0, confidence)),
        )


def cosine_similarity(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return numerator / (left_norm * right_norm)
