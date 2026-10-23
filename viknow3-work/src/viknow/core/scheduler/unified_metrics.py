"""Convert scheduler-core Redis snapshots into admin/metrics pool gauges."""

from __future__ import annotations

from typing import Any

from scheduler_core.config import DomainConfig, SchedulerConfig

# Stable display order aligned with ViKnow/HyperRAG unified scheduler YAML.
UNIFIED_DOMAIN_ORDER: tuple[str, ...] = (
    "chat-llm",
    "vllm",
    "embedding",
    "asr",
    "rerank",
    "hyperrag-db",
)


def unified_domain_order(domains: dict[str, DomainConfig]) -> list[str]:
    """Return configured domains in a stable, UI-friendly order."""
    ordered = [name for name in UNIFIED_DOMAIN_ORDER if name in domains]
    for name in sorted(domains):
        if name not in ordered:
            ordered.append(name)
    return ordered


def pool_snapshots_from_unified_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Map scheduler-core ``store.snapshot()`` domains to ResourceScheduler pool rows."""
    domains = snapshot.get("domains") or {}
    items: list[dict[str, Any]] = []
    for domain in unified_domain_order({name: DomainConfig(capacity=1) for name in domains}):
        row = domains.get(domain)
        if row is None:
            continue
        tool_in_use = int(row.get("tool_in_use") or 0)
        job_in_use = int(row.get("job_in_use") or 0)
        queued_tool = int(row.get("queued_tool") or 0)
        queued_job = int(row.get("queued_job") or 0)
        items.append(
            {
                "resource": domain,
                "queue_key": f"scheduler:queue:{domain}",
                "capacity": int(row.get("capacity") or 0),
                "job_capacity": int(row.get("job_capacity") or 0),
                "in_use": int(row.get("in_use") or tool_in_use + job_in_use),
                "tool_in_use": tool_in_use,
                "job_in_use": job_in_use,
                "queued": int(row.get("queued") or queued_tool + queued_job),
                "queued_tool": queued_tool,
                "queued_job": queued_job,
            }
        )
    return items


def idle_pool_snapshots_from_config(config: SchedulerConfig) -> list[dict[str, Any]]:
    """Zeroed pool rows for admin idle view when unified scheduling is enabled."""
    items: list[dict[str, Any]] = []
    for domain in unified_domain_order(config.domains):
        cfg = config.domains[domain]
        items.append(
            {
                "resource": domain,
                "queue_key": f"scheduler:queue:{domain}",
                "capacity": cfg.capacity,
                "job_capacity": cfg.effective_job_capacity,
                "in_use": 0,
                "tool_in_use": 0,
                "job_in_use": 0,
                "queued": 0,
                "queued_tool": 0,
                "queued_job": 0,
            }
        )
    return items


def pool_configs_from_unified_config(config: SchedulerConfig) -> dict[str, int]:
    return {domain: cfg.capacity for domain, cfg in config.domains.items()}


__all__ = [
    "UNIFIED_DOMAIN_ORDER",
    "idle_pool_snapshots_from_config",
    "pool_configs_from_unified_config",
    "pool_snapshots_from_unified_snapshot",
    "unified_domain_order",
]
