"""Build a process-singleton vendored DeepAgentsRuntime for viknow3 Playground."""

from __future__ import annotations

import asyncio
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from viknow3.application.agent.viknow2_adapter.vendor.checkpointer import Viknow3CheckpointerProvider
from viknow3.application.agent.viknow2_adapter.vendor.thread_id import apply_session_thread_id_patch

log = logging.getLogger(__name__)

_RUNTIME: Any = None
_RUNTIME_LOCK: asyncio.Lock | None = None


def _repo_root() -> Path:
    root = os.environ.get("VIKNOW3_ROOT", "").strip()
    if root:
        return Path(root).resolve()
    # viknow3-work/src/viknow3/application/agent/viknow2_adapter/vendor/factory.py -> repo root
    return Path(__file__).resolve().parents[6]


def _skills_dir() -> Path:
    packaged = Path(__file__).resolve().parents[2] / "qa_box" / "skills"
    if packaged.is_dir():
        return packaged
    return _repo_root() / "skills"


@lru_cache(maxsize=1)
def _viknow_app_config():
    from viknow.core.config import load_app_config

    root = _repo_root()
    for candidate in (root / "config" / "local" / "app.yaml", root / "config" / "app.yaml"):
        if candidate.is_file():
            config = load_app_config(candidate, workspace_root=root)
            break
    else:
        config = load_app_config(workspace_root=root)
    skills = _skills_dir()
    checkpointer = config.agent.deepagents.checkpointer.model_copy(update={"enabled": False})
    deepagents = config.agent.deepagents.model_copy(update={"checkpointer": checkpointer})
    agent = config.agent.model_copy(update={"skill_dirs": (skills,), "deepagents": deepagents})
    return config.model_copy(update={"agent": agent})


async def get_deepagents_runtime() -> Any:
    """Lazy singleton; must be called on the agent event loop."""
    global _RUNTIME, _RUNTIME_LOCK
    if _RUNTIME is not None:
        return _RUNTIME
    if _RUNTIME_LOCK is None:
        _RUNTIME_LOCK = asyncio.Lock()
    async with _RUNTIME_LOCK:
        if _RUNTIME is not None:
            return _RUNTIME
        apply_session_thread_id_patch()
        from viknow.core.config import load_tool_configs
        from viknow.core.runtime.builder import build_agent_runtime

        config = _viknow_app_config()
        profile = config.profile_catalog.require(config.agent.default_profile)
        tool_configs = load_tool_configs(config)
        knowledge_adapter = None
        try:
            from viknow3.application.hyperrag.runtime import get_engine_adapter

            knowledge_adapter = get_engine_adapter()
        except Exception:
            log.warning("HyperRAG adapter unavailable for vendor runtime; knowledge tools may fail", exc_info=True)
        root = _repo_root()
        runtime = build_agent_runtime(
            config,
            profile=profile,
            tool_configs=tool_configs,
            workspace_root=root,
            event_store=None,
            knowledge_adapter=knowledge_adapter,
        )
        runtime._checkpoints = Viknow3CheckpointerProvider()  # noqa: SLF001
        _RUNTIME = runtime
        log.info("vendored DeepAgentsRuntime ready (profile=%s)", profile.id)
        return _RUNTIME
