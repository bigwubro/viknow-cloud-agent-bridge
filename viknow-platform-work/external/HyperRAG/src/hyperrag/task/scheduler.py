"""
任务调度器

统一的任务调度管理，包含：
- Job提交和查询
- 任务执行调度
- 模式管理（RO/RW切换）
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from hyperrag.utils.logging_context import logging_context

from ..config_loader import TaskLimitConfig
from .models import JobInstance, Task, TaskResult, TaskStatus
from .queue import DistributedTaskQueue
from .registry import CallbackRegistry, TaskRegistry

logger = logging.getLogger(__name__)

# Worker 在有任务在跑时也必须 sleep，否则会空转占满 GIL，拖死同进程的 uvicorn。
_UNIFIED_IDLE_SLEEP_SECONDS = 0.1
_UNIFIED_BUSY_SLEEP_SECONDS = 0.05


def unified_worker_idle_seconds(*, executed: bool, running_count: int) -> float:
    """Return how long the unified worker should yield before the next poll.

    Invariant: the value is always > 0. A zero-sleep loop while jobs are in
    flight starves other Python threads in this process (API HTTP included).
    """
    if running_count <= 0 and not executed:
        return _UNIFIED_IDLE_SLEEP_SECONDS
    return _UNIFIED_BUSY_SLEEP_SECONDS


# DAG 推进写操作的重试次数与退避（秒）：即使 L1 连接层重试被瞬时抖动绕过，
# 关键路径的激活/状态写操作仍可自愈，避免单次超时永久断链。
_DAG_WRITE_RETRIES = 3
_DAG_WRITE_BACKOFF = 0.2
# DAG 补偿扫描的周期（秒）：L1/L2 都失败时的最后兜底。
DAG_RECOVERY_INTERVAL_SECONDS = 30


async def _run_with_retry(
    operation: Callable[[], Awaitable[object]],
    *,
    retries: int = _DAG_WRITE_RETRIES,
    backoff: float = _DAG_WRITE_BACKOFF,
    operation_name: str = "redis operation",
) -> object:
    """以指数退避重试一个可安全重试的（幂等）Redis 写操作。

    仅捕获瞬时网络错误（读超时 / 连接错误）。重试耗尽后抛回最后一个异常，
    交由上层（worker loop / 补偿扫描）决定是否重试。
    """
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await operation()
        except (TimeoutError, RedisTimeoutError, RedisConnectionError, OSError) as e:
            last_error = e
            if attempt < retries:
                logger.warning(
                    "%s failed (attempt %s/%s): %s; retrying in %.1fs",
                    operation_name,
                    attempt + 1,
                    retries + 1,
                    e,
                    backoff * (2**attempt),
                )
                await asyncio.sleep(backoff * (2**attempt))
    assert last_error is not None
    raise last_error


class InstanceMode(str, Enum):
    """实例模式"""

    READ_ONLY = "read_only"  # 可查询、可提交，不执行
    READ_WRITE = "read_write"  # 可查询、可提交、可执行


class TaskScheduler:
    """
    统一的任务调度器

    特性：
    - 封装queue在内部
    - 单一worker循环
    - 支持Job提交、查询、重试
    - 集成模式管理（RO/RW切换）
    """

    def __init__(
        self,
        queue: DistributedTaskQueue,
        instance_id: str,
        max_concurrent_job_tasks: int,
        task_limits: dict[str, TaskLimitConfig] | None = None,
        enable_rw_lock: bool = False,
        on_job_callback: Callable[[JobInstance, str | None, str], Awaitable[None]] | None = None,
        on_task_callback: Callable[[Task, str | None, str], Awaitable[None]] | None = None,
        unified_scheduler: Any | None = None,
    ):
        """
        初始化任务调度器

        Args:
            queue: 分布式任务队列
            instance_id: 实例ID
            max_concurrent_job_tasks: 统一调度开态下 worker 同时执行的 job task 上限
            task_limits: 任务并发限制配置（可选，如果为None则使用默认值）
            enable_rw_lock: 是否启用RW分布式锁
            on_job_callback: Job状态变更回调
            on_task_callback: Task状态变更回调
            unified_scheduler: scheduler-core 统一调度器实例。提供时（且其配置启用），
                job 提交与 worker 取活走统一调度；为 None 时保持原 HyperRAG 调度逻辑不变。
        """
        self._queue = queue  # 封装在内部
        self._instance_id = instance_id
        self._enable_rw_lock = enable_rw_lock
        self._on_job_callback = on_job_callback
        self._on_task_callback = on_task_callback
        self._gpu_available: int | None = None
        self._parsed_limits: dict[str, TaskLimitConfig] | None = task_limits
        self._max_concurrent_job_tasks = max(1, max_concurrent_job_tasks)
        self._unified_scheduler = unified_scheduler

        self._worker_task: asyncio.Task | None = None
        self._unified_worker_task: asyncio.Task | None = None
        #: DAG 补偿扫描任务（周期性自愈卡死 job）
        self._recovery_task: asyncio.Task | None = None
        self._running = False
        self._running_tasks: dict[str, tuple[asyncio.Task, Task]] = {}
        #: 开态下统一任务的运行协程（task_id -> asyncio.Task）
        self._unified_running: dict[str, asyncio.Task] = {}

        self._mode = InstanceMode.READ_ONLY

        logger.info(f"TaskScheduler initialized for instance: {instance_id}")

    @property
    def unified_enabled(self) -> bool:
        """True 时 job/task 走统一调度（scheduler-core），False 时走原 HyperRAG 调度。"""
        return (
            self._unified_scheduler is not None
            and bool(getattr(self._unified_scheduler, "config", None))
            and getattr(self._unified_scheduler.config, "enabled", False)
        )

    def _unified(self) -> Any:
        """返回统一调度器实例（仅 unified_enabled 时非空）。"""
        return self._unified_scheduler if self.unified_enabled else None

    # ========== 对外接口 ==========

    async def submit_job(self, job: JobInstance, tasks: list[Task]) -> str:
        """
        提交Job到调度器

        Args:
            job: Job实例
            tasks: 任务列表

        Returns:
            job_id: Job ID
        """
        await self._queue.save_job(job)
        await self._emit_job_status(job, None)
        try:
            from hyperrag.observability.index_usage import on_job_submitted

            on_job_submitted(job)
        except Exception:
            logger.warning("[index-usage] on_job_submitted failed", exc_info=True)

        if self._unified() is not None:
            await self._submit_job_unified(job, tasks)
            return job.job_id

        for task in tasks:
            await self._queue.enqueue(task)
            await self._emit_task_status(task, None)

        logger.info(
            f"Job submitted: {job.job_id} with {len(tasks)} tasks "
            f"(asset={job.asset_id}, workspace={job.workspace})"
        )

        return job.job_id

    async def _submit_job_unified(self, job: JobInstance, tasks: list[Task]) -> str:
        """统一调度开态下的 Job 提交。

        保留 hyperrag:job:* / hyperrag:task:* 元数据与状态回写（asset_status），
        但不写入 hyperrag:queue:{task_type}。DAG 注册与根任务入队由
        scheduler-core ``submit_job`` 负责；下游推进由 ``complete.lua`` 负责。
        """
        for task in tasks:
            await self._queue.save_task(task)
            await self._emit_task_status(task, None)

        from scheduler_core.models import JobSpec

        job_spec = JobSpec(
            job_id=job.job_id,
            tasks=[self._hyperrag_to_unified_task(t) for t in tasks],
        )
        await self._unified().submit_job(job_spec)

        logger.info(
            f"Job submitted via unified scheduler: {job.job_id} with {len(tasks)} tasks "
            f"(asset={job.asset_id}, workspace={job.workspace})"
        )
        return job.job_id

    def _hyperrag_to_unified_task(self, task: Task):
        """将 HyperRAG Task 转为 scheduler-core Task（不含入队）。"""
        from scheduler_core.models import Task as UnifiedTask
        from scheduler_core.models import TaskKind, Tier

        return UnifiedTask(
            id=task.task_id,
            kind=TaskKind.JOB_TASK,
            tier=Tier.BACKGROUND,
            domains=self._hyperrag_domains(task.task_type),
            job_id=task.job_id or "",
            depends_on=list(task.depends_on or []),
            task_type=task.task_type,
            executor_ref=task.task_type,
            preemptible=True,
            payload={
                "hyperrag_task_id": task.task_id,
                "hyperrag_job_id": task.job_id or "",
            },
        )

    async def _enqueue_unified_store(self, task: Task) -> None:
        """将 HyperRAG 任务重新入 scheduler-core 域队列（补偿扫描用）。"""
        utask = self._hyperrag_to_unified_task(task)
        await asyncio.to_thread(self._unified().store.enqueue, utask)

    @staticmethod
    def _hyperrag_domains(task_type: str) -> list[str]:
        """HyperRAG task 类型到 scheduler-core 资源域的映射（多域）。

        与 ViKnow 工具侧的域对齐：job 任务与在线工具共享同一批模型端点，
        只有映射到同一域才能让两端在真实并发（vllm / embedding / asr）上
        互相约束，而不是各自排队互不感知。

        - video_parse / sop_video_parse：VLM（vision caption）+ ASR + 切分，
          同时占用 vllm 与 asr 两个端点。
        - kg_index：LLM 抽取和统一 embedding。
        - bm25_index：LLM 索引。
        - delete_asset / doc_parse 等：数据库 / 纯计算，无模型端点。
        """
        vlm_types = {
            "video_caption",
            "video_tracking",
            "update_caption",
            "video_parse",
            "sop_video_parse",
            "image_caption",
        }
        asr_types = {"video_asr", "video_parse", "sop_video_parse"}
        llm_types = {"bm25_index"}

        if task_type == "kg_index":
            return ["vllm", "embedding"]
        if task_type in vlm_types and task_type in asr_types:
            return ["vllm", "asr"]
        if task_type in vlm_types:
            return ["vllm"]
        if task_type in asr_types:
            return ["asr"]
        if task_type in llm_types:
            return ["vllm"]
        return ["hyperrag-db"]

    async def get_job_status(self, job_id: str) -> dict | None:
        """
        查询Job状态

        Args:
            job_id: Job ID

        Returns:
            Job状态字典
        """
        job = await self._queue.get_job(job_id)
        return job.to_dict() if job else None

    async def get_task_status(self, task_id: str) -> dict | None:
        """
        查询Task状态

        Args:
            task_id: 任务ID

        Returns:
            任务状态字典
        """
        task = await self._queue.get_task(task_id)
        return task.to_dict() if task else None

    async def retry_job(self, job_id: str) -> bool:
        """
        重试失败的Job

        逻辑：
        1. 查询Job和所有Task
        2. 找到第一个failed的Task
        3. 将该Task及后续Task状态改为queued/pending
        4. 将Job状态改为running

        Args:
            job_id: Job ID

        Returns:
            是否重试成功
        """
        job = await self._queue.get_job(job_id)
        if not job or job.status != "failed":
            logger.warning(
                f"Cannot retry job {job_id}: status={job.status if job else 'not_found'}"
            )
            return False

        # 找到第一个失败的任务
        first_failed_idx = None
        for i, task_id in enumerate(job.task_ids):
            task = await self._queue.get_task(task_id)
            if task and task.status == TaskStatus.FAILED:
                first_failed_idx = i
                break

        if first_failed_idx is None:
            logger.warning(f"No failed task found in job {job_id}")
            return False

        # 重置任务状态
        for i in range(first_failed_idx, len(job.task_ids)):
            task_id = job.task_ids[i]
            task = await self._queue.get_task(task_id)
            if task:
                prev_status = task.status
                if i == first_failed_idx:
                    task.status = TaskStatus.QUEUED
                    task.error_message = None
                    task.started_at = None
                    task.completed_at = None
                else:
                    task.status = TaskStatus.PENDING

                # 使用 enqueue，它会根据状态自动决定是否入队
                await self._queue.enqueue(task)
                await self._emit_task_status(task, prev_status)

        # 更新Job状态
        prev_job_status = job.status
        job.status = "running"
        job.current_stage = first_failed_idx
        job.completed_at = None
        job.error_message = None
        await self._queue.update_job(job)
        await self._emit_job_status(job, prev_job_status)

        logger.info(f"Job {job_id} retry initiated from stage {first_failed_idx}")
        return True

    # ========== Worker循环 ==========

    async def start(self):
        """启动调度器（RW模式）"""
        if self._running:
            logger.warning("Scheduler already running")
            return

        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        # DAG 补偿扫描：周期性自愈因 Redis 瞬时故障而卡死的 job
        self._recovery_task = asyncio.create_task(self._recovery_loop())
        logger.info("TaskScheduler started")

    async def stop(self):
        """停止调度器（RO模式或关闭）"""
        if not self._running:
            return

        self._running = False

        # 取消所有running任务
        await self._fail_all_running_tasks(reason="Scheduler stopped")

        # 开态下统一任务的运行协程：不硬杀（租约 TTL 回收），仅取消登记协程
        for t in list(self._unified_running.values()):
            t.cancel()
        self._unified_running.clear()

        # 取消补偿扫描
        if self._recovery_task:
            self._recovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recovery_task
            self._recovery_task = None

        # 取消worker
        if self._worker_task:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None

        logger.info("TaskScheduler stopped")

    async def _recovery_loop(self):
        """周期性补偿扫描：自愈因 Redis 瞬时故障而卡死的 job。

        网络抖动时 _trigger_next_stage 可能中断，导致已完成任务的下游
        永远不被激活（job 停在 running）。本循环定期重新激活这些任务，
        是 DAG 断链的最后兜底（L1 连接层重试、L2 关键写重试之上的第三层）。
        """
        logger.info("DAG recovery loop started")
        while self._running:
            try:
                await asyncio.sleep(DAG_RECOVERY_INTERVAL_SECONDS)
                await self._recover_stalled_jobs()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"DAG recovery error: {e}")
        logger.info("DAG recovery loop stopped")

    async def _recover_stalled_jobs(self) -> int:
        """扫描所有 running job，重新激活第一个依赖已满足但未被激活的任务。

        判定"未激活"：
        - 原调度：任务状态为 PENDING（从未入队），或 QUEUED 但不在
          ``hyperrag:queue:{task_type}`` 中（入队丢失）。
        - 统一调度：由 scheduler-core ``recover_dag`` 扫描 DAG 并补入队；
          HyperRAG 侧仅补偿 job 级完成标记。

        返回本次重新激活的任务数。每次只激活第一个满足条件的任务（串行 DAG）。
        """
        recovered = 0
        try:
            jobs = await self._queue.list_running_jobs()
        except Exception as e:
            logger.warning(f"list_running_jobs failed during recovery: {e}")
            return 0

        if self._unified() is not None:
            try:
                recovered += await asyncio.to_thread(self._unified().store.recover_dag)
            except Exception as e:
                logger.warning(f"scheduler-core recover_dag failed: {e}")

        for job in jobs:
            try:
                if self._unified() is not None:
                    if await self._recover_one_job_unified(job):
                        recovered += 1
                elif await self._recover_one_job(job):
                    recovered += 1
            except Exception as e:
                logger.warning(f"Recovery failed for job {job.job_id}: {e}")
        if recovered:
            logger.info(f"DAG recovery activated {recovered} stalled task(s)")
        return recovered

    async def _recover_one_job_unified(self, job: JobInstance) -> bool:
        """统一调度开态：仅补偿 job 完成标记（DAG 入队由 scheduler-core 负责）。"""
        all_completed = True
        for tid in job.task_ids:
            task = await self._queue.get_task(tid)
            if task is not None and task.status == TaskStatus.COMPLETED:
                admission = task.metadata.get("_unified_admission")
                if admission and tid not in self._unified_running:
                    from .unified_completion import finish_owned_task

                    utask = self._hyperrag_to_unified_task(task)
                    await asyncio.to_thread(
                        finish_owned_task,
                        self._unified().store,
                        utask,
                        started_at=admission["started_at"],
                        seq=admission["seq"],
                        hyperrag_task=task,
                        require_expired=True,
                    )
            if task is None or task.status == TaskStatus.COMPLETED:
                continue
            all_completed = False
            break

        if all_completed and job.status != "completed":
            prev_job_status = job.status
            job.status = "completed"
            job.completed_at = time.time()
            await _run_with_retry(
                lambda: self._queue.update_job(job),
                operation_name=f"recover update_job({job.job_id})",
            )
            await self._emit_job_status(job, prev_job_status)
            logger.warning(
                "DAG recovery marked job completed: %s (all tasks completed)",
                job.job_id,
            )
            return True
        return False

    async def _recover_one_job(self, job: JobInstance) -> bool:
        """尝试重新激活 job 中第一个依赖已满足但未入队的任务。"""
        all_completed = True
        for tid in job.task_ids:
            task = await self._queue.get_task(tid)
            if task is None or task.status == TaskStatus.COMPLETED:
                continue

            all_completed = False

            # 跳过正在运行的任务（job 正常推进中）
            if task.status == TaskStatus.RUNNING:
                return False

            # 任务已激活但丢失了队列位置 → 重新入队
            if task.status == TaskStatus.QUEUED:
                if await self._task_in_queue(task):
                    return False
                await self._reactivate_task(task)
                return True

            # PENDING 但依赖已满足 → 激活
            if task.status == TaskStatus.PENDING:
                if not await self._check_dependencies(task):
                    return False
                await self._reactivate_task(task)
                return True

            # FAILED / CANCELLED 等异常终态不在此恢复
            return False

        # 所有任务已完成但 job 仍是 running（上次 update_job 因 Redis 故障中断）→ 标记完成
        if all_completed and job.status != "completed":
            prev_job_status = job.status
            job.status = "completed"
            job.completed_at = time.time()
            await _run_with_retry(
                lambda: self._queue.update_job(job),
                operation_name=f"recover update_job({job.job_id})",
            )
            await self._emit_job_status(job, prev_job_status)
            logger.warning(
                "DAG recovery marked job completed: %s (all tasks completed)",
                job.job_id,
            )
            return True
        return False

    async def _reactivate_task(self, task: Task) -> None:
        """将任务置为 QUEUED 并重新入队（幂等，供补偿扫描使用）。"""
        prev_status = task.status
        task.status = TaskStatus.QUEUED
        if self._unified() is not None:
            await _run_with_retry(
                lambda t=task: self._queue.save_task(t),
                operation_name=f"recover save_task({task.task_id})",
            )
            await self._emit_task_status(task, prev_status)
            await _run_with_retry(
                lambda t=task: self._enqueue_unified_store(t),
                operation_name=f"recover enqueue_unified({task.task_id})",
            )
        else:
            await _run_with_retry(
                lambda t=task: self._queue.enqueue(t),
                operation_name=f"recover enqueue({task.task_id})",
            )
            await self._emit_task_status(task, prev_status)
        logger.warning(
            "DAG recovery re-activated task: %s (job=%s, type=%s)",
            task.task_id,
            task.job_id,
            task.task_type,
        )

    async def _task_in_queue(self, task: Task) -> bool:
        """判断任务是否已在（当前模式的）等待队列中。"""
        if self._unified() is not None:
            from scheduler_core.redis_store import RedisKeys

            # 任务可能同时位于多个域队列（如 video_parse 在 vllm + asr），
            # 任一队列中存在即视为已入队，避免补偿扫描重复激活。
            for domain in self._hyperrag_domains(task.task_type):
                key = RedisKeys.queue(domain)
                exists = await self._queue.redis.zscore(key, task.task_id)
                if exists is not None:
                    return True
            return False

        key = self._queue.TASK_QUEUE.format(task_type=task.task_type)
        exists = await self._queue.redis.zscore(key, task.task_id)
        return exists is not None

    async def _worker_loop(self):
        """
        单一Worker循环

        逻辑：
        1. 遍历所有任务类型，拉取queued任务
        2. 检查资源是否可用
        3. 执行任务（异步）
        4. 根据结果更新状态
        5. 触发下一阶段或完成Job

        统一调度开态下，改从 scheduler-core 队列取活（handler 逻辑完全不变）。
        """
        if self._unified() is not None:
            await self._unified_worker_loop()
            return

        logger.info("Worker loop started")

        while self._running:
            try:
                executed = False

                # 获取所有任务类型
                task_types = TaskRegistry.list_types()
                if not task_types:
                    logger.warning("No task types registered")
                    await asyncio.sleep(1.0)
                    continue

                # 遍历所有任务类型，寻找可执行的任务
                for task_type in task_types:
                    resource = TaskRegistry.get(task_type).resource
                    limit = self._get_limit(task_type)

                    # 如果没有可运行的槽位，跳过
                    running_task_count = len(
                        [
                            task
                            for _, task in self._running_tasks.values()
                            if task.task_type == task_type
                        ]
                    )
                    if running_task_count >= limit.max_concurrent:
                        continue

                    # 检查该类型是否还有可用资源
                    if not await self._can_acquire(task_type, resource):
                        continue

                    # 从队列拉取任务
                    task = await self._queue.dequeue(task_type, timeout=0.0)
                    if task is None:
                        continue

                    # 申请资源
                    if not await self._acquire(task.task_id, task.task_type, task.resource):
                        await self._queue.requeue(task)
                        continue

                    # 执行任务（异步）
                    coro_task = asyncio.create_task(self._execute_task(task))
                    self._running_tasks[task.task_id] = (coro_task, task)
                    executed = True

                    logger.debug(
                        f"Task dispatched: {task.task_id} "
                        f"(type={task.task_type}, running={len(self._running_tasks)})"
                    )

                # 如果没有执行任何任务，短暂休眠
                if not executed:
                    await asyncio.sleep(0.1)

            except asyncio.CancelledError:
                logger.info("Worker loop cancelled")
                break
            except Exception as e:
                logger.exception(f"Worker loop error: {e}")
                await asyncio.sleep(1.0)

        logger.info("Worker loop exited")

    async def _unified_worker_loop(self):
        """统一调度开态下的 worker 循环。

        从 scheduler-core 队列（按资源域）取活，取到的统一任务转回 HyperRAG
        ``Task`` 后复用 ``_execute_task`` 执行（handler / 状态回写 / PG 镜像不变），
        执行完成后再通过 scheduler-core ``store.complete`` 释放统一租约并推进 DAG。

        并发模型与原 worker 一致：最多同时执行 ``max_concurrent_job_tasks`` 个任务
        （默认 10），每个任务以独立协程运行。
        """
        from scheduler_core.scheduler import task_from_hash

        unified = self._unified()
        if unified is None:
            return
        logger.info("Unified worker loop started")
        store = unified.store
        domains = list(getattr(unified.config, "domains", {}).keys())

        async def drain_ready() -> bool:
            """将 fast-path 已准入任务尽量转为运行协程。"""
            from scheduler_core.models import TaskKind as UnifiedTaskKind

            started = False
            ready = getattr(unified, "_fast_ready", None)
            while ready is not None and not ready.empty():
                if len(self._unified_running) >= self._unified_max_concurrent():
                    break
                utask = ready.get_nowait()
                # fast-path 队列本应只含 JOB_TASK；防御性跳过其他 kind，避免
                # 误领在线工具任务（与 drain_queues 同一不变量）。放回队尾
                # 而不是丢弃：该任务已持有租约，且它属于另一 worker。
                if getattr(utask, "kind", None) is not UnifiedTaskKind.JOB_TASK:
                    logger.error(
                        "Unified worker skipped non-job fast-path task: %s",
                        getattr(utask, "id", "?"),
                    )
                    ready.put_nowait(utask)
                    break
                self._spawn_unified_task(utask)
                started = True
            return started

        async def drain_queues() -> bool:
            """按资源域取本 kind（JOB_TASK）的队首任务，准入后转为运行协程。

            注意：scheduler-core 的 store 是同步 redis-py，必须用
            ``asyncio.to_thread`` 投递到线程池执行，否则远程 Redis 的往返延迟
            会阻塞本事件循环（heartbeat / 租约续期 / 其他执行协程全部饿死）。

            只消费 JOB_TASK：ViKnow worker 与 HyperRAG worker 共享同一批
            domain 队列，若领走 AGENT_TOOL（在线工具请求），这里没有对应
            handler，会把它 complete 掉而不执行，导致 ViKnow 侧等待的
            future 永不 resolve、挂满外层超时。

            并发语义：acquire 失败时若本 kind 的最低 score 任务未变（容量满）
            则停；若已变（任务被其他 worker 领取）则继续取下一个。
            """
            from scheduler_core.models import TaskKind as UnifiedTaskKind

            started = False
            for domain in domains:
                while len(self._unified_running) < self._unified_max_concurrent():
                    task_id = await asyncio.to_thread(
                        store.peek_kind, domain, UnifiedTaskKind.JOB_TASK.value
                    )
                    if not task_id:
                        break
                    raw = await asyncio.to_thread(store.get_task, task_id)
                    if not raw:
                        await asyncio.to_thread(store.pop_queue, domain, task_id)
                        continue
                    utask = task_from_hash(task_id, raw)
                    admission_time = time.time()
                    if not await asyncio.to_thread(store.try_acquire, utask, ts=admission_time):
                        still_head = await asyncio.to_thread(
                            store.peek_kind, domain, UnifiedTaskKind.JOB_TASK.value
                        )
                        if still_head == task_id:
                            break
                        continue
                    admitted = await _run_with_retry(
                        lambda tid=task_id: asyncio.to_thread(store.get_task, tid),
                        operation_name=f"read admitted task {task_id}",
                    )
                    if not admitted or float(admitted.get("started_at") or 0) != admission_time:
                        logger.warning("Unified admission changed before execution: %s", task_id)
                        continue
                    utask.started_at = admission_time
                    utask.seq = int(admitted.get("seq") or 0)
                    self._spawn_unified_task(utask)
                    started = True
            return started

        while self._running:
            try:
                # 清理已完成的运行任务
                await self._prune_finished_unified_tasks()

                executed = False
                if await drain_ready():
                    executed = True
                if await drain_queues():
                    executed = True

                await asyncio.sleep(
                    unified_worker_idle_seconds(
                        executed=executed,
                        running_count=len(self._unified_running),
                    )
                )
            except asyncio.CancelledError:
                logger.info("Unified worker loop cancelled")
                break
            except Exception as e:
                logger.exception(f"Unified worker loop error: {e}")
                await asyncio.sleep(1.0)

        logger.info("Unified worker loop exited")

    def _unified_max_concurrent(self) -> int:
        """开态 worker 的 job task 并发上限。"""
        return self._max_concurrent_job_tasks

    def _spawn_unified_task(self, utask: Any) -> None:
        """启动一个统一任务的执行协程并登记到 _unified_running。"""
        coro = self._execute_unified_task(utask)
        task = asyncio.create_task(coro)
        self._unified_running[utask.id] = task

        def finished(done, tid=utask.id):
            self._unified_running.pop(tid, None)
            if not done.cancelled() and done.exception() is not None:
                logger.error("Unified task coroutine error: %s: %s", tid, done.exception())

        task.add_done_callback(finished)

    async def _prune_finished_unified_tasks(self) -> None:
        """收割已完成的统一任务（done_callback 已移除登记）。"""
        done = [tid for tid, t in self._unified_running.items() if t.done()]
        for tid in done:
            t = self._unified_running.pop(tid, None)
            if t is None:
                continue
            try:
                exc = t.exception()
                if exc is not None:
                    logger.error(f"Unified task coroutine error: {tid}: {exc}")
            except asyncio.CancelledError:
                pass

    async def _finish_unified(self, utask: Any, task: Task) -> str:
        from .unified_completion import finish_owned_task

        result = await _run_with_retry(
            lambda: asyncio.to_thread(
                finish_owned_task,
                self._unified().store,
                utask,
                started_at=utask.started_at,
                seq=utask.seq,
                hyperrag_task=task,
            ),
            operation_name=f"finish unified task {utask.id}",
        )
        if result not in {"completed", "already_done", "failed", "cancelled"}:
            logger.warning("Unified terminal notification deferred: %s (%s)", utask.id, result)
        return result

    async def _execute_unified_task(self, utask: Any):
        """Execute a job admission and reconcile its actual persisted outcome."""
        from scheduler_core.models import TaskKind as UnifiedTaskKind

        unified = self._unified()
        if unified is None:
            return
        if getattr(utask, "kind", None) is not UnifiedTaskKind.JOB_TASK:
            logger.error("Unified worker skipped non-job task: %s", utask.id)
            return
        # submit_job roots enter through drain_queues, which records the timestamp
        # passed to try_acquire. Never adopt a newer worker's admission from Redis.
        if not getattr(utask, "started_at", 0):
            logger.error("Unified job missing admission fence: %s", utask.id)
            return
        store = unified.store
        hb_interval = getattr(unified.config, "heartbeat_interval_seconds", 15)
        hyperrag_task_id = (utask.payload or {}).get("hyperrag_task_id") or utask.id
        stop = asyncio.Event()
        task = None

        async def heartbeat():
            while not stop.is_set():
                await asyncio.sleep(hb_interval)
                try:
                    if not await asyncio.to_thread(store.renew, utask):
                        logger.warning("Unified task lost lease: %s", utask.id)
                        return
                except (RedisTimeoutError, RedisConnectionError, TimeoutError, OSError):
                    # A transient lease error cannot turn a completed handler
                    # into a lost terminal notification. Finalization is fenced.
                    logger.warning("Unified heartbeat failed: %s", utask.id, exc_info=True)

        hb_task = asyncio.create_task(heartbeat())
        try:
            task = await self._queue.get_task(hyperrag_task_id)
            if task is None:
                logger.error("Unified task has no HyperRAG task data: %s", hyperrag_task_id)
                return
            await self._hydrate_task_metadata_from_deps(task)
            task.metadata["_unified_admission"] = {
                "started_at": utask.started_at,
                "seq": utask.seq,
            }
            await self._execute_task(task)
        except asyncio.CancelledError:
            # _execute_task persists CANCELLED; never release its dependencies.
            if task is not None:
                await self._finish_unified(utask, task)
            raise
        except Exception as exc:
            logger.exception("Unified task execution error: %s", utask.id)
            if task is not None and task.status != TaskStatus.COMPLETED:
                await self._on_task_failed(task, TaskResult(status="failed", error=str(exc)))
        finally:
            stop.set()
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("Unified heartbeat cleanup failed: %s", utask.id, exc_info=True)
        if task is not None:
            await self._finish_unified(utask, task)

    async def _execute_task(self, task: Task):
        """
        执行单个任务

        Args:
            task: 任务实例
        """
        request_id = ""
        if isinstance(task.metadata, dict):
            request_id = str(task.metadata.get("_request_id") or "").strip()
        with logging_context(request_id=request_id or "-", job_id=task.job_id or "-"):
            usage_scope = None
            try:
                from hyperrag.observability.index_usage import on_task_end, on_task_start

                usage_scope = on_task_start(task)
            except Exception:
                logger.warning("[index-usage] on_task_start failed", exc_info=True)
                usage_scope = None
            try:
                # 更新任务状态为running
                prev_status = task.status
                task.status = TaskStatus.RUNNING
                task.started_at = time.time()
                await self._queue.update_task(task)
                await self._emit_task_status(task, prev_status)

                logger.info(
                    f"Task started: {task.task_id} (type={task.task_type}, asset={task.asset_id})"
                )

                # 调用注册的handler
                try:
                    task_def = TaskRegistry.get(task.task_type)
                    if not task_def:
                        raise ValueError(f"No handler for task type: {task.task_type}")
                except Exception as e:
                    logger.error(f"Failed to get task definition for {task.task_type}: {e}")
                    raise

                # 执行handler（handler是已绑定依赖的可调用对象）

                result = await task_def.handler(task)

                # 处理结果

                if result.status == "completed":
                    await self._on_task_completed(task, result)
                elif result.status == "failed":
                    await self._on_task_failed(task, result)
                elif result.status == "requeue":
                    prev_status = task.status
                    task.status = TaskStatus.QUEUED
                    await self._queue.requeue(task)
                    await self._emit_task_status(task, prev_status)
                    logger.debug(f"Task requeued: {task.task_id}")

            except asyncio.CancelledError:
                logger.info(f"Task cancelled: {task.task_id}")
                prev_status = task.status
                task.status = TaskStatus.CANCELLED
                await self._queue.update_task(task)
                await self._emit_task_status(task, prev_status)
                raise

            except Exception as e:
                logger.exception(f"Task execution error: {task.task_id}")
                logger.exception(
                    "Scheduler handler raised: task=%s type=%s error=%s",
                    task.task_id,
                    task.task_type,
                    e,
                )
                if task.status == TaskStatus.COMPLETED:
                    logger.error(
                        "Task post-complete scheduler error ignored: task=%s type=%s error=%s",
                        task.task_id,
                        task.task_type,
                        e,
                    )
                else:
                    result = TaskResult(status="failed", error=str(e))
                    await self._on_task_failed(task, result)

            finally:
                try:
                    from hyperrag.observability.index_usage import on_task_end

                    on_task_end(task, usage_scope)
                except Exception:
                    logger.warning("[index-usage] on_task_end failed", exc_info=True)
                # 释放资源
                try:
                    await self._release(task.task_id)
                except Exception as e:
                    logger.error(
                        "Task resource release failed: task=%s type=%s error=%s",
                        task.task_id,
                        task.task_type,
                        e,
                    )
                if task.task_id in self._running_tasks:
                    del self._running_tasks[task.task_id]

    # ========== 资源与限制（原 ResourceTracker 合并） ==========

    RESOURCE_USAGE_KEY = "hyperrag:resource:{task_id}"

    def _get_limits(self) -> dict[str, TaskLimitConfig]:
        """获取任务并发限制配置"""
        if self._parsed_limits is not None:
            return self._parsed_limits

        # 使用默认值
        self._parsed_limits = {
            "kg_index": TaskLimitConfig(max_concurrent=4, gpu_memory_mb=0),
            "doc_parse": TaskLimitConfig(max_concurrent=2, gpu_memory_mb=0),
            "video_parse": TaskLimitConfig(max_concurrent=1, gpu_memory_mb=0),
            "delete_asset": TaskLimitConfig(max_concurrent=1, gpu_memory_mb=0),
        }

        return self._parsed_limits

    def _get_limit(self, task_type: str) -> TaskLimitConfig:
        limits = self._get_limits()
        return limits.get(task_type, TaskLimitConfig(max_concurrent=1, gpu_memory_mb=0))

    async def _can_acquire(self, task_type: str, resource: Any) -> bool:
        # gpu_need = int(getattr(resource, "gpu_memory_mb", 0) or 0)
        # if gpu_need > 0:
        #     available = await self._get_available_gpu_memory()
        #     if available < gpu_need:
        #         logger.debug(
        #             f"Insufficient GPU memory: need {gpu_need}MB, available {available}MB"
        #         )
        #         return False
        return True

    async def _acquire(self, task_id: str, task_type: str, resource: Any) -> bool:
        if not await self._can_acquire(task_type, resource):
            return False
        resource_key = self.RESOURCE_USAGE_KEY.format(task_id=task_id)
        resource_data = {
            "task_id": task_id,
            "task_type": task_type,
            "slots": int(getattr(resource, "slots", 1) or 1),
            "gpu_memory_mb": int(getattr(resource, "gpu_memory_mb", 0) or 0),
        }
        await self._queue.redis.set(resource_key, str(resource_data))
        return True

    async def _release(self, task_id: str):
        resource_key = self.RESOURCE_USAGE_KEY.format(task_id=task_id)
        await self._queue.redis.delete(resource_key)

    async def _get_available_gpu_memory(self) -> int:
        if self._gpu_available is not None:
            return self._gpu_available
        try:
            import os

            import pynvml

            pynvml.nvmlInit()
            cuda_devices = os.environ.get("CUDA_VISIBLE_DEVICES", "0")
            physical_index = int(cuda_devices.split(",")[0])

            handle = pynvml.nvmlDeviceGetHandleByIndex(physical_index)
            info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            free_mb = int(info.free // (1024 * 1024))
            return free_mb
        except Exception:
            # Treat missing or inaccessible GPU memory info as effectively unlimited.
            return int(10**18)

    async def get_resource_usage(self, task_type: str | None = None) -> dict[str, Any]:
        if task_type:
            running = len([t for _, t in self._running_tasks.values() if t.task_type == task_type])
            limit = self._get_limit(task_type)
            return {
                "task_type": task_type,
                "running": running,
                "limit": limit.max_concurrent,
                "available": limit.max_concurrent - running,
            }

        usage: dict[str, Any] = {}
        for ttype, limit in self._get_limits().items():
            running = len([t for _, t in self._running_tasks.values() if t.task_type == ttype])
            usage[ttype] = {
                "running": running,
                "limit": limit.max_concurrent,
                "available": limit.max_concurrent - running,
            }
        return usage

    async def _on_task_completed(self, task: Task, result: TaskResult):
        """
        任务完成处理

        Args:
            task: 任务实例
            result: 任务结果
        """
        prev_status = task.status
        task.status = TaskStatus.COMPLETED
        task.completed_at = time.time()
        task.result = result.data
        task.error_message = result.error
        await self._queue.update_task(task)
        await self._emit_task_status(task, prev_status)

        logger.info(
            f"Task completed: {task.task_id} "
            f"(type={task.task_type}, duration={task.completed_at - task.started_at:.2f}s)"
        )

        # 执行回调
        await self._execute_task_callback(task, "on_complete")

        # 触发下一阶段
        await self._trigger_next_stage(task)

    async def _on_task_failed(self, task: Task, result: TaskResult):
        """
        任务失败处理

        Args:
            task: 任务实例
            result: 任务结果
        """
        if task.status == TaskStatus.COMPLETED:
            logger.warning(
                "Skip failing already completed task: %s (type=%s)",
                task.task_id,
                task.task_type,
            )
            return

        prev_status = task.status
        task.status = TaskStatus.FAILED
        task.completed_at = time.time()
        task.error_message = result.error
        task.result = result.data
        await self._queue.update_task(task)
        await self._emit_task_status(task, prev_status)

        logger.error(f"Task failed: {task.task_id} (type={task.task_type}, error={result.error})")

        # 执行回调
        await self._execute_task_callback(task, "on_fail")

        # 将Job标记为failed
        await self._fail_job(task.job_id, failed_task_id=task.task_id)

    async def _trigger_next_stage(self, task: Task):
        """
        触发Job的下一阶段

        逻辑已改为：遍历Job中所有PENDING任务，检查其依赖是否已满足。
        如果满足，则将其状态改为QUEUED并入队。

        Args:
            task: 已完成的任务
        """
        if not task.job_id:
            return

        job = await self._queue.get_job(task.job_id)
        if not job:
            logger.warning(f"Job not found: {task.job_id}")
            return

        # 获取 Job 下的所有任务
        all_tasks = []
        for tid in job.task_ids:
            t = await self._queue.get_task(tid)
            if t:
                all_tasks.append(t)

        # 查找可以激活的任务
        #
        # 关键约束：只允许触发“直接依赖当前 task”的下游任务。
        # 否则当中间任务（例如 image_caption）执行极快时，
        # 上游 task（doc_parse）的 _trigger_next_stage 仍在扫描 all_tasks，
        # 会因为依赖已满足而激活更下游的任务（kg_index），
        # 且不会把中间任务结果（含 content）传过去，导致偶发 "No content in metadata"。
        activated_count = 0
        if self._unified() is None:
            for t in all_tasks:
                # 只处理“当前任务的直接下游”
                if task.task_id not in (t.depends_on or []):
                    continue

                # 避免 stale：从 Redis 重新读一次
                t_latest = await self._queue.get_task(t.task_id)
                if not t_latest or t_latest.status != TaskStatus.PENDING:
                    continue

                if not await self._check_dependencies(t_latest):
                    continue

                prev_status = t_latest.status
                t_latest.status = TaskStatus.QUEUED
                # Propagate both the upstream input and its result. Intermediate
                # stages may only return their own summary, while later stages
                # still need parse artifacts such as video asset and segments.
                t_latest.metadata.update(task.metadata or {})
                t_latest.metadata.update(task.result or {})

                await _run_with_retry(
                    lambda lt=t_latest: self._queue.enqueue(lt),
                    operation_name=f"enqueue({t_latest.task_id})",
                )
                await self._emit_task_status(t_latest, prev_status)
                activated_count += 1
                logger.debug(
                    f"Task activated: {t_latest.task_id} (job={job.job_id}, type={t_latest.task_type})"
                )
        # 开态：scheduler-core complete.lua 负责 DAG 入队；此处不激活下游任务

        # 检查 Job 整体状态（避免 stale：重新拉取一次 status）
        latest_statuses: list[TaskStatus] = []
        for tid in job.task_ids:
            lt = await self._queue.get_task(tid)
            if lt:
                latest_statuses.append(lt.status)
        is_completed = bool(latest_statuses) and all(
            s == TaskStatus.COMPLETED for s in latest_statuses
        )
        if is_completed:
            prev_status = job.status
            job.status = "completed"
            job.completed_at = time.time()
            logger.info(
                f"Job completed: {job.job_id} "
                f"(asset={job.asset_id}, duration={job.completed_at - job.created_at:.2f}s)"
            )
            await _run_with_retry(
                lambda: self._queue.update_job(job),
                operation_name=f"update_job({job.job_id})",
            )
            await self._emit_job_status(job, prev_status)
        else:
            # 更新 current_stage 为第一个未完成任务的索引
            for i, t in enumerate(all_tasks):
                if t.status != TaskStatus.COMPLETED:
                    job.current_stage = i
                    break

        if not is_completed:
            await _run_with_retry(
                lambda: self._queue.update_job(job),
                operation_name=f"update_job({job.job_id})",
            )

    async def _fail_job(self, job_id: str, failed_task_id: str):
        """
        将Job标记为失败

        Args:
            job_id: Job ID
            failed_task_id: 失败的任务ID
        """
        if not job_id:
            return

        job = await self._queue.get_job(job_id)
        if not job or job.status != "running":
            return

        prev_status = job.status
        job.status = "failed"
        job.completed_at = time.time()
        job.failed_task_id = failed_task_id
        failed_task = await self._queue.get_task(failed_task_id)
        if failed_task:
            job.error_message = (
                failed_task.error_message
                or (failed_task.result or {}).get("error")
                or (failed_task.result or {}).get("error_message")
            )
        await self._queue.update_job(job)
        await self._emit_job_status(job, prev_status)

        logger.warning(f"Job {job_id} marked as failed due to task {failed_task_id}")

    async def _fail_all_running_tasks(self, reason: str):
        """
        将所有running任务标记为失败

        Args:
            reason: 失败原因
        """
        if not self._running_tasks:
            return

        task_ids = list(self._running_tasks.keys())

        logger.info(f"Failing {len(task_ids)} running tasks: {reason}")

        for task_id in task_ids:
            coro_task, _ = self._running_tasks[task_id]
            coro_task.cancel()

            try:
                task = await self._queue.get_task(task_id)
                if task:
                    prev_status = task.status
                    task.status = TaskStatus.FAILED
                    task.error_message = reason
                    task.completed_at = time.time()
                    await self._queue.update_task(task)
                    await self._emit_task_status(task, prev_status)

                    # 更新关联的Job
                    if task.job_id:
                        await self._fail_job(task.job_id, task_id)
            except Exception as e:
                logger.error(f"Error failing task {task_id}: {e}")

        self._running_tasks.clear()
        logger.info(f"Failed {len(task_ids)} running tasks")

    async def _check_dependencies(self, task: Task) -> bool:
        """
        检查任务依赖是否完成

        Args:
            task: 任务实例

        Returns:
            依赖是否全部完成
        """
        for dep_id in task.depends_on:
            dep_task = await self._queue.get_task(dep_id)
            if not dep_task or dep_task.status != TaskStatus.COMPLETED:
                return False
        return True

    async def _hydrate_task_metadata_from_deps(self, task: Task) -> None:
        """执行前合并已完成上游任务的输入元数据与结果。"""
        if not task.depends_on:
            return
        merged = dict(task.metadata or {})
        for dep_id in task.depends_on:
            dep = await self._queue.get_task(dep_id)
            if dep:
                merged.update(dep.metadata or {})
                if isinstance(dep.result, dict):
                    merged.update(dep.result)
        task.metadata = merged

    async def _emit_job_status(self, job: JobInstance, prev_status: str | None):
        """触发 Job 状态回调"""
        new_status = self._status_value(job.status)
        prev = self._status_value(prev_status)
        if self._on_job_callback and new_status != prev:
            await self._safe_invoke_callback(self._on_job_callback, job, prev, new_status)
        if new_status in {"completed", "failed"} and new_status != prev:
            try:
                from hyperrag.observability.index_usage import on_job_finished

                on_job_finished(job)
            except Exception:
                logger.warning("[index-usage] on_job_finished failed", exc_info=True)

    async def _emit_task_status(self, task: Task, prev_status: str | None):
        """触发 Task 状态回调"""
        new_status = self._status_value(task.status)
        prev = self._status_value(prev_status)
        if self._on_task_callback and new_status != prev:
            await self._safe_invoke_callback(self._on_task_callback, task, prev, new_status)

    async def _safe_invoke_callback(
        self,
        callback: Callable[[object, str | None, str], Awaitable[None]],
        obj: object,
        prev_status: str | None,
        new_status: str,
    ):
        """安全调用回调（支持同步/异步）"""
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(obj, prev_status, new_status)
            else:
                await asyncio.to_thread(callback, obj, prev_status, new_status)
        except Exception as e:
            logger.error(f"Status callback error: {e}")

    @staticmethod
    def _status_value(status: object | None) -> str | None:
        """统一返回状态的基础字符串值"""
        if status is None:
            return None
        return status.value if hasattr(status, "value") else str(status)

    async def _execute_task_callback(self, task: Task, callback_key: str):
        """执行注册的任务回调（on_complete/on_fail）"""
        callback_name = task.callbacks.get(callback_key)
        if not callback_name:
            return

        callback = CallbackRegistry.get(callback_name)
        if not callback:
            logger.warning(f"Callback not found: {callback_name}")
            return

        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(task)
            else:
                await asyncio.to_thread(callback, task)

            logger.debug(f"Callback executed: {callback_name} for task {task.task_id}")
        except Exception as e:
            logger.error(f"Callback {callback_name} failed for task {task.task_id}: {e}")

    # ========== 模式管理 ==========

    async def switch_to_rw(self) -> bool:
        """
        切换到RW模式

        Returns:
            是否切换成功
        """
        if self._mode == InstanceMode.READ_WRITE:
            logger.info("Already in RW mode")
            return True

        # 是否启用 RW 分布式锁（默认关闭，调试更友好；多节点部署时再打开）
        if not self._enable_rw_lock:
            self._mode = InstanceMode.READ_WRITE
            await self.start()
            logger.info(f"Switched to RW mode without acquiring RW lock: {self._instance_id}")
            return True

        # 尝试获取RW锁
        acquired = await self._queue.try_acquire_rw_lock(self._instance_id)
        if not acquired:
            logger.warning("Failed to acquire RW lock, another RW node exists")
            return False

        self._mode = InstanceMode.READ_WRITE
        await self.start()

        logger.info(f"Switched to RW mode: {self._instance_id}")
        return True

    async def switch_to_ro(self):
        """切换到RO模式"""
        if self._mode == InstanceMode.READ_ONLY:
            logger.info("Already in RO mode")
            return

        await self.stop()
        # 仅在启用 RW 锁时释放
        if self._enable_rw_lock:
            await self._queue.release_rw_lock(self._instance_id)

        self._mode = InstanceMode.READ_ONLY
        logger.info(f"Switched to RO mode: {self._instance_id}")

    @property
    def current_mode(self) -> InstanceMode:
        """获取当前模式"""
        return self._mode

    # ========== 心跳管理 ==========

    async def start_heartbeat(self, interval: int = 5):
        """
        启动心跳

        Args:
            interval: 心跳间隔（秒）
        """

        async def _heartbeat():
            while True:
                try:
                    await self._queue.update_heartbeat(self._instance_id, self._mode.value)

                    # 如果是RW模式，续期锁
                    # 注意：不要用 continue 跳过续锁逻辑——continue 会连带跳过循环
                    # 尾部的 await asyncio.sleep(interval)，在 READ_WRITE + 关闭 RW 锁
                    # 时退化为忙循环，以事件循环上限疯狂刷写 Redis。
                    if self._mode == InstanceMode.READ_WRITE and self._enable_rw_lock:
                        renewed = await self._queue.renew_rw_lock(self._instance_id)
                        if not renewed:
                            logger.error("Failed to renew RW lock, switching to RO")
                            await self.switch_to_ro()

                except Exception as e:
                    logger.error(f"Heartbeat error: {e}")

                await asyncio.sleep(interval)

        self._heartbeat_task = asyncio.create_task(_heartbeat())
        logger.info(f"Heartbeat started for instance: {self._instance_id}")

    async def stop_heartbeat(self):
        """停止心跳"""
        if hasattr(self, "_heartbeat_task") and self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        logger.info(f"Heartbeat stopped for instance: {self._instance_id}")
