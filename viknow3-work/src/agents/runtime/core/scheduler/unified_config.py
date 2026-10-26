"""Unified scheduler app configuration (ViKnow-side switch + tuning)."""

from __future__ import annotations

import os
import re
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from scheduler_core.config import DomainConfig, SchedulerConfig

_ENV_TOKEN_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

_JOB_TASK_DOMAINS = frozenset({"vllm", "embedding", "asr", "hyperrag-db"})

VIKNOW_CONCURRENCY_ANCHOR = 64
GENERAL_TOOL_DOMAIN = "agent-tool"
_AGENT_ONLY_DOMAINS = frozenset({"chat-llm", "rerank", GENERAL_TOOL_DOMAIN})


def _expand_env_token(value: str, *, field_name: str) -> str:
    """Expand ``${ENV}`` placeholders; missing vars raise (fail fast)."""

    def replace(match: re.Match[str]) -> str:
        env_name = match.group(1)
        env_value = os.getenv(env_name)
        if env_value is None or not env_value.strip():
            raise ValueError(f"{field_name} references unset environment variable: {env_name}")
        return env_value.strip()

    return _ENV_TOKEN_RE.sub(replace, value).strip()


def _maybe_expand(value: str, *, field_name: str) -> str:
    if _ENV_TOKEN_RE.search(value):
        return _expand_env_token(value, field_name=field_name)
    return value


def build_viknow_scheduler_domains(job_cap: int) -> dict[str, DomainConfig]:
    """Build default resource domains; ``job_capacity`` follows ``job_cap`` from config."""
    anchor = VIKNOW_CONCURRENCY_ANCHOR
    return {
        GENERAL_TOOL_DOMAIN: DomainConfig(capacity=anchor, job_capacity=0),
        "chat-llm": DomainConfig(capacity=anchor, job_capacity=0),
        "vllm": DomainConfig(capacity=max(anchor, job_cap), job_capacity=job_cap),
        "embedding": DomainConfig(capacity=anchor, job_capacity=job_cap),
        "asr": DomainConfig(capacity=max(anchor, job_cap), job_capacity=job_cap),
        "rerank": DomainConfig(capacity=anchor, job_capacity=0),
        "hyperrag-db": DomainConfig(capacity=anchor, job_capacity=job_cap),
    }


class UnifiedSchedulerAppConfig(BaseModel):
    """Top-level switch for routing tool/model/job scheduling through scheduler-core.

    When ``enabled`` is false (default), ViKnow keeps its in-memory
    ResourceScheduler and HyperRAG keeps its Redis TaskScheduler untouched.
    When true, tool/model requests and job submissions go through the unified
    scheduler. See ``openspec/changes/unified-scheduler`` for the design.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    redis_url: str = "redis://localhost:6379/0"
    max_concurrent_job_tasks: int = Field(default=2, ge=1)
    domains: dict[str, DomainConfig] = Field(default_factory=dict)
    per_run_max_concurrent_tools: int = Field(default=32, ge=1)
    per_run_max_concurrent_models: int = Field(default=1, ge=1)
    reference_domain: str = "vllm"
    lease_ttl_seconds: int = Field(default=60, ge=5)
    heartbeat_interval_seconds: int = Field(default=15, ge=1)
    queue_wait_timeout_seconds: float = Field(default=30, gt=0)
    default_execution_timeout_seconds: float = Field(default=180, gt=0)

    @field_validator("enabled", mode="before")
    @classmethod
    def _expand_enabled(cls, value: object) -> object:
        """Expand ``${ENV}`` for the switch (private 部署用环境变量控制开关)."""
        if isinstance(value, str) and _ENV_TOKEN_RE.search(value):
            return _expand_env_token(value, field_name="scheduler.unified.enabled").lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
        return value

    @field_validator("redis_url", mode="before")
    @classmethod
    def _expand_redis_url(cls, value: object) -> object:
        """Expand ``${ENV}`` for redis_url (private 部署注入连接串)."""
        if isinstance(value, str) and _ENV_TOKEN_RE.search(value):
            return _expand_env_token(value, field_name="scheduler.unified.redis_url")
        return value

    @model_validator(mode="after")
    def _apply_domain_job_capacity_from_config(self) -> Self:
        """Derive per-domain ``job_capacity`` from ``max_concurrent_job_tasks``."""
        job_cap = self.max_concurrent_job_tasks
        defaults = build_viknow_scheduler_domains(job_cap)
        merged = dict(defaults)
        for name, cfg in self.domains.items():
            if name in _AGENT_ONLY_DOMAINS:
                merged[name] = cfg.model_copy(update={"job_capacity": 0})
                continue
            if name in _JOB_TASK_DOMAINS:
                merged[name] = cfg.model_copy(
                    update={"capacity": max(cfg.capacity, job_cap), "job_capacity": job_cap}
                )
                continue
            merged[name] = cfg
        self.domains = merged
        return self

    def to_scheduler_config(self) -> SchedulerConfig:
        """Build the scheduler-core SchedulerConfig from this app config."""
        return SchedulerConfig(
            enabled=self.enabled,
            redis_url=self.redis_url,
            domains=self.domains,
            per_run_max_concurrent_tools=self.per_run_max_concurrent_tools,
            per_run_max_concurrent_models=self.per_run_max_concurrent_models,
            reference_domain=self.reference_domain,
            lease_ttl_seconds=self.lease_ttl_seconds,
            heartbeat_interval_seconds=self.heartbeat_interval_seconds,
            queue_wait_timeout_seconds=self.queue_wait_timeout_seconds,
            default_execution_timeout_seconds=self.default_execution_timeout_seconds,
        )


def default_unified_scheduler_app_config() -> UnifiedSchedulerAppConfig:
    # 无 YAML 时的兜底（测试 / 最小启动）；生产环境在 app.yaml 显式配置。
    return UnifiedSchedulerAppConfig(
        max_concurrent_job_tasks=2,
        domains=build_viknow_scheduler_domains(2),
    )


class SchedulerSectionConfig(BaseModel):
    """Top-level ``scheduler`` section; ``unified`` mirrors the scheduler-core switch.

    YAML shape (consistent with HyperRAG's ``scheduler.unified``):

    .. code-block:: yaml

        scheduler:
          unified:
            enabled: false
            max_concurrent_job_tasks: 10
            redis_url: redis://localhost:6379/0
            ...
    """

    model_config = ConfigDict(extra="forbid")

    unified: UnifiedSchedulerAppConfig = Field(default_factory=default_unified_scheduler_app_config)


def default_scheduler_section_config() -> SchedulerSectionConfig:
    return SchedulerSectionConfig()


def default_viknow_scheduler_domains() -> dict[str, DomainConfig]:
    """Backward-compatible alias (uses code fallback job cap=2)."""
    return build_viknow_scheduler_domains(2)


__all__ = [
    "GENERAL_TOOL_DOMAIN",
    "SchedulerSectionConfig",
    "UnifiedSchedulerAppConfig",
    "build_viknow_scheduler_domains",
    "default_scheduler_section_config",
    "default_unified_scheduler_app_config",
    "default_viknow_scheduler_domains",
]
