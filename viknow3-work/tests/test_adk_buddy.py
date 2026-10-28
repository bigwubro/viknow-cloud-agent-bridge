"""ADK profile, tools, and shell routing stay off the DeepAgents loop."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from viknow3.agents.runtimes.adk.instruction import build_instruction, load_profile
from viknow3.agents.runtimes.adk.knowledge_tools import CitationLog, build_knowledge_tools
from viknow3.agents.runtimes.adk.notices import AdkNotice, notices_from_adk_event
from viknow3.agents.runtimes.adk.session_url import async_postgres_url


class AdkProfileTests(unittest.TestCase):
    def test_profile_is_separate_and_names_its_own_runtime(self) -> None:
        data = load_profile()
        self.assertEqual(data["id"], "expertbuddy-adk")
        self.assertEqual(data["runtime"], "adk")
        self.assertEqual(
            data["enabled_tools"],
            [
                "knowledge_discover",
                "uploaded_image_search",
                "answer_plan",
                "knowledge_search",
                "video_inspect",
                "visual_ground",
                "document_inspect",
                "list_video_segments",
                "list_doc_segments",
            ],
        )
        self.assertEqual(data["enabled_skills"], ["knowledge-qa-v2", "visual-ground"])
        instruction = build_instruction(("e2e-artifacts-20261022",))
        self.assertIn("ViKnow Knowledge QA v2", instruction)
        self.assertIn("final_check()", instruction)
        self.assertNotIn("只引用 chunk_text", instruction)
        self.assertNotIn("{{qa_final_answer_format_examples}}", instruction)
        self.assertIn("e2e-artifacts-20261022", instruction)

    def test_postgres_url_uses_the_async_driver(self) -> None:
        self.assertEqual(
            async_postgres_url("postgresql://user:secret@db/agent"),
            "postgresql+psycopg://user:secret@db/agent",
        )
        self.assertEqual(
            async_postgres_url("postgresql+asyncpg://user@db/agent"),
            "postgresql+asyncpg://user@db/agent",
        )

    def test_search_refuses_a_library_outside_the_grant(self) -> None:
        client = _FakeKnowledge()
        citations = CitationLog()
        tools = {tool.__name__: tool for tool in build_knowledge_tools(client, ("lib-a",), citations)}
        refused = asyncio.run(tools["knowledge_search"]("phrase", "lib-b"))
        self.assertIn("not granted", refused)
        self.assertIsNone(client.searched)

    def test_search_quotes_evidence_from_the_granted_library(self) -> None:
        client = _FakeKnowledge()
        citations = CitationLog()
        tools = {tool.__name__: tool for tool in build_knowledge_tools(client, ("lib-a",), citations)}
        payload = json.loads(asyncio.run(tools["knowledge_search"]("验收", "")))
        self.assertEqual(client.searched, "lib-a")
        hit = payload["evidence"][0]
        self.assertEqual(hit["chunk_text"], "ViKnow 20261022")
        self.assertNotIn("index_note", hit)
        self.assertNotIn("稳定性", json.dumps(hit, ensure_ascii=False))
        self.assertEqual(citations.rows[0]["id"], "ev-1")

    def test_segments_split_document_and_video(self) -> None:
        client = _FakeKnowledge()
        citations = CitationLog()
        tools = {tool.__name__: tool for tool in build_knowledge_tools(client, ("lib-a",), citations)}
        docs = json.loads(asyncio.run(tools["list_doc_segments"]("lib-a", "asset-doc")))
        videos = json.loads(asyncio.run(tools["list_video_segments"]("lib-a", "asset-doc")))
        self.assertEqual(docs["document_segments"][0]["chunk_text"], "doc body")
        self.assertNotIn("index_note", docs["document_segments"][0])
        self.assertEqual(videos["video_segments"][0]["chunk_text"], "ViKnow")

    def test_discover_labels_the_indexer_summary(self) -> None:
        client = _FakeKnowledge()
        tools = {tool.__name__: tool for tool in build_knowledge_tools(client, ("lib-a",), CitationLog())}
        payload = json.loads(asyncio.run(tools["knowledge_discover"]("验收")))
        asset = payload["libraries"][0]["assets"][0]
        self.assertEqual(asset["title"], "index-smoke-20261022.md")
        self.assertNotIn("index_note", asset)
        self.assertNotIn("summary", asset)
        self.assertNotIn("稳定性", json.dumps(asset, ensure_ascii=False))

    def test_inspect_tools_call_the_adk_backend_inside_the_grant(self) -> None:
        calls: list[tuple[str, dict]] = []

        def inspect(name: str, args: dict) -> str:
            calls.append((name, args))
            return json.dumps({"tool": name, "result": {"claims": [{"text": "走"}]}}, ensure_ascii=False)

        tools = {
            tool.__name__: tool
            for tool in build_knowledge_tools(
                _FakeKnowledge(),
                ("lib-a",),
                CitationLog(),
                question="视频第三秒的内容是什么",
                inspect=inspect,
            )
        }
        video = json.loads(
            asyncio.run(
                tools["video_inspect"](
                    "lib-a",
                    "asset-1",
                    {"start_seconds": 2, "end_seconds": 4},
                    "第三秒有什么",
                    "ev-1",
                )
            )
        )
        self.assertEqual(video["result"]["claims"][0]["text"], "走")
        self.assertEqual(calls[0][0], "video_inspect")
        self.assertEqual(calls[0][1]["time_range"], {"start_seconds": 2, "end_seconds": 4})
        refused = asyncio.run(
            tools["video_inspect"]("lib-b", "asset-1", {"start_seconds": 2, "end_seconds": 4}, "第三秒有什么")
        )
        self.assertIn("not granted", refused)
        asyncio.run(
            tools["visual_ground"](
                "图中有几个人？",
                library_id="lib-a",
                asset_id="asset-1",
                frame_time=3,
            )
        )
        asyncio.run(
            tools["document_inspect"]("lib-a", "asset-doc", "ev-doc", "这一页写了什么")
        )
        asyncio.run(tools["uploaded_image_search"]("uploads/a.png", "图里有什么", "lib-a"))
        self.assertEqual(
            [name for name, _args in calls],
            ["video_inspect", "visual_ground", "document_inspect", "uploaded_image_search"],
        )
        self.assertEqual(calls[1][1]["frame_time"], 3)
        self.assertEqual(len(calls), 4)

    def test_event_notice_keeps_the_tool_name(self) -> None:
        event = SimpleNamespace(
            partial=False,
            error_message=None,
            content=SimpleNamespace(
                parts=[
                    SimpleNamespace(
                        function_call=SimpleNamespace(id="c1", name="knowledge_search", args={"query": "验收"}),
                        function_response=None,
                        text=None,
                        thought=False,
                    )
                ]
            ),
        )
        notices = notices_from_adk_event(event)
        self.assertEqual(notices[0].kind, "tool_started")
        self.assertEqual(notices[0].name, "knowledge_search")

    def test_registry_records_the_adk_runtime(self) -> None:
        from viknow3.service.host.registry.catalog import Registry

        registry = Registry()
        adk = registry.profile("expertbuddy-adk")
        deepagents = registry.profile("expertbuddy-deepagents")
        assert adk is not None and deepagents is not None
        self.assertEqual(adk.runtime, "adk")
        self.assertEqual(deepagents.runtime, "langgraph")
        self.assertNotIn("video_inspect_2_0", adk.tools)

    def test_shell_emits_the_tool_name(self) -> None:
        from viknow3.service.agent.adk_adapter.runtime import _emit

        events = _Events()
        state = SimpleNamespace(citations={})
        _emit(
            AdkNotice(kind="tool_started", name="knowledge_search", tool_call_id="1", arguments={"query": "验收"}),
            events,
            state,
        )
        self.assertEqual(events.rows[0][0], "tool_call_started")
        self.assertEqual(events.rows[0][1]["name"], "knowledge_search")

    def test_host_routes_adk_away_from_deepagents(self) -> None:
        import os

        if not os.environ.get("VIKNOW_ACCESS_DSN"):
            self.skipTest("host import opens the access database")
        from viknow3.service.agent.adk_adapter.runtime import AdkTurnRuntime
        from viknow3.service.agent.deepagents_adapter.vendor_runtime import DeepAgentsTurnRuntime
        from viknow3.service.host.registry.catalog import Registry
        from viknow3.service.host.runtime.host import turn_runtime

        registry = Registry()
        adk = registry.profile("expertbuddy-adk")
        deepagents = registry.profile("expertbuddy-deepagents")
        assert adk is not None and deepagents is not None
        self.assertIsInstance(turn_runtime(adk), AdkTurnRuntime)
        self.assertIsInstance(turn_runtime(deepagents), DeepAgentsTurnRuntime)
        self.assertEqual(adk.runtime, "adk")
        self.assertEqual(deepagents.runtime, "langgraph")


class _Events:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    def emit(self, kind: str, payload: dict | None = None) -> None:
        self.rows.append((kind, payload or {}))


class _FakeKnowledge:
    def __init__(self) -> None:
        self.searched: str | None = None

    async def search_async(self, *, library_id: str, query: str, top_k: int | None = None, threshold=None):
        self.searched = library_id
        evidence = SimpleNamespace(
            id="ev-1",
            title="index-smoke-20261022.md",
            score=0.9,
            metadata={"full_doc_id": "asset-doc", "page": 0},
            parts=[
                SimpleNamespace(
                    text=(
                        '### context\n\n"确认索引构建与运行的稳定性及上线标准符合性。"\n\n'
                        '### chunk text\n\n"ViKnow 20261022"'
                    )
                )
            ],
        )
        return SimpleNamespace(evidence=[evidence], metadata={})

    async def discover_libraries_async(self, query: str, *, top_k: int | None = None, library_ids=None):
        library = SimpleNamespace(id="lib-a", name="lib-a")
        asset = SimpleNamespace(
            asset_id="asset-doc",
            title="index-smoke-20261022.md",
            modality="document",
            summary="确认索引构建与运行的稳定性",
        )
        return [SimpleNamespace(library=library, asset_matches=[asset])]

    def list_asset_segments(self, *, library_id: str, asset_id: str):
        return [
            {"content": "doc body", "metadata": {"chunk_type": "text", "title": "doc"}},
            {"content": "ViKnow", "metadata": {"chunk_type": "video", "begin_ts": 0, "end_ts": 1, "title": "clip"}},
        ]


if __name__ == "__main__":
    unittest.main()
