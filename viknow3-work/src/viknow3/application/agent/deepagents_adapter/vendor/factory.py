"""Build one shared DeepAgentsRuntime per agent profile.

Platform model endpoints come from viknow_ops. Shared loop settings come from
``src/agents/runtime/runtime.yaml``. The profile comes from the agent directory.
"""

from __future__ import annotations

import asyncio
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from agents.layout import AgentPackage, package_for_public_id
from viknow3.application.agent.deepagents_adapter.vendor.host_config import (
    build_agent_app_config,
    review_tool_configs,
)

log = logging.getLogger(__name__)

_RUNTIMES: dict[str, Any] = {}
_RUNTIME_LOCK: asyncio.Lock | None = None


def _repo_root() -> Path:
    root = os.environ.get("VIKNOW3_ROOT", "").strip()
    if root:
        return Path(root).resolve()
    # viknow3-work/src/viknow3/application/agent/deepagents_adapter/vendor/factory.py -> repo root
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


@lru_cache(maxsize=8)
def _viknow_app_config(skills: str, profile_dir: str, public_id: str, profile_id: str):
    root = _repo_root()
    files_root = Path(os.environ.get("VIKNOW_VENDOR_FILES_ROOT", str(root / ".state" / "vendor-files"))).resolve()
    return build_agent_app_config(
        skills_dir=Path(skills),
        profile_dir=Path(profile_dir),
        agent_ids=(public_id, profile_id),
        files_root=files_root,
        static_dir=root / "ui",
    )


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
        from agents.runtime.core.runtime.builder import build_agent_runtime

        log.info("runtime: loading config for %s", package.directory)
        config = _viknow_app_config(
            str(package.skills_dir),
            str(package.profile_path.parent),
            public_id,
            package.profile_id,
        )
        profile = _load_profile(package)
        model_factory = None
        from viknow3.application.agent.deepagents_adapter.script_model import (
            script_chat_model,
            script_model_enabled,
        )

        if script_model_enabled():
            profile = profile.model_copy(
                update={
                    "answer_review": profile.answer_review.model_copy(
                        update={"llm_reviewers_enabled": False, "gate_enabled": False}
                    )
                }
            )
            model_factory = script_chat_model
            log.info("runtime: script model for profile %s", profile.id)
        log.info("runtime: loading tools for profile %s", profile.id)
        tool_configs = review_tool_configs(config)
        knowledge_adapter = None
        actio_client = None
        try:
            from viknow3.application.hyperrag.runtime import get_engine_adapter

            knowledge_adapter = get_engine_adapter()
        except Exception:
            log.warning("HyperRAG adapter unavailable for vendor runtime; knowledge tools may fail", exc_info=True)
        try:
            from viknow3.application.actio.client import get_actio_client

            actio_client = get_actio_client()
        except Exception:
            log.warning("Actio client unavailable for vendor runtime; multimodal tools may fail", exc_info=True)
        try:
            from viknow3.application.hyperrag.runtime import install_actio_port

            install_actio_port()
        except Exception:
            log.warning("Actio indexing port was not installed; video indexing stays unavailable", exc_info=True)
        root = _repo_root()
        log.info("vendor runtime: building DeepAgentsRuntime for %s", profile.id)
        runtime = build_agent_runtime(
            config,
            profile=profile,
            tool_configs=tool_configs,
            workspace_root=root,
            event_store=None,
            knowledge_adapter=knowledge_adapter,
            actio_client=actio_client,
            model_factory=model_factory,
        )
        _RUNTIMES[key] = runtime
        log.info("DeepAgentsRuntime ready (profile=%s)", profile.id)
        return runtime
