"""
查询性能对比测试：空闲 vs 索引进行中

目标：
1) 先完成一次文档索引，确保 workspace 中有可检索数据（避免空库导致查询过快/不稳定）。
2) 在“无索引进行中”状态下，20 并发随机 query，共 100 次，记录耗时分布。
3) 启动第二个文档索引，等待其 kg_index 任务进入 running。
4) 在“索引进行中”状态下，再做一轮 20 并发随机 query，共 100 次，记录耗时分布。
5) 对比两组的平均值、P95、最大值。

说明：
- 使用真实 Redis / Postgres / Neo4j / LLM / Embedding，不使用 mock。
- 该用例是 integration 测试，运行时间可能较长。
"""

from __future__ import annotations

import asyncio
import math
import os
import random
import statistics
import sys
import time
from pathlib import Path
from typing import List, Tuple

import pytest
from loguru import logger

from hyperrag.hyperrag_v2 import HyperRAGV2

# 使用统一的集成测试 fixtures
from .conftest import _wait_job_completed


# =============== 测试配置（真实服务） ===============

# 确保 subprocess 能找到当前 venv 的 CLI（例如 mineru）。
# 说明：直接用 `.venv/bin/python -m pytest` 运行时，PATH 不一定包含 `.venv/bin`。
_venv_bin = str(Path(sys.executable).parent)
if _venv_bin not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _venv_bin + os.pathsep + os.environ.get("PATH", "")


# =============== 工具函数 ===============


def _metrics(latencies_s: List[float]) -> Tuple[float, float, float]:
    """返回 (avg, p95, max)，单位秒。"""
    assert latencies_s, "latencies_s 不能为空"
    xs = sorted(latencies_s)
    avg = float(statistics.mean(xs))
    p95_idx = max(0, math.ceil(0.95 * len(xs)) - 1)
    p95 = float(xs[p95_idx])
    mx = float(xs[-1])
    return avg, p95, mx


async def _run_queries(
    hyperrag: HyperRAGV2,
    workspace: str,
    *,
    total: int = 100,
    concurrency: int = 20,
    seed: int = 42,
    mode: str = "hybrid",
) -> List[float]:
    questions = [
        "这份文档讲了什么？",
        "请给出一个简短总结。",
        "文档的核心概念有哪些？",
        "列出关键术语并解释。",
        "这个文档解决了什么问题？",
        "有哪些重要结论？",
        "请提取主要观点。",
        "文档提到了哪些组件？",
        "有哪些潜在风险或限制？",
        "请用一句话概括。",
    ]
    rnd = random.Random(seed)
    sem = asyncio.Semaphore(concurrency)

    async def _one(i: int) -> float:
        q = rnd.choice(questions)
        async with sem:
            start = time.perf_counter()
            _ = await hyperrag.aquery(workspace=workspace, question=q, mode=mode)
            return time.perf_counter() - start

    tasks = [asyncio.create_task(_one(i)) for i in range(total)]
    latencies = await asyncio.gather(*tasks)
    return list(latencies)


async def _wait_kg_index_running(hyperrag: HyperRAGV2, job_id: str, timeout_s: int):
    poll_interval = 2
    elapsed = 0
    while elapsed < timeout_s:
        job_status = await hyperrag.aget_job_status(job_id)
        if job_status:
            # 先找出 kg_index task
            for tid in job_status.get("task_ids", []) or []:
                t = await hyperrag.scheduler.get_task_status(tid)
                if not t:
                    continue
                if t.get("task_type") != "kg_index":
                    continue
                t_status = str(t.get("status", "")).lower()
                if t_status == "running":
                    return t
                if t_status in {"failed", "completed"}:
                    raise RuntimeError(
                        f"kg_index 未进入 running 就结束: status={t_status}, task_id={tid}, job_id={job_id}"
                    )
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    raise TimeoutError(f"等待 kg_index running 超时: job_id={job_id}, timeout_s={timeout_s}")


# =============== 主测试 ===============


@pytest.mark.asyncio
@pytest.mark.integration
async def test_query_perf_compare_idle_vs_indexing(
    shared_hyperrag: HyperRAGV2, test_workspace: str
):
    """
    20 并发随机查询 100 次（空闲） vs 20 并发随机查询 100 次（索引进行中）
    输出 avg / p95 / max（秒）。
    """
    hyperrag = shared_hyperrag
    workspace = test_workspace

    test_pdf = Path(__file__).parent / "data" / "mini_lam.pdf"
    assert test_pdf.exists(), f"测试文档不存在: {test_pdf}"

    try:
        # 1) 先完成一次索引（不计入对比），确保查询有数据
        warmup_job_id = await hyperrag.aadd_asset(
            workspace=workspace,
            asset_id="mini_lam",
            asset_path=str(test_pdf),
            params={"language": "zh"},
        )
        logger.info(f"Warmup 索引启动: job_id={warmup_job_id}")
        await _wait_job_completed(hyperrag, warmup_job_id, timeout_s=1200)
        logger.info("Warmup 索引完成")

        # 2) 空闲期查询
        logger.info("开始：空闲期 20 并发 query * 100")
        idle_lat = await _run_queries(
            hyperrag, workspace, total=100, concurrency=20, seed=42, mode="hybrid"
        )
        assert len(idle_lat) == 100
        idle_avg, idle_p95, idle_max = _metrics(idle_lat)
        logger.info(f"[Idle] avg={idle_avg:.3f}s, p95={idle_p95:.3f}s, max={idle_max:.3f}s")

        # 3) 启动第二次索引，并等待 kg_index running
        ingest_pdf = Path(__file__).parent / "data" / "lam.pdf"
        assert ingest_pdf.exists(), f"测试文档不存在: {ingest_pdf}"
        ingest_job_id = await hyperrag.aadd_asset(
            workspace=workspace,
            asset_id="lam",
            asset_path=str(ingest_pdf),
            params={"language": "zh"},
        )
        logger.info(f"第二次索引启动: job_id={ingest_job_id}，等待 kg_index=running")
        _ = await _wait_kg_index_running(hyperrag, ingest_job_id, timeout_s=600)
        logger.info("检测到 kg_index 进入 running，开始索引进行中查询压测")

        # 4) 索引进行中查询
        logger.info("开始：索引进行中 20 并发 query * 100")
        indexing_lat = await _run_queries(
            hyperrag, workspace, total=100, concurrency=20, seed=43, mode="hybrid"
        )
        assert len(indexing_lat) == 100
        idx_avg, idx_p95, idx_max = _metrics(indexing_lat)
        logger.info(f"[Indexing] avg={idx_avg:.3f}s, p95={idx_p95:.3f}s, max={idx_max:.3f}s")

        logger.info(
            "对比结果（Indexing / Idle）："
            f" avg={idx_avg / idle_avg:.2f}x,"
            f" p95={idx_p95 / idle_p95:.2f}x,"
            f" max={idx_max / idle_max:.2f}x"
        )

        # 5) 等索引收尾（避免后台任务影响后续测试/fixture shutdown）
        logger.info("等待第二次索引完成（用于收尾）")
        await _wait_job_completed(hyperrag, ingest_job_id, timeout_s=1200)
        logger.info("第二次索引完成")

    finally:
        # workspace 清理由 test_workspace fixture 自动处理
        pass
