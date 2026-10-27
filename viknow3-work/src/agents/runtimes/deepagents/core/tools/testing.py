"""Testing tools used by local runtime verification."""

from __future__ import annotations

from typing import Any

from agents.runtimes.deepagents.core.tools.base import ToolErrorCode, ToolResult

TOOL_SCHEMA = {
    "description": "Return a deterministic ViKnow test answer for a user question.",
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to answer.",
            }
        },
        "required": ["question"],
        "additionalProperties": False,
    },
}


def viknow_test_answer(args: dict[str, Any], **_: Any) -> str:
    """Return a JSON payload so the agent can prove tool execution."""

    question = str(args.get("question", "")).strip()
    if not question:
        return ToolResult.fail(
            "viknow_test_answer",
            ToolErrorCode.INVALID_ARGUMENTS,
            "question is required",
        ).to_json()
    return ToolResult.ok(
        "viknow_test_answer",
        {
            "answer": f"ViKnow test plugin received: {question}",
            "source": "viknow-test",
        },
    ).to_json()
