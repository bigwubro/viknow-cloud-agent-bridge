"""Scheduler configuration models (Pydantic, extra='forbid')."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from agents.runtimes.deepagents.core.scheduler.domain import Priority

CHAT_LLM_RESOURCE = "chat-llm"
VLLM_RESOURCE = "vllm"
ASR_RESOURCE = "asr"
EMBEDDING_RESOURCE = "embedding"
RERANK_RESOURCE = "rerank"
HYPERRAG_DB_RESOURCE = "hyperrag-db"


class ResourcePoolConfig(BaseModel):
    """Capacity for a single resource pool keyed by physical endpoint."""

    model_config = ConfigDict(extra="forbid")

    capacity: int = 1


class ResourceSchedulerConfig(BaseModel):
    """Configuration for ResourceScheduler resource arbitration."""

    model_config = ConfigDict(extra="forbid")

    per_run_max_concurrent_tools: int
    per_run_max_concurrent_models: int = Field(default=1, ge=1)
    interactive_reserve_ratio: float
    queue_wait_timeout_seconds: float
    default_execution_timeout_seconds: float = Field(default=180, gt=0)
    resources: dict[str, ResourcePoolConfig] = Field(default_factory=dict)

    def interactive_reserved(self, resource: str) -> int:
        pool = self.resources.get(resource)
        if pool is None or pool.capacity <= 0 or self.interactive_reserve_ratio <= 0:
            return 0
        reserved = int(pool.capacity * self.interactive_reserve_ratio)
        return max(reserved, 1)


def default_resource_scheduler_config() -> ResourceSchedulerConfig:
    """Production defaults for per-Agent ResourceScheduler pools and per-run caps.

    This function is the source of truth for numeric defaults; design docs should
    describe mechanism only and not duplicate these values.
    """
    return ResourceSchedulerConfig(
        per_run_max_concurrent_tools=2,
        per_run_max_concurrent_models=1,
        interactive_reserve_ratio=0.8,
        queue_wait_timeout_seconds=500,
        default_execution_timeout_seconds=180,
        resources={
            # CHAT_LLM_RESOURCE + VLLM_RESOURCE share the same private vLLM endpoint.
            CHAT_LLM_RESOURCE: ResourcePoolConfig(capacity=8),
            VLLM_RESOURCE: ResourcePoolConfig(capacity=8),
            ASR_RESOURCE: ResourcePoolConfig(capacity=8),
            EMBEDDING_RESOURCE: ResourcePoolConfig(capacity=32),
            RERANK_RESOURCE: ResourcePoolConfig(capacity=16),
            HYPERRAG_DB_RESOURCE: ResourcePoolConfig(capacity=8),
        },
    )


class SchedulerConfig(BaseModel):
    """Top-level scheduler configuration (not yet wired into AppConfig)."""

    model_config = ConfigDict(extra="forbid")

    resource: ResourceSchedulerConfig = Field(default_factory=default_resource_scheduler_config)


def default_scheduler_config() -> SchedulerConfig:
    return SchedulerConfig()


def resource_scheduler_config_with_overrides(
    overrides: dict[str, int] | None = None,
) -> ResourceSchedulerConfig:
    """Return defaults with optional in-memory capacity overrides applied."""

    config = default_resource_scheduler_config()
    if not overrides:
        return config
    resources = dict(config.resources)
    for resource, capacity in overrides.items():
        if resource not in resources:
            continue
        if capacity < 1:
            raise ValueError(f"pool capacity for {resource} must be >= 1")
        resources[resource] = ResourcePoolConfig(capacity=capacity)
    return config.model_copy(update={"resources": resources})


__all__ = [
    "ASR_RESOURCE",
    "CHAT_LLM_RESOURCE",
    "EMBEDDING_RESOURCE",
    "HYPERRAG_DB_RESOURCE",
    "Priority",
    "RERANK_RESOURCE",
    "ResourcePoolConfig",
    "ResourceSchedulerConfig",
    "SchedulerConfig",
    "VLLM_RESOURCE",
    "default_resource_scheduler_config",
    "default_scheduler_config",
    "resource_scheduler_config_with_overrides",
]
