"""knowledge_discover parity with viknow2 knowledge.py."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from viknow3.application.agent.knowledge_assistant.tools import knowledge_discover
from viknow3.application.agent.qa_expert.grant import bind_grant, reset_grant
from viknow3.application.hyperrag.client import HyperragClient
from viknow3.application.hyperrag.knowledge_provider import KnowledgeLibrary


class _Grant:
    def __init__(self, libraries: list[str]) -> None:
        self._libraries = libraries

    def libraries(self) -> list[str]:
        return self._libraries


class DiscoverV2ParityTests(unittest.TestCase):
    def test_single_run_library_uses_run_allowlist_not_hyperrag_discover(self) -> None:
        token = bind_grant(_Grant(["lib-x"]))
        try:
            adapter = MagicMock()
            adapter.list_libraries.return_value = [
                KnowledgeLibrary(id="lib-x", name="测试库3", description="demo", metadata={}),
            ]
            client = HyperragClient.__new__(HyperragClient)
            client._adapter = adapter  # type: ignore[attr-defined]
            with patch("viknow3.application.hyperrag.client.HYPERAG", client):
                with patch.object(client, "discover") as discover:
                    payload = json.loads(knowledge_discover("知识库列表"))
            discover.assert_not_called()
            self.assertEqual(payload["source"], "run_allowlist")
            self.assertEqual(payload["libraries"][0]["library_id"], "lib-x")
            self.assertEqual(payload["libraries"][0]["title"], "测试库3")
        finally:
            reset_grant(token)

    def test_multi_library_client_falls_back_to_list(self) -> None:
        adapter = MagicMock()
        adapter.discover_libraries.return_value = []
        adapter.search_libraries.return_value = []
        adapter.list_libraries.return_value = [
            KnowledgeLibrary(id="lib-a", name="A", description="", metadata={}),
        ]
        client = HyperragClient.__new__(HyperragClient)
        client._adapter = adapter  # type: ignore[attr-defined]
        payload = client.discover("知识库列表", library_ids=["lib-a", "lib-b"])
        self.assertEqual([row["library_id"] for row in payload["libraries"]], ["lib-a"])


if __name__ == "__main__":
    unittest.main()
