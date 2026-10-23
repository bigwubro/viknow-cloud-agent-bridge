"""Resolve mem0 deployment config from app yaml and persistence backend."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from viknow.core.config import PersistenceConfig

_DEFAULT_QDRANT_PATH = ".state/mem0/qdrant"
_DEFAULT_COLLECTION_NAME = "viknow_memories"
_DEFAULT_EMBEDDING_DIMS = 1024
_ENV_TOKEN_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_mem0_config(
    mem0_config: dict[str, Any],
    *,
    state_dir: Path,
    config_dir: Path,
    persistence_backend: str | None = None,
    persistence: PersistenceConfig | None = None,
) -> dict[str, Any]:
    """Build mem0 OSS config dict.

    Vector backend follows ``persistence.backend``: sqlite → local qdrant,
    postgres → pgvector from ``persistence.postgres``. Yaml only needs shared
    fields (``collection_name``, ``embedding_model_dims``); provider/path are injected.
    """

    from viknow.core.config import (
        PersistenceBackend,
        PersistenceConfig,
        resolve_memory_postgres_dsn,
    )

    resolved = _resolve_env_tokens(mem0_config)
    resolved.pop("extraction", None)
    vector_store = dict(resolved.get("vector_store") or {})
    provider = str(vector_store.get("provider") or "").strip().lower()
    provider_config = dict(vector_store.get("config") or {})
    collection_name = str(provider_config.get("collection_name") or _DEFAULT_COLLECTION_NAME)
    embedding_dims = int(provider_config.get("embedding_model_dims") or _DEFAULT_EMBEDDING_DIMS)
    backend = persistence_backend or PersistenceBackend.SQLITE

    if backend == PersistenceBackend.POSTGRES and provider != "pgvector":
        if not isinstance(persistence, PersistenceConfig):
            raise RuntimeError("persistence config is required for mem0 postgres backend")
        provider = "pgvector"
        provider_config = {
            "collection_name": collection_name,
            "embedding_model_dims": embedding_dims,
            "connection_string": resolve_memory_postgres_dsn(persistence),
        }
        vector_store = {"provider": provider, "config": provider_config}
        provider = str(vector_store.get("provider") or "").strip().lower()
        provider_config = dict(vector_store.get("config") or {})
    elif backend == PersistenceBackend.SQLITE and provider in ("", "auto", "qdrant"):
        provider = "qdrant"
        provider_config = {
            **provider_config,
            "collection_name": collection_name,
            "embedding_model_dims": embedding_dims,
        }
        provider_config.setdefault("path", _DEFAULT_QDRANT_PATH)
        provider_config.setdefault("on_disk", True)

    if "path" in provider_config:
        provider_config["path"] = str(
            _resolve_storage_path(
                provider_config["path"], state_dir=state_dir, config_dir=config_dir
            )
        )
        provider_config.setdefault("on_disk", True)
    if provider == "pgvector":
        provider_config.setdefault("embedding_model_dims", _DEFAULT_EMBEDDING_DIMS)
        if "connection_string" in provider_config:
            connection_string = str(provider_config["connection_string"]).strip()
            if not connection_string:
                raise RuntimeError("mem0 pgvector connection_string must not be empty")
            provider_config["connection_string"] = connection_string
    if not provider:
        raise RuntimeError(
            "mem0 vector_store.provider is empty; set persistence.backend or an explicit provider"
        )
    vector_store["provider"] = provider
    vector_store["config"] = provider_config
    resolved["vector_store"] = vector_store
    resolved.setdefault("version", "v1.1")
    return resolved


def _resolve_storage_path(raw_path: str, *, state_dir: Path, config_dir: Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    if raw_path.startswith(".state/"):
        relative = raw_path.removeprefix(".state/").lstrip("/")
        return (state_dir / relative).resolve()
    return (config_dir / path).resolve()


def _resolve_env_tokens(value: Any) -> Any:
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            env_value = os.getenv(match.group(1))
            if env_value is None:
                raise RuntimeError(f"environment variable is required: {match.group(1)}")
            return env_value

        return _ENV_TOKEN_RE.sub(replace, value)
    if isinstance(value, list):
        return [_resolve_env_tokens(item) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_env_tokens(item) for key, item in value.items()}
    return value
