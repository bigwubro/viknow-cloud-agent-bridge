"""OpenTelemetry run-tree spans for a single Q&A.

The HTTP request is not the root: chat returns immediately and eval waits in a
sibling task. ``viknow3.agents.runtime.run`` is started inside the run task and is the query
key (``viknow3.agents.runtime.run_id``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, Token
from typing import Any

from loguru import logger
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import Status, StatusCode

ERROR_EVAL_POLL_TIMEOUT = "EVAL_POLL_TIMEOUT"
ERROR_HYPERRAG_TIMEOUT = "HYPERRAG_TIMEOUT"
ERROR_MODEL_SLOT_QUEUE_TIMEOUT = "MODEL_SLOT_QUEUE_TIMEOUT"
ERROR_RUNTIME_BUDGET_EXHAUSTED = "RUNTIME_BUDGET_EXHAUSTED"
ERROR_RUN_INTERRUPTED = "RUN_INTERRUPTED"
ERROR_TOOL_EXECUTION_TIMEOUT = "TOOL_EXECUTION_TIMEOUT"
ERROR_TOOL_QUEUE_TIMEOUT = "TOOL_QUEUE_TIMEOUT"

ATTR_ENTRY = "viknow3.agents.runtime.entry"
ATTR_ERROR_CODE = "viknow3.agents.runtime.error_code"
ATTR_PHASE = "viknow3.agents.runtime.phase"
ATTR_PROFILE_ID = "viknow3.agents.runtime.profile_id"
ATTR_RESOURCE = "viknow3.agents.runtime.resource"
ATTR_RUN_ID = "viknow3.agents.runtime.run_id"
ATTR_SESSION_ID = "viknow3.agents.runtime.session_id"
ATTR_TOOL = "viknow3.agents.runtime.tool"
ATTR_QUEUE_PATH = "viknow3.agents.runtime.queue_path"
ATTR_QUEUE_RANK = "viknow3.agents.runtime.queue_rank"
ATTR_ACQUIRE_REJECT = "viknow3.agents.runtime.acquire_reject"
ATTR_SERVICE = "viknow3.agents.runtime.service"

_UNKNOWN_TOOL = "unknown"
_MAX_TOOL_NAME_LEN = 128

_TRACER_NAME = "viknow3.agents.runtime.qa"
_ANCHOR_SPAN_NAMES = frozenset(
    {
        "viknow3.agents.runtime.run",
        "precompile_kb",
        "agent.loop",
        "model.generate",
        "tool.execute",
        "tool.work",
        "hyperrag.search",
        "hyperrag.discover",
        "hyperrag.rerank",
        "stop_gate",
        "answer_review",
    }
)
_EXPORTED_OPERATIONS = frozenset(
    {
        "viknow3.agents.runtime.run",
        "precompile_kb",
        "agent.loop",
        "model.wait_slot",
        "model.generate",
        "tool.wait_queue",
        "tool.execute",
        "tool.work",
        "hyperrag.search",
        "hyperrag.discover",
        "hyperrag.rerank",
        "stop_gate",
        "answer_review",
        "eval.wait_terminal",
    }
)
_DROP_OPERATIONS = frozenset(
    {
        "LangGraph",
        "POST /api/v1/eval/runs",
        "POST /api/v1/eval/runs http receive",
        "POST /api/v1/eval/runs http send",
    }
)
_RUN_ID: ContextVar[str | None] = ContextVar("viknow_otel_run_id", default=None)
_RUN_SPAN_ID: ContextVar[int | None] = ContextVar("viknow_otel_run_span_id", default=None)
_enabled = False
_PROVIDER: TracerProvider | None = None
_service_label = "viknow"
_ANCHOR_CONTEXT: dict[str, list[Any]] = {}


class _ServiceLabelSpanProcessor(SpanProcessor):
    """Stamp ``viknow3.agents.runtime.service`` on every span for spanmetrics / Jaeger tag search."""

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        if span.is_recording():
            span.set_attribute(ATTR_SERVICE, _service_label)

    def on_end(self, span: Any) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class _RunIdSpanProcessor(SpanProcessor):
    """Stamp every span with the active run id so Jaeger can filter httpx/redis."""

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        run_id = _RUN_ID.get()
        attributes = span.attributes or {}
        if run_id and span.is_recording() and ATTR_RUN_ID not in attributes:
            span.set_attribute(ATTR_RUN_ID, run_id)

    def on_end(self, span: Any) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class _AllowlistExportProcessor(SpanProcessor):
    """Export only the Q&A phase tree so Jaeger Operation stays on viknow3.agents.runtime.run."""

    def __init__(self, downstream: SpanProcessor) -> None:
        self._downstream = downstream

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        self._downstream.on_start(span, parent_context)

    def on_end(self, span: Any) -> None:
        if _should_export_span(span):
            self._downstream.on_end(span)

    def shutdown(self) -> None:
        self._downstream.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._downstream.force_flush(timeout_millis)


def is_enabled() -> bool:
    return _enabled


def normalize_tool_name(value: object | None) -> str:
    """Return a stable, non-empty tool label for span tags and spanmetrics."""
    if value is None:
        return _UNKNOWN_TOOL
    text = value if isinstance(value, str) else str(value)
    name = text.strip()
    if not name:
        return _UNKNOWN_TOOL
    return name[:_MAX_TOOL_NAME_LEN]


def tool_name_from_metadata(metadata: object | None) -> str:
    """Resolve ``viknow3.agents.runtime.tool`` from ExecUnit metadata."""
    if not isinstance(metadata, dict):
        return _UNKNOWN_TOOL
    for key in ("tool_name", "tool_id", "name"):
        raw = metadata.get(key)
        if isinstance(raw, str) and raw.strip():
            return normalize_tool_name(raw)
    return _UNKNOWN_TOOL


def setup_otel(
    config: Any,
    *,
    app: Any | None = None,
    exporter: SpanExporter | None = None,
    instrument_libraries: bool = True,
    simple_export: bool = False,
) -> TracerProvider | None:
    """Initialize the process tracer. No-op when the config switch is off."""

    global _PROVIDER, _enabled, _service_label
    if not config.is_enabled():
        _enabled = False
        return None
    if _PROVIDER is not None:
        _enabled = True
        _service_label = config.resolve_service_name()
        return _PROVIDER

    service_name = config.resolve_service_name()
    _service_label = service_name
    endpoint = config.resolve_exporter_endpoint()
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(_ServiceLabelSpanProcessor())
    provider.add_span_processor(_RunIdSpanProcessor())
    resolved_exporter = exporter
    if resolved_exporter is None:
        if not endpoint:
            raise RuntimeError(
                "OpenTelemetry is enabled but VIKNOW_OTEL_EXPORTER_ENDPOINT is empty"
            )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        traces_endpoint = (
            endpoint
            if endpoint.rstrip("/").endswith("/v1/traces")
            else endpoint.rstrip("/") + "/v1/traces"
        )
        resolved_exporter = OTLPSpanExporter(endpoint=traces_endpoint)
    if simple_export:
        export_processor: SpanProcessor = SimpleSpanProcessor(resolved_exporter)
    else:
        export_processor = BatchSpanProcessor(resolved_exporter, schedule_delay_millis=1000)
    provider.add_span_processor(_AllowlistExportProcessor(export_processor))
    # Keep Jaeger export on a process-local provider. Langfuse SDK v3 also uses OTel
    # and, when a global TracerProvider is already set, attaches LangfuseSpanProcessor
    # to that same provider — which would mirror viknow3.agents.runtime.run / tool.work into Langfuse.
    _PROVIDER = provider
    _enabled = True
    if instrument_libraries:
        _instrument_detail_libraries()
    _ = app
    logger.info(
        "[otel] enabled service={} endpoint={}",
        service_name,
        endpoint or "in-memory",
    )
    return provider


def shutdown_otel() -> None:
    global _PROVIDER, _enabled, _service_label
    provider = _PROVIDER
    _PROVIDER = None
    _enabled = False
    _service_label = "viknow"
    _ANCHOR_CONTEXT.clear()
    if provider is not None:
        provider.shutdown()


def force_flush() -> None:
    provider = _PROVIDER
    if provider is not None:
        provider.force_flush()


def _tracer() -> Any:
    provider = _PROVIDER
    if provider is not None:
        return provider.get_tracer(_TRACER_NAME)
    return trace.get_tracer(_TRACER_NAME)


def record_error(code: str, description: str | None = None) -> None:
    span = trace.get_current_span()
    _stamp_span_error(span, code, description)
    run_id = _RUN_ID.get()
    stack = _ANCHOR_CONTEXT.get(run_id) if run_id else None
    if not stack:
        return
    run_span = trace.get_current_span(stack[0])
    if run_span is not None and run_span is not span:
        _stamp_span_error(run_span, code, description)


def _stamp_span_error(span: Any, code: str, description: str | None) -> None:
    if span is None or not span.is_recording():
        return
    span.set_attribute(ATTR_ERROR_CODE, code)
    span.set_status(Status(StatusCode.ERROR, description or code))


@asynccontextmanager
async def start_run_span(
    *,
    run_id: str,
    session_id: str,
    entry: str,
    profile_id: str | None = None,
) -> AsyncIterator[None]:
    token = _RUN_ID.set(run_id)
    span_token: Token[int | None] | None = None
    try:
        async with start_span(
            "viknow3.agents.runtime.run",
            **{
                ATTR_RUN_ID: run_id,
                ATTR_SESSION_ID: session_id,
                ATTR_ENTRY: entry,
                ATTR_PROFILE_ID: profile_id,
                ATTR_PHASE: "run",
            },
        ) as run_span:
            if run_span is not None:
                span_token = _RUN_SPAN_ID.set(run_span.get_span_context().span_id)
            yield
    finally:
        if span_token is not None:
            _RUN_SPAN_ID.reset(span_token)
        force_flush()
        _RUN_ID.reset(token)


@asynccontextmanager
async def start_span(name: str, **attributes: Any) -> AsyncIterator[Any]:
    if not is_enabled():
        yield None
        return
    run_id = _span_run_id(attributes)
    tracer = _tracer()
    with tracer.start_as_current_span(name, context=_explicit_parent(run_id)) as span:
        _apply_attributes(span, attributes)
        popped = _push_anchor(name, run_id)
        try:
            yield span
        finally:
            if popped:
                _pop_anchor(run_id)


@contextmanager
def start_span_sync(name: str, **attributes: Any) -> Iterator[Any]:
    if not is_enabled():
        yield None
        return
    run_id = _span_run_id(attributes)
    tracer = _tracer()
    with tracer.start_as_current_span(name, context=_explicit_parent(run_id)) as span:
        _apply_attributes(span, attributes)
        popped = _push_anchor(name, run_id)
        try:
            yield span
        finally:
            if popped:
                _pop_anchor(run_id)


def begin_span(name: str, **attributes: Any) -> Any:
    """Start a span that stays a child of the current (or run) parent.

    Used for agent-loop events: start/end are not a single ``with`` block.
    ``SpanSet`` attaches the span as OTel current context so HTTP auto-
    instrumentation nests under ``model.generate`` / ``tool.execute``.
    """

    if not is_enabled():
        return None
    run_id = _span_run_id(attributes)
    span = _tracer().start_span(name, context=_explicit_parent(run_id))
    _apply_attributes(span, attributes)
    return span


def end_span(span: Any, *, error_code: str | None = None) -> None:
    if span is None:
        return
    if error_code:
        span.set_attribute(ATTR_ERROR_CODE, error_code)
        span.set_status(Status(StatusCode.ERROR, error_code))
    span.end()


class SpanSet:
    """Manual spans keyed by LangChain ``run_id`` (overlapping model/tool calls)."""

    def __init__(self) -> None:
        self._open: dict[str, Any] = {}
        self._anchors: dict[str, str] = {}
        self._context_tokens: dict[str, Token[Any]] = {}

    def start(self, key: str, name: str, **attributes: Any) -> None:
        if not key:
            return
        self.end(key)
        span = begin_span(name, **attributes)
        self._open[key] = span
        if span is not None:
            self._context_tokens[key] = otel_context.attach(trace.set_span_in_context(span))
        run_id = _span_run_id(attributes)
        if span is not None and name in _ANCHOR_SPAN_NAMES and run_id:
            _ANCHOR_CONTEXT.setdefault(run_id, []).append(trace.set_span_in_context(span))
            self._anchors[key] = run_id

    def end(self, key: str, *, error_code: str | None = None) -> None:
        token = self._context_tokens.pop(key, None)
        if token is not None:
            otel_context.detach(token)
        run_id = self._anchors.pop(key, None)
        if run_id is not None:
            _pop_anchor(run_id)
        end_span(self._open.pop(key, None), error_code=error_code)

    def close(self) -> None:
        for key in list(self._open):
            self.end(key)


def bind_run_id(run_id: str) -> Token[str | None]:
    return _RUN_ID.set(run_id)


def reset_run_id(token: Token[str | None]) -> None:
    _RUN_ID.reset(token)


def _span_run_id(attributes: dict[str, Any]) -> str | None:
    raw = attributes.get(ATTR_RUN_ID) or _RUN_ID.get()
    if raw is None:
        return None
    run_id = str(raw).strip()
    return run_id or None


def _explicit_parent(run_id: str | None) -> Any | None:
    """Always hang new spans under the innermost open phase.

    Scheduler workers often still have ``viknow3.agents.runtime.run`` as the current span
    (same trace, copied before ``agent.loop`` started). Treating "same
    trace" as good enough made ``model.wait_slot`` a sibling of the loop.
    """

    stack = _ANCHOR_CONTEXT.get(run_id) if run_id else None
    return stack[-1] if stack else None


def _push_anchor(name: str, run_id: str | None) -> bool:
    if name not in _ANCHOR_SPAN_NAMES or not run_id:
        return False
    _ANCHOR_CONTEXT.setdefault(run_id, []).append(otel_context.get_current())
    return True


def _pop_anchor(run_id: str | None) -> None:
    if not run_id:
        return
    stack = _ANCHOR_CONTEXT.get(run_id)
    if not stack:
        return
    stack.pop()
    if not stack:
        _ANCHOR_CONTEXT.pop(run_id, None)


def _apply_attributes(span: Any, attributes: dict[str, Any]) -> None:
    if span is None or not span.is_recording():
        return
    for key, value in attributes.items():
        if value is None or value == "":
            continue
        span.set_attribute(key, value)


def _should_export_span(span: Any) -> bool:
    name = span.name or ""
    if name in _DROP_OPERATIONS or name.startswith("POST /api/"):
        return False
    if name in _EXPORTED_OPERATIONS:
        return True
    attributes = span.attributes or {}
    if attributes.get("db.system"):
        return False
    is_detail = bool(attributes.get("http.method") or attributes.get("http.request.method"))
    if not is_detail:
        return False
    parent = span.parent
    run_span_id = _RUN_SPAN_ID.get()
    return not (parent is not None and run_span_id is not None and parent.span_id == run_span_id)


def _instrument_detail_libraries() -> None:
    """Record outbound HTTP under the current phase span.

    Redis/Postgres command spans are not instrumented: scheduler Redis and
    session/event persistence would flood phase trees (answer_review, loop).
    """

    provider = _PROVIDER
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    except Exception:
        logger.opt(exception=True).warning("[otel] httpx instrumentation failed")
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        RequestsInstrumentor().instrument(tracer_provider=provider)
    except Exception:
        logger.opt(exception=True).warning("[otel] requests instrumentation failed")
