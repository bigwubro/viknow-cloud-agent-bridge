"""Bubblewrap-isolated workspace backend for deepagents execute()."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol

from viknow3.application.agent.knowledge_assistant.workspace_config import DeepagentsSandboxConfig

if TYPE_CHECKING:
    from pathlib import Path as PathType

logger = logging.getLogger(__name__)

_BWRAP = shutil.which("bwrap")


class BubblewrapBackend(FilesystemBackend, SandboxBackendProtocol):
    """Filesystem rooted at a session workspace; shell runs inside bubblewrap."""

    def __init__(
        self,
        root_dir: str | PathType,
        *,
        virtual_mode: bool = True,
        max_file_size_mb: int = 25,
        sandbox: DeepagentsSandboxConfig | None = None,
    ) -> None:
        self._sandbox = sandbox or DeepagentsSandboxConfig()
        super().__init__(root_dir=root_dir, virtual_mode=virtual_mode, max_file_size_mb=max_file_size_mb)
        self._sandbox_id = f"bwrap-{uuid.uuid4().hex[:8]}"

    @property
    def id(self) -> str:
        return self._sandbox_id

    def _bwrap_argv(self, command: str) -> list[str]:
        host_root = Path(self.cwd).resolve()
        host_root.mkdir(parents=True, exist_ok=True)
        argv = [
            _BWRAP or "bwrap",
            "--die-with-parent",
            "--unshare-all",
            "--new-session",
            "--ro-bind",
            "/usr",
            "/usr",
        ]
        for lib in ("/lib", "/lib64", "/bin", "/sbin"):
            if Path(lib).is_dir():
                argv.extend(["--ro-bind", lib, lib])
        if Path("/etc/resolv.conf").is_file() and self._sandbox.network_access:
            argv.extend(["--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf"])
        argv.extend(["--bind", str(host_root), "/workspace"])
        argv.extend(["--proc", "/proc"])
        argv.extend(["--dev", "/dev"])
        for host, target in self._sandbox.extra_bind_mounts:
            if Path(host).exists():
                argv.extend(["--ro-bind", host, target])
        argv.extend(["--chdir", "/workspace"])
        argv.extend(["/bin/bash", "-lc", command])
        return argv

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        if not _BWRAP:
            return ExecuteResponse(
                output="Error: bubblewrap (bwrap) is not installed on this host.",
                exit_code=127,
                truncated=False,
            )
        limit = timeout if timeout is not None else self._sandbox.timeout_seconds
        env = os.environ.copy()
        env.update(self._sandbox.extra_env)
        try:
            completed = subprocess.run(
                self._bwrap_argv(command),
                capture_output=True,
                text=True,
                timeout=limit,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ExecuteResponse(
                output=f"Error: command timed out after {limit}s",
                exit_code=124,
                truncated=False,
            )
        output = (completed.stdout or "") + (completed.stderr or "")
        truncated = False
        max_bytes = self._sandbox.max_output_bytes
        if len(output.encode("utf-8", errors="replace")) > max_bytes:
            output = output[:max_bytes]
            truncated = True
        return ExecuteResponse(output=output, exit_code=completed.returncode, truncated=truncated)
