"""
任务队列组件测试（使用真实 Redis）
"""

import os
import pytest

from hyperrag.task.queue import DistributedTaskQueue
from hyperrag.task.models import Task, TaskStatus, TaskResource


# 测试配置（使用真实 Redis）
TEST_REDIS_URI = os.getenv("HYPERRAG_TEST_REDIS_URI", "redis://localhost:16379/15")


@pytest.fixture
async def redis_queue():
    """创建 Redis 任务队列"""
    queue = DistributedTaskQueue(TEST_REDIS_URI)
    await queue.initialize()

    # 清理测试 key（仅清理 hyperrag: 前缀的 key）
    redis = queue.redis
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match="hyperrag:*", count=100)
        if keys:
            await redis.delete(*keys)
        if cursor == 0:
            break

    yield queue

    # 测试后清理
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match="hyperrag:*", count=100)
        if keys:
            await redis.delete(*keys)
        if cursor == 0:
            break

    await queue.close()


@pytest.mark.asyncio
async def test_task_queue_initialization(redis_queue):
    """测试任务队列初始化"""
    assert redis_queue._redis is not None
    assert redis_queue._initialized is True


@pytest.mark.asyncio
async def test_enqueue_dequeue_task(redis_queue):
    """测试任务入队和出队"""
    # 创建测试任务
    task = Task(
        task_id="test-task-1",
        task_type="doc_parse",
        workspace="test-workspace",
        asset_id="test-asset-1",
        status=TaskStatus.QUEUED,
        resource=TaskResource(slots=1),
        priority=5,
    )

    # 入队
    await redis_queue.enqueue(task)

    # 出队（显式传递 task_type）
    dequeued_task = await redis_queue.dequeue("doc_parse", timeout=1.0)
    assert dequeued_task is not None
    assert dequeued_task.task_id == task.task_id
    assert dequeued_task.task_type == task.task_type
    # 注意：dequeue 只从队列中取出任务，不自动更新状态
    # 状态更新应由调用者（如 scheduler）负责
    assert dequeued_task.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_task_priority(redis_queue):
    """测试任务优先级"""
    # 创建不同优先级的任务
    task_low = Task(
        task_id="task-low",
        task_type="doc_parse",
        workspace="test",
        asset_id="asset1",
        status=TaskStatus.QUEUED,
        resource=TaskResource(slots=1),
        priority=10,  # 低优先级（数字越大优先级越低）
    )

    task_high = Task(
        task_id="task-high",
        task_type="doc_parse",
        workspace="test",
        asset_id="asset2",
        status=TaskStatus.QUEUED,
        resource=TaskResource(slots=1),
        priority=1,  # 高优先级
    )

    # 先入队低优先级任务
    await redis_queue.enqueue(task_low)
    await redis_queue.enqueue(task_high)

    # 出队应该先获取高优先级任务
    first_task = await redis_queue.dequeue("doc_parse", timeout=1.0)
    assert first_task.task_id == "task-high"


@pytest.mark.asyncio
async def test_task_requeue(redis_queue):
    """测试任务重新入队"""
    task = Task(
        task_id="test-requeue",
        task_type="kg_index",
        workspace="test",
        asset_id="asset1",
        status=TaskStatus.QUEUED,
        resource=TaskResource(slots=1),
        priority=5,
    )

    # 入队
    await redis_queue.enqueue(task)

    # 出队
    dequeued = await redis_queue.dequeue("kg_index", timeout=1.0)
    assert dequeued is not None
    # 注意：dequeue 不自动更新状态，需要手动更新
    dequeued.status = TaskStatus.RUNNING
    await redis_queue.update_task(dequeued)

    # 重新入队
    await redis_queue.requeue(dequeued)

    # 再次出队
    requeued = await redis_queue.dequeue("kg_index", timeout=1.0)
    assert requeued is not None
    assert requeued.task_id == task.task_id


@pytest.mark.asyncio
async def test_get_update_task(redis_queue):
    """测试获取和更新任务"""
    task = Task(
        task_id="test-update",
        task_type="doc_parse",
        workspace="test",
        asset_id="asset1",
        status=TaskStatus.QUEUED,
        resource=TaskResource(slots=1),
        priority=5,
    )

    # 入队
    await redis_queue.enqueue(task)

    # 获取任务
    retrieved = await redis_queue.get_task(task.task_id)
    assert retrieved is not None
    assert retrieved.task_id == task.task_id

    # 更新任务
    retrieved.status = TaskStatus.COMPLETED
    await redis_queue.update_task(retrieved)

    # 再次获取验证更新
    updated = await redis_queue.get_task(task.task_id)
    assert updated.status == TaskStatus.COMPLETED


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--run-integration"])
