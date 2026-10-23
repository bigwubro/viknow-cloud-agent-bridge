"""Knowledge search plugin backed by the configured ViKnow knowledge backend."""

from __future__ import annotations

from typing import Any

from viknow.core.config import load_app_config
from viknow.core.knowledge.hyperrag_adapter import HyperRAGAdapter
from viknow.core.tools import DISCOVER_SCHEMA, SEARCH_SCHEMA, ViKnowToolHandlers

_knowledge_adapter: HyperRAGAdapter | None = None
_knowledge_adapter_init_failed = False


def get_knowledge_adapter() -> HyperRAGAdapter | None:
    global _knowledge_adapter, _knowledge_adapter_init_failed
    if _knowledge_adapter is not None:
        return _knowledge_adapter
    if _knowledge_adapter_init_failed:
        return None

    try:
        config = load_app_config()
        if config.knowledge is None:
            _knowledge_adapter_init_failed = True
            return None
        _knowledge_adapter = HyperRAGAdapter(config)
        return _knowledge_adapter
    except Exception:
        _knowledge_adapter_init_failed = True
        return None


def _build_handlers() -> ViKnowToolHandlers:
    adapter = get_knowledge_adapter()
    return ViKnowToolHandlers(knowledge_adapter=adapter)


def knowledge_search(args: dict[str, Any], **_: Any) -> str:
    return _build_handlers().knowledge_search(args)


def knowledge_discover(args: dict[str, Any], **_: Any) -> str:
    return _build_handlers().knowledge_discover(args)


def register(ctx) -> None:
    ctx.register_tool(
        name="knowledge_discover",
        toolset="viknow_search",
        schema=DISCOVER_SCHEMA,
        handler=knowledge_discover,
        description="List or search ViKnow knowledge libraries.",
    )
    ctx.register_tool(
        name="knowledge_search",
        toolset="viknow_search",
        schema=SEARCH_SCHEMA,
        handler=knowledge_search,
        description="Search the ViKnow knowledge library and return unified evidence.",
    )
