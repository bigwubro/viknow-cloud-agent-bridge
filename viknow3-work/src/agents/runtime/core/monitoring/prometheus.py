"""Read-only Prometheus and vLLM HTTP clients."""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import httpx

from agents.runtime.core.config import PrometheusApiConfig, VllmApiConfig
from agents.runtime.core.monitoring.errors import MonitoringApiError

_METADATA_PATH = "/api/v1/metadata"
_METRIC_NAMES_PATH = "/api/v1/label/__name__/values"
_QUERY_PATH = "/api/v1/query"
_QUERY_RANGE_PATH = "/api/v1/query_range"
_HEALTH_PATH = "/health"
_RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_UNIX_SECONDS_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


class PrometheusClient:
    """Small synchronous client for the Prometheus read APIs used by ViKnow."""

    def __init__(self, config: PrometheusApiConfig, *, client: httpx.Client | None = None) -> None:
        self._base_url = config.base_url
        self._client = client or httpx.Client(timeout=config.timeout_seconds)

    def metric_search(self, keyword: str) -> list[dict[str, str]]:
        names_payload = self._success_payload(self._get(_METRIC_NAMES_PATH))
        if not isinstance(names_payload, list) or not all(
            isinstance(name, str) for name in names_payload
        ):
            raise MonitoringApiError("Prometheus 返回了无效 metric names")
        metadata_payload = self._success_data(self._get(_METADATA_PATH))
        lowered_keyword = keyword.casefold()
        matches: list[dict[str, str]] = []
        candidate_names = set(names_payload)
        candidate_names.update(name for name in metadata_payload if isinstance(name, str))
        for name in sorted(candidate_names):
            if lowered_keyword not in name.casefold():
                continue
            metadata_fields = {"type": "", "unit": "", "help": ""}
            if name in metadata_payload:
                entries = metadata_payload[name]
                if not isinstance(entries, list) or not entries:
                    raise MonitoringApiError("Prometheus 返回了无效 metadata")
                metadata = entries[0]
                if not isinstance(metadata, dict):
                    raise MonitoringApiError("Prometheus 返回了无效 metadata")
                metadata_fields = {
                    field: metadata.get(field, "") for field in ("type", "unit", "help")
                }
                if not all(isinstance(value, str) for value in metadata_fields.values()):
                    raise MonitoringApiError("Prometheus 返回了无效 metadata")
            matches.append(
                {
                    "name": name,
                    "type": metadata_fields["type"],
                    "unit": metadata_fields["unit"],
                    "help": metadata_fields["help"],
                }
            )
        return matches[:100]

    def query(self, expression: str, time: int | float | str | None = None) -> dict[str, Any]:
        self._validate_expression(expression)
        params: dict[str, str] = {"query": expression}
        if time is not None:
            params["time"] = str(self._parse_time(time))
        response = self._get(_QUERY_PATH, params=params)
        return self._query_success_data(response)

    def query_range(
        self,
        expression: str,
        start: int | float | str,
        end: int | float | str,
        step: int | float,
    ) -> dict[str, Any]:
        self._validate_expression(expression)
        start_seconds = self._parse_time(start)
        end_seconds = self._parse_time(end)
        if start_seconds > end_seconds:
            raise MonitoringApiError("开始时间不能晚于结束时间")
        range_seconds = end_seconds - start_seconds
        if range_seconds > 7 * 24 * 60 * 60:
            raise MonitoringApiError("查询时间范围不能超过 7 天")
        if isinstance(step, bool) or not isinstance(step, (int, float)):
            raise MonitoringApiError("步长必须是正数秒")
        step_seconds = float(step)
        if not math.isfinite(step_seconds) or step_seconds <= 0:
            raise MonitoringApiError("步长必须是正数秒")
        if range_seconds >= step_seconds * 1000:
            raise MonitoringApiError("范围查询不能超过 1000 个数据点")
        params = {
            "query": expression,
            "start": str(start_seconds),
            "end": str(end_seconds),
            "step": str(step_seconds),
        }
        response = self._get(_QUERY_RANGE_PATH, params=params)
        return self._query_success_data(response)

    def _get(self, path: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        try:
            return self._client.get(self._url(path), params=params)
        except httpx.TimeoutException:
            raise MonitoringApiError("监控服务请求超时") from None
        except httpx.RequestError:
            raise MonitoringApiError("监控服务请求失败") from None

    def _url(self, path: str) -> str:
        return urljoin(self._base_url, path)

    @staticmethod
    def _validate_expression(expression: str) -> None:
        if len(expression) > 4096:
            raise MonitoringApiError("PromQL 表达式不能超过 4096 个字符")

    @staticmethod
    def _parse_time(value: int | float | str) -> float:
        if isinstance(value, str):
            if _UNIX_SECONDS_PATTERN.fullmatch(value) is not None:
                parsed_seconds = float(value)
                if not math.isfinite(parsed_seconds):
                    raise MonitoringApiError("时间必须是有限的 Unix 秒")
                return parsed_seconds
            if _RFC3339_PATTERN.fullmatch(value) is None:
                raise MonitoringApiError("时间必须是 Unix 秒或带时区的 RFC3339 字符串")
            normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                raise MonitoringApiError("时间必须是 Unix 秒或带时区的 RFC3339 字符串") from None
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise MonitoringApiError("时间必须是 Unix 秒或带时区的 RFC3339 字符串")
            return parsed.timestamp()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise MonitoringApiError("时间必须是 Unix 秒或带时区的 RFC3339 字符串")
        parsed_seconds = float(value)
        if not math.isfinite(parsed_seconds):
            raise MonitoringApiError("时间必须是有限的 Unix 秒")
        return parsed_seconds

    @staticmethod
    def _success_payload(response: httpx.Response) -> Any:
        if len(response.content) > 512 * 1024:
            raise MonitoringApiError("Prometheus 响应不能超过 512 KiB")
        if not response.is_success:
            raise MonitoringApiError("Prometheus 上游请求失败")
        try:
            payload = response.json()
        except ValueError:
            raise MonitoringApiError("Prometheus 返回了无效响应") from None
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise MonitoringApiError("Prometheus 返回了失败响应")
        if "data" not in payload:
            raise MonitoringApiError("Prometheus 返回了无效响应")
        return payload["data"]

    @staticmethod
    def _success_data(response: httpx.Response) -> dict[str, Any]:
        data = PrometheusClient._success_payload(response)
        if not isinstance(data, dict):
            raise MonitoringApiError("Prometheus 返回了无效响应")
        return data

    @staticmethod
    def _query_success_data(response: httpx.Response) -> dict[str, Any]:
        data = PrometheusClient._success_data(response)
        PrometheusClient._validate_query_data_limits(data)
        return data

    @staticmethod
    def _validate_query_data_limits(data: Any) -> None:
        if not isinstance(data, dict):
            return
        result = data.get("result")
        if isinstance(result, list) and len(result) > 200:
            raise MonitoringApiError("Prometheus 响应不能超过 200 个序列")
        if not isinstance(result, list):
            return
        sample_count = 0
        for series in result:
            if not isinstance(series, dict):
                continue
            values = series.get("values")
            if isinstance(values, list):
                sample_count += len(values)
            elif "value" in series:
                sample_count += 1
        if sample_count > 5000:
            raise MonitoringApiError("Prometheus 响应不能超过 5000 个样本")


class VllmClient:
    """Small synchronous client for the vLLM health endpoint."""

    def __init__(self, config: VllmApiConfig, *, client: httpx.Client | None = None) -> None:
        self._health_url = urljoin(config.base_url, _HEALTH_PATH)
        self._client = client or httpx.Client(timeout=config.timeout_seconds)

    def health(self) -> bool:
        try:
            response = self._client.get(self._health_url)
        except httpx.TimeoutException:
            raise MonitoringApiError("vLLM 健康检查超时") from None
        except httpx.RequestError:
            raise MonitoringApiError("vLLM 健康检查失败") from None
        if not response.is_success:
            raise MonitoringApiError("vLLM 健康检查失败")
        return True
