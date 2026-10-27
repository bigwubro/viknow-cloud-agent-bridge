"""ResourceScheduler: resource pool arbitration for Agent tools and models."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from agents.runtimes.deepagents.core.observability.otel import (
    ATTR_PHASE,
    ATTR_RESOURCE,
    ATTR_RUN_ID,
    ATTR_TOOL,
    ERROR_MODEL_SLOT_QUEUE_TIMEOUT,
    ERROR_TOOL_QUEUE_TIMEOUT,
    record_error,
    start_span,
    tool_name_from_metadata,
)
from agents.runtimes.deepagents.core.scheduler.config import ResourcePoolConfig, ResourceSchedulerConfig
from agents.runtimes.deepagents.core.scheduler.domain import (
    ExecUnit,
    Priority,
    RunBudgetCancelledError,
    SlotRequest,
    ToolQueueTimeoutError,
    UnitKind,
)


@dataclass
class _SourceState:
    tools_in_use: int = 0
    models_in_use: int = 0


class _SourceTracker:
    """Track per-run in-flight tool and model units."""

    def __init__(self, config: ResourceSchedulerConfig) -> None:
        self._config = config
        self._states: dict[str, _SourceState] = {}

    def ensure(self, source_id: str) -> None:
        self._states.setdefault(source_id, _SourceState())

    def remove(self, source_id: str) -> None:
        self._states.pop(source_id, None)

    def active_count(self) -> int:
        return len(self._states)

    def in_use(self, source_id: str) -> int:
        state = self._states.get(source_id)
        if state is None:
            return 0
        return state.tools_in_use + state.models_in_use

    def can_acquire(self, source_id: str, unit_kind: UnitKind) -> bool:
        state = self._states.get(source_id)
        if state is None:
            return True
        if unit_kind is UnitKind.MODEL:
            return state.models_in_use < self._config.per_run_max_concurrent_models
        return state.tools_in_use < self._config.per_run_max_concurrent_tools

    def acquire(self, source_id: str, unit_kind: UnitKind) -> None:
        self.ensure(source_id)
        state = self._states[source_id]
        if unit_kind is UnitKind.MODEL:
            state.models_in_use += 1
        else:
            state.tools_in_use += 1

    def release(self, source_id: str, unit_kind: UnitKind) -> None:
        state = self._states.get(source_id)
        if state is None:
            return
        if unit_kind is UnitKind.MODEL:
            state.models_in_use = max(0, state.models_in_use - 1)
        else:
            state.tools_in_use = max(0, state.tools_in_use - 1)


@dataclass
class _Waiter:
    source_id: str
    priority: Priority
    unit_kind: UnitKind
    seq: int
    future: asyncio.Future[bool]


class _ResourcePool:
    """Per-resource slot manager with interactive reservation and fair dequeue."""

    def __init__(
        self,
        resource: str,
        capacity: int,
        interactive_reserved: int,
        *,
        unlimited: bool = False,
    ) -> None:
        self._resource = resource
        self._capacity = capacity
        self._interactive_reserved = interactive_reserved
        self._background_capacity = max(capacity - interactive_reserved, 0)
        self._unlimited = unlimited
        self._in_use_interactive = 0
        self._in_use_background = 0
        self._waiters: list[_Waiter] = []
        self._seq = 0

    @property
    def resource(self) -> str:
        return self._resource

    @property
    def capacity(self) -> int:
        return self._capacity

    def set_capacity(self, capacity: int, *, interactive_reserved: int) -> None:
        if capacity < 1:
            raise ValueError("pool capacity must be >= 1")
        self._capacity = capacity
        self._interactive_reserved = max(0, interactive_reserved)
        self._background_capacity = max(capacity - self._interactive_reserved, 0)

    @property
    def in_use(self) -> int:
        return self._in_use_interactive + self._in_use_background

    @property
    def in_use_interactive(self) -> int:
        return self._in_use_interactive

    @property
    def in_use_background(self) -> int:
        return self._in_use_background

    @property
    def queued(self) -> int:
        return len(self._waiters)

    def snapshot(self) -> dict[str, Any]:
        return {
            "resource": self._resource,
            "capacity": self._capacity,
            "in_use": self.in_use,
            "in_use_interactive": self._in_use_interactive,
            "in_use_background": self._in_use_background,
            "queued": self.queued,
        }

    def _pool_can_acquire(self, priority: Priority) -> bool:
        if self._unlimited:
            return True
        if priority is Priority.INTERACTIVE:
            return self.in_use < self._capacity
        return self._in_use_background < self._background_capacity

    def _do_acquire(self, priority: Priority) -> None:
        if priority is Priority.INTERACTIVE:
            self._in_use_interactive += 1
        else:
            self._in_use_background += 1

    def _do_release(self, priority: Priority) -> None:
        if priority is Priority.INTERACTIVE:
            self._in_use_interactive -= 1
        else:
            self._in_use_background -= 1

    def _can_dispatch(self, waiter: _Waiter, tracker: _SourceTracker) -> bool:
        if not tracker.can_acquire(waiter.source_id, waiter.unit_kind):
            return False
        return self._pool_can_acquire(waiter.priority)

    def _try_acquire_or_queue(
        self,
        *,
        source_id: str,
        priority: Priority,
        unit_kind: UnitKind,
        future: asyncio.Future[bool],
        tracker: _SourceTracker,
    ) -> bool:
        probe = _Waiter(
            source_id=source_id,
            priority=priority,
            unit_kind=unit_kind,
            seq=0,
            future=future,
        )
        if self._can_dispatch(probe, tracker):
            tracker.acquire(source_id, unit_kind)
            self._do_acquire(priority)
            return True
        self._seq += 1
        self._waiters.append(
            _Waiter(
                source_id=source_id,
                priority=priority,
                unit_kind=unit_kind,
                seq=self._seq,
                future=future,
            )
        )
        return False

    def _notify_next(self, tracker: _SourceTracker) -> None:
        if not self._waiters:
            return
        eligible = [w for w in self._waiters if self._can_dispatch(w, tracker)]
        if not eligible:
            return
        min_rank = min(w.priority.rank for w in eligible)
        tier = [w for w in eligible if w.priority.rank == min_rank]
        tier.sort(key=lambda w: (tracker.in_use(w.source_id), w.seq))
        waiter = tier[0]
        self._waiters.remove(waiter)
        tracker.acquire(waiter.source_id, waiter.unit_kind)
        self._do_acquire(waiter.priority)
        if not waiter.future.done():
            waiter.future.set_result(True)

    def release(
        self,
        priority: Priority,
        source_id: str,
        unit_kind: UnitKind,
        tracker: _SourceTracker,
    ) -> None:
        """Return pool and per-run slots. Caller must notify waiters afterwards."""

        self._do_release(priority)
        tracker.release(source_id, unit_kind)

    def cancel_waiters(
        self,
        source_id: str,
        *,
        error: BaseException | None = None,
    ) -> int:
        cancelled = 0
        remaining: list[_Waiter] = []
        for waiter in self._waiters:
            if waiter.source_id == source_id and not waiter.future.done():
                if error is not None:
                    waiter.future.set_exception(error)
                else:
                    waiter.future.cancel()
                cancelled += 1
            else:
                remaining.append(waiter)
        self._waiters = remaining
        return cancelled


class ResourceScheduler:
    """Per-Agent resource pool arbitration for tool calls and chat model streams.

    Owned by one Agent Profile runtime. Same-profile runs share this instance;
    different profiles use different instances with independent pool counters.

    When ``unified_bridge`` is provided (and the bridge is enabled), tool/model
    requests and source lifecycle route through scheduler-core instead of the
    in-memory pools; the legacy pool machinery stays intact and untouched.
    """

    def __init__(
        self,
        config: ResourceSchedulerConfig,
        *,
        name: str = "resource-scheduler",
        unified_bridge: Any | None = None,
    ) -> None:
        self._config = config
        self._name = name
        self._unified_bridge = unified_bridge
        self._pools: dict[str, _ResourcePool] = {}
        self._source_tracker = _SourceTracker(config)
        self._init_pools()
        self._passthrough_pool = _ResourcePool(
            resource="__passthrough__",
            capacity=0,
            interactive_reserved=0,
            unlimited=True,
        )
        self._inflight_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._budget_cancelled_tasks: set[asyncio.Task[Any]] = set()
        self._budget_cancel_dimensions: dict[str, tuple[str, ...]] = {}
        self._budget_exhausted_sources: set[str] = set()

    @property
    def default_execution_timeout_seconds(self) -> float:
        return self._config.default_execution_timeout_seconds

    @property
    def load(self) -> float:
        return self._pool_load()

    @property
    def active_runs(self) -> int:
        if self._unified_active():
            snap = self._unified_bridge.snapshot()
            return int(snap.get("active_runs") or 0)
        return self._source_tracker.active_count()

    def _unified_active(self) -> bool:
        bridge = self._unified_bridge
        return bridge is not None and bool(getattr(bridge.config, "enabled", False))

    @property
    def scheduler_mode(self) -> str:
        return "unified" if self._unified_active() else "legacy"

    def _pool_load(self) -> float:
        snapshots = self.pool_snapshots()
        if not snapshots:
            return 0.0
        utils = [
            (item["in_use"] / item["capacity"])
            for item in snapshots
            if int(item.get("capacity") or 0) > 0
        ]
        return max(utils) if utils else 0.0

    def _init_pools(self) -> None:
        for resource, pool_cfg in self._config.resources.items():
            self._pools[resource] = _ResourcePool(
                resource=resource,
                capacity=pool_cfg.capacity,
                interactive_reserved=self._config.interactive_reserved(resource),
            )

    def pool_snapshots(self) -> list[dict[str, Any]]:
        """Return in-use / capacity / queued counts for configured pools."""

        if self._unified_active():
            return self._unified_bridge.pool_snapshots()
        return [pool.snapshot() for pool in self._pools.values()]

    def pool_configs(self) -> dict[str, int]:
        """Return current effective capacity per named pool."""

        if self._unified_active():
            return self._unified_bridge.pool_configs()
        return {resource: pool.capacity for resource, pool in self._pools.items()}

    def update_pool_capacity(self, resource: str, capacity: int) -> None:
        """Hot-update one pool's capacity (in-memory; does not persist)."""

        if self._unified_active():
            self._unified_bridge.update_pool_capacity(resource, capacity)
            return
        if capacity < 1:
            raise ValueError("pool capacity must be >= 1")
        pool = self._pools.get(resource)
        if pool is None:
            raise KeyError(f"unknown resource pool: {resource}")
        self._config.resources[resource] = ResourcePoolConfig(capacity=capacity)
        pool.set_capacity(
            capacity,
            interactive_reserved=self._config.interactive_reserved(resource),
        )
        pool._notify_next(self._source_tracker)
        for other in self._iter_pools():
            if other is pool:
                continue
            other._notify_next(self._source_tracker)

    def register_source(self, source_id: str) -> None:
        if self._unified_bridge is not None:
            self._unified_bridge.register_source(source_id)
            return
        self._source_tracker.ensure(source_id)
        self._budget_exhausted_sources.discard(source_id)
        self._budget_cancel_dimensions.pop(source_id, None)

    def deregister_source(self, source_id: str) -> None:
        if self._unified_bridge is not None:
            self._unified_bridge.deregister_source(source_id)
            return
        for pool in self._pools.values():
            pool.cancel_waiters(source_id)
        self._passthrough_pool.cancel_waiters(source_id)
        self._source_tracker.remove(source_id)
        self._budget_exhausted_sources.discard(source_id)
        self._budget_cancel_dimensions.pop(source_id, None)

    def cancel_source_units(
        self,
        source_id: str,
        *,
        dimensions: tuple[str, ...] | list[str] | None = None,
    ) -> int:
        """Cancel queued waiters and in-flight tool units for run budget truncation."""

        if self._unified_bridge is not None:
            cancelled = self._unified_bridge.cancel_source_units(source_id)
            dims = tuple(dimensions or ())
            self._budget_cancel_dimensions[source_id] = dims
            self._budget_exhausted_sources.add(source_id)
            return cancelled
        cancelled = 0
        dims = tuple(dimensions or ())
        self._budget_cancel_dimensions[source_id] = dims
        self._budget_exhausted_sources.add(source_id)
        error = RunBudgetCancelledError(
            f"run budget exhausted for source {source_id}",
            dimensions=dims,
        )
        for pool in self._iter_pools():
            cancelled += pool.cancel_waiters(source_id, error=error)
        tasks = list(self._inflight_tasks.get(source_id, ()))
        for task in tasks:
            if task.done():
                continue
            self._budget_cancelled_tasks.add(task)
            task.cancel()
            cancelled += 1
        return cancelled

    def consume_budget_cancelled(self, task: asyncio.Task[Any] | None = None) -> bool:
        """Return True once if ``task`` was cancelled by ``cancel_source_units``."""

        current = task or asyncio.current_task()
        if current is None or current not in self._budget_cancelled_tasks:
            return False
        self._budget_cancelled_tasks.discard(current)
        return True

    def budget_cancel_dimensions(self, source_id: str) -> tuple[str, ...]:
        """Return dimensions recorded by the latest ``cancel_source_units`` for ``source_id``."""

        return self._budget_cancel_dimensions.get(source_id, ())

    def is_budget_exhausted(self, source_id: str) -> bool:
        """True once the run budget was exhausted for ``source_id``."""

        return source_id in self._budget_exhausted_sources

    def _resolve_pool(self, resource: str | None) -> _ResourcePool:
        if not resource:
            return self._passthrough_pool
        pool = self._pools.get(resource)
        if pool is None:
            raise RuntimeError(
                f"resource pool {resource!r} is not configured in scheduler.resource.resources"
            )
        return pool

    def _iter_pools(self) -> tuple[_ResourcePool, ...]:
        return (*self._pools.values(), self._passthrough_pool)

    def _release_and_notify(
        self,
        pool: _ResourcePool,
        priority: Priority,
        source_id: str,
        unit_kind: UnitKind,
    ) -> None:
        """Release slots, then wake waiters that may now be dispatchable.

        Prefer the pool that just gained capacity, then scan other pools.
        Per-run limits are global, so a release on pool A can unblock a waiter
        queued on pool B that was only waiting on ``_SourceTracker``.
        Each ``_notify_next`` re-checks tracker/pool caps before dispatch, so a
        single release cannot over-admit beyond the freed slots.
        """

        pool.release(priority, source_id, unit_kind, self._source_tracker)
        pool._notify_next(self._source_tracker)
        for other in self._iter_pools():
            if other is pool:
                continue
            other._notify_next(self._source_tracker)

    @asynccontextmanager
    async def acquire(self, request: SlotRequest) -> AsyncIterator[None]:
        """Hold a pool slot for the duration of a streaming or long-lived unit."""

        resource = request.resources[0] if request.resources else None
        wait_attrs = {
            ATTR_PHASE: "model.wait_slot",
            ATTR_RESOURCE: resource,
            ATTR_RUN_ID: request.source_id,
        }
        if self._unified_bridge is not None:
            try:
                async with start_span("model.wait_slot", **wait_attrs):
                    lease = await self._unified_bridge.acquire(request)
            except TimeoutError:
                record_error(ERROR_MODEL_SLOT_QUEUE_TIMEOUT)
                raise
            try:
                yield
            finally:
                await lease.release()
            return

        self._source_tracker.ensure(request.source_id)
        pool = self._resolve_pool(resource)

        try:
            async with start_span("model.wait_slot", **wait_attrs):
                acquired = await self._acquire_pool(
                    pool,
                    request.priority,
                    request.source_id,
                    request.unit_kind,
                )
        except TimeoutError:
            record_error(ERROR_MODEL_SLOT_QUEUE_TIMEOUT)
            raise
        if not acquired:
            record_error(ERROR_MODEL_SLOT_QUEUE_TIMEOUT)
            raise TimeoutError(
                f"resource '{resource or pool.resource}' queue wait exceeded "
                f"{self._config.queue_wait_timeout_seconds:g}s"
            )

        try:
            yield
        finally:
            self._release_and_notify(pool, request.priority, request.source_id, request.unit_kind)

    async def run(self, unit: ExecUnit) -> Any:
        if self._unified_bridge is not None:
            if unit.source_id in self._budget_exhausted_sources:
                raise RunBudgetCancelledError(
                    f"run budget exhausted for source {unit.source_id}",
                    dimensions=self.budget_cancel_dimensions(unit.source_id),
                )
            return await self._unified_bridge.run_tool(unit)
        if unit.source_id in self._budget_exhausted_sources:
            raise RunBudgetCancelledError(
                f"run budget exhausted for source {unit.source_id}",
                dimensions=self.budget_cancel_dimensions(unit.source_id),
            )
        self._source_tracker.ensure(unit.source_id)
        resource = unit.resources[0] if unit.resources else None
        pool = self._resolve_pool(resource)
        tool_name = tool_name_from_metadata(unit.metadata)

        async with start_span(
            "tool.wait_queue",
            **{
                ATTR_PHASE: "tool.wait_queue",
                ATTR_RESOURCE: resource,
                ATTR_RUN_ID: unit.source_id,
                ATTR_TOOL: tool_name,
            },
        ):
            acquired = await self._acquire_pool(
                pool,
                unit.priority,
                unit.source_id,
                unit.unit_kind,
            )
        if not acquired:
            record_error(ERROR_TOOL_QUEUE_TIMEOUT)
            raise ToolQueueTimeoutError(
                f"resource '{resource or pool.resource}' queue wait exceeded "
                f"{self._config.queue_wait_timeout_seconds:g}s"
            )

        exec_task = asyncio.create_task(self._execute_unit(unit))
        self._inflight_tasks.setdefault(unit.source_id, set()).add(exec_task)
        try:
            async with start_span(
                "tool.work",
                **{
                    ATTR_PHASE: "tool.work",
                    ATTR_RESOURCE: resource,
                    ATTR_RUN_ID: unit.source_id,
                    ATTR_TOOL: tool_name,
                },
            ):
                return await exec_task
        except asyncio.CancelledError:
            if self.consume_budget_cancelled(exec_task):
                dims = self.budget_cancel_dimensions(unit.source_id)
                raise RunBudgetCancelledError(
                    f"run budget exhausted for source {unit.source_id}",
                    dimensions=dims,
                ) from None
            raise
        finally:
            inflight = self._inflight_tasks.get(unit.source_id)
            if inflight is not None:
                inflight.discard(exec_task)
                if not inflight:
                    self._inflight_tasks.pop(unit.source_id, None)
            self._budget_cancelled_tasks.discard(exec_task)
            self._release_and_notify(pool, unit.priority, unit.source_id, unit.unit_kind)

    async def _acquire_pool(
        self,
        pool: _ResourcePool,
        priority: Priority,
        source_id: str,
        unit_kind: UnitKind,
    ) -> bool:
        future: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
        if pool._try_acquire_or_queue(
            source_id=source_id,
            priority=priority,
            unit_kind=unit_kind,
            future=future,
            tracker=self._source_tracker,
        ):
            return True

        timeout = (
            self._config.queue_wait_timeout_seconds if priority is Priority.INTERACTIVE else None
        )
        try:
            await asyncio.wait_for(future, timeout=timeout)
            return True
        except RunBudgetCancelledError:
            pool._waiters = [w for w in pool._waiters if w.future is not future]
            raise
        except (TimeoutError, asyncio.CancelledError):
            pool._waiters = [w for w in pool._waiters if w.future is not future]
            return False

    async def _execute_unit(self, unit: ExecUnit) -> Any:
        timeout = (
            unit.timeout
            if unit.timeout is not None
            else self._config.default_execution_timeout_seconds
        )
        return await asyncio.wait_for(unit.handler(), timeout=timeout)
