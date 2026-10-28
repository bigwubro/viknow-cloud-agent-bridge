"""Read-only LiteLLM monitoring HTTP client."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urljoin

import httpx

from viknow3.agents.runtimes.deepagents.core.config import LiteLlmApiConfig
from viknow3.agents.runtimes.deepagents.core.monitoring.errors import MonitoringApiError

_LIVELINESS_PATH = "/health/liveliness"
_READINESS_PATH = "/health/readiness"
_ACTIVE_HEALTH_PATH = "/health"
_MODEL_INFO_PATH = "/v1/model/info"


class LiteLlmClient:
    """Small synchronous client for LiteLLM's read-only monitoring APIs."""

    def __init__(self, config: LiteLlmApiConfig, *, client: httpx.Client | None = None) -> None:
        self._base_url = f"{config.base_url.rstrip('/')}/"
        self._api_key_env = config.api_key_env
        self._client = client or httpx.Client(timeout=config.timeout_seconds)

    def status(self) -> dict[str, Any]:
        liveliness = self._get_json(_LIVELINESS_PATH)
        if not isinstance(liveliness, str):
            self._raise_contract_error()
        readiness = self._get_json(_READINESS_PATH)
        active_health = self._get_json(_ACTIVE_HEALTH_PATH)
        if not isinstance(readiness, dict) or not isinstance(active_health, dict):
            self._raise_contract_error()
        readiness_fields = self._required_strings(
            readiness,
            ("status", "db", "cache", "litellm_version"),
        )
        healthy_count = self._required_count(active_health, "healthy_count")
        unhealthy_count = self._required_count(active_health, "unhealthy_count")
        healthy_models = self._health_models(active_health.get("healthy_endpoints"))
        unhealthy_models = self._health_models(active_health.get("unhealthy_endpoints"))
        return {
            "liveliness": {"status": "ok"},
            "readiness": readiness_fields,
            "active_health": {
                "healthy_count": healthy_count,
                "unhealthy_count": unhealthy_count,
                "healthy_models": healthy_models,
                "unhealthy_models": unhealthy_models,
            },
        }

    def models(self) -> list[dict[str, Any]]:
        payload = self._get_json(_MODEL_INFO_PATH)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            self._raise_contract_error()
        models: list[dict[str, Any]] = []
        for item in payload["data"]:
            if not isinstance(item, dict):
                self._raise_contract_error()
            model_info = item.get("model_info", {})
            if not isinstance(model_info, dict):
                self._raise_contract_error()
            cleaned: dict[str, Any] = {}
            self._copy_optional_string(item, "model_name", cleaned, "model_name")
            self._copy_optional_string(model_info, "id", cleaned, "model_id")
            self._copy_optional_string(
                model_info,
                "litellm_provider",
                cleaned,
                "provider",
            )
            self._copy_optional_string(model_info, "mode", cleaned, "mode")
            for field in ("max_input_tokens", "max_output_tokens", "max_tokens"):
                self._copy_optional_count(model_info, field, cleaned)
            models.append(cleaned)
        return models

    def _get_json(self, path: str) -> Any:
        api_key = os.environ.get(self._api_key_env)
        if not api_key:
            raise MonitoringApiError("LiteLLM API 密钥未配置")
        try:
            response = self._client.get(
                urljoin(self._base_url, path.lstrip("/")),
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.TimeoutException:
            raise MonitoringApiError("LiteLLM 请求超时") from None
        except httpx.RequestError:
            raise MonitoringApiError("LiteLLM 请求失败") from None
        if not response.is_success:
            raise MonitoringApiError("LiteLLM 上游请求失败")
        if len(response.content) > 512 * 1024:
            raise MonitoringApiError("LiteLLM 响应不能超过 512 KiB")
        try:
            return response.json()
        except ValueError:
            raise MonitoringApiError("LiteLLM 返回了无效响应") from None

    @classmethod
    def _health_models(cls, endpoints: Any) -> list[dict[str, str]]:
        if not isinstance(endpoints, list):
            cls._raise_contract_error()
        models: list[dict[str, str]] = []
        for endpoint in endpoints:
            if not isinstance(endpoint, dict) or not isinstance(endpoint.get("model"), str):
                cls._raise_contract_error()
            models.append({"model": endpoint["model"]})
        return models

    @classmethod
    def _required_strings(
        cls,
        payload: dict[str, Any],
        fields: tuple[str, ...],
    ) -> dict[str, str]:
        values: dict[str, str] = {}
        for field in fields:
            value = payload.get(field)
            if not isinstance(value, str):
                cls._raise_contract_error()
            values[field] = value
        return values

    @classmethod
    def _required_count(cls, payload: dict[str, Any], field: str) -> int:
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            cls._raise_contract_error()
        return value

    @classmethod
    def _copy_optional_string(
        cls,
        source: dict[str, Any],
        source_field: str,
        target: dict[str, Any],
        target_field: str,
    ) -> None:
        value = source.get(source_field)
        if value is None:
            return
        if not isinstance(value, str):
            cls._raise_contract_error()
        target[target_field] = value

    @classmethod
    def _copy_optional_count(
        cls,
        source: dict[str, Any],
        field: str,
        target: dict[str, Any],
    ) -> None:
        value = source.get(field)
        if value is None:
            return
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            cls._raise_contract_error()
        target[field] = value

    @staticmethod
    def _raise_contract_error() -> None:
        raise MonitoringApiError("LiteLLM 返回了无效响应")
