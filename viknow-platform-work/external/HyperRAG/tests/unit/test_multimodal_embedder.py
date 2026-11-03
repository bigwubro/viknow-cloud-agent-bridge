from urllib import error

import pytest
from hyperrag.multimodal_embedder import OpenAICompatibleMultimodalEmbedder


def test_build_video_messages_includes_semantic_text_for_video_embedding() -> None:
    embedder = OpenAICompatibleMultimodalEmbedder(
        base_url="http://embed.example/v1",
        model="demo-embedding",
    )

    messages = embedder._build_video_messages(
        video_path="https://example.test/clip.mp4",
        instruction="Encode video",
        text="<caption>打开面板</caption>\n<ocr_keywords>SX-70</ocr_keywords>",
    )

    assert messages[0]["content"][0]["text"] == "Encode video"
    user_content = messages[1]["content"]
    assert user_content[0]["type"] == "video_url"
    assert user_content[1] == {
        "type": "text",
        "text": "<caption>打开面板</caption>\n<ocr_keywords>SX-70</ocr_keywords>",
    }


def test_build_image_text_messages_uses_openai_multimodal_content() -> None:
    embedder = OpenAICompatibleMultimodalEmbedder(
        base_url="http://embed.example/v1",
        model="demo-embedding",
    )

    messages = embedder._build_image_text_messages(
        image_url="data:image/png;base64,ZmFrZQ==",
        text="查找相同的阻门器",
        instruction="Encode image and text",
    )

    assert messages[0]["content"][0]["text"] == "Encode image and text"
    assert messages[1]["content"] == [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,ZmFrZQ=="},
        },
        {"type": "text", "text": "查找相同的阻门器"},
    ]


@pytest.mark.parametrize(
    "task,expected_attempts",
    [
        ("video_indexing", 1),
        ("query_text_to_video", 3),
        (None, 3),
    ],
)
def test_video_embedding_http_attempts_are_owned_by_batch_layer(
    monkeypatch,
    task,
    expected_attempts,
) -> None:
    import hyperrag.multimodal_embedder as module

    calls = []
    sleeps = []

    def fail_request(*args, **kwargs):
        calls.append(kwargs)
        raise error.URLError("connection unavailable")

    monkeypatch.setattr(module.request, "urlopen", fail_request)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    embedder = OpenAICompatibleMultimodalEmbedder(
        base_url="http://embed.example/v1",
        model="demo",
    )
    with pytest.raises(error.URLError, match="connection unavailable"):
        embedder.embed_video_sync("https://example.test/clip.mp4", task=task)

    assert len(calls) == expected_attempts
    assert len(sleeps) == expected_attempts - 1


@pytest.mark.parametrize(
    "task,code,attempts",
    [
        ("video_indexing", 503, 1),
        ("query_text_to_video", 503, 3),
        ("query_text_to_video", 429, 3),
        ("query_text_to_video", 400, 1),
    ],
)
def test_http_error_responses_closed_without_losing_error(monkeypatch, task, code, attempts):
    import io

    import hyperrag.multimodal_embedder as module

    bodies = []
    errors = []

    def fail(*args, **kwargs):
        body = io.BytesIO(b"upstream failure")
        exc = error.HTTPError(
            "http://embed.example/v1/embeddings", code, "original reason", {}, body
        )
        bodies.append(body)
        errors.append(exc)
        raise exc

    monkeypatch.setattr(module.request, "urlopen", fail)
    monkeypatch.setattr(module.time, "sleep", lambda delay: None)
    embedder = OpenAICompatibleMultimodalEmbedder(base_url="http://embed.example/v1", model="demo")
    with pytest.raises(error.HTTPError) as caught:
        embedder.embed_video_sync("https://example.test/clip.mp4", task=task)
    assert len(bodies) == attempts
    assert all(body.closed for body in bodies)
    assert caught.value is errors[-1]
    assert caught.value.code == code
    assert caught.value.reason == "original reason"
