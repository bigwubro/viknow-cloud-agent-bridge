"""Video discovery client backed by the actio package."""

from __future__ import annotations

from pathlib import Path
from time import perf_counter
from typing import Any

import httpx

from agents.runtime.core.evidence import Evidence, EvidenceTimeRange
from agents.runtime.core.model_gateway import ModelGatewayRoute, gateway_chat_json
from agents.runtime.core.multimodal.video_discovery import (
    FrameInspectResult,
    GlobalBrowseResult,
    _clip_context,
)
from viknow3.application.actio._load import load_actio

_actio_video_discovery = load_actio("actio.video_discovery")
_ActioOpenAICompatibleVideoDiscoveryClient = (
    _actio_video_discovery.OpenAICompatibleVideoDiscoveryClient
)


class OpenAICompatibleVideoDiscoveryClient(_ActioOpenAICompatibleVideoDiscoveryClient):
    """Video discovery client that returns runtime result models."""

    def __init__(self, *, gateway: ModelGatewayRoute | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.gateway = gateway

    def _chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.gateway is None:
            return super()._chat(payload)
        _actio_video_discovery.apply_vllm_priority(payload, self.priority)
        self.last_request_payload_bytes = None
        started = perf_counter()
        try:
            return gateway_chat_json(
                route=self.gateway,
                base_url=self.base_url,
                api_key=self.api_key,
                payload=payload,
                timeout_seconds=self.timeout_seconds,
                request_size_callback=lambda size: setattr(
                    self, "last_request_payload_bytes", size
                ),
            )
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"video discovery request failed: {exc.response.status_code}. {exc.response.text}"
            ) from exc
        finally:
            self.last_http_elapsed_seconds = round(perf_counter() - started, 3)

    def answer_from_clips(
        self,
        *,
        question: str,
        clips: list[Evidence],
        instruction: str | None = None,
    ) -> GlobalBrowseResult:
        result = super().answer_from_clip_contexts(
            question=question,
            clip_contexts=[_clip_context(item) for item in clips],
            instruction=instruction,
        )
        return GlobalBrowseResult(answer=result.answer, metadata=result.metadata)

    def inspect_frames(
        self,
        *,
        question: str,
        frame_paths: list[Path],
        source_time_ranges: list[EvidenceTimeRange],
        number_frames: bool = False,
        instruction: str | None = None,
    ) -> FrameInspectResult:
        result = super().inspect_frames(
            question=question,
            frame_paths=frame_paths,
            source_time_ranges=[
                (item.start_seconds, item.end_seconds) for item in source_time_ranges
            ],
            number_frames=number_frames,
            instruction=instruction,
        )
        return FrameInspectResult(answer=result.answer, metadata=result.metadata)
