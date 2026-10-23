"""Langfuse tracing for HyperRAG index jobs (trace name ``index``).

Independent of DeepAgents / LangChain callbacks. HyperRAG registers hooks via
``hyperrag.observability.index_usage.set_hooks`` at adapter startup.
"""

from __future__ import annotations

import contextlib
import os
import threading
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

_INDEX_TRACE_NAME = "index"
_ENABLED: bool | None = None
_CLIENT: Any | None = None
_CLIENT_LOCK = threading.Lock()
_JOBS: dict[str, _IndexJobState] = {}
_JOBS_LOCK = threading.Lock()

_ACTIVE_JOB_ID: ContextVar[str | None] = ContextVar("viknow_index_job_id", default=None)
_ACTIVE_TASK: ContextVar[_IndexTaskScope | None] = ContextVar(
    "viknow_index_task_scope",
    default=None,
)


@dataclass
class _IndexTaskScope:
    job_id: str
    task_id: str
    task_type: str
    span: Any | None = None
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)

    def add_usage(self, model: str | None, usage: dict[str, Any] | None) -> None:
        counts = _normalize_usage(usage)
        if not _has_tokens(counts):
            return
        key = (model or "").strip() or "unknown"
        entry = self.by_model.setdefault(key, _zero_counts())
        for field_name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            entry[field_name] += counts[field_name]
        entry["generations"] += 1

    def token_usage(self) -> dict[str, Any] | None:
        if not self.by_model:
            return None
        totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        by_model: list[dict[str, Any]] = []
        for model_key in sorted(self.by_model):
            entry = dict(self.by_model[model_key])
            by_model.append({"model": model_key, **entry})
            for field_name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                totals[field_name] += entry[field_name]
        return {"totals": totals, "by_model": by_model}


@dataclass
class _IndexJobState:
    job_id: str
    trace_id: str
    root: Any
    metadata: dict[str, Any]
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    task_token_usages: list[dict[str, Any]] = field(default_factory=list)

    def merge_task_usage(self, task_type: str, task_id: str, usage: dict[str, Any]) -> None:
        self.task_token_usages.append(
            {"task_type": task_type, "task_id": task_id, "token_usage": usage}
        )
        for item in usage.get("by_model") or []:
            if not isinstance(item, dict):
                continue
            model_key = str(item.get("model") or "unknown")
            entry = self.by_model.setdefault(model_key, _zero_counts())
            for field_name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                entry[field_name] += int(item.get(field_name) or 0)
            entry["generations"] += int(item.get("generations") or 0)

    def breakdown(self) -> dict[str, Any] | None:
        if not self.by_model:
            return None
        totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        by_model: list[dict[str, Any]] = []
        for model_key in sorted(self.by_model):
            entry = dict(self.by_model[model_key])
            by_model.append({"model": model_key, **entry})
            for field_name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                totals[field_name] += entry[field_name]
        return {"totals": totals, "by_model": by_model}


def configure_index_tracing(*, enabled: bool | None = None) -> None:
    """Enable/disable index Langfuse tracing (None = follow VIKNOW_LANGFUSE_ENABLED)."""
    global _ENABLED
    _ENABLED = enabled


def is_index_tracing_enabled() -> bool:
    if _ENABLED is not None:
        return bool(_ENABLED)
    value = os.getenv("VIKNOW_LANGFUSE_ENABLED")
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


class IndexTracingHooks:
    """Hooks registered into HyperRAG ``observability.index_usage``."""

    def on_job_submitted(self, job: Any) -> None:
        on_index_job_submitted(job)

    def on_task_start(self, task: Any) -> Any:
        return on_index_task_start(task)

    def on_task_end(self, task: Any, scope_token: Any = None) -> None:
        on_index_task_end(task, scope_token)

    def on_job_finished(self, job: Any) -> None:
        on_index_job_finished(job)

    def record_generation(
        self,
        *,
        name: str,
        model: str | None = None,
        usage: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        record_index_generation(name=name, model=model, usage=usage, metadata=metadata)
        try:
            from viknow.agent_adapters.langfuse import record_model_generation

            record_model_generation(
                name=name,
                model=model,
                usage=usage,
                metadata=metadata,
            )
        except Exception:
            logger.opt(exception=True).warning(
                "[index-langfuse] forward record_generation to tool usage failed"
            )


def install_index_tracing_hooks() -> None:
    """Register hooks into HyperRAG (no-op if HyperRAG unavailable)."""
    try:
        from hyperrag.observability.index_usage import set_hooks
    except ImportError:
        logger.debug("[index-langfuse] hyperrag.observability.index_usage unavailable")
        return
    set_hooks(IndexTracingHooks())
    logger.info("[index-langfuse] hooks installed (enabled={})", is_index_tracing_enabled())


def on_index_job_submitted(job: Any) -> None:
    if not is_index_tracing_enabled():
        return
    try:
        job_id = str(getattr(job, "job_id", "") or "").strip()
        if not job_id:
            return
        client = _langfuse_client()
        if client is None:
            return
        metadata = {
            "viknow_job_id": job_id,
            "library_id": str(getattr(job, "workspace", "") or ""),
            "workspace": str(getattr(job, "workspace", "") or ""),
            "asset_id": str(getattr(job, "asset_id", "") or ""),
            "job_name": str(getattr(job, "job_name", "") or getattr(job, "name", "") or ""),
        }
        job_metadata = getattr(job, "metadata", None)
        if isinstance(job_metadata, dict):
            request_id = str(job_metadata.get("_request_id") or "").strip()
            if request_id:
                metadata["viknow_request_id"] = request_id
            for field_name in (
                "source_type",
                "source_space_id",
                "source_node_token",
                "source_url",
                "source_id",
                "sync_run_id",
                "sync_trigger",
            ):
                value = job_metadata.get(field_name)
                if value is not None and str(value).strip():
                    metadata[field_name] = str(value).strip()
        trace_id = client.create_trace_id()
        root = client.start_observation(
            trace_context={"trace_id": trace_id},
            name=_INDEX_TRACE_NAME,
            as_type="span",
            metadata=metadata,
        )
        update_trace = getattr(root, "update_trace", None)
        if callable(update_trace):
            update_trace(name=_INDEX_TRACE_NAME, metadata=metadata)
        with _JOBS_LOCK:
            _JOBS[job_id] = _IndexJobState(
                job_id=job_id,
                trace_id=trace_id,
                root=root,
                metadata=metadata,
            )
    except Exception:
        logger.opt(exception=True).warning("[index-langfuse] on_job_submitted failed")


def on_index_task_start(task: Any) -> Token | None:
    if not is_index_tracing_enabled():
        return None
    try:
        job_id = str(getattr(task, "job_id", "") or "").strip()
        task_id = str(getattr(task, "task_id", "") or "").strip()
        task_type = str(getattr(task, "task_type", "") or "").strip() or "task"
        if not job_id or not task_id:
            return None
        with _JOBS_LOCK:
            job_state = _JOBS.get(job_id)
        span = None
        if job_state is not None:
            start_observation = getattr(job_state.root, "start_observation", None)
            meta = {
                "viknow_job_id": job_id,
                "viknow_task_id": task_id,
                "task_type": task_type,
                "asset_id": str(getattr(task, "asset_id", "") or ""),
            }
            if callable(start_observation):
                span = start_observation(
                    name=task_type,
                    as_type="span",
                    metadata=meta,
                )
            else:
                client = _langfuse_client()
                if client is not None:
                    span = client.start_observation(
                        trace_context={
                            "trace_id": job_state.trace_id,
                            "parent_span_id": str(getattr(job_state.root, "id", "") or ""),
                        },
                        name=task_type,
                        as_type="span",
                        metadata=meta,
                    )
        scope = _IndexTaskScope(
            job_id=job_id,
            task_id=task_id,
            task_type=task_type,
            span=span,
        )
        job_token = _ACTIVE_JOB_ID.set(job_id)
        task_token = _ACTIVE_TASK.set(scope)
        return (job_token, task_token)
    except Exception:
        logger.opt(exception=True).warning("[index-langfuse] on_task_start failed")
        return None


def on_index_task_end(task: Any, scope_token: Any = None) -> None:
    if not is_index_tracing_enabled():
        return
    try:
        scope = _ACTIVE_TASK.get()
        token_usage = scope.token_usage() if scope is not None else None
        if scope is not None and scope.span is not None:
            update = getattr(scope.span, "update", None)
            end = getattr(scope.span, "end", None)
            if callable(update):
                task_status = getattr(task, "status", None)
                task_status = getattr(task_status, "value", task_status)
                task_metadata: dict[str, Any] = {
                    "task_status": str(task_status or "unknown"),
                }
                if token_usage:
                    task_metadata["token_usage"] = token_usage
                error_message = str(getattr(task, "error_message", "") or "").strip()
                if error_message:
                    task_metadata["error_message"] = error_message
                started_at = getattr(task, "started_at", None)
                completed_at = getattr(task, "completed_at", None)
                if started_at is not None and completed_at is not None:
                    task_metadata["duration_seconds"] = max(
                        0.0,
                        float(completed_at) - float(started_at),
                    )
                update(metadata=task_metadata)
            if callable(end):
                end()
        if scope is not None and token_usage:
            with _JOBS_LOCK:
                job_state = _JOBS.get(scope.job_id)
                if job_state is not None:
                    job_state.merge_task_usage(scope.task_type, scope.task_id, token_usage)
        if isinstance(scope_token, tuple) and len(scope_token) == 2:
            _ACTIVE_TASK.reset(scope_token[1])
            _ACTIVE_JOB_ID.reset(scope_token[0])
        elif scope_token is not None:
            # Best-effort reset when caller only passes one token.
            with contextlib.suppress(Exception):
                _ACTIVE_TASK.reset(scope_token)
    except Exception:
        logger.opt(exception=True).warning("[index-langfuse] on_task_end failed")


def on_index_job_finished(job: Any) -> None:
    if not is_index_tracing_enabled():
        return
    try:
        job_id = str(getattr(job, "job_id", "") or "").strip()
        if not job_id:
            return
        with _JOBS_LOCK:
            job_state = _JOBS.pop(job_id, None)
        if job_state is None:
            return
        breakdown = job_state.breakdown()
        metadata = dict(job_state.metadata)
        job_status = getattr(job, "status", None)
        job_status = getattr(job_status, "value", job_status)
        metadata["job_status"] = str(job_status or "unknown")
        error_message = str(getattr(job, "error_message", "") or "").strip()
        if error_message:
            metadata["error_message"] = error_message
        if breakdown:
            metadata["viknow_usage_breakdown"] = breakdown
            metadata["usage_finalized"] = True
        update_trace = getattr(job_state.root, "update_trace", None)
        if callable(update_trace):
            update_trace(name=_INDEX_TRACE_NAME, metadata=metadata)
        end = getattr(job_state.root, "end", None)
        if callable(end):
            end()
        client = _langfuse_client()
        flush = getattr(client, "flush", None) if client is not None else None
        if callable(flush):
            flush()
        from viknow.core.usage.pending_store import (
            empty_usage_snapshot,
            register_pending_usage_push,
        )

        if job_state.trace_id:
            register_pending_usage_push(
                kind="job",
                source_id=job_id,
                langfuse_trace_id=job_state.trace_id,
                snapshot=breakdown or empty_usage_snapshot(),
            )
    except Exception:
        logger.opt(exception=True).warning("[index-langfuse] on_job_finished failed")


def record_index_generation(
    *,
    name: str,
    model: str | None = None,
    usage: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record one model generation into the active index task (best-effort)."""
    if not is_index_tracing_enabled():
        return
    try:
        counts = _normalize_usage(usage)
        if not _has_tokens(counts):
            return
        scope = _ACTIVE_TASK.get()
        if scope is None:
            return
        scope.add_usage(model, counts)
        span = scope.span
        if span is None:
            return
        usage_details = {
            key: value
            for key, value in {
                "input": counts["prompt_tokens"],
                "output": counts["completion_tokens"],
                "total": counts["total_tokens"],
            }.items()
            if value
        }
        meta = dict(metadata or {})
        meta.setdefault("viknow_job_id", scope.job_id)
        meta.setdefault("viknow_task_id", scope.task_id)
        meta.setdefault("task_type", scope.task_type)
        start_observation = getattr(span, "start_observation", None)
        if not callable(start_observation):
            return
        generation = start_observation(
            name=name,
            as_type="generation",
            model=(model or "").strip() or None,
            usage_details=usage_details or None,
            metadata=meta,
        )
        end = getattr(generation, "end", None)
        if callable(end):
            end()
    except Exception:
        logger.opt(exception=True).warning("[index-langfuse] record_index_generation failed")


def usage_from_openai_response(response: Any) -> dict[str, int] | None:
    """Extract prompt/completion/total tokens from an OpenAI-compatible response."""
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None
    if isinstance(usage, dict):
        prompt = usage.get("prompt_tokens")
        if prompt is None:
            prompt = usage.get("input_tokens")
        completion = usage.get("completion_tokens")
        if completion is None:
            completion = usage.get("output_tokens")
        total = usage.get("total_tokens")
    else:
        prompt = getattr(usage, "prompt_tokens", None)
        if prompt is None:
            prompt = getattr(usage, "input_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
        if completion is None:
            completion = getattr(usage, "output_tokens", None)
        total = getattr(usage, "total_tokens", None)
    counts = {
        "prompt_tokens": _int_count(prompt),
        "completion_tokens": _int_count(completion),
        "total_tokens": _int_count(total),
    }
    if not counts["total_tokens"]:
        counts["total_tokens"] = counts["prompt_tokens"] + counts["completion_tokens"]
    return counts if _has_tokens(counts) else None


def _langfuse_client() -> Any | None:
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            return _CLIENT
        try:
            from viknow.agent_adapters.langfuse import (
                build_langfuse_client,
                configure_langfuse_sdk_env,
            )

            configure_langfuse_sdk_env()
            _CLIENT = build_langfuse_client()
            return _CLIENT
        except Exception:
            logger.opt(exception=True).warning("[index-langfuse] failed to build Langfuse client")
            return None


def _zero_counts() -> dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "generations": 0}


def _normalize_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    if not isinstance(usage, dict):
        return _zero_counts()
    prompt = usage.get("prompt_tokens")
    if prompt is None:
        prompt = usage.get("input_tokens")
    completion = usage.get("completion_tokens")
    if completion is None:
        completion = usage.get("output_tokens")
    total = usage.get("total_tokens")
    counts = {
        "prompt_tokens": _int_count(prompt),
        "completion_tokens": _int_count(completion),
        "total_tokens": _int_count(total),
    }
    if not counts["total_tokens"]:
        counts["total_tokens"] = counts["prompt_tokens"] + counts["completion_tokens"]
    return counts


def _has_tokens(counts: dict[str, int]) -> bool:
    return bool(
        counts.get("prompt_tokens") or counts.get("completion_tokens") or counts.get("total_tokens")
    )


def _int_count(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0
