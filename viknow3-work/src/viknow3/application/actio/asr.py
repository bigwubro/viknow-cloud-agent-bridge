"""ASR clients backed by the actio package."""

from __future__ import annotations

import copy
from pathlib import Path
from time import perf_counter

from agents.runtime.core.multimodal.asr import (
    ASRTranscript,
    TranscriptSegment,
    _convert_asr_result,
    _extracted_wav,
    _is_low_quality_asr_result,
    _sliced_wav,
    detect_voice_regions,
)
from agents.runtime.core.tools.context import remaining_run_seconds
from viknow3.application.actio._load import load_actio

_actio_asr_client_module = load_actio("actio.asr_client")
_ActioOpenAICompatibleASRClient = _actio_asr_client_module.OpenAICompatibleASRClient


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


class TimestampedASRClient:
    """Timestamped ASR that calls the actio task helper."""

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
        task_module = load_actio("actio.tasks.asr_task")
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
