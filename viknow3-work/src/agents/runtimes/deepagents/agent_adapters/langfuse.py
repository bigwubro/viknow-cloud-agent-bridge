"""Langfuse tracing integration for agent adapters."""

from __future__ import annotations

import json
import os
import re
import threading
from collections import deque
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from agents.runtimes.deepagents.core.config import LangfuseTracingConfig

_LANGFUSE_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_TRACE_METADATA_SOURCES: dict[str, Callable[[], dict[str, Any]]] = {}

_ACTIVE_GENERATION_RECORDER: ContextVar[GenerationRecorder | None] = ContextVar(
    "viknow_active_generation_recorder",
    default=None,
)
_ACTIVE_TOOL_CONTEXT: ContextVar[ToolUsageScope | None] = ContextVar(
    "viknow_active_tool",
    default=None,
)
_PENDING_TOOL_TOKEN_USAGE: deque[tuple[str, dict[str, Any]]] = deque()
_PENDING_TOOL_TOKEN_USAGE_LOCK = threading.Lock()


@dataclass
class ToolUsageScope:
    """Accumulates model usage for one tool invocation lifecycle."""

    name: str
    _by_model: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, model: str | None, usage: dict[str, Any] | None) -> None:
        counts = _normalize_usage_counts(usage or {})
        if not _has_token_count(counts):
            return
        model_key = _model_key(model)
        entry = self._by_model.setdefault(model_key, _zero_usage_counts())
        for field_name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            entry[field_name] += counts[field_name]
        entry["generations"] += 1

    def breakdown(self) -> dict[str, Any]:
        if not self._by_model:
            return {}
        totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        by_model: list[dict[str, Any]] = []
        for model_key in sorted(self._by_model):
            entry = dict(self._by_model[model_key])
            by_model.append({"model": model_key, **entry})
            for field_name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                totals[field_name] += entry[field_name]
        return {"totals": totals, "by_model": by_model}


def register_trace_metadata_source(
    run_id: str,
    source: Callable[[], dict[str, Any]],
) -> None:
    _TRACE_METADATA_SOURCES[run_id] = source


def unregister_trace_metadata_source(run_id: str) -> None:
    _TRACE_METADATA_SOURCES.pop(run_id, None)


def resolve_trace_metadata(run_id: str) -> dict[str, Any]:
    source = _TRACE_METADATA_SOURCES.get(run_id)
    if source is None:
        return {}
    metadata = source()
    return metadata if isinstance(metadata, dict) else {}


class GenerationRecorder:
    """Per-run collector and writer of explicit model-usage generations.

    Accumulates token usage by model name. When a Langfuse callback handler is
    attached, ``record`` also writes one ``generation`` observation to the run's
    trace (no input/output payload by default). All Langfuse side effects are
    best-effort: a failure must never affect the caller path.
    """

    def __init__(self, viknow_run_id: str, handler: Any | None = None) -> None:
        self.viknow_run_id = viknow_run_id
        self._handler = handler
        self._by_model: dict[str, dict[str, int]] = {}

    @property
    def handler(self) -> Any | None:
        return self._handler

    def record(
        self,
        *,
        name: str,
        model: str | None,
        usage: dict[str, Any] | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Accumulate usage and best-effort write one Langfuse generation."""
        counts = _normalize_usage_counts(usage or {})
        if not _has_token_count(counts):
            return
        self._accumulate(model, counts)
        tool_scope = _ACTIVE_TOOL_CONTEXT.get()
        if tool_scope is not None:
            tool_scope.add(model, counts)
        if self._handler is None:
            return
        try:
            metadata = dict(metadata or {})
            metadata["viknow_run_id"] = self.viknow_run_id
            self._handler.record_generation(
                viknow_run_id=self.viknow_run_id,
                name=name,
                model=_clean_model_name(model),
                usage=counts,
                metadata=metadata,
            )
        except Exception:
            logger.opt(exception=True).warning(
                "[langfuse] failed to record explicit generation for run {}",
                self.viknow_run_id,
            )

    def add_usage(self, model: str | None, usage: dict[str, Any] | None) -> None:
        """Accumulate usage without writing a Langfuse observation.

        Used for calls that are already traced by LangChain callbacks
        (e.g. the main agent model), to keep the run breakdown complete.
        """
        counts = _normalize_usage_counts(usage or {})
        if _has_token_count(counts):
            self._accumulate(model, counts)

    def _accumulate(self, model: str | None, counts: dict[str, int]) -> None:
        model_key = _model_key(model)
        entry = self._by_model.setdefault(model_key, _zero_usage_counts())
        for field_name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            entry[field_name] += counts[field_name]
        entry["generations"] += 1

    def breakdown(self) -> dict[str, Any]:
        """Return ``{"totals": ..., "by_model": [...]}`` or ``{}`` when empty."""
        if not self._by_model:
            return {}
        totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        by_model: list[dict[str, Any]] = []
        for model_key in sorted(self._by_model):
            entry = dict(self._by_model[model_key])
            by_model.append({"model": model_key, **entry})
            for field_name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                totals[field_name] += entry[field_name]
        return {"totals": totals, "by_model": by_model}

    def trace_id(self) -> str:
        """Best-effort resolve the Langfuse trace id for this run."""
        if self._handler is None:
            return ""
        resolve = getattr(self._handler, "resolved_langfuse_trace_id", None)
        if callable(resolve):
            try:
                return str(resolve(self.viknow_run_id) or "").strip()
            except Exception:
                return ""
        return ""


def set_active_generation_recorder(recorder: GenerationRecorder | None) -> Token:
    return _ACTIVE_GENERATION_RECORDER.set(recorder)


def reset_active_generation_recorder(token: Token) -> None:
    _ACTIVE_GENERATION_RECORDER.reset(token)


def get_active_generation_recorder() -> GenerationRecorder | None:
    return _ACTIVE_GENERATION_RECORDER.get()


def set_active_tool(name: str | None) -> Token:
    """Begin a tool-lifecycle usage scope (context-scoped) for explicit generations."""
    scope = ToolUsageScope(name=name) if name else None
    return _ACTIVE_TOOL_CONTEXT.set(scope)


def reset_active_tool(token: Token) -> None:
    _ACTIVE_TOOL_CONTEXT.reset(token)


def get_active_tool() -> str | None:
    scope = _ACTIVE_TOOL_CONTEXT.get()
    return scope.name if scope is not None else None


def get_active_tool_scope() -> ToolUsageScope | None:
    return _ACTIVE_TOOL_CONTEXT.get()


def publish_pending_tool_token_usage(tool_name: str, token_usage: dict[str, Any]) -> None:
    """Hand off tool lifecycle token_usage for the matching on_tool_end callback."""
    if not tool_name or not token_usage:
        return
    with _PENDING_TOOL_TOKEN_USAGE_LOCK:
        _PENDING_TOOL_TOKEN_USAGE.append((tool_name, dict(token_usage)))


def take_pending_tool_token_usage(tool_name: str | None) -> dict[str, Any] | None:
    """Pop the oldest pending token_usage for ``tool_name`` (FIFO)."""
    if not tool_name:
        return None
    with _PENDING_TOOL_TOKEN_USAGE_LOCK:
        for index, (pending_name, token_usage) in enumerate(_PENDING_TOOL_TOKEN_USAGE):
            if pending_name == tool_name:
                del _PENDING_TOOL_TOKEN_USAGE[index]
                return token_usage
    return None


def record_model_generation(
    *,
    name: str,
    model: str | None = None,
    usage: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Best-effort record model usage for the active agent run / tool lifecycle.

    When a run-level ``GenerationRecorder`` is active, usage is accumulated for
    trace breakdown and mirrored into the active ``ToolUsageScope``. When only a
    tool scope is active (e.g. HyperRAG retrieval inside ``knowledge_search``),
    usage is still accumulated for tool metadata. Never raises into callers.
    """
    counts = _normalize_usage_counts(usage or {})
    if not _has_token_count(counts):
        return
    recorder = _ACTIVE_GENERATION_RECORDER.get()
    if recorder is not None:
        try:
            recorder.record(name=name, model=model, usage=counts, metadata=metadata)
        except Exception:
            logger.opt(exception=True).warning("[langfuse] record_model_generation failed")
        return
    tool_scope = _ACTIVE_TOOL_CONTEXT.get()
    if tool_scope is not None:
        tool_scope.add(model, counts)


def build_langfuse_callbacks(config: LangfuseTracingConfig) -> list[Any]:
    """Build LangChain callbacks for Langfuse tracing."""

    if not _is_enabled(config):
        return []

    configure_langfuse_sdk_env()
    try:
        from langfuse.langchain import CallbackHandler
    except ImportError as exc:
        raise RuntimeError(
            "langfuse is required when observability.langfuse.enabled is true"
        ) from exc
    return [_ViKnowLangfuseCallbackHandler(CallbackHandler)]


class _ViKnowLangfuseCallbackHandler:
    """Root-trace metadata updater for Langfuse LangChain callbacks."""

    def __new__(cls, base_handler: type[Any]) -> Any:
        class ViKnowLangfuseCallbackHandler(base_handler):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs.setdefault("update_trace", True)
                super().__init__(*args, **kwargs)
                self._root_viknow_run_ids: dict[Any, str] = {}
                self._root_run_ids_by_viknow_run_id: dict[str, Any] = {}
                self._root_run_ids_by_viknow_run_id_all: dict[str, list[Any]] = {}
                self._root_trace_metadata: dict[Any, dict[str, Any]] = {}
                self._trace_metadata_by_viknow_run_id: dict[str, dict[str, Any]] = {}
                self._resolved_trace_ids_by_viknow_run_id: dict[str, list[str]] = {}
                self._root_observation_ids_by_viknow_run_id: dict[str, str] = {}
                # (tool_name, observation, langchain_run_id) — mirrors active TOOL callbacks.
                self._active_tool_observation_stack: list[tuple[str, Any, Any]] = []

            def on_chain_start(
                self,
                serialized: dict[str, Any],
                inputs: dict[str, Any],
                *,
                run_id: Any,
                parent_run_id: Any | None = None,
                tags: list[str] | None = None,
                metadata: dict[str, Any] | None = None,
                **kwargs: Any,
            ) -> Any:
                if parent_run_id is None and isinstance(metadata, dict):
                    viknow_run_id = str(metadata.get("viknow_run_id") or "").strip()
                    if viknow_run_id:
                        self._root_viknow_run_ids[run_id] = viknow_run_id
                        self._root_run_ids_by_viknow_run_id[viknow_run_id] = run_id
                        root_run_ids = self._root_run_ids_by_viknow_run_id_all.setdefault(
                            viknow_run_id, []
                        )
                        if run_id not in root_run_ids:
                            root_run_ids.append(run_id)
                        self._root_trace_metadata[run_id] = dict(metadata)
                        self._trace_metadata_by_viknow_run_id[viknow_run_id] = dict(metadata)
                previous_trace_context = getattr(self, "trace_context", None)
                if parent_run_id is None and isinstance(metadata, dict):
                    trace_id = _metadata_langfuse_trace_id(metadata)
                    if trace_id:
                        self.trace_context = {"trace_id": trace_id}
                try:
                    return super().on_chain_start(
                        serialized,
                        inputs,
                        run_id=run_id,
                        parent_run_id=parent_run_id,
                        tags=tags,
                        metadata=metadata,
                        **kwargs,
                    )
                finally:
                    if parent_run_id is None and isinstance(metadata, dict):
                        self._remember_root_observation_ids(run_id, metadata)
                    if parent_run_id is None:
                        self.trace_context = previous_trace_context

            def on_chain_end(
                self,
                outputs: dict[str, Any],
                *,
                run_id: Any,
                parent_run_id: Any | None = None,
                **kwargs: Any,
            ) -> Any:
                if parent_run_id is None:
                    self._update_root_trace_metadata(run_id)
                try:
                    return super().on_chain_end(
                        outputs,
                        run_id=run_id,
                        parent_run_id=parent_run_id,
                        **kwargs,
                    )
                finally:
                    if parent_run_id is None:
                        self._clear_root_trace_state(run_id)

            def on_chain_error(
                self,
                error: BaseException,
                *,
                run_id: Any,
                parent_run_id: Any | None = None,
                **kwargs: Any,
            ) -> Any:
                if parent_run_id is None:
                    self._update_root_trace_metadata(run_id)
                try:
                    return super().on_chain_error(
                        error,
                        run_id=run_id,
                        parent_run_id=parent_run_id,
                        **kwargs,
                    )
                finally:
                    if parent_run_id is None:
                        self._clear_root_trace_state(run_id)

            def on_tool_start(
                self,
                serialized: dict[str, Any] | None,
                input_str: str,
                *,
                run_id: Any,
                parent_run_id: Any | None = None,
                tags: list[str] | None = None,
                metadata: dict[str, Any] | None = None,
                **kwargs: Any,
            ) -> Any:
                result = super().on_tool_start(
                    serialized,
                    input_str,
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                    tags=tags,
                    metadata=metadata,
                    **kwargs,
                )
                self._register_active_tool_observation(run_id, serialized, kwargs)
                return result

            def on_tool_end(
                self,
                output: Any,
                *,
                run_id: Any,
                parent_run_id: Any | None = None,
                **kwargs: Any,
            ) -> Any:
                """End the TOOL observation and attach token_usage + envelope metadata."""
                tool_name = self._tool_name_from_tool_event(None, kwargs, output=output)
                self._unregister_active_tool_observation(tool_name, run_id)
                try:
                    log_debug = getattr(self, "_log_debug_event", None)
                    if callable(log_debug):
                        log_debug(
                            "on_tool_end",
                            run_id,
                            parent_run_id,
                            output=output,
                        )
                    detach = getattr(self, "_detach_observation", None)
                    observation = detach(run_id) if callable(detach) else None
                    if not tool_name:
                        tool_name = self._tool_name_from_tool_event(None, kwargs, output=output)
                    token_usage = _token_usage_from_tool_output(output)
                    if token_usage is None:
                        token_usage = take_pending_tool_token_usage(tool_name)
                    if observation is None:
                        return super().on_tool_end(
                            output,
                            run_id=run_id,
                            parent_run_id=parent_run_id,
                            **kwargs,
                        )
                    update_kwargs: dict[str, Any] = {
                        "output": output,
                        "input": kwargs.get("inputs"),
                    }
                    tool_result = _tool_result_from_tool_output(tool_name, output)
                    discover_metadata = _knowledge_discover_observation_metadata(
                        tool_name or getattr(observation, "name", None),
                        output,
                    )
                    extra_metadata: dict[str, Any] = {}
                    if token_usage:
                        extra_metadata["token_usage"] = token_usage
                    if tool_result:
                        extra_metadata["tool_result"] = tool_result
                    extra_metadata.update(discover_metadata)
                    if extra_metadata:
                        update_kwargs["metadata"] = _merged_observation_metadata(
                            observation, kwargs, extra_metadata
                        )
                        if token_usage:
                            logger.debug(
                                "[langfuse] writing tool token_usage name={} totals={}",
                                tool_name or getattr(observation, "name", None),
                                token_usage.get("totals"),
                            )
                    update = getattr(observation, "update", None)
                    if callable(update):
                        updated = update(**update_kwargs)
                        end = getattr(updated, "end", None)
                        if callable(end):
                            end()
                        return None
                    end = getattr(observation, "end", None)
                    if callable(end):
                        end()
                except Exception:
                    logger.opt(exception=True).warning(
                        "[langfuse] on_tool_end failed to write tool token_usage"
                    )
                    return super().on_tool_end(
                        output,
                        run_id=run_id,
                        parent_run_id=parent_run_id,
                        **kwargs,
                    )
                return None

            def on_tool_error(
                self,
                error: BaseException,
                *,
                run_id: Any,
                parent_run_id: Any | None = None,
                **kwargs: Any,
            ) -> Any:
                tool_name = self._tool_name_from_tool_event(None, kwargs)
                self._unregister_active_tool_observation(tool_name, run_id)
                try:
                    detach = getattr(self, "_detach_observation", None)
                    observation = detach(run_id) if callable(detach) else None
                    if observation is None:
                        return super().on_tool_error(
                            error,
                            run_id=run_id,
                            parent_run_id=parent_run_id,
                            **kwargs,
                        )
                    update_kwargs: dict[str, Any] = {
                        "output": error,
                        "input": kwargs.get("inputs"),
                    }
                    tool_result = _tool_result_from_error(tool_name, error)
                    if tool_result:
                        update_kwargs["metadata"] = _merged_observation_metadata(
                            observation, kwargs, {"tool_result": tool_result}
                        )
                    update = getattr(observation, "update", None)
                    if callable(update):
                        updated = update(**update_kwargs)
                        end = getattr(updated, "end", None)
                        if callable(end):
                            end()
                        return None
                    end = getattr(observation, "end", None)
                    if callable(end):
                        end()
                except Exception:
                    logger.opt(exception=True).warning(
                        "[langfuse] on_tool_error failed to write tool_result"
                    )
                    return super().on_tool_error(
                        error,
                        run_id=run_id,
                        parent_run_id=parent_run_id,
                        **kwargs,
                    )
                return None

            def _tool_name_from_tool_event(
                self,
                serialized: dict[str, Any] | None,
                kwargs: dict[str, Any],
                *,
                output: Any | None = None,
            ) -> str:
                serialized_name = ""
                if isinstance(serialized, dict):
                    serialized_name = str(serialized.get("name") or "").strip()
                return str(
                    kwargs.get("name") or serialized_name or getattr(output, "name", None) or ""
                ).strip()

            def _register_active_tool_observation(
                self,
                run_id: Any,
                serialized: dict[str, Any] | None,
                kwargs: dict[str, Any],
            ) -> None:
                observation = getattr(self, "runs", {}).get(run_id)
                tool_name = self._tool_name_from_tool_event(serialized, kwargs)
                if observation is None or not tool_name:
                    return
                self._active_tool_observation_stack.append((tool_name, observation, run_id))

            def _unregister_active_tool_observation(
                self,
                tool_name: str,
                run_id: Any,
            ) -> None:
                stack = self._active_tool_observation_stack
                for index in range(len(stack) - 1, -1, -1):
                    name, _observation, registered_run_id = stack[index]
                    if registered_run_id == run_id or (tool_name and name == tool_name):
                        del stack[index]
                        return

            def _active_tool_observation(
                self,
                tool_name_hint: str | None = None,
            ) -> Any | None:
                tool_name = str(tool_name_hint or get_active_tool() or "").strip()
                if not tool_name:
                    return None
                for name, observation, _run_id in reversed(self._active_tool_observation_stack):
                    if name == tool_name:
                        return observation
                return None

            def _update_root_trace_metadata(self, run_id: Any) -> None:
                viknow_run_id = self._root_viknow_run_ids.get(run_id)
                if not viknow_run_id:
                    return
                dynamic_metadata = resolve_trace_metadata(viknow_run_id)
                observation = getattr(self, "runs", {}).get(run_id)
                if observation is None or not hasattr(observation, "update_trace"):
                    return
                merged = dict(self._root_trace_metadata.get(run_id) or {})
                merged.update(dynamic_metadata)
                update: dict[str, Any] = {"metadata": merged}
                trace_name = str(merged.get("langfuse_trace_name") or "").strip()
                if trace_name:
                    update["name"] = trace_name
                observation.update_trace(**update)
                trace_id = str(getattr(observation, "trace_id", "") or "").strip()
                if trace_id:
                    merged["langfuse_resolved_trace_id"] = trace_id
                observation_name = str(getattr(observation, "name", "") or "").strip()
                if observation_name:
                    merged["langfuse_resolved_trace_name"] = observation_name
                observation_id = str(getattr(observation, "id", "") or "").strip()
                if observation_id:
                    self._root_observation_ids_by_viknow_run_id[viknow_run_id] = observation_id
                self._trace_metadata_by_viknow_run_id[viknow_run_id] = merged

            def _remember_root_observation_ids(
                self,
                run_id: Any,
                metadata: dict[str, Any],
            ) -> None:
                viknow_run_id = str(metadata.get("viknow_run_id") or "").strip()
                if not viknow_run_id:
                    return
                observation = getattr(self, "runs", {}).get(run_id)
                if observation is None:
                    return
                trace_id = str(getattr(observation, "trace_id", "") or "").strip()
                observation_id = str(getattr(observation, "id", "") or "").strip()
                observation_name = str(getattr(observation, "name", "") or "").strip()
                stored_metadata = dict(
                    self._trace_metadata_by_viknow_run_id.get(viknow_run_id) or {}
                )
                if trace_id:
                    stored_metadata["langfuse_resolved_trace_id"] = trace_id
                    trace_ids = self._resolved_trace_ids_by_viknow_run_id.setdefault(
                        viknow_run_id, []
                    )
                    if trace_id not in trace_ids:
                        trace_ids.append(trace_id)
                if observation_name:
                    stored_metadata["langfuse_resolved_trace_name"] = observation_name
                if observation_id:
                    self._root_observation_ids_by_viknow_run_id[viknow_run_id] = observation_id
                self._trace_metadata_by_viknow_run_id[viknow_run_id] = stored_metadata

            def update_viknow_trace_metadata(
                self,
                viknow_run_id: str,
                metadata: dict[str, Any],
            ) -> None:
                run_id = self._root_run_ids_by_viknow_run_id.get(viknow_run_id)
                if run_id is None:
                    return
                observation = getattr(self, "runs", {}).get(run_id)
                if observation is None:
                    observation = getattr(self, "completed_observations", {}).get(run_id)
                merged = dict(self._trace_metadata_by_viknow_run_id.get(viknow_run_id) or {})
                merged.update(metadata)
                update: dict[str, Any] = {"metadata": merged}
                trace_name = str(merged.get("langfuse_trace_name") or "").strip()
                if trace_name:
                    update["name"] = trace_name
                if observation is not None and hasattr(observation, "update_trace"):
                    observation.update_trace(**update)
                self._trace_metadata_by_viknow_run_id[viknow_run_id] = merged
                if "viknow_answer_review" in metadata:
                    self._record_answer_review_trace_metadata_span(viknow_run_id, metadata)

            def update_viknow_trace_output(
                self,
                viknow_run_id: str,
                output: dict[str, Any],
            ) -> bool:
                run_id = self._root_run_ids_by_viknow_run_id.get(viknow_run_id)
                if run_id is None:
                    return False
                observation = getattr(self, "runs", {}).get(run_id)
                if observation is None:
                    observation = getattr(self, "completed_observations", {}).get(run_id)
                if observation is None or not hasattr(observation, "update_trace"):
                    return False
                try:
                    observation.update_trace(output=output)
                except Exception:
                    logger.opt(exception=True).warning(
                        "[langfuse] failed to update root output for run {}", viknow_run_id
                    )
                    return False
                return True

            def update_viknow_trace_outputs(
                self,
                viknow_run_id: str,
                output: dict[str, Any],
            ) -> int:
                updated_count = 0
                for run_id in self._root_run_ids_by_viknow_run_id_all.get(viknow_run_id, []):
                    observation = getattr(self, "runs", {}).get(run_id)
                    if observation is None:
                        observation = getattr(self, "completed_observations", {}).get(run_id)
                    if observation is None or not hasattr(observation, "update_trace"):
                        continue
                    try:
                        observation.update_trace(output=output)
                    except Exception:
                        logger.opt(exception=True).warning(
                            "[langfuse] failed to update root output for run {}",
                            viknow_run_id,
                        )
                        continue
                    updated_count += 1
                return updated_count

            def resolved_langfuse_trace_ids(self, viknow_run_id: str) -> list[str]:
                return list(self._resolved_trace_ids_by_viknow_run_id.get(viknow_run_id, []))

            def resolved_langfuse_trace_id(self, viknow_run_id: str) -> str:
                resolved_ids = self.resolved_langfuse_trace_ids(viknow_run_id)
                if resolved_ids:
                    return resolved_ids[-1]
                stored = self._trace_metadata_by_viknow_run_id.get(viknow_run_id) or {}
                return str(
                    stored.get("langfuse_resolved_trace_id")
                    or getattr(self, "last_trace_id", "")
                    or ""
                ).strip()

            def record_generation(
                self,
                *,
                viknow_run_id: str,
                name: str,
                model: str | None,
                usage: dict[str, Any],
                metadata: dict[str, Any],
            ) -> None:
                trace_id = self.resolved_langfuse_trace_id(viknow_run_id)
                if not trace_id:
                    return
                usage_details = _usage_details(usage)
                tool_name_hint = str(
                    metadata.get("tool") or metadata.get("tool_name") or ""
                ).strip()
                tool_observation = self._active_tool_observation(tool_name_hint or None)
                parent_observation = tool_observation or self._root_observation_for_viknow_run_id(
                    viknow_run_id
                )
                start_observation = getattr(parent_observation, "start_observation", None)
                if callable(start_observation):
                    generation = start_observation(
                        name=name,
                        as_type="generation",
                        model=model,
                        usage_details=usage_details,
                        metadata=metadata,
                    )
                else:
                    client = getattr(self, "client", None)
                    start_observation = getattr(client, "start_observation", None)
                    if not callable(start_observation):
                        return
                    trace_context: dict[str, str] = {"trace_id": trace_id}
                    parent_span_id = str(getattr(parent_observation, "id", "") or "").strip()
                    if not parent_span_id:
                        parent_span_id = self._root_observation_ids_by_viknow_run_id.get(
                            viknow_run_id, ""
                        )
                    if parent_span_id:
                        trace_context["parent_span_id"] = parent_span_id
                    generation = start_observation(
                        trace_context=trace_context,
                        name=name,
                        as_type="generation",
                        model=model,
                        usage_details=usage_details,
                        metadata=metadata,
                    )
                end = getattr(generation, "end", None)
                if callable(end):
                    end()

            def _record_answer_review_trace_metadata_span(
                self,
                viknow_run_id: str,
                metadata: dict[str, Any],
            ) -> None:
                stored_metadata = self._trace_metadata_by_viknow_run_id.get(viknow_run_id, {})
                trace_id = str(
                    stored_metadata.get("langfuse_resolved_trace_id")
                    or getattr(self, "last_trace_id", "")
                    or ""
                ).strip()
                if not trace_id:
                    return
                parent_span_id = self._root_observation_ids_by_viknow_run_id.get(viknow_run_id)
                if not parent_span_id:
                    return
                root_observation = self._root_observation_for_viknow_run_id(viknow_run_id)
                start_observation = getattr(root_observation, "start_observation", None)
                if callable(start_observation):
                    span = start_observation(
                        name="answer_review",
                        as_type="span",
                        metadata=metadata,
                        output=metadata,
                    )
                    end = getattr(span, "end", None)
                    if callable(end):
                        end()
                    return

                client = getattr(self, "client", None)
                start_observation = getattr(client, "start_observation", None)
                if not callable(start_observation):
                    return
                span = start_observation(
                    trace_context={"trace_id": trace_id, "parent_span_id": parent_span_id},
                    name="answer_review",
                    as_type="span",
                    metadata=metadata,
                    output=metadata,
                )
                trace_update: dict[str, Any] = {"metadata": stored_metadata}
                trace_name = str(
                    stored_metadata.get("langfuse_trace_name")
                    or stored_metadata.get("langfuse_resolved_trace_name")
                    or ""
                ).strip()
                if trace_name:
                    trace_update["name"] = trace_name
                update_trace = getattr(span, "update_trace", None)
                if callable(update_trace):
                    update_trace(**trace_update)
                end = getattr(span, "end", None)
                if callable(end):
                    end()

            def _root_observation_for_viknow_run_id(self, viknow_run_id: str) -> Any | None:
                run_id = self._root_run_ids_by_viknow_run_id.get(viknow_run_id)
                if run_id is None:
                    return None
                observation = getattr(self, "runs", {}).get(run_id)
                if observation is not None:
                    return observation
                return getattr(self, "completed_observations", {}).get(run_id)

            def _clear_root_trace_state(self, run_id: Any) -> None:
                self._root_viknow_run_ids.pop(run_id, None)
                self._root_trace_metadata.pop(run_id, None)

        return ViKnowLangfuseCallbackHandler()


def _merged_observation_metadata(
    observation: Any,
    kwargs: dict[str, Any],
    extra: dict[str, Any],
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    callback_metadata = kwargs.get("metadata")
    if isinstance(callback_metadata, dict):
        merged.update(callback_metadata)
    observation_metadata = getattr(observation, "metadata", None)
    if isinstance(observation_metadata, dict):
        merged.update(observation_metadata)
    merged.update(extra)
    return merged


def _tool_result_from_tool_output(tool_name: str | None, output: Any) -> dict[str, Any] | None:
    """Read a compact envelope summary from artifact, ToolMessage content, or JSON."""

    from agents.runtimes.deepagents.core.tools.base import ToolResult, summarize_tool_result

    artifact = getattr(output, "artifact", None)
    if not isinstance(artifact, dict) and isinstance(output, dict):
        maybe_artifact = output.get("artifact")
        artifact = maybe_artifact if isinstance(maybe_artifact, dict) else None
    if isinstance(artifact, dict):
        existing = artifact.get("tool_result")
        if (
            isinstance(existing, dict)
            and existing.get("status") in {"ok", "error"}
            and existing.get("code")
        ):
            return existing
    raw = getattr(output, "content", None)
    if raw is None:
        if isinstance(output, (str, dict, ToolResult)):
            raw = output
        else:
            return None
    if not isinstance(raw, (str, dict, ToolResult)):
        return None
    try:
        return summarize_tool_result(tool_name or "unknown", raw)
    except Exception:
        logger.opt(exception=True).debug(
            "[langfuse] failed to summarize tool output for {}",
            tool_name or "unknown",
        )
        return None


def _tool_result_from_error(tool_name: str | None, error: BaseException) -> dict[str, Any]:
    from agents.runtimes.deepagents.core.tools.base import summarize_tool_result

    text = str(error)
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            summary = summarize_tool_result(tool_name or "unknown", stripped)
            if summary.get("status") == "error":
                return summary
        except Exception:
            logger.opt(exception=True).debug(
                "[langfuse] failed to coerce tool error envelope for {}",
                tool_name or "unknown",
            )
    name = tool_name or "unknown"
    return {
        "tool": name,
        "status": "error",
        "code": "internal_error",
        "error": {
            "code": "internal_error",
            "message": text[:240],
        },
        "retryable": False,
    }


def _metadata_langfuse_trace_id(metadata: dict[str, Any]) -> str | None:
    trace_id = str(metadata.get("langfuse_trace_id") or "").strip()
    if not _LANGFUSE_TRACE_ID_PATTERN.fullmatch(trace_id):
        return None
    return trace_id


def _token_usage_from_tool_output(output: Any) -> dict[str, Any] | None:
    """Extract ``token_usage`` (totals + by_model) from a tool ToolMessage artifact."""
    artifact = getattr(output, "artifact", None)
    if not isinstance(artifact, dict):
        if isinstance(output, dict):
            artifact = (
                output.get("artifact") if isinstance(output.get("artifact"), dict) else output
            )
        else:
            return None
    if not isinstance(artifact, dict):
        return None
    token_usage = artifact.get("token_usage")
    if not isinstance(token_usage, dict) or not token_usage:
        return None
    totals = token_usage.get("totals")
    by_model = token_usage.get("by_model")
    if not isinstance(totals, dict) or not isinstance(by_model, list):
        return None
    return {"totals": dict(totals), "by_model": list(by_model)}


def _knowledge_discover_observation_metadata(
    tool_name: str | None,
    output: Any,
) -> dict[str, Any]:
    """Expose the effective library scope on knowledge_discover observations."""
    if str(tool_name or "").strip() != "knowledge_discover":
        return {}

    content = getattr(output, "content", output)
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
        content = "".join(text_parts)
    if isinstance(content, dict):
        payload = content
    elif isinstance(content, str):
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return {}
    else:
        return {}

    libraries = payload.get("libraries")
    if not isinstance(libraries, list):
        return {}
    library_ids: list[str] = []
    sources: set[str] = set()
    explicit = False
    for library in libraries:
        if not isinstance(library, dict):
            continue
        library_id = str(library.get("id") or "").strip()
        if library_id:
            library_ids.append(library_id)
        metadata = library.get("metadata")
        if isinstance(metadata, dict):
            source = str(metadata.get("source") or "").strip()
            if source:
                sources.add(source)
            explicit = explicit or metadata.get("explicit") is True
    source_value: str | list[str] | None
    if len(sources) == 1:
        source_value = next(iter(sources))
    elif sources:
        source_value = sorted(sources)
    else:
        source_value = None
    return {
        "library_ids": library_ids,
        "library_scope_source": source_value,
        "run_allowlist": source_value == "run_allowlist",
        "library_scope_explicit": explicit,
    }


def _zero_usage_counts() -> dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "generations": 0}


def _normalize_usage_counts(usage: dict[str, Any]) -> dict[str, int]:
    """Normalize provider usage dicts into prompt/completion/total tokens."""
    if not isinstance(usage, dict):
        return _zero_usage_counts()
    prompt = usage.get("prompt_tokens")
    if prompt is None:
        prompt = usage.get("input_tokens")
    completion = usage.get("completion_tokens")
    if completion is None:
        completion = usage.get("output_tokens")
    total = usage.get("total_tokens")
    counts = {
        "prompt_tokens": _int_token_count(prompt),
        "completion_tokens": _int_token_count(completion),
        "total_tokens": _int_token_count(total),
    }
    if not counts["total_tokens"]:
        counts["total_tokens"] = counts["prompt_tokens"] + counts["completion_tokens"]
    return counts


def _int_token_count(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _has_token_count(counts: dict[str, int]) -> bool:
    return bool(
        counts.get("prompt_tokens") or counts.get("completion_tokens") or counts.get("total_tokens")
    )


def _model_key(model: str | None) -> str:
    name = _clean_model_name(model)
    return name or "unknown"


def _clean_model_name(model: str | None) -> str | None:
    if not model:
        return None
    name = str(model).strip()
    return name or None


def _usage_details(counts: dict[str, int]) -> dict[str, int] | None:
    details = {
        "prompt_tokens": counts["prompt_tokens"],
        "completion_tokens": counts["completion_tokens"],
        "total_tokens": counts["total_tokens"],
    }
    return {key: value for key, value in details.items() if value} or None


def _is_enabled(config: LangfuseTracingConfig) -> bool:
    if config.enabled:
        return True
    if not config.enabled_env:
        return False
    value = os.getenv(config.enabled_env)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_langfuse_client() -> Any:
    """Build a Langfuse client from ``VIKNOW_LANGFUSE_*`` env configuration.

    Raises ``RuntimeError`` when required credentials are missing. Used by the
    usage aggregate reader; the tracing client remains the LangChain handler.
    """
    configure_langfuse_sdk_env()
    from langfuse import Langfuse

    return Langfuse()


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if value and value.strip():
        return value.strip()
    raise RuntimeError(f"{name} is required when observability.langfuse.enabled is true")


def configure_langfuse_sdk_env() -> None:
    os.environ["LANGFUSE_PUBLIC_KEY"] = _require_env("VIKNOW_LANGFUSE_PUBLIC_KEY")
    os.environ["LANGFUSE_SECRET_KEY"] = _require_env("VIKNOW_LANGFUSE_SECRET_KEY")
    host = os.getenv("VIKNOW_LANGFUSE_HOST")
    if host and host.strip():
        os.environ["LANGFUSE_HOST"] = host.strip()
