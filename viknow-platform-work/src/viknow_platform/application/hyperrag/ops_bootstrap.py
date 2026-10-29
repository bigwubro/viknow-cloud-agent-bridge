"""Build HyperRAG runtime dict from viknow_ops (no repo YAML)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from viknow_platform.application.hyperrag.engine_defaults import ENGINE_DEFAULTS
from viknow_platform.ops.config.runtime import load_runtime_bundle


def _is_loopback_url(url: str) -> bool:
    lowered = url.lower()
    return "127.0.0.1" in lowered or "localhost" in lowered


def _normalize_embedding_model(model_name: str) -> str:
    if model_name == "Qwen3-VL-Embedding-2B":
        return "Qwen/Qwen3-VL-Embedding-2B"
    return model_name


def _resolve_embedding_settings(bundle) -> tuple[str, str, str]:
    """Materialize embedding model, base URL, and API key from ops + app config."""
    from viknow_platform.ops.config.model_resolve import resolve_openai_chat
    from viknow_platform.ops.config.store import plain_provider

    row = plain_provider("embedding") or bundle.providers.get("embedding") or {}
    model = _normalize_embedding_model(str(row.get("model_name") or "Qwen/Qwen3-VL-Embedding-2B"))

    override_base = os.environ.get("VIKNOW_HYPERRAG_EMBEDDING_BASE_URL", "").strip()
    base = override_base or str(row.get("base_url") or "").strip()
    if not base or _is_loopback_url(base):
        chat = resolve_openai_chat("server-default")
        parsed = urlparse(chat.base_url)
        host = parsed.hostname or "127.0.0.1"
        port = os.environ.get("VIKNOW_EMBEDDING_GATEWAY_PORT", "8601").strip() or "8601"
        scheme = os.environ.get("VIKNOW_EMBEDDING_GATEWAY_SCHEME", "http").strip() or "http"
        base = f"{scheme}://{host}:{port}/v1"

    override_key = os.environ.get("VIKNOW_HYPERRAG_EMBEDDING_API_KEY", "").strip()
    api_key = override_key or str(row.get("api_key") or "").strip()
    if not api_key:
        api_key = resolve_openai_chat("server-default").api_key

    return model, base.rstrip("/"), api_key


def _knowledge_libraries_root() -> Path:
    explicit = os.environ.get("VIKNOW_LIBRARIES_ROOT", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    data_dir = os.environ.get("VIKNOW_DATA_DIR", "").strip() or "/data/viknow"
    return (Path(data_dir) / "hyperrag-wd").resolve()


def build_hyperrag_payload_from_ops(state_dir: Path | None = None) -> dict[str, Any]:
    bundle = load_runtime_bundle()
    payload: dict[str, Any] = dict(ENGINE_DEFAULTS)
    mineru_backend = os.environ.get("VIKNOW_HYPERRAG_MINERU_BACKEND", "").strip()
    if mineru_backend:
        payload["mineru_backend"] = mineru_backend

    retrieval = bundle.retrieval
    payload["workspace"] = retrieval.get("library") or "default"
    if retrieval.get("rerank_enabled") is not None:
        payload["rerank_enabled"] = bool(retrieval["rerank_enabled"])

    payload["redis_uri"] = bundle.redis_url
    payload["working_dir"] = str(_knowledge_libraries_root())

    if bundle.scheduler_unified:
        unified = dict(bundle.scheduler_unified)
        # HyperRAG defaults scheduler.unified.redis_url to localhost without auth when omitted.
        unified.setdefault("redis_url", bundle.redis_url)
        payload["scheduler"] = {"unified": unified}
        if unified.get("max_concurrent_job_tasks") is not None:
            payload["max_concurrent_job_tasks"] = unified["max_concurrent_job_tasks"]

    env: dict[str, str] = dict(payload.get("env") or {})
    pg = bundle.postgres
    for key, col in (
        ("POSTGRES_HOST", "host"),
        ("POSTGRES_PORT", "port"),
        ("POSTGRES_USER", "user"),
        ("POSTGRES_PASSWORD", "password"),
        ("POSTGRES_DATABASE", "database"),
    ):
        value = pg.get(col)
        if value is not None and str(value).strip() != "":
            env[key] = str(value)

    neo = bundle.neo4j
    for key, col in (("NEO4J_URI", "uri"), ("NEO4J_USERNAME", "username"), ("NEO4J_PASSWORD", "password")):
        value = neo.get(col)
        if value is not None and str(value).strip() != "":
            env[key] = str(value)

    observe = bundle.observe
    for src, dst in (
        ("langfuse_host", "LANGFUSE_HOST"),
        ("langfuse_public_key", "LANGFUSE_PUBLIC_KEY"),
        ("langfuse_secret_key", "LANGFUSE_SECRET_KEY"),
    ):
        value = observe.get(src)
        if value:
            env[dst] = str(value)
    if observe.get("tracing_enabled"):
        env["LANGFUSE_TRACING_ENABLED"] = "true"
    if observe.get("media_enabled"):
        env["LANGFUSE_MEDIA_UPLOAD_ENABLED"] = "true"

    env.setdefault("LOG_LEVEL", os.environ.get("HYPERRAG_LOG_LEVEL", "INFO"))
    env.setdefault("PROMPT_ENV", os.environ.get("HYPERRAG_PROMPT_ENV", "test"))
    env.setdefault("JOB_LOG_DIR", os.environ.get("HYPERRAG_JOB_LOG_DIR", "/tmp/viknow-platform-job-logs"))
    env.setdefault("LD_LIBRARY_PATH", os.environ.get("HYPERRAG_LD_LIBRARY_PATH", ""))
    payload["env"] = env

    from viknow_platform.application.hyperrag.actio_materialize import _materialize_private_llm_base_url
    from viknow_platform.ops.config.model_resolve import resolve_openai_chat
    from viknow_platform.ops.config.runtime_env import load_agent_runtime_env

    load_agent_runtime_env()
    private = bundle.providers.get("private") or {}
    chat = resolve_openai_chat("server-default")
    if private.get("model_name"):
        payload["llm_model"] = str(private["model_name"])
    ops_llm_base = str(private.get("base_url") or "").strip()
    deploy_llm_base = str(chat.base_url or "").strip()
    if ops_llm_base or deploy_llm_base:
        payload["llm_base_url"] = _materialize_private_llm_base_url(ops_llm_base, deploy_llm_base)
    if private.get("api_key"):
        payload["llm_api_key"] = str(private["api_key"])

    embed_model, embed_base, embed_key = _resolve_embedding_settings(bundle)
    payload["embedding_model"] = embed_model
    payload["embedding_provider"] = payload.get("embedding_provider") or "vllm"
    payload["embedding_base_url"] = embed_base
    if embed_key:
        payload["embedding_api_key"] = embed_key
    model_override = os.environ.get("VIKNOW_HYPERRAG_EMBEDDING_MODEL", "").strip()
    if model_override:
        payload["embedding_model"] = _normalize_embedding_model(model_override)

    if payload.get("enable_video_embedding"):
        for target, source in (
            ("video_embedding_model", "embedding_model"),
            ("video_embedding_base_url", "embedding_base_url"),
            ("video_embedding_api_key", "embedding_api_key"),
        ):
            if payload.get(source) is not None:
                payload[target] = payload[source]

    rerank = bundle.providers.get("rerank") or {}
    if rerank.get("model_name"):
        payload["rerank_model"] = str(rerank["model_name"])
    if rerank.get("base_url"):
        payload["rerank_base_url"] = str(rerank["base_url"])
    if rerank.get("api_key"):
        payload["rerank_api_key"] = str(rerank["api_key"])

    from viknow_platform.application.hyperrag.actio_materialize import (
        build_actio_hyperrag_section,
        resolve_platform_repo_root,
    )

    repo_root = resolve_platform_repo_root()
    if repo_root is not None:
        resolved_state = (state_dir or Path(os.environ.get("VIKNOW_STATE_DIR", "/tmp/viknow-platform-state"))).resolve()
        try:
            actio_section, actio_config_dir = build_actio_hyperrag_section(
                repo_root=repo_root,
                state_dir=resolved_state,
                bundle=bundle,
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if actio_section:
            payload["actio"] = actio_section
        if actio_config_dir:
            payload["actio_config_dir"] = actio_config_dir

    return payload
