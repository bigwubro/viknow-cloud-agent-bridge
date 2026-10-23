"""Playground convention: LangGraph thread_id equals viknow3 session_id."""

from __future__ import annotations


def apply_session_thread_id_patch() -> None:
    import viknow.agent_adapters.deepagents as deepagents_module

    deepagents_module._thread_id = (  # noqa: SLF001 — boundary wiring
        lambda _user_id, _workspace_id, session_id: session_id
    )
