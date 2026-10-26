"""Bridge between ViKnow's ResourceScheduler and scheduler-core.

When unified scheduling is enabled, tool/model requests are mapped to
scheduler-core ``Task`` objects, submitted to Redis, and executed by an
in-process worker that feeds the result back to the awaiting caller.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from scheduler_core.acquire_reason import AcquireReason
from scheduler_core.config import SchedulerConfig
from scheduler_core.models import Task, TaskKind, TaskStatus, Tier, UnitKind
from scheduler_core.scheduler import UnifiedScheduler, task_from_hash
from scheduler_core.store import SchedulerStore, task_ts

from agents.expert_buddy.core.observability.otel import (
    ATTR_ACQUIRE_REJECT,
    ATTR_PHASE,
    ATTR_QUEUE_PATH,
    ATTR_QUEUE_RANK,
    ATTR_RESOURCE,
    ATTR_RUN_ID,
    ATTR_TOOL,
    ERROR_TOOL_QUEUE_TIMEOUT,
    record_error,
    start_span,
    tool_name_from_metadata,
)
from agents.expert_buddy.core.scheduler.domain import ExecUnit, Priority, SlotRequest, ToolQueueTimeoutError
from agents.expert_buddy.core.scheduler.unified_config import GENERAL_TOOL_DOMAIN
from agents.expert_buddy.core.scheduler.unified_reconcile import (
    QUEUE_ORPHAN_AFTER_SECONDS,
    created_at_is_stale,
)

logger = logging.getLogger(__name__)

#: Polling interval for streaming-lease waits. Starts fast so a slot freed at
#: any moment is picked up quickly, then backs off so a burst of queued model
#: calls does not hammer Redis while capacity stays saturated.
_ACQUIRE_POLL_INITIAL_SECONDS = 0.05
_ACQUIRE_POLL_MAX_SECONDS = 0.5
_QUEUE_HEAD_SCAN_MULTIPLIER = 4
_RUN_QUOTA_POLL_SECONDS = 0.05


class _TaskResult:
    """Container for a task's execution result passed back to the caller."""

    __slots__ = ("value", "error")

    def __init__(self) -> None:
        self.value: Any = None
        self.error: BaseException | None = None


class UnifiedSchedulerBridge:
    """Adapter making ResourceScheduler talk to scheduler-core when enabled.

    Lifecycle: one bridge per Agent Profile runtime (mirroring
    ``ResourceScheduler`` ownership). It owns a shared Redis-backed
    ``UnifiedScheduler`` and a background worker that executes admitted tasks.
    """

    def __init__(
        self,
        config: SchedulerConfig,
        *,
        store: SchedulerStore | None = None,
    ) -> None:
        self.config = config
        self.store = store or SchedulerStore.from_url(config.redis_url, config)
        self.scheduler = UnifiedScheduler(config, self.store)
        #: In-process handler registry for tool/model tasks (not serializable).
        self._handlers: dict[str, Callable[[], Awaitable[Any]]] = {}
        #: task_id -> future resolved by the worker when the task finishes.
        self._pending: dict[str, asyncio.Future[Any]] = {}
        #: task_id -> event set when the worker is about to run the handler.
        self._worker_started: dict[str, asyncio.Event] = {}
        self._worker_task: asyncio.Task[Any] | None = None
        self._worker_stop = asyncio.Event()
        self._inflight_tasks: set[asyncio.Task[Any]] = set()

    def _queue_head_scan_limit(self) -> int:
        """How many queued AGENT_TOOL heads to inspect before giving up this tick."""
        return _QUEUE_HEAD_SCAN_MULTIPLIER * self.config.per_run_max_concurrent_tools

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Start the background worker that executes admitted tasks."""
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        self._worker_stop.set()
        task = self._worker_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._worker_task = None
        inflight = list(self._inflight_tasks)
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
        self._inflight_tasks.clear()

    # ------------------------------------------------------------------ run/acquire

    async def run_tool(self, unit: ExecUnit) -> Any:
        """Execute a tool under unified scheduling, returning the handler result.

        Maps ExecUnit -> Task (source_id -> run_id, resources -> domains,
        priority -> tier, unit_kind preserved), submits, then waits for the
        worker to execute it and report back.
        """
        await self.start()
        task_id = f"tool-{uuid4().hex}"
        # Tool handlers rely on ContextVar collectors (usage scope, Langfuse tool
        # scope). The worker loop is not a child task of the caller, so copy the
        # submitter context and run the handler task inside it.
        self._handlers[task_id] = _wrap_handler(unit, context=contextvars.copy_context())
        task = _exec_unit_to_task(unit, task_id=task_id)

        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[task.id] = future

        tool_name = tool_name_from_metadata(unit.metadata)
        resource = unit.resources[0] if unit.resources else None
        worker_started = asyncio.Event()
        self._worker_started[task.id] = worker_started
        span_attrs = {
            ATTR_PHASE: "tool.wait_queue",
            ATTR_RESOURCE: resource,
            ATTR_RUN_ID: unit.source_id,
            ATTR_TOOL: tool_name,
        }
        work_attrs = {**span_attrs, ATTR_PHASE: "tool.work"}
        try:
            async with start_span("tool.wait_queue", **span_attrs) as wait_span:
                await self._submit_tool_and_wait(
                    task,
                    future=future,
                    worker_started=worker_started,
                    timeout=self.config.queue_wait_timeout_seconds,
                    span=wait_span,
                )
            if future.done():
                return future.result()
            async with start_span("tool.work", **work_attrs):
                exec_timeout = unit.timeout or self.config.default_execution_timeout_seconds
                return await asyncio.wait_for(future, timeout=exec_timeout + 3600)
        except TimeoutError:
            await self._cleanup_tool_waiter(task)
            raise
        finally:
            self._pending.pop(task.id, None)
            self._worker_started.pop(task.id, None)
            self._handlers.pop(task_id, None)

    async def acquire(self, request: SlotRequest) -> Any:
        """Hold a unified lease for the duration of a streaming unit.

        Fast path: try to acquire immediately when capacity is free. When
        saturated, enqueue into the domain Redis queue (same score/seq FIFO as
        tools) and only call ``try_acquire`` while this task is the queue head,
        so waiters are admitted in order. The wait is bounded by
        ``queue_wait_timeout_seconds``; on timeout a ``TimeoutError`` is raised.
        The lease is released via ``_StreamingLease.release``.
        """
        await self.start()
        task = _slot_request_to_task(request)
        enqueued = False
        await asyncio.to_thread(self.store.create_task, task)
        try:
            admitted = await asyncio.to_thread(self.store.try_acquire, task)
            if admitted:
                return _StreamingLease(self, task)

            await asyncio.to_thread(self.store.enqueue, task)
            enqueued = True

            loop = asyncio.get_event_loop()
            deadline = loop.time() + self.config.queue_wait_timeout_seconds
            interval = _ACQUIRE_POLL_INITIAL_SECONDS
            while loop.time() < deadline:
                if await self._is_queue_head(task):
                    admitted = await asyncio.to_thread(self.store.try_acquire, task)
                    if admitted:
                        return _StreamingLease(self, task)
                await asyncio.sleep(interval)
                interval = min(interval * 2, _ACQUIRE_POLL_MAX_SECONDS)
            raise TimeoutError(f"resource queue wait exceeded for {task.id}")
        except BaseException:
            await self._cleanup_streaming_waiter(task, enqueued=enqueued)
            raise

    async def _is_queue_head(self, task: Task) -> bool:
        """True when ``task`` is the lowest-scored member in every domain queue."""
        for domain in task.domains:
            head = await asyncio.to_thread(self.store.peek, domain)
            if head != task.id:
                return False
        return True

    async def _cleanup_streaming_waiter(self, task: Task, *, enqueued: bool) -> None:
        status = await asyncio.to_thread(self.store.task_status, task.id)
        if status == TaskStatus.RUNNING.value:
            await asyncio.to_thread(self.store.complete, task)
            return
        if enqueued or status == TaskStatus.QUEUED.value:
            await asyncio.to_thread(self.store.cancel, task)
            return
        await asyncio.to_thread(self.store.delete_task, task.id)

    async def _release_streaming(self, task: Task) -> None:
        await asyncio.to_thread(self.store.complete, task)

    # ------------------------------------------------------------------ worker

    async def _worker_loop(self) -> None:
        """Continuously drain domain queues and execute admitted tasks."""
        domains = list(self.config.domains.keys())
        while not self._worker_stop.is_set():
            any_work = False
            # 1. drain fast-path admitted tasks (already hold leases)
            if await self._drain_fast_path():
                any_work = True
            # 2. pick queued tasks per domain, admitting + executing them
            for domain in domains:
                if await self._pick_and_run_domain(domain):
                    any_work = True
            if not any_work and not self._inflight_tasks:
                await asyncio.sleep(0.05)

    def _spawn_execute_task(self, task: Task) -> None:
        """Run a admitted tool handler concurrently with other in-flight work."""
        exec_task = asyncio.create_task(
            self._execute_task(task),
            name=f"unified-exec-{task.id}",
        )
        self._inflight_tasks.add(exec_task)
        exec_task.add_done_callback(self._inflight_tasks.discard)

    async def _drain_fast_path(self) -> bool:
        """Start every fast-path admitted AGENT_TOOL without blocking the worker loop."""
        started = False
        while not self.scheduler._fast_ready.empty():
            fast = self.scheduler._fast_ready.get_nowait()
            # fast-path 队列本应只含 AGENT_TOOL；防御性跳过其他 kind（与
            # _pick_and_run_domain 同一不变量）。放回队尾：任务已持有租约，且
            # 它属于另一 worker（HyperRAG），不能 complete。
            if fast.kind is not TaskKind.AGENT_TOOL:
                logger.error("tool worker skipped non-tool fast-path task %s", fast.id)
                self.scheduler._fast_ready.put_nowait(fast)
                break
            self._spawn_execute_task(fast)
            started = True
        return started

    async def _reclaim_stale_queue_head(self, task: Task, domain: str) -> None:
        """Drop an AGENT_TOOL head that has waited longer than 300s."""

        cancelled = await asyncio.to_thread(self.store.cancel, task)
        if not cancelled:
            await asyncio.to_thread(self.store.pop_queue, domain, task.id)
        self._resolve(
            task,
            error=ToolQueueTimeoutError(f"resource queue wait exceeded for {task.id}"),
        )
        logger.warning("Reclaimed stale unified queue head %s on %s", task.id, domain)

    async def _pick_and_run_domain(self, domain: str) -> bool:
        """Peek queued AGENT_TOOL tasks, admit + execute the first eligible one.

        Scans up to ``4 × per_run_max_concurrent_tools`` queue members so a
        run-quota-blocked head task does not HOL-block unrelated runs when domain
        capacity exists.
        """
        task_ids = await asyncio.to_thread(
            self.store.scan_kind,
            domain,
            TaskKind.AGENT_TOOL.value,
            self._queue_head_scan_limit(),
        )
        if not task_ids:
            return False
        head_id = task_ids[0]
        started = False
        for task_id in task_ids:
            raw = await asyncio.to_thread(self.store.get_task, task_id)
            if not raw:
                await asyncio.to_thread(self.store.pop_queue, domain, task_id)
                continue
            task = task_from_hash(task_id, raw)
            if created_at_is_stale(
                float(task.created_at or 0),
                now=task_ts(),
                stale_after=QUEUE_ORPHAN_AFTER_SECONDS,
            ):
                await self._reclaim_stale_queue_head(task, domain)
                continue
            if _is_streaming_task(task) or not self._owns_agent_tool(task):
                continue
            reason = await asyncio.to_thread(self.store.try_acquire_with_reason, task)
            if reason is AcquireReason.OK:
                self._spawn_execute_task(task)
                started = True
                continue
            if reason is AcquireReason.RUN_QUOTA:
                continue
            if (
                task_id == head_id
                and reason in {AcquireReason.DOMAIN_FULL, AcquireReason.JOB_RESERVE}
                and (
                    await asyncio.to_thread(
                        self.store.peek_kind,
                        domain,
                        TaskKind.AGENT_TOOL.value,
                    )
                )
                == head_id
            ):
                return started
            continue
        return started

    # ------------------------------------------------------------------ task execution

    async def _execute_task(self, task: Task) -> None:
        handler = self._handlers.get(task.id)
        executor = self.scheduler._executors.get(task.executor_ref or "")
        try:
            # 防御：本 worker 只应执行 AGENT_TOOL。若混入 JOB_TASK（如未启用
            # kind 过滤的旧版本），不能 complete——那会拿走 HyperRAG 的 DAG
            # 任务并把它标成 done。放回队列等待真正的 job worker 领取（租约
            # TTL 过期后即可被重新 acquire），避免任务被误标完成或永久滞留。
            if task.kind is not TaskKind.AGENT_TOOL:
                logger.error(
                    "skipping non-tool task %s (kind=%s) in tool worker; re-enqueuing",
                    task.id,
                    task.kind,
                )
                await asyncio.to_thread(self.store.enqueue, task)
                return
            if handler is None and executor is None:
                logger.error(
                    "skipping unowned tool task %s; leaving queued for the submitting worker",
                    task.id,
                )
                await asyncio.to_thread(self.store.enqueue, task)
                return
            self._mark_worker_started(task.id)
            if handler is not None:
                value = await handler()
            else:
                value = await executor(task)
            # If soft-preempted while executing, re-enqueue instead of completing.
            status = await asyncio.to_thread(self.store.task_status, task.id)
            if status == TaskStatus.PREEMPTED.value:
                await asyncio.to_thread(self.store.enqueue, task)
                self._resolve(task, value=value)
                return
            await asyncio.to_thread(self.store.complete, task)
            self._resolve(task, value=value)
        except asyncio.CancelledError:
            await asyncio.to_thread(self.store.complete, task)
            raise
        except BaseException as exc:  # noqa: BLE001 - surface to awaiting caller
            logger.exception("unified task %s failed", task.id)
            await asyncio.to_thread(self.store.mark_failed, task, error=str(exc))
            self._resolve(task, error=exc)

    def _owns_agent_tool(self, task: Task) -> bool:
        """True when this process submitted the tool and still holds its handler."""
        return task.id in self._handlers

    async def _submit_tool_and_wait(
        self,
        task: Task,
        *,
        future: asyncio.Future[Any],
        worker_started: asyncio.Event,
        timeout: float,
        span: Any,
    ) -> None:
        """Submit a tool task and wait until the worker begins the handler."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        result = await self.scheduler.submit_task(task)
        if result.accepted:
            self._apply_queue_span_attrs(span, task, queue_path="fast_ready")
            await self._wait_for_worker_start(
                task,
                future,
                worker_started,
                deadline=deadline,
            )
            return
        if result.reject_reason is AcquireReason.RUN_QUOTA:
            self._apply_queue_span_attrs(
                span,
                task,
                queue_path="run_quota",
                acquire_reject=AcquireReason.RUN_QUOTA.name.lower(),
            )
            queue_path = await self._wait_and_admit_after_run_quota(task, deadline=deadline)
            self._apply_queue_span_attrs(span, task, queue_path=queue_path)
            if queue_path == "redis":
                rank = await asyncio.to_thread(
                    self.store.queue_rank,
                    task.domains[0],
                    task.id,
                )
                if rank is not None and span is not None:
                    span.set_attribute(ATTR_QUEUE_RANK, rank)
            await self._wait_for_worker_start(
                task,
                future,
                worker_started,
                deadline=deadline,
            )
            return
        if result.queued:
            self._apply_queue_span_attrs(
                span,
                task,
                queue_path="redis",
                acquire_reject=(
                    result.reject_reason.name.lower() if result.reject_reason is not None else None
                ),
            )
            rank = await asyncio.to_thread(self.store.queue_rank, task.domains[0], task.id)
            if rank is not None and span is not None:
                span.set_attribute(ATTR_QUEUE_RANK, rank)
            await self._wait_for_worker_start(
                task,
                future,
                worker_started,
                deadline=deadline,
            )
            return
        record_error(ERROR_TOOL_QUEUE_TIMEOUT)
        raise ToolQueueTimeoutError(f"resource queue wait exceeded for tool {task.id}")

    async def _wait_for_run_quota(self, task: Task, *, deadline: float) -> None:
        """Wait until this run has room under per-run tool/model quota."""
        if not task.run_id:
            return
        field = "models_in_use" if task.unit_kind is UnitKind.MODEL else "tools_in_use"
        limit = (
            self.config.per_run_max_concurrent_models
            if field == "models_in_use"
            else self.config.per_run_max_concurrent_tools
        )
        loop = asyncio.get_event_loop()
        while loop.time() < deadline:
            usage = await asyncio.to_thread(self.store.run_usage, task.run_id)
            if int(usage.get(field, 0)) + task.slots <= limit:
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(_RUN_QUOTA_POLL_SECONDS, remaining))
        record_error(ERROR_TOOL_QUEUE_TIMEOUT)
        raise ToolQueueTimeoutError(f"run quota wait exceeded for tool {task.id}")

    async def _wait_and_admit_after_run_quota(self, task: Task, *, deadline: float) -> str:
        """Wait for run quota, then admit; retry on quota races until deadline."""
        while True:
            if asyncio.get_event_loop().time() >= deadline:
                record_error(ERROR_TOOL_QUEUE_TIMEOUT)
                raise ToolQueueTimeoutError(f"run quota wait exceeded for tool {task.id}")
            await self._wait_for_run_quota(task, deadline=deadline)
            reason = await asyncio.to_thread(self.store.try_acquire_with_reason, task)
            if reason is AcquireReason.OK:
                self.scheduler._fast_ready.put_nowait(task)
                return "fast_ready"
            if reason.should_enqueue:
                await asyncio.to_thread(self.store.enqueue, task)
                return "redis"
            if reason is AcquireReason.RUN_QUOTA:
                continue
            record_error(ERROR_TOOL_QUEUE_TIMEOUT)
            raise ToolQueueTimeoutError(f"resource queue wait exceeded for tool {task.id}")

    async def _cleanup_tool_waiter(self, task: Task) -> None:
        """Drop a tool task that timed out before the caller received a result."""
        status = await asyncio.to_thread(self.store.task_status, task.id)
        if status == TaskStatus.RUNNING.value:
            await asyncio.to_thread(self.store.complete, task)
            return
        if await asyncio.to_thread(self.store.cancel, task):
            return
        for domain in task.domains:
            await asyncio.to_thread(self.store.pop_queue, domain, task.id)
        await asyncio.to_thread(self.store.delete_task, task.id)

    def _apply_queue_span_attrs(
        self,
        span: Any,
        task: Task,
        *,
        queue_path: str,
        acquire_reject: str | None = None,
    ) -> None:
        if span is None:
            return
        span.set_attribute(ATTR_QUEUE_PATH, queue_path)
        if acquire_reject:
            span.set_attribute(ATTR_ACQUIRE_REJECT, acquire_reject)

    async def _wait_for_worker_start(
        self,
        task: Task,
        future: asyncio.Future[Any],
        worker_started: asyncio.Event,
        *,
        deadline: float,
    ) -> None:
        """Block until the worker begins the handler or the wait times out."""
        loop = asyncio.get_event_loop()
        while not worker_started.is_set() and not future.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                record_error(ERROR_TOOL_QUEUE_TIMEOUT)
                raise ToolQueueTimeoutError(f"resource queue wait exceeded for tool {task.id}")
            try:
                await asyncio.wait_for(worker_started.wait(), timeout=min(remaining, 0.1))
            except TimeoutError:
                continue

    def _mark_worker_started(self, task_id: str) -> None:
        started = self._worker_started.get(task_id)
        if started is not None and not started.is_set():
            started.set()

    def _resolve(
        self,
        task: Task,
        *,
        value: Any = None,
        error: BaseException | None = None,
    ) -> None:
        future = self._pending.get(task.id)
        if future is None or future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(value)

    # ------------------------------------------------------------------ run lifecycle

    def register_source(self, source_id: str) -> None:
        self.scheduler.register_run(source_id)

    def deregister_source(self, source_id: str) -> None:
        self.scheduler.deregister_run(source_id)

    def cancel_source_units(self, source_id: str) -> int:
        return self.scheduler.cancel_source(source_id)

    # ------------------------------------------------------------------ views

    def snapshot(self) -> dict[str, Any]:
        return self.scheduler.snapshot()

    def pool_snapshots(self) -> list[dict[str, Any]]:
        from scheduler_core.models import TaskKind
        from scheduler_core.redis_store import RedisKeys

        from agents.expert_buddy.core.scheduler.unified_metrics import pool_snapshots_from_unified_snapshot

        snap = self.snapshot()
        domains: dict[str, dict[str, Any]] = {}
        for domain, row in (snap.get("domains") or {}).items():
            enriched = dict(row)
            tool_q = job_q = 0
            for task_id in self.store.queued_ids(domain):
                kind = self.store.client.hget(RedisKeys.task(task_id), "kind") or ""
                if kind == TaskKind.JOB_TASK.value:
                    job_q += 1
                elif kind == TaskKind.AGENT_TOOL.value:
                    tool_q += 1
            enriched["queued_tool"] = tool_q
            enriched["queued_job"] = job_q
            enriched["queued"] = tool_q + job_q
            domains[domain] = enriched
        return pool_snapshots_from_unified_snapshot({**snap, "domains": domains})

    def pool_configs(self) -> dict[str, int]:
        from agents.expert_buddy.core.scheduler.unified_metrics import pool_configs_from_unified_config

        return pool_configs_from_unified_config(self.config)

    def update_pool_capacity(self, resource: str, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("pool capacity must be >= 1")
        if resource not in self.config.domains:
            raise KeyError(f"unknown resource pool: {resource}")
        current = self.config.domains[resource]
        updated = current.model_copy(update={"capacity": capacity})
        self.config.domains[resource] = updated
        self.store._domain_capacities[resource] = capacity


class _StreamingLease:
    """Context-manager-like lease returned by ``bridge.acquire``."""

    __slots__ = ("bridge", "task", "_closed", "_heartbeat_stop", "_heartbeat_task")

    def __init__(self, bridge: UnifiedSchedulerBridge, task: Task) -> None:
        self.bridge = bridge
        self.task = task
        self._closed = False
        self._heartbeat_stop = asyncio.Event()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._start_heartbeat()

    async def __aenter__(self) -> _StreamingLease:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.release()

    def _start_heartbeat(self) -> None:
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        interval = self.bridge.config.heartbeat_interval_seconds
        while not self._heartbeat_stop.is_set():
            await asyncio.sleep(interval)
            if self._heartbeat_stop.is_set():
                break
            alive = await asyncio.to_thread(self.bridge.store.renew, self.task)
            if not alive:
                logger.warning("streaming lease renew failed for %s", self.task.id)
                break

    async def release(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._heartbeat_stop.set()
        hb = self._heartbeat_task
        if hb is not None:
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb
        await self.bridge._release_streaming(self.task)


def _wrap_handler(
    unit: ExecUnit,
    *,
    context: contextvars.Context | None = None,
) -> Callable[[], Awaitable[Any]]:
    """Wrap an ExecUnit's handler with its timeout applied."""

    async def run() -> Any:
        async def execute() -> Any:
            if unit.timeout is not None:
                return await asyncio.wait_for(unit.handler(), timeout=unit.timeout)
            return await unit.handler()

        if context is not None:
            return await asyncio.create_task(execute(), context=context)
        return await execute()

    return run


def _exec_unit_to_task(unit: ExecUnit, *, task_id: str) -> Task:
    """Map a ViKnow ExecUnit to a scheduler-core Task."""
    return Task(
        id=task_id,
        kind=TaskKind.AGENT_TOOL,
        tier=_priority_to_tier(unit.priority),
        domains=list(unit.resources) if unit.resources else [GENERAL_TOOL_DOMAIN],
        unit_kind=_unit_kind(unit.unit_kind),
        run_id=unit.source_id,
        slots=1,
        executor_ref="",
        created_at=task_ts(),
        preemptible=False,
        payload={
            "tool_timeout": unit.timeout,
            "tool_metadata": unit.metadata,
        },
    )


def _slot_request_to_task(request: SlotRequest, *, task_id: str | None = None) -> Task:
    return Task(
        id=task_id or f"stream-{task_ts():.6f}",
        kind=TaskKind.AGENT_TOOL,
        tier=_priority_to_tier(request.priority),
        domains=list(request.resources) if request.resources else [GENERAL_TOOL_DOMAIN],
        unit_kind=_unit_kind(request.unit_kind),
        run_id=request.source_id,
        slots=1,
        executor_ref="",
        created_at=task_ts(),
        preemptible=False,
        payload={"streaming": True, "slot_metadata": request.metadata},
    )


def _is_streaming_task(task: Task) -> bool:
    return bool(task.payload.get("streaming"))


def _priority_to_tier(priority: Priority) -> Tier:
    if priority is Priority.INTERACTIVE:
        return Tier.INTERACTIVE
    if priority is Priority.BACKGROUND:
        return Tier.BACKGROUND
    return Tier.IDLE


def _unit_kind(unit_kind: UnitKind) -> UnitKind:
    return unit_kind


__all__ = ["UnifiedSchedulerBridge"]
