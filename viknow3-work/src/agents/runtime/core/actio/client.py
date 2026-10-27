"""Actio capability a turn calls. The shell owns the package and injects this client."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class ActioClient(Protocol):
    """Injected Actio package. The runtime does not import it."""

    def video_inspection_client(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        gateway: Any,
    ) -> Any: ...

    def asr_client(self, *, provider: str, **kwargs: Any) -> Any: ...

    def ocr_client(self, *, provider: str, **kwargs: Any) -> Any: ...

    def locate_anything_client(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
    ) -> Any: ...

    def video_discovery_client(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        gateway: Any,
    ) -> Any: ...

    def sample_video_frames_at_timestamps(
        self,
        *,
        video_path: Path,
        output_dir: Path,
        timestamps: list[float],
    ) -> list[Path]: ...

    def validate_crop_time_range(
        self,
        *,
        source_time_range: tuple[float, float],
        crop_time_range: tuple[float, float] | None,
    ) -> tuple[float, float]: ...

    def effective_sample_count(
        self,
        *,
        requested_sample_count: int,
        sampling_strategy: str,
        question: str,
        instruction: str | None,
    ) -> int: ...

    def sample_video_frames(
        self,
        *,
        video_path: Path,
        output_dir: Path,
        time_range: tuple[float, float],
        sample_count: int,
        sampling_strategy: str = "uniform",
        number_frames: bool = False,
    ) -> list[Path]: ...
