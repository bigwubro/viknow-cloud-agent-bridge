"""Model-call boundary trace capture for eval runs."""

from __future__ import annotations

import json
import re
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Protocol

_SKILL_PATH_RE = re.compile(r"\.skills/([^/]+)/SKILL\.md")


@dataclass
class ModelCallSpan:
    """One model-call round captured at before/after boundaries."""

    index: int
    input_messages: list[Any] = field(default_factory=list)
    input_tools: list[Any] | None = None
    model: str | None = None
    output_content: str = ""
    output_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    started_at: float = 0.0
    first_token_at: float | None = None
    ended_at: float | None = None

    @property
    def latency_seconds(self) -> float | None:
        if self.started_at and self.ended_at is not None:
            return max(0.0, self.ended_at - self.started_at)
        return None

    @property
    def token_in(self) -> int:
        return _token_value(self.usage or {}, "input_tokens", "prompt_tokens")

    @property
    def token_out(self) -> int:
        return _token_value(self.usage or {}, "output_tokens", "completion_tokens")


class ModelCallTraceRecorder(Protocol):
    def on_model_request(self, payload: dict[str, Any]) -> None: ...

    def on_model_end(self, output: Any, usage: dict[str, Any] | None) -> None: ...

    def on_model_usage(
        self,
        usage: dict[str, Any],
        *,
        invocation_id: str | None = None,
    ) -> None: ...

    def on_stream_token(self) -> None: ...


class NoOpModelCallTraceRecorder:
    """Default recorder for chat runs; all methods are no-ops."""

    def on_model_request(self, payload: dict[str, Any]) -> None:
        return None

    def on_model_end(self, output: Any, usage: dict[str, Any] | None) -> None:
        return None

    def on_model_usage(
        self,
        usage: dict[str, Any],
        *,
        invocation_id: str | None = None,
    ) -> None:
        return None

    def on_stream_token(self) -> None:
        return None


NOOP_RECORDER = NoOpModelCallTraceRecorder()

_RECORDER_VAR: ContextVar[ModelCallTraceRecorder] = ContextVar(
    "viknow_model_call_trace_recorder",
    default=NOOP_RECORDER,
)


def active_model_call_trace_recorder() -> ModelCallTraceRecorder:
    return _RECORDER_VAR.get()


def activate_model_call_trace_recorder(recorder: ModelCallTraceRecorder) -> Token:
    return _RECORDER_VAR.set(recorder)


def reset_model_call_trace_recorder(token: Token) -> None:
    _RECORDER_VAR.reset(token)


class EvalModelCallTraceRecorder:
    """Captures model-call spans during eval runs."""

    def __init__(
        self,
        *,
        run_started_at: float,
    ) -> None:
        self.run_started_at = run_started_at
        self.spans: list[ModelCallSpan] = []
        self._current: ModelCallSpan | None = None
        self._first_token_at: float | None = None
        self._last_token_at: float | None = None
        self._run_token_in = 0
        self._run_token_out = 0
        self._usage_by_invocation: dict[str, tuple[int, int]] = {}

    def on_model_request(self, payload: dict[str, Any]) -> None:
        messages = payload.get("messages")
        tools = payload.get("tools")
        self._current = ModelCallSpan(
            index=len(self.spans),
            input_messages=list(messages) if isinstance(messages, list) else [],
            input_tools=list(tools) if isinstance(tools, list) else None,
            model=str(payload.get("model") or "") or None,
            started_at=perf_counter(),
        )

    def on_model_end(self, output: Any, usage: dict[str, Any] | None) -> None:
        if self._current is None:
            self._current = ModelCallSpan(index=len(self.spans), started_at=perf_counter())
        self._current.output_content = _message_content(output)
        self._current.output_tool_calls = normalize_tool_calls(output)
        self._current.ended_at = perf_counter()
        if usage:
            self._current.usage = usage
        self.spans.append(self._current)
        self._current = None

    def on_model_usage(
        self,
        usage: dict[str, Any],
        *,
        invocation_id: str | None = None,
    ) -> None:
        token_in = _token_value(usage, "input_tokens", "prompt_tokens")
        token_out = _token_value(usage, "output_tokens", "completion_tokens")
        if token_in == 0 and token_out == 0:
            return
        normalized = {
            "input_tokens": token_in,
            "output_tokens": token_out,
        }
        total_tokens = _token_value(usage, "total_tokens", "total_tokens")
        if total_tokens:
            normalized["total_tokens"] = total_tokens
        if self._current is not None:
            self._current.usage = normalized
        key = invocation_id or "__anonymous__"
        previous = self._usage_by_invocation.get(key)
        if previous is not None:
            self._run_token_in -= previous[0]
            self._run_token_out -= previous[1]
        self._usage_by_invocation[key] = (token_in, token_out)
        self._run_token_in += token_in
        self._run_token_out += token_out

    def on_stream_token(self) -> None:
        now = perf_counter()
        self._last_token_at = now
        if self._first_token_at is not None:
            return
        self._first_token_at = now
        if self._current is not None and self._current.first_token_at is None:
            self._current.first_token_at = now

    def project_metadata(self) -> tuple[str | None, list[list[str]]]:
        activated_skill: str | None = None
        tool_calls: list[list[str]] = []
        for span in self.spans:
            if activated_skill is None:
                activated_skill = skill_name_from_tool_calls(span.output_tool_calls)
            planned = [
                str(tool_call.get("name") or "")
                for tool_call in span.output_tool_calls
                if str(tool_call.get("name") or "")
            ]
            if planned:
                tool_calls.append(planned)
        return activated_skill, tool_calls

    def build_performance_payload(self) -> dict[str, Any]:
        activated_skill, tool_calls = self.project_metadata()
        token_in = self._run_token_in
        token_out = self._run_token_out
        if token_in == 0 and token_out == 0:
            for span in self.spans:
                usage = span.usage or {}
                token_in += _token_value(usage, "input_tokens", "prompt_tokens")
                token_out += _token_value(usage, "output_tokens", "completion_tokens")
        thinking_step = 0
        for span in self.spans:
            if _span_is_thinking_step(span):
                thinking_step += 1
        ttft: float | None = None
        if self._first_token_at is not None:
            ttft = max(0.0, self._first_token_at - self.run_started_at)
        total_cost: float | None = None
        if self._last_token_at is not None:
            total_cost = max(0.0, self._last_token_at - self.run_started_at)
        model_spans = [
            {
                "index": span.index,
                "latency_seconds": span.latency_seconds,
                "token_in": span.token_in,
                "token_out": span.token_out,
                "tool_calls": [
                    str(tool_call.get("name") or "")
                    for tool_call in span.output_tool_calls
                    if str(tool_call.get("name") or "")
                ],
            }
            for span in self.spans
        ]
        return {
            "ttft": ttft,
            "total_cost": total_cost,
            "token_in": token_in,
            "token_out": token_out,
            "thinking_step": thinking_step,
            "activated_skill": activated_skill,
            "tool_calls": tool_calls,
            "model_spans": model_spans,
        }

    def last_context_messages(self) -> list[Any]:
        if not self.spans:
            return []
        return list(self.spans[-1].input_messages)


def _span_is_thinking_step(span: ModelCallSpan) -> bool:
    if skill_name_from_tool_calls(span.output_tool_calls):
        return True
    return any(str(tool_call.get("name") or "") for tool_call in span.output_tool_calls)


def normalize_tool_calls(message: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for tool_call in _message_tool_calls(message):
        if isinstance(tool_call, dict):
            name = str(tool_call.get("name") or "")
            args = tool_call.get("args")
            if args is None:
                args = tool_call.get("arguments")
            normalized.append({"name": name, "args": _coerce_args(args)})
            continue
        name = str(getattr(tool_call, "name", "") or "")
        args = getattr(tool_call, "args", None)
        if args is None:
            args = getattr(tool_call, "arguments", None)
        normalized.append({"name": name, "args": _coerce_args(args)})
    return normalized


def skill_name_from_tool_calls(tool_calls: list[dict[str, Any]]) -> str | None:
    for tool_call in tool_calls:
        if tool_call.get("name") != "read_file":
            continue
        args = tool_call.get("args")
        if not isinstance(args, dict):
            continue
        for key in ("file_path", "path"):
            value = args.get(key)
            if not isinstance(value, str):
                continue
            match = _SKILL_PATH_RE.search(value.replace("\\", "/"))
            if match:
                return match.group(1)
    return None


def _message_tool_calls(message: Any) -> list[Any]:
    if message is None:
        return []
    tool_calls = getattr(message, "tool_calls", None)
    if isinstance(tool_calls, list):
        return tool_calls
    if isinstance(message, dict):
        value = message.get("tool_calls")
        return value if isinstance(value, list) else []
    return []


def _message_content(message: Any) -> str:
    if message is None:
        return ""
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return str(content)


def _coerce_args(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return parsed if isinstance(parsed, dict) else {"raw": parsed}
    if value is None:
        return {}
    return {"raw": value}


def _token_value(usage: dict[str, Any], primary: str, fallback: str) -> int:
    for key in (primary, fallback):
        value = usage.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
    return 0
