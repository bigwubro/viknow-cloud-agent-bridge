"""Composite deepagents backend: skills tree + per-session workspace."""

from __future__ import annotations

from pathlib import Path

from deepagents.backends.composite import CompositeBackend
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.protocol import BackendProtocol

from viknow3.application.agent.knowledge_assistant.bubblewrap_backend import BubblewrapBackend
from viknow3.application.agent.knowledge_assistant.session_workspace import ensure_session_workspace
from viknow3.application.agent.knowledge_assistant.workspace_config import load_deepagents_workspace_config

_SKILLS_ROOT = Path(__file__).resolve().parent / "skills"


def build_deepagents_backend(session_id: str) -> BackendProtocol:
    cfg = load_deepagents_workspace_config()
    skills_backend = FilesystemBackend(
        root_dir=str(_SKILLS_ROOT),
        virtual_mode=True,
        max_file_size_mb=cfg.max_file_size_mb,
    )
    workspace_host = ensure_session_workspace(session_id)
    backend_name = cfg.backend
    if backend_name == "bubblewrap":
        workspace_backend: BackendProtocol = BubblewrapBackend(
            root_dir=workspace_host,
            virtual_mode=cfg.virtual_mode,
            max_file_size_mb=cfg.max_file_size_mb,
            sandbox=cfg.sandbox,
        )
    else:
        workspace_backend = FilesystemBackend(
            root_dir=workspace_host,
            virtual_mode=cfg.virtual_mode,
            max_file_size_mb=cfg.max_file_size_mb,
        )
    return CompositeBackend(
        default=skills_backend,
        routes={"/workspace/": workspace_backend},
    )
