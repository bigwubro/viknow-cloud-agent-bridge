"""Optional video grounding support for advanced video inspection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from viknow3.agents.runtimes.deepagents.core.multimodal.advanced_video_claim import NormalizedCrop
from viknow3.agents.runtimes.deepagents.core.multimodal.advanced_video_media import extract_frame, padded_crop
from viknow3.agents.runtimes.deepagents.core.multimodal.locate_anything import LocateAnythingResult


class VideoGroundingClient(Protocol):
    def locate_path_async(
        self,
        media_path: Path,
        *,
        query: str,
        mode: str | None = None,
        max_results: int | None = None,
        generation_mode: str | None = None,
    ) -> LocateAnythingResult: ...


class GroundedFrame(BaseModel):
    timestamp: float
    box: list[float]
    score: float = Field(default=0, ge=0, le=1)
    image_width: int
    image_height: int


class GroundedCropResult(BaseModel):
    success: bool
    target: str
    crop: NormalizedCrop | None = None
    confidence: float = Field(default=0, ge=0, le=1)
    frames: list[GroundedFrame] = Field(default_factory=list)
    fallback_reason: str | None = None


@dataclass
class VideoGroundingCropper:
    """Ground a text target on sampled video frames and aggregate a stable crop."""

    client: VideoGroundingClient
    max_frames: int = 4
    padding: float = 0.25
    min_confidence: float = 0.45
    max_area_ratio: float = 0.72
    min_area_ratio: float = 0.002

    def ground_crop(
        self,
        *,
        video_path: Path,
        target_text: str,
        timestamps: list[float],
        temp_dir: Path,
    ) -> GroundedCropResult:
        target = target_text.strip()
        if not target:
            return GroundedCropResult(
                success=False,
                target=target_text,
                fallback_reason="empty grounding target",
            )
        temp_dir.mkdir(parents=True, exist_ok=True)
        sampled = _evenly_select(timestamps, self.max_frames)
        frames: list[GroundedFrame] = []
        for index, timestamp in enumerate(sampled):
            frame_path = temp_dir / f"ground-{index:02d}-{timestamp:.3f}.jpg"
            try:
                extract_frame(video_path, frame_path, timestamp=timestamp, crop=None)
                result = self.client.locate_path_async(
                    frame_path,
                    query=target,
                    mode="ground",
                    max_results=1,
                )
            except Exception:
                continue
            detection = next((item for item in result.detections if item.box), None)
            if detection is None or not result.image_width or not result.image_height:
                continue
            box = _xyxy_box(detection.box or [], result.image_width, result.image_height)
            if box is None:
                continue
            frames.append(
                GroundedFrame(
                    timestamp=timestamp,
                    box=box,
                    score=float(detection.score if detection.score is not None else 0.5),
                    image_width=result.image_width,
                    image_height=result.image_height,
                )
            )
        if not frames:
            return GroundedCropResult(
                success=False,
                target=target,
                fallback_reason="grounding returned no boxes",
            )
        crop = _aggregate_crop(frames, padding=self.padding)
        confidence = _grounding_confidence(frames, crop)
        if confidence < self.min_confidence:
            return GroundedCropResult(
                success=False,
                target=target,
                crop=crop,
                confidence=confidence,
                frames=frames,
                fallback_reason="grounding confidence below threshold",
            )
        area = crop.width * crop.height
        if area < self.min_area_ratio:
            return GroundedCropResult(
                success=False,
                target=target,
                crop=crop,
                confidence=confidence,
                frames=frames,
                fallback_reason="grounded crop is too small",
            )
        if area > self.max_area_ratio:
            return GroundedCropResult(
                success=False,
                target=target,
                crop=crop,
                confidence=confidence,
                frames=frames,
                fallback_reason="grounded crop is too large",
            )
        return GroundedCropResult(
            success=True,
            target=target,
            crop=crop,
            confidence=confidence,
            frames=frames,
        )


def _evenly_select(values: list[float], limit: int) -> list[float]:
    if len(values) <= limit:
        return values
    if limit <= 1:
        return [values[len(values) // 2]]
    indexes = [round(index * (len(values) - 1) / (limit - 1)) for index in range(limit)]
    return [values[index] for index in dict.fromkeys(indexes)]


def _xyxy_box(box: list[float], width: int, height: int) -> list[float] | None:
    if len(box) < 4 or width <= 0 or height <= 0:
        return None
    x1, y1, x2, y2 = [float(value) for value in box[:4]]
    if 0 <= x1 <= 1 and 0 <= x2 <= 1 and 0 <= y1 <= 1 and 0 <= y2 <= 1:
        x1, x2 = x1 * width, x2 * width
        y1, y2 = y1 * height, y2 * height
    left, right = sorted((max(0.0, x1), min(float(width), x2)))
    top, bottom = sorted((max(0.0, y1), min(float(height), y2)))
    if right <= left or bottom <= top:
        return None
    return [left, top, right, bottom]


def _aggregate_crop(frames: list[GroundedFrame], *, padding: float) -> NormalizedCrop:
    width = max(frame.image_width for frame in frames)
    height = max(frame.image_height for frame in frames)
    left = min(frame.box[0] for frame in frames) / width
    top = min(frame.box[1] for frame in frames) / height
    right = max(frame.box[2] for frame in frames) / width
    bottom = max(frame.box[3] for frame in frames) / height
    base = NormalizedCrop(
        x=max(0.0, left),
        y=max(0.0, top),
        width=min(1.0, right) - max(0.0, left),
        height=min(1.0, bottom) - max(0.0, top),
    )
    return padded_crop(base, padding=padding)


def _grounding_confidence(frames: list[GroundedFrame], crop: NormalizedCrop) -> float:
    mean_score = sum(frame.score for frame in frames) / len(frames)
    if len(frames) < 2:
        return min(mean_score, 0.55)
    centers = [
        (
            (frame.box[0] + frame.box[2]) / (2 * frame.image_width),
            (frame.box[1] + frame.box[3]) / (2 * frame.image_height),
        )
        for frame in frames
    ]
    center_x = sum(item[0] for item in centers) / len(centers)
    center_y = sum(item[1] for item in centers) / len(centers)
    drift = max(((x - center_x) ** 2 + (y - center_y) ** 2) ** 0.5 for x, y in centers)
    stability = max(0.0, 1.0 - drift * 4)
    area_penalty = 0.85 if crop.width * crop.height > 0.45 else 1.0
    return max(0.0, min(1.0, mean_score * 0.7 + stability * 0.3) * area_penalty)
