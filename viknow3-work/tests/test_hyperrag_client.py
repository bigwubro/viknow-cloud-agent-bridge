"""HyperRAG client shape."""

from __future__ import annotations

import unittest
from typing import Any

from viknow3.service.hyperrag.client import HyperragClient, HyperragClientError
from viknow3.service.hyperrag.evidence import Evidence, EvidencePart, EvidencePartKind, EvidenceTimeRange
from viknow3.service.hyperrag.knowledge_provider import (
    KnowledgeAssetDiscovery,
    KnowledgeLibrary,
    KnowledgeLibrarySearchResult,
)


class _Payload:
    def __init__(self, evidence: list[Evidence]) -> None:
        self.library_id = "lib-1"
        self.query = "安装"
        self.evidence = evidence
        self.metadata = {"retrieval_mode": "hybrid"}


class _Adapter:
    def __init__(self) -> None:
        self.search_kwargs: dict[str, Any] = {}

    def discover_libraries(self, query: str, *, top_k: int | None = None, library_ids=None):
        del query, top_k
        library_id = list(library_ids or ["lib-1"])[0]
        return [
            KnowledgeLibrarySearchResult(
                library=KnowledgeLibrary(id=library_id, name="手册", description="安装说明"),
                score=0.9,
                reason="title match",
                asset_matches=[
                    KnowledgeAssetDiscovery(
                        asset_id="doc-1",
                        title="产品手册.pdf",
                        modality="document",
                        summary="安装",
                        score=0.8,
                    )
                ],
            )
        ]

    def search(self, *, library_id: str, query: str, top_k: int | None = None, threshold: float | None = None):
        self.search_kwargs = {"library_id": library_id, "query": query, "top_k": top_k, "threshold": threshold}
        return _Payload(
            [
                Evidence(
                    id="chunk-1",
                    source="knowledge_search",
                    title="产品手册.pdf",
                    score=0.7,
                    parts=[
                        EvidencePart(kind=EvidencePartKind.TEXT, text="先固定支架，再接通电源。"),
                        EvidencePart(
                            kind=EvidencePartKind.VIDEO,
                            time_range=EvidenceTimeRange(start_seconds=1.0, end_seconds=4.0),
                        ),
                    ],
                    metadata={"library_id": library_id, "asset_id": "vid-1", "chunk_type": "video"},
                )
            ]
        )

    def list_asset_segments(self, *, library_id: str, asset_id: str):
        del library_id, asset_id
        return [
            {
                "chunk_id": "v1",
                "order": 0,
                "content": "开场",
                "metadata": {"chunk_type": "video", "begin_ts": 0, "end_ts": 2},
            },
            {
                "chunk_id": "v2",
                "order": 1,
                "content": "安装步骤在第二节",
                "metadata": {"chunk_type": "video", "begin_ts": 2, "end_ts": 5},
            },
            {
                "chunk_id": "d1",
                "order": 2,
                "content": "文档里的安装步骤",
                "metadata": {"chunk_type": "text"},
            },
        ]

    def search_by_image(self, **kwargs):
        self.image_kwargs = kwargs
        return _Payload([])


def _client() -> tuple[HyperragClient, _Adapter]:
    adapter = _Adapter()
    client = HyperragClient.__new__(HyperragClient)
    client._adapter = adapter
    return client, adapter


class HyperragClientTests(unittest.TestCase):
    def test_discover_search_and_segments_share_one_client_shape(self) -> None:
        client, adapter = _client()
        found = client.discover("安装", library_ids=["lib-1"], top_k=1)
        self.assertEqual(found["source"], "hyperrag")
        self.assertEqual(found["libraries"][0]["library_id"], "lib-1")
        self.assertEqual(found["libraries"][0]["top_files"][0]["asset_id"], "doc-1")

        searched = client.search("lib-1", "安装", None, top_k=3, threshold=0.2)
        self.assertEqual(adapter.search_kwargs["top_k"], 3)
        self.assertEqual(adapter.search_kwargs["threshold"], 0.2)
        evidence = searched["evidence"][0]
        self.assertEqual(evidence["asset_id"], "vid-1")
        self.assertEqual(evidence["modality"], "video")
        self.assertEqual(evidence["snippet"], "先固定支架，再接通电源。")
        self.assertEqual(evidence["time_range"]["end_seconds"], 4.0)
        self.assertEqual(evidence["parts"][0]["text"], "先固定支架，再接通电源。")

        video = client.list_segments("lib-1", "vid-1", modality="video", mode="select", query="安装", limit=5)
        self.assertEqual([item["segment_id"] for item in video["segments"]], ["v2"])
        document = client.list_segments("lib-1", "doc-1", modality="document", mode="map", page_size=10)
        self.assertEqual([item["segment_id"] for item in document["segments"]], ["d1"])
        self.assertNotIn("text", document["segments"][0])

        with self.assertRaises(HyperragClientError) as caught:
            client.list_segments("lib-1", "vid-1", modality="video", mode="nope")
        self.assertEqual(caught.exception.status, 422)


if __name__ == "__main__":
    unittest.main()
