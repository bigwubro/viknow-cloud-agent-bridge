"""Dedicated thread pool for ViKnow sync tool handlers."""

from __future__ import annotations

import asyncio
import atexit
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
from typing import Any

DEFAULT_TOOL_THREAD_POOL_SIZE = 64

_executor: ThreadPoolExecutor | None = None
_executor_size: int | None = None


def configure_tool_thread_pool(*, max_workers: int) -> None:
    """Configure or resize the process-wide ViKnow tool thread pool."""

    global _executor, _executor_size
    if max_workers < 1:
        raise ValueError("max_workers must be >= 1")
    if _executor is not None and _executor_size == max_workers:
        return
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
    _executor_size = max_workers
    _executor = ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="viknow-tool",
    )


def _ensure_executor() -> ThreadPoolExecutor:
    if _executor is None:
        configure_tool_thread_pool(max_workers=DEFAULT_TOOL_THREAD_POOL_SIZE)
    assert _executor is not None
    return _executor


async def run_tool_sync[T](fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run a sync callable on the dedicated ViKnow tool thread pool."""

    loop = asyncio.get_running_loop()
    executor = _ensure_executor()
    # Match asyncio.to_thread: each call inherits its caller's run context,
    # without leaking handler mutations to the caller or another pooled call.
    context = copy_context()
    return await loop.run_in_executor(executor, partial(context.run, fn, *args, **kwargs))


def shutdown_tool_thread_pool() -> None:
    global _executor, _executor_size
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None
        _executor_size = None


atexit.register(shutdown_tool_thread_pool)
