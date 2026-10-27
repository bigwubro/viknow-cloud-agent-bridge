"""Build configured user memory services."""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from agents.runtimes.deepagents.core.config import AppConfig
from agents.runtimes.deepagents.core.memory.config import resolve_mem0_config
from agents.runtimes.deepagents.core.memory.extraction import KIND_EXTRACTION_PROMPTS
from agents.runtimes.deepagents.core.memory.mem0_client import build_mem0_memory_client
from agents.runtimes.deepagents.core.memory.service import (
    Mem0UserMemoryService,
    NoOpUserMemoryService,
    UserMemoryService,
)


def build_user_memory_service(config: AppConfig) -> UserMemoryService:
    memory_config = config.memory
    if memory_config.mem0 is None:
        return NoOpUserMemoryService()
    try:
        mem0_raw = memory_config.mem0.model_dump(mode="python")
        mem0_config = resolve_mem0_config(
            mem0_raw,
            state_dir=config.app.state_dir,
            config_dir=config.config_dir,
            persistence_backend=config.persistence.backend,
            persistence=config.persistence,
        )
        vector_path = mem0_config.get("vector_store", {}).get("config", {}).get("path")
        if vector_path:
            Path(vector_path).mkdir(parents=True, exist_ok=True)
        client = build_mem0_memory_client(mem0_config)
        return Mem0UserMemoryService(
            client,
            extraction_prompts=KIND_EXTRACTION_PROMPTS,
        )
    except Exception as exc:
        logger.warning("failed to initialize mem0 memory service: {}", exc)
        return NoOpUserMemoryService()
