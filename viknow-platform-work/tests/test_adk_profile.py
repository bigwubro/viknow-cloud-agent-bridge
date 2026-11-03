"""One viknow-v2 profile, two assemblers. ADK event projection stays free of the SDK."""

from __future__ import annotations

import importlib.util
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("VIKNOW_USE_SCRIPT_MODEL", "1")

from viknow_platform.application.agent.knowledge_assistant.adk_events import AdkTurn, project_adk_event
from viknow_platform.application.agent.knowledge_assistant.instructions import runtime_instruction
from viknow_platform.application.agent.knowledge_assistant.profile import load_viknow_v2_profile
from viknow_platform.application.agentcore.registry.catalog import Registry


class _Events:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    def emit(self, event_type: str, payload: dict | None = None) -> None:
        self.rows.append((event_type, payload or {}))


def _part(text: str | None = None, *, call=None, response=None):
    return SimpleNamespace(text=text, function_call=call, function_response=response)


def _event(*parts, partial: bool = False, usage=None):
    return SimpleNamespace(partial=partial, usage_metadata=usage, content=SimpleNamespace(parts=list(parts)))


class AdkProfileTests(unittest.TestCase):
    def test_assemblers_share_the_profile_and_add_their_own_skill_step(self) -> None:
        profile = load_viknow_v2_profile()
        deep = runtime_instruction(profile, "deepagents")
        adk = runtime_instruction(profile, "adk")
        shared = profile.system_prompt.strip()
        self.assertIn(shared, deep)
        self.assertIn(shared, adk)
        self.assertIn("read_file", deep)
        self.assertIn("write_todos", deep)
        self.assertNotIn("load_skill", deep)
        self.assertIn("list_skills", adk)
        self.assertIn("load_skill", adk)
        self.assertIn("load_skill_resource", adk)

    def test_registry_pairs_the_same_tools_with_two_runtimes(self) -> None:
        reg = Registry()
        deep = reg.profile("deepagents")
        adk = reg.profile("adk")
        assert deep is not None and adk is not None
        self.assertEqual(deep.runtime, "langgraph")
        self.assertEqual(adk.runtime, "adk")
        self.assertEqual(adk.tools, deep.tools)
        self.assertEqual(adk.tools, load_viknow_v2_profile().enabled_tools)

    def test_partial_text_is_not_repeated_when_the_final_event_echoes_it(self) -> None:
        events = _Events()
        turn = AdkTurn()
        project_adk_event(events, turn, _event(_part("你"), partial=True))
        project_adk_event(events, turn, _event(_part("好"), partial=True))
        project_adk_event(
            events,
            turn,
            _event(
                _part("你好"),
                usage=SimpleNamespace(prompt_token_count=3, candidates_token_count=2, total_token_count=5),
            ),
        )
        deltas = [payload["text"] for kind, payload in events.rows if kind == "message_delta"]
        self.assertEqual(deltas, ["你", "好"])
        self.assertEqual(turn.text, "你好")
        self.assertEqual(turn.usage["total_tokens"], 5)
        self.assertEqual([kind for kind, _ in events.rows if kind == "model_call_started"], ["model_call_started"])

    def test_tool_round_starts_another_model_call(self) -> None:
        events = _Events()
        turn = AdkTurn()
        project_adk_event(
            events,
            turn,
            _event(
                _part(
                    call=SimpleNamespace(id="c1", name="knowledge_search", args={"query": "手册"}),
                )
            ),
        )
        project_adk_event(
            events,
            turn,
            _event(
                _part(
                    response=SimpleNamespace(id="c1", name="knowledge_search", response={"text": "候选"}),
                )
            ),
        )
        project_adk_event(events, turn, _event(_part("依据候选。")))
        kinds = [kind for kind, _ in events.rows]
        self.assertEqual(
            kinds,
            ["model_call_started", "tool_call_started", "tool_call_completed", "model_call_started", "message_delta"],
        )
        self.assertEqual(events.rows[1][1]["arguments"], {"query": "手册"})
        self.assertEqual(turn.text, "依据候选。")

    def test_llm_agent_carries_profile_tools_and_skill_toolset(self) -> None:
        if importlib.util.find_spec("google.adk") is None:
            self.skipTest("google-adk is not installed")
        from viknow_platform.application.agent.knowledge_assistant.adk_builder import agent_tool_names, build_llm_agent

        with patch(
            "viknow_platform.application.agent.knowledge_assistant.adk_builder.litellm_model",
            return_value="gemini-2.0-flash",
        ):
            agent = build_llm_agent(None)
        names = agent_tool_names(agent)
        profile = load_viknow_v2_profile()
        self.assertTrue(set(profile.enabled_tools) <= names)
        self.assertIn("list_skills", names)
        self.assertIn("load_skill", names)
        self.assertIn("load_skill_resource", names)
        self.assertIn("load_skill", agent.instruction)

    def test_adk_session_tables_use_their_own_schema(self) -> None:
        if importlib.util.find_spec("google.adk") is None:
            self.skipTest("google-adk is not installed")
        if not os.environ.get("VIKNOW_AGENT_DSN"):
            self.skipTest("agent database is not configured")
        from viknow_platform.application.agent.knowledge_assistant.adk_session import delete_session, ensure_session
        from viknow_platform.application.agentcore.runtime.loop import LOOP
        from viknow_platform.pg import AGENT, connect, require_test_database

        require_test_database(AGENT)
        user_id = "adk-schema-user"
        session_id = "adk-schema-session"
        LOOP.call(ensure_session(user_id, session_id), timeout=30)
        try:
            with connect(AGENT) as conn:
                tables = conn.execute(
                    """
                    SELECT table_schema FROM information_schema.tables
                    WHERE table_name IN ('sessions', 'events', 'app_states', 'user_states', 'adk_internal_metadata')
                    """
                ).fetchall()
                stored = conn.execute(
                    "SELECT id FROM adk.sessions WHERE id = %s AND user_id = %s",
                    (session_id, user_id),
                ).fetchall()
            self.assertIn("adk", {row[0] for row in tables})
            self.assertNotIn("public", {row[0] for row in tables})
            self.assertEqual([row[0] for row in stored], [session_id])
        finally:
            LOOP.call(delete_session(user_id, session_id), timeout=30)


if __name__ == "__main__":
    unittest.main()
