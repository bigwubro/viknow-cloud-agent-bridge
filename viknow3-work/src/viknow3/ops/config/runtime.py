"""Server-side runtime reads from viknow_ops (includes decrypted secrets)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from viknow3.ops.config.store import ConfigError, configured, plain_provider, plain_singleton
from viknow3.ops.config.urls import redis_url_from_parts


@dataclass(frozen=True)
class RuntimeBundle:
    redis_url: str
    postgres: dict[str, Any] = field(default_factory=dict)
    neo4j: dict[str, Any] = field(default_factory=dict)
    observe: dict[str, Any] = field(default_factory=dict)
    retrieval: dict[str, Any] = field(default_factory=dict)
    scheduler_unified: dict[str, Any] = field(default_factory=dict)
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)


def load_runtime_bundle() -> RuntimeBundle:
    if not configured():
        raise RuntimeError("VIKNOW_OPS_DSN is required for HyperRAG and runtime services")

    try:
        redis = plain_singleton("redis_connection")
        postgres = plain_singleton("postgres_connection")
        neo4j = plain_singleton("neo4j_connection")
        observe = plain_singleton("observe_setting")
        retrieval = plain_singleton("retrieval_setting")
    except ConfigError as exc:
        raise RuntimeError(exc.body.get("message", "ops config unavailable")) from exc

    # scheduler lives in share in viknow2; ops catalog may extend — read unified from env-backed row later
    scheduler_unified: dict[str, Any] = {"enabled": True, "max_concurrent_job_tasks": 10}
    enabled = __import__("os").environ.get("VIKNOW_UNIFIED_SCHEDULER_ENABLED", "true").lower()
    scheduler_unified["enabled"] = enabled in {"1", "true", "yes", "on"}

    providers: dict[str, dict[str, Any]] = {}
    for provider_id in ("private", "embedding", "rerank", "actio"):
        row = plain_provider(provider_id)
        if row:
            providers[provider_id] = row

    host = str(redis.get("host") or "")
    if not host:
        raise RuntimeError("redis_connection.host is not configured in viknow_ops")
    redis_url = redis_url_from_parts(
        host,
        redis.get("port") or 6379,
        str(redis.get("username") or ""),
        redis.get("password"),
        redis.get("database") or 0,
    )

    return RuntimeBundle(
        redis_url=redis_url,
        postgres=postgres,
        neo4j=neo4j,
        observe=observe,
        retrieval=retrieval,
        scheduler_unified=scheduler_unified,
        providers=providers,
    )
