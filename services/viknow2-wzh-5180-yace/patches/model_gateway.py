"""Optional gateway routing at the HTTP boundary of a model client.

The SDK is always configured with the original model, URL and credential. A
gateway attempt is an independent request; returning a streaming response pins
the request to that endpoint, so a partial answer can never be replayed.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
from collections.abc import Callable
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from viknow.core.model_gateway_protocol import (
    PROTOCOL_HEADER,
    PROTOCOL_VERSION,
    GatewayMessageFailure,
    parse_result,
    protocol_failure,
    publish_interruption,
    publish_message,
)
from viknow.core.model_gateway_stream import start_async_stream, start_stream

logger = logging.getLogger(__name__)


class ModelGatewayRoute(BaseModel):
    """Gateway settings for one explicitly mapped original model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    path: str = ""
    base_url: str = ""
    base_url_env: str = "VIKNOW_TOKEN_GATEWAY_CHAT_BASE_URL"
    api_key_env: str = "VIKNOW_TOKEN_GATEWAY_API_KEY"
    connect_timeout_seconds: float = Field(default=3, gt=0)
    read_timeout_seconds: float = Field(default=30, gt=0)
    thinking_protocol: Literal["template", "enable", "disabled", "none"] = "none"

    def endpoint(self) -> tuple[httpx.URL, str] | None:
        key = os.getenv(self.api_key_env, "").strip()
        if not key:
            return None
        base = self.base_url.strip() or os.getenv(self.base_url_env, "").strip()
        if not base.strip():
            return None
        try:
            url = httpx.URL(base.rstrip("/") + "/" + self.path.strip("/"))
            if (
                url.scheme not in {"http", "https"}
                or not url.host
                or url.userinfo
                or url.query
                or url.fragment
            ):
                raise ValueError("invalid gateway URL")
            return url, key
        except (ValueError, httpx.InvalidURL):
            logger.warning("Model gateway URL is invalid; using original model endpoint")
            return None


def _body(request: httpx.Request, content: bytes, route: ModelGatewayRoute) -> bytes:
    if not request.url.path.endswith("/chat/completions"):
        return content
    payload = json.loads(content)
    if route.thinking_protocol != "none":
        for key in (
            "enable_thinking",
            "thinking",
            "reasoning",
            "reasoning_effort",
            "thinking_budget",
            "reasoning_budget",
            "preserve_thinking",
            "chat_template_kwargs",
        ):
            payload.pop(key, None)
        if route.thinking_protocol == "template":
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        elif route.thinking_protocol == "enable":
            payload["enable_thinking"] = False
        else:
            payload["thinking"] = {"type": "disabled"}
    return json.dumps(payload).encode()


def _request(
    original: httpx.Request,
    *,
    url: httpx.URL,
    content: bytes,
    key: str | None = None,
    connect_timeout: float | None = None,
    read_timeout: float | None = None,
) -> httpx.Request:
    headers = original.headers.copy()
    for name in ("host", "content-length", "transfer-encoding"):
        headers.pop(name, None)
    if key is not None:
        headers["authorization"] = f"Bearer {key}"
        headers[PROTOCOL_HEADER] = PROTOCOL_VERSION
        # Provider-specific credentials must not escape to the gateway.
        for name in ("api-key", "x-api-key", "openai-organization", "openai-project"):
            headers.pop(name, None)
    else:
        headers.pop(PROTOCOL_HEADER, None)
    extensions = dict(original.extensions)
    if connect_timeout is not None:
        previous = extensions.get("timeout", {}).get("connect")
        extensions["timeout"] = {
            **extensions.get("timeout", {}),
            "connect": min(previous, connect_timeout) if previous else connect_timeout,
        }
    if read_timeout is not None:
        previous = extensions.get("timeout", {}).get("read")
        extensions["timeout"]["read"] = min(previous, read_timeout) if previous else read_timeout
    return httpx.Request(
        original.method,
        url,
        headers=headers,
        content=content,
        extensions=extensions,
    )


def _attempts(
    original: httpx.Request,
    content: bytes,
    base_url: str,
    route: ModelGatewayRoute,
) -> tuple[httpx.Request | None, httpx.Request]:
    content = _body(original, content, route)
    direct_body = content
    if "application/json" in original.headers.get("content-type", ""):
        payload = json.loads(content)
        # ViKnow correlation metadata belongs to the gateway, not a raw vLLM API.
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            metadata = {
                key: value for key, value in metadata.items() if not key.startswith("viknow_")
            }
            if metadata:
                payload["metadata"] = metadata
            else:
                payload.pop("metadata", None)
        direct_body = json.dumps(payload).encode()
    direct = _request(original, url=original.url, content=direct_body)
    endpoint = route.endpoint()
    if endpoint is None:
        return None, direct
    root = httpx.URL(base_url)
    prefix = root.path.rstrip("/")
    if (original.url.scheme, original.url.host, original.url.port) != (
        root.scheme,
        root.host,
        root.port,
    ) or not original.url.path.startswith(prefix + "/"):
        return None, direct
    gateway_url, gateway_key = endpoint
    suffix = original.url.path[len(prefix) :]
    gateway_url = gateway_url.copy_with(
        path=gateway_url.path.rstrip("/") + suffix,
        query=original.url.query,
    )
    # When token-gateway is pointed at the same origin as the raw model
    # endpoint (5180 yace -> 127.0.0.1:8500), rewriting `model` to the
    # logical alias (viknow-agent-chat) 404s on vLLM. Skip that hop and
    # send the served name on the direct request only.
    def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
        port = url.port
        if port is None:
            port = 443 if url.scheme == "https" else 80
        return url.scheme, (url.host or "").lower(), port

    if _origin(gateway_url) == _origin(original.url):
        return None, direct
    if "application/json" in original.headers.get("content-type", ""):
        payload = json.loads(content)
        if "model" in payload:
            payload["model"] = route.model
        content = json.dumps(payload).encode()
    return _request(
        original,
        url=gateway_url,
        content=content,
        key=gateway_key,
        connect_timeout=route.connect_timeout_seconds,
        read_timeout=route.read_timeout_seconds,
    ), direct


def _valid_embedding(value: Any) -> bool:
    if isinstance(value, str):
        raw = base64.b64decode(value, validate=True)
        return bool(raw) and len(raw) % 4 == 0
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item)
            for item in value
        )
    )


def _is_stream_response(response: httpx.Response, request: httpx.Request) -> bool:
    streaming = (
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        == "text/event-stream"
    )
    if request.url.path.endswith("/chat/completions"):
        expected = bool(json.loads(request.content).get("stream", False))
        if streaming != expected:
            raise ValueError("gateway response stream mode differs from request")
    return streaming


def _check_json(response: httpx.Response, path: str) -> None:
    """Reject malformed completed model responses before returning to the SDK."""
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError("invalid model response")
    if path.endswith("/chat/completions"):
        choices = payload.get("choices")
        if (
            not isinstance(choices, list)
            or not choices
            or any(
                not isinstance(choice, dict)
                or not isinstance(choice.get("message"), dict)
                or not isinstance(choice["message"].get("role"), str)
                or not choice["message"]["role"]
                or not isinstance(choice["message"].get("content"), (str, list, type(None)))
                for choice in choices
            )
        ):
            raise ValueError("invalid model choices")
    if path.endswith("/embeddings"):
        data = payload.get("data")
        if (
            not isinstance(data, list)
            or not data
            or any(
                not isinstance(item, dict) or not _valid_embedding(item.get("embedding"))
                for item in data
            )
        ):
            raise ValueError("invalid model embeddings")
    if path.endswith("/rerank"):
        results = payload.get("results")
        if not isinstance(results, list):
            raise ValueError("invalid reranker results")
        for item in results:
            if not isinstance(item, dict):
                raise ValueError("invalid reranker result")
            try:
                index = int(item["index"])
                score = float(item.get("relevance_score", item.get("score")))
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise ValueError("invalid reranker result") from exc
            if index < 0 or not math.isfinite(score):
                raise ValueError("invalid reranker result")


class GatewayFirstTransport(httpx.BaseTransport):
    def __init__(
        self,
        route: ModelGatewayRoute,
        base_url: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.route, self.base_url = route, base_url
        self.transport = transport or httpx.HTTPTransport(retries=0)
        self.last_request_payload_bytes: int | None = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        gateway, direct = _attempts(request, request.read(), self.base_url, self.route)
        if gateway is not None:
            response = None
            try:
                self.last_request_payload_bytes = len(gateway.content)
                response = self.transport.handle_request(gateway)
                if not 200 <= response.status_code < 300:
                    response.read()
                    return _model_response(response, request)
                if _is_stream_response(response, gateway):
                    return start_stream(response)
                response.read()
                return _model_response(response, request)
            except GatewayMessageFailure as exc:
                if response is not None:
                    response.close()
                publish_interruption(exc)
                if not exc.report.permits_direct():
                    return _failure_response(exc)
            except httpx.TransportError as exc:
                if response is not None:
                    response.close()
                logger.warning(
                    "Model gateway failed (%s); using original endpoint", type(exc).__name__
                )
                publish_interruption(exc, allow_direct=True)
            except (httpx.DecodingError, ValueError):
                if response is not None:
                    response.close()
                failure = protocol_failure("网关响应无效，不能判定为网关宕机。")
                publish_interruption(failure)
                return _failure_response(failure)
        self.last_request_payload_bytes = len(direct.content)
        return self.transport.handle_request(direct)

    def close(self) -> None:
        self.transport.close()


class AsyncGatewayFirstTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        route: ModelGatewayRoute,
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.route, self.base_url = route, base_url
        self.transport = transport or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        gateway, direct = _attempts(request, await request.aread(), self.base_url, self.route)
        if gateway is not None:
            response = None
            try:
                response = await self.transport.handle_async_request(gateway)
                if not 200 <= response.status_code < 300:
                    await response.aread()
                    return _model_response(response, request)
                if _is_stream_response(response, gateway):
                    return await start_async_stream(response)
                await response.aread()
                return _model_response(response, request)
            except GatewayMessageFailure as exc:
                if response is not None:
                    await response.aclose()
                publish_interruption(exc)
                if not exc.report.permits_direct():
                    return _failure_response(exc)
            except httpx.TransportError as exc:
                if response is not None:
                    await response.aclose()
                logger.warning(
                    "Model gateway failed (%s); using original endpoint", type(exc).__name__
                )
                publish_interruption(exc, allow_direct=True)
            except (httpx.DecodingError, ValueError):
                if response is not None:
                    await response.aclose()
                failure = protocol_failure("网关响应无效，不能判定为网关宕机。")
                publish_interruption(failure)
                return _failure_response(failure)
            except BaseException:
                if response is not None:
                    await response.aclose()
                raise
        return await self.transport.handle_async_request(direct)

    async def aclose(self) -> None:
        await self.transport.aclose()


def _failure_response(failure: GatewayMessageFailure) -> httpx.Response:
    return httpx.Response(
        502, json=failure.report.model_dump(), headers={PROTOCOL_HEADER: PROTOCOL_VERSION}
    )


def _model_response(response: httpx.Response, request: httpx.Request) -> httpx.Response:
    if response.headers.get(PROTOCOL_HEADER) != PROTOCOL_VERSION:
        # During a staged rollout, legacy successes remain consumable. An old
        # 5xx/error is not evidence that the gateway (rather than its pool) died.
        if not 200 <= response.status_code < 300:
            return response
        _check_json(response, request.url.path)
        return response
    report = parse_result(response.json())
    publish_message(report.model_dump(exclude={"completion"}))
    if report.status == "failed":
        if report.permits_direct():
            raise GatewayMessageFailure(report, published=True)
        return httpx.Response(
            response.status_code if response.status_code >= 400 else 502,
            json=report.model_dump(),
            headers={PROTOCOL_HEADER: PROTOCOL_VERSION},
        )
    if response.status_code >= 400 or report.completion is None:
        raise protocol_failure("网关成功结果缺少模型内容或 HTTP 状态冲突。")
    unwrapped = httpx.Response(
        200, json=report.completion, extensions={"viknow_gateway_result": report.model_dump()}
    )
    _check_json(unwrapped, request.url.path)
    return unwrapped


def model_gateway_clients(
    route: ModelGatewayRoute, base_url: str, *, pooled: bool = True
) -> dict[str, Any]:
    return {
        "http_client": httpx.Client(
            transport=GatewayFirstTransport(
                route, base_url, None if pooled else _PerRequestTransport()
            )
        ),
        "http_async_client": httpx.AsyncClient(
            transport=AsyncGatewayFirstTransport(
                route, base_url, None if pooled else _PerRequestAsyncTransport()
            )
        ),
        # A retry of the whole SDK operation would repeat a successful gateway
        # attempt or amplify a failed direct attempt. The transport owns fallback.
        "max_retries": 0,
    }


def model_connection_options(config: Any, *, cache: dict | None = None) -> dict[str, Any]:
    """The sole SDK connection entry point, independent of the chosen chat class."""
    route = config.gateway
    if route is None or (route.endpoint() is None and route.thinking_protocol == "none"):
        return {}
    base_url = config.resolve_base_url()
    if not base_url:
        raise ValueError("Original model base URL is required for gateway fallback")
    if cache is None:
        return model_gateway_clients(route, base_url, pooled=False)
    key = (route, base_url)
    if key not in cache:
        cache[key] = model_gateway_clients(route, base_url)
    return cache[key]


async def close_model_connections(cache: dict) -> None:
    for options in cache.values():
        options["http_client"].close()
        await options["http_async_client"].aclose()
    cache.clear()


def gateway_chat_json(
    route: ModelGatewayRoute,
    base_url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout_seconds: float,
    request_size_callback: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Use the same connection policy for non-SDK OpenAI-compatible tools."""
    transport = GatewayFirstTransport(route, base_url)
    try:
        with httpx.Client(transport=transport, timeout=timeout_seconds) as client:
            response = client.post(
                base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
            if response.headers.get(PROTOCOL_HEADER) == PROTOCOL_VERSION and response.is_error:
                raise GatewayMessageFailure(parse_result(response.json()), published=True)
            response.raise_for_status()
            return response.json()
    finally:
        if request_size_callback is not None and transport.last_request_payload_bytes is not None:
            try:
                request_size_callback(transport.last_request_payload_bytes)
            except Exception:
                logger.warning("Model request size observer failed")


class _OwnedStream(httpx.SyncByteStream):
    def __init__(self, stream, transport):
        self.stream, self.transport = stream, transport

    def __iter__(self):
        yield from self.stream

    def close(self):
        try:
            self.stream.close()
        finally:
            self.transport.close()


class _OwnedAsyncStream(httpx.AsyncByteStream):
    def __init__(self, stream, transport):
        self.stream, self.transport = stream, transport

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self):
        try:
            await self.stream.aclose()
        finally:
            await self.transport.aclose()


class _PerRequestTransport(httpx.BaseTransport):
    """Tool contexts have no shutdown hook: their connections end with responses."""

    def handle_request(self, request):
        transport = httpx.HTTPTransport(retries=0)
        try:
            response = transport.handle_request(request)
            response.stream = _OwnedStream(response.stream, transport)
            return response
        except BaseException:
            transport.close()
            raise


class _PerRequestAsyncTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        transport = httpx.AsyncHTTPTransport(retries=0)
        try:
            response = await transport.handle_async_request(request)
            response.stream = _OwnedAsyncStream(response.stream, transport)
            return response
        except BaseException:
            await transport.aclose()
            raise
