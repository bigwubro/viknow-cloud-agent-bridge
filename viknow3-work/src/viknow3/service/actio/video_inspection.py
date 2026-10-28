"""Video inspection client backed by the actio package."""

from __future__ import annotations

from time import perf_counter
from typing import Any

import httpx

from viknow3.agents.runtimes.deepagents.core.config import AgentModelConfig
from viknow3.agents.runtimes.deepagents.core.model_gateway import ModelGatewayRoute, gateway_chat_json
from viknow3.service.actio._load import load_actio

_actio_video_inspection = load_actio("actio.video_inspection")
_ActioOpenAICompatibleVideoInspectionClient = (
    _actio_video_inspection.OpenAICompatibleVideoInspectionClient
)


class OpenAICompatibleVideoInspectionClient(_ActioOpenAICompatibleVideoInspectionClient):
    """Routes video inspection through the model gateway when one is configured."""

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
