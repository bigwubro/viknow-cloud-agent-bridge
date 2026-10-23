from __future__ import annotations

import asyncio
import hashlib
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger
from tqdm import tqdm

from .utils.segment_tools import format_caption_data_text_lines


def _clean_embedding_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, (list, tuple, set)):
        return "；".join(text for item in value if (text := _clean_embedding_text(item)))
    return " ".join(str(value).split())


def build_video_embedding_text(segment: dict[str, Any]) -> str:
    extra = segment.get("extra") or {}
    caption_data = segment.get("main_struct") or extra.get("caption_data") or {}
    caption_body = []
    for line in format_caption_data_text_lines(caption_data):
        if line.startswith("OCR关键词: "):
            continue
        if line.startswith(("caption: ", "视频描述: ")):
            line = line.split(": ", 1)[1]
        caption_body.append(line)
    if not caption_body and extra.get("caption"):
        caption_body.append(str(extra["caption"]))
    visual_text = caption_data.get("visual_text") or {}
    values = {
        "caption": "\n".join(caption_body),
        "ocr_keywords": (
            caption_data.get("ocr")
            or visual_text.get("ocr_keywords")
            or caption_data.get("ocr_keywords")
            or visual_text.get("ocr_text")
            or caption_data.get("ocr_text")
        ),
    }
    return "\n".join(
        f"<{tag}>{text}</{tag}>"
        for tag, value in values.items()
        if (text := _clean_embedding_text(value))
    )


def _materialize_temp_clip(
    source_video_path: Path, clip_path: Path, begin_ts: float, end_ts: float
) -> None:
    clip_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".mp4", dir=clip_path.parent, delete=False) as file:
        temporary_path = Path(file.name)
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                str(begin_ts),
                "-i",
                str(source_video_path),
                "-t",
                str(end_ts - begin_ts),
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                str(temporary_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode or temporary_path.stat().st_size == 0:
            raise RuntimeError(f"Failed to materialize video clip: {result.stderr.strip()}")
        temporary_path.replace(clip_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def resolve_segment_video_path(
    *,
    global_config: dict[str, Any],
    workspace: str,
    video_name: str,
    segment_id: str,
    video_output_format: str,
    metadata: dict[str, Any],
) -> str:
    if metadata.get("begin_ts") is None or metadata.get("end_ts") is None:
        raise ValueError(f"Missing time range for video segment {segment_id}")
    begin = float(metadata["begin_ts"])
    end = float(metadata["end_ts"])
    if not math.isfinite(begin) or not math.isfinite(end) or begin < 0 or end <= begin:
        raise ValueError(f"Invalid time range for video segment {segment_id}")
    source = Path(str(metadata.get("file_path") or ""))
    if not source.is_file():
        raise FileNotFoundError(f"No video source available for segment {segment_id}")
    source_stat = source.stat()
    fingerprint = hashlib.sha256(
        (
            f"{source.resolve()}:{source_stat.st_size}:{source_stat.st_mtime_ns}:"
            f"{begin.hex()}:{end.hex()}"
        ).encode()
    ).hexdigest()[:20]
    working_dir = Path(global_config["working_dir"])
    asset_id = str(metadata.get("asset_id") or video_name)
    # Legacy segment-ID caches and caption paths do not prove source/range provenance.
    clip_path = (
        working_dir
        / "temp_clips"
        / workspace
        / asset_id
        / f"{segment_id}_{fingerprint}.{video_output_format}"
    )
    if clip_path.is_file() and clip_path.stat().st_size > 0:
        return str(clip_path)
    _materialize_temp_clip(source, clip_path, begin, end)
    return str(clip_path)


async def encode_video_segments(
    video_paths: list[str],
    embedder: Any,
    texts: list[str],
    max_concurrency: int = 4,
    progress_description: str = "Encoding Video Segments",
) -> np.ndarray:
    if len(video_paths) != len(texts):
        raise ValueError("Every video segment must have exactly one embedding text")
    if not video_paths:
        return np.empty((0, 0), dtype=np.float32)
    if embedder is None:
        raise ValueError("multimodal embedder is required for video segment encoding")
    semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))

    async def encode(path: str, text: str) -> list[float]:
        async with semaphore:
            embed_video = getattr(embedder, "embed_video", None)
            if callable(embed_video):
                vector = await embed_video(path, task="video_indexing", text=text)
            else:
                vector = await asyncio.to_thread(
                    embedder.embed_video_sync, path, task="video_indexing", text=text
                )
            progress.update(1)
            return vector

    with tqdm(total=len(video_paths), desc=progress_description) as progress:
        results = await asyncio.gather(
            *(encode(path, text) for path, text in zip(video_paths, texts, strict=True)),
            return_exceptions=True,
        )
    for result in results:
        if isinstance(result, BaseException):
            logger.error("Video mixed embedding failed: {}", result)
            raise result
    matrix = np.asarray(results, dtype=np.float32)
    if matrix.ndim != 2 or not np.isfinite(matrix).all():
        raise ValueError("Invalid video embedding matrix")
    return matrix
