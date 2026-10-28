"""Second-hop gateway messages; no changes to ViKnow's public event protocol."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import suppress
from contextvars import ContextVar
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_HEADER = "X-ViKnow-Response-Protocol"
PROTOCOL_VERSION = "message-v1"
MODEL_MESSAGE_SINK: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "model_gateway_message_sink", default=None
)


class ProviderAttempt(BaseModel):
    model_config = ConfigDict(extra="ignore")

    attempt: int = Field(ge=1)
    provider: str
    deployment: str
    status: Literal["succeeded", "failed"]
    http_status: int | None = None
    code: str
    message: str
    upstream_request_id: str | None = None
    duration_ms: float = Field(ge=0)
    usage: dict[str, Any] | None = None


class GatewayResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    request_id: str | None
    status: Literal["succeeded", "failed"]
    code: str
    failure_origin: str | None
    direct_fallback_allowed: bool
    trace_complete: bool
    message: str
    model: str
    provider_attempts: list[ProviderAttempt] = Field(default_factory=list)
    completion: dict[str, Any] | None = None
    retryable: bool = False
    submission_state: Literal["not_submitted", "confirmed_failed", "unknown", "succeeded"] = (
        "unknown"
    )
    retry_after_seconds: float | None = Field(default=None, ge=0)

    def permits_direct(self) -> bool:
        # A provider cannot turn its own 5xx into permission to bypass the pool.
        return (
            self.status == "failed"
            and self.code == "GATEWAY_UNAVAILABLE"
            and self.failure_origin == "gateway"
            and self.direct_fallback_allowed
        )


class GatewayMessageFailure(RuntimeError):
    """An authoritative terminal result, never a generic connection failure."""

    def __init__(self, report: GatewayResult, *, published: bool = False):
        self.report = report
        self.published = published
        super().__init__(json.dumps(report.model_dump(exclude={"completion"}), ensure_ascii=False))


def publish_message(message: dict[str, Any]) -> None:
    """Deliver diagnostics to the second-hop caller without emitting UI events."""
    diagnostic = {
        key: value for key, value in message.items() if key not in {"completion", "chunk"}
    }
    # Use the application logger: ViKnow does not bridge arbitrary stdlib INFO logs.
    # Observability must never turn a successful model call into a retry.
    with suppress(Exception):
        logger.info("model_gateway_message={}", json.dumps(diagnostic, ensure_ascii=False))
    sink = MODEL_MESSAGE_SINK.get()
    if sink is not None:
        with suppress(Exception):
            sink(diagnostic)


def publish_interruption(
    exc: Exception, *, request_id: str | None = None, allow_direct: bool = False
) -> None:
    if isinstance(exc, GatewayMessageFailure):
        # Authoritative terminal results have already been published by the decoder.
        if exc.published:
            return
        exc.published = True
        report = exc.report.model_copy(update={"request_id": request_id or exc.report.request_id})
    else:
        report = GatewayResult(
            request_id=request_id,
            status="failed",
            code="GATEWAY_CONNECTION_LOST",
            failure_origin="unknown",
            direct_fallback_allowed=allow_direct,
            trace_complete=False,
            message="网关连接中断，未取得完整调用结果。",
            model="",
        )
    publish_message({"kind": "result", **report.model_dump(exclude={"completion"})})


def protocol_failure(message: str) -> GatewayMessageFailure:
    return GatewayMessageFailure(
        GatewayResult(
            request_id=None,
            status="failed",
            code="GATEWAY_PROTOCOL_INVALID",
            failure_origin="unknown",
            direct_fallback_allowed=False,
            trace_complete=False,
            message=message,
            model="",
        )
    )


def parse_result(payload: Any) -> GatewayResult:
    try:
        result = GatewayResult.model_validate(payload)
    except ValueError as exc:
        raise protocol_failure("网关返回的 message-v1 结果不符合约定。") from exc
    if result.status == "succeeded" and result.code != "OK":
        raise protocol_failure("网关成功状态与结果码不一致。")
    return result


class MessageStreamDecoder:
    """Incrementally unwrap SSE while requiring an explicit terminal result."""

    def __init__(self) -> None:
        self.pending = bytearray()
        self.sequence = 0
        self.request_id: str | None = None
        self.terminal = False

    def feed(self, content: bytes) -> Iterator[bytes]:
        self.pending.extend(content)
        while True:
            # Handle both standard line endings without corrupting split CRLFs.
            normalized = bytes(self.pending).replace(b"\r\n", b"\n")
            boundary = normalized.find(b"\n\n")
            if boundary < 0:
                if len(self.pending) > 4 * 1024 * 1024:
                    raise protocol_failure("网关单条流式消息超过限制。")
                break
            frame, rest = normalized[:boundary], normalized[boundary + 2 :]
            self.pending = bytearray(rest)
            data = b"\n".join(
                line[5:].strip() for line in frame.splitlines() if line.startswith(b"data:")
            )
            if not data:
                continue
            if self.terminal:
                raise protocol_failure("网关终态之后继续返回模型消息。")
            try:
                message = json.loads(data)
            except (ValueError, UnicodeError) as exc:
                raise protocol_failure("网关流式消息不是有效 JSON。") from exc
            if not isinstance(message, dict):
                raise protocol_failure("网关流式消息必须为对象。")
            sequence = message.get("sequence")
            if type(sequence) is not int or sequence <= self.sequence:
                raise protocol_failure("网关流式消息序号未递增。")
            self.sequence = sequence
            request_id = message.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                raise protocol_failure("网关流式消息缺少请求编号。")
            if self.request_id is not None and self.request_id != request_id:
                raise protocol_failure("网关流式消息包含不同请求编号。")
            self.request_id = request_id
            kind = message.get("kind")
            if kind == "model_chunk":
                chunk = message.get("chunk")
                if not isinstance(chunk, dict) or not isinstance(chunk.get("choices"), list):
                    raise protocol_failure("网关模型增量格式无效。")
                yield b"data: " + json.dumps(chunk).encode() + b"\n\n"
            elif kind == "provider_result":
                try:
                    ProviderAttempt.model_validate(message)
                except ValueError as exc:
                    raise protocol_failure("网关 provider 结果格式无效。") from exc
                publish_message(message)
            elif kind == "result":
                report = parse_result(message)
                publish_message(message)
                self.terminal = True
                if report.status == "failed":
                    raise GatewayMessageFailure(report, published=True)
                yield b"data: [DONE]\n\n"
            else:
                raise protocol_failure("网关返回了未知消息类型。")

    def finish(self) -> None:
        if not self.terminal:
            raise protocol_failure("网关流式连接结束，但未收到最终结果；执行结果未知。")
