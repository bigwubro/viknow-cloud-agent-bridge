"""Recover structured tool_calls from model markup dialects.

Some providers leak native tool-call text into ``content`` or
``reasoning_content`` instead of OpenAI ``tool_calls``. ViKnow recovers
complete GLM ``<tool_call>`` and DeepSeek DSML blocks when structured
``tool_calls`` are missing.
"""

from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

_RECOVERY_TRACE_MAX_CHARS = 50_000
_RECOVERY_TRACE_FILENAME = "tool-call-recovery.jsonl"

_RECOVERY_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "viknow_tool_call_recovery_context",
    default=None,
)


def set_tool_call_recovery_context(**kwargs: Any) -> None:
    """Attach optional run/model identifiers to subsequent recovery records."""
    current = dict(_RECOVERY_CONTEXT.get() or {})
    current.update({key: value for key, value in kwargs.items() if value is not None})
    _RECOVERY_CONTEXT.set(current)


def clear_tool_call_recovery_context() -> None:
    _RECOVERY_CONTEXT.set(None)


_GLM_TOOL_CALL_RE = re.compile(
    r"<tool_call>(.*?)</tool_call>",
    re.DOTALL,
)
_GLM_ARG_RE = re.compile(
    r"<arg_key>\s*(.*?)\s*</arg_key>\s*<arg_value>\s*(.*?)\s*</arg_value>",
    re.DOTALL,
)
# DeepSeek DSML uses fullwidth ｜ (U+FF5C) inside the tag token.
_DSML_BLOCK_RE = re.compile(
    r"<｜DSML｜(?:tool_calls|function_calls)>(.*?)</｜DSML｜(?:tool_calls|function_calls)>",
    re.DOTALL,
)
_DSML_INVOKE_RE = re.compile(
    r'<｜DSML｜invoke\s+name="([^"]+)">(.*?)</｜DSML｜invoke>',
    re.DOTALL,
)
_DSML_PARAM_RE = re.compile(
    r'<｜DSML｜parameter\s+name="([^"]+)"(?:\s+string="(true|false)")?\s*>'
    r"(.*?)</｜DSML｜parameter>",
    re.DOTALL,
)
_ASCII_TOOL_CALLS_BLOCK_RE = re.compile(
    r"<\|tool_calls\|>(.*?)</\|tool_calls\|>",
    re.DOTALL,
)
_ASCII_INVOKE_RE = re.compile(
    r'<\|tool_call(?:s)?\|\s*name="([^"]+)">(.*?)</\|tool_call(?:s)?\|>',
    re.DOTALL,
)

_MARKUP_FRAGMENT_HINTS = (
    "<tool_call>",
    "</tool_call>",
    "<arg_key>",
    "<｜DSML｜",
    "｜DSML｜",
    "<|tool_calls|>",
    "<|tool_call",
    "</|tool_",
)


def is_tool_call_markup_fragment(text: str) -> bool:
    if not text:
        return False
    return any(hint in text for hint in _MARKUP_FRAGMENT_HINTS)


def is_tool_call_markup_start(text: str) -> bool:
    stripped = (text or "").lstrip()
    return (
        stripped.startswith("<tool_call>")
        or stripped.startswith("<｜DSML｜tool_calls>")
        or stripped.startswith("<｜DSML｜function_calls>")
        or stripped.startswith("<|tool_calls|>")
        or stripped.startswith("<|tool_call")
    )


def is_tool_call_markup_end(text: str) -> bool:
    return (
        "</tool_call>" in (text or "")
        or "</｜DSML｜tool_calls>" in (text or "")
        or "</｜DSML｜function_calls>" in (text or "")
        or "</|tool_calls|>" in (text or "")
        or "</|tool_call|>" in (text or "")
    )


def extract_markup_tool_calls(text: str) -> list[dict[str, Any]]:
    """Parse complete markup tool calls from text. Incomplete blocks are ignored."""
    if not text or not is_tool_call_markup_fragment(text):
        return []
    calls: list[dict[str, Any]] = []
    calls.extend(_parse_glm_tool_calls(text))
    calls.extend(_parse_dsml_tool_calls(text))
    calls.extend(_parse_ascii_tool_calls(text))
    return calls


def strip_markup_tool_calls(text: str) -> str:
    """Remove complete markup tool-call blocks from text."""
    if not text:
        return ""
    cleaned = _GLM_TOOL_CALL_RE.sub("", text)
    cleaned = _DSML_BLOCK_RE.sub("", cleaned)
    cleaned = _ASCII_TOOL_CALLS_BLOCK_RE.sub("", cleaned)
    return cleaned.strip()


def recover_message_tool_calls(message: Any) -> bool:
    """If ``message`` lacks tool_calls, recover them from content/reasoning markup.

    Mutates ``message`` in place. Returns True when recovery applied.
    """
    existing = _message_tool_calls(message)
    if existing:
        return False

    content = _message_content_text(message)
    reasoning = _message_reasoning_text(message)
    recovered = extract_markup_tool_calls(content) + extract_markup_tool_calls(reasoning)
    if not recovered:
        return False

    cleaned_content = strip_markup_tool_calls(content)
    cleaned_reasoning = strip_markup_tool_calls(reasoning)
    _set_message_content(message, cleaned_content)
    _set_message_reasoning(message, cleaned_reasoning)
    _set_message_tool_calls(message, recovered)

    response_metadata = getattr(message, "response_metadata", None)
    if isinstance(response_metadata, dict):
        response_metadata = dict(response_metadata)
        response_metadata["finish_reason"] = "tool_calls"
        response_metadata["viknow_tool_call_recovery"] = True
        message.response_metadata = response_metadata

    record_tool_call_recovery(
        source="message",
        before_content=content,
        before_reasoning=reasoning,
        after_content=cleaned_content,
        after_reasoning=cleaned_reasoning,
        tool_calls=recovered,
    )
    return True


def recover_chat_result_tool_calls(result: Any) -> Any:
    generations = getattr(result, "generations", None)
    if not isinstance(generations, list):
        return result
    for generation in generations:
        message = getattr(generation, "message", None)
        if message is None:
            continue
        if recover_message_tool_calls(message):
            info = getattr(generation, "generation_info", None)
            if isinstance(info, dict):
                info = dict(info)
                info["finish_reason"] = "tool_calls"
                info["viknow_tool_call_recovery"] = True
                generation.generation_info = info
    return result


def tool_calls_to_tool_call_chunks(
    tool_calls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for index, call in enumerate(tool_calls):
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        chunks.append(
            {
                "name": str(call.get("name") or ""),
                "args": json.dumps(args, ensure_ascii=False),
                "id": str(call.get("id") or _new_tool_call_id()),
                "index": index,
                "type": "tool_call_chunk",
            }
        )
    return chunks


@dataclass
class StreamMarkupBuffer:
    content: str = ""
    reasoning: str = ""
    saw_structured_tool_calls: bool = False


_STREAM_MARKUP_BUFFER: ContextVar[StreamMarkupBuffer | None] = ContextVar(
    "viknow_stream_markup_buffer",
    default=None,
)


def reset_stream_markup_buffer() -> None:
    """Reset per-request stream accumulation used for markup recovery."""
    _STREAM_MARKUP_BUFFER.set(StreamMarkupBuffer())


def accumulate_stream_markup_buffer(generation_chunk: Any, reasoning_delta: str) -> None:
    buffer = _STREAM_MARKUP_BUFFER.get()
    if buffer is None:
        buffer = StreamMarkupBuffer()
        _STREAM_MARKUP_BUFFER.set(buffer)
    message = getattr(generation_chunk, "message", None)
    if message is None:
        return
    content = getattr(message, "content", None)
    if isinstance(content, str) and content:
        buffer.content += content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                buffer.content += block
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                buffer.content += block["text"]
    if reasoning_delta:
        buffer.reasoning += reasoning_delta
    tool_calls = getattr(message, "tool_calls", None) or ()
    tool_call_chunks = getattr(message, "tool_call_chunks", None) or ()
    if tool_calls or tool_call_chunks:
        buffer.saw_structured_tool_calls = True


def maybe_recover_stream_tool_calls(generation_chunk: Any) -> bool:
    """On stream finish, recover markup tool calls into the final chunk.

    Returns True when recovery was applied.
    """
    info = getattr(generation_chunk, "generation_info", None)
    if not isinstance(info, dict) or not info.get("finish_reason"):
        return False
    buffer = _STREAM_MARKUP_BUFFER.get()
    if buffer is None or buffer.saw_structured_tool_calls:
        return False
    message = getattr(generation_chunk, "message", None)
    if message is None:
        return False
    if getattr(message, "tool_calls", None) or getattr(message, "tool_call_chunks", None):
        return False
    before_content = buffer.content
    before_reasoning = buffer.reasoning
    recovered = extract_markup_tool_calls(before_content) + extract_markup_tool_calls(
        before_reasoning
    )
    if not recovered:
        return False
    after_content = strip_markup_tool_calls(before_content)
    after_reasoning = strip_markup_tool_calls(before_reasoning)
    _set_message_tool_calls(message, recovered)
    content = getattr(message, "content", None)
    if isinstance(content, str) and is_tool_call_markup_fragment(content):
        message.content = strip_markup_tool_calls(content)
    elif not content:
        message.content = after_content
    additional_kwargs = getattr(message, "additional_kwargs", None)
    if not isinstance(additional_kwargs, dict):
        additional_kwargs = {}
        message.additional_kwargs = additional_kwargs
    reasoning = additional_kwargs.get("reasoning_content")
    if isinstance(reasoning, str) and is_tool_call_markup_fragment(reasoning):
        cleaned = strip_markup_tool_calls(reasoning)
        if cleaned:
            additional_kwargs["reasoning_content"] = cleaned
        else:
            additional_kwargs.pop("reasoning_content", None)
    elif after_reasoning and not reasoning:
        additional_kwargs["reasoning_content"] = after_reasoning
    updated_info = dict(info)
    updated_info["finish_reason"] = "tool_calls"
    updated_info["viknow_tool_call_recovery"] = True
    generation_chunk.generation_info = updated_info
    buffer.saw_structured_tool_calls = True
    record_tool_call_recovery(
        source="stream",
        before_content=before_content,
        before_reasoning=before_reasoning,
        after_content=after_content,
        after_reasoning=after_reasoning,
        tool_calls=recovered,
    )
    return True


def record_tool_call_recovery(
    *,
    source: str,
    before_content: str,
    before_reasoning: str,
    after_content: str,
    after_reasoning: str,
    tool_calls: list[dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record one successful markup→tool_calls recovery for ops auditing.

    Always emits a structured log line and appends one JSONL record under
    ``{app.state_dir}/traces/tool-call-recovery.jsonl`` (same state volume /
    traces dir as existing runtime artifacts). Line count = interception count.
    """
    record = {
        "event": "agents.expert_buddy.tool_call_recovery",
        "ts": datetime.now(UTC).isoformat(),
        "source": source,
        "tool_call_count": len(tool_calls),
        "tool_call_names": [str(call.get("name") or "") for call in tool_calls],
        "tool_calls": tool_calls,
        "before": {
            "content": _clip_text(before_content, _RECOVERY_TRACE_MAX_CHARS),
            "reasoning_content": _clip_text(before_reasoning, _RECOVERY_TRACE_MAX_CHARS),
        },
        "after": {
            "content": _clip_text(after_content, _RECOVERY_TRACE_MAX_CHARS),
            "reasoning_content": _clip_text(after_reasoning, _RECOVERY_TRACE_MAX_CHARS),
        },
    }
    if extra:
        record["extra"] = extra
    context = _RECOVERY_CONTEXT.get()
    if context:
        record["context"] = dict(context)
    payload = json.dumps(record, ensure_ascii=False, default=str)
    logger.info("agents.expert_buddy.tool_call_recovery %s", payload)
    path = resolve_tool_call_recovery_trace_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(payload)
        file.write("\n")
    return record


@lru_cache(maxsize=1)
def resolve_tool_call_recovery_trace_path() -> Path:
    """``{app.state_dir}/traces/tool-call-recovery.jsonl`` on the existing state volume."""
    state_dir = _resolve_app_state_dir()
    return state_dir / "traces" / _RECOVERY_TRACE_FILENAME


def _resolve_app_state_dir() -> Path:
    try:
        from agents.expert_buddy.core.config import load_app_config

        return Path(load_app_config().app.state_dir)
    except Exception:
        return Path.cwd() / ".state"


def _clip_text(text: str, max_chars: int) -> dict[str, Any]:
    value = text or ""
    if len(value) <= max_chars:
        return {"text": value, "chars": len(value), "truncated": False}
    return {
        "text": value[:max_chars],
        "chars": len(value),
        "truncated": True,
        "omitted_chars": len(value) - max_chars,
    }


def _parse_glm_tool_calls(text: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for match in _GLM_TOOL_CALL_RE.finditer(text):
        body = match.group(1)
        name, args = _split_glm_body(body)
        if not name:
            continue
        calls.append(_tool_call(name, args))
    return calls


def _split_glm_body(body: str) -> tuple[str, dict[str, Any]]:
    first_arg = body.find("<arg_key>")
    if first_arg < 0:
        return body.strip(), {}
    name = body[:first_arg].strip()
    args: dict[str, Any] = {}
    for key_match in _GLM_ARG_RE.finditer(body[first_arg:]):
        key = key_match.group(1).strip()
        raw_value = key_match.group(2).strip()
        if key:
            args[key] = _coerce_arg_value(raw_value)
    return name, args


def _parse_dsml_tool_calls(text: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for block in _DSML_BLOCK_RE.finditer(text):
        calls.extend(_parse_dsml_invokes(block.group(1)))
    # Also accept bare invoke blocks if the outer wrapper was stripped.
    if not calls and "<｜DSML｜invoke" in text:
        calls.extend(_parse_dsml_invokes(text))
    return calls


def _parse_dsml_invokes(text: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for match in _DSML_INVOKE_RE.finditer(text):
        name = match.group(1).strip()
        body = match.group(2).strip()
        if not name:
            continue
        calls.append(_tool_call(name, _parse_dsml_args(body)))
    return calls


def _parse_dsml_args(body: str) -> dict[str, Any]:
    trimmed = body.strip()
    if trimmed.startswith("{") and trimmed.endswith("}"):
        try:
            parsed = json.loads(trimmed)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed

    args: dict[str, Any] = {}
    for match in _DSML_PARAM_RE.finditer(body):
        key = match.group(1).strip()
        is_string = (match.group(2) or "true").lower() == "true"
        raw_value = match.group(3).strip()
        if not key:
            continue
        if is_string:
            args[key] = raw_value
        else:
            args[key] = _coerce_arg_value(raw_value)
    return args


def _parse_ascii_tool_calls(text: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for block in _ASCII_TOOL_CALLS_BLOCK_RE.finditer(text):
        for match in _ASCII_INVOKE_RE.finditer(block.group(1)):
            name = match.group(1).strip()
            body = match.group(2).strip()
            if not name:
                continue
            args: dict[str, Any] = {}
            if body.startswith("{") and body.endswith("}"):
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    args = parsed
            calls.append(_tool_call(name, args))
    return calls


def _coerce_arg_value(raw_value: str) -> Any:
    if not raw_value:
        return raw_value
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        return raw_value


def _tool_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "args": args,
        "id": _new_tool_call_id(),
        "type": "tool_call",
    }


def _new_tool_call_id() -> str:
    return f"viknow_recovered_{uuid4().hex}"


def _message_tool_calls(message: Any) -> list[Any]:
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        return list(tool_calls)
    if isinstance(message, dict):
        value = message.get("tool_calls") or []
        return list(value) if value else []
    return []


def _message_content_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return ""


def _message_reasoning_text(message: Any) -> str:
    additional_kwargs = getattr(message, "additional_kwargs", None)
    if not isinstance(additional_kwargs, dict) and isinstance(message, dict):
        additional_kwargs = message.get("additional_kwargs")
    if not isinstance(additional_kwargs, dict):
        return ""
    for key in ("reasoning_content", "reasoning"):
        value = additional_kwargs.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _set_message_content(message: Any, content: str) -> None:
    if isinstance(message, dict):
        message["content"] = content
        return
    message.content = content


def _set_message_reasoning(message: Any, reasoning: str) -> None:
    if isinstance(message, dict):
        additional = dict(message.get("additional_kwargs") or {})
        if reasoning:
            additional["reasoning_content"] = reasoning
        else:
            additional.pop("reasoning_content", None)
            additional.pop("reasoning", None)
        message["additional_kwargs"] = additional
        return
    additional = getattr(message, "additional_kwargs", None)
    additional = {} if not isinstance(additional, dict) else dict(additional)
    if reasoning:
        additional["reasoning_content"] = reasoning
    else:
        additional.pop("reasoning_content", None)
        additional.pop("reasoning", None)
    message.additional_kwargs = additional


def _set_message_tool_calls(message: Any, tool_calls: list[dict[str, Any]]) -> None:
    if isinstance(message, dict):
        message["tool_calls"] = tool_calls
        return
    message.tool_calls = tool_calls
    # Streaming chunks also expose tool_call_chunks for aggregation.
    if hasattr(message, "tool_call_chunks"):
        message.tool_call_chunks = tool_calls_to_tool_call_chunks(tool_calls)
