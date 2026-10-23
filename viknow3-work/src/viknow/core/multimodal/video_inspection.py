"""ViKnow wrapper for OpenAI-compatible video inspection."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx

from viknow.core.config import AgentModelConfig
from viknow.core.model_gateway import ModelGatewayRoute, gateway_chat_json


def _load_actio_video_inspection():
    try:
        return importlib.import_module("actio.video_inspection")
    except ModuleNotFoundError:
        repo_root = Path(__file__).resolve().parents[4]
        actio_src = repo_root / "external" / "actio" / "src"
        if actio_src.exists() and str(actio_src) not in sys.path:
            sys.path.insert(0, str(actio_src))
        return importlib.import_module("actio.video_inspection")


_actio_video_inspection = _load_actio_video_inspection()
_ActioOpenAICompatibleVideoInspectionClient = (
    _actio_video_inspection.OpenAICompatibleVideoInspectionClient
)
video_data_url = _actio_video_inspection.video_data_url


class OpenAICompatibleVideoInspectionClient(_ActioOpenAICompatibleVideoInspectionClient):
    """ViKnow-compatible wrapper for actio video inspection client."""

    def __init__(self, *, gateway: ModelGatewayRoute | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.gateway = gateway

    def _chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.gateway is None:
            return super()._chat(payload)
        _actio_video_inspection.apply_vllm_priority(payload, self.priority)
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
                f"video inspection request failed: {exc.response.status_code}. {exc.response.text}"
            ) from exc
        finally:
            self.last_http_elapsed_seconds = round(perf_counter() - started, 3)

    @classmethod
    def from_model_config(
        cls,
        model_config: AgentModelConfig,
    ) -> OpenAICompatibleVideoInspectionClient:
        base_url = model_config.resolve_base_url()
        if not base_url:
            raise RuntimeError("agent.model.base_url or agent.model.base_url_env is required")
        return cls(
            model=model_config.require_model(),
            base_url=base_url,
            api_key=model_config.require_api_key(),
            gateway=model_config.gateway,
        )
