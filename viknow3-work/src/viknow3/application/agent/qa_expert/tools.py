"""Tools this agent is willing to call."""

from __future__ import annotations

from langchain_core.tools import tool

from viknow3.application.agent.qa_expert.grant import current_grant
from viknow3.application.agent.qa_expert.pipeline import retrieve


@tool
def knowledge_search(query: str) -> str:
    """Search the manuals granted to this run and return one passage."""
    grant = current_grant()
    libraries = [] if grant is None else [str(item) for item in grant.libraries()]
    return retrieve(query, libraries)
