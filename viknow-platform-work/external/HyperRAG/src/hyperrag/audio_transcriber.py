from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

try:
    import torch
except ImportError:  # pragma: no cover - optional runtime optimization
    torch = None

try:
    from silero_vad import get_speech_timestamps, load_silero_vad, read_audio
except ImportError as exc:  # pragma: no cover - explicit runtime guidance
    raise SystemExit(
        "Missing dependency silero-vad. Please install silero-vad before using audio transcription."
    ) from exc


ASRSegment = Dict[str, Any]
ASRCallable = Callable[[str, Optional[str], Optional[List[str]]], List[ASRSegment]]


@dataclass(frozen=True)
class VADConfig:
    threshold: float = 0.5
    min_speech_duration_ms: int = 400
    min_silence_duration_ms: int = 400
    speech_pad_ms: int = 100
    merge_gap_sec: float = 0.5
    max_speech_duration_sec: float = 30.0


@dataclass(frozen=True)
class TranscriptionConfig:
    vad: VADConfig = VADConfig()
    max_concurrent_asr: int = 8
    min_asr_segment_duration_sec: float = 1.0
    asr_lang: Optional[str] = "zh"
    asr_hotwords: Optional[List[str]] = None


class SileroVADAudioTranscriber:
    _vad_model: Any = None

    def __init__(
        self,
        *,
        asr_callable: ASRCallable,
        config: Optional[TranscriptionConfig] = None,
    ) -> None:
        self.asr_callable = asr_callable
        self.config = config or TranscriptionConfig()

    @classmethod
    def _get_vad_model(cls) -> Any:
        if cls._vad_model is None:
            if torch is not None:
                torch.set_num_threads(1)
            cls._vad_model = load_silero_vad()
        return cls._vad_model

    def transcribe_file(self, audio_path: str | Path) -> List[ASRSegment]:
        return asyncio.run(self.atranscribe_file(audio_path))

    async def atranscribe_file(self, audio_path: str | Path) -> List[ASRSegment]:
        audio_file = Path(audio_path)
        if not audio_file.exists():
            raise FileNotFoundError(f"Audio file does not exist: {audio_file}")

        with TemporaryDirectory(prefix="hyperrag_audio_") as temp_dir:
            temp_root = Path(temp_dir)
            prepared_audio_path = temp_root / f"{audio_file.stem}_vad_input.wav"
            await asyncio.to_thread(normalize_audio_for_vad, audio_file, prepared_audio_path)

            wav = read_audio(str(prepared_audio_path), sampling_rate=16000)
            vad_model = self._get_vad_model()
            raw_segments = get_speech_timestamps(
                wav,
                vad_model,
                threshold=self.config.vad.threshold,
                min_speech_duration_ms=self.config.vad.min_speech_duration_ms,
                min_silence_duration_ms=self.config.vad.min_silence_duration_ms,
                speech_pad_ms=self.config.vad.speech_pad_ms,
                max_speech_duration_s=self.config.vad.max_speech_duration_sec,
                return_seconds=True,
            )
            merged_segments = merge_speech_segments(
                raw_segments,
                merge_gap_sec=self.config.vad.merge_gap_sec,
                max_segment_duration_sec=self.config.vad.max_speech_duration_sec,
            )
            bounded_segments = split_long_segments(
                merged_segments,
                max_segment_duration_sec=self.config.vad.max_speech_duration_sec,
            )
            prepared_segments = merge_short_segments(
                bounded_segments,
                min_segment_duration_sec=self.config.min_asr_segment_duration_sec,
                max_segment_duration_sec=self.config.vad.max_speech_duration_sec,
            )

            logger.info(
                'AudioTranscriber: raw_vad_segments=%s, bounded_segments=%s, prepared_segments=%s, max_duration_sec=%s, min_asr_segment_duration_sec=%s',
                len(raw_segments),
                len(bounded_segments),
                len(prepared_segments),
                self.config.vad.max_speech_duration_sec,
                self.config.min_asr_segment_duration_sec,
            )

            if not prepared_segments:
                return []

            started_at = asyncio.get_running_loop().time()
            segments = await self._transcribe_segments(prepared_audio_path, prepared_segments)
            logger.info(
                'AudioTranscriber: completed %s ASR segments in %.3fs',
                len(segments),
                asyncio.get_running_loop().time() - started_at,
            )
            return segments

    async def _transcribe_segments(
        self,
        audio_path: Path,
        vad_segments: Sequence[Dict[str, float]],
    ) -> List[ASRSegment]:
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrent_asr))
        with TemporaryDirectory(prefix="hyperrag_asr_") as temp_dir:
            temp_root = Path(temp_dir)
            tasks = [
                self._transcribe_single_segment(
                    semaphore=semaphore,
                    audio_path=audio_path,
                    clip_dir=temp_root,
                    segment_index=index,
                    begin_ts=float(item["start"]),
                    end_ts=float(item["end"]),
                )
                for index, item in enumerate(vad_segments)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        flattened: List[ASRSegment] = []
        for result in results:
            if isinstance(result, Exception):
                logger.error('AudioTranscriber: ASR segment failed: %s', result)
                raise result
            flattened.extend(result)

        flattened.sort(key=lambda item: (float(item.get("begin_ts", 0.0) or 0.0), float(item.get("end_ts", 0.0) or 0.0)))
        return flattened

    async def _transcribe_single_segment(
        self,
        *,
        semaphore: asyncio.Semaphore,
        audio_path: Path,
        clip_dir: Path,
        segment_index: int,
        begin_ts: float,
        end_ts: float,
    ) -> List[ASRSegment]:
        async with semaphore:
            if end_ts <= begin_ts:
                return []
            clip_path = clip_dir / (
                f"speech_{segment_index:04d}_{_format_stamp(begin_ts)}_{_format_stamp(end_ts)}.wav"
            )
            await asyncio.to_thread(
                extract_audio_slice,
                audio_path,
                clip_path,
                begin_ts,
                end_ts,
            )
            local_segments = await asyncio.to_thread(
                self.asr_callable,
                str(clip_path),
                self.config.asr_lang,
                self.config.asr_hotwords,
            )
            return shift_asr_segments(local_segments, offset_sec=begin_ts)


def merge_speech_segments(
    segments: Sequence[Dict[str, Any]],
    *,
    merge_gap_sec: float,
    max_segment_duration_sec: float,
) -> List[Dict[str, float]]:
    if not segments:
        return []

    ordered_segments = sorted(
        (
            {
                "start": float(item["start"]),
                "end": float(item["end"]),
            }
            for item in segments
        ),
        key=lambda item: (item["start"], item["end"]),
    )

    merged: List[Dict[str, float]] = [ordered_segments[0].copy()]
    for current in ordered_segments[1:]:
        previous = merged[-1]
        candidate_duration = current["end"] - previous["start"]
        if (
            current["start"] - previous["end"] <= merge_gap_sec
            and candidate_duration <= max_segment_duration_sec
        ):
            previous["end"] = max(previous["end"], current["end"])
            continue
        merged.append(current.copy())
    return merged


def split_long_segments(
    segments: Sequence[Dict[str, Any]],
    *,
    max_segment_duration_sec: float,
) -> List[Dict[str, float]]:
    if max_segment_duration_sec <= 0:
        raise ValueError("max_segment_duration_sec must be positive")

    bounded_segments: List[Dict[str, float]] = []
    for item in segments:
        start = float(item["start"])
        end = float(item["end"])
        while end - start > max_segment_duration_sec:
            bounded_segments.append(
                {
                    "start": round(start, 3),
                    "end": round(start + max_segment_duration_sec, 3),
                }
            )
            start += max_segment_duration_sec
        bounded_segments.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
            }
        )
    return bounded_segments


def merge_short_segments(
    segments: Sequence[Dict[str, Any]],
    *,
    min_segment_duration_sec: float,
    max_segment_duration_sec: float,
) -> List[Dict[str, float]]:
    """Merge short segments into adjacent ones whenever possible.

    This avoids dropping short speech while producing ASR clips that are less
    likely to trigger backend edge cases. Merged timestamps always expand to
    cover the full time span, e.g. [1,5] + [6,7] -> [1,7].
    """
    if not segments:
        return []

    if min_segment_duration_sec <= 0:
        return [
            {"start": float(item["start"]), "end": float(item["end"])}
            for item in sorted(segments, key=lambda x: (float(x["start"]), float(x["end"])))
        ]

    merged: List[Dict[str, float]] = [
        {"start": float(item["start"]), "end": float(item["end"])}
        for item in sorted(segments, key=lambda x: (float(x["start"]), float(x["end"])))
    ]

    idx = 0
    while idx < len(merged):
        current = merged[idx]
        current_duration = current["end"] - current["start"]
        if current_duration >= min_segment_duration_sec:
            idx += 1
            continue

        left_idx = idx - 1 if idx > 0 else None
        right_idx = idx + 1 if idx + 1 < len(merged) else None

        candidates: List[tuple[float, int]] = []
        if left_idx is not None:
            left = merged[left_idx]
            candidate_start = min(left["start"], current["start"])
            candidate_end = max(left["end"], current["end"])
            if candidate_end - candidate_start <= max_segment_duration_sec:
                gap = max(0.0, current["start"] - left["end"])
                candidates.append((gap, left_idx))

        if right_idx is not None:
            right = merged[right_idx]
            candidate_start = min(right["start"], current["start"])
            candidate_end = max(right["end"], current["end"])
            if candidate_end - candidate_start <= max_segment_duration_sec:
                gap = max(0.0, right["start"] - current["end"])
                candidates.append((gap, right_idx))

        if not candidates:
            # Keep as-is when neither side can be merged under duration limit.
            idx += 1
            continue

        candidates.sort(key=lambda x: (x[0], x[1]))
        _, target_idx = candidates[0]

        target = merged[target_idx]
        target["start"] = min(target["start"], current["start"])
        target["end"] = max(target["end"], current["end"])

        merged.pop(idx)
        if target_idx < idx:
            idx = max(target_idx, 0)
        else:
            idx = max(idx - 1, 0)

    return [
        {"start": round(item["start"], 3), "end": round(item["end"], 3)}
        for item in merged
    ]


def shift_asr_segments(
    asr_segments: Sequence[ASRSegment],
    *,
    offset_sec: float,
) -> List[ASRSegment]:
    shifted: List[ASRSegment] = []
    for item in asr_segments:
        text = str(item.get("text", "")).strip()
        shifted.append(
            {
                "begin_ts": round(offset_sec + float(item.get("begin_ts", 0.0) or 0.0), 3),
                "end_ts": round(offset_sec + float(item.get("end_ts", 0.0) or 0.0), 3),
                "text": text,
            }
        )
    return shifted


def extract_audio_slice(
    audio_path: Path,
    out_path: Path,
    begin_ts: float,
    end_ts: float,
) -> None:
    duration = max(0.0, end_ts - begin_ts)
    if duration <= 0:
        raise ValueError(f"Invalid speech segment duration: {begin_ts} -> {end_ts}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        str(begin_ts),
        "-t",
        str(duration),
        "-i",
        str(audio_path),
        "-ac",
        "1",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        str(out_path),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg not found. Please install ffmpeg.") from exc


def normalize_audio_for_vad(audio_path: Path, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(audio_path),
        "-ac",
        "1",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        str(out_path),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg not found. Please install ffmpeg.") from exc


def join_transcript_text(asr_segments: Sequence[ASRSegment]) -> str:
    return "\n".join(
        str(item.get("text", "")).strip() for item in asr_segments if str(item.get("text", "")).strip()
    ).strip()


def _format_stamp(ts: float) -> str:
    return f"{int(round(ts * 1000)):010d}ms"