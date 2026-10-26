"""Structured LLM output helpers (migrated from viknow1).

Uses langchain's ``with_structured_output`` with the provider-native
``json_schema`` method, plus a ``json_repair`` fallback for resilient parsing
of VLM/LLM structured responses. Sync-only: callers run in tool-handler
threads, so we avoid the asyncio.run cross-loop pitfall by using ``invoke``
instead of ``ainvoke``.
"""

from __future__ import annotations

import contextlib
import json
import re
from contextvars import ContextVar
from typing import get_type_hints

from langchain_core.utils.function_calling import convert_to_openai_tool
from loguru import logger
from pydantic import BaseModel, ValidationError

from agents.runtime.agent_adapters.langfuse import get_active_tool, record_model_generation

_STRUCTURED_LLM_TRACE_COLLECTOR: ContextVar[list[dict] | None] = ContextVar(
    "viknow_structured_llm_trace_collector",
    default=None,
)
_STRUCTURED_LLM_USAGE_COLLECTOR: ContextVar[list[dict] | None] = ContextVar(
    "viknow_structured_llm_usage_collector",
    default=None,
)


@contextlib.contextmanager
def collect_structured_llm_traces():
    traces: list[dict] = []
    token = _STRUCTURED_LLM_TRACE_COLLECTOR.set(traces)
    try:
        yield traces
    finally:
        _STRUCTURED_LLM_TRACE_COLLECTOR.reset(token)


@contextlib.contextmanager
def collect_structured_llm_usage():
    usage_records: list[dict] = []
    token = _STRUCTURED_LLM_USAGE_COLLECTOR.set(usage_records)
    try:
        yield usage_records
    finally:
        _STRUCTURED_LLM_USAGE_COLLECTOR.reset(token)


def structured_llm_usage_summary(records: list[dict]) -> dict[str, object]:
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    by_model: dict[str, dict[str, int]] = {}
    calls: list[dict] = []
    for record in records:
        usage = _token_usage(record.get("usage"))
        if not _has_token_usage(usage):
            continue
        _add_token_usage(total_usage, usage)
        model = str(record.get("model") or "").strip() or "unknown"
        entry = by_model.setdefault(
            model,
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "generations": 0},
        )
        _add_token_usage(entry, usage)
        entry["generations"] += 1
        calls.append(
            {
                "run_name": str(record.get("run_name") or ""),
                "schema": str(record.get("schema") or ""),
                "model": model,
                "attempt": int(record.get("attempt") or 0),
                "usage": usage,
            }
        )
    token_usage: dict[str, object] = {}
    if _has_token_usage(total_usage):
        token_usage = {
            "totals": dict(total_usage),
            "by_model": [
                {"model": model_key, **dict(counts)}
                for model_key, counts in sorted(by_model.items())
            ],
        }
    return {"usage": total_usage, "calls": calls, "token_usage": token_usage}


def structured_llm_trace_token_usage(traces: list[dict]) -> dict[str, int]:
    """Sum token usage recorded in structured LLM diagnostic traces."""

    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for trace in traces:
        attempts = trace.get("attempts") if isinstance(trace, dict) else None
        if not isinstance(attempts, list):
            continue
        for attempt in attempts:
            if isinstance(attempt, dict):
                _add_token_usage(usage, attempt.get("usage"))
    return usage


def _coerce_str_fields(schema: type[BaseModel], obj: dict) -> dict:
    """Coerce non-str values into str for fields declared as str.

    Pydantic v2 does not implicitly convert list/int/dict into str fields.
    LLMs under json_mode may emit list/int/dict for semantically-str fields.
    Only handles top-level str fields (not Optional[str] or unions).
    """

    try:
        hints = get_type_hints(schema)
    except Exception:
        return obj
    result = dict(obj)
    for field_name, field_type in hints.items():
        if field_name not in result or field_type is not str:
            continue
        value = result[field_name]
        if isinstance(value, str):
            continue
        if isinstance(value, list):
            result[field_name] = "、".join(str(i) for i in value)
        elif isinstance(value, dict):
            logger.warning(
                f"[invoke_structured_llm] str field '{field_name}' received dict, "
                f"schema={schema.__name__}"
            )
            result[field_name] = json.dumps(value, ensure_ascii=False)
        elif value is None:
            result[field_name] = ""
        else:
            result[field_name] = str(value)
    return result


def invoke_text_llm(llm, messages: list, *, run_name: str, max_tokens: int):
    """Invoke a plain-text planning step with the tool's model observability.

    No parsing or retries are added here. The caller owns semantic validation;
    Exceptions propagate through the span. Generation recording is best effort
    and may omit failures without usage; invocation success is not validation.
    """
    from agents.runtime.core.observability.otel import ATTR_PHASE, start_span_sync

    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    success = False
    with start_span_sync(
        "model.generate",
        **{ATTR_PHASE: "model.generate", "agents.runtime.model": run_name},
    ):
        try:
            response = llm.invoke(
                messages,
                max_tokens=max_tokens,
                config={"run_name": run_name, "callbacks": []},
            )
            usage = _response_token_usage(response)
            if _has_token_usage(usage):
                _append_structured_llm_usage(
                    {
                        "run_name": run_name,
                        "schema": "",
                        "model": _llm_model_name(llm) or "",
                        "attempt": 1,
                        "usage": usage,
                    }
                )
            success = True
            return response
        finally:
            record_model_generation(
                name=run_name,
                model=_llm_model_name(llm),
                usage=usage,
                metadata={"tool": get_active_tool() or "", "invocation_success": success},
            )


def invoke_structured_llm[T: BaseModel](
    llm,
    schema: type[T],
    messages: list | str,
    *,
    run_name: str | None = None,
    inherit_callbacks: bool = False,
    max_attempts: int = 3,
    max_tokens: int | None = None,
) -> T | None:
    """Invoke an LLM with structured output and resilient JSON fallback.

    Args:
        llm: A langchain ChatModel supporting ``with_structured_output``.
        schema: Target Pydantic model for the structured output.
        messages: Either a prompt string or a list of langchain messages.
        run_name: Optional run name for tracing.
        inherit_callbacks: When true, keep the current LangChain callbacks so the
            selected internal call can be traced by the active runtime.
        max_attempts: Retry count for the structured-output call.
        max_tokens: Optional ``max_tokens`` binding.

    Returns:
        Parsed schema instance, or ``None`` if all attempts and fallbacks fail.
    """

    from agents.runtime.core.observability.otel import ATTR_PHASE, start_span_sync

    with start_span_sync(
        "model.generate",
        **{
            ATTR_PHASE: "model.generate",
            "agents.runtime.model": run_name or schema.__name__,
        },
    ):
        return _invoke_structured_llm(
            llm,
            schema,
            messages,
            run_name=run_name,
            inherit_callbacks=inherit_callbacks,
            max_attempts=max_attempts,
            max_tokens=max_tokens,
        )


def _invoke_structured_llm[T: BaseModel](
    llm,
    schema: type[T],
    messages: list | str,
    *,
    run_name: str | None = None,
    inherit_callbacks: bool = False,
    max_attempts: int = 3,
    max_tokens: int | None = None,
) -> T | None:
    trace = _structured_llm_trace(
        schema=schema,
        messages=messages,
        run_name=run_name,
        inherit_callbacks=inherit_callbacks,
    )
    _append_structured_llm_trace(trace)

    from agents.runtime.core.tools.context import remaining_run_seconds

    if remaining_run_seconds() is not None:
        llm = _without_sdk_retries(llm)
    last_raw_content: str | None = None
    method = "json_schema"
    structured_llm = _structured_llm_for_method(
        llm,
        schema,
        method=method,
        inherit_callbacks=inherit_callbacks,
        run_name=run_name,
        max_tokens=max_tokens,
    )
    for attempt in range(max_attempts):
        from agents.runtime.core.tools.context import remaining_run_seconds

        remaining = remaining_run_seconds()
        if remaining is not None:
            base = getattr(llm, "bound", llm)
            original_timeout = getattr(base, "request_timeout", None)
            if isinstance(original_timeout, (int, float)) and original_timeout > 0:
                remaining = min(remaining, original_timeout)
            structured_llm = _structured_llm_for_method(
                llm.bind(timeout=remaining),
                schema,
                method=method,
                inherit_callbacks=inherit_callbacks,
                run_name=run_name,
                max_tokens=max_tokens,
            )
        try:
            resp = structured_llm.invoke(messages)
            parsed_payload = resp.get("parsed")
            parsing_error = resp.get("parsing_error")
            parsed: T | None = None
            if parsed_payload is not None:
                try:
                    parsed = schema.model_validate(parsed_payload)
                except ValidationError as exc:
                    parsing_error = exc
            raw = resp.get("raw")
            usage = _response_token_usage(raw)
            if _has_token_usage(usage):
                _append_structured_llm_usage(
                    {
                        "run_name": run_name or "",
                        "schema": schema.__name__,
                        "model": _llm_model_name(llm) or "",
                        "attempt": attempt + 1,
                        "usage": usage,
                    }
                )
                _record_attempt_generation(
                    llm,
                    run_name=run_name,
                    schema=schema,
                    attempt=attempt + 1,
                    usage=usage,
                    success=parsed is not None,
                )
            if parsed is not None:
                attempt_trace = {"attempt": attempt + 1, "method": method, "parsed": True}
                if _has_token_usage(usage):
                    attempt_trace["usage"] = usage
                trace["attempts"].append(attempt_trace)
                trace["fallback"] = {"attempted": False, "success": False}
                trace["result"] = "parsed"
                return parsed
            raw_content = raw.content
            last_raw_content = raw_content if isinstance(raw_content, str) else str(raw_content)
            attempt_trace = {
                "attempt": attempt + 1,
                "method": method,
                "parsed": False,
                "parsing_error": _preview_text(parsing_error),
                "raw_output": _preview_text(last_raw_content),
            }
            if _has_token_usage(usage):
                attempt_trace["usage"] = usage
            trace["attempts"].append(attempt_trace)
            logger.warning(
                f"[invoke_structured_llm] method={method} "
                f"attempt {attempt + 1}/{max_attempts}: "
                f"parsing_error={parsing_error}"
            )
        except Exception as exc:
            from agents.runtime.core.model_gateway_recovery import terminal_model_failure

            if terminal_model_failure(exc):
                trace["attempts"].append(
                    {
                        "attempt": attempt + 1,
                        "method": method,
                        "parsed": False,
                        "retryable": False,
                        "exception_type": type(exc).__name__,
                    }
                )
                # The transport owns safe retries. A fresh invoke could replay an unknown outcome.
                raise
            retryable = not _is_response_format_unavailable(exc)
            trace["attempts"].append(
                {
                    "attempt": attempt + 1,
                    "method": method,
                    "parsed": False,
                    "exception": _preview_text(exc),
                    "retryable": retryable,
                }
            )
            logger.warning(
                f"[invoke_structured_llm] method={method} "
                f"attempt {attempt + 1}/{max_attempts} exception: {exc}"
            )
            if not retryable:
                break

    if not last_raw_content:
        trace["fallback"] = {"attempted": False, "success": False}
        trace["result"] = "failed"
        logger.error(f"[invoke_structured_llm] all attempts failed for schema={schema.__name__}")
        return None

    result = _json_repair_fallback(schema, last_raw_content)
    trace["fallback"] = {"attempted": True, "success": result is not None}
    trace["result"] = "fallback_parsed" if result is not None else "failed"
    return result


def _structured_llm_trace(
    *,
    schema: type[BaseModel],
    messages: list | str,
    run_name: str | None,
    inherit_callbacks: bool,
) -> dict:
    return {
        "run_name": run_name or "",
        "schema": schema.__name__,
        "inherit_callbacks": inherit_callbacks,
        "input": _preview_text(messages),
        "attempts": [],
        "fallback": {"attempted": False, "success": False},
        "result": "pending",
    }


def _structured_llm_for_method(
    llm,
    schema: type[BaseModel],
    *,
    method: str,
    inherit_callbacks: bool,
    run_name: str | None,
    max_tokens: int | None = None,
):
    provider_schema: type[BaseModel] | dict = schema
    if method == "json_schema":
        provider_schema = convert_to_openai_tool(schema)["function"]
    structured_llm = llm.with_structured_output(
        provider_schema,
        method=method,
        include_raw=True,
        # include_raw wraps the model in a RunnableMap/sequence. Binding the
        # outer runnable loses generation kwargs before they reach the model.
        **({"max_tokens": max_tokens} if max_tokens is not None else {}),
    )
    # Suppress callbacks by default so intermediate VLM traces stay private unless
    # a caller deliberately opts in for a specific internal step.
    if not inherit_callbacks:
        structured_llm = structured_llm.with_config({"callbacks": []})
    if run_name:
        structured_llm = structured_llm.with_config({"run_name": run_name})
    return structured_llm


def _append_structured_llm_trace(trace: dict) -> None:
    collector = _STRUCTURED_LLM_TRACE_COLLECTOR.get()
    if collector is not None:
        collector.append(trace)


def _append_structured_llm_usage(record: dict) -> None:
    collector = _STRUCTURED_LLM_USAGE_COLLECTOR.get()
    if collector is not None:
        collector.append(record)


def _record_attempt_generation(
    llm,
    *,
    run_name: str | None,
    schema: type[BaseModel],
    attempt: int,
    usage: dict[str, int],
    success: bool,
) -> None:
    """Best-effort write one explicit Langfuse generation per attempt."""
    tool = get_active_tool() or ""
    metadata = {
        "viknow_schema": schema.__name__,
        "tool": tool,
        "attempt": attempt,
        "success": success,
    }
    record_model_generation(
        name=f"{run_name or schema.__name__}",
        model=_llm_model_name(llm),
        usage=usage,
        metadata=metadata,
    )


def _llm_model_name(llm: object) -> str | None:
    for attr in ("model_name", "model", "deployment_name"):
        value = getattr(llm, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _response_token_usage(response: object) -> dict[str, int]:
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    usage_metadata = getattr(response, "usage_metadata", None)
    if isinstance(usage_metadata, dict):
        _add_token_usage(
            usage,
            {
                "prompt_tokens": usage_metadata.get("input_tokens"),
                "completion_tokens": usage_metadata.get("output_tokens"),
                "total_tokens": usage_metadata.get("total_tokens"),
            },
        )
    response_metadata = getattr(response, "response_metadata", None)
    if isinstance(response_metadata, dict):
        _add_token_usage(usage, response_metadata.get("token_usage"))
    return usage


def _add_token_usage(target: dict[str, int], source: object) -> None:
    source = _token_usage(source)
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        target[field] += source[field]


def _token_usage(value: object) -> dict[str, int]:
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if not isinstance(value, dict):
        return usage
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        token_count = value.get(field)
        if isinstance(token_count, int) and not isinstance(token_count, bool):
            usage[field] = token_count
    return usage


def _has_token_usage(usage: dict[str, int]) -> bool:
    return any(value > 0 for value in usage.values())


def _preview_text(value: object, *, limit: int = 4000) -> str:
    text = value if isinstance(value, str) else str(value)
    text = text.strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _is_response_format_unavailable(exc: Exception) -> bool:
    text = str(exc).lower()
    return "response_format" in text and "unavailable" in text


def _json_repair_fallback[T: BaseModel](schema: type[T], raw_content: str) -> T | None:
    """Best-effort recovery: strip reasoning tags, repair JSON, coerce fields."""

    try:
        from json_repair import repair_json
    except ImportError:
        logger.error("[invoke_structured_llm] json_repair not installed; fallback unavailable")
        return None

    try:
        clean = re.sub(
            r"<think>.*?</think>",
            "",
            raw_content,
            flags=re.DOTALL | re.IGNORECASE,
        ).strip()
        obj = repair_json(clean, return_objects=True)
    except Exception as exc:
        logger.error(f"[invoke_structured_llm] json_repair fallback failed: {exc}")
        return None

    if not isinstance(obj, dict):
        logger.error(f"[invoke_structured_llm] all attempts failed for schema={schema.__name__}")
        return None

    # First attempt: use obj as-is.
    try:
        return schema(**_coerce_str_fields(schema, obj))
    except Exception:
        pass

    # Unwrap common LLM wrapper formats like {"response": "[...]"}.
    if "response" in obj:
        inner = obj["response"]
        if isinstance(inner, str):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                inner = json.loads(inner)
        if isinstance(inner, dict):
            obj = inner
        elif isinstance(inner, list):
            # If the schema has a single list field, unwrap the list into it.
            try:
                hints = get_type_hints(schema)
                list_fields = [
                    name
                    for name, hint in hints.items()
                    if getattr(hint, "__origin__", None) is list or hint is list
                ]
                if len(list_fields) == 1:
                    obj = {list_fields[0]: inner}
                    logger.info(
                        f"[invoke_structured_llm] unwrapped response list into "
                        f"field '{list_fields[0]}'"
                    )
            except Exception:
                pass

    try:
        return schema(**_coerce_str_fields(schema, obj))
    except Exception as exc:
        logger.error(
            f"[invoke_structured_llm] all attempts failed for schema={schema.__name__}: {exc}"
        )
        return None


def _without_sdk_retries(llm):
    """Keep retries in the deadline-aware outer loop; leave shared clients intact."""
    from langchain_core.runnables import RunnableBinding
    from langchain_openai.chat_models.base import BaseChatOpenAI

    if isinstance(llm, RunnableBinding):
        return llm.model_copy(update={"bound": _without_sdk_retries(llm.bound)})
    if not isinstance(llm, BaseChatOpenAI) or llm.root_client is None:
        return llm
    client = llm.root_client.with_options(max_retries=0)
    return llm.model_copy(
        update={"max_retries": 0, "root_client": client, "client": client.chat.completions}
    )
