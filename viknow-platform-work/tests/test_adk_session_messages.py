"""ADK events → LangChain messages (session_messages parity)."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("VIKNOW_USE_SCRIPT_MODEL", "1")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from viknow_platform.application.agent.knowledge_assistant.adk_session_messages import events_to_langchain_messages


def _part(**kwargs):
    return SimpleNamespace(**kwargs)


def _event(author: str, parts: list) -> SimpleNamespace:
    return SimpleNamespace(author=author, content=SimpleNamespace(role=author, parts=parts))


class AdkSessionMessagesTests(unittest.TestCase):
    def test_user_and_assistant_text(self) -> None:
        events = [
            _event("user", [_part(text="你好")]),
            _event("viknow_platform_adk", [_part(text="你好，有什么可以帮您？")]),
        ]
        messages = events_to_langchain_messages(events)
        self.assertEqual(len(messages), 2)
        self.assertIsInstance(messages[0], HumanMessage)
        self.assertEqual(messages[0].content, "你好")
        self.assertIsInstance(messages[1], AIMessage)

    def test_tool_round_trip(self) -> None:
        call = SimpleNamespace(id="c1", name="knowledge_search", args={"query": "x"})
        response = SimpleNamespace(id="c1", name="knowledge_search", response={"ok": True})
        events = [
            _event("user", [_part(text="搜一下")]),
            _event("viknow_platform_adk", [_part(function_call=call)]),
            _event("viknow_platform_adk", [_part(function_response=response)]),
            _event("viknow_platform_adk", [_part(text="答案")]),
        ]
        messages = events_to_langchain_messages(events)
        self.assertTrue(any(isinstance(m, ToolMessage) for m in messages))
        self.assertTrue(any(isinstance(m, AIMessage) and getattr(m, "tool_calls", None) for m in messages))


if __name__ == "__main__":
    unittest.main()
