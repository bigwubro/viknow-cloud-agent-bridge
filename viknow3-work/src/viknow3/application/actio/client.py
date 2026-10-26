"""Actio client the shell injects into the agent and into HyperRAG."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from viknow3.application.actio._load import load_actio

_CLIENT: ActioClientImpl | None = None


class ActioClientImpl:
    """Factories and frame helpers backed by the actio package."""

    def video_inspection_client(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        gateway: Any,
    ) -> Any:
        from viknow3.application.actio.video_inspection import OpenAICompatibleVideoInspectionClient

        return OpenAICompatibleVideoInspectionClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            gateway=gateway,
        )

    def asr_client(self, *, provider: str, **kwargs: Any) -> Any:
        from viknow3.application.actio.asr import OpenAICompatibleASRClient, TimestampedASRClient

        model = kwargs["model"]
        base_url = kwargs["base_url"]
        api_key = kwargs["api_key"]
        timeout_seconds = kwargs["timeout_seconds"]
        if provider == "openai-compatible":
            return OpenAICompatibleASRClient(
                model=model,
                base_url=base_url,
                api_key=api_key,
                timeout_seconds=timeout_seconds,
                vad_enabled=kwargs.get("vad_enabled", True),
                vad_frame_ms=kwargs.get("vad_frame_ms", 30),
                vad_min_region_s=kwargs.get("vad_min_region_s", 0.4),
                vad_merge_gap_s=kwargs.get("vad_merge_gap_s", 0.25),
                vad_padding_s=kwargs.get("vad_padding_s", 0.15),
                vad_energy_threshold=kwargs.get("vad_energy_threshold", 300),
            )
        if provider in {"granite", "ark"}:
            return TimestampedASRClient(
                model=model,
                base_url=base_url,
                api_key=api_key,
                provider=provider,
                timeout_seconds=timeout_seconds,
                region_mode=kwargs.get("region_mode", "vad"),
                transport=kwargs.get("transport", "auto"),
                max_upload_mb=kwargs.get("max_upload_mb", 128),
                chunk_seconds=kwargs.get("chunk_seconds", 1800),
                job_timeout_seconds=kwargs.get("job_timeout_seconds", 7200),
                preparation_timeout_seconds=kwargs.get("preparation_timeout_seconds", 1800),
                queue_timeout_seconds=kwargs.get("queue_timeout_seconds", 1800),
                transcription_timeout_seconds=kwargs.get("transcription_timeout_seconds", 3600),
                poll_interval_seconds=kwargs.get("poll_interval_seconds", 2),
            )
        raise ValueError(f"unsupported asr provider: {provider}")

    def ocr_client(self, *, provider: str, **kwargs: Any) -> Any:
        from viknow3.application.actio.ocr import OpenAICompatibleOCRClient, PaddleOCRClient

        common = {
            "model": kwargs["model"],
            "base_url": kwargs["base_url"],
            "api_key": kwargs["api_key"],
            "timeout_seconds": kwargs["timeout_seconds"],
        }
        if provider == "openai-compatible":
            if kwargs.get("priority") is not None:
                common["priority"] = kwargs["priority"]
            return OpenAICompatibleOCRClient(**common)
        return PaddleOCRClient(**common)

    def locate_anything_client(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
    ) -> Any:
        from viknow3.application.actio.locate_anything import ActioLocateAnythingClient

        return ActioLocateAnythingClient(
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )

    def video_discovery_client(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        gateway: Any,
    ) -> Any:
        from viknow3.application.actio.video_discovery import OpenAICompatibleVideoDiscoveryClient

        return OpenAICompatibleVideoDiscoveryClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            gateway=gateway,
        )

    def sample_video_frames_at_timestamps(
        self,
        *,
        video_path: Path,
        output_dir: Path,
        timestamps: list[float],
    ) -> list[Path]:
        frames = load_actio("actio.video_frames").sample_video_frames_at_timestamps(
            video_path=video_path,
            output_dir=output_dir,
            timestamps=timestamps,
        )
        return list(frames)

    def validate_crop_time_range(
        self,
        *,
        source_time_range: tuple[float, float],
        crop_time_range: tuple[float, float] | None,
    ) -> tuple[float, float]:
        return load_actio("actio.video_frames").validate_crop_time_range(
            source_time_range=source_time_range,
            crop_time_range=crop_time_range,
        )

    def effective_sample_count(
        self,
        *,
        requested_sample_count: int,
        sampling_strategy: str,
        question: str,
        instruction: str | None,
    ) -> int:
        return load_actio("actio.video_frames").effective_sample_count(
            requested_sample_count=requested_sample_count,
            sampling_strategy=sampling_strategy,
            question=question,
            instruction=instruction,
        )

    def sample_video_frames(
        self,
        *,
        video_path: Path,
        output_dir: Path,
        time_range: tuple[float, float],
        sample_count: int,
        sampling_strategy: str = "uniform",
        number_frames: bool = False,
    ) -> list[Path]:
        frames = load_actio("actio.video_frames").sample_video_frames(
            video_path=video_path,
            output_dir=output_dir,
            time_range=time_range,
            sample_count=sample_count,
            sampling_strategy=sampling_strategy,
            number_frames=number_frames,
        )
        return list(frames)


def get_actio_client() -> ActioClientImpl:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = ActioClientImpl()
    return _CLIENT


def indexing_bindings() -> dict[str, Any]:
    """Callables HyperRAG's port holds. The engine does not import actio itself."""

    video_parser = load_actio("actio.video_parser")
    return {
        "video_parser": video_parser.VideoParser,
        "video_parser_config": video_parser.VideoParserConfig,
        "observe_caption_generations": load_actio(
            "actio.video_caption"
        ).observe_caption_generations,
        "normalize_global_context": load_actio("actio.global_context").normalize_global_context,
    }


def normalize_global_context(value: Any) -> Any:
    return load_actio("actio.global_context").normalize_global_context(value)
