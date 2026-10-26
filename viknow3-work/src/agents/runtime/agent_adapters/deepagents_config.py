"""DeepAgents kernel configuration helpers."""

from __future__ import annotations

from agents.runtime.core.config import AppConfig, DeepAgentsKernelConfig, resolve_sessions_root


def resolve_deepagents_config(config: AppConfig) -> DeepAgentsKernelConfig:
    """Return DeepAgents config with workspace/state_root defaults applied.

    When ``app.files_root`` is set it owns the session file tree, so
    ``workspace.root_dir`` always resolves to ``{files_root}/sessions``.
    Checkpointer backend and paths come solely from ``AppConfig.persistence``
    (resolved in ``load_app_config``).
    """

    deepagents_config = config.agent.deepagents
    state_root = deepagents_config.state_root or config.app.state_dir / "deepagents"
    workspace = deepagents_config.workspace
    if config.app.files_root is not None:
        workspace = workspace.model_copy(
            update={"root_dir": resolve_sessions_root(config.app.files_root)}
        )
    elif workspace.root_dir is None:
        workspace = workspace.model_copy(update={"root_dir": state_root / "workspaces"})
    return deepagents_config.model_copy(
        update={
            "state_root": state_root,
            "workspace": workspace,
        }
    )
