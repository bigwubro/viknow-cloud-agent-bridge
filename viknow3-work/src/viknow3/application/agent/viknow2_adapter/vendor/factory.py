"""Build one shared DeepAgentsRuntime per agent profile."""

from __future__ import annotations

import asyncio
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from agents.layout import AgentPackage, package_for_public_id
from viknow3.application.agent.viknow2_adapter.vendor.checkpointer import Viknow3CheckpointerProvider
from viknow3.application.agent.viknow2_adapter.vendor.thread_id import apply_session_thread_id_patch

log = logging.getLogger(__name__)

_RUNTIMES: dict[str, Any] = {}
_RUNTIME_LOCK: asyncio.Lock | None = None


def _repo_root() -> Path:
    root = os.environ.get("VIKNOW3_ROOT", "").strip()
    if root:
        return Path(root).resolve()
    # viknow3-work/src/viknow3/application/agent/viknow2_adapter/vendor/factory.py -> repo root
    return Path(__file__).resolve().parents[6]


def _load_profile(package: AgentPackage):
    """Load one agent profile. Expert Buddy keeps the playground budget off."""
    from agents.runtime.core.runtime.domain import AgentProfile

    data = yaml.safe_load(package.profile_path.read_text(encoding="utf-8"))
    if package.directory == "expert_buddy":
        data.pop("runtime_budget", None)
    unknown = sorted(set(data) - set(AgentProfile.model_fields))
    for key in unknown:
        data.pop(key, None)
    if unknown:
        log.info("profile %s: ignored fields %s", package.profile_id, ", ".join(unknown))
    profile = AgentProfile.model_validate(data)
    if package.directory == "expert_buddy" and profile.runtime_budget.enabled:
        profile = profile.model_copy(
            update={"runtime_budget": profile.runtime_budget.model_copy(update={"enabled": False})}
        )
    return profile


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


@lru_cache(maxsize=8)
def _viknow_app_config(skills: Path, profile_dir: Path):
    from agents.runtime.core.config import load_app_config

    root = _repo_root()
    config_path = _viknow_config_path(root)
    config = load_app_config(config_path, workspace_root=config_path.parent.parent)
    checkpointer = config.agent.deepagents.checkpointer.model_copy(update={"enabled": False})
    deepagents = config.agent.deepagents.model_copy(
        update={"checkpointer": checkpointer, "recursion_limit": 999}
    )
    # Playground uses viknow3 Postgres checkpointer. Do not open viknow2's Redis scheduler.
    unified = config.share.scheduler.unified.model_copy(update={"enabled": False})
    scheduler = config.share.scheduler.model_copy(update={"unified": unified})
    # viknow2 files_root (/data/viknow) is not writable by the playground user.
    files_root = Path(os.environ.get("VIKNOW_VENDOR_FILES_ROOT", str(root / ".state" / "vendor-files"))).resolve()
    files_root.mkdir(parents=True, exist_ok=True)
    (files_root / "state").mkdir(parents=True, exist_ok=True)
    share = config.share.model_copy(update={"scheduler": scheduler, "files_root": files_root})
    app = config.app.model_copy(update={"files_root": files_root, "state_dir": files_root / "state"})
    agent = config.agent.model_copy(
        update={"skill_dirs": (skills,), "profile_dir": profile_dir, "deepagents": deepagents}
    )
    return config.model_copy(update={"agent": agent, "share": share, "app": app})


async def get_deepagents_runtime(public_id: str = "deepagents") -> Any:
    """Lazy runtime for one public profile id. Must run on the agent event loop."""
    global _RUNTIME_LOCK
    package = package_for_public_id(public_id)
    if package is None:
        raise KeyError(f"no agent package for profile {public_id}")
    key = str(package.profile_path)
    cached = _RUNTIMES.get(key)
    if cached is not None:
        return cached
    if _RUNTIME_LOCK is None:
        _RUNTIME_LOCK = asyncio.Lock()
    async with _RUNTIME_LOCK:
        cached = _RUNTIMES.get(key)
        if cached is not None:
            return cached
        apply_session_thread_id_patch()
        from agents.runtime.core.config import load_tool_configs
        from agents.runtime.core.runtime.builder import build_agent_runtime

        log.info("runtime: loading config for %s", package.directory)
        config = _viknow_app_config(package.skills_dir, package.profile_path.parent)
        profile = _load_profile(package)
        log.info("runtime: loading tools for profile %s", profile.id)
        tool_configs = load_tool_configs(config)
        knowledge_adapter = None
        try:
            from viknow3.application.hyperrag.runtime import get_engine_adapter

            knowledge_adapter = get_engine_adapter()
        except Exception:
            log.warning("HyperRAG adapter unavailable for vendor runtime; knowledge tools may fail", exc_info=True)
        root = _repo_root()
        log.info("vendor runtime: building DeepAgentsRuntime for %s", profile.id)
        runtime = build_agent_runtime(
            config,
            profile=profile,
            tool_configs=tool_configs,
            workspace_root=root,
            event_store=None,
            knowledge_adapter=knowledge_adapter,
        )
        runtime._checkpoints = Viknow3CheckpointerProvider()  # noqa: SLF001
        _RUNTIMES[key] = runtime
        log.info("vendored DeepAgentsRuntime ready (profile=%s)", profile.id)
        return runtime
