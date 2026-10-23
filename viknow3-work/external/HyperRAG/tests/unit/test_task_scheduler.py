"""
任务调度器集成测试（使用真实 Redis + Postgres + Neo4j + LLM/Embedding）
该文件对应的旧版生命周期实现已废弃，暂时跳过。
"""

import pytest

pytest.skip(
    "Legacy scheduler test skipped: lifecycle callbacks integrated into TaskScheduler",
    allow_module_level=True,
)

