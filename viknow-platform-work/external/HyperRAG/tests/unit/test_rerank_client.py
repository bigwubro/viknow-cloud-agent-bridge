import json
import urllib.request

import pytest
from hyperrag.rerank import (
    OpenAICompatibleReranker,
    RerankCandidate,
    RerankMedia,
    RerankMode,
)


class FakeRerankResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


@pytest.mark.asyncio
async def test_reranker_posts_text_documents_and_maps_results(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(request: urllib.request.Request, timeout: float):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeRerankResponse(
            {"results": [{"index": 1, "relevance_score": 0.9, "document": "echo"}]}
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    reranker = OpenAICompatibleReranker(
        api_key="test-key",
        model="demo-reranker",
        api_url="https://rerank.example.test/v1/rerank",
        mode=RerankMode.TEXT,
        timeout_seconds=12,
    )

    results = await reranker.rerank(
        "bolt torque",
        [
            RerankCandidate(id="a", text="general notes"),
            RerankCandidate(id="b", text="tighten bolt to 30 Nm"),
        ],
    )

    assert captured["url"] == "https://rerank.example.test/v1/rerank"
    assert captured["timeout"] == 12
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["body"] == {
        "model": "demo-reranker",
        "query": "bolt torque",
        "documents": ["general notes", "tighten bolt to 30 Nm"],
    }
    assert results[0].candidate_id == "b"
    assert results[0].index == 1
    assert results[0].relevance_score == 0.9
    assert "document" not in results[0].metadata


@pytest.mark.asyncio
async def test_reranker_builds_multimodal_video_document(monkeypatch, tmp_path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"test-video-bytes")
    captured = {}

    def fake_urlopen(request: urllib.request.Request, timeout: float):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeRerankResponse({"results": [{"index": 0, "relevance_score": 0.8}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    reranker = OpenAICompatibleReranker(
        api_key="test-key",
        model="demo-reranker",
        api_url="https://rerank.example.test/v1/rerank",
        mode=RerankMode.MULTIMODAL,
    )

    await reranker.rerank(
        "bolt torque",
        [
            RerankCandidate(
                id="video-1",
                text="<caption>tighten the bolt</caption>",
                media=[RerankMedia(kind="video", path=video)],
            )
        ],
    )

    document = captured["body"]["documents"][0]
    assert document["content"][0] == {
        "type": "text",
        "text": "<caption>tighten the bolt</caption>",
    }
    assert document["content"][1]["type"] == "video_url"
    assert document["content"][1]["video_url"]["url"].startswith("data:video/mp4;base64,")


@pytest.mark.asyncio
async def test_reranker_adjusts_result_indices_across_batches(monkeypatch) -> None:
    captured_documents = []

    def fake_urlopen(request: urllib.request.Request, timeout: float):
        body = json.loads(request.data.decode("utf-8"))
        captured_documents.append(body["documents"])
        return FakeRerankResponse({"results": [{"index": 0, "relevance_score": 0.7}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    reranker = OpenAICompatibleReranker(
        api_key="test-key",
        model="demo-reranker",
        api_url="https://rerank.example.test/v1/rerank",
        batch_size=1,
    )

    results = await reranker.rerank(
        "query",
        [
            RerankCandidate(id="a", text="first"),
            RerankCandidate(id="b", text="second"),
        ],
    )

    assert captured_documents == [["first"], ["second"]]
    assert [(result.candidate_id, result.index) for result in results] == [
        ("a", 0),
        ("b", 1),
    ]
