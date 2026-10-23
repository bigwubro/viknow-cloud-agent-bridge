"""Deepagents filesystem workspace backend (non-bubblewrap)."""

from __future__ import annotations

from deepagents.backends.composite import CompositeBackend
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.protocol import BackendProtocol

from viknow3.application.agent.knowledge_assistant.session_workspace import ensure_session_workspace
from viknow3.application.agent.knowledge_assistant.workspace_config import load_deepagents_workspace_config

_WORKSPACE_ROUTE = "/workspace/"


def build_filesystem_workspace_backend(session_id: str) -> BackendProtocol:
    cfg = load_deepagents_workspace_config()
    workspace_host = ensure_session_workspace(session_id)
    workspace_backend = FilesystemBackend(
        root_dir=str(workspace_host),
        virtual_mode=cfg.virtual_mode,
        max_file_size_mb=cfg.max_file_size_mb,
    )
    return CompositeBackend(
        default=workspace_backend,
        routes={_WORKSPACE_ROUTE: workspace_backend},
    )
