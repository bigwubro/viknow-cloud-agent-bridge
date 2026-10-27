"""Media primitives used only by advanced video inspection."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from agents.runtimes.deepagents.core.multimodal.advanced_video_claim import NormalizedCrop


def uniform_timestamps(start: float, end: float, count: int) -> list[float]:
    if count < 1:
        raise ValueError("count must be greater than zero")
    if end <= start:
        raise ValueError("end must be greater than start")
    if count == 1:
        return [(start + end) / 2]
    step = (end - start) / (count - 1)
    return [start + step * index for index in range(count)]


def fps_timestamps(start: float, end: float, fps: float, *, limit: int) -> list[float]:
    if not 0.1 <= fps <= 8:
        raise ValueError("fps must be between 0.1 and 8")
    requested = max(1, int((end - start) * fps) + 1)
    return uniform_timestamps(start, end, min(requested, limit))


def padded_crop(crop: NormalizedCrop, padding: float = 0.15) -> NormalizedCrop:
    pad_x = crop.width * padding
    pad_y = crop.height * padding
    x = max(0.0, crop.x - pad_x)
    y = max(0.0, crop.y - pad_y)
    right = min(1.0, crop.x + crop.width + pad_x)
    bottom = min(1.0, crop.y + crop.height + pad_y)
    return NormalizedCrop(x=x, y=y, width=right - x, height=bottom - y)


def extract_frame(
    video_path: Path,
    output_path: Path,
    *,
    timestamp: float,
    crop: NormalizedCrop | None = None,
    max_edge: int = 1280,
) -> Path:
    filters: list[str] = []
    if crop is not None:
        filters.append(
            f"crop=trunc(iw*{crop.width}/2)*2:trunc(ih*{crop.height}/2)*2:iw*{crop.x}:ih*{crop.y}"
        )
    filters.append(
        f"scale='if(gt(iw,ih),min({max_edge},iw),-2)':"
        f"'if(gt(iw,ih),-2,min({max_edge},ih))':force_original_aspect_ratio=decrease"
    )
    filters.append("format=yuvj420p")
    last_error: Exception | None = None
    for candidate_timestamp in _frame_seek_candidates(timestamp):
        try:
            _run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    f"{candidate_timestamp:.6f}",
                    "-i",
                    str(video_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    ",".join(filters),
                    "-q:v",
                    "2",
                    "-pix_fmt",
                    "yuvj420p",
                    "-strict",
                    "-2",
                    str(output_path),
                ],
                "frame extraction",
            )
            if output_path.exists() and output_path.stat().st_size > 0:
                return output_path
        except RuntimeError as exc:
            last_error = exc
        if output_path.exists():
            output_path.unlink(missing_ok=True)
    if last_error is not None:
        raise last_error
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("frame extraction produced no image")
    return output_path


def _frame_seek_candidates(timestamp: float) -> list[float]:
    base = max(0.0, timestamp)
    candidates = [base]
    for delta in (0.05, 0.15, 0.35, 0.75):
        fallback = max(0.0, base - delta)
        if fallback not in candidates:
            candidates.append(fallback)
    return candidates


@contextmanager
def refined_video_clip(
    video_path: Path,
    output_path: Path,
    *,
    start: float,
    end: float,
    crop: NormalizedCrop | None = None,
    max_edge: int | None = None,
) -> Iterator[Path]:
    if end <= start:
        raise ValueError("refined clip end must be greater than start")
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.6f}",
        "-i",
        str(video_path),
        "-t",
        f"{end - start:.6f}",
    ]
    filters: list[str] = []
    if crop is not None:
        filters.append(
            f"crop=trunc(iw*{crop.width}/2)*2:trunc(ih*{crop.height}/2)*2:iw*{crop.x}:ih*{crop.y}"
        )
    if max_edge is not None:
        if max_edge < 320:
            raise ValueError("refined clip max_edge must be at least 320")
        filters.append(f"scale='if(gt(iw,ih),{max_edge},-2)':'if(gt(iw,ih),-2,{max_edge})'")
    if filters:
        command.extend(["-vf", ",".join(filters)])
    command.extend(["-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output_path)])
    _run(command, "refined clip creation")
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("refined clip creation produced no video")
    yield output_path


def motion_score(frame_paths: list[Path]) -> float:
    if len(frame_paths) < 2:
        return 0.0
    scores: list[float] = []
    previous: Image.Image | None = None
    for path in frame_paths:
        with Image.open(path) as image:
            current = image.convert("L").resize((160, 90))
        if previous is not None:
            difference = ImageChops.difference(previous, current)
            scores.append(ImageStat.Stat(difference).mean[0] / 255.0)
        previous = current
    return sum(scores) / len(scores) if scores else 0.0


def _run(command: list[str], operation: str) -> None:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown ffmpeg error"
        raise RuntimeError(f"{operation} failed: {detail}")
