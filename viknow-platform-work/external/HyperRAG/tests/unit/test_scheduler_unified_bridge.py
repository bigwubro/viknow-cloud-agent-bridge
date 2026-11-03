"""
HyperRAG TaskScheduler 统一调度接入测试。

验证开态下 submit_job / _trigger_next_stage / worker 的分流行为：
- 开态（unified_scheduler 提供且 enabled）：job/task 元数据保留，但不写入
  hyperrag:queue；任务提交到 scheduler-core；worker 从 scheduler-core 队列取活。
- 关态（unified_scheduler=None 或 disabled）：走原 HyperRAG 调度逻辑，行为不变。

这些测试不依赖真实 Redis / scheduler-core，全部使用 mock 验证分流边界。
"""

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hyperrag.task.models import (
    JobInstance,
    Task,
    TaskResource,
    TaskStatus,
)
from hyperrag.task.queue import DistributedTaskQueue
from hyperrag.task.scheduler import InstanceMode, TaskScheduler, unified_worker_idle_seconds


def _make_task(task_id: str, task_type: str, job_id: str = "job-1", **kwargs):
    return Task(
        task_id=task_id,
        task_type=task_type,
        workspace="ws-1",
        asset_id="asset-1",
        status=kwargs.pop("status", TaskStatus.PENDING),
        resource=TaskResource(),
        job_id=job_id,
        **kwargs,
    )


def _make_job(job_id: str = "job-1", task_ids=None):
    return JobInstance(
        job_id=job_id,
        job_name="video",
        workspace="ws-1",
        asset_id="asset-1",
        asset_path="/tmp/video-test.mp4",
        stages=["video_parse", "kg_index", "bm25_index"],
        task_ids=task_ids or ["t1", "t2", "t3"],
    )


def _make_scheduler(
    unified_enabled: bool = True, queue=None, *, max_concurrent_job_tasks: int = 10
):
    """构造 TaskScheduler，mock queue 与 unified scheduler。"""
    q = queue or AsyncMock(spec=DistributedTaskQueue)
    unified = None
    if unified_enabled:
        unified = SimpleNamespace(
            config=SimpleNamespace(
                enabled=True,
                domains={
                    "chat-llm": object(),
                    "vllm": object(),
                    "embedding": object(),
                    "asr": object(),
                    "rerank": object(),
                    "hyperrag-db": object(),
                },
            ),
            store=MagicMock(),
            submit_task=AsyncMock(),
            submit_job=AsyncMock(),
        )
    return TaskScheduler(
        queue=q,
        instance_id="inst-1",
        max_concurrent_job_tasks=max_concurrent_job_tasks,
        unified_scheduler=unified,
    )


@pytest.mark.asyncio
async def test_unified_enabled_flag():
    """开态/关态下 unified_enabled 与 _unified() 返回正确。"""
    on = _make_scheduler(unified_enabled=True)
    assert on.unified_enabled is True
    assert on._unified() is not None

    off = _make_scheduler(unified_enabled=False)
    assert off.unified_enabled is False
    assert off._unified() is None

    # 提供实例但配置禁用 -> 视为关态
    disabled = TaskScheduler(
        queue=AsyncMock(spec=DistributedTaskQueue),
        instance_id="inst-1",
        max_concurrent_job_tasks=10,
        unified_scheduler=SimpleNamespace(
            config=SimpleNamespace(enabled=False, domains={}),
            store=MagicMock(),
            submit_task=AsyncMock(),
        ),
    )
    assert disabled.unified_enabled is False
    assert disabled._unified() is None


@pytest.mark.asyncio
async def test_submit_job_unified_skips_hyperrag_queue():
    """开态：submit_job 不调用 queue.enqueue，改调 save_task + unified.submit_job。"""
    q = AsyncMock(spec=DistributedTaskQueue)
    s = _make_scheduler(unified_enabled=True, queue=q)

    tasks = [
        _make_task("t1", "video_parse"),
        _make_task("t2", "kg_index", depends_on=["t1"]),
    ]
    job_id = await s.submit_job(_make_job(task_ids=["t1", "t2"]), tasks)

    assert job_id == "job-1"
    q.save_job.assert_awaited_once()
    q.save_task.assert_awaited()
    q.enqueue.assert_not_awaited()
    s._unified().submit_job.assert_awaited_once()
    job_spec = s._unified().submit_job.await_args.args[0]
    assert job_spec.job_id == "job-1"
    assert len(job_spec.tasks) == 2
    assert job_spec.tasks[0].id == "t1"
    assert job_spec.tasks[0].domains == ["vllm", "asr"]
    assert job_spec.tasks[1].depends_on == ["t1"]
    assert job_spec.tasks[0].payload["hyperrag_task_id"] == "t1"
    assert job_spec.tasks[0].executor_ref == "video_parse"
    s._unified().submit_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_job_unified_submits_full_dag():
    """开态：整棵 DAG 一次 submit_job 交给 scheduler-core，不在 HyperRAG 侧逐根提交。"""
    s = _make_scheduler(unified_enabled=True)
    tasks = [
        _make_task("t1", "video_parse"),
        _make_task("t2", "kg_index", depends_on=["t1"]),
        _make_task("t3", "bm25_index", depends_on=["t2"]),
    ]
    await s.submit_job(_make_job(task_ids=["t1", "t2", "t3"]), tasks)
    s._unified().submit_job.assert_awaited_once()
    job_spec = s._unified().submit_job.await_args.args[0]
    assert [t.id for t in job_spec.tasks] == ["t1", "t2", "t3"]
    assert job_spec.tasks[2].depends_on == ["t2"]


@pytest.mark.asyncio
async def test_submit_job_legacy_path_unchanged():
    """关态：submit_job 完全走原逻辑（enqueue 每个 task）。"""
    q = AsyncMock(spec=DistributedTaskQueue)
    s = _make_scheduler(unified_enabled=False, queue=q)
    tasks = [_make_task("t1", "video_parse"), _make_task("t2", "kg_index")]
    await s.submit_job(_make_job(task_ids=["t1", "t2"]), tasks)
    assert q.enqueue.await_count == 2
    assert not hasattr(s, "_unified") or s._unified() is None


@pytest.mark.asyncio
async def test_trigger_next_stage_unified_does_not_enqueue_downstream():
    """开态：_trigger_next_stage 不激活下游；DAG 入队由 scheduler-core 负责。"""
    q = AsyncMock(spec=DistributedTaskQueue)
    s = _make_scheduler(unified_enabled=True, queue=q)

    t1 = _make_task("t1", "video_parse", status=TaskStatus.COMPLETED)
    t2 = _make_task("t2", "kg_index", depends_on=["t1"], status=TaskStatus.PENDING)
    q.get_job = AsyncMock(return_value=_make_job(task_ids=["t1", "t2"]))
    q.get_task = AsyncMock(side_effect=lambda tid: {"t1": t1, "t2": t2}[tid])

    await s._trigger_next_stage(t1)

    q.get_job.assert_awaited_once()
    q.enqueue.assert_not_awaited()
    q.save_task.assert_not_awaited()
    s._unified().submit_task.assert_not_awaited()
    assert t2.status == TaskStatus.PENDING


@pytest.mark.asyncio
async def test_trigger_next_stage_legacy_path_unchanged():
    """关态：_trigger_next_stage 走原 enqueue 逻辑。"""
    q = AsyncMock(spec=DistributedTaskQueue)
    s = _make_scheduler(unified_enabled=False, queue=q)

    t1 = _make_task("t1", "video_parse", status=TaskStatus.COMPLETED)
    t2 = _make_task("t2", "kg_index", depends_on=["t1"], status=TaskStatus.PENDING)
    q.get_job = AsyncMock(return_value=_make_job(task_ids=["t1", "t2"]))
    q.get_task = AsyncMock(side_effect=lambda tid: {"t1": t1, "t2": t2}[tid])

    await s._trigger_next_stage(t1)

    q.enqueue.assert_awaited_once()
    assert q.enqueue.await_args.args[0].task_id == "t2"


@pytest.mark.asyncio
async def test_trigger_next_stage_preserves_parse_artifacts_through_intermediate_stage():
    """Legacy scheduling retains parse artifacts when an intermediate stage omits them."""
    q = AsyncMock(spec=DistributedTaskQueue)
    s = _make_scheduler(unified_enabled=False, queue=q)

    kg_index = _make_task(
        "t2",
        "kg_index",
        status=TaskStatus.COMPLETED,
        metadata={
            "asset": {"doc_id": "asset-1"},
            "segments": [{"segment_id": "segment-1"}],
        },
    )
    kg_index.result = {"chunk_count": 1}
    bm25_index = _make_task(
        "t3",
        "bm25_index",
        depends_on=["t2"],
        status=TaskStatus.PENDING,
    )
    q.get_job = AsyncMock(return_value=_make_job(task_ids=["t2", "t3"]))
    q.get_task = AsyncMock(side_effect=lambda tid: {"t2": kg_index, "t3": bm25_index}[tid])

    await s._trigger_next_stage(kg_index)

    queued = q.enqueue.await_args.args[0]
    assert queued.metadata["asset"] == {"doc_id": "asset-1"}
    assert queued.metadata["segments"] == [{"segment_id": "segment-1"}]
    assert queued.metadata["chunk_count"] == 1


@pytest.mark.asyncio
async def test_hydrate_task_metadata_preserves_parse_artifacts_through_intermediate_stage():
    """Unified scheduling hydrates both upstream metadata and result."""
    q = AsyncMock(spec=DistributedTaskQueue)
    s = _make_scheduler(unified_enabled=True, queue=q)

    kg_index = _make_task(
        "t2",
        "kg_index",
        status=TaskStatus.COMPLETED,
        metadata={
            "asset": {"doc_id": "asset-1"},
            "segments": [{"segment_id": "segment-1"}],
        },
    )
    kg_index.result = {"chunk_count": 1}
    bm25_index = _make_task(
        "t3",
        "bm25_index",
        depends_on=["t2"],
        metadata={"title": "video"},
    )
    q.get_task = AsyncMock(return_value=kg_index)

    await s._hydrate_task_metadata_from_deps(bm25_index)

    assert bm25_index.metadata == {
        "title": "video",
        "asset": {"doc_id": "asset-1"},
        "segments": [{"segment_id": "segment-1"}],
        "chunk_count": 1,
    }


@pytest.mark.asyncio
async def test_worker_loop_unified_branch():
    """开态：_worker_loop 走 _unified_worker_loop，不执行原 worker 轮询。"""
    q = AsyncMock(spec=DistributedTaskQueue)
    s = _make_scheduler(unified_enabled=True, queue=q)
    s._running = True

    unified_loop = AsyncMock()
    s._unified_worker_loop = unified_loop
    await s._worker_loop()
    unified_loop.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_loop_legacy_branch():
    """关态：_worker_loop 走原逻辑（不进入 unified worker）。"""
    q = AsyncMock(spec=DistributedTaskQueue)
    s = _make_scheduler(unified_enabled=False, queue=q)
    s._running = True

    unified_loop = AsyncMock()
    s._unified_worker_loop = unified_loop

    # 原 worker 无限循环；让 list_types 抛 CancelledError 使其首次迭代即退出
    with patch("hyperrag.task.scheduler.TaskRegistry.list_types") as mock_list:
        mock_list.side_effect = asyncio.CancelledError
        await s._worker_loop()

    unified_loop.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_unified_task_runs_hyperrag_handler():
    """开态：_execute_unified_task 从 hyperrag 读取 task 数据并走 _execute_task。"""
    from scheduler_core.models import TaskKind as UnifiedTaskKind

    q = AsyncMock(spec=DistributedTaskQueue)
    s = _make_scheduler(unified_enabled=True, queue=q)

    store = MagicMock()
    s._unified().store = store

    t1 = _make_task("t1", "video_parse", status=TaskStatus.COMPLETED)
    q.get_task = AsyncMock(return_value=t1)

    executed = []
    s._execute_task = AsyncMock(side_effect=lambda t: executed.append(t))

    utask = SimpleNamespace(
        id="t1",
        kind=UnifiedTaskKind.JOB_TASK,
        payload={"hyperrag_task_id": "t1", "hyperrag_job_id": "job-1"},
    )
    utask.started_at, utask.seq = 123.0, 1
    s._finish_unified = AsyncMock()
    await s._execute_unified_task(utask)

    q.get_task.assert_awaited_once_with("t1")
    s._execute_task.assert_awaited_once_with(t1)
    s._finish_unified.assert_awaited_once_with(utask, t1)
    store.complete.assert_not_called()


@pytest.mark.asyncio
async def test_execute_unified_task_failure_marks_failed():
    """开态：handler 抛异常时 store.mark_failed，不误调 complete。"""
    from scheduler_core.models import TaskKind as UnifiedTaskKind

    q = AsyncMock(spec=DistributedTaskQueue)
    s = _make_scheduler(unified_enabled=True, queue=q)

    store = MagicMock()
    store.renew = MagicMock(return_value=True)
    store.task_status = MagicMock(return_value="running")
    s._unified().store = store

    t1 = _make_task("t1", "video_parse", status=TaskStatus.COMPLETED)
    q.get_task = AsyncMock(return_value=t1)
    s._execute_task = AsyncMock(side_effect=RuntimeError("boom"))

    utask = SimpleNamespace(
        id="t1", kind=UnifiedTaskKind.JOB_TASK, payload={"hyperrag_task_id": "t1"}
    )
    utask.started_at, utask.seq = 123.0, 1
    s._finish_unified = AsyncMock()
    await s._execute_unified_task(utask)

    s._finish_unified.assert_awaited_once_with(utask, t1)
    store.complete.assert_not_called()


@pytest.mark.asyncio
async def test_execute_unified_task_preempted_does_not_replay_handler():
    """开态：执行期间被软抢占（PREEMPTED）时重新入队，不 complete。"""
    from scheduler_core.models import TaskKind as UnifiedTaskKind

    q = AsyncMock(spec=DistributedTaskQueue)
    s = _make_scheduler(unified_enabled=True, queue=q)

    store = MagicMock()
    store.renew = MagicMock(return_value=True)
    store.task_status = MagicMock(return_value="preempted")
    s._unified().store = store

    t1 = _make_task("t1", "video_parse", status=TaskStatus.RUNNING)
    q.get_task = AsyncMock(return_value=t1)
    s._execute_task = AsyncMock()

    utask = SimpleNamespace(
        id="t1", kind=UnifiedTaskKind.JOB_TASK, payload={"hyperrag_task_id": "t1"}
    )
    utask.started_at, utask.seq = 123.0, 1
    s._finish_unified = AsyncMock()
    await s._execute_unified_task(utask)

    store.enqueue.assert_not_called()
    store.complete.assert_not_called()
    assert t1.status == TaskStatus.RUNNING
    s._finish_unified.assert_awaited_once_with(utask, t1)


@pytest.mark.asyncio
async def test_hyperrag_domain_mapping():
    """task_type -> 资源域映射（与 ViKnow 工具域对齐）。"""
    assert TaskScheduler._hyperrag_domains("video_parse") == ["vllm", "asr"]
    assert TaskScheduler._hyperrag_domains("sop_video_parse") == ["vllm", "asr"]
    assert TaskScheduler._hyperrag_domains("video_asr") == ["asr"]
    assert TaskScheduler._hyperrag_domains("video_caption") == ["vllm"]
    assert TaskScheduler._hyperrag_domains("image_caption") == ["vllm"]
    assert TaskScheduler._hyperrag_domains("kg_index") == ["vllm", "embedding"]
    assert TaskScheduler._hyperrag_domains("bm25_index") == ["vllm"]
    assert TaskScheduler._hyperrag_domains("doc_parse") == ["hyperrag-db"]
    assert TaskScheduler._hyperrag_domains("delete_asset") == ["hyperrag-db"]


# ---------------------------------------------------------------------------
# 配置解析：scheduler.unified 段
# ---------------------------------------------------------------------------


def _write_yaml(tmp_path, content: str) -> str:
    p = tmp_path / "hyperrag.yml"
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_load_config_unified_enabled(tmp_path):
    """yaml 带 scheduler.unified.enabled=true 时解析出 UnifiedSchedulerConfig。"""
    from hyperrag.config_loader import load_config_from_yaml

    path = _write_yaml(
        tmp_path,
        """
working_dir: "./hyperrag-storage"
redis_uri: "redis://localhost:6379/0"
scheduler:
  unified:
    enabled: true
    redis_url: "redis://localhost:6379/5"
    lease_ttl_seconds: 120
    heartbeat_interval_seconds: 30
    domains:
      gpu:
        capacity: 2
      vllm:
        capacity: 4
""",
    )
    cfg = load_config_from_yaml(path)
    assert cfg.scheduler_unified is not None
    assert cfg.scheduler_unified.enabled is True
    assert cfg.scheduler_unified.redis_url == "redis://localhost:6379/5"
    assert cfg.scheduler_unified.lease_ttl_seconds == 120
    assert cfg.scheduler_unified.heartbeat_interval_seconds == 30
    assert cfg.scheduler_unified.domains["gpu"]["capacity"] == 2
    assert cfg.scheduler_unified.domains["vllm"]["capacity"] == 4


def test_load_config_unified_disabled_default(tmp_path):
    """yaml 无 scheduler.unified 段或 enabled=false 时 scheduler_unified 为 None。"""
    from hyperrag.config_loader import load_config_from_yaml

    path = _write_yaml(tmp_path, "working_dir: './hyperrag-storage'\n")
    cfg = load_config_from_yaml(path)
    assert cfg.scheduler_unified is None

    path2 = _write_yaml(
        tmp_path,
        """
scheduler:
  unified:
    enabled: false
    redis_url: "redis://localhost:6379/9"
""",
    )
    cfg2 = load_config_from_yaml(path2)
    assert cfg2.scheduler_unified is not None
    assert cfg2.scheduler_unified.enabled is False


def test_build_unified_scheduler_disabled_returns_none():
    """关态下 _build_unified_scheduler 返回 None。"""
    from hyperrag.hyperrag_v2 import HyperRAGV2

    inst = HyperRAGV2.__new__(HyperRAGV2)
    inst.config = SimpleNamespace(scheduler_unified=None)
    assert inst._build_unified_scheduler() is None

    inst.config = SimpleNamespace(scheduler_unified=SimpleNamespace(enabled=False))
    assert inst._build_unified_scheduler() is None


def test_build_unified_scheduler_enabled_builds_scheduler(monkeypatch):
    """开态下 _build_unified_scheduler 构造 UnifiedScheduler（不连真实 Redis）。"""
    from hyperrag.hyperrag_v2 import HyperRAGV2

    inst = HyperRAGV2.__new__(HyperRAGV2)
    inst.config = SimpleNamespace(
        scheduler_unified=SimpleNamespace(
            enabled=True,
            redis_url="redis://localhost:6379/0",
            lease_ttl_seconds=60,
            heartbeat_interval_seconds=15,
            queue_wait_timeout_seconds=30.0,
            default_execution_timeout_seconds=180.0,
            domains={"gpu": {"capacity": 2}},
        )
    )

    built = {}
    import scheduler_core.scheduler as sc_scheduler

    def fake_store_from_url(url, cfg):
        return MagicMock()

    def fake_unified(cfg, store):
        built["cfg"] = cfg
        built["store"] = store
        return SimpleNamespace(config=cfg, store=store)

    monkeypatch.setattr(
        "scheduler_core.store.SchedulerStore.from_url", staticmethod(fake_store_from_url)
    )
    monkeypatch.setattr(sc_scheduler, "UnifiedScheduler", fake_unified)

    result = inst._build_unified_scheduler()
    assert result is not None
    assert built["cfg"].enabled is True
    assert built["cfg"].redis_url == "redis://localhost:6379/0"
    assert built["cfg"].domains["gpu"].capacity == 2
    # reference_domain 缺失时自动补充默认 vllm
    assert built["cfg"].domains["vllm"].capacity > 0


# ---------------------------------------------------------------------------
# 开态 worker 并发行为
# ---------------------------------------------------------------------------


def test_unified_max_concurrent_uses_config():
    """开态 worker 并发上限沿用 max_concurrent_job_tasks 配置。"""
    s = _make_scheduler(unified_enabled=True)
    assert s._unified_max_concurrent() == 10

    from hyperrag.task.scheduler import TaskScheduler

    custom = TaskScheduler(
        queue=AsyncMock(spec=DistributedTaskQueue),
        instance_id="inst-1",
        max_concurrent_job_tasks=6,
    )
    assert custom._unified_max_concurrent() == 6


def test_unified_max_concurrent_minimum_one():
    """max_concurrent_job_tasks 低于 1 时收敛到 1。"""
    from hyperrag.task.scheduler import TaskScheduler

    s = TaskScheduler(
        queue=AsyncMock(spec=DistributedTaskQueue),
        instance_id="inst-1",
        max_concurrent_job_tasks=0,
    )
    assert s._unified_max_concurrent() == 1


@pytest.mark.asyncio
async def test_spawn_unified_task_registers_and_cleans_up():
    """_spawn_unified_task 登记到 _unified_running 并在完成后自动清理。"""
    q = AsyncMock(spec=DistributedTaskQueue)
    s = _make_scheduler(unified_enabled=True, queue=q)

    async def fake_execute(utask):
        await asyncio.sleep(0)

    s._execute_unified_task = fake_execute
    utask = SimpleNamespace(id="t1", payload={"hyperrag_task_id": "t1"})

    s._spawn_unified_task(utask)
    assert "t1" in s._unified_running

    # 等协程完成，done_callback 清理登记
    await asyncio.sleep(0.05)
    assert "t1" not in s._unified_running


@pytest.mark.asyncio
async def test_heartbeat_throttled_when_rw_without_lock():
    """READ_WRITE + 关闭 RW 锁时心跳必须按 interval 节流，不得退化为忙循环。

    回归测试：此前 `continue` 跳过续锁逻辑时会连带跳过循环尾部的
    `await asyncio.sleep(interval)`，在 READ_WRITE + enable_rw_lock=False
    组合下退化为忙循环，以事件循环上限疯狂刷写 Redis 心跳。
    """
    q = AsyncMock(spec=DistributedTaskQueue)
    s = TaskScheduler(
        queue=q,
        instance_id="inst-1",
        max_concurrent_job_tasks=10,
        enable_rw_lock=False,
    )
    s._mode = InstanceMode.READ_WRITE

    heartbeat_calls = 0
    q.update_heartbeat = AsyncMock(side_effect=lambda *a, **k: nonlocal_heartbeat())

    def nonlocal_heartbeat():
        nonlocal heartbeat_calls
        heartbeat_calls += 1

    # 启动心跳，interval 足够大，用于验证 sleep 未被跳过
    await s.start_heartbeat(interval=5)
    try:
        await asyncio.sleep(0.2)
    finally:
        await s.stop_heartbeat()

    # 若 sleep 被 continue 跳过，0.2s 内会调用成百上千次；节流后最多 1 次
    assert heartbeat_calls <= 1
    assert q.renew_rw_lock.await_count == 0


@pytest.mark.asyncio
async def test_heartbeat_renews_rw_lock_when_enabled():
    """READ_WRITE + 开启 RW 锁时心跳正常续锁且仍按 interval 节流。"""
    q = AsyncMock(spec=DistributedTaskQueue)
    s = TaskScheduler(
        queue=q,
        instance_id="inst-1",
        max_concurrent_job_tasks=10,
        enable_rw_lock=True,
    )
    s._mode = InstanceMode.READ_WRITE

    heartbeat_calls = 0
    q.update_heartbeat = AsyncMock(side_effect=lambda *a, **k: nonlocal_heartbeat())
    q.renew_rw_lock = AsyncMock(return_value=True)

    def nonlocal_heartbeat():
        nonlocal heartbeat_calls
        heartbeat_calls += 1

    await s.start_heartbeat(interval=5)
    try:
        await asyncio.sleep(0.2)
    finally:
        await s.stop_heartbeat()

    # 每轮心跳都尝试续锁；0.2s 内只应跑完第一轮
    assert heartbeat_calls <= 1
    assert q.renew_rw_lock.await_count == heartbeat_calls


@pytest.mark.asyncio
async def test_worker_loop_drains_fast_ready_concurrently():
    """开态 worker 优先处理 fast-path 任务并支持并发执行。"""
    from scheduler_core.models import TaskKind as UnifiedTaskKind

    q = AsyncMock(spec=DistributedTaskQueue)
    s = _make_scheduler(unified_enabled=True, queue=q)

    unified = s._unified()
    # fast-path 队列放入两个任务
    unified._fast_ready = asyncio.Queue()
    for i in range(2):
        unified._fast_ready.put_nowait(
            SimpleNamespace(
                id=f"t{i}",
                kind=UnifiedTaskKind.JOB_TASK,
                payload={"hyperrag_task_id": f"t{i}"},
            )
        )

    started = []

    async def fake_execute(utask):
        started.append(utask.id)
        await asyncio.sleep(0.05)

    s._execute_unified_task = fake_execute
    s._running = True

    # 手动跑一轮 drain：直接调用内部逻辑太脆弱，改为让 worker 跑短暂时间
    s._unified_max_concurrent = lambda: 4
    worker_task = asyncio.create_task(s._unified_worker_loop())
    await asyncio.sleep(0.15)
    s._running = False
    worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker_task

    assert sorted(started) == ["t0", "t1"]


# ---------------------------------------------------------------------------
# worker kind 隔离：HyperRAG worker 绝不领走 AGENT_TOOL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("takeover", [False, True])
async def test_drain_queues_only_picks_job_tasks(takeover):
    """drain_queues 只取 JOB_TASK：worker 取活必须按 kind 过滤，不领在线工具任务。

    多 worker 共享同一批 domain 队列，HyperRAG worker 若领走 AGENT_TOOL 会在
    无 handler 的情况下把它 complete，导致 ViKnow 侧等待的 future 永不 resolve。
    锁定不变量：worker 取活走 peek_kind(JOB_TASK)，且不会执行 agent_tool。
    """
    import json

    from scheduler_core.models import TaskKind as UnifiedTaskKind

    q = AsyncMock(spec=DistributedTaskQueue)
    s = _make_scheduler(unified_enabled=True, queue=q)

    store = MagicMock()
    store.peek_kind = MagicMock(return_value="job-t1")
    store.get_task = MagicMock(
        return_value={
            "kind": "job_task",
            "tier": "background",
            "domains": "gpu",
            "slots": "1",
            "status": "queued",
            "unit_kind": "",
            "run_id": "",
            "depends_on": "",
            "job_id": "job-1",
            "task_type": "video_parse",
            "executor_ref": "video_parse",
            "preemptible": "1",
            "payload": json.dumps({"hyperrag_task_id": "t1", "hyperrag_job_id": "job-1"}),
            "error": "",
            "created_at": "0.0",
        }
    )
    admissions = []

    def acquire(task, *, ts):
        admissions.append(ts)
        store.get_task.return_value["started_at"] = str(ts + (1 if takeover else 0))
        # Queue seq may have changed between the initial read and admission.
        store.get_task.return_value["seq"] = "99"
        return True

    store.try_acquire = MagicMock(side_effect=acquire)
    s._unified().store = store
    s._unified_max_concurrent = lambda: 4
    s._running = True

    executed = []
    s._execute_unified_task = AsyncMock(side_effect=lambda utask: executed.append(utask))

    # 运行 worker 一个短周期，让它走 drain_queues 分支
    worker_task = asyncio.create_task(s._unified_worker_loop())
    await asyncio.sleep(0.15)
    s._running = False
    worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker_task

    # 取活必须按 JOB_TASK 过滤（绝不能用 peek 全量或 AGENT_TOOL）
    store.peek_kind.assert_any_call("chat-llm", UnifiedTaskKind.JOB_TASK.value)
    # 不变量：被执行的统一任务必须是 JOB_TASK
    if takeover:
        assert not executed
    else:
        assert executed and executed[0].id == "job-t1"
        assert executed[0].kind is UnifiedTaskKind.JOB_TASK
        assert executed[0].started_at in admissions
        assert executed[0].seq == 99
    # 未使用未过滤的 peek 取活
    store.peek.assert_not_called()


@pytest.mark.asyncio
async def test_execute_unified_task_skips_agent_tool():
    """_execute_unified_task 拿到 AGENT_TOOL（在线工具请求）时重新入队，不 complete。

    锁定不变量：HyperRAG worker 不会把 ViKnow 的工具任务标成 done。
    """
    from scheduler_core.models import TaskKind as UnifiedTaskKind

    q = AsyncMock(spec=DistributedTaskQueue)
    s = _make_scheduler(unified_enabled=True, queue=q)

    store = MagicMock()
    s._unified().store = store

    utask = SimpleNamespace(
        id="tool-99",
        kind=UnifiedTaskKind.AGENT_TOOL,
        payload={"tool_metadata": {"tool_id": "video_inspect"}},
    )
    await s._execute_unified_task(utask)

    store.enqueue.assert_not_called()
    store.complete.assert_not_called()
    store.mark_failed.assert_not_called()
    q.get_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_drain_ready_skips_non_job_tasks():
    """drain_ready 跳过 fast-path 中非 JOB_TASK 的任务并放回队尾。"""
    from scheduler_core.models import TaskKind as UnifiedTaskKind

    q = AsyncMock(spec=DistributedTaskQueue)
    s = _make_scheduler(unified_enabled=True, queue=q)

    unified = s._unified()
    unified._fast_ready = asyncio.Queue()
    unified._fast_ready.put_nowait(
        SimpleNamespace(
            id="tool-1",
            kind=UnifiedTaskKind.AGENT_TOOL,
            payload={"tool_metadata": {}},
        )
    )

    started = []
    s._execute_unified_task = AsyncMock(side_effect=lambda utask: started.append(utask.id))
    s._running = True
    s._unified_max_concurrent = lambda: 4

    worker_task = asyncio.create_task(s._unified_worker_loop())
    await asyncio.sleep(0.15)
    s._running = False
    worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker_task

    # AGENT_TOOL 不会被 HyperRAG worker 执行
    assert started == []
    # 且被放回 fast-path 队列（未被丢弃）
    assert unified._fast_ready.qsize() == 1


# ---------------------------------------------------------------------------
# DAG 补偿扫描（_recover_stalled_jobs）
# ---------------------------------------------------------------------------


def _make_recovery_scheduler(unified_enabled: bool = True, queue=None):
    """构造用于补偿扫描测试的 scheduler，unified 存储 mock。"""
    q = queue or AsyncMock(spec=DistributedTaskQueue)
    unified = None
    if unified_enabled:
        store = MagicMock()
        store.recover_dag = MagicMock(return_value=0)
        unified = SimpleNamespace(
            config=SimpleNamespace(
                enabled=True,
                domains={
                    "chat-llm": object(),
                    "vllm": object(),
                    "embedding": object(),
                    "asr": object(),
                    "rerank": object(),
                    "hyperrag-db": object(),
                },
            ),
            store=store,
            submit_task=AsyncMock(),
            submit_job=AsyncMock(),
        )
    return TaskScheduler(
        queue=q,
        instance_id="inst-1",
        max_concurrent_job_tasks=10,
        unified_scheduler=unified,
    )


def _with_redis_zscore(queue: AsyncMock, member_in_queue: bool):
    """让 mock queue 的 redis.zscore 返回非空（任务已在队列）或 None（不在队列）。"""
    queue.redis = AsyncMock()
    queue.redis.zscore.return_value = 5.0 if member_in_queue else None
    return queue


@pytest.mark.asyncio
async def test_recover_one_job_unified_does_not_reactivate_tasks():
    """开态：HyperRAG 补偿扫描不激活下游；DAG 入队由 scheduler-core recover_dag 负责。"""
    q = AsyncMock(spec=DistributedTaskQueue)
    _with_redis_zscore(q, member_in_queue=False)
    s = _make_recovery_scheduler(unified_enabled=True, queue=q)

    job = _make_job(task_ids=["t1", "t2"])
    t1 = _make_task("t1", "video_parse", status=TaskStatus.COMPLETED)
    t2 = _make_task("t2", "kg_index", status=TaskStatus.PENDING, depends_on=["t1"])
    s._queue.get_task = AsyncMock(side_effect=lambda tid: {t1.task_id: t1, t2.task_id: t2}[tid])

    recovered = await s._recover_one_job_unified(job)
    assert recovered is False
    assert t2.status == TaskStatus.PENDING
    s._queue.save_task.assert_not_awaited()
    s._queue.enqueue.assert_not_awaited()
    s._unified().submit_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_recover_one_job_skips_task_already_in_queue():
    """关态：QUEUED 任务已在等待队列中 → 不重复激活。"""
    q = AsyncMock(spec=DistributedTaskQueue)
    _with_redis_zscore(q, member_in_queue=True)
    s = _make_recovery_scheduler(unified_enabled=True, queue=q)

    job = _make_job(task_ids=["t1"])
    t1 = _make_task("t1", "video_parse", status=TaskStatus.QUEUED)
    s._queue.get_task = AsyncMock(return_value=t1)

    recovered = await s._recover_one_job(job)
    assert recovered is False
    s._queue.enqueue.assert_not_awaited()
    assert s._unified().submit_task.await_count == 0


@pytest.mark.asyncio
async def test_recover_one_job_requeues_queued_but_missing_from_queue():
    """关态：QUEUED 任务不在等待队列中（入队丢失）→ 补偿扫描重新入队。"""
    q = AsyncMock(spec=DistributedTaskQueue)
    q.redis = AsyncMock()
    q.redis.zscore.return_value = None
    s = _make_recovery_scheduler(unified_enabled=False, queue=q)

    job = _make_job(task_ids=["t1"])
    t1 = _make_task("t1", "video_parse", status=TaskStatus.QUEUED)
    s._queue.get_task = AsyncMock(return_value=t1)
    s._queue.TASK_QUEUE = "hyperrag:queue:{task_type}"

    recovered = await s._recover_one_job(job)
    assert recovered is True
    assert t1.status == TaskStatus.QUEUED
    s._queue.enqueue.assert_awaited_once_with(t1)


@pytest.mark.asyncio
async def test_recover_one_job_waits_for_dependencies():
    """PENDING 任务依赖未满足 → 不激活。"""
    q = AsyncMock(spec=DistributedTaskQueue)
    s = _make_recovery_scheduler(unified_enabled=True, queue=q)

    job = _make_job(task_ids=["t1", "t2"])
    t1 = _make_task("t1", "video_parse", status=TaskStatus.PENDING)
    t2 = _make_task("t2", "kg_index", status=TaskStatus.PENDING, depends_on=["t1"])
    s._queue.get_task = AsyncMock(side_effect=lambda tid: {t1.task_id: t1, t2.task_id: t2}[tid])
    # 模拟依赖未满足（例如依赖任务还没完成）
    s._check_dependencies = AsyncMock(return_value=False)

    recovered = await s._recover_one_job(job)
    assert recovered is False
    assert t1.status == TaskStatus.PENDING
    assert s._unified().submit_task.await_count == 0


@pytest.mark.asyncio
async def test_recover_one_job_skips_running():
    """任务正在运行 → 不干预（job 正常推进中）。"""
    q = AsyncMock(spec=DistributedTaskQueue)
    s = _make_recovery_scheduler(unified_enabled=True, queue=q)

    job = _make_job(task_ids=["t1"])
    t1 = _make_task("t1", "video_parse", status=TaskStatus.RUNNING)
    s._queue.get_task = AsyncMock(return_value=t1)

    recovered = await s._recover_one_job(job)
    assert recovered is False
    assert s._unified().submit_task.await_count == 0


@pytest.mark.asyncio
async def test_recover_stalled_jobs_unified_calls_core_recover_dag():
    """开态：_recover_stalled_jobs 调用 scheduler-core recover_dag。"""
    q = AsyncMock(spec=DistributedTaskQueue)
    s = _make_recovery_scheduler(unified_enabled=True, queue=q)
    s._queue.list_running_jobs = AsyncMock(return_value=[])

    s._unified().store.recover_dag.return_value = 2
    recovered = await s._recover_stalled_jobs()
    assert recovered == 2
    s._unified().store.recover_dag.assert_called_once()


@pytest.mark.asyncio
async def test_recover_stalled_jobs_iterates_running_jobs():
    """关态：_recover_stalled_jobs 遍历 running job 并补偿入队。"""
    q = AsyncMock(spec=DistributedTaskQueue)
    q.redis = AsyncMock()
    q.redis.zscore.return_value = None
    s = _make_recovery_scheduler(unified_enabled=False, queue=q)

    job = _make_job(task_ids=["t1", "t2"])
    t1 = _make_task("t1", "video_parse", status=TaskStatus.COMPLETED)
    t2 = _make_task("t2", "kg_index", status=TaskStatus.PENDING, depends_on=["t1"])
    s._queue.list_running_jobs = AsyncMock(return_value=[job])
    s._queue.get_task = AsyncMock(side_effect=lambda tid: {t1.task_id: t1, t2.task_id: t2}[tid])
    s._queue.TASK_QUEUE = "hyperrag:queue:{task_type}"

    recovered = await s._recover_stalled_jobs()
    assert recovered == 1
    assert t2.status == TaskStatus.QUEUED
    s._queue.list_running_jobs.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_recover_stalled_jobs_tolerates_list_failure():
    """枚举 running job 失败时不崩溃，返回 0。"""
    q = AsyncMock(spec=DistributedTaskQueue)
    s = _make_recovery_scheduler(unified_enabled=True, queue=q)
    s._queue.list_running_jobs = AsyncMock(side_effect=RuntimeError("redis down"))

    recovered = await s._recover_stalled_jobs()
    assert recovered == 0


@pytest.mark.asyncio
async def test_recover_one_job_legacy_mode_uses_hyperrag_queue():
    """原调度模式（unified 关态）：补偿扫描走 hyperrag:queue 检查并重新 enqueue。"""
    q = AsyncMock(spec=DistributedTaskQueue)
    q.redis = AsyncMock()
    # 任务不在 hyperrag:queue → 需要重新入队
    q.redis.zscore.return_value = None
    s = _make_recovery_scheduler(unified_enabled=False, queue=q)

    job = _make_job(task_ids=["t1"])
    t1 = _make_task("t1", "video_parse", status=TaskStatus.QUEUED)
    s._queue.get_task = AsyncMock(return_value=t1)
    s._queue.TASK_QUEUE = "hyperrag:queue:{task_type}"

    recovered = await s._recover_one_job(job)
    assert recovered is True
    # 原调度模式：检查的是 hyperrag:queue 键，重新入队走 enqueue
    q.redis.zscore.assert_awaited_once_with("hyperrag:queue:video_parse", "t1")
    s._queue.enqueue.assert_awaited_once_with(t1)
    s._queue.save_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_recover_one_job_legacy_mode_skips_when_in_queue():
    """原调度模式：任务已在 hyperrag:queue 中 → 不重复入队。"""
    q = AsyncMock(spec=DistributedTaskQueue)
    q.redis = AsyncMock()
    q.redis.zscore.return_value = 5.0
    s = _make_recovery_scheduler(unified_enabled=False, queue=q)

    job = _make_job(task_ids=["t1"])
    t1 = _make_task("t1", "video_parse", status=TaskStatus.QUEUED)
    s._queue.get_task = AsyncMock(return_value=t1)
    s._queue.TASK_QUEUE = "hyperrag:queue:{task_type}"

    recovered = await s._recover_one_job(job)
    assert recovered is False
    s._queue.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_recover_one_job_marks_completed_when_all_tasks_done():
    """所有任务已完成但 job 仍是 running（update_job 曾因 Redis 故障中断）时补偿标记 completed。"""
    q = AsyncMock(spec=DistributedTaskQueue)
    _with_redis_zscore(q, member_in_queue=False)
    s = _make_recovery_scheduler(unified_enabled=True, queue=q)

    job = _make_job(task_ids=["t1", "t2"])
    job.status = "running"
    t1 = _make_task("t1", "video_parse", status=TaskStatus.COMPLETED)
    t2 = _make_task("t2", "kg_index", status=TaskStatus.COMPLETED)
    s._queue.get_task = AsyncMock(side_effect=lambda tid: {t1.task_id: t1, t2.task_id: t2}[tid])

    recovered = await s._recover_one_job(job)
    assert recovered is True
    assert job.status == "completed"
    assert job.completed_at is not None
    s._queue.update_job.assert_awaited_once_with(job)
    assert s._unified().submit_task.await_count == 0


@pytest.mark.asyncio
async def test_recovery_loop_runs_and_recovers_stalled_jobs(monkeypatch):
    """_recovery_loop 周期性触发补偿扫描，不因属性/内部错误而中断循环。

    回归保护：常量 DAG_RECOVERY_INTERVAL_SECONDS 是模块级变量，过去误以
    self.DAG_RECOVERY_INTERVAL_SECONDS 访问导致每轮 sleep 前抛
    AttributeError，补偿扫描从未真正执行。
    """
    q = AsyncMock(spec=DistributedTaskQueue)
    s = _make_recovery_scheduler(unified_enabled=True, queue=q)
    s._running = True

    recover_calls = 0

    async def fake_recover():
        nonlocal recover_calls
        recover_calls += 1
        if recover_calls >= 2:
            s._running = False
        return 0

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(s, "_recover_stalled_jobs", fake_recover)
    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    await s._recovery_loop()

    assert recover_calls == 2
    assert s._running is False


def test_unified_worker_always_sleeps_while_jobs_running():
    """有任务在跑时也必须 sleep，否则会空转占满 GIL、拖死同进程 uvicorn。"""
    assert unified_worker_idle_seconds(executed=False, running_count=1) > 0
    assert unified_worker_idle_seconds(executed=True, running_count=2) > 0
    assert unified_worker_idle_seconds(executed=True, running_count=0) > 0
    assert unified_worker_idle_seconds(executed=False, running_count=0) == 0.1


@pytest.mark.asyncio
async def test_heartbeat_error_must_not_mask_persisted_completion():
    from redis.exceptions import TimeoutError as RedisTimeoutError
    from scheduler_core.models import TaskKind as UnifiedTaskKind

    scheduler = _make_scheduler()
    scheduler._unified().config.heartbeat_interval_seconds = 0.001
    store = scheduler._unified().store
    store.renew.side_effect = RedisTimeoutError("transient network")
    task = _make_task("t1", "video_parse", status=TaskStatus.COMPLETED)
    scheduler._queue.get_task.return_value = task

    async def execute(_):
        await asyncio.sleep(0.015)

    scheduler._execute_task = execute
    scheduler._finish_unified = AsyncMock(return_value="completed")
    utask = SimpleNamespace(
        id="t1",
        kind=UnifiedTaskKind.JOB_TASK,
        payload={"hyperrag_task_id": "t1"},
        started_at=123.0,
        seq=1,
    )
    await scheduler._execute_unified_task(utask)
    scheduler._finish_unified.assert_awaited_once_with(utask, task)


@pytest.mark.asyncio
async def test_cancelled_unified_handler_never_completes_dependencies():
    from scheduler_core.models import TaskKind as UnifiedTaskKind

    scheduler = _make_scheduler()
    task = _make_task("t1", "video_parse", status=TaskStatus.CANCELLED)
    scheduler._queue.get_task.return_value = task
    scheduler._execute_task = AsyncMock(side_effect=asyncio.CancelledError)
    scheduler._finish_unified = AsyncMock(return_value="cancelled")
    utask = SimpleNamespace(
        id="t1",
        kind=UnifiedTaskKind.JOB_TASK,
        payload={"hyperrag_task_id": "t1"},
        started_at=123.0,
        seq=1,
    )
    with pytest.raises(asyncio.CancelledError):
        await scheduler._execute_unified_task(utask)
    scheduler._unified().store.complete.assert_not_called()


@pytest.mark.asyncio
async def test_handler_recorded_failure_never_completes_dependencies():
    from scheduler_core.models import TaskKind as UnifiedTaskKind

    scheduler = _make_scheduler()
    task = _make_task("t1", "video_parse", status=TaskStatus.FAILED)
    scheduler._queue.get_task.return_value = task
    scheduler._execute_task = AsyncMock()
    scheduler._finish_unified = AsyncMock(return_value="failed")
    utask = SimpleNamespace(
        id="t1",
        kind=UnifiedTaskKind.JOB_TASK,
        payload={"hyperrag_task_id": "t1"},
        started_at=123.0,
        seq=1,
    )
    await scheduler._execute_unified_task(utask)
    scheduler._unified().store.complete.assert_not_called()


@pytest.mark.asyncio
async def test_current_admission_overrides_upstream_metadata():
    from scheduler_core.models import TaskKind as UnifiedTaskKind

    scheduler = _make_scheduler()
    task = _make_task("t2", "kg_index", status=TaskStatus.COMPLETED)
    scheduler._queue.get_task.return_value = task

    async def hydrate(task):
        task.metadata["_unified_admission"] = {"started_at": 1.0, "seq": 1}

    async def execute(task):
        assert task.metadata["_unified_admission"] == {"started_at": 123.0, "seq": 9}

    scheduler._hydrate_task_metadata_from_deps = hydrate
    scheduler._execute_task = execute
    scheduler._on_task_failed = AsyncMock(side_effect=AssertionError("handler assertion failed"))
    scheduler._finish_unified = AsyncMock()
    utask = SimpleNamespace(
        id="t2",
        kind=UnifiedTaskKind.JOB_TASK,
        payload={"hyperrag_task_id": "t2"},
        started_at=123.0,
        seq=9,
    )
    # Avoid the completed-status exception guard hiding an assertion.
    task.status = TaskStatus.RUNNING
    await scheduler._execute_unified_task(utask)
