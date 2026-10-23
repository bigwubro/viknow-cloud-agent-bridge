"""Run-scoped library_id resolution (viknow2 ToolRuntime parity).

viknow2 handlers use ``str(args.get("library_id") or self.default_library_id())``
then ``is_library_allowed``. We keep the same split:

- ``knowledge_discover`` / ``query_refine``: no ``library_id`` tool arg; scope from grant.
- Search / plan / inspect / segments: optional ``library_id`` on the tool call; empty falls back
  to configured default, or the sole granted library when the run authorizes exactly one.
- Harness middleware (later) validates scope; it does not inject args into tool JSON.
"""

from __future__ import annotations

from viknow3.application.agent.qa_expert.grant import current_grant
from viknow3.application.hyperrag.config import load_app_config


def granted_library_ids() -> list[str]:
    grant = current_grant()
    return [] if grant is None else [str(item) for item in grant.libraries()]


def configured_default_library_id() -> str:
    return str(load_app_config().knowledge.default_library_id)


def resolve_library_id(provided: str | None = None) -> str:
    explicit = (provided or "").strip()
    if explicit:
        return explicit
    granted = granted_library_ids()
    if len(granted) == 1:
        return granted[0]
    return configured_default_library_id()


def library_allowed(library_id: str) -> bool:
    granted = granted_library_ids()
    if not granted:
        return False
    return library_id in granted
