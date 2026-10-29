"""HyperRAG: 多模态知识图谱检索引擎。"""

__version__ = "0.1.0"

# 配置类和加载工具
from .config_loader import (
    HyperRAGConfig,
    TaskLimitConfig,
    load_config_from_yaml,
    get_default_task_limits,
)

# 新入口：HyperRAGV2（多 workspace + task）
from .hyperrag_v2 import HyperRAGV2
from .single_layer_kg import SingleLayerKGService

# 任务管理相关（从新的 task 模块导入）
from .task import (
    # 数据模型
    Task,
    JobDefinition,
    JobInstance,
    TaskResource,
    TaskResult,
    TaskStage,
    TaskStatus,
    TaskType,
    # 注册表
    CallbackRegistry,
    JobRegistry,
    TaskRegistry,
    # 调度器
    InstanceMode,
)

__all__ = [
    # 版本
    "__version__",
    # 主类
    "HyperRAGV2",
    "SingleLayerKGService",
    "HyperRAGConfig",
    # 配置加载
    "load_config_from_yaml",
    "get_default_task_limits",
    "TaskLimitConfig",
    # 任务管理
    "Task",
    "TaskStatus",
    "TaskType",
    "TaskResource",
    "TaskResult",
    "TaskStage",
    "JobDefinition",
    "JobInstance",
    "InstanceMode",
    # 注册表
    "TaskRegistry",
    "JobRegistry",
    "CallbackRegistry",
]
