"""
Job 构造器

根据资源信息自动创建 Job 和 Task
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from .models import SOP_VIDEO_JOB, VIDEO_UPDATE_JOB, JobInstance, Task, TaskStatus, TaskType
from .registry import JobRegistry, TaskRegistry

logger = logging.getLogger(__name__)

OPTIONAL_TASK_TYPES = {TaskType.IMAGE_CAPTION}


class JobBuilder:
    """
    Job构造器，根据资源信息自动创建Job和Task
    """

    @staticmethod
    def from_asset(
        workspace: str,
        asset_id: str,
        asset_path: str,
        params: dict | None = None,
        callbacks: dict | None = None,
        request_id: str | None = None,
    ) -> tuple[JobInstance, list[Task]]:
        """
        根据资源路径构造Job

        1. 根据文件扩展名查询JobRegistry获取JobDefinition
        2. 创建JobInstance
        3. 为每个stage创建Task，设置依赖关系
        4. 返回构造好的JobInstance和Task列表

        Args:
            workspace: 工作空间名称
            asset_id: 资源ID
            asset_path: 资源文件路径
            params: 处理参数
            callbacks: 回调配置

        Returns:
            (job, tasks): Job实例和Task列表

        Raises:
            ValueError: 如果找不到匹配的Job定义
        """
        # 优先检查 params 中的 parser_type 参数
        params_dict = params or {}
        parser_type = params_dict.get("parser_type")

        # 如果指定了 sop_video parser_type，使用 SOP_VIDEO_JOB
        if params_dict.get("job_type") == "video_update":
            job_def = VIDEO_UPDATE_JOB
            logger.info("Using VIDEO_UPDATE_JOB for job_type=video_update")
        elif parser_type == "sop_video":
            job_def = SOP_VIDEO_JOB
            logger.info(f"Using SOP_VIDEO_JOB for parser_type={parser_type}")
        else:
            # 根据文件扩展名选择 Job
            ext = Path(asset_path).suffix.lower()
            job_def = JobRegistry.get_by_extension(ext)

        if not job_def:
            ext = Path(asset_path).suffix.lower()
            raise ValueError(
                f"No job definition found for extension: {ext}. "
                f"Available extensions: {_get_available_extensions()}"
            )

        enabled_stages = []
        pre_compile_mode = bool(params_dict.get("pre_compile", False))
        for stage in job_def.stages:
            if pre_compile_mode and stage.task_type == TaskType.BM25_INDEX:
                logger.info("Skip stage for pre_compile mode: %s", stage.task_type)
                continue
            task_def = TaskRegistry.get(stage.task_type)
            if task_def is None and stage.task_type in OPTIONAL_TASK_TYPES:
                logger.info(f"Skip optional stage without registered handler: {stage.task_type}")
                continue
            if task_def is None:
                raise ValueError(
                    f"Task type not registered: {stage.task_type}. "
                    f"Please register this task type before using it."
                )
            enabled_stages.append(stage)

        # 创建Job实例
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        job = JobInstance(
            job_id=job_id,
            job_name=job_def.name,
            workspace=workspace,
            asset_id=asset_id,
            asset_path=asset_path,
            stages=[stage.name for stage in enabled_stages],
            metadata={**(params or {}), **({"_request_id": request_id} if request_id else {})},
        )

        # 创建所有阶段的Task
        tasks = []
        prev_task_id = None

        for i, stage in enumerate(enabled_stages):
            task_def = TaskRegistry.get(stage.task_type)
            assert task_def is not None

            resource = task_def.resource

            task = Task(
                task_id=f"task-{uuid.uuid4().hex[:12]}",
                task_type=stage.task_type,
                workspace=workspace,
                asset_id=asset_id,
                status=TaskStatus.PENDING if i > 0 else TaskStatus.QUEUED,
                resource=resource,
                priority=resource.priority,
                job_id=job_id,
                job_name=job_def.name,
                stage_index=i,
                depends_on=[prev_task_id] if prev_task_id else [],
                callbacks=callbacks or {},
                metadata={
                    "asset_path": asset_path,
                    **(params or {}),
                    **({"_request_id": request_id} if request_id else {}),
                },
            )

            tasks.append(task)
            job.task_ids.append(task.task_id)
            prev_task_id = task.task_id

        logger.info(
            f"Job built: {job_id} for asset {asset_id} "
            f"({len(tasks)} stages: {', '.join(stage.name for stage in enabled_stages)})"
        )

        return job, tasks


def _get_available_extensions() -> str:
    """获取所有已注册的文件扩展名列表"""
    all_extensions = []
    for job_name in JobRegistry.list_jobs():
        job_def = JobRegistry.get(job_name)
        if job_def:
            all_extensions.extend(job_def.file_extensions)
    return ", ".join(sorted(set(all_extensions)))
