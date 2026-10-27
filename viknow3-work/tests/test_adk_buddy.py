"""ADK profile, tools, and shell routing stay off the DeepAgents loop."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from agents.adk_buddy.instruction import build_instruction, load_profile
from agents.adk_buddy.knowledge_tools import CitationLog, build_knowledge_tools
from agents.adk_buddy.notices import AdkNotice, notices_from_adk_event
from agents.adk_buddy.session_url import async_postgres_url


class AdkProfileTests(unittest.TestCase):
    def test_profile_is_separate_and_names_its_own_runtime(self) -> None:
        data = load_profile()
        self.assertEqual(data["id"], "adk")
        self.assertEqual(data["runtime"], "adk")
        self.assertEqual(
            data["enabled_tools"],
            ["knowledge_discover", "knowledge_search", "list_doc_segments", "list_video_segments"],
        )
        instruction = build_instruction(("e2e-artifacts-20261022",))
        self.assertIn("chunk_text", instruction)
        self.assertIn("index_note", instruction)
        self.assertIn("e2e-artifacts-20261022", instruction)
        self.assertNotIn("read_file", instruction)
        self.assertNotIn("visual_ground", instruction)

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
        self.assertIn("稳定性", hit["index_note"])
        self.assertNotIn("稳定性", hit["chunk_text"])
        self.assertNotIn("snippet", hit)
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
        self.assertEqual(asset["index_note"], "确认索引构建与运行的稳定性")
        self.assertNotIn("summary", asset)

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
        from viknow3.application.agentcore.registry.catalog import Registry

        registry = Registry()
        adk = registry.profile("adk")
        deepagents = registry.profile("deepagents")
        assert adk is not None and deepagents is not None
        self.assertEqual(adk.runtime, "adk")
        self.assertEqual(deepagents.runtime, "langgraph")
        self.assertNotIn("video_inspect_2_0", adk.tools)

    def test_shell_emits_the_tool_name(self) -> None:
        from viknow3.application.agent.adk_adapter.runtime import _emit

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
        from viknow3.application.agent.adk_adapter.runtime import AdkTurnRuntime
        from viknow3.application.agent.deepagents_adapter.vendor_runtime import DeepAgentsTurnRuntime
        from viknow3.application.agentcore.registry.catalog import Registry
        from viknow3.application.agentcore.runtime.host import turn_runtime

        registry = Registry()
        adk = registry.profile("adk")
        deepagents = registry.profile("deepagents")
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
