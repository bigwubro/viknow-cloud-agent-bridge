"""Compatibility exports for the Hermes video inspect plugin."""

from __future__ import annotations

import os
from pathlib import Path

from viknow.core.config import AgentModelConfig
from viknow.core.multimodal.video_inspection import (
    OpenAICompatibleVideoInspectionClient as CoreOpenAICompatibleVideoInspectionClient,
)
from viknow.core.multimodal.video_inspection import video_data_url


class OpenAICompatibleVideoInspectionClient(CoreOpenAICompatibleVideoInspectionClient):
    """Backward-compatible Hermes client facade backed by the core client."""

    @classmethod
    def from_hermes_home(cls, hermes_home: Path) -> OpenAICompatibleVideoInspectionClient:
        env_model = os.getenv("VIKNOW_PRIVATE_MODEL")
        env_base_url = os.getenv("VIKNOW_PRIVATE_BASE_URL")
        env_api_key = os.getenv("VIKNOW_PRIVATE_API_KEY")
        if env_model and env_base_url and env_api_key:
            return cls.from_model_config(
                AgentModelConfig(
                    model_env="VIKNOW_PRIVATE_MODEL",
                    base_url_env="VIKNOW_PRIVATE_BASE_URL",
                    api_key_env="VIKNOW_PRIVATE_API_KEY",
                )
            )

        import yaml

        config_path = hermes_home / "config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        model_config = config.get("model") if isinstance(config, dict) else None
        if not isinstance(model_config, dict):
            raise RuntimeError(f"missing model config in {config_path}")

        model = model_config.get("default") or model_config.get("name") or model_config.get("model")
        base_url = model_config.get("base_url")
        api_key = model_config.get("api_key")
        if not model or not base_url or not api_key:
            raise RuntimeError(f"model, base_url and api_key are required in {config_path}")
        return cls(model=str(model), base_url=str(base_url), api_key=str(api_key))


__all__ = ["OpenAICompatibleVideoInspectionClient", "video_data_url"]
