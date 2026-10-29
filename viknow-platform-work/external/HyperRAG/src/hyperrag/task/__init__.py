"""
HyperRAG 任务管理模块
"""

from .models import (
    DOCUMENT_JOB,
    TERMINOLOGY_JOB,
    VIDEO_JOB,
    VIDEO_UPDATE_JOB,
    Task,
    JobDefinition,
    JobInstance,
    TaskResource,
    TaskResult,
    TaskStage,
    TaskStatus,
    TaskType,
    TaskTypeDefinition,
)
from .registry import TaskRegistry, JobRegistry, CallbackRegistry
from .queue import DistributedTaskQueue
from .scheduler import TaskScheduler, InstanceMode
from .job_builder import JobBuilder
from . import handler as default_handlers  # 触发默认 handler 注册

__all__ = [
    # Models
    "Task",
    "TaskStatus",
    "TaskType",
    "TaskResource",
    "TaskResult",
    "TaskStage",
    "JobDefinition",
    "JobInstance",
    "TaskTypeDefinition",
    "DOCUMENT_JOB",
    "TERMINOLOGY_JOB",
    "VIDEO_JOB",
    "VIDEO_UPDATE_JOB",
    # Registry
    "TaskRegistry",
    "JobRegistry",
    "CallbackRegistry",
    # Queue
    "DistributedTaskQueue",
    # Scheduler
    "TaskScheduler",
    "InstanceMode",
    # Job Builder
    "JobBuilder",
    # Handlers
    "default_handlers",
]
