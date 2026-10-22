"""Per-session deepagents backend (viknow2 parity: one workspace, skills under `.skills/`)."""

from __future__ import annotations

from collections.abc import Sequence

from deepagents.backends.composite import CompositeBackend
from deepagents.backends.protocol import BackendProtocol

from viknow3.application.agent.knowledge_assistant.bubblewrap_backend import BubblewrapBackend
from viknow3.application.agent.knowledge_assistant.session_workspace import (
    ensure_session_workspace,
    stage_runtime_skills,
)
from viknow3.application.agent.knowledge_assistant.workspace_config import load_deepagents_workspace_config

_WORKSPACE_ROUTE = "/workspace/"


def build_deepagents_backend(session_id: str, enabled_skills: Sequence[str]) -> BackendProtocol:
    """Session workspace with staged skills; virtual paths use the `/workspace/` prefix."""
    cfg = load_deepagents_workspace_config()
    workspace_host = ensure_session_workspace(session_id)
    stage_runtime_skills(session_id, enabled_skills)
    if cfg.backend == "bubblewrap":
        workspace_backend: BackendProtocol = BubblewrapBackend(
            root_dir=workspace_host,
            virtual_mode=cfg.virtual_mode,
            max_file_size_mb=cfg.max_file_size_mb,
            sandbox=cfg.sandbox,
        )
    else:
        from deepagents.backends.filesystem import FilesystemBackend

        workspace_backend = FilesystemBackend(
            root_dir=str(workspace_host),
            virtual_mode=cfg.virtual_mode,
            max_file_size_mb=cfg.max_file_size_mb,
        )
    return CompositeBackend(
        default=workspace_backend,
        routes={_WORKSPACE_ROUTE: workspace_backend},
    )


def workspace_skills_source() -> str:
    return f"{_WORKSPACE_ROUTE}.skills/"
