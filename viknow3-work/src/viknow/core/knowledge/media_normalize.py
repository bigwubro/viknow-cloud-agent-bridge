"""Normalize videos via ffmpeg subprocess before HyperRAG indexing."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = frozenset(
    {
        ".mp4",
        ".mov",
        ".mkv",
        ".avi",
        ".flv",
        ".webm",
        ".m4v",
        ".mpeg",
        ".mpg",
    }
)

MAX_CONCURRENT_NORMALIZATIONS = 3
_FFMPEG_SLOTS = threading.Semaphore(MAX_CONCURRENT_NORMALIZATIONS)

_SCALE_FILTER = (
    "scale='trunc(iw*min(1,min(1280/iw,720/ih))/2)*2':'trunc(ih*min(1,min(1280/iw,720/ih))/2)*2'"
)


class VideoNormalizeError(RuntimeError):
    """Raised when ffmpeg normalization fails."""


def is_video_path(path: Path | str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def normalized_output_path(source: Path) -> Path:
    """Return a stable `.mp4` path next to the source for index consumption."""

    return source.with_name(f"{source.stem}.normalized.mp4")


def normalize_video(source: Path, output: Path | None = None) -> Path:
    """Run ffmpeg in a child process; globally capped at 3 concurrent runs."""

    source = Path(source)
    if not source.is_file():
        raise VideoNormalizeError(f"source video not found: {source}")
    destination = Path(output) if output is not None else normalized_output_path(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        _SCALE_FILTER,
        "-r",
        "30",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-c:a",
        "aac",
        str(destination),
    ]
    with _FFMPEG_SLOTS:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not destination.is_file() or destination.stat().st_size <= 0:
        if destination.exists():
            destination.unlink(missing_ok=True)
        detail = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
        logger.warning("video normalize failed source=%s detail=%s", source, detail)
        raise VideoNormalizeError(f"ffmpeg normalize failed for {source}")
    return destination


async def normalize_video_async(source: Path, output: Path | None = None) -> Path:
    """Async wrapper that does not block the event loop on ffmpeg."""

    return await asyncio.to_thread(normalize_video, Path(source), output)
