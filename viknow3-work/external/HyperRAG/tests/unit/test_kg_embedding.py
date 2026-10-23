import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hyperrag.config_loader import HyperRAGConfig
from hyperrag.kg_vector_store import KGEmbeddingClient
from hyperrag.single_layer_kg import SingleLayerKGService


def client_with_endpoint(endpoint, *, concurrency=3):
    client = KGEmbeddingClient(
        model="test",
        base_url="http://unused",
        api_key="test",
        batch_size=2,
        max_concurrency=concurrency,
    )
    client._client = SimpleNamespace(embeddings=SimpleNamespace(create=endpoint))
    client._truncate_text = lambda text: text
    return client


@pytest.mark.asyncio
async def test_text_batches_reach_concurrency_limit_and_preserve_input_order():
    active = 0
    peak = 0
    ready = asyncio.Event()
    batches = []

    async def create(*, model, input):
        nonlocal active, peak
        assert model == "test"
        batches.append(input)
        active += 1
        peak = max(peak, active)
        if active == 3:
            ready.set()
        try:
            await ready.wait()
            await asyncio.sleep((10 - int(input[0])) / 1000)
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[float(text)]) for text in input]
            )
        finally:
            active -= 1

    client = client_with_endpoint(create)
    vectors = await asyncio.wait_for(client.embed_texts([str(i) for i in range(10)]), 2)
    assert peak == 3
    assert active == 0
    assert len(batches) == 5
    assert all(len(batch) == 2 for batch in batches)
    assert vectors == [[float(i)] for i in range(10)]


@pytest.mark.asyncio
async def test_text_embedding_propagates_endpoint_failure():
    endpoint = AsyncMock(side_effect=RuntimeError("embedding unavailable"))
    client = client_with_endpoint(endpoint)
    with pytest.raises(RuntimeError, match="embedding unavailable"):
        await client.embed_texts(["one", "two", "three"])


@pytest.mark.asyncio
async def test_text_embedding_rejects_incomplete_response():
    endpoint = AsyncMock(return_value=SimpleNamespace(data=[]))
    client = client_with_endpoint(endpoint)
    with pytest.raises(ValueError, match="response count"):
        await client.embed_texts(["one"])


def test_service_passes_shared_concurrency_and_text_batch_size():
    config = HyperRAGConfig(embedding_max_async=5, kg_vector_embedding_batch_size=7)
    client = SingleLayerKGService(config)._get_embedding_client()
    assert client.max_concurrency == 5
    assert client.batch_size == 7


def test_service_passes_er_extraction_concurrency():
    config = HyperRAGConfig(ER_extraction_max_async=3)
    extractor = SingleLayerKGService(config)._get_extractor()
    assert extractor._semaphore._value == 3
