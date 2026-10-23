"""Agent Service runs a turn through the inbound gateway, not inside the access layer."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

os.environ.setdefault("VIKNOW_ACCESS_DSN", "postgresql://test:test@127.0.0.1:5432/access")
os.environ.setdefault("VIKNOW_AGENT_DSN", "postgresql://test:test@127.0.0.1:5432/agent")
os.environ.setdefault("VIKNOW_HYPERRAG_DSN", "postgresql://test:test@127.0.0.1:5432/hyperrag")
os.environ.setdefault("VIKNOW_USE_SCRIPT_MODEL", "1")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from viknow3.application.agentcore.client import AgentClient
from viknow3.application.agentcore.runtime.host import session_messages
from viknow3.application.agentcore.runtime.langgraph_runner import unanswered_tool_calls
from viknow3.application.agentcore.runtime.loop import LOOP

try:
    import jieba  # noqa: F401
    import scheduler_core  # noqa: F401

    HYPERRAG_RUNTIME = True
except ImportError:
    HYPERRAG_RUNTIME = False


class _ScriptHyperrag:
    """Stand-in so script-model runs exercise the client call without booting the engine."""

    def reset(self) -> None:
        return None

    def discover(self, query: str, *, library_ids: list[str], top_k: int | None = None) -> dict:
        libraries = [
            {"library_id": library_id, "title": library_id, "summary": "", "top_files": [], "score": 1.0}
            for library_id in library_ids
        ]
        if top_k is not None:
            libraries = libraries[:top_k]
        return {"query": query or "", "libraries": libraries, "source": "hyperrag"}

    def search(self, library_id: str, query: str, keywords, *, top_k=None, threshold=None) -> dict:
        del keywords, top_k, threshold
        return {
            "library_id": library_id,
            "query": query,
            "evidence": [
                {
                    "id": "ev-1",
                    "evidence_id": "ev-1",
                    "snippet": query,
                    "parts": [{"kind": "text", "text": query}],
                    "asset_id": "asset-1",
                    "modality": "document",
                }
            ],
            "source": "hyperrag",
        }

    def list_segments(self, library_id: str, asset_id: str, **kwargs) -> dict:
        return {
            "library_id": library_id,
            "asset_id": asset_id,
            "mode": kwargs.get("mode"),
            "segments": [],
            "source": "hyperrag",
        }

    def search_by_image(self, library_id: str, image_data_url: str, query: str = "", **kwargs) -> dict:
        del image_data_url, kwargs
        return self.search(library_id, query, None)


_REAL_HYPERAG = None


def setUpModule() -> None:
    from viknow3.application.agentcore.runtime.checkpoint import reset_agent_tables
    from viknow3.application.hyperrag import client as hyper_client
    from viknow3.store import STORE

    global _REAL_HYPERAG
    _REAL_HYPERAG = hyper_client.HYPERAG
    stub = _ScriptHyperrag()
    hyper_client.HYPERAG = stub
    STORE.reset()
    stub.reset()
    reset_agent_tables()


def tearDownModule() -> None:
    from viknow3.application.hyperrag import client as hyper_client

    global _REAL_HYPERAG
    if _REAL_HYPERAG is not None:
        hyper_client.HYPERAG = _REAL_HYPERAG


def _submit(client: AgentClient, session_id: str, user_id: str, profile_id: str, message: str, library_ids: list[str]) -> dict:
    return client.submit(
        session_id=session_id,
        user_id=user_id,
        profile_id=profile_id,
        message=message,
        model=None,
        file_ids=[],
        library_ids=library_ids,
        attachments=[],
    )


def _finish(client: AgentClient, run_id: str) -> list[dict]:
    return list(client.stream_events(run_id, 0))


class AgentServiceTests(unittest.TestCase):
    def test_stream_events_yields_until_completed(self) -> None:
        client = AgentClient()
        run = _submit(client, "session-stream", "user-stream", "deepagents", "流式", ["library-1"])
        types = [item["type"] for item in client.stream_events(run["id"], 0)]
        self.assertEqual(types[:3], ["run_started", "user_message", "model_call_started"])
        self.assertIn("message_delta", types)
        self.assertEqual(types[-2:], ["message_completed", "run_completed"])
        self.assertEqual([item["sequence"] for item in client.events(run["id"], 0)], list(range(1, len(types) + 1)))

    def test_deepagents_runs_tools_on_the_granted_library(self) -> None:
        client = AgentClient()
        run = _submit(client, "session-a", "user-a", "deepagents", "你好", ["library-1"])
        events = _finish(client, run["id"])
        types = [item["type"] for item in events]
        self.assertEqual(events[0]["payload"]["library_ids"], ["library-1"])
        started = [item["payload"] for item in events if item["type"] == "tool_call_started"]
        self.assertEqual(
            [item["name"] for item in started],
            ["read_file", "knowledge_discover", "query_refine", "knowledge_search"],
        )
        completed = [item["payload"] for item in events if item["type"] == "tool_call_completed"]
        self.assertEqual([item["tool_call_id"] for item in completed], [item["tool_call_id"] for item in started])
        discover = json.loads(completed[0]["text"])
        self.assertEqual([item["library_id"] for item in discover["libraries"]], ["library-1"])
        self.assertEqual(types[-1], "run_completed")
        output = client.get(run["id"])["output"]
        self.assertIn("答案", output)
        self.assertIn("你好", output)

    def test_consecutive_runs_share_one_loop(self) -> None:
        client = AgentClient()
        for index in range(4):
            run = _submit(client, f"session-seq-{index}", "user-seq", "deepagents", f"第{index}问", ["library-1"])
            types = [item["type"] for item in _finish(client, run["id"])]
            self.assertEqual(types[-1], "run_completed", types)
            self.assertEqual(client.get(run["id"])["status"], "completed")

    def test_concurrent_runs_keep_their_own_libraries(self) -> None:
        client = AgentClient()
        runs = [
            _submit(client, f"session-par-{index}", f"user-par-{index}", "deepagents", f"并发{index}", [f"library-par-{index}"])
            for index in range(6)
        ]
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda run: _finish(client, run["id"]), runs))
        for index, events in enumerate(results):
            self.assertEqual(events[-1]["type"], "run_completed")
            discover = next(item for item in events if item["type"] == "tool_call_completed")
            libraries = [item["library_id"] for item in json.loads(discover["payload"]["text"])["libraries"]]
            self.assertEqual(libraries, [f"library-par-{index}"])
            self.assertIn(f"并发{index}", client.get(runs[index]["id"])["output"])
        self.assertFalse(client.permission("user-par-0", "library-par-1", None))

    def test_same_session_keeps_earlier_turns(self) -> None:
        client = AgentClient()
        first = _submit(client, "session-history", "user-history", "deepagents", "你好", ["library-1"])
        _finish(client, first["id"])
        second = _submit(client, "session-history", "user-history", "deepagents", "下一句", ["library-1"])
        _finish(client, second["id"])
        self.assertIn("下一句", client.get(second["id"])["output"])
        humans = [m.content for m in session_messages("deepagents", "session-history") if isinstance(m, HumanMessage)]
        self.assertEqual(humans, ["你好", "下一句"])
        self.assertEqual(session_messages("deepagents", "session-other"), [])

    def test_interrupt_cancels_an_active_search(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def block(library_id: str, query: str, **kwargs: object) -> str:
            del library_id, query, kwargs
            started.set()
            release.wait(3)
            return '{"error":"permission_denied"}'

        client = AgentClient()
        with patch("viknow3.application.agent.qa_box.tools.knowledge_search", block):
            run = _submit(client, "session-cancel", "user-cancel", "deepagents", "清洁燃气气孔是为了防止什么", ["library-1"])
            reader = ThreadPoolExecutor(max_workers=1).submit(_finish, client, run["id"])
            self.assertTrue(started.wait(5))
            client.interrupt(run["id"])
            types = [item["type"] for item in reader.result(3)]
            release.set()
        self.assertEqual(types[-1], "run_interrupted")
        self.assertNotIn("run_completed", types)
        self.assertEqual(client.get(run["id"])["status"], "interrupted")

        with patch(
            "viknow3.application.agent.qa_box.tools.knowledge_search",
            lambda library_id, query, **kwargs: json.dumps(
                {"library_id": library_id, "query": query, "evidence": [], "source": "hyperrag"}
            ),
        ):
            again = _submit(client, "session-cancel", "user-cancel", "deepagents", "清洁燃气气孔是为了防止什么", ["library-1"])
            self.assertEqual(_finish(client, again["id"])[-1]["type"], "run_completed")
        history = session_messages("deepagents", "session-cancel")
        self.assertEqual(unanswered_tool_calls(history), [])

    def test_interrupt_rejects_a_finished_run(self) -> None:
        from viknow3.application.agentcore.errors import AgentServiceError

        client = AgentClient()
        run = _submit(client, "session-done", "user-done", "deepagents", "完成", ["library-1"])
        _finish(client, run["id"])
        with self.assertRaises(AgentServiceError) as caught:
            client.interrupt(run["id"])
        self.assertEqual(caught.exception.status, 409)

    def test_a_run_closes_only_once(self) -> None:
        from viknow3.application.agentcore.environment.context import RunContext, RunState
        from viknow3.application.agentcore.events.sink import EventSink
        from viknow3.application.agentcore.runtime.events import RunEvents
        from viknow3.clock import now

        moment = now()
        ctx = RunContext("p", "u", "s", "run-once", "hi", None, [], [], [])
        state = RunState(ctx, "running", None, {}, None, moment, moment)
        sink = EventSink()
        events = RunEvents(state, sink)
        events.started()
        events.interrupted("partial")
        events.emit("message_delta", {"text": "late"})
        events.completed("full", None)
        events.failed("late")
        self.assertEqual([item["type"] for item in sink.read("run-once", 0)], ["run_started", "user_message", "run_interrupted"])
        self.assertEqual((state.status, state.output), ("interrupted", "partial"))

    def test_unanswered_tool_calls_are_closed(self) -> None:
        history = [
            HumanMessage(content="q"),
            AIMessage(content="", tool_calls=[{"id": "a", "name": "t", "args": {}}, {"id": "b", "name": "t", "args": {}}]),
            ToolMessage(content="ok", tool_call_id="a"),
        ]
        closing = unanswered_tool_calls(history)
        self.assertEqual([item.tool_call_id for item in closing], ["b"])

    def test_unknown_runtime_fails_the_run(self) -> None:
        from viknow3.application.agentcore.registry.catalog import _profile
        from viknow3.clock import now

        client = AgentClient()
        client._inbound.registry._profiles["ghost"] = _profile("ghost", "ghost", "", now())
        run = _submit(client, "session-ghost", "user-ghost", "ghost", "hi", [])
        events = _finish(client, run["id"])
        self.assertEqual(events[-1]["type"], "run_failed")
        self.assertEqual(events[-1]["payload"]["code"], "unknown_profile")

    def test_server_default_is_the_first_model_option(self) -> None:
        listed = AgentClient().models()
        self.assertEqual(listed["default_model"], "server-default")
        self.assertEqual(listed["models"][0]["id"], "server-default")
        self.assertTrue(listed["models"][0]["label"].startswith("服务端默认"))

    def test_agent_loop_survives_a_failed_task(self) -> None:
        async def boom() -> None:
            raise ValueError("boom")

        async def fine() -> int:
            await asyncio.sleep(0)
            return 7

        with self.assertRaises(ValueError):
            LOOP.call(boom())
        self.assertEqual(LOOP.call(fine()), 7)


if __name__ == "__main__":
    unittest.main()
