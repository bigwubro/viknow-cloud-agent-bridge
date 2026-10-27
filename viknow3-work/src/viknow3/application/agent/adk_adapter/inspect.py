"""ADK vision tools. The functions live on the ADK agent; this module runs them."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_HANDLERS: Any = None


def shared_handlers(knowledge: Any) -> Any:
    global _HANDLERS
    if _HANDLERS is None:
        _HANDLERS = _build_handlers(knowledge)
    return _HANDLERS


def dispatch_inspect(
    handlers: Any,
    name: str,
    args: dict[str, Any],
    *,
    library_ids: tuple[str, ...],
    question: str,
    user_id: str,
    workspace_id: str,
    session_id: str,
) -> str:
    from agents.runtimes.deepagents.core.tools.context import (
        ToolRunContext,
        reset_tool_run_context,
        set_tool_run_context,
        with_run_question,
    )

    method_name = "locate_anything" if name == "visual_ground" else name
    token = set_tool_run_context(
        ToolRunContext(
            run_id=session_id or "adk",
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
            allowed_library_ids=frozenset(library_ids),
            original_question=question,
        )
    )
    try:
        method = getattr(handlers, method_name)
        return method(with_run_question(method_name, dict(args)))
    finally:
        reset_tool_run_context(token)


class RuntimeInspect:
    """Callable the ADK tool functions use for one turn."""

    def __init__(
        self,
        knowledge: Any,
        *,
        library_ids: tuple[str, ...],
        question: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
    ) -> None:
        self._knowledge = knowledge
        self._library_ids = library_ids
        self._question = question
        self._user_id = user_id
        self._workspace_id = workspace_id
        self._session_id = session_id

    def __call__(self, name: str, args: dict[str, Any]) -> str:
        try:
            handlers = shared_handlers(self._knowledge)
        except Exception as exc:
            log.exception("adk inspect backend failed to start")
            return json.dumps(
                {"tool": name, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        return dispatch_inspect(
            handlers,
            name,
            args,
            library_ids=self._library_ids,
            question=self._question,
            user_id=self._user_id,
            workspace_id=self._workspace_id,
            session_id=self._session_id,
        )


def _build_handlers(knowledge: Any) -> Any:
    from agents.layout import package_for_public_id
    from agents.runtimes.deepagents.core.tools import ViKnowToolHandlers
    from viknow3.application.agent.deepagents_adapter.vendor.host_config import (
        build_agent_app_config,
        review_tool_configs,
    )

    configured = os.environ.get("VIKNOW3_ROOT", "").strip()
    root = Path(configured).resolve() if configured else Path(__file__).resolve().parents[5]
    package = package_for_public_id("expertbuddy-adk")
    if package is None:
        raise RuntimeError("expertbuddy-adk profile is not installed")
    files_root = Path(
        os.environ.get("VIKNOW_VENDOR_FILES_ROOT", str(root / ".state" / "vendor-files"))
    ).resolve()
    config = build_agent_app_config(
        skills_dir=package.skills_dir,
        profile_dir=package.profile_path.parent,
        agent_ids=(package.profile_id,),
        files_root=files_root,
        static_dir=root / "ui",
    )
    actio_client = None
    try:
        from viknow3.application.actio.client import get_actio_client

        actio_client = get_actio_client()
    except Exception:
        log.warning("Actio client unavailable for ADK inspect", exc_info=True)
    try:
        from viknow3.application.hyperrag.runtime import install_actio_port

        install_actio_port()
    except Exception:
        log.warning("Actio indexing port was not installed for ADK inspect", exc_info=True)
    return ViKnowToolHandlers(
        repo_root=root,
        knowledge_adapter=knowledge,
        actio_client=actio_client,
        tool_configs=review_tool_configs(config),
    )
