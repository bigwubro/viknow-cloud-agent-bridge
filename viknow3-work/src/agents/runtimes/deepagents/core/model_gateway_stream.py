"""Release a model stream after its first usable event, without replaying output."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator

import httpx

from agents.runtimes.deepagents.core.model_gateway_protocol import (
    PROTOCOL_HEADER,
    PROTOCOL_VERSION,
    MessageStreamDecoder,
    protocol_failure,
    publish_interruption,
)


def _ready(prefix: bytes) -> bool:
    # Only startup/role/keepalive events may be buffered. A malformed gateway
    # must not consume unbounded memory before falling back to the original.
    for event in prefix.replace(b"\r\n", b"\n").split(b"\n\n")[:-1]:
        data = b"\n".join(
            line[5:].strip() for line in event.splitlines() if line.startswith(b"data:")
        )
        if not data:
            continue
        if data == b"[DONE]":
            raise ValueError("gateway stream ended without a model result")
        payload = json.loads(data)
        if not isinstance(payload, dict) or payload.get("error"):
            raise protocol_failure("网关返回模型失败，未授权直连回退。")
        choices = payload.get("choices", [])
        if not isinstance(choices, list):
            raise ValueError("invalid gateway stream choices")
        for choice in choices:
            if not isinstance(choice, dict):
                raise ValueError("invalid gateway stream choice")
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                raise ValueError("invalid gateway stream choice")
            if choice.get("finish_reason") or any(
                delta.get(key) for key in ("content", "tool_calls", "refusal", "reasoning_content")
            ):
                return True
    if len(prefix) > 65536:
        raise ValueError("gateway stream startup exceeds 64 KiB")
    return False


class _SyncStream(httpx.SyncByteStream):
    def __init__(self, prefix: bytes, tail: Iterator[bytes], response: httpx.Response):
        self.prefix, self.tail, self.response = prefix, tail, response

    def __iter__(self):
        yield self.prefix
        try:
            yield from self.tail
        except Exception as exc:
            publish_interruption(
                exc,
                request_id=self.response.extensions.get("viknow_gateway_request_id")
                or self.response.headers.get("x-request-id"),
            )
            raise

    def close(self):
        self.response.close()


class _AsyncStream(httpx.AsyncByteStream):
    def __init__(self, prefix: bytes, tail: AsyncIterator[bytes], response: httpx.Response):
        self.prefix, self.tail, self.response = prefix, tail, response

    async def __aiter__(self):
        yield self.prefix
        try:
            async for chunk in self.tail:
                yield chunk
        except Exception as exc:
            publish_interruption(
                exc,
                request_id=self.response.extensions.get("viknow_gateway_request_id")
                or self.response.headers.get("x-request-id"),
            )
            raise

    async def aclose(self):
        await self.response.aclose()


def _response(original: httpx.Response, stream) -> httpx.Response:
    headers = original.headers.copy()
    for name in ("content-length", "content-encoding", "transfer-encoding"):
        headers.pop(name, None)
    return httpx.Response(
        original.status_code, headers=headers, stream=stream, extensions=original.extensions
    )


def start_stream(response: httpx.Response) -> httpx.Response:
    prefix = bytearray()
    tail = _decoded_stream(response)
    for chunk in tail:
        prefix.extend(chunk)
        if _ready(prefix):
            return _response(response, _SyncStream(bytes(prefix), tail, response))
    raise ValueError("empty gateway stream")


async def start_async_stream(response: httpx.Response) -> httpx.Response:
    prefix = bytearray()
    tail = _decoded_async_stream(response)
    async for chunk in tail:
        prefix.extend(chunk)
        if _ready(prefix):
            return _response(response, _AsyncStream(bytes(prefix), tail, response))
    raise ValueError("empty gateway stream")


def _decoded_stream(response: httpx.Response) -> Iterator[bytes]:
    if response.headers.get(PROTOCOL_HEADER) != PROTOCOL_VERSION:
        yield from response.iter_bytes()
        return
    decoder = MessageStreamDecoder()
    for content in response.iter_bytes():
        for part in decoder.feed(content):
            response.extensions["viknow_gateway_request_id"] = decoder.request_id
            yield part
    decoder.finish()


async def _decoded_async_stream(response: httpx.Response) -> AsyncIterator[bytes]:
    if response.headers.get(PROTOCOL_HEADER) != PROTOCOL_VERSION:
        async for content in response.aiter_bytes():
            yield content
        return
    decoder = MessageStreamDecoder()
    async for content in response.aiter_bytes():
        for part in decoder.feed(content):
            response.extensions["viknow_gateway_request_id"] = decoder.request_id
            yield part
    decoder.finish()
