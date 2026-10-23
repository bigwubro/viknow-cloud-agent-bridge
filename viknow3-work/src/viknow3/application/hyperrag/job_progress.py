"""HyperRAG job pipeline progress reader (admin 任务作业页签数据源).

档位2（分阶段进度）的数据来自 HyperRAG 持久化在 Redis 的 job/task 元数据：

- ``hyperrag:job:{job_id}``  -> 阶段列表 stages / 当前阶段 current_stage / 状态
- ``hyperrag:task:{task_id}`` -> 每个阶段任务的 status / 起止时间 / 错误

统一调度开态下 HyperRAG 仍会写这两类 key（只是不再写 hyperrag:queue:*），
因此无论是否启用 scheduler-core，本 reader 都能读到完整的分阶段进度。

进度语义（重要）：
- 阶段级百分比 = 已完成阶段数 / 总阶段数，这是唯一可诚实计算的数值。
- 阶段内没有真实百分比数据源（tqdm / video_split 百分比均未落库），
  因此 active 阶段只上报运行时长，不虚构百分比。
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from viknow3.application.hyperrag.config import AppConfig

logger = logging.getLogger(__name__)

# HyperRAG Redis key 前缀（与 external/HyperRAG/src/hyperrag/task/queue.py 保持一致）
JOB_KEY_TEMPLATE = "hyperrag:job:{job_id}"
TASK_KEY_TEMPLATE = "hyperrag:task:{task_id}"

# 阶段状态（对齐 TaskStatus）
STAGE_STATUS_QUEUED = "queued"
STAGE_STATUS_RUNNING = "running"
STAGE_STATUS_COMPLETED = "completed"
STAGE_STATUS_FAILED = "failed"
STAGE_STATUS_PENDING = "pending"

# Job 状态（对齐 JobInstance.status）
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"

_ACTIVE_TASK_STATUSES = {STAGE_STATUS_QUEUED, STAGE_STATUS_RUNNING, STAGE_STATUS_PENDING}


@dataclass(frozen=True)
class JobStageProgress:
    """单个阶段任务的进度信息。"""

    name: str
    status: str  # pending | queued | running | completed | failed
    task_id: str | None = None
    started_at: float | None = None
    completed_at: float | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class JobProgress:
    """一个索引 job 的分阶段进度。"""

    job_id: str
    job_name: str
    workspace: str
    asset_id: str
    status: str  # running | completed | failed | cancelled
    stages: list[JobStageProgress] = field(default_factory=list)
    current_stage_index: int = 0
    overall_progress: float = 0.0  # 0.0~1.0，由阶段完成数推导
    created_at: float | None = None
    completed_at: float | None = None
    error_message: str | None = None
    running_duration_seconds: float | None = None

    @property
    def stage_count(self) -> int:
        return len(self.stages)

    @property
    def completed_stage_count(self) -> int:
        return sum(1 for stage in self.stages if stage.status == STAGE_STATUS_COMPLETED)

    @property
    def failed_stage(self) -> JobStageProgress | None:
        for stage in self.stages:
            if stage.status == STAGE_STATUS_FAILED:
                return stage
        return None


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _derive_overall_progress(
    status: str,
    stages: list[JobStageProgress],
    current_stage_index: int,
) -> float:
    """由阶段完成情况推导整体进度。

    - 已完成 job -> 1.0
    - 失败/取消  -> 停在失败前的累计进度
    - 运行中     -> 已完成的阶段数 / 总阶段数（不虚构进行中阶段的百分比）
    """
    total = len(stages)
    if total == 0:
        return 1.0 if status == JOB_STATUS_COMPLETED else 0.0
    if status == JOB_STATUS_COMPLETED:
        return 1.0
    completed = sum(1 for stage in stages if stage.status == STAGE_STATUS_COMPLETED)
    if status == JOB_STATUS_FAILED:
        # 失败时整体进度不归零，停在已完成的阶段（含失败前的当前阶段位置）
        return min(1.0, (completed + min(current_stage_index, 1)) / max(total, 1))
    return min(1.0, completed / max(total, 1))


class HyperRAGJobProgressReader:
    """从 Redis 读取 HyperRAG job 分阶段进度的同步 reader。

    使用同步 redis-py 客户端（与 scheduler-core 一致），不绑定 asyncio 事件循环，
    可由 FastAPI 的同步 handler 直接调用。
    """

    def __init__(self, config: AppConfig):
        self._config = config
        self._redis: Any | None = None

    def _client(self) -> Any:
        if self._redis is not None:
            return self._redis
        redis_url = self._redis_url()
        try:
            from scheduler_core.redis_store import create_redis_client
        except ImportError:
            from redis import from_url

            self._redis = from_url(
                redis_url,
                decode_responses=True,
                socket_timeout=5.0,
                socket_connect_timeout=5.0,
                socket_keepalive=True,
            )
        else:
            self._redis = create_redis_client(redis_url)
        return self._redis

    def _redis_url(self) -> str:
        # 统一调度开关只决定 job 任务走哪套队列；job/task 元数据始终落在
        # hyperrag:job:* / hyperrag:task:*，读这些 key 的 Redis 地址取
        # scheduler.unified.redis_url 即可（默认与 hyperrag 的 redis_uri 同源）。
        return self._config.scheduler.unified.redis_url

    def close(self) -> None:
        if self._redis is not None:
            with contextlib.suppress(Exception):
                self._redis.close()
            self._redis = None

    # ---------- 读取 ----------

    def get_job(self, job_id: str) -> JobProgress | None:
        """按 job_id 读取一个 job 的分阶段进度；Redis 无此 job 时返回 None。"""
        raw = self._client().get(JOB_KEY_TEMPLATE.format(job_id=job_id))
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            logger.warning("corrupted job json: %s", job_id)
            return None
        return self._build_progress(data)

    def get_job_by_asset(self, workspace: str, asset_id: str) -> JobProgress | None:
        """通过 job_by_asset 索引读取最新 job。"""
        index_key = f"hyperrag:job_by_asset:{workspace}:{asset_id}"
        job_id = self._client().get(index_key)
        if not job_id:
            return None
        return self.get_job(str(job_id))

    def _build_progress(self, data: dict[str, Any]) -> JobProgress:
        stages_raw = data.get("stages") or []
        task_ids = data.get("task_ids") or []
        # task_ids 与 stages 一一对应（JobBuilder 按 stage 顺序生成）
        stage_count = max(len(stages_raw), len(task_ids))

        task_map = self._load_tasks(task_ids)
        stages: list[JobStageProgress] = []
        for index in range(stage_count):
            stage_name = stages_raw[index] if index < len(stages_raw) else f"stage_{index}"
            task_id = task_ids[index] if index < len(task_ids) else None
            task = task_map.get(task_id) if task_id else None
            if task is not None:
                stages.append(self._stage_from_task(stage_name, task))
            else:
                # 无任务元数据时（如 job 尚未生成所有 task），fallback 到 job 级别信息
                fallback_status = (
                    STAGE_STATUS_RUNNING
                    if index == data.get("current_stage", 0)
                    else STAGE_STATUS_PENDING
                )
                stages.append(
                    JobStageProgress(
                        name=stage_name,
                        status=fallback_status,
                        task_id=task_id,
                    )
                )

        status = str(data.get("status") or JOB_STATUS_RUNNING)
        current_stage = int(data.get("current_stage") or 0)
        created_at = _as_float(data.get("created_at"))
        completed_at = _as_float(data.get("completed_at"))
        overall = _derive_overall_progress(status, stages, current_stage)

        running_duration: float | None = None
        now = time.time()
        if status == JOB_STATUS_RUNNING and created_at is not None:
            running_duration = max(0.0, now - created_at)

        return JobProgress(
            job_id=str(data.get("job_id") or ""),
            job_name=str(data.get("job_name") or ""),
            workspace=str(data.get("workspace") or ""),
            asset_id=str(data.get("asset_id") or ""),
            status=status,
            stages=stages,
            current_stage_index=current_stage,
            overall_progress=overall,
            created_at=created_at,
            completed_at=completed_at,
            error_message=data.get("error_message"),
            running_duration_seconds=running_duration,
        )

    def _load_tasks(self, task_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not task_ids:
            return {}
        keys = [TASK_KEY_TEMPLATE.format(task_id=tid) for tid in task_ids]
        raw_values = self._client().mget(keys)
        result: dict[str, dict[str, Any]] = {}
        for task_id, raw in zip(task_ids, raw_values, strict=True):
            if raw is None:
                continue
            try:
                result[task_id] = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                logger.warning("corrupted task json: %s", task_id)
        return result

    def _stage_from_task(self, stage_name: str, task: dict[str, Any]) -> JobStageProgress:
        return JobStageProgress(
            name=stage_name,
            status=str(task.get("status") or STAGE_STATUS_PENDING),
            task_id=task.get("task_id"),
            started_at=_as_float(task.get("started_at")),
            completed_at=_as_float(task.get("completed_at")),
            error_message=task.get("error_message"),
        )



def _epoch_to_iso(value: float | None) -> str | None:
    if value is None:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def stage_progress_to_api(stage: JobStageProgress) -> dict[str, Any]:
    return {
        "name": stage.name,
        "status": stage.status,
        "task_id": stage.task_id,
        "started_at": _epoch_to_iso(stage.started_at),
        "completed_at": _epoch_to_iso(stage.completed_at),
        "error_message": stage.error_message,
    }


def index_job_task_to_api(stage: JobStageProgress, *, stage_index: int) -> dict[str, Any]:
    """Serialize one pipeline task for ``GET …/index-jobs/{job_id}/tasks``."""
    return {
        "task_id": stage.task_id,
        "name": stage.name,
        "stage": stage.name,
        "stage_index": stage_index,
        "status": stage.status,
        "progress": None,
        "started_at": _epoch_to_iso(stage.started_at),
        "completed_at": _epoch_to_iso(stage.completed_at),
        "error_message": stage.error_message,
    }


def index_job_tasks_from_progress(progress: JobProgress) -> list[dict[str, Any]]:
    return [
        index_job_task_to_api(stage, stage_index=index)
        for index, stage in enumerate(progress.stages)
    ]


def job_progress_to_api(progress: JobProgress) -> dict[str, Any]:
    """Serialize Redis job progress for HTTP ``IndexJob`` polling."""
    current_stage_name = None
    if progress.stages and 0 <= progress.current_stage_index < len(progress.stages):
        current_stage_name = progress.stages[progress.current_stage_index].name
    return {
        "overall": progress.overall_progress,
        "current_stage": current_stage_name,
        "current_stage_index": progress.current_stage_index,
        "stage_count": progress.stage_count,
        "completed_stage_count": progress.completed_stage_count,
        "running_duration_seconds": progress.running_duration_seconds,
        "stages": [stage_progress_to_api(stage) for stage in progress.stages],
    }


def index_job_from_progress(
    progress: JobProgress,
    *,
    status: str,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    error_message = progress.error_message
    if status == "failed" and not error_message:
        failed = progress.failed_stage
        if failed is not None and failed.error_message:
            error_message = failed.error_message
    return {
        "job_id": progress.job_id,
        "library_id": progress.workspace,
        "asset_id": progress.asset_id,
        "status": status,
        "error_message": error_message if status == "failed" else None,
        "metadata": metadata,
        "created_at": _epoch_to_iso(progress.created_at),
        "completed_at": _epoch_to_iso(progress.completed_at),
        "progress": job_progress_to_api(progress),
    }

__all__ = [
    "JOB_STATUS_CANCELLED",
    "JOB_STATUS_COMPLETED",
    "JOB_STATUS_FAILED",
    "JOB_STATUS_RUNNING",
    "STAGE_STATUS_COMPLETED",
    "STAGE_STATUS_FAILED",
    "STAGE_STATUS_PENDING",
    "STAGE_STATUS_QUEUED",
    "STAGE_STATUS_RUNNING",
    "HyperRAGJobProgressReader",
    "JobProgress",
    "JobStageProgress",
    "index_job_from_progress",
    "index_job_task_to_api",
    "index_job_tasks_from_progress",
    "job_progress_to_api",
    "stage_progress_to_api",
]
