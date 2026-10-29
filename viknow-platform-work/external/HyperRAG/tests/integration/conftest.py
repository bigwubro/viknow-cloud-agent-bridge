"""
集成测试配置文件

提供统一的 HyperRAGV2 实例创建、workspace 管理和清理功能
"""

import asyncio
from pathlib import Path
import os

os.environ["MINERU_VL_SERVER"] = "http://dev.ai.qingxiang.tech:8114"
import shutil
import time
import uuid
from typing import AsyncGenerator, Dict, Any

import pytest
from loguru import logger

from hyperrag.config_loader import HyperRAGConfig, TaskLimitConfig, get_default_task_limits
from hyperrag.hyperrag_v2 import HyperRAGV2
from hyperrag.model_func import ImageCaptionFunc, VideoCaptionFunc, ModelConfig


# =============== 测试配置（真实服务） ===============

TEST_REDIS_URI = os.getenv("HYPERRAG_TEST_REDIS_URI", "redis://localhost:16379/15")

TEST_LLM_BASE_URL = os.getenv("HYPERRAG_TEST_LLM_BASE_URL", "http://dev.ai.qingxiang.tech:8080/v1")
TEST_LLM_API_KEY = os.getenv("HYPERRAG_TEST_LLM_API_KEY", "QX123456")
TEST_LLM_MODEL = os.getenv("HYPERRAG_TEST_LLM_MODEL", "Qwen/Qwen3-VL-30B-A3B-Instruct-FP8")
TEST_EMBED_MODEL = os.getenv("HYPERRAG_TEST_EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B")
TEST_EMBEDDING_DIM = int(os.getenv("HYPERRAG_TEST_EMBEDDING_DIM", "1024"))

# Postgres / Neo4j 配置
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "15432")
os.environ.setdefault("POSTGRES_USER", "postgres")
os.environ.setdefault("POSTGRES_PASSWORD", "qx123456")
os.environ.setdefault("POSTGRES_DATABASE", "viknow")

os.environ.setdefault("NEO4J_URI", "bolt://localhost:17687")
os.environ.setdefault("NEO4J_USERNAME", "neo4j")
os.environ.setdefault("NEO4J_PASSWORD", "neo4j")


# =============== 工具函数 ===============


async def _wait_job_completed(v2: HyperRAGV2, job_id: str, timeout_s: int = 1200) -> Dict[str, Any]:
    """
    等待任务完成，失败时展开 task 错误信息

    Args:
        v2: HyperRAGV2 实例
        job_id: 任务ID
        timeout_s: 超时时间（秒）

    Returns:
        job_status 字典

    Raises:
        RuntimeError: 如果任务失败
        TimeoutError: 如果任务超时
    """
    poll = 2
    elapsed = 0
    while elapsed < timeout_s:
        js = await v2.aget_job_status(job_id)
        if js:
            st = str(js.get("status", "")).lower()
            if st == "completed":
                return js
            if st == "failed":
                # 尽量把失败原因挖出来：遍历 task_ids 拉取 task 详情
                details = []
                for tid in js.get("task_ids", []) or []:
                    try:
                        t = await v2.scheduler.get_task_status(tid)  # type: ignore[union-attr]
                    except Exception as e:
                        details.append(f"- task_id={tid} (get_task_status error: {e})")
                        continue
                    if not t:
                        details.append(f"- task_id={tid} (missing)")
                        continue
                    t_type = t.get("task_type")
                    t_status = t.get("status")
                    t_err = (
                        t.get("error")
                        or t.get("error_message")
                        or (t.get("result") or {}).get("error")
                        or (t.get("result") or {}).get("error_message")
                    )
                    details.append(f"- task_id={tid} type={t_type} status={t_status} error={t_err}")

                combined = "\n".join(details)
                # 真实外部服务偶发 502/网关错误时，跳过以避免误报（CI/本地可重试）
                if "502 Bad Gateway" in combined:
                    pytest.skip(f"外部 Embedding/LLM 服务 502，跳过本次集成用例。\n{combined}")

                raise RuntimeError(
                    "Job failed: "
                    f"job_id={job_id}, "
                    f"error={js.get('error_message', 'Unknown error')}\n" + combined
                )
        await asyncio.sleep(poll)
        elapsed += poll
    raise TimeoutError(f"Job timeout: job_id={job_id}, timeout_s={timeout_s}")


async def _ensure_rw(v2: HyperRAGV2):
    """确保 HyperRAGV2 实例处于 RW 模式"""
    if getattr(v2, "current_mode", "") == "read_write":
        return
    deadline = time.time() + 30
    while time.time() < deadline:
        ok = await v2.aswitch_mode("read_write")
        if ok and getattr(v2, "current_mode", "") == "read_write":
            return
        await asyncio.sleep(2)
    pytest.skip("无法获取 RW 锁（已有其他 RW 节点在运行），跳过以避免误报。")


# =============== Fixtures ===============


@pytest.fixture(scope="session")
def integration_workspace_dir(tmp_path_factory):
    """session 级工作目录：多个用例共享同一个 HyperRAGV2 实例，避免 RW 锁冲突"""
    d = tmp_path_factory.mktemp("hyperrag_integration_shared")
    return str(d)


@pytest.fixture(scope="session")
async def shared_hyperrag(integration_workspace_dir) -> AsyncGenerator[HyperRAGV2, None]:
    """
    session 级共享 HyperRAGV2：避免多实例 RW 锁互相冲突

    使用方式：
        async def test_something(shared_hyperrag):
            v2 = shared_hyperrag
            workspace = await create_test_workspace(v2)
            # ... 测试代码 ...
            await cleanup_workspace(v2, workspace)
    """
    config = HyperRAGConfig(
        working_dir=integration_workspace_dir,
        # LLM 配置（真实 API）
        llm_model=TEST_LLM_MODEL,
        llm_base_url=TEST_LLM_BASE_URL,
        llm_api_key=TEST_LLM_API_KEY,
        llm_model_kwargs={
            "temperature": 0.7,
            "max_tokens": 4096,
            "presence_penalty": 1.2,
            "top_p": 0.95,
        },
        llm_timeout=60,
        # Embedding 配置（真实 API）
        embedding_model=TEST_EMBED_MODEL,
        embedding_base_url=TEST_LLM_BASE_URL,
        embedding_api_key=TEST_LLM_API_KEY,
        embedding_dim=TEST_EMBEDDING_DIM,
        embedding_timeout=60,
        embedding_max_async=32,
        kg_vector_embedding_batch_size=32,
        # 分块配置
        chunk_token_size=200,
        chunk_overlap_token_size=20,
        # 存储配置（真实 Postgres + Neo4j）
        kv_storage="PGKVStorage",
        vector_storage="PGVectorStorage",
        graph_storage="Neo4JStorage",
        doc_status_storage="PGDocStatusStorage",
        # KG 优化配置
        ER_extraction_max_async=16,
        max_parallel_insert=4,
        enable_llm_cache=True,
        summary_max_tokens=100,
        summary_context_size=1024,
        max_graph_nodes=20,
        cosine_better_than_threshold=0.2,
        # Redis 配置（用于任务队列）
        redis_uri=TEST_REDIS_URI,
        # 实例配置（共享一个 RW 实例）
        instance_id="test-instance-integration-shared",
        initial_mode="read_write",
        heartbeat_interval=15,
        failover_timeout=30,
        # 任务限制配置
        task_limits={
            "doc_parse": TaskLimitConfig(max_concurrent=1, gpu_memory_mb=0),
            "chunking": TaskLimitConfig(max_concurrent=2, gpu_memory_mb=0),
            "kg_index": TaskLimitConfig(max_concurrent=2, gpu_memory_mb=0),
        },
        # Actio 视频解析配置
        actio_config_dir=os.getenv("HYPERRAG_TEST_ACTIO_CONFIG_DIR", "external/actio/configs"),
        actio_sop_schema_path=os.getenv(
            "HYPERRAG_TEST_ACTIO_SCHEMA_PATH",
            "external/actio/docs/specs/sop_schema.md",
        ),
        actio_device=os.getenv("HYPERRAG_TEST_ACTIO_DEVICE", "cuda:0"),
    )

    # 注入 vision_model_func（image_caption 用）
    config.vision_model_func = ImageCaptionFunc(
        ModelConfig(
            base_url=TEST_LLM_BASE_URL,
            api_key=TEST_LLM_API_KEY,
            model=TEST_LLM_MODEL,
            provider="vllm",
        ),
        max_tokens=512,
    )

    # 尝试注入 video_caption_func（video_parse 用）；minio 不存在时保持 None
    try:
        config.video_caption_func = VideoCaptionFunc(
            ModelConfig(
                base_url=TEST_LLM_BASE_URL,
                api_key=TEST_LLM_API_KEY,
                model=TEST_LLM_MODEL,
                provider="vllm",
            ),
            max_tokens=512,
            bucket="viknow",
        )
    except Exception:
        config.video_caption_func = None

    # 确保 task_limits 配置正确，避免 semaphore 容量为 0
    if config.task_limits is None:
        default_limits = get_default_task_limits()
        config.task_limits = {k: TaskLimitConfig(**v) for k, v in default_limits.items()}

    hyperrag = HyperRAGV2(config=config)
    await hyperrag.initialize()
    await _ensure_rw(hyperrag)

    yield hyperrag

    # 清理：关闭所有连接
    await hyperrag.shutdown()


@pytest.fixture
async def test_workspace(shared_hyperrag: HyperRAGV2) -> AsyncGenerator[str, None]:
    """
    为每个测试用例创建唯一的 workspace

    使用方式：
        async def test_something(test_workspace):
            workspace = test_workspace
            # ... 测试代码 ...
            # 测试结束后自动清理
    """
    v2 = shared_hyperrag
    workspace = f"test_ws_{uuid.uuid4().hex[:8]}"

    await v2._get_kg_experiment(workspace)

    yield workspace

    # 清理：删除 workspace 数据（DB + 本地目录）
    try:
        await (await v2._get_kg_experiment(workspace)).clear_workspace()
        # 清理本地目录
        workspace_dir = Path(v2.config.working_dir) / workspace
        if workspace_dir.exists():
            shutil.rmtree(workspace_dir, ignore_errors=True)
        logger.info(f"已清理 workspace: {workspace}")
    except Exception as e:
        logger.warning(f"清理 workspace 失败: {e}")


# =============== 导出工具函数供测试使用 ===============

__all__ = ["_wait_job_completed", "_ensure_rw", "shared_hyperrag", "test_workspace"]
