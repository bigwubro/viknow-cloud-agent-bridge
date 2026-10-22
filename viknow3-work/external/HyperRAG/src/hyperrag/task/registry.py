"""
任务注册表

提供可扩展的任务类型、Job和回调函数注册机制
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Optional

from .models import (
    DELETE_JOB,
    DOCUMENT_JOB,
    SOP_VIDEO_JOB,
    TERMINOLOGY_JOB,
    VIDEO_JOB,
    VIDEO_UPDATE_JOB,
    JobDefinition,
    TaskResource,
    TaskTypeDefinition,
)

logger = logging.getLogger(__name__)


class TaskRegistry:
    """
    任务类型注册表

    支持扩展：用户可以注册新的任务类型和处理器
    """

    _types: Dict[str, TaskTypeDefinition] = {}

    @classmethod
    def register(
        cls,
        name: str,
        handler: Callable,
        resource: TaskResource,
        rollback_handler: Optional[Callable] = None,
    ):
        """
        注册新任务类型

        Args:
            name: 任务类型名称
            handler: 任务处理器函数
            resource: 资源需求
            rollback_handler: 回退处理器（可选）
        """
        cls._types[name] = TaskTypeDefinition(
            name=name, handler=handler, resource=resource, rollback_handler=rollback_handler
        )
        logger.info(f"Registered task type: {name}")

    @classmethod
    def get(cls, name: str) -> Optional[TaskTypeDefinition]:
        """获取任务类型定义"""
        return cls._types.get(name)

    @classmethod
    def list_types(cls) -> list[str]:
        """列出所有已注册的任务类型"""
        return list(cls._types.keys())

    @classmethod
    def unregister(cls, name: str) -> bool:
        """注销任务类型"""
        if name in cls._types:
            del cls._types[name]
            logger.info(f"Unregistered task type: {name}")
            return True
        return False


class JobRegistry:
    """
    Job注册表

    管理不同文件类型对应的Job定义
    """

    _jobs: Dict[str, JobDefinition] = {}

    @classmethod
    def register(cls, job: JobDefinition):
        """
        注册Job

        Args:
            job: Job定义实例
        """
        cls._jobs[job.name] = job
        logger.info(f"Registered job: {job.name} for extensions: {job.file_extensions}")

    @classmethod
    def get(cls, name: str) -> Optional[JobDefinition]:
        """根据名称获取Job定义"""
        return cls._jobs.get(name)

    @classmethod
    def get_by_extension(cls, ext: str) -> Optional[JobDefinition]:
        """
        根据文件扩展名选择Job定义

        Args:
            ext: 文件扩展名（如 ".pdf"）

        Returns:
            匹配的Job定义，如果没有匹配则返回 None
        """
        ext_lower = ext.lower()
        for job in cls._jobs.values():
            if ext_lower in job.file_extensions:
                return job
        return None

    @classmethod
    def list_jobs(cls) -> list[str]:
        """列出所有已注册的Job名称"""
        return list(cls._jobs.keys())

    @classmethod
    def unregister(cls, name: str) -> bool:
        """注销Job"""
        if name in cls._jobs:
            del cls._jobs[name]
            logger.info(f"Unregistered job: {name}")
            return True
        return False


class CallbackRegistry:
    """
    回调函数注册表

    管理任务完成/失败时的回调函数
    """

    _callbacks: Dict[str, Callable] = {}

    @classmethod
    def register(cls, name: str, callback: Callable):
        """
        注册回调函数

        Args:
            name: 回调名称
            callback: 回调函数，签名应为 (task: Task) -> None 或 async (task: Task) -> None
        """
        cls._callbacks[name] = callback
        logger.info(f"Registered callback: {name}")

    @classmethod
    def get(cls, name: str) -> Optional[Callable]:
        """获取回调函数"""
        return cls._callbacks.get(name)

    @classmethod
    def list_callbacks(cls) -> list[str]:
        """列出所有已注册的回调名称"""
        return list(cls._callbacks.keys())

    @classmethod
    def unregister(cls, name: str) -> bool:
        """注销回调函数"""
        if name in cls._callbacks:
            del cls._callbacks[name]
            logger.info(f"Unregistered callback: {name}")
            return True
        return False


# 注册内置Job定义
JobRegistry.register(DOCUMENT_JOB)
JobRegistry.register(VIDEO_JOB)
JobRegistry.register(TERMINOLOGY_JOB)
JobRegistry.register(DELETE_JOB)
JobRegistry.register(SOP_VIDEO_JOB)
JobRegistry.register(VIDEO_UPDATE_JOB)


def register_default_callbacks():
    """注册默认回调函数"""

    def notify_completion(task):
        """任务完成通知（示例）"""
        logger.info(
            f"Task completed: {task.task_id} (type={task.task_type}, asset={task.asset_id})"
        )

    def notify_failure(task):
        """任务失败通知（示例）"""
        logger.error(
            f"Task failed: {task.task_id} "
            f"(type={task.task_type}, asset={task.asset_id}, "
            f"error={task.error_message})"
        )

    CallbackRegistry.register("notify_completion", notify_completion)
    CallbackRegistry.register("notify_failure", notify_failure)


# 自动注册默认回调
register_default_callbacks()
