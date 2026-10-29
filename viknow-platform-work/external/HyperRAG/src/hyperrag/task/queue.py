"""
分布式任务队列

基于 Redis 实现的分布式任务队列，支持：
- 任务入队/出队
- 优先级队列
- RW 锁和实例管理
- Job实例存储
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

import redis.asyncio as aioredis
from redis.backoff import ExponentialBackoff
from redis.retry import Retry

from .models import JobInstance, Task, TaskStatus, TaskType

logger = logging.getLogger(__name__)


class DistributedTaskQueue:
    """
    分布式任务队列

    使用 Redis 作为后端，支持多进程/多实例协同工作
    """

    # Redis Key Templates
    TASK_KEY = "hyperrag:task:{task_id}"
    TASK_QUEUE = "hyperrag:queue:{task_type}"
    JOB_KEY = "hyperrag:job:{job_id}"
    RW_LOCK_KEY = "hyperrag:rw_lock"
    INSTANCE_KEY = "hyperrag:instance:{instance_id}"

    # 超时和TTL常量
    RW_LOCK_TTL = 30  # RW锁的TTL（秒）
    INSTANCE_HEARTBEAT_TTL = 60  # 实例心跳TTL（秒）
    DEFAULT_OPERATION_TIMEOUT = 5.0  # 默认操作超时（秒）

    def __init__(self, redis_uri: str):
        """
        初始化分布式任务队列

        Args:
            redis_uri: Redis连接URI
        """
        self.redis_uri = redis_uri
        self._redis: Optional[aioredis.Redis] = None
        self._initialized = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def initialize(self):
        """初始化 Redis 连接"""
        if self._initialized:
            return

        # 连接加固（治本）：远程 Redis 网络抖动时，连接层自动重试瞬时超时，
        # 避免单次 TimeoutError 直接冒泡打断 DAG 推进（_trigger_next_stage）。
        # - socket_timeout / socket_connect_timeout：单条命令与建连超时，
        #   防止 Redis 无响应时命令无期限挂起事件循环。
        # - retry_on_timeout + Retry(ExponentialBackoff)：redis-py 内置重试，
        #   对幂等/可安全重试的命令在超时后指数退避重试若干次。
        # - health_check_interval：空闲连接定期 ping，提前发现坏连接。
        self._redis = await aioredis.from_url(
            self.redis_uri,
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=5.0,
            socket_connect_timeout=5.0,
            socket_keepalive=True,
            health_check_interval=30,
            retry_on_timeout=True,
            retry=Retry(
                ExponentialBackoff(base=0.1, cap=1.0),
                retries=3,
            ),
        )
        # 绑定当前运行中的 event loop，避免 Redis 客户端跨 loop 使用导致卡死
        self._loop = asyncio.get_running_loop()
        # 主动探活：避免首次 Redis 命令在网络/连接异常时无期限等待
        await asyncio.wait_for(self._redis.ping(), timeout=self.DEFAULT_OPERATION_TIMEOUT)
        self._initialized = True
        logger.info(f"Distributed task queue initialized: {self.redis_uri}")

    async def close(self):
        """关闭 Redis 连接"""
        if self._redis:
            await self._redis.close()
            self._initialized = False
            self._loop = None

    @property
    def redis(self) -> aioredis.Redis:
        """获取 Redis 客户端"""
        if not self._initialized or not self._redis:
            raise RuntimeError("Task queue not initialized. Call initialize() first.")
        if self._loop is not None:
            try:
                current = asyncio.get_running_loop()
            except RuntimeError as e:
                raise RuntimeError("Redis client accessed outside of a running event loop") from e
            if current is not self._loop:
                raise RuntimeError(
                    "Redis client used from a different event loop. "
                    "This can cause hangs due to asyncio locks in the connection pool."
                )
        return self._redis

    async def _with_timeout(self, coro, timeout: float = None):
        """为 Redis 操作添加超时控制"""
        if timeout is None:
            timeout = self.DEFAULT_OPERATION_TIMEOUT
        return await asyncio.wait_for(coro, timeout=timeout)

    # ==================== 任务操作 ====================

    async def enqueue(self, task: Task):
        """
        任务入队

        Args:
            task: 任务实例
        """
        # 保存任务数据
        task_key = self.TASK_KEY.format(task_id=task.task_id)
        await self.redis.set(task_key, json.dumps(task.to_dict()))

        # 仅当状态为 QUEUED 时加入优先级队列
        if task.status == TaskStatus.QUEUED:
            queue_key = self.TASK_QUEUE.format(task_type=task.task_type)
            score = task.priority  # priority 数字越小优先级越高
            await self.redis.zadd(queue_key, {task.task_id: score})

            logger.debug(
                f"Task enqueued to priority queue: {task.task_id} "
                f"(type={task.task_type}, priority={task.priority})"
            )
        else:
            logger.debug(f"Task saved but not enqueued (status={task.status}): {task.task_id}")

    async def dequeue(self, task_type: str | TaskType, timeout: float = 1.0) -> Optional[Task]:
        """
        从队列中获取任务

        Args:
            task_type: 任务类型
            timeout: 超时时间（秒）

        Returns:
            任务实例，如果队列为空则返回 None
        """
        if isinstance(task_type, TaskType):
            task_type = task_type.value

        queue_key = self.TASK_QUEUE.format(task_type=task_type)

        try:
            # 单机单实例：优先使用非阻塞 pop，避免对每个 task_type 做阻塞等待。
            # 多实例/需要阻塞等待时，再用 BZPOPMIN。
            if timeout <= 0:
                items = await self.redis.zpopmin(queue_key, 1)
                if items:
                    task_id = items[0][0]
                    task = await self.get_task(task_id)
                    if task:
                        return task
                    logger.warning(f"Task data not found for dequeued task: {task_id}")
                return None

            # 使用 BZPOPMIN 阻塞弹出任务
            result = await asyncio.wait_for(
                self.redis.bzpopmin(queue_key, timeout=timeout), timeout=timeout
            )

            if result:
                # result: [queue_key, task_id, score]
                task_id = result[1]
                task = await self.get_task(task_id)
                if task:
                    return task
                else:
                    logger.warning(f"Task data not found for dequeued task: {task_id}")
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            logger.error(f"Error during dequeue for {task_type}: {e}")

        return None

    async def requeue(self, task: Task):
        """
        任务重新入队

        Args:
            task: 任务实例
        """
        task.status = TaskStatus.QUEUED
        await self.update_task(task)

        queue_key = self.TASK_QUEUE.format(task_type=task.task_type)
        score = task.priority
        await self.redis.zadd(queue_key, {task.task_id: score})

        # logger.debug(f"Task requeued: {task.task_id}")

    async def get_task(self, task_id: str) -> Optional[Task]:
        """获取任务"""
        try:
            task_key = self.TASK_KEY.format(task_id=task_id)
            data = await self.redis.get(task_key)

            if data is None:
                return None

            try:
                return Task.from_dict(json.loads(data))
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON data for task {task_id}: {e}")
                raise ValueError(f"Corrupted task data: {task_id}") from e
            except Exception as e:
                logger.error(f"Failed to deserialize task {task_id}: {e}")
                raise ValueError(f"Invalid task data format: {task_id}") from e
        except Exception as e:
            logger.error(f"Failed to get task {task_id}: {e}")
            raise

    async def update_task(self, task: Task):
        """更新任务"""
        try:
            task_key = self.TASK_KEY.format(task_id=task.task_id)
            data = json.dumps(task.to_dict())
            update_res = await self.redis.set(task_key, data)
            if update_res is None:
                logger.error(f"Failed to update task {task.task_id}: update_res is None")
        except Exception as e:
            logger.error(f"Failed to update task {task.task_id}: {e}")
            raise

    async def save_task(self, task: Task):
        """只保存任务数据到 Redis，不加入优先级队列。

        统一调度开态下，任务由 scheduler-core 队列承载，不再写入
        ``hyperrag:queue:{task_type}``；此方法仅保留 hyperrag:task:{id} 元数据，
        供状态回写（asset_status / job）与 admin 查询使用。
        """
        task_key = self.TASK_KEY.format(task_id=task.task_id)
        await self.redis.set(task_key, json.dumps(task.to_dict()))

    async def delete_task(self, task_id: str):
        """删除任务"""
        task_key = self.TASK_KEY.format(task_id=task_id)
        await self.redis.delete(task_key)

    # ==================== 流水线操作 ====================

    async def save_job(self, job: JobInstance):
        """保存Job实例"""
        try:
            job_key = self.JOB_KEY.format(job_id=job.job_id)
            data = json.dumps(job.to_dict())
            await self.redis.set(job_key, data)
            logger.debug(f"Job saved: {job.job_id}")
        except json.JSONEncodeError as e:
            logger.error(f"Failed to serialize job {job.job_id}: {e}")
            raise ValueError(f"Invalid job data: {job.job_id}") from e
        except Exception as e:
            logger.error(f"Failed to save job {job.job_id}: {e}")
            raise

    async def get_job(self, job_id: str) -> Optional[JobInstance]:
        """获取Job实例"""
        try:
            job_key = self.JOB_KEY.format(job_id=job_id)
            data = await self.redis.get(job_key)

            if data is None:
                return None

            try:
                return JobInstance.from_dict(json.loads(data))
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON data for job {job_id}: {e}")
                raise ValueError(f"Corrupted job data: {job_id}") from e
            except Exception as e:
                logger.error(f"Failed to deserialize job {job_id}: {e}")
                raise ValueError(f"Invalid job data format: {job_id}") from e
        except Exception as e:
            logger.error(f"Failed to get job {job_id}: {e}")
            raise

    async def list_running_jobs(self) -> list[JobInstance]:
        """枚举所有处于 running 状态的 Job（供 DAG 补偿扫描使用）。

        DAG 推进（_trigger_next_stage）因 Redis 瞬时故障中断后，没有任何
        正常路径会再次激活卡住的任务；补偿扫描靠这个方法兜底。
        """
        running_jobs: list[JobInstance] = []
        async for job_key in self.redis.scan_iter(match=self.JOB_KEY.format(job_id="*"), count=200):
            job_id = job_key.rsplit(":", 1)[-1]
            job = await self.get_job(job_id)
            if job is not None and job.status == "running":
                running_jobs.append(job)
        return running_jobs

    async def update_job(self, job: JobInstance):
        """更新Job实例"""
        await self.save_job(job)

    async def delete_job(self, job_id: str):
        """删除Job实例"""
        job_key = self.JOB_KEY.format(job_id=job_id)
        await self.redis.delete(job_key)

    # ==================== RW 锁和实例管理 ====================

    async def try_acquire_rw_lock(self, instance_id: str) -> bool:
        """
        尝试获取 RW 锁（原子操作）

        Args:
            instance_id: 实例ID

        Returns:
            是否获取成功
        """
        result = await self.redis.set(
            self.RW_LOCK_KEY,
            instance_id,
            nx=True,  # 仅当key不存在时设置
            ex=self.RW_LOCK_TTL,
        )

        if result:
            logger.info(f"RW lock acquired by instance: {instance_id}")
            return True
        return False

    async def release_rw_lock(self, instance_id: str) -> bool:
        """
        释放 RW 锁（仅当自己持有时）

        Args:
            instance_id: 实例ID

        Returns:
            是否释放成功
        """
        # Lua脚本确保原子性
        script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        result = await self.redis.eval(script, 1, self.RW_LOCK_KEY, instance_id)

        if result:
            logger.info(f"RW lock released by instance: {instance_id}")
            return True
        return False

    async def renew_rw_lock(self, instance_id: str) -> bool:
        """
        续期 RW 锁

        Args:
            instance_id: 实例ID

        Returns:
            是否续期成功
        """
        script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("expire", KEYS[1], ARGV[2])
        else
            return 0
        end
        """
        result = await self.redis.eval(script, 1, self.RW_LOCK_KEY, instance_id, self.RW_LOCK_TTL)
        return result == 1

    async def get_rw_instance(self) -> Optional[str]:
        """获取当前持有 RW 锁的实例ID"""
        return await self.redis.get(self.RW_LOCK_KEY)

    async def update_heartbeat(self, instance_id: str, mode: str):
        """
        更新实例心跳

        Args:
            instance_id: 实例ID
            mode: 实例模式 ("read_only" | "read_write")
        """
        instance_key = self.INSTANCE_KEY.format(instance_id=instance_id)
        data = {
            "instance_id": instance_id,
            "mode": mode,
            "last_heartbeat": time.time(),
            "is_healthy": True,
        }
        await self.redis.set(
            instance_key,
            json.dumps(data),
            ex=self.INSTANCE_HEARTBEAT_TTL,
        )

    async def get_instance_info(self, instance_id: str) -> Optional[Dict[str, Any]]:
        """获取实例信息"""
        instance_key = self.INSTANCE_KEY.format(instance_id=instance_id)
        data = await self.redis.get(instance_key)

        if data:
            return json.loads(data)
        return None

    async def list_instances(self) -> List[Dict[str, Any]]:
        """列出所有活跃实例"""
        pattern = self.INSTANCE_KEY.format(instance_id="*")
        keys = await self.redis.keys(pattern)

        if not keys:
            return []

        # 使用 MGET 批量获取所有实例数据，避免 N+1 查询
        values = await self.redis.mget(keys)

        instances = []
        for key, data in zip(keys, values):
            if data:
                try:
                    instances.append(json.loads(data))
                except json.JSONDecodeError as e:
                    logger.warning(f"Invalid JSON data for instance key {key}: {e}")
                    continue

        return instances
