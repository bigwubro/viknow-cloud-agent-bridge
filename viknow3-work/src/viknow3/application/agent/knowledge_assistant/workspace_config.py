"""Deepagents workspace settings from config/app.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_WORKSPACE_ROOT_ENV = "VIKNOW_AGENT_WORKSPACE_ROOT"
_SCRATCH_ENV = "VIKNOW_SCRATCH_DIR"


def _repo_root() -> Path:
    root = os.environ.get("VIKNOW3_ROOT", "").strip()
    if root:
        return Path(root)
    return Path(__file__).resolve().parents[5]


@lru_cache(maxsize=1)
def _load_app_config() -> dict[str, Any]:
    root = _repo_root()
    for path in (root / "config" / "app.yaml", root / "config" / "local" / "app.yaml"):
        if path.is_file():
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return payload if isinstance(payload, dict) else {}
    return {}


@dataclass(frozen=True)
class DeepagentsSandboxConfig:
    memory_limit_mb: int = 512
    max_pids: int = 256
    cpu_shares: int = 100
    timeout_seconds: int = 60
    max_output_bytes: int = 262_144
    network_access: bool = False
    gpu: bool = False
    extra_bind_mounts: tuple[tuple[str, str], ...] = ()
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DeepagentsWorkspaceConfig:
    backend: str = "bubblewrap"
    virtual_mode: bool = True
    max_file_size_mb: int = 25
    staging_keep_uploads_cache: bool = True
    staging_purge_outputs_after_sync: bool = True
    staging_purge_scratch_after_run: bool = True
    sandbox: DeepagentsSandboxConfig = DeepagentsSandboxConfig()


def agent_workspace_root() -> Path:
    explicit = os.environ.get(_WORKSPACE_ROOT_ENV, "").strip()
    if explicit:
        return Path(explicit)
    scratch = os.environ.get(_SCRATCH_ENV, "").strip()
    if scratch:
        return Path(scratch)
    return _repo_root() / ".state" / "agent-workspaces"


def session_workspace_dir(session_id: str) -> Path:
    safe = session_id.replace("/", "_").replace("..", "_")
    return agent_workspace_root() / "sessions" / safe / "workspace"


def load_deepagents_workspace_config() -> DeepagentsWorkspaceConfig:
    agent = _load_app_config().get("agent")
    if not isinstance(agent, dict):
        return DeepagentsWorkspaceConfig()
    deep = agent.get("deepagents")
    if not isinstance(deep, dict):
        return DeepagentsWorkspaceConfig()
    ws = deep.get("workspace")
    if not isinstance(ws, dict):
        return DeepagentsWorkspaceConfig()
    staging = ws.get("staging") if isinstance(ws.get("staging"), dict) else {}
    sandbox_raw = deep.get("sandbox") if isinstance(deep.get("sandbox"), dict) else {}
    binds: list[tuple[str, str]] = []
    for item in sandbox_raw.get("extra_bind_mounts") or []:
        if isinstance(item, dict):
            host = str(item.get("host") or item.get("src") or "").strip()
            target = str(item.get("target") or item.get("dst") or "").strip()
            if host and target:
                binds.append((host, target))
    extra_env_raw = sandbox_raw.get("extra_env")
    extra_env = {str(k): str(v) for k, v in extra_env_raw.items()} if isinstance(extra_env_raw, dict) else {}
    sandbox = DeepagentsSandboxConfig(
        memory_limit_mb=int(sandbox_raw.get("memory_limit_mb") or 512),
        max_pids=int(sandbox_raw.get("max_pids") or 256),
        cpu_shares=int(sandbox_raw.get("cpu_shares") or 100),
        timeout_seconds=int(sandbox_raw.get("timeout_seconds") or 60),
        max_output_bytes=int(sandbox_raw.get("max_output_bytes") or 262_144),
        network_access=bool(sandbox_raw.get("network_access")),
        gpu=bool(sandbox_raw.get("gpu")),
        extra_bind_mounts=tuple(binds),
        extra_env=extra_env,
    )
    return DeepagentsWorkspaceConfig(
        backend=str(ws.get("backend") or "bubblewrap").strip().lower(),
        virtual_mode=bool(ws.get("virtual_mode", True)),
        max_file_size_mb=int(ws.get("max_file_size_mb") or 25),
        staging_keep_uploads_cache=bool(staging.get("keep_uploads_cache", True)),
        staging_purge_outputs_after_sync=bool(staging.get("purge_outputs_after_sync", True)),
        staging_purge_scratch_after_run=bool(staging.get("purge_scratch_after_run", True)),
        sandbox=sandbox,
    )
