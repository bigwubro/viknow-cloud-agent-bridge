"""
任务定义和数据结构

包含：
- TaskStatus: 任务状态枚举
- TaskType: 任务类型枚举
- TaskResource: 任务资源需求
- Task: 任务实例
- TaskStage: Job阶段
- JobDefinition: Job定义
- JobInstance: Job实例
- TaskResult: 任务执行结果
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """任务状态"""

    PENDING = "pending"  # 等待依赖完成
    QUEUED = "queued"  # 已入队，等待调度
    RUNNING = "running"  # 执行中
    COMPLETED = "completed"  # 已完成
    FAILED = "failed"  # 失败
    CANCELLED = "cancelled"  # 已取消
    ABORTED = "aborted"  # 已中止
    ROLLED_BACK = "rolled_back"  # 已回退


class TaskType(str, Enum):
    """任务类型"""

    DOC_PARSE = "doc_parse"  # 文档解析
    IMAGE_CAPTION = "image_caption"  # 图片/表格Caption增强
    VIDEO_PARSE = "video_parse"  # 视频解析（合并ASR/Caption等预处理）
    VIDEO_ASR = "video_asr"  # 语音ASR
    VIDEO_CAPTION = "video_caption"  # 视频描述生成
    VIDEO_TRACKING = "video_tracking"  # 目标追踪（SAM3，预留）
    CHUNKING = "chunking"  # chunking
    KG_INDEX = "kg_index"  # KG索引
    BM25_INDEX = "bm25_index"  # BM25全文索引

    DELETE_ASSET = "delete_asset"  # 资源删除
    DELETE_INDEX = "delete_index"  # 仅删除索引数据（保留视频缓存与语义文件）
    UPDATE_CAPTION = "update_caption"  # 基于已编辑语义文件更新视频 caption
    SOP_VIDEO_PARSE = "sop_video_parse"  # SOP视频解析（SOP文档+视频对齐）


class TaskResource(BaseModel):
    """任务资源需求"""

    slots: int = 1  # 占用的并发槽位
    gpu_memory_mb: int = 0  # 需要的GPU显存(MB)
    priority: int = 5  # 优先级(1-10，1最高)


class Task(BaseModel):
    """
    任务实例

    表示一个需要执行的任务，包含状态、资源需求、依赖关系等
    """

    task_id: str
    task_type: str
    workspace: str
    asset_id: str
    status: TaskStatus
    resource: TaskResource
    priority: int = 5

    # 依赖管理
    job_id: str = ""  # 所属Job实例ID
    job_name: str = ""  # 所属Job名称
    stage_index: int = 0  # 在Job中的阶段索引
    depends_on: List[str] = Field(default_factory=list)  # 依赖的task_id

    # 生命周期
    created_at: float = Field(default_factory=lambda: time.time())
    started_at: Optional[float] = None
    completed_at: Optional[float] = None

    # 回调配置
    callbacks: Dict[str, str] = Field(default_factory=dict)  # {"on_complete": "callback_name", ...}

    # 执行结果
    result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None

    # 元数据（传递给下一阶段）
    metadata: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        use_enum_values = True

    def to_dict(self) -> dict:
        """序列化为字典（用于 Redis 存储）"""
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        """从字典反序列化"""
        return cls.model_validate(data)


class TaskStage(BaseModel):
    """Job阶段"""

    name: str  # 阶段名称
    task_type: str  # 任务类型
    optional: bool = False  # 是否可选


class JobDefinition(BaseModel):
    """
    Job定义

    定义文档处理的任务序列，每个文件类型对应一个Job定义
    """

    name: str  # Job名称
    stages: List[TaskStage]  # 任务阶段列表（按顺序执行）
    file_extensions: List[str]  # 适用的文件扩展名


class JobInstance(BaseModel):
    """
    Job实例

    跟踪一个文档的完整处理流程
    """

    job_id: str
    job_name: str
    workspace: str
    asset_id: str
    asset_path: str
    stages: List[str]  # 阶段名称列表
    current_stage: int = 0
    task_ids: List[str] = Field(default_factory=list)  # 各阶段的task_id
    status: str = "running"  # running, completed, failed, cancelled
    created_at: float = Field(default_factory=lambda: time.time())
    completed_at: Optional[float] = None
    # 失败时的错误信息（便于上层直接展示/打印）
    error_message: Optional[str] = None
    failed_task_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict:
        """序列化为字典（用于 Redis 存储）"""
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict) -> "JobInstance":
        """从字典反序列化"""
        return cls.model_validate(data)


class TaskResult(BaseModel):
    """任务执行结果"""

    status: str  # "completed", "failed", "requeue"
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class TaskTypeDefinition(BaseModel):
    """任务类型定义"""

    name: str
    handler: Any  # Callable[[Task, Any], Any] - pydantic 不支持 Callable 序列化
    resource: TaskResource
    rollback_handler: Optional[Any] = None  # 回退处理器

    class Config:
        arbitrary_types_allowed = True


# 内置Job定义
DOCUMENT_JOB = JobDefinition(
    name="document",
    stages=[
        TaskStage(name="delete_asset", task_type=TaskType.DELETE_ASSET),
        TaskStage(name="doc_parse", task_type=TaskType.DOC_PARSE),
        TaskStage(name="image_caption", task_type=TaskType.IMAGE_CAPTION),
        TaskStage(name="kg_index", task_type=TaskType.KG_INDEX),
        TaskStage(name="bm25_index", task_type=TaskType.BM25_INDEX),
    ],
    file_extensions=[
        ".pdf",
        ".doc",
        ".docx",
        ".txt",
        ".md",
        ".ppt",
        ".pptx",
        ".xls",
        ".xlsx",
        ".png",
        ".jpg",
        ".jpeg",
        ".bmp",
        ".tiff",
        ".tif",
        ".gif",
        ".webp",
    ],
)

VIDEO_JOB = JobDefinition(
    name="video",
    stages=[
        TaskStage(name="delete_asset", task_type=TaskType.DELETE_ASSET),
        TaskStage(name="video_parse", task_type=TaskType.VIDEO_PARSE),
        TaskStage(name="kg_index", task_type=TaskType.KG_INDEX),
        TaskStage(name="bm25_index", task_type=TaskType.BM25_INDEX),
    ],
    file_extensions=[".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm"],
)

VIDEO_UPDATE_JOB = JobDefinition(
    name="video_update",
    stages=[
        TaskStage(name="delete_index", task_type=TaskType.DELETE_INDEX),
        TaskStage(name="update_caption", task_type=TaskType.UPDATE_CAPTION),
        TaskStage(name="kg_index", task_type=TaskType.KG_INDEX),
        TaskStage(name="bm25_index", task_type=TaskType.BM25_INDEX),
    ],
    file_extensions=[],
)


# Terminology assets: structured JSON text, no parse stage; index directly.
TERMINOLOGY_JOB = JobDefinition(
    name="terminology",
    stages=[
        TaskStage(name="delete_asset", task_type=TaskType.DELETE_ASSET),
        TaskStage(name="kg_index", task_type=TaskType.KG_INDEX),
    ],
    # NOTE: JobBuilder selects by Path(asset_path).suffix, so this must be the final suffix.
    file_extensions=[".terminology"],
)

# Delete asset job: remove asset and all related data
DELETE_JOB = JobDefinition(
    name="delete",
    stages=[
        TaskStage(name="delete_asset", task_type=TaskType.DELETE_ASSET),
    ],
    file_extensions=[],  # Not file-based, manually constructed
)

# SOP Video job: process video with SOP document alignment
# Triggered by parser_type="sop_video" in params, not by file extension
SOP_VIDEO_JOB = JobDefinition(
    name="sop_video",
    stages=[
        TaskStage(name="sop_video_parse", task_type=TaskType.SOP_VIDEO_PARSE),
        TaskStage(name="kg_index", task_type=TaskType.KG_INDEX),
        TaskStage(name="bm25_index", task_type=TaskType.BM25_INDEX),
    ],
    file_extensions=[],  # Triggered via params, not file extension
)
