"""Resolve a chat model from viknow_ops.

Platform rows hold the endpoint and key. Sampling defaults live in
``src/agents/runtime/runtime.yaml``. This module does not read ``app.yaml``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from viknow3.ops.config.runtime_env import load_agent_runtime_env

_SERVER_DEFAULT = "server-default"
_PROVIDER_ENV = {
    "private": ("VIKNOW_PRIVATE_BASE_URL", "VIKNOW_PRIVATE_API_KEY", "VIKNOW_PRIVATE_MODEL"),
    "siliconflow": ("VIKNOW_SILICONFLOW_BASE_URL", "VIKNOW_SILICONFLOW_API_KEY", None),
    "embedding": ("VIKNOW_EMBEDDING_BASE_URL", "VIKNOW_EMBEDDING_API_KEY", "VIKNOW_EMBEDDING_MODEL"),
    "rerank": ("HYPERRAG_RERANK_BASE_URL", "HYPERRAG_RERANK_API_KEY", None),
    "actio": ("ACTIO_SERVER_URL", "ACTIO_API_KEY", None),
}


@dataclass(frozen=True)
class ResolvedChatModel:
    model: str
    base_url: str
    api_key: str
    temperature: float = 0.0


@dataclass(frozen=True)
class ChatProvider:
    base_url: str
    api_key: str
    model_name: str | None = None


@dataclass(frozen=True)
class ChatModelRow:
    id: str
    provider_id: str
    name: str
    label: str


@dataclass(frozen=True)
class PlatformCatalog:
    """Models and providers copied out of viknow_ops."""

    providers: dict[str, ChatProvider]
    models: tuple[ChatModelRow, ...]
    default_model_id: str | None = None
    agent_model_id: str | None = None


def provider_env_names(provider_id: str) -> tuple[str, str, str | None]:
    known = _PROVIDER_ENV.get(provider_id)
    if known is not None:
        return known
    token = re.sub(r"[^A-Za-z0-9]", "_", provider_id).upper()
    return (
        f"VIKNOW_PROVIDER_{token}_BASE_URL",
        f"VIKNOW_PROVIDER_{token}_API_KEY",
        f"VIKNOW_PROVIDER_{token}_MODEL",
    )


def publish_catalog_env(catalog: PlatformCatalog) -> None:
    """Put ops endpoints into the env names the agent runtime already reads."""
    for provider_id, provider in catalog.providers.items():
        base_env, key_env, model_env = provider_env_names(provider_id)
        if provider.base_url:
            os.environ[base_env] = provider.base_url
        if provider.api_key:
            os.environ[key_env] = provider.api_key
        if model_env and provider.model_name:
            os.environ[model_env] = provider.model_name


def load_platform_catalog(*agent_ids: str) -> PlatformCatalog:
    """Read model providers and the optional per-agent model from viknow_ops."""
    load_agent_runtime_env()
    from viknow3.ops.config.store import (
        agent_model_map,
        configured,
        plain_models,
        plain_providers,
        server_default_model,
    )

    if not configured():
        raise RuntimeError("VIKNOW_OPS_DSN is required. Chat models are read from viknow_ops, not app.yaml.")
    raw_providers = plain_providers()
    providers = {
        provider_id: ChatProvider(
            base_url=str(row.get("base_url") or "").strip(),
            api_key=str(row.get("api_key") or "").strip(),
            model_name=(str(row.get("model_name")).strip() if row.get("model_name") else None),
        )
        for provider_id, row in raw_providers.items()
    }
    models = tuple(
        ChatModelRow(
            id=row["id"],
            provider_id=row["provider_id"],
            name=row["name"],
            label=row["label"],
        )
        for row in plain_models()
    )
    binding = agent_model_map()
    agent_model_id = next((binding[agent_id] for agent_id in agent_ids if binding.get(agent_id)), None)
    catalog = PlatformCatalog(
        providers=providers,
        models=models,
        default_model_id=server_default_model(),
        agent_model_id=agent_model_id,
    )
    publish_catalog_env(catalog)
    return catalog


def resolve_openai_chat(
    model_selector: str | None = None,
    *,
    catalog: PlatformCatalog | None = None,
) -> ResolvedChatModel:
    """Resolve an OpenAI-compatible chat endpoint."""
    from agents.runtime.runtime_config import load_model_defaults

    if catalog is None:
        catalog = load_platform_catalog()
    else:
        publish_catalog_env(catalog)
    return resolve_chat(model_selector, catalog, temperature=load_model_defaults().temperature)


def resolve_chat(
    model_selector: str | None,
    catalog: PlatformCatalog,
    *,
    temperature: float,
) -> ResolvedChatModel:
    selector = (model_selector or "").strip()
    if not selector or selector == _SERVER_DEFAULT:
        row = _preferred_row(catalog)
        if row is None:
            raise RuntimeError(
                "No default chat model in viknow_ops. Set model_provider.private.model_name or agent_model."
            )
        return _from_row(row, catalog, temperature)

    matched = _match_row(catalog, selector)
    if matched is not None:
        return _from_row(matched, catalog, temperature)

    if ":" in selector:
        provider_id, model_name = selector.split(":", 1)
        provider = catalog.providers.get(provider_id.strip())
        if provider is not None and provider.base_url and provider.api_key and model_name.strip():
            return ResolvedChatModel(
                model=model_name.strip(),
                base_url=provider.base_url,
                api_key=provider.api_key,
                temperature=temperature,
            )

    private = catalog.providers.get("private")
    if private is not None and private.base_url and private.api_key:
        return ResolvedChatModel(
            model=selector,
            base_url=private.base_url,
            api_key=private.api_key,
            temperature=temperature,
        )
    raise RuntimeError(f"Chat model {selector!r} is not configured in viknow_ops")


def agent_model_ref(catalog: PlatformCatalog) -> str:
    row = _preferred_row(catalog)
    if row is None:
        raise RuntimeError(
            "No chat model for this agent. Set agent_model or model_provider.private.model_name in viknow_ops."
        )
    if row.provider_id not in catalog.providers:
        raise RuntimeError(f"model {row.id} references unknown provider {row.provider_id}")
    return f"{row.provider_id}:{row.name}"


def _preferred_row(catalog: PlatformCatalog) -> ChatModelRow | None:
    for model_id in (catalog.agent_model_id, catalog.default_model_id):
        if not model_id:
            continue
        found = _row_by_id(catalog, model_id)
        if found is not None:
            return found
    private = catalog.providers.get("private")
    if private is not None and private.model_name:
        return ChatModelRow(
            id=f"private:{private.model_name}",
            provider_id="private",
            name=private.model_name,
            label=private.model_name,
        )
    return None


def _row_by_id(catalog: PlatformCatalog, model_id: str) -> ChatModelRow | None:
    for row in catalog.models:
        if row.id == model_id:
            return row
    return None


def _match_row(catalog: PlatformCatalog, selector: str) -> ChatModelRow | None:
    normalized = selector.strip().lower()
    for row in catalog.models:
        ref = f"{row.provider_id}:{row.name}".lower()
        if normalized in {row.id.lower(), row.label.lower(), row.name.lower(), ref}:
            return row
    return None


def _from_row(row: ChatModelRow, catalog: PlatformCatalog, temperature: float) -> ResolvedChatModel:
    provider = catalog.providers.get(row.provider_id)
    if provider is None or not provider.base_url or not provider.api_key:
        raise RuntimeError(f"provider {row.provider_id!r} has no base_url or api_key in viknow_ops")
    if not row.name:
        raise RuntimeError(f"model {row.id!r} has an empty name")
    return ResolvedChatModel(
        model=row.name,
        base_url=provider.base_url,
        api_key=provider.api_key,
        temperature=temperature,
    )
