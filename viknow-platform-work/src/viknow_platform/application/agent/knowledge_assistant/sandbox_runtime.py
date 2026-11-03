"""deepagents_sandbox.Sandbox factory (viknow2 _create_bubblewrap_sandbox)."""

from __future__ import annotations

import platform
from dataclasses import dataclass
from typing import Any, Callable

from viknow_platform.application.agent.knowledge_assistant.workspace_config import load_deepagents_workspace_config


@dataclass
class BubblewrapRuntime:
    backend: Any
    close: Callable[[], None] | None = None


def create_bubblewrap_sandbox() -> BubblewrapRuntime:
    if platform.system() != "Linux":
        raise RuntimeError(
            "bubblewrap sandbox requires Linux, bubblewrap, and cgroup v2; "
            "run ViKnow in the devcontainer or on Ubuntu."
        )
    from deepagents_sandbox import Sandbox, SandboxConfig, detect

    cfg = load_deepagents_workspace_config()
    sandbox_cfg = cfg.sandbox
    failed_checks = [check for check in detect.check_all() if not check.passed]
    if failed_checks:
        details = "; ".join(check.reason for check in failed_checks)
        raise RuntimeError(
            "bubblewrap sandbox requires Linux, bubblewrap, user namespaces, "
            f"and cgroup v2: {details}"
        )

    config = SandboxConfig(
        memory_limit_mb=sandbox_cfg.memory_limit_mb,
        max_pids=sandbox_cfg.max_pids,
        cpu_shares=sandbox_cfg.cpu_shares,
        timeout_seconds=sandbox_cfg.timeout_seconds,
        max_output_bytes=sandbox_cfg.max_output_bytes,
        network_access=sandbox_cfg.network_access,
        gpu=sandbox_cfg.gpu,
        extra_bind_mounts=list(sandbox_cfg.extra_bind_mounts),
        extra_env={
            **sandbox_cfg.extra_env,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        },
    )
    sandbox = Sandbox(config=config, default_timeout=int(sandbox_cfg.timeout_seconds))
    return BubblewrapRuntime(backend=sandbox, close=getattr(sandbox, "close", None))
