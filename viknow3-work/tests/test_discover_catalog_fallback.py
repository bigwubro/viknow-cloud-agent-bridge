"""Discover falls back to granted library catalog when query matches nothing."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from viknow3.application.hyperrag.client import HyperragClient
from viknow3.application.hyperrag.knowledge_provider import KnowledgeLibrary


class DiscoverCatalogFallbackTests(unittest.TestCase):
    def test_empty_match_returns_granted_libraries(self) -> None:
        adapter = MagicMock()
        adapter.discover_libraries.return_value = []
        adapter.list_libraries.return_value = [
            KnowledgeLibrary(id="lib-x", name="测试库3", description="demo", metadata={}),
        ]
        client = HyperragClient()
        client._adapter = adapter
        payload = client.discover("知识库列表", library_ids=["lib-x"])
        self.assertEqual([row["library_id"] for row in payload["libraries"]], ["lib-x"])
        self.assertEqual(payload["libraries"][0]["reason"], "granted_library_catalog")


if __name__ == "__main__":
    unittest.main()
