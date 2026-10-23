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


def _viknow_config_path(root: Path) -> Path:
    override = os.environ.get("VIKNOW_VENDOR_APP_CONFIG", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    v2_root = Path(os.environ.get("VIKNOW2_ROOT", "/home/cursor-agent/work/viknow2")).expanduser()
    for base in (v2_root, root):
        for rel in ("config/local/app.yaml", "config/app.yaml"):
            candidate = (base / rel).resolve()
            if candidate.is_file():
                return candidate
    return (root / "config" / "app.yaml").resolve()


@lru_cache(maxsize=1)
def _viknow_app_config():
    from viknow.core.config import load_app_config

    root = _repo_root()
    config_path = _viknow_config_path(root)
    config = load_app_config(config_path, workspace_root=config_path.parent.parent)
    skills = _skills_dir()
    checkpointer = config.agent.deepagents.checkpointer.model_copy(update={"enabled": False})
    deepagents = config.agent.deepagents.model_copy(update={"checkpointer": checkpointer})
    # Playground uses viknow3 Postgres checkpointer. Do not open viknow2's Redis scheduler.
    unified = config.share.scheduler.unified.model_copy(update={"enabled": False})
    scheduler = config.share.scheduler.model_copy(update={"unified": unified})
    # viknow2 files_root (/data/viknow) is not writable by the playground user.
    files_root = Path(os.environ.get("VIKNOW_VENDOR_FILES_ROOT", str(root / ".state" / "vendor-files"))).resolve()
    files_root.mkdir(parents=True, exist_ok=True)
    (files_root / "state").mkdir(parents=True, exist_ok=True)
    share = config.share.model_copy(update={"scheduler": scheduler, "files_root": files_root})
    app = config.app.model_copy(update={"files_root": files_root, "state_dir": files_root / "state"})
    agent = config.agent.model_copy(update={"skill_dirs": (skills,), "deepagents": deepagents})
    return config.model_copy(update={"agent": agent, "share": share, "app": app})


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

        log.info("vendor runtime: loading viknow config")
        config = _viknow_app_config()
        profile = config.profile_catalog.require(config.agent.default_profile)
        # Vendor middleware counts graph steps. The profile cap of 60 ends vague
        # questions before a final answer on this playground.
        if profile.runtime_budget.enabled and profile.runtime_budget.max_recursion_steps < 180:
            profile = profile.model_copy(
                update={
                    "runtime_budget": profile.runtime_budget.model_copy(
                        update={"max_recursion_steps": 180}
                    )
                }
            )
        log.info("vendor runtime: loading tools for profile %s", profile.id)
        tool_configs = load_tool_configs(config)
        knowledge_adapter = None
        try:
            from viknow3.application.hyperrag.runtime import get_engine_adapter

            knowledge_adapter = get_engine_adapter()
        except Exception:
            log.warning("HyperRAG adapter unavailable for vendor runtime; knowledge tools may fail", exc_info=True)
        root = _repo_root()
        log.info("vendor runtime: building DeepAgentsRuntime")
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
