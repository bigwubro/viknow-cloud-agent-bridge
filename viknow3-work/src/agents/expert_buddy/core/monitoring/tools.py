"""Tool specifications for the read-only monitoring HTTP clients."""

from __future__ import annotations

from collections.abc import Collection
from threading import Lock
from typing import Any

from agents.expert_buddy.core.config import (
    LiteLlmApiConfig,
    MonitoringApiConfig,
    NpmApiConfig,
    PrometheusApiConfig,
    VllmApiConfig,
)
from agents.expert_buddy.core.monitoring.litellm import LiteLlmClient
from agents.expert_buddy.core.monitoring.npm import NpmClient
from agents.expert_buddy.core.monitoring.prometheus import PrometheusClient, VllmClient
from agents.expert_buddy.core.tools.base import FunctionTool, ToolErrorCode, ToolResult
from agents.expert_buddy.core.tools.domain import ToolSpec

MONITORING_TOOL_IDS = (
    "prometheus_metric_search",
    "prometheus_query",
    "prometheus_query_range",
    "vllm_health",
    "litellm_status",
    "litellm_models",
    "npm_health",
    "npm_proxy_hosts",
    "npm_certificates",
)
PROMETHEUS_TOOL_IDS = frozenset(MONITORING_TOOL_IDS[:3])
VLLM_TOOL_IDS = frozenset({"vllm_health"})
LITELLM_TOOL_IDS = frozenset({"litellm_status", "litellm_models"})
NPM_TOOL_IDS = frozenset({"npm_health", "npm_proxy_hosts", "npm_certificates"})

_RFC3339_OR_NUMBER_SCHEMA = {
    "anyOf": [
        {"type": "number"},
        {"type": "string", "format": "date-time"},
    ]
}
_STRING_OR_NUMBER_SCHEMA = {
    "anyOf": [
        {"type": "number"},
        {"type": "string"},
    ]
}
_EMPTY_SCHEMA = {
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
}
_METRIC_SEARCH_SCHEMA = {
    "parameters": {
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "required": ["keyword"],
        "additionalProperties": False,
    }
}
_QUERY_SCHEMA = {
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 4096},
            "time": _RFC3339_OR_NUMBER_SCHEMA,
        },
        "required": ["query"],
        "additionalProperties": False,
    }
}
_QUERY_RANGE_SCHEMA = {
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 4096},
            "start": _STRING_OR_NUMBER_SCHEMA,
            "end": _STRING_OR_NUMBER_SCHEMA,
            "step": {"type": "number", "exclusiveMinimum": 0},
        },
        "required": ["query", "start", "end", "step"],
        "additionalProperties": False,
    }
}


def _tool_spec(**kwargs) -> ToolSpec:
    return FunctionTool(**kwargs).spec()


def build_monitoring_tool_specs(
    config: MonitoringApiConfig | None,
    *,
    enabled_tools: Collection[str],
) -> tuple[ToolSpec, ...]:
    """Build only enabled monitoring tools, with service-local lazy clients."""

    enabled = set(enabled_tools) & set(MONITORING_TOOL_IDS)
    if not enabled:
        return ()
    if config is None:
        raise RuntimeError("monitoring tool config is required")

    specs: list[ToolSpec] = []
    if enabled & PROMETHEUS_TOOL_IDS:
        specs.extend(
            _prometheus_specs(
                _require_service(config.prometheus, "prometheus"),
                enabled,
            )
        )
    if enabled & VLLM_TOOL_IDS:
        specs.extend(_vllm_specs(_require_service(config.vllm, "vllm"), enabled))
    if enabled & LITELLM_TOOL_IDS:
        specs.extend(
            _litellm_specs(
                _require_service(config.litellm, "litellm"),
                enabled,
            )
        )
    if enabled & NPM_TOOL_IDS:
        specs.extend(_npm_specs(_require_service(config.npm, "npm"), enabled))
    return tuple(specs)


def _prometheus_specs(
    config: PrometheusApiConfig,
    enabled: set[str],
) -> tuple[ToolSpec, ...]:
    client: PrometheusClient | None = None
    client_init_lock = Lock()

    def get_client() -> PrometheusClient:
        nonlocal client
        with client_init_lock:
            if client is None:
                client = PrometheusClient(config)
            return client

    def metric_search(args: dict[str, Any]) -> str:
        return _tool_call(
            "prometheus_metric_search",
            lambda: _tool_result(
                "prometheus_metric_search",
                get_client().metric_search(args["keyword"]),
            ),
        )

    def query(args: dict[str, Any]) -> str:
        return _tool_call(
            "prometheus_query",
            lambda: _tool_result(
                "prometheus_query",
                get_client().query(args["query"], args.get("time")),
            ),
        )

    def query_range(args: dict[str, Any]) -> str:
        return _tool_call(
            "prometheus_query_range",
            lambda: _tool_result(
                "prometheus_query_range",
                get_client().query_range(
                    args["query"],
                    args["start"],
                    args["end"],
                    args["step"],
                ),
            ),
        )

    candidates = (
        _tool_spec(
            id="prometheus_metric_search",
            name="prometheus_metric_search",
            schema=_METRIC_SEARCH_SCHEMA,
            handler=metric_search,
            description=(
                "Discover Prometheus metric names and metadata by keyword. "
                "When metric names are uncertain, use this before composing PromQL queries."
            ),
        ),
        _tool_spec(
            id="prometheus_query",
            name="prometheus_query",
            schema=_QUERY_SCHEMA,
            handler=query,
            description="Run a read-only instant PromQL query against the configured Prometheus.",
        ),
        _tool_spec(
            id="prometheus_query_range",
            name="prometheus_query_range",
            schema=_QUERY_RANGE_SCHEMA,
            handler=query_range,
            description="Run a read-only range PromQL query against the configured Prometheus.",
        ),
    )
    return tuple(spec for spec in candidates if spec.id in enabled)


def _vllm_specs(config: VllmApiConfig, enabled: set[str]) -> tuple[ToolSpec, ...]:
    client: VllmClient | None = None
    client_init_lock = Lock()

    def health(_: dict[str, Any]) -> str:
        nonlocal client
        with client_init_lock:
            if client is None:
                client = VllmClient(config)
        return _tool_call("vllm_health", lambda: _tool_result("vllm_health", client.health()))

    candidates = (
        _tool_spec(
            id="vllm_health",
            name="vllm_health",
            schema=_EMPTY_SCHEMA,
            handler=health,
            description="Check the configured vLLM service health endpoint.",
        ),
    )
    return tuple(spec for spec in candidates if spec.id in enabled)


def _litellm_specs(
    config: LiteLlmApiConfig,
    enabled: set[str],
) -> tuple[ToolSpec, ...]:
    client: LiteLlmClient | None = None
    client_init_lock = Lock()

    def get_client() -> LiteLlmClient:
        nonlocal client
        with client_init_lock:
            if client is None:
                client = LiteLlmClient(config)
            return client

    def status(_: dict[str, Any]) -> str:
        return _tool_call(
            "litellm_status",
            lambda: _tool_result("litellm_status", get_client().status()),
        )

    def models(_: dict[str, Any]) -> str:
        return _tool_call(
            "litellm_models",
            lambda: _tool_result("litellm_models", get_client().models()),
        )

    candidates = (
        _tool_spec(
            id="litellm_status",
            name="litellm_status",
            schema=_EMPTY_SCHEMA,
            handler=status,
            description="Read LiteLLM liveliness, readiness, and active health summaries.",
        ),
        _tool_spec(
            id="litellm_models",
            name="litellm_models",
            schema=_EMPTY_SCHEMA,
            handler=models,
            description="List the configured LiteLLM models using whitelisted fields.",
        ),
    )
    return tuple(spec for spec in candidates if spec.id in enabled)


def _npm_specs(config: NpmApiConfig, enabled: set[str]) -> tuple[ToolSpec, ...]:
    client: NpmClient | None = None
    client_init_lock = Lock()

    def get_client() -> NpmClient:
        nonlocal client
        with client_init_lock:
            if client is None:
                client = NpmClient(config)
            return client

    def health(_: dict[str, Any]) -> str:
        return _tool_call("npm_health", lambda: _tool_result("npm_health", get_client().health()))

    def proxy_hosts(_: dict[str, Any]) -> str:
        return _tool_call(
            "npm_proxy_hosts",
            lambda: _tool_result("npm_proxy_hosts", get_client().proxy_hosts()),
        )

    def certificates(_: dict[str, Any]) -> str:
        return _tool_call(
            "npm_certificates",
            lambda: _tool_result("npm_certificates", get_client().certificates()),
        )

    candidates = (
        _tool_spec(
            id="npm_health",
            name="npm_health",
            schema=_EMPTY_SCHEMA,
            handler=health,
            description="Read Nginx Proxy Manager health and version information.",
        ),
        _tool_spec(
            id="npm_proxy_hosts",
            name="npm_proxy_hosts",
            schema=_EMPTY_SCHEMA,
            handler=proxy_hosts,
            description="List Nginx Proxy Manager proxy hosts using whitelisted fields.",
        ),
        _tool_spec(
            id="npm_certificates",
            name="npm_certificates",
            schema=_EMPTY_SCHEMA,
            handler=certificates,
            description="List Nginx Proxy Manager certificate metadata using whitelisted fields.",
        ),
    )
    return tuple(spec for spec in candidates if spec.id in enabled)


def _require_service[T](service: T | None, name: str) -> T:
    if service is None:
        raise RuntimeError(f"{name} monitoring config is required")
    return service


def _tool_result(tool_id: str, result: Any) -> str:
    return ToolResult.ok(tool_id, {"result": result}).to_json()


def _tool_call(tool_id: str, producer):
    try:
        return producer()
    except Exception as exc:
        return ToolResult.fail(
            tool_id,
            ToolErrorCode.UPSTREAM_ERROR,
            str(exc),
            detail=str(exc),
            retryable=True,
        ).to_json()
