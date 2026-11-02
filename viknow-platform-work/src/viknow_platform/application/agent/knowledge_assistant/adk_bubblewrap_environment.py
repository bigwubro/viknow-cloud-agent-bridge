"""ADK skill script execution inside bubblewrap (session workspace bind-mount only)."""

from __future__ import annotations

import asyncio
import logging
import platform
from pathlib import Path
from typing import Any

from google.adk.environment._base_environment import ExecutionResult
from google.adk.environment._local_environment import LocalEnvironment

from viknow_platform.application.agent.knowledge_assistant.workspace_config import DeepagentsSandboxConfig

log = logging.getLogger(__name__)

_SANDBOX_WORKDIR = "/workspace"


class BubblewrapScriptEnvironment(LocalEnvironment):
    """Host read/write for skill materialization; ``execute`` runs in deepagents_sandbox."""

    def __init__(
        self,
        *,
        working_dir: Path,
        sandbox_cfg: DeepagentsSandboxConfig,
        env_vars: dict[str, str] | None = None,
    ) -> None:
        super().__init__(working_dir=working_dir, env_vars=env_vars)
        self._sandbox_cfg = sandbox_cfg
        self._sandbox: Any = None

    async def initialize(self) -> None:
        await super().initialize()
        for name in ("uploads", "scratch", "outputs"):
            (self.working_dir / name).mkdir(parents=True, exist_ok=True)

    def _ensure_sandbox(self) -> Any:
        if self._sandbox is not None:
            return self._sandbox
        if platform.system() != "Linux":
            raise RuntimeError("ADK bubblewrap script sandbox requires Linux")
        from deepagents_sandbox import Sandbox, SandboxConfig

        host_root = str(self.working_dir.resolve())
        binds = list(self._sandbox_cfg.extra_bind_mounts)
        if not any(host == host_root for host, _target in binds):
            binds.append((host_root, _SANDBOX_WORKDIR))
        config = SandboxConfig(
            memory_limit_mb=self._sandbox_cfg.memory_limit_mb,
            max_pids=self._sandbox_cfg.max_pids,
            cpu_shares=self._sandbox_cfg.cpu_shares,
            timeout_seconds=self._sandbox_cfg.timeout_seconds,
            max_output_bytes=self._sandbox_cfg.max_output_bytes,
            network_access=self._sandbox_cfg.network_access,
            gpu=self._sandbox_cfg.gpu,
            extra_bind_mounts=binds,
            extra_env={
                **self._sandbox_cfg.extra_env,
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": _SANDBOX_WORKDIR,
            },
        )
        self._sandbox = Sandbox(config=config, default_timeout=int(self._sandbox_cfg.timeout_seconds))
        return self._sandbox

    def _execute_sync(self, command: str, timeout: int | None) -> ExecutionResult:
        sandbox = self._ensure_sandbox()
        wrapped = f"cd {_SANDBOX_WORKDIR} && {command}"
        response = sandbox.execute(wrapped, timeout=timeout)
        output = response.output or ""
        truncated = bool(getattr(response, "truncated", False))
        stderr = ""
        if truncated:
            stderr = "(output truncated by sandbox limit)\n"
        return ExecutionResult(
            exit_code=int(response.exit_code or 0),
            stdout=output,
            stderr=stderr,
            timed_out=False,
        )

    async def execute(
        self,
        command: str,
        *,
        timeout: float | None = None,
    ) -> ExecutionResult:
        if self._working_dir is None:
            raise RuntimeError("`working_dir` is not set. Call initialize() first.")
        effective = int(timeout) if timeout is not None else int(self._sandbox_cfg.timeout_seconds)
        try:
            return await asyncio.to_thread(self._execute_sync, command, effective)
        except Exception as exc:
            log.warning("bubblewrap execute failed, falling back to local subprocess: %s", exc)
            return await super().execute(command, timeout=timeout)

    async def close(self) -> None:
        if self._sandbox is not None:
            close = getattr(self._sandbox, "close", None)
            if callable(close):
                close()
            self._sandbox = None
        await super().close()


def build_adk_script_environment(
    working_dir: Path,
    *,
    script_sandbox: str,
    sandbox_cfg: DeepagentsSandboxConfig,
) -> LocalEnvironment:
    mode = (script_sandbox or "host").strip().lower()
    if mode == "bubblewrap" and platform.system() == "Linux":
        try:
            from deepagents_sandbox import detect

            failed = [check for check in detect.check_all() if not check.passed]
            if failed:
                log.warning(
                    "ADK bubblewrap unavailable (%s); using LocalEnvironment",
                    "; ".join(check.reason for check in failed),
                )
                return LocalEnvironment(working_dir=working_dir)
        except ImportError:
            log.warning("deepagents_sandbox not installed; ADK using LocalEnvironment")
            return LocalEnvironment(working_dir=working_dir)
        return BubblewrapScriptEnvironment(working_dir=working_dir, sandbox_cfg=sandbox_cfg)
    return LocalEnvironment(working_dir=working_dir)
