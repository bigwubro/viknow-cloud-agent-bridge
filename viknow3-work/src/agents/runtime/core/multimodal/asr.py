"""ASR services and remote provider clients."""

from __future__ import annotations

import audioop
import copy
import importlib
import re
import subprocess
import sys
import tempfile
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

from pydantic import BaseModel, Field

from agents.runtime.core.evidence import Evidence, EvidencePart, EvidencePartKind
from agents.runtime.core.multimodal.evidence_utils import first_media_part, resolve_media_path
from agents.runtime.core.multimodal.review import review_video_path
from agents.runtime.core.tools.context import remaining_run_seconds


def _load_actio_asr_client_module():
    try:
        return importlib.import_module("actio.asr_client")
    except ModuleNotFoundError:
        repo_root = Path(__file__).resolve().parents[4]
        actio_src = repo_root / "external" / "actio" / "src"
        if actio_src.exists() and str(actio_src) not in sys.path:
            sys.path.insert(0, str(actio_src))
        return importlib.import_module("actio.asr_client")


_actio_asr_client_module = _load_actio_asr_client_module()
_ActioOpenAICompatibleASRClient = _actio_asr_client_module.OpenAICompatibleASRClient


def _load_actio_asr_task_module():
    try:
        return importlib.import_module("actio.tasks.asr_task")
    except ModuleNotFoundError:
        repo_root = Path(__file__).resolve().parents[4]
        actio_src = repo_root / "external" / "actio" / "src"
        if actio_src.exists() and str(actio_src) not in sys.path:
            sys.path.insert(0, str(actio_src))
        return importlib.import_module("actio.tasks.asr_task")


class TranscriptSegment(BaseModel):
    """One timestamped ASR transcript segment."""

    start_seconds: float | None = None
    end_seconds: float | None = None
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ASRTranscript(BaseModel):
    """Normalized transcript returned by ASR providers."""

    text: str
    segments: list[TranscriptSegment] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ASRClient(Protocol):
    """Port implemented by remote ASR provider adapters."""

    def transcribe(
        self,
        media_path: Path,
        *,
        language: str | None = None,
        prompt: str | None = None,
    ) -> ASRTranscript: ...


class OpenAICompatibleASRClient(_ActioOpenAICompatibleASRClient):
    """OpenAI-compatible Whisper wrapper with lightweight local VAD."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 120,
        vad_enabled: bool = True,
        vad_frame_ms: int = 30,
        vad_min_region_s: float = 0.4,
        vad_merge_gap_s: float = 0.25,
        vad_padding_s: float = 0.15,
        vad_energy_threshold: int = 300,
    ) -> None:
        super().__init__(
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
        self.vad_enabled = vad_enabled
        self.vad_frame_ms = vad_frame_ms
        self.vad_min_region_s = vad_min_region_s
        self.vad_merge_gap_s = vad_merge_gap_s
        self.vad_padding_s = vad_padding_s
        self.vad_energy_threshold = vad_energy_threshold

    def _transcribe_part(self, media_path, *, language=None, prompt=None):
        remaining = remaining_run_seconds()
        client = self
        if remaining is not None:
            client = copy.copy(self)
            client.timeout_seconds = min(self.timeout_seconds, remaining)
        result = _ActioOpenAICompatibleASRClient.transcribe(
            client, media_path, language=language, prompt=prompt
        )
        if client is not self:
            for key in (
                "last_media_bytes",
                "last_request_payload_bytes",
                "last_http_elapsed_seconds",
            ):
                if hasattr(client, key):
                    setattr(self, key, getattr(client, key))
        return result

    def transcribe(
        self,
        media_path: Path,
        *,
        language: str | None = None,
        prompt: str | None = None,
    ) -> ASRTranscript:
        remaining_run_seconds()
        if not self.vad_enabled:
            return _convert_asr_result(
                self._transcribe_part(media_path, language=language, prompt=prompt)
            )

        with _extracted_wav(media_path) as wav_path:
            if wav_path is None:
                self.last_media_bytes = media_path.stat().st_size
                self.last_request_payload_bytes = 0
                self.last_http_elapsed_seconds = 0
                return ASRTranscript(
                    text="",
                    segments=[],
                    metadata={
                        "vad_enabled": True,
                        "vad_region_count": 0,
                        "no_audio": True,
                        "no_voice_activity": True,
                    },
                )

            regions = detect_voice_regions(
                wav_path,
                frame_ms=self.vad_frame_ms,
                min_region_s=self.vad_min_region_s,
                merge_gap_s=self.vad_merge_gap_s,
                padding_s=self.vad_padding_s,
                energy_threshold=self.vad_energy_threshold,
            )
            if not regions:
                self.last_media_bytes = media_path.stat().st_size
                self.last_request_payload_bytes = 0
                self.last_http_elapsed_seconds = 0
                return ASRTranscript(
                    text="",
                    segments=[],
                    metadata={
                        "vad_enabled": True,
                        "vad_region_count": 0,
                        "no_voice_activity": True,
                    },
                )

            texts: list[str] = []
            segments: list[TranscriptSegment] = []
            total_payload_bytes = 0
            total_http_elapsed = 0.0
            filtered_count = 0
            for start_seconds, end_seconds in regions:
                remaining_run_seconds()
                with _sliced_wav(wav_path, start_seconds, end_seconds) as region_path:
                    result = self._transcribe_part(region_path, language=language, prompt=prompt)
                total_payload_bytes += self.last_request_payload_bytes or 0
                total_http_elapsed += self.last_http_elapsed_seconds or 0
                if _is_low_quality_asr_result(result):
                    filtered_count += 1
                    continue
                if result.text.strip():
                    texts.append(result.text.strip())
                for segment in result.segments:
                    if not segment.text.strip():
                        continue
                    segments.append(
                        TranscriptSegment(
                            start_seconds=(
                                segment.start_seconds + start_seconds
                                if segment.start_seconds is not None
                                else start_seconds
                            ),
                            end_seconds=(
                                segment.end_seconds + start_seconds
                                if segment.end_seconds is not None
                                else end_seconds
                            ),
                            text=segment.text,
                            metadata=segment.metadata,
                        )
                    )

            self.last_media_bytes = media_path.stat().st_size
            self.last_request_payload_bytes = total_payload_bytes
            self.last_http_elapsed_seconds = round(total_http_elapsed, 3)
            return ASRTranscript(
                text=" ".join(texts).strip(),
                segments=segments,
                metadata={
                    "vad_enabled": True,
                    "vad_region_count": len(regions),
                    "vad_regions": [
                        {"start": round(start, 3), "end": round(end, 3)} for start, end in regions
                    ],
                    "asr_filtered_low_quality_count": filtered_count,
                },
            )


@contextmanager
def _extracted_wav(media_path: Path) -> Iterator[Path | None]:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        wav_path = Path(handle.name)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(media_path),
        "-vn",
        "-sn",
        "-dn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-f",
        "wav",
        str(wav_path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=remaining_run_seconds()
        )
    except BaseException:
        wav_path.unlink(missing_ok=True)
        raise
    if result.returncode != 0:
        wav_path.unlink(missing_ok=True)
        stderr = result.stderr or ""
        if "does not contain any stream" in stderr or "Stream map" in stderr:
            yield None
            return
        raise RuntimeError(f"Failed to extract audio for ASR VAD: {stderr}")
    try:
        yield wav_path
    finally:
        wav_path.unlink(missing_ok=True)


@contextmanager
def _sliced_wav(wav_path: Path, start_seconds: float, end_seconds: float) -> Iterator[Path]:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        out_path = Path(handle.name)
    duration = max(0.01, end_seconds - start_seconds)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{start_seconds:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(wav_path),
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(out_path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=remaining_run_seconds()
        )
    except BaseException:
        out_path.unlink(missing_ok=True)
        raise
    if result.returncode != 0:
        out_path.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to slice ASR VAD audio: {result.stderr}")
    try:
        yield out_path
    finally:
        out_path.unlink(missing_ok=True)


def detect_voice_regions(
    wav_path: Path,
    *,
    frame_ms: int = 30,
    min_region_s: float = 0.4,
    merge_gap_s: float = 0.25,
    padding_s: float = 0.15,
    energy_threshold: int = 300,
) -> list[tuple[float, float]]:
    with wave.open(str(wav_path), "rb") as wav:
        sample_rate = wav.getframerate()
        sample_width = wav.getsampwidth()
        frame_samples = max(1, int(sample_rate * frame_ms / 1000))
        energies: list[tuple[float, float, int]] = []
        index = 0
        while True:
            data = wav.readframes(frame_samples)
            if not data:
                break
            rms = audioop.rms(data, sample_width)
            start = index * frame_samples / sample_rate
            end = start + len(data) / sample_width / sample_rate
            energies.append((start, end, rms))
            index += 1
    if not energies:
        return []
    sorted_rms = sorted(rms for *_times, rms in energies)
    noise_floor = sorted_rms[max(0, int(len(sorted_rms) * 0.2) - 1)]
    peak = sorted_rms[-1]
    threshold = max(energy_threshold, int(noise_floor * 2.5), int(peak * 0.08))
    active = [(start, end) for start, end, rms in energies if rms >= threshold]
    if not active:
        return []
    regions: list[tuple[float, float]] = []
    current_start, current_end = active[0]
    for start, end in active[1:]:
        if start - current_end <= merge_gap_s:
            current_end = end
        else:
            regions.append((current_start, current_end))
            current_start, current_end = start, end
    regions.append((current_start, current_end))
    duration = energies[-1][1]
    padded = [
        (max(0.0, start - padding_s), min(duration, end + padding_s))
        for start, end in regions
        if end - start >= min_region_s
    ]
    if not padded:
        return []
    merged: list[tuple[float, float]] = []
    current_start, current_end = padded[0]
    for start, end in padded[1:]:
        if start - current_end <= merge_gap_s:
            current_end = max(current_end, end)
        else:
            merged.append((current_start, current_end))
            current_start, current_end = start, end
    merged.append((current_start, current_end))
    return merged


def _convert_asr_result(result: Any) -> ASRTranscript:
    return ASRTranscript(
        text=result.text,
        segments=[
            TranscriptSegment(
                start_seconds=segment.start_seconds,
                end_seconds=segment.end_seconds,
                text=segment.text,
                metadata=segment.metadata,
            )
            for segment in result.segments
        ],
        metadata=result.metadata,
    )


def _is_low_quality_asr_result(result: Any) -> bool:
    text = str(getattr(result, "text", "") or "").strip()
    if not text:
        return True
    if not re.search(r"[A-Za-z0-9一-鿿]", text):
        return True
    metadata_values = [
        getattr(segment, "metadata", {}) or {} for segment in getattr(result, "segments", [])
    ]
    compression_ratios = [
        float(metadata.get("compression_ratio"))
        for metadata in metadata_values
        if isinstance(metadata.get("compression_ratio"), (int, float))
    ]
    if compression_ratios and max(compression_ratios) >= 4.0:
        alnum_count = len(re.findall(r"[A-Za-z0-9一-鿿]", text))
        if alnum_count / max(1, len(text)) < 0.35:
            return True
    return False


class TimestampedASRClient:
    """ViKnow-compatible wrapper for Actio timestamped ASR task helpers."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        provider: str = "ark",
        timeout_seconds: float = 600,
        region_mode: str = "vad",
        transport: str = "auto",
        max_upload_mb: float = 128,
        chunk_seconds: float = 1800,
        job_timeout_seconds: float = 7200,
        preparation_timeout_seconds: float = 1800,
        queue_timeout_seconds: float = 1800,
        transcription_timeout_seconds: float = 3600,
        poll_interval_seconds: float = 2,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.region_mode = region_mode
        self.transport = transport
        self.max_upload_mb = max_upload_mb
        self.chunk_seconds = chunk_seconds
        self.job_timeout_seconds = job_timeout_seconds
        self.preparation_timeout_seconds = preparation_timeout_seconds
        self.queue_timeout_seconds = queue_timeout_seconds
        self.transcription_timeout_seconds = transcription_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.last_media_bytes: int | None = None
        self.last_request_payload_bytes: int | None = None
        self.last_http_elapsed_seconds: float | None = None

    def transcribe(
        self,
        media_path: Path,
        *,
        language: str | None = None,
        prompt: str | None = None,
    ) -> ASRTranscript:
        del language, prompt
        started = perf_counter()
        task_module = _load_actio_asr_task_module()
        remaining = remaining_run_seconds()
        job_timeout = (
            self.job_timeout_seconds
            if remaining is None
            else min(self.job_timeout_seconds, remaining)
        )
        payload, transport_metadata = task_module.transcribe_timestamped_video(
            media_path,
            {
                "model_name": self.model,
                "provider": self.provider,
                "job_timeout_seconds": job_timeout,
                "preparation_timeout_seconds": self.preparation_timeout_seconds,
                "queue_timeout_seconds": self.queue_timeout_seconds,
                "transcription_timeout_seconds": self.transcription_timeout_seconds,
                "poll_interval_seconds": self.poll_interval_seconds,
                "base_url": self.base_url,
                "api_key": self.api_key,
                "region_mode": self.region_mode,
                "transport": self.transport,
                "max_upload_mb": self.max_upload_mb,
                "chunk_seconds": self.chunk_seconds,
                "timeout_seconds": self.timeout_seconds,
            },
        )
        result = _actio_asr_client_module.normalize_transcript(payload)
        self.last_media_bytes = None
        self.last_request_payload_bytes = None
        self.last_http_elapsed_seconds = round(perf_counter() - started, 3)
        return ASRTranscript(
            text=result.text,
            segments=[
                TranscriptSegment(
                    start_seconds=segment.start_seconds,
                    end_seconds=segment.end_seconds,
                    text=segment.text,
                    metadata=segment.metadata,
                )
                for segment in result.segments
            ],
            metadata={
                **result.metadata,
                "provider": self.provider,
                "region_mode": self.region_mode,
                "transport": transport_metadata.get("transport"),
                "chunk_count": transport_metadata.get("chunk_count"),
                "stage_timings": transport_metadata.get("stage_timings"),
                "total_elapsed_seconds": transport_metadata.get("total_elapsed_seconds"),
                "total_timeout_seconds": transport_metadata.get("total_timeout_seconds"),
            },
        )


GraniteASRClient = TimestampedASRClient


class EvidenceTranscriber:
    """Application service backing evidence ASR tools."""

    def __init__(
        self,
        *,
        repo_root: Path | None = None,
        asr_client: ASRClient,
    ) -> None:
        self.repo_root = (repo_root or Path(__file__).resolve().parents[4]).resolve()
        self.asr_client = asr_client

    def transcribe_evidence(
        self,
        evidence: Evidence,
        *,
        language: str | None = None,
        prompt: str | None = None,
    ) -> Evidence:
        part = first_media_part(evidence)
        media_path = resolve_media_path(self.repo_root, part.uri or "")
        time_range = None
        if part.time_range is not None:
            time_range = (part.time_range.start_seconds, part.time_range.end_seconds)

        started = perf_counter()
        clip_started = perf_counter()
        with review_video_path(media_path, time_range) as submitted_media_path:
            clip_elapsed_seconds = perf_counter() - clip_started
            submitted_media_bytes = submitted_media_path.stat().st_size
            transcript = self.asr_client.transcribe(
                submitted_media_path,
                language=language,
                prompt=prompt,
            )
        elapsed_seconds = perf_counter() - started
        transcript_text = transcript.text.strip()
        transcript_is_empty = not transcript_text
        display_text = transcript_text or "未识别到语音内容"
        metadata = {
            **evidence.metadata,
            **transcript.metadata,
            "transcribed": True,
            "asr_empty": transcript_is_empty,
            "original_evidence_id": evidence.id,
            "asr_language": language,
            "asr_prompt": prompt,
            "asr_elapsed_seconds": round(elapsed_seconds, 3),
            "asr_source_kind": part.kind.value,
            "asr_source_uri": part.uri,
            "asr_media_submitted": True,
            "asr_media_submitted_kind": "clip" if time_range is not None else "source",
            "asr_source_media_bytes": media_path.stat().st_size,
            "asr_media_submitted_bytes": submitted_media_bytes,
            "asr_segment_count": len(transcript.segments),
            "asr_segments": [segment.model_dump(mode="json") for segment in transcript.segments],
        }
        if time_range is not None:
            metadata["asr_time_range"] = {
                "start_seconds": time_range[0],
                "end_seconds": time_range[1],
            }
            metadata["asr_media_submitted_clip"] = True
            metadata["asr_clip_extract_elapsed_seconds"] = round(clip_elapsed_seconds, 3)
        for metadata_key, client_attr in {
            "asr_model": "model",
            "asr_base_url": "base_url",
            "asr_remote_media_bytes": "last_media_bytes",
            "asr_request_payload_bytes": "last_request_payload_bytes",
            "asr_http_elapsed_seconds": "last_http_elapsed_seconds",
        }.items():
            value = getattr(self.asr_client, client_attr, None)
            if value is not None:
                metadata[metadata_key] = value
        return Evidence(
            id=f"{evidence.id}:evidence_asr",
            source="evidence_asr",
            title="语音转写结果",
            parts=[
                EvidencePart(
                    kind=EvidencePartKind.TEXT,
                    text=display_text,
                    metadata={"role": "transcript"},
                ),
                part.model_copy(deep=True),
            ],
            metadata=metadata,
        )
