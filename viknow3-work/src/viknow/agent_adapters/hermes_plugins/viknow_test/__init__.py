"""Minimal ViKnow test plugin for Hermes Agent."""

from __future__ import annotations

from viknow.core.tools import TOOL_SCHEMA, viknow_test_answer


def register(ctx) -> None:
    ctx.register_tool(
        name="viknow_test_answer",
        toolset="viknow_test",
        schema=TOOL_SCHEMA,
        handler=viknow_test_answer,
        description="Minimal ViKnow QA verification tool.",
    )
