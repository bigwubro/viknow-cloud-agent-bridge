"""Tool result envelopes aligned with viknow2 BusinessStructuredTool / tool_gate."""

from __future__ import annotations

import json
from typing import Any


def tool_gate_blocked(tool: str, rule: str, message: str, recovery: str) -> str:
    payload: dict[str, Any] = {
        "tool": tool,
        "status": "error",
        "code": "tool_gate_blocked",
        "message": message,
        "data": {"tool": tool, "rule": rule},
        "tool_gate": {
            "code": rule,
            "recovery": recovery,
            "message": message,
        },
    }
    return json.dumps(payload, ensure_ascii=False)


def ok_tool(tool: str, data: dict[str, Any]) -> str:
    body = {"tool": tool, "status": "ok", "data": data, **data}
    return json.dumps(body, ensure_ascii=False)
