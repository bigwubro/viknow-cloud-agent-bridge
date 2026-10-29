"""Optional index-job usage hooks (implemented by ViKnow, invoked by HyperRAG).

HyperRAG must not hard-depend on ViKnow. ViKnow registers hooks at adapter
startup via ``set_hooks``. All entry points are best-effort no-ops when hooks
are unset or raise.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_hooks: IndexUsageHooks | None = None


class IndexUsageHooks(Protocol):
    def on_job_submitted(self, job: Any) -> None: ...

    def on_task_start(self, task: Any) -> Any: ...

    def on_task_end(self, task: Any, scope_token: Any = None) -> None: ...

    def on_job_finished(self, job: Any) -> None: ...

    def record_generation(
        self,
        *,
        name: str,
        model: str | None = None,
        usage: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        input: Any | None = None,
        output: Any | None = None,
        error: str | None = None,
    ) -> None: ...


def set_hooks(hooks: IndexUsageHooks | None) -> None:
    global _hooks
    _hooks = hooks


def get_hooks() -> IndexUsageHooks | None:
    return _hooks


def on_job_submitted(job: Any) -> None:
    hooks = _hooks
    if hooks is None:
        return
    try:
        hooks.on_job_submitted(job)
    except Exception:
        logger.warning("[index-usage] on_job_submitted hook failed", exc_info=True)


def on_task_start(task: Any) -> Any:
    hooks = _hooks
    if hooks is None:
        return None
    try:
        return hooks.on_task_start(task)
    except Exception:
        logger.warning("[index-usage] on_task_start hook failed", exc_info=True)
        return None


def on_task_end(task: Any, scope_token: Any = None) -> None:
    hooks = _hooks
    if hooks is None:
        return
    try:
        hooks.on_task_end(task, scope_token)
    except Exception:
        logger.warning("[index-usage] on_task_end hook failed", exc_info=True)


def on_job_finished(job: Any) -> None:
    hooks = _hooks
    if hooks is None:
        return
    try:
        hooks.on_job_finished(job)
    except Exception:
        logger.warning("[index-usage] on_job_finished hook failed", exc_info=True)


def record_generation(
    *,
    name: str,
    model: str | None = None,
    usage: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    input: Any | None = None,
    output: Any | None = None,
    error: str | None = None,
) -> None:
    hooks = _hooks
    if hooks is None:
        return
    try:
        hooks.record_generation(
            name=name,
            model=model,
            usage=usage,
            metadata=metadata,
            input=input,
            output=output,
            error=error,
        )
    except Exception:
        logger.warning("[index-usage] record_generation hook failed", exc_info=True)


def usage_from_openai_response(response: Any) -> dict[str, int] | None:
    """Best-effort extract usage; prefers ViKnow helper when available."""
    try:
        from viknow.core.usage.index_tracing import usage_from_openai_response as _extract

        return _extract(response)
    except Exception:
        pass
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None
    if isinstance(usage, dict):
        prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        total = int(usage.get("total_tokens") or 0)
    else:
        prompt = int(getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0)
        completion = int(
            getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0) or 0
        )
        total = int(getattr(usage, "total_tokens", 0) or 0)
    if not total:
        total = prompt + completion
    if not (prompt or completion or total):
        return None
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
